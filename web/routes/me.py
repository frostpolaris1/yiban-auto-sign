# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""个人域路由：自助改密、自助注销、冷静期恢复、身份信息、邮箱通知开关。

**功能**
`POST /api/me/password` 修改本人密码（内置管理员写 .env 哈希，注册用户写 users 表）；
`POST /api/me/delete` 自助注销（软删除 + 7 天宽限期）；`POST /api/me/restore` 冷静期内
恢复账号并直接建立会话；`GET /api/me` 返回身份、角色、调度展示项与 CSRF token；
`GET` / `PUT /api/my-mail-notify` 读写本人签到失败邮件开关。

**归属**
`web.app.create_app` 的"个人域"。工厂骨架与跨域中间件（前置限速、登录守卫、CSRF、
同源校验、安全响应头）仍留在 `web/app.py`，本模块只提供六条视图。

**复用**
`register(app)` 供 `web.routes.register_all` 装配；`get_csrf_token()` 惰性生成会话内的
CSRF token，供身份接口下发给前端（校验侧仍在 web/app.py 的 check_csrf）。

**通信**
视图体不直接读 web.app 的模块级名字，一律经 `web.routes.appmod()` 按属性取——测试用
`mock.patch.object(web.app, …)` 打桩（ENV_FILE / verify_admin / check_password_hash /
generate_password_hash / write_env_batch / _env_write_lock / _client_ip / db 等）必须
继续生效。登录失败计数表经 `web.routes.login_fails()` 取回，与登录/注册路由共用同一份账
（安全语义依赖同一份计数）；恢复接口的每 IP 聚合窗口挂在 app.extensions，保每 app 实例一份。
"""
import secrets
import sqlite3
import time
from datetime import timedelta

from flask import current_app, jsonify, session

from web.routes import appmod as _appmod
from web.routes import login_fails as _login_fails


def _restore_fail_rate():
    """账号恢复的每 IP 聚合失败窗口（每 app 实例一份；create_app 登记在 extensions）。"""
    return current_app.extensions["yiban_restore_fail_rate"]


def get_csrf_token():
    """惰性生成并返回当前会话的 CSRF token。"""
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_hex(32)
    return session["csrf_token"]


def api_me_password():
    """所有用户自助修改自己的密码（账号不可修改）。

    内置管理员（.env）验证当前口令后写入新哈希（YIBAN_ADMIN_PASSWORD_HASH，scrypt），
    并清理旧明文；注册用户（含提升的管理员）验证当前密码后更新 users 表密码哈希。
    失败计数与登录共用限速：达阈值（LOGIN_MAX_FAILS）锁定，超阈值返回 429；
    旧会话失效由 pw_version 递增实现（_effective_role 实时校验）。
    """
    m = _appmod()
    data = m._json_body()
    old_password = str(data.get("old_password", ""))
    new_password = str(data.get("new_password", ""))
    if new_password != str(data.get("confirm_password", "")):
        return jsonify({"error": "两次输入的新密码不一致"}), 400
    # 主管理员（内置 .env 管理员）口令走 12 位三类提档策略；注册用户维持原口径
    pw_err = (
        m._admin_password_policy_error(new_password)
        if m._is_builtin_admin_session()
        else m._password_policy_error(new_password)
    )
    if pw_err:
        return jsonify({"error": f"新密码不符合要求：{pw_err}"}), 400
    username = session.get("username", "")
    ip = m._client_ip()
    now = time.time()
    # 失败计数键与登录一致：按 (IP, 用户名) 组合
    fail_key = (ip, username.strip().lower())
    with m._rate_lock:
        _fails, lock_until, _ = _login_fails().get(fail_key, (0, 0, 0))
        if now < lock_until:
            # 不显示剩余秒数：避免向用户暴露锁定窗口参数（信息分层）
            return jsonify({"error": "尝试次数过多，请稍后再试"}), 429

    def _handle_failed_login():
        """当前密码校验失败：递增失败计数，达阈值锁定（与 api_login 一致）。

        告警排在锁定之前：两个阈值同值时这一刻既要告警也要锁定，按"先锁定即 return"
        的顺序会让告警永不执行。
        """
        nfails = m._bump_login_failure(_login_fails(), fail_key, now)
        lock_now = nfails >= m.LOGIN_MAX_FAILS
        if lock_now:
            with m._rate_lock:
                _login_fails()[fail_key] = (0, now + m.LOGIN_LOCK_SECONDS, now)
            m.logger.warning("改密失败次数过多，IP %s 锁定 %s 秒", m.db.hash_ip(ip), m.LOGIN_LOCK_SECONDS)
        if nfails == m.LOGIN_FAIL_NOTIFY:
            m.send_notification(
                "改密失败告警",
                m.mail_layout.Mail(
                    summary=f"IP {m._nl_safe(ip)} 连续 {nfails} 次修改密码失败。",
                    fields=[("用户名", m._nl_safe(username))],
                    advice=["如非本人操作，请检查是否有人尝试暴力破解"],
                    level="warn",
                ),
            )
        if lock_now:
            # 不暴露锁定时长分钟数（信息分层）
            return jsonify({"error": "密码错误次数过多，请稍后再试"}), 429
        return jsonify({"error": "当前密码不正确"}), 400

    # 内置管理员：验证 .env 当前口令后更新（同邮箱注册用户不进入此分支）
    if m._is_builtin_admin_session():
        if not m.verify_admin(username, old_password):
            return _handle_failed_login()
        # 哈希在锁外先算（scrypt 不该占住写锁）；现值读取必须与落盘同处一把
        # 写锁临界区：写锁同线程可重入（write_env_batch 内部的再次加锁直接
        # 放行），否则读在锁外时两个并发改密都会读到旧版本并写出同一个
        # 递增值——一次递增被吞，本应随版本失效的旧会话继续有效。
        new_hash = m.generate_password_hash(new_password, method=m.SCRYPT_METHOD)
        with m._env_write_lock(m.ENV_FILE):
            m.write_env_batch(
                m.ENV_FILE,
                {
                    "YIBAN_ADMIN_PASSWORD_HASH": new_hash,
                    "YIBAN_ADMIN_PASSWORD": "",  # 清理旧明文口令，改由哈希校验
                    "YIBAN_ADMIN_PW_VERSION": str(m.load_env_int(m.ENV_FILE, "YIBAN_ADMIN_PW_VERSION", 1) + 1),
                    # 会话凭据与版本号同一次原子写换发（改口令 = 假定会话已失窃；
                    # 本会话随版本号递增一起失效，故无需回填 session）
                    m.ADMIN_SID_ENV_KEY: m._new_admin_sid(),
                },
            )
        with m._rate_lock:
            _login_fails().pop(fail_key, None)
        # 审计留痕：主管理员改密是最高权限的关键事件，注册用户分支有审计而本分支
        # 同样关键，缺席则防篡改链上无法追责
        m.db.audit(
            username or "builtin-admin",
            "admin_password",
            m._mask_email(username) if username else "-",
            "内置管理员自助改密",
        )
        # 主管理员即时告警——改密是「被盗号接管」最强信号
        #（本人会话随 PW_VERSION 失效，攻击者以新密重登），须与注册用户分支同口径接通告警渠道
        m.send_notification(
            "账号安全事件告警",
            m.mail_layout.Mail(
                summary=f"内置主管理员（{m._mask_email(username) if username else 'builtin-admin'}）"
                        "密码已通过自助改密修改。",
                advice=["如非本人操作，请立即按 README「主管理员权限追回」流程处理"
                        "（SSH 重写 YIBAN_ADMIN_PASSWORD + PW_VERSION 递增）"],
                level="urgent",
            ),
            urgent=True,
        )
        m.logger.info("内置管理员密码已更新")
        return jsonify({"ok": True, "msg": "密码已更新，下次登录使用新密码"})
    # 注册用户（含提升的管理员）：db 单行更新（事务内，防并发覆盖）
    with m._file_lock:
        u = m.db.find_user(username.strip().lower())
        if u is not None:
            if not m.check_password_hash(u.get("password_hash", ""), old_password):
                return _handle_failed_login()
            m.db.update_user(
                u["email"],
                {
                    "password_hash": m.generate_password_hash(new_password, method=m.SCRYPT_METHOD),
                    "pw_version": u.get("pw_version", 1) + 1,  # 旧会话随之失效
                },
            )
            m.db.audit(username, "user_password", username, "自助改密")
            # 自助改密轮换 sid——当前会话保持有效（同步 session），
            # 被窃取的 cookie 副本随旧 sid 失效
            new_sid = secrets.token_hex(16)
            m.db.set_user_sid(username.strip().lower(), new_sid)
            session["sid"] = new_sid
            with m._rate_lock:
                _login_fails().pop(fail_key, None)
            # 改密是核心安全事件——本人邮件（直接 send_user 绕过
            # mail_notify 开关：开关本身可被攻击者关闭）+ 管理员告警（被盗号
            # 改密时的可感知信号，审计之外的第一时间渠道）
            m.mailer.send_user(
                username,
                "【易班签到】您的账号密码已被修改",
                m.mail_layout.Mail(
                    summary="您的账号密码刚刚通过自助改密被修改。",
                    advice=["如非本人操作，请立即联系管理员重置密码并检查账号安全"],
                    level="urgent",
                ),
            )
            m.send_notification(
                "账号安全事件告警",
                m.mail_layout.Mail(
                    summary=f"用户 {m._mask_email(username)} 自助修改密码。",
                ),
            )
            m.logger.info("用户 %s 已修改自己的密码", m._mask_email(username))
            return jsonify({"ok": True, "msg": "密码已更新，下次登录使用新密码"})
    return jsonify({"error": "用户不存在"}), 404


def api_me_delete():
    """用户自助注销（软删除 + 7 天宽限期，数据库 v5）。

    安全设计（docs/design/plan-frontend-user-deregistration.md）：
    - 登录要求 + CSRF：全局写请求校验（X-CSRF-Token）自动覆盖；
    - 防 IDOR：只从 session 取当前用户，请求体任何目标参数一律忽略；
    - 密码确认：防"离开电脑被恶意页面直接注销"；
    - 防批量：每用户 60s 1 次 + 每 IP 60s 5 次（user_delete_requests 表计数，
      成功进入注销流程才记录；试密码已由登录失败表限速，两层防护不重叠），
      超限 429 且不暴露冷却秒数；
    - 管理员保护：内置管理员（.env 主管理员）不可注销；最后一个注册管理员
      不可注销（is_last_registered_admin，防失去全部管理入口）；
    - 审计：user_self_delete_request / user_self_delete_confirm，detail 脱敏；
    - 注销即清会话（软删除后 find_user 查无此人，_effective_role 同步失效）。
    """
    m = _appmod()
    if not session.get("auth") or not session.get("username"):
        return jsonify({"error": "未登录"}), 401
    data = m._json_body()
    password = str(data.get("password", ""))
    if not password:
        return jsonify({"error": "请输入当前密码"}), 400
    username = session.get("username", "")
    email = username.strip().lower()
    ip = m._client_ip()
    now = time.time()
    # 防批量冷却：计数基于 user_delete_requests 表（kind=delete，恢复记录不占
    # 注销冷却，允许"恢复后立即再注销"）
    since_ts = (m.clock.now() - timedelta(seconds=m.DELETE_COOLDOWN_SEC)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    if (
        m.db.count_user_delete_requests(
            username=email, since_ts=since_ts, kind="delete"
        )
        >= 1
    ):
        return jsonify({"error": "操作过于频繁，请稍后再试"}), 429
    # 与全项目 IP 匿名口径同源：原先是不加盐的 sha256(ip)，IPv4 空间可直接枚举
    # 反推，等于把这一列的匿名性单独降级成"看着像哈希"
    ip_hash = m.db.hash_ip(ip)
    if (
        m.db.count_user_delete_requests(
            ip_hash=ip_hash, since_ts=since_ts, kind="delete"
        )
        >= m.DELETE_MAX_REQUESTS_PER_IP
    ):
        return jsonify({"error": "操作过于频繁，请稍后再试"}), 429
    # 内置管理员（.env 主管理员）不可自助注销：系统兜底账号，不落 users 表；
    # 同邮箱注册用户不受此限制（可正常自助注销）
    if m._is_builtin_admin_session():
        return jsonify({"error": "当前账号不可注销"}), 400
    # 密码确认 + 失败锁定（与登录/改密共用登录失败表计数，达阈值锁定）
    fail_key = (ip, email)
    with m._rate_lock:
        _fails, lock_until, _ = _login_fails().get(fail_key, (0, 0, 0))
        if now < lock_until:
            return jsonify({"error": "尝试次数过多，请稍后再试"}), 429

    def _handle_failed_login():
        """当前密码校验失败：递增失败计数，达阈值锁定（与 api_me_password 一致）。

        告警排在锁定之前（同阈值时两者都要发生，见 api_me_password 的说明）。
        """
        nfails = m._bump_login_failure(_login_fails(), fail_key, now)
        lock_now = nfails >= m.LOGIN_MAX_FAILS
        if lock_now:
            with m._rate_lock:
                _login_fails()[fail_key] = (0, now + m.LOGIN_LOCK_SECONDS, now)
            m.logger.warning("注销密码失败次数过多，IP %s 锁定 %s 秒", m.db.hash_ip(ip), m.LOGIN_LOCK_SECONDS)
        if nfails == m.LOGIN_FAIL_NOTIFY:
            m.send_notification(
                "注销密码失败告警",
                m.mail_layout.Mail(
                    summary=f"IP {m._nl_safe(ip)} 连续 {nfails} 次注销密码验证失败。",
                    fields=[("用户名", m._nl_safe(username))],
                    advice=["如非本人操作，请检查是否有人尝试注销该账号"],
                    level="warn",
                ),
            )
        if lock_now:
            # 不暴露锁定时长（信息分层）
            return jsonify({"error": "密码错误次数过多，请稍后再试"}), 429
        return jsonify({"error": "当前密码不正确"}), 400

    with m._file_lock:
        u = m.db.find_user(email)
        if u is None:
            return jsonify({"error": "用户不存在"}), 404
        if not m.check_password_hash(u.get("password_hash", ""), password):
            return _handle_failed_login()
        # 最后一个注册管理员不可注销（无内置管理员时会失去全部管理入口）
        if u.get("role") == "admin" and m.db.is_last_registered_admin(email):
            return jsonify({"error": "当前账号不可注销（系统最后一个管理员）"}), 400
        # 审计 + 软注销（db 单事务：账号/time_prefs 清除 + 用户标记）
        m.db.audit(username, "user_self_delete_request", email, "用户发起注销申请")
        try:
            _deleted = m.db.soft_delete_user_with_accounts(email)
        except m.db.LastAdminError:
            # 事务内复核兜底：跨进程并发注销时上面的 is_last_registered_admin
            # 预检可能双双通过，db 层复核拦截后转 400（原路径会 500）
            return jsonify({"error": "当前账号不可注销（系统最后一个管理员）"}), 400
        if not _deleted:
            return jsonify({"error": "注销失败，请稍后再试"}), 500
        # 防批量计数仅在注销成功后才记录（失败不占冷却额度）
        m.db.record_user_delete_request(email, ip_hash=ip_hash, kind="delete")
        m.db.audit(username, "user_self_delete_confirm", email, "注销已确认（软删除，7 天宽限期）")
        with m._rate_lock:
            _login_fails().pop(fail_key, None)
        m.logger.info("用户 %s 已注销账号（7 天宽限期）", m._mask_email(username))
        # 用户裁决：自助注销不发管理员通知（正常操作，避免通知轰炸）；
        # 管理员在「用户管理 → 已注销用户」区块主动查看（/api/users/deleted）
    session.clear()
    return jsonify({"ok": True, "msg": "账号已注销，7 天内可撤销"})


def api_me_restore():
    """冷静期账号恢复（用户裁决：仅登录即恢复，不做注册引导）。

    未登录可调（冷静期用户无会话；CSRF 走同源校验，与登录同等级）；
    密码验证 + 冷却限速（复用 user_delete_requests 计数：每邮箱 60s 1 次、
    每 IP 60s 5 次——与注销同一套底层冷却系统）；
    成功后 restore_user（联动恢复易班账号）+ 建立会话 + 审计 user_self_delete_restore。
    """
    m = _appmod()
    data = m._json_body()
    email = str(data.get("email", "")).strip().lower()
    password = str(data.get("password", ""))
    ip = m._client_ip()
    if not email or not password:
        return jsonify({"error": "邮箱和密码为必填项"}), 400
    # 防批量冷却（与注销同一张计数表，但按 kind 分流——注销动作自身的记录不再
    # 阻断 60s 内的恢复请求，"注销后立即反悔"路径畅通）
    since_ts = (m.clock.now() - timedelta(seconds=m.DELETE_COOLDOWN_SEC)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    if (
        m.db.count_user_delete_requests(
            username=email, since_ts=since_ts, kind="restore"
        )
        >= 1
    ):
        return jsonify({"error": "操作过于频繁，请稍后再试"}), 429
    # 与全项目 IP 匿名口径同源：原先是不加盐的 sha256(ip)，IPv4 空间可直接枚举
    # 反推，等于把这一列的匿名性单独降级成"看着像哈希"
    ip_hash = m.db.hash_ip(ip)
    if (
        m.db.count_user_delete_requests(
            ip_hash=ip_hash, since_ts=since_ts, kind="restore"
        )
        >= m.DELETE_MAX_REQUESTS_PER_IP
    ):
        return jsonify({"error": "操作过于频繁，请稍后再试"}), 429
    # 密码失败锁定预检：与登录/注销共用 (ip, email) 计数与锁定窗口。
    # email 直接来自请求体、长度无界，fail_key 统一截断 [:128]
    # （真实邮箱不可能超过 128；防公网扫描器用海量超长键打爆内存表）
    fail_key = (ip, email[:128])
    with m._rate_lock:
        m._ip_store_trim(_login_fails(), m.LOGIN_LOCK_SECONDS + m._IP_STORE_MAX_AGE)
        _fails, lock_until, _ = _login_fails().get(fail_key, (0, 0, 0))
        if time.time() < lock_until:
            return jsonify({"error": "密码错误次数过多，请稍后再试"}), 429
    u = m.db.find_user_any(email)
    in_grace = (
        u is not None
        and u.get("deleted")
        and m._delete_grace_remaining(u.get("deleted_at", "")) > 0
    )
    verified = in_grace and m.check_password_hash(u.get("password_hash", ""), password)
    if not in_grace:
        # 时延拉平：账号不存在/过期时也做等开销 dummy 比对，防时序探测
        m._constant_time_dummy(password)
    if not verified:
        # 密码失败锁定：与登录/注销共用登录失败表（键 (ip, email) 相同）——
        # 若不计数，同 IP 可无限爆破冷却期账号密码，命中即恢复并建立会话；
        # 共用计数后登录侧锁定同样约束本接口
        now2 = time.time()
        nfails = m._bump_login_failure(_login_fails(), fail_key, now2)
        # 补每 IP 聚合失败窗口（30 次/10 分钟）——单邮箱的失败阈值只约束单账号，
        # 攻击者可跨邮箱喷洒（总速率仅受全局限速约束）；命中即获得该冷静期账号的
        # 完整会话与其易班凭据，须有聚合闸门。恢复失败表写入路径补同口径清理
        # （窗口 + 最大年龄），防无界增长。
        with m._rate_lock:
            m._ip_store_trim(_restore_fail_rate(), m.RESTORE_FAIL_WINDOW + m._IP_STORE_MAX_AGE)
        _rc, _rs, ip_allowed = m._bump_window_count(
            _restore_fail_rate(), ip, now2, m.RESTORE_FAIL_WINDOW, limit=m.RESTORE_FAIL_MAX
        )
        if not ip_allowed:
            m.logger.warning("恢复密码尝试过于频繁（每 IP 聚合），IP %s 临时限制", m.db.hash_ip(ip))
            return jsonify({"error": "尝试过于频繁，请稍后再试"}), 429
        lock_now = nfails >= m.LOGIN_MAX_FAILS
        if lock_now:
            with m._rate_lock:
                _login_fails()[fail_key] = (0, now2 + m.LOGIN_LOCK_SECONDS, now2)
            m.logger.warning("恢复密码失败次数过多，IP %s 锁定 %s 秒", m.db.hash_ip(ip), m.LOGIN_LOCK_SECONDS)
        # 告警排在锁定之前（同阈值时两者都要发生，见 api_me_password 的说明）
        if nfails == m.LOGIN_FAIL_NOTIFY:
            m.send_notification(
                "恢复密码失败告警",
                m.mail_layout.Mail(
                    summary=f"IP {m._nl_safe(ip)} 连续 {nfails} 次恢复密码验证失败。",
                    fields=[("邮箱", m._nl_safe(email))],
                    advice=["如非本人操作，请检查是否有人尝试冒充恢复已注销账号"],
                    level="warn",
                ),
            )
        if lock_now:
            return jsonify({"error": "密码错误次数过多，请稍后再试"}), 429
        # 统一文案：不区分"账号不存在/已过期"与"密码错误"，防无凭探测"哪些邮箱
        # 正处于注销冷却期"（注销用户警惕性低，是钓鱼高价值目标）
        return jsonify({"error": "邮箱或密码错误，或账号已过恢复期"}), 400
    try:
        _restored = m.db.restore_user(email)
    except sqlite3.IntegrityError:
        # 并发注册抢注同邮箱（db 层已用写锁串行化，此处为防御纵深）——
        # 提示占用而非笼统"恢复失败"
        return jsonify({"error": "该邮箱已被注册，无法恢复"}), 409
    if not _restored:
        return jsonify({"error": "恢复失败，请稍后再试"}), 500
    # 防批量计数仅在恢复成功后才记录（失败不占冷却额度）
    m.db.record_user_delete_request(email, ip_hash=ip_hash, kind="restore")
    with m._rate_lock:
        _login_fails().pop(fail_key, None)
    m.db.audit(email, "user_self_delete_restore", email, "冷静期内恢复账号")
    # 恢复即登录：与 api_login 同款会话建立（防 session 固定）
    role = "admin" if u.get("role") == "admin" else "user"
    session.clear()
    session.permanent = True
    session["auth"] = True
    session["role"] = role
    session["username"] = email
    session["auth_source"] = "user"
    session["pw_version"] = u.get("pw_version", 1)
    # 会话绝对过期基准，与 api_login 同口径
    session["login_ts"] = int(time.time())
    # 登录出口与 api_login 同口径：risk 档"换环境"判据的基准之一，会话还没验证过口令
    # 时用它兜底。此处不写则本会话两级基准都空 = 未知，那条判据对这条会话永久失效
    # （恢复即登录建立的同样是完整会话，不该比登录路径少一层风控）。
    session["login_ip"] = ip
    # 恢复即登录须与 api_login 同样签发 sid 并落库。注销与恢复
    # （db.restore_user）均不轮换 sid，库内保留注销前登录签发的旧值——
    # 此处不签发则新会话无 sid、与库内旧值不匹配，恢复成功后下个请求即 401；
    # 且注销前被窃取的旧 cookie 会在恢复后原样复活，绕过整套 sid 吊销设计。
    sid = secrets.token_hex(16)
    session["sid"] = sid
    m.db.set_user_sid(email, sid)
    # 恢复即登录也要留 login_ok。上面建立的是与 api_login 完全同款
    # 的会话（sid 签发与 pw_version 语义一字未动），只记 user_self_delete_restore
    # 会让"这条会话当时是怎么建立的"在链上缺一半——恢复入口同样是被认证认可的
    # 登录成功路径，盗号者可借它取得带 sid 的完整会话。detail 用「恢复登录」区分
    # 入口；三元组与 login_ok 同口径（target 为 IP 的 HMAC，不落明文，用户名截断）。
    m.db.audit(
        (email or "?")[:64], "login_ok", m.db.hash_ip(ip), "登录成功（恢复登录）",
    )
    m.logger.info("用户 %s 已恢复注销账号", m._mask_email(email))
    return jsonify({"ok": True, "role": role})


def api_me():
    m = _appmod()
    # admin 字段为旧版前端兼容（早期前端检查 me.admin；新版用 role）——
    # 防止浏览器缓存旧页面时误判未登录导致刷新循环
    role = m._current_role()
    username = session.get("username") or ""
    # 邮箱通知开关（B 线：用户签到失败提醒，默认开；用户端可关）
    _me = m.db.find_user(username) if username else None
    mail_notify = bool(_me.get("mail_notify", 1)) if _me else True
    # 调度 v2：排序×分布模式与自选开关同步给用户（只读展示）
    env = m.read_env(m.ENV_FILE)
    mode = env.get("YIBAN_SIGN_MODE", "").strip().lower()
    sign_order = env.get("YIBAN_SIGN_ORDER", "").strip().lower() or (
        "random" if mode == "random" else "sequence"
    )
    sign_dist = env.get("YIBAN_SIGN_DIST", "").strip().lower() or (
        "normal" if mode == "normal" else "uniform"
    )
    # 窗口展示取**有效**窗口端点（`window.bounds`，含裁剪吃空时的回退）：与同页自选片
    # 卡片同一份几何——直读原始配置会在回退时让两处显示两个钟点（片卡 06:30~07:50、
    # 这里 07:00~07:10）。
    win = m.sign_window_bounds()
    return jsonify(
        {
            "ok": True,
            "auth": bool(session.get("auth")),
            "role": role,
            "username": username,
            "email": username,  # 普通用户顶部显示邮箱前缀（管理员为用户名）
            "admin": role == "admin",
            "mail_notify": mail_notify,  # B 线：用户签到失败邮件开关
            "is_builtin_admin": m._is_builtin_admin_session(),  # 仅 .env 主管理员会话为 True
            "csrf_token": get_csrf_token(),
            # 调度 v2（docs/design/plan-scheduler-v2.md 2.1/2.2）
            "sign_order": sign_order,
            "sign_dist": sign_dist,
            "time_pref_allowed": m.load_env_int(m.ENV_FILE, "YIBAN_ALLOW_TIME_PREF", 0) == 1,
            "sign_window": (f"{win.start_min // 60:02d}:{win.start_min % 60:02d}"
                            f" ~ {win.end_min // 60:02d}:{win.end_min % 60:02d}"),
        }
    )


def api_my_mail_notify():
    """读取当前用户邮箱通知开关。未登录默认视为开启（前端展示用）。"""
    m = _appmod()
    email = session.get("username") or ""
    user = m.db.find_user(email) if email else None
    return jsonify(
        {"ok": True, "mail_notify": bool(user.get("mail_notify", 1)) if user else True}
    )


def api_my_mail_notify_save():
    """保存当前用户邮箱通知开关：{enabled: bool}。CSRF 由 before_request 统一校验。"""
    m = _appmod()
    email = session.get("username") or ""
    if not email:
        return jsonify({"error": "未登录"}), 401
    data = m._json_body()
    enabled = data.get("enabled")
    if not isinstance(enabled, bool):
        return jsonify({"error": "取值无效"}), 400
    affected = m.db.update_user(email, {"mail_notify": 1 if enabled else 0})
    if affected == 0:
        # update_user 对不存在的邮箱是静默 no-op：内置管理员（.env 账号，不在
        # users 表）原会收到 ok:true 但刷新后开关弹回开启，还写入一条不存在的
        # 变更审计——改为明确 404 且不审计
        return jsonify({
            "error": "当前账号不支持此设置（内置管理员请用管理员邮件配置）"
        }), 404
    m.db.audit(email, "mail_notify", email, "on" if enabled else "off")
    if not enabled:
        # 关闭通知本身是"先静默关通知再作案"攻击链的一环——
        # 确认邮件直接 send_user 绕过刚被关闭的开关，让本人知情
        m.mailer.send_user(
            email,
            "【易班签到】签到失败邮件通知已被关闭",
            m.mail_layout.Mail(
                summary="您的签到失败邮件通知已被关闭（本人操作确认）。",
                advice=["如非本人操作，请立即联系管理员（账号可能已被他人控制）"],
                footer="需要重新开启：登录后在「我的账号」页打开邮箱通知开关。",
                level="urgent",
            ),
        )
    return jsonify({"ok": True, "mail_notify": enabled})


def register(app):
    """在本域注册六条个人域路由；endpoint 取函数名（url_for 依赖）。"""
    app.add_url_rule("/api/me/password", view_func=api_me_password, methods=["POST"])
    app.add_url_rule("/api/me/delete", view_func=api_me_delete, methods=["POST"])
    app.add_url_rule("/api/me/restore", view_func=api_me_restore, methods=["POST"])
    app.add_url_rule("/api/me", view_func=api_me)
    app.add_url_rule("/api/my-mail-notify", view_func=api_my_mail_notify)
    app.add_url_rule(
        "/api/my-mail-notify", view_func=api_my_mail_notify_save, methods=["PUT"]
    )
