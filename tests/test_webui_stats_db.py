# -*- coding: utf-8 -*-
"""WebUI 统计/监控 DB 补齐（v6）测试。

覆盖：
- v6 迁移：新列/索引；
- 写入函数新字段；
- 批量写入；
- 新增查询函数。
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


class WebuiStatsDbTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-webui-stats-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.accounts_file = os.path.join(cls.tmp, "accounts.json")
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
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

    def test_migration_v6_schema(self):
        conn = db.init_db(self.db_file)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 12)
        sign_cols = {r["name"] for r in conn.execute("PRAGMA table_info(sign_events)").fetchall()}
        self.assertIn("account_id", sign_cols)
        self.assertIn("dur_sec", sign_cols)
        self.assertIn("finished_at", sign_cols)
        page_cols = {r["name"] for r in conn.execute("PRAGMA table_info(page_visits)").fetchall()}
        self.assertIn("user_id", page_cols)
        for index in ("idx_sign_events_phone_ts", "idx_sign_events_account_ts",
                      "idx_page_visits_path_ts", "idx_page_visits_role_ts"):
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name=?", (index,)
            ).fetchone()
            self.assertIsNotNone(row, f"索引 {index} 应存在")

    def test_add_sign_event_with_new_fields(self):
        db.add_sign_event("2026-08-16 06:30:00", "13800138000", "success",
                          account_id=1, dur_sec=1.5, finished_at="2026-08-16 06:30:02")
        conn = db.get_conn()
        row = conn.execute("SELECT * FROM sign_events").fetchone()
        self.assertEqual(row["account_id"], 1)
        self.assertEqual(row["dur_sec"], 1.5)
        self.assertEqual(row["finished_at"], "2026-08-16 06:30:02")

    def test_add_page_visit_with_user_id(self):
        db.add_page_visit("2026-08-16 08:00:00", "user", "/", user_id=42)
        conn = db.get_conn()
        row = conn.execute("SELECT * FROM page_visits").fetchone()
        self.assertEqual(row["user_id"], 42)

    def test_batch_write_functions(self):
        db.add_sign_events_batch([
            {"ts": "2026-08-16 06:30:00", "phone": "13800138000", "status": "success",
             "account_id": 1, "dur_sec": 1.0, "finished_at": "2026-08-16 06:30:01"},
            {"ts": "2026-08-16 06:31:00", "phone": "13900139000", "status": "failed"},
        ])
        db.add_page_visits_batch([
            {"ts": "2026-08-16 08:00:00", "role": "user", "path": "/", "user_id": 1},
            {"ts": "2026-08-16 08:01:00", "role": "admin", "path": "/login", "user_id": 2},
        ])
        conn = db.get_conn()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM sign_events").fetchone()[0], 2)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM page_visits").fetchone()[0], 2)

    def test_sign_events_by_phone(self):
        db.add_sign_event("2026-08-16 06:30:00", "13800138000", "success")
        db.add_sign_event("2026-08-16 06:31:00", "13900139000", "failed")
        rows = db.sign_events_by_phone("13800138000", days=30)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["phone"], "13800138000")

    def test_sign_events_since(self):
        db.add_sign_event("2026-08-16 06:30:00", "13800138000", "success")
        rows = db.sign_events_since("2026-08-16 06:00:00", limit=10)
        self.assertEqual(len(rows), 1)
        rows2 = db.sign_events_since("2026-08-16 06:30:01", limit=10)
        self.assertEqual(len(rows2), 0)

    def test_sign_event_peak(self):
        # 用"昨天"时间，确保落在 days=7 窗口内（避免写死日期随时间过期）
        base = (datetime.datetime.now() - datetime.timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        db.add_sign_event(base, "13800138000", "success")
        db.add_sign_event(base, "13900139000", "failed")
        peak = db.sign_event_peak(days=7, bucket_minutes=5)
        self.assertTrue(peak)
        self.assertIn("bucket", peak[0])
        self.assertIn("cnt", peak[0])

    def test_sign_event_summary_today(self):
        db.add_sign_event(datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                          "13800138000", "success")
        db.add_sign_event(datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                          "13900139000", "failed")
        summary = db.sign_event_summary_today()
        self.assertEqual(len(summary), 2)

    def test_page_visit_hourly(self):
        db.add_page_visit("2026-08-16 08:00:00", "user", "/")
        db.add_page_visit("2026-08-16 09:00:00", "admin", "/login")
        hourly = db.page_visit_hourly(days=30)
        self.assertTrue(hourly)
        self.assertIn("hour", hourly[0])
        self.assertIn("pv", hourly[0])

    def test_page_visit_top_paths(self):
        db.add_page_visit("2026-08-16 08:00:00", "user", "/")
        db.add_page_visit("2026-08-16 08:01:00", "user", "/")
        db.add_page_visit("2026-08-16 08:02:00", "admin", "/login")
        top = db.page_visit_top_paths(days=30, limit=10)
        self.assertEqual(top[0]["path"], "/")
        self.assertEqual(top[0]["cnt"], 2)

    def test_page_visit_active_users(self):
        db.add_page_visit("2026-08-16 08:00:00", "user", "/", user_id=1)
        db.add_page_visit("2026-08-16 08:01:00", "user", "/", user_id=2)
        db.add_page_visit("2026-08-16 08:02:00", "anonymous", "/", user_id=None)
        active = db.page_visit_active_users(days=30)
        self.assertEqual(active, 2)

    def test_server_metric_latest(self):
        db.add_server_metric("2026-08-16 08:00:00", cpu=1.0)
        db.add_server_metric("2026-08-16 08:01:00", cpu=2.0)
        latest = db.server_metric_latest(limit=1)
        self.assertEqual(len(latest), 1)
        self.assertEqual(latest[0]["cpu"], 2.0)


if __name__ == "__main__":
    unittest.main()
