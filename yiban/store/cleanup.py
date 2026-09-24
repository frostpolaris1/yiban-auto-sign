# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""每日清理域：把周期性物理清除集中到一处编排。

**功能**
- `run_daily_cleanup()`：每日清理入口，依次跑审计/事件保留期清理、孤儿会话缓存清除，
  再调用账号与用户的到期物理清除；
- `_audit_cleanup(conn)`：删除超过保留期的审计日志（删除与清理留痕同事务）；
- `_purge_expired_deleted(conn)` / `purge_expired_deleted_accounts()`：物理清除超过
  保留期的软删账号（含"owner 仍在注销反悔窗口内"的豁免）。

**归属**
清理是横跨多张表的动作，故单列一域：本模块不定义自己的表，只按各表保留期调用。
sign_events 的保留期清理（同事务兼清超期 verify_jobs）留在 `yiban/store/events.py` 的
`_event_cleanup`——那是事件表写入者对自己表的保留策略，本模块只在编排里调用，不改其
内部；用户行与注销请求的到期清除在 `yiban/store/users.py`，同样只调用。

**复用**
`db.run_daily_cleanup()` 是 web 每日线程与 `init_db(cleanup=True)` 的共用入口；
`db._audit_cleanup` / `db.purge_expired_deleted_accounts` 仍可从门面直接调用。

