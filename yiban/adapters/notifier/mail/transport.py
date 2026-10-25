# -*- coding: utf-8 -*-
"""邮箱通知的发送层：SMTP（SSL / starttls）投递与主备条目 failover。

授权码与完整发件地址不回显（只记失败粗分类、打码地址与条目序号）；发送异常只记日志、
绝不抛出。发信条目与通道状态判定住在 `config` 层，本层只做构造与投递。

一封邮件、以及一次多收件人告警的全部邮件，共用一份时间预算。超预算即放弃剩余
条目，并留一行可定位的 warning。单个 socket 操作的超时不是整封的预算（工单 ba-p09-01）。

「连接失败」在同一条目内有界重试（最多 2 次，退避 5s/15s，工单 0eul）。认证失败、
发送被拒、其他失败一律不重试。DATA 已发出后的任何失败也不重试（防双发）——判据是
会话阶段，不是异常类型。重试次数与退避取值只有 `_CONNECT_RETRY_BACKOFF_SEC` 一处。
"""
import hashlib
import logging
import smtplib
import ssl
import time
from contextlib import suppress
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from yiban.adapters.config.env_io import escape_line_breaks
from yiban.adapters.persistence import db
from yiban.core.masking import sanitize_text

from . import config, layout

logger = logging.getLogger("mailer")

#: 单个 socket 操作的超时（既有取值，本单不许改小）。smtplib 的 timeout 是每个
#: socket 操作一次，不是整封调用的截止时间。
_OP_TIMEOUT_SEC = 15

#: SMTPS 协议端口（隐式 TLS 的标准端口）。这是**协议常量**，不是配置缺省：
#: 缺省端口住在配置名册；显式把条目端口设成 465 时仍必须走 SMTPS。
_SMTPS_PORT = 465

#: 整封（一次 `_send`、一次 `send_admin_alert`）的时间预算上界。取值按一条正常条目
#: 推导：连接、登录、投递各给一次操作超时。预算只截停**条目循环的开始**。已在跑的
#: 那一条会跑完自己的操作序列。故整封耗时上界 = 预算 + 一条条目的最坏耗时。
_TOTAL_BUDGET_SEC = 3 * _OP_TIMEOUT_SEC

#: 「连接失败」类的有界重试退避（秒）。**唯一定义点**：重试次数 = 本元组长度，
#: 故最多 2 次重试，间隔依次为 5 秒、15 秒。认证失败、发送被拒、其他失败不重试；
#: DATA 已发出后的任何失败也不重试（防双发，判据见 `_retryable_connect_error`）。
_CONNECT_RETRY_BACKOFF_SEC = (5, 15)

# 发信条目 port 非法的发送侧一次性告警（同 config._port_warned 风格；条目 port 与旧键
# SMTP_PORT 来源不同，分开记旗避免互相吞掉对方的告警）
_entry_port_warned = False

# SMTP 目标为内网/保留地址的发送侧一次性告警旗：进程内只记一次，不阻断发送（写侧硬拦
# 由设置页负责，库层只提示）。**只由「内网/保留」类 reason 置位**；空主机、方括号不闭合、
# 非标准 IPv4 字面量等配置错误另记 `_target_reason_warned`（按 reason 各提示一次），
# 否则一条无关的配置错误就把这面唯一的安全提示永久静音（ba-p09-09）。
_private_target_warned = False
#: 其余配置类拒绝原因 → 已提示过的 reason 集合（不同病因分开喊，互不吞声）。
_target_reason_warned = set()


def _quiet_quit(server):
    """关闭已建立的 SMTP 连接；quit/close 的异常一律吞掉。

    只在 sendmail 已返回（成功路径）或异常收口处调用。**QUIT 失败不改变送达结论**：
    旧实现把 `quit()` 留在 `with server:` 的 try 内，服务器收信后掉线会让已 ACK 的
    邮件被记成失败并换条目重投（ba-p09-02）。
    """
    try:
        server.quit()
    except Exception:
        with suppress(Exception):
            server.close()



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


