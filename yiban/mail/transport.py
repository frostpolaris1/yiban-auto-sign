# -*- coding: utf-8 -*-
"""邮箱通知的发送层：SMTP（SSL / starttls）投递与主备条目 failover。

授权码与完整发件地址不回显（只记失败粗分类、打码地址与条目序号）；发送异常只记日志、
绝不抛出。发信条目与通道状态判定住在 `config` 层，本层只做构造与投递。
"""
import logging
import smtplib
import ssl
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from . import config, layout

logger = logging.getLogger("mailer")

# 发信条目 port 非法的发送侧一次性告警（同 config._port_warned 风格；条目 port 与旧键
# SMTP_PORT 来源不同，分开记旗避免互相吞掉对方的告警）
_entry_port_warned = False

# SMTP 目标为内网/保留地址的发送侧一次性告警旗：进程内只记一次，不阻断发送（写侧硬拦
# 由设置页负责，库层只提示）。
_private_target_warned = False


def _classify_send_error(e):
    """SMTP 发送异常 → 失败粗分类文案（认证失败/发送被拒/连接失败/其他失败）。

    失败日志只记粗分类，**不得**回显 `type(e).__name__`：`ConnectionRefusedError` /
    `TimeoutError` / `socket.gaierror` 等原始类型名会把「目标端口是否开放、是否超时、
    域名是否可解析」的探测指纹写进管理员可读日志——持被窃主管理员会话者据此可把 SMTP
    目标当内网端口扫描器。粗分类保留"哪一类问题"的排障价值，又不暴露端口状态。
    """
    if isinstance(e, smtplib.SMTPAuthenticationError):
        return "认证失败"
    if isinstance(e, (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused,
                      smtplib.SMTPDataError, smtplib.SMTPHeloError)):
        return "发送被拒"
    if isinstance(e, (OSError, smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError,
                      smtplib.SMTPNotSupportedError)):
        return "连接失败"
    return "其他失败"


def _send(subject, text, to):
    """发送一封邮件：按 smtp_list() 顺序逐条尝试（主备 failover），静默失败（记日志不抛出）。

    任一条发送成功即返回；单条失败（SMTP 异常/网络错误）记 warning（含条目序号与
    host，不含凭据）后尝试下一条；条目 port 非法回退 465 并记一次性告警；目标为内网/
    保留地址时进程内只记一次告警但**不**停发；全部失败返回 False。
    """
    global _private_target_warned
    to = str(to or "").strip()
    if not to or not config.is_enabled():
        return False
    entries = config.smtp_list()
    for idx, entry in enumerate(entries):
        host = str(entry.get("host") or "").strip()
        port = entry.get("port", 465)
        if not _private_target_warned:
            reason = config.check_smtp_host(host)
            if reason:
                _private_target_warned = True
                logger.warning(
                    "邮件通知 SMTP 目标 %s（条目 %d/%d）属内网/保留地址：%s。"
                    "仍会尝试发送；如需保留该目标，请在 .env 显式开启 "
                    "YIBAN_MAIL_ALLOW_PRIVATE_HOST",
                    host or "?", idx + 1, len(entries), reason,
                )
        try:
            port = int(port)
        except (TypeError, ValueError):
            global _entry_port_warned
            port = 465
            if not _entry_port_warned:
                _entry_port_warned = True
                logger.warning(
                    "SMTP 发信条目 %d/%d（host=%s）port=%r 不是合法端口，本次发送回退 465",
                    idx + 1, len(entries), host or "?", entry.get("port"),
                )
        user = str(entry.get("user") or "").strip()
        password = str(entry.get("pass") or "")

        plain, html_body = layout.as_body(text)
        if html_body:
            # multipart/alternative：parts 按"偏好递增"排（先 plain 后 html），
            # 不支持 HTML 的客户端与终端读到的仍是排好的纯文本——排版层只是增益，
            # 不构成新的送达依赖。
            msg = MIMEMultipart("alternative")
            msg.attach(MIMEText(plain, "plain", "utf-8"))
            msg.attach(MIMEText(html_body, "html", "utf-8"))
        else:
            msg = MIMEText(plain, "plain", "utf-8")
        msg["Subject"] = Header(subject, "utf-8")
        msg["From"] = user
        msg["To"] = to

        try:
            # 显式证书校验——smtplib 默认 context（ssl._create_stdlib_context）
            # verify_mode=CERT_NONE 不校验服务器证书，SMTP 授权码可被中间人窃取后
            # 以系统名义向用户发钓鱼邮件；主流服务商均为公共 CA，无兼容性损失
            ctx = ssl.create_default_context()
            if port == 465:
                server = smtplib.SMTP_SSL(host, port, timeout=15, context=ctx)
            else:
                server = smtplib.SMTP(host, port, timeout=15)
                server.starttls(context=ctx)
            with server:
                server.login(user, password)
                server.sendmail(user, [to], msg.as_string())
            logger.info("邮件通知已发送: %s → %s", subject, config._mask_addr(to))
            return True
        except (OSError, smtplib.SMTPException) as e:
            # 不回显授权码与异常详情（异常文本可能包含敏感信息），只记序号、脱敏地址与
            # 失败粗分类（原始类型名会泄露端口/超时指纹，原因见 _classify_send_error）；
            # OSError 已覆盖 socket 异常（py3 中 socket.error 是其别名）
            logger.warning(
                "邮件通知发送失败（SMTP 条目 %d/%d host=%s，%s）: %s → %s",
                idx + 1, len(entries), host or "?", _classify_send_error(e),
                subject, config._mask_addr(to),
            )
    return False


def send_admin_alert(subject, text, to=None):
    """A 线：管理员告警邮件。收件人 YIBAN_MAIL_ADMIN_TO（逗号分隔支持多地址）。

    to 必须由调用方合成后传入（ADMIN_TO 经收件人个人开关过滤 ∪ 开启接收的管理员用户
    邮箱，见 db.admin_mail_recipients）。未配置收件人或邮件未启用时静默跳过；与 Webhook
    通知互不依赖。

    to 缺省 fail-closed 拒发（不再回退原始 ADMIN_TO）：那会绕过 users.mail_notify
    个人开关过滤，是留给未来调用方的隐私回归陷阱。
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


def send_user(to, subject, text):
    """B 线：给指定用户发邮件（收件人由调用方显式传入，如账号归属用户邮箱）。

    邮件未启用或无收件人时静默跳过；发送失败只记日志。内容脱敏由调用方负责。
    """
    return _send(subject, text, to)
