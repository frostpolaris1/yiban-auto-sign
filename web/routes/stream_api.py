# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""实时进度流（SSE）：`GET /api/stream/progress` 与 `GET /api/stream/my-signin`。

**功能**
把 `run_events`（内核进度事件层）的增量读通过 Server-Sent Events 推给浏览器。
事件类型全集冻结为 5 类：`snapshot` / `progress` / `heartbeat` / `resync` / `end`
（类型与 payload 的唯一定义点在 `yiban/store/run_events.py`；本模块只做帧封装与调度）。

**两条端点的受众与数据面**
- `/api/stream/progress`：**仅主管理员**（强档判定，与 `/api/scheduler/executors/progress`
  同一口径；注册管理员同得 403）。执行体级运行细节不给注册管理员，故授权面用强档，
  不用"弱档角色 == admin"那一条。推送聚合计数（快照）+ 全部执行体的进度事件；
  payload **不含手机号**。
- `/api/stream/my-signin`：**任何已登录会话**。只推**本人账号**的事件；服务端按会话用户
  的本人 phone 集合过滤；过滤用的 phone **不进 payload**。payload 只保留
  id/ts/day/node/node_label/executor/executor_label/message（后三者均为公开标签，
  不含手机号与主机名）。

**建连与续传（125 §3.3）**
1. 无 `Last-Event-ID`（首连）：发 `snapshot`（定向聚合，带 cursor），转常态。
2. 有 `Last-Event-ID = L`：判 L 是否有效（`min_id`/`max_id`）——
   - 无效（`L > max_id`，或 `L < min_id-1`：其下的行已被 purge）⇒ `resync` + `snapshot`，
     **绝不静默从 0 重放**；
   - 缺口 `max_id - L > YIBAN_SSE_GAP_MAX`（默认 500）⇒ 同样 `resync` + `snapshot`；
   - 可补 ⇒ 按 `id > L` 顺序补发 `progress`，再转常态。
   `id` 是全表自增、不按天重置 ⇒ 跨零点天然连续。
   **订阅必须在补发之前**：广播线程只推"订阅之后新增"的事件，补发覆盖"订阅时刻之前的
   表内区间"，两者叠起来才不漏事件（重叠部分按 id 去重）。

**连接治理（全部 < gunicorn `--timeout 60`）**
心跳 15 秒；每会话连接上限 3（`YIBAN_SSE_SESSION_MAX`）；全局上限**两条流各自预算**
（设计 §6.3"用户端流连接预算另算"）：管理员流 `YIBAN_SSE_CONNECTIONS_MAX`（缺省 10）、
用户流 `YIBAN_SSE_USER_CONNECTIONS_MAX`（缺省 10）；
空闲 300 秒（**无 progress 事件**）关；会话复查 60 秒（登出即 `end(session_expired)`）；
单连接硬上限 120 分钟。全部取自 `config/registry.json` 的键。
上限判定与订阅在 `run_stream.hub().try_subscribe` 的**同一把锁**内完成（check-and-subscribe
原子化），消除并发建连的 TOCTOU。

**隐私红线（125 §3.6）**
`progress` 与快照都不含手机号；`executor` 只回公开标签（角色 + 槽位序号）；
`message` 读取面复遮一次（纵深防御）；用户端流只推本人。

**鉴权与审计**
两端点**显式**校验会话与角色（长连接是权限旁路高发区）：progress 走强档主管理员判定
（注册管理员 403），my-signin 只要求会话已登录（弱档）。两端点都对 GET 补 `Origin`
同源校验（`EventSource` 不能自定义请求头，只能靠 Cookie；无 Origin 放行，语义同应用
既有的同源判定）。只读面留痕**只在建连时**调一次 `read_audit_trace()`——绝不每拍写
（每拍写 = 50 行/秒并串哈希链写者，125 §5.2）。

**归属**
`web.app.create_app` 的"数据"面（`/api/*`，过登录守卫）。装配在
`web.routes.register_all`。进程内广播器与帧封装在 `web/services/run_stream.py`。