def _retryable_connect_error(e, phase):
    """仅「连接阶段失败」可重试。判据是会话阶段，不是异常类型。

    `phase == "send"` 表示已进入 `sendmail`（MAIL/RCPT/DATA 全在里面）。此时任何
    异常都可能发生在 DATA 已发出之后：重试会让同封邮件被服务器收下两次。故该阶段
    一律不重试，即使粗分类仍是「连接失败」（如 DATA 后掉线的 SMTPServerDisconnected）。
    """
    return phase != "send" and _classify_send_error(e) == "连接失败"


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


def _send(subject, text, to, deadline=None):
    """发送一封邮件：按 smtp_list() 顺序逐条尝试（主备 failover），静默失败（记日志不抛出）。

    任一条发送成功即返回；单条失败（SMTP 异常/网络错误）记 warning（含条目序号、
    本封幂等键与 host，不含凭据）后尝试下一条；换条目重投留一行可观测记录
    （"第 N 条目重投同一封"，MF-90）；条目 port 非法回退 465 并记一次性告警；目标为
    内网/保留地址时进程内只记一次告警但**不**停发；全部失败返回 False。

    「连接失败」先在同一条目内有界重试（`_CONNECT_RETRY_BACKOFF_SEC`，最多 2 次，
    退避 5s/15s），每次重试各记一行日志；重试用尽或不可重试时按原口径记最终失败
    warning 并换下一条。认证失败、发送被拒、其他失败不重试。`sendmail` 阶段（MAIL/
    RCPT/DATA）的任何失败都不重试——DATA 可能已发出，重试会导致同封双发。

    `deadline` 是整封预算的到期时刻（单调钟秒）。缺省按 `_TOTAL_BUDGET_SEC` 在本函数
    内起算；`send_admin_alert` 传入同一个到期时刻，让多收件人共用一份预算。到期即放弃
    剩余条目并记 warning；每次 socket 操作的超时取"剩余预算与该操作超时的较小者"。
    """
    global _private_target_warned
    to = str(to or "").strip()
    if not to:
        return False
    # 一轮发信只取一份 .env 快照、只解密一次条目（ba-p09-07）：is_enabled() 会再走一遍
    # smtp_channel_state → smtp_list，等于第二次 AES-GCM；判据用已解析条目复算即可。
    envs = config._read_env_file()
    entries = config.smtp_list(envs)
    if not config.is_enabled(envs, entries):
        return False
    if deadline is None:
        deadline = time.monotonic() + _TOTAL_BUDGET_SEC
    # 正文在条目循环**之外**定稿：同封 failover 的每一条都必须是同一封——同
    # plain/html、同 Message-ID（键含正文，哈希跟着正文走，正文若在循环内二次
    # 排版，幂等键就白造）。
    # 排版定稿同样进 try：本层契约是"静默失败（记日志不抛出）"，而排版吃的是
    # 调用方拼出来的正文（用户名/邮箱/理由等外部输入可携带孤立代理对，utf-8
    # 编码直接失败）。此前这段与 MIME 构造都露在 try 外，一次坏字符会把异常
    # 抛回当时在 `_file_lock` 里同步发信的调用点——业务已落盘、锁内爆 500、
    # 排在其后的审计行也没了。
    try:
        plain, html_body = layout.as_body(text)
        message_id = _message_id(subject, to, plain)
    except Exception as e:
        # 细节走 ascii() 转义：坏字符正是失败原因本身，原样进日志会在写流时二次爆
        logger.warning("邮件正文渲染失败（本封不发，只记不抛）: %s", ascii(e))
        return False
    for idx, entry in enumerate(entries):
        # 整封预算：条目循环逐条吃 socket 超时，最坏 = 条目数 × 操作数 × 每操作超时
        # （工单 ba-p09-01）。到点即放弃剩余条目；已在途的那次操作无法中断，故预算
        # 只截停"还开不开始下一次连接"。剩余预算同时压住每次 socket 操作超时。
        if deadline - time.monotonic() <= 0:
            logger.warning(
                "邮件通知整封超时，放弃剩余条目（已试 %d/%d 条目，id=%s）: %s → %s",
                idx, len(entries), message_id, subject, config._mask_addr(to),
            )
            return False
        host = str(entry.get("host") or "").strip()
        port = entry.get("port", config._default_smtp_port())
        reason = config.check_smtp_host(host)
        if reason:
            if config.is_private_target_reason(reason):
                if not _private_target_warned:
                    _private_target_warned = True
                    logger.warning(
                        "邮件通知 SMTP 目标 %s（条目 %d/%d）属内网/保留地址：%s。"
                        "仍会尝试发送；如需保留该目标，请在 .env 显式开启 "
                        "YIBAN_MAIL_ALLOW_PRIVATE_HOST",
                        host or "?", idx + 1, len(entries), reason,
                    )
            elif reason not in _target_reason_warned:
                # 空主机 / 方括号不闭合 / 非标准字面量等配置错误：按 reason 各提示一次。
                # 与内网旗分开记，任何一条病因都不许吞掉别的病因的告警。
                _target_reason_warned.add(reason)
                logger.warning(
                    "邮件通知 SMTP 目标配置无效（条目 %d/%d host=%s）：%s；本条仍会尝试发送",
                    idx + 1, len(entries), host or "?", reason,
                )
        try:
            port = int(port)
        except (TypeError, ValueError):
            global _entry_port_warned
            port = config._default_smtp_port()
            if not _entry_port_warned:
                _entry_port_warned = True
                logger.warning(
                    "SMTP 发信条目 %d/%d（host=%s）port=%r 不是合法端口，本次发送回退 %d",
                    idx + 1, len(entries), host or "?", entry.get("port"), port,
                )
        # 发信账号过单行安全原语（与告警正文净化同一份实现）：user 来自管理员配置，
        # 若含换行族字符会直进 AUTH 参数、MAIL FROM 与 From 头——单行化后非法形态
        # 只会被对端按坏地址拒绝，物理注入面不存在（MF-44 登记"零校验"项）。
        user = escape_line_breaks(str(entry.get("user") or "").strip())
        password = str(entry.get("pass") or "")

        # 同一条目内的连接重试：次数与间隔只由 _CONNECT_RETRY_BACKOFF_SEC 决定。
        retry_no = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logger.warning(
                    "邮件通知整封超时，放弃剩余条目（已试 %d/%d 条目，id=%s）: %s → %s",
                    idx, len(entries), message_id, subject, config._mask_addr(to),
                )
                return False
            op_timeout = min(_OP_TIMEOUT_SEC, remaining)
            # 会话阶段：连接与认证阶段失败可重试；进入 sendmail 后任何失败都按
            # "DATA 可能已发出"处理，一律不重试（见 `_retryable_connect_error`）。
            phase = "connect"
            server = None
            try:
                # MIME 构造与序列化一起进 try：孤立代理对这类坏字符在
                # `Header(...).encode()` / `as_string()` 时才真正抛 UnicodeEncodeError，
                # 构造语句挪进来但 except 不收口等于没挪（下方 Exception 分支）。
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

                # 显式证书校验——smtplib 默认 context（ssl._create_stdlib_context）
                # verify_mode=CERT_NONE 不校验服务器证书，SMTP 授权码可被中间人窃取后
                # 以系统名义向用户发钓鱼邮件；主流服务商均为公共 CA，无兼容性损失
                ctx = ssl.create_default_context()
                try:
                    if port == _SMTPS_PORT:
                        server = smtplib.SMTP_SSL(host, port, timeout=op_timeout, context=ctx)
                    else:
                        server = smtplib.SMTP(host, port, timeout=op_timeout)
                        server.starttls(context=ctx)
                    phase = "auth"
                    server.login(user, password)
                    phase = "send"
                    accepted = server.sendmail(user, [to], msg.as_string())
                finally:
                    # QUIT/close 失败不属于投递结论：sendmail 返回即已 ACK（ba-p09-02）。
                    # 关连接的异常在这里被吞掉，不进入下面的失败分支。
                    if server is not None:
                        _quiet_quit(server)
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
                classification = _classify_send_error(e)
                delay = None
                if (retry_no < len(_CONNECT_RETRY_BACKOFF_SEC)
                        and _retryable_connect_error(e, phase)):
                    delay = _CONNECT_RETRY_BACKOFF_SEC[retry_no]
                    # 退避也吃整封预算：睡醒后没有剩余预算就不再开新连接。
                    if deadline - time.monotonic() - delay <= 0:
                        delay = None
                if delay is not None:
                    logger.warning(
                        "邮件通知连接失败，%d 秒后重试（SMTP 条目 %d/%d host=%s，"
                        "第 %d/%d 次重试，id=%s）",
                        delay, idx + 1, len(entries), host or "?",
                        retry_no + 1, len(_CONNECT_RETRY_BACKOFF_SEC), message_id,
                    )
                    time.sleep(delay)
                    retry_no += 1
                    continue
                logger.warning(
                    "邮件通知发送失败（SMTP 条目 %d/%d host=%s，%s，id=%s）: %s → %s",
                    idx + 1, len(entries), host or "?", classification,
                    message_id, subject, config._mask_addr(to),
                )
                if idx + 1 < len(entries):
                    # 换条目重投的有界可观测记录（MF-90）：重投的是同一封（同幂等键），
                    # 不再无声；上界 = smtp_list() 长度（配置面 MAIL_SMTPS_MAX=10 封顶）。
                    logger.info(
                        "同封重投：条目 %d/%d 接手（id=%s）", idx + 2, len(entries), message_id)
                break
            except Exception as e:
                # 消息级缺陷（构造/序列化对坏字符敏感，如孤立代理对 →
                # UnicodeEncodeError）：换条目重投的是同一封、必然同样失败，直接按
                # "本封未送达"收口。不在本层收口的话异常会抛回调用方（`ba-p05-01`
                # 前多为持 `_file_lock` 同步发信的 web 路由），等于让一封坏邮件在
                # 锁内爆 500、并把排在其后的审计留痕一起带走。细节走 ascii() 转义：
                # 坏字符正是失败原因本身，原样进日志会在写流时二次爆。
                logger.warning("邮件构造/序列化失败（本封不再换条目重试）: %s", ascii(e))
                return False
    return False


