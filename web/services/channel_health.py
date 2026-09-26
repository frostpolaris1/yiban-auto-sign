# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""告警通道健康族：两条通道的结构化可用性判据、展示行与例行健康报告。

**功能**
两条告警通道的结构化可用性判据 `_alert_channel_status`；"是否算降级"的判定
`_channel_health_degraded`；当前状态文本行 `_channel_status_lines` 与两本推送额度账的
今日剩余 `_daily_budget_desc`；报告"今日已播"标记读取 `_health_report_sent_today`、
压缩事实摘要 `_channel_health_facts`、降级痕迹落审计链 `_audit_channel_health_degraded`；
健康报告本体 `_send_channel_health_report`（含 app_meta 去重标记）。本模块自己**不管
什么时候播**——例行播一周一次、通道降级或额度耗尽当天照发，判据在 `web/app.py` 的
`_channel_health_report_due`；名字里的"日报"是收敛为周报之前的旧称，仍留在若干符号里。

**归属**
原 `web/app.py` 的模块级通道健康辅助，唯一真源在本模块；`web/app.py` 只保留名字面与
转发，把它自己持有、而本模块需要的入口——判定`.env` 里推送是否曾配置的
`_push_ever_configured`、结构化状态生产者 `_alert_channel_status`、状态行生产者
`_channel_status_lines`、告警出口 `send_notification`——在调用时刻现取后注入。

**复用**
判定与文案共用同一份结构化数据（`_alert_channel_status` 的字段），`_channel_status_lines`
只把它翻译成人话；是否降级由 `_channel_health_degraded` 直接看结构化字段，不回过来嗅
正文里有没有 ⚠ 字符。`_channel_health_facts` 的摘要被审计 detail 与 app_meta 记录共用
一份，推送账本中文名表 `_NOTIFY_LEDGER_LABELS` 取自 `web/services/notify_mail.py`（只留
一份）。降级判定委托 `_channel_health_degraded`，日报不另写一套判据。

