# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""账号管理域路由：管理员侧的账号列表/详情/增删改、批量操作、审核与排序。

**功能**
`GET /api/accounts` 列表（脱敏 + 今日签到状态 + 自选时间片 + 上一业务日执行体归属）；
`GET /api/accounts/<idx>/detail` 单号完整信息（列表外唯一回传明文口令与完整手机号的面，
带会话级限速与聚合读审计）；`POST /api/accounts` 添加（不填邮箱=管理员自有号直接生效，
填邮箱=归属该用户并回待审核，邮箱未注册则连带创建网站用户）；`PUT /api/accounts/<idx>`
编辑（改绑手机号一律回待审核）；`POST /api/accounts/batch` 批量
approve/reject/restore/delete/purge；`DELETE /api/accounts/<idx>` 软删；
`POST /api/accounts/<idx>/restore|purge|review|move` 恢复/彻底删除/审核/排序。

**归属**
`web.app.create_app` 的"账号管理"面（管理员专用，路径不在普通用户白名单）。工厂骨架、
跨域中间件（前置限速、登录守卫、CSRF、同源校验、安全响应头）与库访问层仍留在
`web/app.py`，本模块只提供十条视图。

**复用**
`register(app)` 供 `web.routes.register_all` 装配。`web.routes.high_risk_gate()` 取回
高危动作统一门禁（先二次鉴权、通过后才占高危额度），`admin_delete_limited()` 取回
"软删/不可逆清除"共用的同管理员窗口限速；`verify_fails()` / `verify_limits()` 是账号
验证冷却与配额的唯一取用点（与用户自助提交路径共用同一份账），`detail_limits()` 是详情
读取限速表；`read_audit_trace()` / `read_audit_denied_trace()` 是只读面聚合留痕。

