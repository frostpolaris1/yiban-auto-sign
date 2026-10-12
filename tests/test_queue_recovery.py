# -*- coding: utf-8 -*-
"""v3 崩溃恢复的队列访问层：`queue_store.reap_expired` 与死主分片接管的领取链。

标签：B · 调度：领取/队列/执行体
覆盖：reap_expired
   的宽限期与判据分档（过期未超宽限不动、超宽限回收、未过期不动、终态与历史行不动、幂等、day
   限定作用域、表缺失与 now 不可解析分别告警）、死主分片接管的等价链（行归属改写已裁：
   并入死主分片后，死主的 `pending` 行与被回收出来的行都能被真实 `claim_batch`
   领到、领取后 owner=领取者、epoch 由领取自增；不并入则领不到）、回收与 fencing 的联动。
对应实现：yiban/store/queue_store.py（reap_expired、REAP_GRACE_SEC、claim_batch、settle_tasks）、yiban/engine/executor_v3.py
   的回收与接管接线（`_widen_with_dead_peers`：只并入分片集，不改行归属）。
关键断言：租约到期不等于持有者已死：单次尝试可能比租约还慢，刚过期就回收会让行回退
   pending 并被同一执行体重领 ⇒ 同一账号两条通道并发登录（epoch+1
   只挡迟到的结论写回，挡不住这次重复真实登录）。故宽限期必须 ≥ 2×
   租约。回收后原持有者带旧 token
   的迟到收尾必须写不进去，否则新执行体的结论被陈旧结论覆盖。vshard=-1
   的历史行不属于任何分片集，回收成 pending 只会永不被领取。
依赖：临时 sqlite（sign_tasks 由 db.init_db 的迁移建表）+
   固定时刻常量；不发网络请求、不起应用。整文件在本机执行，无 skip。

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
DEAD = "worker-9@testhost" # 稳定槽位名格式：接管链用例里充当死主（计划 owner 的真实写法）
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


class DeadShardMergeTakeoverTest(_Base):
    """死主分片接管的等价覆盖：行归属改写已裁（原 `steal_shards` 随之删除）。

    接管只剩「并入分片集」一跳，等价性用真实领取路径钉住：`claim_batch` 不筛 `owner`
    （只按 day/vshard/state/run_at 选行，领取动作本身 `SET owner=<领取者>`、`epoch+1`），
    `reap_expired` 回收时把 `owner` 清空——改写对"行能否被领到"零贡献。本类钉住：并入
    死主分片后，死主的行（含被重排回 `pending`、owner 带运行时身份的行）与回收出来的行
    都能被领到；不并入则领不到。活着执行体的行不受影响由判死层保证
    （`test_executor_v3.py::DeadPeerTakeoverSkipLiveTest`：非 `stale` 不并入）。
    """

    def test_merged_dead_peer_rows_are_claimable_and_owned_by_claimer(self):
        # 死主的行有两类 owner：计划行写稳定槽位名、被重排回 pending 的行带运行时身份
        # （`{稳定名}:{进程号}:{代次}`）——领取不筛 owner，两类都必须能被领到
        # （原"改写按前缀匹配两类 owner"的等价覆盖）。
        self._add("13800000001", vshard=2, state="pending", owner=DEAD, epoch=1)
        self._add("13800000002", vshard=2, state="pending",
                  owner=DEAD + ":4210:1", epoch=1)
        claimed = queue_store.claim_batch(ME, DAY, (2,), now=NOW)
        self.assertEqual(sorted(c["phone"] for c in claimed),
                         ["13800000001", "13800000002"],
                         "并入死主分片后，死主的两类 pending 行都必须能被真实领取路径领到")
        for phone in ("13800000001", "13800000002"):
            row = self._row(phone)
            self.assertEqual(row["state"], "claimed")
            self.assertEqual(row["owner"], ME,
                             "领取后 owner=领取者（行归属在领取时落，无单独改写步骤）")
            self.assertEqual(row["epoch"], 2, "epoch+1 由领取动作完成（fencing）")

    def test_unmerged_dead_peer_shards_are_not_claimable(self):
        """不并入死主分片 ⇒ 死主的行不在任何人的领取集内 = "崩溃即卡死"的反面直证。"""
        self._add("13800000010", vshard=5, state="pending", owner=DEAD, epoch=1)
        self.assertEqual(queue_store.claim_batch(ME, DAY, (2,), now=NOW), [])
        self.assertEqual(self._row("13800000010")["state"], "pending")
        self.assertEqual(self._row("13800000010")["owner"], DEAD)

    def test_reclaimed_rows_of_dead_peer_are_claimable_after_merge(self):
        """核心等价链：死主把待办领成 `claimed` 后崩 → 回收（owner 清空）→ 并入分片领到。

        回收出的行 `owner` 已是 `''`，能不能领到与任何 owner 改写无关——这正是
        "仅并入即可把死主分片的回收行领到"的直证。
        """
        self._add("13800000020", vshard=2, state="claimed", owner=DEAD,
                  lease_until=EXPIRED, epoch=1)
        self.assertEqual(queue_store.reap_expired(now=NOW), 1)
        self.assertEqual(self._row("13800000020")["owner"], "", "回收清 owner")
        claimed = queue_store.claim_batch(ME, DAY, (2,), now=NOW)
        self.assertEqual([c["phone"] for c in claimed], ["13800000020"])
        row = self._row("13800000020")
        self.assertEqual(row["state"], "claimed")
        self.assertEqual(row["owner"], ME, "回收行由并入分片的领取者领走")
        self.assertEqual(row["epoch"], 3)

    def test_inflight_and_terminal_rows_stay_put_after_merge(self):
        """只领 `pending` 的等价物：租约未到仍在飞的 `claimed` 行与终态行，并入死主分片后
        也领不到（在飞不抢、终态不复活）。"""
        self._add("13800000030", vshard=2, state="claimed", owner=DEAD,
                  lease_until=FRESH, epoch=1)
        self._add("13800000031", vshard=2, state="done", owner=DEAD, epoch=1)
        self._add("13800000032", vshard=2, state="failed", owner=DEAD, epoch=1)
        self.assertEqual(queue_store.claim_batch(ME, DAY, (2,), now=NOW), [])
        self.assertEqual(self._row("13800000030")["state"], "claimed")
        self.assertEqual(self._row("13800000031")["state"], "done")
        self.assertEqual(self._row("13800000032")["state"], "failed")

    def test_historical_rows_are_never_claimable(self):
        """`vshard=-1` 不属于任何分片集：挡它的是分片集本身（原 steal 的显式 -1 拦截
        之上，领取端 `vshard IN (...)` 同样挡着）。"""
        self._add("13800000040", vshard=-1, state="pending", owner=DEAD, epoch=1)
        self.assertEqual(queue_store.claim_batch(ME, DAY, (2,), now=NOW), [])
        self.assertEqual(self._row("13800000040")["state"], "pending")

    def test_reclaimed_row_is_claimed_exactly_once(self):
        """原"重跑 0 行"的等价物：接管不落行改写，幂等由领取的 state 翻转承担——
        领过即 `claimed`，同一行不会被重复领走（epoch 不得再自增）。"""
        self._add("13800000050", vshard=2, state="claimed", owner=DEAD,
                  lease_until=EXPIRED, epoch=1)
        self.assertEqual(queue_store.reap_expired(now=NOW), 1)
        first = queue_store.claim_batch(ME, DAY, (2,), now=NOW)
        self.assertEqual([c["phone"] for c in first], ["13800000050"])
        self.assertEqual(queue_store.claim_batch(ME, DAY, (2,), now=NOW), [],
                         "已领走的行不得被重复领取")
        self.assertEqual(self._row("13800000050")["epoch"], 3, "重复领取不得再自增 epoch")

    def test_empty_shards_claim_nothing(self):
        self._add("13800000060", vshard=2, state="pending", owner=DEAD, epoch=1)
        self.assertEqual(queue_store.claim_batch(ME, DAY, (), now=NOW), [])
        self.assertEqual(self._row("13800000060")["state"], "pending")


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
