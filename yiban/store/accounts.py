# -*- coding: utf-8 -*-
"""数据层：accounts 表的运行期有效性判定与相关清理。

连接与进程内锁取自同包的 `yiban.store.db`（当前唯一的连接持有者）——延迟到函数内
导入，避免与它形成导入环；`yiban.store.db` 把公开名再导出，调用方无需改动。
"""
import logging

logger = logging.getLogger("yiban.store.accounts")

# 不参与签到的审核态（与 web 的 ACCOUNT_STATUS_PENDING/REJECTED 同值；此处是**库内
# 落库值**的唯一列举点，新增审核态时必须同步这里，否则新态的账号会被签到进程放行）。
ACCOUNT_AUDIT_INACTIVE = ("pending", "rejected")


def signs_in(row):
    """该账号行是否会发起易班签到请求（**容量/配额口径的唯一判据**）。

    审核态未通过（pending/rejected）的行永不签到——引擎加载（signin
    `_load_accounts_from_file`）与运行期复核（`is_signable`）用的是同一条条件。
    把它们计入容量会让"永不签到的存量"长期占满名额：新账号在提交时被
    「账号数量已达上限」误拒，而设置页/总览还会把它们显示成"正常"。

    行 dict 可能来自 `load_accounts_raw`（无 status 的旧档等于已通过审核）。

    user_paused（用户自暂停）**仍计入**：那是用户主动且一键可恢复的状态，
    账号仍在名册里；设置页三分类已把它单独列出。
    """
    return not row.get("deleted") and row.get("status") not in ACCOUNT_AUDIT_INACTIVE


def is_signable(account_id):
    """该账号当前是否仍可签到（行存在、未软删、审核态仍生效）。

    签到进程用**启动时的快照**跑完整轮（窗口最长 80 分钟），期间 web 端可能删除或
    停用账号。此前只在启动时筛一次，运行期被删的账号仍会被登录签到，并把加密的
    会话缓存写回一个已不存在的账号——`session_cache` 的清理全按"现存账号行的
    phone"驱动，那种行就成了**永久孤儿**。

    account_id 为 0/None（JSON / 环境变量账号模式：库内没有对应行）时返回 True——
    那些模式本来就不存在"库内账号行过期"的问题。
    """
    from yiban.store import db
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

    签到进程按启动快照跑完整轮，期间 web 端可能删除或停用账号。查询异常按
    "仍有效"处理——不因一次库抖动跳过全部账号；JSON/环境变量账号模式
    （account_id=0）恒为 True。
    """
    account_id = getattr(account, "account_id", 0)
    if not account_id:
        return True
    try:
        from yiban.store import db
        return db.account_is_signable(account_id)
    except Exception as e:
        from yiban import masking
        logger.debug(f"[{getattr(account, 'phone', '')}] 账号有效性复核失败（按有效处理）: "
                     f"{masking.sanitize_text(e)}")
        return True


def purge_orphan_session_cache(conn):
    """清除"账号行已不存在"的会话缓存（孤儿行；须在调用方事务内）。

    账号物理删除已由 `db._cascade_phone_owned` 连带清理 session_cache，但仍有一种
    窄竞态残留：登录完成时账号行刚被物理清除，写入发生在连带清理之后。会话缓存
    只在库内账号模式下写入，故"phone 不在 accounts 里"即为定义上的孤儿。
    返回删除行数。
    """
    cur = conn.execute(
        "DELETE FROM session_cache WHERE phone NOT IN (SELECT phone FROM accounts)"
    )
    return cur.rowcount
