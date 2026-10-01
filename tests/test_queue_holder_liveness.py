# -*- coding: utf-8 -*-
"""M17：租约回收的**判活按持有者身份**，不只看 `lease_until`。

标签：B · 调度：领取/队列/执行体
覆盖：
   - 存储层 `queue_store.reap_expired` 的两道持有者豁免（在途 `held` / 存活持有者
     `live_owners`）、大在途集的分块绑定、以及"豁免只压住重复登录、不吃掉崩溃恢复"；
   - 引擎层 `_Ctx.held` 的登记与摘除时点、补货循环把两道豁免真正传下去。

对应实现：yiban/store/queue_store.py（reap_expired、SQL_VAR_CHUNK、_chunks）、
   yiban/engine/executor_v3.py（_Ctx.held、_lane 的 finally 摘除、_refiller 的接线、
   _live_peer_owners）。

关键断言：**同一账号当天只能被领一次**。判据不是"跑通了"，而是数"同一个手机号被
   `attempt_signin` 打了多少次"——回收器把仍躺在本进程通道队列里的行判死、回退
   `pending`、下一次 `claim_batch` 又原地领回，就会出现两次真实登录（易班侧锁号）。
   `epoch+1` 只挡迟到的结论写回，**挡不住第二次登录**，所以 fencing 不是这里的答案。
   排队等待时间不受租约约束（等通道/等限速/等逐账号 gap/等计划时刻都能等过
   租约 60s + 宽限 120s），因此"把 lease_until 加长"也不是答案——只有按持有者判活。

依赖：临时 sqlite（sign_tasks 由 db.init_db 的迁移建表）+ 假时钟 + 打桩
   attempt_signin；不发网络请求。整文件在本机执行，无 skip。
"""
import asyncio
import contextlib
import datetime
import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import db

from yiban import clock, egress
from yiban.engine import (
    executor_v3,
    state_io,
)
from yiban.store import clock_meta, queue_store

TEST_KEY = "a" * 64
DAY = "2026-09-22"          # 周二，避开周末门
START = datetime.datetime(2026, 9, 22, 6, 40, 0)
OWNER = "single@testhost"
RUNTIME_OWNER = egress.runtime_owner(OWNER)
#: 死主的两种 owner 形态：计划行写稳定槽位名，被重排/领取过的行写运行时身份。
DEAD_STABLE = "worker-9@testhost"
DEAD_RUNTIME = egress.runtime_owner(DEAD_STABLE)
#: 租约过期且**超出宽限期**（LEASE_SECONDS 60 + REAP_GRACE_SEC 120 = 180s）
PAST_GRACE = "2026-09-22 06:36:00.000"
NOW = "2026-09-22 06:40:00.000"


def _ts(**kw):
    return (START + datetime.timedelta(**kw)).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def _phone(i):
    return "1380000%04d" % i


