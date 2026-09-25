# SPDX-License-Identifier: AGPL-3.0-only
"""签到状态码词汇表 —— **唯一事实源**。

状态码只在此定义，`scripts/signin.py`、`web/app.py`、`docker/scheduler.py` 一律
别名引用同一对象；各写一份必然漂移（曾出现 web 的图标表缺 `no_position`、signin
的符号表缺 `pending`）。

**映射表刻意保留两张，不合并**——它们服务不同消费方，当前确有差异：

| 表 | 消费方 | 现状 |
|----|--------|------|
| `SYMBOL` | `signin` 写日状态文件 → 日历渲染 | 含 `no_position`(🚫) / `global_paused`(⏸)，无 `pending` |
| `ICON` / `TEXT` | `/api/my-accounts` 的 `state_icon` / 文案 → 前端按码渲染 | 含 `pending`(⏳)，无 `no_position` / `global_paused` |

合并会**改变前端可见表现**，属需要前后端协同的改动，不宜顺手做。

「今日是否了结」的划分同样收在本模块（`UNDONE_STATUSES` / `CLAIM_DONE_STATUSES` /
`CONCLUDED_JSON_STATUSES`，以及领取池侧的 `TASKS_*`）：补签闸门、补签轮剔除与领取池
收尾都引用同一批对象，判定口径只有一处可改。

**这里没有脱敏**：本模块只是状态词汇，遮手机号/凭据发生在 `yiban.masking` 与
`yiban.logging_ext` 两层——状态串会进日志、日状态文件与 `/api/my-accounts`，把关不在这里。
"""
# ---- 状态码 ----
STATUS_SUCCESS = "success"               # 签到成功（服务器确认打卡完成）
STATUS_ALREADY = "already"               # 今日已签到（重复执行时服务器告知）
STATUS_NO_TASK = "no_task"               # 今日无需签到（服务器确认今日无任务）
STATUS_FAILED = "failed"                 # 最终失败（重试耗尽）
STATUS_RETRYING = "retrying"             # 重试中
STATUS_SKIPPED_WINDOW = "skipped_window"  # 未在签到时段（窗口外）
STATUS_SKIPPED_NORANGE = "skipped_norange"  # 签到窗口缺失（Range 为空）
# 易班侧无签到点位：登录成功、signPosition 返回 code=0 但 Position 为空
# （任务未配置/当日任务已关闭）。必须与 STATUS_FAILED 区分——否则会触发
# "签到失败"告警轰炸、并把补签闸门判为未了结而白跑一轮全量；独立状态后
# 展示可区分、不按失败告警、不触发补签重跑（重试拿不到就是拿不到）。
STATUS_NO_POSITION = "no_position"
STATUS_PAUSED = "paused"                # 账密异常暂停（连续凭据失败，熔断器）
STATUS_USER_CANCELLED = "user_cancelled"  # 用户自取消（用户暂停自己的签到任务）
STATUS_PENDING = "pending"               # 待签（未执行/无记录）
# 全局暂停（管理员 Web UI 一键暂停：整站停止自动签到）。
# 注意：签到进程不产此状态——暂停时 main() exit(2)，由 run.sh 依据
# YIBAN_GLOBAL_PAUSE=1 在「日状态文件」写入 GLOBAL_PAUSED（区别于普通 SKIPPED，
# 供运维/监控区分）；此处保留常量与符号，供显示层消费日状态时映射。
STATUS_GLOBAL_PAUSED = "global_paused"

# 状态码 → 日历/日志符号（signin 写日状态文件，web 日历按此渲染）
SYMBOL = {
    STATUS_SUCCESS: "✅", STATUS_ALREADY: "✅", STATUS_NO_TASK: "➖",
    STATUS_FAILED: "❌", STATUS_RETRYING: "🔄",
    STATUS_SKIPPED_WINDOW: "⛔", STATUS_SKIPPED_NORANGE: "⛔",
    STATUS_NO_POSITION: "🚫",
    STATUS_PAUSED: "⏸️", STATUS_USER_CANCELLED: "⏹️",
    STATUS_GLOBAL_PAUSED: "⏸",
}

