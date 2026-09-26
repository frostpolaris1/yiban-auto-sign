# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""账号域：accounts 表的 CRUD、行加解密与运行期有效性判定。

**功能**
- 行加解密与明文自愈：`_encrypt_field` / `_decrypt_row` / `_row_to_account` /
  `_is_encrypted_value`，以及把明文驻留行幂等加密回写的 `_apply_plaintext_heal`；
- 读路径：`accounts_snapshot`（不解密快照）、`decrypt_account_rows`（锁外解密）、
  `read_accounts`（AAD 失配重试一次）、`load_accounts`（全量解密）、
  `load_accounts_raw`（导出用，保持密文）；
- 写路径：`add_account` / `update_account` / `set_account_deleted` / `purge_account` /
  `update_account_status` / `set_user_paused` / `move_account` /
  `delete_accounts_by_owner` / `replace_accounts` / `batch_account_ops` /
  `update_account_status_if`；
- 有效性判定：`signs_in` / `is_signable` / `account_still_signable` 与孤儿会话缓存清理
  `purge_orphan_session_cache`。

**归属**
`yiban.store.db` 门面之后：定义点在本模块，门面把全部迁出名纳入模块级读写转发
（`db.add_account()` 一类调用、`db.add_account = 替身` 一类打桩与 `del db.add_account`
都落到这里）。

**复用**
`DuplicatePhoneError`（accounts.phone 唯一约束冲突）由 web 层捕获转 400，
`db.DuplicatePhoneError` 是唯一可见名字；`ACCOUNT_AUDIT_INACTIVE` 是审核态**落库值**的
唯一列举点，新增审核态时必须同步，否则新态的账号会被签到进程放行。

