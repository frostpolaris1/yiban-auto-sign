# -*- coding: utf-8 -*-
"""`yiban/store/queue_store.py`：sign_tasks 的批量领取 / 批量收尾 / 重排 / 当日计数。

标签：B · 调度：领取/队列/执行体
覆盖：sign_tasks 的批量领取（分片集过滤、limit 截断、priority + run_at
   排序、不改动不相关行、领到的行落 state
   与任务级短租约、二次领取为空、空分片集不碰库）、表缺失与「只是没到期行」的告警分档、settle_tasks
   的 owner 作用域与 result 截断、requeue_task 的 priority/attempts
   递增、day_counts 与直接 SQL 的一致口径及派生键。
对应实现：yiban/store/queue_store.py（claim_batch、settle_tasks、requeue_task、day_counts）、scripts/db.py
   的建表迁移、yiban/store/claims.py 的 stats 键口径。
关键断言：「表未落地」与「表在但无到期行」必须是两件事：前者要回 `None` 哨兵并告警（调用方据此计入有界放弃闸门），后者静默，不能天天喊库坏了。limit
   截断时的取舍必须显式排序（priority 小者、同优先级 run_at 早者），因为 UPDATE
   ... RETURNING 的行序不保证。空 day（表在、当日无行）也要给出与 claims.stats
   同口径的键，调用方不得 KeyError。
依赖：临时 sqlite（每用例重建）+
   假时间戳助手；无网络请求、不起应用。整文件在本机执行，无 skip。

覆盖 claim_batch 的四条边界（分片集过滤、limit 截断、priority+run_at 排序、
不改动不相关行）、领取后的 state 与任务级短租约、settle_tasks 的 owner 作用域与
result 截断、requeue_task 的 priority/attempts 递增，以及 day_counts 与直接 SQL
计数的一致口径（空 day 不抛）。
"""
import contextlib
import datetime
import os
import re
import shutil
import tempfile
import unittest
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import db  # noqa: E402

from yiban import clock  # noqa: E402
from yiban.store import queue_store  # noqa: E402

TEST_KEY = "a" * 64


def _day(days_ago=0):
    """按业务时区构造相对业务日：`days_ago=0` 是今日，1 是昨日。

    写死日历日期的夹具会随运行日老化。本文件的窗口类断言按业务日算：
    `owners_since` 只回保留期（14 天）内的 owner，`purge` 只删保留期外的行。
    夹具日期一旦滑出窗口，用例就在某个日历日突然变红。故日期一律相对 clock 生成。
    """
    return (clock.now() - datetime.timedelta(days=days_ago)).strftime("%Y-%m-%d")


# 主夹具业务日。它必须在 14 天保留窗内，且**严格早于今日**：`owners_since(days=0)`
# 的 cutoff 是今日，只有早于今日的行才被判成窗外（见 test_owners_since_window）。
DAY = _day(1)
# 更早但仍在窗内的业务日。用于 latest_day / owners_for_day 的"存在更旧记录"场景。
OLDER_DAY = _day(3)
# 保证没有行的业务日（远期，永不被任何保留窗收纳）。空 day 计数口径用它。
EMPTY_DAY = _day(3650)
OWNER = "hostA:100:090000"
OTHER = "hostB:200:090001"
MY_SHARDS = (0, 1) # 只认领两片：7 号片的行必须原样不动，跨界领取是最贵的那类 bug
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
            # 状态目录同样要隔离：迁移会读它补账，未隔离时读到的是本机真实部署的状态文件
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

    def _add_task(self, phone, vshard=0, state="pending", run_at=None, priority=5, # 默认值就是「已到期、可领」的那一行，用例只改与断言有关的那一维
                  owner="", attempts=0, lease_until="", result="", day=DAY, epoch=0):
        conn = db.get_conn()
        conn.execute(
            "INSERT INTO sign_tasks (phone, day, vshard, owner, run_at, priority, "
            "state, attempts, lease_until, result, epoch, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (phone, day, vshard, owner, run_at or _ts(seconds=-1), priority, state,
             attempts, lease_until, result, epoch, _ts(seconds=-60)))
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
            self.assertEqual(set(r), {"phone", "run_at", "attempts", "epoch"})
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

    def test_missing_table_returns_sentinel_with_warning(self):
        """表未落地 = 队列读不通：回 `None` 哨兵**并告警**，调用方据此计入有界放弃闸门。

        `[]` 只回答"表在、但没有到期行"；用 `[]` 回答"读不通"会让执行体把库坏了
        当成"今天没活了"（全天零签到而现场只有两条 WARNING，ba-p01-01）。
        """
        conn = db.get_conn()
        conn.execute("DROP TABLE sign_tasks")
        conn.commit()
        with self.assertLogs("yiban.store.queue_store", level="WARNING") as cm:
            self.assertIsNone(queue_store.claim_batch(OWNER, DAY, MY_SHARDS, now=_ts()))
        self.assertIn("批量领取签到任务失败", "\n".join(cm.output))

    def test_no_due_rows_is_silent(self):
        """表在、只是没有到期行 ⇒ 空返回但**不告警**：与"库坏了"必须是两件事。"""
        self._add_task(_phone(1), run_at=_ts(minutes=+30))
        with mock.patch.object(queue_store.logger, "warning") as warn:
            self.assertEqual(queue_store.claim_batch(OWNER, DAY, MY_SHARDS, now=_ts()), [])
        warn.assert_not_called()


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


