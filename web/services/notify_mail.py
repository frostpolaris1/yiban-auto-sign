# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""通知与告警邮件族：告警正文净化、审计事实、变更/审核邮件与双通道发送。

**功能**
告警正文插值净化 `_nl_safe`；审计行的 actor 取法 `_audit_actor` 与审计链异常告警的
事实清单 `_audit_alert_facts` / `_last_cleanup_text`；变更类告警正文唯一形状
`_change_mail`；审核拒绝通知 `_review_reject_mail`；A 线告警收件人唯一算法
`_alert_mail_recipients`；双通道发送出口 `send_notification`（邮件 + Webhook）；两个邮件
开关的中文名表 `_MAIL_FLAG_NAMES`、推送"是否曾配置过"的判据 `_push_ever_configured`。

**归属**
原 `web/app.py` 的模块级通知与邮件辅助，唯一真源在本模块；`web/app.py` 只保留名字面与
转发，把它自己持有、而本模块需要的模块级名字——`.env` 路径 `ENV_FILE`、读取器
`read_env`、同类型告警邮件节流 `_mail_alert_due`——在调用时刻现取后注入。

**复用**
`_alert_mail_recipients` 只留一份实现，由 `send_notification` 与
`web/services/channel_health.py` 的 `_alert_channel_status` 共用（分叉代价见其函数文档）。
`_nl_safe` 与 `_audit_alert_facts` / `_last_cleanup_text` 同样只留一份。

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
    防止请求体夹带换行在告警正文中伪造额外行。与 .env 写入侧同一个行模型，判据只留一份。
    """
    s = str(value).replace("\r", "\\r").replace("\n", "\\n")
    # 剩下 8 个分隔符（U+0085 / U+2028 …）也一并转义成 \uXXXX：邮件客户端与日志页同样
    # 会在它们处断行，只压 \r\n 等于留 8 条"在管理员告警里伪造一行"的口子
    for ch in sorted(_ENV_LINE_BREAK_CHARS - {"\r", "\n"}):
        s = s.replace(ch, f"\\u{ord(ch):04x}")
    return s


def _audit_actor():
    """审计行的 actor 唯一取法：当前会话用户名，缺省 `?`，截 64 防超长打爆索引列。

    actor 列只能有一种语义：这行操作是谁做的。写死某个固定名（哪怕就是 "admin"）会让
    事后按 actor 追人追错，也与全表其它行不可比。
    """
    return (session.get("username") or "?")[:64]  # 无会话（后台线程/脚本）时是 "?"，不假装有主


#: 独立见证形态 → 告警正文文本（缺失/不可读 = 降级：双写掩盖无独立证据）
_WITNESS_TEXT = {
    "separate": "跨权限独立文件已启用（属主与锚点不同）",
    "same-owner": "在位但与锚点同属主（降级：同 uid 双写者仍可一起改）",
    "unknown": "在位（属主未知）",
    "absent": "未启用（降级：同属主双写无独立证据）",
    "unreadable": "存在但不可读（降级：本次无法比对）",
    "corrupt": "存在但损坏（需人工核查）",
}


def _audit_alert_facts(health):
    """审计链异常告警的事实清单（每日线程用，测试直接断言同一份形状）。

    `诊断备注` 不能删：`audit_health` 有两种"链自洽=是、锚点=一致，但体检仍判不健康"
    的原因（锚点之后又跑了全表重链、有记录签名被清空等着被重签），它们只写进 `note`。
    不带出来时管理员收到的是一条"各项都正常"的告警，只能靠猜。

    `库外锚点` 对"无法定论"单独一档：它既不是"一致"也不是"不一致"，混淆会让管理员
    要么忽略一次真的没验成、要么把编码事故当成确证篡改去响应。
    """
    status = health.get("anchor_status")
    if status == "indeterminate":
        anchor_text = "无法定论（本次没验成，不等于无异常）"
    elif health["anchor_ok"]:
        anchor_text = "一致"
    else:
        anchor_text = "不一致"
    return [
        ("链自洽", "是" if health["chain_ok"] else f"否（断点 {health['broken']} 处）"),
        ("库外锚点", anchor_text),
        ("锚点说明", _nl_safe(health["anchor_msg"]) or "（无）"),
        # 独立见证的形态：跨权限存放是否生效。缺失/不可读 = 降级（同属主双写无独立证据）
        ("锚点独立见证", _WITNESS_TEXT.get(health.get("anchor_witness"), "（未知）")),
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

    "唯一形状"是要守住的不变量：同类告警若各处各写一套（冒号有无、时间位置不同），
    管理员扫不动。时间由排版层统一收口在末尾，这里不自己拼。

    `operator` 缺省取当前会话用户；调用方已有目标用户名（如权限变更用的是局部
    `username`）时显式传入，避免在路由里再拼一遍字段。
    """
    fields = list(detail or [])
    fields.append(("操作者", _nl_safe(
        session.get("username", "?") if operator is None else operator)))
    return mail_layout.Mail(summary=summary, fields=fields, advice=advice, level=level)


