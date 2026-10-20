# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""`pool_db_declared` 的缓存键必须在解析之前生效。

工单 `ba-p02-07`：旧实现先执行 `env_io.resolve_path(...)`（读 .env），再查
`_pool_declared_cache`——注释宣称"缓存使同一条配置每次问一遍变成一次"的收益
没有兑现。唯一活调用方是 workers 的兜底轮询（每 `_YIELD_POLL_SEC` 一次）。

修法：先按（进程环境取值, .env 路径, .env 指纹）查缓存，未命中再解析。
指纹取 mtime_ns/size/inode，与 `env_io.parse_env_file` 的缓存失效口径同源：
.env 改路径后下一次即重算。

标签：E · 配置与路径解析
覆盖：缓存命中不触发路径解析；.env 内容变化使缓存失效；进程环境优先。
对应实现：`yiban/adapters/persistence/connection.py` 的 `pool_db_declared`。
关键断言：同一条配置第二次调用**不再调用** `env_io.resolve_path`；.env 改写后
返回值随内容变化。两处都必须是行为断言，不是"代码里有个 dict"。
依赖：临时 `.env`；`mock.patch.object(env_io, "resolve_path", wraps=…)` 计数，
不联网、无 skip。
"""
import os
import shutil
import tempfile
import unittest
from unittest import mock

from yiban.adapters.config import env_io
from yiban.adapters.persistence import connection


class PoolDbDeclaredCacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-pool-declared-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.env_file = os.path.join(self.tmp, ".env")
        self._write_env("YIBAN_DB_FILE=/srv/yiban/data/yiban.db\n")
        connection._pool_declared_cache.clear()
        self.addCleanup(connection._pool_declared_cache.clear)

    def _write_env(self, text):
        with open(self.env_file, "w", encoding="utf-8") as f:
            f.write(text)

    def test_cache_hit_does_not_resolve_path_again(self):
        empty_env = {}
        with mock.patch.object(env_io, "resolve_path",
                               wraps=env_io.resolve_path) as resolve:
            self.assertTrue(connection.pool_db_declared(env=empty_env,
                                                        env_file=self.env_file))
            self.assertTrue(connection.pool_db_declared(env=empty_env,
                                                        env_file=self.env_file))
        self.assertEqual(resolve.call_count, 1,
                         "同一条配置的第二次询问必须命中缓存，不得再读 .env")

    def test_env_file_change_invalidates_cache(self):
        empty_env = {}
        self.assertTrue(connection.pool_db_declared(env=empty_env,
                                                    env_file=self.env_file))
        self._write_env("# 路径被移除了\n")
        self.assertFalse(connection.pool_db_declared(env=empty_env,
                                                     env_file=self.env_file),
                         ".env 改写后缓存必须失效（键含文件指纹，不是只缓存 bool）")

    def test_blank_process_env_value_falls_back_to_env_file(self):
        """进程环境是空白串时仍按 .env 判定（与 resolve_path 的既有口径一致）。"""
        self.assertTrue(connection.pool_db_declared(
            env={"YIBAN_DB_FILE": "   "}, env_file=self.env_file))
        self.assertTrue(connection.pool_db_declared(
            env={"YIBAN_DB_FILE": "/env/yiban.db"}, env_file=self.env_file),
            "进程环境给出路径时同样算「已声明」")


if __name__ == "__main__":
    unittest.main(verbosity=2)
