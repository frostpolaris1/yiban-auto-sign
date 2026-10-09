# -*- coding: utf-8 -*-
"""ba-p03-01（critical）：回收豁免名册必须覆盖**全部在途持有者**，含兜底常驻身份。

标签：B · 调度：领取/队列/执行体

问题：`_live_peer_owners` 只遍历 `cfg["executors"]`（并行槽位名 / 无清单时的 `single@host`），
而兜底常驻身份 `egress.fallback_owner()` 从不在该候选集里。兜底与定时轮并发时，兜底手上
租约过期（并超出宽限）的 `claimed` 行不被豁免 → 被回退 `pending` → 同轮 `claim_batch`
原地领回 → **同一账号当天两次真实登录**（易班侧锁号，M17 第一红线）。

判据（预期行为）：
  1. 兜底身份在跑（心跳新鲜）且名下有 `claimed` 行 ⇒ 那些行**不得**被回收；
  2. 兜底身份确实已死（心跳 `stale`：有开始、无收尾、过期）⇒ 其行**必须**被回收；
     判活口径是「**非 `stale` 即活**」——`running`、`finished`、`idle` 都算活，只有
     `stale` 算死。`idle`（当日无该身份心跳记录）算活是**保守侧**：少回收 = 不会误杀
     活持有者、不会造成重复登录（见 `NoHeartbeatCountsAsLiveTest`）；
  3. 并行槽位身份 / 单执行体身份的既有行为不得退化；
  4. 本执行体**自己历史代次**遗留的行仍必须被本进程回收（"重启即卡死"不得回归）；
  5. 名册读失败（`claimed_owners` 回 `None`）⇒ 本轮**跳过回收**（fail-closed），不得
     拿空名册去回收（空名册会把所有超期行都判死，见 `RosterReadFailureTest`）。

断言一律打在**库里的行状态 / owner / epoch** 上，不停在函数返回值。回收入口取执行体真正
使用的那两步：`queue_store.reap_expired(..., live_owners=executor_v3._live_row_owners(...))`；
另有一条用例驱动真实补货循环 `_refiller` 钉接线。

依赖：临时 sqlite（`db.init_db` 建 `sign_tasks`）+ 临时状态目录 + 假时钟。不发网络请求。
"""
import contextlib
import datetime
import os
import shutil
import tempfile
import unittest
from unittest import mock

import db

from yiban import clock, egress
from yiban.engine import executor_v3, state_io
from yiban.store import queue_store

TEST_KEY = "a" * 64
DAY = "2026-09-22"                       # 周二，避开周末门
NOW = datetime.datetime(2026, 9, 22, 6, 40, 0)
NOW_STAMP = NOW.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
#: 租约过期且**超出宽限期**（LEASE_SECONDS 60 + REAP_GRACE_SEC 120 = 180s）
PAST_GRACE = "2026-09-22 06:36:00.000"

#: 本测试里的"回收者"（定时轮 / 并行执行体），与兜底并发。
REAPER = "worker-0@testhost"
REAPER_RUNTIME = egress.runtime_owner(REAPER, pid=1111, gen="110000")
#: 兜底常驻身份的运行时串（写进 sign_tasks.owner 的形态）。
FALLBACK_STABLE = egress.fallback_owner("testhost")
FALLBACK_RUNTIME = egress.runtime_owner(FALLBACK_STABLE, pid=2222, gen="220000")
#: 单执行体身份（无清单形态）。
SINGLE = egress.single_owner("testhost")
SINGLE_RUNTIME = egress.runtime_owner(SINGLE, pid=3333, gen="330000")
#: 并行兄弟槽位。
PEER = "worker-3@testhost"
PEER_RUNTIME = egress.runtime_owner(PEER, pid=4444, gen="440000")


def _ts(**kw):
    return (NOW + datetime.timedelta(**kw)).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def _phone(i):
    return "1380000%04d" % i


