# -*- coding: utf-8 -*-
"""邮箱通知的配置层：`.env` / 环境变量读取、发信条目解析、收件人算法与脱敏展示。

只读配置（`YIBAN_MAIL_*`）与判定通道状态（ok/broken/off），不发送；发信在
`transport` 层。SMTP 目标地址判据（`check_smtp_host`）复用 `yiban.notify.config`
的不可路由段判据，是该口径在邮件侧的单一实现，供 web 设置页与库层共用。
"""
import ipaddress
import json
import logging
import os

from yiban.infra import account_crypto, env_io
from yiban.notify import config as notify_config

logger = logging.getLogger("mailer")

_PREFIX = "YIBAN_MAIL_"

# SMTP_PORT 配置无效时的「回退 465」一次性告警：静默回退会让填错端口的用户在设置页
# 看到 465 而误以为配置正确，排查困难。
_port_warned = False

# 允许私网/回环 SMTP 目标的显式开关。取值口径与项目其它开关一致（1/true/on/yes，
# 大小写不敏感），未设或其它值一律为假。
_ALLOW_PRIVATE_KEY = "YIBAN_MAIL_ALLOW_PRIVATE_HOST"
_TRUTHY_LITERALS = ("1", "true", "on", "yes")

# 可经显式开关放行的「私网/回环」段：RFC1918 三段 + 127.0.0.0/8 + IPv6 环回 ::1。
# 其余不可路由/保留段（链路本地、CGNAT、组播、保留、未指定、site-local 等）无论如何
# 都拒——把安全开关的放行面收窄到"确属自建内网/本机"的场景。
_PRIVATE_TOGGLE_NETS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
)
_LOOPBACK_V6 = ipaddress.ip_address("::1")


def _allow_private_host(env=None):
    """`YIBAN_MAIL_ALLOW_PRIVATE_HOST` 真值判定（1/true/on/yes，大小写不敏感）。

    env：测试注入用的映射；给出时只读它（不回落 .env），缺省读进程环境变量并回落
    `.env`——口径与 `_get` 一致：web 写入 .env，cron 子进程未必带该键。
    """
    if env is not None:
        return str(env.get(_ALLOW_PRIVATE_KEY, "")).strip().lower() in _TRUTHY_LITERALS
    value = (os.environ.get(_ALLOW_PRIVATE_KEY, "").strip()
             or _read_env_file().get(_ALLOW_PRIVATE_KEY, "").strip())
    return value.lower() in _TRUTHY_LITERALS


def _is_toggleable_private(ip):
    """是否属于"可用显式开关放行"的私网/回环段（RFC1918 + 127/8 + ::1）。

    `notify_config._is_nonroutable_target` 把私网/回环与链路本地、保留段一并判为不可
    路由；邮件侧需要把前者（可开关放行）与后者（一律拒）区分开，故类别在这里单独判定，
    不改变 notify 侧语义。
    """
    if ip.version == 4:
        return any(ip in net for net in _PRIVATE_TOGGLE_NETS)
    return ip == _LOOPBACK_V6


def check_smtp_host(host, env=None):
    """SMTP 目标地址判据：返回拒绝原因（中文文案）或 None（放行）。

    判据三档：
    1. 链路本地 / CGNAT / 组播 / 保留 / 未指定 / site-local 等不可路由段一律拒；
    2. RFC1918 私网、127.0.0.0/8、::1 默认拒，`YIBAN_MAIL_ALLOW_PRIVATE_HOST` 为真时放行；
    3. 域名与其余公网 IPv4/IPv6 放行（DNS rebinding 由发送超时兜底，不做连接期复检）。

    `localhost`、非标准 IPv4 字面量（`2130706433`、`0x7f000001`、`127.1` 等）与回环同档；
    方括号 IPv6（`[::1]`、`[::1]:465`）先去括号再解析；IPv4-mapped IPv6 按其映射的 v4 判。
    web 设置页的写侧硬拦与库层告警共用本函数，避免两处各写一遍判据。
    """
    raw = str(host or "").strip()
    if not raw:
        return "SMTP 主机为空"
    candidate = raw
    if candidate.startswith("["):
        end = candidate.find("]")
        if end < 0:
            return "IPv6 地址方括号不闭合"
        candidate = candidate[1:end].strip()
    # 尾点（FQDN 根）剥掉，否则 `127.0.0.1.` 会绕过 ip_address 解析被当成域名放行
    candidate = candidate.rstrip(".").lower()
    if not candidate:
        return "SMTP 主机为空"
    if candidate == "localhost":
        return "目标为本机回环名，属内网地址"
    try:
        ip = ipaddress.ip_address(candidate)
    except ValueError:
        if notify_config._is_ipv4_literal_like(candidate):
            return "目标为非标准 IPv4 字面量（疑似回环/内网写法），已拒绝"
        return None  # 真域名放行
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        # `[::ffff:100.100.100.200]` 形态：v6 对象自身属性全 False，须按映射的 v4 判
        ip = mapped
    if not notify_config._is_nonroutable_target(ip):
        return None
    if _is_toggleable_private(ip):
        if _allow_private_host(env):
            return None
        return "目标为内网/回环地址，如需保留请开启 YIBAN_MAIL_ALLOW_PRIVATE_HOST"
    return "目标为链路本地/CGNAT/组播/保留等不可路由地址，不允许作为 SMTP 目标"


