# -*- coding: utf-8 -*-
"""fencing epoch：领取自增、**所有终态写**都带 `epoch=?`，以及 `try_claim` 的 fail-closed。

标签：B · 调度：领取/队列/执行体
覆盖：epoch 的领取返回值（首行为 1、同 owner 重入单调递增、接管抬高、未领到返回
   0）、settle / give_up / touch 三类终态写的 WHERE 带 epoch、写 0
   行后的回读分档（已有终态按幂等键、无终态则告警）、库异常时 try_claim 的
   fail-closed 与只告警一次、sign_tasks 侧同口径（claim_batch / settle_tasks /
   requeue_task）。
对应实现：yiban/store/claims.py（epoch 自增与守卫）、scripts/db.py（claim_sign_account
   / claim_settle 的 fencing
   参数）、yiban/store/queue_store.py（claim_batch、settle_tasks、requeue_task）、yiban/engine/alerts.py（fail-closed
   告警）。
关键断言：fencing 用例必须是可判别的：判别力只在「owner 相同、epoch
   落后」这一形态上——被接管时 owner 条件本身就拦住了写，测不出 epoch 有没有进
   WHERE。只做 owner 作用域而漏掉 epoch 守卫时照样全绿，等于没测。fail-closed
   是另一条：库异常时拒跑并告警，绝不允许答「可执行」（多执行体下 fail-open =
   同一账号两次真实登录）。唯一键冲突是「别人刚领到」，不得混进「池子坏了」的告警。
依赖：临时 sqlite（每用例重建）+ 打桩
   yiban.engine.alerts；无网络请求。整文件在本机执行，无 skip。

`epoch=None`（不传）时的行为与改造前逐字一致，由既有 `tests/test_claims.py` 全绿覆盖。
"""
import contextlib
import datetime
import os
import shutil
import sqlite3
import tempfile
import unittest
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import db  # noqa: E402

from yiban import clock  # noqa: E402
from yiban.engine import alerts  # noqa: E402
from yiban.store import claims, queue_store  # noqa: E402

TEST_KEY = "a" * 64
DAY = "2026-09-22"
PHONE = "13800138000"
PHONE_B = "13800138001"
OWNER_A = "hostA:100:090000" # 旧格式身份串：本文件只把它当不透明的持有者标识用
OWNER_B = "hostB:200:090001"


def _ts(**kw):
    return (clock.now() + datetime.timedelta(**kw)).strftime("%Y-%m-%d %H:%M:%S") # 相对业务钟取偏移：租约与心跳的判据必须落在同一根时间轴上


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-fencing-")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n")
        os.environ.update({
            "YIBAN_ACCOUNTS_KEY": TEST_KEY,
            "YIBAN_ENV_FILE": cls.env_file,
            "YIBAN_DB_FILE": cls.db_file,
        })

    @classmethod
    def tearDownClass(cls):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k in ("YIBAN_ACCOUNTS_KEY", "YIBAN_ENV_FILE", "YIBAN_DB_FILE"):
            os.environ.pop(k, None)

    def setUp(self):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        for suffix in ("", "-wal", "-shm"):
            p = self.db_file + suffix
            if os.path.exists(p):
                os.remove(p)
        db.init_db(self.db_file, env_file=self.env_file, cleanup=False)

    def tearDown(self):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None

    def _claim(self, phone=PHONE, owner=OWNER_A, **kw):
        return db.claim_sign_account(phone, DAY, owner, **kw)

    def _row(self, phone=PHONE):
        return dict(db.get_conn().execute(
            "SELECT * FROM sign_claims WHERE phone=? AND day=?", (phone, DAY)).fetchone())


