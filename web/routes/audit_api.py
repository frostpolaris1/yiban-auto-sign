# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""审计只读路由：`GET /api/audit-logs`（管理员面）。

**功能**
按页读取审计链行，回答"谁在什么时候干了什么"。仅读，不写、不校验链。

**入库契约（七约束，改动即破坏 Vue 线对接，须同步前端）**
1. 分页显式：只认 `page`（默认 1，`1.._MAX_PAGE`）与 `page_size`（默认 50，1..200），
   两者都在响应里回显；不使用隐式 offset。上界 `_MAX_PAGE` 只挡
   `(page-1)*page_size` 溢出 SQLite 有符号 int64（溢出被下面的 `except` 兜成 500，
   实为客户端参数错，应 400）。
2. **响应分页信封（前端据此终止翻页）**：`{"rows": [...], "page": N, "page_size": M,
   "total": T, "has_more": bool}`。`rows` 是本页审计行（脱敏后，见第 5 条），
   `total` 是**当前过滤条件下**的命中总行数（忽略分页），
   `has_more == page * page_size < total`——前端翻页终止判据只看 `has_more`，
   不得用 `len(rows) == page_size` 自行推断（末页恰好满页时会多翻一次空页）。
3. 过滤字段白名单：只认 `action`/`actor`/`target`/`from_ts`/`to_ts`，出现任何其它
   查询键即 400 JSON（`{"error": "不支持的过滤字段: X"}`）。
4. 对外 id 不透明：不返回自增 `audit_logs.id`，行定位符用
   `yiban.adapters.persistence.audit_chain.public_row_id(id)`（HMAC 前 16 hex，字段名 `row_id`）。
5. 脱敏单出口：行一律经 `_serialize_audit_row()` 出 HTTP，username 走 `actor_tag()`、
   target/detail 走 `_nl_safe()` 后截断，原串绝不进响应。
6. 访问留痕：每次成功读取经 `read_audit_trace()` 聚合成一条只读面审计
   （动作 `audit_logs_read`），与 users/logs 等只读端点同口径——审计读取本身也留痕。