def _read_env_file():
    """读取 .env 键值；文件不存在/读失败返回空 dict（宽松策略）。

    YIBAN_ENV_FILE 指定路径（与 web/signin 子进程约定一致），回退默认 .env。
    解析实现单一来源见 env_io.parse_env_file。
    """
    return env_io.parse_env_file(env_io.env_path())


def _get(key):
    """读取配置：环境变量优先，回退 .env 文件（与 web/app.py send_notification 惯例一致）。"""
    return os.environ.get(_PREFIX + key, "").strip() or _read_env_file().get(_PREFIX + key, "").strip()


def _mask_addr(addr):
    """邮箱打码（保留域名；非邮箱原样返回）。**逗号列表逐项打码**。

    口径：用户名 >6 位保留前 3 位，否则只保留第 1 位；星号数 = max(3,
    用户名长度 - 可见位数)，3 星下限兜底（短名打码段总宽可能略宽于原用户名）。
    固定保留前 3 位会让短名几乎全暴露（ab@x.com → ab***@x.com），故短名只留 1 位。

    `YIBAN_MAIL_ADMIN_TO` 的语义本就是逗号分隔多地址（`admin_recipients` 按逗号拆），
    而"按第一个 @ 切分、其余整段当域名"会把第 2 个及之后的地址**原样**留在返回值里
    ——收件人名单因此明文进 HTTP 响应、推送正文与审计 detail，故逐项处理。
    """
    raw = str(addr or "").strip()
    if not raw:
        return "<未配置>"
    if "," in raw:
        return ",".join(_mask_addr(one) for one in raw.split(",") if one.strip())
    if "@" not in raw:
        return raw
    name, _, domain = raw.partition("@")
    visible = name[:3] if len(name) > 6 else name[:1]
    return visible + "*" * max(3, len(name) - len(visible)) + "@" + domain


def _smtps_enc_decrypts_to_list():
    """SMTPS_ENC 密文能否解出一个 JSON 列表（内容不限，元素有效性由 smtp_list 过滤）。

    供 smtp_channel_state 区分「解密成功但无有效条目」（设置页允许 smtps: [] 的合法
    清空/未配置）与「密文解不开」（换钥失配等真故障）——两者 smtp_list 都返回空列表，
    病因却完全不同。
    """
    try:
        entry = json.loads(_get("SMTPS_ENC"))
        plain = account_crypto.decrypt_text(entry, account_crypto.load_key(env_io.env_path()))
        return isinstance(json.loads(plain), list)
    except (ValueError, OSError, TypeError):
        return False


def smtp_list():
    """SMTP 发信条目列表（主备 failover，按列表顺序逐条尝试）。

    优先读 YIBAN_MAIL_SMTPS_ENC（web 设置页写入：明文 JSON 列表经
    account_crypto.encrypt_text 加密，密文对象与 webhook 密钥同口径、AAD 固定
    yiban-notify，落盘为 json.dumps 字符串）；解密/解析异常只记 warning 并回落旧键，
    绝不抛出（发信配置损坏不能拖累主流程）。
    旧单条键（SMTP_HOST/SMTP_PORT/USER/PASS/ADMIN_TO）仍兼容：USER 与 PASS
    均非空时返回单元素列表，否则 []（未配置）。
    """
    raw = _get("SMTPS_ENC")
    if raw:
        try:
            # 密钥路径口径与 _read_env_file 一致（YIBAN_ENV_FILE 优先）。不能省路径：
            # 不带路径会回落 cwd/.env，容器部署下解错钥致通道静默死亡。
            entry = json.loads(raw)
            plain = account_crypto.decrypt_text(entry, account_crypto.load_key(env_io.env_path()))
            items = json.loads(plain)
            if isinstance(items, list):
                # 非 dict 元素过滤：脏数据不让调用方的 e.get(...) 崩溃
                return [e for e in items if isinstance(e, dict)]
            logger.warning("YIBAN_MAIL_SMTPS_ENC 解密结果不是列表，回落旧单条键")
        except (ValueError, OSError, TypeError) as e:
            # ValueError：json 解析/密文格式/密钥不匹配；OSError：密钥文件读失败
            logger.warning("YIBAN_MAIL_SMTPS_ENC 解密失败（%s），回落旧单条键", type(e).__name__)
    if _get("USER") and _get("PASS"):
        try:
            port = int(_get("SMTP_PORT") or "465")
        except ValueError:
            port = 465
        return [{
            "host": _get("SMTP_HOST") or "smtp.qq.com",
            "port": port,
            "user": _get("USER"),
            "pass": _get("PASS"),
            "admin_to": _get("ADMIN_TO"),
        }]
    return []


