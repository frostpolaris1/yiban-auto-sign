# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""**功能**
告警与邮件：管理员汇总邮件、用户失败提醒、Webhook 推送。

两条通道（口径不同，勿混）：
- **A 线（管理员）**：运行期 `_collect_admin_mail` 只收集，任务收尾
  `_flush_admin_mail_summary` 汇总成一封发出——避免多账号失败时逐封轰炸；收件人为空
  且推送已配置时，同一份汇总改走 webhook 兜底（紧急 + 绕过节流），不让告警静默全灭；
- **B 线（用户本人）**：`send_user_fail_mail` 逐条即时，受按天额度（默认每账号 1 封，
  签到/手动/探针三个入口统一计算）与用户开关约束，**发送成功才消耗额度**，未发出即归还。

`mailer` / `notify` 是本包内的实现（`from yiban import mail as mailer`、`from yiban import notify`），
发送失败一律只留痕，绝不抛出，也不影响退出码。

**归属**
`yiban.engine` 的告警通道层；写汇总条目的调用方是 `round` / `executor_v3`（签到成败）、
`schedule`（窗口配置异常）、`store.claims`（领取池异常）与 `probe`（健康探测），
`runner` 只负责收尾发送。

**复用**
`_collect_admin_mail` / `_flush_admin_mail_summary`（A 线汇总）、`send_user_fail_mail`
（B 线逐条）、`send_notification` 族与额度口径被三个入口共用；`STATUS_*` 取自
`yiban.status`。

