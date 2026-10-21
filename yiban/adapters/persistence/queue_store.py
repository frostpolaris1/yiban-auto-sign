# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""任务队列访问层：`sign_tasks` 的批量领取 / 批量收尾 / 重排 / 当日计数（v18）。

**算法溯源**
本表是数据库做任务队列的标准形态，各机制对应如下：
- **claim-at-dispatch（领取即执行）**：调用方只在通道空闲时领**一行**、领到即执行。
  领取与执行之间没有进程内排队，故一行的持有时间 = 一次尝试的时间（有界），不再是
  "领回本地等通道/等令牌/等间隔/等计划时刻"的**无上界**等待。批量预取与进程内
  `PriorityQueue` 因此退场（工单 `l1rx`）。
- **elbow（肘界谓词）**：领取谓词可带一个肘界 `run_at <= now - elbow_sec`——兜底常驻
  只领"已过期超过肘界"的行，"谁该做"是**行的函数**而不是**进程身份的函数**。谓词语义
  的唯一事实源是 `yiban/core/elbow.py`，本层的 SQL 是它的**下推实现**（见 `claim_batch`）。
- Fencing Token（栅栏令牌）：Martin Kleppmann。`epoch` 仍是所有权变更的单调计数：每次
  领取 `epoch=epoch+1`，`settle_tasks` / `requeue_task` 以 `(owner, epoch)` 双条件结算，
  拒掉迟到写。**注意**：任务级**租约**（`lease_until` / 宽限 / `reap_expired`）已删除
  （工单 `l1rx`）——真正的持有者死亡由**监督进程按进程句柄**处置（`reap_abandoned`），
  不靠时间戳判活；`epoch` 只服务"回退重排后原持有者不得再写"这一件事。
- SKIP LOCKED（PostgreSQL 9.5 引入）的等价形态：单条 `UPDATE ... WHERE phone IN
  (SELECT ...) RETURNING` 在本仓 SQLite 的写者串行语义下天然原子，无需显式 `SKIP LOCKED`
  语法；若迁到 PG/MySQL 这是等价替换点（MySQL 8 无 `UPDATE...RETURNING`，须另写
  `UPDATE...LIMIT` + 回查）。
- 分片领取围栏：Rendezvous Hashing（Thaler & Ravishankar，见 `yiban/core/hrw.py`）。
  `vshard IN (...)` 让执行体只碰自己名下分片，是「领取零竞争」的来源。

**功能**
- `claim_batch`：按虚分片领取**到期**任务——单条 `UPDATE ... RETURNING`，在
  SQLite 的写者串行语义下天然原子（等价于 PG 的 `SKIP LOCKED`，本仓无需跨机形态）。
  `limit` 缺省 1（claim-at-dispatch：调用方接口在通道空闲时调、领一条执行一条）；
  `elbow_sec` 是肘界（0 = 到期即可领；正数 = 只领过期达到该时长的行）。领取自增
  `epoch` 并随行返回，收尾侧据此拒绝被接管者的迟到写；
- `settle_tasks`：一批完成的任务在单事务里收尾（owner + epoch 作用域），不逐账号 commit；
- `requeue_task`：失败重排——`priority` / `attempts` 递增、`state` 回 `pending`；只对
  未了结行生效（终态行不得被重排复活，否则会被重新领取＝当日再登录一次）；
- `requeue_failed`：当日回炉——把 `failed` 行按 `retry:`/`final:` 档位逐行走
  `requeue_task`（state+epoch 门沿用，不另造协议），`claim_batch` 只取 `pending`，
  没有这条路 v3 的当日失败就无人接手；
- `reclaim_tasks`：显式重签——把指定账号当日的**终态**行（`done`/`skipped`）翻回
  `pending`，只服务手动 `--only`（终态复活默认是红线，故无缺省调用者）；
