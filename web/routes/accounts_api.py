# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""账号管理域路由：管理员侧的账号列表/详情/增删改、批量操作、审核与排序。

**功能**
`GET /api/accounts` 列表（脱敏 + 今日签到状态 + 自选时间片 + 上一业务日执行体归属；
可选 `limit`/`offset` 分页，不传＝整表，向后兼容；可选 `count_only=1` 只回计数、
不下发列表，供 10 秒轮询的导航徽标取标量）；
`GET /api/accounts/<idx>/detail` 单号完整信息（列表外唯一回传明文口令与完整手机号的面，
带会话级限速与聚合读审计）；`POST /api/accounts` 添加（不填邮箱=管理员自有号直接生效，
填邮箱=归属该用户并回待审核，邮箱未注册则连带创建网站用户）；`PUT /api/accounts/<idx>`
编辑（改绑手机号一律回待审核）；`POST /api/accounts/batch` 批量
approve/reject/restore/delete/purge；`DELETE /api/accounts/<idx>` 软删；
`POST /api/accounts/<idx>/restore|purge|review|move` 恢复/彻底删除/审核/排序；
`POST /api/accounts/<idx>/device-refresh` 设备字段按需补全（登录一次读回服务端已登记
设备并落库；读不到不改库）；`PUT /api/accounts/<idx>/profile` 二段资料与暴露开关的
管理员维护面（本人自助走 my 域端点，工单 qmz4）。

**归属**
`web.app.create_app` 的"账号管理"面（管理员专用，路径不在普通用户白名单）。工厂骨架、
跨域中间件（前置限速、登录守卫、CSRF、同源校验、安全响应头）与库访问层仍留在
`web/app.py`，本模块只提供十二条视图。

**复用**
`register(app)` 供 `web.routes.register_all` 装配。`web.routes.high_risk_gate()` 取回
高危动作统一门禁（先二次鉴权、通过后才占高危额度），`admin_delete_limited()` 取回
"软删/不可逆清除"共用的同管理员窗口限速；`verify_fails()` / `verify_limits()` 是账号
验证冷却与配额的唯一取用点（与用户自助提交路径共用同一份账），`detail_limits()` 是详情
读取限速表；`read_audit_trace()` / `read_audit_denied_trace()` 是只读面聚合留痕。

