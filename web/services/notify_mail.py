# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""通知与告警邮件族：告警正文净化、审计事实、变更/审核邮件与双通道发送。

**功能**
告警正文插值净化 `_nl_safe`；审计行的 actor 取法 `_audit_actor` 与审计链异常告警的
事实清单 `_audit_alert_facts` / `_last_cleanup_text`；变更类告警正文唯一形状
`_change_mail`；审核拒绝通知 `_review_reject_mail`；A 线告警收件人唯一算法
`_alert_mail_recipients`；双通道发送出口 `send_notification`（邮件 + Webhook，含推送
额度耗尽告知 `_exhaustion_notice_mail`）；两个邮件开关的中文名表 `_MAIL_FLAG_NAMES` 与
描述 `_mail_flags_desc`、消息推送配置变更描述 `_notify_change_desc`、推送"是否曾配置过"
的判据 `_push_ever_configured`。

**归属**
原 `web/app.py` 的模块级通知与邮件辅助，唯一真源在本模块；`web/app.py` 只保留名字面与
转发，把它自己持有、而本模块需要的模块级名字——`.env` 路径 `ENV_FILE`、读取器
`read_env`、同类型告警邮件节流 `_mail_alert_due`——在调用时刻现取后注入。

**复用**
`_alert_mail_recipients` 只留一份实现，由 `send_notification`、`_exhaustion_notice_mail`
与 `web/services/channel_health.py` 的 `_alert_channel_status` 共用：通道健康判据要回答的
是"这一封日报到底发不发得出去"，它与 `send_notification` 实际取收件人的算法必须严格
一致，各算一套就会分叉。`_nl_safe` 与 `_audit_alert_facts` / `_last_cleanup_text` 同样
只留一份；`_exhaustion_notice_mail` 刻意不经 `send_notification`，避免与本函数互相递归。

