# -*- coding: utf-8 -*-
"""v3 崩溃恢复的队列访问层：`queue_store.reap_expired` 与 `queue_store.steal_shards`。

标签：B · 调度：领取/队列/执行体
覆盖：reap_expired
   的宽限期与判据分档（过期未超宽限不动、超宽限回收、未过期不动、终态与历史行不动、幂等、day
   限定作用域、表缺失与 now 不可解析分别告警）、steal_shards
   的接管边界（只动分片集内 pending 且 owner 恰为死主的行、epoch+1、重跑 0
   行、空分片集不碰库）、回收与 fencing 的联动。
对应实现：yiban/store/queue_store.py（reap_expired、steal_shards、REAP_GRACE_SEC、claim_batch、settle_tasks）、yiban/engine/executor_v3.py
   的回收接线。
关键断言：租约到期不等于持有者已死：单次尝试可能比租约还慢，刚过期就回收会让行回退
   pending 并被同一执行体重领 ⇒ 同一账号两条通道并发登录（epoch+1
   只挡迟到的结论写回，挡不住这次重复真实登录）。故宽限期必须 ≥ 2×
   租约。回收后原持有者带旧 token
   的迟到收尾必须写不进去，否则新执行体的结论被陈旧结论覆盖。vshard=-1
   的历史行不属于任何分片集，回收成 pending 只会永不被领取。
依赖：临时 sqlite（sign_tasks 由 db.init_db 的迁移建表）+
   固定时刻常量；不发网络请求、不起应用。整文件在本机执行，无 skip。

依赖：临时库（`sign_tasks` 由 `db.init_db` 的迁移建表）。
"""
import contextlib
import os
import shutil
import tempfile
import unittest

import db

from yiban.store import queue_store

DAY = "2026-09-22"
NOW = "2026-09-22 06:40:00.000"
#: 租约已过期且**超出宽限期**（`REAP_GRACE_SEC` 120s）——回收条件成立
EXPIRED = "2026-09-22 06:36:00.000"
#: 租约刚过期 30s、仍在宽限期内——持有者可能还在飞，不得回收
JUST_EXPIRED = "2026-09-22 06:39:30.000"
#: 租约过期 150s、已过宽限期（> 120s）——回收条件成立
GRACE_EXPIRED = "2026-09-22 06:37:30.000"
FRESH = "2026-09-22 06:41:00.000"
DEAD = "worker-9@testhost" # 用真格式的身份串：接管按 owner 精确匹配，随手编的串会躲过判据
LIVE = "worker-2@testhost"
ME = "worker-1@testhost"
TEST_KEY = "a" * 64


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-recover-")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n")
        os.environ.update({
            "YIBAN_ACCOUNTS_KEY": TEST_KEY,
            "YIBAN_ENV_FILE": cls.env_file,
            "YIBAN_DB_FILE": cls.db_file,
            "YIBAN_STATE_DIR": cls.tmp,
        })

    @classmethod
    def tearDownClass(cls):
        cls._close_conn()
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k in ("YIBAN_ACCOUNTS_KEY", "YIBAN_ENV_FILE", "YIBAN_DB_FILE",
                  "YIBAN_STATE_DIR"):
            os.environ.pop(k, None)

    @staticmethod
    def _close_conn():
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None

    def setUp(self):
        self._close_conn()
        for suffix in ("", "-wal", "-shm"):
            p = self.db_file + suffix
            if os.path.exists(p):
                os.remove(p)
        db.init_db(self.db_file, env_file=self.env_file, cleanup=False)

    def tearDown(self):
        self._close_conn()

    def _add(self, phone, *, vshard=0, state="pending", owner="", lease_until="",
             epoch=0, run_at=None, priority=5, day=DAY):
        conn = db.get_conn()
        conn.execute(
            "INSERT INTO sign_tasks (phone, day, vshard, owner, run_at, priority, "
            "state, attempts, lease_until, result, epoch, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (phone, day, vshard, owner, run_at or "2026-09-22 06:00:00.000", priority,
             state, 0, lease_until, "", epoch, "2026-09-22 05:00:00.000"))
        conn.commit()

    def _row(self, phone, day=DAY):
        row = db.get_conn().execute(
            "SELECT * FROM sign_tasks WHERE phone=? AND day=?", (phone, day)).fetchone()
        return dict(row) if row else None