class _StoreBase(unittest.TestCase):
    """存储层夹具：真实 sign_tasks 表 + 固定时刻。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-holder-")
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

    def _add_claimed(self, phone, *, owner, vshard=0, lease_until=PAST_GRACE, epoch=1):
        conn = db.get_conn()
        conn.execute(
            "INSERT INTO sign_tasks (phone, day, vshard, owner, run_at, priority, "
            "state, attempts, lease_until, result, epoch, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (phone, DAY, vshard, owner, _ts(seconds=-60), 5, "claimed", 0,
             lease_until, "", epoch, _ts(minutes=-10)))
        conn.commit()

    def _row(self, phone):
        row = db.get_conn().execute(
            "SELECT * FROM sign_tasks WHERE phone=? AND day=?", (phone, DAY)).fetchone()
        return dict(row) if row else None


class InFlightExemptionTest(_StoreBase):
    """在途豁免（`held`）：本进程通道队列里的行，持有者是自己且正在被处理。"""

    def test_held_row_is_not_reaped(self):
        """在途行即使过期且超出宽限期也不回收。

        回收 = 回退 `pending` = 下一次 `claim_batch` 能原地领回 = 同一账号当天第二次
        真实登录。行还在自己进程的手上，持有者根本没死。
        """
        phone = _phone(1)
        self._add_claimed(phone, owner=RUNTIME_OWNER)
        self.assertEqual(queue_store.reap_expired(now=NOW, held={phone}), 0)
        row = self._row(phone)
        self.assertEqual((row["state"], row["owner"], row["epoch"]),
                         ("claimed", RUNTIME_OWNER, 1),
                         "在途行不得被回收：状态/持有者/epoch 都要原样")

    def test_held_exemption_still_reaps_other_rows(self):
        """豁免是**逐行**的，不是整段跳过：同一天里没在途的过期行照旧回收。

        不整段跳过会让"崩溃即卡死"的那批行永远无人回收——本条挡的只是重复登录。
        """
        held_phone, dead_phone = _phone(1), _phone(2)
        self._add_claimed(held_phone, owner=RUNTIME_OWNER)
        self._add_claimed(dead_phone, owner=DEAD_RUNTIME)
        self.assertEqual(queue_store.reap_expired(now=NOW, held={held_phone}), 1)
        self.assertEqual(self._row(held_phone)["state"], "claimed")
        self.assertEqual(self._row(dead_phone)["state"], "pending")

    def test_held_row_stays_claimed_and_cannot_be_claimed_again(self):
        """端到端的不变式：在途行不会被回收 ⇒ `claim_batch` 领不到它 ⇒ 不会被领第二次。

        这是"同账号被领两次"的存储层直证：回收后的行是 `pending`，下一次
        `claim_batch` 必然把它领走（同一手机号、第二代 epoch）。
        """
        phone = _phone(3)
        self._add_claimed(phone, owner=RUNTIME_OWNER, vshard=1)
        queue_store.reap_expired(now=NOW, held={phone})
        self.assertEqual(
            queue_store.claim_batch(RUNTIME_OWNER, DAY, (1,), now=NOW), [],
            "在途行仍在库中 claimed：领取只取 pending，重复领取被这条不变式挡住")
        self.assertEqual(self._row(phone)["epoch"], 1, "没有第二次领取 ⇒ epoch 不得再进")

    def test_released_row_is_reclaimed_again(self):
        """豁免随在途登记消失而消失：`held` 不含它时按原口径回收（自愈，不是永久护身符）。

        反过来若豁免永在，本进程"已领却没登记"的泄漏行就永远卡在 `claimed`，当天该
        账号无人再签。判据是"此刻谁持有"，不是"曾经持有过"。
        """
        phone = _phone(4)
        self._add_claimed(phone, owner=RUNTIME_OWNER)
        self.assertEqual(queue_store.reap_expired(now=NOW, held={phone}), 0)
        self.assertEqual(queue_store.reap_expired(now=NOW, held=set()), 1,
                         "不在途集合后必须按租约+宽限回收（泄漏行自愈）")
        self.assertEqual(self._row(phone)["state"], "pending")

    def test_large_held_set_is_chunked_not_rejected(self):
        """在途集大于 SQLite 绑定变量上限时仍要生效。

        整段拼一条 `NOT IN` 会在变量数超限时抛 `sqlite3.ProgrammingError`，而回收失败
        只告警 ⇒ 回收静默退化成"什么都不回收"。故按 `SQL_VAR_CHUNK` 分块。
        """
        big = 1500  # 远大于老版本 SQLite 的 999 上限
        held = {_phone(i) for i in range(big)}
        for i in range(big):
            self._add_claimed(_phone(i), owner=RUNTIME_OWNER)
        self._add_claimed(_phone(big + 5), owner=DEAD_RUNTIME)
        reaped = queue_store.reap_expired(now=NOW, held=held)
        self.assertEqual(reaped, 1, "1500 个在途行一个都不能被回收，死主的必须回收")
        self.assertEqual(self._row(_phone(big + 5))["state"], "pending")
        self.assertEqual(self._row(_phone(0))["state"], "claimed")

    def test_empty_held_and_live_owners_keep_legacy_behaviour(self):
        """缺省参数逐字保持既有语义（既有调用点/既有测试不因本项改变行为）。"""
        phone = _phone(5)
        self._add_claimed(phone, owner=DEAD_STABLE)
        self.assertEqual(queue_store.reap_expired(now=NOW), 1)
        self.assertEqual(self._row(phone)["state"], "pending")


class LiveOwnerExemptionTest(_StoreBase):
    """存活持有者豁免（`live_owners`）：跨进程的同一类重复登录。"""

    def test_live_peer_runtime_identity_is_exempt(self):
        """兄弟执行体还在心跳，它手上的行不回收（跨进程同一道红线）。

        owner 列存的是运行时身份 `{稳定名}:{进程号}:{代次}`，故豁免必须前缀匹配
        运行时形态；只等值匹配稳定名（计划行的写法）会漏掉真正在飞的那种。
        """
        phone = _phone(11)
        self._add_claimed(phone, owner=DEAD_RUNTIME.replace("worker-9", "worker-3"))
        self.assertEqual(queue_store.reap_expired(now=NOW, live_owners=["worker-3@testhost"]),
                         0, "活着的兄弟持有者的行不回收")
        self.assertEqual(self._row(phone)["state"], "claimed")

    def test_live_peer_stable_name_is_exempt(self):
        """计划行形态的 owner（裸稳定名）同样豁免——两种形态都要盖住。"""
        phone = _phone(12)
        self._add_claimed(phone, owner="worker-3@testhost")
        self.assertEqual(queue_store.reap_expired(now=NOW, live_owners=["worker-3@testhost"]),
                         0)
        self.assertEqual(self._row(phone)["state"], "claimed")

    def test_underscore_hostname_is_not_a_like_wildcard(self):
        """主机名里的 `_` 是 LIKE 的通配符：用 `instr` 前缀而非 LIKE 才不会误豁免。

        误豁免的后果是"死主的行永远不回收" = 崩溃即卡死——比重复登录更难发现。
        """
        live, dead = _phone(13), _phone(14)
        self._add_claimed(live, owner="worker-3@my_test_host")
        self._add_claimed(dead, owner="worker-3@myXtestYhost")
        self.assertEqual(queue_store.reap_expired(now=NOW,
                                                  live_owners=["worker-3@my_test_host"]), 1)
        self.assertEqual(self._row(live)["state"], "claimed")
        self.assertEqual(self._row(dead)["state"], "pending")

    def test_dead_peer_is_still_reaped(self):
        """豁免名单之外的持有者照旧回收——崩溃恢复链不能被这道豁免堵死。"""
        phone = _phone(15)
        self._add_claimed(phone, owner=DEAD_RUNTIME)
        self.assertEqual(queue_store.reap_expired(now=NOW, live_owners=["worker-3@testhost"]),
                         1)
        self.assertEqual(self._row(phone)["state"], "pending")

    def test_blank_owner_entries_are_ignored(self):
        """空串/空白持有者不得当成"前缀匹配一切"（`instr(owner, ':')` 会命中所有行）。"""
        phone = _phone(16)
        self._add_claimed(phone, owner=DEAD_RUNTIME)
        self.assertEqual(queue_store.reap_expired(now=NOW, live_owners=["", "   ", None]),
                         1)
        self.assertEqual(self._row(phone)["state"], "pending")


class LivePeerOwnerTest(unittest.TestCase):
    """`_live_peer_owners` 与 `_widen_with_dead_peers` 共用同一份判活事实。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-peers-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        prev = os.environ.get("YIBAN_STATE_DIR")
        os.environ["YIBAN_STATE_DIR"] = self.tmp

        def _restore():
            if prev is None:
                os.environ.pop("YIBAN_STATE_DIR", None)
            else:
                os.environ["YIBAN_STATE_DIR"] = prev

        self.addCleanup(_restore)

    def _ctx(self, executors):
        return SimpleNamespace(cfg={"executors": list(executors)}, executor_id=OWNER)

    def test_running_peer_is_live_and_stale_peer_is_not(self):
        with mock.patch.object(executor_v3, "_now", lambda: START):
            for peer in ("worker-0@testhost", "worker-1@testhost"):
                state_io.mark_worker_started(int(peer.split("-")[1].split("@")[0]),
                                             now=START, role=egress.ROLE_WORKER)
            for peer, slot in (("worker-0@testhost", 0), ("worker-1@testhost", 1)):
                self.assertEqual(state_io.worker_presence(slot, now=START,
                                                         role=egress.ROLE_WORKER)[0],
                                 state_io.WORKER_STATE_RUNNING,
                                 f"{peer} 的心跳刚写，必须是 running（否则下面的断言在测空气）")
            self.assertEqual(
                sorted(executor_v3._live_peer_owners(self._ctx(
                    ["worker-0@testhost", "worker-1@testhost"]).cfg, OWNER)),
                ["worker-0@testhost", "worker-1@testhost"],
                "心跳新鲜的兄弟执行体都算存活 ⇒ 它的行不被回收")
        # 越过 `2 × WORKER_HEARTBEAT_SEC` 的新鲜度门但仍在**同一业务日** ⇒ `stale`
        # （判死，与 `_widen_with_dead_peers` 同一口径）。跨日会被 `worker_presence`
        # 判成 `idle`（保守侧算"活"），所以这里必须只推进秒数、不能推进年份。
        later = START + datetime.timedelta(
            seconds=2 * state_io.WORKER_HEARTBEAT_SEC + 5)
        self.assertEqual(later.strftime("%Y-%m-%d"), DAY, "推进不得跨业务日")
        with mock.patch.object(executor_v3, "_now", lambda: later):
            self.assertEqual(
                state_io.worker_presence(0, now=later, role=egress.ROLE_WORKER)[0],
                state_io.WORKER_STATE_STALE, "心跳过期必须判死，否则下面的断言在测空气")
            self.assertEqual(
                executor_v3._live_peer_owners(self._ctx(
                    ["worker-0@testhost", "worker-1@testhost"]).cfg, OWNER),
                [], "心跳过期的执行体判死 ⇒ 其行回到租约+宽限的回收路径")

    def test_own_identity_is_never_listed(self):
        """自己的身份不进名单：本进程的行由更精确的 `ctx.held` 逐行豁免。

        把自己塞进豁免名单会让"已领却没登记在途"的泄漏行永远回收不到。
        """
        with mock.patch.object(state_io, "mark_worker_started", lambda *a, **k: None), \
             mock.patch.object(state_io, "mark_worker_beat", lambda *a, **k: None), \
             mock.patch.object(executor_v3, "_now", lambda: START):
            self.assertNotIn(OWNER, executor_v3._live_peer_owners(
                self._ctx([OWNER, "worker-0@testhost"]).cfg, OWNER))

    def test_unconfigured_slot_is_treated_as_live(self):
        """当日没有记录的槽位四态是 `idle`，按保守侧算"活"（少回收，不会重复登录）。"""
        with mock.patch.object(executor_v3, "_now", lambda: START):
            self.assertEqual(
                executor_v3._live_peer_owners(
                    self._ctx(["worker-7@testhost"]).cfg, OWNER),
                ["worker-7@testhost"])


