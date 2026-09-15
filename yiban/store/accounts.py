# -*- coding: utf-8 -*-
"""数据层：accounts 表的运行期有效性判定与相关清理。

连接与进程内锁取自 `scripts/db.py`（当前唯一的连接持有者）——延迟到函数内
`import db`，避免与 db.py 形成导入环；db.py 把公开名再导出，调用方无需改动。
"""
import logging

logger = logging.getLogger("yiban.store.accounts")

# 不参与签到的审核态（与 web 的 ACCOUNT_STATUS_PENDING/REJECTED 同值；此处是**库内
# 落库值**的唯一列举点，新增审核态时必须同步这里，否则新态的账号会被签到进程放行）。
ACCOUNT_AUDIT_INACTIVE = ("pending", "rejected")


def is_signable(account_id):
    """该账号当前是否仍可签到（行存在、未软删、审核态仍生效）。

    签到进程用**启动时的快照**跑完整轮（窗口最长 80 分钟），期间 web 端可能删除或
    停用账号。此前只在启动时筛一次，运行期被删的账号仍会被登录签到，并把加密的
    会话缓存写回一个已不存在的账号——`session_cache` 的清理全按"现存账号行的
    phone"驱动，那种行就成了**永久孤儿**（docs/refactor/52 §2.3 DAT-1）。

    account_id 为 0/None（JSON / 环境变量账号模式：库内没有对应行）时返回 True——
    那些模式本来就不存在"库内账号行过期"的问题。
    """
    import db
    if not account_id:
        return True
    with db._conn_lock:
        row = db.get_conn().execute(
            "SELECT deleted, status FROM accounts WHERE id=?", (account_id,)
        ).fetchone()
    if row is None or row["deleted"]:
        return False
    return row["status"] not in ACCOUNT_AUDIT_INACTIVE


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
