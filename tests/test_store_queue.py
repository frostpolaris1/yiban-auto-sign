# -*- coding: utf-8 -*-
"""`yiban/store/queue_store.py`：sign_tasks 的批量领取 / 批量收尾 / 重排 / 当日计数。

覆盖 claim_batch 的四条边界（分片集过滤、limit 截断、priority+run_at 排序、
不改动不相关行）、领取后的 state 与任务级短租约、settle_tasks 的 owner 作用域与
result 截断、requeue_task 的 priority/attempts 递增，以及 day_counts 与直接 SQL
计数的一致口径（空 day 不抛）。
"""
import contextlib
import datetime
import os
import shutil
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import db  # noqa: E402

from yiban import clock  # noqa: E402
from yiban.store import queue_store  # noqa: E402

TEST_KEY = "a" * 64
DAY = "2026-09-22"
OWNER = "hostA:100:090000"
OTHER = "hostB:200:090001"
MY_SHARDS = (0, 1)
FOREIGN_SHARD = 7


def _ts(**kw):
    return (clock.now() + datetime.timedelta(**kw)).strftime("%Y-%m-%d %H:%M:%S")


def _phone(i):
    return f"1380000{i:04d}"


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-queue-")
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
        cls._close_conn()
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k in ("YIBAN_ACCOUNTS_KEY", "YIBAN_ENV_FILE", "YIBAN_DB_FILE"):
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

    def _add_task(self, phone, vshard=0, state="pending", run_at=None, priority=5,
                  owner="", attempts=0, lease_until="", result="", day=DAY):
        conn = db.get_conn()
        conn.execute(
            "INSERT INTO sign_tasks (phone, day, vshard, owner, run_at, priority, "
            "state, attempts, lease_until, result, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (phone, day, vshard, owner, run_at or _ts(seconds=-1), priority, state,
             attempts, lease_until, result, _ts(seconds=-60)))
        conn.commit()

    def _row(self, phone, day=DAY):
        return dict(db.get_conn().execute(
            "SELECT * FROM sign_tasks WHERE phone=? AND day=?", (phone, day)).fetchone())

    def _all(self, day=DAY):
        return {r["phone"]: dict(r) for r in db.get_conn().execute(
            "SELECT * FROM sign_tasks WHERE day=?", (day,)).fetchall()}


