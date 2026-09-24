# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""任务队列访问层：`sign_tasks` 的批量领取 / 批量收尾 / 重排 / 当日计数（v18）。

**功能**
- `claim_batch`：按虚分片批量领取到期任务——单条 `UPDATE ... RETURNING`，在
  SQLite 的写者串行语义下天然原子（等价于 PG 的 `SKIP LOCKED`，本仓无需跨机形态）；
  领取时自增 `epoch`（fencing token）并随行返回，收尾侧据此拒绝被接管者的迟到写；
- `settle_tasks`：一批完成的任务在单事务里收尾（owner + epoch 作用域），不逐账号 commit；
- `requeue_task`：失败重排——`priority` / `attempts` 递增、`state` 回 `pending`；
- `reap_expired`：租约过期**且超出宽限期**的 `claimed` 行回退 `pending`（不做就是"崩溃即卡死"，
  宽限期的取值理由见 `REAP_GRACE_SEC` 与该函数说明）；
- `steal_shards`：死主分片接管——把心跳过期执行体分片集内 `owner` 为**该死主**的
  `pending` 行改归本执行体（只动 `pending`，CAS 精确到死主 + `epoch+1`）；
- `pending_count`：当日「我的分片集」内的待办计数（带 `vshard` 过滤的"当日是否了结"
  闸门，见该函数说明）；
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
设计上的调用方是执行入口 `yiban/engine/round.py`（领取/收尾）与调度 v3 的执行体
`yiban/engine/executor_v3.py`（批量领取/收尾/重排/待办计数/桶状态落库），展示侧是
`yiban/engine/state_io.py`、`web/services/executor_env.py`（经路由
`web/routes/accounts_api.py` 暴露）。**注意这两条路径读写的不是同一张表**：`round.py`
与展示侧读写的仍是 v17 的 `sign_claims`（经 `yiban/store/db.py` 的再导出调用
`claims.py`），只有 `executor_v3.py` 消费本模块的 `sign_tasks`——过渡期两个写者按
"当日单一写者"的运维规则互斥。
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

#: 回收宽限期（秒）：租约到期只是"持有者**可能**已死"，不等于真死。单次尝试可能比租约
#: 还慢（项目既有"慢签到"告警阈值 30s，超 60s 的尝试并非不可能），此时回收并重领会
#: 让同一账号被两条通道并发登录——`epoch+1` 只挡迟到的结论写回，挡不住这次重复真实
#: 登录。宽限期把"在飞被回收"的窗口压到可忽略；要收紧响应速度，真正的旋钮是租约时长
#: 而不是这里。取 ≥ 2× 租约留出余量。
REAP_GRACE_SEC = 120

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

#: 了结态（当日不必再签）。「了结」的词义只在 `yiban.status` 定义一处，本表不自写判据。
SETTLED_STATES = yiban_status.TASKS_SETTLED_STATES  # = {done, skipped}：`skipped` 承接暂停/取消类结论（paused / user_cancelled / global_paused，v18 平移映射见 `yiban.store.migrations._JSON_TERMINAL_TO_TASK_STATE`）；旧表 `sign_claims` 没有 `skipped` 这一档、同批结论当时落 `failed`（未了结、可再领），跨表比对"当日是否了结"不得直接对齐
#: 未了结态（当日仍可能被重排、被接手，或正被某个执行体持有）。
OPEN_STATES = tuple(s for s in STATES if s in yiban_status.TASKS_OPEN_STATES)  # 成员取自 `yiban.status.TASKS_OPEN_STATES`；顺序沿用本表 `STATES`——成员无先后语义，但顺序稳定便于比对与调试


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


def _shift_stamp(stamp, sec):
    """时间串平移 `sec` 秒，返回毫秒精度、与 `run_at` 同格式的可比字符串。

    本库的时间串一律北京时间（`yiban.clock`），**不能用 SQL 的 `datetime(..., '-N seconds')`
    代替**：它的输出**没有毫秒**（`datetime('2026-09-22 06:40:00','-120 seconds')` 得
    `'2026-09-22 06:38:00'`），而 `lease_until` 是毫秒格式；`'…06:38:00' < '…06:38:00.000'`
    为真，拿它跟毫秒串做字符串比较会在同一秒内错位。**不是时区问题**：纯秒平移下把北京时间
    当 UTC 解释只差一个常量偏移，平移量不变（`_lease_until` 不能用 SQL `'now'` 才是时区坑）。
    """
    text = str(stamp)
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            t = datetime.datetime.strptime(text, fmt)
            break
        except ValueError:
            continue
    else:
        raise ValueError(f"不可解析的时间串: {text!r}")
    return (t + datetime.timedelta(seconds=sec)).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


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


