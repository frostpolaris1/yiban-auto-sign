# -*- coding: utf-8 -*-
"""邮箱通知的发送层：SMTP（SSL / starttls）投递与主备条目 failover。

授权码与完整发件地址不回显（只记失败粗分类、打码地址与条目序号）；发送异常只记日志、
绝不抛出。发信条目与通道状态判定住在 `config` 层，本层只做构造与投递。
"""
import hashlib
import logging
import smtplib
import ssl
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from yiban.infra.env_io import escape_line_breaks
from yiban.masking import sanitize_text
from yiban.store import db

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


#: Message-ID 的域段：确定性幂等键的固定后缀（不含主机名，跨条目/跨部署同形）
_MESSAGE_ID_DOMAIN = "yiban"


def _message_id(subject, to, plain):
    """同封邮件的确定性幂等键（MF-90：上界从 10 降到 1 的机制基础）。

    failover 换条目重投的是**同一封**——旧构造每条各生成一份邮件且全仓无
    `Message-ID`，接收端没有可去重的标识，QUIT 抛异常一条接一条重投时同一封最多
    可送达 10 次（SMTP 条目上限）。键由 (subject, 收件人, 正文) 哈希而来：
    同告警同键（刻意**不用**随机 nonce——随机键等于没有键），不同告警/不同收件人
    不同键；接收端按 `Message-ID` 去重即可把"同封多条"压回 1 条。
    """
    digest = hashlib.sha256(
        f"{subject}\n{to}\n{plain}".encode("utf-8")).hexdigest()[:32]
    return f"<{digest}@{_MESSAGE_ID_DOMAIN}>"


def _record_recipient_refusals(refused, host, idx, total, message_id):
    """投递账入库（MF-44 补句）：SMTP 服务端给的逐收件人结果必须可机读地落库。

    `SMTPRecipientsRefused.recipients`（及部分拒收时 `sendmail` 的返回 dict）此前
    **零消费者**：逐地址结果只剩一行粗分类 warning，管理员无法回答"哪个地址死了、
    是退信还是中继拒绝（码不同处置不同）"——验收不变量"送达与已发送两个状态都入库"
    缺的正是这本账。落既有审计面（HMAC 链覆盖），不新造第二套状态存储。
    隐私口径：地址一律打码；服务端回显的原文里若含完整地址先换成打码形态，再过
    `sanitize_text` 单行化（防日志/审计注入）。
    """
    for addr, result in (refused or {}).items():
        masked = config._mask_addr(str(addr))
        code, msg = ("?", "")
        if isinstance(result, (tuple, list)) and len(result) == 2:
            code, msg = result[0], result[1]
        else:
            msg = result
        if isinstance(msg, bytes):
            msg = msg.decode("utf-8", "replace")
        # SMTP 拒信文本常把完整收件人地址回显回来——审计行不得二次外发明文地址
        msg = sanitize_text(str(msg).replace(str(addr), masked))[:80]
        try:
            ok = db.audit("system", "mail_refused", masked,
                          f"code={code} entry={idx}/{total} id={message_id[1:25]} msg={msg}")
        except Exception as e:  # 记账失败绝不拖垮发送主流程（本层契约：静默失败只记日志）
            ok = False
            logger.warning("写入邮件投递账失败（%s）: %s", type(e).__name__, masked)
        if not ok:
            logger.error("邮件投递账未能落审计链，请人工核查: %s", masked)


def _send(subject, text, to):
    """发送一封邮件：按 smtp_list() 顺序逐条尝试（主备 failover），静默失败（记日志不抛出）。

    任一条发送成功即返回；单条失败（SMTP 异常/网络错误）记 warning（含条目序号、
    本封幂等键与 host，不含凭据）后尝试下一条；换条目重投留一行可观测记录
    （"第 N 条目重投同一封"，MF-90）；条目 port 非法回退 465 并记一次性告警；目标为
    内网/保留地址时进程内只记一次告警但**不**停发；全部失败返回 False。
    """
    global _private_target_warned
    to = str(to or "").strip()
    if not to or not config.is_enabled():
        return False
    entries = config.smtp_list()
    # 正文在条目循环**之外**定稿：同封 failover 的每一条都必须是同一封——同
    # plain/html、同 Message-ID（键含正文，正文若在循环内二次排版，哈希跟着漂移，
    # 幂等键就白造）。
    plain, html_body = layout.as_body(text)
    message_id = _message_id(subject, to, plain)
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
        # 发信账号过单行安全原语（与告警正文净化同一份实现）：user 来自管理员配置，
        # 若含换行族字符会直进 AUTH 参数、MAIL FROM 与 From 头——单行化后非法形态
        # 只会被对端按坏地址拒绝，物理注入面不存在（MF-44 登记"零校验"项）。
        user = escape_line_breaks(str(entry.get("user") or "").strip())
        password = str(entry.get("pass") or "")

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
        msg["Message-ID"] = message_id

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
                accepted = server.sendmail(user, [to], msg.as_string())
            if accepted:
                # sendmail 的返回 dict 是"其余收件人收下、这些被拒"的结构化结果，
                # 与下面的 SMTPRecipientsRefused 同一本账，此前同样零消费者。
                _record_recipient_refusals(accepted, host, idx + 1, len(entries), message_id)
                logger.warning(
                    "邮件通知部分收件人被拒（SMTP 条目 %d/%d host=%s，id=%s）: %s → %s",
                    idx + 1, len(entries), host or "?", message_id,
                    subject, config._mask_addr(to),
                )
                return False
            logger.info("邮件通知已发送: %s → %s（id=%s，条目 %d/%d）",
                        subject, config._mask_addr(to), message_id, idx + 1, len(entries))
            return True
        except (OSError, smtplib.SMTPException) as e:
            # 不回显授权码与异常详情（异常文本可能包含敏感信息），只记序号、脱敏地址、
            # 幂等键与失败粗分类（原始类型名会泄露端口/超时指纹，原因见
            # _classify_send_error）；OSError 已覆盖 socket 异常（py3 中 socket.error
            # 是其别名）
            if isinstance(e, smtplib.SMTPRecipientsRefused):
                _record_recipient_refusals(e.recipients, host, idx + 1, len(entries),
                                           message_id)
            logger.warning(
                "邮件通知发送失败（SMTP 条目 %d/%d host=%s，%s，id=%s）: %s → %s",
                idx + 1, len(entries), host or "?", _classify_send_error(e),
                message_id, subject, config._mask_addr(to),
            )
            if idx + 1 < len(entries):
                # 换条目重投的有界可观测记录（MF-90）：重投的是同一封（同幂等键），
                # 不再无声；上界 = smtp_list() 长度（配置面 MAIL_SMTPS_MAX=10 封顶）。
                logger.info(
                    "同封重投：条目 %d/%d 接手（id=%s）", idx + 2, len(entries), message_id)
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
