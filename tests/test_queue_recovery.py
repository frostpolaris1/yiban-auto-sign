# -*- coding: utf-8 -*-
"""v3 崩溃恢复的队列访问层：`queue_store.reap_expired` 与 `queue_store.steal_shards`。

覆盖（对应简报 ⑤ 的恢复部分）：
5. `reap_expired`：租约**过期且超出宽限期**的 `claimed` → `pending` + 清
   `owner`/`lease_until` + `epoch+1`；刚过期但仍在宽限期内（可能还在飞）的**不动**；
   未过期的**不动**；终态不动；`vshard=-1` 的历史行不动；幂等；库异常 → 0 且不抛；
6. `steal_shards`：只动「分片集内 + `state='pending'` + 非本执行体所有」的行；
   不动 `claimed`/终态/历史行；`epoch+1`；`owner` 已是自己时不计入；`vshards=()` → 0；
7. **fencing 联动**：被回收（`epoch+1`）后，原持有者带旧 epoch 的 `settle_tasks` 不生效。

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
DEAD = "worker-9@testhost"
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


class StealShardsTest(_Base):
    def test_takes_over_pending_rows_in_shards(self):
        self._add("13800000001", vshard=2, state="pending", owner=DEAD, epoch=1)
        self.assertEqual(queue_store.steal_shards(ME, (2,), DAY), 1)
        row = self._row("13800000001")
        self.assertEqual(row["owner"], ME, "接管后 owner 必须是本执行体")
        self.assertEqual(row["epoch"], 2, "接管同样要 epoch+1（fencing）")
        self.assertEqual(row["state"], "pending", "只改归属，不改状态")

    def test_only_pending_rows_are_touched(self):
        self._add("13800000010", vshard=2, state="claimed", owner=DEAD, lease_until=FRESH,
                  epoch=1)
        self._add("13800000011", vshard=2, state="done", owner=DEAD, epoch=1)
        self._add("13800000012", vshard=2, state="failed", owner=DEAD, epoch=1)
        self.assertEqual(queue_store.steal_shards(ME, (2,), DAY), 0)
        self.assertEqual(self._row("13800000010")["owner"], DEAD)
        self.assertEqual(self._row("13800000011")["owner"], DEAD)
        self.assertEqual(self._row("13800000012")["owner"], DEAD)

    def test_shards_outside_the_set_are_untouched(self):
        self._add("13800000020", vshard=5, state="pending", owner=DEAD, epoch=1)
        self.assertEqual(queue_store.steal_shards(ME, (2, 3), DAY), 0)
        self.assertEqual(self._row("13800000020")["owner"], DEAD)

    def test_historical_rows_are_never_stolen(self):
        self._add("13800000030", vshard=-1, state="pending", owner=DEAD, epoch=1)
        self.assertEqual(queue_store.steal_shards(ME, (-1,), DAY), 0)
        self.assertEqual(self._row("13800000030")["owner"], DEAD)

    def test_rows_already_owned_by_me_are_not_counted(self):
        """CAS 语义：owner 已是自己（上一次已接管）则不计入，重跑返回 0。"""
        self._add("13800000040", vshard=2, state="pending", owner=DEAD, epoch=1)
        self.assertEqual(queue_store.steal_shards(ME, (2,), DAY), 1)
        self.assertEqual(queue_store.steal_shards(ME, (2,), DAY), 0, "重跑必须 0 行")
        self.assertEqual(self._row("13800000040")["epoch"], 2, "不得重复自增")

    def test_empty_shards_is_zero_without_touching_db(self):
        self._add("13800000050", vshard=2, state="pending", owner=DEAD, epoch=1)
        self.assertEqual(queue_store.steal_shards(ME, (), DAY), 0)
        self.assertEqual(self._row("13800000050")["owner"], DEAD)

    def test_missing_table_returns_zero_with_warning(self):
        conn = db.get_conn()
        conn.execute("DROP TABLE sign_tasks")
        conn.commit()
        with self.assertLogs("yiban.store.queue_store", level="WARNING") as cm:
            self.assertEqual(queue_store.steal_shards(ME, (2,), DAY), 0)
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
        claimed = queue_store.claim_batch(ME, DAY, (2,), now=NOW)
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
