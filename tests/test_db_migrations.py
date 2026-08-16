# -*- coding: utf-8 -*-
"""Phase 0：通用幂等迁移框架测试。

覆盖：
- 新库自动达到当前 schema 版本；
- 旧库缺列时自动补齐；
- 重复启动幂等；
- 核心迁移失败阻断启动并清理连接；
- 可选迁移失败不阻断启动。
"""
import contextlib
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "scripts"))

import db  # noqa: E402


class DbMigrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-db-migrate-")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        os.environ["YIBAN_DB_FILE"] = cls.db_file

    @classmethod
    def tearDownClass(cls):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        os.environ.pop("YIBAN_DB_FILE", None)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        for suffix in ("", "-wal", "-shm"):
            p = self.db_file + suffix
            if os.path.exists(p):
                os.remove(p)

    def _create_old_accounts_table(self):
        """手工创建一个缺少 user_paused 列的旧 accounts 表。"""
        conn = sqlite3.connect(self.db_file)
        conn.execute(
            "CREATE TABLE accounts ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "sort_order INTEGER NOT NULL, "
            "name TEXT NOT NULL DEFAULT '', "
            "phone TEXT NOT NULL UNIQUE, "
            "password TEXT NOT NULL DEFAULT '', "
            "phone_model TEXT NOT NULL DEFAULT '', "
            "phone_code TEXT NOT NULL DEFAULT '', "
            "owner TEXT NOT NULL DEFAULT 'admin', "
            "status TEXT NOT NULL DEFAULT 'pending', "
            "reject_reason TEXT NOT NULL DEFAULT '', "
            "deleted INTEGER NOT NULL DEFAULT 0, "
            "deleted_at TEXT NOT NULL DEFAULT ''"
            ")"
        )
        conn.commit()
        conn.close()

    def test_new_db_gets_version_1(self):
        conn = db.init_db(self.db_file)
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        self.assertEqual(version, 1)
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(accounts)").fetchall()}
        self.assertIn("user_paused", cols)

    def test_old_db_missing_user_paused_is_migrated(self):
        self._create_old_accounts_table()
        conn = db.init_db(self.db_file)
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        self.assertEqual(version, 1)
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(accounts)").fetchall()}
        self.assertIn("user_paused", cols)

    def test_repeated_init_idempotent(self):
        conn = db.init_db(self.db_file)
        self.assertIsNotNone(conn)
        # 同一连接重复 init 不应报错
        db.init_db(self.db_file)
        # 关闭后重新 init，旧库已带版本号，迁移应跳过
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        conn2 = db.init_db(self.db_file)
        version = conn2.execute("PRAGMA user_version").fetchone()[0]
        self.assertEqual(version, 1)

    def test_core_migration_failure_blocks_and_cleans_conn(self):
        def failing_migration(conn):
            raise RuntimeError("boom")

        old_migrations = db._MIGRATIONS
        db._MIGRATIONS = [(1, "failing_core", failing_migration, True)]
        try:
            with self.assertRaises(RuntimeError):
                db.init_db(self.db_file)
            self.assertIsNone(db._conn)
        finally:
            db._MIGRATIONS = old_migrations
            if db._conn is not None:
                with contextlib.suppress(Exception):
                    db._conn.close()
                db._conn = None

    def test_optional_migration_failure_does_not_block(self):
        def failing_migration(conn):
            raise RuntimeError("boom")

        old_migrations = db._MIGRATIONS
        db._MIGRATIONS = [(1, "failing_optional", failing_migration, False)]
        try:
            conn = db.init_db(self.db_file)
            self.assertIsNotNone(conn)
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            self.assertEqual(version, 0)
        finally:
            db._MIGRATIONS = old_migrations
            if db._conn is not None:
                with contextlib.suppress(Exception):
                    db._conn.close()
                db._conn = None


if __name__ == "__main__":
    unittest.main()
