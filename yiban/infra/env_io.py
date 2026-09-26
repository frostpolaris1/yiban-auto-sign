# SPDX-License-Identifier: AGPL-3.0-only
"""**功能**
单一 .env 解析与单键写入实现（mailer / notify / account_crypto / db 共用）。

读的一半是 `parse_env_file`；写的一半是 `write_env_key`——读-改-写里"保留其他行、
只替换目标键"的那套行模型（窄行、逐行行分隔符校验、键行折叠、原子 0600 替换）只有
这一份，密钥（account_crypto）/ 审计密钥（audit_chain）/ 追踪盐（tracking）三处共用，
避免各自再抄一份而口径漂移。

解析口径：utf-8-sig 兼容 BOM（Windows 记事本等工具保存常见，否则首个键名带
\\ufeff 前缀导致读不到）；忽略空行与 # 注释行；按首个 = 切分，键值两侧 strip；
无 = 的行跳过。

**归属**
`yiban.infra` 的基础设施层（无项目内依赖），是全项目 `.env` 读写的**唯一实现**；
`web/services/env_io.py` 是它在 web 侧的服务包装，不另立第二套行模型。

**复用**
`parse_env_file`（含 `env_path`）与 `write_env_key` / `write_env_keys` 由
`yiban.infra.account_crypto`、`yiban.store.audit_chain`、`yiban.store.tracking`、
`yiban.mail.config`、`yiban.notify.config`、`web/services/env_io.py`、
`web/routes/settings_api.py`、`scripts/loadtest/seed_accounts.py` 复用；**单一行模型 + 单一校验器**=`split_env_lines`（窄行）/
`env_key_values` / `validate_env_key` / `validate_env_value` / `validate_env_updates` /
`render_env_write`，`write_env_keys` 是唯一写入口（内部做写入前后"键集合 diff"、
越权即回滚+审计+抛 `EnvWriteRefused`）。读侧判定 `has_line_break` / `is_valid_env_key`
/ `key_line_pattern` / `count_key_lines` / `find_env_key_collisions` 与写入口共用同一套
行模型；`resolve_path` 是路径类配置（STATE_DIR / LOG_FILE / DB_FILE）的解析口径，
`web/app.py`、`yiban.cred_state`、`yiban.engine.alerts`、`yiban.engine.cli_support`、
`scripts/ledger_check.py` 共用。

**通信**
输入：`.env` 路径、键名与值、`strict` 错误策略、解析/写入的调用方参数。
输出：解析后的 dict 或写回后的 `.env` 文件（原子 0600 替换）。
各调用方的解析口径完全一致，差异仅在错误策略，由 `strict` 参数表达：
- 宽松（默认）：任何 OSError（含文件不存在）→ 返回空 dict，调用方走"未配置"分支；
- 严格（strict=True）：文件不存在 → 空 dict；文件存在但读取失败（权限/占用等）
  → 直接抛出 OSError，由调用方记日志并失败。

严格模式的理由（勿简化掉）：密钥/审计盐的自动生成路径若把"读失败"误判为"未配置"，
会静默生成新钥覆盖旧钥，致存量密文与审计链永久不可解——宁可启动失败也不生成替代密钥。

调用谁：仅标准库（`os` / `re` / `secrets` / `contextlib`）。
谁调用：`yiban.engine.*`、`yiban.store.*`、`yiban.infra.account_crypto`、
`web/services/env_io.py` 与设置页写入路径。
前端调用点：系统设置页 `/api/settings`（`web/static/js/components/settings-*.js`）的开关落盘经
web 服务层走本模块——行模型或 `strict` 口径变化会影响设置保存与密钥/盐的生成。

`child_env.parse_env_file` 不在此收敛：它有额外语义（仅接受 YIBAN_ 前缀且键名
合法的行，供子进程环境注入，防 .env 被写入特殊键后污染子进程），保持独立实现。
"""
import contextlib
import os
import re
import secrets


