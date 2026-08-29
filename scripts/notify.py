# -*- coding: utf-8 -*-
"""Webhook 推送组件（Server酱 / 自定义 URL）。

配置与仓库代码解耦（存 .env，gitignored；密钥字段 AES-GCM 加密）：
- YIBAN_NOTIFY_TYPE            : serverchan / custom / 空 = 不启用
- YIBAN_NOTIFY_SECRET_ENC      : 加密后的密文 JSON（serverchan=SendKey；custom=URL）
- YIBAN_NOTIFY_COOLDOWN        : 同类型告警节流秒数（默认 60，0=关闭）
- YIBAN_NOTIFY_URGENT_ONLY     : 1 = 仅推送重要（urgent）告警；0/空 = 全部推送
- YIBAN_NOTIFY_DAILY_MAX       : 每日推送条数上限（0=不限；默认 5，匹配 Server酱
                                  免费版 5 条/天；超限后当日不再推送，邮件通道不受影响）

兼容旧配置：未配置加密密文时回退明文 YIBAN_NOTIFY_URL（custom 语义），
旧部署迁移后无需手动改 .env。

防滥用（2026-08-29 被盗号滥用面加固）：
- 同类型告警节流：窗口内同标题只推一条（防盗号/异常反复触发刷爆 Server酱等
  第三方配额与管理员手机）；
- 每日预算硬上限：当天推满 YIBAN_NOTIFY_DAILY_MAX 后停止（邮件仍全量送达），
  保证不超出 Server酱 免费额度；
- 仅重要告警：YIBAN_NOTIFY_URGENT_ONLY 开启后，非紧急（如用户日常改密、签到
  结果类）通知不推手机，把预算留给真正威胁系统/账号安全的事件；
- 检查服务端响应：Server酱 code!=0 / 非 JSON 一律记日志（配额耗尽、限频可见），
  不再"发出即成功"地静默失败；
- 自定义 URL 走 SSRF 白名单（https + 非回环/内网），与 web/signin 既有口径一致。

设计原则：不配置 = 不启用；发送异常只记日志、绝不抛出（不拖累签到主流程）。
"""

import ipaddress
import json
import logging
import os
import threading
import time
from urllib.parse import urlparse

import requests

try:
    from . import account_crypto
except ImportError:  # 非包上下文（scripts/ 直接 import）
    import account_crypto

logger = logging.getLogger("notify")

_PREFIX = "YIBAN_NOTIFY_"
SERVERCHAN_TURBO_HOST = "sctapi.ftqq.com"
DEFAULT_COOLDOWN = 60
DEFAULT_DAILY_MAX = 5
DEFAULT_URL_TIMEOUT = 10
MAX_TITLE_CHARS = 32

_throttle_ts = {}
_throttle_lock = threading.Lock()

# 每日推送预算（进程内计数，重启清零；Server酱免费 5 条/天由服务端自身响应兜底，
# 本计数用于提前感知并停止，避免徒劳请求）
_daily_state = {"date": "", "count": 0}
_daily_lock = threading.Lock()


def _read_env_file():
    """读取 .env（utf-8-sig 兼容 BOM），供读配置用。"""
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


def _env_str(key):
    """环境变量优先，回退 .env（与 web/signin 惯例一致）。"""
    return os.environ.get(_PREFIX + key, "").strip() or _read_env_file().get(_PREFIX + key, "").strip()


def _env_int(key, default):
    try:
        return max(0, int(_env_str(key)))
    except (TypeError, ValueError):
        return default


def _mask_secret(secret):
    """密钥打码：保留前 3 位，其余星号。空返回空。"""
    if not secret:
        return ""
    if len(secret) <= 6:
        return secret[:2] + "**"
    return secret[:3] + "*" * max(4, len(secret) - 3)


def _host_of(url):
    """脱敏 URL 描述：仅 scheme://host[:port]，不含 userinfo/路径/查询（token 不外泄）。

    与 signin._notify_url_desc 同口径，供日志使用。
    """
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(url)
        if parts.hostname:
            desc = f"{parts.scheme}://{parts.hostname}"
            if parts.port:
                desc += f":{parts.port}"
            return desc
    except ValueError:
        pass
    return "<无法解析>"