# 状态码 → 图标（经 /api/my-accounts 的 state_icon 下发，前端按码渲染）
ICON = {
    STATUS_SUCCESS: "✅", STATUS_ALREADY: "✅", STATUS_NO_TASK: "➖",
    STATUS_FAILED: "❌", STATUS_RETRYING: "🔄",
    STATUS_SKIPPED_WINDOW: "⛔", STATUS_SKIPPED_NORANGE: "⛔",
    STATUS_PAUSED: "⏸️", STATUS_USER_CANCELLED: "⏹️", STATUS_PENDING: "⏳",
}
# 状态码 → 中文文案（同上，前端直接用）
TEXT = {
    STATUS_SUCCESS: "签到成功", STATUS_ALREADY: "已签到", STATUS_NO_TASK: "无需签到",
    STATUS_FAILED: "签到失败", STATUS_RETRYING: "重试中",
    STATUS_SKIPPED_WINDOW: "时段外", STATUS_SKIPPED_NORANGE: "窗口缺失",
    STATUS_PAUSED: "暂停", STATUS_USER_CANCELLED: "已取消", STATUS_PENDING: "待签",
}

# 「未了结」状态集合：状态文件里任一账号落此集合，即判定本轮尚未跑完 → 触发补签轮。
# **单一事实源**：`docker/scheduler.py` 以别名引用同一对象，宿主 run.sh 依据
# `signin --second-run-check` 的退出码判定（该脚本内部亦用本集合）。
UNDONE_STATUSES = frozenset((
    STATUS_FAILED, STATUS_RETRYING, STATUS_PENDING,
    STATUS_SKIPPED_WINDOW, STATUS_SKIPPED_NORANGE,
    STATUS_NO_POSITION,
))

#: 全部 JSON 状态码（两张映射表的键并集即此；新增状态码时必须同步本元组）。
#: 它是「有结论 / 未了结」这类划分的**分母**——用差集表达，就不必手写枚举，
#: 手写的枚举会在新增状态码时静默漏掉一格。
ALL_STATUSES = (STATUS_SUCCESS, STATUS_ALREADY, STATUS_NO_TASK, STATUS_FAILED,
                STATUS_RETRYING, STATUS_SKIPPED_WINDOW, STATUS_SKIPPED_NORANGE,
                STATUS_NO_POSITION, STATUS_PAUSED, STATUS_USER_CANCELLED,
                STATUS_PENDING, STATUS_GLOBAL_PAUSED)

#: 「已了结、今日不必再签」的 JSON 状态集：补签轮定向剔除与领取池记 `done` 共用
#: 同一对象（各写一份会漂移成漏签或重复登录，而重复登录踩上游风控红线）。
CLAIM_DONE_STATUSES = frozenset((STATUS_SUCCESS, STATUS_ALREADY, STATUS_NO_TASK))

#: 「已有结论」的 JSON 状态集：非空且非 pending（`state_io._has_conclusion` 的口径）。
#: 注意两点：
#: - 本集合**不含**空串/缺 status 键——那两种是「无记录」，既非结论也非未了结；
#: - 它**不是** `UNDONE_STATUSES` 的补集：failed / retrying / skipped_window /
#:   skipped_norange / no_position 既「未了结」又「已有结论」。「还要不要再签」与
#:   「有没有留下事实」是两个问题，故两个集合刻意重叠，勿合并成一个。
CONCLUDED_JSON_STATUSES = frozenset(ALL_STATUSES) - {STATUS_PENDING}


def is_concluded_status(value):
    """该状态串是否代表「已有结论」（非空且非 `pending`）；未知串按有结论处理。

    判据用**排除法**而不是集合成员：状态文件是跨进程事实源，其 `status` 可能是本进程
    不认识的串（新增状态码而读侧未同步、外部工具手写）。消费方是「仅当当日无结论才写」
    的 CAS 与窗口收尾预筛，失效方向必须偏「不覆盖已有结果」——把一个看不懂的结果判成
    「无记录」，窗口外跳过就会把它覆盖掉，真实失败随之从告警里消失。
    故 `CONCLUDED_JSON_STATUSES`（只枚举已知常量）供展示/枚举类消费者使用，**不是**本
    谓词的定义域，两者不等价。
    """
    return str(value).strip() not in ("", STATUS_PENDING)


#: 领取池（sign_tasks）侧的两个集合，state 词表见 yiban/store/queue_store.py。
#: 与 JSON 状态码是两套词，且**不是一一对应**：池里的 `done` 只收 `CLAIM_DONE_STATUSES`
#: 那三种（success / already / no_task）；`no_position` / `skipped_window` / `skipped_norange`
#: 平移进的是 `failed`（见 `migrations._JSON_TERMINAL_TO_TASK_STATE`，`round._settle_claims`
#: 与 `executor_v3` 都走同一口径）。把这三格误读成 `done`，等于把"当日没签成"当成
#: "当日已了结"，补签闸门会因此放过它——判「当日是否了结」一律用下面这两个集合。
TASKS_OPEN_STATES = frozenset(("pending", "claimed", "failed", "stolen"))
TASKS_SETTLED_STATES = frozenset(("done", "skipped"))