def get_config():
    """读取邮件配置概览（脱敏：不含授权码，发件地址打码），供设置页/日志展示。

    port_fallback：SMTP_PORT 缺省或非法时为 True——展示层据此提示「端口未按预期生效，
    实际按 465 处理」，不再无声回退。
    """
    global _port_warned
    raw_port = _get("SMTP_PORT")
    try:
        port = int(raw_port or "465")
        port_fallback = not raw_port.strip()
    except ValueError:
        port = 465
        port_fallback = True
        if raw_port and not _port_warned:
            _port_warned = True
            logger.warning(
                "YIBAN_MAIL_SMTP_PORT=%r 不是合法端口，已回退 465；请修正 .env",
                raw_port,
            )
    entries = smtp_list()
    return {
        "enable": _get("ENABLE"),
        "host": _get("SMTP_HOST") or "smtp.qq.com",
        "port": port,
        "port_fallback": port_fallback,
        # user 打码显示第一主条目（SMTPS_ENC 配置时旧键 USER 可能为空）
        "user": _mask_addr(entries[0].get("user")) if entries else _mask_addr(_get("USER")),
        "admin_to": _mask_addr(_get("ADMIN_TO")),
        "admin_notify": admin_notify_enabled(),
    }


def smtp_channel_state():
    """邮件发信通道三态判定：返回 (state, detail)。

    state：ok=已开启且有真正可发信的条目；broken=已开启但一封都发不出去
    （未配置条目 / 条目缺发件账号或授权码 / 密文解不开且旧键也为空）；
    off=总开关未开启。detail 为人话病因，供健康日报展示。

    需要三态而非"smtp_list 非空"：条目结构性残缺（如 {"host":"x"} 缺 user/pass，
    每封必败）与"密文解不开回落空旧键"两种病态同样非空或被当作正常。is_enabled() 与
    本函数单源（只有 ok 算启用）；调用方需要区分「没配置 / 坏了 / 可用」时用它。

    刻意保留：密文解不开但旧单条键可用时按 ok 报（沿用旧键发信确实可达），解密失败的
    WARNING 由 smtp_list 记录；不把"密文废但旧键活"误报成坏。
    """
    if _get("ENABLE").strip().lower() not in ("1", "true", "on", "yes"):
        return "off", "YIBAN_MAIL_ENABLE 未开启"
    entries = smtp_list()
    if _get("SMTPS_ENC"):
        # 密文存在但条目为空 = smtp_list 已回落旧键仍空。两种病因分开报：解密成功但无
        # 有效条目（含合法清空）只该补条目；解不开才指向密钥失配，否则一次合法的
        # smtps: [] 清空会把 ops 引去排查换钥问题
        if not entries:
            if _smtps_enc_decrypts_to_list():
                return "broken", ("YIBAN_MAIL_SMTPS_ENC 解密成功但无有效发信条目"
                                  "（未配置或已清空，请在设置页补齐 SMTP 列表）")
            return "broken", ("YIBAN_MAIL_SMTPS_ENC 无法解密或已损坏（换钥后密钥不匹配？），"
                              "旧单条键也未配置")
    elif not entries:
        return "broken", ("未配置 SMTP 发信条目（请在设置页补齐 SMTP 列表或旧键 "
                          "YIBAN_MAIL_USER / YIBAN_MAIL_PASS）")
    if not any(str(e.get("user") or "").strip() and str(e.get("pass") or "")
               for e in entries):
        return "broken", "SMTP 条目缺发件账号/授权码"
    return "ok", f"{len(entries)} 条发信条目"


def is_enabled():
    """邮件通知是否启用：仅当通道状态为 ok（开关开且发信条目真正可用）。

    与 smtp_channel_state 单源，只有 ok 算启用——否则条目缺 user/pass、或密文解不开且
    旧键也为空时会被报成启用，日报显示「已开启」而实际一封都发不出去。
    """
    return smtp_channel_state()[0] == "ok"


def admin_recipients():
    """YIBAN_MAIL_ADMIN_TO 拆分为邮箱列表（未配置返回空列表）。

    供调用方按收件人个人开关（users.mail_notify）过滤后回传给 send_admin_alert。
    """
    return [a.strip() for a in _get("ADMIN_TO").split(",") if a.strip()]


def admin_notify_enabled():
    """主管理员是否接收发到 ADMIN_TO 的告警邮件（YIBAN_MAIL_ADMIN_NOTIFY，默认接收）。

    关闭（0）后：ADMIN_TO 列表不再收到 A 线告警邮件，但普通管理员自动收件人
    （users.role=admin 且 mail_notify=1）不受影响。空值视为接收。
    """
    return _get("ADMIN_NOTIFY").strip().lower() in ("", "1", "true", "on", "yes")