class ReclaimTasksTest(_Base):
    """`reclaim_tasks`：手动 `--only` 的显式重签——只翻终态、`run_at` 置现在、不动在飞。"""

    def test_terminal_rows_flip_to_pending_with_bumped_epoch(self):
        self._add_task(_phone(1), state="done", owner=OWNER, attempts=1)
        self._add_task(_phone(2), state="skipped", owner=OWNER)
        n = queue_store.reclaim_tasks(DAY, [_phone(1), _phone(2)])
        self.assertEqual(n, 2)
        for p in (_phone(1), _phone(2)):
            row = self._row(p)
            self.assertEqual(row["state"], "pending")
            self.assertEqual(row["owner"], "")
            self.assertEqual(row["lease_until"], "")
            self.assertGreater(row["epoch"], 0, "翻态必须换一代 token（fence 迟到旧代写）")

    def test_inflight_and_failed_rows_are_not_state_changed(self):
        """`claimed`（他人在飞）不改状态、不动 token；`failed` 由回炉口管，不在此翻。"""
        self._add_task(_phone(1), state="claimed", owner="other:1:1", epoch=7)
        self._add_task(_phone(2), state="failed", owner="other:1:1", epoch=3)
        queue_store.reclaim_tasks(DAY, [_phone(1), _phone(2)])
        r1 = self._row(_phone(1))
        self.assertEqual((r1["state"], r1["owner"], r1["epoch"]),
                         ("claimed", "other:1:1", 7), "在飞行不得被手动重签打断")
        r2 = self._row(_phone(2))
        self.assertEqual((r2["state"], r2["epoch"]), ("failed", 3),
                         "failed 不归重签口管（走 requeue_failed）")

    def test_historical_and_unknown_rows_are_untouched(self):
        self._add_task(_phone(1), vshard=-1, state="done")
        self._add_task(_phone(2), state="done")
        n = queue_store.reclaim_tasks(DAY, [_phone(1), _phone(2), "19999999999"])
        self.assertEqual(n, 1, "只翻在册且分片内的那一行")
        self.assertEqual(self._row(_phone(1))["state"], "done", "vshard=-1 历史行不碰")
        self.assertEqual(self._row(_phone(2))["state"], "pending")


class DisplayReadsTest(_Base):
    """展示读口径（`sign_tasks` 为唯一台账）：latest_day / owners_for_day / activity / owners_since。"""

    def test_latest_day_and_owners_for_day(self):
        self._add_task(_phone(1), state="done", owner=OWNER, day=DAY)
        self._add_task(_phone(2), state="done", owner=OWNER, day=OLDER_DAY)
        self.assertEqual(queue_store.latest_day(), DAY)
        self.assertEqual(queue_store.owners_for_day(DAY), {_phone(1): OWNER})
        self.assertEqual(queue_store.owners_for_day(EMPTY_DAY), {})

    def test_activity_folds_states_into_kpi_keys(self):
        # 同一 owner：done/skipped 归 done、claimed/stolen 归 claimed、pending/failed 归 failed
        for i, st in enumerate(("done", "skipped", "claimed", "stolen", "pending", "failed")):
            self._add_task(_phone(i + 1), state=st, owner=OWNER)
        rows = queue_store.activity(DAY)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["owner"], OWNER)
        self.assertEqual((row["done"], row["claimed"], row["failed"], row["total"]),
                         (2, 2, 2, 6))

    def test_owners_since_window(self):
        self._add_task(_phone(1), state="done", owner="worker-0@h", day=DAY)
        self._add_task(_phone(2), state="done", owner="fallback@h", day=DAY)
        self.assertEqual(queue_store.owners_since(), ["fallback@h", "worker-0@h"])
        self.assertEqual(queue_store.owners_since(days=0), [])


