# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""事件与统计域：sign_events 表的写入、查询与保留期清理，以及 audit_logs 上的暂停冷却查询。

**功能**
- 写入：`add_sign_event`（单条，失败仅告警）与 `add_sign_events_batch`（单事务批量，
  供 signin 一轮结束后落库）；
- 查询与统计：`sign_event_stats`（按天 × 状态聚合，**事件表口径**）、`sign_events_by_phone`
  （单账号时间线）、`sign_events_since`（实时事件流）、`probe_events_on` / `sign_events_on`
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
import math

from yiban.core import clock
from yiban.core.masking import mask_email

logger = logging.getLogger("yiban.store.events")


def _actor_forms(username):
    """审计 actor 列查询的**双形态**匹配集：原值 + 遮罩值。

    MF-49 actor 收口后新行的 `username` 是遮罩形态，历史行仍是明文——只按传入原值查
    会永远查不到新行，只按遮罩查会在升级边界丢历史行。两形态同查（`mask_email` 幂等，
    非邮箱标识两形态同值，`IN` 集自然去重），暂停冷却在升级前后都成立。
    """
    u = username or ""
    return [u, mask_email(u)]


def _facade():
    """db 门面（函数内延迟导入，避免与 `yiban.store.db` 形成导入环）。

    打桩可见性见模块说明：必须按**属性**取而不是模块级 from-import，`db.<名字> = 替身`
    才会在函数体里生效。
    """
    from yiban.adapters.persistence import db
    return db


# sign_events 保留期（天）：超期行由 _event_cleanup 清理。
SIGN_EVENTS_RETENTION_DAYS = 180

# 单条与批量写入共用同一列序：分列两处时，只改一侧的列序会让另一条写入路径静默错位。
# 条件 INSERT（工单 f7or）：sign 事件按 (账号, 业务日, status, attempt) 幂等——
# 同一事实被重扫/多执行体重发时只留第一行。探针（stage='probe'）是另一条观测通道，
# 有自己的 cadence，不参与去重。单语句 `INSERT … SELECT … WHERE NOT EXISTS` 在
# `_begin_immediate` 的写锁内执行，故"查重"与"写入"之间没有别的写者能插进来。
_INSERT_SIGN_EVENT_SQL = (
    "INSERT INTO sign_events (ts, phone, status, message, stage, attempt, "
    "account_id, dur_sec, finished_at) "
    "SELECT ?,?,?,?,?,?,?,?,? "
    "WHERE ? = 'probe' OR NOT EXISTS ("
    "SELECT 1 FROM sign_events "
    "WHERE phone = ? AND stage <> 'probe' AND status = ? AND attempt = ? "
    "AND substr(ts, 1, 10) = substr(?, 1, 10))"
)


def _event_params(ts, phone, status, message, stage, attempt,
                  account_id, dur_sec, finished_at):
    """条件 INSERT 的 14 个占位：9 个列值 + 去重判据（stage/phone/status/attempt/ts）。

    去重键与门（`tests/test_sign_events.py::sign_events_observation_gate`）同一口径：
    **账号 + 业务日（ts 前 10 位）+ status + attempt**；stage 只用来把探针排除在外。
    """
    attempt = int(attempt or 0)
    stage = str(stage or "")
    return (ts, phone, status, message, stage, attempt,
            account_id, dur_sec, finished_at,
            stage, phone, status, attempt, ts)

