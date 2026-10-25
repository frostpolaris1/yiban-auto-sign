# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""工作队列只读聚合（lcpx 试点，2026-10-25）：按「当日终态」过滤的账号待办视图。

**功能**
`GET /api/queue`：三组待办中**今日失败 / 今日未签**两组的服务端过滤分页视图；
响应随车下发今日摘要（业务日 / 星期 / 签到窗口 / 各组计数），供队列页顶端一行。
`GET /api/queue/<int:idx>/events`：单账号近 N 天事件时间线（脱敏、封顶），
供对象抽屉的「近 30 天事件」。

**为什么新增端点（缺口盘点，2026-10-25）**
- 待审核组无缺口：`GET /api/accounts?group=pending` 已是服务端过滤分页视图，前端直取；
- 「今日失败 / 今日未签」需要**按当日终态过滤 + 服务端分页**：`/api/accounts` 的
  `states` 只随当前页下发且无状态过滤参数；`/api/admin/sign-events` 是事件口径
  （封顶 200 条，且"每账号取最后一条事件当终态"正是工单 v5ty 的错误口径）；
- 抽屉时间线按**账号下标**取：明文手机号进 GET 查询串会落访问日志，违反
  「手机号零落盘」红线，故服务端按下标自行解析号码。
两条均只读，不写任何状态；既有端点形状零变化。

**终态唯一事实源**：按日状态文件（`web.services.logs.load_sign_state`），与看板
`accounts_terminal_summary`、账号表 `states` 同源。「当日是否有结论」用
`yiban.core.status.is_concluded_status`（排除法口径：未知串按有结论处理）。

**分组口径**（与账号管理页展示语义一致，不另立条件）：
- 今日失败 = 已生效、未软删、当日**有效终态**为 `failed`；
- 今日未签 = 已生效、未软删、未被用户暂停、当日**无结论**（无记录或 `pending`）；
- 用户自暂停且当日无结论的账号，按账号页同一合成视为「用户已取消」（有结论），
  不进任何一组；
- 排序：未签按当日计划时刻升序（执行序即队列序，"待生成"垫底）；失败按终态时刻
  降序（最新失败在前）；同键按 index 稳定。

**性能**：分组、计数与检索只读**未解密快照**（raw 行的 phone / status / deleted /
user_paused 全是明文列）＋当日状态文件；当前页行只做展示序列化
（`mask_account` 同样只读明文列）——全程零 AES-GCM，5k 账号下与
`count_only=1` 同阶。