class EpochReturnTest(_Base):
    """领取把 fencing token 交回调用方：没有它，收尾侧无从带上 epoch。"""

    def test_new_row_starts_at_one(self):
        ok, epoch = self._claim() # ok 只是前置，epoch 才是本文件的断言对象
        self.assertTrue(ok)
        self.assertEqual(epoch, 1, "插入分支的 epoch 取 1（默认 0 表示从未领取）")
        self.assertEqual(self._row()["epoch"], 1)

    def test_same_owner_reclaim_is_monotonic(self):
        """同 owner 重入（重试、进程重启接管自己的行）也必须单调递增。

        收尾写的是"最近一次领取"的 epoch，若重入不递增，旧 epoch 就仍然能写进去。
        """
        _ok, e1 = self._claim()
        _ok, e2 = self._claim()
        self.assertGreater(e2, e1, "同 owner 重入必须严格递增")
        self.assertEqual(self._row()["epoch"], e2)

    def test_takeover_by_other_owner_raises_epoch(self):
        _ok, e_a = self._claim()
        _ok, e_b = self._claim(owner=OWNER_B, lease_sec=0)
        self.assertGreater(e_b, e_a, "接管者的 epoch 必须严格大于被接管者")
        self.assertEqual(self._row()["owner"], OWNER_B)

    def test_refused_claim_returns_zero_epoch(self):
        """没领到就是 (False, 0)：调用方不得把 0 当成一个可用的 token。"""
        self._claim()
        self.assertEqual(self._claim(owner=OWNER_B), (False, 0))


class FencedSettleTest(_Base):
    """`settle` 的 WHERE 带 epoch：陈旧 token 的写必须被存储端拒绝（Kleppmann）。"""

    def test_stale_epoch_same_owner_is_refused(self):
        """判别力所在：owner 相同、epoch 落后 → 只有 epoch 守卫能拦住这次写。"""
        _ok, e1 = self._claim()
        _ok, e2 = self._claim()          # 同 owner 重入 → e2 > e1
        self.assertFalse(
            db.claim_settle(PHONE, DAY, OWNER_A, db.CLAIM_STATE_DONE, "陈旧", epoch=e1),
            "陈旧 epoch 的收尾必须被拒（否则被接管者迟到写会覆盖接管者的结论）")
        row = self._row()
        self.assertEqual(row["state"], db.CLAIM_STATE_CLAIMED)
        self.assertEqual(row["result"], "")
        self.assertTrue(
            db.claim_settle(PHONE, DAY, OWNER_A, db.CLAIM_STATE_DONE, "最新", epoch=e2))

    def test_takeover_scenario_settles_only_with_latest_epoch(self):
        """简报场景：A 领 → B 租约过期接管 → A 用旧 epoch 收尾被拒、B 用新 epoch 成功。"""
        _ok, e_a = self._claim()
        _ok, e_b = self._claim(owner=OWNER_B, lease_sec=0)
        self.assertFalse(
            db.claim_settle(PHONE, DAY, OWNER_A, db.CLAIM_STATE_DONE, "A 的结论", epoch=e_a))
        row = self._row()
        self.assertEqual(row["state"], db.CLAIM_STATE_CLAIMED, "A 不得改动被 B 接管的行")
        self.assertEqual(row["result"], "", "A 的结果不得落库")
        self.assertEqual(row["owner"], OWNER_B)
        self.assertTrue(
            db.claim_settle(PHONE, DAY, OWNER_B, db.CLAIM_STATE_DONE, "B 的结论", epoch=e_b))
        self.assertEqual(self._row()["state"], db.CLAIM_STATE_DONE)

    def test_fenced_write_with_terminal_state_reads_back_as_info(self):
        """写 0 行且传了 epoch → **必须回读**：已有终态按幂等键语义处理（Stripe）。"""
        _ok, e_a = self._claim()
        _ok, e_b = self._claim(owner=OWNER_B, lease_sec=0)
        db.claim_settle(PHONE, DAY, OWNER_B, db.CLAIM_STATE_DONE, "ok", epoch=e_b)
        with mock.patch.object(claims.logger, "info") as info, \
                mock.patch.object(claims.logger, "warning") as warn:
            self.assertFalse(
                db.claim_settle(PHONE, DAY, OWNER_A, db.CLAIM_STATE_DONE, "迟到", epoch=e_a))
        self.assertTrue(info.called, "已有终态：降为 info（重入不是故障）")
        self.assertFalse(warn.called, "已有终态不得报 warning（否则日志被正常重入刷满）")

    def test_fenced_write_without_terminal_state_warns(self):
        """写 0 行且回读无终态 = 真异常（本执行体被接管却仍以为在做）→ warning。"""
        _ok, e_a = self._claim()
        self._claim(owner=OWNER_B, lease_sec=0)
        with mock.patch.object(claims.logger, "warning") as warn:
            self.assertFalse(
                db.claim_settle(PHONE, DAY, OWNER_A, db.CLAIM_STATE_DONE, "迟到", epoch=e_a))
        self.assertTrue(warn.called, "被 fencing 拒且无终态必须 warning")