def parse_env_file(path, *, strict=False):
    """解析 .env 全部键值，返回 dict。strict 语义见模块 docstring。

    **`export KEY=v` 行为设计上的分叉，勿"归一"**：三个 .env 读取方对它各有不同处理，
    但对**任何真实键的取值**恒一致（KEY 一律读不到，export 行永不生效）——
      · 本实现（读侧宽松）：按首个 `=` 切，键名是整串 `"export KEY"`（含空格，
        不匹配任何 `get("KEY")`，惰性挂在一个畸形键下）；写侧折叠 `key_line_pattern`
        同样不认它，故该行原样驻留、被后续任何 `get("真实键")` 无视；
      · `scripts/child_env.py`（YIBAN_ 前缀 + 合法键名双白名单）：直接丢弃；
      · `run.sh` / `run_probe.sh`（键名 `^[A-Za-z_]` 校验）：告警后跳过。
    若剥掉 `export` 前缀"归一"到本模型，会把历史上一向未生效的行扶正成生效配置
    （改既有文件读取结果；行内若藏 `GLOBAL_PAUSE`/`ADMIN_PASSWORD_HASH` 即把潜伏
    载荷实体化成提权面）——正撞"键语义不变"红线，故保留三态、以双向测试钉住
    "真实键可读到、export 键读不到"这一收敛不变量（见 tests/test_env_export_line_divergence.py）。
    """
    result = {}
    try:
        with open(path, encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    result[key.strip()] = value.strip()
    except FileNotFoundError:
        pass  # 文件确实不存在 → 空配置（调用方据此走"未配置"分支）
    except OSError:
        if strict:
            raise
        return {}
    return result


def env_path(default=".env"):
    """解析 .env 路径的统一口径：环境变量 YIBAN_ENV_FILE 优先（去空白），回退默认值。

    与 web/signin 子进程约定一致（web/app.py 写入 YIBAN_ENV_FILE 传给子进程）。
    """
    return os.environ.get("YIBAN_ENV_FILE", "").strip() or default


def resolve_path(key, default, *, env=None, env_file=None):
    """路径类配置的唯一解析口径：**进程环境 → .env 文件 → 默认值**。

    为什么必须读 .env：`YIBAN_STATE_DIR` / `YIBAN_LOG_FILE` 这类部署路径过去只在进程
    环境里找（`os.environ.get(...)`）。而 README 与 `.env.example` 教的是把配置写进
    `.env`——于是同一条配置里，"写进 .env"对跑 `run.sh` 的那条路有效（脚本自己 export），
    对 **web 进程**与**直接调用的脚本**无效：它们静默回落到 `/var/log/yiban`。后果不只是
    "配置没生效"：同一台机器上跑第二份部署时，两份会往同一个 `/var/log/yiban` 写状态文件、
    锁与磁盘外锚点，互相污染对方的取证基线。

    env / env_file 参数供测试注入；缺省读进程环境与 `env_path()`。
    """
    raw = (os.environ if env is None else env).get(key, "")
    if not str(raw).strip():
        try:
            raw = parse_env_file(env_path() if env_file is None else env_file).get(key, "")
        except OSError:
            raw = ""
    return str(raw).strip() or default


# ---------------------------------------------------------------------------
# .env 行模型工具：读的一半 parse_env_file 在上，写的一半在此收敛
# ---------------------------------------------------------------------------
# 读-改-写 .env 的全部写入方（web.app.write_env_batch、account_crypto / db /
# signin 各自的写键实现）用的是同一套宽行模型：
#   f.read().splitlines() + "\n".join(...)
# 而 str.splitlines() 除了 \n \r 还把 \v \f \x1c \x1d \x1e \x85 \u2028 \u2029
# 当行边界。校验若只挡 \n \r，含后 8 个字符的键/值就能过检，作为**潜伏分隔符**
# 留在同一条物理行里；下一次任何读-改-写把 splitlines() 拆出的第二行用
# "\n".join() 拼成真配置行——parse_env_file 按文件顺序建 dict 且后写覆盖先写，
# 载荷就此实体化生效（例如注入出一条 `YIBAN_ADMIN_PASSWORD_HASH=` 顶掉主管理员哈希）。
# 不变量分两半：新注入在写入口被 has_line_break 拦下（校验行模型 = 写入行模型）；
# 升级前已埋下的潜伏载荷不会被回溯改写——由 find_env_key_collisions 在启动时
# 报告、运维手工清理。这些判定放在本模块（而非 web）：读的一半本就在此，
# 各写入方都能直接 import，无需反向依赖 web 造成循环。
ENV_LINE_BREAK_CHARS = frozenset("\n\r\v\f\x1c\x1d\x1e\u0085\u2028\u2029")
# 上面这 10 个字符是**实测**出来的，不是照抄文档：遍历全部 Unicode 码位，
# `len(("A" + chr(c) + "B").splitlines()) > 1` 恰好命中这 10 个
# （U+000A U+000B U+000C U+000D U+001C U+001D U+001E U+0085 U+2028 U+2029）。
# 修的是"校验行模型 ≠ 写入行模型"：只要校验漏掉其中任一字符，含它的键/值就能过检、
# 作为潜伏分隔符留下，下一次宽模型读-改-写把它实体化成真配置行。
#
# **容忍边界（关键约束，勿删）**：窄行模型只用于**写入/校验**路径。读取路径
# （`parse_env_file` 的文件迭代 / `env_key_values` 的窄拆分）对历史文件里**已经存在**
# 的换行族字符**容忍读取**——现网文件可能已被旧版本写脏，"读不了就整站瘫"是不可接受的
# 后果。因此宽/窄模型的分歧只在写入口 fail-closed 拒绝，绝不回溯改写存量文件
# （静默归一化会连带改动其他键的存量值）；存量歧义由 `find_env_key_collisions` 只读报告。
# 配置键名字符集（is_valid_env_key 用）：大写字母开头 + 大写字母/数字/下划线
_ENV_KEY_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
# 单条配置值长度上限（字符）：只为拦异常输入（把 .env 撑爆、拖慢逐请求读取），
# 远大于现有最长值（执行体清单 JSON、200 字公告、64 位十六进制密钥）。
ENV_VALUE_MAX_LEN = 65536


class EnvWriteRefused(ValueError):
    """写入会改变本次未请求的键 —— fail-closed 拒绝（已回滚 + 已审计）。

    基类刻意保持 `ValueError`：既有调用方（account_crypto / tracking / audit_chain）
    只 `except ValueError`，换更宽或更窄的类型会从它们的 except 缝里漏出去。
    """


def has_line_break(s):
    """s 是否含任何被 str.splitlines() 当作行边界的字符（= 能把一行撑成两行配置）。

    唯一的"会不会注入出一行配置"判据：write_env_batch 的兜底硬校验与各路由的
    友好前置校验都调它，防两处字符集再次各自漂移。非字符串入参按 str 处理
    （调用方传的都是已 str() 的文本）。
    """
    return not ENV_LINE_BREAK_CHARS.isdisjoint(str(s))


def is_valid_env_key(key):
    """写入口的键名白名单：`^[A-Z][A-Z0-9_]*$`（本项目所有配置键都在这个形态里）。

    此前 `write_env_batch` 对键名只查"会不会撑出第二行"，没查键名本身——带 `=`、`#`、
    空白、小写或前导数字的键虽过不了行分隔符那关，却能写出**解析口径分歧**的行
    （`parse_env_file` 按首个 `=` 切分、`key_line_pattern` 按"键名+可选空白+="折叠，
    两边对"这是哪一条键"的理解可以不一致，后写覆盖先写就成了提权面）。
    这条白名单顺带把行分隔符也挡死（分隔符不在字符集内）。
    """
    return bool(_ENV_KEY_NAME_RE.match(str(key)))


def key_line_pattern(key):
    """构造"该物理行属于键 key"的正则（键在行首、'=' 前可有空白）。

    parse_env_file 的解析口径是"按首个 = 切分 + 两侧 strip"，故 `KEY = v` 与
    `KEY=v` 是同一个键的配置行——旧行折叠与重复检测都必须认得前者，否则
    `KEY = v` 永远折不掉，积累成一条影子行（后写覆盖先写）。
    整键匹配由相邻的 `\\s*=` 保证：前缀更长的另一个键（YIBAN_MAX_USERS_EXTRA）
    不会被 YIBAN_MAX_USERS 命中。re.escape 只是防御纵深——键名若出现 [A-Z_]
    之外的正则元字符，仍按字面匹配。
    传入已 strip 的行文本即可：行首是 # 的注释行天然不匹配，注释得以保留。
    """
    return re.compile(rf"^{re.escape(str(key))}\s*=")


def split_env_lines(raw):
    """**窄行模型**：只把 `\\n` / `\\r\\n` / `\\r` 当行边界，返回物理行列表。

    这是全项目 `.env` 写入/折叠/校验的**唯一切行实现**（web 与引擎共用）；它与
    `parse_env_file` 的读侧口径逐行等价——文本模式读取做普适换行（`\\r\\n`/`\\r` → `\\n`），
    故"能解析出的键"与"能折叠的行"永远是同一套。结尾换行不构成空配置行（对齐 splitlines）。
    刻意不叫 `splitlines`：`.env` 的写侧不得再用宽模型（差异见 ENV_LINE_BREAK_CHARS 注释）。
    """
    text = str(raw).replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def env_key_values(raw):
    """按窄行模型解析"键 → 值"（与 `parse_env_file` 同口径：strip + 首个 `=` + 后写覆盖先写）。

    用于写入前后的**键集合 diff**：把"这次写盘到底动了哪些键"变成可比对的物证，
    而不是信任写入实现的自述。
    """
    result = {}
    for line in split_env_lines(raw):
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            key, _, value = s.partition("=")
            result[key.strip()] = value.strip()
    return result


def validate_env_key(key):
    """写入口的键名校验（单一实现，web 与引擎共用）：`^[A-Z][A-Z0-9_]*$`。

    刻意保持本项目的大写键形态（POSIX 通用式 `[A-Za-z_][A-Za-z0-9_]*` 的**严格子集**）：
    `.env` 既有键全是这个形态，放开小写等于改动既有键语义（会接受过去一律拒绝的键），
    与"既有键语义不变"的契约冲突；白名单顺带把行分隔符挡死。
    """
    if not is_valid_env_key(key):
        raise ValueError(
            f"配置键名非法（须匹配 ^[A-Z][A-Z0-9_]*$）: {str(key)[:40]!r}")


def validate_env_value(key, value):
    """写入口的值校验（单一实现，web 与引擎共用）：禁换行族 + 长度上限。

    `str(value)`：调用方传的都是文本；非文本按 str 处理（与 `has_line_break` 一致）。
    错误消息带键名（便于定位）但绝不回带值原文——值可能含口令/明文代理串。
    """
    text = str(value)
    if has_line_break(text):
        raise ValueError(
            f"配置值不能包含行分隔符（键 {key}）：会注入出新的配置行，故拒绝写入")
    if len(text) > ENV_VALUE_MAX_LEN:
        raise ValueError(
            f"配置值过长（键 {key}，上限 {ENV_VALUE_MAX_LEN} 字符）")


def validate_env_updates(updates):
    """逐条校验本次要写入的键与值（键→值的存在性/顺序由调用方保证）。"""
    for key in updates:
        validate_env_key(key)
    for key, value in updates.items():
        validate_env_value(key, value)


def _validate_env_lines(lines, env_file):
    """逐物理行校验：任一既有行含换行族字符即整体拒绝。

    为什么连注释行也要过：潜伏载荷最常藏在注释尾部（`# 备注<U+0085>YIBAN_GLOBAL_PAUSE=1`），
    宽模型读到它时会先把后半截拆成"第二行"，任何一次读-改-写都把载荷坐实成真配置行。
    错误消息只给行号（1-based），绝不回带行原文——行原文可能带口令等敏感内容。
    """
    for ln_no, ln in enumerate(lines, start=1):
        if has_line_break(ln):
            raise ValueError(
                f"{env_file} 第 {ln_no} 行（1-based）含潜伏行分隔符（U+2028 等），"
                f"写回会把它后面的内容实体化成新配置行，故拒绝写入；"
                f"请人工清理该行后重试"
            )


def render_env_write(raw, updates, *, env_file="", delete_empty=False):
    """把一次写入渲染成新的 `.env` 全文（校验 + 旧键折叠 + 追加），返回文本。

    行模型、校验、折叠三件事只有这一份：web `write_env_batch` 与引擎 `write_env_keys`
    都调它。`delete_empty` 是**调用方契约**差异而非行模型差异：web 侧空值 = 删键
    （`delete_empty=True`，不追加行），引擎侧保留既有追加语义（写 `KEY=`）。
    校验先于折叠：将被折叠丢弃的旧键行同样要过校验，否则含载荷的旧行会被静默删掉。
    """
    validate_env_updates(updates)
    lines = split_env_lines(raw)
    _validate_env_lines(lines, env_file)
    pats = [key_line_pattern(key) for key in updates]
    out = [ln for ln in lines if not any(p.match(ln.strip()) for p in pats)]
    for key, value in updates.items():
        if value or not delete_empty:
            out.append(f"{key}={value}")
    return "\n".join(out) + "\n"


def count_key_lines(env_path, key):
    """.env 中属于 key 的行数（口径与 key_line_pattern / 写入方折叠同源）。

    刻意按 splitlines()（写入侧的**宽**行模型）而非解析侧的普适换行计数：
    潜伏在单行里的分隔符（见 ENV_LINE_BREAK_CHARS 注释）在宽模型下就已经是
    第二行——"已实体化"与"尚未实体化"两种歧义态都能在这里被抓出来，
    不必等下一次写盘把载荷坐实。
    OSError（含文件缺失）返回 0——调用方必须把 0 当作"未确认"而非"未配置"，
    在凭据判定上 fail-closed（读失败 ≠ 键不存在，与 parse_env_file 的 strict
    口径同一立场）。UnicodeDecodeError 是 ValueError 不是 OSError，不会被这里
    吞掉，原样抛出——损坏的 .env 宁可炸也不 fail-open。
    """
    try:
        with open(env_path, encoding="utf-8-sig") as f:  # utf-8-sig：兼容带 BOM 的 .env
            content = f.read()
    except OSError:
        return 0
    pat = key_line_pattern(key)
    return sum(1 for ln in content.splitlines() if pat.match(ln.strip()))


def find_env_key_collisions(env_path):
    """找出 .env 中行模型有歧义的键：{键: (宽模型行数, 窄模型行数)}，空 dict = 干净。

    检测两类隐患（只报告，绝不改写——静默归一化会连带改动其他键的存量值）：
    - 潜伏分隔符：值里藏着 splitlines 才认的行分隔符（U+2028 等）。窄模型
      （parse_env_file 的文件迭代，普适换行）看不到由此撑出的第二行，
      任何一次读-改-写都会把它实体化成真配置行；
    - 影子重复：同名键占多行（含 `KEY = v` 带空格写法），解析器后写覆盖先写，
      生效值由落盘顺序决定。
    判定：宽模型行数 > 1（影子重复）或宽/窄行数不等（潜伏分隔符）；
    注释行、空行、无 = 的行两侧口径都不计入。
    OSError（含文件缺失）返回空 dict——启动报告不得因读失败把启动炸掉，
    读失败的 fail-closed 由各凭据调用方把关；UnicodeDecodeError 原样抛出。
    """
    try:
        with open(env_path, encoding="utf-8-sig") as f:
            content = f.read()
    except OSError:
        return {}

    def _counts(lines):
        counts = {}
        for ln in lines:
            s = ln.strip()
            if s and not s.startswith("#") and "=" in s:
                k = s.partition("=")[0].strip()
                counts[k] = counts.get(k, 0) + 1
        return counts

    # 宽模型（splitlines）是**刻意**的：它比窄模型多认 8 个分隔符，正因如此才能
    # 看见"尚未实体化"的潜伏载荷。窄模型这一侧走单一实现 split_env_lines（不再
    # 就地重抄一遍 replace/split），保证与写入侧的行模型永远同源。
    wide = _counts(content.splitlines())
    narrow = _counts(split_env_lines(content))
    return {
        k: (wide[k], narrow.get(k, 0))
        for k in wide
        if wide[k] > 1 or wide[k] != narrow.get(k, 0)
    }


def _read_env_text(env_file):
    """读取 .env 全文（utf-8-sig 兼容 BOM）；不存在返回空串。"""
    if not os.path.exists(env_file):
        return ""
    with open(env_file, encoding="utf-8-sig") as f:  # utf-8-sig：兼容带 BOM 的 .env
        return f.read()


def _read_env_bytes(env_file):
    """读取 .env **原始字节**（不剥 BOM、不做普适换行）；文件不存在返回 None。

    回滚路径专用：文本读取（`_read_env_text`）的 utf-8-sig 与普适换行会把 BOM/CRLF
    归一掉，拿文本回滚无法还原 BOM/CRLF 文件的原始字节。返回 None 表达"写入前并无
    此文件"，回滚 = 删除本次新创建的文件，而不是落一个零字节文件假装还原。
    """
    if not os.path.exists(env_file):
        return None
    with open(env_file, "rb") as f:
        return f.read()


def _restore_env_bytes(env_file, data):
    """把回滚快照（原始字节 / None=删除文件）原子落回 .env。

    与 `_atomic_replace_env` 同纪律：tmp 创建即 0600、fsync 后 `os.replace`、失败清 tmp。
    data=None 表示写入前文件并不存在，回滚即 unlink（best-effort，失败由调用方吞）。
    """
    if data is None:
        with contextlib.suppress(OSError):
            os.unlink(env_file)
        return
    tmp = f"{env_file}.tmp{secrets.token_hex(4)}"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, env_file)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    with contextlib.suppress(OSError):
        os.chmod(env_file, 0o600)