- `reap_abandoned`：对**已确认死亡**（进程句柄已消失，异常退出）的执行体名下 `claimed`
  行立即回退 `pending`——判活靠**进程句柄**而不是心跳文件的时间戳（工单 `l1rx`：心跳降级
  为纯 UI）。任务级租约（`lease_until`）与按时间戳的 `reap_expired` 已删除；不再有
  "租约过期即回收"这条路；
- `pending_count`：当日「我的分片集」内的待办计数（带 `vshard` 过滤的"当日是否了结"
  闸门，见该函数说明）；
- `fallback_event`：兜底常驻的"失败即入队"读取端——默认可接手（`retry:` 档）未了结行的
  事件签名 `(条数, 最新迁移标记)`，短轮询变化即接手；
- `purge`：按保留期清理本表存量（带时钟跳变守卫，见 `yiban/store/cleanup.py` 编排）；
- `day_counts`：当日各 state 计数与派生口径（settled/open/total）——调用方据此给
  进度展示取数；**读不通回 `None` 哨兵**（降级口径见下）；`open` **不过滤 `vshard`，
  不得当"当日是否了结"的闸门**（那会把永不被领取的 `vshard=-1` 惰性行算进去），
  闸门用 `pending_count`（有分片上下文）或 `open_count`（无分片上下文）；
- `run_at_by_phone`：当日 `phone -> run_at` 映射（一次取全）——展示层读**执行计划时刻**
  的唯一事实源；只读 `vshard >= 0` 的真实计划行；**读不通回 `None` 哨兵**（降级口径见下）；
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
重签/桶状态落库）与展示侧 `yiban/adapters/statefiles/state_io.py`、`web/services/executor_env.py`
（经路由 `web/routes/accounts_api.py` 暴露）——单池后两条路径读写的是**同一张表**
`sign_tasks`。
`egress_state` 的调用方是 `yiban/engine/token_bucket.py`（`EgressLimiter.persist` /
`restore_from_store`）。

**降级口径（全模块唯一一条，ba-p01-01）**
读不通（库异常/表未落地/锁等待超时）**一律回 `None` 哨兵**，绝不回"空"值：`[]`、`0`、
全 0 字典、`0` 翻回数都只用来回答"真的没有这一类行"。沿用同文件既有惯例
（各读取面回 `None` 哨兵的口径统一，不另造第二种形状）。
理由：**一次读库失败，永远不得被任何读者解释成"今天没有待办了 / 活已了结"**——把"坏"
折叠成"空"就是全天零签到而现场只有两条 WARNING。执行体侧的有界退避与响亮告警见
`yiban/engine/executor_v3.QUEUE_UNREADABLE_MAX_ROUNDS`。
`vshards=()` / 空允许集**不是**故障（本轮不该领活，与库无关），照旧回空值且不取连接。
补偿动作与纯展示读面（`settle_tasks`、`requeue_task`、`reclaim_tasks`、
`reap_abandoned`、`load_egress_state`、`save_egress_state`、`purge`、`fallback_event`、
`latest_day`、`owners_for_day`、`owners_since`、`activity`）不在这条口径内：它们的失败
方向是"这一轮少做一次"或"页面少显示一块"，不参与任何"退出/了结"判定，改动只会扩大面。