class FencedGiveUpTouchTest(_Base):
    """`give_up` 与 `touch` 同样受 epoch 守卫（终态写与续租都要覆盖）。"""

    def test_give_up_refused_with_stale_epoch_same_owner(self):
        _ok, e1 = self._claim()
        _ok, e2 = self._claim()
        self.assertFalse(db.claim_give_up(PHONE, DAY, OWNER_A, "陈旧", epoch=e1))
        row = self._row()
        self.assertEqual(row["state"], db.CLAIM_STATE_CLAIMED, "陈旧 epoch 不得置 failed")
        self.assertTrue(db.claim_give_up(PHONE, DAY, OWNER_A, "最新", epoch=e2))
        self.assertEqual(self._row()["state"], db.CLAIM_STATE_FAILED)

    def test_give_up_refused_after_takeover(self):
        _ok, e_a = self._claim()
        self._claim(owner=OWNER_B, lease_sec=0)
        self.assertFalse(db.claim_give_up(PHONE, DAY, OWNER_A, "A 弃权", epoch=e_a))
        row = self._row()
        self.assertEqual(row["owner"], OWNER_B)
        self.assertEqual(row["state"], db.CLAIM_STATE_CLAIMED, "行仍由 B 持有")

    def test_touch_refused_with_stale_epoch_same_owner(self):
        """续租也要 fencing：被接管者续租会把租约重新拉长，让死执行体继续占位。"""
        _ok, e1 = self._claim()
        _ok, e2 = self._claim()
        self.assertFalse(db.claim_touch(PHONE, DAY, OWNER_A, epoch=e1))
        self.assertTrue(db.claim_touch(PHONE, DAY, OWNER_A, epoch=e2))


class FailClosedTest(_Base):
    """库异常时拒跑并告警：多执行体下 fail-open = 同一账号两次真实登录。"""

    class _BoomConn:
        def __init__(self, exc):
            self._exc = exc
            self.commits = 0

        def execute(self, sql, params=()):
            raise self._exc

        def commit(self):
            self.commits += 1

    @contextlib.contextmanager
    def _broken_db(self, exc):
        conn = self._BoomConn(exc)
        with mock.patch.object(claims, "_pool_down_notified", False), \
                mock.patch.object(db, "get_conn", return_value=conn), \
                mock.patch.object(db, "_conn_lock", contextlib.nullcontext()):
            yield conn

    def test_library_error_refuses_and_alerts_once(self):
        with self._broken_db(sqlite3.OperationalError("database is locked")), \
                mock.patch.object(claims.logger, "error") as log_err, \
                mock.patch.object(alerts, "_collect_admin_mail") as collect:
            got = self._claim()
            self.assertEqual(got, (False, 0), "库异常必须拒跑")
            self.assertIsNot(got, True)
            self.assertEqual(self._claim(phone=PHONE_B), (False, 0))
            self.assertEqual(self._claim(phone="13800138002"), (False, 0))
            self.assertEqual(log_err.call_count, 1, "error 只报一次（多账号不刷屏）")
            self.assertEqual(collect.call_count, 1, "告警并入当日汇总，且不重复收集")
            self.assertEqual(collect.call_args[0][0], "签到领取池不可用")

    def test_integrity_error_path_stays_silent_refusal(self):
        """唯一键冲突是"别人刚领到"，不是"池子坏了"：不得告警，也不得吞成可执行。"""
        with self._broken_db(sqlite3.IntegrityError("UNIQUE constraint failed")), \
                mock.patch.object(claims.logger, "error") as log_err, \
                mock.patch.object(alerts, "_collect_admin_mail") as collect:
            got = self._claim()
        self.assertEqual(got, (False, 0))
        log_err.assert_not_called()
        collect.assert_not_called()


