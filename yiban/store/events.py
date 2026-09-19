# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""事件与统计域：sign_events 表的写入、查询与保留期清理，以及 audit_logs 上的暂停冷却查询。

**功能**
- 写入：`add_sign_event`（单条，失败仅告警）与 `add_sign_events_batch`（单事务批量，
  供 signin 一轮结束后落库）；
- 查询与统计：`sign_event_stats`（按天 × 状态聚合）、`sign_events_by_phone`（单账号
  时间线）、`sign_events_since`（实时事件流）、`probe_events_on` / `sign_events_on`
  （指定日期的探针 / 签到事件）、`sign_events_recent_date`（窗口内最近有数据的日期）；
- 暂停冷却：`last_pause_at` / `pause_count_since` 查 audit_logs 表里
  action='my_account_pause' 的最近时间与窗口计数；
- 保留期：`SIGN_EVENTS_RETENTION_DAYS` 与 `_event_cleanup`（删除超期 sign_events，
  同事务连带清理超期 verify_jobs 并写留痕）。

**归属**
web 日志页与 `/api/admin/sign-events`、signin 执行体（runner 批量落库、probe 探针）
共用的一张事件表。sign_events 同时承载真实签到（stage="sign"）与健康探针
（stage="probe"），凡以「签到口径」消费的调用方必须显式按 stage 过滤，否则探针的
成功/失败会被计入签到成功率。建表与索引在迁移域（`yiban/store/migrations.py`），本模块只读写。

暂停冷却查询按**表归属**（audit_logs）落在本模块，而不是随「自选时间片」域走：
它按 username 计价（审计 target 为脱敏手机号，故按操作用户名关联；多管理员共享账号
各自独立计价——暂停/恢复冷却仅防噪音，绕过危害极小，可接受），与 sign_events 无表级
关系。与之对称的 time_pref 保存冷却按审计 target=hash_phone(phone) 按被选账号计价，
落在 `yiban/store/time_prefs.py`。

**复用**
`yiban.store.db` 把本模块的事件函数与常量按原样再导出，`db.add_sign_event()` /
`db.sign_event_stats()` / `db._event_cleanup(...)` 一类调用与身份断言不变；本模块并入的
暂停冷却两名（`last_pause_at` / `pause_count_since`）在门面上走**模块级读写转发**，
`db.last_pause_at()` 调用与 `db.<名字> = 替身` 打桩都落到这里。

