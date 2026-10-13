# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""崩溃语义：写前标记的识别（V-S3）与 `unknown_outcome` 的定论判据（V-S2）。

**要解决的问题**
进程可能在"已发出真实请求、未拿到结果"时死掉。死掉那一轮不写终态，次日无法从台账
看出"当时是否已在做"。本模块提供两件东西：

1. **识别**（`report_unknown_outcomes`）：协调器启动时读 `run_events`，把"有 `start`
   行、其后无任何终态节点"的账号列为 `unknown_outcome`，出声告警。**只告警，不自动
   重签**——重签与否是后续批次的裁决，本批不做。
2. **定论判据**（`resolve_state`）：给定平台 `signPosition` 的 `State`，逐值给出处置。
   崩溃遗留账号在**下一次被真实尝试**时，`signin()` 本就会先读 `State` 再决定
   （读能力早已存在，见 `yiban/client.py` 的 `SIGN_POSITION_STATE_STATUS` 与
   `_judge_by_state`）；本函数把同一份判据收敛成**单一、可判定**的契约，供后续自动定论
   批次消费，并由本模块的校验者钉住。**本批不执行定论**（识别只告警）。

**写前标记的落点**
标记就是 `run_events` 的 `start` 节点（`MARKER_NODE`）。它由
`yiban/engine/executor_v3.py` 的 `_attempt` 在**发出真实请求之前**落库（闸门全过、
请求即将发出的那一刻）。**不新增节点值**：节点值域是下游（SSE 端点、日志页）的契约面，
多一个取值就多一份可见面；`start` 已经精确表达"本轮对这个账号开始了真实尝试"。

**标记写失败的行为：继续（`MARKER_WRITE_FAILURE_POLICY = "continue"`）**
`run_events.report` 写失败只告警、不抛，写前标记随即缺失。本批裁决：**不因此中止尝试**
——沿用既有契约"观测面坏了不得改变签到结论"（见 `run_events` 模块说明与
`tests/test_progress_events_e2e.py::ReportingFailureIsolationTest`）。残余风险是
**有界且合法的**：标记缺失只影响"崩溃可被识别"，不制造并发；崩溃后进程已死，重试是
**串行**的，而串行重复登录本就允许（l1rx 2026-10-12 裁决："不并发；串行重复合法且有界"）。
另一侧（fail-closed）的代价更大：观测表一次瞬时写失败就会中止一条真实签到、造成漏签，
与红线方向相反。

**定论路径的真实成本（不许写成"零成本"）**
2026-10-13 实测否掉了"读不是登录"：只读端点（含 `signPosition`）只在**带会话**时返回
200，而会话由登录链铸出。故定论路径的代价是**允许为该账号再铸一次会话**（系统本就给
"未了结"账号补一轮，不破例）。真实成本上限是**平台侧会话寿命**——本地 6 小时是自定
上限，**平台真实寿命至今未测**（有独立单在跑）。若寿命明显长于 80 分钟签到窗口，"读一次
定论"多数情况可复用会话缓存、零额外登录；但在量到之前不得据此宣称零成本。