def send_admin_alert(subject, text, to=None):
    """A 线：管理员告警邮件。收件人 YIBAN_MAIL_ADMIN_TO（逗号分隔支持多地址）。

    to 必须由调用方合成后传入（ADMIN_TO 经收件人个人开关过滤 ∪ 开启接收的管理员用户
    邮箱，见 db.admin_mail_recipients）。未配置收件人或邮件未启用时静默跳过；与 Webhook
    通知互不依赖。

    to 缺省 fail-closed 拒发（不再回退原始 ADMIN_TO）：那会绕过 users.mail_notify
    个人开关过滤，是留给未来调用方的隐私回归陷阱。

    逐收件人的循环乘在条目循环之上，故整次调用只领一份预算：第一个收件人把预算用尽后，
    其余收件人不再开新的 SMTP 连接（工单 ba-p09-01 点名的第二层乘数）。
    """
    to = (to or "").strip()
    if not to:
        logger.warning(
            "send_admin_alert 未提供过滤后的收件人列表，拒绝发送（fail-closed，"
            "请经 db.admin_mail_recipients 合成后传入）: %s", subject,
        )
        return False
    sent = False
    deadline = time.monotonic() + _TOTAL_BUDGET_SEC
    for addr in [a.strip() for a in to.split(",") if a.strip()]:
        if _send(subject, text, addr, deadline=deadline):
            sent = True
    return sent


def send_user(to, subject, text):
    """B 线：给指定用户发邮件（收件人由调用方显式传入，如账号归属用户邮箱）。

    邮件未启用或无收件人时静默跳过；发送失败只记日志。内容脱敏由调用方负责。
    """
    return _send(subject, text, to)