**通信**
本模块不反向导入 `web.app`（本仓测试以别名加载 `app.py`，普通 import 会再执行一份副本
模块）。四个入口都作为显式参数接收：它们在 `web.app` 上会被测试打桩（既有测试在
`web.app` 上打桩 `_channel_status_lines` 做"纯文案改版"对拍，又打桩 `send_notification`
模拟发信失败），本模块另持一份绑定会让这些桩静默失效。`db` 与 `notify` 直接取真源
（与 `web.app.db` / `web.app.notify` 是同一模块对象），不另立第二套读写。
"""

import json
import logging

from web.services.notify_mail import _NOTIFY_LEDGER_LABELS, _alert_mail_recipients, _nl_safe
from yiban import clock, notify
from yiban import mail as mailer
from yiban.mail import layout as mail_layout
from yiban.store import db

# 与 web.app 同名的日志通道：本族的告警落回既有通道，便于运维沿用同一处过滤
logger = logging.getLogger("web")


def _alert_channel_status(push_ever_configured):
    """两条告警通道的结构化可用性判据。

    刻意把"通道到底可用不可用"从展示文案里拆出来单独算：是否降级决定日报按不按
    urgent 发、以及要不要往审计链落痕迹，属安全判定，不能靠"正文里有没有 ⚠ 字符"
    这种字符串嗅探——措辞改一次、或某条本该报的事实恰好不带 ⚠（旧写法里
    「主管理员个人接收=否」与「推送通道：未配置」两行都不带），嗅探就会静默漏判。
    本函数一次读清两侧与收件人，`_channel_status_lines()` 只负责把它翻译成人话，
    判定与文案共用同一份数据，不可能再各说各话。

    "推送是否曾配置过"的判据由调用方传入（`web.app` 的 `_push_ever_configured`）：
    它要读本进程的 `ENV_FILE`，而该路径会被测试改写、也会随 `--config` 变化。
    """
    status = {
        "mail_flag_on": False,      # YIBAN_MAIL_ENABLE 开关本身
        "mail_usable": False,       # mailer.is_enabled()：开关 + SMTP 发信条目真正可用
        "mail_state": "",           # mailer.smtp_channel_state()：ok/broken/off 三态
        "mail_state_detail": "",    # broken 时的具体病因（供日报点名，不必翻日志）
        "mail_self_notify": True,   # 主管理员个人接收（YIBAN_MAIL_ADMIN_NOTIFY）
        "mail_recipients": 0,       # 实际可送达收件人（为空 == 邮件这路等于不存在）
        "mail_user": "",
        "mail_admin_to": "",
        "mail_error": "",
        "push_usable": False,       # notify.get_config()["enabled"]：有 type 且密钥解得开
        "push_configured": False,   # 配过（含"配过又被清钥"= 已知病症）
        # 是否曾配置过（.env 两键存在且值非空）：区分"从未启用推送"与"配过又被拆"
        "push_ever_configured": False,
        "push_type": "",
        "push_secret_masked": "",
        "push_urgent_only": False,
        "push_error": "",
        # 两本推送额度账（分账后分开展示；键名与 notify.get_config 保持一致）
        "daily_max": None,
        "daily_remaining": None,
        "urgent_daily_max": None,
        "urgent_daily_remaining": None,
    }
    # ---- 邮件通道（A 线：全部安全告警的最后送达路径）----
    try:
        mcfg = mailer.get_config()
        status["mail_flag_on"] = str(mcfg.get("enable", "")).strip().lower() in (
            "1", "true", "on", "yes")
        # 可用性判据必须是 mailer.is_enabled()：除 YIBAN_MAIL_ENABLE 外还要求 SMTP
        # 发信条目列表非空（YIBAN_MAIL_SMTPS_ENC 或旧键 USER+PASS），否则
        # mailer._send 就静默跳过、一封都不发。只看 enable 真值会把"开了但发不出去"
        # 误报成"一切正常"。
        status["mail_usable"] = bool(mailer.is_enabled())
        # 三态与病因同行取出（两次调用会重复解密一次）：broken 时日报按
        # "已开启但不可用 + 具体病因"展示——密文解不开与未配置条目是两种不同处置
        mail_state, mail_state_detail = mailer.smtp_channel_state()
        status["mail_state"] = mail_state
        status["mail_state_detail"] = mail_state_detail
        status["mail_self_notify"] = bool(mcfg.get("admin_notify", True))
        status["mail_user"] = mcfg.get("user", "") or "-"
        status["mail_admin_to"] = mcfg.get("admin_to", "") or "-"
    except Exception as e:  # 兜底：日报不得因单通道读取失败整体缺席
        status["mail_error"] = type(e).__name__
    try:
        # 收件人与 send_notification 同一套算法（_alert_mail_recipients）：开关与凭据
        # 都齐、但 ADMIN_TO 被个人接收开关摘掉且无其他接收管理员时，recipients 为空，
        # 邮件这路同样一封都发不出去——只关 admin_notify 且无其他接收管理员的组合变体。
        status["mail_recipients"] = len(_alert_mail_recipients())
    except Exception as e:
        # 读不动收件人按 0 处理（宁可多判一次降级留痕，不可静默当健康）
        status["mail_error"] = status["mail_error"] or type(e).__name__
    # ---- 手机推送通道（B 线：Webhook / Server酱）----
    try:
        ncfg = notify.get_config()
        status["push_usable"] = bool(ncfg.get("enabled"))
        status["push_configured"] = bool(ncfg.get("configured"))
        status["push_type"] = ncfg.get("type", "")
        status["push_secret_masked"] = ncfg.get("secret_masked", "")
        status["push_urgent_only"] = bool(ncfg.get("urgent_only"))
        for key in ("daily_max", "daily_remaining",
                    "urgent_daily_max", "urgent_daily_remaining"):
            status[key] = ncfg.get(key)
        # 曾配置判据与 notify 侧读的是同一份 .env（两键存在且值非空），本函数唯一的额外
        # 开销是一次 os.path.exists；抛错时上面的 push_error 已置位 ⇒ 直接判降级。
        status["push_ever_configured"] = push_ever_configured()
    except Exception as e:  # 兜底：同上
        status["push_error"] = type(e).__name__
    return status


def _channel_health_degraded(status, exhausted=()):
    """本次通道状态是否算"降级"（决定日报 urgent 与要不要落审计痕迹）。

    判据逐条取自结构化数据（_alert_channel_status() 的字段 + pop_exhaustion_notice()
    返回的账本名列表），与正文里有没有 ⚠ 字符无关。

    口径修正：**降级只回答"本应可用的出口现在不可用"**。旧口径把"推送从未
    配置"也算降级，后果是邮件单通道部署（本项目真实生产在某个时点就是这样）每天落
    一条 channel_health 降级痕迹、日报每天挂 ⚠/urgent —— 对一个刻意的终态配置天天
    喊"降级"就是告警疲劳，正是要消灭的病。"清空推送配置"这个**动作**本身已由
    notify_config 审计行 + urgent 播报覆盖，日报无需重复定性。
    注意：本函数只管"要不要挂旗标"，两侧事实由 _channel_health_facts() **无条件**
    落审计与 meta（无论是否降级），"事后核查"不因此少一个字。

    成立条件（任一即降级）：
      - 看不清状态：邮件侧或推送侧读取失败——报警器本身出了毛病；
      - 当日有推送账本额度耗尽（exhausted 非空）：这路当天等于死了；
      - (a) 邮件侧不可用：mailer.is_enabled() 为假（YIBAN_MAIL_ENABLE 开了却没有
        可用的 SMTP 发信条目，或整个开关被关），一封都发不出去；
      - (b) 邮件侧可用却无任何可送达收件人（_alert_mail_recipients() 为空）——
        "只关 admin_notify 且库里没有其他接收管理员"这个组合变体仍算降级；
      - (c) 推送侧**曾配置过**而现在不可用：.env 里 type 或密文至少一个仍有值，却
        已被清钥、或密文存在却解不出（push_configured 真而 push_usable 假
        = 已知病症）。
    「从未配置过推送」不在 (c) 内：.env 里 YIBAN_NOTIFY_TYPE 与 YIBAN_NOTIFY_SECRET_ENC
    两键都不存在、或都在而值都为空（设置页关闭通道是删键行、手工清空是空值行，二者在
    配置文件里同形）—— 这样的部署日报照常用于每日一封，只是不挂降级旗标。
    """
    if status["mail_error"] or status["push_error"]:
        return True  # 看不清通道状态本身就是报警器出了毛病
    if exhausted:
        return True
    if not status["mail_usable"] or status["mail_recipients"] <= 0:
        return True  # (a)(b)：邮件是全部安全告警的最后送达路径，任何时候都本应可用
    # (c)：推送这路只有在"曾经配过"的前提下缺失才算被拆；从未启用手机推送是合法终态
    return bool(status["push_ever_configured"]) and not status["push_usable"]


def _channel_status_lines(alert_channel_status, status=None):
    """两条告警通道的当前状态文本行（供每日健康日报使用）——纯展示层。

    判据一律取自 `alert_channel_status()`（或调用方传入的那一份），本函数只把结构化
    事实翻译成人话；是否降级由 `_channel_health_degraded()` 直接看结构化字段，不回过来
    嗅这里有没有 ⚠。

    刻意"不依赖被改配置本身"：通道被关闭时照样输出"被关"这一行，而不是跳过——
    攻击者关掉报警器后，日报里必须仍然看得见"被关"这个事实，否则关闭动作与
    "一切正常"在运维眼里无法区分。读取失败也出一行（并标注读取失败），保证
    日报每次都有这条状态，不会静默缺席。

    默认的状态生产者由调用方传入（`web.app` 的 `_alert_channel_status`）：本函数在
    `status` 缺省时要现取它，而 `web.app` 上这个名字是既有的打桩面。
    """
    st = status if status is not None else alert_channel_status()
    lines = []
    # ---- 邮件通道 ----
    if st["mail_error"]:
        lines.append(f"邮件通道：⚠ 状态读取失败（{st['mail_error']}）")
    elif st["mail_usable"] and st["mail_recipients"] <= 0:
        lines.append("邮件通道：⚠ 已开启但无可送达收件人（0 人可收）")
        lines.append("成因：主管理员个人接收已关、或未配置 ADMIN_TO，"
                     "且库里没有其他开启接收的管理员")
        lines.append("影响：全部告警邮件实际一封都发不出去")
    elif st["mail_usable"]:
        lines.append("邮件通道：已开启")
        lines.append(f"邮件发件账号：{_nl_safe(st['mail_user'])}")
        lines.append(
            f"主管理员个人接收：{'是' if st['mail_self_notify'] else '否（ADMIN_TO 不收）'}")
        lines.append(f"告警收件地址：{_nl_safe(st['mail_admin_to'])}")
        lines.append(f"今日可送达收件人：{st['mail_recipients']} 人")
    elif st["mail_flag_on"]:
        # 三态 broken（开关开但发不出去）：把具体病因（未配置条目 / 条目缺账号
        # 授权码 / 密文解不开）单独一行带到日报，运维不必翻日志就能区分处置
        lines.append("邮件通道：⚠ 已开启但不可用")
        lines.append(f"病因：{st['mail_state_detail']}")
        lines.append("影响：全部告警邮件实际一封都不会发出")
    else:
        lines.append("邮件通道：⚠ 已关闭（YIBAN_MAIL_ENABLE=0，全部告警邮件不发送）")
    # ---- 手机推送通道 ----
    if st["push_error"]:
        lines.append(f"推送通道：⚠ 状态读取失败（{st['push_error']}）")
    else:
        if st["push_usable"]:
            lines.append("推送通道：已开启")
            lines.append(f"推送类型：{_nl_safe(st['push_type'])}")
            lines.append(f"推送密钥：{_nl_safe(st['push_secret_masked'])}")
            lines.append(
                f"推送范围：{'仅推送重要告警' if st['push_urgent_only'] else '全部告警均推送'}")
        elif st["push_configured"]:
            # 配过但当前不可用（密钥被清 / 换钥后解不开 = 已知病症）
            lines.append("推送通道：⚠ 已配置但不可用（密钥缺失或解密失败，需重新配置）")
        else:
            # 「未配置」同样标 ⚠ 并计入降级。旧写法把它当正常文本，于是
            # "只关 admin_notify + 关闭推送（type 置空后 configured 一并转假）" 这个
            # 组合变体两行都不带 ⚠ —— 日报既发不出去、又一条痕迹不落。手机推送这路
            # 不存在是真实的致盲风险（邮件一挂就零告警），必须看得见。
            lines.append("推送通道：⚠ 未配置（手机推送这路不存在，告警只剩邮件一条出口）")
        lines.extend(_daily_budget_desc(st))
    return lines


def _daily_budget_desc(cfg):
    """两本推送额度账的今日剩余（分账后必须分开报，不能只报非紧急）。

    返回**行列表**而不是拼成一行：两本账各占一行才扫得清哪本先烧完。

    入参可以是 notify.get_config() 的原始输出，也可以是 _alert_channel_status() 的
    快照——后者刻意沿用同名键，展示层不再重复读一遍配置。
    """
    def _fmt(label, remaining, limit):
        if remaining is None:
            return f"{label}不限额"
        cap = f"/{limit}" if isinstance(limit, int) and limit > 0 else ""
        return f"{label}剩余 {remaining}{cap} 条"

    return [
        f"今日推送额度（{_fmt('非紧急', cfg.get('daily_remaining'), cfg.get('daily_max'))}）",
        f"今日推送额度（{_fmt('紧急', cfg.get('urgent_daily_remaining'), cfg.get('urgent_daily_max'))}）",
    ]


# 通道健康日报"今日已播"标记（app_meta 键）。刻意落库而非进程内 dict：
# 每日线程在启动 60 秒后即跑第一轮，_mail_alert_ts 这类进程内状态重启即失效，
# 频繁重启的环境会把"每日健康日报 + 每轮一封"变成"每次重启各发一封外发邮件"。
_HEALTH_REPORT_META_KEY = "channel_health_last"


def _health_report_sent_today(today):
    """app_meta 里记录的最近一次日报是否就是今天（跨进程重启有效）。

    读失败 / 无记录一律按"未发送"处理：宁可多播一封，也不能因为库读不动而让
    报警器彻底沉默（漏播比重复打扰严重）。兼容两种写法：JSON 与裸日期串。
    """
    raw = db.get_meta(_HEALTH_REPORT_META_KEY, "")
    if not raw:
        return False
    try:
        data = json.loads(raw)
    except ValueError:
        return raw.strip() == today
    return isinstance(data, dict) and data.get("date") == today


def _channel_health_facts(status, exhausted=()):
    """两侧通道的压缩事实摘要（审计 detail 与 app_meta 记录共用这一份）。

    旧 detail 只拼正文里含 ⚠ 的行，两通道全断的那个变体里恰好
    两侧都不带 ⚠（或只有一侧带），detail 就只剩半条链，"推送侧也被拆了"这个关键
    事实直接丢在取证之外。这里**无条件**把两侧各写一段，健康的那一侧也记——
    事后要能回答"坏的是哪一路、另一路当时是不是好的"。
    刻意用短串而不是整句人话：db.audit 会把 detail 截到 200 字符
    （见 yiban/store/db.py 的 audit），拼完整句子在最坏情况下会把后半句（推送侧）截没，
    等于重犯同一个错。
    """
    if status["mail_error"]:
        mail = f"读取失败({status['mail_error']})"
    elif not status["mail_usable"]:
        mail = "开关开但凭据缺失" if status["mail_flag_on"] else "已关闭"
    elif status["mail_recipients"] <= 0:
        mail = "可用但收件人为空"
    else:
        mail = "可用"
    if status["push_error"]:
        push = f"读取失败({status['push_error']})"
    elif status["push_usable"]:
        push = "可用"
    elif status["push_configured"]:
        push = "已配置但不可用"
    else:
        push = "未配置"
    facts = (f"邮件通道={mail}(收件人{status['mail_recipients']},"
             f"主管理员接收={'是' if status['mail_self_notify'] else '否'})；"
             f"推送通道={push}")
    if exhausted:
        facts += "；推送额度已用尽=" + "/".join(
            _NOTIFY_LEDGER_LABELS.get(k, k) for k in exhausted)
    return facts


def _audit_channel_health_degraded(facts):
    """把"通道处于降级"这一事实落到审计链。

    两条通道同时被拆时，日报本身既发不出去也没有任何别的出口；没有库内痕迹，
    事后就无法证明"系统曾检测到通道被拆"，攻击者的拔线动作与运维正常停机在
    取证上完全同形。审计行会进入既有 HMAC 哈希链并被库外锚点覆盖（app_meta 不在
    链内，所以痕迹刻意写审计而不是 meta）。db.audit 自身已含重试与失败计数。

    facts 必须由 _channel_health_facts()（结构化状态）产出，不得改为拼正文里含 ⚠
    的行——那正是失准的来源。
    """
    detail = f"degraded=1 {facts}"[:200]
    if not db.audit("system", "channel_health", "alert_channels", detail):
        # 审计也写不进去时只剩日志这条退路（此时大概率两通道与库都在打架）
        logger.error("告警通道降级痕迹未能落审计链，请人工核查: %s", detail)


def _send_channel_health_report(force=False, *, alert_channel_status, status_lines,
                                send_notification):
    """告警通道健康报告（旧称"日报"；线程每日醒一次，例行只在 `_HEALTH_REPORT_WEEKDAY`
    那天播，通道降级或额度耗尽当天照发——要不要播由调用侧判定，本函数不重复判）。

    三件事：① 固定附一行两条通道当前状态（被关闭也要看得见"被关"）；
    ② 接线 `notify.pop_exhaustion_notice()`——当日有账本额度耗尽且尚未告知时，
    在此把行补进日报。这里的 pop 必须在 send_notification 之前：pop 是取走语义，
    先取走就不会再被本次 send_notification 内部的接线重复发一封（一次 pop 拿全列表）；
    ③ "日报本身不得依赖被改配置/进程内状态"：
      - 每日至多一封用 app_meta 落库去重（进程内 dict 重启即失效）；
      - 通道降级时**先落一条审计痕迹再尝试发信**——两条通道同时被拆时这封日报
        既发不出去也没有别的出口，没有库内痕迹就无法在事后证明"系统曾检测到
        通道被拆"。审计行走既有 HMAC 哈希链 + 库外锚点覆盖范围（app_meta 不在链内，
        故痕迹用 db.audit 而非只写 meta）。

    降级判定：一律取自结构化状态 _alert_channel_status() + pop 出的账本
    名列表，不看正文有没有 ⚠；痕迹摘要同样按结构化事实拼，两侧都记。

    返回 True 表示本次已排出一封日报（含"发不出去但痕迹已落库"），
    False 表示今日已播过、本次跳过。force=True 只越过"今日已播"判定，
    仍会写入标记——人工补发同样算当日那一封。
    发信抛异常时异常原样上抛（调用方记日志），且**不落**去重标记：当日稍后仍可重试。

    状态生产者、状态行生产者与发信出口都由调用方传入（`web.app` 的
    `_alert_channel_status` / `_channel_status_lines` / `send_notification`）：三者在
    `web.app` 上都是既有的打桩面（"纯文案改版"对拍、模拟 SMTP 瞬断），本模块另持一份
    绑定会让这些桩静默失效。
    """
    today = clock.now().strftime("%Y-%m-%d")
    if not force and _health_report_sent_today(today):
        logger.info("告警通道健康日报今日已播报（标记 %s），本次跳过", _HEALTH_REPORT_META_KEY)
        return False
    status = alert_channel_status()
    lines = status_lines(status)
    try:
        exhausted = notify.pop_exhaustion_notice() or []
    except Exception as e:  # 兜底：告知接线不得影响日报
        logger.warning("读取推送额度耗尽标记失败（日报内省略）: %s", e)
        exhausted = []
    for kind in exhausted:
        lines.append(
            f"⚠ 手机推送{_NOTIFY_LEDGER_LABELS.get(kind, kind)}额度今日已用尽，"
            "当日后续同类告警请查邮件（本行每日每本账各一次）"
        )
    # 审计链锚点随日报出箱：HMAC 链密钥、锚点文件、备份都在同一台机器上，
    # 这封日报是唯一每天离开这台机器的链状态记录——运维拿昨日邮件对照今日库，
    # 删链/篡改即可被发现。只读（不创建、不轮转锚点）；读取失败省略该行，
    # 不影响日报本体的发送与降级判定。
    try:
        # 三态读取：空链与"读失败"必须分开——把读失败当空链会打印"空链"（把"没查"
        # 印成"没有"），而那正是审计状态出问题最需要看见的时刻。
        state, head = db.audit_head_hash_ex()
        count = db.audit_row_count()
        if state == "error":
            logger.warning("审计链头读取失败（记录数 %d），日报内省略锚点行", count)
        else:
            desc = "空链" if state == "empty" else f"{head[:12]}…"
            lines.append(f"审计链锚点：head_hash={desc}（记录数 {count}）")
    except Exception as e:
        logger.warning("读取审计链锚点失败（日报内省略该行）: %s", e)
    # 降级口径：健康日不占紧急额度——例行日报若每天都吃掉一格紧急预算，反而会把真正
    # 的紧急告警挤出预算（那正是本次修复要治的"该响的不响"）。判据是两条出口是否都活着
    # （邮件可用且有收件人 + 推送可用）以及当日是否还有账本被用尽：攻击链第一步正是
    # "只关邮件"，此时日报必须还能从手机推送那条通道被听见。
    degraded = _channel_health_degraded(status, exhausted)
    report = mail_layout.Mail(
        summary="告警通道每日健康报告：两条通道状态、今日额度与审计链锚点。",
        items=lines,
        level="urgent" if degraded else "info",
    )
    facts = _channel_health_facts(status, exhausted)
    # 痕迹落在发信**之前**：下面这句 send_notification 在两条通道全断时既送不到也没
    # 回执，先落库才谈得上"无论是否发出都留痕"。摘要（facts）与下面的 meta 共用一份，
    # 两侧事实无条件都在，不再从正文里挑 ⚠ 行拼。
    if degraded:
        _audit_channel_health_degraded(facts)
    send_notification("告警通道健康日报", report, urgent=degraded)
    # 去重标记刻意落在发信**之后**：写在之前等于"今天只要想过一遍就
    # 永久不再试"——send_notification 抛异常或 SMTP 瞬断时，当天这封日报既没出去、
    # 标记又已落库，直到次日都不会再播，一次瞬断被放大成整天静默，与"宁可多播不少播"
    # 的取向相反。目标仍是"跨重启每日至多一封"（成功即落标记，重启不会各发一封），
    # 只是失败那一次不占名额：当日稍后（进程重启后的下一轮、或人工 force 补发）还能重试。
    # 不为此另起第三套状态存储——仍用同一个 app_meta 键，只是写入时机后移。
    db.set_meta(_HEALTH_REPORT_META_KEY, json.dumps(
        {"date": today, "ts": clock.now().strftime("%Y-%m-%d %H:%M:%S"),
         "degraded": degraded, "channels": len(lines),
         "summary": facts}, ensure_ascii=False))
    return True
