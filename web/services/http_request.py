# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""HTTP 请求侧的两个纯助手：JSON 请求体解析与同源判定。

**功能**
`_json_body`：安全解析 JSON 请求体（空 body → `{}`；非法 JSON 或非对象 → 400）。
`_request_is_same_origin`：`Origin` 与 `(scheme, host)` 是否一致。

**归属**
批 3-b 拆墙：两者原在 `web/app.py`，被各路由经 `web.routes.appmod()` 按属性取用。
它们**不读 `web/app.py` 的模块级状态**（只读 Flask 的请求上下文代理），故属纯助手，
搬出组合根后路由按域直取。

**复用**
`_json_body` 是各写接口取请求体的唯一入口（口径一致：非对象 JSON 一律 400，不静默按
空请求处理）。`_request_is_same_origin` 是**同源语义的单一定义点**：`create_app` 的
`check_csrf` 闭包与 SSE 端点（`web/routes/stream_api.py`）取同一份判定，避免两处各写一套。

**通信**
本模块只依赖 Flask 的 `request` / `abort`，**不导入** `web.app`（避免导入环）。
无进程内状态、无 I/O 副作用，可在任意请求上下文内调用。
"""

from urllib.parse import urlparse

from flask import abort, request


def _json_body():
    """安全解析 JSON 请求体：
    - 空 body → {}
    - 非法 JSON / 非对象 JSON → 400（API 语义清晰，不静默按空请求处理）
    """
    if not request.data:
        return {}
    data = request.get_json(silent=True)
    if data is None:
        abort(400, description="请求体不是合法 JSON")
    if not isinstance(data, dict):
        abort(400, description="请求体应为 JSON 对象")
    return data


def _request_is_same_origin():
    """请求是否同源（`Origin` 与 `(scheme, host)` 一致）。

    **同源语义的单一定义点**：`create_app` 的 `check_csrf` 闭包与 SSE 端点
    （`web/routes/stream_api.py`）都从这里取同一份判定，避免两处各写一套。
    `EventSource` 不能自定义请求头，只能靠 Cookie 会话，故它的 GET 也必须过同源校验。
    无 `Origin` 头（同站导航、curl、同源 GET）放行——与既有登录/注册口径一致。
    """
    origin = request.headers.get("Origin")
    if not origin:
        return True
    try:
        o = urlparse(origin)
    except ValueError:
        return False
    return (o.scheme, o.netloc) == (request.scheme, request.host)
