# -*- coding: utf-8 -*-
"""Webhook 发送层：节流判定与 serverchan / 自定义 webhook 两条出口。

发送异常只记日志、绝不抛出（不拖累签到主流程）；节流窗口与每日额度的状态住在
`ledger` 层，本层只做判定、发送与失败退还。

账本模块以 `ledger_mod` 限定：`send` 的形参名就是 `ledger`（公开 API，不可改名），
裸名会被形参遮蔽。

**通信**
谁调用：`web/services/notify_mail.py` 的 `notify.send(...)`（告警邮件正文压成纯文本后
捎带推手机）、`yiban/engine/alerts.py`（失败提醒与汇总降级推送）、
`web/routes/notify.py` 的 `api_notify_test` → `send_test()`。
它调用：`config`（读类型/密钥、`is_safe_url` 白名单）、`ledger`（占额度 / 退还 /
节流表与跳过日志的去重表）、`requests.post`（两条出口）。
出口凭据（SendKey 拼在 serverchan 的 URL path 里、custom 的 URL 本身）**只用于发请求，
不进日志**：本层失败日志只记 `type(e).__name__` 与 `url_desc(url)`（脱敏 host）。
`title` / `content` 由调用方组装，本层不做脱敏——内容里的手机号靠日志侧
`MaskingFormatter` 与调用点自净，webhook 对端收到什么取决于调用方传了什么。
"""
import logging
import time

import requests

from yiban.security import url_desc

from . import config
from . import ledger as ledger_mod

logger = logging.getLogger("notify")

SERVERCHAN_TURBO_HOST = "sctapi.ftqq.com"  # 出口一：Server酱，SendKey 拼在 URL 的 path 段里


# 也是自定义地址指向域名时的 DNS rebinding 唯一兜底（白名单对域名目标是放行的），别随手调大
DEFAULT_URL_TIMEOUT = 10
MAX_TITLE_CHARS = 32  # Server酱服务端对 title 的长度上限，超了会被拒
# 跳过原因日志的去重窗口（秒）：同一原因窗口内只记一行，避免被刷爆日志
SKIP_LOG_WINDOW = 60


def _throttle_due(title):
    """同类型告警节流：窗口内已发过返回 False（本次跳过）。0 = 关闭。

    磁盘表（$YIBAN_STATE_DIR/notify-throttle.json，文件锁互斥）是权威：web（常驻）与
    signin（cron 新进程）共享同一窗口，不再各持一份进程内节流表、各放行一条。
    内存 `_throttle_ts` 只作快速路径（本进程刚放行过的标题不读磁盘直接跳过），
    磁盘在内存未命中后兜住另一进程放行过的标题。
    """
    cooldown = config._env_int("COOLDOWN", config.DEFAULT_COOLDOWN)
    if cooldown <= 0:
        return True  # True = 放行本次发送（名字像在问"到点没"，读调用点时最容易反着看懂）
    now = time.time()
    with ledger_mod._throttle_lock:
        # 内存快速路径：本进程刚放行过，窗口内直接跳过（不读磁盘）
        if now - ledger_mod._throttle_ts.get(title, 0.0) < cooldown:
            return False  # False = 节流命中，调用方据此跳过
        # 磁盘权威：单次文件锁临界区内 读盘 → 判定 → 更新 → 写回。
        # 与账本同构——另一进程放行过的窗口内标题会被这里拦下，双进程不双发。
        with ledger_mod._state_file_lock("notify-throttle.json"):
            disk = ledger_mod._load_throttle_file()
            if now - disk.get(title, 0.0) < cooldown:
                return False
            disk[title] = now
            ledger_mod._prune_throttle_entries(disk, now, cooldown)
            ledger_mod._save_throttle_file(disk)
        ledger_mod._throttle_ts[title] = now
        return True


def _log_skip(reason, msg, *args):
    """跳过发送的告知日志：同一原因在一个窗口内只记一行（可运维定位，不刷屏）。"""
    now = time.time()
    with ledger_mod._skip_log_lock:
        last = ledger_mod._skip_logged.get(reason, 0.0)
        if now - last < SKIP_LOG_WINDOW:
            return
        ledger_mod._skip_logged[reason] = now
    logger.info(msg, *args)


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
        logger.warning("通知推送失败（%s）: %s", type(e).__name__, url_desc(url))
        return False
    if r.status_code < 400:
        logger.info("消息推送已发送（custom）: %s", title)
        return True
    logger.warning("通知推送失败（状态码 %s）: %s", r.status_code, url_desc(url))
    return False