**通信**
本模块不反向导入 `web.app`（本仓测试以别名加载 `app.py`，普通 import 会再执行一份副本
模块）。`.env` 路径与读取器由调用方传入；同类型告警邮件节流 `_mail_alert_due` 同样作为
显式参数接收：它在 `web.app` 上会被测试打桩（`mock.patch.object(webapp, "_mail_alert_due")`），
本模块另持一份绑定会让这个桩静默失效。段落净化用的行分隔符字符集直接取
`yiban.infra.env_io` 的真源 `ENV_LINE_BREAK_CHARS`，与本仓 `.env` 写入侧同源，不另抄一份。
"""

import logging
import os

from flask import session

from yiban import mail as mailer
from yiban import notify
from yiban.infra.env_io import ENV_LINE_BREAK_CHARS as _ENV_LINE_BREAK_CHARS
from yiban.mail import layout as mail_layout
from yiban.store import db

# 与 web.app 同名的日志通道：本族的告警落回既有通道，便于运维沿用同一处过滤
logger = logging.getLogger("web")


def _nl_safe(value):
    """告警正文插值净化（与 .env 行模型同源）。

    外部可控字段（用户名/邮箱/IP 等）拼进邮件或通知正文前把换行转义成字面量，
    防止请求体夹带换行在告警正文中伪造额外行。

    字符集刻意取 `_ENV_LINE_BREAK_CHARS`（= `str.splitlines()` 的全部 10 个分隔符）
    而不是只压 `\r\n`：邮件客户端与网页日志页同样会在 `U+0085`/`U+2028` 处断行，
    只压两个等于留 8 条"在管理员告警里伪造一行'操作者: admin'"的口子——与 .env
    写入侧是同一个行模型，判据只留一份。
    """
    s = str(value).replace("\r", "\\r").replace("\n", "\\n")
    for ch in sorted(_ENV_LINE_BREAK_CHARS - {"\r", "\n"}):
        s = s.replace(ch, f"\\u{ord(ch):04x}")
    return s


def _audit_actor():
    """审计行的 actor 唯一取法：当前会话用户名，缺省 `?`，截 64 防超长打爆索引列。

    执行体那几处此前把 actor **硬编码成 `"admin"`**——审计表里出现了一句假话：
    谁做的操作没被记下来，且与全表其他行的口径不一致（同一列两种语义，事后按
    actor 追人时"admin"既可能是内置管理员也可能是别的账号）。
    """
    return (session.get("username") or "?")[:64]


def _audit_alert_facts(health):
    """审计链异常告警的事实清单（每日线程用，测试直接断言同一份形状）。

    `诊断备注` 必须在列：`audit_health` 有两种"链自洽=是、锚点=一致，但体检仍判不健康"
    的原因（锚点之后又跑了全表重链、有记录签名被清空等着被重签），它们只写进 `note`。
    不带出来时管理员看到的是一条"各项都正常"的告警，第一反应是误报——正是这次要修的。
    """
    return [
        ("链自洽", "是" if health["chain_ok"] else f"否（断点 {health['broken']} 处）"),
        ("库外锚点", "一致" if health["anchor_ok"] else "不一致"),
        ("锚点说明", _nl_safe(health["anchor_msg"]) or "（无）"),
        ("审计写入失败次数", health["write_failures"]),
        # 清理量出箱（异机核对用）：本机时钟被渐进拨快时，本机自校验不会报警，
        # 但"累计删除条数"与"最近一次清理的截止点"会持续变化——日报是唯一能把它
        # 带离本机的通道，异机侧据此判断清理是否异常。
        ("累计留痕的审计清理条数", health.get("purge_total", 0)),
        ("最近一次审计清理", _last_cleanup_text(health.get("last_cleanup"))),
        ("诊断备注", _nl_safe(health["note"]) or "（无）"),
    ]


def _last_cleanup_text(ev):
    """最近一次审计清理留痕的可读文本（无记录 → 「（无）」）。"""
    if not ev:
        return "（无）"
    return _nl_safe(
        f"{ev.get('ts') or '?'} 截止 {ev.get('cutoff') or '?'}，"
        f"删除 {ev.get('deleted', 0)} 条"
    )


def _change_mail(summary, detail=None, operator=None, advice=None, level="urgent"):
    """变更/操作类告警正文的唯一形状：事件 → 明细字段 → 操作者 → 时间。

    原先 12 处各写一遍 `"…，操作者 X，时间 Y"`，冒号有无、逗号位置、时间写法
    （`时间: X` 与 `时间 X`）全都不一致——同类告警在管理员眼里长得不一样，
    扫不动。收成一份后只剩这一种形状；时间由排版层统一收口在末尾。

    `operator` 缺省取当前会话用户；调用方已有目标用户名（如权限变更用的是局部
    `username`）时显式传入，避免在路由里再拼一遍字段。
    """
    fields = list(detail or [])
    fields.append(("操作者", _nl_safe(
        session.get("username", "?") if operator is None else operator)))
    return mail_layout.Mail(summary=summary, fields=fields, advice=advice, level=level)


def _review_reject_mail(phones, reason):
    """审核拒绝通知的正文——单条与批量共用这一份，两路不可能再漂移。

    批量分支原先自己另写了一段，且**不写被拒账号**：用户收到拒信却不知道是
    哪一行被拒，只能挨个点开「我的账号」页看状态。
    """
    return mail_layout.Mail(
        summary="您提交的易班账号未通过管理员审核。",
        fields=[
            ("被拒账号", "、".join(phones) if phones else "（见「我的账号」页）"),
            ("审核理由", reason or "管理员未填写，可联系管理员了解详情"),
        ],
        advice=["登录后在「我的账号」页修改并重新提交，重新提交将再次进入审核"],
    )


def _alert_mail_recipients():
    """A 线告警邮件的收件人（唯一算法）：ADMIN_TO（受个人接收开关约束）+ 开启接收的管理员。

    刻意只留一份实现，由 send_notification、_exhaustion_notice_mail 与
    web/services/channel_health.py 的 _alert_channel_status() 共用：通道健康判据要回答的
    是"这一封日报到底发不发得出去"，它与 send_notification 实际取收件人的算法必须严格
    一致，各算一套就会分叉——"只关 admin_notify 且无其他接收管理员"这个组合变体
    正是"邮件通道看着全绿、收件人却为空"，判据若另算一份就会报成"一切正常"。
    """
    extra = mailer.admin_recipients() if mailer.admin_notify_enabled() else []
    return db.admin_mail_recipients(extra)


def send_notification(title, content, urgent=False, force=False, ledger=None, *,
                      mail_alert_due):
    """发送告警通知（A 线邮件 + Webhook 双通道，任一失败不影响另一路）。

    `content` 可以是 `mail_layout.Mail`（邮件出纯文本+HTML 两版、推送取 Markdown 出口，
    一份声明两路同源）或普通字符串（两路原样透传，与改版前逐字一致）。

    - 邮件：SMTP 管理员告警（同类型节流，见 `mail_alert_due`）。收件人 = ADMIN_TO
      （按个人开关过滤）+ 所有开启接收的管理员用户邮箱；主管理员关闭
      YIBAN_MAIL_ADMIN_NOTIFY 后不再收 ADMIN_TO 邮件。邮件不受 urgent 影响，始终发送。
    - Webhook：`yiban/notify` 组件（Server酱/自定义 URL，加密配置 +
      同类型节流 + 每日预算 + 响应检查，兼容旧明文 YIBAN_NOTIFY_URL）。未配置则静默跳过。
      urgent=True 标记重要告警：设置页开启「仅推送重要告警」后，仅 urgent 通知会推手机，
      其余（用户日常改密/签到结果类等）仅走邮件，把推送额度留给真正威胁系统/账号安全的事件。
    - force=True：跳过两侧节流（邮件同类节流 + webhook 的
      节流/每日额度/仅重要开关），供"先告警后落盘"的配置变更告警等**必须送达**的
      场景使用——此刻额度/节流参数仍为旧值，告警不会被本次刚提交的新参数吞掉。
      默认 False，向后兼容（既有调用方行为不变）。
    - ledger：None = 现行行为（按 urgent 归入 general/urgent 两本账）；
      "login_fail" = 登录失败告警独立账本，日额度 YIBAN_LOGINFAIL_DAILY_MAX
      （默认 3，0=不限），与 general/urgent 互不挤占——登录失败是公网最高频的
      告警源，独占账本后喷洒类攻击烧不光紧急账的额度。

    同类型告警邮件节流 `mail_alert_due` 由调用方传入（`web.app` 的 `_mail_alert_due`）：
    它是既有测试的打桩点（`mock.patch.object(webapp, "_mail_alert_due")`），本模块另持一份
    绑定会让那个桩静默失效。
    """
    recipients = _alert_mail_recipients()
    # 高危告警邮件节流：同类标题在窗口内只发一封（防被盗会话反复触发高危操作耗尽
    # SMTP 额度）；webhook 由 yiban.notify 独立节流。force=True 时绕过（必须送达场景）
    if recipients and (force or mail_alert_due(title)):
        mailer.send_admin_alert(title, content, to=",".join(recipients))
    elif recipients:
        logger.info("告警邮件已节流（同类 %s 在窗口内已发送，本次仅通知 webhook）", title)
    # Webhook 推送组件化（Server酱/自定义 URL；未配置 / 节流命中时静默跳过）
    notify.send(title, mail_layout.as_text(content), urgent=urgent, force=force, ledger=ledger)
    # 手机推送额度耗尽的"补一封"——notify 侧当日首次有账本耗尽时会挂上
    # 待取走标记，pop_exhaustion_notice() 一次返回全部耗尽账本（如 ["general","urgent"]）。
    # 必须一次取完拼成一封：循环 pop 到空会让两本账同日各发一封（重复打扰）。
    # 告知只走邮件（推送额度正是刚用尽的东西），且整段兜异常——耗尽告知属附加信息，
    # 绝不能把本次主告警带崩。
    try:
        exhausted = notify.pop_exhaustion_notice()
    except Exception as e:  # 兜底：告知接线不得影响本次主告警
        logger.warning("读取推送额度耗尽标记失败（忽略）: %s", e)
        exhausted = None
    if exhausted:
        try:
            _exhaustion_notice_mail(exhausted)
        except Exception as e:  # 兜底：同上
            logger.warning("推送额度耗尽告知邮件发送失败: %s", e)


_NOTIFY_LEDGER_LABELS = {"general": "非紧急", "urgent": "紧急", "login_fail": "登录失败告警"}


def _exhaustion_notice_mail(kinds):
    """手机推送额度耗尽告知：一封邮件写清哪几本账耗尽、上限是多少。

    kinds 为 notify.pop_exhaustion_notice() 返回的账本名列表（"general"/"urgent"），
    每本账每日各一次，故本函数每天最多被调用两次且不会重复发同一本。
    只走邮件通道（不经 send_notification，避免与本函数互相递归）。
    """
    try:
        cfg = notify.get_config()
    except Exception:  # 兜底：取额度概览失败时按"未知"出文，不抛
        cfg = {}
    max_keys = {"general": "daily_max", "urgent": "urgent_daily_max"}
    parts = []
    for kind in kinds:
        label = _NOTIFY_LEDGER_LABELS.get(kind, kind)
        limit = cfg.get(max_keys.get(kind, ""))
        has_cap = isinstance(limit, int) and limit > 0
        tail = f"（今日上限 {limit} 条已全部用尽）" if has_cap else "（今日额度已用尽）"
        parts.append(f"{label}推送额度已用尽{tail}")
    report = mail_layout.Mail(
        summary="手机消息推送今日额度已用尽，当日后续同类告警不再推手机。",
        items=parts,
        advice=["请改查管理员告警邮件（邮件通道不受影响）",
                "如需调整请在 .env 修改 YIBAN_NOTIFY_DAILY_MAX / "
                "YIBAN_NOTIFY_URGENT_DAILY_MAX（0=不限），或关闭「仅推送重要告警」"],
        level="warn",
    )
    logger.warning("手机推送%s，已补发告知邮件", "、".join(parts))
    recipients = _alert_mail_recipients()
    if not recipients:
        logger.warning("推送额度耗尽告知无法送达（邮件收件人为空），请登录后台自查推送配置")
        return
    mailer.send_admin_alert("手机推送额度已用尽告警", report, to=",".join(recipients))


# 两个邮件开关的中文名表（env_key → 可读名）：变更告警文案与高危动作标签共用，
# 避免同一件事在两个地方各写一套字面量（告警标签必须按字段区分）
_MAIL_FLAG_NAMES = {
    "YIBAN_MAIL_ENABLE": "全局邮件通知",
    "YIBAN_MAIL_ADMIN_NOTIFY": "主管理员个人接收",
}


def _mail_flags_desc(flags):
    """邮件开关变更集（env_key → bool）→ 告警正文可读描述。

    文案里带上"具体改了什么"：运营者只看一行标题无法判断是
    全局关停下线、还是主管理员个人收件被拔线，两者的处置动作完全不同。
    键名来自代码常量（非外部输入），无注入面。
    """
    return "；".join(
        f"{_MAIL_FLAG_NAMES.get(k, k)}：{'开启' if v else '关闭'}" for k, v in flags.items()
    )


def _notify_change_desc(ntype, close_channel, clear_secret, swap_secret, numeric):
    """消息推送配置变更集 → 告警正文可读描述。

    关闭通道与"只是换了个数"在告警里必须一眼可辨：前者是攻击者掩盖痕迹的必经动作，
    后者是日常调参。ntype 已过白名单校验、numeric 为 int/bool，均无注入面。
    """
    parts = []
    if close_channel:
        parts.append("通道：关闭（⚠ 告警不再推手机）")
    elif ntype:
        parts.append(f"通道：{ntype}")
    if swap_secret:
        parts.append("密钥：已更换")
    elif clear_secret and not close_channel:
        parts.append("密钥：已清空（⚠ 通道随之失效）")
    labels = {
        "cooldown": "节流秒数", "urgent_only": "仅重要告警",
        "daily_max": "非紧急每日上限", "urgent_daily_max": "紧急每日上限",
    }
    for key, value in numeric.items():
        shown = ("开启" if value else "关闭") if isinstance(value, bool) else value
        parts.append(f"{labels.get(key, key)}：{shown}")
    return "；".join(parts) or "无实质变更"


# 判定"推送这路是否曾配置过"的最轻事实来源：.env 里这两个键**存在且值非空**。
# 口径：两键都不存在、或都在而值都为空 ⇒ 按"从未配置"处理，不算降级；
# 设置页关闭通道会把两键一并删掉、手工"清空"则写成 `KEY=` 空值行，两者在配置文件里
# 同形，一律落进"从未配置"这个合法终态。
_PUSH_CONFIG_ENV_KEYS = ("YIBAN_NOTIFY_TYPE", "YIBAN_NOTIFY_SECRET_ENC")


def _push_ever_configured(env_file, read_env, envs=None):
    """手机推送通道在本部署历史上是否配置过（降级判据输入）。

    旧口径把"推送未配置"一并判为降级，于是**邮件单通道**这一刻意的终态配置每天落一条
    channel_health 降级痕迹、日报每天挂 ⚠/urgent —— 天天喊降级就是告警疲劳，真出事时
    这条痕迹反而没人看。降级只该回答"本应可用的出口现在不可用"，因此需要一个
    "是否曾配置"的事实来源。刻意复用现成的 .env 解析结果，**不新增 app_meta 键、不新建
    状态存储**："把推送配置拆掉"这个**动作**（设置页关闭/清钥、或直接改文件）本身已由
    notify_config 审计行 + urgent=True 变更播报覆盖，日报无需对一个
    已经安静消失的通道天天重复定性。
    代价照实记下：管理员用设置页"关闭推送"后两个键行都被删除，此后日报不再因此挂
    降级旗标——该动作发生当时那一条 notify_config 审计 + urgent 播报就是痕迹本体。
    .env 整个读不到（文件不存在）时按"可能配过"处理：宁可多判一次降级留痕，不可静默
    当健康——与本函数调用方对"收件人读取失败"的取向完全一致。

    `.env` 路径与读取器由调用方传入（`web.app` 的 `ENV_FILE` 与 `read_env`）：
    两者都是会被测试改写、也会随 `--config` 变化的模块级名字。
    """
    if envs is None:
        if not os.path.exists(env_file):
            return True
        envs = read_env(env_file)
    return any(str(envs.get(k) or "").strip() for k in _PUSH_CONFIG_ENV_KEYS)
