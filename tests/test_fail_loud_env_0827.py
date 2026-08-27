# -*- coding: utf-8 -*-
"""2026-08-27 批次4：.env 解析快速失败（防静默重建密钥）回归测试。

.env「存在但读取失败」必须抛出，而非被当作"未配置"——否则 load_key / 审计密钥 /
追踪盐的自动生成路径会误判无密钥而生成替代密钥，致存量密文/审计链永久失效。
文件真缺失仍返回空（走未配置分支），行为不变。
"""
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "scripts"))

import account_crypto  # noqa: E402
import db  # noqa: E402


class EnvParseFailLoudTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-envfail-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_missing_file_returns_empty(self):
        missing = os.path.join(self.tmp, "no-such.env")
        self.assertEqual(account_crypto._parse_env_file(missing), {})
        self.assertEqual(db._parse_env_file(missing), {})

    def test_unreadable_path_raises_not_silent(self):
        # 目录无法按文件打开（IsADirectoryError，属 OSError 但非 FileNotFoundError）
        d = os.path.join(self.tmp, "adir")
        os.mkdir(d)
        with self.assertRaises(OSError):
            account_crypto._parse_env_file(d)
        with self.assertRaises(OSError):
            db._parse_env_file(d)

    def test_load_key_fails_loud_not_regenerate_on_unreadable_env(self):
        """关键：.env 读不了时 load_key 宁可抛出，也绝不静默生成新钥覆盖旧钥。"""
        d = os.path.join(self.tmp, "adir")
        os.mkdir(d)
        prev_cache = account_crypto._KEY_CACHE
        account_crypto._KEY_CACHE = None
        try:
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("YIBAN_ACCOUNTS_KEY", None)
                with self.assertRaises(OSError):
                    account_crypto.load_key(env_file=d)
        finally:
            account_crypto._KEY_CACHE = prev_cache


if __name__ == "__main__":
    unittest.main()