7. 错误一律 JSON：400/403/500 全 JSON（403 由登录守卫出，500 依赖 M38 的 /api/* 契约）。

**归属**
`web.app.create_app` 的"审计"面；权限复用登录守卫——非 `/api/my-*` 路径对普通用户
即 403（web/app.py 的 `require_login`），故本模块不另写鉴权。

**复用**
`register(app)` 供 `web.routes.register_all` 装配；只读实现住在
`yiban/adapters/persistence/audit_chain.py`（本模块不触 `verify_audit_chain`，读接口不触发全表哈希）。

**通信**
视图体经 `web.routes.appmod()` 取 web.app 自有的模块级名字（`logger`）；A 类转发名
`_nl_safe` 经真源模块直取（批 3-a 拆墙）。两条路径都不与 `web/app.py` 形成导入环；
被 `register_all(app)` 一次接入。
"""
from flask import jsonify, request

from web import security as _security
from web.routes import appmod as _appmod
from web.routes import read_audit_trace

# A 类转发名的真源（批 3-a：路由直连，不再经 web.app 转发墙）。
from web.services import notify_mail as _notify_mail
from yiban.adapters.persistence import audit_chain as _audit_chain

_DEFAULT_PAGE_SIZE = 50
_MAX_PAGE_SIZE = 200
# page 上界：只挡 (page-1)*page_size 溢出 SQLite 有符号 int64（溢出会让绑定抛异常、
# 被下面的 except 兜成 500，而这是客户端参数错）。按最大 page_size 反推最宽松上界，
# 故任何合法 page_size 都不会溢出；实际审计表行数远小于此，不构成功能限制。
_MAX_PAGE = (2 ** 63 - 1) // _MAX_PAGE_SIZE
_ALLOWED_QUERY_KEYS = {"page", "page_size", "action", "actor", "target", "from_ts", "to_ts"}
_TARGET_MAX = 120
_DETAIL_MAX = 200
_ACTION_MAX = 64


def _serialize_audit_row(row):
    """审计行的**唯一**脱敏出口：原串（username/target/detail）绝不进响应。

    username 经 `actor_tag()` 遮罩（与写入侧同一口径）、target/detail 经 `_nl_safe()`
    净化换行后截断；`row_id` 是不透明定位符（见入库契约第 3 条）。
    """
    return {
        "row_id": _audit_chain.public_row_id(row["id"]),
        "ts": str(row["ts"] or "")[:32],
        "actor": _audit_chain.actor_tag(row["username"] or ""),
        "action": _notify_mail._nl_safe(str(row["action"] or ""))[:_ACTION_MAX],
        "target": _notify_mail._nl_safe(str(row["target"] or ""))[:_TARGET_MAX],
        "detail": _notify_mail._nl_safe(str(row["detail"] or ""))[:_DETAIL_MAX],
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
    if page > _MAX_PAGE:
        # 上界只挡 int64 溢出，不暴露内部数值上限的成因（信息分层）
        return jsonify({"error": "page 超出范围"}), 400
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

    # 只读面留痕（与 users/logs 等只读端同口径）：审计读取本身也是一次访问，聚合成
    # 一条（target 用聚合量"本页 N 条"，不含任何原始行内容）。
    read_audit_trace()("audit_logs_read", f"本页 {len(rows)} 条")
    return jsonify({
        "rows": [_serialize_audit_row(r) for r in rows],
        "page": page,
        "page_size": page_size,
        "total": total,
        "has_more": page * page_size < total,
    })


def register(app):
    """在本域注册两条只读路由；endpoint 取函数名。"""
    app.add_url_rule("/api/audit-logs", view_func=api_audit_logs)
    app.add_url_rule("/api/admin/audit-status", view_func=api_audit_status)


#: 快照里锚点摘要**允许出接口**的键（白名单：只出计数与时刻，不出文件路径）。
_PRESENT_ANCHOR_KEYS = ("path_state", "lines", "old_format_lines",
                        "unparsable_lines", "max_id", "max_ts")


def _unavailable_payload(reason, indeterminate_reason):
    """拿不到可用结论时的应答：**无法定论**，绝不当成健康，也绝不静默略过。

    `reason` 是给人看的一句话；`indeterminate_reason` 是"判不出来的区间"那一段的短因。
    """
    return {
        "ok": True,
        "state": _audit_chain.VERDICT_INDETERMINATE,
        "verdict_code": _audit_chain.VERDICT_EXIT_CODES["indeterminate"],
        "reason": reason,
        "cached": False,
        "data_at": None,
        "snapshot_stale": True,
        "chain_ok": None,
        "broken": None,
        "anchor_status": None,
        "anchor": None,
        "indeterminate": {"present": True, "reason": indeterminate_reason},
        "known_debt": [],
        "write_failures": None,
        "empty_hash_rows": None,
        "note": "",
    }


def _no_snapshot_payload():
    """无体检快照时的结论（快照缺失或损坏到读不出）。"""
    return _unavailable_payload(
        "尚无体检结果（每日自检还没跑过或快照已损坏）——本次无法定论，"
        "不等于健康。每日线程在下一次换日自检后写入结果。",
        "尚无体检结果")


def _tampered_payload(snapshot, reason):
    """保留**更响**的 `tampered` 原态（不把篡改洗成中性的"无法定论"）。

    快照的 verdict 可能是坏的（装配抛异常）或签名不匹配。若它自己说的是
    `tampered`，那是比"判不出来"更严重的结论——按 M2 的口径，不得用中性结论
    把它盖掉（否则等于替攻击者消音）。只对**已知状态**保留，未知状态仍回中性。
    """
    base = _unavailable_payload(reason, reason)
    base.update({
        "state": _audit_chain.VERDICT_TAMPERED,
        "verdict_code": _audit_chain.VERDICT_EXIT_CODES[_audit_chain.VERDICT_TAMPERED],
        "cached": isinstance(snapshot, dict),
        "data_at": (str(snapshot.get("at") or "") or None)
        if isinstance(snapshot, dict) else None,
    })
    return base


def _snapshot_state(snapshot):
    """从（可能损坏的）快照里取原始 verdict 状态，取不到返回空串。"""
    if not isinstance(snapshot, dict):
        return ""
    verdict = snapshot.get("verdict")
    if not isinstance(verdict, dict):
        return ""
    state = str(verdict.get("state") or "")
    return state if state in _audit_chain.VERDICT_STATES else ""


def _payload_from_snapshot(snapshot):
    """把体检快照装配成响应体（可能抛：调用方按"无法定论"兜底）。

    超龄降级（M2）在这里做，**只降"健康"**：既有的"体检结论"是当时真验过的结果。
    绿色"健康"必须对应"最近真验过"——每日线程停摆时快照会冻结，页面若继续显示
    绿色就是本块唯一一条"界面说健康、现场可能不健康"的通路。反过来，超龄**不降**
    `tampered`："可能已被篡改"是更响的结论，用"判不出来"把它盖掉等于替攻击者消音
    （失效方向偏安全：宁可留一个陈旧告警，不可把告警洗成中性）。两种情形都如实回
    `snapshot_stale=true`，页面据此标注数据时刻已超龄。
    """
    health = snapshot.get("health") if isinstance(snapshot.get("health"), dict) else {}
    verdict = snapshot.get("verdict")
    if not isinstance(verdict, dict):
        verdict = _audit_chain.audit_verdict(health)
    state = str(verdict.get("state") or "")
    reason = str(verdict.get("reason") or "")
    if state not in _audit_chain.VERDICT_STATES:
        # 快照里存了一个不认识的状态：按"无法定论"处置，绝不放行成健康
        state = _audit_chain.VERDICT_INDETERMINATE
    stale = _audit_chain.audit_health_snapshot_stale(snapshot)
    if stale and state == _audit_chain.VERDICT_HEALTHY:
        state = _audit_chain.VERDICT_INDETERMINATE
        reason = (f"体检快照已超龄（数据时刻 {snapshot.get('at') or '未知'}，"
                  f"阈值 {_audit_chain.AUDIT_HEALTH_SNAPSHOT_MAX_AGE_SEC // 3600} 小时）"
                  "——自检可能已停摆，本次无法定论，不等于健康。")
    anchor = snapshot.get("anchor")
    anchor = ({k: anchor.get(k) for k in _PRESENT_ANCHOR_KEYS}
              if isinstance(anchor, dict) else None)
    debt = snapshot.get("known_debt")
    debt = [d for d in debt if isinstance(d, dict)] if isinstance(debt, list) else []
    return {
        "ok": True,
        "state": state,
        "verdict_code": _audit_chain.VERDICT_EXIT_CODES[state],
        "reason": reason,
        "cached": True,
        "data_at": str(snapshot.get("at") or "") or None,
        "snapshot_stale": bool(stale),
        "chain_ok": health.get("chain_ok"),
        "broken": health.get("broken"),
        "anchor_status": health.get("anchor_status"),
        "anchor": anchor,
        "indeterminate": {"present": state == _audit_chain.VERDICT_INDETERMINATE,
                          "reason": reason},
        "known_debt": [
            {"code": str(d.get("code") or ""),
             "label": _notify_mail._nl_safe(str(d.get("label") or ""))[:120],
             "count": int(d.get("count") or 0),
             "reason": _notify_mail._nl_safe(str(d.get("reason") or ""))[:200]}
            for d in debt
        ],
        "write_failures": health.get("write_failures"),
        "empty_hash_rows": health.get("empty_hash_rows"),
        "note": _notify_mail._nl_safe(str(health.get("note") or ""))[:200],
    }


def api_audit_status():
    """审计链自洽状态块（巡检面第二刀 ⑤，工单 vjy9）。

    **只读体检快照**（`app_meta.audit_health_snapshot`，由 web 每日线程写入）：
    本端点不重算全表哈希（v1 大表全扫会把库跑满），也不写 `.env`/锚点文件。
    快照缺失、损坏到读不出、或**超龄**时一律答"无法定论"，绝不当成健康。

    返回三态之一，与 `scripts/audit_verify.py` 的退出码同源（`audit_verdict`）：
    `healthy`(0) / `tampered`(1) / `indeterminate`(2)。另给：
    · `data_at`：这份结果是什么时候跑出来的（数据时刻；不标时刻的缓存会被当成当下结论）；
    · `snapshot_stale`：数据时刻是否超龄（端点侧判；超龄的"健康"已降级为无法定论）；
    · `anchor`：校验到哪个锚点（行数、定点 `max_id`/`max_ts`、老格式行数）；
    · `indeterminate`：有没有"判不出来"的区间（present + reason）；
    · `known_debt`：**已知欠账**逐条（单独一行显示，不与"篡改"混色）。
    隐私：只出计数与时刻，不出账号明细。

    装配整体包在 try/except 里：本端点自己的威胁模型就是"快照可能被篡改或损坏"，
    一条坏字段（如 `count` 不是整数）就把这面镜子打成 500，既不假绿也不诚实回答。

    **验签（ums7）**：快照住在应用身份可写的 app_meta，光凭内容无法区分"每日线程写的"
    与"有人改成 healthy 的"。每日线程写快照后用审计 HMAC 密钥签名（独立 app_meta 键），
    本端点先验签再看内容：签名缺失/不匹配/密钥不可用一律答"无法定论"；若快照自称
    `tampered`，保留该更响的状态（不得洗成中性）。
    """
    m = _appmod()
    sig_state, raw = _security.verify_audit_health_snapshot()
    if sig_state == "missing":
        read_audit_trace()("audit_status_read", "无体检快照")
        return jsonify(_no_snapshot_payload())
    snapshot = _security.raw_audit_health_snapshot(raw)
    if sig_state != "ok":
        # 无法确认这份快照是每日线程写的：不得显示健康。
        # 伪造者若写 "tampered" 只是让结论更响，保留它不会制造假绿。
        reason = {
            "unsigned": "体检快照缺少签名（无法确认来源）",
            "bad": "体检快照验签失败（内容已被改写）",
            "key-unavailable": "审计密钥不可用，体检快照无法验签",
        }.get(sig_state, "体检快照无法验签")
        read_audit_trace()("audit_status_read", f"验签失败（{sig_state}）")
        if _snapshot_state(snapshot) == _audit_chain.VERDICT_TAMPERED:
            return jsonify(_tampered_payload(snapshot, reason))
        return jsonify(_unavailable_payload(
            f"{reason}——本次无法定论，不等于健康。", reason))
    if not isinstance(snapshot, dict):
        read_audit_trace()("audit_status_read", "无体检快照")
        return jsonify(_no_snapshot_payload())
    try:
        payload = _payload_from_snapshot(snapshot)
    except Exception as e:
        m.logger.warning("审计体检快照装配失败（按无法定论回答）: %s", e)
        read_audit_trace()("audit_status_read", "快照不可读")
        if _snapshot_state(snapshot) == _audit_chain.VERDICT_TAMPERED:
            return jsonify(_tampered_payload(
                snapshot, "体检快照无法解析（可能被篡改或损坏）"))
        return jsonify(_unavailable_payload(
            "体检快照无法解析（可能被篡改或损坏）——本次无法定论，不等于健康。",
            "体检快照无法解析"))
    read_audit_trace()("audit_status_read", f"结论 {payload['state']}")
    return jsonify(payload)
