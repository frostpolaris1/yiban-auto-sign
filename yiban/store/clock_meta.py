# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""app_meta 表的通用单键读写。

**功能**
`get_meta` / `set_meta` 是 app_meta 的最小读写对，值统一按 TEXT 存（调用方自行放
日期串或 JSON）。

**归属**
app_meta 表（键值对留痕表）的通用读写口。建表在迁移域（`yiban/store/migrations.py`，
v8 建、v12 幂等补建），本模块只读写。app_meta 的其余写入方（清理留痕
`_record_purge_event`、库外锚点、时钟跳变守卫的参照点）各自在域内直查，不经本模块；
时钟跳变守卫本体 `_clock_jump_guard` 也留在 db 门面：它是登记承诺的跨域粘合——五处
物理清理（账号 / 用户 / 注销请求 / 审计 / 事件）都以"传入自己的连接、在同一事务内
upsert 参照点"的方式调用它，cleanup / users / events 三域经门面反向依赖，故不随本域
迁出。

**复用**
`web/services/channel_health.py`（告警通道日报的当日去重键）、`yiban/store/audit_chain.py`
（库外锚点指纹与审计欠账计数）都经 `db.get_meta` / `db.set_meta` 读写。

**通信**
连接与进程内锁、写事务入口（`_conn_lock` / `get_conn` / `_begin_immediate`）一律经
`_facade()` 按属性取——必须按属性取而非模块级 from-import，`mock.patch.object(db,
"get_conn" / "_conn_lock", …)` 一类打桩才会在函数体里生效。
"""
import contextlib
import logging

logger = logging.getLogger("yiban.store.clock_meta")


def _facade():
    """db 门面（函数内延迟导入，避免与 `yiban.store.db` 形成导入环）。

    打桩可见性见模块说明：必须按**属性**取而不是模块级 from-import，`db.<名字> = 替身`
    才会在函数体里生效。
    """
    from yiban.store import db
    return db


# ---------------------------------------------------------------------------
# app_meta 通用单键读写
# ---------------------------------------------------------------------------
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