**通信**
A 类转发名（`db` / `clock` / `mailer` / `mail_layout` / `load_accounts` / `mask_account` /
`validate_account` 等）经真源模块直取（批 3-a 拆墙）；批 3-d 后 `_json_body`、
`send_notification` 与容量族同样直取服务层真源，测试打桩点一律落在**真源**上。
仍经 `web.routes.appmod()` 按属性取的只有组合根自身的状态名（`logger` / `ENV_FILE` /
`DB_FILE`）。对外：JSON 请求体经 `_json_body()`，落库走 `db`，
告警经 `send_notification`，账号容量闸门经 `_accounts_at_capacity()`；
被 `web.app` 的 `register_all(app)` 一次接入。
"""
import json
import os
import sqlite3
import time

from flask import jsonify, request, session
from werkzeug.security import generate_password_hash

# A 类转发名的真源（批 3-a：路由直连，不再经 web.app 转发墙）。
from web import render as _render
from web import security as _security
from web.routes import admin_delete_limited as _admin_delete_limited
from web.routes import appmod as _appmod
from web.routes import detail_limits as _detail_limits
from web.routes import high_risk_gate as _high_risk_gate
from web.routes import json_conditional as _json_conditional
from web.routes import page_args as _page_args
from web.routes import page_meta as _page_meta
from web.routes import page_window as _page_window
from web.routes import read_audit_denied_trace as _read_audit_denied_trace
from web.routes import read_audit_trace as _read_audit_trace
from web.routes import verify_fails as _verify_fails
from web.routes import verify_limits as _verify_limits
from web.services import accounts_data as _accounts_data
from web.services import capacity as _capacity
from web.services import env_io as _env_io_svc
from web.services import executor_env as _executor_env
from web.services import http_request as _http
from web.services import limits as _limits
from web.services import locks as _locks
from web.services import logs as _logs_svc
from web.services import notify_mail as _notify_mail
from web.services import signstatus as _signstatus
from web.services import verify_queue as _verify_queue
from web.services.accounts_data import plan_state_message as _plan_state_message
from web.services.accounts_data import today_plan_map as _today_plan_map
from web.services.device_refresh import refresh_account_device as _refresh_account_device
from web.services.verify_queue import start_edit_probe as _start_edit_probe
from yiban.adapters.notifier import mail as mailer
from yiban.adapters.notifier.mail import layout as mail_layout
from yiban.adapters.persistence import db
from yiban.adapters.persistence.accounts import signs_in as _signs_in
from yiban.core import clock
from yiban.core import masking as _masking
from yiban.core import status as yiban_status

_ACCOUNT_GROUPS = ("pending", "active", "deleted")
_SKIPPED_STATES = {
    "no_task", "skipped_window", "skipped_norange", "paused", "user_cancelled",
}


def _account_group(acc):
    """账号所属展示分组（与前端 model.js 的 groupAll 同一口径）。"""
    if acc.get("deleted"):
        return "deleted"
    st = acc.get("status")
    if st in (_accounts_data.ACCOUNT_STATUS_PENDING,
              _accounts_data.ACCOUNT_STATUS_REJECTED):
        return "pending"
    if st == _accounts_data.ACCOUNT_STATUS_ACTIVE:
        return "active"
    return ""


def _account_group_totals(accounts):
    out = {g: 0 for g in _ACCOUNT_GROUPS}
    for a in accounts:
        g = _account_group(a)
        if g:
            out[g] += 1
    return out


def _digits_of(text):
    return "".join(ch for ch in str(text or "") if ch.isdigit())


def _masked_phone_hit(masked, kw):
    """按遮罩串匹配号码（与前端 phoneMaskHit 同口径，服务端只做一次判定）。"""
    digits = _digits_of(kw)
    if len(digits) == 13 and digits[:2] == "86":
        digits = digits[2:]
    if len(digits) < 7:
        return False
    pattern = str(masked or "")
    if "*" not in pattern:
        return digits == pattern
    parts = [part for part in pattern.split("*") if part]
    if not parts:
        return False
    if not digits.startswith(parts[0]):
        return False
    if len(parts) > 1 and not digits.endswith(parts[-1]):
        return False
    tail = parts[-1] if len(parts) > 1 else ""
    if len(digits) < len(parts[0]) + len(tail):
        return False
    middle = digits[len(parts[0]):len(digits) - len(tail)]
    pos = 0
    for part in parts[1:-1]:
        at = middle.find(part, pos)
        if at < 0:
            return False
        pos = at + len(part)
    return True


def _account_search_hit(acc, kw):
    q = str(kw or "").strip().lower()
    if not q:
        return True
    phone = _masking.mask_phone(str(acc.get("phone", "")))
    owner = str(acc.get("owner", ""))
    # 列表「归属」列显示的是遮罩后的完整邮箱；检索必须认这个可见值。
    hay = (acc.get("name"), phone, _accounts_data._owner_display_of(owner),
           _accounts_data._mask_email(owner), owner)
    if any(q in str(v or "").lower() for v in hay):
        return True
    return _masked_phone_hit(phone, q)


def api_accounts():
    """账号列表：脱敏、今日状态；可选 group/q 与 limit/offset。

    不传新参数时响应与分页前逐字相同。传 group 时只返回该组，total 是该组
    再经 q 过滤后的行数；group_totals 给三组全局总数，stats 给全部正常账号的
    今日 KPI。三者都不随当前页收窄，故前端不必先拉整表再分组。
    """
    m = _appmod()
    if str(request.args.get("count_only", "")).strip() == "1":
        # 计数只需明文列：用未解密快照，避免为 5k 账号做一遍 AES-GCM。
        accounts = _accounts_data.load_accounts_raw()
        pending = rejected = 0
        for a in accounts:
            if a.get("deleted"):
                continue
            _st = a.get("status")
            if _st == _accounts_data.ACCOUNT_STATUS_PENDING:
                pending += 1
            elif _st == _accounts_data.ACCOUNT_STATUS_REJECTED:
                rejected += 1
        return _json_conditional({"ok": True, "total": len(accounts),
                                  "pending": pending, "rejected": rejected})
    group = str(request.args.get("group", "")).strip()
    if group and group not in _ACCOUNT_GROUPS:
        return jsonify({"error": "未知账号分组"}), 400
    q = str(request.args.get("q", "")).strip()[:64]
    limit, offset, paged = _page_args()
    # 带 limit/offset 时只解密**当前页**：5k 账号全量 AES-GCM 实测 >20s，会撞前端 20s 超时。
    # 只传 group 是「整组返回」，没有页可裁，仍解密整组；调用方要控成本就带分页。
    # 不传新参数的旧路径仍走 load_accounts()，响应逐字不变。
    accounts = (_accounts_data.load_accounts_raw() if (paged or group)
                else _accounts_data.load_accounts())
    group_totals = _account_group_totals(accounts)
    indexed = [(i, a) for i, a in enumerate(accounts)]
    if group:
        indexed = [(i, a) for i, a in indexed if _account_group(a) == group]
        if group == "pending":
            indexed.sort(key=lambda pair: (
                0 if pair[1].get("status") == _accounts_data.ACCOUNT_STATUS_PENDING else 1,
                -pair[0]))
    if q:
        indexed = [(i, a) for i, a in indexed if _account_search_hit(a, q)]
    total = len(indexed)
    page = _page_window(indexed, limit, offset) if paged else indexed
    if paged or group:
        page_accounts = db.decrypt_account_rows([a for _i, a in page])
    else:
        page_accounts = [a for _i, a in page]
    _page_phones = {a.get("phone", "") for a in page_accounts} if (paged or group) else None
    states = _logs_svc.load_sign_state()
    _run_at = _today_plan_map()
    plan_at = _run_at if isinstance(_run_at, dict) else {}
    # 用户自暂停要在统计口径里生效：统计覆盖全部正常账号，不能只为当前页合成，
    # 否则同一暂停账号在不同 offset 下会分别计入 skipped 与 waiting。
    for acc in accounts:
        if acc.get("user_paused"):
            _prev = states.get(acc.get("phone", ""))
            _prev_status = _prev.get("status", "") if isinstance(_prev, dict) else ""
            if yiban_status.is_concluded_status(_prev_status):
                continue
            states[acc.get("phone", "")] = {
                "status": yiban_status.STATUS_USER_CANCELLED,
                "message": "用户已取消签到",
            }
    prefs = {p: v["slot_min"] for p, v in db.get_time_prefs().items()}
    last_exec = _executor_env._last_executors(db.task_latest_day())
    _win = _signstatus.sign_window_bounds()
    _span_min = _win.end_min - _win.start_min

    def _edge_mark(slot):
        if slot is None:
            return None
        if slot == 0:
            return "first"
        if slot >= _span_min - 5:
            return "last"
        return None

    payload = {
        "ok": True,
        "accounts": [
            {
                **_accounts_data.mask_account(a, abs_i),
                "time_pref": _accounts_data._slot_to_label(prefs.get(a["phone"])),
                "time_pref_edge": _edge_mark(prefs.get(a["phone"])),
                "last_executor": last_exec.get(a["phone"]),
            }
            for (abs_i, _raw), a in zip(page, page_accounts, strict=True)
        ],
        "states": {
            _masking.mask_phone(k): (v.get("status", yiban_status.STATUS_PENDING)
                                     if isinstance(v, dict) else yiban_status.STATUS_PENDING)
            for k, v in states.items()
            if _page_phones is None or k in _page_phones
        },
        "state_msgs": {
            _masking.mask_phone(a.get("phone", "")): _plan_state_message(
                a, states.get(a.get("phone", "")), plan_at)
            for a in page_accounts
        },
        "state_durs": {
            _masking.mask_phone(k): (v.get("dur") if isinstance(v, dict) else None)
            for k, v in states.items()
            if _page_phones is None or k in _page_phones
        },
        "config_file": os.path.basename(m.DB_FILE),
    }
    if group or paged:
        payload["group_totals"] = group_totals
        stats = {"success": 0, "failed": 0, "waiting": 0, "skipped": 0}
        for a in accounts:
            if _account_group(a) != "active":
                continue
            _rec = states.get(a.get("phone", ""))
            st = (_rec.get("status", "pending") if isinstance(_rec, dict) else "pending")
            if st in ("success", "already"):
                stats["success"] += 1
            elif st == "failed":
                stats["failed"] += 1
            elif st in _SKIPPED_STATES:
                stats["skipped"] += 1
            else:
                stats["waiting"] += 1
        payload["stats"] = stats
    if paged:
        payload.update(_page_meta(total, limit, offset, len(payload["accounts"])))
    return _json_conditional(payload)


def api_account_detail(idx):
    """账号完整信息（仅管理员；列表接口已脱敏，编辑/签到等操作按需取完整号）。

    这是列表之外唯一回传明文口令与完整手机号的面，且 idx 从 0 递增即可整库
    枚举——被窃的管理员会话读它零成本、零痕迹，故与日志导出同口径加会话级
    限速 + 聚合读审计（超限 429 不逐条留痕，防把审计表当打字机）。
    """
    with _locks._rate_lock:
        _security._ip_store_trim(_detail_limits(), _limits.DETAIL_WINDOW + _security._IP_STORE_MAX_AGE)
    _cnt, _start, allowed = _security._bump_window_count(
        _detail_limits(), (session.get("username") or "?")[:64], time.time(),
        _limits.DETAIL_WINDOW, limit=_limits.DETAIL_MAX,
    )
    if not allowed:
        # 文案不带阈值数字：这些数值对合法运维没有用处，却正好让攻击者贴着
        # 上限排布枚举节奏（信息分层口径，与其余 429 一致）
        _read_audit_denied_trace()("account_detail_denied")
        return jsonify({"error": "查看账号详情过于频繁，请稍后再试"}), 429
    # 只解密目标行：整表解密在 5k 账号下要为一次详情做一遍全表 AES-GCM（工单 dnox-B）。
    acc = _accounts_data.load_account_at(idx)
    if acc is None:
        return jsonify({"error": "账号不存在"}), 404
    _read_audit_trace()(
        "account_detail_read", _masking.mask_phone(str(acc.get("phone", ""))))
    return jsonify({"ok": True, "account": _accounts_data.mask_account(acc, idx, masked=False)})


def api_account_profile(idx):
    """维护账号二段资料与暴露开关（管理员维护面；本人自助走 /api/my-accounts/<idx>/profile）。

    权限口径跟既有账号编辑门同形：本路径在管理面空间，普通用户由全局登录守卫
    403 并留 forbidden_path 审计；本人改自己的走本人面端点。对齐令牌：管理面
    写口 fail-closed，请求必须携带 `phone`（掩码形态可），防列表漂移后把资料
    写错行（与 delete/restore/review 同闸）。
    资料内容是 PII：落库走存储层同通道加密；审计 detail 只记字段名
    （`profile_audit_detail`），字段值不进审计与日志（qmz4 判据⑥）。
    """
    m = _appmod()
    with _locks._file_lock:
        acc = _accounts_data.load_account_at(idx)  # 只解密目标行（dnox-B 同法）
        if acc is None:
            return jsonify({"error": "账号不存在"}), 404
        if acc.get("deleted"):
            return jsonify({"error": "账号已删除，请先恢复"}), 400
        data = _http._json_body()
        if _accounts_data._stale_idx_guard(acc, data, fail_closed=True):
            return jsonify({"error": "账号列表已变化，请刷新页面后重试"}), 409
        err, profile, expose = _accounts_data.normalize_profile_input(data)
        if err:
            return jsonify({"error": err}), 400
        try:
            content, flags = db.update_account_profile(
                acc["id"], profile, expose,
                audit_spec={
                    "username": session.get("username") or "?",
                    "action": "account_profile_update",
                    "target": _masking.mask_phone(str(acc.get("phone", ""))),
                    "detail": _accounts_data.profile_audit_detail(profile, expose),
                },
            )
        except ValueError as e:
            # 底子损坏（profile 列非合法 JSON）：拒绝覆盖写入，回 500 让维护者先修数据
            m.logger.warning("账号资料维护失败（疑似脏数据）: %s", e)
            return jsonify({"error": str(e)}), 500
        return jsonify({"ok": True, "profile": content, "expose": flags})


def api_account_device_refresh(idx):
    """按需补全账号设备字段：登录一次读回服务端已登记设备并落库。

    鉴权档：管理员。CSRF：**要**。
    副作用：**再铸一次会话**（读 `getState` 需要会话）＋ 成功时写账号行的识别码/机型。
    读不到一律不改库（见 `web/services/device_refresh`）；详情字段口径见 §1.4。

    为什么单独一条端点：惰性补全（`YibanClient._device_identity`）只在**该账号真的开签**
    时顺带发生——账号在签到前被打开表单、或长期签不上时，设备字段可能一直是空值。
    """
    m = _appmod()
    accounts = _accounts_data.load_accounts()
    if not 0 <= idx < len(accounts):
        return jsonify({"error": "账号不存在"}), 404
    acc = accounts[idx]
    data = _http._json_body()
    # 管理面写口一律 fail-closed（与 update/delete/restore/purge/review/move 同纪律）：
    # 本端点会对目标账号**发起一次真实登录**并写它的行，错位代价高于让调用方刷新一次
    if _accounts_data._stale_idx_guard(acc, data, fail_closed=True):
        return jsonify({"error": "账号列表已变化，请刷新页面后重试"}), 409
    if acc.get("deleted"):
        return jsonify({"error": "账号已删除，请先恢复"}), 400
    if not _signs_in(acc):
        # 只有"会签到"的账号才补全（判据复用 `signs_in`，与容量/引擎同源）：
        # pending/rejected 永不签到，为它发起真实登录既无收益又多一次风控暴露面
        return jsonify({"error": "账号未生效（待审核或已拒绝），请先通过审核再补全设备"}), 400
    status, payload = _refresh_account_device(
        m, acc, session.get("username") or "?", _verify_limits(), _verify_fails(),
        "account_device_refresh")
    if status == 503:
        return jsonify(payload), status, {"Retry-After": "2"}
    return jsonify(payload), status


def api_account_add():
    """添加账号。

    - 不填邮箱：管理员自有账号（owner=admin，直接生效）
    - 填用户邮箱：账号归属该用户并进入待审核（仍需管理员点"通过"）；
      邮箱未注册时自动创建网站用户（生成临时密码，需告知用户）。
    """
    m = _appmod()
    # 操作级锁：手机号唯一/每人限 1/自动注册检查与写入原子（防并发重复添加与覆盖丢失）
    data = _http._json_body()
    err, clean = _accounts_data.validate_account(data, require_password=True)
    if err:
        return jsonify({"error": err}), 400
    # 添加口没有旧值可保：哨兵同样折算（协议令牌绝不允许当字面量送去验证或落库）
    _accounts_data.fold_phone_code(clean)
    # 预筛（同 api_my_account_add）：容量/手机号占用/内置邮箱先拦，注定失败的
    # 添加不再消耗易班网络验证。权威校验仍在下方写锁内。
    with _locks._file_lock:
        accounts_pre = _accounts_data.load_accounts()
        max_accounts_pre = _env_io_svc.load_env_int(m.ENV_FILE, "YIBAN_MAX_ACCOUNTS", _limits.DEFAULT_MAX_ACCOUNTS)
        email_screen = str(data.get("email", "")).strip().lower()
        # 容量兜底：账号配额 = 活跃账号数（含裸账号）；
        # 本次将新增 1 个非删除账号——归属邮箱已持有活跃账号的重复添加
        # 不会新增（随后 400 拦截），不计增量，保持原错误优先级
        holds_live = email_screen and any(
            a.get("owner") == email_screen and not a["deleted"] for a in accounts_pre)
        if _capacity._accounts_at_capacity(0 if holds_live else 1):
            _capacity._notify_capacity_once("accounts", max_accounts_pre, "账号数量")
            return jsonify({"error": f"账号数量已达上限（{max_accounts_pre}），请联系管理员扩容"}), 403
        if _accounts_data.find_account_index(accounts_pre, clean["phone"]) is not None:
            return jsonify({"error": f"手机号 {clean['phone']} 已存在"}), 400
        if email_screen and email_screen == _security._builtin_admin_email():
            return jsonify({"error": "内置管理员邮箱不可注册"}), 400
        # 域名预筛：注定被拒的占位/一次性域名不消耗易班验证配额
        if email_screen:
            dom_err = _render.email_domain_error(email_screen)
            if dom_err:
                return jsonify({"error": dom_err}), 400
    # 添加账号即时验证（管理员开启 YIBAN_ACCOUNT_VERIFY 后生效，验证失败当场打回）；
    # 验证尝试受每用户配额限制，认证失败另有按手机号冷却（与用户提交路径同口径）
    verify_async_job = False
    if _verify_queue._account_verify_enabled():
        if _security._verify_fail_cooldown_remaining(_verify_fails(), clean["phone"], time.time()) > 0:
            return jsonify({"error": _verify_queue.VERIFY_FAIL_COOLDOWN_MSG}), 429
        _vuser = str(session.get("username", ""))
        if _verify_queue.verify_async_enabled():
            # 异步：请求线程只**先扣配额**，外呼交给后台任务。
            # 待办队列满 → 503（此时账号尚未落库，拒绝无副作用）。
            if _verify_queue._verify_queue_full():
                return jsonify({"error": _verify_queue.VERIFY_BUSY_MSG}), 503, {"Retry-After": "2"}
            if not _security._verify_attempt_allowed(_verify_limits(), _vuser):
                return jsonify({"error": "账号验证尝试过于频繁，请稍后再试"}), 429
            verify_async_job = True
        else:
            try:
                verify_err = _verify_queue.run_verify_with_gate(clean, _vuser, _verify_limits())
            except _verify_queue.VerifyGateBusy:
                return jsonify({"error": _verify_queue.VERIFY_BUSY_MSG}), 503, {"Retry-After": "2"}
            except _verify_queue.VerifyQuotaExceeded:
                return jsonify({"error": "账号验证尝试过于频繁，请稍后再试"}), 429
            if verify_err:
                fail_kind = _security._record_verify_failure(_verify_fails(), clean["phone"], verify_err, time.time())
                db.audit(
                    session.get("username") or "?",
                    "account_add_verify_fail",
                    _masking.mask_phone(clean["phone"]),
                    f"验证未通过（{fail_kind}）",
                )
                return jsonify({"error": verify_err}), 400
    email = str(data.get("email", "")).strip().lower()
    initial_hash = None  # 锁外预计算（scrypt ~100ms 不阻塞其他请求）
    if email:
        initial = str(data.get("initial_password", ""))
        if initial:
            pw_err = _accounts_data._password_policy_error(initial)
            if pw_err:
                return jsonify({"error": f"初始密码不符合要求：{pw_err}"}), 400
            initial_hash = generate_password_hash(initial, method=_security.SCRYPT_METHOD)
    with _locks._file_lock:
        accounts = _accounts_data.load_accounts()
        # 容量兜底：账号配额（活跃账号数含裸账号，防无限增长）；
        # 归属邮箱已持有活跃账号的重复添加不新增，不计增量（保持原错误优先级）
        max_accounts = _env_io_svc.load_env_int(m.ENV_FILE, "YIBAN_MAX_ACCOUNTS", _limits.DEFAULT_MAX_ACCOUNTS)
        holds_live = email and any(a.get("owner") == email and not a["deleted"] for a in accounts)
        if _capacity._accounts_at_capacity(0 if holds_live else 1):
            _capacity._notify_capacity_once("accounts", max_accounts, "账号数量")
            return jsonify({"error": f"账号数量已达上限（{max_accounts}），请联系管理员扩容"}), 403
        if _accounts_data.find_account_index(accounts, clean["phone"]) is not None:
            return jsonify({"error": f"手机号 {clean['phone']} 已存在"}), 400

        if email:
            if len(email.split("@")[0]) > _limits.EMAIL_USER_MAX:
                return jsonify({"error": f"邮箱用户名部分过长（最多 {_limits.EMAIL_USER_MAX} 字符）"}), 400
            if not _limits.EMAIL_RE.match(email) or len(email) > 64:
                return jsonify({"error": "用户邮箱格式不正确"}), 400
            # 邮箱域名审查：与开放注册同规则，防自动注册路径绕过
            dom_err = _render.email_domain_error(email)
            if dom_err:
                return jsonify({"error": dom_err}), 400
            # 内置管理员邮箱不可被自动注册占用
            if email.strip().lower() == _security._builtin_admin_email().strip().lower():
                return jsonify({"error": "内置管理员邮箱不可注册"}), 400
            # 该用户已有账号（每人限 1 个）则拒绝（软删除的不占名额，与用户端一致）
            if any(a.get("owner") == email and not a.get("deleted") for a in accounts):
                return jsonify({"error": f"{email} 已有一个账号，无需重复添加"}), 400
            # 自动注册：邮箱未注册则创建网站用户（初始密码由管理员在表单中设置，不生成明文临时密码）
            if db.find_user(email) is None:
                if initial_hash is None:
                    return jsonify({"error": f"{email} 尚未注册，请填写「初始密码」为其创建首登密码"}), 400
                # 自动注册同样受用户容量（全部未删除用户）与注销冷却期约束
                max_users = _env_io_svc.load_env_int(m.ENV_FILE, "YIBAN_MAX_USERS", _limits.DEFAULT_MAX_USERS)
                if _capacity._users_at_capacity():
                    _capacity._notify_capacity_once("users", max_users, "注册人数")
                    return jsonify({"error": "注册人数已达上限，请联系管理员"}), 403
                du = db.find_user_any(email)
                if (
                    du is not None
                    and du.get("deleted")
                    and _accounts_data._delete_grace_remaining(du.get("deleted_at", "")) > 0
                ):
                    return jsonify({"error": "该邮箱账号正在注销冷却期（7 天内可登录恢复）"}), 400
                try:
                    created = db.create_user(
                        email, initial_hash, "user",
                        clock.now().strftime("%Y-%m-%d %H:%M:%S"), 1,
                        # 与开放注册同口径的注册留痕，且与 INSERT 同事务：注册即建立
                        # 账号凭据，中间被杀不留"建了却无痕"。
                        audit_spec={
                            "username": session.get("username") or "?",
                            "action": "user_register",
                            "target": email,
                            "detail": "管理员添加账号自动注册",
                        },
                    )
                except sqlite3.IntegrityError:
                    return jsonify({"error": "该邮箱已注册"}), 400  # 并发注册兜底
                if not created:
                    return jsonify({"error": "该邮箱已注册"}), 400  # OR IGNORE 未实际创建
                m.logger.info("为邮箱 %s 自动注册用户（管理员设置初始密码）", _accounts_data._mask_email(email))
            clean["owner"] = email
            clean["status"] = _accounts_data.ACCOUNT_STATUS_PENDING
        else:
            clean["owner"] = "admin"
            clean["status"] = _accounts_data.ACCOUNT_STATUS_ACTIVE
        try:
            # 审计与 INSERT 同事务（audit_spec）：账号建了却没有审计行这件事不可能
            # 发生——中间被杀也不会留下"做了无留痕、欠账仍为 0"。
            new_id = db.add_account(clean, audit_spec={
                "username": session.get("username") or "?",
                "action": "account_add",
                "target": _masking.mask_phone(clean["phone"]),
                "detail": f"归属 {_accounts_data._mask_email(clean['owner'])} 状态 {clean['status']}",
            })
        except db.DuplicatePhoneError:
            return jsonify({"error": f"手机号 {clean['phone']} 已存在"}), 400
        except db.DuplicateOwnerError:
            return jsonify({"error": "该用户已有一个账号，无需重复添加"}), 400
        except sqlite3.IntegrityError:
            return jsonify({"error": f"手机号 {clean['phone']} 已存在"}), 400  # 并发重复兜底
        accounts = _accounts_data.load_accounts()  # 重读（含新行，返回前端列表）
    m.logger.info(
        "添加账号 %s（归属 %s，状态 %s）",
        _masking.mask_phone(clean["phone"]),
        _accounts_data._mask_email(clean["owner"]),
        clean["status"],
    )
    resp = {
        "ok": True,
        "msg": "已添加，等待审核通过后参与签到",
        "accounts": [_accounts_data.mask_account(a, i) for i, a in enumerate(accounts)],
    }
    if verify_async_job:
        # 账号已落库，随后由后台任务校验；响应**增补** job_id/status（不改既有字段）
        try:
            job_id, _ = _verify_queue._start_verify_job(
                clean, str(session.get("username", "")), new_id,
                _verify_fails(), _verify_limits(),
            )
            resp["job_id"] = job_id
            resp["status"] = "verifying"
        except _verify_queue.VerifyGateBusy:
            # 待办队列在扣配额与建任务之间被占满（竞态兜底）：账号已入库，
            # 不因此让请求失败——置为 rejected 并说明原因，由用户重试。
            m.logger.warning("校验任务队列已满，账号 %s 未建校验任务",
                           _masking.mask_phone(clean["phone"]))
    return jsonify(resp)


def api_account_update(idx):
    m = _appmod()
    with _locks._file_lock:
        accounts = _accounts_data.load_accounts()
        if not 0 <= idx < len(accounts):
            return jsonify({"error": "账号不存在"}), 404
        old = accounts[idx]
        # 软删除账号禁止编辑（防编辑流程绕过软删除，恢复需走 restore 接口）
        if old.get("deleted"):
            return jsonify({"error": "账号已删除，请先恢复"}), 400
        data = _http._json_body()
        # 乐观锁：请求携带编辑打开时的账号快照（JSON 字符串），与库内当前值
        # 不一致 → db 返回 False → 409，防多管理员/多标签页并发编辑互相覆盖
        snapshot = None
        snapshot_raw = data.get("_snapshot") or ""
        if snapshot_raw:
            try:
                snapshot = (
                    json.loads(snapshot_raw) if isinstance(snapshot_raw, str) else snapshot_raw
                )
            except json.JSONDecodeError:
                snapshot = None
        # 防错位守卫：目标行被物理清除（purge、用户注销连带）后，旧列表里的 idx
        # 会指到另一账号上——没有这层守卫就是"静默改写他人凭据 + 返回 200"。
        # 比对基准优先用乐观锁快照里的 phone：编辑表单允许"填写完整新号码"改绑
        # 手机号（db.update_account 专门做了重加密），那是一次变更而不是错位，
        # 直接拿 data["phone"] 比会把这条合法路径全部 409 掉；快照缺失或不含
        # phone 时退回与 /restore、/review 一致的 data["phone"] 比对。两种标识都
        # 拿不出即 fail-closed 拒绝：本端点改的是别人的易班凭据，放行一次错位的
        # 代价是静默改写他人账号并回 200，拒绝的代价只是调用方刷新一次列表。
        # 合法编辑路径不受影响——编辑表单要么带快照（前端 edit 先 GET detail 再
        # 回传 _snapshot），要么必须带完整新号。
        guard_src = (
            {"phone": snapshot["phone"]}
            if isinstance(snapshot, dict) and snapshot.get("phone")
            else data
        )
        if _accounts_data._stale_idx_guard(old, guard_src, fail_closed=True):
            return jsonify({"error": "账号列表已变化，请刷新页面后重试"}), 409
        err, clean = _accounts_data.validate_account(data, require_password=False)
        if err:
            return jsonify({"error": err}), 400
        # 手机号变更时检查冲突（排除自己）
        if (
            clean["phone"] != old.get("phone")
            and _accounts_data.find_account_index(accounts, clean["phone"]) is not None
        ):
            return jsonify({"error": f"手机号 {clean['phone']} 已存在"}), 400
        # 设备识别码先折算成将进 SET 的最终值，再进下面的"改写凭据"判定——写侧
        # （store/accounts.py）把 phone_code 与 password 同档加密/重加密，读侧若少认
        # 这一项，"只改写设备识别码"就会既不过门、不标位、也不发信：写侧干了凭据
        # 的活，读侧记账口径却是零。哨兵/留空的语义与 /my-accounts 及添加路径共用
        # fold_phone_code，任何一侧自行解读都会把"清除"做成空操作。
        old_code = old.get("phone_code") or ""
        code_written = _accounts_data.fold_phone_code(clean, old_code) != old_code
        # 凭据改写分两档（按用户拍板清单收窄）：
        # - **过门**：改易班密码 / 改绑手机号（改他人凭据类）——拿到被窃管理员会话的人
        #   一次 PUT 就能把某用户的账号换成自己的凭据，此后签到在攻击者侧完成、真用户
        #   被静默挤出，界面上看不出异常。走 _high_risk_gate：先二次鉴权、通过后才占
        #   高危额度（顺序即该函数的立身之本）。
        # - **免门**：只改/只清设备识别码（MF-86 的归类回退为"只标位/只发信"）——
        #   识别码是学校开设备绑定时的登录要素，属完整性/可用性轴，可逆（改回即可），
        #   不再要口令、不占额度；但**保留全部信号**：creds_written（下方审计"改写凭据"
        #   位 + 当事人信 + 非 full 档管理员紧急告警）仍把 code_written 算在内。
        # 只改备注/设备型号/名称不算任何一档（零误报不变）。
        creds_written = (bool(str(data.get("password", "")).strip())
                         or clean["phone"] != old.get("phone")
                         or code_written)
        if bool(str(data.get("password", "")).strip()) or clean["phone"] != old.get("phone"):
            # risk_always=True（用户 2026-10-01 拍板）：「改写他人易班凭据」整类摘出
            # risk 档"同出口免口令"——同出口也必须当次口令。full/off 两档逐字不变。
            denied = _high_risk_gate()(data, "改写他人易班凭据", quota="creds",
                                       risk_always=True)
            if denied is not None:
                return denied
        # 密码留空 = 保持不变（密码明文永不下发前端）
        if not clean["password"]:
            clean["password"] = old.get("password", "")
        # 归属保持不变（管理员编辑不改变提交者）
        clean["owner"] = old.get("owner", "admin")
        # 改绑手机号一律回待审核重审——
        # 手机号即凭据主体，原审核结论绑定的是旧号，改绑后若维持 ACTIVE 就等于
        # "免审换号继续签到"。管理员改绑用户的号同样回 pending，由管理员再批；
        # 仅密码/识别码变更（phone 不变）维持原状态不变。
        rebind = clean["phone"] != old.get("phone")
        if rebind:
            clean["status"] = _accounts_data.ACCOUNT_STATUS_PENDING
            # 回审即清除旧拒绝理由（与用户侧重新提交同口径，避免 pending 行带旧理由）
            clean["reject_reason"] = ""
        else:
            clean["status"] = old.get("status", _accounts_data.ACCOUNT_STATUS_ACTIVE)
        try:
            # 审计与本次 UPDATE 同事务：改写他人易班凭据必须与留痕共存亡——凭据已改而
            # 审计表无此条，正是"改了凭据但追不到谁改的"的核心症状。
            result = db.update_account(
                old["id"],
                clean,
                expect_snapshot=snapshot if isinstance(snapshot, dict) else None,
                audit_spec={
                    "username": session.get("username") or "?",
                    "action": "account_update",
                    "target": _masking.mask_phone(clean["phone"]),
                    "detail": ("编辑账号" + (" 改绑回审" if rebind else "")
                               + (" 改写凭据" if creds_written else "")),
                },
            )
        except db.DuplicatePhoneError:
            return jsonify({"error": f"手机号 {clean['phone']} 已存在"}), 400
        except db.DuplicateOwnerError:
            return jsonify({"error": "该用户已有一个账号，无需重复添加"}), 400
        except sqlite3.IntegrityError:
            return jsonify({"error": f"手机号 {clean['phone']} 已存在"}), 400  # 并发改号兜底
        if result is False:
            return jsonify({"error": "账号已被其他管理员修改，请刷新后重试"}), 409
        if result is None:
            return jsonify({"error": "账号不存在"}), 404
        # 手机号变更 → 旧号自选时间片失效，必须在 update_account 成功后再清，
        # 避免更新失败时误删旧号自选（防孤儿 pref 占容量）
        if clean["phone"] != old.get("phone"):
            db.clear_time_pref(old.get("phone", ""))
        # 凭据变更（改密码/改绑手机号/改写识别码）才清除熔断暂停，立即恢复签到；
        # 仅改备注/状态等不动熔断计数（防任意编辑把 fail_days 清零、熔断永不跳闸）
        _logs_svc.clear_fuse_on_cred_change(old.get("phone", ""), old.get("password", ""), clean,
                                    old_code)
        # 审计行已随 update_account 同事务写入（见上面 audit_spec）
        # 当事人必须知情（管理员改写他人易班凭据除二次鉴权外，还要绕过
        # 其通知开关发变更信）。send_user 直收地址、不读 mail_notify——攻击者把本人
        # 的接收开关关掉也照样收得到，与自助改密、审核拒绝同一口径。未启用邮件/无
        # 收件人时它自己静默跳过；整段兜异常：编辑已落盘，通知失败不得把结果带崩。
        if creds_written:
            _owner = str(clean.get("owner") or "")
            if _owner and _owner != "admin":
                try:
                    _what = []
                    if str(data.get("password", "")).strip():
                        _what.append("重设了易班登录密码")
                    if rebind:
                        _what.append("改绑了手机号（需管理员重新审核后才参与签到）")
                    if code_written:
                        _what.append("改写或清除了设备识别码"
                                     "（开启设备绑定的学校，签到时提交给易班的验证码随之改变）")
                    _locks.run_after_file_lock(
                        mailer.send_user,
                        _owner,
                        "【易班签到】您的易班账号信息被管理员修改",
                        mail_layout.Mail(
                            summary=f"您在本站提交的易班账号 {_masking.mask_phone(clean['phone'])}"
                                    " 刚刚被管理员修改。",
                            items=_what,
                            fields=[("操作者", _accounts_data._mask_email(
                                str(session.get("username") or "?")[:64]))],
                            advice=["如非您本人申请，请立即联系管理员核实"],
                            level="urgent",
                        ),
                    )
                except Exception as e:
                    m.logger.warning("账号凭据变更通知发送失败（不影响已完成的编辑）: %s", e)
        # 改写他人凭据靠"事后告警 + 审计链"兜底：一封"刚才执行了 XX 操作"让管理员
        # 可追溯、可回滚（此前只通知当事人，管理员侧零信号）。抑制口径随**门是否真
        # 索要口令**走，而不是只认 full 档：本落点 2026-10-01 起 `risk_always=True`，
        # risk（缺省）档同样当次要口令；只有 off 档不索要口令、事后告警是唯一信号。
        # 只认 full 会让缺省档里每一次合法改写都多发一封 urgent 告警，抢
        # DEFAULT_URGENT_DAILY_MAX=3 的紧急日账（ba-p05-02）。
        gated_creds = (bool(str(data.get("password", "")).strip())
                       or clean["phone"] != old.get("phone"))
        if (creds_written and not (gated_creds and
                                   _security._pw_gate_tier() != _security.PW_GATE_OFF)):
            _locks.run_after_file_lock(
                _notify_mail.send_notification,
                "高危管理操作告警",
                _notify_mail._change_mail(
                    "改写他人易班凭据。",
                    detail=[("目标", _masking.mask_phone(clean["phone"])),
                            ("归属用户",
                             _accounts_data._mask_email(str(clean.get("owner") or "admin")))],
                    advice=["如非本人申请，请立即核实并回滚该账号凭据"],
                ),
                urgent=True,
            )
        accounts = _accounts_data.load_accounts()
        m.logger.info("编辑账号 %s", _masking.mask_phone(clean["phone"]))
        # 编辑路径的**异步探针**：口令或识别码实际变更后，后台复核新凭据是否可用。
        # 豁免（不做本地口令策略）的前提正是"有真实登录探针兜底"。缺它时错误凭据静默
        # 潜伏到下次签到才暴露。一律异步（同步会占住请求线程与外呼席位，而编辑已落盘
        # 不能回滚）；失败只留记录，不改账号可用状态语义。经 run_after_file_lock 登记：
        # 探针建任务（DB 写）并入队外呼，按锁纪律挪到出锁后执行。
        # 触发口径只认口令/识别码变更，不认改绑手机号——改绑已回待审核重审。
        if bool(str(data.get("password", "")).strip()) or code_written:
            _probe_user = str(session.get("username", ""))
            _probe_limits = _verify_limits()
            _probe_fails = _verify_fails()
            _locks.run_after_file_lock(
                _start_edit_probe,
                clean, _probe_user, _probe_fails, _probe_limits, _verify_queue._start_verify_job,
                _verify_queue._account_verify_enabled, _verify_queue._verify_queue_full,
                lambda: _security._verify_attempt_allowed(_probe_limits, _probe_user),
                lambda: _security._verify_fail_cooldown_remaining(
                    _probe_fails, clean["phone"], time.time()),
            )
        return jsonify(
            {"ok": True, "accounts": [_accounts_data.mask_account(a, i) for i, a in enumerate(accounts)]}
        )


def api_accounts_batch():
    """批量操作账号（批量多选功能）：approve/reject 审核，purge 彻底删除。

    body: {"action": ..., "ids": [...], "reason": "批量拒绝理由"}
    整体事务：失败全部回滚，无效项软跳过。

    purge 与用户侧 /api/users/batch(delete)、/api/users/deleted/purge 属同一类
    "不可逆清除"：一个请求最多 BATCH_OP_LIMIT 条，漏了门禁即为数十秒内把全部
    易班凭据不可逆清零且无人知晓。故 purge 要求二次鉴权 + 同管理员窗口限速
    （429）。delete（软删）虽可逆（7 天宽限 + 409 防错位），但它立即停止该用户
    代签且受害者无法自助恢复，同样接入该高危限速、但**不要求二次
    口令**（可逆操作加口令只增误伤）。approve/reject/restore 可逆且有防错位兜底，
    维持无门禁。
    """
    m = _appmod()
    # 参数校验与高危门禁刻意留在 _file_lock 之外（与三处高危删除同口径）：
    # scrypt 口令校验单次数百毫秒，放进全局文件锁里会让一次鉴权阻塞全进程的
    # 账号读写（该锁同时护着 JSON 与 SQLite 侧的读-改-写）。
    data = _http._json_body()
    action = data.get("action")
    ids = data.get("ids") or []
    if action not in ("approve", "reject", "purge", "restore", "delete"):
        return jsonify({"error": "未知操作"}), 400
    if not isinstance(ids, list) or not ids:
        return jsonify({"error": "请选择要操作的账号"}), 400
    # 单次批量上限：没有它，一个请求即可清空全部账号；与
    # /api/users/deleted/purge 共用 BATCH_OP_LIMIT
    if len(ids) > _limits.BATCH_OP_LIMIT:
        return jsonify({"error": f"单次批量操作最多 {_limits.BATCH_OP_LIMIT} 个账号"}), 400
    # 理由清洗：与单条拒绝同一净化口径（覆盖全部分隔符——理由会落库、进审计详情、
    # 进 sign.log，只压 \r\n 时 U+2028/U+0085 等能一路穿过去伪造行）
    reason = _notify_mail._nl_safe(str(data.get("reason", "")).strip()[:100])
    if action == "reject" and not reason:
        return jsonify({"error": "批量拒绝需要填写理由"}), 400
    if action == "purge":
        # 统一走 _high_risk_gate（先验口令，通过了才占高危额度）；
        # 429 文案与用户侧批量删除一致，运维只需记一句话
        gate = _high_risk_gate()(
            data, "批量彻底删除账号", limit_msg="删除操作过于频繁，请稍后再试",
            irreversible=True)
        if gate:
            return gate
    # 软删也占用同一份高危额度：它虽可逆（7 天内可恢复），但立即让该用户当天起
    # 停止代签，且**受害者无法自助恢复**（/api/my-accounts/<idx>/restore 对管理员
    # 删除的行返回 403），因此被盗的注册管理员会话可用几十次调用在数秒内静默让
    # 全站停签——与"删数据 / 拆报警器是同一条链" 同风险。这里**仍然不要求二次
    # 口令**（可逆不加口令，加了口令只增误伤），只限制速率（留痕在审计行）。
    if action == "delete" and _admin_delete_limited()():
        return jsonify({"error": "删除操作过于频繁，请稍后再试"}), 429
    with _locks._file_lock:
        accounts = _accounts_data.load_accounts()
        # idx 寻址防错位：客户端随 ids 携带对齐的 phones 数组，与服务端当前列表
        # 逐一比对，不一致整体拒绝（409 引导刷新）。另注意 isinstance(True, int)
        # 为真，ids 里的 JSON true 会被当作索引 1，故用 type(i) is int 严格判定。
        valid = sorted(
            {i for i in ids if type(i) is int and 0 <= i < len(accounts)}
        )
        if not valid:
            return jsonify({"error": "所选账号不存在"}), 404
        phones_in = data.get("phones")
        # 对齐令牌必须完整：缺 phones / 非数组 / 与 ids 长度不等 —— 三者任一都
        # fail-closed 409，前端重取对齐快照。此前只判「长度不等」，完全不带 phones
        # （None）时两个分支都不进、静默按 idx 作用；purge 不可逆，一次错位即不可恢复。
        # 前端 account-ops.js 总带对齐 phones；脚本/第三方不带会被一并 409（契约收紧）。
        if not isinstance(phones_in, list) or len(phones_in) != len(ids):
            return jsonify({"error": "账号列表已变化，请刷新页面后重试"}), 409
        expect = {
            i: str(phones_in[k]).strip()
            for k, i in enumerate(ids)
            if type(i) is int
        }
        # 双侧 _mask_phone 归一（出站为脱敏号，见 _stale_idx_guard 注释）
        if any(
            i in expect
            and _masking.mask_phone(expect[i]) != _masking.mask_phone(str(accounts[i].get("phone", "")))
            for i in valid
        ):
            return jsonify({"error": "账号列表已变化，请刷新页面后重试"}), 409

        # 容量闸门（approve 专属）：与单条审核口（api_account_review 的 approve 分支）
        # 同一语义——通过审核 = 让这一行开始产生签到负载，未通过的行不计入容量，
        # 故这里是本口径下唯一的把关点。缺这道门时批量口一次请求即可整道绕过上限。
        if action == "approve":
            become = sum(
                1
                for i in valid
                if not accounts[i].get("deleted")
                and accounts[i].get("status") in (
                    _accounts_data.ACCOUNT_STATUS_PENDING,
                    _accounts_data.ACCOUNT_STATUS_REJECTED,
                )
            )
            if become and _capacity._accounts_at_capacity(become):
                return jsonify({
                    "error": "账号数量已达上限，无法通过审核。请清理不用的账号或提高账号容量上限后重试"
                }), 403

        # 容量闸门（restore 专属）：恢复 = 让这一行重新计入容量（软删期间不计入，见
        # _capacity_account_count）。缺这道门时「软删 k 个腾空 → 批通过 k 个 pending →
        # 恢复 k 个」可把占用推过 YIBAN_MAX_ACCOUNTS：软删不计容量，故腾空后 approve
        # 能过，恢复时却无人把关。与 approve 同一门、同一 precompute 口径；pending/
        # rejected 的软删行恢复后仍不签到（不计容量），不纳入 become，避免误伤。
        if action == "restore":
            become = sum(
                1
                for i in valid
                if accounts[i].get("deleted")
                and accounts[i].get("status") not in (
                    _accounts_data.ACCOUNT_STATUS_PENDING,
                    _accounts_data.ACCOUNT_STATUS_REJECTED,
                )
            )
            if become and _capacity._accounts_at_capacity(become):
                return jsonify({
                    "error": "账号数量已达上限，无法恢复。请清理不用的账号或提高账号容量上限后重试"
                }), 403

        ops = []
        batch_targets = []  # 审计留目标清单（脱敏截断）：只收真正产出了操作的行
        reject_notify_owners = {}  # 批量拒绝每户一封
        # 内存中跟踪每个 owner 当前是否有未删除账号，用于恢复防呆
        live_owners = {
            a.get("owner", "")
            for a in accounts
            if a.get("owner") and not a.get("deleted")
        }
        for i in valid:
            acc = accounts[i]
            _ops_before = len(ops)  # 本行是否真的产出了操作（目标清单与 ops 同口径的判据）
            if action == "approve":
                # 软删除账号不可被审核通过（deleted 账号不参与审核流转）
                if not acc.get("deleted") and acc.get("status") in (
                    _accounts_data.ACCOUNT_STATUS_PENDING,
                    _accounts_data.ACCOUNT_STATUS_REJECTED,
                ):
                    ops.append(("update_status", acc["id"], _accounts_data.ACCOUNT_STATUS_ACTIVE, ""))
            elif action == "reject":
                if acc.get("status") in (_accounts_data.ACCOUNT_STATUS_PENDING, _accounts_data.ACCOUNT_STATUS_REJECTED):
                    ops.append(("update_status", acc["id"], _accounts_data.ACCOUNT_STATUS_REJECTED, reason))
                    if acc.get("owner"):
                        reject_notify_owners.setdefault(
                            acc["owner"], []
                        ).append(_masking.mask_phone(str(acc.get("phone", ""))))
            elif action == "purge":
                # 仅允许彻底删除「已软删除」账号（与单个彻底删除一致，防误删正常账号）
                if acc.get("deleted"):
                    ops.append(("purge", acc["id"]))
            elif action == "restore":
                if acc.get("deleted"):
                    owner = acc.get("owner", "")
                    if owner and owner != "admin" and owner in live_owners:
                        return jsonify(
                            {
                                "error": f"账号「{acc.get('name', '')}」的归属用户已有生效账号，无法恢复（每人限 1 个）"
                            }
                        ), 400
                    ops.append(("set_deleted", acc["id"], 0, ""))
                    if owner:
                        live_owners.add(owner)
            elif action == "delete" and not acc.get("deleted"):
                # 软删除：进入待删除列表（保留期内可恢复），与单个删除一致
                ops.append(
                    ("set_deleted", acc["id"], 1, clock.now().strftime("%Y-%m-%d %H:%M:%S"))
                )
            # 目标清单与 ops 同源：不满足前置（本行没生成操作）的行不进审计——
            # 此前 `done=len(ops)` 数操作、`batch_targets` 数全部合法下标，
            # 同一条审计行里"处理 N 个"与其后清单是两套口径。
            if len(ops) > _ops_before:
                batch_targets.append(acc.get("phone", ""))
        done = len(ops)
        # 批量操作留目标清单（脱敏截断），破坏事后可从审计还原"动了谁"。
        # 审计与 ops 同事务（R3-005）：批量里可能含 purge，审计失败必须整体回滚。
        batch_audit_spec = {
            "username": session.get("username") or "?",
            "action": "account_batch",
            "target": action,
            "detail": (f"处理 {done} 个: " + ",".join(
                _masking.mask_phone(str(p)) for p in (batch_targets or [])[:20]
            ))[:db.AUDIT_DETAIL_LIMIT],
        }
        if ops:
            try:
                db.batch_account_ops(ops, audit_spec=batch_audit_spec)
            except db.DuplicateOwnerError:
                db.audit(
                    session.get("username") or "?",
                    "account_batch",
                    action,
                    "失败，已回滚（该用户已有一个账号）",
                )
                return jsonify({"error": "批量操作失败，已全部回滚（该用户已有一个账号）"}), 400
            except Exception as e:
                m.logger.error("批量%s账号失败: %s（已回滚）", action, e)
                db.audit(
                    session.get("username") or "?",
                    "account_batch",
                    action,
                    "失败，已回滚",
                )
                return jsonify({"error": "批量操作失败，已全部回滚"}), 500
        if action == "reject" and reject_notify_owners:
            # 批量拒绝每户一封、同样文案（批量拒绝必填理由，无空理由分支）。
            # 刻意放在 batch_account_ops 成功之后——回滚路径已提前 return，不会
            # 出现"状态没变先收拒信"。本枚是 `ba-p05-01` 最重的落点：逐户登记、
            # 出锁后按 owner 顺序统一发出（修复前是锁内最多 10 封 × 每封 10 条目 ×
            # 每条 socket 超时）。
            for _owner, _phones in sorted(reject_notify_owners.items()):
                _locks.run_after_file_lock(
                    mailer.send_user,
                    _owner,
                    "【易班签到】您提交的账号未通过审核",
                    _notify_mail._review_reject_mail(_phones, reason),
                )
        if not ops:
            # 没有操作（处理 0 个）：无删除可回滚，请求本身仍要留痕。
            db.audit(batch_audit_spec["username"], batch_audit_spec["action"],
                     batch_audit_spec["target"], batch_audit_spec["detail"])
        # 软删与彻底删除不再外发即时告警：留痕由上面的审计行承担（动作 + 目标清单），
        # 软删本身可逆（保留期内可恢复），把它当"事故"逐条发信会让真故障淹没。
        accounts = _accounts_data.load_accounts()
        m.logger.info("批量%s账号 %d 个", action, done)
        msg = {
            "approve": f"已通过 {done} 个账号",
            "reject": f"已拒绝 {done} 个账号",
            "purge": f"已彻底删除 {done} 个账号",
            "restore": f"已恢复 {done} 个账号",
            "delete": f"已删除 {done} 个账号（可恢复）",
        }[action]
        return jsonify(
            {
                "ok": True,
                "msg": msg,
                "accounts": [_accounts_data.mask_account(a, i) for i, a in enumerate(accounts)],
            }
        )


def api_account_delete(idx):
    """删除账号（软删除）：进入待删除状态，保留期内可恢复，超期自动彻底清除。

    软删占用高危额度（软删可逆但立即停签、受害者无法自助恢复，被盗注册管理员
    会话可借此静默让全站停签），但不要求二次口令（可逆操作不加口令），也不再
    外发即时告警——留痕由审计行承担，见函数体内的注释。
    """
    m = _appmod()
    # 门禁刻意留在 _file_lock 之外：_admin_delete_limited 只做内存计数与判速，
    # 放进全局文件锁会白占锁（与批量 purge 的门禁位置同口径）。
    if _admin_delete_limited()():
        return jsonify({"error": "删除操作过于频繁，请稍后再试"}), 429
    with _locks._file_lock:
        accounts = _accounts_data.load_accounts()
        if not 0 <= idx < len(accounts):
            return jsonify({"error": "账号不存在"}), 404
        acc = accounts[idx]
        if _accounts_data._stale_idx_guard(acc, _http._json_body(), fail_closed=True):
            return jsonify({"error": "账号列表已变化，请刷新页面后重试"}), 409
        db.set_account_deleted(
            acc["id"], 1, clock.now().strftime("%Y-%m-%d %H:%M:%S"),
            deleted_by="admin",
            audit_spec={
                "username": session.get("username") or "?",
                "action": "account_delete",
                "target": _masking.mask_phone(acc.get("phone", "")),
                "detail": "软删除",
            },
        )
        # 软删不再外发即时告警：留痕由上面的审计行承担，且软删可逆
        # （db.SOFT_DELETE_RETENTION_DAYS 天内可在待删除列表恢复）。
        accounts = _accounts_data.load_accounts()
        m.logger.info(
            "软删除账号 %s（%s 天内可恢复）", _masking.mask_phone(acc.get("phone", "")),
            db.SOFT_DELETE_RETENTION_DAYS,
        )
        return jsonify(
            {
                "ok": True,
                "msg": (f"已删除「{acc.get('name', '')}」，{db.SOFT_DELETE_RETENTION_DAYS}"
                        " 天内可在待删除列表恢复"),
                "accounts": [_accounts_data.mask_account(a, i) for i, a in enumerate(accounts)],
            }
        )


def api_account_restore(idx):
    """恢复待删除账号：撤销软删除，回到删除前状态。"""
    m = _appmod()
    with _locks._file_lock:
        accounts = _accounts_data.load_accounts()
        if not 0 <= idx < len(accounts):
            return jsonify({"error": "账号不存在"}), 404
        acc = accounts[idx]
        if _accounts_data._stale_idx_guard(acc, _http._json_body(), fail_closed=True):
            return jsonify({"error": "账号列表已变化，请刷新页面后重试"}), 409
        if not acc.get("deleted"):
            return jsonify({"error": "该账号不在待删除状态"}), 400
        # 防呆：归属用户名下已有其他未删除账号则拒绝恢复（每人限 1 个，防恢复后重复）
        if _accounts_data._owner_has_other_live(accounts, acc):
            return jsonify(
                {"error": "该用户已有生效账号，无法恢复（每人限 1 个）"}
            ), 400
        # 容量闸门：恢复 = 让这一行重新计入容量（软删期间不计入）。软删腾空后批过
        # pending 再逐条恢复同样能越界，故单条与批量同一门；仅对会真正计入容量的行
        # （status 已通过审核）判定，pending/rejected 行恢复后仍不签到，不误伤。
        if (
            acc.get("status") not in (_accounts_data.ACCOUNT_STATUS_PENDING, _accounts_data.ACCOUNT_STATUS_REJECTED)
            and _capacity._accounts_at_capacity(1)
        ):
            return jsonify({
                "error": "账号数量已达上限，无法恢复。请清理不用的账号或提高账号容量上限后重试"
            }), 403
        db.set_account_deleted(acc["id"], 0, audit_spec={
            "username": session.get("username") or "?",
            "action": "account_restore",
            "target": _masking.mask_phone(acc.get("phone", "")),
            "detail": "撤销软删除",
        })
        accounts = _accounts_data.load_accounts()
        m.logger.info("恢复账号 %s", _masking.mask_phone(acc.get("phone", "")))
        return jsonify(
            {
                "ok": True,
                "msg": f"已恢复「{acc.get('name', '')}」",
                "accounts": [_accounts_data.mask_account(a, i) for i, a in enumerate(accounts)],
            }
        )


def api_account_purge(idx):
    """彻底删除待删除账号：立即物理清除，不可恢复。

    单条物理清除与批量 purge 同口径：二次鉴权 + 同管理员窗口限速（429）。
    缺任一项都等于给被盗管理员会话留一条安静的清库通道（不要求确认口令、
    不受删除冷却约束）；动作本身经审计行留痕。
    """
    m = _appmod()
    # 门禁放在 _file_lock 之外（同 api_accounts_batch 与三处高危删除）：
    # 口令校验耗时数百毫秒，放进全局锁里会凭一次尝试卡住全进程账号读写
    data = _http._json_body()
    gate = _high_risk_gate()(
        data, "彻底删除账号", limit_msg="删除操作过于频繁，请稍后再试",
        irreversible=True)
    if gate:
        return gate
    with _locks._file_lock:
        accounts = _accounts_data.load_accounts()
        if not 0 <= idx < len(accounts):
            return jsonify({"error": "账号不存在"}), 404
        acc = accounts[idx]
        if _accounts_data._stale_idx_guard(acc, data, fail_closed=True):
            return jsonify({"error": "账号列表已变化，请刷新页面后重试"}), 409
        if not acc.get("deleted"):
            return jsonify({"error": "该账号不在待删除状态"}), 400
        # 删除与审计同事务：审计写失败 ⇒ 删除回滚，账号仍在（R3-005）。
        # 文件面的手机号清理在 DB 事务之后，失败不回滚已完成的物理删除。
        _accounts_data.purge_account(
            acc["id"],
            audit_spec=dict(
                username=session.get("username") or "?",
                action="account_purge",
                target=_masking.mask_phone(acc.get("phone", "")),
                detail="彻底删除",
            ),
        )
        # 彻底删除不再外发即时告警：留痕由上面的审计行承担（谁、删了哪个号）。
        accounts = _accounts_data.load_accounts()
        m.logger.info("彻底删除账号 %s", _masking.mask_phone(acc.get("phone", "")))
        return jsonify(
            {
                "ok": True,
                "msg": f"已彻底删除「{acc.get('name', '')}」",
                "accounts": [_accounts_data.mask_account(a, i) for i, a in enumerate(accounts)],
            }
        )


def api_account_review(idx):
    """审核普通用户提交的账号：
    approve=生效参与定时签到；reject=标记拒绝并附理由（用户可编辑后重新提交）。
    """
    m = _appmod()
    with _locks._file_lock:
        accounts = _accounts_data.load_accounts()
        if not 0 <= idx < len(accounts):
            return jsonify({"error": "账号不存在"}), 404
        data = _http._json_body()
        action = data.get("action")
        acc = accounts[idx]
        if _accounts_data._stale_idx_guard(acc, data, fail_closed=True):
            return jsonify({"error": "账号列表已变化，请刷新页面后重试"}), 409
        if action == "approve":
            # 软删除账号不可被审核通过（deleted 账号不参与审核流转）
            if acc.get("deleted") or acc.get("status") not in (_accounts_data.ACCOUNT_STATUS_PENDING, _accounts_data.ACCOUNT_STATUS_REJECTED):
                return jsonify({"error": "该账号无需审核"}), 400
            # 容量闸门：通过审核 = 让这一行开始产生签到负载。未通过审核的行不计入
            # 账号容量（见 _capacity_account_count），故此处是本口径下唯一的把关点——
            # 不放这道门就等于"提交时受限、审批时任意越界"，容量上限失去意义。
            if _capacity._accounts_at_capacity(1):
                return jsonify({
                    "error": "账号数量已达上限，无法通过审核。请清理不用的账号或提高账号容量上限后重试"
                }), 403
            db.update_account_status(acc["id"], _accounts_data.ACCOUNT_STATUS_ACTIVE, reject_reason="")
            db.audit(
                session.get("username") or "?",
                "account_review",
                _masking.mask_phone(acc.get("phone", "")),
                "approve",
            )
            m.logger.info("审核通过账号 %s（提交者 %s）", _masking.mask_phone(acc.get("phone", "")), _accounts_data._mask_email(acc.get("owner", "")))
            # 回显脱敏（与列表口径一致，防响应混入完整 PII；管理员详情页可取完整号）
            return jsonify({"ok": True, "msg": f"已通过 {_masking.mask_phone(acc.get('phone', ''))}，将参与定时签到"})
        if action == "reject":
            if acc.get("status") not in (_accounts_data.ACCOUNT_STATUS_PENDING, _accounts_data.ACCOUNT_STATUS_REJECTED):
                return jsonify({"error": "该账号无需拒绝"}), 400
            # 理由清洗：走统一净化 `_nl_safe`（覆盖 `str.splitlines()` 的全部分隔符）。
            # 此前只把 \r\n 压成空格，U+0085/U+2028 等其余分隔符仍能在审计详情与
            # sign.log 里伪造一行（如假造一条"操作者"记录）。
            reason = _notify_mail._nl_safe(str(data.get("reason", "")).strip()[:100])
            db.update_account_status(acc["id"], _accounts_data.ACCOUNT_STATUS_REJECTED, reason)
            db.audit(
                session.get("username") or "?",
                "account_review",
                _masking.mask_phone(acc.get("phone", "")),
                "reject" + (f" {reason[:60]}" if reason else ""),
            )
            # 拒绝必须主动触达提交者：理由只挂「我的账号」页属被动知情，提交者
            # 不回访即永远不知情；审核通过不发（登录即见生效，节约额度）。
            # send_user 未启用/无收件人静默跳过、失败仅记日志，不影响审核流；
            # 绕过 mail_notify 开关与「本人知情权」口径一致。
            _owner = acc.get("owner", "")
            if _owner:
                _locks.run_after_file_lock(
                    mailer.send_user,
                    _owner,
                    "【易班签到】您提交的账号未通过审核",
                    _notify_mail._review_reject_mail(
                        [_masking.mask_phone(str(acc.get("phone", "")))], reason
                    ),
                )
            m.logger.info(
                "拒绝账号 %s（提交者 %s，理由: %s）",
                _masking.mask_phone(acc.get("phone", "")),
                _accounts_data._mask_email(acc.get("owner", "")),
                reason or "无",
            )
            return jsonify({"ok": True, "msg": "已拒绝，用户可查看理由并重新提交"})
    return jsonify({"error": "未知操作"}), 400


def api_account_move(idx):
    """上移/下移账号：调整顺序模式下的打卡顺序。body: {"dir": -1|1}"""
    with _locks._file_lock:
        accounts = _accounts_data.load_accounts()
        if not 0 <= idx < len(accounts):
            return jsonify({"error": "账号不存在"}), 404
        data = _http._json_body()
        acc = accounts[idx]
        if _accounts_data._stale_idx_guard(acc, data, fail_closed=True):
            return jsonify({"error": "账号列表已变化，请刷新页面后重试"}), 409
        try:
            direction = int(data.get("dir", 0))
        except (TypeError, ValueError):
            return jsonify({"error": "无法移动"}), 400
        if direction not in (-1, 1):
            return jsonify({"error": "无法移动"}), 400
        if not db.move_account(acc["id"], direction):
            return jsonify({"error": "无法移动"}), 400
        db.audit(
            session.get("username") or "?",
            "account_move",
            _masking.mask_phone(acc.get("phone", "")),
            f"dir {direction}",
        )
        accounts = _accounts_data.load_accounts()
        return jsonify(
            {"ok": True, "accounts": [_accounts_data.mask_account(a, i) for i, a in enumerate(accounts)]}
        )


def register(app):
    """在本域注册十二条账号管理路由；endpoint 取函数名（url_for 依赖）。"""
    app.add_url_rule("/api/accounts", view_func=api_accounts)
    app.add_url_rule("/api/accounts/<int:idx>/detail", view_func=api_account_detail)
    app.add_url_rule("/api/accounts/<int:idx>/device-refresh",
                     view_func=api_account_device_refresh, methods=["POST"])
    app.add_url_rule("/api/accounts/<int:idx>/profile", view_func=api_account_profile,
                     methods=["PUT"])
    app.add_url_rule("/api/accounts", view_func=api_account_add, methods=["POST"])
    app.add_url_rule("/api/accounts/<int:idx>", view_func=api_account_update, methods=["PUT"])
    app.add_url_rule("/api/accounts/batch", view_func=api_accounts_batch, methods=["POST"])
    app.add_url_rule("/api/accounts/<int:idx>", view_func=api_account_delete, methods=["DELETE"])
    app.add_url_rule("/api/accounts/<int:idx>/restore", view_func=api_account_restore, methods=["POST"])
    app.add_url_rule("/api/accounts/<int:idx>/purge", view_func=api_account_purge, methods=["POST"])
    app.add_url_rule("/api/accounts/<int:idx>/review", view_func=api_account_review, methods=["POST"])
    app.add_url_rule("/api/accounts/<int:idx>/move", view_func=api_account_move, methods=["POST"])