**通信**
连接、锁、时钟守卫、清理留痕（`get_conn` / `_conn_lock` / `_clock_jump_guard` /
`_table_min_max` / `_record_purge_event` / `_audit_purge_total`）与各域清理函数
（`_event_cleanup` / `purge_orphan_session_cache` / `purge_deleted_users` /
`purge_old_delete_requests` / `purge_sign_claims` / `_cascade_phone_owned`，以及
`SOFT_DELETE_RETENTION_SECONDS` / `PURGE_SKIP_CANCELLED_OWNER`）一律经 `_facade()`
按属性取——按属性取保证 `db.<名字> = 替身` 打桩可见。
"""
import contextlib
import datetime
import logging

from yiban import clock

logger = logging.getLogger("yiban.store.cleanup")


def _facade():
    """db 门面（函数内延迟导入，避免与 `yiban.store.db` 形成导入环）。

    打桩可见性见模块说明：必须按**属性**取而不是模块级 from-import，`db.<名字> = 替身`
    才会在函数体里生效。
    """
    from yiban.store import db
    return db


def _purge_expired_deleted(conn):
    """软删除超过保留期的行物理清除（库内必有 deleted_at）。

    deleted_at 为 %Y-%m-%d %H:%M:%S 格式，字符串比较等价时间序。清理失败仅告警不阻断。
    """
    try:
        # 时钟异常跳变（拨快 / 回拨超限）时跳过清理，防"系统时间被拨快后刚软删 1 秒的
        # 账号被立即物理清除、反悔窗口归零"。只跳本轮：守卫在越界路径上也推进参照点，
        # 下一轮（≤24h 后）即恢复。守卫的 INSERT upsert 在 WAL 下即持 RESERVED 写锁，
        # 兼作下面的读-删-连带清理的事务边界。
        ok, note = _facade()._clock_jump_guard(conn, "purge_accounts_clock")
        if not ok:
            logger.error("%s", note)
            with contextlib.suppress(Exception):
                conn.rollback()
            return
        # 旧版本/手工写入的 deleted=1 且 deleted_at='' 行不参与保留期判定（条件含
        # deleted_at != ''），会成为不死僵尸——统一补记当前时间，宽限期自此起算
        now_str = clock.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "UPDATE accounts SET deleted_at=? WHERE deleted=1 AND deleted_at=''",
            (now_str,),
        )
        cutoff = (clock.now() - datetime.timedelta(
            seconds=_facade().SOFT_DELETE_RETENTION_SECONDS
        )).strftime("%Y-%m-%d %H:%M:%S")
        skip = _facade().PURGE_SKIP_CANCELLED_OWNER
        # 先查有无超期行再删——无行时不发 DELETE 事务，只提交守卫的时钟参照一行
        #（每天 1~2 次调用，开销可忽略）
        probe = conn.execute(
            "SELECT 1 FROM accounts WHERE deleted=1 AND deleted_at != '' AND deleted_at <= ?"
            + skip + " LIMIT 1",
            (cutoff, cutoff),
        ).fetchone()
        if not probe:
            conn.commit()
            return
        # 读 phones → DELETE → 连带清理 整段原子：SELECT 与 DELETE 之间跨进程无互斥时，
        # 期间被管理员恢复的账号（deleted=0）其行会被 WHERE deleted=1 正确保留，但
        # time_prefs / session_cache 会被陈旧 phones 列表连带误删——用户自选签到时段被
        # 静默重置为自动错峰。守卫 INSERT 已持写锁，事务内重读 phones（不复用事务前
        # 列表），读-删-清对外原子。
        phones = [
            r["phone"]
            for r in conn.execute(
                "SELECT phone FROM accounts WHERE deleted=1 AND deleted_at != '' AND deleted_at <= ?"
                + skip,
                (cutoff, cutoff),
            ).fetchall()
        ]
        conn.execute(
            "DELETE FROM accounts WHERE deleted=1 AND deleted_at != '' AND deleted_at <= ?"
            + skip,
            (cutoff, cutoff),
        )
        _facade()._cascade_phone_owned(conn, phones)
        conn.commit()
    except Exception as e:
        with contextlib.suppress(Exception):
            conn.rollback()
        logger.warning("清理超期软删除账号失败: %s", e)


def purge_expired_deleted_accounts():
    """物理清除超过保留期的软删除账号（显式调用：init_db 启动清理 / web 每日线程 / signin 启动）。

    清理刻意只发生在显式时机，不挂在 load_accounts() 读路径上：读中途物理删行会让此后
    所有下标前移，web 层按 idx 寻址的 mutation 在管理员持旧视图时会静默命中错误对象
    （"无人触发的索引漂移"）。移出读路径后，两次读取之间的列表顺序保持稳定。
    """
    with _facade()._conn_lock:
        conn = _facade().get_conn()
        _facade()._purge_expired_deleted(conn)


def _audit_cleanup(conn):
    """删除超期审计日志（启动/每日清理时一条 DELETE）。清理失败仅告警。

    删除旧行后不重建哈希链——剩余首行仍保留指向已删前序行的 prev_hash 作为锚，
    verify_audit_chain 以该锚校验首行 hash。

    接入时钟跳变守卫：审计是篡改取证数据源，时钟被拨快（NTP 故障或拿到服务器权限者
    掩盖痕迹）会让 cutoff 前移、审计链被一次性清空，且该清理不动"最后一条"锚点，
    库外锚点校验不会报警。与三个短保留期 purge 同口径（同样只跳一轮）。
    """
    try:
        ok, note = _facade()._clock_jump_guard(conn, "audit_cleanup_clock")
        if not ok:
            logger.error("%s", note)
            with contextlib.suppress(Exception):
                conn.rollback()
            return
        cutoff = (clock.now() - datetime.timedelta(days=180)).strftime("%Y-%m-%d %H:%M:%S")
        before = _facade()._table_min_max(conn, "audit_logs")
        cur = conn.execute("DELETE FROM audit_logs WHERE ts < ?", (cutoff,))
        deleted = cur.rowcount or 0
        after = _facade()._table_min_max(conn, "audit_logs")
        if deleted:
            # 删除与留痕同事务：锚点的稠密性判据拿"有留痕的删除累计数"解释缺口，
            # 缺了这一步，保留期清理每天都会被判成非法删除。
            _facade()._record_purge_event(
                conn, "audit_logs", "audit_cleanup", cutoff, deleted,
                before, after, audit_seq=_facade()._audit_purge_total(conn) + deleted,
            )
        conn.commit()
    except Exception as e:
        with contextlib.suppress(Exception):
            conn.rollback()
        logger.warning("清理旧审计日志失败: %s", e)


def run_daily_cleanup():
    """每日定期清理的集中入口。

    清理集中在显式时机，而不是挂在 init_db 上：signin 子进程每天要跑 2~3 次（Docker
    调度器首签/补签/探针，宿主 cron 同理），每次都执行一轮全表 DELETE + 多个 purge，会
    与 web 的请求线程抢库级写锁，是审计写入失败与陈旧列表误删的主要诱因。现由 web 每日
    线程统一调用本函数；signin 侧走 init_db(cleanup=False)，只在其 main() 里保留对超期
    软删账号的显式清理（覆盖无 web 的纯 cron 场景）。

    _audit_cleanup / _event_cleanup 直接在共享连接上 execute+commit——必须持
    _conn_lock，否则与请求线程的 BEGIN IMMEDIATE 事务交叠时，清理的 DELETE 会加入他人
    未提交事务、commit 把半程事务提前发布（撕裂事务，破坏原子性）。
    """
    with _facade()._conn_lock:
        conn = _facade().get_conn()
        _facade()._audit_cleanup(conn)
        _facade()._event_cleanup(conn)
        try:
            orphans = _facade().purge_orphan_session_cache(conn)
            conn.commit()
            if orphans:
                # 孤儿行意味着"账号已不存在但凭据缓存还在"：留痕（不含手机号明文）
                logger.warning("已清除 %d 条孤儿会话缓存（账号行已不存在）", orphans)
        except Exception as e:
            with contextlib.suppress(Exception):
                conn.rollback()
            logger.warning("清除孤儿会话缓存失败（不影响其他清理）: %s", e)
    _facade().purge_expired_deleted_accounts()
    _facade().purge_deleted_users()
    _facade().purge_old_delete_requests()
    _facade().purge_sign_claims()