def _atomic_replace_env(env_file, text):
    """引擎侧的原子替换（tmp 创建即 0600 → fsync → os.replace → chmod）。

    失败（磁盘满/权限/Windows 上替换目标被占用）时**清掉 tmp** 再原样抛出：tmp 装着
    本次要写入的新密钥/新盐，永久残留即凭据暴露面。
    """
    tmp = f"{env_file}.tmp{secrets.token_hex(4)}"
    try:
        # 创建即 0600——open("w") 在默认 umask 下 0644，写完到 replace 之间（及进程崩溃
        # 残留 tmp 时）密钥/盐对同机其他用户可读
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, env_file)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    with contextlib.suppress(OSError):
        os.chmod(env_file, 0o600)  # 仅属主可读写（Windows 无实际效果，忽略失败）


def _unrequested_key_changes(before, after, requested):
    """本次写入**未请求**却发生变化的键集合（键集合 diff 的判据）。

    判定比"键增删"更宽：同名键的**值**变了、或从"有值"变"无值"，都算"发生变化"——
    未请求的键本应逐字保留，任何变化都是写入实现越权。`requested` 是本次 updates 的键。

    **已知盲区（勿据此以为 diff 能兜住一切）**：`env_key_values` 是"后写覆盖先写"的
    dict，故某个**被请求键**自身的影子重复行（同键多行、或同键行长里潜伏分隔符）在
    本 diff 里不可见——diff 只比对键→最终值的映射，不比对行数。这类歧义由
    `find_env_key_collisions` 在启动时只读报告，不靠本 diff 兜。
    """
    changed = set()
    for key in set(before) | set(after):
        if key in requested:
            continue
        if before.get(key) != after.get(key):
            changed.add(key)
    return changed


