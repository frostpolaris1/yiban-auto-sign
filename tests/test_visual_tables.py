# -*- coding: utf-8 -*-
"""Phase 4：可视化三表（sign_events / page_visits / server_metrics）测试。

覆盖：
- 迁移后版本 4，三张表与索引存在；
- 写入函数成功写入；
- 表被误删时写入函数不抛异常（降级）；
- 清理函数删除超期数据；
- 只读聚合函数返回预期结构；
- hash_ip 使用 YIBAN_TRACK_SALT 且结果稳定。
"""
import contextlib
import datetime
import os
import shutil
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "scripts"))

TEST_KEY = "a" * 64
AUDIT_KEY = "b" * 64
TRACK_SALT = "c" * 64


class VisualTablesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-visual-tables-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(
                f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
                f"YIBAN_AUDIT_KEY={AUDIT_KEY}\n"
                f"YIBAN_TRACK_SALT={TRACK_SALT}\n"
            )
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.accounts_file = os.path.join(cls.tmp, "accounts.json")
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
        os.environ["YIBAN_AUDIT_KEY"] = AUDIT_KEY
        os.environ["YIBAN_TRACK_SALT"] = TRACK_SALT
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
        os.environ.pop("YIBAN_TRACK_SALT", None)
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

    def test_migration_creates_tables_and_version_4(self):
        conn = db.init_db(self.db_file)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 12)
        for table in ("sign_events", "page_visits", "server_metrics"):
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            self.assertIsNotNone(row, f"表 {table} 应存在")
        for index in ("idx_sign_events_ts", "idx_sign_events_phone",
                      "idx_page_visits_ts", "idx_page_visits_role",
                      "idx_server_metrics_ts"):
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name=?", (index,)
            ).fetchone()
            self.assertIsNotNone(row, f"索引 {index} 应存在")

    def test_write_functions_succeed(self):
        db.add_sign_event("2026-08-16 06:30:00", "13800138000", "success", "ok", "signin", 1)
        db.add_page_visit("2026-08-16 08:00:00", "admin", "/", db.hash_ip("1.2.3.4"), "UA", 100)
        db.add_server_metric("2026-08-16 08:00:00", cpu=10.0, mem_pct=50.0)
        conn = db.get_conn()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM sign_events").fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM page_visits").fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM server_metrics").fetchone()[0], 1)

    def test_write_functions_degrade_when_table_missing(self):
        conn = db.get_conn()
        conn.execute("DROP TABLE sign_events")
        conn.execute("DROP TABLE page_visits")
        conn.execute("DROP TABLE server_metrics")
        conn.commit()
        # 不应抛异常
        db.add_sign_event("2026-08-16 06:30:00", "13800138000", "success")
        db.add_page_visit("2026-08-16 08:00:00", "admin", "/")
        db.add_server_metric("2026-08-16 08:00:00", cpu=1.0)

    def test_cleanup_removes_expired(self):
        old = (datetime.datetime.now() - datetime.timedelta(days=400)).strftime("%Y-%m-%d %H:%M:%S")
        recent = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        db.add_sign_event(old, "13800138000", "success")
        db.add_page_visit(old, "admin", "/old")
        db.add_server_metric(old, cpu=1.0)
        db.add_sign_event(recent, "13900139000", "success")
        db._event_cleanup(db.get_conn())
        conn = db.get_conn()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM sign_events").fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM page_visits").fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM server_metrics").fetchone()[0], 0)

    def test_read_functions_return_expected_shape(self):
        # 时间戳用相对时间（此前硬编码 2026-08-16 会随真实时钟过期：
        # server_metric_history 按 now 过滤 24h，隔天跑即空——2026-08-17 发现）
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        earlier = (datetime.datetime.now() - datetime.timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
        db.add_sign_event(now, "13800138000", "success", "ok", "signin", 1)
        db.add_sign_event(earlier, "13900139000", "failed", "bad", "signin", 1)
        db.add_page_visit(now, "admin", "/", "hash1", "UA", 100)
        db.add_page_visit(earlier, "admin", "/", "hash2", "UA", 200)
        db.add_server_metric(now, cpu=10.0, mem_pct=50.0)
        sign_stats = db.sign_event_stats(days=30)
        self.assertTrue(sign_stats)
        self.assertIn("day", sign_stats[0])
        self.assertIn("status", sign_stats[0])
        page_stats = db.page_visit_stats(days=30)
        self.assertTrue(page_stats)
        self.assertIn("pv", page_stats[0])
        self.assertIn("uv", page_stats[0])
        history = db.server_metric_history(hours=24)
        self.assertTrue(history)
        self.assertIn("cpu", history[0])

    def test_hash_ip_uses_salt_and_is_stable(self):
        h1 = db.hash_ip("1.2.3.4")
        h2 = db.hash_ip("1.2.3.4")
        h3 = db.hash_ip("5.6.7.8")
        self.assertEqual(h1, h2)
        self.assertNotEqual(h1, h3)
        self.assertEqual(len(h1), 64)


if __name__ == "__main__":
    unittest.main()
