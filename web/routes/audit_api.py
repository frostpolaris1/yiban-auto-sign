# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""审计只读路由：`GET /api/audit-logs`（管理员面）。

**功能**
按页读取审计链行，回答"谁在什么时候干了什么"。仅读，不写、不校验链。

**入库契约（五约束，改动即破坏 Vue 线对接，须同步前端）**
1. 分页显式：只认 `page`（默认 1，≥1）与 `page_size`（默认 50，1..200），
   两者都在响应里回显；不使用隐式 offset。
2. 过滤字段白名单：只认 `action`/`actor`/`target`/`from_ts`/`to_ts`，出现任何其它
   查询键即 400 JSON（`{"error": "不支持的过滤字段: X"}`）。
3. 对外 id 不透明：不返回自增 `audit_logs.id`，行定位符用
   `yiban.store.audit_chain.public_row_id(id)`（HMAC 前 16 hex，字段名 `row_id`）。
4. 脱敏单出口：行一律经 `_serialize_audit_row()` 出 HTTP，username 走 `actor_tag()`、
   target/detail 走 `_nl_safe()` 后截断，原串绝不进响应。
5. 错误一律 JSON：400/403/500 全 JSON（403 由登录守卫出，500 依赖 M38 的 /api/* 契约）。

**归属**
`web.app.create_app` 的"审计"面；权限复用登录守卫——非 `/api/my-*` 路径对普通用户
即 403（web/app.py 的 `require_login`），故本模块不另写鉴权。

**复用**
`register(app)` 供 `web.routes.register_all` 装配；只读实现住在
`yiban/store/audit_chain.py`（本模块不触 `verify_audit_chain`，读接口不触发全表哈希）。

**通信**
视图体经 `web.routes.appmod()` 取 web.app 模块级名字（`logger`、`_nl_safe`），避免与
`web/app.py` 形成导入环；被 `register_all(app)` 一次接入。
"""
from flask import jsonify, request

from web.routes import appmod as _appmod
from yiban.store import audit_chain as _audit_chain

_DEFAULT_PAGE_SIZE = 50
_MAX_PAGE_SIZE = 200
_ALLOWED_QUERY_KEYS = {"page", "page_size", "action", "actor", "target", "from_ts", "to_ts"}
_TARGET_MAX = 120
_DETAIL_MAX = 200
_ACTION_MAX = 64


def _serialize_audit_row(row):
    """审计行的**唯一**脱敏出口：原串（username/target/detail）绝不进响应。

    username 经 `actor_tag()` 遮罩（与写入侧同一口径）、target/detail 经 `_nl_safe()`
    净化换行后截断；`row_id` 是不透明定位符（见入库契约第 3 条）。
    """
    m = _appmod()
    return {
        "row_id": _audit_chain.public_row_id(row["id"]),
        "ts": str(row["ts"] or "")[:32],
        "actor": _audit_chain.actor_tag(row["username"] or ""),
        "action": m._nl_safe(str(row["action"] or ""))[:_ACTION_MAX],
        "target": m._nl_safe(str(row["target"] or ""))[:_TARGET_MAX],
        "detail": m._nl_safe(str(row["detail"] or ""))[:_DETAIL_MAX],
    }


def api_audit_logs():
    """按页读取审计行（管理员）。入库契约见模块 docstring。"""
    m = _appmod()
    for key in request.args:
        if key not in _ALLOWED_QUERY_KEYS:
            return jsonify({"error": f"不支持的过滤字段: {key}"}), 400

    raw_page = (request.args.get("page") or "").strip() or "1"
    raw_size = (request.args.get("page_size") or "").strip() or str(_DEFAULT_PAGE_SIZE)
    try:
        page = int(raw_page)
        page_size = int(raw_size)
    except ValueError:
        return jsonify({"error": "分页参数必须是整数"}), 400
    if page < 1:
        return jsonify({"error": "page 必须大于等于 1"}), 400
    if page_size < 1 or page_size > _MAX_PAGE_SIZE:
        return jsonify({"error": f"page_size 必须在 1..{_MAX_PAGE_SIZE} 之间"}), 400

    action = (request.args.get("action") or "").strip() or None
    actor = (request.args.get("actor") or "").strip() or None
    target = (request.args.get("target") or "").strip() or None
    from_ts = (request.args.get("from_ts") or "").strip() or None
    to_ts = (request.args.get("to_ts") or "").strip() or None
    # 库里 actor 列已遮罩（写入口收口），过滤值必须先过同一口径才对得上。
    if actor:
        actor = _audit_chain.actor_tag(actor)

    try:
        rows, total = _audit_chain.read_audit_rows(
            action=action, actor=actor, target=target,
            from_ts=from_ts, to_ts=to_ts,
            limit=page_size, offset=(page - 1) * page_size,
        )
    except Exception as e:
        m.logger.warning("审计只读查询失败: %s", e)
        return jsonify({"error": "审计日志读取失败"}), 500

    return jsonify({
        "rows": [_serialize_audit_row(r) for r in rows],
        "page": page,
        "page_size": page_size,
        "total": total,
        "has_more": page * page_size < total,
    })


def register(app):
    """在本域注册一条只读路由；endpoint 取函数名。"""
    app.add_url_rule("/api/audit-logs", view_func=api_audit_logs)
