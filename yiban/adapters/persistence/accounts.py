# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""账号域：accounts 表的 CRUD、行加解密与运行期有效性判定。

**功能**
- 行加解密与明文自愈：`_encrypt_field` / `_decrypt_row` / `_row_to_account` /
  `_is_encrypted_value`，以及把明文驻留行幂等加密回写的 `_apply_plaintext_heal`；
- 读路径：`accounts_snapshot`（不解密快照）、`decrypt_account_rows`（锁外解密）、
  `read_accounts`（AAD 失配重试一次）、`load_accounts`（全量解密）、
  `load_accounts_raw`（导出用，保持密文）、`account_row_at` / `read_account_at`
  （按整表绝对下标单行窄取，只解密一行）；
- 写路径：`add_account` / `update_account` / `set_account_deleted` / `purge_account` /
  `update_account_status` / `set_user_paused` / `move_account` /
  `delete_accounts_by_owner` / `replace_accounts` / `batch_account_ops` /
  `update_account_status_if`；
- 有效性判定：`signs_in` / `is_signable` / `account_still_signable` 与孤儿会话缓存清理
  `purge_orphan_session_cache`。

**归属**
`yiban.adapters.persistence.db` 门面之后：定义点在本模块，门面把全部迁出名纳入模块级读写转发
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

from yiban.adapters.config import account_crypto
from yiban.adapters.persistence import connection as _connection
from yiban.core.masking import mask_phone as _mask_phone

logger = logging.getLogger("yiban.store.accounts")

# 不参与签到的审核态（与 web 的 ACCOUNT_STATUS_PENDING/REJECTED 同值；此处是**库内
# 落库值**的唯一列举点，新增审核态时必须同步这里，否则新态的账号会被签到进程放行）。
ACCOUNT_AUDIT_INACTIVE = ("pending", "rejected")

#: `account_still_signable` 的异常留痕是否已报过（进程内一次）。与
#: `claims._pool_down_notified` 同法：同一场库故障只写一条 ERROR、只进一次当日汇总，
#: 防逐账号刷屏把真正的信号淹掉。
_validity_check_failure_reported = False

#: `account_still_signable` 的**查询异常哨兵**：`None` = 账号状态未知（查询本身失败）。
#: 调用方一律按 fail-closed 拒绝发起真实登录；但文案必须与"确实不可签"区分——把一次
#: 库错误显示成"账号已被删除或停用"是归因错误（工单 ba-p02-06 同族残余）。刻意用
#: `None` 而不是第二个布尔：`if not account_still_signable(...)` 的既有调用点自动
#: 继续 fail-closed，只有需要区分文案的调用点才显式判 `is None`。
VALIDITY_UNKNOWN = None


