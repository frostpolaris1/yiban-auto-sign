# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""任务队列访问层：`sign_tasks` 的批量领取 / 批量收尾 / 重排 / 当日计数（v18）。

**功能**
- `claim_batch`：按虚分片批量领取到期任务——单条 `UPDATE ... RETURNING`，在
  SQLite 的写者串行语义下天然原子（等价于 PG 的 `SKIP LOCKED`，本仓无需跨机形态）；
  领取时自增 `epoch`（fencing token）并随行返回，收尾侧据此拒绝被接管者的迟到写；
- `settle_tasks`：一批完成的任务在单事务里收尾（owner + epoch 作用域），不逐账号 commit；
- `requeue_task`：失败重排——`priority` / `attempts` 递增、`state` 回 `pending`；只对
  未了结行生效（终态行不得被重排复活，否则会被重新领取＝当日再登录一次）；
- `requeue_failed`：当日回炉——把 `failed` 行按 `retry:`/`final:` 档位逐行走
  `requeue_task`（state+epoch 门沿用，不另造协议），`claim_batch` 只取 `pending`，
  没有这条路 v3 的当日失败就无人接手；
- `reclaim_tasks`：显式重签——把指定账号当日的**终态**行（`done`/`skipped`）翻回
  `pending`，只服务手动 `--only`（终态复活默认是红线，故无缺省调用者）；
- `reap_expired`：租约过期**且超出宽限期**的 `claimed` 行回退 `pending`（不做就是"崩溃即卡死"，
  宽限期的取值理由见 `REAP_GRACE_SEC` 与该函数说明）；**判活按持有者身份**——调用方本进程
  通道队列里在途的行（`held`）与心跳仍存活的持有者（`live_owners`）一律豁免，否则同一账号
  会被原地重领、当天两次真实登录；
- `claimed_owners`：当日**在途 `claimed` 行**的持有者全集——豁免名册的**唯一来源**。
  调用方据此逐持有者判活，再把存活者的稳定槽位名交给 `reap_expired(live_owners=...)`；
  兜底常驻身份从不在执行体清单里，只有这条来源才包得住它（ba-p03-01）。读失败回 `None`
  （哨兵，与"确实无在途持有者"的空集区分）⇒ 调用方跳过本轮回收（fail-closed）；
- `reap_abandoned`：监督进程对**已确认死亡**（异常退出）的执行体名下 `claimed` 行立即回退
  `pending`——证据强于"租约过期"，故不等宽限期；
- `pending_count`：当日「我的分片集」内的待办计数（带 `vshard` 过滤的"当日是否了结"
  闸门，见该函数说明）；
- `fallback_event`：兜底常驻的"失败即入队"读取端——默认可接手（`retry:` 档）未了结行的
  事件签名 `(条数, 最新迁移标记)`，短轮询变化即接手；
- `purge`：按保留期清理本表存量（带时钟跳变守卫，见 `yiban/store/cleanup.py` 编排）；
- `day_counts`：当日各 state 计数与派生口径（settled/open/total）——调用方据此给
  进度展示取数；**读不通回 `None` 哨兵**（降级口径见下）；`open` **不过滤 `vshard`，
  不得当"当日是否了结"的闸门**（那会把永不被领取的 `vshard=-1` 惰性行算进去），
  闸门用 `pending_count`（有分片上下文）或 `open_count`（无分片上下文）；
- `load_egress_state` / `save_egress_state`：出口令牌桶状态（`egress_state`，v18 建表）
  的读写薄封装，供 `yiban/engine/token_bucket.py` 落库与崩溃重启恢复。

**归属**
`sign_tasks` 由 `yiban/store/migrations.py` 的 v18 迁移建立；本模块是该表与
`egress_state` 在 store 层的**唯一访问点**——表结构、SQL 与降级口径都收在这里，
调用方不自己拼 SQL。台账单池化后本表是**唯一生产台账**（`sign_claims` 已冻结、零写入，
其旧访问层 `yiban/store/claims.py` 保留但无生产调用点）。

**复用**
调用方按模块属性取（`from yiban.store import queue_store` 后 `queue_store.claim_batch(...)`），
这样打桩与"换成独立队列库文件"都不需要改调用点。

**通信**
数据源连接与进程内写锁暂取 `yiban.store.connection` 的单例 `get_conn()` / `_conn_lock`
（`_queue_conn()` 是唯一取点：将来本表迁到独立库文件、换独立连接时只改这一处）。
设计上的调用方是执行入口 `yiban/engine/executor_v3.py`（批量领取/收尾/重排/待办计数/
重签/桶状态落库）与展示侧 `yiban/engine/state_io.py`、`web/services/executor_env.py`
（经路由 `web/routes/accounts_api.py` 暴露）——单池后两条路径读写的是**同一张表**
`sign_tasks`。
`egress_state` 的调用方是 `yiban/engine/token_bucket.py`（`EgressLimiter.persist` /
`restore_from_store`）。

**降级口径（全模块唯一一条，ba-p01-01）**
读不通（库异常/表未落地/锁等待超时）**一律回 `None` 哨兵**，绝不回"空"值：`[]`、`0`、
全 0 字典、`0` 翻回数都只用来回答"真的没有这一类行"。沿用同文件既有惯例
（`claimed_owners` 回 `None`、`reap_expired` 见 `None` 即跳过本轮），不另造第二种形状。
理由：**一次读库失败，永远不得被任何读者解释成"今天没有待办了 / 活已了结"**——把"坏"
折叠成"空"就是全天零签到而现场只有两条 WARNING。执行体侧的有界退避与响亮告警见
`yiban/engine/executor_v3.QUEUE_UNREADABLE_MAX_ROUNDS`。
`vshards=()` / 空允许集**不是**故障（本轮不该领活，与库无关），照旧回空值且不取连接。
补偿动作与纯展示读面（`settle_tasks`、`requeue_task`、`reclaim_tasks`、`reap_expired`、
`reap_abandoned`、`load_egress_state`、`save_egress_state`、`purge`、`fallback_event`、
`latest_day`、`owners_for_day`、`owners_since`、`activity`）不在这条口径内：它们的失败
方向是"这一轮少做一次"或"页面少显示一块"，不参与任何"退出/了结"判定，改动只会扩大面。