**归属**
`yiban/engine/crash_semantics.py`（本模块，引擎侧）＋ `yiban/store/run_events.py` 的
读取面 `unresolved_starts`（数据层）。调用点：`yiban/engine/runner.py`（单执行体/手动）
与 `yiban/engine/workers.py`（多执行体监督进程）的启动路径。定论判据的**平台枚举事实源**
是 `yiban/client.py`，本模块只 import 复用、不另抄一张表。
"""
import logging

from yiban import client as yiban_client
from yiban import status as yiban_status
from yiban.engine import alerts
from yiban.masking import mask_phone
from yiban.store import run_events

logger = logging.getLogger("yiban.engine.crash_semantics")

#: 写前标记的节点：`run_events` 的"开始"行。由 `executor_v3._attempt` 在真实请求之前落库。
MARKER_NODE = run_events.NODE_START

#: 标记写入失败时的行为：`"continue"`（不中止尝试）。取值只此一个——本批裁决见模块说明；
#: 校验者 `tests/test_crash_semantics.py::MarkerWriteFailurePolicyTest` 钉住它。
MARKER_WRITE_FAILURE_POLICY = "continue"

# ---- V-S2 定论判据：平台 State → 处置 ----
#: State ∈ {3,4}：今日已签到（含班委手动补签）。只回填结果，**不再提交**。
RESOLVE_ALREADY = "already"
#: State == 2：今日无需签到。收敛为 `no_task`。
RESOLVE_NO_TASK = "no_task"
#: State ∈ {0,1}：尚未签到。按可签**重新纳入**（下一次真实尝试会照常取点位并提交）。
RESOLVE_SIGNABLE = "signable"
#: State == 5（补签中）：结果仍未定。**不提交、不记失败**。
RESOLVE_UNDETERMINED = "undetermined"
#: 读不到（会话失效／形状不标准）：维持 `unknown_outcome` ＋ 告警，**不擅自重签**。
RESOLVE_UNKNOWN = "unknown_outcome"

#: 允许发起一次真实签到提交的处置只有"可签"这一档：其余四档都不得提交。
_SUBMITTING_DISPOSITIONS = frozenset((RESOLVE_SIGNABLE,))


def resolve_state(state):
    """平台 `signPosition` 的 `State` → `(处置, 状态码)`。V-S2 的定论判据（纯函数）。

    `state` 是**已收敛**的 int 或 `None`（上游原始值先经 `yiban/client.py::_as_state_int`
    收敛，协议库 SPEC §3.1 容忍数字字符串）。映射取
    `yiban.client.SIGN_POSITION_STATE_STATUS`——平台枚举的**唯一事实源**；本函数不另抄
    一张表，改枚举即改本函数的结论。

    逐值口径（与 l3a4 2026-10-13 一致的"已知能力 ＋ 一条判据"）：
    - `3` / `4` ⇒ `RESOLVE_ALREADY` ＋ `already`：只回填结果、不提交；
    - `2` ⇒ `RESOLVE_NO_TASK` ＋ `no_task`：收敛为无需签到；
    - `0` / `1` ⇒ `RESOLVE_SIGNABLE` ＋ `None`：尚未签到，按可签重新纳入；
    - `5` ⇒ `RESOLVE_UNDETERMINED` ＋ `supplementing`：结果未定，不提交、不记失败；
    - 其余（`None`、未知枚举、非整数）⇒ `RESOLVE_UNKNOWN` ＋ `None`：维持
      `unknown_outcome` 并告警，不擅自重签。
    """
    if isinstance(state, bool) or state not in yiban_client.SIGN_POSITION_STATE_STATUS:
        return RESOLVE_UNKNOWN, None
    status = yiban_client.SIGN_POSITION_STATE_STATUS[state]
    if status is None:                                  # 0 / 1：尚无结论 → 可签
        return RESOLVE_SIGNABLE, None
    if status == yiban_status.STATUS_ALREADY:
        return RESOLVE_ALREADY, yiban_status.STATUS_ALREADY
    if status == yiban_status.STATUS_NO_TASK:
        return RESOLVE_NO_TASK, yiban_status.STATUS_NO_TASK
    if status == yiban_status.STATUS_SUPPLEMENTING:
        return RESOLVE_UNDETERMINED, yiban_status.STATUS_SUPPLEMENTING
    # 平台枚举新增了本函数还没接的取值：保守地当"读不到"，不擅自定论。
    return RESOLVE_UNKNOWN, None


def disposition_submits(disposition):
    """该处置是否允许发起一次真实签到提交。→ 仅 `RESOLVE_SIGNABLE` 为真。

    崩溃遗留账号的定论判据：已签到（只回填）、无需签到、补签中、读不到**四档都不提交**。
    """
    return disposition in _SUBMITTING_DISPOSITIONS


def unknown_outcomes(day):
    """→ 当日"有标记无结果"的行（`run_events.unresolved_starts` 的语义化别名）。只读。"""
    return run_events.unresolved_starts(day)


def report_unknown_outcomes(day, *, notify_url=None):
    """协调器启动识别：列出 `unknown_outcome`，出声告警，**不自动重签**。→ 识别到的行。

    只读 `run_events`：不改 `sign_tasks`、不发任何真实请求。没有未定论行时静默返回空表
    （不出声，避免每个正常轮都报一次）。识别出的行原样返回（带原手机号），供调用方与
    测试观察；**日志与告警里的手机号一律遮罩**（本仓日志红线：不得出现裸号）。

    只告警不重签是**本批的边界**：重签与否由后续批次裁决（见模块说明）。这里给出的告警
    字段必须能让管理员定位到"哪些账号、哪一天、该怎么处置"。
    """
    rows = unknown_outcomes(day)
    if not rows:
        return []
    masked = sorted({mask_phone(r["phone"]) for r in rows})
    logger.warning(
        "崩溃语义：当日 %d 个账号有 start 标记、无终态结果（unknown_outcome），"
        "可能已在发出请求后崩溃；只告警、不自动重签。执行体=%s 账号=%s",
        len(rows), sorted({r["executor"] for r in rows}), masked,
    )
    try:
        alerts.notify_admin_entry("易班签到：崩溃遗留的未定论尝试", [
            ("现象", f"当日 {len(rows)} 个账号有开始标记、无结果（unknown_outcome）"),
            ("业务日", str(day)),
            ("账号", "、".join(masked[:10]) + ("、…" if len(masked) > 10 else "")),
            ("处置", "读平台 signPosition 的 State 定论（已签到则回填、未签则重纳入）；"
                     "读不到则维持未定论。本批只告警，不自动重签。"),
        ], notify_url, push=True, level=alerts.ALERT_LEVEL_CRITICAL)
    except Exception as e:      # 告警通道故障不得反噬启动：识别已完成，日志已出声。
        logger.warning("崩溃遗留告警投递失败（%s），识别结果已记于日志", type(e).__name__)
    return rows
