# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""重签不得翻掉兜底执行体**正在飞**的行（工单 `uob1` X2，方案 b）。

标签：B · 调度：领取/队列/执行体
覆盖：`queue_store.reclaim_tasks` 在把 `claimed` 行翻回 `pending` 之前，显式探测
   **兜底锁**（`signin-run.lock.fallback`）。锁被持有 ⇒ 该行**不翻、不换代、不动
   `run_at`**，并留一条带业务日与遮罩账号的 WARNING；锁释放后同一入口照常回收。
   终态行（`done`/`skipped`）不受影响：兜底在跑也照翻。
对应实现：yiban/adapters/persistence/queue_store.py（`reclaim_tasks`）、
   yiban/engine/cli_support.py（`FALLBACK_LOCK_NAME` / `_run_lock_held`）。
关键断言（不变式）：**同一账号在任何时刻不得被两条登录路径同时持有**。故本文件断言
   "兜底锁被持有时 `claimed` 行的 owner/epoch/state 三元组逐字节不变"，不断言调用顺序。
   判别力：去掉探锁（无条件翻 `claimed`）⇒ 本文件的 e2e 必红。
依赖：**两个真子进程**（进程 A 持兜底锁并真领取一行；进程 B 走重签路径）＋ 临时 sqlite。
   不用线程：`claim_batch` 在进程内锁下执行，两线程会被串行化，测不到跨进程互斥
   （AGENTS §十二.32）。POSIX flock 专有，Windows 上显式 skip（不许静默当绿）。
"""
import datetime
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

import db  # noqa: E402

from yiban.adapters.persistence import queue_store  # noqa: E402
from yiban.core import clock, masking  # noqa: E402
from yiban.engine import cli_support  # noqa: E402
from yiban.workers import workers  # noqa: E402

TEST_KEY = "a" * 64
#: 持有者身份（模拟兜底的运行时身份形态；本文件不依赖主机名）
OWNER = "fallback@e2e:4242:090000"
PHONE_IN_FLIGHT = "13800000001"
PHONE_DONE = "13800000002"
#: 业务日相对当前时钟生成（写死日历日期会随运行日老化，AGENTS §十三.27 形状④）。
DAY = clock.now().strftime("%Y-%m-%d")
#: 到期时刻必须早于"现在"，否则 A 领不到行（claim_batch 只领 run_at <= now 的行）。
RUN_AT = (clock.now() - datetime.timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
CREATED_AT = (clock.now() - datetime.timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

#: 进程 A：真持兜底锁，再真领取一行，然后保持持锁在飞。
_HOLDER_SRC = r"""
import os, sys, time
sys.path.insert(0, os.environ["RECLAIM_E2E_REPO"])
from yiban.adapters.persistence import queue_store
from yiban.engine import cli_support
fh = cli_support._acquire_run_lock(True, name=os.environ["RECLAIM_E2E_LOCK"])
rows = queue_store.claim_batch(os.environ["RECLAIM_E2E_OWNER"],
                               os.environ["RECLAIM_E2E_DAY"], (0,))
if not rows:
    sys.stdout.write("NO_CLAIM\n")
    sys.stdout.flush()
    raise SystemExit(4)
