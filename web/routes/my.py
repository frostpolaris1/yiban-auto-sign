# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""个人自助族路由：用户管理自己账号的提交/查看/编辑/删除/暂停、自选时间片与个人历史。

**功能**
`GET/POST /api/my-accounts` 与 `PUT/DELETE /api/my-accounts/<idx>`、
`POST /api/my-accounts/<idx>/restore`、`PUT /api/my-accounts/<idx>/pause`：本人账号的
提交、列表、编辑、软删与撤销、自暂停；`GET/PUT /api/my-time-pref` 与
`GET /api/time-prefs/stats`：自选时间片的读写、拥挤度与管理员侧逐片人数；
`GET/DELETE /api/verify-jobs/<job_id>`：在线校验异步任务的查询与取消；
`GET /api/my-calendar` 与 `GET /api/my-logs`：本人已生效账号的月历与按天日志。

**归属**
`web.app.create_app` 的"个人自助"面（普通用户路径，只读/只写本人数据）。工厂骨架、
跨域中间件（前置限速、登录守卫、CSRF、同源校验、安全响应头）与库访问层仍留在
`web/app.py`，本模块只提供十三条视图与本人视图助手。

**复用**
`register(app)` 供 `web.routes.register_all` 装配；`verify_fails()` / `verify_limits()`
取回账号验证冷却与配额（与管理员添加路径共用同一份账）。

