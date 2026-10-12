# -*- coding: utf-8 -*-
"""Webhook 推送的配置层：读 `.env` / 环境变量、解出密钥、判定通道是否可用。

只做"读配置 + 判定通道可用性"，不发送也不记账。唯一例外是 `get_config` 要展示每日
剩余额度，故在函数内延迟导入 `ledger`——否则 config ↔ ledger 会形成模块级导入环
（ledger 反向依赖本层的 `_env_int` / `_env_str`）。

**通信**
输入：`.env` 里的 `YIBAN_NOTIFY_*`（+ 无 `NOTIFY_` 前缀的
`YIBAN_LOGINFAIL_DAILY_MAX`），**.env 优先、进程环境只补缺**（M27：与写侧同一口径，
否则设置页写入被 web 进程环境静默盖住；解析来自 `env_io.env_path` /
`parse_env_file`）。
它调用：`account_crypto.load_key` / `decrypt_text`（解 `SECRET_ENC` 密文）、
`ledger._daily_limit` / `_daily_remaining`、`ipaddress`。
谁调用：`transport.send` / `send_test`（类型、密钥、白名单）、
`web/routes/notify.py` 的 `api_notify_config` / `api_notify_config_save`（设置页读概览
与保存前判定）、`yiban/engine/alerts.py`（`is_configured` 决定要不要走手机通道）。
输出给前端的凭据只有一种形态：`get_config()` 里的 `secret_masked`（`_mask_secret` 的
前 3 后 2 短指纹）。`get_secret()` 返回的是明文，只被 `transport` 拿去发请求；本层
`logger` 的三处调用记的是解密/解析异常与 `url_desc(host)`，都不写出密钥本身。
"""
import ipaddress
import json
import logging
import os
from urllib.parse import urlparse

from yiban.infra import account_crypto, env_io
from yiban.security import url_desc

logger = logging.getLogger("notify")

_PREFIX = "YIBAN_NOTIFY_"

# 自定义地址的发送期只读复核：进程内只记一次告警。手工编辑 .env 写入的地址不再经设置页
# 校验，加载/发送前复核一次并提示；不改发送行为，异常也绝不逃逸（见 _recheck_custom_url）。
_unsafe_custom_warned = False


DEFAULT_COOLDOWN = 60
DEFAULT_DAILY_MAX = 5
# 紧急告警另开一本独立额度，保证噪声烧完非紧急额度后仍有手机通道
DEFAULT_URGENT_DAILY_MAX = 3
# 「仅推送重要告警」默认开：推送日额度有限（Server酱免费版 5 条/天），默认就该留给
# 安全与系统级告警，日常改密、签到结果类只走邮件。`.env` 写 YIBAN_NOTIFY_URGENT_ONLY=0
# 即显式关闭，恢复"全部告警都推手机"。
# 发送期判定（yiban/notify/transport.py）与上报值（本模块 get_config 的 urgent_only
# 字段）必须取同一个默认常量——两处各写一个字面量，设置页显示与实际行为就会分叉。
DEFAULT_URGENT_ONLY = 1
# 登录失败告警独立账本的日额度默认值；该键无 NOTIFY_ 前缀（独立命名），但读取口径
# （.env 优先、进程环境补缺、非法值回退默认）与其他 notify 键一致
DEFAULT_LOGINFAIL_DAILY_MAX = 3
LOGINFAIL_DAILY_MAX_KEY = "YIBAN_LOGINFAIL_DAILY_MAX"
# "管理员本人操作的回执"类告警（执行体清单变更）的独立日额。刻意只有代码内缺省、
# 不配 env 键：本账的意义是把这类高频可达的管理侧告警从紧急账里摘出来（喷洒者烧光
# 紧急账的守卫不能被一条"改清单回执"顶掉），给管理员再加一个可拨开关不扩大问题面。
DEFAULT_ADMIN_CHANGE_DAILY_MAX = 3
# CGNAT（RFC 6598）：`ipaddress.is_private` 不覆盖，而云厂商元数据服务常落在此段
_CGNAT_V4 = ipaddress.ip_network("100.64.0.0/10")


