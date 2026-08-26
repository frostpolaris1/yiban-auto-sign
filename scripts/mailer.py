# -*- coding: utf-8 -*-
"""SMTP 邮箱通知模块（A 线：管理员告警邮件；B 线：用户签到结果邮件）。

通过 .env / 环境变量（YIBAN_MAIL_*）配置，仅用 Python 标准库 smtplib 发送，
零第三方依赖。设计原则：

- 不配置 = 不启用：YIBAN_MAIL_ENABLE 未开启或配置不完整时静默跳过，不影响现有功能；
- 静默失败：发送异常只记日志，绝不抛出（告警/通知失败不能拖累签到主流程）；
- 日志脱敏：授权码、完整发件地址不回显，只记录异常类型与打码后的邮箱。
"""
import logging
import os
import smtplib
from email.header import Header
from email.mime.text import MIMEText

logger = logging.getLogger("mailer")

_PREFIX = "YIBAN_MAIL_"


def _mask_addr(addr):
    """邮箱打码：477929858@qq.com → 477****@qq.com（保留域名；非邮箱原样返回）。"""
    addr = str(addr or "").strip()
    if "@" not in addr:
        return addr or "<未配置>"
    name, _, domain = addr.partition("@")
    visible = name[:3]
    return visible + "*" * max(3, len(name) - len(visible)) + "@" + domain


def _read_env_file():
    """读取 .env 键值（utf-8-sig 兼容 BOM）；文件不存在返回空 dict。

    YIBAN_ENV_FILE 指定路径（与 web/signin 子进程约定一致），回退默认 .env。
    """
    path = os.environ.get("YIBAN_ENV_FILE", "").strip() or ".env"
    result = {}
    try:
        with open(path, encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    result[key.strip()] = value.strip()
    except OSError:
        pass
    return result


def _get(key):
    """读取配置：环境变量优先，回退 .env 文件（与 web/app.py send_notification 惯例一致）。"""
    return os.environ.get(_PREFIX + key, "").strip() or _read_env_file().get(_PREFIX + key, "").strip()


def get_config():
    """读取邮件配置概览（脱敏：不含授权码，发件地址打码），供设置页/日志展示。"""
    try:
        port = int(_get("SMTP_PORT") or "465")
    except ValueError:
        port = 465
    return {
        "enable": _get("ENABLE"),
        "host": _get("SMTP_HOST") or "smtp.qq.com",
        "port": port,
        "user": _mask_addr(_get("USER")),
        "admin_to": _mask_addr(_get("ADMIN_TO")),
        "admin_notify": admin_notify_enabled(),
    }


def is_enabled():
    """邮件通知是否启用：总开关=1 且发件邮箱/授权码均已配置。"""
    if _get("ENABLE").strip().lower() not in ("1", "true", "on", "yes"):
        return False
    return bool(_get("USER") and _get("PASS"))


def _send(subject, text, to):
    """发送一封邮件，静默失败（记日志不抛出）。"""
    to = str(to or "").strip()
    if not to or not is_enabled():
        return False
    host = _get("SMTP_HOST") or "smtp.qq.com"
    try:
        port = int(_get("SMTP_PORT") or "465")
    except ValueError:
        port = 465
    user = _get("USER")
    password = _get("PASS")

    msg = MIMEText(text, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = user
    msg["To"] = to

    try:
        if port == 465:
            server = smtplib.SMTP_SSL(host, port, timeout=15)
        else:
            server = smtplib.SMTP(host, port, timeout=15)
            server.starttls()
        with server:
            server.login(user, password)
            server.sendmail(user, [to], msg.as_string())
        logger.info("邮件通知已发送: %s → %s", subject, _mask_addr(to))
        return True
    except (OSError, smtplib.SMTPException) as e:
        # 不回显授权码与异常详情（异常文本可能包含敏感信息），只记类型与脱敏地址
        logger.warning(
            "邮件通知发送失败（%s）: %s → %s",
            type(e).__name__, subject, _mask_addr(to),
        )
        return False


def send_admin_alert(subject, text, to=None):
    """A 线：管理员告警邮件。收件人 YIBAN_MAIL_ADMIN_TO（逗号分隔支持多地址）。

    to 必须由调用方合成后传入（ADMIN_TO 经收件人个人开关过滤 ∪ 开启接收的
    管理员用户邮箱，见 db.admin_mail_recipients）。未配置收件人或邮件未启用时
    静默跳过；与 Webhook 通知互不依赖。

    2026-08-27 对抗性审查（P2）：to 缺省不再回退原始 ADMIN_TO——那会绕过
    users.mail_notify 个人开关过滤，是留给未来调用方的隐私回归陷阱；改为
    fail-closed 拒发并 warning 提示。
    """
    to = (to or "").strip()
    if not to:
        logger.warning(
            "send_admin_alert 未提供过滤后的收件人列表，拒绝发送（fail-closed，"
            "请经 db.admin_mail_recipients 合成后传入）: %s", subject,
        )
        return False
    sent = False
    for addr in [a.strip() for a in to.split(",") if a.strip()]:
        if _send(subject, text, addr):
            sent = True
    return sent


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


def send_user(to, subject, text):
    """B 线：给指定用户发邮件（收件人由调用方显式传入，如账号归属用户邮箱）。

    邮件未启用或无收件人时静默跳过；发送失败只记日志。内容脱敏由调用方负责。
    """
    return _send(subject, text, to)