"""
import datetime
import logging

from yiban import clock
from yiban import status as yiban_status
from yiban.store import claims as claims_mod

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

#: 单条 SQL 的绑定变量分块大小。SQLite 的 `SQLITE_MAX_VARIABLE_NUMBER` 老版本是 999
#: （新版本 32766），而 `reap_expired` 的在途豁免要把整个通道队列的手机号逐个展开成
#: `NOT IN` 占位符——大站一个补货循环就能攒出上千条。整段拼一条会在变量数超限时直接
#: 抛 `sqlite3.ProgrammingError`，而回收失败只告警 ⇒ 回收静默退化成"什么都不回收"，
#: 崩溃即卡死。取 900：给 day/允许集等其余占位留出余量。**不承诺**永远稳在变量上限内：
#: `phones`（手动 `--only` 的允许集）与 `live_owners` 不分块，条目极多时仍可能超限——
#: 届时 `reap_expired` 捕获异常 → 告警 → 本轮不回收，失败模式有界（现代 SQLite 上限
#: 32766，实际难以触到；真触到说明队列已异常，该看的是队列而不是这个常数）。
SQL_VAR_CHUNK = 900

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
#: 可被 `requeue_task` 重排回 `pending` 的状态：所有**未了结**态（`OPEN_STATES`）。
#: 终态（`done` / `skipped`）绝不许复活——复活会被 `claim_batch` 重新领取，等于同一
#: 账号当日再登录一次，"当日是否了结"的闸门也会凭空又出现待办。`pending` 行重排只是
#: 刷新落点/优先级（幂等），保留它以免调用方按"先看再排"写出跨语句窗口。
REQUEUEABLE_STATES = OPEN_STATES


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


def _chunks(items, size=SQL_VAR_CHUNK):
    """把可迭代对象切成不超过 `size` 的定长块（生成器，空输入不产出）。"""
    chunk = []
    for item in items:
        chunk.append(item)
        if len(chunk) >= size:
            yield chunk
            chunk = []
    if chunk:
        yield chunk


def claim_batch(owner, day, vshards, now=None, limit=CLAIM_BATCH_LIMIT,
                lease_sec=LEASE_SECONDS, phones=None):
    """按分片（`vshard`）批量领取到期任务。返回
    `[{"phone","run_at","attempts","epoch"}, ...]`。

    单事务 `UPDATE ... WHERE (phone, day) IN (SELECT ...) RETURNING`：返回的行即被本
    执行体占住的行（`state='claimed'` + 写入 owner）。`SET owner` 是必需的——`owner`
    一列同时是"计划 owner"与"当前持有者"，收尾路径按 owner 做作用域校验，
    不写它则接管换不了手。

    `epoch` 是本次领取的 fencing token（**每次领取自增**，单调）：调用方收尾时必须把它
    原样传回 `settle_tasks` / `requeue_task`。没有它，被接管者迟到的写会覆盖接管者的结论。

    `phones` 是本轮**允许领取的账号允许集**（`None` = 不限，保持既有调用点语义）：
    手动 `--only` 轮只该领取"本轮传进来的那几个账号"，宽分片集不得把当日别人的
    `pending` 领走（否则那些行会在执行体里被误当了结——见 `executor_v3` 的 `acc is None`
    处置）。分片集与允许集是**与**关系，两者都满足才领。

    `vshards=()` / 空允许集返回 `[]`（本轮不该领活，不算故障，且不取连接）；表未落地/库
    异常返回 **`None`（哨兵）并告警**——与"表在、但无到期行"的空返回分成两个可判的值，
    调用方据此把"读不通"计入有界放弃闸门（见 `executor_v3.QUEUE_UNREADABLE_MAX_ROUNDS`），
    而不是当成"今天没活了"。本层不替调用方做降级决策。
    """
    shards = tuple(vshards or ())
    if not shards:
        return []
    placeholders = ",".join("?" for _ in shards)
    phone_filter, phone_params = "", []
    if phones is not None:
        allowed = tuple(phones)
        if not allowed:
            return []
        phone_filter = f"AND phone IN ({','.join('?' for _ in allowed)}) "
        phone_params = list(allowed)
    sql = (
        "UPDATE sign_tasks SET state='claimed', owner=?, lease_until=?, "
        "epoch=epoch + 1 "
        "WHERE (phone, day) IN ("
        "SELECT phone, day FROM sign_tasks "
        f"WHERE day=? AND vshard IN ({placeholders}) "
        "AND state='pending' AND run_at<=? "
        + phone_filter +
        "ORDER BY priority, run_at LIMIT ?"
        ") RETURNING phone, run_at, attempts, epoch"
    )
    params = (owner, _lease_until(lease_sec), day, *shards, now or clock.ts(),
              *phone_params, int(limit))
    try:
        conn, lock = _queue_conn()
        with lock:
            rows = conn.execute(sql, params).fetchall()
            conn.commit()
    except Exception as e:
        logger.warning("批量领取签到任务失败（读不通，不等于无可领）: %s", e)
        return None
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
    """重试重排：返回受影响行数（0 = 该行不存在 / 态不允许 / 被 token 拒）。

    `priority` 递增让重试任务排在新任务之后（活号优先）；`attempts` 落库后即**跨执行体
    共享**，接手者不再从 0 起算重试预算。任务回到 `pending` 意味着上一轮的 result 不再
    代表当前状态，故用传入值覆盖（缺省清空）。

    **只重排未了结的行**（`OPEN_STATES`：`pending` / `claimed` / `failed` / `stolen`）：重排
    的语义是"这次尝试要再来一遍"，而已了结（`done` / `skipped`）的行一旦被改回 `pending`
    就会被 `claim_batch` 重新领取——那是一次重复真实登录，且"当日是否了结"的闸门
    （`pending_count`）会凭空又出现待办。迟到的重排（持有者已被接管后才到达）正是这么
    把 done 复活的。

    `epoch` 给了就带 `epoch=?`：只有当前持有者能把在飞任务重排回 `pending`，
    被接管者不得把接管者的任务重新投回池子（那会让同一账号被第三个执行体再领一次）。
    生产调用方（执行体）必须传它；缺省 `None` 只为迁移期调用方与既有测试保留。
    """
    sql = ("UPDATE sign_tasks SET state=?, run_at=?, priority=priority+?, "
           "attempts=attempts+1, lease_until='', result=? "
           f"WHERE phone=? AND day=? AND state IN ({','.join('?' for _ in REQUEUEABLE_STATES)})")
    params = [STATE_PENDING, run_at, int(priority_delta),
              (result or "")[:RESULT_MAX], phone, day, *REQUEUEABLE_STATES]
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


def requeue_failed(day, shards, include_final=False, run_at=None, phones=None):
    """当日回炉：把本业务日 `failed` 行逐行经 `requeue_task` 翻回 `pending`，返回翻回数。

    **为什么必须有**：`claim_batch` 只取 `pending`，v3 执行体弃权（give-up 档）留下的
    `failed` 行若没有这条路，当日就**无人接手**——v2 的等价物是领取层"默认参数可再领
    `retry:` 档"（`claims.try_claim`），v3 的队列以 `pending` 为唯一可领态，档位语义只能
    靠这里翻态来兑现。**档位判据沿用领取层的同一份常量**（`claims.RESULT_RETRY_PREFIX`
    /`RESULT_FINAL_PREFIX`，v18 平移时逐字带过来的协议，不另造一套）：

    - 缺省只回炉 `retry:` 档（窗口外/无点位/"本轮没产生结论"，"该重试"）——当日任何
      后续轮次都接得动的默认档；
    - `include_final=True` 是**有界显式路径**（补签轮）才给的口子：连同 `final:` 档
      （预算耗尽/风控）与**无前缀的历史行**一并回炉。无前缀按保守档与
      `try_claim`"历史行须 `allow_failed` 才放行"同一纪律——判不清原因的宁可要求显式
      路径，也不要无上限重复真实登录。

    实现逐行调 `requeue_task(..., epoch=SELECT 时读到的 epoch)`：状态与 epoch 门全部
    沿用既有原语，不另起第二条写路径。列举与回炉之间被他人重领/接管的行 epoch 已进
    一代，写被 fence 拒（0 行不计入）；`done`/`skipped` 终态行不在 SELECT 里、又被
    `REQUEUEABLE_STATES` 挡第二道——绝不复活（复活 = 当日重复真实登录，第一红线）。
    `vshard=-1` 的历史行不属于任何分片集，回收成 pending 只会变成永不被领取的空转行，
    一律不碰（与 `reap_expired`/`pending_count` 同界）。

    `run_at` 缺省取"现在"（毫秒格式与 `run_at` 同型，字符串序比较不出偏）：回炉行
    立刻可领，与 v2"后续轮次马上接得动"同拍；`priority` 仍按 `requeue_task` 递增一档，
    回炉排在新任务之后。空分片集 / 空允许集 → 0 且不取连接；**库异常 → `None`（哨兵）+
    warning**——0 只用来回答"没有要回炉的 failed 行"。这两件事必须分开：回炉读不通时
    `failed` 行留在原态、不会进 `pending_count` 的视野，调用方若把"坏"当"0 翻回"就等于
    把这批行当日判死（ba-p01-01）。

    `phones` 是本轮回炉的**账号允许集**（`None` = 不限，既有调用点语义不变）：手动
    `--only` 轮只回炉"本轮传进来的那几个账号"的 failed 行，不得把当日别人的
    `final:` 档行一并复活——那会让别人的账号在本轮被误接手（见 `claim_batch` 同参数）。
    """
    shard_set = tuple(shards or ())
    if not shard_set:
        return 0
    placeholders = ",".join("?" for _ in shard_set)
    phone_filter, phone_params = "", []
    if phones is not None:
        allowed = tuple(phones)
        if not allowed:
            return 0
        phone_filter = f" AND phone IN ({','.join('?' for _ in allowed)})"
        phone_params = list(allowed)
    sql = ("SELECT phone, epoch, result FROM sign_tasks "
           "WHERE day=? AND state=? AND vshard >= 0 "
           f"AND vshard IN ({placeholders})" + phone_filter)
    stamp = run_at or clock.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    try:
        conn, lock = _queue_conn()
        with lock:
            rows = conn.execute(sql, (day, STATE_FAILED, *shard_set, *phone_params)).fetchall()
    except Exception as e:
        logger.warning("读取当日弃用任务失败（读不通，不等于无可回炉）: %s", e)
        return None
    retry_prefix = claims_mod.RESULT_RETRY_PREFIX
    flipped = 0
    for r in rows:
        result = str(r["result"] or "")
        # 前缀比较按**字节前缀**（startswith）而不是 LIKE：结果文本里可能出现 `_`
        # （LIKE 通配符），与 `claims.fallback_event` 的同一避坑纪律
        if not include_final and not result.startswith(retry_prefix):
            continue
        if requeue_task(r["phone"], day, stamp, result="", epoch=r["epoch"]):
            flipped += 1
    return flipped


def reclaim_tasks(day, phones):
    """显式重签：把指定账号当日**终态**行（`done` / `skipped`）翻回 `pending`，并把
    这些账号的 `run_at` 一概置为"现在"（让手动签到不必等到计划时刻），返回行数。

    只服务手动 `--only`（"用户主动点的那一下应当照做"）这条有界显式路径：终态复活默认
    是红线（复活回 `pending` 会被 `claim_batch` 重新领取 ⇒ 同一账号当日再真实登录一次），
    故本函数没有缺省调用者，必须显式传具体 `phones`；且只翻 `done`/`skipped` 两类——
    `failed` 走当日回炉口（`requeue_failed`），`claimed`（他人在飞）不改状态。

    `run_at` 置"现在"对两类行都做：`pending` 行（当日计划尚未到点）也要能被立刻领到，
    否则手动签到会睡到计划时刻（窗口外更会一直等）。`epoch` 只在**翻态**的行上自增
    （在飞 `claimed` 行不动 token，免得打断当前持有者的收尾）；清 `owner`/`lease_until`
    同理只对翻态行做。`vshard < 0` 的历史行不属于任何分片集，一律不碰（与 `reap_expired`
    / `pending_count` 同界）。空 `phones` → 0 且不取连接；库异常 → 0 + warning。
    """
    items = tuple(phones or ())
    if not items:
        return 0
    placeholders = ",".join("?" for _ in items)
    stamp = clock.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    # 所有 SET 右值都按**原行值**求值（SQLite 语义）：故用 CASE 就地把终态行翻回 pending，
    # 非终态行只刷新 run_at，一次 UPDATE 完成两件事。
    sql = (
        "UPDATE sign_tasks SET "
        "state=CASE WHEN state IN (?, ?) THEN ? ELSE state END, "
        "owner=CASE WHEN state IN (?, ?) THEN '' ELSE owner END, "
        "lease_until=CASE WHEN state IN (?, ?) THEN '' ELSE lease_until END, "
        "epoch=epoch + CASE WHEN state IN (?, ?) THEN 1 ELSE 0 END, "
        "run_at=? "
        "WHERE day=? AND vshard >= 0 "
        f"AND phone IN ({placeholders})"
    )
    params = (STATE_DONE, STATE_SKIPPED, STATE_PENDING,
              STATE_DONE, STATE_SKIPPED,
              STATE_DONE, STATE_SKIPPED,
              STATE_DONE, STATE_SKIPPED,
              stamp, day, *items)
    try:
        conn, lock = _queue_conn()
        with lock:
            cur = conn.execute(sql, params)
            conn.commit()
            return cur.rowcount
    except Exception as e:
        logger.warning("重签已了结签到任务失败（按未重签处理）: %s", e)
        return 0


def claimed_owners(day=None):
    """当日**在途 `claimed` 行**的持有者全集（去重、非空、升序）——回收豁免名册的输入。

    豁免的**唯一来源是队列本身**：谁真的握着 `claimed` 行，谁才需要判活。按执行体清单
    （`cfg["executors"]`）枚举会漏掉清单外的身份——兜底常驻的运行时串（`fallback@host`）
    从不在清单里，它手上的行于是整段落在豁免之外（ba-p03-01：兜底与定时轮并发可致同一
    账号当天两次真实登录）。调用方对每个持有者判活后，把仍存活者的稳定槽位名传给
    `reap_expired(live_owners=...)`。

    `vshard < 0` 的历史行不属于任何分片集（`reap_expired` 同界不回收），不计入；`owner`
    为空的行无法判活，也不计入（它们本来就不被任何豁免盖住）。`day` 给了就只取该业务日
    ——回收只碰当日，名册同样只该看当日。

    **库异常 → `None`（不是空集）**：`None` 是"读不到"的哨兵，"确实没有在途持有者"回
    `[]`。两者必须区分——空集意味着"所有超期行都不豁免"，拿它去回收会把活持有者的行
    判死（同账号二次登录）。调用方把 `None` 直接传给 `reap_expired`，后者据此**跳过本轮
    回收**（fail-closed）；宁可这一轮不回收，也不放宽回收面。
    """
    sql = ("SELECT DISTINCT owner FROM sign_tasks "
           "WHERE state=? AND owner != '' AND vshard >= 0")
    params = [STATE_CLAIMED]
    if day is not None:
        sql += " AND day=?"
        params.append(day)
    try:
        conn, lock = _queue_conn()
        with lock:
            rows = conn.execute(sql, tuple(params)).fetchall()
    except Exception as e:
        logger.warning("读取在途持有者名单失败（本轮跳过回收）: %s", e)
        return None
    return sorted({str(r[0]) for r in rows if r[0]})


def reap_expired(now=None, day=None, grace_sec=REAP_GRACE_SEC, phones=None,
                 held=(), live_owners=()):
    """回收租约**过期且超出宽限期**的在飞任务：`state='claimed'` 且
    `lease_until < now - grace_sec` 的行回退为 `pending`（清 `owner`/`lease_until`、
    `epoch = epoch + 1`）。返回受影响行数。

    **为什么必须做**：`claim_batch` 只取 `state='pending'` 的行，崩溃/被杀的通道留下的
    `claimed` 行**永远不会被重新领取**——不做回收就是"崩溃即卡死"，该账号当天不再有人签。

    **判活按持有者身份，不只看 `lease_until`（M17）**：租约到期只说明持有者"**可能**已死"，
    不等于真死。宽限期（`REAP_GRACE_SEC`）是这道闸的第一层，但它**兜不住"在途等待"**——
    执行体把行领回 `pending` 之前的排队时间不受租约约束：条目躺在本进程的通道队列里等
    通道、等限速额度、等逐账号 gap、等计划时刻，都可能把 `lease_until` 拖过
    「租约 + 宽限」（60s + 120s）。此刻本进程明明还在正常干活，行却被回收器判死、回退
    `pending`，**下一次 `claim_batch` 又把它原地领回来**——同一账号当天两次真实登录
    （`epoch+1` 只挡迟到的结论写回，挡不住第二次登录；易班侧会因此锁号）。
    故本函数有两道**持有者身份**豁免，两者都是"判活"而不是"改租约"：

    - `held`：**调用方本进程通道队列里在途的 phone 集合**（已领、尚未收尾/重排的条目）。
      这些行一律跳过——它们的持有者就是调用方自己，且正在被处理。
      集合随领取增、随条目处理完减（见 `executor_v3._Ctx.held`），所以本进程真正泄漏的
      行（已领却没进集合）仍会被回收自愈，不会永久卡死。
    - `live_owners`：调用方判定**仍存活**的持有者稳定名（跨进程用；心跳未过期）。持有者
      身份存的是运行时串 `{稳定名}:{进程号}:{代次}`（`yiban.egress.runtime_owner`），故
      与 `reap_abandoned` 同口径按「等值 + `instr` 前缀」匹配，不用 `LIKE`
      （主机名里可能有 `_`，那是 LIKE 的通配符）。心跳过期的死主不在此集合里 → 仍按租约
      + 宽限回收，崩溃恢复链不受影响。**传 `None` 表示名册来源读失败**（`claimed_owners`
      的哨兵）⇒ 本函数**跳过本轮回收**（fail-closed）：空名册会放宽回收面、把活持有者的
      行判死，故读不到时不回收。

    **为什么不用"把 `lease_until` 加长"糊过去**：续租只是把同一道判据的阈值调大，等待
    时间没有上界（积压时队列里的条目能等任意久），阈值迟早被跨过；而且续租会让"崩溃即
    卡死"的行一直续下去。豁免按"谁持有、是否在途"判定才是根因口径。

    `held` 逐行展开成 `NOT IN` 子句并按 `SQL_VAR_CHUNK` 分块：SQLite 的绑定变量上限
    （老版本 999）远小于一个大队列可能的手机号数，整段拼一条会在大站上直接报错、
    回收静默退化成"什么都不回收"。

    **`epoch + 1` 是 fencing 红线**：回退后原持有者可能迟到提交 `settle_tasks`，自增
    token 让它的旧 epoch 写被拒（`settle_tasks(..., epochs=...)` 已支持），否则迟到的旧
    结论会覆盖接手者的结论。

    **必须排除 `vshard = -1` 的历史行**（v18 平移 / v20 补账写入）：它们不属于任何分片集，
    回收成 `pending` 只会变成永不被领取的行（`claim_batch` 的 `vshard IN (...)` 挡着），
    白白制造"看着有活、其实无人领"的假象。`day` 给了就只回收该业务日。

    `phones` 是回收的**账号允许集**（`None` = 不限，既有调用点语义不变）：手动 `--only`
    轮只该回收"本轮账号自己"的陈旧 `claimed` 行——回收别人的行是跨账号写，还会 `epoch+1`
    fence 掉一个仍存活但慢的持有者的迟到收尾。**不整段跳过回收**：手动账号自身若是陈旧
    `claimed`，正需要这条路径把它拉回来（`reclaim_tasks` 只翻 `done`/`skipped`）。

    幂等：回收后的行不再是 `claimed`，重跑 0 行。库异常 → 0 + warning（回收是补偿动作，
    失败不该打断签到；下一轮会再试）；`now` 不可解析是**调用方入参问题**，单独一条
    warning（不与库异常共用文案，免得把排查方向带到存储层）。`live_owners=None`（名册
    来源读失败哨兵）→ 0 + warning，**不做任何回收**（fail-closed）。
    """
    if live_owners is None:
        # 名册来源读失败（`claimed_owners` 的哨兵）：跳过本轮回收，不放宽回收面。
        logger.warning("豁免名册不可用（来源读取失败），跳过本轮回收（fail-closed）")
        return 0
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
        if phones is not None:
            allowed = tuple(phones)
            if not allowed:
                return 0
            sql += f" AND phone IN ({','.join('?' for _ in allowed)})"
            params.extend(allowed)
        # 在途豁免（M17）：本进程通道队列里还在的条目不回收。与上面 `phones` 允许集
        # 取**与**关系——允许集管"能碰哪些账号"，在途集管"哪些行持有者还活着"。
        for chunk in _chunks(tuple(held or ())):
            sql += f" AND phone NOT IN ({','.join('?' for _ in chunk)})"
            params.extend(chunk)
        # 存活持有者豁免（M17）：心跳未过期的执行体手上的行不回收（跨进程同一类重复登录）。
        for owner in tuple(live_owners or ()):
            owner = str(owner or "").strip()
            if not owner:
                continue
            sql += " AND owner <> ? AND instr(owner, ?) <> 1"
            params.extend((owner, owner + ":"))
        conn, lock = _queue_conn()
        with lock:
            cur = conn.execute(sql, tuple(params))
            conn.commit()
            return cur.rowcount
    except Exception as e:
        logger.warning("回收过期签到任务失败（按无可回收处理）: %s", e)
        return 0


def reap_abandoned(owner, day=None):
    """显式回收某个**已确认死亡**的执行体名下仍 `claimed` 的任务：回退 `pending`，
    清 `owner`/`lease_until`、`epoch = epoch + 1`。返回受影响行数。

    `owner` 是**稳定槽位名**（`worker-3@{主机名}` 之类）。持有者列存的是运行时身份
    （`{稳定名}:{进程号}:{代次}`，见 `yiban.egress.runtime_owner`），故按前缀匹配：
    `owner = ?` 覆盖计划行写入的裸稳定名，`instr(owner, ?) = 1` 匹配运行时身份。
    **不用 `LIKE`**——主机名里可能出现 `_`，那是 LIKE 的通配符，会把别的槽位一起吃掉。

    与 `reap_expired` 的分工：后者按"租约过期 ⇒ 可能死了"回收（须过宽限期，见
    `REAP_GRACE_SEC`）；本函数给**监督进程**用——子进程被信号杀死时它直接观测到了异常
    退出（返回码为负），这比心跳过期更强，故不必等宽限期即可回收。不做这一步，被杀执行体
    留下的在领任务只能等 `reap_expired` 的租约 + 宽限期（合计可达数十分钟），期间该账号
    当天无人再签。

    只动 `state='claimed'` 的行：已被别人接管的行 owner 已换、前缀不再命中；`pending`
    重排行与终态行也不该动。回退后的行 `pending` 且租约已放开，下一次 `claim_batch` 即可
    领取。`epoch + 1` 与 `reap_expired` 同一条红线——让原持有者迟到的旧代结论被
    `settle_tasks` 的 `epochs` fence。`vshard >= 0` 与 `reap_expired` 同界：排除 v18 平移 /
    v20 补账的历史行，免得把它们回退成永不被领取（`claim_batch` 的 `vshard IN (...)` 挡着）
    的假待办。`day` 给了就只回收该业务日。

    库异常 → 0 + warning（回收是补偿动作，失败不该打断调用方；下一轮起租约接管兜住）。
    """
    prefix = owner + ":"
    sql = ("UPDATE sign_tasks SET state=?, owner='', lease_until='', epoch=epoch + 1 "
           "WHERE state=? AND vshard >= 0 AND (owner = ? OR instr(owner, ?) = 1)")
    params = [STATE_PENDING, STATE_CLAIMED, owner, prefix]
    if day is not None:
        sql += " AND day=?"
        params.append(day)
    try:
        conn, lock = _queue_conn()
        with lock:
            cur = conn.execute(sql, tuple(params))
            conn.commit()
            return cur.rowcount
    except Exception as e:
        logger.warning("轮末收尸签到任务失败（按未收尸处理）: %s", e)
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


def purge(days=claims_mod.RETENTION_DAYS):
    """清理保留期外的任务行（按业务日字符串比较）。失败仅告警，返回删除行数。

    本表是**唯一台账**（当日计划 + 了结事实 + 手机号/owner/结果），不清理会逐日无限
    增长。**复用与 `claims.purge` 同一套时钟跳变守卫**（`db._clock_jump_guard`，各表各
    一份参照点）：系统时间被拨快 >72h 时按日比较的 cutoff 会一下子跳到未来，"保留期外"
    的判据于是把最近几天的行全算超期——整删当日行等于把当天所有账号放回"待登录"
    （补签轮会重复真实登录）。跳变只跳本轮：守卫在越界路径上也推进参照点，下一轮
    （≤24h 后）即恢复正常清理。守卫的 INSERT upsert 在 WAL 下即持 RESERVED 写锁，
    兼作 DELETE 的事务边界。
    """
    from yiban.store import db
    cutoff = (clock.now() - datetime.timedelta(days=days)).strftime("%Y-%m-%d")
    try:
        conn, lock = _queue_conn()
        with lock:
            ok, note = db._clock_jump_guard(conn, "purge_sign_tasks_clock")
            if not ok:
                logger.error("%s", note)
                conn.rollback()   # 越界路径已在守卫内提交参照点；此处只是解除写锁
                return 0
            cur = conn.execute("DELETE FROM sign_tasks WHERE day < ?", (cutoff,))
            conn.commit()
            return cur.rowcount
    except Exception as e:
        logger.warning("清理签到任务失败: %s", e)
        return 0


def fallback_event(day, exclude_owner=""):
    """兜底常驻的事件签名：默认可接手（`retry:` 档）未了结行的 `(条数, 最新迁移标记)`。

    为什么这一行就是"失败即入队"：执行体弃权（give-up 档）在同一事务里把行置 `failed`
    并写入 `retry:`/`final:` 档位前缀——兜底扫空后按短周期轮询本签名，一变即接手，
    不必等满一个扫描间隔；签名不变则等满间隔为上限。

    **两条收紧，防止唤醒被放大成重复真实登录**：
    - 只数 `retry:` 档：`final:` 档（预算耗尽/风控）在默认参数下兜底**接不动**
      （`requeue_failed` 的缺省档位门），为它醒来只会空转，事件频率必须与"可接手频率"同集；
    - 剔除 `exclude_owner`（兜底自己的稳定槽位名，含 `{稳定名}:{进程号}:{代次}`
      运行时形态）弃权：它一轮扫完本就有紧接的再扫节律，自己的弃权再触发自己的
      唤醒会把"扫→弃权→醒→再扫"接成紧循环。

    "最新迁移标记"取 `MAX(epoch)`——epoch 每次领取进一且永不回退，弃权/接手两向迁移
    都会动它。前缀比较按**字节前缀**（`substr`）而不是 LIKE：主机名/结果文本里可能出现
    `_`（LIKE 通配符），与 `requeue_failed` / `reap_abandoned` 同一避坑纪律；匹配口径
    也同源（等值 + `instr` 前缀）。
    `vshard >= 0` 与其它当日读一致：v18 平移 / v20 补账的 `vshard=-1` 历史行不属于任何
    分片集、永远接不动，不得计入唤醒频率。
    库不可用返回 None：调用方退回等满间隔——事件驱动是**延迟优化**，不是正确性依赖，
    读不到时绝不据此做任何互斥判断。
    """
    tail, ex = "", []
    if exclude_owner:
        tail = " AND owner<>? AND instr(owner, ?)<>1"
        ex = [exclude_owner, exclude_owner]
    sql = ("SELECT COUNT(*) AS n, MAX(epoch) AS h FROM sign_tasks "
           "WHERE day=? AND state=? AND vshard >= 0 AND substr(result, 1, ?)=?" + tail)
    probe = [day, STATE_FAILED, len(claims_mod.RESULT_RETRY_PREFIX),
             claims_mod.RESULT_RETRY_PREFIX, *ex]
    try:
        conn, lock = _queue_conn()
        with lock:
            row = conn.execute(sql, tuple(probe)).fetchone()
        return (int(row["n"] or 0), int(row["h"] or 0))
    except Exception as e:
        logger.debug("读取兜底事件签名失败（按无事件处理）: %s", e)
        return None


def pending_count(day, vshards, phones=None):
    """当日「我的分片集」内仍待办（`state='pending'`）的行数——**当日是否了结的闸门**。

    为什么必须带 `vshard` 过滤，而不能用 `day_counts(day)["open"]`：历史平移/补账写入的
    `vshard=-1` 行里 `state='failed'` 属 `OPEN_STATES`，可它们永不被 `claim_batch` 领取、
    也没有 owner/epoch 可供 `requeue`。把它们算作"未了结"，该日就**永远不了结**（补签轮
    反复空跑）。分片集恒是 `0..V-1` 的子集，故历史行天然不在其中；SQL 里再显式写一遍
    `vshard >= 0` 是双保险（防调用方传入非法分片集）。

    `phones` 与 `claim_batch` 同义（`None` = 不限）：收干判据必须与领取范围**同集**，
    否则手动 `--only` 轮会因"同分片里别人的 pending 行"永远数不完而空转到超时。

    `vshards=()` / 空允许集 → 0（本轮不该领活，不算故障，且不取连接，与 `claim_batch`
    同口径）；**库异常 → `None`（哨兵）+ warning**。0 与 `None` 是两件事：0 才允许调用方
    走"没有待办"的收干分支，`None` 意味着"读不出来"，不得据此判了结（本函数是收干判据的
    事实源，把它折回 0 就是全天零签到而现场只有两条 WARNING——ba-p01-01）。调用方对
    `None` 的处理必须是"继续等下一轮 + 计入有界放弃闸门"，不是抛出去打断整轮签到。
    """
    shards = tuple(vshards or ())
    if not shards:
        return 0
    placeholders = ",".join("?" for _ in shards)
    phone_filter, phone_params = "", []
    if phones is not None:
        allowed = tuple(phones)
        if not allowed:
            return 0
        phone_filter = f" AND phone IN ({','.join('?' for _ in allowed)})"
        phone_params = list(allowed)
    sql = ("SELECT COUNT(*) FROM sign_tasks WHERE day=? AND state=? "
           f"AND vshard >= 0 AND vshard IN ({placeholders})" + phone_filter)
    try:
        conn, lock = _queue_conn()
        with lock:
            row = conn.execute(sql, (day, STATE_PENDING, *shards, *phone_params)).fetchone()
    except Exception as e:
        logger.warning("读取当日待办任务计数失败（读不通，不等于无待办）: %s", e)
        return None
    return int(row[0]) if row else 0


def open_count(day):
    """当日仍**未了结**（`OPEN_STATES`）且**可被领取**（`vshard >= 0`）的行数。

    这是「当日是否了结」的库内事实源，供**没有分片上下文**的调用方使用
    （`state_io.has_undone_accounts_today` 由宿主 run.sh / 容器调度器调用，只拿得到
    状态目录与业务日，拿不到本执行体的分片集，故不能走 `pending_count(day, vshards)`）。

    **`vshard >= 0` 过滤不是可选优化，是判据的一部分**：历史平移 / v20 补账写入的
    `vshard=-1` 行永不被 `claim_batch` 领取（`vshard IN (分片集)` 挡着），把它们的
    `failed`（属 `OPEN_STATES`）算作"未了结"，该日就**永远不了结**、补签轮反复空跑
    ——与 `pending_count` 的核心口径一致，理由详见其文档。

    只数 `OPEN_STATES`（`pending`/`claimed`/`failed`/`stolen`），与 `day_counts(day)["open"]`
    的差别仅在这个 `vshard` 过滤（后者不过滤，故不能当闸门用）。**库不可用 → `None`
    （哨兵）+ warning**：0 只用来回答"当日确实没有未了结行"。调用方
    （`state_io.has_undone_accounts_today`）见到 `None` 必须答"仍有未了结"并出声，
    **不得**只回退状态文件——状态文件干净 + 库读不通 ⇒ 判"无未了结" ⇒ 补签轮不跑 ⇒
    漏签，那正是本哨兵要消灭的形状（ba-p01-01）。
    """
    placeholders = ",".join("?" for _ in OPEN_STATES)
    sql = ("SELECT COUNT(*) FROM sign_tasks WHERE day=? AND vshard >= 0 "
           f"AND state IN ({placeholders})")
    try:
        conn, lock = _queue_conn()
        with lock:
            row = conn.execute(sql, (day, *OPEN_STATES)).fetchone()
    except Exception as e:
        logger.warning("读取当日未了结任务计数失败（读不通，不等于已了结）: %s", e)
        return None
    return int(row[0]) if row else 0


def day_counts(day):
    """当日各 state 计数与派生口径——供调用方判"当日是否了结"、给进度展示取数。

    与 `claims.stats` 同口径：`GROUP BY state` 计数、空 day 全 0（那是"当日没有行"这个
    事实），并在状态计数之外派生三项——调用方**不需要自己求和**。**库不可用 → `None`
    （哨兵）+ warning**：全 0 字典里的 `open=0` 就是"没有未了结"，不许拿来回答
    "读不出来"（ba-p01-01）。展示类调用方拿到 `None` 必须把"未知"如实标成未知，
    不得静默画成全 0。

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
        logger.warning("读取当日签到任务计数失败（读不通，不返回全 0）: %s", e)
        return None
    out["settled"] = sum(out[s] for s in SETTLED_STATES)
    out["open"] = sum(out[s] for s in OPEN_STATES)
    out["total"] = out["settled"] + out["open"]
    return out