def _env_path():
    """本模块解析 .env 的唯一口径：YIBAN_ENV_FILE 优先（去空白），否则当前目录 .env。"""
    return env_io.env_path()


def _read_env_file():
    """读取 .env（utf-8-sig 兼容 BOM），供读配置用（宽松策略，实现见 env_io）。"""
    return env_io.parse_env_file(_env_path())


def _env_str(key, envs=None):
    """读取一个 `YIBAN_NOTIFY_*` 键：**.env 文件优先**，进程环境只补缺（M27）。

    **为什么不是"环境变量优先"**：本组件的**写侧**（设置页 `web/routes/notify.py`
    落盘）与 **web 读侧**都是「`.env` 文件优先」。读侧若反过来，键一旦进了 web 进程的
    环境变量，设置页的写入就**静默失效**——管理员在页面上改了配置，读侧仍返回旧值，
    `get_config` 的 GET 回显与磁盘状态长期不一致。口径必须与写侧一致。

    进程环境保留为**兜底**：`.env` 里没有该键时（compose 只注入环境变量的部署形态）
    仍取得到值，不会因为翻优先级把这类部署读成"未配置"。

    envs：调用方本轮已解析好的 .env 快照（`_read_env_file()` 的返回值）。不传则本函数
    自己读文件——一次调用读一遍全文件，故一次取多个键的路径（如 get_config）会把同一
    轮快照传进来复用。
    """
    if envs is None:
        envs = _read_env_file()
    value = envs.get(_PREFIX + key, "").strip()
    if value:
        return value
    return os.environ.get(_PREFIX + key, "").strip()


# 非法值告警的一次性旗标（键名集合）：同一键在进程生命周期内只喊一次，不刷屏
_bad_value_warned = set()
# 布尔开关键的取值口径。**单一事实源**在 `yiban.infra.env_io` 的
# `ENV_TRUTHY_LITERALS` / `ENV_FALSY_LITERALS`（与引擎 `schedule._env_flag`、面板读侧、
# bash `run.sh._is_truthy` 同一套字面量）；本处只引用不再另抄一份。
# 本模块的 `_env_flag` 因契约不同（读 `YIBAN_NOTIFY_*` 键 + 带缺省值 + 与 `_env_int`
# 共用"只喊一次"闩）未并入 `env_io.parse_env_flag`，但**口径同源**——字面量表来自它。
_FLAG_TRUE = env_io.ENV_TRUTHY_LITERALS
_FLAG_FALSE = env_io.ENV_FALSY_LITERALS


def _env_int(key, default, envs=None):
    """读整数键：非法值回退 default；负值同样非法——回退 default 并出声一次。

    旧实现把负值 `max(0, ·)` 钳成 0，可 0 在额度类键上是**有含义的合法值**（"不限额"）、
    在 COOLDOWN 上是"不节流"——`YIBAN_NOTIFY_DAILY_MAX=-1` 就此静默变成"放开上限"
    （MF-44 登记项）。想要哪种放开形态就显式写 0；写负数是笔误，笔误应当出声，
    不该被折叠成最危险的那一档。
    """
    raw = _env_str(key, envs)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    if value < 0:
        if key not in _bad_value_warned:
            _bad_value_warned.add(key)
            logger.warning("YIBAN_NOTIFY_%s=%r 为负值（非法键值），本次按缺省 %d 处理；"
                           "需要放开上限/关闭节流请显式写 0", key, raw, default)
        return default
    return value


