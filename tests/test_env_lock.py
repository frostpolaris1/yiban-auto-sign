# -*- coding: utf-8 -*-
"""Task 1：共享 .env 文件锁 + 密钥生成竞态修复测试。

覆盖：
- env_lock.env_write_lock 同线程可重入（RLock 语义）
- account_crypto._write_key_to_env_file 已存在密钥时不得覆盖（写前重读 + 锁内整体保护）
"""
import os
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

import account_crypto  # noqa: E402


class EnvLockTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="yiban-env-lock-")
        self.addCleanup(self.tmp.cleanup)
        self.env_file = os.path.join(self.tmp.name, ".env")

    def test_env_write_lock_reentrant_same_thread(self):
        """同进程同线程重入不阻塞（Windows RLock / POSIX RLock + flock 均可重入）。"""
        import env_lock  # 在测试内导入，使模块缺失时本测试单独红、另一条测试仍可跑

        with env_lock.env_write_lock(self.env_file):
            with env_lock.env_write_lock(self.env_file):
                pass  # 能进入嵌套块即视为同线程重入不阻塞

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
