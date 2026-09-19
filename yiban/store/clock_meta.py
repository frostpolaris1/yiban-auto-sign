# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""时钟守卫告警与 app_meta 通用单键读写。

**功能**
- 告警留痕：`_record_clock_guard_alert` 在时钟守卫拦截清理时把告警落进 app_meta（刻意的
  独立短连接，理由见函数说明）；`clock_guard_alert` 读回尚未清除的告警（无告警 → None）；
- 单键读写：`get_meta` / `set_meta` 是 app_meta 的最小读写对，值统一按 TEXT 存
  （调用方自行放日期串或 JSON）——告警通道健康日报需要一把"当日串键"做跨进程重启的
  每日去重，进程内 dict（如 `_mail_alert_ts`）重启即失效，兜不住"每次重启各发一封"。

**归属**
app_meta 表（键值对留痕表）与时钟跳变守卫的**告警出口**。守卫本体 `_clock_jump_guard`
留在 db 门面：它是登记承诺的跨域粘合——五处物理清理（账号 / 用户 / 注销请求 / 审计 /
事件）都以"传入自己的连接、在同一事务内 upsert 参照点"的方式调用它，cleanup / users /
events 三域经门面反向依赖，故不随本域迁出。app_meta 的其余写入方（清理留痕
`_record_purge_event`、库外锚点）各自的域内直查，不经本模块。建表在迁移域
（`yiban/store/migrations.py`，v8 建、v12 幂等补建），本模块只读写。

**复用**
`web/app.py`（每日线程/体检读 `clock_guard_alert` 发冻结告警邮件；告警通道健康日报的
当日去重键走 `get_meta` / `set_meta`）、`yiban/store/audit_chain.py`（库外锚点指纹与
审计欠账计数经 `_facade().get_meta` 读取）、`scripts/clock_guard_reset.py`（人工重置
工具以同一键名字面量读写 app_meta 并清除告警）。

**通信**
连接与进程内锁、写事务入口（`_conn_lock` / `get_conn` / `_begin_immediate`）一律经
`_facade()` 按属性取——必须按属性取而非模块级 from-import，`mock.patch.object(db,
"get_conn" / "_conn_lock", …)` 一类打桩才会在函数体里生效。告警留痕的库路径直接读连接
模块（`_connection._db_file` / `_connection.DB_DEFAULT`；`db._db_file = path` 的写入由
db 门面转发落到那里）——该路径刻意不经 `get_conn()`，理由见函数说明。时钟取 `yiban.clock`。
"""
import contextlib
import json
import logging
import os
import sqlite3

from yiban import clock
from yiban.store import connection as _connection

logger = logging.getLogger("yiban.store.clock_meta")


def _facade():
    """db 门面（函数内延迟导入，避免与 `yiban.store.db` 形成导入环）。

    打桩可见性见模块说明：必须按**属性**取而不是模块级 from-import，`db.<名字> = 替身`
    才会在函数体里生效。
    """
    from yiban.store import db
    return db


# 守卫失败告警在 app_meta 的留痕键（web 每日线程读取并发邮件；人工重置后清除）
_CLOCK_GUARD_ALERT_KEY = "clock_guard_alert"


def _record_clock_guard_alert(note):
    """守卫拦截时把告警落到 app_meta。

    原实现 ok=False 仅 logger.error：无任何告警出口，且因不更新参照点，5 处清理
    **永久**冻结（软删数据永不物理清除、审计/事件表无限膨胀）——与注释承诺的
    「清理推迟一天」相悖，日志无人看时静默腐烂。此处用**独立短连接**写入：
    守卫运行在调用方事务内，随后调用方会 rollback，同连接写入会被一起回滚。
    写失败不影响主流程（只告警）。JSON 结构 {ts, note}。
    """
    try:
        target_db = _connection._db_file or os.environ.get("YIBAN_DB_FILE") or _connection.DB_DEFAULT
        conn2 = sqlite3.connect(target_db, timeout=5)
        try:
            conn2.execute(
                "INSERT OR REPLACE INTO app_meta (key, value) VALUES (?,?)",
                (
                    _CLOCK_GUARD_ALERT_KEY,
                    json.dumps(
                        {
                            "ts": clock.now().strftime("%Y-%m-%d %H:%M:%S"),
                            "note": note,
                        },
                        ensure_ascii=False,
                    ),
                ),
            )
            conn2.commit()
        finally:
            conn2.close()
    except Exception as e:
        logger.warning("时钟守卫告警留痕失败: %s", e)


def clock_guard_alert():
    """读取未清除的时钟守卫告警（供 web 每日线程/体检调用）。无告警返回 None。"""
    try:
        db = _facade()
        with db._conn_lock:
            conn = db.get_conn()
            r = conn.execute(
                "SELECT value FROM app_meta WHERE key=?", (_CLOCK_GUARD_ALERT_KEY,)
            ).fetchone()
        if r is None or not r["value"]:
            return None
        try:
            data = json.loads(r["value"])
            if isinstance(data, dict) and data.get("note"):
                return data
        except ValueError:
            pass
        return {"ts": "", "note": str(r["value"])}
    except Exception as e:
        logger.warning("读取时钟守卫告警失败: %s", e)
        return None


# ---------------------------------------------------------------------------
# app_meta 通用单键读写
# ---------------------------------------------------------------------------
# app_meta 此前只有内联 SQL（见 _record_clock_guard_alert / record_audit_anchor）。
# 告警通道健康日报需要一把"当日串键"做跨进程重启的每日去重——进程内 dict（如
# _mail_alert_ts）重启即失效，兜不住"每次重启各发一封"。故在此收口一对最小读写：
# 不新建表、不加迁移，值统一按 TEXT 存（调用方自行放日期串或 JSON）。
def get_meta(key, default=""):
    """读取 app_meta 单键值（str）。键不存在 / 表缺失（旧库未跑 v12）/ 读失败 → default。

    刻意不抛：本函数的调用方是"尽力而为"的元数据留痕（如日报去重），读失败时按
    "无记录"继续即可，不能把兜底路径变成新故障点。
    """
    try:
        db = _facade()
        with db._conn_lock:
            conn = db.get_conn()
            row = conn.execute("SELECT value FROM app_meta WHERE key=?", (key,)).fetchone()
        if row is None or row["value"] is None:
            return default
        return str(row["value"])
    except Exception as e:
        logger.warning("读取 app_meta[%s] 失败（按无记录处理）: %s", key, e)
        return default


def set_meta(key, value):
    """写入/更新 app_meta 单键值（INSERT OR REPLACE）。返回 True 表示已落库。

    与其余写路径同口径走 _begin_immediate（WAL 下该 INSERT 即持 RESERVED 写锁）；
    失败只告警并返回 False——元数据留痕不得放大故障，也不得留下未决事务。
    """
    try:
        db = _facade()
        with db._conn_lock:
            conn = db.get_conn()
            db._begin_immediate(conn)
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO app_meta (key, value) VALUES (?,?)",
                    (key, str(value)),
                )
                conn.commit()
            except Exception:
                with contextlib.suppress(Exception):
                    conn.rollback()
                raise
        return True
    except Exception as e:
        logger.warning("写入 app_meta[%s] 失败: %s", key, e)
        return False
