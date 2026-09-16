# -*- coding: utf-8 -*-
"""Task 1：共享 .env 文件锁 + 密钥生成竞态修复测试。

覆盖：
- env_lock.env_write_lock 同线程可重入（RLock 语义）
- account_crypto._write_key_to_env_file 已存在密钥时不得覆盖（写前重读 + 锁内整体保护）
"""
import os
import tempfile
import unittest
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from yiban.infra import (  # noqa: E402
    account_crypto,
    env_lock,
    locks,
)


def _posix_lock_worker(env_file, ready, go, attempting, entered, release):
    """POSIX 跨进程互斥测试子进程：等待 go 后尝试获取 env_write_lock。"""
    from yiban.infra import env_lock

    ready.set()
    if not go.wait(5):
        return
    attempting.set()
    with env_lock.env_write_lock(env_file):
        entered.set()
        release.wait(5)


class EnvLockTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="yiban-env-lock-")
        self.addCleanup(self.tmp.cleanup)
        self.env_file = os.path.join(self.tmp.name, ".env")

    def test_env_write_lock_reentrant_same_thread(self):
        """同进程同线程重入不阻塞（Windows RLock / POSIX RLock + flock 均可重入）。"""
        with env_lock.env_write_lock(self.env_file):  # noqa: SIM117 - 嵌套 with 正是重入场景
            with env_lock.env_write_lock(self.env_file):
                pass  # 能进入嵌套块即视为同线程重入不阻塞

    # ---- 文件锁降级路径必须告警留痕（降级 = 跨进程互斥失效，静默降级会丢并发写入）----

    def test_open_failure_logs_warning_and_degrades_to_inprocess_lock(self):
        """锁文件打不开（目录不可写等）：warning 留痕，仍返回进程内锁，业务不阻断。"""
        with mock.patch.object(env_lock.os, "open", side_effect=OSError(13, "Permission denied")), \
                self.assertLogs("yiban.locks", level="WARNING") as logs, \
                env_lock.env_write_lock(self.env_file):
            pass  # 能进入临界区 = 降级为进程内锁后仍可用
        self.assertTrue(any("退化为进程内锁" in m for m in logs.output), logs.output)

    def test_lock_failure_logs_warning_and_degrades(self):
        """加锁本身失败（抢锁超时/平台无锁后端/文件系统不支持）：必须告警留痕。

        注入点在 portalocker 边界：底层平台分发已交给该库，**在导入时就确定了平台
        后端**，所以再 patch `fcntl`/`msvcrt` 已打不到它（旧版那两条用例正是因此失效）。
        要钉的契约没变——**任何加锁失败都要告警并降级，绝不静默**。
        """
        import portalocker

        with mock.patch.object(
                locks.portalocker.Lock, "acquire",
                side_effect=portalocker.exceptions.LockException(
                    portalocker.exceptions.LockException.LOCK_FAILED, "抢锁超时")), \
                self.assertLogs("yiban.locks", level="WARNING") as logs, \
                env_lock.env_write_lock(self.env_file):
            pass  # 降级为进程内锁后临界区仍可用（业务不阻断）
        self.assertTrue(any("退化为进程内锁" in m for m in logs.output), logs.output)

    def test_lock_failure_still_excludes_within_process(self):
        """降级后**进程内**互斥仍然生效（同线程重入不阻塞、跨线程会互斥）。"""
        import threading

        import portalocker

        entered = threading.Event()

        def _other_thread():
            with locks.file_lock(self.env_file):
                entered.set()

        with mock.patch.object(
                locks.portalocker.Lock, "acquire",
                side_effect=portalocker.exceptions.LockException(
                    portalocker.exceptions.LockException.LOCK_FAILED, "抢锁超时")), \
                self.assertLogs("yiban.locks", level="WARNING"), \
                env_lock.env_write_lock(self.env_file):
            t = threading.Thread(target=_other_thread, daemon=True)
            t.start()
            self.assertFalse(entered.wait(0.2),
                             "另一线程不得在持锁期间进入（进程内 RLock 生效）")
        t.join(timeout=5)
        self.assertTrue(entered.is_set(), "释放后另一线程应能进入")

    @unittest.skipUnless(os.name == "posix", "跨进程 flock 仅 POSIX 可用；Windows 退化为进程内锁")
    def test_env_write_lock_cross_process_posix(self):
        """POSIX 跨进程互斥：父进程持锁时子进程不得进入，父进程释放后子进程进入。"""
        import multiprocessing as mp
        import time

        ctx = mp.get_context("fork")
        ready = ctx.Event()
        go = ctx.Event()
        attempting = ctx.Event()
        entered = ctx.Event()
        release = ctx.Event()
        proc = ctx.Process(
            target=_posix_lock_worker,
            args=(self.env_file, ready, go, attempting, entered, release),
        )
        proc.start()
        try:
            self.assertTrue(ready.wait(5), "子进程未就绪")
            with env_lock.env_write_lock(self.env_file):
                go.set()
                self.assertTrue(attempting.wait(5), "子进程未开始尝试获取锁")
                time.sleep(0.2)
                self.assertFalse(
                    entered.is_set(),
                    "父进程持锁期间子进程不应进入临界区（跨进程 flock 未生效）",
                )
            self.assertTrue(entered.wait(5), "父进程释放后子进程应能获取锁")
        finally:
            release.set()
            proc.join(5)
            self.assertFalse(proc.is_alive(), "子进程未在超时内退出")

    def test_account_crypto_does_not_overwrite_existing_key(self):
        """先写 key A，再调 _write_key_to_env_file(env, B) 返回 A 且文件仍为 A。"""
        key_a = bytes(range(32))
        key_b = bytes(range(32, 64))

        self.assertEqual(
            account_crypto._write_key_to_env_file(self.env_file, key_a), key_a
        )
        result = account_crypto._write_key_to_env_file(self.env_file, key_b)

        self.assertEqual(result, key_a)
        with open(self.env_file, encoding="utf-8") as f:
            content = f.read()
        self.assertIn(f"YIBAN_ACCOUNTS_KEY={key_a.hex()}", content)
        self.assertNotIn(key_b.hex(), content)


if __name__ == "__main__":
    unittest.main(verbosity=2)