**通信**
视图体经 `web.routes.appmod()` 取 web.app 的模块级名字（`logger` / 角色判定与同源判定
两枚 helper / `read_env` / `ENV_FILE` / `load_accounts` / `ACCOUNT_STATUS_ACTIVE`），
避免导入环；本人号码集合复用 `web/routes/my.py` 的归属口径。
"""
import contextlib
import time

from flask import Response, jsonify, request, session, stream_with_context

from web.routes import appmod as _appmod
from web.routes import read_audit_trace
from web.services import run_stream as _run_stream
from yiban import clock
from yiban.store import run_events as _run_events


def _same_origin(m):
    """GET 同源校验：沿用 `web.app._request_is_same_origin` 的单源语义（无 Origin 放行）。"""
    return m._request_is_same_origin()


def _last_event_id():
    """`Last-Event-ID` 头 → int（缺失/非法回 0）。浏览器原生重连自动带上。"""
    raw = request.headers.get("Last-Event-ID", "")
    try:
        return max(0, int(str(raw).strip()))
    except (TypeError, ValueError):
        return 0


def _my_phones(m):
    """当前会话用户的本人手机号集合（生效且未删除）；用户流只推这些号。

    归属口径复用 `web/routes/my.py`（`_my_account_indices_of` + `_my_active_phones`），
    避免"本人是谁"出现第二套定义。管理员会话也按同一归属规则取（其名下账号）。
    """
    from web.routes import my as _my
    accounts = m.load_accounts()
    indices = _my._my_account_indices_of(accounts)
    return _my._my_active_phones(accounts, indices)


def _sse_response(generator):
    """把生成器包成 SSE 响应；双保险关缓冲（应用侧回 `X-Accel-Buffering: no`）。"""
    resp = Response(stream_with_context(generator), mimetype="text/event-stream")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"
    return resp


def _end_only(reason):
    """只发一个 `end` 事件的流（连接上限/开关关闭等）。"""
    def gen():
        yield _run_stream.sse_frame("end", {"reason": reason})
    return _sse_response(gen())


def _end_ts():
    return clock.now().strftime("%Y-%m-%d %H:%M:%S")


def _user_snapshot():
    """用户端（`my-signin`）的最小快照：只给 cursor/day/生成时刻。

    **刻意不含全局 `totals` / `by_executor`**：那是管理员面口径，推给普通用户就是
    "推了不属于他的数据"。用户端只需要一个断线续传游标与业务日。
    """
    return {
        "cursor": _run_events.max_id(),
        "day": _run_events.default_day(),
        "generated_ts": _end_ts(),
    }


def _stream_generator(m, hub, cfg, last_id, sub, phones):
    """建连补发 + 常态扇出（管理员流与用户流共用；`phones` 非 None 即用户流）。

    `sub` 由端点在 `try_subscribe` 里**原子**登记后传入（上限判定与订阅同锁，见 125 §3.4
    与 `run_stream.hub`）。订阅先于补发：广播线程自此起投递"新增"事件，补发覆盖"此刻
    表内区间"，两者叠起来才不漏事件（重叠按 id 去重）。
    """
    heartbeat = cfg["heartbeat_sec"] or 15
    gap_max = cfg["gap_max"] if cfg["gap_max"] is not None else 500
    user_mode = phones is not None
    started = clock_monotonic()

    def snapshot(fresh=False):
        """→ 快照 payload。用户流用最小快照；管理员流走聚合快照（重同步时取新）。"""
        if user_mode:
            return _user_snapshot()
        return hub.snapshot(None, max_age=(0 if fresh else None))

    try:
        if last_id <= 0:
            snap = snapshot()
            yield _run_stream.sse_frame("snapshot", snap, event_id=snap["cursor"])
            cur = snap["cursor"]
        else:
            minid = _run_events.min_id()
            maxid = _run_events.max_id()
            invalid = (maxid == 0) or (last_id > maxid) or (
                minid is not None and last_id < minid - 1)
            if invalid or (maxid - last_id) > gap_max:
                yield _run_stream.sse_frame(
                    "resync",
                    {"reason": "cursor_invalid" if invalid else "gap_too_large",
                     "cursor": last_id})
                snap = snapshot(fresh=True)
                yield _run_stream.sse_frame("snapshot", snap, event_id=snap["cursor"])
                cur = snap["cursor"]
            else:
                cur = last_id
                while True:
                    rows = _run_events.stream_events(
                        cur, _run_events.STREAM_LIMIT, phones)
                    if not rows:
                        break
                    for e in rows:
                        cur = e["id"]
                        yield _run_stream.sse_frame("progress", e, event_id=e["id"])
                    if len(rows) < _run_events.STREAM_LIMIT:
                        break

        last_progress = clock_monotonic()
        last_check = last_progress
        while True:
            rows = sub.take(heartbeat)
            now = clock_monotonic()
            if rows:
                for r in rows:
                    rid = int(r["id"])
                    if rid <= cur:
                        continue          # 补发与广播的重叠：按 id 去重
                    cur = rid
                    yield _run_stream.sse_frame(
                        "progress", _run_events.render_stream_event(r), event_id=rid)
                last_progress = now
            else:
                yield _run_stream.sse_frame(
                    "heartbeat", {"ts": _end_ts(), "cursor": cur},
                    event_id=(cur or None))
            if cfg["idle_sec"] and now - last_progress >= cfg["idle_sec"]:
                yield _run_stream.sse_frame("end", {"reason": "idle_timeout"})
                return
            if cfg["connection_max_sec"] and now - started >= cfg["connection_max_sec"]:
                yield _run_stream.sse_frame("end", {"reason": "max_duration"})
                return
            if cfg["recheck_sec"] and now - last_check >= cfg["recheck_sec"]:
                last_check = now
                if m._current_role() is None:
                    yield _run_stream.sse_frame("end", {"reason": "session_expired"})
                    return
    finally:
        sub.close()
        hub.unsubscribe(sub)


def clock_monotonic():
    """单调钟（墙钟被拨时不致让超时判据跳变）。"""
    return time.monotonic()


def _session_key():
    """每会话连接计数的键：优先会话凭据 sid，退回用户名（同一标签页共享同一会话）。"""
    return str(session.get("sid") or session.get("username") or "?")


def api_stream_progress():
    """SSE：主管理员进度流（聚合计数 + 全部执行体事件；不含手机号）。

    授权走**强档**主管理员判定（与 `/api/scheduler/executors/progress` 同口径，见
    `web/routes/settings_api.py` 的该端点）。注册管理员（`users.role=admin`）虽被弱档
    角色判定当成 admin，仍得 403——执行体级运行细节不给它。匿名由登录守卫守成 401
    （本端点不自判未登录，与 settings_api 先例同形）。
    """
    m = _appmod()
    if not _same_origin(m):
        return jsonify({"error": "请求来源异常，请刷新页面后重试"}), 403
    if not m._is_builtin_admin_session():
        return jsonify({"error": "仅主管理员可查看实时进度流"}), 403
    cfg = _run_stream.stream_config(m.ENV_FILE, m.read_env)
    if not cfg["enabled"]:
        return _end_only("disabled")
    hub = _run_stream.hub()
    sub, reason = hub.try_subscribe(
        _session_key(), kind="admin", poll=cfg["poll_sec"] or 1, phones=None,
        session_max=cfg["session_max"], connections_max=cfg["connections_max"])
    if sub is None:
        return _end_only(reason)
    # 只读面留痕：**建连时一次**，绝不每拍写（125 §5.2）。
    with contextlib.suppress(Exception):
        read_audit_trace()("run_events_stream", "progress")
    return _sse_response(_stream_generator(
        m, hub, cfg, _last_event_id(), sub, None))


def api_stream_my_signin():
    """SSE：用户端流，只推**本人账号**的事件（服务端过滤；phone 不进 payload）。

    `progress` 事件实际下发的字段 = `id` / `ts` / `day` / `node` / `node_label` /
    `executor` / `executor_label` / `message`。`executor` 与 `executor_label` 都是
    **公开标签**（角色 + 槽位序号，经读取面 `_public_executor` / `_executor_label` 收敛），
    不含手机号与主机名；过滤用的 phone 不进 payload。快照只给 `cursor` / `day` / 生成时刻，
    **不含**全局 `totals` / `by_executor`（那是管理员面口径，推给普通用户就是越权）。

    连接预算与管理员流**分家**：本端点用 `YIBAN_SSE_USER_CONNECTIONS_MAX`（125 §6.3
    "用户端流连接预算另算"），管理员流另用 `YIBAN_SSE_CONNECTIONS_MAX`。
    """
    m = _appmod()
    if not _same_origin(m):
        return jsonify({"error": "请求来源异常，请刷新页面后重试"}), 403
    if m._current_role() is None:
        return jsonify({"error": "未登录"}), 401
    cfg = _run_stream.stream_config(m.ENV_FILE, m.read_env)
    if not cfg["enabled"]:
        return _end_only("disabled")
    hub = _run_stream.hub()
    phones = _my_phones(m)
    sub, reason = hub.try_subscribe(
        _session_key(), kind="user", poll=cfg["poll_sec"] or 1, phones=phones,
        session_max=cfg["session_max"], connections_max=cfg["user_connections_max"])
    if sub is None:
        return _end_only(reason)
    with contextlib.suppress(Exception):
        read_audit_trace()("run_events_stream", "my-signin")
    return _sse_response(_stream_generator(
        m, hub, cfg, _last_event_id(), sub, phones))


def register(app):
    """在本域注册两条 SSE 路由；endpoint 取函数名。"""
    app.add_url_rule("/api/stream/progress", view_func=api_stream_progress)
    app.add_url_rule("/api/stream/my-signin", view_func=api_stream_my_signin)
