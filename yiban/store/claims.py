# -*- coding: utf-8 -*-
"""数据层：签到领取池与账号级租约（v17，多执行体协调）。

多执行体的分工**不做静态分片**：按账号数平均切分时，"最慢的那一份"决定全天成败，
而签到窗口是硬的、学校放号时间不由我们定。改成**动态领取**——每个执行体在
`BEGIN IMMEDIATE` 语义下原子领取一个账号，领到才算它的活；执行体崩了，它持有的
账号靠**租约超时**被任何执行体接管（不需要人工介入，也不需要"分片数"这种记账）。

一行 = 一个账号在一个业务日的一条记录，其 `state` 就是"当日了结与否"的判据：

| state | 含义 | 是否了结 |
|-------|------|----------|
| `claimed` | 已被某执行体领取、尚未收尾（含执行中） | 否 |
| `done` | 收尾且结果正常（成功/已签到/今日无任务/无点位/窗口外跳过） | 是 |
| `failed` | 收尾但结果是失败（重试预算耗尽，本窗口内不再重试） | 是 |

**三条纪律**：

1. **只做协调，不改展示契约**：按日状态文件（`sign-state-*.json`）的 JSON 结构与
   写入时机不动——它是日历/状态展示的事实源，本表只回答"谁领了、了结没有"。
2. **写入带 owner 条件（CAS）**：租约被接管后，被接管的旧执行体写不进去，
   避免"两个执行体都以为自己签成功了"。
3. **降级不阻断**：表未落地（迁移被延后）或库抖动时，`try_claim` 返回 `True`
   （按"没人在抢"处理）而不是让签到停摆——单执行体形态下这个表可有可无，
   多执行体形态下它必须可用（届时由启动期自检拦住）。

连接与进程内锁取自 `scripts/db.py`；db.py 把本模块公开名全部再导出。
"""
import datetime
import logging
import os
import socket

from yiban import clock

logger = logging.getLogger("yiban.store.claims")

#: 租约时长（秒）：超过它没续租的记录可被任何执行体接管。
#: 合法单账号耗时上限 = 登录 + 签到 + 重试间隔（数十秒级），900s 是其数倍；
#: 取太短会把"正在重试的慢账号"误判为死执行体而重复登录（同一账号两次登录
#: 会加速触发易班侧风控），取太长会让崩溃后的账号等到窗口结束都没人接。
LEASE_SECONDS = 900

#: 保留期（天）：只用于运维追溯与"昨日的了结情况"，展示口径不读它。
RETENTION_DAYS = 14

STATE_CLAIMED = "claimed"
STATE_DONE = "done"
STATE_FAILED = "failed"
SETTLED_STATES = (STATE_DONE, STATE_FAILED)


def _integrity_errors():
    """唯一键冲突类异常（延迟取 sqlite3，便于本模块零依赖导入）。"""
    import sqlite3
    return sqlite3.IntegrityError


def new_owner(prefix=""):
    """执行体标识：`主机:进程:启动时刻`——重启后必然变化，便于识别"上一轮的持有者"。"""
    return f"{prefix}{socket.gethostname()}:{os.getpid()}:{datetime.datetime.now():%H%M%S}"


def _now_str(now=None):
    return now or clock.ts()


def _utc_offset_str(seconds):
    """把"早于此刻即过期"换算成可比较的时间串（与库内时间串同口径：业务时间）。"""
    t = clock.now() - datetime.timedelta(seconds=seconds)
    return t.strftime("%Y-%m-%d %H:%M:%S")


def try_claim(phone, day, owner, lease_sec=LEASE_SECONDS, now=None, allow_settled=False):
    """原子领取一个账号。返回是否领到。

    领不到的情形：已被别的执行体领取且租约未过期，或当日已了结（除非
    `allow_settled=True`——手动指定账号、补签轮重跑等显式路径用）。

    实现是**单条 upsert**：并发下 SQLite 串行化写者，后到者的 WHERE 会看到
    先到者已提交的行，故"只可能有一个赢家"，不需要额外的锁表。
    """
    import db
    ts = _now_str(now)
    expired_before = _utc_offset_str(lease_sec)
    # 冲突分支的两种情形分开写清楚：
    #  ① 行已了结（done/failed）：**无人持有**，故租约条件不适用——只由 allow_settled 决定；
    #  ② 行在飞（claimed）：自己的可重入；他人的须租约已过期（<=：租约 0 秒即"立刻可接管"）。
    sql = (
        "INSERT INTO sign_claims (phone, day, owner, claimed_at, heartbeat_at, "
        "state, result, attempts) VALUES (?, ?, ?, ?, ?, ?, '', 0) "
        "ON CONFLICT(phone, day) DO UPDATE SET "
        "owner=excluded.owner, claimed_at=excluded.claimed_at, "
        "heartbeat_at=excluded.heartbeat_at, state=excluded.state, "
        "attempts=sign_claims.attempts + 1 "
        "WHERE (sign_claims.state != ? AND ?)"
        "   OR (sign_claims.state = ? "
        "       AND (sign_claims.owner = excluded.owner "
        "            OR sign_claims.heartbeat_at <= ?))"
    )
    try:
        conn = db.get_conn()
        with db._conn_lock:
            cur = conn.execute(
                sql, (phone, day, owner, ts, ts, STATE_CLAIMED,
                      STATE_CLAIMED, 1 if allow_settled else 0,
                      STATE_CLAIMED, expired_before),
            )
            conn.commit()
            return cur.rowcount == 1
    except _integrity_errors() as e:
        # 唯一键冲突 = 有别人刚领到（极端时序下 upsert 之外的可能路径）。
        # 此时**必须**答"没领到"：答 True 会让两个执行体同时登录同一账号，
        # 那是本设计的第一红线（重复登录会加速触发易班侧风控）。
        logger.debug("领取竞争失败（他人已领）: %s", e)
        return False
    except Exception as e:
        # 表未落地/库锁/磁盘错误：协调能力不可用。按"没人在抢"处理——
        # 单执行体形态下这个表可有可无，不能因为协调表坏了就让签到停摆；
        # 多执行体形态由启动期自检拦住（届时它必须可用）。
        logger.warning("领取签到账号失败（按可执行处理）: %s", e)
        return True