def send(title, content, force=False, urgent=False, ledger=None):
    """发送一条 webhook 通知（serverchan / custom）。返回是否成功发送。

    未配置 / 不启用 / 非紧急（仅重要告警开启时）/ 每日预算耗尽 / 节流命中 /
    发送失败均返回 False（静默，不拖累主流程，但会在日志留一行可定位的原因）。
    force=True 跳过节流与每日预算（供"测试推送"用）。
    urgent=True 标记重要告警：
    - YIBAN_NOTIFY_URGENT_ONLY 开启后仅此类会推送（邮件通道不受影响）；
    - 额度走紧急账（YIBAN_NOTIFY_URGENT_DAILY_MAX），与非紧急账互不挤占；
    - 只有真正发送成功才扣额度，失败（含 HTTP 异常、服务端非零 code、白名单拒发）凭
      占用时拿到的退还凭证退回。
    ledger：None = 按 urgent 归入 general/urgent 两本账；具名账本（如 "login_fail"）→
    独立日额度（用 YIBAN_LOGINFAIL_DAILY_MAX，默认 3，0=不限），与 general/urgent 互不
    挤占。节流与「仅重要告警」开关仍按全局口径执行，不受 ledger 影响。
    """
    # 同一逻辑段内复用一份 .env 快照：TYPE / SECRET_ENC / URGENT_ONLY 三个键共用。
    # 这不是"整次 send 只解析一次"——节流窗口（_throttle_due）与额度上限
    # （_consume_daily_budget / _daily_limit）仍各自按需解析，它们要读发送当刻的最新
    # 配置，把快照传下去反而会读到陈旧上限。
    envs = config._read_env_file()
    # 下面分五道门，任一道不过就短路返回 False：①配置 ②紧急开关 ③节流 ④占额度 ⑤发送
    ntype = config._env_str("TYPE", envs).strip().lower()
    secret = config.get_secret(envs)
    if not ntype:
        if not secret:
            return False  # 门①未配置（类型与密钥都没有），静默不推
        ntype = "custom"  # 兼容旧明文 YIBAN_NOTIFY_URL（未配 TYPE 但有 URL 时按 custom 发送）
    if not secret:
        return False  # 门①有类型没密钥，同样静默不推（抛错会拖累签到主流程）
    # 三本日额度互不挤占：具名账优先于紧急账，紧急账优先于普通账
    ledger_id = ledger or ("urgent" if urgent else "general")
    ticket = None  # None = 本次没占额度（force 路径），退还动作对它就是空操作
    if not force:
        # ①~④ 全排在 ⑤ 之前：这几道闸门一个不过就不该发出请求、更不该花额度
        if config._env_int("URGENT_ONLY", config.DEFAULT_URGENT_ONLY, envs) and not urgent:
            _log_skip("urgent_only",
                      "非紧急告警未推手机（YIBAN_NOTIFY_URGENT_ONLY 未显式置 0）: %s", title)
            return False  # 门②仅重要告警：非紧急跳过（因此也不消耗预算）
        if not _throttle_due(title):
            _log_skip("throttle", "推送节流命中（YIBAN_NOTIFY_COOLDOWN 窗口内同类已推）: %s", title)
            return False  # 门③节流命中
        # 门④必须在发送之前占：两个进程同时判定的话，占晚了就会双发
        ticket = ledger_mod._consume_daily_budget(ledger_id)
        if not ticket.allowed:
            _log_skip("budget_exhausted_" + ledger_id,
                      "今日推送额度（%s 账）已用尽，本次不推手机: %s", ledger_id, title)
            return False  # 门④今日额度用尽
    if ntype == "serverchan":  # 门⑤出口一：SendKey 拼在 URL 的 path 段里
        sent = _send_serverchan(secret, title, content)
    elif ntype == "custom":  # 门⑤出口二：URL 整体就是密钥，因此比出口一更敏感
        # 白名单只校验"你给的这一个 URL"。若目标回 3xx 把客户端引向内网或云元数据地址，
        # 跟随跳转等于绕过白名单——所以 _send_custom 里必须 allow_redirects=False。
        # 两者是一对，拆开了各自都只剩一半效力。
        if not config.is_safe_url(secret):
            logger.warning("自定义通知地址未通过白名单校验，已拒发: host=%s", url_desc(secret))
            sent = False
        else:
            sent = _send_custom(secret, title, content)
    else:
        logger.warning("未知通知类型: %s", ntype)
        sent = False  # 类型写错时宁可拒发，也不猜一条出口——那会把凭据送去没预期的对端
    if not sent:
        # "先占再发"的补偿边：不退还的话失败会把额度磨光，真要报警的那天反而推不出
        ledger_mod._refund_daily_budget(ticket)
    return sent


def send_test():
    """发送一条测试消息（跳过节流，供设置页"测试推送"）。"""
    return send(
        "消息推送测试",
        "这是一条来自易班自动签到系统的测试消息，收到即表示消息推送配置正常。",
        force=True,
    )