def _facade():
    """db 门面（函数内延迟导入，避免与 `yiban.adapters.persistence.db` 形成导入环）。

    打桩可见性见模块说明：必须按**属性**取而不是模块级 from-import，`db.<名字> = 替身`
    才会在函数体里生效。
    """
    from yiban.adapters.persistence import db
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

    **查询异常按 fail-closed 拒绝**（工单 ba-p02-06）：旧实现返回 True，理由是"不因一次
    库抖动跳过全部账号"；但那等于在库抖动/连接断开期间让管理员刚删除或停用的账号继续
    被真实登录（静默多签，且现场只留 DEBUG 行，事后无法回答"是谁在什么状态下签的"）。
    拿不到账号状态就不做有风控暴露面的动作——与 `claims.notify_pool_down` 的
    fail-closed 同口径。留痕分两层：进程内只报一次 ERROR，并并入当日管理员汇总
    （`_collect_admin_mail`），同一场库故障不得按账号刷屏。

    JSON/环境变量账号模式（account_id=0）恒为 True：没有库内行可查，不存在"行过期"。
    查询本身即 `is_signable`（db 层已将同名函数导出为 `account_is_signable`）。

    **返回值三态**：`True` = 可签；`False` = 确实不可签（行已删/停用/审核态不符）；
    `VALIDITY_UNKNOWN`（`None`）= 查询异常、状态未知。调用方必须把后两者都按"不发起
    真实登录"处理（fail-closed），但**文案/分支要分开**：把库错误说成"账号已被删除或
    停用"会误导管理员对账号做补救。
    """
    global _validity_check_failure_reported
    account_id = getattr(account, "account_id", 0)
    if not account_id:
        return True
    try:
        return _facade().account_is_signable(account_id)
    except Exception as e:
        from yiban.core import masking
        reason = masking.sanitize_text(e)
        if not _validity_check_failure_reported:
            # 去重是**全局**的（进程内一次），不是逐账号：同一场库故障下逐账号重报会把
            # 几百个账号刷成几百条 ERROR 与邮件，掩盖真正的信号（与 notify_pool_down 同法）
            _validity_check_failure_reported = True
            logger.error(
                "[%s] 账号有效性复核失败（fail-closed 拒绝发起真实登录）: %s",
                _mask_phone(str(getattr(account, "phone", ""))), reason,
            )
            try:
                # 局部导入：notifier 经引擎入口反向依赖数据层，模块级互引会成环
                from yiban.adapters.notifier import report as alerts
                alerts._collect_admin_mail(
                    "账号有效性复核失败",
                    "账号状态查询失败，相关账号已拒绝发起登录"
                    f"（防已删除/停用账号继续被真实登录）：{reason}",
                    level=alerts.ALERT_LEVEL_CRITICAL,
                )
            except Exception as alert_err:
                logger.warning("账号有效性复核告警入账失败: %s",
                               masking.sanitize_text(alert_err))
        return VALIDITY_UNKNOWN


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
    """展示用打码（仅用于日志文案）：**转发** `yiban.core.masking.mask_phone`，不另立公式。

    本层曾有与 `masking.mask_phone` 平行的一份切片公式；两份必然分叉（改一边漏一边），
    故收敛为转发（2026-10-06 census P0-3 契约化）。
    """
    return _mask_phone(phone)


def _decrypt_row(row, env_file=None):
    """纯 CPU：把一行原始行转成账号 dict，并摘出需要明文自愈的字段。

    **不访问数据库、不加锁**。调用方负责在 `_conn_lock` **之外**调用它——逐行 AES-GCM
    解密若全程持锁，web 侧数十个调用点会把全站 DB 访问串行化——再把摘出的 pending
    交给 `_apply_plaintext_heal` 在锁内落库。

    `env_file`：密钥来源 .env 路径；None 时取连接层最近一次 `init_db` 记录的路径。
    只读装载路径不经 `init_db`，由调用方显式传入，免得密钥来源依赖 cwd。

    返回 `(account_dict, pending)`；pending 元素为
    `(字段名, 行 id, 明文原值, 手机号, 打码手机号)`。
    """
    a = dict(row)
    a["deleted"] = bool(a["deleted"])
    a["user_paused"] = bool(a.get("user_paused", 0))  # 用户自暂停签到（调度 v2）
    key_env_file = _connection._env_file if env_file is None else env_file
    pending = []
    # 本行只取一次钥（工单 dnox ④）：判定"有没有钥"与装载钥都在下面 `key is None` 的
    # 首枚密文字段处做一次，字段循环内不再逐字段重复。一行有 password / phone_code 两枚
    # 密文字段，逐字段判定会把 `has_key`（读盘）与 `load_key` 的调用翻倍。
    key = None
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
            if key is None:
                if not account_crypto.has_key(key_env_file):
                    raise RuntimeError(
                        "账号已加密但未配置 YIBAN_ACCOUNTS_KEY（请在 .env 配置或恢复密钥备份）"
                    )
                key = account_crypto.load_key(key_env_file)
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
    """写库前密文化：dict 密文对象 → JSON 串；其他非空值 → AES-GCM 加密（AAD=phone，
    新写 v2 带 kid）→ JSON 串。

    **空凭据显式语义**：空值（None/""/假值）按空串 "" 落库——空即空，既不产出
    "看似密文的空串"，也不触 load_key（空值写入不依赖密钥存在与否）。此前这一支
    完全静默，凭据真被清空时库内与"从来就空"不可分辨，现留 DEBUG 痕。
    非空但密钥非法则抛错（绝不静默降级明文）；无密钥时 load_key 自动生成并持久化
    （与 web 现状一致）。
    """
    if not value:
        logger.debug("凭据字段为空（type=%s），按空值语义落空串（不加密）",
                     type(value).__name__)
        return ""
    if isinstance(value, dict):
        return json.dumps(value)  # 已是密文对象
    key = account_crypto.load_key(_connection._env_file)
    return json.dumps(account_crypto.encrypt_password(str(value), key, phone))


# ---------------------------------------------------------------------------
# 读路径（快照 / 锁外解密）
# ---------------------------------------------------------------------------
#: 账号全序：`sort_order` 升序，`id` 破平局。
#: `sort_order` 无唯一约束（移动账号只交换两行，旧库可能有重值）；不加破平局键时，
#: "整表第 idx 行"在两次查询间没有定义。列表下标与详情下标共用这一条全序。
_ACCOUNTS_ORDER_BY = "sort_order, id"


def accounts_snapshot():
    """账号原始行快照（**不解密**）。持 `_conn_lock` 取到即释放。

    与 `decrypt_account_rows` 配对使用，让调用方能把 CPU 密集的解密放到锁外：
    调用方只需在自己那一层护住"取快照"这一步（web 层是 `_file_lock`）。

    顺序是 `sort_order, id`（见 `_ACCOUNTS_ORDER_BY`）：下标寻址（详情端点）与列表
    必须共享同一条全序，否则重值时同一 idx 可能指向不同账号。
    """
    db = _facade()
    with db._conn_lock:
        conn = db.get_conn()
        return [
            {**dict(r), "deleted": bool(r["deleted"])}
            for r in conn.execute(
                f"SELECT * FROM accounts ORDER BY {_ACCOUNTS_ORDER_BY}").fetchall()
        ]


def account_row_at(index):
    """按**整表绝对下标**取一行原始行（不解密）；越界或负下标返回 None。

    详情端点只读一行，不得为它解密整表（工单 dnox-B）。下标口径与
    `accounts_snapshot()` 的全序一致（`sort_order, id`）。

    持 `_conn_lock` 取到即释放；解密由 `read_account_at` 在锁外做。
    """
    if not isinstance(index, int) or index < 0:
        return None
    db = _facade()
    with db._conn_lock:
        row = db.get_conn().execute(
            f"SELECT * FROM accounts ORDER BY {_ACCOUNTS_ORDER_BY} LIMIT 1 OFFSET ?",
            (index,),
        ).fetchone()
    if row is None:
        return None
    return {**dict(row), "deleted": bool(row["deleted"])}


def read_account_at(fetch):
    """取单行快照 → 锁外解密；越界返回 None。

    `fetch` 是零参可调用对象，返回 `account_row_at()` 的结果。AAD 失配（并发改绑
    手机号）重取一次，与 `read_accounts` 同口径；重试后仍失败按真实损坏原样抛出。
    """
    for attempt in (0, 1):
        row = fetch()
        if row is None:
            return None
        try:
            return decrypt_account_rows([row])[0]
        except RuntimeError:
            if attempt:
                raise


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


def load_accounts_readonly(db_file, env_file=None):
    """只读装载账号：返回与 `load_accounts()` 同形的 dict 列表，但**零写操作**。

    与 `load_accounts()` 的差别正是它存在的理由：本函数用 `connection.open_readonly`
    独立连库，**不建库、不建表、不迁移、不切 WAL、不做明文自愈回写**——自称"只读"
    的维护路径（`config` / `sign --check-config`）不得因为"读账号"就把库造出来或改掉。
    库文件或 accounts 表不存在 → `[]`（调用方据此回落 JSON/环境变量来源，而不是报错）。

    解密沿用 `_decrypt_row`（同一份 AES-GCM 口径与手机号 AAD），`env_file` 显式传入
    密钥来源；明文驻留行只告警不回写（回写是写操作，只读路径不做）。
    """
    conn = _connection.open_readonly(db_file)
    if conn is None:
        return []
    try:
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "accounts" not in names:
            return []
        rows = [dict(r) for r in conn.execute(
            f"SELECT * FROM accounts ORDER BY {_ACCOUNTS_ORDER_BY}").fetchall()]
    finally:
        conn.close()
    accts = []
    for row in rows:
        account, pending = _decrypt_row(row, env_file=env_file)
        for field, _row_id, _plain, _phone, masked in pending:
            logger.warning("只读装载：账号 %s 的 %s 为明文存储（只读路径不回写）",
                           masked, field)
        accts.append(account)
    return accts


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
    却没有这条且欠账为 0"的静默丢失窗口（见 audit_chain.record_in_txn）。
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

    **软删即停用凭据缓存**（工单 ba-p02-04）：`session_cache` 存的是易班登录态
    （cookie + csrf 密文）。软删后到物理清除之间最长 7 天，凭据一直可从库文件/备份
    解密复用；`users.soft_delete_user_with_accounts` 早已"注销即刻停用"，账号级软删
    漏了同一半。清理与 UPDATE 同事务：审计写失败时整体回滚，不会留下"号没删、缓存
    已清"或"号删了、缓存还在"的半程状态。恢复（deleted=0）不重建缓存——重新登录即可。
    """
    db = _facade()
    conn = db.get_conn()
    with db._conn_lock, conn:
        row = conn.execute("SELECT phone FROM accounts WHERE id=?", (account_id,)).fetchone()
        conn.execute(
            "UPDATE accounts SET deleted=?, deleted_at=?, deleted_by=? WHERE id=?",
            (1 if deleted else 0, deleted_at, deleted_by if deleted else "", account_id),
        )
        if deleted and row is not None:
            db._clear_session_cache_by_phones(conn, [row["phone"]])
        if audit_spec:
            db.record_in_txn(conn, **audit_spec)