**通信**
视图体不直接读 web.app 的模块级名字，一律经 `web.routes.appmod()` 按属性取——测试用
`mock.patch.object(web.app, …)` 打桩（db / logger / clock / mailer / mail_layout /
load_accounts / mask_account / validate_account / send_notification / ENV_FILE 等）必须
继续生效。对外：JSON 请求体经 `_json_body()`，落库走 `db`，告警经 `send_notification`，
账号容量闸门经 `_accounts_at_capacity()`；被 `web.app` 的 `register_all(app)` 一次接入。
"""
import json
import os
import sqlite3
import time

from flask import jsonify, session

from web.routes import admin_delete_limited as _admin_delete_limited
from web.routes import appmod as _appmod
from web.routes import detail_limits as _detail_limits
from web.routes import high_risk_gate as _high_risk_gate
from web.routes import read_audit_denied_trace as _read_audit_denied_trace
from web.routes import read_audit_trace as _read_audit_trace
from web.routes import verify_fails as _verify_fails
from web.routes import verify_limits as _verify_limits


def api_accounts():
    m = _appmod()
    accounts = m.load_accounts()
    # 附带今日签到状态（键脱敏与 /api/logs 一致）：前端账号表格状态图标不再依赖
    # 单独的日志轮询（logs/accounts tab 各自可见时才请求对应接口，减少无效轮询）
    # 状态来源：signin.py 写的结构化状态文件（status 码），前端做图标映射
    states = m.load_sign_state()
    # 用户自暂停账号：状态直接呈现"已取消"（⏹️）——无需等下次签到执行写状态文件，
    # 管理员面板立即反映
    for acc in accounts:
        if acc.get("user_paused"):
            states[acc.get("phone", "")] = {
                "status": m.STATUS_USER_CANCELLED,
                "message": "用户已取消签到",
            }
    # 自选时间（管理员查看每个用户选的片；slot_min → "HH:MM" + 首尾标记）
    prefs = {p: v["slot_min"] for p, v in m.db.get_time_prefs().items()}
    # "上次实领是谁签的"：口径是**最近一次有记录的业务日**（用户 2026-09-21 定，
    # "上次"的字面意即最近一次——周末停签后按"昨天"取会让整列空白到下一个工作日）。
    # **一次取全**（几百行账号不能逐账号查），角色解析与脱敏都在 _last_executors 里；
    # 库不存在/未初始化 → {}，于是每行 last_executor 为 null（新部署很正常）。
    last_exec = m._last_executors(m.db.claim_latest_day())
    sw = m._sign_window()
    _span_min = (sw[1][0] * 60 + sw[1][1]) - (sw[0][0] * 60 + sw[0][1])

    def _edge_mark(slot):
        if slot is None:
            return None
        if slot == 0:
            return "first"
        if slot >= _span_min - 5:
            return "last"
        return None

    return jsonify(
        {
            "ok": True,
            "accounts": [
                {
                    **m.mask_account(a, i),
                    "time_pref": m._slot_to_label(prefs.get(a["phone"])),
                    "time_pref_edge": _edge_mark(prefs.get(a["phone"])),
                    # 无记录必须是 null（前端靠它显示"—"，空对象/空串会让前端
                    # 误以为"有归属但字段缺失"）
                    "last_executor": last_exec.get(a["phone"]),
                }
                for i, a in enumerate(accounts)
            ],
            # states 值压成状态码字符串（前端图标映射用）
            "states": {
                m._mask_phone(k): (v.get("status", m.STATUS_PENDING) if isinstance(v, dict) else m.STATUS_PENDING)
                for k, v in states.items()
            },
            # 状态原因/计划（如"计划 06:42"），前端表格 title 展示
            "state_msgs": {
                m._mask_phone(k): (v.get("message", "") if isinstance(v, dict) else "")
                for k, v in states.items()
            },
            # 单次签到耗时秒数：表格状态 title 展示"耗时 xx s"；无记录为 None
            "state_durs": {
                m._mask_phone(k): (v.get("dur") if isinstance(v, dict) else None)
                for k, v in states.items()
            },
            "config_file": os.path.basename(m.DB_FILE),
        }
    )


def api_account_detail(idx):
    """账号完整信息（仅管理员；列表接口已脱敏，编辑/签到等操作按需取完整号）。

    这是列表之外唯一回传明文口令与完整手机号的面，且 idx 从 0 递增即可整库
    枚举——被窃的管理员会话读它零成本、零痕迹，故与日志导出同口径加会话级
    限速 + 聚合读审计（超限 429 不逐条留痕，防把审计表当打字机）。
    """
    m = _appmod()
    with m._rate_lock:
        m._ip_store_trim(_detail_limits(), m.DETAIL_WINDOW + m._IP_STORE_MAX_AGE)
    _cnt, _start, allowed = m._bump_window_count(
        _detail_limits(), (session.get("username") or "?")[:64], time.time(),
        m.DETAIL_WINDOW, limit=m.DETAIL_MAX,
    )
    if not allowed:
        # 文案不带阈值数字：这些数值对合法运维没有用处，却正好让攻击者贴着
        # 上限排布枚举节奏（信息分层口径，与其余 429 一致）
        _read_audit_denied_trace()("account_detail_denied")
        return jsonify({"error": "查看账号详情过于频繁，请稍后再试"}), 429
    accounts = m.load_accounts()
    if not 0 <= idx < len(accounts):
        return jsonify({"error": "账号不存在"}), 404
    _read_audit_trace()(
        "account_detail_read", m._mask_phone(str(accounts[idx].get("phone", ""))))
    return jsonify({"ok": True, "account": m.mask_account(accounts[idx], idx, masked=False)})


def api_account_add():
    """添加账号。

    - 不填邮箱：管理员自有账号（owner=admin，直接生效）
    - 填用户邮箱：账号归属该用户并进入待审核（仍需管理员点"通过"）；
      邮箱未注册时自动创建网站用户（生成临时密码，需告知用户）。
    """
    m = _appmod()
    # 操作级锁：手机号唯一/每人限 1/自动注册检查与写入原子（防并发重复添加与覆盖丢失）
    data = m._json_body()
    err, clean = m.validate_account(data, require_password=True)
    if err:
        return jsonify({"error": err}), 400
    # 预筛（同 api_my_account_add）：容量/手机号占用/内置邮箱先拦，注定失败的
    # 添加不再消耗易班网络验证。权威校验仍在下方写锁内。
    with m._file_lock:
        accounts_pre = m.load_accounts()
        max_accounts_pre = m.load_env_int(m.ENV_FILE, "YIBAN_MAX_ACCOUNTS", m.DEFAULT_MAX_ACCOUNTS)
        email_screen = str(data.get("email", "")).strip().lower()
        # 容量兜底：账号配额 = 活跃账号数（含裸账号）；
        # 本次将新增 1 个非删除账号——归属邮箱已持有活跃账号的重复添加
        # 不会新增（随后 400 拦截），不计增量，保持原错误优先级
        holds_live = email_screen and any(
            a.get("owner") == email_screen and not a["deleted"] for a in accounts_pre)
        if m._accounts_at_capacity(0 if holds_live else 1):
            m._notify_capacity_once("accounts", max_accounts_pre, "账号数量")
            return jsonify({"error": f"账号数量已达上限（{max_accounts_pre}），请联系管理员扩容"}), 403
        if m.find_account_index(accounts_pre, clean["phone"]) is not None:
            return jsonify({"error": f"手机号 {clean['phone']} 已存在"}), 400
        if email_screen and email_screen == m._builtin_admin_email():
            return jsonify({"error": "内置管理员邮箱不可注册"}), 400
        # 域名预筛：注定被拒的占位/一次性域名不消耗易班验证配额
        if email_screen:
            dom_err = m.email_domain_error(email_screen)
            if dom_err:
                return jsonify({"error": dom_err}), 400
    # 添加账号即时验证（管理员开启 YIBAN_ACCOUNT_VERIFY 后生效，验证失败当场打回）；
    # 验证尝试受每用户配额限制，认证失败另有按手机号冷却（与用户提交路径同口径）
    verify_async_job = False
    if m._account_verify_enabled():
        if m._verify_fail_cooldown_remaining(_verify_fails(), clean["phone"], time.time()) > 0:
            return jsonify({"error": m.VERIFY_FAIL_COOLDOWN_MSG}), 429
        _vuser = str(session.get("username", ""))
        if m.verify_async_enabled():
            # 异步：请求线程只**先扣配额**，外呼交给后台任务。
            # 待办队列满 → 503（此时账号尚未落库，拒绝无副作用）。
            if m._verify_queue_full():
                return jsonify({"error": m.VERIFY_BUSY_MSG}), 503, {"Retry-After": "2"}
            if not m._verify_attempt_allowed(_verify_limits(), _vuser):
                return jsonify({"error": "账号验证尝试过于频繁，请稍后再试"}), 429
            verify_async_job = True
        else:
            try:
                verify_err = m.run_verify_with_gate(clean, _vuser, _verify_limits())
            except m.VerifyGateBusy:
                return jsonify({"error": m.VERIFY_BUSY_MSG}), 503, {"Retry-After": "2"}
            except m.VerifyQuotaExceeded:
                return jsonify({"error": "账号验证尝试过于频繁，请稍后再试"}), 429
            if verify_err:
                fail_kind = m._record_verify_failure(_verify_fails(), clean["phone"], verify_err, time.time())
                m.db.audit(
                    session.get("username") or "?",
                    "account_add_verify_fail",
                    m._mask_phone(clean["phone"]),
                    f"验证未通过（{fail_kind}）",
                )
                return jsonify({"error": verify_err}), 400
    email = str(data.get("email", "")).strip().lower()
    initial_hash = None  # 锁外预计算（scrypt ~100ms 不阻塞其他请求）
    if email:
        initial = str(data.get("initial_password", ""))
        if initial:
            pw_err = m._password_policy_error(initial)
            if pw_err:
                return jsonify({"error": f"初始密码不符合要求：{pw_err}"}), 400
            initial_hash = m.generate_password_hash(initial, method=m.SCRYPT_METHOD)
    with m._file_lock:
        accounts = m.load_accounts()
        # 容量兜底：账号配额（活跃账号数含裸账号，防无限增长）；
        # 归属邮箱已持有活跃账号的重复添加不新增，不计增量（保持原错误优先级）
        max_accounts = m.load_env_int(m.ENV_FILE, "YIBAN_MAX_ACCOUNTS", m.DEFAULT_MAX_ACCOUNTS)
        holds_live = email and any(a.get("owner") == email and not a["deleted"] for a in accounts)
        if m._accounts_at_capacity(0 if holds_live else 1):
            m._notify_capacity_once("accounts", max_accounts, "账号数量")
            return jsonify({"error": f"账号数量已达上限（{max_accounts}），请联系管理员扩容"}), 403
        if m.find_account_index(accounts, clean["phone"]) is not None:
            return jsonify({"error": f"手机号 {clean['phone']} 已存在"}), 400

        if email:
            if len(email.split("@")[0]) > m.EMAIL_USER_MAX:
                return jsonify({"error": f"邮箱用户名部分过长（最多 {m.EMAIL_USER_MAX} 字符）"}), 400
            if not m.EMAIL_RE.match(email) or len(email) > 64:
                return jsonify({"error": "用户邮箱格式不正确"}), 400
            # 邮箱域名审查：与开放注册同规则，防自动注册路径绕过
            dom_err = m.email_domain_error(email)
            if dom_err:
                return jsonify({"error": dom_err}), 400
            # 内置管理员邮箱不可被自动注册占用
            if email.strip().lower() == m._builtin_admin_email().strip().lower():
                return jsonify({"error": "内置管理员邮箱不可注册"}), 400
            # 该用户已有账号（每人限 1 个）则拒绝（软删除的不占名额，与用户端一致）
            if any(a.get("owner") == email and not a.get("deleted") for a in accounts):
                return jsonify({"error": f"{email} 已有一个账号，无需重复添加"}), 400
            # 自动注册：邮箱未注册则创建网站用户（初始密码由管理员在表单中设置，不生成明文临时密码）
            if m.db.find_user(email) is None:
                if initial_hash is None:
                    return jsonify({"error": f"{email} 尚未注册，请填写「初始密码」为其创建首登密码"}), 400
                # 自动注册同样受用户容量（全部未删除用户）与注销冷却期约束
                max_users = m.load_env_int(m.ENV_FILE, "YIBAN_MAX_USERS", m.DEFAULT_MAX_USERS)
                if m._users_at_capacity():
                    m._notify_capacity_once("users", max_users, "注册人数")
                    return jsonify({"error": "注册人数已达上限，请联系管理员"}), 403
                du = m.db.find_user_any(email)
                if (
                    du is not None
                    and du.get("deleted")
                    and m._delete_grace_remaining(du.get("deleted_at", "")) > 0
                ):
                    return jsonify({"error": "该邮箱账号正在注销冷却期（7 天内可登录恢复）"}), 400
                try:
                    created = m.db.create_user(
                        email, initial_hash, "user",
                        m.clock.now().strftime("%Y-%m-%d %H:%M:%S"), 1,
                    )
                except sqlite3.IntegrityError:
                    return jsonify({"error": "该邮箱已注册"}), 400  # 并发注册兜底
                if not created:
                    return jsonify({"error": "该邮箱已注册"}), 400  # OR IGNORE 未实际创建
                m.logger.info("为邮箱 %s 自动注册用户（管理员设置初始密码）", m._mask_email(email))
            clean["owner"] = email
            clean["status"] = m.ACCOUNT_STATUS_PENDING
        else:
            clean["owner"] = "admin"
            clean["status"] = m.ACCOUNT_STATUS_ACTIVE
        try:
            new_id = m.db.add_account(clean)
        except m.db.DuplicatePhoneError:
            return jsonify({"error": f"手机号 {clean['phone']} 已存在"}), 400
        except m.db.DuplicateOwnerError:
            return jsonify({"error": "该用户已有一个账号，无需重复添加"}), 400
        except sqlite3.IntegrityError:
            return jsonify({"error": f"手机号 {clean['phone']} 已存在"}), 400  # 并发重复兜底
        m.db.audit(
            session.get("username") or "?",
            "account_add",
            m._mask_phone(clean["phone"]),
            f"归属 {m._mask_email(clean['owner'])} 状态 {clean['status']}",
        )
        accounts = m.load_accounts()  # 重读（含新行，返回前端列表）
    m.logger.info(
        "添加账号 %s（归属 %s，状态 %s）",
        m._mask_phone(clean["phone"]),
        m._mask_email(clean["owner"]),
        clean["status"],
    )
    resp = {
        "ok": True,
        "msg": "已添加，等待审核通过后参与签到",
        "accounts": [m.mask_account(a, i) for i, a in enumerate(accounts)],
    }
    if verify_async_job:
        # 账号已落库，随后由后台任务校验；响应**增补** job_id/status（不改既有字段）
        try:
            job_id, _ = m._start_verify_job(
                clean, str(session.get("username", "")), new_id,
                _verify_fails(), _verify_limits(),
            )
            resp["job_id"] = job_id
            resp["status"] = "verifying"
        except m.VerifyGateBusy:
            # 待办队列在扣配额与建任务之间被占满（竞态兜底）：账号已入库，
            # 不因此让请求失败——置为 rejected 并说明原因，由用户重试。
            m.logger.warning("校验任务队列已满，账号 %s 未建校验任务",
                           m._mask_phone(clean["phone"]))
    return jsonify(resp)


def api_account_update(idx):
    m = _appmod()
    with m._file_lock:
        accounts = m.load_accounts()
        if not 0 <= idx < len(accounts):
            return jsonify({"error": "账号不存在"}), 404
        old = accounts[idx]
        # 软删除账号禁止编辑（防编辑流程绕过软删除，恢复需走 restore 接口）
        if old.get("deleted"):
            return jsonify({"error": "账号已删除，请先恢复"}), 400
        data = m._json_body()
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
        if m._stale_idx_guard(old, guard_src, fail_closed=True):
            return jsonify({"error": "账号列表已变化，请刷新页面后重试"}), 409
        err, clean = m.validate_account(data, require_password=False)
        if err:
            return jsonify({"error": err}), 400
        # 手机号变更时检查冲突（排除自己）
        if (
            clean["phone"] != old.get("phone")
            and m.find_account_index(accounts, clean["phone"]) is not None
        ):
            return jsonify({"error": f"手机号 {clean['phone']} 已存在"}), 400
        # 改写他人易班凭据（填了新密码 / 改绑手机号）与"不可逆清除"同档：拿到被窃
        # 管理员会话的人一次 PUT 就能把某用户的账号换成自己的凭据——此后签到在攻击
        # 者侧完成、真用户被静默挤出，界面上看不出任何异常。只改备注/设备型号不算。
        # 走 _high_risk_gate：先二次鉴权、通过后才占高危额度（顺序即该函数的立身之本）。
        creds_written = bool(str(data.get("password", "")).strip()) or (
            clean["phone"] != old.get("phone"))
        if creds_written:
            denied = _high_risk_gate()(data, "改写他人易班凭据")
            if denied is not None:
                return denied
        # 密码留空 = 保持不变（密码明文永不下发前端）
        if not clean["password"]:
            clean["password"] = old.get("password", "")
        # 设备识别码：__clear__ = 显式清空该字段；留空 = 保持不变（表单不预填防误清空）
        if clean["phone_code"] == m.CLEAR_SENTINEL:
            clean.pop("phone_code", None)
        elif not clean["phone_code"]:
            clean["phone_code"] = old.get("phone_code", "")
        # 归属保持不变（管理员编辑不改变提交者）
        clean["owner"] = old.get("owner", "admin")
        # 改绑手机号一律回待审核重审——
        # 手机号即凭据主体，原审核结论绑定的是旧号，改绑后若维持 ACTIVE 就等于
        # "免审换号继续签到"。管理员改绑用户的号同样回 pending，由管理员再批；
        # 仅密码/识别码变更（phone 不变）维持原状态不变。
        rebind = clean["phone"] != old.get("phone")
        if rebind:
            clean["status"] = m.ACCOUNT_STATUS_PENDING
            # 回审即清除旧拒绝理由（与用户侧重新提交同口径，避免 pending 行带旧理由）
            clean["reject_reason"] = ""
        else:
            clean["status"] = old.get("status", m.ACCOUNT_STATUS_ACTIVE)
        try:
            result = m.db.update_account(
                old["id"],
                clean,
                expect_snapshot=snapshot if isinstance(snapshot, dict) else None,
            )
        except m.db.DuplicatePhoneError:
            return jsonify({"error": f"手机号 {clean['phone']} 已存在"}), 400
        except m.db.DuplicateOwnerError:
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
            m.db.clear_time_pref(old.get("phone", ""))
        # 凭据变更（改密码/改绑手机号）才清除熔断暂停，立即恢复签到；
        # 仅改备注/状态等不动熔断计数（防任意编辑把 fail_days 清零、熔断永不跳闸）
        m.clear_fuse_on_cred_change(old.get("phone", ""), old.get("password", ""), clean)
        m.db.audit(
            session.get("username") or "?",
            "account_update",
            m._mask_phone(clean["phone"]),
            ("编辑账号" + (" 改绑回审" if rebind else "")
             + (" 改写凭据" if creds_written else "")),
        )
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
                    m.mailer.send_user(
                        _owner,
                        "【易班签到】您的易班账号信息被管理员修改",
                        m.mail_layout.Mail(
                            summary=f"您在本站提交的易班账号 {m._mask_phone(clean['phone'])}"
                                    " 刚刚被管理员修改。",
                            items=_what,
                            fields=[("操作者", m._mask_email(
                                str(session.get("username") or "?")[:64]))],
                            advice=["如非您本人申请，请立即联系管理员核实"],
                            level="urgent",
                        ),
                    )
                except Exception as e:
                    m.logger.warning("账号凭据变更通知发送失败（不影响已完成的编辑）: %s", e)
        accounts = m.load_accounts()
        m.logger.info("编辑账号 %s", m._mask_phone(clean["phone"]))
        return jsonify(
            {"ok": True, "accounts": [m.mask_account(a, i) for i, a in enumerate(accounts)]}
        )


def api_accounts_batch():
    """批量操作账号（批量多选功能）：approve/reject 审核，purge 彻底删除。

    body: {"action": ..., "ids": [...], "reason": "批量拒绝理由"}
    整体事务：失败全部回滚，无效项软跳过。

    purge 与用户侧 /api/users/batch(delete)、/api/users/deleted/purge 属同一类
    "不可逆清除"：一个请求最多 BATCH_OP_LIMIT 条，漏了门禁即为数十秒内把全部
    易班凭据不可逆清零且无人知晓。故 purge 要求二次鉴权 + 同管理员窗口限速
    （429）。delete（软删）虽可逆（7 天宽限 + 409 防错位），但它立即停止该用户
    代签且受害者无法自助恢复，同样接入该高危限速并逐次告警、但**不要求二次
    口令**（可逆操作加口令只增误伤）。approve/reject/restore 可逆且有防错位兜底，
    维持无门禁。
    """
    m = _appmod()
    # 参数校验与高危门禁刻意留在 _file_lock 之外（与三处高危删除同口径）：
    # scrypt 口令校验单次数百毫秒，放进全局文件锁里会让一次鉴权阻塞全进程的
    # 账号读写（该锁同时护着 JSON 与 SQLite 侧的读-改-写）。
    data = m._json_body()
    action = data.get("action")
    ids = data.get("ids") or []
    if action not in ("approve", "reject", "purge", "restore", "delete"):
        return jsonify({"error": "未知操作"}), 400
    if not isinstance(ids, list) or not ids:
        return jsonify({"error": "请选择要操作的账号"}), 400
    # 单次批量上限：没有它，一个请求即可清空全部账号；与
    # /api/users/deleted/purge 共用 BATCH_OP_LIMIT
    if len(ids) > m.BATCH_OP_LIMIT:
        return jsonify({"error": f"单次批量操作最多 {m.BATCH_OP_LIMIT} 个账号"}), 400
    reason = str(data.get("reason", "")).strip()[:100]
    if action == "reject" and not reason:
        return jsonify({"error": "批量拒绝需要填写理由"}), 400
    if action == "purge":
        # 统一走 _high_risk_gate（先验口令，通过了才占高危额度）；
        # 429 文案与用户侧批量删除一致，运维只需记一句话
        gate = _high_risk_gate()(
            data, "批量彻底删除账号", limit_msg="删除操作过于频繁，请稍后再试")
        if gate:
            return gate
    # 软删也占用同一份高危额度：它虽可逆（7 天内可恢复），但立即让该用户当天起
    # 停止代签，且**受害者无法自助恢复**（/api/my-accounts/<idx>/restore 对管理员
    # 删除的行返回 403），因此被盗的注册管理员会话可用几十次调用在数秒内静默让
    # 全站停签——与"删数据 / 拆报警器是同一条链" 同风险。这里**仍然不要求二次
    # 口令**（可逆不加口令，加了口令只增误伤），只限制速率并逐次告警（告警在
    # ops 落库与审计之后发送，见下方）。
    if action == "delete" and _admin_delete_limited()():
        return jsonify({"error": "删除操作过于频繁，请稍后再试"}), 429
    with m._file_lock:
        accounts = m.load_accounts()
        # idx 寻址防错位：客户端随 ids 携带对齐的 phones 数组，与服务端当前列表
        # 逐一比对，不一致整体拒绝（409 引导刷新）。另注意 isinstance(True, int)
        # 为真，ids 里的 JSON true 会被当作索引 1，故用 type(i) is int 严格判定。
        valid = sorted(
            {i for i in ids if type(i) is int and 0 <= i < len(accounts)}
        )
        if not valid:
            return jsonify({"error": "所选账号不存在"}), 404
        phones_in = data.get("phones")
        if isinstance(phones_in, list) and len(phones_in) == len(ids):
            expect = {
                i: str(phones_in[k]).strip()
                for k, i in enumerate(ids)
                if type(i) is int
            }
            # 双侧 _mask_phone 归一（出站为脱敏号，见 _stale_idx_guard 注释）
            if any(
                i in expect
                and m._mask_phone(expect[i]) != m._mask_phone(str(accounts[i].get("phone", "")))
                for i in valid
            ):
                return jsonify({"error": "账号列表已变化，请刷新页面后重试"}), 409

        ops = []
        batch_targets = []  # 审计留目标清单（脱敏截断）
        purge_targets = []  # 高危操作（物理删除）即时告警汇总
        soft_delete_targets = []  # 软删即时告警汇总
        reject_notify_owners = {}  # 批量拒绝每户一封
        # 内存中跟踪每个 owner 当前是否有未删除账号，用于恢复防呆
        live_owners = {
            a.get("owner", "")
            for a in accounts
            if a.get("owner") and not a.get("deleted")
        }
        for i in valid:
            acc = accounts[i]
            if action == "approve":
                # 软删除账号不可被审核通过（deleted 账号不参与审核流转）
                if not acc.get("deleted") and acc.get("status") in (
                    m.ACCOUNT_STATUS_PENDING,
                    m.ACCOUNT_STATUS_REJECTED,
                ):
                    ops.append(("update_status", acc["id"], m.ACCOUNT_STATUS_ACTIVE, ""))
            elif action == "reject":
                if acc.get("status") in (m.ACCOUNT_STATUS_PENDING, m.ACCOUNT_STATUS_REJECTED):
                    ops.append(("update_status", acc["id"], m.ACCOUNT_STATUS_REJECTED, reason))
                    if acc.get("owner"):
                        reject_notify_owners.setdefault(
                            acc["owner"], []
                        ).append(m._mask_phone(str(acc.get("phone", ""))))
            elif action == "purge":
                # 仅允许彻底删除「已软删除」账号（与单个彻底删除一致，防误删正常账号）
                if acc.get("deleted"):
                    ops.append(("purge", acc["id"]))
                    purge_targets.append(m._mask_phone(str(acc.get("phone", ""))))
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
                    ("set_deleted", acc["id"], 1, m.clock.now().strftime("%Y-%m-%d %H:%M:%S"))
                )
                soft_delete_targets.append(m._mask_phone(str(acc.get("phone", ""))))
            batch_targets.append(acc.get("phone", ""))
        done = len(ops)
        if ops:
            try:
                m.db.batch_account_ops(ops)
                if purge_targets:
                    # 高危操作即时告警（不等每日审计体检）
                    m.send_notification(
                        "高危管理操作告警",
                        m._change_mail(
                            f"批量彻底删除账号 {len(purge_targets)} 个。",
                            detail=[("目标", "、".join(purge_targets[:20]))],
                            advice=["物理删除不可恢复；操作前应有当日备份"],
                        ),
                        urgent=True,
                    )
            except m.db.DuplicateOwnerError:
                m.db.audit(
                    session.get("username") or "?",
                    "account_batch",
                    action,
                    "失败，已回滚（该用户已有一个账号）",
                )
                return jsonify({"error": "批量操作失败，已全部回滚（该用户已有一个账号）"}), 400
            except Exception as e:
                m.logger.error("批量%s账号失败: %s（已回滚）", action, e)
                m.db.audit(
                    session.get("username") or "?",
                    "account_batch",
                    action,
                    "失败，已回滚",
                )
                return jsonify({"error": "批量操作失败，已全部回滚"}), 500
        if action == "reject" and reject_notify_owners:
            # 批量拒绝每户一封、同样文案（批量拒绝必填理由，无空理由分支）。
            # 刻意放在 batch_account_ops 成功之后——回滚路径已提前 return，不会
            # 出现"状态没变先收拒信"。
            for _owner, _phones in sorted(reject_notify_owners.items()):
                m.mailer.send_user(
                    _owner,
                    "【易班签到】您提交的账号未通过审核",
                    m._review_reject_mail(_phones, reason),
                )
        m.db.audit(
            session.get("username") or "?",
            "account_batch",
            action,
            # 批量操作留目标清单（脱敏截断），破坏事后可从审计还原"动了谁"
            (f"处理 {done} 个: " + ",".join(
                m._mask_phone(str(p)) for p in (batch_targets or [])[:20]
            ))[:200],
        )
        if action == "delete" and soft_delete_targets:
            # 软删即时告警刻意排在 db.audit 之后、
            # 返回之前——先把证据落进审计链（HMAC 链 + 库外锚点），再尝试外发，
            # 外发失败不影响留痕（与 api_account_purge 同顺序、同理由）。
            # 标题沿用「高危管理操作告警」：send_notification 的邮件节流按**标题**
            # 计窗（见 _mail_alert_due），因此被盗会话快速连删不会刷爆 SMTP 额度、
            # 合法运维的批量清理也只留一封邮件；webhook 仍逐条实时推送（告警实时性
            # 由 webhook 保证），两头的语义都保住。
            m.send_notification(
                "高危管理操作告警",
                m._change_mail(
                    f"批量删除账号（软删）{len(soft_delete_targets)} 个。",
                    detail=[("目标", "、".join(soft_delete_targets[:20]))],
                    advice=[f"{m.DELETED_RETENTION_DAYS} 天内可在待删除列表恢复"],
                ),
                urgent=True,
            )
        accounts = m.load_accounts()
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
                "accounts": [m.mask_account(a, i) for i, a in enumerate(accounts)],
            }
        )


def api_account_delete(idx):
    """删除账号（软删除）：进入待删除状态，保留期内可恢复，超期自动彻底清除。

    软删占用高危额度并即时告警——理由见 /api/accounts/batch 的 action=="delete"
    分支注释（软删可逆但立即停签、受害者无法自助恢复，被盗注册管理员会话可
    借此静默让全站停签），但不要求二次口令（可逆操作不加口令）。
    """
    m = _appmod()
    # 门禁刻意留在 _file_lock 之外：_admin_delete_limited 只做内存计数与判速，
    # 放进全局文件锁会白占锁（与批量 purge 的门禁位置同口径）。
    if _admin_delete_limited()():
        return jsonify({"error": "删除操作过于频繁，请稍后再试"}), 429
    with m._file_lock:
        accounts = m.load_accounts()
        if not 0 <= idx < len(accounts):
            return jsonify({"error": "账号不存在"}), 404
        acc = accounts[idx]
        if m._stale_idx_guard(acc, m._json_body()):
            return jsonify({"error": "账号列表已变化，请刷新页面后重试"}), 409
        m.db.set_account_deleted(
            acc["id"], 1, m.clock.now().strftime("%Y-%m-%d %H:%M:%S"),
            deleted_by="admin",
        )
        m.db.audit(
            session.get("username") or "?",
            "account_delete",
            m._mask_phone(acc.get("phone", "")),
            "软删除",
        )
        # 先落审计再外发：外发失败不影响留痕（与 api_account_purge 同顺序）。
        # 标题沿用「高危管理操作告警」以共享邮件节流窗口（见批量分支注释）。
        m.send_notification(
            "高危管理操作告警",
            m._change_mail(
                "删除账号（软删）。",
                detail=[("目标", m._mask_phone(str(acc.get("phone", ""))))],
                advice=[f"{m.DELETED_RETENTION_DAYS} 天内可在待删除列表恢复"],
            ),
            urgent=True,
        )
        accounts = m.load_accounts()
        m.logger.info(
            "软删除账号 %s（%s 天内可恢复）", m._mask_phone(acc.get("phone", "")), m.DELETED_RETENTION_DAYS
        )
        return jsonify(
            {
                "ok": True,
                "msg": f"已删除「{acc.get('name', '')}」，{m.DELETED_RETENTION_DAYS} 天内可在待删除列表恢复",
                "accounts": [m.mask_account(a, i) for i, a in enumerate(accounts)],
            }
        )


def api_account_restore(idx):
    """恢复待删除账号：撤销软删除，回到删除前状态。"""
    m = _appmod()
    with m._file_lock:
        accounts = m.load_accounts()
        if not 0 <= idx < len(accounts):
            return jsonify({"error": "账号不存在"}), 404
        acc = accounts[idx]
        if m._stale_idx_guard(acc, m._json_body()):
            return jsonify({"error": "账号列表已变化，请刷新页面后重试"}), 409
        if not acc.get("deleted"):
            return jsonify({"error": "该账号不在待删除状态"}), 400
        # 防呆：归属用户名下已有其他未删除账号则拒绝恢复（每人限 1 个，防恢复后重复）
        if m._owner_has_other_live(accounts, acc):
            return jsonify(
                {"error": "该用户已有生效账号，无法恢复（每人限 1 个）"}
            ), 400
        m.db.set_account_deleted(acc["id"], 0)
        m.db.audit(
            session.get("username") or "?",
            "account_restore",
            m._mask_phone(acc.get("phone", "")),
            "撤销软删除",
        )
        accounts = m.load_accounts()
        m.logger.info("恢复账号 %s", m._mask_phone(acc.get("phone", "")))
        return jsonify(
            {
                "ok": True,
                "msg": f"已恢复「{acc.get('name', '')}」",
                "accounts": [m.mask_account(a, i) for i, a in enumerate(accounts)],
            }
        )


def api_account_purge(idx):
    """彻底删除待删除账号：立即物理清除，不可恢复。

    单条物理清除与批量 purge 同口径：二次鉴权 + 同管理员窗口限速（429），
    成功后发一条 urgent 告警；缺任一项都等于给被盗管理员会话留一条安静的
    清库通道（不要求确认口令、不受删除冷却约束、成功零外发）。
    """
    m = _appmod()
    # 门禁放在 _file_lock 之外（同 api_accounts_batch 与三处高危删除）：
    # 口令校验耗时数百毫秒，放进全局锁里会凭一次尝试卡住全进程账号读写
    data = m._json_body()
    gate = _high_risk_gate()(
        data, "彻底删除账号", limit_msg="删除操作过于频繁，请稍后再试")
    if gate:
        return gate
    with m._file_lock:
        accounts = m.load_accounts()
        if not 0 <= idx < len(accounts):
            return jsonify({"error": "账号不存在"}), 404
        acc = accounts[idx]
        if m._stale_idx_guard(acc, data):
            return jsonify({"error": "账号列表已变化，请刷新页面后重试"}), 409
        if not acc.get("deleted"):
            return jsonify({"error": "该账号不在待删除状态"}), 400
        m.db.purge_account(acc["id"])
        m.db.audit(
            session.get("username") or "?",
            "account_purge",
            m._mask_phone(acc.get("phone", "")),
            "彻底删除",
        )
        # 即时告警刻意排在 db.audit 之后、返回之前——先把证据落进
        # 审计链（HMAC 哈希链 + 库外锚点），再尝试外发，外发失败不影响留痕。
        # 标题与批量 purge / 用户侧清除完全相同：send_notification 的邮件节流
        # 按标题计窗（_mail_alert_due），同标题才共享窗口——被盗会话快速连删
        # 不会被刷爆 SMTP 额度，合法运维的批量清理也只留一封，两头的语义都保住。
        m.send_notification(
            "高危管理操作告警",
            m._change_mail(
                "彻底删除账号。",
                detail=[("目标", m._mask_phone(str(acc.get("phone", ""))))],
                advice=["物理删除不可恢复"],
            ),
            urgent=True,
        )
        accounts = m.load_accounts()
        m.logger.info("彻底删除账号 %s", m._mask_phone(acc.get("phone", "")))
        return jsonify(
            {
                "ok": True,
                "msg": f"已彻底删除「{acc.get('name', '')}」",
                "accounts": [m.mask_account(a, i) for i, a in enumerate(accounts)],
            }
        )


def api_account_review(idx):
    """审核普通用户提交的账号：
    approve=生效参与定时签到；reject=标记拒绝并附理由（用户可编辑后重新提交）。
    """
    m = _appmod()
    with m._file_lock:
        accounts = m.load_accounts()
        if not 0 <= idx < len(accounts):
            return jsonify({"error": "账号不存在"}), 404
        data = m._json_body()
        action = data.get("action")
        acc = accounts[idx]
        if m._stale_idx_guard(acc, data):
            return jsonify({"error": "账号列表已变化，请刷新页面后重试"}), 409
        if action == "approve":
            # 软删除账号不可被审核通过（deleted 账号不参与审核流转）
            if acc.get("deleted") or acc.get("status") not in (m.ACCOUNT_STATUS_PENDING, m.ACCOUNT_STATUS_REJECTED):
                return jsonify({"error": "该账号无需审核"}), 400
            # 容量闸门：通过审核 = 让这一行开始产生签到负载。未通过审核的行不计入
            # 账号容量（见 _capacity_account_count），故此处是本口径下唯一的把关点——
            # 不放这道门就等于"提交时受限、审批时任意越界"，容量上限失去意义。
            if m._accounts_at_capacity(1):
                return jsonify({
                    "error": "账号数量已达上限，无法通过审核。请清理不用的账号或提高账号容量上限后重试"
                }), 403
            m.db.update_account_status(acc["id"], m.ACCOUNT_STATUS_ACTIVE, reject_reason="")
            m.db.audit(
                session.get("username") or "?",
                "account_review",
                m._mask_phone(acc.get("phone", "")),
                "approve",
            )
            m.logger.info("审核通过账号 %s（提交者 %s）", m._mask_phone(acc.get("phone", "")), m._mask_email(acc.get("owner", "")))
            # 回显脱敏（与列表口径一致，防响应混入完整 PII；管理员详情页可取完整号）
            return jsonify({"ok": True, "msg": f"已通过 {m._mask_phone(acc.get('phone', ''))}，将参与定时签到"})
        if action == "reject":
            if acc.get("status") not in (m.ACCOUNT_STATUS_PENDING, m.ACCOUNT_STATUS_REJECTED):
                return jsonify({"error": "该账号无需拒绝"}), 400
            # 理由清洗：换行/控制字符 → 空格（防日志注入伪造日志行）
            reason = (
                str(data.get("reason", ""))
                .strip()[:100]
                .replace("\r", " ")
                .replace("\n", " ")
            )
            m.db.update_account_status(acc["id"], m.ACCOUNT_STATUS_REJECTED, reason)
            m.db.audit(
                session.get("username") or "?",
                "account_review",
                m._mask_phone(acc.get("phone", "")),
                "reject" + (f" {reason[:60]}" if reason else ""),
            )
            # 拒绝必须主动触达提交者：理由只挂「我的账号」页属被动知情，提交者
            # 不回访即永远不知情；审核通过不发（登录即见生效，节约额度）。
            # send_user 未启用/无收件人静默跳过、失败仅记日志，不影响审核流；
            # 绕过 mail_notify 开关与「本人知情权」口径一致。
            _owner = acc.get("owner", "")
            if _owner:
                m.mailer.send_user(
                    _owner,
                    "【易班签到】您提交的账号未通过审核",
                    m._review_reject_mail(
                        [m._mask_phone(str(acc.get("phone", "")))], reason
                    ),
                )
            m.logger.info(
                "拒绝账号 %s（提交者 %s，理由: %s）",
                m._mask_phone(acc.get("phone", "")),
                m._mask_email(acc.get("owner", "")),
                reason or "无",
            )
            return jsonify({"ok": True, "msg": "已拒绝，用户可查看理由并重新提交"})
    return jsonify({"error": "未知操作"}), 400


def api_account_move(idx):
    """上移/下移账号：调整顺序模式下的打卡顺序。body: {"dir": -1|1}"""
    m = _appmod()
    with m._file_lock:
        accounts = m.load_accounts()
        if not 0 <= idx < len(accounts):
            return jsonify({"error": "账号不存在"}), 404
        data = m._json_body()
        acc = accounts[idx]
        if m._stale_idx_guard(acc, data):
            return jsonify({"error": "账号列表已变化，请刷新页面后重试"}), 409
        try:
            direction = int(data.get("dir", 0))
        except (TypeError, ValueError):
            return jsonify({"error": "无法移动"}), 400
        if direction not in (-1, 1):
            return jsonify({"error": "无法移动"}), 400
        if not m.db.move_account(acc["id"], direction):
            return jsonify({"error": "无法移动"}), 400
        m.db.audit(
            session.get("username") or "?",
            "account_move",
            m._mask_phone(acc.get("phone", "")),
            f"dir {direction}",
        )
        accounts = m.load_accounts()
        return jsonify(
            {"ok": True, "accounts": [m.mask_account(a, i) for i, a in enumerate(accounts)]}
        )


def register(app):
    """在本域注册十条账号管理路由；endpoint 取函数名（url_for 依赖）。"""
    app.add_url_rule("/api/accounts", view_func=api_accounts)
    app.add_url_rule("/api/accounts/<int:idx>/detail", view_func=api_account_detail)
    app.add_url_rule("/api/accounts", view_func=api_account_add, methods=["POST"])
    app.add_url_rule("/api/accounts/<int:idx>", view_func=api_account_update, methods=["PUT"])
    app.add_url_rule("/api/accounts/batch", view_func=api_accounts_batch, methods=["POST"])
    app.add_url_rule("/api/accounts/<int:idx>", view_func=api_account_delete, methods=["DELETE"])
    app.add_url_rule("/api/accounts/<int:idx>/restore", view_func=api_account_restore, methods=["POST"])
    app.add_url_rule("/api/accounts/<int:idx>/purge", view_func=api_account_purge, methods=["POST"])
    app.add_url_rule("/api/accounts/<int:idx>/review", view_func=api_account_review, methods=["POST"])
    app.add_url_rule("/api/accounts/<int:idx>/move", view_func=api_account_move, methods=["POST"])