def _env_flag(key, default, envs=None):
    """读布尔开关键：1/true/on/yes 开、0/false/off/no 关；其余回退缺省并出声一次。

    治的是「仅推送重要告警」的口径分叉（MF-44）：旧实现在这条开关上走 `_env_int`，
    而 `_env_int` 只认整数——管理员照直觉写 `YIBAN_NOTIFY_URGENT_ONLY=false`，
    ValueError 回退缺省 1 = **想关却静默保持开启**，非紧急告警从此不再推手机。
    开关语义按开关的词汇读，不再按整数读；认不出的写法不猜意图：保持缺省 + 喊一次。
    """
    raw = _env_str(key, envs).strip().lower()
    if raw in _FLAG_TRUE:
        return 1
    if raw in _FLAG_FALSE:
        return 0
    if not raw:
        return default
    if key not in _bad_value_warned:
        _bad_value_warned.add(key)
        logger.warning("YIBAN_NOTIFY_%s=%r 不是可辨认的开关写法，本次按缺省 %d 处理；"
                       "可写 1/true/on/yes 或 0/false/off/no", key, raw, default)
    return default


def _mask_secret(secret):
    """密钥打码：前 3 位 + 后 2 位供辨认，中间**固定**星号。空返回空。

    星数刻意与密钥长度无关：原实现 `"*" * (len - 3)` 把精确长度也发给前端，
    对定长前缀的 sendkey（Server酱 `SCT` + 固定宽度）等于多泄露一个强特征。
    短值（<12 位）不回尾段——3+2 会把六七位的密钥几乎整个露出来，辨认价值没增加、
    泄露却实打实发生。
    """
    if not secret:
        return ""
    s = str(secret)
    if len(s) < 12:
        return s[:2] + "***"
    return s[:3] + "***" + s[-2:]


# ---------------------------------------------------------------------------
# 通道配置与可用性
# ---------------------------------------------------------------------------

def _is_nonroutable_target(ip):
    """该 IP 是否属于"不得作为推送目标"的地址段（SSRF 白名单的唯一判据）。

    `ipaddress.is_private` **不含** 100.64.0.0/10（RFC 6598 CGNAT）——而阿里云
    ECS 元数据服务 `100.100.100.200` 恰在该段，只判 private/link_local 会把
    "拿推送地址当 SSRF 跳板读实例凭据"这条路留着；组播与保留段同理。
    """
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        # `[::ffff:100.100.100.200]` 形态：v6 对象自身属性全 False，按其映射的 v4 判
        return _is_nonroutable_target(mapped)
    return (ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_unspecified
            or ip.is_multicast or ip.is_reserved
            or getattr(ip, "is_site_local", False)
            or (ip.version == 4 and ip in _CGNAT_V4))


def is_safe_url(url):
    """自定义通知地址的 SSRF 白名单：https + 非回环/内网/链路本地/未指定/CGNAT/组播/保留。

    防 http 明文外泄与拿推送地址当 SSRF 跳板。域名目标放行（DNS rebinding 由发送
    超时兜底）。本函数是该口径的唯一实现，web 设置页与发送层共用。

    **白名单外写法收严**：`localhost.`（尾点）、纯数字/十六进制/前导零
    IPv4 字面量（`2130706433` = 127.0.0.1）、短式回环（`127.1`）等非 `ipaddress`
    可解析的 host 一律拒掉——否则 `https://2130706433/hook` 这类地址会直通。
    `[::ffff:127.0.0.1]` 等 IPv6 形式已由 `ipaddress` 拦下。

    **反斜杠收严**：`urlparse` 的 `_hostinfo` 把反斜杠当 userinfo 边界，于是
    `https://127.0.0.1:443\\@example.com/hook` 的 `hostname` 是 `example.com`（放行），
    而 requests/urllib3 的解析不这样切分、实际连的是 `127.0.0.1:443`。故带反斜杠的
    URL 一律拒掉（正常 URL 不含反斜杠，零误杀）。带 userinfo 的地址不在此收严之列：
    `https://evil.com@127.0.0.1/hook` 的 hostname 就是 `127.0.0.1`，已被下面的内网
    判定拦下；`user:pass@host` 形态本身不构成绕过，且是合法的 webhook basic-auth 写法。
    """
    try:
        o = urlparse(url)
    except ValueError:
        return False
    if "\\" in url:
        return False
    if o.scheme != "https" or not o.hostname:
        return False
    host = o.hostname.strip().lower().rstrip(".")
    if host == "localhost":
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        # 非 ipaddress 可解析的 host：若是"纯数字 IPv4 字面量"形态（十进制/0x/
        # 前导零/短式）→ 拒掉；只有真域名（含点号且非全数字组件）放行。
        return not _is_ipv4_literal_like(host)
    return not _is_nonroutable_target(ip)


