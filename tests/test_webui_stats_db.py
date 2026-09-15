# -*- coding: utf-8 -*-
"""签到事件表（sign_events）补齐（v6）测试。

覆盖：
- v6 迁移：sign_events 新列/索引；
- 写入函数新字段；
- 批量写入；
- 按手机号 / 按时间窗查询。

原文件同时测过 page_visits / server_metrics 与 sign_event_peak /
sign_event_summary_today，这几项生产侧零引用，已由 v14 迁移删除（见 db.migrate_v14），
对应用例随之移除。
"""
import contextlib
import datetime
import os
import shutil
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TEST_KEY = "a" * 64


def _recent(days=0, hours=0, minutes=0):
    """距今给定偏移的 ts 字面量。

    查询函数（sign_events_by_phone / sign_events_since）按 days 窗口裁剪，写死日期
    会在窗口滑过该日期后假红。
    """
    delta = datetime.timedelta(days=days, hours=hours, minutes=minutes)
    return (datetime.datetime.now() - delta).strftime("%Y-%m-%d %H:%M:%S")


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
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0],
                         db._MIGRATIONS[-1][0])
        sign_cols = {r["name"] for r in conn.execute("PRAGMA table_info(sign_events)").fetchall()}
        self.assertIn("account_id", sign_cols)
        self.assertIn("dur_sec", sign_cols)
        self.assertIn("finished_at", sign_cols)
        for index in ("idx_sign_events_phone_ts", "idx_sign_events_account_ts"):
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

    def test_batch_write_functions(self):
        db.add_sign_events_batch([
            {"ts": "2026-08-16 06:30:00", "phone": "13800138000", "status": "success",
             "account_id": 1, "dur_sec": 1.0, "finished_at": "2026-08-16 06:30:01"},
            {"ts": "2026-08-16 06:31:00", "phone": "13900139000", "status": "failed"},
        ])
        conn = db.get_conn()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM sign_events").fetchone()[0], 2)

    def test_sign_events_by_phone(self):
        db.add_sign_event(_recent(hours=2), "13800138000", "success")
        db.add_sign_event(_recent(hours=1), "13900139000", "failed")
        rows = db.sign_events_by_phone("13800138000", days=30)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["phone"], "13800138000")

    def test_sign_events_since(self):
        db.add_sign_event("2026-08-16 06:30:00", "13800138000", "success")
        rows = db.sign_events_since("2026-08-16 06:00:00", limit=10)
        self.assertEqual(len(rows), 1)
        rows2 = db.sign_events_since("2026-08-16 06:30:01", limit=10)
        self.assertEqual(len(rows2), 0)


if __name__ == "__main__":
    unittest.main()