def _refuse(audit, code, detail):
    """拒绝写入的统一出口：先落一条 fail-closed 审计，再抛 `EnvWriteRefused`。

    审计失败**不得**把拒绝变成放行：本函数在审计异常时吞掉（记日志）后仍拒绝，
    立场是"写入被拒"而非"审计成功才拒"。detail 只许放键名/行号，绝不带值原文。
    """
    if audit is not None:
        with contextlib.suppress(Exception):
            audit(code, detail)
    raise EnvWriteRefused(f"拒绝写入 {code}：{detail}")


def write_env_keys(env_file, updates, *, write_text=None, audit=None, delete_empty=False):
    """把多条配置一次写入 .env：折叠同键旧行、保留其余行、原子替换为 0600。

    行模型/校验/折叠只有一份（`split_env_lines` / `validate_env_updates` /
    `render_env_write`），web 与引擎都调本函数，两侧拒绝文案因此天然同源。

    生命周期（缺一即留隐藏通道）：
    1. 读入原文 → 渲染新全文（**校验先于折叠**：将被丢弃的旧键行同样要过校验，
       否则含载荷的旧行会被静默删掉）；任一既有行或本次值含换行族字符即拒绝，磁盘零改动；
    2. **写入前**对"原文键集合"与"渲染后键集合"做 diff，本次未请求的键发生变化即拒绝；
    3. 提交（`write_text` 注入 web 的 `_atomic_write` 打桩点；缺省用引擎原子替换）；
    4. **写入后**重读文件再做一次 diff：写入实现若越权改了未请求的键，按**写入前原始
       字节**回滚（`_restore_env_bytes`，BOM/CRLF 原样还原；不依赖注入的 `write_text`）
       + 审计 + 抛 `EnvWriteRefused`。
    第 2 步挡"渲染阶段引入的越权"，第 4 步挡"落盘阶段引入的越权"——两道都做才覆盖
    "写入实现自述可信"这条假设。第 4 步的回滚是**字节级**且无条件成立：快照在写入前
    以二进制读取取得，故即使原文件带 BOM 或 CRLF 也能逐字节还原。

    `delete_empty`（调用方契约，非行模型）：web 侧空值 = 删键；引擎侧缺省保留追加语义。
    `audit(code, detail)` 由调用方注入（web 传 db.audit，引擎启动路径无 actor 可传 None）。

    调用方须自行持有 `env_lock.env_write_lock(env_file)`（跨进程 .env 写互斥）：本函数
    不做加锁，把"写前重读既有值"的判定留在调用方，避免嵌套取锁。
    """
    raw = _read_env_text(env_file)
    # 回滚快照：**二进制**读取（BOM/CRLF 原样保留），文本读取已把它们归一掉，
    # 拿文本回滚无法还原 BOM/CRLF 文件的原字节（见 _read_env_bytes）。
    raw_bytes = _read_env_bytes(env_file)
    before = env_key_values(raw)
    # 调用方入参非法（键名/值含换行族、值过长）：直接拒绝，**不入审计**——这是普通
    # 输入错误（路由层已先行友好校验），不是"文件态歧义/未请求键变化"这类需要留痕的
    # 运行时越权；混进审计只会让正常的 400 刷审计链。此处必须独立先校验一次：若省掉它、
    # 只靠 render_env_write 内部的同名校验，入参错误会被下方 `except ValueError` 当成
    # 文件态留痕（并错报为"潜伏分隔符"）。render 内部那次是给其它调用方的纵深防御，
    # render 的任何入参错误到这里都已被本行挡住，故 try 内必为文件态。
    validate_env_updates(updates)
    try:
        new_text = render_env_write(raw, updates, env_file=env_file,
                                    delete_empty=delete_empty)
    except ValueError as e:
        # 走到这里 = 既有文件行含潜伏分隔符（render 的入参校验已在上一步做过，此处必为
        # 文件态）：这是"一次无关保存会实体化载荷"的现场，必须留痕。
        _refuse(audit, "env_write_rejected", str(e))
    requested = set(updates)
    changed = _unrequested_key_changes(before, env_key_values(new_text), requested)
    if changed:
        _refuse(audit, "unrequested_key_change",
                "未请求的键发生变化: " + ", ".join(sorted(changed))[:160])
    commit = write_text or _atomic_replace_env
    commit(env_file, new_text)
    after = env_key_values(_read_env_text(env_file))
    changed = _unrequested_key_changes(before, after, requested)
    if changed:
        # 写入器越权改了未请求的键：按**写入前原始字节**回滚（含 BOM/CRLF），再拒绝。
        # 回滚不走调用方注入的 commit（它收文本，还原不了原始字节）；best-effort——
        # 回滚自身失败也不能把"未请求的键已变"这件事说成成功。
        with contextlib.suppress(OSError):
            _restore_env_bytes(env_file, raw_bytes)
        _refuse(audit, "unrequested_key_change_after_write",
                "落盘后未请求的键发生变化: " + ", ".join(sorted(changed))[:160])


def write_env_key(env_file, key, value):
    """把单条配置写入 .env：折叠同键旧行、保留其余行、原子替换为 0600。

    单键形态 = `write_env_keys(env_file, {key: value})`；行模型、注入校验与
    折叠口径的单一说明在 `write_env_keys`，本函数不再复述。
    """
    write_env_keys(env_file, {key: value})
