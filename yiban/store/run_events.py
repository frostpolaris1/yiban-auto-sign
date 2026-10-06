# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""内核进度事件域：`run_events` 表的写入与保留期清理（N2a 打点层）。

**功能**
- 写入：`report`（单条）与 `report_many`（单事务批量）——执行体与编排层共用的
  **唯一 reporter 接口**，调用方不拼 SQL；
- 保留期：`purge`（删除超期事件行，同事务写清理留痕）。

**节点值域（本模块是唯一定义点）**
`NODE_CLAIM` 领取 / `NODE_START` 开始 / `NODE_SUCCESS` 成功 / `NODE_FAIL` 失败 /
`NODE_PAUSE` 暂停 / `NODE_FINALIZE` 收尾。写未知节点被拒并告警：节点值是下游
（SSE 端点、日志页）的契约面，落一个没人认得的取值比丢一条事件更坏。

**归属**
`yiban/engine/executor_v3.py`（执行体节点）与 `yiban/engine/runner.py`（编排节点）
共用的一张**观测表**。本表不参与签到正确性：任何一行写不进去都不改变签到结论。
建表与索引在迁移域（`yiban/store/migrations.py` 的 v21），本模块只读写；
保留期清理由 `yiban/store/cleanup.py` 的每日编排调用（`purge_run_events`）。

**收尾有两层（`finalize` 的 message 前缀是它们的判别面）**
- `执行体会话收尾：`：一次执行体会话正常结束（`run_executor_v3` 正常返回路径）；
- `轮次收尾：`：一次轮次结束、退出码与汇总已成定局（`runner.main` 汇总处）。

单执行体路径下这两层在同一个进程里前后各落一行，`(业务日, 执行体, 节点)` 因此
相同。这不是重复计数：两层是不同的事实（会话可以在一轮里出现多次，例如兜底常驻
的反复扫描）。消费方按 message 前缀取自己要的那一层，**不得对 `finalize` 计数求和**。

**为什么是独立表而不是并入 sign_events**
`sign_events` 按**尝试**落行（一次登录一条，含 dur_sec / 结果文本），是签到事实；
本表按**进度节点**落行（领取/开始等不发起请求的节点也在内），是运行进度。两者
基数与消费者不同：签到报表按账号去重统计 `sign_events`，进度流按 id 增量尾读
本表。并表会让"成功率"口径混入进度行。

**复用**
`report` / `report_many` 是仅有的两个写入入口（后者只做"同一时刻的一批"，例如
一次领取返回的整批行）；节点常量供调用方与下游消费者对齐取值。写入失败**只告警
不抛**——打点是观测面，不是业务面。行随事件即时落库（不做攒批）：本表的下游是
"实时进度"，攒批会让进度流滞后到一轮结束。批量入口存在的理由是**取一次写锁**：
领取发生在 asyncio 事件循环线程里，逐行提交会把"一行一次锁等待"叠加成整条通道的
停顿（连接层的写锁等待上限是秒级）。