**通信**
输入：本轮结果（状态码、账号、失败原因）、收件人/开关/额度配置（来自 .env 与 db）。
输出：SMTP 邮件、webhook 推送与 `sign_events` 留痕；**只留痕不抛出**，不影响退出码。
调用谁：`mail`（`mailer`）、`notify`、`db`、`state_io`、`cli_support`、`schedule`。
谁调用：`round`、`probe`、`runner`、`schedule`、`store.claims`、`executor_v3`。
前端调用点：邮件/推送配置与"发送测试"由 `/api/mail-config`、`/api/notify-config`、
`/api/notify-test`（`web/static/js/components/settings-notify.js` 等设置页）管理——告警通道或措辞
变化会影响用户收到的邮件/推送。
跨模块一律走模块属性访问。
"""
import contextlib
import json
import logging
import os
import sys

from yiban import clock, notify, window
from yiban import mail as mailer
from yiban import status as yiban_status
from yiban.engine import cli_support, config_check, schedule, state_io
from yiban.infra import env_io
from yiban.mail import layout
from yiban.masking import mask_phone as _mask_phone
from yiban.masking import sanitize_text as _sanitize_text
from yiban.store import db

logger = logging.getLogger("yiban")

# 状态码别名（与 yiban.status 同一对象）
STATUS_SKIPPED_WINDOW = yiban_status.STATUS_SKIPPED_WINDOW
STATUS_SKIPPED_NORANGE = yiban_status.STATUS_SKIPPED_NORANGE


# ---------------------------------------------------------------------------
# 消息通知
# ---------------------------------------------------------------------------
def send_notification(title, content, url=None, urgent=False, force=False):
    """通过 Webhook 推送组件发送通知（Server酱/自定义 URL，见 `yiban/notify`）。

    `content` 可以是 `layout.Mail`（取 Markdown 出口，短通道自带裁剪）或普通字符串
    （原样透传）。透传 urgent/force 到 notify.send：汇总邮件发送失败降级 webhook 时以
    urgent=True + force=True 调用（绕过节流与当日额度，保证兜底必达）；默认 False。
    `url` 保留旧调用签名，推送组件自行从配置/环境变量解析地址。
    说明：签到脚本给管理员的**邮件**不在此处发送（避免逐条轰炸），而是由各触发点
    _collect_admin_mail 收集、任务结束 _flush_admin_mail_summary 汇总。
    """
    try:
        notify.send(title, layout.as_text(content), urgent=urgent, force=force)
    except Exception as e:
        # 组件异常不得拖累签到主流程；只记类型名（异常文本可能含 URL/token）
        logger.warning("通知推送组件调用失败: %s", type(e).__name__)


# A 线：运行期把"发给管理员"的邮件先收集，任务结束统一汇总发送
# （避免多账号失败时逐封轰炸）。B 线用户邮件不在此收集，保持逐条即时。
_mail_summary = []  # list[(subject, text)]

# 汇总邮件条数/体积封顶：巨量账号全失败时不封顶会生成超大 MIME 被 SMTP 拒收，
# 整封告警丢失。截断部分指引看后台日志。
MAIL_SUMMARY_MAX_ENTRIES = 200
MAIL_SUMMARY_MAX_CHARS = 200_000


def _collect_admin_mail(subject, text):
    """把一条管理员告警并入任务结束汇总（不立即发送）。

    `text` 可以是 `[(标签, 值)]` 字段表（汇总时一项一行）或普通字符串（作为一段说明，
    兼容既有调用与测试）。
    """
    _mail_summary.append((subject, text))


def notify_admin_entry(subject, entry, notify_url=None, push=True):
    """一条管理员告警并入任务结束汇总邮件；`push=True` 时同时即时推送。

    两路读同一份 `entry`：原先各调用点要把同一条 `"账号: X\n原因: Y"` 字面量写两遍
    （邮件一遍、推送一遍），改一处必漏另一处，收成一次调用后只维护一份字段表。

    `push=False` 用于"事后可读"的慢信号（单账号耗时、容量超载）：它们的价值在汇总信
    正文里，即时推送只是把同一件事再喊一遍；而推送日额度有限，要留给"现在就得知道"
    的故障（签到失败、通道降级）。探针预警与窗口配置异常本就只进汇总，与此同口径。
    """
    _collect_admin_mail(subject, entry)
    if push and notify.is_configured():
        send_notification(subject, layout.Mail(fields=entry, time=""), notify_url)


def _alert_slow_sign(phone, dur, slow_sec, status, message):
    """单次尝试耗时超阈值 → warning 日志 + 管理员汇总邮件（不即时推送）。

    堆队列与手动队列两个分支共用，统一口径防漂移；字段表只建一次。
    """
    logger.warning(f"[{phone}] ⏱️ 签到耗时 {dur:.1f}s 超过阈值 {slow_sec}s（结果: {status}）")
    notify_admin_entry("易班签到耗时告警", [
        ("账号", _mask_phone(phone)),
        ("耗时", f"{dur:.1f}s（阈值 {slow_sec}s）"),
        ("结果", _sanitize_text(message)),
    ], push=False)


def _maybe_alert_zero_success(accounts, results, ok_n, is_second_run=None):
    """窗口外未了结账号的管理员告警。

    场景：学校签到窗口晚于本地配置（或 Range 延迟放出），账号落
    skipped_window/skipped_norange。容器调度闸门已把 skip 类
    计入未了结使补签得以重跑；宿主 run.sh 退出码语义同样保证补签
    不被「部分成功」吞掉。

    告警时机（避免误报噪音）：
      - 零成功（ok_n==0）且存在窗口外跳过：任何轮次都告警（全员窗口外 =
        当天可能无签，必须当天知情）；
      - 部分成功 + 窗口外跳过：只有在"**窗口已关**或**后面不会再有人跑**"时才告警。
        前面还会重试（补签轮未到 / 兜底执行体在跑）时不打扰管理员。

    `is_second_run` 参数保留给调用方表达"本轮是不是补签轮"，但抑制判据**不再依赖它**
    （多执行体下轮次身份不再可靠，见下）；它仅用于告警文案/日志语境。

    返回是否产生了告警（测试用）。
    """
    if not accounts:
        return False
    window_skips = [
        acc.phone for acc in accounts
        if results.get(acc.phone, (False, "", False, ""))[3]
        in (STATUS_SKIPPED_WINDOW, STATUS_SKIPPED_NORANGE)
    ]
    if not window_skips:
        return False
    if is_second_run is None:
        is_second_run = state_io._sched_marker_exists()
    # 抑制的判据不是"猜这是第几轮"，而是两个事实：
    #   ① 窗口还开着——账号理论上还签得上；
    #   ② 后面还有没有人接着跑——补签轮还没到（时刻事实），或兜底执行体在跑（心跳事实）。
    # 两个都成立才抑制：这时打扰管理员没有意义（马上会重试）。
    # 之所以不看 is_second_run：多执行体形态下"轮次身份"不再可靠——兜底执行体会一直
    # 重试到窗口关闭，此时即便挂着补签轮身份也没必要告警；反之（没兜底、补签轮也过了）
    # 必须告警，因为当天不会再有触发了。
    _now = clock.now()
    _alive, _ = state_io.fallback_alive()
    _later_round = (_now.hour, _now.minute) < window.retry_hm() or _alive
    _window_open = not schedule._window_closed(schedule._schedule_config(), _now)
    if ok_n > 0 and _later_round and _window_open:
        return False
    title = "当日签到异常告警" if ok_n == 0 else "签到窗口异常告警"
    if ok_n == 0:
        entry = [
            ("成功账号", "0 个"),
            ("窗口外/Range 跳过", f"{len(window_skips)} 个"),
            ("请核查", "YIBAN_SIGN_START / YIBAN_SIGN_END 与学校实际放号窗口是否匹配"
                       "（容器部署另需确认 YIBAN_RUN_TIMEOUT_SEC 未过早截断子进程）"),
        ]
    else:
        entry = [
            ("成功账号", f"{ok_n} 个"),
            ("未了结", f"{len(window_skips)} 个账号因窗口外/Range 缺失未签到（补签轮后仍未了结）"),
            ("请核查", "YIBAN_SIGN_START / YIBAN_SIGN_END 与学校实际放号窗口是否匹配"),
        ]
    _collect_admin_mail(title, entry)
    return True


def _flush_admin_mail_summary(phase=None):
    """签到任务结束：把运行期收集的管理员邮件汇总成一封发送。

    无异常则不发送（成功不打扰）；按主题分组，每个账号独立条目；
    条数超过 MAIL_SUMMARY_MAX_ENTRIES 或正文超长时截断并在尾部注明，
    明细以按天签到日志为准；mailer 内部静默失败，不影响退出码。
    收件人集为空且推送通道已配置时，同一份汇总改走推送兜底（urgent+force）——
    「无收件人」本身不得成为第二处静默点，零成功且零收件人的一轮仍可被观测。
    发送后清空收集器。

    phase：任务阶段标签。定时签到缺省 None → 沿用「签到任务」文案；
    探针调用传「健康探测」，避免复用造成「并无当日签到却报签到结束」的误导。
    """
    if not _mail_summary:
        return
    total = len(_mail_summary)
    entries = _mail_summary[:MAIL_SUMMARY_MAX_ENTRIES]
    truncated = total - len(entries)
    groups = {}
    order = []
    for subject, text in entries:
        if subject not in groups:
            groups[subject] = []
            order.append(subject)
        groups[subject].append(text)
    footer = []
    if truncated > 0:
        footer.append(
            f"其余 {truncated} 条已截断以免邮件过大被拒收，"
            "明细见管理后台「日志」页或 /var/log/yiban 按天日志"
        )
    if phase:
        summary = f"易班{phase}已完成，共 {total} 条异常/预警。"
    else:
        summary = f"易班签到任务已结束，共 {total} 条异常/预警。"
    mail = layout.Mail(summary=summary, groups=[(s, groups[s]) for s in order],
                       footer=footer, level="urgent")
    body = mail.to_plain()
    payload = mail
    if len(body) > MAIL_SUMMARY_MAX_CHARS:
        # 超长只可能在数百条明细时出现：那种量级下放弃 HTML、整封按纯文本截断送出，
        # 也好过生成超大 MIME 被 SMTP 拒收而整封告警丢失。
        body = body[:MAIL_SUMMARY_MAX_CHARS].rstrip() + "\n…（超长截断，明细见日志）"
        payload = body
    # 收件人 = ADMIN_TO（按个人开关过滤） + 所有开启接收的管理员用户邮箱：
    # 普通管理员自动获得告警收件权；关闭 mail_notify 后从收件人剔除。
    # 内置主管理员关闭 YIBAN_MAIL_ADMIN_NOTIFY 后不再收 ADMIN_TO 邮件。
    extra = mailer.admin_recipients() if mailer.admin_notify_enabled() else []
    recipients = db.admin_mail_recipients(extra)
    if recipients:
        sent = False
        try:
            sent = mailer.send_admin_alert("易班签到汇总", payload, to=",".join(recipients))
        except Exception as e:
            # mailer 自身承诺内部静默，此处兜底防调用链变化引入的异常外泄
            logger.warning("签到汇总邮件发送异常（%s），降级走 webhook", type(e).__name__)
        if not sent:
            # 邮件通道不可用（未配置/发送失败/异常）→ webhook 兜底（urgent+force
            # 绕过节流与当日额度），不再单点依赖 SMTP 可用性。
            send_notification("易班签到汇总", body, urgent=True, force=True)
    else:
        # 收件人集为空时不能静默跳过（告警"看起来发了"实则全灭，且无从排障）：
        # 显式 warning 留痕；推送通道已配置时把同一份汇总整卷改推（urgent+force
        # 绕过节流与当日额度），使"零成功 + 无收件人"的一轮仍可观测；推送也未
        # 配置时无事可做，仅留痕供日志页/状态页排查。
        logger.warning(
            "签到汇总告警无可用收件人（ADMIN_TO 与开启接收的管理员均为空），"
            "%d 条告警未走邮件通道，请检查邮件配置", total,
        )
        if notify.is_configured():
            send_notification(
                "易班签到汇总", "邮件无可用收件人，改推：\n" + body, urgent=True, force=True,
            )
    _mail_summary.clear()


def _flush_mail_on_sigterm(signum, frame):
    """SIGTERM（run.sh timeout / 手动 terminate / 容器超时终止）兜底冲刷。

    签到轮被超时击杀时进程内 _mail_summary 随进程死亡——整轮已收集的告警
    （含部分成功的汇总）一并消失。信号处理器在退出前冲刷一次；汇总为空时
    不产生任何发送（不重复告警），正常收尾路径已清空收集器。
    """
    # 冲刷失败不改变退出码：SIGTERM 路径的首要契约是尽快退出
    with contextlib.suppress(Exception):
        _flush_admin_mail_summary(phase="签到超时终止")
    sys.exit(128 + int(signum or 15))


# B 线用户失败提醒每日限频：承诺是「每天每个账号最多 1 封」，只靠单次运行终态路径
# 隐式保证不够——手动 --only 签到与探针进程可在同日追加发送，故用按天状态文件显式
# 去重（0 或负数 = 不限）。
USER_FAIL_MAIL_DAILY_CAP = config_check.parse_env_int("YIBAN_MAIL_USER_FAIL_DAILY_CAP", 1)


def _user_fail_mail_state_path(today_str):
    state_dir = env_io.resolve_path("YIBAN_STATE_DIR", "/var/log/yiban")
    return os.path.join(state_dir, f"mail-user-fail-{today_str}.json")


def _user_fail_mail_reserve(phone, today_str):
    """预占该账号今日失败提醒额度：允许则占位并返回 True，超额返回 False。

    读-改-写整体持状态文件锁；跨进程（签到主进程 / 手动 --only / 探针）一致。
    文件按天命名自然轮转，无需清理历史。

    **调用约定**：占位后若邮件实际未发出（未启用 / SMTP 失败），必须调
    `_user_fail_mail_release` 归还，否则一次 SMTP 抖动就会吞掉该账号当天
    唯一的提醒机会。
    """
    cap = USER_FAIL_MAIL_DAILY_CAP
    if cap <= 0:
        return True
    path = _user_fail_mail_state_path(today_str)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with cli_support._state_file_lock(path):
            data = {}
            if os.path.exists(path):
                try:
                    with open(path, encoding="utf-8-sig") as f:
                        data = json.load(f)
                except (OSError, ValueError, TypeError):
                    data = {}
            if not isinstance(data, dict):
                data = {}
            try:
                used = int(data.get(phone, 0))
            except (TypeError, ValueError):
                # 状态文件被手工改成非数字时不得冒泡中断整轮签到
                used = 0
            if used >= cap:
                return False
            data[phone] = used + 1
            tmp = path + ".tmp" + str(os.getpid())
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, path)
        return True
    except OSError:
        # 状态目录不可写：退回不限频（不因限频设施故障吞掉真实失败告警）
        return True


def _user_fail_mail_release(phone, today_str):
    """归还一个失败提醒额度（邮件未真正发出时调用）；下限 0，失败仅告警。

    状态目录不可写时与 reserve 同口径静默放行（限频设施故障不得放大成业务故障）。
    """
    path = _user_fail_mail_state_path(today_str)
    try:
        with cli_support._state_file_lock(path):
            if not os.path.exists(path):
                return
            try:
                with open(path, encoding="utf-8-sig") as f:
                    data = json.load(f)
            except (OSError, ValueError, TypeError):
                return
            if not isinstance(data, dict) or phone not in data:
                return
            try:
                used = int(data.get(phone, 0))
            except (TypeError, ValueError):
                return
            data[phone] = max(0, used - 1)
            tmp = path + ".tmp" + str(os.getpid())
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, path)
    except OSError as e:
        logger.warning("归还失败提醒额度失败（不影响签到）: %s", e)


def send_user_fail_mail(owner, phone, message, scenario="signin"):
    """B 线：向账号归属用户发送失败类提醒邮件。

    scenario="signin"（默认）：签到最终失败提醒（原行为，主题/正文不变）；
    scenario="probe"：健康探测发现账号异常——探测并无「当日签到」语义，
    沿用签到措辞会误导用户。

    仅当用户存在且开启 mail_notify（默认开）时发送；每账号每日上限
    USER_FAIL_MAIL_DAILY_CAP 封（默认 1，定时/手动/探针三个入口统一计算；
    发送成功才消耗额度，SMTP 故障不吞当日重试机会）；用户注销/关闭/未配置
    邮件时静默跳过；发送失败不影响签到（mailer 内部捕获）。
    内容脱敏：手机号打码、消息经 _sanitize_text 清洗（不含账号密码）。
    """
    if not owner:
        return
    try:
        user = db.find_user(owner)
    except Exception as e:
        # 库瞬时故障时不能当"查无此人"静默跳过——那恰是用户最需要触达的时刻
        # （区别于用户不存在：find_user 正常返回 None，不走此分支）。
        # 打码手机号定位账号，不打印原始邮箱。
        logger.warning("查询账号 %s 的归属用户失败，本次失败提醒未发送: %s", _mask_phone(phone), e)
        user = None
    if not user:
        return
    if str(user.get("mail_notify", 1)).strip().lower() not in ("1", "true", "on", "yes"):
        return
    _today = clock.now().strftime("%Y-%m-%d")
    if not _user_fail_mail_reserve(phone, _today):
        logger.info(
            "账号 %s 今日失败提醒已达上限（%d 封），跳过发送",
            _mask_phone(phone), USER_FAIL_MAIL_DAILY_CAP,
        )
        return
    if scenario == "probe":
        # 主题带【易班签到】前缀与 web 侧 6 处用户邮件同口径：邮箱里一眼可辨来源
        subject = "【易班签到】账号健康预警"
        body = layout.Mail(
            summary=f"您的易班账号 {_mask_phone(phone)} 在系统例行健康检查中未能正常登录。",
            fields=[("异常详情", _sanitize_text(message))],
            advice=["请尽快核对账号密码是否变更，或按提示处理验证问题，避免下次签到失败",
                    "本次预警不影响已完成的签到"],
            footer="可在「我的账号」页面关闭本邮件提醒。",
            level="warn",
        )
    else:
        subject = "【易班签到】签到失败提醒"
        body = layout.Mail(
            summary=f"您的易班账号 {_mask_phone(phone)} 今日签到失败。",
            fields=[("失败原因", _sanitize_text(message))],
            advice=["连续失败会被系统自动暂停账号",
                    "如账号本身正常，请登录网站检查或联系管理员"],
            footer="可在「我的账号」页面关闭本邮件提醒。",
            level="warn",
        )
    if not mailer.send_user(owner, subject, body):
        # 未真正发出（邮件未启用 / 无收件人 / SMTP 全部失败）：归还额度，让当天
        # 还有机会重试——额度语义是"每天最多成功提醒 N 次"
        _user_fail_mail_release(phone, _today)
        logger.info(
            "账号 %s 的失败提醒未发出（邮件未启用或发送失败），已归还今日额度",
            _mask_phone(phone),
        )