def _is_ipv4_literal_like(host):
    """host 是否形如 IPv4 字面量（`ipaddress` 解析不了的非标准写法）。

    覆盖：全数字（十进制整数，含 `2130706433`）、`0x` 十六进制（`0x7f000001`）、
    前导零八进制（`0177.0.0.1`）、短式回环（`127.1`，点分但末段缺失）。这些都会在
    连接时被解析为内网/回环地址。前缀/尾点优先于本判定已处理；`host` 已 rstrip(".")。
    含字母（真域名）返回 False。
    """
    if not host:
        return False
    parts = host.split(".")

    def _seg_ok(seg):
        if not seg:
            return False
        if seg.isdigit():
            return True  # 十进制（含前导零：0177 按八进制解析，仍属 IP 字面量）
        low = seg.lower()
        return (low.startswith("0x") and len(seg) > 2 and
                all(c in "0123456789abcdef" for c in low[2:]))

    if len(parts) == 1:
        return _seg_ok(parts[0])
    return len(parts) <= 4 and all(_seg_ok(p) for p in parts)


def _recheck_custom_url(secret, envs=None):
    """启用中的自定义通知地址做一次只读安全复核，不通过只记一次 WARNING。

    手工编辑 .env 写入的地址不会再经设置页校验，本函数在加载/发送前的配置读取路径补
    一次复核：仅当类型为 custom（含未设 TYPE 时旧明文 URL 按 custom 的兼容口径）时判；
    不通过只提示"发送仍会继续、请在设置页更正"，**不改变发送行为**。
    复核本身绝不抛异常（它只是增益，不能拖累告警通道）。
    """
    global _unsafe_custom_warned
    if _unsafe_custom_warned or not secret:
        return
    ntype = _env_str("TYPE", envs).strip().lower() or "custom"
    if ntype != "custom":
        return
    try:
        safe = is_safe_url(secret)
    except Exception:
        safe = False
    if safe:
        return
    _unsafe_custom_warned = True
    logger.warning(
        "自定义通知地址未通过安全复核（非 HTTPS 或指向内网/保留地址），"
        "发送仍会继续；请在设置页更正: %s", url_desc(secret),
    )


def get_secret(envs=None):
    """返回当前加密配置解出的明文密钥（serverchan=SendKey；custom=URL）。

    未配置 / 密文损坏 / 密钥不匹配均返回空（并记日志），绝不抛异常。
    envs：调用方本轮已解析的 .env 快照，省略则自行读取（见 _env_str）。
    """
    enc = _env_str("SECRET_ENC", envs)
    if not enc:
        url = _env_str("URL", envs) or ""  # 兼容旧明文 YIBAN_NOTIFY_URL
        _recheck_custom_url(url, envs)
        return url
    try:
        entry = json.loads(enc)
    except ValueError:
        logger.warning("YIBAN_NOTIFY_SECRET_ENC 解析失败，消息推送不可用")
        return ""
    try:
        # 必须显式传路径：load_key() 不带参数会回落到 cwd/.env，而本模块的密文是按
        # YIBAN_ENV_FILE 读的。容器里 cwd=/app、真实配置在 /data/.env，不带路径会
        # "就地生成一把游离新密钥并写盘"，再用它解本模块读到的密文 → 通道静默死亡，
        # 还额外在镜像工作目录留下密钥文件。
        secret = account_crypto.decrypt_text(entry, account_crypto.load_key(_env_path()))
    except (ValueError, OSError) as e:
        # OSError：密钥文件存在但读不到（权限/占用），按"解不出"处理而非炸主流程
        logger.warning("消息推送密钥解密失败: %s", e)
        return ""
    _recheck_custom_url(secret, envs)
    return secret


