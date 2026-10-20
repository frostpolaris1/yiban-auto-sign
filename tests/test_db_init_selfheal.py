# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""`init_db` 失败自愈：建表/PRAGMA 段抛错时不得把半初始化连接留在单例里。

工单 `ba-p02-03`：`_connection.set_conn(conn)` 之后、`try:` 之前的
`row_factory` / 三条 PRAGMA / `_create_tables(conn)` 任一抛错（磁盘满、并发启动锁
超时、schema 损坏），既不关连接也不重置单例；此后任何重试都会在
`conn = current(); if conn is not None: return conn` 处直接复用这条**没有表**的连接，
整个进程永久 "no such table"。

修法：把"登记连接"之后的一切初始化动作纳入同一个 try/except，异常路径
`conn.close()` + `reset_conn()` 后再抛出。

标签：C · 存储：迁移与库完整性
覆盖：`init_db` 在建表期失败后的单例状态与重试可达性。
对应实现：`yiban/adapters/persistence/db.py` 的 `init_db`。
关键断言：失败后 `connection.current()` 必须为 None；紧接着的重试必须建出完整表结构。
依赖：临时库 + 临时 `.env`；注入失败用 `mock.patch.object(db, "_create_tables", …)`，
不 sleep、不碰真实磁盘错误。
"""
import contextlib
import os
import shutil
import sqlite3
import tempfile
import unittest
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TEST_KEY = "a" * 64


class _PragmaBoom:
    """只读分支的替身连接：登记后的 PRAGMA 抛错，并记录是否被关闭。"""

    def __init__(self):
        self.closed = False

    def execute(self, *a, **k):
        raise sqlite3.OperationalError("disk I/O error")

    def close(self):
        self.closed = True


class InitDbSelfHealTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-db-selfheal-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls._old = {}
        for k, v in (("YIBAN_ACCOUNTS_KEY", TEST_KEY), ("YIBAN_ENV_FILE", cls.env_file),
                     ("YIBAN_DB_FILE", cls.db_file),
                     ("YIBAN_STATE_DIR", cls.tmp)):
            cls._old[k] = os.environ.get(k)
            os.environ[k] = v
        global db
        import db

    @classmethod
    def tearDownClass(cls):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        for k, v in cls._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        for suffix in ("", "-wal", "-shm"):
            with contextlib.suppress(OSError):
                os.remove(self.db_file + suffix)

    def test_ddl_failure_leaves_no_poisoned_singleton(self):
        with mock.patch.object(db, "_create_tables",
                               side_effect=sqlite3.OperationalError("disk I/O error")), \
                self.assertRaises(sqlite3.OperationalError):
            db.init_db(self.db_file, env_file=self.env_file, cleanup=False)

        self.assertIsNone(db._connection.current(),
                          "建表失败后不得把半初始化连接留在单例里")
        self.assertIsNone(db._conn)

        conn = db.init_db(self.db_file, env_file=self.env_file, cleanup=False)
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        self.assertIn("accounts", names, "重试必须建出完整表结构（不得复用坏连接）")
        self.assertIn("audit_logs", names)

    def test_readonly_init_pragma_failure_also_resets_singleton(self):
        """同形状调用点（create=False 的只读分支）也必须自愈，不能只修一半。"""
        broken = _PragmaBoom()
        with mock.patch.object(db._connection, "open_readonly", return_value=broken), \
                self.assertRaises(sqlite3.OperationalError):
            db.init_db(self.db_file, env_file=self.env_file, create=False)
        self.assertTrue(broken.closed, "失败路径必须关闭半初始化连接")
        self.assertIsNone(db._connection.current(),
                          "只读分支失败也不得把连接留在单例里")


if __name__ == "__main__":
    unittest.main(verbosity=2)