**通信**
连接、进程内锁与写事务入口（`_conn_lock` / `get_conn` / `_begin_immediate`），以及留在
db.py 的跨域助手（`_cascade_phone_owned` 连带清理、`_clear_session_cache_by_phones` 停用
凭据缓存）与共享异常 `DuplicateOwnerError` 一律经 `_facade()` 按属性取——必须按属性取而
非模块级 from-import，`mock.patch.object(db, "get_conn" / "_decrypt_row", …)` 一类打桩才会
在函数体里生效。密钥来源路径 `_connection._env_file` 直接读连接模块（`db._env_file = path`
的写入由 db 门面转发落到那里）。
"""
import contextlib
import json
import logging
import sqlite3

from yiban.infra import account_crypto
from yiban.store import connection as _connection

logger = logging.getLogger("yiban.store.accounts")

# 不参与签到的审核态（与 web 的 ACCOUNT_STATUS_PENDING/REJECTED 同值；此处是**库内
# 落库值**的唯一列举点，新增审核态时必须同步这里，否则新态的账号会被签到进程放行）。
ACCOUNT_AUDIT_INACTIVE = ("pending", "rejected")


def _facade():
    """db 门面（函数内延迟导入，避免与 `yiban.store.db` 形成导入环）。

    打桩可见性见模块说明：必须按**属性**取而不是模块级 from-import，`db.<名字> = 替身`
    才会在函数体里生效。
    """
    from yiban.store import db
    return db


class DuplicatePhoneError(Exception):
    """手机号已存在（accounts.phone 唯一约束冲突）。

    由本模块的 `_convert_integrity_error` 在写路径抛出，web 捕获转 400；
    定义点随 accounts 表放在本模块，`db.DuplicatePhoneError` 是唯一可见名字。
    """


def signs_in(row):
    """该账号行是否会发起易班签到请求（**容量/配额口径的唯一判据**）。

    审核态未通过（pending/rejected）的行永不签到——引擎加载
    （`yiban.engine.accounts._load_accounts_from_file`）与运行期复核（`is_signable`）
    用的是同一条条件。把它们计入容量会让"永不签到的存量"长期占满名额：新账号在提交时
    被「账号数量已达上限」误拒，而设置页/总览还会把它们显示成"正常"。

    行 dict 可能来自 `load_accounts_raw`（无 status 的旧档等于已通过审核）。user_paused
    （用户自暂停）**仍计入**：那是用户主动且一键可恢复的状态，账号仍在名册里。
    """
    return not row.get("deleted") and row.get("status") not in ACCOUNT_AUDIT_INACTIVE


def is_signable(account_id):
    """该账号当前是否仍可签到（行存在、未软删、审核态仍生效）。

    签到进程用**启动时的快照**跑完整轮（窗口最长 80 分钟），期间 web 端可能删除或停用
    账号。若只在启动时筛一次，运行期被删的账号仍会被登录签到，并把加密的会话缓存写回
    一个已不存在的账号——`session_cache` 的清理全按"现存账号行的 phone"驱动，那种行就
    成了**永久孤儿**。

    account_id 为 0/None（JSON / 环境变量账号模式：库内没有对应行）时返回 True——
    那些模式本来就不存在"库内账号行过期"的问题。
    """
    db = _facade()
    if not account_id:
        return True
    with db._conn_lock:
        row = db.get_conn().execute(
            "SELECT deleted, status FROM accounts WHERE id=?", (account_id,)
        ).fetchone()
    if row is None:
        return False
    return signs_in(dict(row))


def account_still_signable(account):
    """运行期复核：账号**对象**当前是否仍可签到（见 `is_signable`）。

    查询异常按"仍有效"处理——不因一次库抖动跳过全部账号；JSON/环境变量账号模式
    （account_id=0）恒为 True。查询本身即 `is_signable`（db 层已将同名函数导出为
    `account_is_signable`）。
    """
    account_id = getattr(account, "account_id", 0)
    if not account_id:
        return True
    try:
        return _facade().account_is_signable(account_id)
    except Exception as e:
        from yiban import masking
        logger.debug(f"[{getattr(account, 'phone', '')}] 账号有效性复核失败（按有效处理）: "
                     f"{masking.sanitize_text(e)}")
        return True


def purge_orphan_session_cache(conn):
    """清除"账号行已不存在"的会话缓存（孤儿行；须在调用方事务内）。

    账号物理删除已由 `db._cascade_phone_owned` 连带清理 session_cache，但仍有一种窄
    竞态残留：登录完成时账号行刚被物理清除，写入发生在连带清理之后。会话缓存只在库内
    账号模式下写入，故"phone 不在 accounts 里"即为定义上的孤儿。返回删除行数。
    """
    cur = conn.execute(
        "DELETE FROM session_cache WHERE phone NOT IN (SELECT phone FROM accounts)"
    )
    return cur.rowcount


# ---------------------------------------------------------------------------
# 行加解密与明文自愈
# ---------------------------------------------------------------------------
def _mask_phone_display(phone):
    """展示用打码（仅用于日志文案，与 web 层同口径）。"""
    return phone[:3] + "****" + phone[7:] if len(phone) == 11 else phone


def _decrypt_row(row):
    """纯 CPU：把一行原始行转成账号 dict，并摘出需要明文自愈的字段。

    **不访问数据库、不加锁**。调用方负责在 `_conn_lock` **之外**调用它——逐行 AES-GCM
    解密若全程持锁，web 侧数十个调用点会把全站 DB 访问串行化——再把摘出的 pending
    交给 `_apply_plaintext_heal` 在锁内落库。

    返回 `(account_dict, pending)`；pending 元素为
    `(字段名, 行 id, 明文原值, 手机号, 打码手机号)`。
    """
    a = dict(row)
    a["deleted"] = bool(a["deleted"])
    a["user_paused"] = bool(a.get("user_paused", 0))  # 用户自暂停签到（调度 v2）
    pending = []
    # 密文解密（password/phone_code 存 JSON 串；解密失败抛明确错误，绝不静默降级）
    for k in ("password", "phone_code"):
        v = a.get(k)
        if not v:
            continue
        try:
            obj = json.loads(v)
        except (TypeError, ValueError):
            obj = None
        if isinstance(obj, dict) and "ct" in obj:
            if not account_crypto.has_key(_connection._env_file):
                raise RuntimeError(
                    "账号已加密但未配置 YIBAN_ACCOUNTS_KEY（请在 .env 配置或恢复密钥备份）"
                )
            key = account_crypto.load_key(_connection._env_file)
            try:
                a[k] = account_crypto.decrypt_password(obj, key, a.get("phone", ""))
            except ValueError as e:
                # 统一收口：解密失败（密钥不匹配/密文损坏）→ RuntimeError，
                # 与密钥缺失分支一致，由 web 层统一 JSON 错误处理。
                raise RuntimeError(str(e)) from e
        else:
            # 明文驻留检测：非密文值照常使用（不阻断业务），但必须告警 + 幂等加密回写
            # ——堵住"明文已进库"无人察觉；对照 session_cache 对旧明文行抛错清除。
            phone = str(a.get("phone", ""))
            pending.append((k, a["id"], v, phone, _mask_phone_display(phone)))
            a[k] = v
    return a, pending


def _apply_plaintext_heal(conn, pending):
    """锁内：对明文驻留字段做 CAS 加密回写（幂等；并发修改时跳过并告警）。"""
    for k, account_id, plain, phone, masked in pending:
        enc = _encrypt_field(plain, phone)
        # CAS 回写——并发进程可能刚改掉该行（如 update_account 改密），无条件按 id
        # 覆盖会把旧明文重新加密写回，静默回滚他人修改。以"仍处于本进程读到的明文
        # 原值"为条件，0 行命中即放弃并告警。
        cur = conn.execute(
            f"UPDATE accounts SET {k}=? WHERE id=? AND {k}=?",
            (enc, account_id, plain),
        )
        if cur.rowcount == 0:
            logger.warning("账号 %s 的 %s 已被并发修改，跳过明文自愈回写", masked, k)
            continue
        logger.warning(
            "账号 %s 的 %s 为明文存储（迁移残留/手工改库/第三方写入），已自动加密回写",
            masked, k,
        )


def _row_to_account(row, conn=None):
    """单行转换（更新路径用）：conn 非空时顺带做明文自愈回写。"""
    a, pending = _decrypt_row(row)
    if conn is not None:
        _apply_plaintext_heal(conn, pending)
        return a
    for k, _account_id, _plain, _phone, masked in pending:
        logger.warning(
            "账号 %s 的 %s 为明文存储；本次读取未持连接上下文，未回写，"
            "将在下次带连接的读取时自动加密（现有调用方均传连接，此为防御分支）",
            masked, k,
        )
    return a


def _is_encrypted_value(v):
    """字段值是否为密文（dict 密文对象，或密文 JSON 串）——迁移/写路径判定用。"""
    if isinstance(v, dict):
        return account_crypto.is_encrypted(v)
    if isinstance(v, str):
        try:
            obj = json.loads(v)
        except (TypeError, ValueError):
            return False
        return account_crypto.is_encrypted(obj)
    return False


def _encrypt_field(value, phone):
    """写库前密文化：dict 密文对象 → JSON 串；其他非空值 → AES-GCM 加密（AAD=phone）→ JSON 串；空值原样。

    无密钥时 load_key 自动生成并持久化（与 web 现状一致）；密钥非法则抛错（绝不静默降级明文）。
    """
    if not value:
        return ""
    if isinstance(value, dict):
        return json.dumps(value)  # 已是密文对象
    key = account_crypto.load_key(_connection._env_file)
    return json.dumps(account_crypto.encrypt_password(str(value), key, phone))


# ---------------------------------------------------------------------------
# 读路径（快照 / 锁外解密）
# ---------------------------------------------------------------------------
def accounts_snapshot():
    """账号原始行快照（**不解密**）。持 `_conn_lock` 取到即释放。

    与 `decrypt_account_rows` 配对使用，让调用方能把 CPU 密集的解密放到锁外：
    调用方只需在自己那一层护住"取快照"这一步（web 层是 `_file_lock`）。
    """
    db = _facade()
    with db._conn_lock:
        conn = db.get_conn()
        return [
            {**dict(r), "deleted": bool(r["deleted"])}
            for r in conn.execute("SELECT * FROM accounts ORDER BY sort_order").fetchall()
        ]


def load_accounts_raw():
    """账号原始行（password/phone_code 保持密文 JSON 串，不解密）。

    供 db_export 等导出场景使用：避免生成明文凭据文件。
    """
    return accounts_snapshot()


def decrypt_account_rows(rows):
    """把 `accounts_snapshot()` 的结果解密为账号列表。

    解密全程在**锁外**（纯 CPU，不碰连接）；仅当发现明文驻留行时才另取一次
    短 `_conn_lock` 做 CAS 自愈回写。
    """
    accts = []
    pending = []
    try:
        for r in rows:
            a, p = _decrypt_row(r)
            accts.append(a)
            pending.extend(p)
    except Exception:
        # 解密中途抛错（如某行密文损坏）：本函数此刻尚未写库，但并发的写操作可能在
        # 本进程共享连接上留下未提交的隐式事务；不回滚会让后续所有
        # `BEGIN IMMEDIATE` 写路径报 "cannot start a transaction within a
        # transaction"，夜间事件落库等连锁失效。先回滚清场再原样抛出。
        db = _facade()
        with db._conn_lock, contextlib.suppress(Exception):
            db.get_conn().rollback()
        raise
    if pending:
        db = _facade()
        with db._conn_lock:
            conn = db.get_conn()
            _apply_plaintext_heal(conn, pending)
            conn.commit()
    return accts


def read_accounts(snapshot):
    """取快照 → 锁外解密，并在 AAD 失配时重取一次快照重试。

    `snapshot` 是零参可调用对象，返回 `accounts_snapshot()` 的结果（调用方负责它自己
    那一层的锁语义：db 层传 `accounts_snapshot` 自身，web 层在 `_file_lock` 内取）。

    **为什么要重试**：解密移出 `_conn_lock` 后，读到的快照可能已被并发写改过——
    改绑手机号会同时换掉 AAD，于是快照里的密文按新手机号（或反之）解不开，抛
    RuntimeError。这类失败重取一次快照即可消除；重试后仍失败即视为真实损坏
    （密文损坏/密钥不匹配），原样抛出，不掩盖问题。
    """
    for attempt in (0, 1):
        try:
            return decrypt_account_rows(snapshot())
        except RuntimeError:
            if attempt:
                raise


def load_accounts():
    """全部账号（按 sort_order 升序），已解密。

    「取快照」持 `_conn_lock`，**逐行 AES-GCM 解密在锁外**——此前解密全程持锁，而全
    项目有数十个调用点，使全站 DB 访问被串行化（实测 /api/accounts 恒定 28 rps 而
    CPU 仅 0.66 核 → 锁瓶颈而非 CPU 瓶颈）。明文自愈回写另取一次短锁（CAS 条件更新，
    与并发写安全）。

    注意：不在读路径顺带清除超期软删除行——读中途物理删行会使 idx 寻址的 mutation
    错位命中其他账号；清理改由 purge_expired_deleted_accounts() 在启动/每日线程/signin
    启动时显式执行。
    """
    return read_accounts(accounts_snapshot)


# ---------------------------------------------------------------------------
# 单行写路径（事务内）
# ---------------------------------------------------------------------------
def _next_sort_order(conn):
    row = conn.execute("SELECT COALESCE(MAX(sort_order), 0) + 1 AS n FROM accounts").fetchone()
    return row["n"]


def _convert_integrity_error(e):
    """把 sqlite3.IntegrityError 转换为可区分异常；无法识别则原样抛出。"""
    msg = str(e)
    if "accounts.owner" in msg:
        raise _facade().DuplicateOwnerError("该用户已有一个未删除账号") from e
    if "accounts.phone" in msg:
        raise DuplicatePhoneError("手机号已存在") from e
    raise e


def add_account(fields, audit_spec=None):
    """新增账号（fields 为业务层明文 dict），返回新 id。

    敏感字段写库前加密（AAD=手机号）；手机号重复抛 sqlite3.IntegrityError（业务层捕获）。
    BEGIN IMMEDIATE：跨进程（多 worker）并发时提前获取写锁，
    保证 MAX(sort_order)+1 的读与 INSERT 原子（防并发重复排序号）。

    audit_spec 非 None 时（dict：username/action/target/detail/request_id），审计行与本
    INSERT **同事务**写入，`db.record_in_txn` 失败即整体回滚——消除"账号已建、审计表
    却没有这条且欠账为 0"的静默丢失窗口（见 audit_chain.audit_unit）。
    """
    db = _facade()
    conn = db.get_conn()
    with db._conn_lock:
        try:
            db._begin_immediate(conn)
            cur = conn.execute(
                "INSERT INTO accounts (sort_order, name, phone, password, phone_model, phone_code, owner, status, reject_reason) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    _next_sort_order(conn),
                    fields.get("name", ""),
                    fields.get("phone", ""),
                    _encrypt_field(fields.get("password"), fields.get("phone", "")),
                    fields.get("phone_model", ""),
                    _encrypt_field(fields.get("phone_code"), fields.get("phone", "")),
                    fields.get("owner", "admin"),
                    fields.get("status", "pending"),
                    fields.get("reject_reason", ""),
                ),
            )
            new_id = cur.lastrowid
            if audit_spec:
                db.record_in_txn(conn, **audit_spec)
            conn.commit()
            return new_id
        except sqlite3.IntegrityError as e:
            conn.rollback()
            _convert_integrity_error(e)
        except Exception:
            conn.rollback()
            raise


def update_account(account_id, fields, expect_snapshot=None, audit_spec=None):
    """更新单行；expect_snapshot 为乐观锁指纹 dict（name/phone/phone_model/status/deleted），不匹配返回 False。

    手机号变更时自动用新手机号重加密 password/phone_code（旧密文 AAD 绑定旧手机号）；
    改 phone 撞 UNIQUE 抛 sqlite3.IntegrityError（业务层捕获）。

    整个读-改-写过程纳入 BEGIN IMMEDIATE 事务：原实现 SELECT（读行 + 解密）与 UPDATE
    之间跨进程无互斥（_conn_lock 仅进程内），中间还夹着解密与重加密——两个进程或两个
    标签页并发编辑同一账号时，后提交者静默覆盖前者。危险的是 web 用户自编辑路径不传
    expect_snapshot、且总把 old["password"] 回填，覆盖时可能把用户刚改的密码静默回滚。
    持锁后读-改-写原子，即使调用方不传乐观锁指纹，并发也不会丢更新。

    audit_spec 非 None 时，审计行与本次 UPDATE **同事务**写入（写入在 _body 内、由
    外层统一 commit）；未发生实际更新（快照不匹配/行不存在/无字段变化）不写审计——
    "没做的事不留痕"，也不会留下"留痕了却没做"的假记录。
    """
    db = _facade()
    conn = db.get_conn()

    def _body():
        """读-改-写事务体（在 BEGIN IMMEDIATE 写锁内执行）。

        拆出闭包是为了让事务边界清晰：外层统一 commit/rollback，内层只负责
        读-改-写。IntegrityError 分支需要"回滚主更新 → 重放密文自愈 → 独立提交"，
        故该分支自行控制事务（_convert_integrity_error 必定抛出，不会走到末尾）。
        """
        # 改绑手机号时体内会重新绑定 fields（补齐待重加密的敏感字段）：
        # 不加 nonlocal 会被当作 _body 的局部变量，首次读取即 UnboundLocalError。
        nonlocal fields
        cur = conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,))
        row = cur.fetchone()
        if row is None:
            return None if expect_snapshot is not None else False
        cur_a = _row_to_account(row, conn)  # 解密（AAD=库内当前手机号）；明文驻留自动加密回写
        if expect_snapshot is not None:
            snap = {
                "name": cur_a.get("name", ""),
                "phone": cur_a.get("phone", ""),
                "phone_model": cur_a.get("phone_model", ""),
                "status": cur_a.get("status", ""),
                "deleted": bool(cur_a.get("deleted")),
            }
            if snap != expect_snapshot:
                return False  # 已被他人修改（409）
        # 手机号变更且敏感字段未随本次提供 → 用旧手机号解密的明文按新手机号重加密
        new_phone = fields.get("phone")
        if new_phone is not None and new_phone != cur_a.get("phone"):
            # 改绑手机号：旧手机号的会话缓存随之失效（主键/AAD 均按旧号，不复用）
            db._clear_session_cache_by_phones(conn, [cur_a.get("phone", "")])
            for k in ("password", "phone_code"):
                if k not in fields:
                    fields = dict(fields)
                    fields[k] = cur_a.get(k, "")  # 已解密明文
        phone_for_aad = new_phone if new_phone is not None else cur_a.get("phone", "")
        sets = []
        vals = []
        for k in ("name", "phone", "phone_model", "owner", "status", "reject_reason", "deleted", "deleted_at"):
            if k in fields:
                sets.append(f"{k}=?")
                vals.append(fields[k])
        for k in ("password", "phone_code"):
            if k in fields:
                sets.append(f"{k}=?")
                vals.append(_encrypt_field(fields[k], phone_for_aad))
        if not sets:
            return True
        vals.append(account_id)
        try:
            conn.execute(f"UPDATE accounts SET {', '.join(sets)} WHERE id=?", vals)
            if audit_spec:
                # 与实际 UPDATE 同事务（外层 commit）：审计写失败即整体回滚，
                # 不会出现"凭据已改、审计表无此条"的静默丢失。
                db.record_in_txn(conn, **audit_spec)
        except sqlite3.IntegrityError as e:
            conn.rollback()
            # 主更新失败回滚会连带撤销 _row_to_account 的明文自愈；凭据不留明文优先，
            # 用已解密值重放自愈并独立提交（幂等，仅原行确为明文时生效）。
            for k in ("password", "phone_code"):
                raw = row[k]
                if raw and not _is_encrypted_value(raw) and cur_a.get(k):
                    conn.execute(
                        f"UPDATE accounts SET {k}=? WHERE id=?",
                        (_encrypt_field(cur_a[k], cur_a.get("phone", "")), account_id),
                    )
            conn.commit()
            _convert_integrity_error(e)
        return True

    with db._conn_lock:
        db._begin_immediate(conn)
        try:
            result = _body()
            conn.commit()
            return result
        except Exception:
            with contextlib.suppress(Exception):
                conn.rollback()
            raise


def set_account_deleted(account_id, deleted, deleted_at="", deleted_by="", audit_spec=None):
    """软删除/恢复账号；deleted_by 留痕删除来源（用户邮箱 / 'admin' / ''=系统），v10。

    恢复（deleted=0）时 deleted_by 一并清空，避免残留旧来源被后续语义误读。
    audit_spec（dict username/action/target/detail）非 None 时，审计行与本次 UPDATE
    同事务写入（`with conn` 退出时统一提交）：软删除不可逆程度不高但同属"改了谁"的
    追责点，同事务使"删了却无痕"不存在。
    """
    db = _facade()
    conn = db.get_conn()
    with db._conn_lock, conn:
        conn.execute(
            "UPDATE accounts SET deleted=?, deleted_at=?, deleted_by=? WHERE id=?",
            (1 if deleted else 0, deleted_at, deleted_by if deleted else "", account_id),
        )
        if audit_spec:
            db.record_in_txn(conn, **audit_spec)


def purge_account(account_id):
    db = _facade()
    conn = db.get_conn()
    with db._conn_lock, conn:
        row = conn.execute("SELECT phone FROM accounts WHERE id=?", (account_id,)).fetchone()
        conn.execute("DELETE FROM accounts WHERE id=?", (account_id,))
        if row is not None:
            db._cascade_phone_owned(conn, [row["phone"]])  # 连带清理自选/会话/事件/校验任务


def update_account_status(account_id, status, reject_reason=None):
    db = _facade()
    conn = db.get_conn()
    with db._conn_lock, conn:
        if reject_reason is None:
            conn.execute("UPDATE accounts SET status=? WHERE id=?", (status, account_id))
        else:
            conn.execute("UPDATE accounts SET status=?, reject_reason=? WHERE id=?", (status, reject_reason, account_id))


def set_user_paused(account_id, paused):
    """用户自暂停/恢复签到（user_paused 0/1）。"""
    db = _facade()
    conn = db.get_conn()
    with db._conn_lock, conn:
        conn.execute("UPDATE accounts SET user_paused=? WHERE id=?", (1 if paused else 0, account_id))


def move_account(account_id, direction):
    """direction: -1 上移 / 1 下移。事务内与相邻账号交换 sort_order。"""
    db = _facade()
    conn = db.get_conn()
    with db._conn_lock:
        try:
            db._begin_immediate(conn)
            rows = conn.execute(
                "SELECT id, sort_order FROM accounts WHERE deleted=0 ORDER BY sort_order"
            ).fetchall()
            pos = next((i for i, r in enumerate(rows) if r["id"] == account_id), None)
            if pos is None:
                conn.rollback()
                return False
            target = pos + direction
            if target < 0 or target >= len(rows):
                conn.rollback()
                return False
            a, b = rows[pos]["sort_order"], rows[target]["sort_order"]
            conn.execute("UPDATE accounts SET sort_order=? WHERE id=?", (b, rows[pos]["id"]))
            conn.execute("UPDATE accounts SET sort_order=? WHERE id=?", (a, rows[target]["id"]))
            conn.commit()
            return True
        except Exception:
            with contextlib.suppress(Exception):
                conn.rollback()
            raise


def delete_accounts_by_owner(owner, audit_spec=None):
    """删除某用户提交的全部易班账号（用户删除/清空账号用，事务内）。返回删除行数。

    audit_spec 非 None 时，审计行与本次 DELETE **同事务**写入（口径见 add_account）：
    一次请求清空该用户全部凭据属不可逆操作，同事务使"清了却无痕"不可能。
    """
    db = _facade()
    conn = db.get_conn()
    with db._conn_lock, conn:
        rows = conn.execute("SELECT phone FROM accounts WHERE owner=?", (owner,)).fetchall()
        cur = conn.execute("DELETE FROM accounts WHERE owner=?", (owner,))
        phones = [r["phone"] for r in rows]
        db._cascade_phone_owned(conn, phones)  # 自选/会话/事件/校验任务连带清理
        if audit_spec:
            db.record_in_txn(conn, **audit_spec)
        return cur.rowcount


def replace_accounts(accounts):
    """整表替换：事务内清空并重插，sort_order=列表顺序 1..N。

    敏感字段密文化同 add_account（AAD=手机号）。
    ⚠️ 整表替换语义：与 web 并发使用时以最后一次保存为准（勿与其他写入方同时编辑）。
    不再存在的账号连带清理自选时间片、会话缓存与事件（防孤儿 pref 虚高拥挤度、
    凭据残留、明文手机号驻留至保留期满）。
    """
    db = _facade()
    conn = db.get_conn()
    with db._conn_lock, conn:
        old = conn.execute("SELECT phone FROM accounts").fetchall()
        conn.execute("DELETE FROM accounts")
        keep = {a.get("phone", "") for a in accounts}
        removed = [r["phone"] for r in old if r["phone"] not in keep]
        # 只连带清理被移除账号的关联数据；保留者整表重插时不因此重新登录。
        db._cascade_phone_owned(conn, removed)
        for i, a in enumerate(accounts):
            try:
                conn.execute(
                    "INSERT INTO accounts (sort_order, name, phone, password, phone_model, phone_code, owner, status, reject_reason, deleted, deleted_at, user_paused) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        i + 1,
                        a.get("name", ""),
                        a.get("phone", ""),
                        _encrypt_field(a.get("password"), a.get("phone", "")),
                        a.get("phone_model", ""),
                        _encrypt_field(a.get("phone_code"), a.get("phone", "")),
                        a.get("owner", "admin"),
                        a.get("status", "active"),
                        a.get("reject_reason", ""),
                        1 if a.get("deleted") else 0,
                        a.get("deleted_at", ""),
                        1 if a.get("user_paused") else 0,
                    ),
                )
            except sqlite3.IntegrityError as e:
                conn.rollback()
                _convert_integrity_error(e)
    return len(accounts)


def batch_account_ops(ops):
    """在一个事务内批量执行账号操作（Phase 1：整体成功或整体回滚）。

    ops 为 (op, params) 列表，op 支持：
      ("update_status", account_id, status, reject_reason)
      ("set_deleted", account_id, deleted, deleted_at)
      ("purge", account_id)
    """
    db = _facade()
    conn = db.get_conn()
    with db._conn_lock:
        try:
            db._begin_immediate(conn)
            for op in ops:
                kind = op[0]
                if kind == "update_status":
                    _, account_id, status, reject_reason = op
                    conn.execute(
                        "UPDATE accounts SET status=?, reject_reason=? WHERE id=?",
                        (status, reject_reason, account_id),
                    )
                elif kind == "set_deleted":
                    _, account_id, deleted, deleted_at = op
                    try:
                        # 批量操作仅管理员可达：留痕 'admin'（v10 用户撤销仅限本人自删行）
                        conn.execute(
                            "UPDATE accounts SET deleted=?, deleted_at=?, deleted_by=? WHERE id=?",
                            (1 if deleted else 0, deleted_at, "admin", account_id),
                        )
                    except sqlite3.IntegrityError as e:
                        _convert_integrity_error(e)
                elif kind == "purge":
                    _, account_id = op
                    row = conn.execute(
                        "SELECT phone FROM accounts WHERE id=?", (account_id,)
                    ).fetchone()
                    conn.execute("DELETE FROM accounts WHERE id=?", (account_id,))
                    if row is not None:
                        db._cascade_phone_owned(conn, [row["phone"]])
                else:
                    raise ValueError(f"未知批量账号操作: {kind}")
            conn.commit()
        except Exception:
            with contextlib.suppress(Exception):
                conn.rollback()
            raise


def update_account_status_if(account_id, new_status, expect_status, reject_reason=None):
    """CAS 更新账号状态：仅当当前状态仍是 expect_status 时才写。返回是否写入。

    人类决定优先：管理员在异步校验执行期间审批（pending → active）后，迟到的
    校验结果不得把管理员的决定静默回滚；反向（管理员已拒绝）同样不覆盖，
    以保留管理员写的理由。按 id 定位（accounts.phone 全局唯一，但 id 不受改绑影响）。
    """
    db = _facade()
    conn = db.get_conn()
    with db._conn_lock, conn:
        if reject_reason is None:
            cur = conn.execute(
                "UPDATE accounts SET status=? WHERE id=? AND status=?",
                (new_status, account_id, expect_status),
            )
        else:
            cur = conn.execute(
                "UPDATE accounts SET status=?, reject_reason=? WHERE id=? AND status=?",
                (new_status, reject_reason, account_id, expect_status),
            )
        return cur.rowcount == 1
