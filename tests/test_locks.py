# -*- coding: utf-8 -*-
"""跨进程文件锁原语（`locks`，M0-5）与 D-4 回归守护。

背景：本项目原有四份各自为政的文件锁，Windows 行为不一致——`env_lock` 有真锁
（msvcrt），而 `signin._state_file_lock` 与 `notify._state_file_lock` 在 Windows 上
**退化为 no-op 且无任何告警**，5 处状态文件读-改-写与账本/节流因此失去跨进程互斥。

本文件钉住两点：
1. 三处都用同一个原语（不再各写一份）；
2. 本平台的锁后端**不是 None**（`fcntl` 或 `msvcrt` 至少有一个可用）——若哪天
   又一次"静默降级"，`lock_kind()` 会连同降级告警一起暴露出来。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

import env_lock  # noqa: E402
import locks  # noqa: E402
import notify  # noqa: E402
import signin  # noqa: E402


class LockPrimitiveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-locks-")
        self.target = os.path.join(self.tmp, "state.json")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_platform_has_a_real_lock_backend(self):
        """本平台必须有真锁后端——None 意味着只剩进程内互斥（曾经的静默降级）。"""
        self.assertIsNotNone(
            locks.lock_kind(),
            "fcntl 与 msvcrt 均不可用：跨进程互斥失效（会是告警而非静默，但仍需可见）",
        )

    def test_lock_file_created_0600(self):
        with locks.file_lock(self.target):
            pass
        lock_file = self.target + ".lock"
        self.assertTrue(os.path.exists(lock_file), "锁文件应为 <key>.lock")
        if os.name == "posix":
            import stat
            self.assertEqual(stat.S_IMODE(os.stat(lock_file).st_mode), 0o600)

    def test_reentrant_same_thread(self):
        with locks.file_lock(self.target):  # noqa: SIM117 - 嵌套正是重入场景
            with locks.file_lock(self.target):
                pass

    def test_released_after_exit(self):
        with locks.file_lock(self.target):
            pass
        with locks.file_lock(self.target):  # 能再次进入 = 已释放
            pass

    @unittest.skipUnless(os.name == "posix", "跨进程 flock 断言仅 POSIX 可用")
    def test_waits_for_holder_instead_of_degrading_immediately(self):
        """**引入 portalocker 的核心理由**：抢不到锁要重试等待，不是立刻降级为进程内锁。

        自写版一次失败就降级（Windows 上 `msvcrt.LK_LOCK` 约 10 秒放弃），多执行体
        竞争下跨进程互斥会静默失效；portalocker 的 Lock 带重试循环，等到超时才报错。
        """
        import multiprocessing as mp
        import time

        ctx = mp.get_context("fork")
        holding, release = ctx.Event(), ctx.Event()

        def _holder():
            with locks.file_lock(self.target):
                holding.set()
                release.wait(10)

        proc = ctx.Process(target=_holder)
        proc.start()
        try:
            self.assertTrue(holding.wait(5), "持有者未就绪")
            t0 = time.monotonic()
            # 持有者 0.6s 后释放：给 5s 超时的调用应当**等到**它释放并成功拿到
            def _release_later():
                time.sleep(0.6)
                release.set()

            import threading
            threading.Thread(target=_release_later, daemon=True).start()
            with locks.file_lock(self.target, timeout=5.0):
                waited = time.monotonic() - t0
            self.assertGreaterEqual(waited, 0.4,
                                    "应等待持有者释放（而不是立刻降级/返回）")
        finally:
            release.set()
            proc.join(5)
            if proc.is_alive():
                proc.terminate()

    def test_wrapper_passes_a_retry_timeout(self):
        """守护配置：包装层必须把**正数**重试时长交给 portalocker。

        若有人把它改回"零超时/不重试"，上面那条等待断言在快机器上可能侥幸通过，
        这条会在配置层面直接拦下。
        """
        with mock.patch.object(locks.portalocker, "Lock",
                              wraps=locks.portalocker.Lock) as spy, \
                locks.file_lock(self.target):
            pass
        spy.assert_called_once()
        self.assertGreater(spy.call_args.kwargs.get("timeout", 0), 0,
                           "必须传入正数 timeout 以启用重试")
        self.assertGreater(spy.call_args.kwargs.get("check_interval", 0), 0)

    @unittest.skipUnless(os.name == "posix", "跨进程 flock 断言仅 POSIX 可用")
    def test_cross_process_exclusion_posix(self):
        import multiprocessing as mp
        import time

        ctx = mp.get_context("fork")
        ready, go, entered = ctx.Event(), ctx.Event(), ctx.Event()
        lock_path = self.target

        def _child():
            ready.set()
            go.wait(5)
            with locks.file_lock(lock_path):
                entered.set()

        proc = ctx.Process(target=_child)
        proc.start()
        self.assertTrue(ready.wait(5), "子进程未就绪")
        with locks.file_lock(lock_path):
            go.set()
            time.sleep(0.4)
            self.assertFalse(entered.wait(0.2), "父进程持锁期间子进程不得进入")
        self.assertTrue(entered.wait(5), "父进程释放后子进程应能获取锁")
        proc.join(5)
        self.assertFalse(proc.is_alive())


class D4DivergentLocksRemovedTest(unittest.TestCase):
    """D-4：三处状态文件锁都必须走统一原语。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-locks-d4-")
        self.orig_state_dir = os.environ.get("YIBAN_STATE_DIR")
        os.environ["YIBAN_STATE_DIR"] = self.tmp

    def tearDown(self):
        if self.orig_state_dir is None:
            os.environ.pop("YIBAN_STATE_DIR", None)
        else:
            os.environ["YIBAN_STATE_DIR"] = self.orig_state_dir
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_signin_state_file_lock_uses_primitive(self):
        path = os.path.join(self.tmp, "cred-state.json")
        with mock.patch.object(signin.locks, "file_lock", wraps=locks.file_lock) as spy, \
                signin._state_file_lock(path):
            pass
        spy.assert_called_once()
        self.assertEqual(os.path.abspath(spy.call_args[0][0]), os.path.abspath(path),
                         "应以被保护文件路径为 key（原语自行拼 .lock）")
        self.assertTrue(os.path.exists(path + ".lock"), "应真的建出锁文件（不再是 no-op）")

    def test_notify_state_file_lock_uses_primitive(self):
        with mock.patch.object(notify.locks, "file_lock", wraps=locks.file_lock) as spy, \
                notify._state_file_lock("notify-ledger.json"):
            pass
        spy.assert_called_once()
        self.assertTrue(
            os.path.exists(os.path.join(self.tmp, "notify-ledger.json.lock")),
            "应真的建出锁文件（不再是 no-op）",
        )

    def test_env_write_lock_delegates_to_primitive(self):
        env_path = os.path.join(self.tmp, ".env")
        with mock.patch.object(env_lock, "file_lock", wraps=locks.file_lock) as spy, \
                env_lock.env_write_lock(env_path):
            pass
        spy.assert_called_once()
        self.assertEqual(os.path.abspath(spy.call_args[0][0]), os.path.abspath(env_path))


if __name__ == "__main__":
    unittest.main(verbosity=2)
