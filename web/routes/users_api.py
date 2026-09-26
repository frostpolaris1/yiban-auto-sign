# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""用户管理域路由：注册用户的列表/批量操作/角色/密码/删除与已注销用户清除（管理员面）。

**功能**
`GET /api/users` 用户列表（含内置管理员信息与账号计数）；`GET /api/users/deleted`
已注销用户与剩余宽限期；`POST /api/users/deleted/purge` 主管理员物理清除；
`POST /api/users/batch` 批量重置密码/删除；`POST /api/users/<email>/role`
设置/取消管理员；`POST /api/users/<email>/password` 重置密码；
`POST /api/users/<email>/delete` 完全删除或仅清空其易班账号。

**归属**
`web.app.create_app` 的"用户管理"面（管理端）。工厂骨架、跨域中间件（前置限速、登录
守卫、CSRF、同源校验、安全响应头）与库访问层仍留在 `web/app.py`。

**复用**
`register(app)` 供 `web.routes.register_all` 装配；`high_risk_gate()` 取回高危动作统一
门禁（先二次鉴权、通过后才占额度），`read_audit_trace()` 是只读面聚合留痕入口。

**通信**
视图体不直接读 web.app 的模块级名字，一律经 `web.routes.appmod()` 按属性取——测试用
`mock.patch.object(web.app, …)` 打桩（db / logger / load_users / load_accounts /
send_notification / write_env_batch / ENV_FILE 等）必须继续生效。删除、角色变更与重置
他人口令一律经 `high_risk_gate()` 门禁后落库；被 `web.app` 的 `register_all(app)` 一次接入。
"""
import contextlib
import secrets

from flask import jsonify, session

from web.routes import appmod as _appmod
from web.routes import high_risk_gate, read_audit_trace


def _builtin_admin_display():
    """内置管理员显示名（保留 .env 原始大小写，仅用于界面展示）。"""
    m = _appmod()
    env = m.read_env(m.ENV_FILE)
    return env.get("YIBAN_ADMIN_USER", "").strip() or "admin"


def api_users():
    """用户列表（完整邮箱/角色/注册时间/账号数/待审核账号数）+ 内置管理员信息。"""
    m = _appmod()
    users = m.load_users()
    accounts = m.load_accounts()
    # 性能优化：单次遍历 accounts 预计算每个 owner 的计数，避免 O(用户数×账号数)
    owner_account_count = {}
    owner_pending_count = {}
    owner_review_count = {}
    for a in accounts:
        if a.get("deleted"):
            continue
        owner = a.get("owner", "")
        owner_account_count[owner] = owner_account_count.get(owner, 0) + 1
        if a.get("status") == m.ACCOUNT_STATUS_PENDING:
            owner_pending_count[owner] = owner_pending_count.get(owner, 0) + 1
        # review 口径 = 待审核 + 已拒绝，与账号管理「待处理账号」组一致
        # （仅算待审核时，只有已拒绝账号的用户不会出现在待处理栏）
        if a.get("status") in (m.ACCOUNT_STATUS_PENDING, m.ACCOUNT_STATUS_REJECTED):
            owner_review_count[owner] = owner_review_count.get(owner, 0) + 1
    result = [
        {
            "email": u.get("email", ""),
            "role": u.get("role", "user"),
            "created_at": u.get("created_at", ""),
            # 计数排除软删除账号（删除后不占账号数/待审核数）
            "account_count": owner_account_count.get(u.get("email", ""), 0),
            "pending_count": owner_pending_count.get(u.get("email", ""), 0),
            "review_count": owner_review_count.get(u.get("email", ""), 0),
        }
        for u in users
    ]
    read_audit_trace()("users_list_read", f"整表 {len(result)} 条用户")
    return jsonify(
        {
            "ok": True,
            "users": result,
            "builtin_admin": _builtin_admin_display(),
        }
    )


def api_users_deleted():
    """已注销用户列表（仅管理员）：软删除冷却中/待清除用户 + 剩余天数。

    自助注销不发管理员通知，改为本视图主动查看；
    时间按天粒度（remaining_days 整天向下取整，0 = 不足一天），无秒级计算。
    require_login 已限定仅管理员（普通用户白名单外 → 403）。
    """
    m = _appmod()
    items = []
    for u in m.db.load_users(include_deleted=True):
        if not u.get("deleted"):
            continue
        deleted_at = str(u.get("deleted_at") or "")
        remain_sec = m._delete_grace_remaining(deleted_at)
        status = "cooling" if remain_sec > 0 else "purge_pending"
        items.append(
            {
                "email": u["email"],
                "deleted_at": deleted_at,
                "remaining_days": int(remain_sec // 86400) if remain_sec > 0 else 0,
                "status": status,
            }
        )
    items.sort(key=lambda x: x["deleted_at"])  # 最早到期在前（ISO 字符串字典序 = 时间序）
    read_audit_trace()("users_deleted_read", f"整表 {len(items)} 条已注销用户")
    return jsonify({"ok": True, "items": items})


def api_users_deleted_purge():
    """主管理员手动物理清除已注销用户（不留存已注销用户信息）。

    body: {"emails": [...]}——只清 deleted=1 的用户（db 层再校验，活跃用户传入即跳过）；
    冷却期内的用户也可被清除（管理员裁决权高于 7 天宽限承诺，审计留痕可追溯）。
    连带清理其全部易班账号行（含软删）与 time_prefs；单事务失败全部回滚。

    收归主管理员专属——物理清除不可逆且剥夺用户 7 天反悔权，
    与角色变更/邮件配置等 master-only 口径对齐（普通管理员可绕过宽限承诺
    清除用户，故 403）。过期清理由系统每日清理自动完成，不受影响。
    同步即时告警。
    """
    m = _appmod()
    if not m._is_builtin_admin_session():
        return jsonify({"error": "仅主管理员可操作"}), 403
    data = m._json_body()
    emails = data.get("emails") or []
    if not isinstance(emails, list) or not emails:
        return jsonify({"error": "请选择要清除的用户"}), 400
    if len(emails) > m.BATCH_OP_LIMIT:
        return jsonify({"error": f"单次最多清除 {m.BATCH_OP_LIMIT} 个用户"}), 400
    if any(not isinstance(e, str) or len(e) > 64 for e in emails):
        return jsonify({"error": "邮箱格式不正确"}), 400
    # 物理清除不可逆 → 过口令门禁 + 同管理员限速（顺序恒为"先鉴权、通过了才占额度"）
    gate = high_risk_gate()(data, "彻底清除已注销用户", irreversible=True)  # irreversible：非 full 档还要倒计时确认
    if gate:
        return gate
    with m._file_lock:
        purged = m.db.purge_deleted_users_hard(emails)
        if purged:
            admin = session.get("username") or "admin"
            # 有意留在提交后的审计：清除清单要跑完才知道（非已注销行被跳过），
            # 而提交前能备好的 target/detail 只能按"请求清单"写，会把没清除的
            # 项也写成清除过；此处只物理清除**已软删**用户（非活跃凭据），
            # 且 master-only + 限速门禁。
            m.db.audit(
                admin,
                "user_deleted_purge",
                ",".join(purged),
                f"管理员手动清除 {len(purged)} 个已注销用户（含其易班账号与自选时间）",
            )
    skipped = [e for e in emails if e not in purged]
    m.logger.info("主管理员手动清除已注销用户: 成功 %d 个", len(purged))
    # 物理清除不再外发即时告警：留痕由上面的审计行承担（谁、清了哪些、数量），
    # 管理操作逐条发信会把告警邮件刷成"操作日志"。
    return jsonify({
        "ok": True,
        "purged": purged,
        "skipped": skipped,
        "msg": f"已彻底清除 {len(purged)} 个用户"
        + (f"，跳过 {len(skipped)} 个（非已注销状态）" if skipped else ""),
    })


def api_users_batch():
    """批量操作注册用户：reset_password/delete。

    body: {"action": ..., "emails": [...], "password": "批量重置的新密码"}
    角色变更不支持批量：提权/降权仅保留 /api/users/<email>/role 单个路径，
    且须先过高危口令门禁（是否真要当次口令随 `YIBAN_PW_GATE` 档位）。
    Phase 1：整体事务，失败全部回滚；无效项软跳过。
    """
    m = _appmod()
    data = m._json_body()
    action = data.get("action")
    emails = data.get("emails") or []
    if action not in ("reset_password", "delete"):
        return jsonify({"error": "未知操作"}), 400
    if not isinstance(emails, list) or not emails:
        return jsonify({"error": "请选择要操作的用户"}), 400
    if len(emails) > m.BATCH_OP_LIMIT:
        # 单次批量上限——否则被盗 admin 会话可用一个请求物理删除全部
        # 用户；与 accounts/batch 共用 BATCH_OP_LIMIT
        return jsonify({"error": f"单次批量操作最多 {m.BATCH_OP_LIMIT} 个用户"}), 400
    if any(not isinstance(e, str) or len(e) > 64 for e in emails):
        return jsonify({"error": "邮箱格式不正确"}), 400
    password = str(data.get("password", ""))
    reset_hash = None
    if action == "reset_password":
        pw_err = m._password_policy_error(password)
        if pw_err:
            return jsonify({"error": f"新密码不符合要求：{pw_err}"}), 400
        # 在 _file_lock 外预计算 scrypt 哈希，避免长时间占用进程锁
        reset_hash = m.generate_password_hash(password, method=m.SCRYPT_METHOD)

    # 删除与批量重置口令都走高危门禁（口令 + 同管理员窗口内限速，防被盗会话快速反复删除
    # 用户并刷告警邮件）。重置口令入门禁的理由：它同为账号控制权转移操作（e2e 实锤：普通
    # 管理员无口令即可批量接管用户登录）——只把 delete 当高危是不够的。
    if action in ("delete", "reset_password"):
        gate = high_risk_gate()(  # 顺序恒为"先鉴权、通过了才占额度"；429 文案各自保持原样
            data,
            "批量删除用户" if action == "delete" else "批量重置密码",
            limit_msg="删除操作过于频繁，请稍后再试"
            if action == "delete" else "重置操作过于频繁，请稍后再试",
            # 只有 delete 不可逆；批量重置口令可再重置一次，不套倒计时确认
            irreversible=(action == "delete"),
        )
        if gate:
            return gate

    with m._file_lock:
        users = m.load_users()
        builtin = m._builtin_admin_email()
        # `builtin` 是"这一行就是内置管理员，别当普通用户批量操作"的身份比对；
        # "还有没有兜底入口"是另一件事，必须用可登录判据（两个变量别混用一个）
        builtin_ok = m._builtin_admin_loginable()

        # 内存模拟用户表，保持动态管理员数量判断
        sim_users = {u["email"]: dict(u) for u in users}
        ops = []
        processed = []  # 真正进了 ops 的邮箱（sid 轮换只认它，不含被跳过的）
        for email in emails:
            target = sim_users.get(email)
            if not target or email == builtin:  # 内置管理员不可批量操作
                continue
            if (
                action in ("reset_password", "delete")
                and target.get("role") == "admin"
                and not m._is_builtin_admin_session()
            ):
                # 普通管理员不可重置/删除其他管理员（与单条 403 同口径；
                # 批量沿用"无效项软跳过"惯例，与内置管理员跳过一致）
                continue
            if action == "reset_password":
                ops.append(
                    (
                        "update_user",
                        email,
                        {
                            "password_hash": reset_hash,
                            "pw_version": target.get("pw_version", 1) + 1,
                        },
                    )
                )
                sim_users[email]["pw_version"] = target.get("pw_version", 1) + 1
                processed.append(email)
            elif action == "delete":
                # 防呆：目标为管理员时校验至少保留 1 个管理员
                # （内置管理员**进得来**时才允许删掉最后一个注册管理员，与单条路径一致）
                if target.get("role") == "admin":
                    admins = [u for u in sim_users.values() if u.get("role") == "admin"]
                    if len(admins) <= 1 and not builtin_ok:
                        continue
                ops.append(("delete_user_with_accounts", email, builtin_ok))
                sim_users.pop(email, None)
        done = len(ops)
        # 批量操作留目标清单（脱敏截断），破坏事后可从审计还原"动了谁"；
        # 重置密码时补"跳过 N 个"（被软跳过项），运维能看出批量里有没处理上的
        audit_detail = (f"处理 {done} 个: " + ",".join(
            m._mask_email(e) for e in (emails or [])[:20]
        ))[:200]
        if action == "reset_password" and done < len(emails or []):
            audit_detail += f"；跳过 {len(emails or []) - done} 个"
        audit_spec = {
            "username": session.get("username") or "?",
            "action": "users_batch",
            "target": action,
            "detail": audit_detail,
        }
        if ops:
            try:
                # 审计与整批操作同事务：批量重置口令/删除是凭据路径，生效与留痕
                # 必须同生共死（命中最后管理员/异常时整批回滚，另走下面的单独留痕）。
                m.db.batch_user_ops(ops, audit_spec=audit_spec)
            except m.db.LastAdminError:
                # db 事务内复核兜底（跨进程竞态时整体回滚转 400）
                m.db.audit(
                    session.get("username") or "?",
                    "users_batch",
                    action,
                    "被拒：批量操作命中最后一个注册管理员保护，已整体回滚",
                )
                return jsonify({"error": "批量操作包含最后一个注册管理员的删除/降权，已整体回滚"}), 400
            except Exception as e:
                m.logger.error("批量%s用户失败: %s（已回滚）", action, e)
                m.db.audit(
                    session.get("username") or "?",
                    "users_batch",
                    action,
                    "失败，已回滚",
                )
                return jsonify({"error": "批量操作失败，已全部回滚"}), 500
            # 批量删除/重置密码不再外发即时告警：留痕由上面的审计行承担
            # （动作 + 目标清单），管理操作逐条发信会把告警邮件刷成"操作日志"。
            # 批量重置密码后轮换各目标 sid（吊销被盗旧会话）。
            # 只轮换**真正重置了密码**的账号（processed）：若遍历请求里的 emails
            # 原文，被跳过的管理员（内置/非主管理员动其他管理员）密码没变、
            # sid 却被换掉 → 会话被无端登出（越权影响他人会话）。
            if action == "reset_password":
                for e in processed:
                    with contextlib.suppress(Exception):
                        m.db.set_user_sid(e.strip().lower(), secrets.token_hex(16))
        else:
            # 无实际可操作项：无业务效果，仅留一条"处理 0 个"的痕迹（无同事务对象）
            m.db.audit(session.get("username") or "?", "users_batch", action, audit_detail)
        m.logger.info("批量%s用户 %d 个", action, done)
        msg = {
            "reset_password": f"已重置密码 {done} 个用户",
            "delete": f"已删除 {done} 个用户",
        }[action]
        return jsonify({"ok": True, "msg": msg})


def api_user_role(email):
    """设为管理员 / 取消管理员。仅主管理员（.env 内置管理员）可操作；
    只能将「正式用户」（有生效账号且无待审核）设为管理员；
    防呆：内置管理员不可改；至少保留 1 个管理员。

    角色变更是权限面变更，接入高危门禁（口令复核是否索要随 `YIBAN_PW_GATE` 档位 + 限速）；
    批量角色变更入口已移除，本端点是唯一变更路径。
    """
    m = _appmod()
    # 权限：仅主管理员（普通管理员无管理员权限变更权）
    username = (session.get("username") or "").strip().lower()
    if not m._is_builtin_admin_session():
        return jsonify({"error": "仅主管理员可修改管理员权限"}), 403
    data = m._json_body()
    gate = high_risk_gate()(data, "修改管理员权限")  # 与删除/重置同口径：先过门禁，通过了才占额度
    if gate:
        return gate
    new_role = data.get("role")
    if new_role not in ("admin", "user"):
        return jsonify({"error": "未知角色"}), 400
    # 内置管理员（.env）不可修改角色
    if email.strip().lower() == m._builtin_admin_email().strip().lower():
        return jsonify({"error": "内置管理员不可修改角色"}), 400
    with m._file_lock:
        target = m.db.find_user(email)
        if not target:
            return jsonify({"error": "用户不存在"}), 404
        if new_role == "admin":
            # 只能将正式用户（有生效账号且无待审核）设为管理员；
            # 正式用户判定仅 status==active 算（rejected 不算），且软删除不算
            accounts = m.load_accounts()
            has_pending = any(
                a.get("owner") == email
                and a.get("status") == m.ACCOUNT_STATUS_PENDING
                and not a.get("deleted")
                for a in accounts
            )
            has_active = any(
                a.get("owner") == email
                and a.get("status") == m.ACCOUNT_STATUS_ACTIVE
                and not a.get("deleted")
                for a in accounts
            )
            if not has_active or has_pending:
                return jsonify({"error": "仅正式用户可设为管理员（需有已生效账号且无待审核）"}), 400
        if new_role == "user" and target.get("role") == "admin":
            admins = [u for u in m.load_users() if u.get("role") == "admin"]
            # 内置管理员也可登录时才算"还有人兜底"——只配置了用户名不算（失能态
            # 下把最后一个注册管理员也降级，Web 面就一个入口都不剩）
            if len(admins) <= 1 and not m._builtin_admin_loginable():
                return jsonify({"error": "至少保留 1 个管理员"}), 400
        # 改走事务内复核的 set_user_role——进程内预检挡不住
        # 跨进程并发（多实例）同时把最后一个注册管理员降权
        try:
            changed = m.db.set_user_role(
                email, new_role, allow_last_admin=m._builtin_admin_loginable(),
                # 审计与角色 UPDATE 同事务：权限面变更必须与生效同事务落库，
                # 中间被杀不留"权限改了却无痕"。
                audit_spec={
                    "username": username,
                    "action": "user_role",
                    "target": m._mask_email(email),
                    "detail": f"角色 → {new_role}",
                },
            )
        except m.db.LastAdminError:
            return jsonify({"error": "至少保留 1 个管理员"}), 400
        if changed == 0:
            # 0 行 = 目标已被并发删除，不得谎报成功
            return jsonify({"error": "用户不存在"}), 404
        m.logger.info("主管理员 %s 将用户 %s 角色 → %s", m._mask_email(username), m._mask_email(email), new_role)
        # 提降权不再外发即时告警：留痕由上面的审计行承担（谁把谁改成了什么角色）。
        # 成功 msg 出站即脱敏（与日志/告警口径一致），完整邮箱不回显
        return jsonify(
            {
                "ok": True,
                "msg": f"{m._mask_email(email)} 已{'设为管理员' if new_role == 'admin' else '取消管理员'}",
            }
        )


def api_user_password(email):
    """重置用户密码（管理员无法查看原密码，只能设置新密码）。
    目标为注册管理员时仅主管理员可操作（防普通管理员横向接管）。"""
    m = _appmod()
    data = m._json_body()
    password = str(data.get("password", ""))
    pw_err = m._password_policy_error(password)
    if pw_err:
        return jsonify({"error": f"新密码不符合要求：{pw_err}"}), 400
    # 管理员重置他人密码 = 账号控制权转移 → 与批量重置同口径过高危门禁（口令 + 同管理员限速）；
    # 普通用户自改密码走 /api/me/password（验当前旧密码），不落本门禁。
    if m._current_role() == "admin":
        gate = high_risk_gate()(
            data, "重置用户密码", limit_msg="重置操作过于频繁，请稍后再试")
        if gate:
            return gate
    is_master = m._is_builtin_admin_session()
    with m._file_lock:
        target = m.db.find_user(email)
        if not target:
            return jsonify({"error": "用户不存在"}), 404
        if target.get("role") == "admin" and not is_master:
            return jsonify({"error": "仅主管理员可重置管理员密码"}), 403
        if m.db.update_user(
            email,
            {
                "password_hash": m.generate_password_hash(password, method=m.SCRYPT_METHOD),
                "pw_version": target.get("pw_version", 1) + 1,  # 被重置用户的旧会话随之失效
            },
            # 审计与口令 UPDATE 同事务：管理员重置他人密码是账号控制权转移，
            # 生效与留痕必须同生共死。
            audit_spec={
                "username": session.get("username") or "?",
                "action": "user_password_reset",
                "target": m._mask_email(email),
                "detail": "管理员重置密码",
            },
        ) == 0:
            # 0 行 = 目标已被并发删除
            return jsonify({"error": "用户不存在"}), 404
        # 轮换目标 sid，被盗 cookie 即便未因 pw_version 失效（如
        # 旧版本客户端）也双重确保吊销
        m.db.set_user_sid(email.strip().lower(), secrets.token_hex(16))
        m.logger.info("已重置用户 %s 密码", m._mask_email(email))
        # 重置他人密码不再外发即时告警：留痕由上面的审计行承担（谁重置了谁），
        # 目标用户的旧会话已随 sid 轮换失效，管理操作逐条发信只会把告警刷成操作日志。
        return jsonify({"ok": True, "msg": f"{m._mask_email(email)} 密码已重置"})


def api_user_delete(email):
    """删除用户：mode=accounts_only 仅清空其易班账号（保留用户可重新提交）；
    mode=full 完全删除用户及其账号。
    目标为注册管理员时仅主管理员可操作（与 role/密码重置口径一致）。"""
    m = _appmod()
    data = m._json_body()
    mode = data.get("mode", "full")
    if mode not in ("accounts_only", "full"):
        return jsonify({"error": "未知操作"}), 400
    if email.strip().lower() == m._builtin_admin_email().strip().lower():
        return jsonify({"error": "内置管理员不可删除"}), 400
    is_master = m._is_builtin_admin_session()
    # accounts_only 也进门禁的理由：一次请求就把该用户**全部**易班凭据清零，滥用面与
    # full 同级；两种模式的响应语义对齐（口令错 400 / 未登录 401 / 超限与冷却 429）。
    gate = high_risk_gate()(  # 完全删除 = 高危不可逆：先过口令门禁（要口令随档位），通过了才占限速额度
        data,
        "完全删除用户" if mode == "full" else "清空用户账号",
        limit_msg="删除操作过于频繁，请稍后再试",
        # 两种模式都不可逆（full 连用户一起删，accounts_only 把其全部易班凭据清零）
        irreversible=True,
    )
    if gate:
        return gate
    with m._file_lock:
        target = m.db.find_user(email)
        if not target:
            return jsonify({"error": "用户不存在"}), 404
        if target.get("role") == "admin" and not is_master:
            return jsonify({"error": "仅主管理员可删除管理员"}), 403
        if mode == "full" and target.get("role") == "admin":
            admins = [u for u in m.load_users() if u.get("role") == "admin"]
            # 内置管理员可登录时才算"还有兜底入口"（判据见 _builtin_admin_loginable）
            if len(admins) <= 1 and not m._builtin_admin_loginable():
                return jsonify({"error": "至少保留 1 个管理员"}), 400
        # 删除其提交的易班账号（full 模式用单事务组合函数，防崩溃窗口不一致）
        # 审计随业务写同事务：删除不可逆，且一次请求即可清空该用户全部易班凭据，
        # 中途被杀不得留下"删了却无痕、欠账仍为 0"。
        delete_spec = {
            "username": session.get("username") or "?",
            "action": "user_delete",
            "target": m._mask_email(email),
            "detail": f"mode={mode}",
        }
        if mode == "full":
            # 事务内复核最后一个注册管理员（allow 与原预检同语义：
            # 内置管理员确实进得来时允许删掉 users 表最后一个注册管理员）
            try:
                m.db.delete_user_with_accounts(
                    email, allow_last_admin=m._builtin_admin_loginable(),
                    audit_spec=delete_spec,
                )
            except m.db.LastAdminError:
                return jsonify({"error": "至少保留 1 个管理员"}), 400
        else:
            m.db.delete_accounts_by_owner(email, audit_spec=delete_spec)
        if mode == "full":
            m.logger.info("完全删除用户 %s（含易班账号）", m._mask_email(email))
            # 完全删除不再外发即时告警：留痕由上面的审计行承担（谁、删了谁、mode）。
            return jsonify({"ok": True, "msg": f"{m._mask_email(email)} 已完全删除"})
        m.logger.info("清空用户 %s 的易班账号（保留用户）", m._mask_email(email))
        # 清空账号保留事后告警：一次请求即把该用户的**全部**易班凭据不可逆清零，
        # 而当事人未必立刻发现（不像完全删除那样连登录入口一起消失）。这条是
        # 非 full 档下"无当次口令"的补偿信号，与 irreversible 的声明配套。
        m.send_notification(
            "高危管理操作告警",
            m._change_mail(
                f"清空用户 {m._mask_email(email)} 的全部易班账号。",
                detail=[("连带", "其易班账号凭据被不可逆清除（用户保留，需重新提交）")],
                advice=["凭据清空不可恢复；如非本人申请，请核实操作者身份"],
            ),
            urgent=True,
        )
        return jsonify({"ok": True, "msg": f"{m._mask_email(email)} 的易班账号已清空（用户保留，可重新提交）"})


def register(app):
    """在本域注册七条用户管理路由；endpoint 取函数名（url_for 依赖）。"""
    app.add_url_rule("/api/users", view_func=api_users)
    app.add_url_rule("/api/users/deleted", view_func=api_users_deleted)
    app.add_url_rule("/api/users/deleted/purge", view_func=api_users_deleted_purge,
                     methods=["POST"])
    app.add_url_rule("/api/users/batch", view_func=api_users_batch, methods=["POST"])
    app.add_url_rule("/api/users/<email>/role", view_func=api_user_role, methods=["POST"])
    app.add_url_rule("/api/users/<email>/password", view_func=api_user_password,
                     methods=["POST"])
    app.add_url_rule("/api/users/<email>/delete", view_func=api_user_delete,
                     methods=["POST"])