class QueueStoreEpochTest(_Base):
    """`sign_tasks` 侧同一套口径：领取自增、收尾带 epoch。"""

    OWNER = OWNER_A
    SHARDS = (0, 1)

    def _add_task(self, phone, vshard=0, run_at=None):
        conn = db.get_conn()
        conn.execute(
            "INSERT INTO sign_tasks (phone, day, vshard, owner, run_at, priority, "
            "state, attempts, lease_until, result, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (phone, DAY, vshard, "", run_at or _ts(seconds=-1), 5, "pending", 0,
             "", "", _ts(seconds=-60)))
        conn.commit()

    def _task_row(self, phone):
        return dict(db.get_conn().execute(
            "SELECT * FROM sign_tasks WHERE phone=? AND day=?", (phone, DAY)).fetchone())

    def test_claim_batch_returns_and_bumps_epoch(self):
        self._add_task(PHONE)
        first = queue_store.claim_batch(self.OWNER, DAY, self.SHARDS, now=_ts())
        self.assertEqual(len(first), 1)
        self.assertEqual(set(first[0]), {"phone", "run_at", "attempts", "epoch"})
        e1 = first[0]["epoch"]
        self.assertGreaterEqual(e1, 1)
        # 重排回 pending 后再次领取（同一 owner）→ epoch 严格递增
        queue_store.requeue_task(PHONE, DAY, _ts(seconds=-1))
        second = queue_store.claim_batch(self.OWNER, DAY, self.SHARDS, now=_ts())
        self.assertEqual(len(second), 1)
        e2 = second[0]["epoch"]
        self.assertGreater(e2, e1, "接管/重领必须拿到更大的 fencing token")

    def test_settle_tasks_is_fenced_by_epoch(self):
        """owner 相同、epoch 落后 → 只有 epoch 守卫能拦住这次收尾（判别力所在）。"""
        self._add_task(PHONE)
        e1 = queue_store.claim_batch(self.OWNER, DAY, self.SHARDS, now=_ts())[0]["epoch"]
        queue_store.requeue_task(PHONE, DAY, _ts(seconds=-1))
        e2 = queue_store.claim_batch(self.OWNER, DAY, self.SHARDS, now=_ts())[0]["epoch"]
        self.assertEqual(
            queue_store.settle_tasks(self.OWNER, DAY, [(PHONE, "陈旧")], epochs={PHONE: e1}), 0)
        self.assertEqual(self._task_row(PHONE)["state"], "claimed", "陈旧 epoch 不得收尾")
        self.assertEqual(
            queue_store.settle_tasks(self.OWNER, DAY, [(PHONE, "最新")], epochs={PHONE: e2}), 1)
        row = self._task_row(PHONE)
        self.assertEqual(row["state"], "done")
        self.assertEqual(row["result"], "最新")

    def test_settle_tasks_without_epochs_keeps_old_behaviour(self):
        self._add_task(PHONE)
        queue_store.claim_batch(self.OWNER, DAY, self.SHARDS, now=_ts())
        self.assertEqual(queue_store.settle_tasks(self.OWNER, DAY, [(PHONE, "ok")]), 1)

    def test_requeue_task_is_fenced_by_epoch(self):
        self._add_task(PHONE)
        e1 = queue_store.claim_batch(self.OWNER, DAY, self.SHARDS, now=_ts())[0]["epoch"]
        self.assertEqual(
            queue_store.requeue_task(PHONE, DAY, _ts(minutes=+1), epoch=e1 + 1), 0,
            "陈旧/未知 epoch 不得把在飞任务重排回 pending")
        self.assertEqual(self._task_row(PHONE)["state"], "claimed")
        self.assertEqual(
            queue_store.requeue_task(PHONE, DAY, _ts(minutes=+1), epoch=e1), 1)
        self.assertEqual(self._task_row(PHONE)["state"], "pending")


if __name__ == "__main__":
    unittest.main()
