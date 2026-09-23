# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""任务队列访问层：`sign_tasks` 的批量领取 / 批量收尾 / 重排 / 当日计数（v18）。

**功能**
- `claim_batch`：按虚分片批量领取到期任务——单条 `UPDATE ... RETURNING`，在
  SQLite 的写者串行语义下天然原子（等价于 PG 的 `SKIP LOCKED`，本仓无需跨机形态）；
  领取时自增 `epoch`（fencing token）并随行返回，收尾侧据此拒绝被接管者的迟到写；
- `settle_tasks`：一批完成的任务在单事务里收尾（owner + epoch 作用域），不逐账号 commit；
- `requeue_task`：失败重排——`priority` / `attempts` 递增、`state` 回 `pending`；
- `day_counts`：当日各 state 计数与派生口径（settled/open/total）——调用方据此判
  "当日是否了结"、给进度展示取数；
- `load_egress_state` / `save_egress_state`：出口令牌桶状态（`egress_state`，v18 建表）
  的读写薄封装，供 `yiban/engine/token_bucket.py` 落库与崩溃重启恢复。

**归属**
`sign_tasks` 由 `yiban/store/migrations.py` 的 v18 迁移建立；本模块是该表与
`egress_state` 在 store 层的**唯一访问点**——表结构、SQL 与降级口径都收在这里，
调用方不自己拼 SQL。v17 的 `sign_claims` 平移进本表后进入只读过渡期，其访问点仍是
`yiban/store/claims.py`。

**复用**
调用方按模块属性取（`from yiban.store import queue_store` 后 `queue_store.claim_batch(...)`），
这样打桩与"换成独立队列库文件"都不需要改调用点。

