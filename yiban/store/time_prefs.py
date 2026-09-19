# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""用户自选时间片域：time_prefs 表的读写、拥挤度统计与保存冷却查询。

**功能**
- 读写：`get_time_prefs`（全量）、`get_time_pref`（单账号，无则 None）、
  `set_time_pref`（UPSERT，updated_at 由调用方给出）、`clear_time_pref`（回退自动错峰）；
- 统计：`time_pref_stats` 按 slot_min 聚合**未删除账号**的已选人数（拥挤度），
  已注销/已软删账号的残留 pref 不参与，否则会虚高拥挤度；
- 保存冷却：`last_time_pref_set_at`（最近一次保存时间）与 `time_pref_set_count_since`
  （窗口内保存次数）读 audit_logs 里 action='time_pref_set' 的行。

**归属**
time_prefs 表以 phone 为键，服务调度 v2 的「用户自选时间片」
（docs/design/plan-scheduler-v2.md 2.2）：签到前每人选一个窗口内的分钟数，
拥挤度用于错峰提示。冷却查询按**被选账号**计价而非按操作管理员计价
（审计 target=hash_phone(phone)，匿名稳定键）：多管理员共享 admin 账号时冷却全局生效
（管理员 A 保存后 B 立即改选也被拦截）；改手机号/删号重提交新号后，新 phone 无历史审计，
不被旧账号冷却误伤。暂停/恢复的冷却查询按 username 计价、查 audit_logs 表，按**表归属**
落在 `yiban/store/events.py`。建表在迁移域（`yiban/store/migrations.py`），本模块只读写。

**复用**
`web/routes/my.py`（自选读写、拥挤度与保存冷却）、`web/routes/accounts_api.py`（改绑手机号
时清旧号自选）、`yiban/engine/schedule.py`（`build_schedule` 每次启动读一次全量）经 db 门面
调用；`scripts/generate_demo_data.py` 写演示自选。换号/注销路径是否保留自选由
`yiban/store/users.py` 与账号软删路径决定，本模块不做取舍。

**通信**
连接与进程内锁（`_conn_lock` / `get_conn`）一律经 `_facade()` 按属性取——必须按属性取而非
模块级 from-import，`mock.patch.object(db, "get_conn"/"_conn_lock", …)` 一类打桩才会在函数
体里生效。冷却查询用的 `hash_phone` 定义点在 `yiban/store/tracking.py`，同样经 `_facade()`
按属性取，`db.hash_phone = 替身` 才能被这里的查询看见。
"""
import logging

logger = logging.getLogger("yiban.store.time_prefs")


def _facade():
    """db 门面（函数内延迟导入，避免与 `yiban.store.db` 形成导入环）。

    打桩可见性见模块说明：必须按**属性**取而不是模块级 from-import，`db.<名字> = 替身`
    才会在函数体里生效。
    """
    from yiban.store import db
    return db


def last_time_pref_set_at(phone):
    """指定账号最近一次自选时间片保存时间（切换冷却判定用；无记录返回 None）。

    按被选账号（审计 target=hash_phone(phone)（匿名稳定键））而非操作用户计价：
    - 多管理员共享 admin 账号时冷却全局生效（管理员 A 保存后 B 立即改选也被拦截）；
    - 改手机号/删号重提交新号后，新 phone 无历史审计 → 不被旧账号冷却误伤。
    """
    try:
        db = _facade()
        with db._conn_lock:
            conn = db.get_conn()
            target = db.hash_phone(phone) if phone else phone or ""
            row = conn.execute(
                "SELECT ts FROM audit_logs WHERE action='time_pref_set' AND target=? "
                "ORDER BY id DESC LIMIT 1",
                (target,),
            ).fetchone()
            return row["ts"] if row else None
    except Exception as e:
        raise RuntimeError(f"查询自选保存时间失败: {e}") from e


def time_pref_set_count_since(phone, since_ts):
    """指定账号在 since_ts 之后的保存次数（弹性冷却高频判定用；ts 定宽字符串可比较）。

    审计 target=hash_phone(phone)（匿名稳定键）。
    """
    try:
        db = _facade()
        with db._conn_lock:
            conn = db.get_conn()
            target = db.hash_phone(phone) if phone else phone or ""
            row = conn.execute(
                "SELECT COUNT(*) FROM audit_logs WHERE action='time_pref_set' "
                "AND target=? AND ts >= ?",
                (target, since_ts),
            ).fetchone()
            return row[0] if row else 0
    except Exception as e:
        raise RuntimeError(f"统计自选保存次数失败: {e}") from e


# ---------------------------------------------------------------------------
# 用户自选时间片（调度 v2，docs/design/plan-scheduler-v2.md 2.2）
# ---------------------------------------------------------------------------
def get_time_prefs():
    """全量自选 {phone: {"slot_min": int, "updated_at": str}}（build_schedule 每次启动读一次）。"""
    try:
        db = _facade()
        with db._conn_lock:
            conn = db.get_conn()
            rows = conn.execute("SELECT phone, slot_min, updated_at FROM time_prefs").fetchall()
            return {r["phone"]: {"slot_min": r["slot_min"], "updated_at": r["updated_at"]} for r in rows}
    except Exception as e:
        logger.warning("读取 time_prefs 失败: %s", e)
        return {}


def get_time_pref(phone):
    """单个账号自选；无则 None。"""
    try:
        db = _facade()
        with db._conn_lock:
            conn = db.get_conn()
            row = conn.execute(
                "SELECT phone, slot_min, updated_at FROM time_prefs WHERE phone=?", (phone,)
            ).fetchone()
            return None if row is None else {"slot_min": row["slot_min"], "updated_at": row["updated_at"]}
    except Exception as e:
        logger.warning("读取 time_pref %s 失败: %s", phone, e)
        return None


def set_time_pref(phone, slot_min, updated_at):
    """保存/更新自选（UPSERT）。slot_min 为窗口内分钟数（06:30 → 390，5 对齐）。"""
    db = _facade()
    conn = db.get_conn()
    with db._conn_lock, conn:
        conn.execute(
            "INSERT INTO time_prefs (phone, slot_min, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(phone) DO UPDATE SET slot_min=excluded.slot_min, updated_at=excluded.updated_at",
            (phone, slot_min, updated_at),
        )


def clear_time_pref(phone):
    """清除自选（回退自动错峰）。"""
    db = _facade()
    conn = db.get_conn()
    with db._conn_lock, conn:
        conn.execute("DELETE FROM time_prefs WHERE phone=?", (phone,))


def time_pref_stats():
    """每片已选人数（拥挤度）：[{slot_min, count}]，按 slot_min 升序。

    只统计未删除账号的自选，避免已注销/已软删账号的残留 pref 虚高拥挤度。
    """
    try:
        db = _facade()
        with db._conn_lock:
            conn = db.get_conn()
            rows = conn.execute(
                "SELECT t.slot_min, COUNT(*) AS count "
                "FROM time_prefs t "
                "JOIN accounts a ON a.phone = t.phone AND a.deleted = 0 "
                "GROUP BY t.slot_min ORDER BY t.slot_min"
            ).fetchall()
            return [{"slot_min": r["slot_min"], "count": r["count"]} for r in rows]
    except Exception as e:
        logger.warning("time_prefs 统计失败: %s", e)
        return []
