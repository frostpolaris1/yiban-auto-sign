# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""数据组路由：签到日志的查看/导出、签到事件的结构化查询与系统状态探针（管理员面）。

**功能**
`GET /api/logs` 按天签到日志（检索 q / 全量 all / 探针与签到事件摘要）；
`GET /api/logs/export` 某日日志的脱敏导出（与展示层同一条可见性过滤管线）；
`GET /api/admin/sign-events` 单账号时间线 / 实时事件流 / 按天统计；
`GET /api/clock` 与 `POST /api/ping` 是数据总览页「系统状态」卡的两条探针

**归属**
`web.app.create_app` 的"日志与状态"面；数据总览页（/data/dashboard）是页面路由，
属 `web/routes/pages.py`。工厂骨架、跨域中间件与库访问层仍留在 `web/app.py`。

**复用**
`register(app)` 供 `web.routes.register_all` 装配；`export_limits()` 取回日志导出的
每 IP 限速表（每 app 实例一份），`read_audit_trace()` 是只读面聚合留痕入口。

**通信**
视图体不直接读 web.app 的模块级名字，一律经 `web.routes.appmod()` 按属性取——测试用
`mock.patch.object(web.app, …)` 打桩（db / logger / clock / signin / 日志解析助手 /
ENV_FILE 等）必须继续生效。日志行一律经 `_log_lines_for()` 过滤、`_mask_log_phones()`
脱敏后才出 HTTP；被 `web.app` 的 `register_all(app)` 一次接入。
"""
import os
import time
from datetime import timedelta

from flask import Response, jsonify, request, session

from web.routes import appmod as _appmod
from web.routes import export_limits, read_audit_trace


def api_logs_export():
    """导出某日签到日志（脱敏副本，管理员）。

    date 必填且强校验（防路径穿越），文件名仅含日期。响应体不是磁盘原
    文件：按天日志在盘上按设计保留完整手机号（signin 状态解析 / run.sh
    依赖），HTTP 出口必须与 /api/logs 展示层同一契约——经 _log_lines_for
    （yiban 全量 + 其余组件仅告警级，同一尾部读取封顶）过滤后逐行
    _mask_log_phones 脱敏，任何登录身份都无法经 HTTP 取得未脱敏号码。
    成功导出写 logs_export 审计留痕（400/404 不写），并受每 IP 窗口限速。
    """
    m = _appmod()
    date = str(request.args.get("date", "")).strip()
    if not m._is_valid_date_str(date):
        return jsonify({"error": "日期格式不正确，应为 YYYY-MM-DD"}), 400
    ip = m._client_ip()
    now = time.time()
    with m._rate_lock:
        m._ip_store_trim(export_limits(), m.EXPORT_WINDOW + m._IP_STORE_MAX_AGE)
    _cnt, _start, allowed = m._bump_window_count(
        export_limits(), ip, now, m.EXPORT_WINDOW, limit=m.EXPORT_MAX
    )
    if not allowed:
        return jsonify({"error": "导出过于频繁，请稍后再试"}), 429
    if not os.path.exists(m.log_path_for(date)):
        return jsonify({"error": f"{date} 无签到日志"}), 404
    text = "".join(m._mask_log_phones(ln) + "\n" for ln in m._log_lines_for(date))
    m.db.audit(
        session.get("username") or "?",
        "logs_export",
        date,
        f"导出 {date} 签到日志（脱敏）",
    )
    return Response(text, mimetype="text/plain", headers={
        "Content-Disposition": f"attachment; filename=sign-{date}.log",
    })


def api_logs():
    """签到日志与今日状态。

    ?date=YYYY-MM-DD（可选，仅管理员）：缺省=最近有日志的一天（优先今天）；
    指定日期时 logs 为该日日志、states 仍为今日状态（账号表格图标语义不随历史日期变化）。
    """
    m = _appmod()
    date = str(request.args.get("date", "")).strip()
    if date and not m._is_valid_date_str(date):
        return jsonify({"error": "日期格式不正确，应为 YYYY-MM-DD"}), 400
    # 默认：今天有日志则显示今天，否则找最近有日志的一天（_most_recent_log_date 内部先查今天）
    if not date:
        date = m._most_recent_log_date()
    logs = m._log_lines_for(date)
    # 检索与全量查看。q=子串过滤（大小写不敏感，作用于当日全量行）；
    # all=1 返回当日全部行（封顶 5000 行防拖垮浏览器，truncated 标记）；
    # 缺省仍返回最后 80 行（轮询口径不变，靠前日志经 all=1 或导出获取）。
    q = str(request.args.get("q", "")).strip()
    show_all = str(request.args.get("all", "")).strip() == "1"
    masked_all = [m._mask_log_phones(ln) for ln in logs]
    total_lines = len(masked_all)
    if q:
        _ql = q.lower()
        masked_all = [ln for ln in masked_all if _ql in ln.lower()]
    if show_all:
        _LOG_VIEW_CAP = 5000
        out_lines = masked_all[:_LOG_VIEW_CAP]
        truncated = len(masked_all) > _LOG_VIEW_CAP
    else:
        out_lines = masked_all[-80:]
        truncated = False
    # 探针结构化事件：stage="probe" 若无 HTTP 出口只落库不可见，故随日志接口
    # 附带当日探测记录（独立字段，不混入签到文本流；手机号打码，条数封顶）。
    probe_events = []
    try:
        for ev in m.db.probe_events_on(date, limit=100):
            msg = str(ev.get("message") or "")
            probe_events.append({
                "time": str(ev.get("ts", ""))[-8:] if ev.get("ts") else "",
                "phone": m.signin._mask_phone(str(ev.get("phone") or "")),
                "status": str(ev.get("status") or ""),
                "message": (msg[:160] + "…") if len(msg) > 160 else msg,
            })
    except Exception as e:
        m.logger.warning("probe_events 查询失败（不影响日志页）: %s", e)
        probe_events = []
    # 当日签到事件（stage="sign"）与探针记录同口径脱敏展示（手机号打码、条数封顶）。
    sign_events = []
    try:
        for ev in m.db.sign_events_on(date, limit=100):
            msg = str(ev.get("message") or "")
            sign_events.append({
                "time": str(ev.get("ts", ""))[-8:] if ev.get("ts") else "",
                "phone": m.signin._mask_phone(str(ev.get("phone") or "")),
                "status": str(ev.get("status") or ""),
                "attempt": int(ev.get("attempt") or 0),
                "message": (msg[:160] + "…") if len(msg) > 160 else msg,
            })
    except Exception as e:
        m.logger.warning("sign_events 查询失败（不影响日志页）: %s", e)
        sign_events = []
    # 空态一键跳转的「最近有数据日期」：仅在该标签当前无数据时才查库，
    # 避免每次 10s 轮询都多做两次聚合查询。检索态（q 非空）不给出口——
    # 「无匹配」是检索结果，不是日期没数据。
    recent_log_date = ""
    recent_probe_date = ""
    recent_sign_date = ""
    if not logs and not q:
        candidate = m._most_recent_log_date()
        if candidate and candidate != date:
            recent_log_date = candidate
    if not probe_events:
        candidate = m.db.sign_events_recent_date("probe")
        if candidate and candidate != date:
            recent_probe_date = candidate
    if not sign_events:
        candidate = m.db.sign_events_recent_date("sign")
        if candidate and candidate != date:
            recent_sign_date = candidate
    # 响应层脱敏：日志行内 [手机号] 不落完整号（前端 maskPhone 幂等兼容）。
    # 注意：不返回 states——账号表格图标的事实源是 /api/accounts（sign-state 文件），
    # 日志符号（✅/❌）与状态码（success/failed）语义不同，曾造成前端图标/统计卡被
    # 符号污染。
    read_audit_trace()("logs_read", f"{date} {total_lines} 行")
    return jsonify(
        {
            "ok": True,
            "logs": out_lines,
            "total_lines": total_lines,
            "returned": len(out_lines),
            "truncated": truncated,
            "q": q,
            "log_file": f"sign-{date}.log",  # 只暴露文件名，不暴露服务器路径
            "date": date,
            "is_today": date == m.clock.now().strftime("%Y-%m-%d"),
            "probe_events": probe_events,
            "sign_events": sign_events,
            # 三块各自的「最近有数据日期」（空态一键跳转用；当前日期即最近时为空串）
            "recent_log_date": recent_log_date,
            "recent_probe_date": recent_probe_date,
            "recent_sign_date": recent_sign_date,
        }
    )


def api_admin_sign_events():
    """sign_events 结构化查询：单账号时间线 / 实时事件流 / 按天统计。

    权限：路径不在普通用户白名单，require_login 统一 403（普通用户越权访问
    会另记 forbidden_path 审计）。手机号一律打码后返回，条数封顶 200。
    """
    m = _appmod()
    phone = str(request.args.get("phone", "")).strip()
    try:
        days = min(max(int(request.args.get("days", 7)), 1), 90)
    except (TypeError, ValueError):
        days = 7
    try:
        limit = min(max(int(request.args.get("limit", 100)), 1), 200)
    except (TypeError, ValueError):
        limit = 100
    # stage 过滤：sign=真实签到、probe=健康探针；缺省不过滤（两者混算，保持旧行为）。
    # 两种事件共用 sign_events 表，不区分会让探针的成功/失败污染签到成功率，
    # 故需要「签到口径」的调用方显式传 stage=sign。取值走白名单 + 参数绑定。
    stage = str(request.args.get("stage", "")).strip().lower()
    if stage not in ("sign", "probe"):
        stage = ""

    def _mask(ev):
        msg = str(ev.get("message") or "")
        return {
            "ts": str(ev.get("ts", "")),
            "phone": m.signin._mask_phone(str(ev.get("phone") or "")),
            "status": str(ev.get("status") or ""),
            "message": (msg[:160] + "…") if len(msg) > 160 else msg,
            "stage": str(ev.get("stage") or ""),
            "attempt": int(ev.get("attempt") or 0),
            "dur_sec": ev.get("dur_sec"),
        }

    events = []
    if phone:
        events = [_mask(ev) for ev in m.db.sign_events_by_phone(phone, days=days)][:limit]
    else:
        cutoff = (m.clock.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        events = [_mask(ev) for ev in m.db.sign_events_since(cutoff, limit=limit)]
    if stage:
        # 事件流本身不带 stage 过滤（sign_events_since 无该参数），故在此收口；
        # 代价是过滤后条数可能少于 limit，对展示样本无影响。
        events = [ev for ev in events if ev["stage"] == stage]
    # 单账号时间线的读取目标就是那个手机号（脱敏后进审计）；无 phone 的整表扫
    # 只有取数量可摘要，与"谁在按天拖事件流"这一威胁问题同粒度
    read_audit_trace()(
        "sign_events_read",
        m.signin._mask_phone(phone) if phone else f"{days} 天 {len(events)} 条事件",
    )
    stats = m.db.sign_event_stats(days=days, stage=stage or None)
    return jsonify(
        {
            "ok": True,
            "days": days,
            "stage": stage,
            "count": len(events),
            "events": events,
            "daily_stats": stats,
        }
    )


def api_ping():
    m = _appmod()
    ok, detail = m.check_connectivity()
    return jsonify({"ok": True, "reachable": ok, "detail": detail})


def api_clock():
    m = _appmod()
    text, color = m.sign_status()
    try:
        tz_offset_min = int(m.clock.now().astimezone().utcoffset().total_seconds() // 60)
    except Exception:
        tz_offset_min = 0
    return jsonify(
        {
            "ok": True,
            "now": m.clock.now().strftime("%Y-%m-%d %H:%M:%S"),
            "server_ts": int(time.time()),  # 服务器 epoch 秒，供前端平滑走秒与校准
            "tz_offset_min": tz_offset_min,  # 服务器本地时区相对 UTC 的分钟偏移
            "sign_status": text,
            "color": color,
        }
    )


def register(app):
    """在本域注册五条数据路由；endpoint 取函数名（url_for 依赖）。"""
    app.add_url_rule("/api/logs/export", view_func=api_logs_export)
    app.add_url_rule("/api/logs", view_func=api_logs)
    app.add_url_rule("/api/admin/sign-events", view_func=api_admin_sign_events)
    app.add_url_rule("/api/ping", view_func=api_ping, methods=["POST"])
    app.add_url_rule("/api/clock", view_func=api_clock)
