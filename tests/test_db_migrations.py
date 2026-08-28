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
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write("YIBAN_ACCOUNTS_KEY=" + "a" * 64 + "\n")
        os.environ["YIBAN_DB_FILE"] = cls.db_file
        os.environ["YIBAN_ENV_FILE"] = cls.env_file

    @classmethod
    def tearDownClass(cls):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        os.environ.pop("YIBAN_DB_FILE", None)
        os.environ.pop("YIBAN_ENV_FILE", None)
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

    def _create_old_production_like_db(self):
        """模拟 0.19.8 生产库：无迁移版本号、users 无 deleted 列、audit 无哈希列。

        对抗审查 2026-08-16：真实旧库升级路径此前无测试覆盖——本地测试全从新库
        （users 表直接含 deleted 列）开始，掩盖了 _create_tables 提前建
        idx_users_email_live（依赖 users.deleted）导致旧库升级崩溃的回归。
        """
        conn = sqlite3.connect(self.db_file)
        conn.executescript(
            """
            CREATE TABLE accounts (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              sort_order INTEGER NOT NULL,
              name TEXT NOT NULL DEFAULT '',
              phone TEXT NOT NULL UNIQUE,
              password TEXT NOT NULL DEFAULT '',
              phone_model TEXT NOT NULL DEFAULT '',
              phone_code TEXT NOT NULL DEFAULT '',
              owner TEXT NOT NULL DEFAULT 'admin',
              status TEXT NOT NULL DEFAULT 'pending',
              reject_reason TEXT NOT NULL DEFAULT '',
              deleted INTEGER NOT NULL DEFAULT 0,
              deleted_at TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE users (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              email TEXT NOT NULL UNIQUE,
              password_hash TEXT NOT NULL,
              role TEXT NOT NULL DEFAULT 'user',
              created_at TEXT NOT NULL DEFAULT '',
              pw_version INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE audit_logs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              ts TEXT NOT NULL,
              username TEXT NOT NULL,
              action TEXT NOT NULL,
              target TEXT NOT NULL DEFAULT '',
              detail TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE time_prefs (
              phone TEXT PRIMARY KEY,
              slot_min INTEGER NOT NULL,
              updated_at TEXT NOT NULL
            );
            INSERT INTO users (email, password_hash, role, created_at, pw_version)
              VALUES ('old@test.local', 'hash', 'user', '2026-08-01 10:00:00', 1);
            INSERT INTO accounts (sort_order, name, phone, password, phone_model,
                                  phone_code, owner, status, reject_reason, deleted, deleted_at)
              VALUES (1, 'Old', '13800138000', 'enc', '', '', 'old@test.local',
                      'active', '', 0, '');
            INSERT INTO audit_logs (ts, username, action, target, detail)
              VALUES ('2026-08-01 10:00:00', 'old@test.local', 'user_register',
                      'old@test.local', '开放注册');
            """
        )
        conn.commit()
        conn.close()

    def test_old_production_db_upgrades_to_v5(self):
        """真实旧库（0.19.8 结构）升级到 v5 不崩溃、数据保留、审计链回填校验通过。"""
        self._create_old_production_like_db()
        conn = db.init_db(self.db_file)
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        self.assertEqual(version, 10)
        users_cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
        self.assertIn("deleted", users_cols)
        self.assertIn("deleted_at", users_cols)
        idx = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_users_email_live'"
        ).fetchone()
        self.assertIsNotNone(idx, "migrate_v5 应创建邮箱部分唯一索引")
        # 数据保留
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0], 1)
        # 审计哈希链回填后校验通过
        ok, broken, _ = db.verify_audit_chain()
        self.assertTrue(ok)
        self.assertEqual(broken, 0)

    def test_new_db_gets_latest_version(self):
        conn = db.init_db(self.db_file)
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        self.assertEqual(version, 10)
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(accounts)").fetchall()}
        self.assertIn("user_paused", cols)

    def test_old_db_missing_user_paused_is_migrated(self):
        self._create_old_accounts_table()
        conn = db.init_db(self.db_file)
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        self.assertEqual(version, 10)
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
        self.assertEqual(version, 10)

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