**通信**
连接、进程内锁与写事务入口（`_conn_lock` / `get_conn` / `_begin_immediate`），以及留在
db.py 的时钟守卫与清理留痕写入侧（`_clock_jump_guard` / `_table_min_max` /
`_record_purge_event`）一律经 `_facade()` 按属性取。必须按属性取而非模块级 from-import：
`mock.patch.object(db, "get_conn"/"_conn_lock", …)` 一类打桩要求打桩点落在 db 门面上，
直接调本模块同名函数会让打桩静默失效（测试仍绿，但打桩点不再是它以为的那一个）。
"""
import contextlib
import datetime
import logging

from yiban import clock

logger = logging.getLogger("yiban.store.events")


def _facade():
    """db 门面（函数内延迟导入，避免与 `yiban.store.db` 形成导入环）。

    打桩可见性见模块说明：必须按**属性**取而不是模块级 from-import，`db.<名字> = 替身`
    才会在函数体里生效。
    """
    from yiban.store import db
    return db


# sign_events 保留期（天）：超期行由 _event_cleanup 清理。
SIGN_EVENTS_RETENTION_DAYS = 180

# 单条与批量写入共用同一列序：分列两处时，只改一侧的列序会让另一条写入路径静默错位。
_INSERT_SIGN_EVENT_SQL = (
    "INSERT INTO sign_events (ts, phone, status, message, stage, attempt, "
    "account_id, dur_sec, finished_at) VALUES (?,?,?,?,?,?,?,?,?)"
)


def _normalize_limit(limit, default):
    """把 limit 钳制到 1..1000；非法值回退到默认值。"""
    try:
        return max(1, min(int(limit), 1000))
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# 写入
# ---------------------------------------------------------------------------
def add_sign_event(ts, phone, status, message="", stage="", attempt=0,
                   account_id=None, dur_sec=None, finished_at=None):
    """写入签到事件；失败仅告警，不影响调用方。"""
    try:
        with _facade()._conn_lock:
            conn = _facade().get_conn()
            conn.execute(
                _INSERT_SIGN_EVENT_SQL,
                (ts, phone, status, message, stage, attempt,
                 account_id, dur_sec, finished_at),
            )
            conn.commit()
    except Exception as e:
        with contextlib.suppress(Exception):
            conn.rollback()
        logger.warning("写入 sign_events 失败: %s", e)


def add_sign_events_batch(rows):
    """批量写入签到事件（单事务）；失败仅告警，不影响调用方。

    rows 为 dict 列表，支持 add_sign_event 的全部字段。
    """
    with _facade()._conn_lock:
        try:
            conn = _facade().get_conn()
            _facade()._begin_immediate(conn)
            for r in rows:
                conn.execute(
                    _INSERT_SIGN_EVENT_SQL,
                    (
                        r.get("ts", ""),
                        r.get("phone", ""),
                        r.get("status", ""),
                        r.get("message", ""),
                        r.get("stage", ""),
                        r.get("attempt", 0),
                        r.get("account_id"),
                        r.get("dur_sec"),
                        r.get("finished_at"),
                    ),
                )
            conn.commit()
        except Exception as e:
            with contextlib.suppress(Exception):
                conn.rollback()
            logger.warning("批量写入 sign_events 失败: %s", e)


# ---------------------------------------------------------------------------
# 查询与统计
# ---------------------------------------------------------------------------
def sign_event_stats(days=30, stage=None):
    """按天统计签到事件数量/状态分布；失败返回空列表。

    stage 为可选过滤开关：sign_events 同时承载真实签到（stage="sign"）与健康探针
    （stage="probe"），不传时两者混算。需要「签到口径」的调用方必须显式传
    stage="sign"，否则探针的成功/失败会被计入签到成功率。
    """
    try:
        with _facade()._conn_lock:
            conn = _facade().get_conn()
            cutoff = (clock.now() - datetime.timedelta(days=days)).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            sql = (
                "SELECT substr(ts, 1, 10) AS day, status, COUNT(*) AS cnt "
                "FROM sign_events WHERE ts >= ?"
            )
            params = [cutoff]
            if stage:
                sql += " AND stage = ?"
                params.append(stage)
            sql += " GROUP BY day, status ORDER BY day"
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]
    except Exception as e:
        logger.warning("sign_events 统计失败: %s", e)
        return []


def sign_events_by_phone(phone, days=30):
    """单账号历史表现：按手机号返回时间线事件列表。"""
    try:
        with _facade()._conn_lock:
            conn = _facade().get_conn()
            cutoff = (clock.now() - datetime.timedelta(days=days)).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            rows = conn.execute(
                "SELECT id, ts, phone, status, message, stage, attempt, "
                "account_id, dur_sec, finished_at "
                "FROM sign_events WHERE phone=? AND ts >= ? ORDER BY ts",
                (phone, cutoff),
            ).fetchall()
            return [dict(r) for r in rows]
    except Exception as e:
        logger.warning("sign_events_by_phone 失败: %s", e)
        return []


def sign_events_since(since_ts, phone=None, limit=100):
    """实时事件流：返回 since_ts 之后的事件，可选按手机号过滤。"""
    try:
        with _facade()._conn_lock:
            conn = _facade().get_conn()
            sql = (
                "SELECT id, ts, phone, status, message, stage, attempt, "
                "account_id, dur_sec, finished_at FROM sign_events WHERE ts >= ?"
            )
            params = [since_ts]
            if phone:
                sql += " AND phone=?"
                params.append(phone)
            sql += " ORDER BY ts LIMIT ?"
            params.append(_normalize_limit(limit, 100))
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]
    except Exception as e:
        logger.warning("sign_events_since 失败: %s", e)
        return []


def probe_events_on(date_str, limit=100):
    """指定日期（YYYY-MM-DD）的健康探测事件（stage="probe"，按时间正序）。

    供 Web 日志页展示探针结构化记录。
    查询失败返回空列表，不影响调用方。
    """
    try:
        with _facade()._conn_lock:
            conn = _facade().get_conn()
            start = f"{date_str} 00:00:00"
            end = f"{date_str} 23:59:59"
            sql = (
                "SELECT id, ts, phone, status, message, attempt "
                "FROM sign_events WHERE stage='probe' AND ts BETWEEN ? AND ? "
                "ORDER BY ts LIMIT ?"
            )
            params = [start, end, _normalize_limit(limit, 100)]
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]
    except Exception as e:
        logger.warning("probe_events_on 失败: %s", e)
        return []


def sign_events_on(date_str, limit=100):
    """指定日期（YYYY-MM-DD）的签到事件（stage="sign"，按时间正序）。

    随 /api/logs 附带当日签到事件（与 probe_events_on 同口径：手机号脱敏、
    条数封顶由调用方处理）。
    查询失败返回空列表，不影响调用方。
    """
    try:
        with _facade()._conn_lock:
            conn = _facade().get_conn()
            start = f"{date_str} 00:00:00"
            end = f"{date_str} 23:59:59"
            sql = (
                "SELECT id, ts, phone, status, message, attempt "
                "FROM sign_events WHERE stage='sign' AND ts BETWEEN ? AND ? "
                "ORDER BY ts LIMIT ?"
            )
            params = [start, end, _normalize_limit(limit, 100)]
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]
    except Exception as e:
        logger.warning("sign_events_on 失败: %s", e)
        return []


def sign_events_recent_date(stage, max_days=30):
    """最近有指定 stage 事件的日期（YYYY-MM-DD，窗口内无则空串）。

    供日志页空态给出「查看最近有数据日期」的一键入口：某标签当前日期无事件时，
    用本函数找到该标签最近有事件的日期（stage=probe/sign 分别对应探针/签到事件）。
    查询失败返回空串，不影响调用方。
    """
    stage = "probe" if stage == "probe" else "sign"
    try:
        with _facade()._conn_lock:
            conn = _facade().get_conn()
            cutoff = (clock.now() - datetime.timedelta(days=max_days)).strftime(
                "%Y-%m-%d 00:00:00"
            )
            row = conn.execute(
                "SELECT MAX(ts) AS m FROM sign_events WHERE stage=? AND ts >= ?",
                (stage, cutoff),
            ).fetchone()
            if row and row["m"]:
                return str(row["m"])[:10]
    except Exception as e:
        logger.warning("sign_events_recent_date 失败: %s", e)
    return ""


# ---------------------------------------------------------------------------
# 暂停冷却（audit_logs 表，按表归属落在本模块）
# ---------------------------------------------------------------------------
def last_pause_at(username):
    """指定用户最近一次暂停签到时间（暂停冷却判定用；恢复不计，按用户计价）。

    审计 target 为脱敏手机号，故按 username 关联；多管理员共享账号各自独立计价
    （暂停/恢复冷却仅防噪音，绕过危害极小，可接受）。
    """
    try:
        db = _facade()
        with db._conn_lock:
            conn = db.get_conn()
            row = conn.execute(
                "SELECT ts FROM audit_logs WHERE username=? AND action='my_account_pause' "
                "ORDER BY id DESC LIMIT 1",
                (username or "",),
            ).fetchone()
            return row["ts"] if row else None
    except Exception as e:
        raise RuntimeError(f"查询暂停时间失败: {e}") from e


def pause_count_since(username, since_ts):
    """指定用户在 since_ts 之后的暂停次数（弹性冷却高频判定用）。"""
    try:
        db = _facade()
        with db._conn_lock:
            conn = db.get_conn()
            row = conn.execute(
                "SELECT COUNT(*) FROM audit_logs WHERE username=? "
                "AND action='my_account_pause' AND ts >= ?",
                (username or "", since_ts),
            ).fetchone()
            return row[0] if row else 0
    except Exception as e:
        raise RuntimeError(f"统计暂停次数失败: {e}") from e


# ---------------------------------------------------------------------------
# 保留期清理
# ---------------------------------------------------------------------------
def _event_cleanup(conn):
    """清理可视化表超期数据；失败仅告警。

    接入时钟跳变守卫（同 _audit_cleanup）——sign_events 等表是
    取证数据源，时钟跳变不应放大清理窗口。
    """
    try:
        ok, note = _facade()._clock_jump_guard(conn, "event_cleanup_clock")
        if not ok:
            logger.error("%s", note)
            with contextlib.suppress(Exception):
                conn.rollback()
            return
        now = clock.now()
        sign_cutoff = (now - datetime.timedelta(days=SIGN_EVENTS_RETENTION_DAYS)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        before = _facade()._table_min_max(conn, "sign_events")
        cur = conn.execute("DELETE FROM sign_events WHERE ts < ?", (sign_cutoff,))
        _facade()._record_purge_event(
            conn, "sign_events", "event_cleanup", sign_cutoff, cur.rowcount or 0,
            before, _facade()._table_min_max(conn, "sign_events"),
        )
        job_cutoff = (now - datetime.timedelta(days=_facade().VERIFY_JOB_RETENTION_DAYS)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        before = _facade()._table_min_max(conn, "verify_jobs")
        cur = conn.execute("DELETE FROM verify_jobs WHERE created_at < ?", (job_cutoff,))
        _facade()._record_purge_event(
            conn, "verify_jobs", "event_cleanup", job_cutoff, cur.rowcount or 0,
            before, _facade()._table_min_max(conn, "verify_jobs"),
        )
        conn.commit()
    except Exception as e:
        with contextlib.suppress(Exception):
            conn.rollback()
        logger.warning("清理可视化表失败: %s", e)
