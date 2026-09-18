# -*- coding: utf-8 -*-
"""数据层：在线校验任务表 verify_jobs（异步校验任务的持久化与状态机）。

任务态（排队/在跑/完成/被拒/已取消）与账号审核态（pending/active/rejected）是两件事，
因此独立成表，不污染 accounts.status 枚举。

**状态推进全部走 CAS**：任务状态由内存里的线程推进，进程随时可能消失，所有 UPDATE 都带
期望状态条件，迟到者写不进去也就覆盖不了别人的终态。

连接与进程内锁取自同包的 `yiban.store.db`（当前唯一的连接持有者）——延迟到函数内导入，
避免它在文件末尾导入本模块时形成导入环。`yiban.store.db` 把本模块的公开名全部再导出
（`db.create_verify_job` 等），故历史调用方无需改动。
"""
import datetime
import logging

from yiban import clock

logger = logging.getLogger("yiban.store.verify_jobs")

VERIFY_JOB_RETENTION_DAYS = 7  # 保留期（与账号软删同档）

VERIFY_JOB_PENDING = "pending"
VERIFY_JOB_RUNNING = "running"
VERIFY_JOB_DONE = "done"
VERIFY_JOB_REJECTED = "rejected"
# 取消是终态，但不在 pending|running|done|rejected 四态里——取消既不是"完成"也不是
# "校验未通过"，用独立值表达，避免把用户的主动撤销记成拒绝。
VERIFY_JOB_CANCELLED = "cancelled"

ACTIVE_STATUSES = (VERIFY_JOB_PENDING, VERIFY_JOB_RUNNING)
TERMINAL_STATUSES = (VERIFY_JOB_DONE, VERIFY_JOB_REJECTED, VERIFY_JOB_CANCELLED)

# 超龄收口阈值（秒）：超过它的 pending/running 任务判定为"执行体已死"，收口为 rejected。
# 合法任务的最长寿命是等席位上限 + 看门狗（900s 是其数倍），留足余量，避免把仍在排队
# 等席位的任务误收口。
VERIFY_JOB_STALE_SECONDS = 900

VERIFY_JOB_STALE_MSG = "校验任务超时未收口（进程重启或执行线程异常终止）"


def create(account_id, phone, owner_email, prev_status=VERIFY_JOB_PENDING):
    """创建一条任务，返回 (job_id, created_at)。

    prev_status 记下账号建库时的状态，作为校验结果能否写回账号的 CAS 依据
    （见 db.update_account_status_if）：管理员在任务执行期间审批后，迟到的校验结果
    不得覆盖人工决定。
    """
    from yiban.store import db
    conn = db.get_conn()
    ts = clock.ts()
    with db._conn_lock:
        cur = conn.execute(
            "INSERT INTO verify_jobs (account_id, phone, owner_email, status, prev_status, "
            "created_at) VALUES (?,?,?,?,?,?)",
            (account_id, phone, owner_email, VERIFY_JOB_PENDING,
             prev_status or VERIFY_JOB_PENDING, ts),
        )
        conn.commit()
        return cur.lastrowid, ts


def get(job_id):
    """单条任务（dict）或 None。"""
    from yiban.store import db
    with db._conn_lock:
        row = db.get_conn().execute(
            "SELECT * FROM verify_jobs WHERE id=?", (job_id,)
        ).fetchone()
    return dict(row) if row else None


def claim(job_id):
    """pending → running（CAS）。返回是否抢到——抢不到说明已被取消或已在跑。"""
    from yiban.store import db
    conn = db.get_conn()
    with db._conn_lock:
        cur = conn.execute(
            "UPDATE verify_jobs SET status=?, started_at=? WHERE id=? AND status=?",
            (VERIFY_JOB_RUNNING, clock.ts(), job_id, VERIFY_JOB_PENDING),
        )
        conn.commit()
        return cur.rowcount == 1


def finish(job_id, error=""):
    """把 running 任务落终态（error 非空 → rejected，否则 done）。

    CAS 在 running 上：任务若已被取消或被超龄收口，这里自然 0 行命中，不会把终态改写掉。
    返回是否写入。
    """
    from yiban.store import db
    conn = db.get_conn()
    status = VERIFY_JOB_REJECTED if error else VERIFY_JOB_DONE
    with db._conn_lock:
        cur = conn.execute(
            "UPDATE verify_jobs SET status=?, error=?, finished_at=? "
            "WHERE id=? AND status=?",
            (status, error or "", clock.ts(), job_id, VERIFY_JOB_RUNNING),
        )
        conn.commit()
        return cur.rowcount == 1