"""
import datetime
import logging
from typing import Any, Iterable, Iterator, Mapping

from yiban.adapters.persistence import claims as claims_mod
from yiban.core import clock
from yiban.core import status as yiban_status

logger = logging.getLogger("yiban.store.queue_store")

#: 领取行数缺省 = 1（**claim-at-dispatch**）：调用方只在通道空闲时领取一行、领到即执行。
#: 批量预取已退场（工单 `l1rx`）——预取让行的持有时间变成
#: "排队等待时间"，无上界，正是租约族复杂度的根源。
CLAIM_LIMIT = 1

#: 单条 SQL 的绑定变量分块大小。SQLite 的 `SQLITE_MAX_VARIABLE_NUMBER` 老版本是 999
#: （新版本 32766），而 `row_owners` 的 `IN` 与手动 `--only` 的允许集要把手机号逐个展开
#: 成占位符——大站一个补货循环就能攒出上千条。整段拼一条会在变量数超限时直接抛
#: `sqlite3.ProgrammingError`。取 900：给 day 等其余占位留出余量。
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


def _queue_conn() -> tuple[Any, Any]:
    """队列库连接与写锁的**唯一取点**。

    当前与业务表同库同连接（`sign_tasks` 落在 yiban.db）；按属性取而不是模块级
    from-import，独立队列库或打桩才切得动。
    """
    from yiban.adapters.persistence import connection
    return connection.get_conn(), connection._conn_lock


def _shift_stamp(stamp: object, sec: float) -> str:
    """时间串平移 `sec` 秒，返回毫秒精度、与 `run_at` 同格式的可比字符串。

    本库的时间串一律北京时间（`yiban.clock`），**不能用 SQL 的 `datetime(..., '-N seconds')`
    代替**：它的输出**没有毫秒**（`datetime('2026-09-22 06:40:00','-120 seconds')` 得
    `'2026-09-22 06:38:00'`），而 `run_at` 是毫秒格式；`'…06:38:00' < '…06:38:00.000'`
    为真，拿它跟毫秒串做字符串比较会在同一秒内错位。**不是时区问题**：纯秒平移下把北京时间
    当 UTC 解释只差一个常量偏移，平移量不变（真正的时区坑在 SQL 的 `'now'`：它是 UTC）。
    `claim_batch` 的肘界下推用本函数把 `now` 平移成 `now - elbow`。
    """
    text = str(stamp)
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            t = datetime.datetime.strptime(text, fmt)  # noqa: DTZ007 解析库内业务本地 naive 时间串（上方注释已说明非时区问题）
            break
        except ValueError:
            continue
    else:
        raise ValueError(f"不可解析的时间串: {text!r}")
    return (t + datetime.timedelta(seconds=sec)).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def _chunks(items: Iterable[Any], size: int = SQL_VAR_CHUNK) -> Iterator[list[Any]]:
    """把可迭代对象切成不超过 `size` 的定长块（生成器，空输入不产出）。"""
    chunk: list[Any] = []
    for item in items:
        chunk.append(item)
        if len(chunk) >= size:
            yield chunk
            chunk = []
    if chunk:
        yield chunk


def claim_batch(owner: str, day: str, vshards: Iterable[int], now: str | None = None,
                limit: int = CLAIM_LIMIT, elbow_sec: float = 0.0,
                phones: Iterable[str] | None = None) -> list[dict[str, Any]] | None:
    """按分片（`vshard`）领取**到期**任务。返回
    `[{"phone","run_at","attempts","epoch"}, ...]`（`limit` 缺省 1）。

    单事务 `UPDATE ... WHERE phone IN (SELECT ...) RETURNING`：返回的行即被本执行体占住
    的行（`state='claimed'` + 写入 owner）。`SET owner` 是必需的——`owner` 一列同时是
    "计划 owner"与"当前持有者"，收尾路径按 owner 做作用域校验，不写它则接管换不了手。

    **claim-at-dispatch（工单 `l1rx`）**：调用方（执行体的通道）**只在空闲时**调用本函数，
    领到即执行；`limit` 缺省 1。领取与执行之间不再有进程内排队，故一行的持有时间 =
    一次尝试的时间（有界）。批量预取已退场——正是"先领一大批、再在本地慢慢等"造出了
    "租约过期被判死、原地重领会双登录"的整族复杂度。

    `elbow_sec` 是**肘界**（秒，见 `yiban/core/elbow.py`）：只领 `run_at <= now - elbow_sec`
    的行。0 = 到期即可领（定时执行体 / 手动 / 补签轮的语义）；正数 = 只领"已经过期达到
    该时长"的行——兜底常驻用它避免与计划轮抢新到点的活，"谁该做"于是成为**行的函数**，
    10-07 型启动竞态从机制上消失（工单 `l1rx`）。本层的 SQL 是
    `elbow.may_claim` 的**下推实现**：两者的等价由 `tests/test_claim_dispatch_elbow_e2e.py`
    对拍钉住。

    `epoch` 是本次领取的 fencing token（**每次领取自增**，单调）：调用方收尾时必须把它
    原样传回 `settle_tasks` / `requeue_task`。没有它，被回退重排后的原持有者迟到的写会
    覆盖新持有者的结论。

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
    # 肘界下推：到期判据从"run_at <= now"平移成"run_at <= now - elbow"。now 与 run_at 同
    # 域（北京时间毫秒串），用 `_shift_stamp` 做**同域平移**（不能用 SQL 的 datetime 算术，
    # 它丢毫秒，见 `_shift_stamp`）。
    try:
        cutoff = _shift_stamp(now or clock.ts(), -float(elbow_sec or 0.0))
    except ValueError:
        # now 不可解析是**调用方入参问题**：按"读不通"处置（回 `None` 哨兵，fail-closed），
        # 让调用方的有界放弃闸门计数——回 `[]` 会被当"本轮无到期行"，把整轮静默空转。
        logger.warning("领取到期任务的时刻参数不可解析（读不通，本轮不领取）: %r", now)
        return None
    sql = (
        "UPDATE sign_tasks SET state='claimed', owner=?, epoch=epoch + 1 "
        "WHERE (phone, day) IN ("
        "SELECT phone, day FROM sign_tasks "
        f"WHERE day=? AND vshard IN ({placeholders}) "
        "AND state='pending' AND run_at<=? "
        + phone_filter +
        "ORDER BY priority, run_at LIMIT ?"
        ") RETURNING phone, run_at, attempts, epoch"
    )
    params = (owner, day, *shards, cutoff, *phone_params, int(limit))
    try:
        conn, lock = _queue_conn()
        with lock:
            rows = conn.execute(sql, params).fetchall()
            conn.commit()
    except Exception as e:
        logger.warning("领取签到任务失败（读不通，不等于无可领）: %s", e)
        return None
    return [{"phone": r["phone"], "run_at": r["run_at"], "attempts": r["attempts"],
             "epoch": r["epoch"]} for r in rows]