def latest_day():
    """`sign_tasks` 里最近一次有记录的业务日（`MAX(day)`）；表空 / 库不可用返回 None。

    展示口径的"上次实领是哪天"：取最近一次**有记录**的日而不是"昨天"——周末停签后按
    "昨天"取会让整列空白到下一个工作日。
    """
    try:
        conn, lock = _queue_conn()
        with lock:
            row = conn.execute("SELECT MAX(day) FROM sign_tasks").fetchone()
    except Exception as e:
        logger.debug("读取最近一次签到任务日失败（按空处理）: %s", e)
        return None
    return row[0] if row else None


def owners_for_day(day):
    """某业务日 `phone -> owner` 映射，供账号列表批量标注归属。

    **必须一次取全**：账号列表可能有几百行，逐账号查会让一次列表请求变成几百次查询。
    只回 owner 原串、**不解析角色、不脱敏**——角色口径与脱敏是展示层的事（Web 层用
    `yiban.egress.parse_owner` 折成角色与槽位，绝不把 owner 原串回给前端）。
    库未初始化/表未落地/读不通 → `{}` 且不抛：本函数只喂展示，不参与退出/了结判定，
    故按空处理而不取哨兵（为什么它不在那条口径内，见模块头降级口径）。
    """
    try:
        conn, lock = _queue_conn()
        with lock:
            rows = conn.execute(
                "SELECT phone, owner FROM sign_tasks WHERE day=?", (day,)).fetchall()
        return {r["phone"]: r["owner"] for r in rows}
    except Exception as e:
        logger.debug("读取当日任务归属失败（按空处理）: %s", e)
        return {}


