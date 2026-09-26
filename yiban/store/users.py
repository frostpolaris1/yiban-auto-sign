# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""用户与注销域：users / user_delete_requests 两表的状态机与生命周期。

**功能**
- 账号体系（users 表）读写：`load_users` / `find_user` / `find_user_any` /
  `create_user` / `update_user` / `set_user_sid`，以及告警收件人过滤
  `filter_mail_notify` / `admin_mail_recipients`；
- 角色管理：`set_user_role` 与「最后一个注册管理员」守卫
  `_assert_not_last_admin` / `is_last_registered_admin`（复核在事务内，跨进程安全）；
- 注销与反悔：`soft_delete_user_with_accounts`（软注销 + 宽限期）与 `restore_user`
  （只恢复注销当时生效的那一个账号）；
- 到期物理清除与冷却计数：`purge_deleted_users` / `purge_deleted_users_hard` /
  `purge_old_delete_requests`，以及 `record_user_delete_request` /
  `count_user_delete_requests`；
- 批量入口 `batch_user_ops`（单事务，整体成功或整体回滚）。

**归属**
`yiban.store.db` 门面之后：定义点在本模块，门面按原样再导出（`db.load_users` /
`db.restore_user` / `db.LastAdminError` 等调用与身份断言不变）。注销宽限期常量
`SOFT_DELETE_RETENTION_DAYS` / `PURGE_SKIP_CANCELLED_OWNER` 也定义在这里——它们同时
约束用户行与账号行的清除时机。

**复用**
`DuplicateOwnerError`（accounts.owner 部分唯一索引冲突）与 `LastAdminError` 由 web 层
捕获转 400；`DuplicateOwnerError` 与 `accounts` 域抛出的对象同名同型（db 门面统一
再导出，`db.DuplicateOwnerError` 是唯一可见名字）。

