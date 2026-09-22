# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""任务队列访问层：`sign_tasks` 的批量领取 / 批量收尾 / 重排 / 当日计数（v18）。

**功能**
- `claim_batch`：按虚分片批量领取到期任务——单条 `UPDATE ... RETURNING`，在
  SQLite 的写者串行语义下天然原子（等价于 PG 的 `SKIP LOCKED`，本仓无需跨机形态）；
- `settle_tasks`：一批完成的任务在单事务里收尾（owner 作用域），不逐账号 commit；
- `requeue_task`：失败重排——`priority` / `attempts` 递增、`state` 回 `pending`；
- `day_counts`：当日各 state 计数，供降级链与进度展示判"当日是否了结"。

**归属**
`sign_tasks` 由 `yiban/store/migrations.py` 的 v18 迁移建立；本模块是该表在 store 层的
**唯一访问点**——表结构、SQL 与降级口径都收在这里，调用方不自己拼 SQL。v17 的
`sign_claims` 平移进本表后进入只读过渡期，其访问点仍是 `yiban/store/claims.py`。

**复用**
调用方按模块属性取（`from yiban.store import queue_store` 后 `queue_store.claim_batch(...)`），
这样打桩与"换成独立队列库文件"都不需要改调用点。

**通信**
数据源连接与进程内写锁暂取 `yiban.store.connection` 的单例 `get_conn()` / `_conn_lock`
（`_queue_conn()` 是唯一取点：三库分离落地时只改这一处）。设计上的调用方是执行入口
`yiban/engine/round.py`（领取/收尾）与展示侧 `yiban/engine/state_io.py`、
`web/services/executor_env.py`（经路由 `web/routes/accounts_api.py` 暴露）；这几处
**今天读写的仍是 v17 的 `sign_claims`**（经 `yiban/store/db.py` 的再导出调用
`claims.py`），本模块尚无调用点——本任务只落访问层与迁移。
"""
import datetime
import logging

from yiban import clock

logger = logging.getLogger("yiban.store.queue_store")

#: 任务级短租约（秒）：通道崩溃后到期即被回收重排——比账号级 900s 租约快一个量级，
#: 崩溃的账号不必等到窗口结束才有人接手。
LEASE_SECONDS = 60

#: 批领缺省行数 = 通道数 × 预取系数。
CLAIM_BATCH_LIMIT = 32

#: 一个 result 字段最多保留的字符数（与 `claims.settle` 同口径）。
RESULT_MAX = 200

#: `state` 取值全集（与 v18 DDL 的列注释同一套口径）。
STATE_PENDING = "pending"
STATE_CLAIMED = "claimed"
STATE_DONE = "done"
STATE_FAILED = "failed"
STATE_SKIPPED = "skipped"
STATE_STOLEN = "stolen"
STATES = (STATE_PENDING, STATE_CLAIMED, STATE_DONE, STATE_FAILED, STATE_SKIPPED,
          STATE_STOLEN)


def _queue_conn():
    """队列库连接与写锁的**唯一取点**。

    当前与业务表同库同连接（`sign_tasks` 落在 yiban.db）；按属性取而不是模块级
    from-import，独立队列库或打桩才切得动。
    """
    from yiban.store import connection
    return connection.get_conn(), connection._conn_lock


def _lease_until(lease_sec):
    """租约到期时刻（毫秒精度、与 `run_at` 同格式的可比字符串）。

    **不用 SQL 的 `strftime(..., 'now')`**：SQLite 的 `'now'` 是 UTC，而全库时间串是
    北京时间（`yiban.clock`），直接用会写出早 8 小时的租约 ⇒ 在飞任务被误判为过期。
    """
    t = clock.now() + datetime.timedelta(seconds=lease_sec)
    return t.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def claim_batch(owner, day, vshards, now=None, limit=CLAIM_BATCH_LIMIT,
                lease_sec=LEASE_SECONDS):
    """按分片批量领取到期任务（A 案 §5.2）。返回 `[{"phone","run_at","attempts"}, ...]`。

    单事务 `UPDATE ... WHERE (phone, day) IN (SELECT ...) RETURNING`：返回的行即被本
    执行体占住的行（`state='claimed'` + 写入 owner）。`SET owner` 是必需的——`owner`
    一列同时是"计划 owner（HRW）"与"当前持有者"，收尾路径按 owner 做作用域校验，
    不写它则接管/窃取换不了手。

    `vshards=()` 返回 `[]`（本轮不该领活，不算故障）；表未落地/库异常返回 `[]` **并
    告警**——与"表在、但无到期行"的空返回是两件事，调用方据此决定是否退回动态领取
    路径（本层不替调用方做降级决策）。
    """
    shards = tuple(vshards or ())
    if not shards:
        return []
    placeholders = ",".join("?" for _ in shards)
    sql = (
        "UPDATE sign_tasks SET state='claimed', owner=?, lease_until=? "
        "WHERE (phone, day) IN ("
        "SELECT phone, day FROM sign_tasks "
        f"WHERE day=? AND vshard IN ({placeholders}) "
        "AND state='pending' AND run_at<=? "
        "ORDER BY priority, run_at LIMIT ?"
        ") RETURNING phone, run_at, attempts"
    )
    params = (owner, _lease_until(lease_sec), day, *shards, now or clock.ts(), int(limit))
    try:
        conn, lock = _queue_conn()
        with lock:
            rows = conn.execute(sql, params).fetchall()
            conn.commit()
    except Exception as e:
        logger.warning("批量领取签到任务失败（按无可领处理）: %s", e)
        return []
    return [{"phone": r["phone"], "run_at": r["run_at"], "attempts": r["attempts"]}
            for r in rows]


def settle_tasks(owner, day, outcomes, state=STATE_DONE):
    """批量收尾（A 案 §5.4）：`outcomes=[(phone, result), ...]`，返回受影响行数。

    `WHERE day=? AND phone=? AND owner=?` 单事务收尾：被接管（owner 已是别人）的行
    写不进去，与 `claims.settle` 的 owner 作用域同一条纪律。`result` 调用方须先脱敏，
    本层只截断（转义与否是上层口径）。逐行参数走 executemany——result 是逐账号的，
    一次调用仍只有一次 commit（不逐账号 commit）。
    """
    items = list(outcomes or ())
    if not items:
        return 0
    sql = ("UPDATE sign_tasks SET state=?, result=?, lease_until='' "
           "WHERE day=? AND phone=? AND owner=?")
    params = [(state, (result or "")[:RESULT_MAX], day, phone, owner)
              for phone, result in items]
    try:
        conn, lock = _queue_conn()
        with lock:
            cur = conn.executemany(sql, params)
            conn.commit()
            return cur.rowcount
    except Exception as e:
        logger.warning("批量收尾签到任务失败（不影响签到结果）: %s", e)
        return 0


def requeue_task(phone, day, run_at, priority_delta=1, result=""):
    """重试重排（A 案 §5.3）：返回受影响行数（0 = 该行不存在）。

    `priority` 递增让重试任务排在新任务之后（活号优先）；`attempts` 落库后即**跨执行体
    共享**，接手者不再从 0 起算重试预算。任务回到 `pending` 意味着上一轮的 result 不再
    代表当前状态，故用传入值覆盖（缺省清空）。
    """
    sql = ("UPDATE sign_tasks SET state=?, run_at=?, priority=priority+?, "
           "attempts=attempts+1, lease_until='', result=? WHERE phone=? AND day=?")
    params = (STATE_PENDING, run_at, int(priority_delta),
              (result or "")[:RESULT_MAX], phone, day)
    try:
        conn, lock = _queue_conn()
        with lock:
            cur = conn.execute(sql, params)
            conn.commit()
            return cur.rowcount
    except Exception as e:
        logger.warning("重排签到任务失败: %s", e)
        return 0


def day_counts(day):
    """当日各 state 计数——供降级链 / 进度展示判"当日是否了结"。

    与 `claims.stats` 同口径：`GROUP BY state` 计数、空 day 全 0、库不可用也不抛
    （失败记 warning 并按全 0 返回）。返回全集是 v18 DDL 的 `state` 取值，
    **不派生 settled/open**：`skipped` / `stolen` 在 `sign_claims` 时代没有对应状态，
    "哪些状态算了结"由调用方按自己的口径判。
    """
    out = dict.fromkeys(STATES, 0)
    try:
        conn, lock = _queue_conn()
        with lock:
            rows = conn.execute(
                "SELECT state, COUNT(*) AS n FROM sign_tasks WHERE day=? GROUP BY state",
                (day,),
            ).fetchall()
    except Exception as e:
        logger.warning("读取当日签到任务计数失败（按全 0 处理）: %s", e)
        return out
    for r in rows:
        out[r["state"]] = r["n"]   # 未知状态照实计数，不丢数
    return out