**通信**
视图体不直接读 web.app 的模块级名字，一律经 `web.routes.appmod()` 按属性取——测试用
`mock.patch.object(web.app, …)` 打桩（db / clock / logger / load_accounts /
validate_account / send_notification / mailer / mail_layout / ENV_FILE 等）必须继续生效。
本人身份一律取 `session["username"]`，数据范围由 `_my_account_indices_of()` 按归属邮箱
收窄；被 `web.app` 的 `register_all(app)` 一次接入。
"""
import calendar
import json
import os
import sqlite3
import time
from datetime import datetime, timedelta

from flask import jsonify, request, session

from web.routes import appmod as _appmod
from web.routes import verify_fails, verify_limits


def _my_account_indices_of(accounts):
    """按账号列表快照计算当前用户的账号下标（锁内调用，避免重复读文件）。

    管理员：**仅本人邮箱归属**的账号（一人一号）。无人认领的裸账号（owner='admin'）
    属「代管」，只在账号管理页（/api/accounts）维护，不进「我的账号」视图 ——
    否则内置管理员会把名下全部裸账号当成"我的账号"列出，与一人一号口径冲突。
    普通用户：本人邮箱（含待删除，用于展示「已删除」状态；单账号限制在提交处另行排除）。

    注意：裸账号的 owner 是字面量 `admin`，当 YIBAN_ADMIN_USER 也取 `admin`（默认值）
    时两者不可区分；生产建议把 YIBAN_ADMIN_USER 设为管理员本人邮箱。
    """
    m = _appmod()
    email = session.get("username", "").lower()
    if m._current_role() == "admin":
        return [
            i for i, a in enumerate(accounts) if a.get("owner") == email and not a.get("deleted")
        ]
    return [i for i, a in enumerate(accounts) if a.get("owner") == email]


def _my_active_phones(accounts, indices):
    """历史查询用手机号（历史数据隔离）：仅取已生效
    （status=active）且未软删除账号——待审核/已拒绝/软删除行不参与签到，
    其历史日历/日志也不再回显，防"提交个 pending 号就翻到该号全部历史"。
    /api/my-accounts 列表展示口径不变（仍含 pending/deleted，展示状态用）。"""
    m = _appmod()
    return [
        str(accounts[i].get("phone", ""))
        for i in indices
        if accounts[i].get("status") == m.ACCOUNT_STATUS_ACTIVE
        and not accounts[i].get("deleted")
    ]


def _my_account_view(accounts, indices):
    """用户视图：账号脱敏 + 今日状态（结构化状态文件）+ 审核状态 + 最近相关日志 + 排队信息。

    排队说明：开启签到调度时按当日计划时间执行，否则按账号列表顺序（队列重试）；
    queue_ahead = 自己账号之前、今日尚未了结（未 success/already/no_task）的已生效账号数。
    """
    m = _appmod()
    recent = m.parse_sign_log(m.log_path_for())  # 最近日志仅用于「最近签到记录」展示（按天文件 = 今天）
    states = m.load_sign_state()  # 今日状态事实源（signin.py 写入）
    # 参与排队队列的账号：已生效（active，pending 不参与签到）且未软删除、未自暂停
    active = [
        a for a in accounts
        if a.get("status") == m.ACCOUNT_STATUS_ACTIVE and not a.get("deleted")
        and not a.get("user_paused", False)
    ]
    # 执行顺序（调度 v2）：优先按今日计划时间（sign-state scheduled 字段，
    # cron 生成后即真实执行顺序——覆盖自选/正态/随机模式）；计划未生成（06:31 前）回退列表顺序。
    # scheduled 为 "HH:MM:SS" 字符串，字典序即时间序；无计划者排在有计划者之后（列表序兜底）。
    def _exec_order_key(a):
        st = states.get(a.get("phone", ""), {})
        sched = st.get("scheduled", "") if isinstance(st, dict) else ""
        return (0 if sched else 1, sched, a.get("sort_order", 0))

    active_sorted = sorted(active, key=_exec_order_key)
    # 排队位置预计算（单次遍历累计，替代每个账号 O(pos) 切片求和）
    queue_before = {}
    running = 0
    for a in active_sorted:
        queue_before[a.get("phone", "")] = running
        st_status = states.get(a.get("phone", ""), {}).get("status", m.STATUS_PENDING)
        if st_status not in (m.STATUS_SUCCESS, m.STATUS_ALREADY, m.STATUS_NO_TASK):
            running += 1
    # 今日前缀：账号卡片「最近签到记录」只显示今天的日志（日志文件跨多天时避免混入历史）
    today_prefix = f"[{m.clock.now().strftime('%Y-%m-%d')} "
    result = []
    for i, real_idx in enumerate(indices):
        acc = accounts[real_idx]
        phone = acc.get("phone", "")
        my_logs = [
            line for line in recent
            if line.startswith(today_prefix) and f"[{phone}]" in line
        ]
        # 排队：按今日计划时间排序的队列中，自己之前未了结的账号数（含自暂停排除）
        queue_ahead = 0
        if acc.get("status") == m.ACCOUNT_STATUS_ACTIVE and not acc.get("user_paused", False):
            queue_ahead = queue_before.get(phone, 0)
        st = states.get(phone, {})
        st_status = st.get("status", m.STATUS_PENDING) if isinstance(st, dict) else m.STATUS_PENDING
        result.append(
            {
                "index": i,
                "name": acc.get("name", ""),
                "display_name": acc.get("name") or f"账号{i + 1}",
                "phone": phone,
                "phone_model": acc.get("phone_model", ""),
                "status": acc.get("status", m.ACCOUNT_STATUS_ACTIVE),
                "reject_reason": acc.get("reject_reason", ""),
                "state_icon": m.STATUS_ICON.get(st_status, "⏳"),
                "state_status": st_status,  # 状态码（前端按码映射文案）
                "state_message": st.get("message", "") if isinstance(st, dict) else "",
                "queue_ahead": queue_ahead,
                # 出站脱敏：与 /api/my-logs、/api/logs
                # 统一口径——当前 signin.py 日志每行仅含本人手机号，但口径不设防时，
                # 未来出现一行多号的日志格式会把他人号码原样下发普通用户
                "logs": [m._mask_log_phones(ln) for ln in my_logs[-5:]],
                "deleted": bool(acc.get("deleted")),
                "deleted_at": acc.get("deleted_at", ""),
                # 用户删除可撤销：仅本人自删行（deleted_by=本人）前端展示撤销入口
                "deleted_by_me": bool(acc.get("deleted"))
                and acc.get("deleted_by", "")
                == (session.get("username", "") or "").strip().lower(),
                "user_paused": bool(acc.get("user_paused", False)),  # 用户自暂停（调度 v2）
                # 用户确认：管理员账号（owner=admin）不支持自暂停——
                # 暂停是普通用户管理自己账号的能力；该字段仅管理员视图可见（前端据此隐藏按钮）
                "pause_forbidden": acc.get("owner", "admin") == "admin" and not acc.get("deleted"),
            }
        )
    return result


def _my_phone():
    """当前用户的自选绑定账号（与「我的账号」视图同口径）。

    普通用户=本人账号；内置管理员=本人邮箱归属的账号（一人一号，裸账号不参与）；
    注册管理员=归属本人邮箱的账号。
    ——若按 owner='admin' 归属裸账号，注册管理员会绑定到内置管理员的账号上，
    选片显示/保存互相覆盖，故一律按本人邮箱归属取号。
    仅 status=active（正式进入签到列表）才算——pending/rejected 的"注册但未生效"
    用户不可查看/选择时间片（GET 返回 has_account=False → 前端整卡隐藏；PUT 400 兜底）。
    """
    m = _appmod()
    accounts = m.load_accounts()
    for idx in _my_account_indices_of(accounts):
        acc = accounts[idx]
        if not acc.get("deleted") and acc.get("status") == m.ACCOUNT_STATUS_ACTIVE:
            return acc.get("phone", "")
    return None


def _pref_slots(sw):
    """窗口内 5 分钟片（时钟对齐）：[{slot_min, label, disabled, edge_note}]。

    掐头去尾前后独立：完全落入裁剪区（裁剪 >= 5 分钟覆盖整块）的片标记
    disabled（前端灰色不可选）；部分落入（如前裁 2 分钟 → 首片剩 3 分钟可用）的片
    标记 edge_note 提示且仍可点选（调度在可用部分内安排）。返回全部片（含 disabled），
    前端据此渲染，保证"满 5 分钟才完全灰掉、不足时提示"的需求语义。
    """
    m = _appmod()
    start_min = sw[0][0] * 60 + sw[0][1]
    end_min = sw[1][0] * 60 + sw[1][1]
    span = end_min - start_min
    front_min = m.edge_config()[0] / 60.0
    back_min = m.edge_config()[1] / 60.0
    slots = []
    for b in range(start_min, end_min, 5):
        off = b - start_min  # 片起点相对窗口起点的分钟偏移
        lo = max(off, front_min)
        hi = min(off + 5, span - back_min)
        disabled = hi <= lo  # 整片在裁剪区内（裁剪值 >= 5 分钟覆盖）
        note = ""
        if not disabled and (off < front_min or off + 5 > span - back_min):
            # 部分裁剪：提示"开头/结尾保留 X 分钟"，仍可选
            if off < front_min:
                note = f"开头 {front_min:g} 分钟保留"
            else:
                note = f"结尾 {back_min:g} 分钟保留"
        slots.append({
            "slot_min": off,
            "label": f"{b // 60:02d}:{b % 60:02d}",
            "disabled": disabled,
            "edge_note": note,
        })
    return slots


def _verify_job_visible(job, username):
    """归属校验：任务只有**本人**读得见、取消得了；管理面仅内置主管理员放行。

    "取消"会让对方的新账号一直停在「校验中」——跨归属的可用性动作，不该是
    "同为管理员"就能做的；按 `role == "admin"` 放行等于任意注册管理员都能读/取消
    别人的任务，故按"权限歧义取窄侧"收窄为：本人 + 内置主管理员。
    """
    m = _appmod()
    if m._is_builtin_admin_session():
        return True
    return str(job.get("owner_email") or "").strip().lower() == str(username or "").strip().lower()


def _job_payload(job):
    m = _appmod()
    return {
        "job_id": job["id"],
        "status": job["status"],
        "error": job.get("error") or "",
        "phone": m._mask_phone(job.get("phone", "")),
        "created_at": job.get("created_at"),
        "started_at": job.get("started_at"),
        "finished_at": job.get("finished_at"),
    }


def api_my_accounts():
    m = _appmod()
    # 单快照：下标与视图必须基于同一份账号列表——两次读取之间其他线程的物理删除
    # 会让下标套错行，极端时短暂展示他人账号（含完整手机号）
    accounts = m.load_accounts()
    indices = _my_account_indices_of(accounts)
    return jsonify({"ok": True, "accounts": _my_account_view(accounts, indices)})


def api_my_time_pref():
    """我的自选 + 拥挤度 + 预计签到时段（选片卡片数据；总开关关时仍可预配置，调度侧不激活）。

    拥挤度防调研：普通用户端只下发「已选百分比」（整数，四舍五入），
    不下发真实人数/块容量——不知道 K 无法反推人数；管理端 stats 接口保留精确计数。
    """
    m = _appmod()
    sw = m._sign_window()
    phone = _my_phone()
    pref = m.db.get_time_pref(phone) if phone else None
    stats = {s["slot_min"]: s["count"] for s in m.db.time_pref_stats()}
    cap = m.load_env_int(m.ENV_FILE, "YIBAN_BLOCK_CAP", 15)
    slots = []
    for s in _pref_slots(sw):
        count = stats.get(s["slot_min"], 0)
        # 粗粒度 10% 档：精确百分比 + 已知默认 K 可反推人数；
        # 未满封顶 90、满员恰好 100——前端 pct>=100 判满精确（19/20=95% 不会再被
        # 四舍五入成 100 误报"已选满"，与后端 count>=cap 口径一致）
        if cap > 0:
            pct = 100 if count >= cap else min(90, round(count * 100 / cap / 10) * 10)
        else:
            pct = 0
        slots.append({
            "slot_min": s["slot_min"],
            "label": s["label"],
            "pct": pct,
            "disabled": s["disabled"],     # 完全在裁剪区 → 灰色不可选
            "edge_note": s["edge_note"],   # 部分裁剪提示（如"开头 2 分钟保留"）
        })
    estimated, estimate_note = m._estimate_slot(phone) if phone else (None, "")
    front_sec, back_sec = m.edge_config()
    return jsonify({
        "ok": True,
        "pref": m._slot_to_label(pref["slot_min"]) if pref else None,
        "pref_slot": pref["slot_min"] if pref else None,
        "slots": slots,
        "allowed": m.load_env_int(m.ENV_FILE, "YIBAN_ALLOW_TIME_PREF", 0) == 1,
        "window": f"{sw[0][0]:02d}:{sw[0][1]:02d} ~ {sw[1][0]:02d}:{sw[1][1]:02d}",
        "edge_sec": front_sec,                    # 兼容旧前端（=前裁）
        "edge_front_sec": front_sec,              # 前后独立
        "edge_back_sec": back_sec,
        "has_account": bool(phone),
        "estimated": estimated,        # 预计签到时段（顺序排序可预期；随机为 None）
        "estimate_note": estimate_note,
    })


def api_my_time_pref_save():
    """保存/清除自选：{slot_min: int|null}。校验 5 对齐、窗口内；生效按分界时刻提示。"""
    m = _appmod()
    # read-check-write 整段持 _file_lock 原子化（防 TOCTOU）：
    # 并发 purge 删号时不再重新插入孤儿 pref（已删 phone 残留→重占号被新账号继承泄漏）；
    # 冷却检查与写入原子化（防并发双请求绕过冷却）；满员统计与写入原子化（防超容写入）
    with m._file_lock:
        phone = _my_phone()
        if not phone:
            # 非正式用户不可选时间片——区分"未提交"与"已提交未生效"，提示不给待审核
            # 用户误导（信息分层，不暴露审核细节）
            # has_submitted 与「我的账号」同口径（_my_account_indices_of），避免双口径漂移；
            # 单快照读取消除双读间列表漂移的越界/错判窗口
            _accounts_now = m.load_accounts()
            has_submitted = any(
                not _accounts_now[i].get("deleted")
                for i in _my_account_indices_of(_accounts_now)
            )
            if has_submitted:
                return jsonify({"error": "账号审核通过后即可选择签到时间"}), 400
            return jsonify({"error": "请先提交易班账号"}), 400
        data = m._json_body()
        slot = data.get("slot_min")
        if slot is None:
            m.db.clear_time_pref(phone)
            m.db.audit(session.get("username", "?"), "time_pref_clear", m.db.hash_phone(phone), "")
            return jsonify({"ok": True, "msg": "已清除自选，恢复自动分配"})
        # 严格类型校验——bool（False→0）与小数（5.9→5）截断不得误入合法槽位
        if isinstance(slot, bool) or (isinstance(slot, float) and not slot.is_integer()):
            return jsonify({"error": "时间片取值无效"}), 400
        try:
            slot = int(slot)
        except (TypeError, ValueError):
            return jsonify({"error": "时间片取值无效"}), 400
        sw = m._sign_window()
        span = (sw[1][0] * 60 + sw[1][1]) - (sw[0][0] * 60 + sw[0][1])
        front_min = m.edge_config()[0] / 60.0
        back_min = m.edge_config()[1] / 60.0
        # 前后独立裁剪：部分落入裁剪区的片（如首片剩 3 分钟）允许保存，
        # 调度会在可用部分内安排；完全落入裁剪区（前端已置灰）拒绝。
        if slot % 5 != 0 or not (0 <= slot < span) or not (
            slot + 5 > front_min and slot < span - back_min
        ):
            # 不暴露"5 分钟对齐"等调度机制细节（信息分层）
            return jsonify({"error": "所选时间片不在可选范围内，请重新选择"}), 400
        # 弹性切换冷却：60s 窗口内自由次数内完全放行（浏览式"全点一遍再定"正常）；
        # 超出后冷却随超限次数递增（30s→60s→120s→…封顶 300s），持续高频才被压制。
        # 按被选账号计价（多管理员共享全局生效；新号豁免）。
        # 时长可配（YIBAN_TIME_PREF_COOLDOWN_SEC 基础值，默认 30；0=关闭）
        base_cd = m.load_env_int(m.ENV_FILE, "YIBAN_TIME_PREF_COOLDOWN_SEC", m.TIME_PREF_COOLDOWN_SEC)
        if base_cd > 0:
            now_ts = m.clock.now()
            since = (now_ts - timedelta(seconds=m.TIME_PREF_COOLDOWN_WINDOW)
                     ).strftime("%Y-%m-%d %H:%M:%S")
            count = m.db.time_pref_set_count_since(phone, since)
            if count >= m.TIME_PREF_COOLDOWN_FREE:
                # 弹性冷却 = 基础 × 2^(超限次数)，封顶
                cooldown = min(base_cd * (2 ** (count - m.TIME_PREF_COOLDOWN_FREE + 1)),
                               m.TIME_PREF_COOLDOWN_MAX)
                last_ts = m.db.last_time_pref_set_at(phone)
                if last_ts:
                    try:
                        last_dt = datetime.strptime(str(last_ts), "%Y-%m-%d %H:%M:%S")
                        elapsed = (now_ts - last_dt).total_seconds()
                        # 负间隔（时钟回拨）视为已过冷却，不误伤
                        if 0 <= elapsed < cooldown:
                            # 不暴露冷却时长（信息分层）
                            return jsonify({"error": "切换过于频繁，请稍后再试"}), 429
                    except ValueError:
                        # ts 格式异常（写坏）：保守按冷却生效拦截（防 fail-open 绕过）
                        return jsonify({"error": "切换过于频繁，请稍后再试"}), 429
        # 满员提示（可继续选+提示会顺延）：
        # 该片已选人数 ≥ 块容量时仍允许保存（先到先得+溢出顺延语义），但明确告知；
        # 提示不暴露真实人数/容量（防调研，与用户端 pct 口径一致）
        cap = m.load_env_int(m.ENV_FILE, "YIBAN_BLOCK_CAP", 15)
        stats = {s["slot_min"]: s["count"] for s in m.db.time_pref_stats()}
        count = stats.get(slot, 0)
        cur = m.db.get_time_pref(phone)
        if cur and cur.get("slot_min") == slot:
            count = max(0, count - 1)  # 排除自己已占的位（换片/保留不误报）
        # cap=0（不限容量）时不应提示“已选满”
        full_notice = "，该时段已选满，将就近安排到附近时段" if (cap > 0 and count >= cap) else ""
        # updated_at 带微秒：同秒保存的"先到先得"可区分先后，
        # 不再退化为按 phone 顺序的不可预期平局（字典序定宽，旧秒级数据兼容为更早）
        m.db.set_time_pref(phone, slot, m.clock.now().strftime("%Y-%m-%d %H:%M:%S.%f"))
        m.db.audit(session.get("username", "?"), "time_pref_set", m.db.hash_phone(phone), m._slot_to_label(slot))
        # 生效分界（卡点缓冲）：
        # 优先用当日调度快照标记（signin 构建调度后写入 sched-snapshot-YYYY-MM-DD.json，
        # 精确等于 cron 实际读取自选表的时刻）——改选在快照后必为"明日生效"，提示与实际 100% 一致；
        # 标记不存在（当日 cron 未运行/自选未激活）回退"窗口起点 + 1 分钟"兜底
        now = m.clock.now()
        boundary = None
        try:
            snap_path = os.path.join(m.STATE_DIR, f"sched-snapshot-{now.strftime('%Y-%m-%d')}.json")
            with open(snap_path, encoding="utf-8") as f:
                snap = json.load(f)
            boundary = datetime.strptime(
                f"{now.strftime('%Y-%m-%d')} {snap['snapshot_at']}", "%Y-%m-%d %H:%M:%S"
            )
            # 快照标记晚于当前时刻（时钟偏移/写坏）→ 视为无效回退兜底，
            # 避免"提示今日生效但实际不可能"（改选时 cron 早已建表）
            if boundary > now:
                boundary = None
        except (OSError, ValueError, KeyError, TypeError):
            boundary = None
        if boundary is None:
            try:
                boundary = now.replace(hour=sw[0][0], minute=sw[0][1], second=0, microsecond=0)
            except ValueError:
                boundary = now
            boundary += timedelta(minutes=1)
        when = "今日生效" if now < boundary else "明日生效"
        return jsonify({"ok": True, "msg": f"已保存自选 {m._slot_to_label(slot)}，{when}{full_notice}"})


def api_time_prefs_stats():
    """每片已选人数（拥挤度，管理员；用户端由 my-time-pref 附带，不单独暴露）。"""
    m = _appmod()
    sw = m._sign_window()
    stats = {s["slot_min"]: s["count"] for s in m.db.time_pref_stats()}
    cap = m.load_env_int(m.ENV_FILE, "YIBAN_BLOCK_CAP", 15)
    return jsonify({
        "ok": True,
        "slots": [{**s, "count": stats.get(s["slot_min"], 0), "cap": cap}
                  for s in _pref_slots(sw)],
    })


def api_my_account_add():
    """提交自己的易班账号：每个用户仅限 1 套，写入 accounts 表状态 pending（待审核）。

    操作级锁：单账号限制与手机号唯一检查 + 写入原子（防并发双提交互相覆盖）。
    """
    m = _appmod()
    data = m._json_body()
    err, clean = m.validate_account(data, require_password=True)
    if err:
        return jsonify({"error": err}), 400
    # 预筛：资格校验全部前置到网络验证之前，杜绝「先向易班发起真实登录、再发现
    # 根本没资格」的凭据试探滥用面。权威校验仍保留在下方写入临界区
    # （预筛通过≠最终名额，双检以锁内为准）。
    email_pre = str(session.get("username", "")).lower()
    with m._file_lock:
        accounts_pre = m.load_accounts()
        max_accounts_pre = m.load_env_int(m.ENV_FILE, "YIBAN_MAX_ACCOUNTS", m.DEFAULT_MAX_ACCOUNTS)
        # 容量兜底：账号配额 = 活跃账号数（含裸账号）；
        # 提交者已持有未删除账号时不新增（随后 400 拦截），不计增量，保持原错误优先级
        holds_live = any(a.get("owner") == email_pre and not a["deleted"] for a in accounts_pre)
        if m._accounts_at_capacity(0 if holds_live else 1):
            m._notify_capacity_once("accounts", max_accounts_pre, "账号数量")
            # 不向普通用户暴露容量数字（信息分层）
            return jsonify({"error": "账号数量已达上限，请联系管理员"}), 403
        if holds_live:
            return jsonify({"error": "每个用户只能提交一个账号，可编辑或删除后重新提交"}), 400
        if m.find_account_index(accounts_pre, clean["phone"]) is not None:
            err = m._duplicate_phone_error(accounts_pre, clean["phone"], email_pre)
            if err:
                return jsonify({"error": err}), 400
            return jsonify({"error": f"手机号 {clean['phone']} 已被使用"}), 400
    # 用户提交账号即时验证（管理员开启 YIBAN_ACCOUNT_VERIFY 后生效，验证失败当场打回）
    # 放锁外：verify 为网络操作，不阻塞其他请求；验证尝试受每用户配额限制，
    # 认证失败另有按手机号的冷却（密码错误反复重交会把易班账号打锁定，
    # 冷却同时给用户明确提示）
    verify_async_job = False
    if m._account_verify_enabled():
        if m._verify_fail_cooldown_remaining(verify_fails(), clean["phone"], time.time()) > 0:
            return jsonify({"error": m.VERIFY_FAIL_COOLDOWN_MSG}), 429
        if m.verify_async_enabled():
            # 异步：请求线程只**先扣配额**，外呼交给后台任务。
            # 待办队列满 → 503（账号尚未落库，拒绝无副作用）。
            if m._verify_queue_full():
                return jsonify({"error": m.VERIFY_BUSY_MSG}), 503, {"Retry-After": "2"}
            if not m._verify_attempt_allowed(verify_limits(), email_pre):
                return jsonify({"error": "账号验证尝试过于频繁，请稍后再试"}), 429
            verify_async_job = True
        else:
            try:
                verify_err = m.run_verify_with_gate(clean, email_pre, verify_limits())
            except m.VerifyGateBusy:
                return jsonify({"error": m.VERIFY_BUSY_MSG}), 503, {"Retry-After": "2"}
            except m.VerifyQuotaExceeded:
                return jsonify({"error": "账号验证尝试过于频繁，请稍后再试"}), 429
            if verify_err:
                fail_kind = m._record_verify_failure(verify_fails(), clean["phone"], verify_err, time.time())
                # 验证失败同样留痕审计：否则无法还原
                # 「某账号被反复试错锁定」事件的提交者与次数；detail 只记类别不记原始消息
                m.db.audit(
                    email_pre,
                    "my_account_add_verify_fail",
                    m._mask_phone(clean["phone"]),
                    f"验证未通过（{fail_kind}）",
                )
                return jsonify({"error": verify_err}), 400
    with m._file_lock:
        accounts = m.load_accounts()
        # 容量兜底：账号配额（活跃账号数含裸账号；用户提交同样受限）；
        # 提交者已持有未删除账号时不新增，不计增量（保持原错误优先级）
        max_accounts = m.load_env_int(m.ENV_FILE, "YIBAN_MAX_ACCOUNTS", m.DEFAULT_MAX_ACCOUNTS)
        email = session.get("username", "").lower()
        # 单账号限制：已有未删除提交（含待审核/已生效）则拒绝；待删除（管理员已删）不占名额
        has_live = any(a.get("owner") == email and not a["deleted"] for a in accounts)
        if m._accounts_at_capacity(0 if has_live else 1):
            m._notify_capacity_once("accounts", max_accounts, "账号数量")
            # 不向普通用户暴露容量数字（信息分层）
            return jsonify({"error": "账号数量已达上限，请联系管理员"}), 403
        if has_live:
            return jsonify({"error": "每个用户只能提交一个账号，可编辑或删除后重新提交"}), 400
        if m.find_account_index(accounts, clean["phone"]) is not None:
            err = m._duplicate_phone_error(accounts, clean["phone"], email)
            if err:
                return jsonify({"error": err}), 400
            return jsonify({"error": f"手机号 {clean['phone']} 已被使用"}), 400
        # 归属一律取本人邮箱（与「我的账号」视图同口径，一人一号）；无人认领的裸账号
        # （owner='admin'）只由账号管理页的「不填邮箱」分支创建。若按 'admin' 归属，
        # 注册管理员自提交的账号会立刻从个人视图消失。
        clean["owner"] = session.get("username", "").lower()
        # 一律待审核（不按角色分叉出"管理员提交即生效"）：本端点的语义是
        # "提交一份要参与全站签动的凭据"，生效与否交给审核结论，而不是交给提交者
        # 的角色——被窃的管理员会话就此少一条"自己交、立即跑"的免检通道。
        # 管理员给自己邮箱代管的入口没被堵死：/api/accounts/<idx>/review 的
        # approve 对任意 pending 行开放（不看提交者是谁），管理员自己点一次通过
        # 即生效，并且这一步会单独留 account_review 审计（隐式置 ACTIVE 不留任何痕迹）。
        clean["status"] = m.ACCOUNT_STATUS_PENDING
        try:
            new_id = m.db.add_account(clean)
        except sqlite3.IntegrityError:
            return jsonify({"error": f"手机号 {clean['phone']} 已被使用"}), 400  # 并发提交兜底
        m.db.audit(
            clean["owner"],
            "my_account_add",
            m._mask_phone(clean["phone"]),
            f"用户提交 状态 {clean['status']}",
        )
        m.logger.info("用户 %s 提交账号 %s（待审核）", m._mask_email(clean["owner"]), m._mask_phone(clean["phone"]))
        # 申请入库后管理员侧零通知，只能靠主动打开后台发现，于是出现"用户说交了
        # 申请、管理员说没收到"。补一条非紧急告警：邮件必达，手机推送受「仅推送
        # 重要告警」与当日非紧急额度约束（不挤占紧急账）。整段兜异常——账号已入库，
        # 通知失败不得把提交结果带崩成 500。
        try:
            m.send_notification(
                "新账号申请待审核",
                m.mail_layout.Mail(
                    summary="有新提交的易班账号待审核。",
                    fields=[("提交者", m._nl_safe(str(clean["owner"]))),
                            ("账号", m._mask_phone(clean["phone"])),
                            ("处理入口", "管理台「待审核」列表")],
                    level="info",
                ),
            )
        except Exception as e:
            m.logger.warning("新申请待审核通知发送失败（不影响提交结果）: %s", e)
        resp = {"ok": True, "msg": "已提交，等待管理员审核后参与签到"}
        if verify_async_job:
            # 账号已落库，随后由后台任务校验；响应**增补** job_id/status（不改既有字段）
            try:
                job_id, _ = m._start_verify_job(
                    clean, clean["owner"], new_id, verify_fails(), verify_limits())
                resp["job_id"] = job_id
                resp["status"] = "verifying"
            except m.VerifyGateBusy:
                m.logger.warning("校验任务队列已满，账号 %s 未建校验任务",
                               m._mask_phone(clean["phone"]))
        return jsonify(resp)


def api_verify_job_get(job_id):
    """查询校验任务状态与结果（仅本人；内置主管理员可读）。"""
    m = _appmod()
    job = m.db.get_verify_job(job_id)
    if not job:
        return jsonify({"error": "任务不存在"}), 404
    if not _verify_job_visible(job, session.get("username")):
        return jsonify({"error": "无权限"}), 403
    return jsonify({"ok": True, "job": _job_payload(job)})


def api_verify_job_cancel(job_id):
    """取消校验任务（仅 pending 可取消；仅本人，内置主管理员可代管）。

    先收口超龄任务：卡在 running 的任务若因进程重启而无人在跑，会因
    "只允许取消 pending" 而永远无法撤销，这里先把它判定为终态。
    """
    m = _appmod()
    m._reclaim_stale_verify_jobs()
    job = m.db.get_verify_job(job_id)
    if not job:
        return jsonify({"error": "任务不存在"}), 404
    if not _verify_job_visible(job, session.get("username")):
        return jsonify({"error": "无权限"}), 403
    if job["status"] in m.VERIFY_JOB_TERMINAL:
        return jsonify({"error": "任务已结束，无法取消"}), 409
    if not m.db.cancel_verify_job(job_id):
        # 与查询之间被后台线程抢走（进入 running）→ 同样视为已无法取消
        return jsonify({"error": "任务已开始执行，无法取消"}), 409
    return jsonify({"ok": True, "job": _job_payload(m.db.get_verify_job(job_id))})


def api_my_calendar():
    """我的账号月历：返回指定月份（YYYY-MM）每天每账号的签到状态（✅/❌/空字符串）。"""
    m = _appmod()
    month = str(request.args.get("month", "")).strip()
    try:
        year, mon = map(int, month.split("-"))
        if not (2000 <= year <= 2100 and 1 <= mon <= 12):
            raise ValueError
    except Exception:
        return jsonify({"error": "月份格式不正确，应为 YYYY-MM"}), 400
    accounts = m.load_accounts()
    indices = _my_account_indices_of(accounts)  # 单快照：防两次读取间列表漂移（同 api_my_accounts）
    # 日历仅回显已生效账号的历史（pending/rejected/软删除不回显）
    phones = _my_active_phones(accounts, indices)
    days_in_month = calendar.monthrange(year, mon)[1]
    result = {f"{year:04d}-{mon:02d}-{d:02d}": {} for d in range(1, days_in_month + 1)}
    # 聚合读取：单次目录遍历取本月全部日文件（替代每天一次 exists+open 共 30 次 IO）
    prefix = f"sign-daily-{year:04d}-{mon:02d}-"
    try:
        for entry in os.scandir(m.STATE_DIR):
            if entry.name.startswith(prefix):
                date = entry.name[len("sign-daily-") : -len(".json")]
                try:
                    with open(entry.path, encoding="utf-8") as f:
                        daily = json.load(f)
                except Exception:
                    daily = {}
                # setdefault：异常文件名（非 YYYY-MM-DD）不落入本月键时自动补空，防 KeyError 500
                result.setdefault(date, {}).update({p: daily.get(p, "") for p in phones})
    except OSError:
        pass  # STATE_DIR 不存在等：按无记录返回
    return jsonify({
        "ok": True,
        "month": month,
        "days": result,
        "sunday_sign": m.load_env_int(m.ENV_FILE, "YIBAN_SUNDAY_SIGN", 0),  # 前端据此决定周日是否置灰/可查
        "saturday_sign": m.load_env_int(m.ENV_FILE, "YIBAN_SATURDAY_SIGN", 0),  # 默认关闭；前端据此决定周六是否置灰/可查
    })


def api_my_logs():
    """我的账号指定日期（YYYY-MM-DD）的日志（按手机号过滤，最多 50 条）。

    读按天文件（sign-YYYY-MM-DD.log）：历史日期同样可查（只读当前 sign.log 时，
    轮转后历史日期恒为空）。
    """
    m = _appmod()
    date = str(request.args.get("date", "")).strip()
    if date and not m._is_valid_date_str(date):
        return jsonify({"error": "日期格式不正确，应为 YYYY-MM-DD"}), 400
    # 默认：今天有日志则显示今天，否则找最近有日志的一天（_most_recent_log_date 内部先查今天）
    if not date:
        date = m._most_recent_log_date()
    accounts = m.load_accounts()
    indices = _my_account_indices_of(accounts)  # 单快照：防两次读取间列表漂移（同 api_my_accounts）
    # 日志仅回显已生效账号的历史（pending/rejected/软删除不回显）
    phones = _my_active_phones(accounts, indices)
    out = []
    for line in m._log_lines_for(date):
        if any(f"[{p}]" in line for p in phones):
            out.append(line.strip())
    # 脱敏后再截断：与 /api/logs 同口径（日志行内 [手机号] 不落完整号）
    return jsonify({"ok": True, "date": date, "logs": [m._mask_log_phones(ln) for ln in out[-50:]]})


def api_my_account_update(idx):
    """编辑自己提交的账号：密码/识别码留空=保留；改绑手机号一律回待审核重审
    仅密码/识别码变更不影响已生效状态。"""
    m = _appmod()
    with m._file_lock:
        accounts = m.load_accounts()
        indices = _my_account_indices_of(accounts)
        if not 0 <= idx < len(indices):
            return jsonify({"error": "账号不存在"}), 404
        real_idx = indices[idx]
        old = accounts[real_idx]
        data = m._json_body()
        # 防错位（与 /api/accounts/* 同口径）：本人视图的 idx 在渲染后可能因
        # 管理员删除/清除而漂移，不校验会静默改到本人另一行。
        # 比对基准优先取编辑表单的乐观锁快照 phone——本端点允许"填写完整新号码"
        # 改绑（改绑后回待审核），直接拿 data["phone"] 比会把这条合法路径 409 掉。
        _snap = data.get("_snapshot")
        if isinstance(_snap, str):
            try:
                _snap = json.loads(_snap)
            except json.JSONDecodeError:
                _snap = None
        guard_src = ({"phone": _snap["phone"]}
                     if isinstance(_snap, dict) and _snap.get("phone") else data)
        if m._stale_idx_guard(old, guard_src):
            return jsonify({"error": "账号列表已变化，请刷新页面后重试"}), 409
        # 软删除账号禁止编辑（防编辑流程绕过软删除；恢复由管理员操作）
        if old.get("deleted"):
            return jsonify({"error": "账号已删除，请先恢复"}), 400
        err, clean = m.validate_account(data, require_password=False)
        if err:
            return jsonify({"error": err}), 400
        if (
            clean["phone"] != old.get("phone")
            and m.find_account_index(accounts, clean["phone"]) is not None
        ):
            return jsonify({"error": f"手机号 {clean['phone']} 已被使用"}), 400
        if not clean["password"]:
            clean["password"] = old.get("password", "")
        # 设备识别码：__clear__ = 显式清空该字段；留空 = 保持不变
        if clean["phone_code"] == m.CLEAR_SENTINEL:
            clean.pop("phone_code", None)
        elif not clean["phone_code"]:
            clean["phone_code"] = old.get("phone_code", "")
        clean["owner"] = old.get("owner", "")
        # 改绑手机号一律回待审核重审——否则 ACTIVE 号可被改绑成任意新号免审生效，
        # 历史审核结论不再可信。无论原状态（含 ACTIVE）；REJECTED 本就回 pending。
        # 仅密码/识别码变更（phone 不变）→ 状态不变。
        rebind = clean["phone"] != old.get("phone")
        clean["status"] = (
            m.ACCOUNT_STATUS_PENDING
            if rebind or old.get("status") == m.ACCOUNT_STATUS_REJECTED
            else old.get("status", m.ACCOUNT_STATUS_PENDING)
        )
        if clean["status"] == m.ACCOUNT_STATUS_PENDING:
            # 显式置空（clean.pop 对不存在的键是空操作，会残留旧拒绝理由），随 update 落库
            clean["reject_reason"] = ""
        try:
            m.db.update_account(old["id"], clean)
        except sqlite3.IntegrityError:
            return jsonify({"error": f"手机号 {clean['phone']} 已被使用"}), 400  # 并发改号兜底
        # 手机号变更 → 旧号自选时间片失效，必须在 update_account 成功后再清
        if rebind:
            m.db.clear_time_pref(old.get("phone", ""))
        m.db.audit(
            clean["owner"],
            "my_account_update",
            m._mask_phone(clean["phone"]),
            "用户编辑 改绑回审" if rebind else "用户编辑",
        )
        # 用户改密码/改绑手机号（凭据变更）才清除熔断暂停；
        # 仅改备注/状态等不动熔断计数（与管理员编辑路由同一口径）
        m.clear_fuse_on_cred_change(old.get("phone", ""), old.get("password", ""), clean)
        m.logger.info("用户 %s 编辑账号 %s", m._mask_email(clean["owner"]), m._mask_phone(clean["phone"]))
        if rebind or old.get("status") == m.ACCOUNT_STATUS_REJECTED:
            return jsonify({"ok": True, "msg": "已重新提交，等待管理员审核"})
        return jsonify({"ok": True, "msg": "已保存"})


def api_my_account_delete(idx):
    """用户删除自己的账号：软删除进入 7 天宽限期，可在本页撤销恢复，超期自动清除。

    软删 + 7 天宽限 + 可撤销：与「注销登录账号 7 天可恢复」的产品语义一致
    （物理清除则无任何反悔余地）。deleted_by 留痕操作者：仅本人自删行可自行撤销，
    管理员删除的账号仍由管理员恢复/清除，防清退账号被用户一键复活。
    """
    m = _appmod()
    with m._file_lock:
        accounts = m.load_accounts()
        indices = _my_account_indices_of(accounts)
        if not 0 <= idx < len(indices):
            return jsonify({"error": "账号不存在"}), 404
        removed = accounts[indices[idx]]
        if removed.get("deleted"):
            return jsonify({"error": "该账号已在待删除状态，可在本页撤销恢复"}), 400
        # 防错位（同 /api/accounts/*）：删除是不可逆前置动作，视图漂移时宁可让
        # 用户刷新，也不能删到本人另一行
        if m._stale_idx_guard(removed, m._json_body()):
            return jsonify({"error": "账号列表已变化，请刷新页面后重试"}), 409
        m.db.set_account_deleted(
            removed["id"],
            1,
            m.clock.now().strftime("%Y-%m-%d %H:%M:%S"),
            deleted_by=(session.get("username", "") or "").strip().lower(),
        )
        m.db.audit(
            session.get("username", "") or "?",
            "my_account_delete",
            m._mask_phone(removed.get("phone", "")),
            "用户删除（软删除，宽限期内可撤销）",
        )
        m.logger.info(
            "用户 %s 删除账号 %s（软删除）",
            session.get("username", ""),
            m._mask_phone(removed.get("phone", "")),
        )
        # 删号（软删）给本人留痕邮件（绕过 mail_notify 开关）
        m.mailer.send_user(
            session.get("username", ""),
            "【易班签到】您的易班账号已删除（7 天内可撤销）",
            m.mail_layout.Mail(
                summary=f"您的易班账号（{m._mask_phone(removed.get('phone', ''))}）已被删除，"
                        f"进入 {m.DELETED_RETENTION_DAYS} 天宽限期。",
                advice=["宽限期内可在「我的账号」页自行撤销恢复",
                        "如非本人操作，请立即联系管理员"],
            ),
        )
        return jsonify({"ok": True, "msg": "已删除，7 天内可在本页撤销恢复，超期自动清除"})


def api_my_account_restore(idx):
    """用户撤销删除自己的账号：仅限本人自删（deleted_by=本人）且名下无其他生效账号。

    与管理员 /api/accounts/<idx>/restore 同构，但收窄授权：
    - 普通用户视图（_my_account_indices_of）含本人待删除行，idx 口径与展示一致；
    - deleted_by != 本人（管理员删除/系统连带）一律 403，防清退账号被复活；
    - 「每人限 1」防呆与管理员侧同源（_owner_has_other_live）。
    """
    m = _appmod()
    with m._file_lock:
        accounts = m.load_accounts()
        indices = _my_account_indices_of(accounts)
        if not 0 <= idx < len(indices):
            return jsonify({"error": "账号不存在"}), 404
        acc = accounts[indices[idx]]
        if not acc.get("deleted"):
            return jsonify({"error": "该账号不在待删除状态"}), 400
        if acc.get("deleted_by", "") != (session.get("username", "") or "").strip().lower():
            return jsonify({"error": "该账号由管理员删除，如需恢复请联系管理员"}), 403
        if m._owner_has_other_live(accounts, acc):
            return jsonify(
                {"error": "你已有生效账号，无法恢复（每人限 1 个）"}
            ), 400
        if m._stale_idx_guard(acc, m._json_body()):
            return jsonify({"error": "账号列表已变化，请刷新页面后重试"}), 409
        m.db.set_account_deleted(acc["id"], 0)
        m.db.audit(
            session.get("username") or "?",
            "my_account_restore",
            m._mask_phone(acc.get("phone", "")),
            "用户撤销删除",
        )
        accounts = m.load_accounts()
        m.logger.info(
            "用户 %s 撤销删除账号 %s",
            session.get("username", ""),
            m._mask_phone(acc.get("phone", "")),
        )
        return jsonify(
            {
                "ok": True,
                "msg": f"已恢复「{acc.get('name', '')}」",
                "accounts": _my_account_view(
                    accounts, _my_account_indices_of(accounts)
                ),
            }
        )


def api_my_account_pause(idx):
    """用户自暂停/恢复签到（调度 v2）：暂停后主程序自动跳过，状态显示红底"已取消"。"""
    m = _appmod()
    with m._file_lock:
        accounts = m.load_accounts()
        indices = _my_account_indices_of(accounts)
        if not 0 <= idx < len(indices):
            return jsonify({"error": "账号不存在"}), 404
        acc = accounts[indices[idx]]
        if acc.get("deleted"):
            return jsonify({"error": "账号已删除，请先恢复"}), 400
        data = m._json_body()
        # 防错位（同 /api/accounts/*）：视图漂移时不得改到本人另一行
        if m._stale_idx_guard(acc, data):
            return jsonify({"error": "账号列表已变化，请刷新页面后重试"}), 409
        paused = 1 if str(data.get("paused", "")).strip().lower() in ("1", "true", "on", "yes") else 0
        # 用户确认：管理员不能暂停自己账号（owner=admin 为系统/管理员账号；暂停是
        # 普通用户管理自己账号的能力，管理端界面本无此入口，防 /user 页绕过）。
        # 恢复放行（幂等无害；该状态本不可达，仅保持接口一致性）
        if paused and acc.get("owner", "admin") == "admin":
            return jsonify({"error": "管理员账号不支持自暂停"}), 403
        # 暂停冷却（用户裁决）：仅"暂停"计冷却（固定间隔，默认 30s），"恢复"不受限
        # ——恢复是紧迫正向操作，绝不该被挡。冷却防连点/防审计噪音，非安全边界。
        # 按用户计价（多管理员共享账号各自独立，可接受）。时长可配
        # （YIBAN_PAUSE_COOLDOWN_SEC，默认 30；0=关闭）。不暴露时长（信息分层）。
        if paused:
            base_cd = m.load_env_int(m.ENV_FILE, "YIBAN_PAUSE_COOLDOWN_SEC", m.PAUSE_COOLDOWN_SEC)
            if base_cd > 0:
                # 弹性冷却：60s 窗口内前 PAUSE_COOLDOWN_FREE 次完全自由，
                # 超出后冷却 = 基础 × 2^(超限次数)，封顶 PAUSE_COOLDOWN_MAX。
                now_ts = m.clock.now()
                since = (now_ts - timedelta(seconds=m.PAUSE_COOLDOWN_WINDOW)
                         ).strftime("%Y-%m-%d %H:%M:%S")
                pause_count = m.db.pause_count_since(
                    session.get("username", "") or "", since
                )
                if pause_count >= m.PAUSE_COOLDOWN_FREE:
                    cooldown = min(
                        base_cd * (2 ** (pause_count - m.PAUSE_COOLDOWN_FREE + 1)),
                        m.PAUSE_COOLDOWN_MAX,
                    )
                    last_ts = m.db.last_pause_at(session.get("username", "") or "")
                    if last_ts:
                        try:
                            last_dt = datetime.strptime(str(last_ts), "%Y-%m-%d %H:%M:%S")
                            # 负间隔（时钟回拨）视为已过冷却，不误伤
                            if 0 <= (now_ts - last_dt).total_seconds() < cooldown:
                                return jsonify({"error": "操作过于频繁，请稍后再试"}), 429
                        except ValueError:
                            # ts 格式异常（写坏）：保守按冷却生效拦截
                            return jsonify({"error": "操作过于频繁，请稍后再试"}), 429
        m.db.set_user_paused(acc["id"], paused)
        m.db.audit(
            session.get("username", "") or "?",
            "my_account_pause" if paused else "my_account_resume",
            m._mask_phone(acc.get("phone", "")),
            "用户自暂停" if paused else "用户恢复",
        )
        m.logger.info(
            "用户 %s %s 账号 %s",
            session.get("username", ""), "暂停" if paused else "恢复",
            m._mask_phone(acc.get("phone", "")),
        )
        return jsonify({
            "ok": True,
            "msg": "已暂停签到，主程序将自动跳过" if paused else "已恢复签到",
            "paused": bool(paused),
        })


def register(app):
    """在本域注册十三条个人自助路由；endpoint 取函数名（url_for 依赖）。"""
    app.add_url_rule("/api/my-accounts", view_func=api_my_accounts)
    app.add_url_rule("/api/my-time-pref", view_func=api_my_time_pref)
    app.add_url_rule("/api/my-time-pref", view_func=api_my_time_pref_save, methods=["PUT"])
    app.add_url_rule("/api/time-prefs/stats", view_func=api_time_prefs_stats)
    app.add_url_rule("/api/my-accounts", view_func=api_my_account_add, methods=["POST"])
    app.add_url_rule("/api/verify-jobs/<int:job_id>", view_func=api_verify_job_get)
    app.add_url_rule("/api/verify-jobs/<int:job_id>", view_func=api_verify_job_cancel,
                     methods=["DELETE"])
    app.add_url_rule("/api/my-calendar", view_func=api_my_calendar)
    app.add_url_rule("/api/my-logs", view_func=api_my_logs)
    app.add_url_rule("/api/my-accounts/<int:idx>", view_func=api_my_account_update,
                     methods=["PUT"])
    app.add_url_rule("/api/my-accounts/<int:idx>", view_func=api_my_account_delete,
                     methods=["DELETE"])
    app.add_url_rule("/api/my-accounts/<int:idx>/restore", view_func=api_my_account_restore,
                     methods=["POST"])
    app.add_url_rule("/api/my-accounts/<int:idx>/pause", view_func=api_my_account_pause,
                     methods=["PUT"])