#: `_drive_refiller(claimed_owners=...)` 的"未注入"哨兵（`None` 是合法的注入值——
#: 它正是"名册读失败"的返回值，不能与"不注入"混为一谈）。
_UNSET = object()


class _QueueFixture(unittest.TestCase):
    """真实 `sign_tasks` 表 + 真实状态目录 + 固定时刻。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-fbex-")
        self.db_file = os.path.join(self.tmp, "yiban.db")
        self.env_file = os.path.join(self.tmp, ".env")
        with open(self.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n")
        self._prev_env = {k: os.environ.get(k) for k in
                          ("YIBAN_ACCOUNTS_KEY", "YIBAN_ENV_FILE", "YIBAN_DB_FILE",
                           "YIBAN_STATE_DIR")}
        os.environ.update({
            "YIBAN_ACCOUNTS_KEY": TEST_KEY,
            "YIBAN_ENV_FILE": self.env_file,
            "YIBAN_DB_FILE": self.db_file,
            "YIBAN_STATE_DIR": self.tmp,
        })
        self.addCleanup(self._teardown)
        self._close_conn()
        db.init_db(self.db_file, env_file=self.env_file, cleanup=False)
        self.addCleanup(self._close_conn)
        self._patches = [
            mock.patch.object(executor_v3, "_now", lambda: NOW),
            mock.patch.object(clock, "now", lambda: NOW),
        ]
        for p in self._patches:
            p.start()
        self.addCleanup(self._stop_patches)

    def _stop_patches(self):
        for p in reversed(self._patches):
            with contextlib.suppress(Exception):
                p.stop()

    def _teardown(self):
        self._close_conn()
        for k, v in self._prev_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def _close_conn():
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None

    # ---- 夹具动作 ----
    def _add_claimed(self, phone, *, owner, vshard=0, lease_until=PAST_GRACE, epoch=1):
        conn = db.get_conn()
        conn.execute(
            "INSERT INTO sign_tasks (phone, day, vshard, owner, run_at, priority, "
            "state, attempts, lease_until, result, epoch, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (phone, DAY, vshard, owner, _ts(minutes=-60), 5, "claimed", 0,
             lease_until, "", epoch, _ts(minutes=-90)))
        conn.commit()

    def _row(self, phone):
        row = db.get_conn().execute(
            "SELECT * FROM sign_tasks WHERE phone=? AND day=?", (phone, DAY)).fetchone()
        return dict(row) if row else None

    def _assert_claimed_untouched(self, phone, owner, epoch=1):
        row = self._row(phone)
        self.assertEqual(
            (row["state"], row["owner"], row["epoch"]), ("claimed", owner, epoch),
            "在途持有者活着的行不得被回收：状态/持有者/epoch 都要原样")

    def _assert_reaped(self, phone):
        row = self._row(phone)
        self.assertEqual((row["state"], row["owner"], row["epoch"]), ("pending", "", 2),
                         "死主的行必须被回退 pending（清 owner、epoch+1）")

    def _recover(self, executor_id):
        """执行体真实使用的回收入口：名册 + 回收（与两处调用点同一两步）。"""
        return queue_store.reap_expired(
            now=NOW_STAMP, day=DAY, held=set(),
            live_owners=executor_v3._live_row_owners(DAY, executor_id))

    def _heartbeat(self, slot, role, *, age_sec=0):
        state_io.mark_worker_started(
            slot, now=NOW - datetime.timedelta(seconds=age_sec), role=role)

    def _started_long_ago(self, slot, role):
        """写一个已过新鲜度门的心跳：`stale`（有开始、无收尾、心跳过期）。"""
        self._heartbeat(slot, role, age_sec=2 * state_io.WORKER_HEARTBEAT_SEC + 5)

    def _refiller_cfg_ctx(self):
        """补货循环用例的最小 `cfg` + `ctx`（与生产同构：一条分片、本体为回收者）。"""
        cfg = {"executors": [REAPER], "bucket_rate": 100.0, "avg_attempt_sec": 1,
               "retry_min_interval": 60, "sign_start": (6, 30), "sign_end": (7, 50),
               "edge_front_sec": 60, "edge_back_sec": 60,
               "min_exec_gap": 1, "exec_gap_min": 1}
        ctx = executor_v3._Ctx(
            accounts={}, day=DAY, cfg=cfg, v=1, shards=(0,), executor_id=REAPER,
            results={}, cred_state={}, delegated=None, notify_url="", event_sink=None,
            rng=None, slot=0, runtime_id=REAPER_RUNTIME)
        return cfg, ctx

    def _drive_refiller(self, *, claimed_owners=_UNSET):
        """驱动真实补货循环 `_refiller` 一拍，返回 `(ctx, reap_kwargs, roster_calls)`。

        它捕获循环调用 `reap_expired` 时的 kwargs。`RECOVER_SEC` 置 0，故首拍即进入
        回收分支。有界等待：捕到回收调用（或名册被读）为止，再让几拍，然后取消。

        `claimed_owners` 显式给出（含 `None`）时，把 `queue_store.claimed_owners` 换成
        返回该值的替身——用于模拟名册来源异常（`None` = 读失败哨兵）。
        """
        import asyncio
        _, ctx = self._refiller_cfg_ctx()
        seen = []
        roster_calls = []
        real = queue_store.reap_expired

        def spy(*a, **kw):
            seen.append(kw)
            return real(*a, **kw)

        async def drive():
            patches = [
                mock.patch.object(queue_store, "reap_expired", spy),
                mock.patch.object(executor_v3, "RECOVER_SEC", 0),
                mock.patch.object(queue_store, "pending_count", lambda *a, **k: 1),
            ]
            if claimed_owners is not _UNSET:
                value = claimed_owners

                def _roster(*a, **k):
                    roster_calls.append(1)
                    return value

                patches.append(mock.patch.object(queue_store, "claimed_owners", _roster))
            with contextlib.ExitStack() as stack:
                for p in patches:
                    stack.enter_context(p)
                task = asyncio.ensure_future(executor_v3._refiller(
                    asyncio.PriorityQueue(), (0,), ctx))
                for _ in range(50):
                    await asyncio.sleep(0)
                    if seen or roster_calls:
                        break
                # 再给几拍：确认"该跳过的回收"确实没有被调用（而非只是还没跑到）
                for _ in range(10):
                    await asyncio.sleep(0)
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

        asyncio.run(drive())
        return ctx, seen, roster_calls


class FallbackExemptRedlineTest(_QueueFixture):
    """红线：兜底身份在跑时，它名下的在途行不得被回收。"""

    def test_live_fallback_row_is_not_reaped(self):
        """兜底心跳新鲜 ⇒ 它的 `claimed` 行即使过期超宽限也不回收。"""
        phone = _phone(1)
        self._add_claimed(phone, owner=FALLBACK_RUNTIME, vshard=1)
        self._heartbeat(0, egress.ROLE_FALLBACK)
        self.assertEqual(state_io.worker_presence(
            0, now=NOW, role=egress.ROLE_FALLBACK)[0],
            state_io.WORKER_STATE_RUNNING, "兜底心跳刚写，必须 running（否则断言在测空气）")

        self.assertEqual(self._recover(REAPER), 0, "兜底的活行不得被回收")
        self._assert_claimed_untouched(phone, FALLBACK_RUNTIME)

    def test_live_fallback_row_cannot_be_claimed_again(self):
        """端到端不变式：行仍在 `claimed` ⇒ `claim_batch` 领不到它 ⇒ 不会二次真实登录。"""
        phone = _phone(2)
        self._add_claimed(phone, owner=FALLBACK_RUNTIME, vshard=1)
        self._heartbeat(0, egress.ROLE_FALLBACK)

        self._recover(REAPER)
        self.assertEqual(
            queue_store.claim_batch(REAPER_RUNTIME, DAY, (1,), now=NOW_STAMP), [],
            "在途行仍在库中 claimed：领取只取 pending，二次领取被这条不变式挡住")
        self.assertEqual(self._row(phone)["epoch"], 1, "没有第二次领取 ⇒ epoch 不得再进")

    def test_fallback_row_survives_real_refiller(self):
        """真实补货循环 `_refiller` 走一遍：它把兜底稳定名真的传进了豁免名册。

        这是"接线直证"：只断言 `_live_row_owners` 的返回值不够——调用点若仍按清单
        枚举（旧实现），名册里不会有 `fallback@testhost`，这条用例会红。
        """
        phone = _phone(3)
        self._add_claimed(phone, owner=FALLBACK_RUNTIME, vshard=0)
        self._heartbeat(0, egress.ROLE_FALLBACK)

        _ctx, seen, _roster_calls = self._drive_refiller()
        self.assertTrue(seen, "补货循环必须调用回收")
        self.assertIn(FALLBACK_STABLE, seen[0].get("live_owners") or (),
                      "兜底常驻的稳定名必须真的进豁免名册（旧实现只枚举清单，包不住它）")
        self._assert_claimed_untouched(phone, FALLBACK_RUNTIME)


class RosterReadFailureTest(_QueueFixture):
    """名册来源异常：`claimed_owners` 读失败回 `None` ⇒ 本轮跳过回收（fail-closed）。

    名册读失败若退化成空集，**所有**超期 `claimed` 行都会失去豁免、被判死回收 ⇒
    活持有者手上的行被回退 `pending`、下一轮被原地领回 ⇒ 同账号当天二次真实登录。
    故"读不到"必须与"确实没有在途持有者"区分开，调用方据此跳过本轮回收。
    """

    def test_roster_read_failure_skips_reap(self):
        """真实补货循环：名册读失败时不得回收任何行。"""
        phone = _phone(4)
        self._add_claimed(phone, owner=FALLBACK_RUNTIME, vshard=0)
        self._heartbeat(0, egress.ROLE_FALLBACK)

        _ctx, seen, roster_calls = self._drive_refiller(claimed_owners=None)
        self.assertTrue(roster_calls, "补货循环必须（尝试）读名册，否则断言在测空气")
        self.assertTrue(seen, "补货循环进入了回收分支")
        self.assertTrue(all(kw.get("live_owners") is None for kw in seen),
                        "名册读失败必须原样传 None（不得退化成空名册）")
        self._assert_claimed_untouched(
            phone, FALLBACK_RUNTIME)  # 名册不可用 ⇒ 无一被回收（fail-closed）


class FallbackCrashRecoveryTest(_QueueFixture):
    """反向：兜底确实已死（`stale`）⇒ 它的行必须被回收（崩溃恢复不得被豁免堵死）。"""

    def test_stale_fallback_row_is_reaped(self):
        """兜底心跳 `stale`（有开始、无收尾、过期）⇒ 行被回退 pending。"""
        phone = _phone(11)
        self._add_claimed(phone, owner=FALLBACK_RUNTIME)
        self._started_long_ago(0, egress.ROLE_FALLBACK)
        self.assertEqual(state_io.worker_presence(
            0, now=NOW, role=egress.ROLE_FALLBACK)[0],
            state_io.WORKER_STATE_STALE, "心跳过期必须判死，否则断言在测空气")

        self.assertEqual(self._recover(REAPER), 1, "死兜底的行必须被回收")
        self._assert_reaped(phone)


class NoHeartbeatCountsAsLiveTest(_QueueFixture):
    """保守侧：当日**没有可用心跳**的持有者也算「活」⇒ 其行当日不回收。

    判活口径是「**非 `stale` 即活**」。`worker_presence` 在三种情形都回 `idle`（无当日
    记录）：进程没跑 / 心跳写失败 / 心跳记录属别的业务日。三者一律算活，不回收。

    `idle` 算活是**保守侧**：少回收 = 不会误杀活持有者、不会造成重复登录。这挡住两条
    红线：①心跳写失败（磁盘满 / 权限错 / 状态目录被换）时活持有者仍被判死回收；
    ②长轮次跨午夜后兄弟持有者的心跳文件仍写着前一日，被判死回收。两条都会导致同一账号
    当天二次真实登录（易班侧锁号）。

    遗留（如实登记）：持有者真死且心跳文件缺失时，其行当日不被回收，该账号当日漏签；
    只有 `stale`（有开始、无收尾、过期）才回收。红线（二次登录）严于漏签，故取保守侧。
    """

    def test_missing_heartbeat_row_is_exempt(self):
        """当日**无**该身份心跳（进程没跑 / 心跳文件丢失）⇒ 行不回收。"""
        phone = _phone(12)
        self._add_claimed(phone, owner=FALLBACK_RUNTIME)
        # 不写任何心跳文件
        self.assertEqual(state_io.worker_presence(
            0, now=NOW, role=egress.ROLE_FALLBACK)[0],
            state_io.WORKER_STATE_IDLE, "无记录必须是 idle（否则断言在测空气）")

        self.assertEqual(self._recover(REAPER), 0, "无心跳的持有者按活处理，其行不得被回收")
        self._assert_claimed_untouched(phone, FALLBACK_RUNTIME)

    def test_heartbeat_write_failure_row_is_exempt(self):
        """持有者活着但心跳**写失败**（`OSError`）⇒ presence 回 idle ⇒ 行不回收。

        `mark_worker_started` / `mark_worker_beat` 把写盘 `OSError` 只记 debug、不抛
        （心跳不该影响签到）。磁盘满 / 状态目录被换 / 权限错都会走到这里：活持有者写不出
        心跳，若把 idle 判死就会被回收 ⇒ 同账号二次登录（第一红线）。
        """
        phone = _phone(13)
        self._add_claimed(phone, owner=FALLBACK_RUNTIME, vshard=1)
        with mock.patch.object(state_io, "_write_private_json",
                               side_effect=OSError("disk full")):
            state_io.mark_worker_started(0, now=NOW, role=egress.ROLE_FALLBACK)
        self.assertEqual(state_io.worker_presence(
            0, now=NOW, role=egress.ROLE_FALLBACK)[0],
            state_io.WORKER_STATE_IDLE, "心跳写失败后无文件 ⇒ idle（否则断言在测空气）")

        self.assertEqual(self._recover(REAPER), 0, "心跳写失败的活持有者其行不得被回收")
        self._assert_claimed_untouched(phone, FALLBACK_RUNTIME)

    def test_cross_midnight_row_is_exempt(self):
        """跨业务日长轮次：心跳记录属前一业务日 ⇒ presence 回 idle ⇒ 行不回收。

        `mark_worker_beat` 只刷 `ts`、不刷 `day`。轮次跨过午夜后，兄弟持有者的心跳文件
        仍写着前一业务日；`worker_presence` 见"记录属别的业务日"即回 `idle`。「非 stale
        即活」把 idle 当活，故长轮次的持有者不被误判死、其行当日不被回收。
        """
        phone = _phone(14)
        self._add_claimed(phone, owner=FALLBACK_RUNTIME, vshard=1)
        # 轮次在前一日 23:50 起跑；跨午夜后只刷 ts（mark_worker_beat 不刷 day）。
        before_midnight = NOW.replace(hour=23, minute=50, second=0, microsecond=0)
        after_midnight = before_midnight + datetime.timedelta(minutes=19)  # 次日 00:09
        self.assertEqual(after_midnight.strftime("%Y-%m-%d") == DAY, False,
                         "跨午夜用例必须真的换了业务日（否则断言在测空气）")
        state_io.mark_worker_started(0, now=before_midnight, role=egress.ROLE_FALLBACK)
        state_io.mark_worker_beat(0, now=after_midnight, role=egress.ROLE_FALLBACK)

        with mock.patch.object(executor_v3, "_now", lambda: after_midnight):
            self.assertEqual(state_io.worker_presence(
                0, now=after_midnight, role=egress.ROLE_FALLBACK)[0],
                state_io.WORKER_STATE_IDLE,
                "心跳记录属别的业务日 ⇒ idle（否则断言在测空气）")
            self.assertEqual(self._recover(REAPER), 0, "跨午夜的活持有者其行不得被回收")
        self._assert_claimed_untouched(phone, FALLBACK_RUNTIME)


class PeerBehaviourUnchangedTest(_QueueFixture):
    """回归：并行槽位身份 / 单执行体身份的既有行为不得退化。"""

    def test_live_parallel_peer_row_is_exempt(self):
        phone = _phone(21)
        self._add_claimed(phone, owner=PEER_RUNTIME)
        self._heartbeat(3, egress.ROLE_WORKER)
        self.assertEqual(self._recover(REAPER), 0)
        self._assert_claimed_untouched(phone, PEER_RUNTIME)

    def test_stale_parallel_peer_row_is_reaped(self):
        phone = _phone(22)
        self._add_claimed(phone, owner=PEER_RUNTIME)
        self._started_long_ago(3, egress.ROLE_WORKER)
        self.assertEqual(self._recover(REAPER), 1)
        self._assert_reaped(phone)

    def test_finished_parallel_peer_row_is_exempt(self):
        """`finished`（当轮正常收尾）不算死：与既有"非 stale 即活"的口径一致。"""
        phone = _phone(23)
        self._add_claimed(phone, owner=PEER_RUNTIME)
        state_io.mark_worker_started(3, now=NOW, role=egress.ROLE_WORKER)
        state_io.mark_worker_finished(3, exit_code=0, now=NOW, role=egress.ROLE_WORKER)
        self.assertEqual(self._recover(REAPER), 0)
        self._assert_claimed_untouched(phone, PEER_RUNTIME)

    def test_live_single_row_is_exempt(self):
        phone = _phone(24)
        self._add_claimed(phone, owner=SINGLE_RUNTIME)
        self._heartbeat(0, egress.ROLE_SINGLE)
        self.assertEqual(self._recover(REAPER), 0)
        self._assert_claimed_untouched(phone, SINGLE_RUNTIME)

    def test_stale_single_row_is_reaped(self):
        phone = _phone(25)
        self._add_claimed(phone, owner=SINGLE_RUNTIME)
        self._started_long_ago(0, egress.ROLE_SINGLE)
        self.assertEqual(self._recover(REAPER), 1)
        self._assert_reaped(phone)

    def test_own_previous_generation_row_is_reaped(self):
        """**重启即卡死**不得回归：本进程自己**上一代**遗留的行必须被本进程回收。

        回收者是 `single@testhost`（本进程心跳新鲜），行属于同一稳定槽位的**旧代次**
        （`single@testhost:999:000001`）。名册必须按**稳定槽位名**排除自己，而不是只排除
        本代运行时串——否则旧代次的行会被判成"活着的自己"，永远回收不到。
        """
        phone = _phone(26)
        old_gen = egress.runtime_owner(SINGLE, pid=999, gen="000001")
        self._add_claimed(phone, owner=old_gen)
        self._heartbeat(0, egress.ROLE_SINGLE)  # 本进程（回收者）心跳新鲜

        self.assertEqual(self._recover(SINGLE), 1, "自己旧代次的行必须被回收")
        self._assert_reaped(phone)


if __name__ == "__main__":
    unittest.main()