**通信**
数据源连接与进程内写锁暂取 `yiban.store.connection` 的单例 `get_conn()` / `_conn_lock`
（`_queue_conn()` 是唯一取点：将来本表迁到独立库文件、换独立连接时只改这一处）。
设计上的调用方是执行入口 `yiban/engine/round.py`（领取/收尾）与展示侧
`yiban/engine/state_io.py`、`web/services/executor_env.py`（经路由
`web/routes/accounts_api.py` 暴露）；这几处**今天读写的仍是 v17 的 `sign_claims`**
（经 `yiban/store/db.py` 的再导出调用 `claims.py`），本模块尚无调用点。
`egress_state` 的调用方是 `yiban/engine/token_bucket.py`（`EgressLimiter.persist` /
`restore_from_store`）。
"""
import datetime
import logging

from yiban import clock
from yiban import status as yiban_status

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

#: 了结态（当日不必再签）。`sign_claims` 时代"今日无任务/窗口外跳过"记在 `done` 上，
#: 故 `skipped` 与它同类——判"当日是否了结"时两者都算完。
#: 成员取自 `yiban.status.TASKS_SETTLED_STATES`（「了结」词义的唯一定义处）。
SETTLED_STATES = yiban_status.TASKS_SETTLED_STATES
#: 未了结态（当日仍可能被重排、被接手，或正被某个执行体持有）。
#: 成员取自 `yiban.status.TASKS_OPEN_STATES`（「未了结」词义的唯一定义处），顺序沿用本表
#: 自己的 `STATES`（成员无先后语义，但顺序稳定便于比对与调试）。
OPEN_STATES = tuple(s for s in STATES if s in yiban_status.TASKS_OPEN_STATES)


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
    """按分片（`vshard`）批量领取到期任务。返回
    `[{"phone","run_at","attempts","epoch"}, ...]`。

    单事务 `UPDATE ... WHERE (phone, day) IN (SELECT ...) RETURNING`：返回的行即被本
    执行体占住的行（`state='claimed'` + 写入 owner）。`SET owner` 是必需的——`owner`
    一列同时是"计划 owner"与"当前持有者"，收尾路径按 owner 做作用域校验，
    不写它则接管/窃取换不了手。

    `epoch` 是本次领取的 fencing token（**每次领取自增**，单调）：调用方收尾时必须把它
    原样传回 `settle_tasks` / `requeue_task`。没有它，被接管者迟到的写会覆盖接管者的结论。

    `vshards=()` 返回 `[]`（本轮不该领活，不算故障）；表未落地/库异常返回 `[]` **并
    告警**——与"表在、但无到期行"的空返回是两件事，调用方据此决定是否退回动态领取
    路径（本层不替调用方做降级决策）。
    """
    shards = tuple(vshards or ())
    if not shards:
        return []
    placeholders = ",".join("?" for _ in shards)
    sql = (
        "UPDATE sign_tasks SET state='claimed', owner=?, lease_until=?, "
        "epoch=epoch + 1 "
        "WHERE (phone, day) IN ("
        "SELECT phone, day FROM sign_tasks "
        f"WHERE day=? AND vshard IN ({placeholders}) "
        "AND state='pending' AND run_at<=? "
        "ORDER BY priority, run_at LIMIT ?"
        ") RETURNING phone, run_at, attempts, epoch"
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
    return [{"phone": r["phone"], "run_at": r["run_at"], "attempts": r["attempts"],
             "epoch": r["epoch"]} for r in rows]


def settle_tasks(owner, day, outcomes, state=STATE_DONE, epochs=None):
    """批量收尾：`outcomes=[(phone, result), ...]`，返回受影响行数。

    `WHERE day=? AND phone=? AND owner=?` 单事务收尾：被接管（owner 已是别人）的行
    写不进去，与 `claims.settle` 的 owner 作用域同一条纪律。`result` 调用方须先脱敏，
    本层只截断（转义与否是上层口径）。逐行参数走 executemany——result 是逐账号的，
    一次调用仍只有一次 commit（不逐账号 commit）。

    `epochs` 是 `{phone: 领取时拿到的 epoch}`：给了就逐行带上 `epoch=?`，owner 相同但
    token 落后的行同样写不进去（被接管者迟到的收尾会覆盖接管者的结论）。缺省 `None`
    表示不校验 token（迁移期调用方）。逐行的 token 用 `(? IS NULL OR epoch=?)` 表达
    而不是拼两种 SQL——executemany 要求整批共用一条语句。
    """
    items = list(outcomes or ())
    if not items:
        return 0
    tokens = epochs or {}
    sql = ("UPDATE sign_tasks SET state=?, result=?, lease_until='' "
           "WHERE day=? AND phone=? AND owner=? AND (? IS NULL OR epoch=?)")
    params = [(state, (result or "")[:RESULT_MAX], day, phone, owner,
               tokens.get(phone), tokens.get(phone)) for phone, result in items]
    try:
        conn, lock = _queue_conn()
        with lock:
            cur = conn.executemany(sql, params)
            conn.commit()
            return cur.rowcount
    except Exception as e:
        logger.warning("批量收尾签到任务失败（不影响签到结果）: %s", e)
        return 0


def requeue_task(phone, day, run_at, priority_delta=1, result="", epoch=None):
    """重试重排：返回受影响行数（0 = 该行不存在或被 token 拒）。

    `priority` 递增让重试任务排在新任务之后（活号优先）；`attempts` 落库后即**跨执行体
    共享**，接手者不再从 0 起算重试预算。任务回到 `pending` 意味着上一轮的 result 不再
    代表当前状态，故用传入值覆盖（缺省清空）。

    `epoch` 给了就带 `epoch=?`：只有当前持有者能把在飞任务重排回 `pending`，
    被接管者不得把接管者的任务重新投回池子（那会让同一账号被第三个执行体再领一次）。
    """
    sql = ("UPDATE sign_tasks SET state=?, run_at=?, priority=priority+?, "
           "attempts=attempts+1, lease_until='', result=? WHERE phone=? AND day=?")
    params = [STATE_PENDING, run_at, int(priority_delta),
              (result or "")[:RESULT_MAX], phone, day]
    if epoch is not None:
        sql += " AND epoch=?"
        params.append(epoch)
    try:
        conn, lock = _queue_conn()
        with lock:
            cur = conn.execute(sql, tuple(params))
            conn.commit()
            return cur.rowcount
    except Exception as e:
        logger.warning("重排签到任务失败: %s", e)
        return 0


def load_egress_state(egress):
    """读某出口的令牌桶状态：`{"rate","burst","tat"}`；无记录/库异常 → `None`。

    `rate` 单位是**账号尝试/s**（attempt/s，见 `yiban/engine/token_bucket.py` 模块头）。
    读失败**不抛**：调用方（执行体）按"没有记忆"回退出厂速率——重启后拿不到速率也不该
    卡住签到，出厂速率本就比自适应上限保守。
    """
    try:
        conn, lock = _queue_conn()
        with lock:
            row = conn.execute(
                "SELECT rate, burst, tat FROM egress_state WHERE egress=?", (egress,)
            ).fetchone()
    except Exception as e:
        logger.warning("读取出口令牌桶状态失败（按无记录处理）: %s", e)
        return None
    if row is None:
        return None
    return {"rate": row["rate"], "burst": row["burst"], "tat": row["tat"]}


def save_egress_state(egress, rate, burst, tat, now=None):
    """UPSERT 某出口的令牌桶状态，返回是否写入成功。

    `rate` 单位 = 账号尝试/s（attempt/s）；`burst` 是突发额度（尝试数）；`tat` 是 GCRA 的
    理论到达时刻（浮点秒，与调用方注入的时钟**同域**）。`now` 是写入时刻（墙钟字符串，
    缺省 `clock.ts()`）——它与 `tat` 不同域，只作"上次落库时刻"给运维看，**不可**拿来与
    `tat` 直接比较。写失败只告警不抛：桶状态是记忆不是业务事实，写不进去不该阻断签到。
    """
    sql = ("INSERT INTO egress_state (egress, rate, burst, tat, updated_at) "
           "VALUES (?,?,?,?,?) ON CONFLICT(egress) DO UPDATE SET "
           "rate=excluded.rate, burst=excluded.burst, tat=excluded.tat, "
           "updated_at=excluded.updated_at")
    params = (egress, float(rate), float(burst), float(tat), now or clock.ts())
    try:
        conn, lock = _queue_conn()
        with lock:
            conn.execute(sql, params)
            conn.commit()
        return True
    except Exception as e:
        logger.warning("写入出口令牌桶状态失败（不影响签到）: %s", e)
        return False


def day_counts(day):
    """当日各 state 计数与派生口径——供调用方判"当日是否了结"、给进度展示取数。

    与 `claims.stats` 同口径：`GROUP BY state` 计数、空 day 全 0、库不可用也不抛
    （记 warning 后按全 0 返回），并在状态计数之外派生三项——调用方**不需要自己求和**：

    | 派生键 | 定义 |
    |--------|------|
    | `settled` | `done` + `skipped`（当日不必再签；「了结」的词义见 `yiban.status.TASKS_SETTLED_STATES`） |
    | `open` | `pending` + `claimed` + `failed` + `stolen`（仍可能被重排/接手） |
    | `total` | 当日全部行数 = `settled` + `open` |

    `state` 词汇比 `sign_claims` 多两个（`skipped` 归入了结、`stolen` 归未了结），
    派生口径按上表定；未登记的 state 照实计进自己的键（不丢数）但不进派生三项
    ——与 `claims.stats` 对未知状态的处理同形。
    """
    out = dict.fromkeys(STATES, 0)
    try:
        conn, lock = _queue_conn()
        with lock:
            rows = conn.execute(
                "SELECT state, COUNT(*) AS n FROM sign_tasks WHERE day=? GROUP BY state",
                (day,),
            ).fetchall()
        for r in rows:
            out[r["state"]] = r["n"]
    except Exception as e:
        logger.warning("读取当日签到任务计数失败（按全 0 处理）: %s", e)
    out["settled"] = sum(out[s] for s in SETTLED_STATES)
    out["open"] = sum(out[s] for s in OPEN_STATES)
    out["total"] = out["settled"] + out["open"]
    return out
