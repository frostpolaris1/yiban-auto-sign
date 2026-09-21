# SPDX-License-Identifier: AGPL-3.0-only
"""单一 .env 解析与单键写入实现（mailer / notify / account_crypto / db 共用）。

读的一半是 `parse_env_file`；写的一半是 `write_env_key`——读-改-写里"保留其他行、
只替换目标键"的那套行模型（窄行、逐行行分隔符校验、键行折叠、原子 0600 替换）只有
这一份，密钥（account_crypto）/ 审计密钥（audit_chain）/ 追踪盐（tracking）三处共用，
避免各自再抄一份而口径漂移。

各调用方的解析口径完全一致，差异仅在错误策略，由 strict 参数表达：
- 宽松（默认）：任何 OSError（含文件不存在）→ 返回空 dict，调用方走"未配置"分支；
- 严格（strict=True）：文件不存在 → 空 dict；文件存在但读取失败（权限/占用等）
  → 直接抛出 OSError，由调用方记日志并失败。

严格模式的理由（勿简化掉）：密钥/审计盐的自动生成路径若把"读失败"误判为"未配置"，
会静默生成新钥覆盖旧钥，致存量密文与审计链永久不可解——宁可启动失败也不生成替代密钥。

解析口径：utf-8-sig 兼容 BOM（Windows 记事本等工具保存常见，否则首个键名带
\\ufeff 前缀导致读不到）；忽略空行与 # 注释行；按首个 = 切分，键值两侧 strip；
无 = 的行跳过。

`child_env.parse_env_file` 不在此收敛：它有额外语义（仅接受 YIBAN_ 前缀且键名
合法的行，供子进程环境注入，防 .env 被写入特殊键后污染子进程），保持独立实现。
"""
import contextlib
import os
import re
import secrets


def parse_env_file(path, *, strict=False):
    """解析 .env 全部键值，返回 dict。strict 语义见模块 docstring。"""
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
# rekey_accounts / signin 各自的写键实现）用的是同一套宽行模型：
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
# 配置键名字符集（is_valid_env_key 用）：大写字母开头 + 大写字母/数字/下划线
_ENV_KEY_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


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

    wide = _counts(content.splitlines())
    narrow = _counts(content.replace("\r\n", "\n").replace("\r", "\n").split("\n"))
    return {
        k: (wide[k], narrow.get(k, 0))
        for k in wide
        if wide[k] > 1 or wide[k] != narrow.get(k, 0)
    }


def write_env_key(env_file, key, value):
    """把单条配置写入 .env：折叠同键旧行、保留其余行、原子替换为 0600。

    行模型取**窄行模型**（只把 \\r\\n / \\r / \\n 当行边界），不是 `str.splitlines()`：
    后者额外把 \\v \\f \\x1c \\x1d \\x1e \\x85 \\u2028 \\u2029 当边界。值里潜伏这些字符
    时，窄模型下它仍留在本行内，逐行 `has_line_break` 才看得见；宽模型会先把它拆成两行，
    读-改-写于是把后半截实体化成真配置行（`parse_env_file` 按文件顺序建 dict、后写覆盖
    先写 → 载荷生效）。无潜伏分隔符时两种模型逐行等价，正常 .env 写回结果与宽模型逐字相同。

    校验先于折叠：将被折叠丢弃的旧键行同样要过 `has_line_break`——丢弃同样要先过这一关，
    否则含载荷的旧键行会被静默删掉（报"写入成功"，实则抹掉一行本函数根本没看懂的配置）。
    任一行含潜伏分隔符即 ValueError 且磁盘上一个字节都不改（fail-closed：本函数只保留
    别人的行、没有清理权）。错误消息只给行号（1-based），绝不回带行原文——值里可能有口令
    等敏感内容，会随异常消息落日志与 HTTP 500。

    旧行折叠走 `key_line_pattern`，与 `parse_env_file`"首个 = 切分 + 两侧 strip"的认键
    口径同源：`KEY = v` 这类带空白的写法同样是同一条键的行，只认字面前缀 `KEY=` 折不掉它，
    残留的影子行与新行谁生效由落盘顺序决定。

    调用方须自行持有 `env_lock.env_write_lock(env_file)`（跨进程 .env 写互斥）：本函数
    不做加锁，把"写前重读既有值"的判定留在调用方，避免嵌套取锁。
    """
    raw = ""
    if os.path.exists(env_file):
        with open(env_file, encoding="utf-8-sig") as f:  # utf-8-sig：兼容带 BOM 的 .env
            raw = f.read()
    lines = raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if lines and lines[-1] == "":
        lines.pop()  # 结尾换行不构成空配置行（对齐 splitlines 的行数）
    for ln_no, ln in enumerate(lines, start=1):
        if has_line_break(ln):
            raise ValueError(
                f"{env_file} 第 {ln_no} 行（1-based）含潜伏行分隔符（U+2028 等），"
                f"写回会把它后面的内容实体化成新配置行，故拒绝写入；"
                f"请人工清理该行后重试"
            )
    pat = key_line_pattern(key)
    out = [ln for ln in lines if not pat.match(ln.strip())]
    out.append(f"{key}={value}")
    tmp = f"{env_file}.tmp{secrets.token_hex(4)}"
    # 创建即 0600——open("w") 在默认 umask 下 0644，写完到 replace 之间（及进程崩溃
    # 残留 tmp 时）密钥/盐对同机其他用户可读
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, env_file)
    with contextlib.suppress(OSError):
        os.chmod(env_file, 0o600)  # 仅属主可读写（Windows 无实际效果，忽略失败）