class ReapExpiredTest(_Base):
    def test_expired_claimed_returns_to_pending(self):
        self._add("13800000001", state="claimed", owner=DEAD, lease_until=EXPIRED,
                  epoch=1)
        self.assertEqual(queue_store.reap_expired(now=NOW), 1)
        row = self._row("13800000001")
        self.assertEqual(row["state"], "pending")
        self.assertEqual(row["owner"], "", "回收必须清 owner：它不再被任何人持有")
        self.assertEqual(row["lease_until"], "", "回收必须清租约，否则下一轮又判过期")
        self.assertEqual(row["epoch"], 2, "epoch+1 是 fencing 红线（见 fencing 用例）")

    def test_unexpired_claimed_is_untouched(self):
        self._add("13800000002", state="claimed", owner=DEAD, lease_until=FRESH,
                  epoch=1)
        self.assertEqual(queue_store.reap_expired(now=NOW), 0)
        row = self._row("13800000002")
        self.assertEqual((row["state"], row["owner"], row["epoch"]),
                         ("claimed", DEAD, 1))

    def test_lease_just_expired_is_within_grace_and_untouched(self):
        """租约到期 ≠ 持有者已死：单次尝试可能比租约还慢。

        刚过期就回收，行会回退 `pending` 并可能被同一执行体重领 ⇒ 同一账号两条通道
        并发登录（`epoch+1` 只挡迟到的结论写回，挡不住这次重复真实登录）。
        """
        self._add("13800000003", state="claimed", owner=DEAD, lease_until=JUST_EXPIRED,
                  epoch=1)
        self.assertEqual(queue_store.reap_expired(now=NOW), 0)
        row = self._row("13800000003")
        self.assertEqual((row["state"], row["owner"], row["epoch"]), ("claimed", DEAD, 1))
        self.assertEqual(row["lease_until"], JUST_EXPIRED, "未回收不得改租约")

    def test_lease_expired_beyond_grace_is_reaped(self):
        """超出宽限期（150s > 120s）才认为持有者确实不在了。"""
        self._add("13800000004", state="claimed", owner=DEAD, lease_until=GRACE_EXPIRED,
                  epoch=1)
        self.assertEqual(queue_store.reap_expired(now=NOW), 1)
        row = self._row("13800000004")
        self.assertEqual(row["state"], "pending")
        self.assertEqual(row["epoch"], 2)

    def test_grace_is_at_least_two_leases(self):
        """宽限期必须 ≥ 2× 租约：一次尝试慢到两倍租约仍不该被判死。"""
        self.assertGreaterEqual(queue_store.REAP_GRACE_SEC,
                                2 * queue_store.LEASE_SECONDS)

    def test_terminal_states_are_untouched(self):
        for i, state in enumerate(("done", "failed", "skipped")):
            self._add(f"1380000001{i}", state=state, owner=DEAD, lease_until=EXPIRED,
                      epoch=1)
        self.assertEqual(queue_store.reap_expired(now=NOW), 0)
        for i, state in enumerate(("done", "failed", "skipped")):
            self.assertEqual(self._row(f"1380000001{i}")["state"], state)

    def test_historical_rows_are_never_reaped(self):
        """`vshard=-1` 的行不属于任何分片集：回收成 pending 只会永不被领取。"""
        self._add("13800000020", vshard=-1, state="claimed", owner=DEAD,
                  lease_until=EXPIRED, epoch=1)
        self.assertEqual(queue_store.reap_expired(now=NOW), 0)
        row = self._row("13800000020")
        self.assertEqual(row["state"], "claimed")
        self.assertEqual(row["epoch"], 1)

    def test_idempotent(self):
        self._add("13800000030", state="claimed", owner=DEAD, lease_until=EXPIRED,
                  epoch=1)
        self.assertEqual(queue_store.reap_expired(now=NOW), 1)
        self.assertEqual(queue_store.reap_expired(now=NOW), 0, "重跑必须 0 行")

    def test_day_filter_limits_scope(self):
        self._add("13800000040", state="claimed", owner=DEAD, lease_until=EXPIRED,
                  epoch=1)
        self._add("13800000041", state="claimed", owner=DEAD, lease_until=EXPIRED,
                  epoch=1, day="2026-09-21")
        self.assertEqual(queue_store.reap_expired(now=NOW, day=DAY), 1)
        self.assertEqual(self._row("13800000040")["state"], "pending")
        self.assertEqual(self._row("13800000041", day="2026-09-21")["state"], "claimed")

    def test_missing_table_returns_zero_with_warning(self):
        conn = db.get_conn()
        conn.execute("DROP TABLE sign_tasks")
        conn.commit()
        with self.assertLogs("yiban.store.queue_store", level="WARNING") as cm:
            self.assertEqual(queue_store.reap_expired(now=NOW), 0)
        self.assertIn("回收过期签到任务失败", "\n".join(cm.output))

    def test_unparseable_now_warns_separately_from_db_error(self):
        """`now` 不可解析是调用方入参问题，不与"库异常"共用文案（排查方向不同）。"""
        self._add("13800000050", state="claimed", owner=DEAD, lease_until=EXPIRED,
                  epoch=1)
        with self.assertLogs("yiban.store.queue_store", level="WARNING") as cm:
            self.assertEqual(queue_store.reap_expired(now="not-a-stamp"), 0)
        text = "\n".join(cm.output)
        self.assertIn("时刻参数不可解析", text)
        self.assertNotIn("回收过期签到任务失败", text)