**鉴权**：路径不在普通用户白名单 → `require_login` 默认分支（仅管理员）。
**复用**：`register(app)` 供 `web.routes.register_all` 装配；检索命中取
`web.routes.accounts_api._account_search_hit` 的同一实现（不复制第二份口径）；
终态文案取 `accounts_data.plan_state_message`（跨端点唯一取点）。
"""
import sqlite3

from flask import jsonify, request

from web.routes import appmod as _appmod
from web.routes import json_conditional as _json_conditional
from web.routes import page_args as _page_args
from web.routes import page_meta as _page_meta
from web.routes import page_window as _page_window
from web.routes import read_audit_trace as _read_audit_trace
from web.routes.accounts_api import _account_search_hit as _account_search_hit_of
from web.services import accounts_data as _accounts_data
from web.services import logs as _logs_svc
from web.services import signstatus as _signstatus
from web.services.accounts_data import _plan_clock
from web.services.accounts_data import plan_state_message as _plan_state_message
from web.services.accounts_data import today_plan_map as _today_plan_map
from yiban.adapters.persistence import db
from yiban.core import clock
from yiban.core import masking as _masking
from yiban.core import status as yiban_status

#: 桶名白名单（本端点只承两组；待审核走既有 /api/accounts?group=pending）。
_BUCKETS = ("failed", "unsigned")
#: 单账号事件时间线的响应封顶。
_EVENTS_CAP = 50
#: 事件消息截断长度（与 /api/admin/sign-events 的 `_mask` 同值）。
_EVENT_MSG_MAX = 160
#: 「今天」的星期展示（Monday=0 → 周一）。
_WEEKDAYS = ("一", "二", "三", "四", "五", "六", "日")


def _effective_entry(acc, states):
    """账号当日的**有效**状态条目：用户自暂停且无结论时合成「用户已取消」。

    与 `web/routes/accounts_api.py` 列表展示的合成同口径：自暂停账号的调度由
    用户意图接管，未了结的旧记录不再当「待签」展示。返回 (status_str, entry|None)。
    """
    entry = states.get(str(acc.get("phone", "")))
    status = entry.get("status", "") if isinstance(entry, dict) else ""
    if acc.get("user_paused") and not yiban_status.is_concluded_status(status):
        return yiban_status.STATUS_USER_CANCELLED, entry
    return (status or ""), entry


def _scan():
    """一次遍历：摘要计数 + failed / unsigned 两桶的 (index, acc, entry) 列表。

    全程只读未解密快照与当日状态文件（见模块 docstring「性能」）。
    """
    accounts = _accounts_data.load_accounts_raw()
    states = _logs_svc.load_sign_state()
    counts = {"pending": 0, "rejected": 0, "active": 0, "failed": 0, "unsigned": 0}
    buckets = {b: [] for b in _BUCKETS}
    for i, acc in enumerate(accounts):
        if acc.get("deleted"):
            continue
        st = acc.get("status")
        # 只有已生效账号参与当日终态分桶；待审/已拒绝/删除与桶无关
        if st == _accounts_data.ACCOUNT_STATUS_PENDING:
            counts["pending"] += 1
            continue
        if st == _accounts_data.ACCOUNT_STATUS_REJECTED:
            counts["rejected"] += 1
            continue
        if st != _accounts_data.ACCOUNT_STATUS_ACTIVE:
            continue
        counts["active"] += 1
        status, entry = _effective_entry(acc, states)
        if status == yiban_status.STATUS_FAILED:
            counts["failed"] += 1
            buckets["failed"].append((i, acc, entry))
        elif not acc.get("user_paused") and not yiban_status.is_concluded_status(status):
            counts["unsigned"] += 1
            buckets["unsigned"].append((i, acc, entry))
    return counts, buckets, states, clock.now()


def api_queue():
    """工作队列分页视图：`bucket`（failed|unsigned）+ 可选 `limit`/`offset`/`q`。

    响应：`counts`（五组计数，不随 q/页收窄）、`today`/`weekday`/`window`（摘要行）、
    `rows`（展示行，字段 = `mask_account` + 当日终态/计划/自选时段）、`page_meta` 四键
    （仅在带分页参数时出现，与账号列表同形）。
    """
    bucket = str(request.args.get("bucket", "")).strip()
    if bucket not in _BUCKETS:
        return jsonify({"error": "未知队列分组"}), 400
    q = str(request.args.get("q", "")).strip()[:64]
    limit, offset, paged = _page_args()
    counts, buckets, states, today = _scan()
    indexed = buckets[bucket]
    plan_at = _today_plan_map() or {}
    if bucket == "unsigned":
        # 计划时刻升序（执行序即队列序）；"待生成"（无分钟键）垫底；同键按 index。
        indexed.sort(key=lambda trip: (
            0 if _plan_minutes(_plan_at_of(plan_at, trip[1])) is not None else 1,
            _plan_minutes(_plan_at_of(plan_at, trip[1])) or 0,
            trip[0]))
    else:
        # 终态时刻降序（最新失败在前）；无时刻（-1）自然垫底；同刻 index 大者在前。
        indexed.sort(key=lambda trip: (_state_seconds(trip[2]), trip[0]), reverse=True)
    if q:
        indexed = [(i, a, e) for i, a, e in indexed if _account_search_hit_of(a, q)]
    total = len(indexed)
    page = _page_window(indexed, limit, offset) if paged else indexed
    prefs = db.get_time_prefs()
    rows = [_queue_row(i, acc, entry, states, plan_at, prefs) for i, acc, entry in page]
    win = _signstatus._sign_window()
    payload = {
        "ok": True,
        "bucket": bucket,
        "today": today.strftime("%Y-%m-%d"),
        "weekday": "周" + _WEEKDAYS[today.weekday()],
        # 与 GET /api/settings 的 `sign_window` 同一表达式（同源 _signstatus._sign_window），
        # 不另立第二种窗口格式。
        "window": "%02d:%02d ~ %02d:%02d" % (win[0][0], win[0][1], win[1][0], win[1][1]),
        "counts": counts,
        "rows": rows,
    }
    if paged:
        payload.update(_page_meta(total, limit, offset, len(rows)))
    return _json_conditional(payload)


def _plan_at_of(plan_at, acc):
    """当日计划 run_at 原值（账号 → 台账映射）；缺行回 ""。"""
    return plan_at.get(str(acc.get("phone", ""))) or ""


def _plan_minutes(run_at):
    """台账 run_at → 当日分钟数（排序键）；空值 / 短串回 None（"待生成"垫底）。"""
    hm = _plan_clock(run_at)
    if not hm:
        return None
    hh, mm = hm.split(":")
    return int(hh) * 60 + int(mm)


def _state_seconds(entry):
    """终态时刻 "HH:MM(:SS)" → 当日秒数（排序键）；缺失回 -1（垫底）。"""
    text = str((entry or {}).get("time") or "").strip()
    parts = text.split(":")
    if len(parts) < 2 or not all(p.isdigit() for p in parts[:2]):
        return -1
    sec = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
    return int(parts[0]) * 3600 + int(parts[1]) * 60 + sec


def _queue_row(i, acc, entry, states, plan_at, prefs):
    """展示行：`mask_account`（服务端脱敏单出口）+ 当日终态/计划/自选时段。"""
    status, entry = _effective_entry(acc, states)
    phone = str(acc.get("phone", ""))
    slot = prefs.get(phone)
    return {
        **_accounts_data.mask_account(acc, i),
        "state": status or yiban_status.STATUS_PENDING,
        "state_msg": _plan_state_message(acc, entry, plan_at),
        "state_time": str(entry.get("time") or "") if isinstance(entry, dict) else "",
        "plan_at": _plan_clock(_plan_at_of(plan_at, acc)),
        "time_pref": _accounts_data._slot_to_label(
            slot.get("slot_min") if isinstance(slot, dict) else None),
    }


def api_queue_events(idx):
    """单账号事件时间线（脱敏、封顶 `_EVENTS_CAP`），供对象抽屉「近 30 天事件」。

    按**账号下标**解析号码（明文号不进 URL、不进日志）；响应里的手机号一律
    `mask_phone` 后下发。只读面聚合留痕一行（与 /api/admin/sign-events 同纪律）。
    """
    m = _appmod()
    try:
        days = min(max(int(request.args.get("days", 30)), 1), 90)
    except (TypeError, ValueError):
        days = 30
    try:
        accounts = _accounts_data.load_accounts_raw()
    except (RuntimeError, sqlite3.Error) as exc:  # 密钥缺失 / 库不可读：显性失败
        m.logger.warning("queue events 读库失败：%s", exc)
        return jsonify({"error": "账号数据暂不可读，请稍后再试"}), 503
    if not 0 <= idx < len(accounts):
        return jsonify({"error": "账号不存在"}), 404
    phone = str(accounts[idx].get("phone", ""))
    masked = _masking.mask_phone(phone)
    raw = db.sign_events_by_phone(phone, days=days)  # 时间升序
    window = recent_window(raw)
    events = [_mask_event(ev) for ev in window]
    _read_audit_trace()("queue_events_read", f"{masked} 近 {days} 天 {len(raw)} 条")
    return jsonify({
        "ok": True,
        "count": len(raw),
        "returned": len(events),
        "truncated": len(raw) > len(events),
        "events": events,
    })


def recent_window(events, cap=_EVENTS_CAP):
    """事件序列 → 保留**最近** `cap` 条，仍按时间升序返回。

    「近 30 天事件」回答的是"最近发生了什么"：截断必须砍**最旧**的一端。
    直接 `events[:cap]` 会保留最早 50 条，页面上写「已截断」却看不到今天的记录，
    等于给出"最近无事发生"的假信号。入参约定时间升序（`sign_events_by_phone`
    的 `ORDER BY ts`）；`cap <= 0` 返回空表（不返回全量）。
    """
    if cap <= 0:
        return []
    return list(events[-cap:])


def _mask_event(ev):
    """事件行脱敏序列化（字段集合与 /api/admin/sign-events 的 `_mask` 同形）。"""
    msg = str(ev.get("message") or "")
    return {
        "ts": str(ev.get("ts", "")),
        "phone": _masking.mask_phone(str(ev.get("phone") or "")),
        "status": str(ev.get("status") or ""),
        "message": (msg[:_EVENT_MSG_MAX] + "…") if len(msg) > _EVENT_MSG_MAX else msg,
        "stage": str(ev.get("stage") or ""),
        "attempt": int(ev.get("attempt") or 0),
        "dur_sec": ev.get("dur_sec"),
    }


def register(app):
    """本域两条只读路由；endpoint 取函数名（url_for 依赖）。"""
    app.add_url_rule("/api/queue", view_func=api_queue)
    app.add_url_rule("/api/queue/<int:idx>/events", view_func=api_queue_events)