sys.stdout.write("HELD %s\n" % rows[0]["phone"])
sys.stdout.flush()
time.sleep(float(os.environ.get("RECLAIM_E2E_HOLD", "30")))
"""

#: 进程 B：走重签路径，只回翻回数。
_RECLAIMER_SRC = r"""
import logging, os, sys
logging.basicConfig(level=logging.INFO, stream=sys.stderr)
sys.path.insert(0, os.environ["RECLAIM_E2E_REPO"])
from yiban.adapters.persistence import queue_store
phones = os.environ["RECLAIM_E2E_PHONES"].split(",")
n = queue_store.reclaim_tasks(os.environ["RECLAIM_E2E_DAY"], phones)
sys.stdout.write("FLIPPED %d\n" % n)
sys.stdout.flush()
"""


class _DbBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-reclaim-lock-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db_file = os.path.join(self.tmp, "yiban.db")
        self.env_file = os.path.join(self.tmp, ".env")
        with open(self.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n")
        patcher = mock.patch.dict(os.environ, {
            "YIBAN_ACCOUNTS_KEY": TEST_KEY,
            "YIBAN_ENV_FILE": self.env_file,
            "YIBAN_DB_FILE": self.db_file,
            "YIBAN_STATE_DIR": self.tmp,
            "YIBAN_RUN_LOCK_NAME": "",
            "YIBAN_LOG_FILE": os.path.join(self.tmp, "sign.log"),
        }, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        self._close_conn()
        db.init_db(self.db_file, env_file=self.env_file, cleanup=False)
        self._close_conn()

    def tearDown(self):
        self._close_conn()

    @staticmethod
    def _close_conn():
        import contextlib
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None

    def _add_task(self, phone, state, epoch=0, owner=""):
        conn = db.get_conn()
        conn.execute(
            "INSERT INTO sign_tasks (phone, day, vshard, owner, run_at, priority, "
            "state, attempts, lease_until, result, epoch, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (phone, DAY, 0, owner, RUN_AT, 5, state, 0, "", "",
             epoch, CREATED_AT))
        conn.commit()
        self._close_conn()

    def _row(self, phone):
        conn = db.get_conn()
        try:
            return dict(conn.execute(
                "SELECT phone, day, state, owner, epoch, run_at FROM sign_tasks "
                "WHERE phone=? AND day=?", (phone, DAY)).fetchone())
        finally:
            self._close_conn()

    def _subprocess_env(self, **extra):
        env = dict(os.environ)
        env.update({
            "RECLAIM_E2E_REPO": BASE,
            "RECLAIM_E2E_DAY": DAY,
            "YIBAN_ACCOUNTS_KEY": TEST_KEY,
            "YIBAN_ENV_FILE": self.env_file,
            "YIBAN_DB_FILE": self.db_file,
            "YIBAN_STATE_DIR": self.tmp,
            "YIBAN_RUN_LOCK_NAME": "",
            "PYTHONPATH": BASE,
        })
        env.update(extra)
        return env

    def _write_script(self, name, source):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(source)
        return path


class ReclaimUnderHeldFallbackLockE2ETest(_DbBase):
    """真跨进程：A 持兜底锁在飞 ⇒ B 不翻 A 的行；A 退出后 B 才回收。"""

    def test_held_fallback_lock_blocks_claimed_reclaim_until_released(self):
        if cli_support.fcntl is None:
            self.skipTest("跨进程 flock 仅 POSIX 可用（Windows 上取不到锁）")
        self._add_task(PHONE_IN_FLIGHT, state="pending")
        self._add_task(PHONE_DONE, state="done", epoch=1)
        holder_src = self._write_script("holder.py", _HOLDER_SRC)
        reclaimer_src = self._write_script("reclaimer.py", _RECLAIMER_SRC)
        holder = subprocess.Popen(
            [sys.executable, holder_src],
            env=self._subprocess_env(RECLAIM_E2E_LOCK=workers.FALLBACK_LOCK_NAME,
                                     RECLAIM_E2E_OWNER=OWNER,
                                     RECLAIM_E2E_HOLD="40"),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            cwd=BASE, text=True)
        try:
            line = holder.stdout.readline().strip()
            self.assertEqual(line, f"HELD {PHONE_IN_FLIGHT}",
                             "进程 A 必须真的持兜底锁并领到那一行（否则本用例什么都没测）")
            inflight = self._row(PHONE_IN_FLIGHT)
            self.assertEqual(inflight["state"], "claimed",
                             "前提：A 领取后该行处于在飞行")
            held_epoch = inflight["epoch"]
            self.assertEqual(inflight["owner"], OWNER)

            # ---- 进程 B：兜底锁在 A 手里，走重签路径 ----
            proc = subprocess.run(
                [sys.executable, reclaimer_src],
                env=self._subprocess_env(RECLAIM_E2E_PHONES=f"{PHONE_IN_FLIGHT},{PHONE_DONE}",
                                         RECLAIM_E2E_LOCK=workers.FALLBACK_LOCK_NAME),
                cwd=BASE, capture_output=True, text=True, timeout=120)
            self.assertEqual(proc.returncode, 0,
                             f"重签进程必须正常结束：stdout={proc.stdout!r} "
                             f"stderr={proc.stderr!r}")
            self.assertIn("FLIPPED 1", proc.stdout,
                          "在飞的行不得被翻；只有终态行被翻（1 行）。"
                          f"实际 stdout={proc.stdout!r} stderr={proc.stderr!r}")
            after = self._row(PHONE_IN_FLIGHT)
            self.assertEqual(
                (after["state"], after["owner"], after["epoch"]),
                ("claimed", OWNER, held_epoch),
                "兜底锁被持有时，在飞行的 state/owner/epoch 必须逐字节不变"
                "（翻了就是同账号并发登录）")
            self.assertEqual(self._row(PHONE_DONE)["state"], "pending",
                             "终态行不受锁保护：兜底在跑也照翻")
            self.assertIn(masking.mask_phone(PHONE_IN_FLIGHT), proc.stderr,
                          "跳过必须留可归因日志（遮罩账号）")
            self.assertNotIn(PHONE_IN_FLIGHT, proc.stderr,
                             "日志不得出现裸手机号（本仓日志红线）")
            self.assertIn(DAY, proc.stderr, "日志必须带业务日，便于归因")
            self.assertIn(workers.FALLBACK_LOCK_NAME, proc.stderr,
                          "日志必须点名被探测的锁")
        finally:
            holder.kill()
            holder.wait(timeout=30)

        # ---- A 已退出（锁随进程释放）：B 才可回收 ----
        proc = subprocess.run(
            [sys.executable, reclaimer_src],
            env=self._subprocess_env(RECLAIM_E2E_PHONES=f"{PHONE_IN_FLIGHT},{PHONE_DONE}"),
            cwd=BASE, capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("FLIPPED 2", proc.stdout,
                      f"锁释放后在飞行必须可回收，实际 stdout={proc.stdout!r}")
        after = self._row(PHONE_IN_FLIGHT)
        self.assertEqual((after["state"], after["owner"]), ("pending", ""),
                         "回收后排空 owner 并回 pending")
        self.assertGreater(after["epoch"], held_epoch,
                           "回收必须换代——原持有者迟到的写被 fence")


class ReclaimRacesFallbackStartupTest(_DbBase):
    """竞态形态：重签**探测时**兜底还没起，**翻行时**兜底已起并领取了同一行。

    「探测后放行」存在检查-使用窗口（审查 D1 实测可翻掉刚被兜底持有的行）。
    修法＝改持锁：重签若**持有**兜底锁跨整个翻行事务，则「兜底在我们翻行时启动并领取」
    在结构上不可能——兜底取不到锁（rc=3 退出），下一次 cron 再拉。故本用例断言：
    翻行后重查，行不得处于 claimed（它要么已翻且不可能是兜底持有的，要么根本没被领）。
    """

    def test_reclaim_holds_lock_across_update(self):
        if cli_support.fcntl is None:
            self.skipTest("跨进程 flock 仅 POSIX 可用（Windows 上取不到锁）")
        self._add_task(PHONE_IN_FLIGHT, state="done", epoch=1)
        # 持锁形态的判别断言必须在**事务进行中**验（reclaim 返回即已放锁）。
        # sqlite3.Connection 的属性只读，故探锁挂在 clock.now 上：reclaim 先取锁、
        # 后调 clock.now() 生成 stamp，再 UPDATE——桩被调时锁必已持有。
        observed = {}
        real_now = queue_store.clock.now

        def spying_now():
            observed["held"] = cli_support._run_lock_held(
                name=cli_support.FALLBACK_LOCK_NAME)
            return real_now()

        with mock.patch.object(queue_store.clock, "now", spying_now):
            n = queue_store.reclaim_tasks(DAY, [PHONE_IN_FLIGHT])
        self.assertEqual(n, 1)
        self.assertIs(observed.get("held"), True,
                      "翻行进行中本进程必须持有兜底锁（探测形态下 held=False，即红）")
        row = self._row(PHONE_IN_FLIGHT)
        self.assertEqual((row["state"], row["owner"]), ("pending", ""),
                         "无并发兜底时重签必须照常翻行")


class ProbeFailureFailClosedTest(_DbBase):
    """探不出锁状态 ⇒ 按"有人在跑"处置（fail-closed），在飞行一律不翻。"""

    def test_unavailable_lock_backend_keeps_claimed_rows(self):
        """锁后端不存在（`fcntl is None`）＝锁取不到：不得假装"锁没人持"。"""
        self._add_task(PHONE_IN_FLIGHT, state="claimed", owner=OWNER, epoch=7)
        self._add_task(PHONE_DONE, state="done", epoch=1)
        with mock.patch.object(cli_support, "fcntl", None):
            n = queue_store.reclaim_tasks(DAY, [PHONE_IN_FLIGHT, PHONE_DONE])
        self.assertEqual(n, 1, "只有终态行可翻；在飞行按'有人在跑'跳过")
        row = self._row(PHONE_IN_FLIGHT)
        self.assertEqual((row["state"], row["owner"], row["epoch"]),
                         ("claimed", OWNER, 7),
                         "取不到锁时，在飞行必须原样保留")

    def test_lock_takeover_exception_keeps_claimed_rows(self):
        """取锁自身抛异常（非预期故障，非 `_RunLockHeld`）同样按被持有处置，
        不得外泄成"照翻"。"""
        self._add_task(PHONE_IN_FLIGHT, state="claimed", owner=OWNER, epoch=7)
        with mock.patch.object(queue_store.cli_support, "_acquire_run_lock",
                               side_effect=RuntimeError("取锁炸了")), \
                self.assertLogs("yiban.store.queue_store", "WARNING") as cm:
            n = queue_store.reclaim_tasks(DAY, [PHONE_IN_FLIGHT])
        self.assertEqual(n, 0)
        row = self._row(PHONE_IN_FLIGHT)
        self.assertEqual((row["state"], row["owner"], row["epoch"]),
                         ("claimed", OWNER, 7))
        self.assertTrue(any("锁" in line for line in cm.output),
                        f"取锁失败必须留痕，实际 {cm.output}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