def settle_tasks(owner: str, day: str, outcomes: Iterable[tuple[str, str]] | None,
                 state: str = STATE_DONE, epochs: Mapping[str, int] | None = None) -> int:
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
    sql = ("UPDATE sign_tasks SET state=?, result=? "
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


def requeue_task(phone: str, day: str, run_at: str, priority_delta: int = 1, result: str = "",
                 epoch: int | None = None) -> int:
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
           "attempts=attempts+1, result=? "
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


def requeue_failed(day: str, shards: Iterable[int], include_final: bool = False,
                   run_at: str | None = None, phones: Iterable[str] | None = None) -> int | None:
    """当日回炉：把本业务日 `failed` 行逐行经 `requeue_task` 翻回 `pending`，返回翻回数。

    **为什么必须有**：`claim_batch` 只取 `pending`，v3 执行体弃权（give-up 档）留下的
    `failed` 行若没有这条路，当日就**无人接手**——v2 的等价物是领取层"默认参数可再领
    `retry:` 档"（`claims.try_claim`），v3 的队列以 `pending` 为唯一可领态，档位语义只能
    靠这里翻态来兑现。**档位判据沿用领取层的同一份常量**（`yiban.status.RESULT_RETRY_PREFIX`
    /`RESULT_FINAL_PREFIX`，v18 平移时逐字带过来的协议，128-P1 迁入 status，不另造一套）：

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
    一律不碰（与 `reap_abandoned`/`pending_count` 同界）。

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
    retry_prefix = yiban_status.RESULT_RETRY_PREFIX
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


def reclaim_tasks(day: str, phones: Iterable[str]) -> int:
    """显式重签：把指定账号当日的行翻回 `pending`，并把它们的 `run_at` 一概置为"现在"
    （让手动签到不必等到计划时刻），返回行数。

    只服务手动 `--only`（"用户主动点的那一下应当照做"）这条有界显式路径：复活默认是红线
    （复活回 `pending` 会被 `claim_batch` 重新领取 ⇒ 同一账号当日再真实登录一次），故本
    函数没有缺省调用者，必须显式传具体 `phones`。翻三类行：

    - `done` / `skipped`（终态）：用户主动触发即重签；
    - `claimed`（**在飞行**）：这是**卡死行的逃生口**（工单 `l1rx` 必修 R2）。任务级租约
      删除后，"持有者已死"不再由时间戳判定，故本口对所有 `claimed` 一律翻回——用户点的
      那一下对卡死行必须有效。翻动**换代**（`epoch+1`）并清 `owner`，故原持有者迟到的
      `settle_tasks` / `requeue_task` 仍被 fence 住、写不回去。
      **代价（登记为残余）**：原持有者若仍存活（只有兜底腿能与本口并存——手动轮持全局
      运行锁，全量轮起不来），会有一次同账号并发登录。这是"逃生口"换来的风险，方向由
      用户在管理面上显式选择。
    - `failed` 走当日回炉口（`requeue_failed`），不在此翻。

    `run_at` 置"现在"对全部在册行都做：`pending` 行（当日计划尚未到点）也要能被立刻领到，
    否则手动签到会睡到计划时刻（窗口外更会一直等）。`vshard < 0` 的历史行不属于任何分片集，
    一律不碰（与 `reap_abandoned` / `pending_count` 同界）。空 `phones` → 0 且不取连接；
    库异常 → 0 + warning。
    """
    items = tuple(phones or ())
    if not items:
        return 0
    placeholders = ",".join("?" for _ in items)
    stamp = clock.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    # 所有 SET 右值都按**原行值**求值（SQLite 语义）：故用 CASE 就地把可翻态翻回 pending，
    # 其余行只刷新 run_at，一次 UPDATE 完成两件事。
    sql = (
        "UPDATE sign_tasks SET "
        "state=CASE WHEN state IN (?, ?, ?) THEN ? ELSE state END, "
        "owner=CASE WHEN state IN (?, ?, ?) THEN '' ELSE owner END, "
        "epoch=epoch + CASE WHEN state IN (?, ?, ?) THEN 1 ELSE 0 END, "
        "run_at=? "
        "WHERE day=? AND vshard >= 0 "
        f"AND phone IN ({placeholders})"
    )
    params = (STATE_DONE, STATE_SKIPPED, STATE_CLAIMED, STATE_PENDING,
              STATE_DONE, STATE_SKIPPED, STATE_CLAIMED,
              STATE_DONE, STATE_SKIPPED, STATE_CLAIMED,
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


def reap_abandoned(owner: str, day: str | None = None) -> int:
    """显式回收某个**已确认死亡**的执行体名下仍 `claimed` 的任务：回退 `pending`，
    清 `owner`、`epoch = epoch + 1`。返回受影响行数。

    `owner` 是**稳定槽位名**（`worker-3@{主机名}` 之类）。持有者列存的是运行时身份
    （`{稳定名}:{进程号}:{代次}`，见 `yiban.egress.runtime_owner`），故按前缀匹配：
    `owner = ?` 覆盖计划行写入的裸稳定名，`instr(owner, ?) = 1` 匹配运行时身份。
    **不用 `LIKE`**——主机名里可能出现 `_`，那是 LIKE 的通配符，会把别的槽位一起吃掉。

    **这是任务级租约删除后的唯一"崩溃自愈"路径（工单 `l1rx`）**：判活不再看心跳文件的
    时间戳（心跳降级为纯 UI），而是**监督进程按进程句柄**——子进程被信号杀死时它直接
    观测到异常退出（返回码为负），比"心跳过期 ⇒ 可能死了"更强的证据。不做这一步，被杀
    执行体留下的在领任务永远停在 `claimed`，当天该账号无人再签。

    **未走这条路的崩溃（进程内未预期异常、未被杀）不被自动回收**：留下的 `claimed` 行由
    `crash_semantics.report_unknown_outcomes`（写前标记 `start` 无终态）识别并告警，
    **不自动重签**（l1rx 的裁决；修复留给后续批次）。这不是漏项，是删除租约族的既定口径。

    只动 `state='claimed'` 的行：已被别人接管的行 owner 已换、前缀不再命中；`pending`
    重排行与终态行也不该动。回退后的行 `pending`，下一次 `claim_batch` 即可领取。
    `epoch + 1` 是 fencing 红线——让原持有者迟到的旧代结论被 `settle_tasks` 的 `epochs`
    fence。`vshard >= 0` 与 `claim_batch` 同界：排除 v18 平移 / v20 补账的历史行，免得把
    它们回退成永不被领取（`claim_batch` 的 `vshard IN (...)` 挡着）的假待办。`day` 给了就
    只回收该业务日。

    库异常 → 0 + warning（回收是补偿动作，失败不该打断调用方；下一次监督进程轮末会再试）。
    """
    prefix = owner + ":"
    sql = ("UPDATE sign_tasks SET state=?, owner='', epoch=epoch + 1 "
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


def load_egress_state(egress: str) -> dict[str, Any] | None:
    """读某出口的令牌桶状态：`{"rate","burst","tat","half_open_sec"}`；无记录/库异常 → `None`。

    `rate` 单位是**账号尝试/s**（attempt/s，见 `yiban/engine/token_bucket.py` 模块头）。
    `half_open_sec` 是**剩余**半开秒数（相对量，与调用方注入的单调钟无关）；0 表示无半开。
    读失败**不抛**：调用方（执行体）按"没有记忆"回退出厂速率——重启后拿不到速率也不该
    卡住签到，出厂速率本就比自适应上限保守。
    """
    try:
        conn, lock = _queue_conn()
        with lock:
            row = conn.execute(
                "SELECT rate, burst, tat, half_open_sec FROM egress_state WHERE egress=?",
                (egress,)
            ).fetchone()
    except Exception as e:
        logger.warning("读取出口令牌桶状态失败（按无记录处理）: %s", e)
        return None
    if row is None:
        return None
    return {"rate": row["rate"], "burst": row["burst"], "tat": row["tat"],
            "half_open_sec": row["half_open_sec"]}


def save_egress_state(egress: str, rate: float, burst: float, tat: float,
                      half_open_sec: float = 0.0, now: str | None = None) -> bool:
    """UPSERT 某出口的令牌桶状态，返回是否写入成功。

    `rate` 单位 = 账号尝试/s（attempt/s）；`burst` 是突发额度（尝试数）；`tat` 是 GCRA 的
    理论到达时刻（浮点秒，与调用方注入的时钟**同域**）。`half_open_sec` 是**剩余**半开
    秒数（相对量，restore 时按新进程的 `now` 重新起算；不落绝对时刻，避免跨时钟域）。
    `now` 是写入时刻（墙钟字符串，缺省 `clock.ts()`）——它与 `tat` / `half_open_sec`
    不同域，只作"上次落库时刻"给运维看，**不可**拿来与它们直接比较。写失败只告警不抛：
    桶状态是记忆不是业务事实，写不进去不该阻断签到。
    """
    sql = ("INSERT INTO egress_state (egress, rate, burst, tat, half_open_sec, updated_at) "
           "VALUES (?,?,?,?,?,?) ON CONFLICT(egress) DO UPDATE SET "
           "rate=excluded.rate, burst=excluded.burst, tat=excluded.tat, "
           "half_open_sec=excluded.half_open_sec, updated_at=excluded.updated_at")
    params = (egress, float(rate), float(burst), float(tat), float(half_open_sec),
              now or clock.ts())
    try:
        conn, lock = _queue_conn()
        with lock:
            conn.execute(sql, params)
            conn.commit()
        return True
    except Exception as e:
        logger.warning("写入出口令牌桶状态失败（不影响签到）: %s", e)
        return False


def purge(days: int = claims_mod.RETENTION_DAYS) -> int:
    """清理保留期外的任务行（按业务日字符串比较）。失败仅告警，返回删除行数。

    本表是**唯一台账**（当日计划 + 了结事实 + 手机号/owner/结果），不清理会逐日无限
    增长。**复用与 `claims.purge` 同一套时钟跳变守卫**（`db._clock_jump_guard`，各表各
    一份参照点）：系统时间被拨快 >72h 时按日比较的 cutoff 会一下子跳到未来，"保留期外"
    的判据于是把最近几天的行全算超期——整删当日行等于把当天所有账号放回"待登录"
    （补签轮会重复真实登录）。跳变只跳本轮：守卫在越界路径上也推进参照点，下一轮
    （≤24h 后）即恢复正常清理。守卫的 INSERT upsert 在 WAL 下即持 RESERVED 写锁，
    兼作 DELETE 的事务边界。
    """
    from yiban.adapters.persistence import db
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


def fallback_event(day: str, exclude_owner: str = "") -> tuple[int, int] | None:
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
        # 前缀必须带分隔符：稳定槽位名 worker-1 与 worker-12:... 互为字节前缀，
        # 裸名匹配会把别人名下的行一起剔出唤醒签名（工单 ba-p01-06）。与
        # reap_expired / reap_abandoned / requeue_failed 的 `owner + ":"` 同源。
        ex = [exclude_owner, exclude_owner + ":"]
    sql = ("SELECT COUNT(*) AS n, MAX(epoch) AS h FROM sign_tasks "
           "WHERE day=? AND state=? AND vshard >= 0 AND substr(result, 1, ?)=?" + tail)
    probe = [day, STATE_FAILED, len(yiban_status.RESULT_RETRY_PREFIX),
             yiban_status.RESULT_RETRY_PREFIX, *ex]
    try:
        conn, lock = _queue_conn()
        with lock:
            row = conn.execute(sql, tuple(probe)).fetchone()
        return (int(row["n"] or 0), int(row["h"] or 0))
    except Exception as e:
        logger.debug("读取兜底事件签名失败（按无事件处理）: %s", e)
        return None


def pending_count(day: str, vshards: Iterable[int],
                  phones: Iterable[str] | None = None) -> int | None:
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


def open_count(day: str) -> int | None:
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


def day_counts(day: str) -> dict[str, int] | None:
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
    out: dict[str, int] = dict.fromkeys(STATES, 0)
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


def latest_day() -> str | None:
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


def owners_for_day(day: str) -> dict[str, str]:
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


def run_at_by_phone(day: str) -> dict[str, str] | None:
    """当日 `phone -> run_at` 映射——展示层读**执行计划时刻**的唯一事实源。

    `run_at` 就是执行体 `claim_batch` 的领取依据，故读它即读执行事实。计划落库后
    冻结（`write_plan` 用 `INSERT OR IGNORE` 不覆盖既有行），未了结行的它即当前计划
    时刻；重试/重签会改写它（`requeue_task` / `reclaim_tasks`），此时读到的就是重试时刻。

    只取 `vshard >= 0` 的真实计划行：v18 平移 / v20 补账的 `vshard=-1` 标记行不是任何
    分片集的计划，`claim_batch` 也永不由它领取，读它会把补账痕迹当成计划时刻。
    **一次取全**：账号列表可几百行，逐账号查会把一次列表请求变成几百次查询。

    **读不通回 `None`（哨兵）+ warning**：折成空映射会让展示把"读不出来"当成"今日
    无计划"，页面给每个账号都画上「待生成」，而实际计划已存在。调用方见到 `None`
    必须走同一条「待生成」降级，**绝不回退旧的派生算法**（显示不得有两套算法）。
    """
    sql = "SELECT phone, run_at FROM sign_tasks WHERE day=? AND vshard >= 0"
    try:
        conn, lock = _queue_conn()
        with lock:
            rows = conn.execute(sql, (day,)).fetchall()
        return {r["phone"]: r["run_at"] for r in rows}
    except Exception as e:
        logger.warning("读取当日计划时刻失败（读不通，不等于今日无计划）: %s", e)
        return None


def row_owners(day: str, phones: Iterable[str] | None = None) -> dict[str, tuple[str, str]] | None:
    """当日任务的 `{phone: (state, owner)}`——**轮末"本执行体未执行"判因的事实源**。

    判因怎么用（见 `executor_v3._mark_unreached`）：行已不是 `pending`（或归属写的是
    别人）⇒ 这件活已被别的执行体领走或了结，本执行体没领到**不等于签到失败**；行仍
    `pending` 且归属是本执行体（或无人）⇒ 没人接手，那才是真问题。两件事必须分列：
    2026-10-07 生产把"被兜底领走"报成 51/33 个失败，带的事件表里零记录。

    `owner` 一列同时是**计划归属**与**当前持有者**（`claim_batch` 领取时就地改写，见其
    docstring），所以"这件活派给谁"只有这一处记录。判因**同时读 `state` 与 `owner`**：
    只看 `state` 分不出"仍待领但派给了别人"与"仍待领且没人管"。

    `phones=None` 取该业务日全部行；给了就只取这些账号（**必须一次取全**：轮末判因的
    账号可能上百个，逐账号查会把一次收尾变成上百次查询），按 `SQL_VAR_CHUNK` 分块拼
    `IN`。只回原串，**不解析角色、不脱敏**——角色与脱敏是展示层的事。

    **读不通回 `None`（哨兵）+ warning**（与 `pending_count` 同口径）：
    折成空映射会让判因把"读不出来"当成"没有行"——被领走的账号于是被认成"没人接手"，
    方向不可控。调用方按"判因不可得"处置（见 `executor_v3._mark_unreached`）。
    """
    phones = None if phones is None else tuple(phones)
    if phones is not None and not phones:
        return {}
    sql = ("SELECT phone, state, owner FROM sign_tasks WHERE day=?"
           "{} ORDER BY phone")
    try:
        conn, lock = _queue_conn()
        out: dict[str, tuple[str, str]] = {}
        with lock:
            chunks: list[Any] = [()] if phones is None else list(_chunks(phones))
            for chunk in chunks:
                tail = (" AND phone IN (%s)" % ",".join("?" for _ in chunk)) if chunk else ""
                for r in conn.execute(sql.format(tail), (day, *chunk)).fetchall():
                    out[r["phone"]] = (r["state"], r["owner"])
        return out
    except Exception as e:
        logger.warning("读取当日任务归属与状态失败（读不通，不等于没有行）: %s", e)
        return None


def owners_since(days: int | None = None) -> list[str]:
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


def activity(day: str) -> list[dict[str, Any]]:
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
    out: dict[str, dict[str, int]] = {}
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
    result: list[dict[str, Any]] = []
    for owner, counts in out.items():
        claimed = counts.get(STATE_CLAIMED, 0) + counts.get(STATE_STOLEN, 0)
        done = counts.get(STATE_DONE, 0) + counts.get(STATE_SKIPPED, 0)
        failed = counts.get(STATE_PENDING, 0) + counts.get(STATE_FAILED, 0)
        item: dict[str, Any] = {"owner": owner, "claimed": claimed, "done": done, "failed": failed}
        item["total"] = sum(counts.values())
        result.append(item)
    return result
