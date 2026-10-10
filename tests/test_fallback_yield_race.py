# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""3ukk 校验者：兜底让位判据的**启动竞态**（兜底先起、主进程后持锁）。

标签：B · 调度：领取/队列/执行体
覆盖：`yiban/engine/workers.py`（让位判据与让位粒度）、
     `yiban/engine/executor_v3.py`（执行体运行期的让位复检 `yield_probe`）、
     `yiban/store/connection.py`（`pool_db_declared` 语义）的**消费面**。

**故障现场（2026-10-07 生产 v0.5.1）**：兜底常驻被宿主脚本拉起（异步、不持锁）后先于
主进程进入扫描；它探到"此刻没有人在跑"⇒ 不让位 ⇒ 以 `claim_all` 领走全部 64 个分片，
长跑 58 分钟；两个并行执行体到点领不到自己的行，汇总成大量假失败。兜底开局进入执行的
铁证是源日志第 7 行；全文件"整段让位" 0 次（10-05/10-06 分别 112/117 次）。

**本文件钉住的三条**（判据④"字面 + 反例 + 不误伤"）：
1. 兜底**先起**、主进程**后持锁**：让位必须在锁出现后**生效**——兜底一个分片都不领，
   当日的计划行原样留给计划执行体（分片不外溢）；
2. 反向控制：没有轮在跑时兜底照常领取（判据不是"永远让位"）；
3. 让位判据只认运行时事实：**库被声明且在库**（`pool_db_declared()` 为真，正是生产形状）
   时照样让位——旧判据在这一刻判成"有池 ⇒ 不让位"，那正是 10-07 的入口。