class StealShardsTest(_Base):
    """接管者与死主是两个身份，故签名是 `steal_shards(me, dead_owner, shards, day)`。

    CAS 精确到 `owner = <dead_owner>`：只动死主的行，不误伤活着的其他执行体。
    """

    def test_takes_over_pending_rows_in_shards(self):
        self._add("13800000001", vshard=2, state="pending", owner=DEAD, epoch=1)
        self.assertEqual(queue_store.steal_shards(ME, DEAD, (2,), DAY), 1)
        row = self._row("13800000001")
        self.assertEqual(row["owner"], ME, "接管后 owner 必须是本执行体")
        self.assertEqual(row["epoch"], 2, "接管同样要 epoch+1（fencing）")
        self.assertEqual(row["state"], "pending", "只改归属，不改状态")

    def test_only_pending_rows_are_touched(self):
        self._add("13800000010", vshard=2, state="claimed", owner=DEAD, lease_until=FRESH,
                  epoch=1)
        self._add("13800000011", vshard=2, state="done", owner=DEAD, epoch=1)
        self._add("13800000012", vshard=2, state="failed", owner=DEAD, epoch=1)
        self.assertEqual(queue_store.steal_shards(ME, DEAD, (2,), DAY), 0)
        self.assertEqual(self._row("13800000010")["owner"], DEAD)
        self.assertEqual(self._row("13800000011")["owner"], DEAD)
        self.assertEqual(self._row("13800000012")["owner"], DEAD)

    def test_shards_outside_the_set_are_untouched(self):
        self._add("13800000020", vshard=5, state="pending", owner=DEAD, epoch=1)
        self.assertEqual(queue_store.steal_shards(ME, DEAD, (2, 3), DAY), 0)
        self.assertEqual(self._row("13800000020")["owner"], DEAD)

    def test_historical_rows_are_never_stolen(self):
        self._add("13800000030", vshard=-1, state="pending", owner=DEAD, epoch=1)
        self.assertEqual(queue_store.steal_shards(ME, DEAD, (-1,), DAY), 0)
        self.assertEqual(self._row("13800000030")["owner"], DEAD)

    def test_rows_owned_by_a_live_peer_are_untouched(self):
        """同一分片集里可能混着别的**活着**的执行体先接管的行：只认死主，别的一律不动。"""
        self._add("13800000040", vshard=2, state="pending", owner=DEAD, epoch=1)
        self._add("13800000041", vshard=2, state="pending", owner=LIVE, epoch=5)
        self.assertEqual(queue_store.steal_shards(ME, DEAD, (2,), DAY), 1)
        self.assertEqual(self._row("13800000040")["owner"], ME)
        live = self._row("13800000041")
        self.assertEqual((live["owner"], live["epoch"]), (LIVE, 5),
                         "活着执行体的行不得被误改（含 epoch 不得自增）")

    def test_rerun_after_takeover_is_zero(self):
        """重跑时行已归本执行体（不再是死主所有）→ CAS 不匹配 → 0 行。"""
        self._add("13800000050", vshard=2, state="pending", owner=DEAD, epoch=1)
        self.assertEqual(queue_store.steal_shards(ME, DEAD, (2,), DAY), 1)
        self.assertEqual(queue_store.steal_shards(ME, DEAD, (2,), DAY), 0, "重跑必须 0 行")
        self.assertEqual(self._row("13800000050")["epoch"], 2, "不得重复自增")

    def test_empty_shards_is_zero_without_touching_db(self):
        self._add("13800000060", vshard=2, state="pending", owner=DEAD, epoch=1)
        self.assertEqual(queue_store.steal_shards(ME, DEAD, (), DAY), 0)
        self.assertEqual(self._row("13800000060")["owner"], DEAD)

    def test_missing_table_returns_zero_with_warning(self):
        conn = db.get_conn()
        conn.execute("DROP TABLE sign_tasks")
        conn.commit()
        with self.assertLogs("yiban.store.queue_store", level="WARNING") as cm:
            self.assertEqual(queue_store.steal_shards(ME, DEAD, (2,), DAY), 0)
        self.assertIn("接管死主分片失败", "\n".join(cm.output))