def touch(phone, day, owner, lease_sec=LEASE_SECONDS, now=None):
    """续租（只续自己的）。返回是否续上（被接管/已了结时为 False）。"""
    import db
    ts = _now_str(now)
    try:
        conn = db.get_conn()
        with db._conn_lock:
            cur = conn.execute(
                "UPDATE sign_claims SET heartbeat_at=? "
                "WHERE phone=? AND day=? AND owner=? AND state=?",
                (ts, phone, day, owner, STATE_CLAIMED),
            )
            conn.commit()
            return cur.rowcount == 1
    except Exception as e:
        logger.debug("续租失败（不影响签到）: %s", e)
        return False


def settle(phone, day, owner, state=STATE_DONE, result=""):
    """收尾：把领取记录置为终态。返回是否写成功（被接管时为 False）。

    `result` 只存**摘要**（截断），且调用方须先脱敏——本表可能被运维查询导出。
    """
    import db
    if state not in SETTLED_STATES:
        raise ValueError(f"非法终态: {state!r}")
    try:
        conn = db.get_conn()
        with db._conn_lock:
            cur = conn.execute(
                "UPDATE sign_claims SET state=?, result=?, heartbeat_at=? "
                "WHERE phone=? AND day=? AND owner=?",
                (state, (result or "")[:200], clock.ts(), phone, day, owner),
            )
            conn.commit()
            return cur.rowcount == 1
    except Exception as e:
        logger.warning("收尾签到记录失败（不影响签到结果）: %s", e)
        return False


def states_for_day(day):
    """当日已建记录的 `phone -> state` 映射（未建的账号不在映射里 = 未领取）。"""
    import db
    try:
        with db._conn_lock:
            rows = db.get_conn().execute(
                "SELECT phone, state FROM sign_claims WHERE day=?", (day,)
            ).fetchall()
        return {r["phone"]: r["state"] for r in rows}
    except Exception as e:
        logger.debug("读取当日领取状态失败（按空处理）: %s", e)
        return {}


def in_flight_phones(day, lease_sec=LEASE_SECONDS):
    """当日仍在飞（`claimed` 且租约未过期）的账号——诊断"谁卡住了"用。"""
    import db
    expired_before = _utc_offset_str(lease_sec)
    try:
        with db._conn_lock:
            rows = db.get_conn().execute(
                # 与 try_claim 的接管判据同界：心跳 <= now-lease 即"租约已过期"，
                # 故"在飞"是严格大于（lease_sec=0 时不应有任何在飞记录）
                "SELECT phone, owner, claimed_at, heartbeat_at FROM sign_claims "
                "WHERE day=? AND state=? AND heartbeat_at > ?",
                (day, STATE_CLAIMED, expired_before),
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.debug("读取在飞账号失败（按空处理）: %s", e)
        return []


def stats(day):
    """当日各状态计数——供设置页/CLI 展示"了结进度"。"""
    import db
    try:
        with db._conn_lock:
            rows = db.get_conn().execute(
                "SELECT state, COUNT(*) AS n FROM sign_claims WHERE day=? GROUP BY state",
                (day,),
            ).fetchall()
        out = {STATE_CLAIMED: 0, STATE_DONE: 0, STATE_FAILED: 0}
        for r in rows:
            out[r["state"]] = r["n"]
        out["settled"] = out[STATE_DONE] + out[STATE_FAILED]
        out["total"] = out[STATE_CLAIMED] + out["settled"]
        return out
    except Exception as e:
        logger.debug("读取签到进度失败（按空处理）: %s", e)
        return {"claimed": 0, "done": 0, "failed": 0, "settled": 0, "total": 0}


def purge(days=RETENTION_DAYS):
    """清理保留期外的记录（按业务日字符串比较）。失败仅告警，返回删除行数。"""
    import db
    cutoff = (clock.now() - datetime.timedelta(days=days)).strftime("%Y-%m-%d")
    try:
        conn = db.get_conn()
        with db._conn_lock:
            cur = conn.execute("DELETE FROM sign_claims WHERE day < ?", (cutoff,))
            conn.commit()
            return cur.rowcount
    except Exception as e:
        logger.warning("清理签到领取记录失败: %s", e)
        return 0
