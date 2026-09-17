# -*- coding: utf-8 -*-
"""邮箱通知的配置层：`.env` / 环境变量读取、发信条目解析、收件人算法与脱敏展示。

只读配置（`YIBAN_MAIL_*`）与判定通道状态（ok/broken/off），不发送；发信在
`transport` 层。本层不依赖包内其它子模块。
"""
import json
import logging
import os

from yiban.infra import account_crypto, env_io

logger = logging.getLogger("mailer")

_PREFIX = "YIBAN_MAIL_"

# SMTP_PORT 配置无效时的「回退 465」一次性告警：静默回退会让填错端口的用户在设置页
# 看到 465 而误以为配置正确，排查困难。
_port_warned = False


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
    """邮箱打码（保留域名；非邮箱原样返回）。

    口径：用户名 >6 位保留前 3 位，否则只保留第 1 位；星号数 = max(3,
    用户名长度 - 可见位数)，3 星下限兜底（短名打码段总宽可能略宽于原用户名）。
    固定保留前 3 位会让短名几乎全暴露（ab@x.com → ab***@x.com），故短名只留 1 位。
    """
    addr = str(addr or "").strip()
    if "@" not in addr:
        return addr or "<未配置>"
    name, _, domain = addr.partition("@")
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