**通信**
连接、锁、时钟跳变守卫与留痕写入（`get_conn` / `_conn_lock` / `_begin_immediate` /
`_clock_jump_guard` / `_table_min_max` / `_record_purge_event`）一律经 `_facade()`
按属性取：`mock.patch.object(db, "get_conn", …)` 一类打桩要求打桩点落在 db 门面上，
直接调本模块同名函数会让打桩静默失效（测试仍绿，但打桩点不再是它以为的那一个）。
调用谁：`yiban.clock`。
谁调用：`yiban/engine/executor_v3.py`、`yiban/engine/runner.py`、
`yiban/store/cleanup.py`（每日清理）。
"""
import contextlib
import datetime
import logging

from yiban import clock
from yiban.masking import sanitize_text as _sanitize_text

logger = logging.getLogger("yiban.store.run_events")

# 节点值域：唯一事实源。改名即改契约，下游读取点必须同批对齐。
NODE_CLAIM = "claim"
NODE_START = "start"
NODE_SUCCESS = "success"
NODE_FAIL = "fail"
NODE_PAUSE = "pause"
NODE_FINALIZE = "finalize"
NODES = (NODE_CLAIM, NODE_START, NODE_SUCCESS, NODE_FAIL, NODE_PAUSE, NODE_FINALIZE)

#: 事件行保留期（天）。进度事件只服务"实时进度 + 近期排障"，与签到事实
#: （sign_events 180 天）不同档；取 14 与领取台账（claims.RETENTION_DAYS）同量级。
RETENTION_DAYS = 14

# 单条与批量共用同一列序：分列两处时，只改一侧的列序会让另一条写入路径静默错位。
_INSERT_SQL = (
    "INSERT INTO run_events (ts, day, node, executor, phone, message) "
    "VALUES (?,?,?,?,?,?)"
)

# 写入失败时的定位线索：INSERT 报 "no such table/column" 时，后果是本表在保留期内
# **永久零写入**（每次写都失败、失败只发告警），而告警原文里没有升级链的线索。
_ARTIFACT_HINT = (
    "缺失产物归属：表 run_events=v21（核对 yiban/store/migrations.py 的"
    "产物登记表与迁移完成记录）"
)


def _facade():
    """db 门面（函数内延迟导入，避免与 `yiban.store.db` 形成导入环）。

    打桩可见性见模块说明：必须按**属性**取而不是模块级 from-import。
    """
    from yiban.store import db
    return db


def _row(node, day, executor, phone="", message=""):
    """→ 一行事件元组；节点值不在值域内时返回 None（调用方告警并丢弃）。

    `message` 在这里过脱敏与转义再截断——**净化点收在本模块**，与 `sign_events` 的
    写入口径用同一份原语。为什么不放在各调用点：`message` 常是易班服务端返回的原文
    （`attempts.attempt_signin` 的结果），可能带手机号、token、换行与控制字符；表里
    落了原文就有一条通往"日志/SSE 页面回显"的泄漏路径。逐调用点各净化一遍是
    "漏一个就泄漏"的形状，收在唯一写入点才没有下一个漏点。
    """
    if node not in NODES:
        logger.warning("进度事件节点值非法，已丢弃本条（不在值域 %s 内）: %r",
                       "|".join(NODES), node)
        return None
    ts = clock.now().strftime("%Y-%m-%d %H:%M:%S")
    return (ts, str(day or ""), node, str(executor or ""), str(phone or ""),
            _sanitize_text(str(message or ""))[:200])


def report(node, *, day, executor, phone="", message=""):
    """写一条进度事件；→ 是否落库（False = 节点值非法或写失败）。

    失败仅告警，不影响调用方：事件写入不得拖累签到主流程（既有契约，
    与 `sign_events` 的写入口径一致）。级别保持 warning，文本必须能定位。
    """
    return report_many([{"node": node, "day": day, "executor": executor,
                         "phone": phone, "message": message}]) == 1


def report_many(events):
    """单事务写入一批进度事件；→ 实际落库行数（0 = 全被丢弃或整批失败）。

    `events` 为 dict 列表，键与 `report` 的形参同名。逐行过 `_row` 做节点值域判定，
    再 `executemany` + 一次 commit——**一次写锁**，不在事件循环线程里逐行抢锁。
    失败仅告警，不影响调用方。

    **构造行也在保护区内**（逐条 try）：`_row` 会取业务钟、过脱敏原语、读条目的键
    ——三件事都可能抛（`clock.now()` 异常、净化原语异常、条目不是映射时 `e.get`
    抛 `AttributeError`）。调用方（`executor_v3._report` / runner）都不包 try 也不看
    返回值，它们依赖的是本函数"绝不抛"的承诺；把构造放在 try 之外，一次构造异常就会
    从 asyncio 任务里逃到签到主流程，与"观测面不得改变签到结论"的契约相反。
    """
    rows = []
    for e in events:
        try:
            row = _row(e.get("node"), e.get("day"), e.get("executor"),
                       e.get("phone", ""), e.get("message", ""))
        except Exception as exc:      # 条目不是映射 / 取时钟失败 / 净化失败
            logger.warning("进度事件条目不可用，已丢弃本条: [%s: %s]",
                           type(exc).__name__, exc)
            continue
        if row is not None:
            rows.append(row)
    if not rows:
        return 0
    conn = None
    try:
        db = _facade()
        with db._conn_lock:
            conn = db.get_conn()
            conn.executemany(_INSERT_SQL, rows)
            conn.commit()
        return len(rows)
    except Exception as e:
        if conn is not None:
            with contextlib.suppress(Exception):
                conn.rollback()
        # 出声但不抛：进度事件不得拖累签到主流程；本批事件在保留期内没有补写路径，
        # 故文本要能直接定位到"哪张表、哪一档迁移"。
        logger.warning("写入 run_events 失败（本批 %d 条未落库，%d 天保留期内不补写）: "
                       "[%s: %s]——%s", len(rows), RETENTION_DAYS,
                       type(e).__name__, e, _ARTIFACT_HINT)
        return 0


def purge(days=RETENTION_DAYS):
    """清理保留期外的事件行（按业务日字符串比较）。失败仅告警，返回删除行数。

    本表逐日累积，不清理会无限增长。**复用与 `claims.purge` / `queue_store.purge`
    同一套时钟跳变守卫**（`db._clock_jump_guard`，各表各一份参照点）：系统时间被
    拨快时按日比较的 cutoff 会跳到未来，"保留期外"判据会把最近几天全算超期——
    整删当日行等于把正在看的进度流清空。跳变只跳本轮：守卫在越界路径上也推进
    参照点，下一轮即恢复。
    """
    db = _facade()
    conn = None
    cutoff = (clock.now() - datetime.timedelta(days=days)).strftime("%Y-%m-%d")
    try:
        with db._conn_lock:
            conn = db.get_conn()
            ok, note = db._clock_jump_guard(conn, "purge_run_events_clock")
            if not ok:
                logger.error("%s", note)
                conn.rollback()   # 越界路径已在守卫内提交参照点；此处只是解除写锁
                return 0
            before = db._table_min_max(conn, "run_events")
            cur = conn.execute("DELETE FROM run_events WHERE day < ?", (cutoff,))
            deleted = cur.rowcount or 0
            db._record_purge_event(conn, "run_events", "run_events_cleanup", cutoff,
                                   deleted, before, db._table_min_max(conn, "run_events"))
            conn.commit()
            return deleted
    except Exception as e:
        if conn is not None:
            with contextlib.suppress(Exception):
                conn.rollback()
        logger.warning("清理进度事件失败（不影响其他清理）: %s", e)
        return 0