def get_config():
    """配置概览（脱敏），供设置页 / 日志展示。"""
    # 每日上限与余额的口径在 ledger 层（要读账本锁与磁盘账本）；此处函数内导入
    # 以免 config → ledger → config 的模块级循环。
    from . import ledger

    # 本轮所有键共用一份 .env 解析结果：逐个键各读一遍全文件，在设置页轮询时并不便宜
    envs = _read_env_file()
    ntype = _env_str("TYPE", envs).strip().lower()
    secret = get_secret(envs)
    if not ntype and secret:
        ntype = "custom"  # 兼容旧明文 YIBAN_NOTIFY_URL
    enabled = bool(ntype and secret)
    # 上限各解析一次，紧接着复用给 daily_max / daily_remaining（否则两者各自再读一遍文件）
    general_max = ledger._daily_limit("general", envs)
    urgent_max = ledger._daily_limit("urgent", envs)
    return {
        "ok": True,
        "enabled": enabled,
        "type": ntype if enabled else "",
        "secret_masked": _mask_secret(secret) if enabled else "",
        "configured": bool(ntype or secret),
        "cooldown": _env_int("COOLDOWN", DEFAULT_COOLDOWN, envs),
        # 开关判据与 transport.send 门②同一份 `_env_flag`：发送判定与上报值若各自
        # 解析，设置页显示与实际行为就会分叉（本文件头注释写明的口径）
        "urgent_only": bool(_env_flag("URGENT_ONLY", DEFAULT_URGENT_ONLY, envs)),
        # daily_* 两字段语义是「非紧急账」（字段名不变，前端与既有调用方无需改），
        # 紧急账并列暴露为 urgent_daily_*
        "daily_max": general_max,
        "daily_remaining": ledger._daily_remaining("general", general_max),
        "urgent_daily_max": urgent_max,
        "urgent_daily_remaining": ledger._daily_remaining("urgent", urgent_max),
    }


#: send() 只会走的出口类型；具名之外的 TYPE 值 send() 拒发（"未知通知类型"），
#: 判据必须与那条 else 同源，否则"出口是否存在"两处各说各话。
_KNOWN_PUSH_TYPES = ("serverchan", "custom")


def channel_usable(ntype, secret):
    """「推送出口真实存在」的唯一判据：有密钥 + 类型已知 +（custom 时）过白名单。

    这是 MF-44 登记的口径分叉的收口：`is_configured()` 曾只看"类型已设且密钥解得出"，
    不看 TYPE 白名单也不看 custom 白名单，而 `send()` 的门①/门⑤遇到未知类型或
    白名单外地址**必然拒发**——引擎侧（`yiban/engine/alerts.py`）据此判"有推送出口"
    就会少发一封本该走的邮件，告警在两个判定都"正常"的地方静默消失。
    判据从 send() 的行为机械导出：未知类型不猜出口、不安全 URL 不放行。
    """
    if not secret:
        return False
    t = str(ntype or "").strip().lower() or "custom"  # 旧明文 URL 兼容口径与 send() 一致
    if t not in _KNOWN_PUSH_TYPES:
        return False
    if t == "custom":
        return bool(is_safe_url(secret))  # 只有 custom 的 URL 本身就是投递目标，须过白名单
    return True


def is_configured():
    """推送通道是否已配置可用：与 send() 的门①+门⑤同一判据（见 `channel_usable`）。

    未配置时 send 必然返回 False，先判定可省一次发送尝试；调用方据此在发送前判断
    「推送出口是否存在」。此前该函数漏了 TYPE 白名单与 custom 白名单两档，与 send()
    分叉——修复动机与判据来源见 `channel_usable` 的文档。
    """
    envs = _read_env_file()
    return channel_usable(_env_str("TYPE", envs), get_secret(envs))
