# -*- coding: utf-8 -*-
"""SMTP 邮箱通知模块（A 线：管理员告警邮件；B 线：用户签到结果邮件）。

通过 .env / 环境变量（YIBAN_MAIL_*）配置，仅用 Python 标准库 smtplib 发送，
零第三方依赖。设计原则：

- 不配置 = 不启用：YIBAN_MAIL_ENABLE 未开启或配置不完整时静默跳过，不影响现有功能；
- 静默失败：发送异常只记日志，绝不抛出（告警/通知失败不能拖累签到主流程）；
- 日志脱敏：授权码、完整发件地址不回显，只记录异常类型与打码后的邮箱。
"""
import json
import logging
import os
import smtplib
import ssl
from email.header import Header
from email.mime.text import MIMEText

import account_crypto  # 同目录裸模块：SMTPS_ENC 密文加解密（AES-GCM，同 webhook 密钥口径）
import env_io  # 同目录共享模块：.env 解析单一实现

logger = logging.getLogger("mailer")

_PREFIX = "YIBAN_MAIL_"

# SMTP_PORT 配置无效时的「回退 465」一次性告警（v0.24.4：原静默回退会让
# 填错端口的用户在设置页看到 465 而误以为配置正确，排查困难）
_port_warned = False

# 发信条目 port 非法的发送侧一次性告警（同 _port_warned 风格；条目 port 与旧键
# SMTP_PORT 来源不同，分开记旗避免互相吞掉对方的告警）
_entry_port_warned = False


def _mask_addr(addr):
    """邮箱打码：1234567890@qq.com → 123*******@qq.com（保留域名；非邮箱原样返回）。"""
    addr = str(addr or "").strip()
    if "@" not in addr:
        return addr or "<未配置>"
    name, _, domain = addr.partition("@")
    visible = name[:3]
    return visible + "*" * max(3, len(name) - len(visible)) + "@" + domain


def _read_env_file():
    """读取 .env 键值；文件不存在/读失败返回空 dict（宽松策略）。

    YIBAN_ENV_FILE 指定路径（与 web/signin 子进程约定一致），回退默认 .env。
    解析实现单一来源见 env_io.parse_env_file。
    """
    return env_io.parse_env_file(env_io.env_path())


def _get(key):
    """读取配置：环境变量优先，回退 .env 文件（与 web/app.py send_notification 惯例一致）。"""
    return os.environ.get(_PREFIX + key, "").strip() or _read_env_file().get(_PREFIX + key, "").strip()


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
            # 密钥路径口径与 _read_env_file 一致（YIBAN_ENV_FILE 优先，见 notify.get_secret
            # 的教训：不带路径会回落 cwd/.env，容器部署下解错钥致通道静默死亡）
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

    port_fallback：SMTP_PORT 缺省或非法时为 True——展示层据此提示
    「端口未按预期生效，实际按 465 处理」，不再无声回退（2026-08-27 审查 P3）。
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


def is_enabled():
    """邮件通知是否启用：总开关=1 且发信条目列表非空（SMTPS_ENC 或旧键 USER+PASS）。"""
    if _get("ENABLE").strip().lower() not in ("1", "true", "on", "yes"):
        return False
    return bool(smtp_list())


def _send(subject, text, to):
    """发送一封邮件：按 smtp_list() 顺序逐条尝试（主备 failover），静默失败（记日志不抛出）。

    任一条发送成功即返回；单条失败（SMTP 异常/网络错误）记 warning（含条目序号与
    host，不含凭据）后尝试下一条；条目 port 非法回退 465 并记一次性告警；全部失败
    返回 False。
    """
    to = str(to or "").strip()
    if not to or not is_enabled():
        return False
    entries = smtp_list()
    for idx, entry in enumerate(entries):
        host = str(entry.get("host") or "").strip()
        port = entry.get("port", 465)
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

        msg = MIMEText(text, "plain", "utf-8")
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
            logger.info("邮件通知已发送: %s → %s", subject, _mask_addr(to))
            return True
        except (OSError, smtplib.SMTPException) as e:
            # 不回显授权码与异常详情（异常文本可能包含敏感信息），只记序号、类型与脱敏地址；
            # OSError 已覆盖 socket 异常（py3 中 socket.error 是其别名）
            logger.warning(
                "邮件通知发送失败（SMTP 条目 %d/%d host=%s，%s）: %s → %s",
                idx + 1, len(entries), host or "?", type(e).__name__, subject, _mask_addr(to),
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