def is_safe_url(url):
    """自定义通知地址 SSRF 白名单：https + 非回环/内网/链路本地/未指定。

    与 web/app.py _is_safe_notify_url、signin.py send_notification 同口径，
    防 http 明文外泄与 SSRF 跳板。域名目标放行（DNS rebinding 由超时兜底）。
    """
    try:
        o = urlparse(url)
    except ValueError:
        return False
    if o.scheme != "https" or not o.hostname:
        return False
    host = o.hostname.strip().lower()
    if host == "localhost":
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True  # 域名：非 IP 字面量，放行
    return not (ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_unspecified)


# ---------------------------------------------------------------------------
# 配置读取（加密存储）
# ---------------------------------------------------------------------------

def get_secret():
    """返回当前加密配置解出的明文密钥（serverchan=SendKey；custom=URL）。

    未配置 / 密文损坏 / 密钥不匹配均返回空（并记日志），绝不抛异常。
    """
    enc = _env_str("SECRET_ENC")
    if not enc:
        return _env_str("URL") or ""  # 兼容旧明文 YIBAN_NOTIFY_URL
    try:
        entry = json.loads(enc)
    except ValueError:
        logger.warning("YIBAN_NOTIFY_SECRET_ENC 解析失败，消息推送不可用")
        return ""
    try:
        return account_crypto.decrypt_text(entry, account_crypto.load_key())
    except ValueError as e:
        logger.warning("消息推送密钥解密失败: %s", e)
        return ""


def get_config():
    """配置概览（脱敏），供设置页/日志展示。"""
    ntype = _env_str("TYPE").strip().lower()
    secret = get_secret()
    if not ntype and secret:
        ntype = "custom"  # 兼容旧明文 YIBAN_NOTIFY_URL
    enabled = bool(ntype and secret)
    return {
        "ok": True,
        "enabled": enabled,
        "type": ntype if enabled else "",
        "secret_masked": _mask_secret(secret) if enabled else "",
        "configured": bool(ntype or secret),
        "cooldown": _env_int("COOLDOWN", DEFAULT_COOLDOWN),
        "urgent_only": bool(_env_int("URGENT_ONLY", 0)),
        "daily_max": _env_int("DAILY_MAX", DEFAULT_DAILY_MAX),
        "daily_remaining": _daily_remaining(),
    }


# ---------------------------------------------------------------------------
# 节流与发送
# ---------------------------------------------------------------------------

def _throttle_due(title):
    """同类型告警节流：窗口内已发过返回 False（本次跳过）。0 = 关闭。"""
    cooldown = _env_int("COOLDOWN", DEFAULT_COOLDOWN)
    if cooldown <= 0:
        return True
    now = time.time()
    with _throttle_lock:
        last = _throttle_ts.get(title, 0.0)
        if now - last < cooldown:
            return False
        _throttle_ts[title] = now
        return True


def _daily_today():
    """今日日期串（本地时区，与服务器日期一致）。"""
    return time.strftime("%Y-%m-%d")


def _daily_remaining():
    """今日剩余可推送条数；无上限（DAILY_MAX<=0 或未配置上限）返回 None。"""
    limit = _env_int("DAILY_MAX", DEFAULT_DAILY_MAX)
    if limit <= 0:
        return None
    with _daily_lock:
        if _daily_state["date"] != _daily_today():
            _daily_state["date"] = _daily_today()
            _daily_state["count"] = 0
        return max(0, limit - _daily_state["count"])


def _consume_daily_budget():
    """尝试消耗一条当日预算；已超限返回 False（本次不发，邮件通道不受影响）。"""
    limit = _env_int("DAILY_MAX", DEFAULT_DAILY_MAX)
    if limit <= 0:
        return True  # 0 = 不限
    with _daily_lock:
        if _daily_state["date"] != _daily_today():
            _daily_state["date"] = _daily_today()
            _daily_state["count"] = 0
        if _daily_state["count"] >= limit:
            return False
        _daily_state["count"] += 1
        return True