class PurgeTasksClockGuardTest(_Base):
    """`purge_sign_tasks`：保留期外的行删掉；系统时间前跳 >72h 跳过本轮（复用时钟守卫）。"""

    GUARD_KEY = "purge_sign_tasks_clock"

    def _add_old_row(self, phone=None):
        phone = _phone(1) if phone is None else phone
        old_day = (clock.now() - datetime.timedelta(days=20)).strftime("%Y-%m-%d")
        self._add_task(phone, day=old_day, state="done")
        return old_day

    def _seed_reference(self, value):
        conn = db.get_conn()
        with db._conn_lock:
            conn.execute("INSERT OR REPLACE INTO app_meta (key, value) VALUES (?,?)",
                         (self.GUARD_KEY, value))
            conn.commit()

    def _days(self):
        return {r["day"] for r in db.get_conn().execute(
            "SELECT day FROM sign_tasks").fetchall()}

    def test_forward_clock_jump_skips_purge(self):
        old_day = self._add_old_row()
        self._seed_reference((clock.now() - datetime.timedelta(days=10)).strftime(
            "%Y-%m-%d %H:%M:%S"))   # 参照点在 10 天前 → 本次视为前跳
        self.assertEqual(queue_store.purge(14), 0, "跳变必须跳过本轮清理")
        self.assertIn(old_day, self._days(),
                      "跳变时超期行不得被删（否则当日计划/了结事实被整删 ⇒ 重复真实登录）")

    def test_jump_only_skips_one_round(self):
        """参照点随越界一并推进 ⇒ 下一轮恢复正常清理（不需要人工重置）。"""
        old_day = self._add_old_row()
        self._seed_reference((clock.now() - datetime.timedelta(days=10)).strftime(
            "%Y-%m-%d %H:%M:%S"))
        queue_store.purge(14)
        self.assertEqual(queue_store.purge(14), 1, "参照点推进后下一轮必须恢复清理")
        self.assertNotIn(old_day, self._days())

    def test_normal_purge_keeps_window_and_deletes_older(self):
        """cutoff 前的保留、cutoff 外的删除：当日行必须留着。"""
        old_day = self._add_old_row()
        self._add_task(_phone(2), day=DAY, state="done")
        self.assertEqual(queue_store.purge(14), 1)
        self.assertNotIn(old_day, self._days())
        self.assertIn(DAY, self._days())


class DayCountsTest(_Base):
    def test_counts_match_direct_sql(self):
        for i, state in enumerate(("pending", "pending", "claimed", "done",
                                   "failed", "skipped", "stolen")):
            self._add_task(_phone(i), state=state)
        got = queue_store.day_counts(DAY)
        for state in queue_store.STATES:
            sql_n = db.get_conn().execute(
                "SELECT COUNT(*) FROM sign_tasks WHERE day=? AND state=?",
                (DAY, state)).fetchone()[0]
            self.assertEqual(got[state], sql_n, state)

    def test_derived_settled_open_total(self):
        """派生口径：done+skipped 算完、其余算未完；调用方不必自己求和。"""
        for i, state in enumerate(("pending", "pending", "claimed", "done",
                                   "failed", "skipped", "stolen")):
            self._add_task(_phone(i), state=state)
        got = queue_store.day_counts(DAY)
        self.assertEqual(got["settled"], 2, "done + skipped")
        self.assertEqual(got["open"], 5, "pending + claimed + failed + stolen")
        self.assertEqual(got["total"], 7)

    def test_stats_style_keys_present_when_day_is_empty(self):
        """空 day（表在、但没有当日行）也要给出 claims.stats 口径的键，调用方不得 KeyError。

        "读不通"不走这里：那条路径回 `None` 哨兵（见 `test_missing_table_returns_sentinel_with_warning`）。
        """
        got = queue_store.day_counts(EMPTY_DAY)
        for key in ("claimed", "done", "failed", "settled", "open", "total"):
            self.assertIn(key, got)
        self.assertEqual(got["settled"], 0)
        self.assertEqual(got["open"], 0)
        self.assertEqual(got["total"], 0)
        self.assertTrue(all(n == 0 for n in got.values()), got)


class NoHardcodedDateTest(unittest.TestCase):
    """防回归钉子：本文件的业务日必须相对 clock 构造，不得写死日历日期。

    写死日期是时间老化形状。窗口类断言按业务日算，夹具滑出窗口就在某个日历日变红。
    本断言钉住"文件内不出现写死的 YYYY-MM-DD 字面量"：夹具一旦回退成写死日期即红。
    """

    def test_no_hardcoded_calendar_date_literal(self):
        with open(__file__, encoding="utf-8") as f:
            src = f.read()
        hits = re.findall(r"\d{4}-\d{2}-\d{2}", src)
        self.assertEqual(hits, [], f"写死日历日期会随运行日老化，改用 _day() 相对构造：{hits}")


if __name__ == "__main__":
    unittest.main()
