# -*- coding: utf-8 -*-
"""2026-08-27：.env 解析快速失败（防静默重建密钥）回归测试。

.env「存在但读取失败」必须抛出，而非被当作"未配置"——否则 load_key / 审计密钥 /
追踪盐的自动生成路径会误判无密钥而生成替代密钥，致存量密文/审计链永久失效。
文件真缺失仍返回空（走未配置分支），行为不变。

标签：G · 安全：脱敏/审计/配置注入
覆盖：`.env` 解析的两类出口——真缺失（返回空 dict，走未配置分支）与"存在但读不了"
（必须抛 OSError），以及 `load_key` 在后者下不得顺势生成新钥。
对应实现：`yiban/infra/account_crypto._parse_env_file` 与 `db._parse_env_file`
（两份解析实现都要 fail-loud，故两边各断一次）。
关键断言：三条红线是"缺失→空"与"读不了→抛"必须**分别**成立——把两者混成"异常一律吞掉
返回空"正好是最危险的形态（调用方按"未配置"分支去生成替代密钥，存量密文与审计链永久
失效）。用目录冒充不可读文件（IsADirectoryError，属 OSError 但非 FileNotFoundError）
是刻意的构造：只 except FileNotFoundError 的实现会在这里露出来。
依赖：临时目录，无网络、无 skip；`import db` 走仓库根的旧名门面，故依赖 `sys.path` 里
已插入 BASE。
"""
import os
import shutil
import tempfile
import unittest
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import db  # noqa: E402

from yiban.infra import account_crypto  # noqa: E402


class EnvParseFailLoudTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-envfail-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_missing_file_returns_empty(self):
        missing = os.path.join(self.tmp, "no-such.env")
        # 这是**两份独立实现**（`account_crypto._parse_env_file` 与
        # `yiban/store/audit_chain._parse_env_file`，后者经 `db` 门面暴露），
        # 只测其中一份会放过另一份仍然"读不到就当没配"。
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