# 两条写入路径共用的告警指针：本表的产物各由哪一档迁移建出。
# 为什么要点名到档：INSERT 报 "no such column" 时，后果是这张表在保留期内**永久零写入**
# （每次写都失败、失败只发告警），而告警原文里没有升级链的线索。给出院名与登记表位置，
# 运维才能一步判断"版本说过了这档、schema 里却没有它的产物"（ba-p01-02 的下游形状）。
_SIGN_EVENTS_ARTIFACT_HINT = (
    "缺失产物归属：表 sign_events=v4、列 account_id/dur_sec/finished_at=v6"
    "（核对 yiban/store/migrations.py 的 _ARTIFACTS 与 schema_migrations 记录）"
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
def _accountless_sign_row(stage, phone):
    """→ 该行是不是"无账号的 sign 行"（本表必须拒绝的形状）。

    `sign_events` 只装**账号级事实**。2026-10-18 之前的摘要行用 `phone=''` 的带内
    哨兵表示"这行不关于账号"，于是每个读者都得记得过滤。摘要行已改住 `run_events`
    （观测表，无账号行是正常形态），本表在写入口直接拒绝空账号，负向不变式从
    "靠读者守"变成"写不进来"。探针（stage='probe'）不受影响——它总是按账号探。
    """
    return str(stage or "") != "probe" and not str(phone or "")


def add_sign_event(ts, phone, status, message="", stage="", attempt=0,
                   account_id=None, dur_sec=None, finished_at=None):
    """写入签到事件；失败仅告警，不影响调用方。

    同一 (账号, 业务日, status, attempt) 重复写入只保留第一行（条件 INSERT，工单
    f7or）。"第一行赢"与 `INSERT OR IGNORE` 同义：先到者的 message/ts 留档。
    """
    if _accountless_sign_row(stage, phone):
        logger.warning("拒绝写入无账号的 sign_events 行（本表只装账号级事实；"
                       "摘要/观测行请写 run_events）")
        return
    try:
        with _facade()._conn_lock:
            conn = _facade().get_conn()
            # BEGIN IMMEDIATE：查重与写入同处一把写锁，跨进程重发也只会留下一行。
            _facade()._begin_immediate(conn)
            conn.execute(
                _INSERT_SIGN_EVENT_SQL,
                _event_params(ts, phone, status, message, stage, attempt,
                              account_id, dur_sec, finished_at),
            )
            conn.commit()
    except Exception as e:
        with contextlib.suppress(Exception):
            conn.rollback()
        # 出声但不抛：事件写入不得拖累签到主流程（既有契约）。级别保持 warning，
        # 文本必须能定位——本条事件已经丢了，180 天保留期内没有任何补写路径。
        logger.warning("写入 sign_events 失败（本条事件已丢弃，%d 天保留期内不补写）: "
                       "sign_events 表/列 [%s: %s]——%s",
                       SIGN_EVENTS_RETENTION_DAYS, type(e).__name__, e,
                       _SIGN_EVENTS_ARTIFACT_HINT)


def add_sign_events_batch(rows):
    """批量写入签到事件（单事务）；失败仅告警，不影响调用方。

    rows 为 dict 列表，支持 add_sign_event 的全部字段。去重口径与单条一致；
    无账号的 sign 行逐条丢弃，落库前只记一条汇总告警（见 `_accountless_sign_row`）。
    """
    with _facade()._conn_lock:
        try:
            conn = _facade().get_conn()
            _facade()._begin_immediate(conn)
            dropped = 0
            for r in rows:
                stage = str(r.get("stage") or "")
                phone = r.get("phone", "")
                if _accountless_sign_row(stage, phone):
                    dropped += 1
                    continue
                conn.execute(
                    _INSERT_SIGN_EVENT_SQL,
                    _event_params(
                        r.get("ts", ""), phone, r.get("status", ""),
                        r.get("message", ""), stage, r.get("attempt"),
                        r.get("account_id"), r.get("dur_sec"),
                        r.get("finished_at"),
                    ),
                )
            conn.commit()
            if dropped:
                logger.warning("批量写入 sign_events：丢弃 %d 条无账号行"
                               "（摘要/观测行应写 run_events）", dropped)
        except Exception as e:
            with contextlib.suppress(Exception):
                conn.rollback()
            # 单事务批量：一条失败整批回滚 ⇒ 报出条数，运维才知道丢了几个事件。
            # 出声但不抛：签到主流程不得被事件写入拖累（既有契约）。
            logger.warning("批量写入 sign_events 失败（本批 %d 条全部未落库，%d 天保留期内"
                           "不补写）: sign_events 表/列 [%s: %s]——%s",
                           len(rows), SIGN_EVENTS_RETENTION_DAYS,
                           type(e).__name__, e, _SIGN_EVENTS_ARTIFACT_HINT)


# ---------------------------------------------------------------------------
# 查询与统计
# ---------------------------------------------------------------------------
def sign_event_stats(days=30, stage=None):
    """按天统计签到事件数量/状态分布；失败返回空列表。

    两个计数口径并存：
    - `cnt`：该日该状态的**账号数**（`COUNT(DISTINCT phone)`）。同一账号当日可有多行
      （attempt 递进的真重试），按行计数会把同一账号算多次、令报表虚增；
    - `row_cnt`：该日该状态的**原始行数**（`COUNT(*)`），保留"尝试次数"信息（重试本来
      就会多行，那是真事实）。

    两条语义边界（消费方须自行处理）：
    1. 各状态桶**分别**去重 ⇒ 同一天"先失败后成功"的账号会**同时出现在两个桶**，把各桶
       `cnt` 相加当"账号总数"仍会偏大；消费方不得对 `cnt` 跨维直加。本表是**事件表口径**，
       只答"过程"；看板「涉及多少账号 / 按日终态」的权威值来自终态载体（按日
       状态文件，见 `web/services/logs.py` 的 `accounts_terminal_summary`），
       不来自本表——用"每账号最后一条事件"模拟终态会被观测行污染（2026-10-16 的
       `pending` 洪水把 311 个账号刷成待签）。
    2. 事件**列表**仍会显示多行——那是各次真实尝试各自的事实；写入侧对同一
       `(phone, day, status, attempt)` 只写一次（条件 INSERT，工单 f7or），故多行
       必然对应不同的 attempt 或 status，不是同一事实的重复观测。

    stage 为可选过滤开关：sign_events 同时承载真实签到（stage="sign"）与健康探针
    （stage="probe"），不传时两者混算。需要「签到口径」的调用方必须显式传
    stage="sign"，否则探针的成功/失败会被计入签到成功率。

    **账号级事实是结构性保证（工单 3zr5）**：写入口拒绝无账号的 sign 行
    （`_accountless_sign_row`），摘要/观测行走 `run_events`。`phone <> ''` 保留为
    **历史行防御**：本批上线前的库可能仍有 `phone=''` 的摘要行（180 天保留期内），
    去掉过滤会让 2026-10-10 的 `pending` 桶污染（MF-55／81xt）在旧数据上复活。
    新写入已构造不出这种行，故该过滤对新数据是空操作。
    """
    try:
        with _facade()._conn_lock:
            conn = _facade().get_conn()
            cutoff = (clock.now() - datetime.timedelta(days=days)).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            sql = (
                "SELECT substr(ts, 1, 10) AS day, status, "
                "COUNT(DISTINCT phone) AS cnt, COUNT(*) AS row_cnt "
                "FROM sign_events WHERE ts >= ? AND phone <> ''"
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


def attempt_dur_quantile(pct=0.95, days=7, min_samples=20):
    """近 `days` 天真实签到尝试耗时的分位数（秒）；样本不足/查询失败返回 None。

    容量告警阈值的**实测输入**（MF-56③）：`capacity_accounts` 的单账号耗时此前
    只有配置缺省（1.87~3s 来自 mock 注入，登记表判"不是实测"），继承它的阈值算的
    是假设。`sign_events.dur_sec` 本就按"每次尝试（登录链+签到链）端到端"落盘
    （`round._emit_event`），取近几天成功/已签事件的 p95 即可——**不新增任何持久化
    键/表**，旧库/旧状态文件照常可读（列缺席的老行 dur_sec 为 NULL，天然被过滤）。
    样本 < min_samples 时不拿小样本冒充分位数，返回 None 让调用方回退配置值。
    """
    try:
        with _facade()._conn_lock:
            conn = _facade().get_conn()
            cutoff = (clock.now() - datetime.timedelta(days=days)).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            rows = conn.execute(
                "SELECT dur_sec FROM sign_events WHERE stage='sign' AND dur_sec IS NOT NULL"
                " AND status IN ('success','already') AND ts >= ? ORDER BY dur_sec",
                (cutoff,),
            ).fetchall()
    except Exception as e:
        logger.warning("attempt_dur_quantile 失败: %s", e)
        return None
    vals = [float(r[0]) for r in rows if r[0] is not None]
    if len(vals) < max(1, int(min_samples)):
        return None
    # nearest-rank：升序第 ⌈pct×n⌉ 个（1 基）→ 下标 ⌈pct×n⌉-1（0 基）。
    # 旧实现用 int(pct*n)（向下取整）再夹到末元素：n=20、pct=0.95 时得到 index 19，
    # 返回的是**最大值**而不是 p95（工单 ba-p02-08）。容量告警拿它当实测输入，
    # 小样本时长期偏保守。
    idx = max(0, min(len(vals) - 1, math.ceil(pct * len(vals)) - 1))
    return vals[idx]


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
                "SELECT ts FROM audit_logs WHERE username IN (?, ?) AND action='my_account_pause' "
                "ORDER BY id DESC LIMIT 1",
                tuple(_actor_forms(username)),
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
                "SELECT COUNT(*) FROM audit_logs WHERE username IN (?, ?) "
                "AND action='my_account_pause' AND ts >= ?",
                (*_actor_forms(username), since_ts),
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
    取证数据源，时钟跳变不应放大清理窗口。跳变只跳本轮清理，下一轮恢复。
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