def _send_serverchan(sendkey, title, content):
    """Server酱 Turbo：POST https://sctapi.ftqq.com/{key}.send，title+desp。

    title 必填、最长 32 字符、不含换行；desp 为 Markdown 正文。成功返回 code==0。
    """
    url = f"https://{SERVERCHAN_TURBO_HOST}/{sendkey}.send"
    t = title.replace("\r", " ").replace("\n", " ").strip()
    if len(t) > MAX_TITLE_CHARS:
        t = t[:MAX_TITLE_CHARS]
    try:
        r = requests.post(
            url, data={"title": t, "desp": content or ""},
            timeout=DEFAULT_URL_TIMEOUT, allow_redirects=False,
        )
    except Exception as e:
        # 组件绝不抛异常（web/signin 调用方不兜底）；只记类型名（异常文本可能含 token）
        logger.warning("Server酱推送失败（%s）: %s", type(e).__name__, t)
        return False
    try:
        result = r.json()
    except ValueError:
        logger.warning("Server酱返回非 JSON（HTTP %s），视为失败: %s", r.status_code, t)
        return False
    if result.get("code") == 0:
        logger.info("消息推送已发送（serverchan）: %s", t)
        return True
    # 配额耗尽 / 限频 / 密钥错误等：返回非零 code，记日志可见（不重复刷）
    logger.warning(
        "Server酱推送被拒绝（code=%s, message=%s）: %s",
        result.get("code"), result.get("message", ""), t,
    )
    return False


def _send_custom(url, title, content):
    """自定义 webhook：POST JSON {title, content}（保持既有兼容格式）。"""
    try:
        r = requests.post(
            url, json={"title": title, "content": content},
            timeout=DEFAULT_URL_TIMEOUT, allow_redirects=False,
        )
    except Exception as e:
        # 组件绝不抛异常；只记类型名与脱敏 host（异常文本可能含 URL/token）
        logger.warning("通知推送失败（%s）: %s", type(e).__name__, _host_of(url))
        return False
    if r.status_code < 400:
        logger.info("消息推送已发送（custom）: %s", title)
        return True
    logger.warning("通知推送失败（状态码 %s）: %s", r.status_code, _host_of(url))
    return False


def send(title, content, force=False, urgent=False):
    """发送一条 webhook 通知（serverchan / custom）。返回是否成功发送。

    未配置 / 不启用 / 非紧急（仅重要告警开启时）/ 每日预算耗尽 / 节流命中 /
    发送失败均返回 False（静默，不拖累主流程）。
    force=True 跳过节流与每日预算（供"测试推送"用）。
    urgent=True 标记重要告警：YIBAN_NOTIFY_URGENT_ONLY 开启后仅此类会推送
    （邮件通道不受影响）。
    """
    ntype = _env_str("TYPE").strip().lower()
    secret = get_secret()
    if not ntype:
        if not secret:
            return False
        ntype = "custom"  # 兼容旧明文 YIBAN_NOTIFY_URL（未配 TYPE 但有 URL 时按 custom 发送）
    if not secret:
        return False
    if not force and _env_int("URGENT_ONLY", 0) and not urgent:
        return False  # 仅重要告警：非紧急跳过（不消耗每日预算）
    if not force and not _throttle_due(title):
        return False
    if not force and not _consume_daily_budget():
        logger.warning("今日消息推送已达上限（YIBAN_NOTIFY_DAILY_MAX），本次不再推送，请查邮件")
        return False
    if ntype == "serverchan":
        return _send_serverchan(secret, title, content)
    if ntype == "custom":
        if not is_safe_url(secret):
            logger.warning("自定义通知地址未通过白名单校验，已拒发: host=%s", _host_of(secret))
            return False
        return _send_custom(secret, title, content)
    logger.warning("未知通知类型: %s", ntype)
    return False


def send_test():
    """发送一条测试消息（跳过节流，供设置页"测试推送"）。"""
    return send(
        "消息推送测试",
        "这是一条来自易班自动签到系统的测试消息，收到即表示消息推送配置正常。",
        force=True,
    )