def purge_account(account_id, audit_spec=None):
    """物理删除单条账号，返回被删账号的手机号（无此账号时返回 None）。

    `audit_spec` 非 None 时，审计行与本次 DELETE **同事务**写入（口径同
    `delete_accounts_by_owner`）：物理删除不可逆，先提交删除再补审计会在进程被杀或
    审计写失败时留下"删了却无痕"。审计写失败时整个事务回滚，账号仍在。

    调用方拿返回的 phone 做文件面清理（日状态文件 / 熔断状态），文件不在 DB 事务内。
    """
    db = _facade()
    conn = db.get_conn()
    with db._conn_lock, conn:
        row = conn.execute("SELECT phone FROM accounts WHERE id=?", (account_id,)).fetchone()
        conn.execute("DELETE FROM accounts WHERE id=?", (account_id,))
        if row is not None:
            db._cascade_phone_owned(conn, [row["phone"]])  # 连带清理自选/会话/事件/校验任务
        if audit_spec:
            db.record_in_txn(conn, **audit_spec)
        phone = row["phone"] if row is not None else None
    if phone:
        db._purge_file_facts([phone])
    return phone


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
                "SELECT id, sort_order FROM accounts WHERE deleted=0 "
                "ORDER BY sort_order, id"
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
    一次请求清空该用户全部凭据属不可逆操作，同事务使"清了却无痕"不可能。留痕由本
    函数追加**实际删除行数**——名下 0 个账号时"清空成功"与"真删了 N 个"不得写成
    逐字相同的一条；操作发生了就如实留痕（含 0），与批量面"处理 0 个"同一族。
    """
    db = _facade()
    conn = db.get_conn()
    with db._conn_lock, conn:
        rows = conn.execute("SELECT phone FROM accounts WHERE owner=?", (owner,)).fetchall()
        cur = conn.execute("DELETE FROM accounts WHERE owner=?", (owner,))
        deleted = cur.rowcount or 0
        phones = [r["phone"] for r in rows]
        db._cascade_phone_owned(conn, phones)  # 自选/会话/事件/校验任务连带清理
        if audit_spec:
            spec = dict(audit_spec)
            spec["detail"] = f"{spec.get('detail', '')}；实际删除 {deleted} 个账号"
            db.record_in_txn(conn, **spec)
    db._purge_file_facts(phones)
    return deleted


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
                        # 缺省状态与 add_account 及建表 DDL 同一口径（pending）：
                        # 缺 status 的行曾按 active 落库，使"整体替换/导入"成为绕开
                        # 审核直进主链的第二入口（引擎只跳 pending/rejected，active
                        # 会被真实签到外呼）。带 status 的整表回环（导出→导入）不受
                        # 影响——显式状态原样保留。
                        a.get("status", "pending"),
                        a.get("reject_reason", ""),
                        1 if a.get("deleted") else 0,
                        a.get("deleted_at", ""),
                        1 if a.get("user_paused") else 0,
                    ),
                )
            except sqlite3.IntegrityError as e:
                conn.rollback()
                _convert_integrity_error(e)
    db._purge_file_facts(removed)
    return len(accounts)


def batch_account_ops(ops, audit_spec=None):
    """在一个事务内批量执行账号操作（Phase 1：整体成功或整体回滚）。

    ops 为 (op, params) 列表，op 支持：
      ("update_status", account_id, status, reject_reason)
      ("set_deleted", account_id, deleted, deleted_at)
      ("purge", account_id)

    `audit_spec` 非 None 时，批量审计行与全部操作**同事务**写入（R3-005）：
    批量彻底删除是不可逆操作，审计失败必须整体回滚。文件面的手机号清理在提交后。
    """
    db = _facade()
    conn = db.get_conn()
    purged_phones = []
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
                        # 软删同样即刻停用凭据缓存（工单 ba-p02-04 的同形状调用点：
                        # 管理员批量删除走的是本分支，不经过 set_account_deleted）。
                        row = conn.execute(
                            "SELECT phone FROM accounts WHERE id=?", (account_id,)
                        ).fetchone()
                        conn.execute(
                            "UPDATE accounts SET deleted=?, deleted_at=?, deleted_by=? WHERE id=?",
                            (1 if deleted else 0, deleted_at, "admin", account_id),
                        )
                        if deleted and row is not None:
                            db._clear_session_cache_by_phones(conn, [row["phone"]])
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
                        purged_phones.append(row["phone"])
                else:
                    raise ValueError(f"未知批量账号操作: {kind}")
            if audit_spec:
                db.record_in_txn(conn, **audit_spec)
            conn.commit()
        except Exception:
            with contextlib.suppress(Exception):
                conn.rollback()
            raise
    db._purge_file_facts(purged_phones)


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