class RefillerWiringTest(unittest.TestCase):
    """补货循环把两道豁免真正传下去（钉接线，不靠读代码）。"""

    def setUp(self):
        self.cfg = {"executors": [OWNER], "bucket_rate": 100.0,
                    "avg_attempt_sec": 1, "retry_min_interval": 60,
                    "sign_start": (6, 30), "sign_end": (7, 50),
                    "edge_front_sec": 60, "edge_back_sec": 60,
                    "min_exec_gap": 1, "exec_gap_min": 1}
        self.ctx = executor_v3._Ctx(
            accounts={}, day=DAY, cfg=self.cfg, v=1, shards=(0,),
            executor_id=OWNER, results={}, cred_state={}, delegated=None,
            notify_url="", event_sink=None, rng=None, slot=0)

    def test_refill_reap_passes_held_and_live_owners(self):
        """补货循环内那次回收必须带 `held` 与 `live_owners` 两个实参，且名单**非空**。

        只断言关键字存在是不够的：把实参换成空元组，`"live_owners" in kw` 照样为真
        ——对抗复审的突变验证实测 16 条用例全绿，跨进程那一半的豁免就静默失效了。
        故 cfg 里放一个兄弟执行体并写入新鲜心跳，断言名单里真有它的稳定名。
        """
        peer = "worker-1@testhost"
        self.cfg["executors"] = [OWNER, peer]
        tmp = tempfile.mkdtemp(prefix="yiban-refill-peers-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        prev_state = os.environ.get("YIBAN_STATE_DIR")
        os.environ["YIBAN_STATE_DIR"] = tmp

        def _restore_state():
            if prev_state is None:
                os.environ.pop("YIBAN_STATE_DIR", None)
            else:
                os.environ["YIBAN_STATE_DIR"] = prev_state

        self.addCleanup(_restore_state)
        # 兄弟执行体的新鲜心跳：判活事实与 `_widen_with_dead_peers` 同一来源，
        # 用真实时刻写入，`_now()` 读到的就是 running（不是"未配置槽位算活"的保守侧）。
        state_io.mark_worker_started(1, now=datetime.datetime.now(),
                                     role=egress.ROLE_WORKER)
        self.ctx.held.add("13800000001")
        seen = []
        real = queue_store.reap_expired

        def spy(*a, **kw):
            seen.append(kw)
            return real(*a, **kw)

        import asyncio

        async def drive():
            with mock.patch.object(queue_store, "reap_expired", spy), \
                 mock.patch.object(executor_v3, "RECOVER_SEC", 0), \
                 mock.patch.object(queue_store, "pending_count", lambda *a, **k: 1):
                task = asyncio.ensure_future(executor_v3._refiller(
                    asyncio.PriorityQueue(), (0,), self.ctx))
                await asyncio.sleep(0)
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

        asyncio.run(drive())
        self.assertTrue(seen, "补货循环必须调用回收")
        self.assertTrue(any(kw.get("held") for kw in seen),
                        "回收必须收到在途集合（held），否则队列里的行会被判死重领")
        live_seen = [tuple(kw["live_owners"]) for kw in seen if kw.get("live_owners")]
        self.assertTrue(live_seen,
                        "回收必须收到**非空**的存活持有者名单（live_owners）——"
                        "只断言键存在的话，把实参换成空元组也照样通过")
        self.assertIn(peer, live_seen[0],
                      "存活兄弟执行体的稳定名必须真的进豁免名单，"
                      "否则它手上的行仍会被判死重领（同账号两次真实登录）")


class NoDoubleLoginE2ETest(unittest.TestCase):
    """**验收直证：同一账号当天只被真实登录一次**。

    走真实补货循环（`_refiller`）+ 真实通道（`_lane`）+ 真实 `claim_batch` /
    `reap_expired`，只有"发起登录"那一步打桩并让它把假时钟往前拨。

    场景就是 M17 的现场：一条通道、两个到点的账号。第一个账号的登录很慢（拨 200s
    假时钟，远超「租约 60s + 宽限 120s」），第二个账号因此**躺在本进程的通道队列里
    排队**——它没人在处理，但持有者（这个进程）好好地在跑。回收器若只看
    `lease_until`，此刻就会把第二个账号判死、回退 `pending`，下一次 `claim_batch`
    又原地领回，队列里于是躺着**两份同一个手机号** ⇒ 两条通道各登录一次。

    判据是数"每个手机号被 `attempt_signin` 打了多少次"，不是"跑通了"：
    `epoch+1` 的 fencing 会让第二次的收尾写不进去，库里看着仍然一致——但易班侧
    已经挨了两次真实登录，那才是会被锁号的事。
    """

    SLOW_SEC = 200  # > LEASE_SECONDS(60) + REAP_GRACE_SEC(120)

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-dbl-")
        self.db_file = os.path.join(self.tmp, "yiban.db")
        self.env_file = os.path.join(self.tmp, ".env")
        with open(self.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n")
        os.environ.update({
            "YIBAN_ACCOUNTS_KEY": TEST_KEY,
            "YIBAN_ENV_FILE": self.env_file,
            "YIBAN_DB_FILE": self.db_file,
            "YIBAN_STATE_DIR": self.tmp,
            "YIBAN_EXECUTOR_ID": OWNER,
        })
        self.addCleanup(self._teardown_env)
        # 必须先关旧连接再 init：同进程里别的用例类可能留着一个指向**别的**库文件的
        # 已初始化连接，`init_db` 见 `is_initialized()` 为真就直接返回，于是本用例的
        # 插入落进上一个用例的库（表现为 UNIQUE 约束冲突）。
        self._close_conn()
        db.init_db(self.db_file, env_file=self.env_file, cleanup=False)
        self.addCleanup(self._close_conn)
        self.t = START
        self.mono = 10_000.0
        self._stop = [
            mock.patch.object(executor_v3, "_now", lambda: self.t),
            mock.patch.object(executor_v3, "_mono", lambda: self.mono),
            mock.patch.object(clock, "now", lambda: self.t),
        ]
        for p in self._stop:
            p.start()
        self.addCleanup(self._stop_patches)

    def _teardown_env(self):
        for k in ("YIBAN_ACCOUNTS_KEY", "YIBAN_ENV_FILE", "YIBAN_DB_FILE",
                  "YIBAN_STATE_DIR", "YIBAN_EXECUTOR_ID"):
            os.environ.pop(k, None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _stop_patches(self):
        for p in reversed(self._stop):
            with contextlib.suppress(Exception):
                p.stop()

    @staticmethod
    def _close_conn():
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None

    async def _sleep(self, sec):
        # 假时钟：**先推进再让出**，与 `tests/test_executor_v3.py` 的 `_FakeClock` 同语义
        self.t += datetime.timedelta(seconds=sec)
        self.mono += sec
        await asyncio.sleep(0)

    def _add_pending(self, phone):
        conn = db.get_conn()
        conn.execute(
            "INSERT INTO sign_tasks (phone, day, vshard, owner, run_at, priority, "
            "state, attempts, lease_until, result, epoch, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (phone, DAY, 0, "", _ts(seconds=-30), 5, "pending", 0, "", "", 0,
             _ts(minutes=-10)))
        conn.commit()

    def test_queued_account_is_not_claimed_twice(self):
        import asyncio

        slow, queued = _phone(1), _phone(2)
        self._add_pending(slow)
        self._add_pending(queued)
        cfg = {"executors": [OWNER], "bucket_rate": 100.0, "avg_attempt_sec": 1,
               "retry_min_interval": 60, "sign_start": (6, 30), "sign_end": (7, 50),
               "edge_front_sec": 60, "edge_back_sec": 60,
               "min_exec_gap": 1, "exec_gap_min": 1}
        clock_meta.set_meta(executor_v3.V_META_KEY_PREFIX + DAY, 1)
        ctx = executor_v3._Ctx(
            accounts={p: SimpleNamespace(phone=p, password="", phone_model="",
                                         phone_code="", owner="u@" + p, id=0)
                      for p in (slow, queued)},
            day=DAY, cfg=cfg, v=1, shards=(0,), executor_id=OWNER, results={},
            cred_state={}, delegated=None, notify_url="", event_sink=None, rng=None,
            slot=0, runtime_id=RUNTIME_OWNER)
        ctx.m = 1  # 一条通道：第二个账号必然在队列里排队（多通道就撞不出排队窗口）

        logins = []

        def _signin(acc):
            logins.append(acc.phone)
            if acc.phone == slow:
                # 一次很慢的真实登录：把假时钟拨过「租约 + 宽限」，让排队中的那个
                # 账号的租约在它还没被通道取走时就过期
                self.t += datetime.timedelta(seconds=self.SLOW_SEC)
                self.mono += self.SLOW_SEC
            return (True, "ok", False, "success")

        async def drive():
            q = asyncio.PriorityQueue()
            with mock.patch.object(executor_v3, "_sleep", self._sleep), \
                 mock.patch.object(executor_v3, "RECOVER_SEC", 0), \
                 mock.patch.object(executor_v3.attempts, "attempt_signin", _signin), \
                 mock.patch.object(executor_v3, "_make_limiter",
                                   lambda n: _Permissive()), \
                 mock.patch.object(executor_v3, "_make_global_limiter", lambda: None), \
                 mock.patch.object(executor_v3, "_make_gap_gate", lambda: _Permissive()):
                lane = asyncio.ensure_future(executor_v3._lane(q, 0, ctx))
                refill = asyncio.ensure_future(executor_v3._refiller(q, (0,), ctx))
                await asyncio.gather(refill, lane)

        asyncio.run(drive())

        self.assertEqual(
            sorted(logins), sorted([slow, queued]),
            "每个手机号当天只能被真实登录一次：排队中的那个账号被回收器判死重领后，"
            "队列里会躺着两份同一个手机号 ⇒ 两条通道各登录一次（易班侧锁号）")
        self.assertEqual(ctx.held, set(), "轮次收尾时在途登记必须清空")
        for phone in (slow, queued):
            self.assertEqual(
                db.get_conn().execute(
                    "SELECT state FROM sign_tasks WHERE phone=? AND day=?",
                    (phone, DAY)).fetchone()[0], "done")


class _Permissive:
    """限速/逐账号 gap 的放行替身（本文件只关心队列语义，不引入真实限速）。"""

    def acquire(self, *a, **k):
        return True

    def bucket(self, *a, **k):
        return SimpleNamespace(retry_after=lambda now: 0.0)

    def allow(self, *a, **k):
        return True

    def commit(self, *a, **k):
        pass

    def on_success(self, *a, **k):
        pass

    def on_risk_signal(self, *a, **k):
        pass


if __name__ == "__main__":
    unittest.main()
