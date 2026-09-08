# SPDX-License-Identifier: AGPL-3.0-only
"""单一 .env 解析实现（mailer / notify / account_crypto / db 共用）。

各历史副本的解析口径完全一致，差异仅在错误策略，由 strict 参数表达：
- 宽松（默认）：任何 OSError（含文件不存在）→ 返回空 dict，调用方走"未配置"分支；
- 严格（strict=True）：文件不存在 → 空 dict；文件存在但读取失败（权限/占用等）
  → 直接抛出 OSError，由调用方记日志并失败。

严格模式的理由（勿简化掉）：密钥/审计盐的自动生成路径若把"读失败"误判为"未配置"，
会静默生成新钥覆盖旧钥，致存量密文与审计链永久不可解——宁可启动失败也不生成替代
密钥（2026-08-27 对抗性审查结论）。

解析口径：utf-8-sig 兼容 BOM（Windows 记事本等工具保存常见，否则首个键名带
\\ufeff 前缀导致读不到）；忽略空行与 # 注释行；按首个 = 切分，键值两侧 strip；
无 = 的行跳过。

child_env.parse_env_file 不在此收敛：它有额外语义（仅接受 YIBAN_ 前缀且键名
合法的行，供子进程环境注入，防 .env 被写入特殊键后污染子进程），保持独立实现。
"""
import os
import re


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
# 载荷就此实体化生效（2026-09-07：普通管理员经公告文本注入
# YIBAN_ADMIN_PASSWORD_HASH 顶掉主管理员哈希提权，已活体复现）。
# 不变量分两半：新注入在写入口被 has_line_break 拦下（校验行模型 = 写入行模型）；
# 升级前已埋下的潜伏载荷不会被回溯改写——由 find_env_key_collisions 在启动时
# 报告、运维手工清理。这些判定放在本模块（而非 web）：读的一半本就在此，
# 各写入方都能直接 import，无需反向依赖 web 造成循环。
ENV_LINE_BREAK_CHARS = frozenset("\n\r\v\f\x1c\x1d\x1e\u0085\u2028\u2029")


def has_line_break(s):
    """s 是否含任何被 str.splitlines() 当作行边界的字符（= 能把一行撑成两行配置）。

    唯一的"会不会注入出一行配置"判据：write_env_batch 的兜底硬校验与各路由的
    友好前置校验都调它，防两处字符集再次各自漂移。非字符串入参按 str 处理
    （调用方传的都是已 str() 的文本）。
    """
    return not ENV_LINE_BREAK_CHARS.isdisjoint(str(s))


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