**依赖**：临时 SQLite（真迁移链）+ 真执行体补货循环（只替身登录与限速）+ 脚本化锁探测
+ 假时钟。不发任何网络请求；不为 Windows 单开分支（不依赖 flock）。
"""
import contextlib
import datetime
import json
import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import db

from yiban import egress
from yiban.engine import executor_v3, token_bucket, workers
from yiban.store import connection as store_conn  # current_db_file 的唯一定义点

TEST_KEY = "a" * 64
DAY = "2026-09-22"          # 周二：周末门不拦
START = datetime.datetime(2026, 9, 22, 6, 35, 0)
#: 身份串只从 `egress` 取（唯一构造处）：主机名由运行时决定，测试里不自造。
WORKER0 = egress.worker_owner(0)
WORKER1 = egress.worker_owner(1)
FALLBACK = egress.fallback_owner()

#: 部署清单（3 行：2 个并行执行体 + 1 个兜底）——生产形态：兜底**不在** HRW 候选集里，
#: 它的分片集恒空，不 `claim_all` 就零领取。
MANIFEST = json.dumps([
    {"slot": 0, "type": "worker", "proxy": ""},
    {"slot": 1, "type": "worker", "proxy": ""},
    {"slot": 2, "type": "fallback", "proxy": ""},
], ensure_ascii=False, separators=(",", ":"))

BASE_ENV = {
    "YIBAN_SIGN_START": "06:30",
    "YIBAN_SIGN_END": "07:50",
    "YIBAN_SATURDAY_SIGN": "0",
    "YIBAN_SUNDAY_SIGN": "0",
    "YIBAN_GLOBAL_PAUSE": "0",
    "YIBAN_FALLBACK_INTERVAL": "60",
    "YIBAN_EXECUTORS": MANIFEST,
}


def _phone(i):
    return f"1380000{i:04d}"


class _FakeClock:
    """执行体侧假时钟：`_now` / `_sleep` / `_mono` 共用一份推进。

    `max_sleeps` 是**预算守卫**：执行体不按预期收干时（例如让位冻结后仍空转、或收尾
    路径断了），用例会以一条明确的失败信息结束，而不是挂死等超时（挂死只说得出超时，
    说不出原因）。
    """

    def __init__(self, t=START, max_sleeps=4000):
        self.t = t
        self.mono = 10_000.0
        self.sleeps = []
        self._max = max_sleeps

    def now(self):
        return self.t

    async def sleep(self, sec):
        self.sleeps.append(sec)
        if len(self.sleeps) > self._max:
            raise AssertionError(
                f"执行体在 {self._max} 次睡眠预算内没有收干（领取/收尾路径失控？）")
        self.t += datetime.timedelta(seconds=sec)
        self.mono += sec
        import asyncio
        await asyncio.sleep(0)


class _LoopClock:
    """兜底主循环的时钟替身：**每次取时前进 `step`**，取时次数用满即抛错。

    自走式（而不是按"第几次取时返回什么"的序列）是必需的：循环内的取时次数是实现细节
    （`state_io` 的熔断快照读写、`schedule._schedule_config` 都会取时），按序列给会在重构后
    静默错位。起点落在窗口内（06:35）、步长 1 分钟，于是"窗口内若干轮（足够观察让位）→
    窗口关闭退出"与实现无关；`max_calls` 只是"循环不退出"的保险。

    次数用满抛错也是必需的：兜底主循环不退出时用例会挂死，挂死的用例在 CI 上只表现为超时。
    """

    def __init__(self, start=START, step=datetime.timedelta(minutes=1), max_calls=200):
        self._t = start
        self._step = step
        self._max = max_calls
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if self.calls > self._max:
            raise AssertionError(
                f"兜底主循环没有在 {self._max} 次取时内退出（窗口/让位判定失效？）")
        out = self._t
        self._t += self._step
        return out


class _Limiter:
    manual = False

    def bucket(self, egress_key):
        return SimpleNamespace(retry_after=lambda now: 0.0)

    def acquire(self, egress_key, now):
        return True

    def on_success(self, egress_key):
        pass

    def on_risk_signal(self, egress_key, now):
        pass

    def persist(self, egress_key, stamp=None):
        return True

    def restore_from_store(self, egress_key, now=None):
        return False


class _Gate:
    gap_sec = 10.0

    def allow(self, phone, now):
        return True

    def commit(self, phone, now):
        pass


async def _never(_ctx):
    import asyncio
    await asyncio.Event().wait()


class _Harness(unittest.TestCase):
    """真库 + 真执行体补货循环 + 脚本化锁探测。

    `lock_seq` 是全局锁探测的返回序列（兜底主循环的让位判据与执行体运行期的复检
    **取同一个函数**，故同一份序列按调用顺序被两边消费）：`False` = 这一刻没有轮在跑。
    """

    def setUp(self):
        # 共享 DB 单例教训（2026-10-07 复核 F1）：上一个文件/用例留下的 `db._conn` 会让
        # 本用例的 `db.init_db` 复用旧连接、读到陈旧库。setUp 第一件事就把它关掉。
        self._close_conn()
        self.root = tempfile.mkdtemp(prefix="yiban-yield-race-")
        self.db_file = os.path.join(self.root, "yiban.db")
        self.state_dir = os.path.join(self.root, "state")
        os.makedirs(self.state_dir)
        self.env_file = os.path.join(self.root, ".env")
        with open(self.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n")
        self._env = {k: os.environ.get(k) for k in (
            "YIBAN_DB_FILE", "YIBAN_ENV_FILE", "YIBAN_STATE_DIR", "YIBAN_ACCOUNTS_KEY",
            "YIBAN_EXECUTOR_ID", "YIBAN_EXECUTORS", "YIBAN_LOG_FILE",
            "YIBAN_GLOBAL_PAUSE")}
        os.environ.update({
            "YIBAN_DB_FILE": self.db_file,
            "YIBAN_ENV_FILE": self.env_file,
            "YIBAN_STATE_DIR": self.state_dir,
            "YIBAN_ACCOUNTS_KEY": TEST_KEY,
            "YIBAN_LOG_FILE": os.path.join(self.root, "sign.log"),
        })
        os.environ.pop("YIBAN_EXECUTOR_ID", None)
        os.environ.pop("YIBAN_GLOBAL_PAUSE", None)
        self.addCleanup(self._teardown)

    def _teardown(self):
        self._close_conn()
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.root, ignore_errors=True)

    @staticmethod
    def _close_conn():
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None

    def _init_db(self):
        """开本用例的临时库；先关单例，再断言连接确实指向它（F1 的两道守卫）。"""
        self._close_conn()
        db.init_db(self.db_file, env_file=self.env_file, cleanup=False)
        self.assertEqual(store_conn.current_db_file(), self.db_file,
                         "前置：单例连接必须指向本用例的临时库（残留连接＝读到陈旧库）")

    # ---- 读侧 ----
    def _rows(self, day=DAY):
        cur = db.get_conn().execute(
            "SELECT phone, state, owner FROM sign_tasks WHERE day=? ORDER BY phone", (day,))
        return [dict(r) for r in cur.fetchall()]

    def _claimed_by(self, owner_fragment, day=DAY):
        return [r["phone"] for r in self._rows(day)
                if owner_fragment in (r["owner"] or "")]

    def _accounts(self, n=6):
        return [SimpleNamespace(phone=_phone(i), user_paused=False, owner="admin")
                for i in range(1, n + 1)]

    # ---- 驱动 ----
    def _drive_fallback(self, lock_seq, *, accounts=None, attempt=None,
                        expect_calls=None):
        """跑一次兜底常驻主循环，返回 (rc, sleeps, beats, attempts, exec_calls)。

        `attempt`：登录替身；None = 断言它**不该被调用**（本用例的兜底应当一个账号都不跑）。
        """
        sleeps, beats, calls = [], [], []
        accs = accounts if accounts is not None else self._accounts()
        self._attempts = []
        lock_calls = {"n": 0}

        def _lock_held(*a, **k):
            lock_calls["n"] += 1
            if lock_seq:
                return lock_seq.pop(0) if len(lock_seq) > 1 else lock_seq[0]
            return False

        fc = _FakeClock()

        def _fake_attempt(acc):
            self._attempts.append(acc.phone)
            if attempt is None:
                raise AssertionError(
                    f"兜底在让位期间跑了账号 {acc.phone}——让位判据没生效")
            return attempt(acc)

        patches = [
            mock.patch.dict(os.environ, dict(BASE_ENV), clear=False),
            mock.patch.object(workers, "time", SimpleNamespace(sleep=sleeps.append)),
            mock.patch.object(workers.clock, "now", _LoopClock()),
            mock.patch.object(workers.cli_support, "_run_lock_held", _lock_held),
            mock.patch.object(workers.state_io, "_write_fallback_alive",
                              lambda now=None: beats.append(now)),
            mock.patch.object(workers.state_io, "_clear_fallback_alive", lambda: None),
            mock.patch.object(workers.accounts_mod, "load_accounts",
                              lambda *a, **k: accs),
            mock.patch.object(executor_v3, "_persist_loop", _never),
            mock.patch.object(executor_v3, "_make_limiter", lambda channels, shares=1: _Limiter()),
            mock.patch.object(executor_v3, "_make_gap_gate", lambda: _Gate()),
            mock.patch.object(executor_v3, "_make_global_limiter",
                              lambda: token_bucket.GlobalLimiter("")),
            mock.patch.object(executor_v3, "_now", fc.now),
            mock.patch.object(executor_v3, "_sleep", fc.sleep),
            mock.patch.object(executor_v3, "_mono", lambda: fc.mono),
            mock.patch.object(executor_v3.attempts, "attempt_signin", _fake_attempt),
            # 执行体被调用的次数（真调、不替身）：用于断言"兜底根本没进执行体"。
            mock.patch.object(executor_v3, "run_executor_v3",
                              side_effect=self._spy_v3(executor_v3.run_executor_v3, calls)),
        ]
        for p in patches:
            p.start()
        try:
            rc = workers.run_fallback_worker(["--fallback"])
        finally:
            for p in reversed(patches):
                with contextlib.suppress(Exception):
                    p.stop()
        if expect_calls is not None:
            self.assertEqual(len(calls), expect_calls,
                             "兜底进执行体的次数与预期不符（让位判据在循环顶就拦下了？）")
        return rc, sleeps, beats, calls

    @staticmethod
    def _spy_v3(real, sink):
        def _call(*a, **kw):
            sink.append(kw)
            return real(*a, **kw)
        return _call

    # ---- 计划行 ----
    def _plan_rows(self, phones, *, state="pending", owner=None, vshard=0, run_at=None):
        """直接落当日计划行：owner 写**计划执行体**（`planner.write_plan` 的形态）。

        `run_at` 缺省 = 起始时刻（到点，本轮的补货循环领得到）；给未来时刻即"未到点"。
        """
        conn = db.get_conn()
        stamp = START.strftime("%Y-%m-%d %H:%M:%S") + ".000"
        due = (run_at or START).strftime("%Y-%m-%d %H:%M:%S") + ".000"
        for i, phone in enumerate(phones):
            conn.execute(
                "INSERT OR REPLACE INTO sign_tasks (phone, day, vshard, owner, run_at, "
                "priority, state, attempts, lease_until, result, epoch, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (phone, DAY, vshard + i, owner or WORKER0, due, 5, state, 0, "", "", 0,
                 stamp))
        conn.commit()


class FallbackStartRaceYieldTest(_Harness):
    """判据④（正文）：兜底先起、主进程后持锁 ⇒ 兜底让位，分片不外溢。"""

    def test_fallback_claims_nothing_once_round_takes_lock(self):
        """**先红后绿的主角**：兜底首个探测为假（它先起），其余为真（主进程随后持锁）。

        断言三件：① 兜底一个分片都没领（当日计划行原样待领）；
        ② 计划行的归属仍是计划执行体（没有一行被兜底"吸收"）；
        ③ 兜底下一轮在循环顶整段让位（不再进执行体）。
        """
        self._init_db()
        # 兜底先起：它自己建当日计划（`_ensure_plan`）——生产 06:31:02 的第一行日志即此形态。
        # 主进程随后持锁 ⇒ 从第 2 次探测起恒为真。
        rc, sleeps, beats, _calls = self._drive_fallback(
            [False, True], expect_calls=1)

        rows = self._rows()
        self.assertTrue(rows, "前置：兜底应当已经建出当日计划行（_ensure_plan）")
        claimed = [r for r in rows if r["state"] != "pending"]
        self.assertEqual(
            claimed, [],
            "兜底在轮在飞期间领走了当日计划行——分片外溢（10-07 生产故障形状）")
        self.assertEqual(self._claimed_by("fallback"), [],
                         "兜底名下的领取行必须为空（它应当一个分片都不领）")
        self.assertEqual(self._attempts, [], "让位期间不得发起任何真实登录")
        self.assertIn(workers._YIELD_POLL_SEC, sleeps,
                      "兜底下一轮必须在循环顶整段让位（睡让位轮询间隔）")
        self.assertEqual(rc, 0)
        self.assertTrue(beats, "前置：兜底确实进过一轮（写过心跳）")

    def test_plan_rows_keep_planned_owner(self):
        """分片不外溢的可读面：计划行归属＝计划执行体（不是兜底）。"""
        self._init_db()
        self._drive_fallback([False, True], accounts=self._accounts(), expect_calls=1)
        rows = self._rows()
        self.assertTrue(rows, "前置：兜底应当已经建出当日计划行（_ensure_plan）")
        owners = {r["owner"] for r in rows}
        self.assertNotIn(FALLBACK, owners, "计划行的归属被兜底改写")
        self.assertTrue(owners <= {WORKER0, WORKER1},
                        f"计划行归属不是计划执行体（HRW 口径漂移）: {sorted(owners)}")

    def test_yields_though_queue_present_and_declared(self):
        """判据不是配置声明：库路径已声明、库已初始化 ⇒ `pool_db_declared()` 为真，照样让位。

        旧判据 `not db.pool_db_declared() and _run_lock_held()` 在**这一刻**答"有池 ⇒
        不让位"，于是兜底与全量轮并发领取——账号级仲裁只防重复登录，不防吸收全量。
        """
        self._init_db()
        self.assertTrue(db.pool_db_declared(),
                        "前置：本次部署声明了库路径（生产形状：默认库 + 未写声明键亦为真）")
        _rc, sleeps, _beats, calls = self._drive_fallback([True], expect_calls=0)
        self.assertIn(workers._YIELD_POLL_SEC, sleeps, "有队列时也必须整段让位")
        self.assertEqual(calls, [], "有队列 + 轮在飞 ⇒ 兜底不得进执行体")


class ClaimedRowsStillFinishOnFreezeTest(_Harness):
    """守卫 F2-③：让位**冻结领取**之后，已领的那批必须照常收尾，不留 `claimed` 残留。

    这是设计取舍的守卫面：让位只冻结"继续领取"，不动在飞行——半路丢弃会留下没人收的
    租约（行卡在 `claimed` 直到租约+宽限被回收，其间该账号当天无人再签）。突变验证：
    在冻结分支前加一句 `queue_store.reap_abandoned(ctx.runtime_id, ctx.day)`（把"自己的
    在飞行"也弃权掉）⇒ 本用例的"已领行落 done"断言变红。
    """

    DUE = 3      # 到点的行：会被领走、执行、收尾
    FUTURE = 3   # 未到点的行：本轮不该被领

    def test_claimed_rows_settle_and_none_left_claimed(self):
        def _ok(_acc):
            return (True, "签到成功", False, "success")

        self._init_db()
        phones = [a.phone for a in self._accounts(self.DUE + self.FUTURE)]
        due, future = phones[:self.DUE], phones[self.DUE:]
        self._plan_rows(due)
        self._plan_rows(future, run_at=START + datetime.timedelta(hours=1))
        # 探测序列：循环顶假（兜底先起）→ 执行体第 1 次复检假（照常领取）→ 之后恒真（冻结）。
        rc, _sleeps, _beats, _calls = self._drive_fallback(
            [False, False, True], accounts=self._accounts(self.DUE + self.FUTURE),
            attempt=_ok, expect_calls=1)

        states = {r["phone"]: r["state"] for r in self._rows()}
        self.assertEqual(sorted(self._attempts), sorted(due),
                         "到点的行没被领走执行（前置不成立，本用例失去守卫力）")
        self.assertEqual([states[p] for p in due], ["done"] * self.DUE,
                         "已领的行没有照常收尾——让位把在飞行也丢了")
        self.assertEqual([states[p] for p in future], ["pending"] * self.FUTURE,
                         "未到点的行不该被领走")
        self.assertNotIn("claimed", set(states.values()),
                         "让位后留下 claimed 残留（租约空转、当天无人再签）")
        self.assertEqual(rc, 0)


class FallbackRunsWhenNoRoundTest(_Harness):
    """反向控制：没有轮在跑时兜底照常领取——判据不是"永远让位"。"""

    def test_claims_and_signs_when_no_round_in_flight(self):
        def _ok(_acc):
            return (True, "签到成功", False, "success")

        self._init_db()
        phones = [a.phone for a in self._accounts()]
        self._plan_rows(phones)
        rc, sleeps, _beats, calls = self._drive_fallback(
            [False], accounts=self._accounts(), attempt=_ok)
        self.assertTrue(calls, "没有轮在跑时兜底根本不进执行体——它成了摆设")
        self.assertTrue(calls[0].get("claim_all"),
                        "兜底腿的宽范围领取（claim_all）必须保留：没有轮在飞时它就是捡漏的手")
        self.assertNotIn(workers._YIELD_POLL_SEC, sleeps,
                         "没有轮在跑却让位了——判据成了「永远让位」")
        self.assertEqual(sorted(self._attempts), sorted(phones),
                         "没有轮在跑时兜底必须照常接手")
        self.assertTrue(all(r["state"] == "done" for r in self._rows()),
                        "兜底接手后行应当收尾为 done")
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