class ClaimBatchTest(_Base):
    def test_returns_only_my_shards_and_respects_limit(self):
        conn = db.get_conn()
        rows = []
        for i in range(40):
            rows.append((_phone(i), DAY, MY_SHARDS[i % 2], "", _ts(seconds=-1), 5,
                         "pending", 0, "", "", _ts(seconds=-60)))
        for i in range(40, 45):
            rows.append((_phone(i), DAY, FOREIGN_SHARD, "", _ts(seconds=-1), 5,
                         "pending", 0, "", "", _ts(seconds=-60)))
        conn.executemany(
            "INSERT INTO sign_tasks (phone, day, vshard, owner, run_at, priority, "
            "state, attempts, lease_until, result, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
        conn.commit()

        got = queue_store.claim_batch(OWNER, DAY, MY_SHARDS, now=_ts())
        self.assertEqual(len(got), 32, "limit 缺省 32 = 通道数 × 预取系数")
        for r in got:
            self.assertEqual(set(r), {"phone", "run_at", "attempts"})
            self.assertIn(self._row(r["phone"])["vshard"], MY_SHARDS)
        mine = {p for p, r in self._all().items() if r["state"] == "claimed"}
        self.assertEqual(mine, {r["phone"] for r in got}, "领到的恰好是返回的那些行")

    def test_selection_prefers_low_priority(self):
        """limit 截断时先领 priority 小的（0 最高）——`UPDATE ... RETURNING` 的行序不保证
        跟随子查询的 ORDER BY，故断言的是"领到了哪些行"而不是返回列表的顺序。"""
        self._add_task(_phone(1), priority=5, run_at=_ts(seconds=-30))
        self._add_task(_phone(2), priority=0, run_at=_ts(seconds=-1))
        self._add_task(_phone(3), priority=3, run_at=_ts(seconds=-10))
        got = queue_store.claim_batch(OWNER, DAY, MY_SHARDS, now=_ts(), limit=2)
        self.assertEqual({r["phone"] for r in got}, {_phone(2), _phone(3)})

    def test_selection_prefers_earlier_run_at_within_same_priority(self):
        self._add_task(_phone(1), priority=3, run_at=_ts(seconds=-5))
        self._add_task(_phone(2), priority=3, run_at=_ts(seconds=-10))
        got = queue_store.claim_batch(OWNER, DAY, MY_SHARDS, now=_ts(), limit=1)
        self.assertEqual([r["phone"] for r in got], [_phone(2)])

    def test_claimed_rows_get_state_and_short_lease(self):
        self._add_task(_phone(1))
        before = clock.now()
        got = queue_store.claim_batch(OWNER, DAY, MY_SHARDS, now=_ts())
        self.assertEqual([r["phone"] for r in got], [_phone(1)])
        row = self._row(_phone(1))
        self.assertEqual(row["state"], "claimed")
        self.assertEqual(row["owner"], OWNER)
        lease = datetime.datetime.strptime(row["lease_until"], "%Y-%m-%d %H:%M:%S.%f")
        expected = before + datetime.timedelta(seconds=60)
        self.assertLess(abs((lease - expected).total_seconds()), 2,
                        "任务级短租约应为 now+60s（±2s）")

    def test_untouched_rows_keep_original_values(self):
        self._add_task(_phone(1))                                    # 我的：会被领
        self._add_task(_phone(2), vshard=FOREIGN_SHARD)              # 他人分片
        self._add_task(_phone(3), run_at=_ts(minutes=+30))           # 未到期
        self._add_task(_phone(4), state="claimed", owner=OTHER)      # 非 pending
        untouched = {p: self._row(p) for p in (_phone(2), _phone(3), _phone(4))}
        queue_store.claim_batch(OWNER, DAY, MY_SHARDS, now=_ts())
        for p, before in untouched.items():
            self.assertEqual(self._row(p), before, f"{p} 不应被领取改动")

    def test_second_call_returns_empty(self):
        self._add_task(_phone(1))
        self.assertEqual(len(queue_store.claim_batch(OWNER, DAY, MY_SHARDS, now=_ts())), 1)
        self.assertEqual(queue_store.claim_batch(OWNER, DAY, MY_SHARDS, now=_ts()), [])

    def test_empty_vshards_returns_empty(self):
        self._add_task(_phone(1))
        self.assertEqual(queue_store.claim_batch(OWNER, DAY, (), now=_ts()), [])


class SettleTasksTest(_Base):
    def test_batch_settle_is_owner_scoped_and_truncates_result(self):
        for i in range(33):
            self._add_task(_phone(i), state="claimed", owner=OWNER,
                           lease_until=_ts(seconds=+30))
        self._add_task(_phone(900), state="claimed", owner=OTHER, lease_until=_ts(seconds=+30))
        long_result = "签到失败" + "x" * 300
        outcomes = [(_phone(i), "签到成功") for i in range(32)] + [(_phone(32), long_result)]
        n = queue_store.settle_tasks(OWNER, DAY, outcomes)
        self.assertEqual(n, 33)
        for i in range(33):
            row = self._row(_phone(i))
            self.assertEqual(row["state"], "done")
            self.assertEqual(row["lease_until"], "")
            self.assertNotEqual(row["result"], "")
        self.assertEqual(len(self._row(_phone(32))["result"]), 200, "result 截断 200 字")
        foreign = self._row(_phone(900))
        self.assertEqual(foreign["state"], "claimed", "他人 owner 的行不得被改")
        self.assertEqual(foreign["result"], "")

    def test_empty_outcomes_is_noop(self):
        self._add_task(_phone(1), state="claimed", owner=OWNER)
        self.assertEqual(queue_store.settle_tasks(OWNER, DAY, []), 0)
        self.assertEqual(self._row(_phone(1))["state"], "claimed")


class RequeueTaskTest(_Base):
    def test_increments_priority_and_attempts(self):
        self._add_task(_phone(1), state="claimed", owner=OWNER, priority=5, attempts=2,
                       lease_until=_ts(seconds=+30), result="上次失败")
        new_run_at = _ts(minutes=+1)
        n = queue_store.requeue_task(_phone(1), DAY, new_run_at)
        self.assertEqual(n, 1)
        row = self._row(_phone(1))
        self.assertEqual(row["run_at"], new_run_at)
        self.assertEqual(row["priority"], 6, "priority 恰好 +1")
        self.assertEqual(row["attempts"], 3, "attempts 恰好 +1")
        self.assertEqual(row["state"], "pending")
        self.assertEqual(row["lease_until"], "")

    def test_priority_delta_is_honoured(self):
        self._add_task(_phone(1), state="pending", priority=5)
        queue_store.requeue_task(_phone(1), DAY, _ts(minutes=+1), priority_delta=3)
        self.assertEqual(self._row(_phone(1))["priority"], 8)


class DayCountsTest(_Base):
    def test_counts_match_direct_sql(self):
        for i, state in enumerate(("pending", "pending", "claimed", "done",
                                   "failed", "skipped", "stolen")):
            self._add_task(_phone(i), state=state)
        got = queue_store.day_counts(DAY)
        for state, n in got.items():
            sql_n = db.get_conn().execute(
                "SELECT COUNT(*) FROM sign_tasks WHERE day=? AND state=?",
                (DAY, state)).fetchone()[0]
            self.assertEqual(n, sql_n, state)

    def test_empty_day_returns_zeros_without_raising(self):
        got = queue_store.day_counts("2026-01-01")
        self.assertTrue(got)
        self.assertTrue(all(n == 0 for n in got.values()), got)


if __name__ == "__main__":
    unittest.main()
