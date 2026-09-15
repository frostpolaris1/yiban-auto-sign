# SPDX-License-Identifier: AGPL-3.0-only
"""签到状态码词汇表 —— **唯一事实源**。

此前 `scripts/signin.py` 与 `web/app.py` 各定义了一份状态码常量与映射表，
两份会各自漂移（实测：web 的图标/文案表缺 `no_position` 与 `global_paused`，
signin 的符号表缺 `pending`）。本模块收口为一份：状态码只在此定义，两侧改为别名引用。

**映射表刻意保留两张，不合并**——它们服务不同消费方，且当前确有差异：

| 表 | 消费方 | 现状 |
|----|--------|------|
| `SYMBOL` | `signin` 写日状态文件 → 日历渲染 | 含 `no_position`(🚫) / `global_paused`(⏸)，无 `pending` |
| `ICON` / `TEXT` | `/api/my-accounts` 的 `state_icon` / 文案 → 前端按码渲染 | 含 `pending`(⏳)，无 `no_position` / `global_paused` |

合并会**改变前端可见表现**（某状态的图标/文案会变），属需要前后端协同的改动，
不宜在重构中顺手做——已登记在待办（B-T9 前端打磨）里，届时一并收敛。
"""
# ---- 状态码 ----
STATUS_SUCCESS = "success"               # 签到成功（服务器确认打卡完成）
STATUS_ALREADY = "already"               # 今日已签到（重复执行时服务器告知）
STATUS_NO_TASK = "no_task"               # 今日无需签到（服务器确认今日无任务）
STATUS_FAILED = "failed"                 # 最终失败（重试耗尽）
STATUS_RETRYING = "retrying"             # 重试中
STATUS_SKIPPED_WINDOW = "skipped_window"  # 未在签到时段（窗口外）
STATUS_SKIPPED_NORANGE = "skipped_norange"  # 签到窗口缺失（Range 为空）
# 易班侧无签到点位（2026-09-01 独立状态）：登录成功、signPosition 返回 code=0 但
# Position 为空（任务未配置/当日任务已关闭）。此前并入 STATUS_FAILED——与凭据/网络
# 真失败混淆：触发"签到失败"告警轰炸、把补签闸门判为未了结白跑一轮全量。
# 独立状态后：展示可区分、不按失败告警、不触发补签重跑（重试拿不到就是拿不到）。
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