def reap_expired(now=None, day=None, grace_sec=REAP_GRACE_SEC):
    """回收租约**过期且超出宽限期**的在飞任务：`state='claimed'` 且
    `lease_until < now - grace_sec` 的行回退为 `pending`（清 `owner`/`lease_until`、
    `epoch = epoch + 1`）。返回受影响行数。

    **为什么必须做**：`claim_batch` 只取 `state='pending'` 的行，崩溃/被杀的通道留下的
    `claimed` 行**永远不会被重新领取**——不做回收就是"崩溃即卡死"，该账号当天不再有人签。

    **为什么要宽限期（`REAP_GRACE_SEC`）**：租约到期只说明持有者"可能已死"，不等于真死。
    单次尝试可能比租约还慢（慢签到告警阈值 30s 而租约 60s），此时若按"过期即回收"，该行
    会回退 `pending` 并**可能被同一执行体重新领到** ⇒ 同一账号两条通道并发登录。`epoch+1`
    只保证旧结论写不回，挡不住这次重复真实登录，故必须靠宽限期把"在飞被回收"的窗口压到
    可忽略；真正的旋钮是 `LEASE_SECONDS`（宽限期取 ≥ 2× 租约留余量）。

    **`epoch + 1` 是 fencing 红线**：回退后原持有者可能迟到提交 `settle_tasks`，自增
    token 让它的旧 epoch 写被拒（`settle_tasks(..., epochs=...)` 已支持），否则迟到的旧
    结论会覆盖接手者的结论。

    **必须排除 `vshard = -1` 的历史行**（v18 平移 / v20 补账写入）：它们不属于任何分片集，
    回收成 `pending` 只会变成永不被领取的行（`claim_batch` 的 `vshard IN (...)` 挡着），
    白白制造"看着有活、其实无人领"的假象。`day` 给了就只回收该业务日。

    幂等：回收后的行不再是 `claimed`，重跑 0 行。库异常 → 0 + warning（回收是补偿动作，
    失败不该打断签到；下一轮会再试）；`now` 不可解析是**调用方入参问题**，单独一条
    warning（不与库异常共用文案，免得把排查方向带到存储层）。
    """
    sql = ("UPDATE sign_tasks SET state=?, owner='', lease_until='', epoch=epoch + 1 "
           "WHERE state=? AND lease_until != '' AND lease_until < ? AND vshard >= 0")
    try:
        cutoff = _shift_stamp(now or _lease_until(0), -grace_sec)
    except ValueError as e:
        logger.warning("回收过期签到任务的时刻参数不可解析（按无可回收处理）: %s", e)
        return 0
    try:
        params = [STATE_PENDING, STATE_CLAIMED, cutoff]
        if day is not None:
            sql += " AND day=?"
            params.append(day)
        conn, lock = _queue_conn()
        with lock:
            cur = conn.execute(sql, tuple(params))
            conn.commit()
            return cur.rowcount
    except Exception as e:
        logger.warning("回收过期签到任务失败（按无可回收处理）: %s", e)
        return 0


def steal_shards(me, dead_owner, shards, day, now=None):
    """接管死主分片集内的待办：把「分片集内 + `state='pending'` + `owner` 恰为死主」的行
    改为 `owner=<me>`、`epoch = epoch + 1`。返回受影响行数。

    **两个身份分开传**：`me` 是接管者（本执行体），`dead_owner` 是心跳已被判过期的那具
    死主。单参数表达不了"从谁手里接管"，CAS 只能退化成 `owner != me`——那会连**活着的**
    第三个执行体先接管的行一起改写，把别人的在飞任务抢过来。故 CAS 精确写成
    `owner = <dead_owner>`：只动死主的行。

    `shards` 由调用方保证**只含死主的分片集**（判据是文件心跳四态，不落库）；本层不校验
    归属，只按"这些分片里还是 `pending` 且 owner 是死主"来写。

    **只动 `pending`**：`claimed` 是他人仍在飞的行（租约未到不该抢，租约到了由
    `reap_expired` 回收），终态行更不该动。`vshard = -1` 的历史行不属于任何分片集，
    调用方给出的分片集里天然不含它——`shards` 若被误传含 `-1`，这里再显式挡一道。

    `now` 只为与 `reap_expired` 同签名（本函数不按时间过滤，取哪些分片由调用方决定）。
    `shards=()` → 0（本轮不接管，不算故障）；库异常 → 0 + warning。
    """
    shard_set = tuple(shards or ())
    if not shard_set:
        return 0
    placeholders = ",".join("?" for _ in shard_set)
    sql = ("UPDATE sign_tasks SET owner=?, epoch=epoch + 1 "
           f"WHERE day=? AND vshard >= 0 AND vshard IN ({placeholders}) "
           "AND state=? AND owner=?")
    params = (me, day, *shard_set, STATE_PENDING, dead_owner)
    try:
        conn, lock = _queue_conn()
        with lock:
            cur = conn.execute(sql, params)
            conn.commit()
            return cur.rowcount
    except Exception as e:
        logger.warning("接管死主分片失败（按未接管处理）: %s", e)
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


def pending_count(day, vshards):
    """当日「我的分片集」内仍待办（`state='pending'`）的行数——**当日是否了结的闸门**。

    为什么必须带 `vshard` 过滤，而不能用 `day_counts(day)["open"]`：v18 的 `sign_claims`
    平移行与 v20 的补账行都写 `vshard=-1`，其中 `state='failed'` 属 `OPEN_STATES`，可它们
    永不被 `claim_batch` 领取、也没有 owner/epoch 可供 `requeue`。把它们算作"未了结"，
    该日就**永远不了结**（补签轮反复空跑）。分片集恒是 `0..V-1` 的子集，故历史行天然
    不在其中；SQL 里再显式写一遍 `vshard >= 0` 是双保险（防调用方传入非法分片集）。

    `vshards=()` → 0（本轮不该领活，不算故障，与 `claim_batch` 同口径）；库异常 → 0 +
    warning（调用方据此走"没有待办"的收干分支，而不是抛出去打断签到）。
    """
    shards = tuple(vshards or ())
    if not shards:
        return 0
    placeholders = ",".join("?" for _ in shards)
    sql = ("SELECT COUNT(*) FROM sign_tasks WHERE day=? AND state=? "
           f"AND vshard >= 0 AND vshard IN ({placeholders})")
    try:
        conn, lock = _queue_conn()
        with lock:
            row = conn.execute(sql, (day, STATE_PENDING, *shards)).fetchone()
    except Exception as e:
        logger.warning("读取当日待办任务计数失败（按无待办处理）: %s", e)
        return 0
    return int(row[0]) if row else 0


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