def _review_reject_mail(phones, reason):
    """审核拒绝通知的正文——单条与批量共用这一份，两路不可能再漂移。

    "被拒账号"必须在列：少了它，用户收到拒信却不知是哪一行被拒，只能挨个点开
    「我的账号」页看状态。账号与审核理由都过 `_nl_safe`：理由来自管理员表单
    （外部输入），换行不转义时一封纯文本拒信可以被拆出伪造行（如假造一条签名或说明）。
    """
    return mail_layout.Mail(
        summary="您提交的易班账号未通过管理员审核。",
        fields=[
            ("被拒账号", _nl_safe("、".join(phones)) if phones else "（见「我的账号」页）"),
            ("审核理由", _nl_safe(reason) if reason else "管理员未填写，可联系管理员了解详情"),
        ],
        advice=["登录后在「我的账号」页修改并重新提交，重新提交将再次进入审核"],
    )


def _alert_mail_recipients():
    """A 线告警邮件的收件人（唯一算法）：ADMIN_TO（受个人接收开关约束）+ 开启接收的管理员。

    刻意只留一份实现，由 send_notification 与
    web/services/channel_health.py 的 _alert_channel_status() 共用：通道健康判据要回答的
    是"这一封日报到底发不发得出去"，它与 send_notification 实际取收件人的算法必须严格
    一致，各算一套就会分叉——"只关 admin_notify 且无其他接收管理员"这个组合变体
    正是"邮件通道看着全绿、收件人却为空"，判据若另算一份就会报成"一切正常"。
    """
    # 两个来源都为空时这里就是空收件人——channel_health 必须与这一行同判，
    # 否则它会把"通道全绿、其实没人收得到"报成一切正常
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
    # recipients 排在最前是有意的：and 短路让"收件人为空"这一路不去调
    # mail_alert_due，于是不登记时间戳、不白占一个节流窗口
    if recipients and (force or mail_alert_due(title)):
        mailer.send_admin_alert(title, content, to=",".join(recipients))
    elif recipients:
        logger.info("告警邮件已节流（同类 %s 在窗口内已发送，本次仅通知 webhook）", title)
    # Webhook 推送组件化（Server酱/自定义 URL；未配置 / 节流命中时静默跳过）
    notify.send(title, mail_layout.as_text(content), urgent=urgent, force=force, ledger=ledger)
    # 推送额度耗尽不再在这里"补一封"：告知并进通道健康报告（web/services/channel_health.py
    # 的 _send_channel_health_report 会取走待告知标记并写进报告正文），与其余通道状态
    # 同源同频——耗尽告知本身是"通道状态"的一部分，挂在每条告警后面只会让它在主告警
    # 之外又刷一层。


_NOTIFY_LEDGER_LABELS = {"general": "非紧急", "urgent": "紧急", "login_fail": "登录失败告警"}


# 两个邮件开关的中文名表（env_key → 可读名）：高危动作标签按字段区分用，
# 避免同一件事在两个地方各写一套字面量
_MAIL_FLAG_NAMES = {
    "YIBAN_MAIL_ENABLE": "全局邮件通知",
    "YIBAN_MAIL_ADMIN_NOTIFY": "主管理员个人接收",
}


# 判定"推送这路是否曾配置过"的最轻事实来源：.env 里这两个键**存在且值非空**。
# 口径：两键都不存在、或都在而值都为空 ⇒ 按"从未配置"处理，不算降级；
# 设置页关闭通道会把两键一并删掉、手工"清空"则写成 `KEY=` 空值行，两者在配置文件里
# 同形，一律落进"从未配置"这个合法终态。
_PUSH_CONFIG_ENV_KEYS = ("YIBAN_NOTIFY_TYPE", "YIBAN_NOTIFY_SECRET_ENC")


def _push_ever_configured(env_file, read_env, envs=None):
    """手机推送通道在本部署历史上是否配置过（降级判据输入）。

    降级只该回答"本应可用的出口现在不可用"，所以要分清"从未配过"与"配过又拆了"：
    把"未配置"也判成降级，邮件单通道这一刻意的终态配置就会每天落一条降级痕迹、日报
    每天挂 urgent——天天喊等于没人听，真降级时反而看不出。
    刻意复用现成的 .env 解析结果，**不新增 app_meta 键、不新建状态存储**："拆掉推送"
    这个动作本身已由 notify_config 审计行 + urgent 变更播报留痕，日报不必对一个已经
    安静消失的通道反复定性。代价照实记下：设置页"关闭推送"会删掉两个键行，此后日报
    不再因此挂降级旗标，痕迹只剩动作当时那一条审计 + 播报。
    .env 整个读不到（文件不存在）时按"可能配过"处理：宁可多判一次降级留痕，不可静默
    当健康——与调用方对"收件人读取失败"的取向一致。

    参数注入口径见模块头「通信」（`ENV_FILE` / `read_env`）。
    """
    if envs is None:
        if not os.path.exists(env_file):
            return True
        envs = read_env(env_file)
    return any(str(envs.get(k) or "").strip() for k in _PUSH_CONFIG_ENV_KEYS)