def owners_since(days=None):
    """保留期内出现过的执行体身份串（去重，升序）——供"槽位号只增不复用"用。

    用途：删除清单里**当前最大**那一行之后，纯函数只能给出"最大值 + 1"（它会拿到刚空出
    的号）；追加接口据此再跳过"保留期内真用过的号"。只回答"出现过哪些身份串"，
    **不解析角色**（解析是展示层的事）。库不可用时返回 `[]`（调用方退回"只按清单
    最大值 +1"，不影响追加本身）。
    """
    days = claims_mod.RETENTION_DAYS if days is None else days
    cutoff = (clock.now() - datetime.timedelta(days=days)).strftime("%Y-%m-%d")
    try:
        conn, lock = _queue_conn()
        with lock:
            rows = conn.execute(
                "SELECT DISTINCT owner FROM sign_tasks WHERE day >= ? ORDER BY owner",
                (cutoff,)).fetchall()
        return [r["owner"] for r in rows if r["owner"]]
    except Exception as e:
        logger.debug("读取执行体历史身份失败（按空处理）: %s", e)
        return []


def activity(day):
    """当日**按执行体归属**的分组计数（前端"谁做了多少"的数据来源），已折成 KPI 三键。

    与 `day_counts(day)` 的区别只在分组维度：`day_counts` 回答"当日了结了多少"，本函数
    回答"这些活分别是谁做的"。`sign_tasks` 的 state 词汇比旧领取池多（`pending`/`skipped`/
    `stolen`），故按 KPI 语义折一次（**折叠口径只此一处**，展示层不再各写一份）：

    | 返回键 | 折叠来源 | 含义 |
    |--------|----------|------|
    | `claimed` | `claimed` + `stolen` | 正在被某执行体持有（在飞） |
    | `done` | `done` + `skipped` | 当日已了结（`TASKS_SETTLED_STATES`） |
    | `failed` | `pending` + `failed` | 未了结：待领取或已弃权待接手 |
    | `total` | 全部行 | 当日总行数 |

    只回 owner 原串、**不解析角色、不脱敏**（角色与脱敏是展示层的事，见 `owners_for_day`）。
    返回 `[{"owner":…, "claimed":n, "done":n, "failed":n, "total":n}, …]`（按 owner 升序，
    顺序稳定）；空库/库未初始化/读不通 → `[]` 且不抛，与 `owners_for_day` 同一条展示口径
    （按空处理而不取哨兵，理由见模块头降级口径）。未登记的 state 照实
    计进 `total` 但不进三键（不丢数）。
    """
    out = {}
    try:
        conn, lock = _queue_conn()
        with lock:
            rows = conn.execute(
                "SELECT owner, state, COUNT(*) AS n FROM sign_tasks "
                "WHERE day=? GROUP BY owner, state ORDER BY owner", (day,)).fetchall()
    except Exception as e:
        logger.debug("读取执行体归属计数失败（按空处理）: %s", e)
        return []
    for r in rows:
        out.setdefault(r["owner"], {})[r["state"]] = r["n"]
    result = []
    for owner, counts in out.items():
        claimed = counts.get(STATE_CLAIMED, 0) + counts.get(STATE_STOLEN, 0)
        done = counts.get(STATE_DONE, 0) + counts.get(STATE_SKIPPED, 0)
        failed = counts.get(STATE_PENDING, 0) + counts.get(STATE_FAILED, 0)
        item = {"owner": owner, "claimed": claimed, "done": done, "failed": failed}
        item["total"] = sum(counts.values())
        result.append(item)
    return result
