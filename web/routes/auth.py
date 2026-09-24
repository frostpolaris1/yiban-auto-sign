# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""认证会话域路由：登录、注册、登出。

**功能**
`POST /api/login` 校验内置管理员（.env 口令）或注册用户（users 表，邮箱登录），
按角色重建会话并签发服务端 sid；`POST /api/register` 开放注册普通用户（邮箱格式、
口令策略、域名审查、用户配额、同 IP 限速）；`POST /api/logout` 写审计并轮换服务端
sid，使旧会话（含被盗副本）即时失效。

**归属**
`web.app.create_app` 的"认证面"。工厂骨架与跨域中间件（前置限速、登录守卫、CSRF、
同源校验、安全响应头）仍留在 `web/app.py`，本模块只提供三条视图。

**复用**
`register(app)` 供 `web.routes.register_all` 装配。登录失败表与登录/注册频率表经
`current_app.extensions` 取用——它们原是 create_app 工厂局部可变状态，必须保"每个
app 实例一份"（测试进程反复 create_app，进程级共享会跨实例串计数）。登录失败表由
`web.routes.login_fails()` 单点取用，认证/个人域共用同一份账（安全语义依赖同一份计数）。

**通信**
视图体不直接读 web.app 的模块级名字，一律经 `web.routes.appmod()` 按属性取——测试用
`mock.patch.object(web.app, …)` 打桩（ENV_FILE / verify_admin / check_password_hash /
load_env_int / send_notification / _client_ip / db 等）必须继续生效。
"""
import secrets
import sqlite3
import time

from flask import current_app, jsonify, session

from web.routes import appmod as _appmod
from web.routes import login_fails as _login_fails


def _register_limits():
    """注册限速表（仅注册接口使用，每 app 实例一份）。"""
    return current_app.extensions["yiban_register_limits"]


def _login_rate():
    """登录频率限速表（仅登录接口使用，每 app 实例一份）。"""
    return current_app.extensions["yiban_login_rate"]


def api_login():
    """登录：管理员（.env 配置）或普通用户（users 表注册）。返回 role。"""
    m = _appmod()
    ip = m._client_ip()
    now = time.time()
    data = m._json_body()
    username = str(
        data.get("username", "")
    ).strip()  # 管理员用户名保持原样；邮箱仅用户登录时小写
    password = str(data.get("password", ""))
    # 失败计数按 (IP, 用户名) 组合：同一出口 IP 的用户不因他人爆破尝试被连带锁定
    # 值三元组 (count, lock_until, last_ts)：last_ts 供超限清理
    # username 直接来自请求体、长度无界，fail_key 统一截断 [:128]
    # （防公网扫描器用海量超长用户名打爆 _login_fails 内存表）
    fail_key = (ip, username.lower()[:128])
    with m._rate_lock:
        m._ip_store_trim(_login_fails(), m.LOGIN_LOCK_SECONDS + m._IP_STORE_MAX_AGE)
        _fails, lock_until, _ = _login_fails().get(fail_key, (0, 0, 0))
        if now < lock_until:
            # 不显示剩余秒数：避免向用户暴露锁定窗口参数（信息分层）
            return jsonify({"error": "密码错误次数过多，请稍后再试"}), 429
    # 登录频率限制（60 秒窗口 10 次/IP，比全局限速更严）：防换用户名密码喷洒。
    # 先清理过期条目再按“先判断后递增”的旧语义计数：允许第 10 次，第 11 次 429。
    with m._rate_lock:
        m._ip_store_trim(_login_rate(), 60 + m._IP_STORE_MAX_AGE)
    _lcnt, _lstart, allowed = m._bump_window_count(_login_rate(), ip, now, 60, limit=10)
    if not allowed:
        return jsonify({"error": "登录尝试过于频繁，请稍后再试"}), 429

    role = None
    pw_version = None
    auth_source = None
    recoverable = False
    # 1) 内置管理员（.env，兜底超级管理员）
    if m.verify_admin(username, password):
        role = "admin"
        auth_source = "builtin"
        pw_version = m.load_env_int(m.ENV_FILE, "YIBAN_ADMIN_PW_VERSION", 1)  # 改密后旧会话失效
    else:
        # 2) 普通用户（users，邮箱登录，不区分大小写；role 支持多管理员）
        email = username.lower()
        u = m.db.find_user(email)
        if u is None:
            m._constant_time_dummy(password)  # 时延拉平：防邮箱枚举（与真实比对等开销）
            # 登录即恢复：冷静期（7 天）内密码正确的已注销账号不放行登录，返回
            # recoverable 标记，由前端引导恢复（受冷却限速）
            du = m.db.find_user_any(email)
            if (
                du is not None
                and du.get("deleted")
                and m._delete_grace_remaining(du.get("deleted_at", "")) > 0
                and m.check_password_hash(du.get("password_hash", ""), password)
            ):
                recoverable = True
        elif m.check_password_hash(u.get("password_hash", ""), password):
            role = "admin" if u.get("role") == "admin" else "user"
            auth_source = "user"  # 注册用户（含提升的管理员）统一记为 user
            pw_version = u.get("pw_version", 1)
    if role:
        with m._rate_lock:
            _login_fails().pop(fail_key, None)
        # 登录成功写入审计：成功登录原零留痕，被盗会话无法还原会话何时建立、
        # 来自哪个 IP（IP 经 hash_ip 匿名化，与审计侧口径一致）。
        # 失败登录已有阈值邮件告警，不重复写审计（避免爆破刷爆审计表）。
        # 动作名 login_ok 与 login_failed / logout_ok 同组，并补齐登出端与恢复入口
        # （api_me_restore 的"恢复即登录"）两处留痕；成功路径的 username 截断 64——
        # 该值直接来自请求体，不截断等于把审计表当垃圾场。两条成功分支（内置管理员
        # 走 .env 口令/哈希、注册用户走 users.password_hash）在 `if role:` 处汇合，
        # 所以这一行同时覆盖两条分支、每次登录仍只有一行，走的哪条由 detail 里的
        # auth_source 指明。成组之后取证侧一句 WHERE action='login_ok' 就能重建
        # "谁的账号、何时、从哪个 IP 登录过"的时间线。
        # 跨版本取证须写 action IN ('login','login_ok')：早期即以动作名 login 落在
        # 同一个汇合点，只查 login_ok 会整段漏掉更早写入的行（现网库与历史备份包内
        # 都是）。
        # 位置刻意留在校验通过后立即记录，早于 session 重建与下方 set_user_sid：
        # 口令通过校验即一次既成的登录事实，即使后续 sid 落库失败也要留下这次
        # 登录；且此时手上还没有 sid，本行天然不可能写进可重放的凭据。
        m.db.audit(
            (username.lower() or "?")[:64], "login_ok", m.db.hash_ip(ip),
            f"登录成功（{auth_source}）",
        )
        # auth_source 记录实际认证来源，不用 role+邮箱反推
        # 防 session 固定：登录成功先清空再重建会话
        session.clear()
        session.permanent = True
        session["auth"] = True
        session["role"] = role
        # 会话用户名统一小写：注册用户邮箱库内小写存储，而
        # /api/me、邮件开关等接口以 session username 做大小写敏感的
        # find_user/update_user 精确匹配——存原始大小写会静默失效。
        # 内置管理员不受影响（_is_builtin_admin_session/_effective_role 比对端自行小写）。
        session["username"] = username.lower()
        session["auth_source"] = auth_source
        session["pw_version"] = pw_version  # 密码版本（注册用户改密/被重置后旧会话失效）
        # 会话绝对过期基准：自此刻起最多 SESSION_ABS_TTL_SECONDS
        session["login_ts"] = int(time.time())
        # 登录时的出口 IP：risk 档"换环境"判据的基准之一——本会话还没有"已验证 IP"
        # 时用它兜底。只在登录成功时记录，历史会话没有这个键 = 未知（不判异常，
        # 见 _pw_gate_ip_changed）。
        session["login_ip"] = ip
        # 服务端会话吊销：注册用户登录签发 sid 并落库——登出/被
        # 重置密码/被踢时轮换，被盗 cookie 重放即失效。内置主管理员没有
        # users 行可存，它的"那一行"就是 .env：同一条吊销面落在
        # YIBAN_ADMIN_SID 上（只在登录/登出/改密/追回时写，频率极低）。
        if auth_source == "builtin":
            session["sid"] = m._issue_admin_sid(m.ENV_FILE)
        elif auth_source == "user":
            sid = secrets.token_hex(16)
            session["sid"] = sid
            m.db.set_user_sid(username.lower(), sid)
        return jsonify({"ok": True, "role": role})
    if recoverable:
        # 冷静期账号：密码正确但不建立会话，前端引导恢复（/api/me/restore）
        return jsonify({"ok": True, "recoverable": True, "msg": "账号已注销，7 天内可恢复"})
    fails = m._bump_login_failure(_login_fails(), fail_key, now)
    # 失败登录留痕审计链：失败原仅内存计数+日志，"被盗号溯源"
    # 场景无法从审计还原爆破片段。刻意不在每次失败都写（防爆破刷爆审计表），
    # 与阈值邮件/锁定同节奏：达到告警阈值（3 次）与锁定阈值（5 次）各留痕一条，
    # IP 经 hash_ip 匿名化（与登录成功审计同口径）。用户名截断防长串刷审计。
    if fails in (m.LOGIN_FAIL_NOTIFY, m.LOGIN_MAX_FAILS):
        m.db.audit(
            (username.lower() or "?")[:64],
            "login_failed",
            m.db.hash_ip(ip),
            f"连续失败 {fails} 次（阈值留痕）",
        )
    if fails >= m.LOGIN_MAX_FAILS:
        with m._rate_lock:
            _login_fails()[fail_key] = (0, now + m.LOGIN_LOCK_SECONDS, now)
        m.logger.warning(
            "登录失败次数过多，IP %s 锁定 %s 秒", m.db.hash_ip(ip), m.LOGIN_LOCK_SECONDS
        )
        return jsonify(
            {"error": f"密码错误次数过多，已锁定 {m.LOGIN_LOCK_SECONDS // 60} 分钟"}
        ), 429
    # 连续失败达到阈值时告警（每轮锁定只发一次），提示可能为暴力破解
    if fails == m.LOGIN_FAIL_NOTIFY:
        # 告警级别判据：把"输错 3 次密码"一律标成紧急，而紧急额度默认只有 3 条/天——
        # 一次常见的忘密码触发锁定，就会挤掉"告警通道被人拆了""审计链断裂"这类真紧急信号。
        # 故只有同一 IP 正对多个不同用户名失败（口令喷洒特征）才标紧急；单个账号
        # 反复输错走非紧急账（开启「仅推送重要告警」时不再打扰手机，邮件照旧全量）。
        with m._rate_lock:
            distinct_users = sum(1 for (fip, _u) in _login_fails() if fip == ip)
        m.send_notification(
            "登录失败告警",
            m.mail_layout.Mail(
                summary=f"IP {m._nl_safe(ip)} 连续 {fails} 次登录失败。",
                fields=[
                    ("尝试用户名", m._nl_safe(username)),
                    ("该 IP 试过的不同用户名", f"{distinct_users} 个"),
                ],
                advice=["如非本人操作，请检查是否有人尝试暴力破解"],
                level="urgent" if distinct_users >= m.LOGIN_SPRAY_USERS else "warn",
            ),
            urgent=distinct_users >= m.LOGIN_SPRAY_USERS,
            # 独立账本 YIBAN_LOGINFAIL_DAILY_MAX（默认 3，0=不限）——
            # 登录失败是公网最高频告警源，不再与 general/urgent 两本账互挤，
            # 喷洒类攻击烧光本账后审计链异常等真紧急告警仍可达手机
            ledger="login_fail",
        )
    return jsonify({"error": "用户名或密码错误"}), 401


def api_register():
    """开放注册普通用户：邮箱 + 密码（哈希存储）。

    邮箱格式校验；邮箱全局唯一；不做验证码服务。无昵称体系（一人一号，账号备注名在账号表单中填写）。
    """
    m = _appmod()
    # 暂停注册时最先拦截——全局开关状态本身即公开信息（登录页
    # 同步提示），早返回不产生枚举/时延侧信道；不占注册限速计数，不写审计
    # （防机器人刷审计表）。
    if m._registration_paused():
        return jsonify({"error": "注册已暂停，请联系管理员添加账号"}), 403
    data = m._json_body()
    email = str(data.get("email", "")).strip().lower()
    password = str(data.get("password", ""))
    # 后端同意校验：必须显式勾选《用户协议》与《隐私政策》（前端勾选仅为 UX，此处为强制）。
    # 严格布尔判断（is not True）：真值判断会让 "0"/"false"/"no" 等非空字符串绕过同意校验。
    if data.get("agree") is not True:
        return jsonify({"error": "请先阅读并同意《用户协议》和《隐私政策》"}), 400
    if len(email.split("@")[0]) > m.EMAIL_USER_MAX:
        return jsonify({"error": f"邮箱用户名部分过长（最多 {m.EMAIL_USER_MAX} 字符）"}), 400
    if not m.EMAIL_RE.match(email) or len(email) > 64:
        return jsonify({"error": "请输入有效的邮箱地址"}), 400
    # 邮箱域名审查：占位/一次性域名在写库前排除，防挤占用户池
    dom_err = m.email_domain_error(email)
    if dom_err:
        return jsonify({"error": dom_err}), 400
    pw_err = m._password_policy_error(password)
    if pw_err:
        return jsonify({"error": pw_err}), 400
    # 内置管理员邮箱保留给 .env 主管理员，开放注册/自动注册均不得占用
    if email.strip().lower() == m._builtin_admin_email().strip().lower():
        return jsonify({"error": "内置管理员邮箱不可注册"}), 400
    # 注册限速：同 IP 窗口内成功注册次数超限则拒绝（防邮箱批量注册）
    ip = m._client_ip()
    now = time.time()
    with m._rate_lock:
        m._ip_store_trim(_register_limits(), m.REGISTER_WINDOW)
        rcnt, rstart = _register_limits().get(ip, (0, now))
        if now - rstart > m.REGISTER_WINDOW:
            rcnt, rstart = 0, now
        if rcnt >= m.REGISTER_MAX:
            # 不暴露限速窗口分钟数（防恶意用户据此规划批量注册节奏，信息分层）
            return jsonify({"error": "注册过于频繁，请稍后再试"}), 429
    # 操作级锁：邮箱唯一性检查与写入原子（UNIQUE 约束兜底并发注册）
    with m._file_lock:
        # 容量兜底：用户配额（全部未删除注册用户，含空用户；
        # 防分布式注册无限膨胀 users 表。与账号配额同构，
        # 统一为"再注册 1 人后 > 上限才拒"语义，见 _users_at_capacity）
        max_users = m.load_env_int(m.ENV_FILE, "YIBAN_MAX_USERS", m.DEFAULT_MAX_USERS)
        if m._users_at_capacity():
            m._notify_capacity_once("users", max_users, "注册人数")
            return jsonify({"error": "注册人数已达上限，请联系管理员"}), 403
        if m.db.find_user(email) is not None:
            # 时延拉平：已注册邮箱在此提前返回，跳过了后方的 scrypt 哈希
            #（约百毫秒），响应时序差可被用于批量枚举"哪些邮箱是本站注册用户"。
            # 与登录/恢复的 _constant_time_dummy 惯例对齐。
            m._constant_time_dummy(password)
            return jsonify({"error": "该邮箱已注册"}), 400
        # 冷却期邮箱保护：已注销账号 7 天冷却期内禁止同邮箱注册，
        # 否则恢复权会被新注册抢占（登录即恢复形同虚设）；宽限期结束后邮箱正常释放
        du = m.db.find_user_any(email)
        if (
            du is not None
            and du.get("deleted")
            and m._delete_grace_remaining(du.get("deleted_at", "")) > 0
        ):
            m._constant_time_dummy(password)  # 时延拉平：同上，防探测"近期注销"邮箱
            # （防枚举文案）：冷却期分支文案与「该邮箱已注册」
            # 逐字一致——专属文案（"正在注销冷却期"）让攻击者批量探测"哪些邮箱近期
            # 注销过"（低警惕期用户是钓鱼高价值目标）。恢复入口仍由登录页提供，
            # 注册侧不给出任何差异信号。
            return jsonify({"error": "该邮箱已注册"}), 400
        try:
            created = m.db.create_user(
                email,
                m.generate_password_hash(password, method=m.SCRYPT_METHOD),
                role="user",
                created_at=m.clock.now().strftime("%Y-%m-%d %H:%M:%S"),
                pw_version=1,  # 密码版本：改密时递增，旧会话随之失效
            )
        except sqlite3.IntegrityError:
            return jsonify({"error": "该邮箱已注册"}), 400  # 并发注册兜底
        if not created:
            return jsonify({"error": "该邮箱已注册"}), 400  # OR IGNORE 未实际创建
        m.db.audit(email, "user_register", email, "开放注册")
    # 成功注册计数：原子重读后递增，避免并发注册丢失计数
    m._bump_window_count(_register_limits(), ip, now, m.REGISTER_WINDOW)
    m.logger.info("新用户注册: %s", m._mask_email(email))
    return jsonify({"ok": True})


def api_logout():
    m = _appmod()
    # 登出留痕。生产 audit_logs 里 logout 类动作此前 0 条——
    # "被盗号者用完会话有没有登出、本人何时从哪个 IP 结束登录"完全无从还原；
    # 只有与 login_ok 成对，一次会话的起止两端才都钉在 HMAC 链上。
    # 三元组口径与 forbidden_path/login_ok 逐字同构：target 只存 IP 的 HMAC
    # （不落明文、不存 User-Agent）、username 截断 64、detail 只记认证来源，
    # 绝不写入 sid/Cookie/CSRF 值（那些一旦进链就等于把可重放的凭据抄进日志）。
    # 顺序刻意在 session.clear() 之前：清空后就再也取不到 username 与 auth_source。
    m.db.audit(
        m._audit_actor(), "logout_ok", m.db.hash_ip(m._client_ip()),
        f"登出（{session.get('auth_source') or 'builtin'}）",
    )
    # 登出轮换服务端 sid——此前仅 session.clear()，此前被窃取的
    # cookie 副本在登出后重放依然有效。轮换后所有旧会话（含当前）即时失效；
    # 内置主管理员同一条吊销面落在 .env 的 YIBAN_ADMIN_SID 上（原实现认为它"走
    # PW_VERSION 即可"，但版本号只在改口令时递增——本人登出踢不掉被盗副本）。
    if session.get("auth_source") == "builtin":
        # 必须在 session.clear() 之前判：清空后取不到 auth_source
        try:
            m._issue_admin_sid(m.ENV_FILE)
        except Exception as e:
            # 与注册用户分支同口径：失败意味着"被盗 cookie 在登出后仍有效"这一
            # 服务端吊销机制未生效，而对外仍返回 {"ok": true}，只能靠日志追。
            m.logger.error("登出轮换内置管理员 sid 失败（旧会话可能仍有效）: %s", e)
    elif (
        session.get("auth_source") == "user"
        and session.get("username")
    ):
        try:
            m.db.set_user_sid(
                session["username"].strip().lower(), secrets.token_hex(16)
            )
        except Exception as e:
            # 不得静默吞掉：本条失败意味着"被盗 cookie 在登出后仍有效"这一
            # 服务端吊销机制未生效，而对外仍返回 {"ok": true}。与 db.audit
            # 写失败同口径留痕（调用方无法区分，只能靠日志）。
            m.logger.error("登出轮换 sid 失败（旧会话可能仍有效）: %s", e)
    session.clear()
    return jsonify({"ok": True})


def register(app):
    """在本域注册三条认证路由；endpoint 取函数名（url_for 依赖）。"""
    app.add_url_rule("/api/login", view_func=api_login, methods=["POST"])
    app.add_url_rule("/api/register", view_func=api_register, methods=["POST"])
    app.add_url_rule("/api/logout", view_func=api_logout, methods=["POST"])
