# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""运行进度只读路由：`GET /api/admin/run-events`（管理员面）。

**功能**
本端点回答"谁签的、走到哪一步"。数据来自 `run_events`（内核进度事件层）。
响应分两块：轮级摘要（`rounds`，每轮一行）与单轮时间线（`events`）。
两端点之外，本模块不提供任何写操作。

**入库契约（改动即破坏 Vue 线对接，须同步前端）**
1. 查询键白名单：只认 `day` / `executor` / `limit`。出现其它键即 400 JSON
   （`{"error": "不支持的过滤字段: X"}`）。
2. `day` 可选，格式 `YYYY-MM-DD`。缺省取表内最新业务日；表空时取今天。
   非法格式即 400。
3. `executor` 可选，取值是**公开执行体标签**（`single` / `fallback` / `worker-N` /
   `unknown`，允许带方括号）。身份原串（带主机名）永不出现。非法取值即 400。
   缺省取该日最新一轮，故首屏一次请求就有时间线。
4. `limit` 可选，1..500，默认 200，回显为 `events_limit`。超过上限的行被截断，
   `events_truncated` 置 True。
5. 响应分页无 offset：本表按天聚集，一天的行数有限。`rounds` 上限在读取层。
   当日轮数超过上限时回 `rounds_truncated=true`；页面必须写明"已截断"，
   不许静默出空表。
6. 只读：不写表，不写审计链，**不调 `verify_audit_chain`**（照 audit_api 的先例）。
7. 脱敏单出口：账号遮罩、执行体收敛与 message 复遮都在读取层完成
   （`run_events` 的读取面）。写入面已净化 `message`，**读取面再复遮一次**，
   构成纵深防御。本模块不二次加工，也不放行原串。
8. 保留期：响应回显 `retention_days` 与窗口起止（`window.start_day` /
   `window.end_day`）。窗口外的日期回 `in_window=false`，`has_data=false`。
   页面据此给"跨月回溯请走审计日志页"的指引，不静默出空表。
9. 错误一律 JSON：400/401/403/500 全 JSON。

**归属**
`web.app.create_app` 的"数据"面。权限复用登录守卫：本路径不在任何放行清单内，
故匿名请求得 401、普通用户得 403（见 `web/app.py` 的 `require_login`）。

**复用**
`register(app)` 供 `web.routes.register_all` 装配。只读实现住在
`yiban/store/run_events.py` 的读取面。访问留痕复用 `read_audit_trace()`。

**通信**
视图体经 `web.routes.appmod()` 取 web.app 的模块级名字（`logger`、
`_is_valid_date_str`、`clock`），避免与 `web/app.py` 形成导入环。
"""

import datetime
import re

from flask import jsonify, request

from web.routes import appmod as _appmod
from web.routes import read_audit_trace
from yiban.store import run_events as _run_events

_DEFAULT_LIMIT = 200
_MAX_LIMIT = 500
_ALLOWED_QUERY_KEYS = {"day", "executor", "limit"}
# 公开执行体标签的形状：角色 + 可选槽位序号。它是唯一允许回前端的执行体形态。
_EXECUTOR_RE = re.compile(r"^\[?(?:single|fallback|worker-\d+|unknown)\]?$")


def api_run_events():
    """按轮读取运行进度（管理员）。入库契约见模块 docstring。"""
    m = _appmod()
    for key in request.args:
        if key not in _ALLOWED_QUERY_KEYS:
            return jsonify({"error": f"不支持的过滤字段: {key}"}), 400

    raw_day = (request.args.get("day") or "").strip()
    if raw_day and not m._is_valid_date_str(raw_day):
        return jsonify({"error": "日期格式不正确，应为 YYYY-MM-DD"}), 400

    raw_limit = (request.args.get("limit") or "").strip()
    limit = _DEFAULT_LIMIT
    if raw_limit:
        try:
            limit = int(raw_limit)
        except ValueError:
            return jsonify({"error": "limit 必须是整数"}), 400
        if limit < 1 or limit > _MAX_LIMIT:
            return jsonify({"error": f"limit 必须在 1..{_MAX_LIMIT} 之间"}), 400

    raw_exec = (request.args.get("executor") or "").strip()
    executor = None
    if raw_exec:
        if not _EXECUTOR_RE.match(raw_exec):
            return jsonify({"error": "executor 参数不合法"}), 400
        executor = raw_exec.strip("[]")

    try:
        now = m.clock.now()
        min_day, max_day = _run_events.day_bounds()
        day = raw_day or max_day or now.strftime("%Y-%m-%d")
        start_day = (now - datetime.timedelta(
            days=_run_events.RETENTION_DAYS - 1)).strftime("%Y-%m-%d")
        end_day = now.strftime("%Y-%m-%d")
        rounds, rounds_truncated = _run_events.summarize(day=day)
        if executor is None and rounds:
            executor = rounds[0]["executor"]
        events, truncated = _run_events.timeline(day, executor, limit=limit)
    except Exception as e:
        m.logger.warning("巡检读取失败: %s", e)
        return jsonify({"error": "运行进度读取失败"}), 500

    # 只读面留痕：与 users/logs 同口径。target 只放日期与条数，不含行内容。
    read_audit_trace()("run_events_read", f"{day} {len(events)} 事件")
    return jsonify({
        "ok": True,
        "retention_days": _run_events.RETENTION_DAYS,
        "window": {
            "start_day": start_day,
            "end_day": end_day,
            "day": day,
            "in_window": start_day <= day <= end_day,
            "min_day": min_day,
            "max_day": max_day,
            "has_data": bool(rounds),
        },
        "rounds": rounds,
        "rounds_truncated": rounds_truncated,
        "rounds_limit": _run_events.MAX_ROUNDS,
        "events": events,
        "events_truncated": truncated,
        "events_limit": limit,
    })


def register(app):
    """在本域注册一条只读路由；endpoint 取函数名。"""
    app.add_url_rule("/api/admin/run-events", view_func=api_run_events)
