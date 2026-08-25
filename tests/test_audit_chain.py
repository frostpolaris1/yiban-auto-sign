# -*- coding: utf-8 -*-
"""Phase 3：审计日志 HMAC 哈希链测试。

覆盖：
- 迁移后版本 3 且存在 prev_hash/hash 列；
- 连续写入后校验通过；
- 篡改任意一行后校验失败；
- 清理旧日志后剩余链仍可校验（新根生效）；
- 存量旧数据回填后校验通过。
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

TEST_KEY = "a" * 64
AUDIT_KEY = "b" * 64


class AuditChainTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-audit-chain-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\nYIBAN_AUDIT_KEY={AUDIT_KEY}\n")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.accounts_file = os.path.join(cls.tmp, "accounts.json")
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
        os.environ["YIBAN_AUDIT_KEY"] = AUDIT_KEY
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_ACCOUNTS_FILE"] = cls.accounts_file
        os.environ["YIBAN_DB_FILE"] = cls.db_file
        global db
        import db

    @classmethod
    def tearDownClass(cls):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        os.environ.pop("YIBAN_ACCOUNTS_KEY", None)
        os.environ.pop("YIBAN_AUDIT_KEY", None)
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
        db.init_db(self.db_file, env_file=self.env_file)

    def _reset_conn(self):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None

    def test_migration_adds_hash_columns_and_version_3(self):
        conn = db.init_db(self.db_file)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 9)
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(audit_logs)").fetchall()}
        self.assertIn("prev_hash", cols)
        self.assertIn("hash", cols)

    def test_audit_chain_verify_ok(self):
        db.audit("admin", "account_add", "138****8001", "测试1")
        db.audit("admin", "account_update", "138****8002", "测试2")
        db.audit("user1@test.local", "user_register", "user1@test.local", "测试3")
        ok, broken, first = db.verify_audit_chain()
        self.assertTrue(ok)
        self.assertEqual(broken, 0)
        self.assertIsNone(first)

    def test_tamper_detected(self):
        db.audit("admin", "account_add", "138****8001", "测试1")
        db.audit("admin", "account_update", "138****8002", "测试2")
        db.audit("admin", "account_delete", "138****8003", "测试3")
        conn = sqlite3.connect(self.db_file)
        conn.execute("UPDATE audit_logs SET detail='被篡改' WHERE action='account_update'")
        conn.commit()
        conn.close()
        ok, broken, _ = db.verify_audit_chain()
        self.assertFalse(ok)
        self.assertGreaterEqual(broken, 1)

    def test_cleanup_old_rows_keeps_chain_verifiable(self):
        for i in range(5):
            db.audit("admin", "test", f"target-{i}", f"detail-{i}")
        conn = sqlite3.connect(self.db_file)
        conn.execute("DELETE FROM audit_logs WHERE id <= 2")
        conn.commit()
        conn.close()
        # 模拟 _audit_cleanup 的“删除后重建链”
        db._rechain_audit_logs(db.get_conn())
        ok, broken, first = db.verify_audit_chain()
        self.assertTrue(ok, (broken, first))

    def test_backfill_old_rows(self):
        self._reset_conn()
        # 清掉当前库，手工构造一个 user_version=2 且 audit_logs 无哈希列的旧库
        for suffix in ("", "-wal", "-shm"):
            p = self.db_file + suffix
            if os.path.exists(p):
                os.remove(p)
        conn = sqlite3.connect(self.db_file)
        try:
            conn.executescript(
                """
                CREATE TABLE audit_logs (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  ts TEXT NOT NULL,
                  username TEXT NOT NULL,
                  action TEXT NOT NULL,
                  target TEXT NOT NULL DEFAULT '',
                  detail TEXT NOT NULL DEFAULT ''
                );
                """
            )
            conn.execute("PRAGMA user_version = 2")
            conn.execute(
                "INSERT INTO audit_logs (ts, username, action, target, detail) VALUES "
                "('2026-08-16 10:00:00','admin','account_add','138****8001','旧数据1'),"
                "('2026-08-16 10:01:00','admin','account_update','138****8002','旧数据2')"
            )
            conn.commit()
        finally:
            conn.close()

        conn = db.init_db(self.db_file)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 9)
        ok, broken, first = db.verify_audit_chain()
        self.assertTrue(ok, (broken, first))


if __name__ == "__main__":
    unittest.main()