def cancel(job_id):
    """取消任务（仅 pending 可取消）。返回是否成功。"""
    from yiban.store import db
    conn = db.get_conn()
    with db._conn_lock:
        cur = conn.execute(
            "UPDATE verify_jobs SET status=?, finished_at=? WHERE id=? AND status=?",
            (VERIFY_JOB_CANCELLED, clock.ts(), job_id, VERIFY_JOB_PENDING),
        )
        conn.commit()
        return cur.rowcount == 1


def count_active():
    """未落终态的任务数（pending + running）。"""
    from yiban.store import db
    with db._conn_lock:
        row = db.get_conn().execute(
            "SELECT COUNT(*) AS n FROM verify_jobs WHERE status IN (?,?)",
            ACTIVE_STATUSES,
        ).fetchone()
    return row["n"] if row else 0


def reclaim_stale(stale_seconds=VERIFY_JOB_STALE_SECONDS, reject_status="",
                  reject_reason=""):
    """把超龄的 pending/running 任务收口为 rejected；返回被收口的任务列表。

    必要性：任务状态由**内存里的线程**推进，进程一旦在 claim 之后、看门狗触发之前消失
    （重启 / 重部署 / OOM），任务就永久停在 running——既不去终态、又因 cancel 只认
    pending 而无法取消。待办名额是有限的（pending + running），卡住的任务持续占用名额，
    累计到上限后所有新增账号的在线校验永久失败且**不会自愈**。故启动期与每次入队前都
    收口一次。

    判定用 COALESCE(started_at, created_at)：running 看开工时刻、pending 看建任务时刻。
    误收口的代价与既有看门狗同性质——任务落 rejected、账号被拒绝，用户可重新提交；时钟
    被大幅拨快时会批量误收口，方向是"少签一次"而非"用错凭据"，与时钟跳变守卫的取舍一致
    （守卫会冻结清理，这里不能冻结，否则功能不自愈）。

    reject_status 非空时，在**同一事务内**对相应账号做 CAS 拒绝（账号状态必须仍是建任务
    时那个状态）——分两步做会在中间被人工审批插入，正是要防的事。
    返回 [{"id","account_id","phone","prev_status"}]。
    """
    from yiban.store import db
    try:
        conn = db.get_conn()
        cutoff = (clock.now()
                  - datetime.timedelta(seconds=stale_seconds)).strftime("%Y-%m-%d %H:%M:%S")
        with db._conn_lock:
            probe = conn.execute(
                "SELECT 1 FROM verify_jobs WHERE status IN (?,?) "
                "AND COALESCE(started_at, created_at) < ? LIMIT 1",
                (*ACTIVE_STATUSES, cutoff),
            ).fetchone()
            if not probe:
                return []
            rows = conn.execute(
                "SELECT id, account_id, phone, "
                "COALESCE(NULLIF(prev_status, ''), ?) AS prev_status "
                "FROM verify_jobs WHERE status IN (?,?) "
                "AND COALESCE(started_at, created_at) < ?",
                (VERIFY_JOB_PENDING, *ACTIVE_STATUSES, cutoff),
            ).fetchall()
            conn.execute(
                "UPDATE verify_jobs SET status=?, error=?, finished_at=? "
                "WHERE status IN (?,?) AND COALESCE(started_at, created_at) < ?",
                (VERIFY_JOB_REJECTED, VERIFY_JOB_STALE_MSG, clock.ts(),
                 *ACTIVE_STATUSES, cutoff),
            )
            if reject_status:
                for row in rows:
                    if row["account_id"] is None or not row["prev_status"]:
                        continue
                    conn.execute(
                        "UPDATE accounts SET status=?, reject_reason=? "
                        "WHERE id=? AND status=?",
                        (reject_status, reject_reason or VERIFY_JOB_STALE_MSG,
                         row["account_id"], row["prev_status"]),
                    )
            conn.commit()
            return [dict(r) for r in rows]
    except Exception as e:
        # 表可能尚未落地（可选迁移被延后）——此处失败不得影响入队主流程
        logger.warning("超龄校验任务收口失败: %s", e)
        return []


def purge(days=VERIFY_JOB_RETENTION_DAYS):
    """清理保留期外的任务；失败仅告警，返回删除行数。"""
    from yiban.store import db
    try:
        conn = db.get_conn()
        cutoff = (clock.now() - datetime.timedelta(days=days)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        with db._conn_lock:
            cur = conn.execute("DELETE FROM verify_jobs WHERE created_at < ?", (cutoff,))
            conn.commit()
            return cur.rowcount
    except Exception as e:
        logger.warning("清理 verify_jobs 失败: %s", e)
        return 0