**通信**
连接、锁与写事务入口（`_conn_lock` / `get_conn` / `_begin_immediate`）以及留在 db.py 的
跨域助手（`_cascade_phone_owned` 连带清理、`_clear_session_cache_by_phones` 停用凭据缓存、
`_clock_jump_guard` 时钟守卫、`_table_min_max` / `_record_purge_event` 清理留痕）一律经
`_facade()` 按属性取。必须按属性取而非模块级 from-import：`mock.patch.object(db, "get_conn"
/ "_conn_lock" / "find_user", …)` 一类打桩要求打桩点落在 db 门面上。
"""
import contextlib
import datetime
import logging

from yiban import clock

logger = logging.getLogger("yiban.store.users")


def _facade():
    """db 门面（函数内延迟导入，避免与 `yiban.store.db` 形成导入环）。

    打桩可见性见模块说明：必须按**属性**取而不是模块级 from-import，`db.<名字> = 替身`
    才会在函数体里生效。
    """
    from yiban.store import db
    return db


# 软删除保留期：天为唯一来源，秒数派生——web 的宽限期与此对齐，改一处必须同步另一处，
# 否则恢复窗口与物理清除时刻错位。
SOFT_DELETE_RETENTION_DAYS = 7
SOFT_DELETE_RETENTION_SECONDS = SOFT_DELETE_RETENTION_DAYS * 86400

# 账号物理清除的豁免条件：owner 已注销**且仍在反悔窗口内**时不清。
# 用户此前自删的账号时刻早于注销事件，按各自 deleted_at 独立到期会先于用户行被删，
# 而用户在窗口内恢复回来却没有账号（见 restore_user 的单行恢复说明）。
# 用同一个 cutoff 比较用户行自身的时间戳（而不是只看 deleted=1）：豁免随用户宽限期
# 自然失效——即使某个部署路径只清了账号没清用户（cron-only 的 signin 只调
# purge_expired_deleted_accounts），也不会留下无界驻留的账号；SQL 里它排在账号自己的
# `deleted_at <= ?` 之后，故调用参数必须传两次 cutoff。
PURGE_SKIP_CANCELLED_OWNER = (
    " AND owner NOT IN (SELECT email FROM users WHERE deleted=1 AND deleted_at > ?)"
)


class DuplicateOwnerError(Exception):
    """该用户已有一个未删除账号（accounts.owner 部分唯一索引冲突）。"""


class LastAdminError(Exception):
    """注销被拒绝：该用户是最后一个注册管理员（事务内复核，跨进程安全）。"""


def set_user_sid(email, sid):
    """写入用户当前有效会话标识；email 须为活跃用户。"""
    conn = _facade().get_conn()
    with _facade()._conn_lock, conn:
        conn.execute(
            "UPDATE users SET sid=? WHERE email=? AND deleted=0", (sid, email)
        )


# ---------------------------------------------------------------------------
# users 表读写
# ---------------------------------------------------------------------------
def load_users(include_deleted=False):
    """全部用户（默认排除已注销/软删除用户）。"""
    with _facade()._conn_lock:
        conn = _facade().get_conn()
        if include_deleted:
            rows = conn.execute("SELECT * FROM users ORDER BY id").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM users WHERE deleted=0 ORDER BY id"
            ).fetchall()
        return [dict(r) for r in rows]


def find_user(email):
    """查找有效（未注销）用户。"""
    with _facade()._conn_lock:
        conn = _facade().get_conn()
        row = conn.execute(
            "SELECT * FROM users WHERE email=? AND deleted=0", (email,)
        ).fetchone()
        return dict(row) if row else None


def find_user_any(email):
    """查找任意用户（含已注销），供恢复/管理排查使用。优先返回活跃用户。"""
    with _facade()._conn_lock:
        conn = _facade().get_conn()
        row = conn.execute(
            "SELECT * FROM users WHERE email=? ORDER BY deleted ASC, id DESC LIMIT 1", (email,)
        ).fetchone()
        return dict(row) if row else None


def filter_mail_notify(emails):
    """过滤出「接收邮件提醒」的邮箱列表（mail_notify=1 或非注册用户默认接收）。

    普通用户/管理员关闭 mail_notify 后，即使邮箱在 YIBAN_MAIL_ADMIN_TO 也不再接收告警；
    内置主管理员（.env 账号，users 表无记录）由全局开关 YIBAN_MAIL_ENABLE 控制；
    查库异常时按「接收」处理（不误伤收件人）。
    """
    result = []
    for e in emails:
        u = None
        try:
            # 经门面取：测试以 `db.find_user = 替身` 打桩时必须落在真调用点上
            u = _facade().find_user(e)
        except Exception:
            u = None
        if u is None or str(u.get("mail_notify", 1)).strip().lower() in ("1", "true", "on", "yes"):
            result.append(e)
    return result


def admin_mail_recipients(extra_emails=()):
    """告警邮件的完整收件人列表（去重）。

    组成 = .env 的 ADMIN_TO（经 filter_mail_notify 按个人开关过滤）+ 所有开启接收邮件的
    管理员用户邮箱（role=admin 且 mail_notify=1）。普通管理员因此自动获得收件权，关闭
    mail_notify 后即被剔除；内置主管理员（不在 users 表）由 extra_emails 覆盖。
    """
    recipients = set(filter_mail_notify(extra_emails))
    try:
        with _facade()._conn_lock:
            conn = _facade().get_conn()
            rows = conn.execute(
                "SELECT email, mail_notify FROM users WHERE role='admin' AND deleted=0"
            ).fetchall()
    except Exception as e:
        rows = []
        # 留痕：否则库故障时普通管理员被无声剔出告警收件人，事后无从回答"为何没人收到告警"
        # （降级仍保留 .env 配置的 ADMIN_TO）
        logger.warning("查询管理员告警收件人失败，仅保留 .env 配置的收件人: %s", e)
    for r in rows:
        if str(r["mail_notify"] if r["mail_notify"] is not None else 1).strip().lower() in ("1", "true", "on", "yes"):
            recipients.add(r["email"])
    return sorted(recipients)


def create_user(email, password_hash, role="user", created_at="", pw_version=1,
                audit_spec=None):
    """新增用户（INSERT OR IGNORE + rowcount 判断是否实际创建，返回是否创建）。

    audit_spec 非 None 时（dict：username/action/target/detail/request_id），审计行与本次
    INSERT **同事务**写入——**实际创建才写**（OR IGNORE 未创建时无业务效果，不留痕）；
    审计写失败即整体回滚，消除"用户已建、审计表却没有这条且欠账为 0"的静默丢失窗口
    （见 audit_chain.record_in_txn）。
    """
    conn = _facade().get_conn()
    with _facade()._conn_lock, conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO users (email, password_hash, role, created_at, pw_version, deleted, deleted_at) "
            "VALUES (?,?,?,?,?,0,'')",
            (email, password_hash, role, created_at, pw_version),
        )
        created = cur.rowcount > 0
        if created and audit_spec:
            _facade().record_in_txn(conn, **audit_spec)
        return created


def update_user(email, fields, audit_spec=None):
    """更新用户字段；返回受影响行数。

    返回 rowcount 而不是 None：调用方要能区分"更新成功"与"邮箱不存在/已注销"的静默
    no-op——邮件通知开关等接口对内置管理员（不在 users 表）不得谎报成功。

    audit_spec 非 None 且实际命中行（rowcount>0）时，审计行与本次 UPDATE **同事务**
    写入（口径见 create_user）。未命中行为不写审计——"没做的事不留痕"，也不会留下
    "留痕了却没做"的假记录。
    """
    conn = _facade().get_conn()
    with _facade()._conn_lock, conn:
        sets, vals = [], []
        for k in ("password_hash", "role", "pw_version", "mail_notify"):
            if k in fields:
                sets.append(f"{k}=?")
                vals.append(fields[k])
        if not sets:
            return 0
        vals.append(email)
        cur = conn.execute(
            f"UPDATE users SET {', '.join(sets)} WHERE email=? AND deleted=0", vals
        )
        if cur.rowcount > 0 and audit_spec:
            _facade().record_in_txn(conn, **audit_spec)
        return cur.rowcount


# ---------------------------------------------------------------------------
# 角色守卫与删除
# ---------------------------------------------------------------------------
def _assert_not_last_admin(conn, email, allow_last_admin):
    """「最后一个注册管理员不可删除/降权」复核——必须在 BEGIN IMMEDIATE 事务内调用。

    预检若只在 web 进程内做，多实例共享同一库时两名操作者可同时通过，把最后一个注册
    管理员清零（未配内置管理员则失去全部管理入口）。故复核下沉到事务内，单删/批量删/
    降权三条路径同口径，命中即抛 LastAdminError（调用方回滚、web 转 400）。

    allow_last_admin：内置管理员（.env）存在时允许删掉 users 表中最后一个注册管理员
    （web 侧传 bool(_builtin_admin_email())，与原预检语义一致）。
    """
    row = conn.execute(
        "SELECT role FROM users WHERE email=? AND deleted=0", (email,)
    ).fetchone()
    if row is not None and row["role"] == "admin" and not allow_last_admin:
        total = conn.execute(
            "SELECT COUNT(*) FROM users WHERE role='admin' AND deleted=0"
        ).fetchone()[0]
        if total <= 1:
            raise LastAdminError(
                "该用户是最后一个注册管理员，不可删除/降权（系统需保留至少一个管理入口）"
            )


def delete_user_with_accounts(email, allow_last_admin=False, audit_spec=None):
    """删除用户及其全部易班账号（单事务，防崩溃窗口数据不一致）。返回删除账号行数。

    allow_last_admin=False（默认）时事务内复核是否为最后一个注册管理员（含跨进程并发
    窗口），命中抛 LastAdminError 且库保持原状；仅「内置管理员存在」的调用方应显式传
    True。

    audit_spec 非 None 时，审计行与本次删除**同事务**写入（口径见 create_user）：删除
    不可逆，同事务使"删了却无痕"在同一事务内不可能（复核未过整体回滚时也不留痕）。
    """
    conn = _facade().get_conn()
    with _facade()._conn_lock:
        _facade()._begin_immediate(conn)
        try:
            _assert_not_last_admin(conn, email, allow_last_admin)
            rows = conn.execute("SELECT phone FROM accounts WHERE owner=?", (email,)).fetchall()
            cur = conn.execute("DELETE FROM accounts WHERE owner=?", (email,))
            phones = [r["phone"] for r in rows]
            _facade()._cascade_phone_owned(conn, phones)
            conn.execute("DELETE FROM users WHERE email=?", (email,))
            _delete_user_delete_requests(conn, email)  # 冷却计数连带清除
            if audit_spec:
                _facade().record_in_txn(conn, **audit_spec)
            conn.commit()
            return cur.rowcount
        except Exception:
            with contextlib.suppress(Exception):
                conn.rollback()
            raise


def set_user_role(email, new_role, allow_last_admin=False, audit_spec=None):
    """单行角色变更（降权最后一个注册管理员的复核下沉事务内）。

    返回受影响行数（0 = 用户不存在/已删除）；降权最后一个注册管理员且
    allow_last_admin=False 时抛 LastAdminError（库保持原状）。

    audit_spec 非 None 且实际命中行（rowcount>0）时，审计行与本次 UPDATE **同事务**
    写入（口径见 create_user）：权限面变更必须与生效同事务，未命中行不留痕。
    """
    conn = _facade().get_conn()
    with _facade()._conn_lock:
        _facade()._begin_immediate(conn)
        try:
            if new_role == "user":
                _assert_not_last_admin(conn, email, allow_last_admin)
            cur = conn.execute(
                "UPDATE users SET role=? WHERE email=? AND deleted=0", (new_role, email)
            )
            if cur.rowcount > 0 and audit_spec:
                _facade().record_in_txn(conn, **audit_spec)
            conn.commit()
            return cur.rowcount
        except Exception:
            with contextlib.suppress(Exception):
                conn.rollback()
            raise


# ---------------------------------------------------------------------------
# 注销（软删除 + 宽限期）与反悔恢复
# ---------------------------------------------------------------------------
def soft_delete_user_with_accounts(email):
    """软注销：标记用户 deleted=1，并软删除其易班账号。

    账号同样走软删除（而非物理删除），与账号级软删的 7 天保留语义对齐——宽限期内
    `restore_user` 可完整恢复（用户 + 账号）；软删账号不参与签到（signin 加载时过滤）。

    「最后一个注册管理员不可注销」在 BEGIN IMMEDIATE 后 COUNT 复核：web 进程内预检
    挡不住多容器/多写入方同时通过检查而双双注销，系统会失去全部管理入口（未配内置
    管理员时彻底无法进入）。命中即抛 LastAdminError（web 捕获转 400）。

    time_prefs 在软删阶段一律保留，仅在物理清除时按 phone 连带清理（与账号级软删路径
    一致）——可逆操作应完整可逆，且 prefs 行极小；time_pref_stats 本就按
    accounts.deleted=0 过滤，残留 pref 不会虚高拥挤度。

    **保留期口径**：只给"注销当时仍生效"的账号打当前时刻，用户此前自删的账号保留各自
    更早的时刻（那是"注销当时哪一行在生效"的唯一线索，不能覆盖）。由
    `_purge_expired_deleted` 的"owner 已注销则不清除"豁免保证它们活到用户反悔窗口结束，
    否则会按各自更早的时刻先被物理清除，用户恢复回来却没有账号。管理员删除的行
    （deleted_by='admin'）不在用户的反悔范围内。

    返回是否找到并注销了有效用户。
    """
    conn = _facade().get_conn()
    with _facade()._conn_lock:
        _facade()._begin_immediate(conn)
        try:
            row = conn.execute(
                "SELECT id, role FROM users WHERE email=? AND deleted=0", (email,)
            ).fetchone()
            if row is None:
                with contextlib.suppress(Exception):
                    conn.rollback()
                return False
            if row["role"] == "admin":
                total = conn.execute(
                    "SELECT COUNT(*) FROM users WHERE role='admin' AND deleted=0"
                ).fetchone()[0]
                if total <= 1:
                    with contextlib.suppress(Exception):
                        conn.rollback()
                    raise LastAdminError(
                        "该用户是最后一个注册管理员，不可注销（系统需保留至少一个管理入口）"
                    )
            rows = conn.execute(
                "SELECT phone FROM accounts WHERE owner=? AND deleted=0", (email,)
            ).fetchall()
            now = clock.now().strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                "UPDATE accounts SET deleted=1, deleted_at=? WHERE owner=? AND deleted=0",
                (now, email),
            )
            # 自选时间片刻意保留至物理清除（见 docstring）；会话缓存仍即时停用——
            # 那是易班登录态凭据缓存，注销后保留会扩大凭据暴露面，恢复时重新登录即可
            _facade()._clear_session_cache_by_phones(conn, [r["phone"] for r in rows])
            conn.execute(
                "UPDATE users SET deleted=1, deleted_at=? WHERE id=?",
                (now, row["id"]),
            )
            conn.commit()
            return True
        except Exception:
            with contextlib.suppress(Exception):
                conn.rollback()
            raise


def restore_user(email):
    """撤销注销：仅当没有同邮箱活跃用户时，把最近一个已注销用户及其账号恢复。

    联动恢复该用户的软删账号（deleted=0），保证反悔恢复 = 用户 + 账号完整回来。
    检查-写入须在 BEGIN IMMEDIATE 写锁内：否则另一进程并发 create_user（同邮箱）可在
    检查通过后抢注成功，本 UPDATE 撞 idx_users_email_live 唯一索引抛 IntegrityError。
    只恢复「注销当时仍生效」的那一行账号（每人限 1 个，故至多一行）：
    - 它是注销时刻最新的软删行 → `ORDER BY deleted_at DESC, id DESC LIMIT 1` 等价于
      「哪一行在注销时生效」；
    - 必须**单行**更新：多行一起置 deleted=0 会当场撞 idx_accounts_owner_live（同一
      owner 只能有一个未删除账号）→ 500；此前自删的其余账号保持软删，用户在「我的
      账号」页可逐个撤销（那时有明确的名额提示）；
    - 用 `<=` 而非等值：兼容旧版本写下的时间戳错位存量行（无需数据迁移），「先自删唯一
      账号再注销」正是这种形态，等值匹配会让用户恢复后一个账号都没有；
    - 排除 deleted_by='admin'（管理员清退不属于用户的反悔范围）与 deleted_at=''（旧
      僵尸行无法判定归属，由清理补记时间后自然到期）。
    """
    conn = _facade().get_conn()
    with _facade()._conn_lock:
        _facade()._begin_immediate(conn)
        try:
            active = conn.execute(
                "SELECT id FROM users WHERE email=? AND deleted=0", (email,)
            ).fetchone()
            if active is not None:
                with contextlib.suppress(Exception):
                    conn.rollback()
                return False
            deleted = conn.execute(
                "SELECT id, deleted_at FROM users WHERE email=? AND deleted=1 "
                "ORDER BY id DESC LIMIT 1",
                (email,),
            ).fetchone()
            if deleted is None:
                with contextlib.suppress(Exception):
                    conn.rollback()
                return False
            conn.execute(
                "UPDATE users SET deleted=0, deleted_at='' WHERE id=?",
                (deleted["id"],),
            )
            row = conn.execute(
                "SELECT id FROM accounts WHERE owner=? AND deleted=1 AND deleted_by != 'admin' "
                "AND deleted_at != '' AND deleted_at <= ? "
                "ORDER BY deleted_at DESC, id DESC LIMIT 1",
                (email, deleted["deleted_at"]),
            ).fetchone()
            if row is not None:
                conn.execute(
                    "UPDATE accounts SET deleted=0, deleted_at='', deleted_by='' WHERE id=?",
                    (row["id"],),
                )
            conn.commit()
            return True
        except Exception:
            with contextlib.suppress(Exception):
                conn.rollback()
            raise


# ---------------------------------------------------------------------------
# 到期物理清除与注销请求记录
# ---------------------------------------------------------------------------
def purge_deleted_users(days=None):
    """物理清除超过宽限期的已注销用户（默认取 SOFT_DELETE_RETENTION_DAYS）；失败仅告警。

    保留期与账号侧共用同一常量：web 的恢复宽限期、账号物理清除、用户物理清除三者若各
    持一份"7"，运维按注释调整时会静默吞掉数据——第 7 天用户与账号应同天清除，邮箱/
    手机号同时释放，消除「反悔窗口内资产被抢占」的风险。时钟跳变只跳本轮清理。
    """
    days = SOFT_DELETE_RETENTION_DAYS if days is None else days
    try:
        conn = _facade().get_conn()
        with _facade()._conn_lock, conn:
            ok, note = _facade()._clock_jump_guard(conn, "purge_users_clock")
            if not ok:
                logger.error("%s", note)
                return
            cutoff = (clock.now() - datetime.timedelta(days=days)).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            # 先清这些已注销用户的冷却计数：明文邮箱随用户行一并释放，
            # 不再驻留 user_delete_requests 至 30 天保留期满
            before = _facade()._table_min_max(conn, "user_delete_requests")
            cur = conn.execute(
                "DELETE FROM user_delete_requests WHERE username IN ("
                "SELECT email FROM users WHERE deleted=1 AND deleted_at != '' AND deleted_at <= ?)",
                (cutoff,),
            )
            _facade()._record_purge_event(
                conn, "user_delete_requests", "purge_deleted_users", cutoff,
                cur.rowcount or 0, before, _facade()._table_min_max(conn, "user_delete_requests"),
            )
            before = _facade()._table_min_max(conn, "users")
            cur = conn.execute(
                "DELETE FROM users WHERE deleted=1 AND deleted_at != '' AND deleted_at <= ?",
                (cutoff,),
            )
            _facade()._record_purge_event(
                conn, "users", "purge_deleted_users", cutoff, cur.rowcount or 0,
                before, _facade()._table_min_max(conn, "users"),
            )
            conn.commit()
    except Exception as e:
        with contextlib.suppress(Exception):
            conn.rollback()
        logger.warning("清理已注销用户失败: %s", e)


def purge_deleted_users_hard(emails, audit_spec=None):
    """管理员手动物理清除指定的已注销用户（不等 7 天自动清除）。

    安全边界：仅处理 deleted=1 的用户行——传入活跃用户邮箱时直接跳过（误操作/并发注册
    新同邮箱用户都不可能误删活跃数据）；账号行只删 deleted=1 的软删账号，活跃账号跳过，
    避免误删用户注销后重新添加的账号。单事务连带清理这些账号的 time_prefs / 会话 /
    事件（_cascade_phone_owned）与用户行。返回实际清除的邮箱列表（供审计与回显）。

    audit_spec 非 None 时（dict：username/action，可带 request_id；target/detail 缺省由
    本函数按**实际清除结果**在事务内补齐），审计行与本次清除**同事务**写入（口径见
    create_user）：清除清单要跑完才知道，留痕若落在提交之后，进程在两个事务之间被杀
    就留下"用户已消失、审计表无此条、欠账仍为 0"。一行未清不留痕。
    """
    if not emails:
        return []
    conn = _facade().get_conn()
    purged = []
    with _facade()._conn_lock:
        # BEGIN IMMEDIATE 写锁内完成"读 phones → 删账号 → 连带清理"，与 restore_user
        # 跨进程串行化：deferred 快照下 phones 列表可能陈旧（并发 restore 后升级写会
        # 表现为 BUSY_SNAPSHOT 500，连带清理列表陈旧）；持 IMMEDIATE 后写锁期间列表
        # 不可能变化。
        _facade()._begin_immediate(conn)
        try:
            for email in emails:
                row = conn.execute(
                    "SELECT id FROM users WHERE email=? AND deleted=1", (email,)
                ).fetchone()
                if row is None:
                    continue
                phones = [
                    r["phone"]
                    for r in conn.execute(
                        "SELECT phone FROM accounts WHERE owner=? AND deleted=1", (email,)
                    ).fetchall()
                ]
                conn.execute("DELETE FROM accounts WHERE owner=? AND deleted=1", (email,))
                _facade()._cascade_phone_owned(conn, phones)
                # DELETE 复核 deleted=1——SELECT 与 DELETE 之间用户可能被并发 restore
                # （跨进程/多 worker），无条件按 id 删会物理删除刚恢复的用户
                cur = conn.execute(
                    "DELETE FROM users WHERE id=? AND deleted=1", (row["id"],)
                )
                if cur.rowcount > 0:
                    purged.append(email)
                    _delete_user_delete_requests(conn, email)  # 冷却计数连带清除
            if purged and audit_spec:
                # target/计数在事务内按实际清除结果产出：按"请求清单"写会把被
                # 跳过的（非已注销）项也留痕成清除过。
                spec = dict(audit_spec)
                spec.setdefault("target", ",".join(purged))
                spec.setdefault(
                    "detail",
                    f"管理员手动清除 {len(purged)} 个已注销用户（含其易班账号与自选时间）",
                )
                _facade().record_in_txn(conn, **spec)
            conn.commit()
        except Exception:
            with contextlib.suppress(Exception):
                conn.rollback()
            raise
    return purged


def purge_old_delete_requests(days=30):
    """物理清除超过保留期的注销请求记录（默认 30 天）；失败仅告警。

    user_delete_requests 只增不删会无限累积（长期使用后 count 查询变慢、库体积膨胀），
    故随每日清理一并删。时钟跳变只跳本轮清理。
    """
    try:
        conn = _facade().get_conn()
        with _facade()._conn_lock, conn:
            ok, note = _facade()._clock_jump_guard(conn, "purge_requests_clock")
            if not ok:
                logger.error("%s", note)
                return
            cutoff = (clock.now() - datetime.timedelta(days=days)).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            conn.execute(
                "DELETE FROM user_delete_requests WHERE created_at <= ?",
                (cutoff,),
            )
            conn.commit()
    except Exception as e:
        with contextlib.suppress(Exception):
            conn.rollback()
        logger.warning("清理注销请求记录失败: %s", e)


def record_user_delete_request(username, ip_hash="", kind="delete"):
    """记录一次注销/恢复请求（供冷却/防批量使用；kind: delete=注销 / restore=恢复）。"""
    try:
        with _facade()._conn_lock:
            conn = _facade().get_conn()
            conn.execute(
                "INSERT INTO user_delete_requests (username, ip_hash, created_at, kind) "
                "VALUES (?,?,?,?)",
                (
                    username or "",
                    ip_hash or "",
                    clock.now().strftime("%Y-%m-%d %H:%M:%S"),
                    kind if kind in ("delete", "restore") else "delete",
                ),
            )
            conn.commit()
    except Exception as e:
        with contextlib.suppress(Exception):
            conn.rollback()
        logger.warning("记录注销请求失败: %s", e)


def count_user_delete_requests(username=None, ip_hash=None, since_ts=None, kind=None):
    """统计窗口内注销/恢复请求次数（用户或 IP 维度；kind=None 统计全部）。

    计数类查询 fail-closed：异常包装为 RuntimeError 由上层统一处理。
    """
    try:
        with _facade()._conn_lock:
            conn = _facade().get_conn()
            sql = "SELECT COUNT(*) FROM user_delete_requests WHERE 1=1"
            params = []
            if username:
                sql += " AND username=?"
                params.append(username)
            if ip_hash:
                sql += " AND ip_hash=?"
                params.append(ip_hash)
            if since_ts:
                sql += " AND created_at >= ?"
                params.append(since_ts)
            if kind:
                sql += " AND kind=?"
                params.append(kind)
            row = conn.execute(sql, params).fetchone()
            return row[0] if row else 0
    except Exception as e:
        raise RuntimeError(f"统计注销请求失败: {e}") from e


def is_last_registered_admin(email):
    """判断该邮箱是否是最后一个注册管理员（不含 .env 内置管理员）。"""
    with _facade()._conn_lock:
        conn = _facade().get_conn()
        row = conn.execute(
            "SELECT COUNT(*) FROM users WHERE role='admin' AND deleted=0"
        ).fetchone()
        total = row[0] if row else 0
        target = conn.execute(
            "SELECT id FROM users WHERE email=? AND role='admin' AND deleted=0",
            (email,),
        ).fetchone()
        return target is not None and total <= 1


def batch_user_ops(ops, audit_spec=None):
    """在一个事务内批量执行用户操作（整体成功或整体回滚）。

    ops 为 (op, params) 列表，op 支持：
      ("update_user", email, fields_dict)          # role/password_hash/pw_version
      ("update_user", email, fields_dict, allow_last_admin)  # 降权含
                                                    # 最后管理员事务内复核的放行开关
      ("delete_user_with_accounts", email)
      ("delete_user_with_accounts", email, allow_last_admin)  # 同上

    audit_spec 非 None 时，审计行与整批操作**同事务**写入（口径见 create_user）：
    批量重置口令/删除是凭据路径，同事务使"整批生效却无痕"不可能；任一 op 失败整体
    回滚时审计行同样不落（拒绝/失败另由调用方单独留痕）。
    """
    conn = _facade().get_conn()
    with _facade()._conn_lock:
        try:
            _facade()._begin_immediate(conn)
            for op in ops:
                kind = op[0]
                if kind == "update_user":
                    email, fields = op[1], op[2]
                    # 角色降级经同一事务内复核（预检在 web 进程内，
                    # 挡不住跨进程并发把最后一个注册管理员降权）
                    if fields.get("role") == "user":
                        _assert_not_last_admin(conn, email, op[3] if len(op) > 3 else False)
                    sets, vals = [], []
                    for k in ("password_hash", "role", "pw_version"):
                        if k in fields:
                            sets.append(f"{k}=?")
                            vals.append(fields[k])
                    if not sets:
                        continue
                    vals.append(email)
                    conn.execute(
                        f"UPDATE users SET {', '.join(sets)} WHERE email=? AND deleted=0",
                        vals,
                    )
                elif kind == "delete_user_with_accounts":
                    email = op[1]
                    # 删除管理员前事务内复核最后管理员
                    _assert_not_last_admin(conn, email, op[2] if len(op) > 2 else False)
                    rows = conn.execute(
                        "SELECT phone FROM accounts WHERE owner=?", (email,)
                    ).fetchall()
                    conn.execute("DELETE FROM accounts WHERE owner=?", (email,))
                    phones = [r["phone"] for r in rows]
                    _facade()._cascade_phone_owned(conn, phones)
                    conn.execute("DELETE FROM users WHERE email=?", (email,))
                    _delete_user_delete_requests(conn, email)
                else:
                    raise ValueError(f"未知批量用户操作: {kind}")
            if audit_spec:
                _facade().record_in_txn(conn, **audit_spec)
            conn.commit()
        except Exception:
            with contextlib.suppress(Exception):
                conn.rollback()
            raise


def _delete_user_delete_requests(conn, email):
    """连带清除某邮箱的注销/恢复冷却计数（用户被物理删除时调用）。

    否则用户行物理删除后其明文邮箱仍驻留 user_delete_requests 至 30 天保留期满。
    """
    if email:
        conn.execute("DELETE FROM user_delete_requests WHERE username=?", (email,))