class FencingAfterReapTest(_Base):
    """回收的 `epoch+1` 必须让原持有者迟到的收尾写不进去（否则结论被覆盖）。"""

    def test_late_settle_with_old_epoch_is_rejected(self):
        # 原持有者领取：owner=DEAD、epoch=1、租约已过期
        self._add("13800000001", vshard=2, state="claimed", owner=DEAD,
                  lease_until=EXPIRED, epoch=1)
        self.assertEqual(queue_store.reap_expired(now=NOW), 1)
        self.assertEqual(self._row("13800000001")["epoch"], 2)

        # 回收后本执行体重新领取同一行 → epoch=3、owner=ME
        claimed = queue_store.claim_batch(ME, DAY, (2,), now=NOW) # 走真实领取路径而不是手改行：epoch 的推进次序必须和生产一致
        self.assertEqual([c["phone"] for c in claimed], ["13800000001"])
        self.assertEqual(claimed[0]["epoch"], 3)

        # 原持有者带旧 token 的迟到收尾：不生效（0 行，结论未被改动）
        self.assertEqual(
            queue_store.settle_tasks(DEAD, DAY, [("13800000001", "迟到的旧结论")],
                                     state=queue_store.STATE_DONE, epochs={"13800000001": 1}),
            0)
        row = self._row("13800000001")
        self.assertEqual(row["state"], "claimed")
        self.assertEqual(row["result"], "")

        # 新持有者带新 token 的收尾：生效
        self.assertEqual(
            queue_store.settle_tasks(ME, DAY, [("13800000001", "新结论")],
                                     state=queue_store.STATE_DONE, epochs={"13800000001": 3}),
            1)
        self.assertEqual(self._row("13800000001")["state"], "done")


if __name__ == "__main__":
    unittest.main()
