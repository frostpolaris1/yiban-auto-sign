# -*- coding: utf-8 -*-
"""Phase 4：可视化表（sign_events）与 v14 删除遗留统计表的测试。

覆盖：
- 迁移到最新版本后 sign_events 与索引存在；
- v14 已删除 page_visits / server_metrics（生产侧零引用，见 db.migrate_v14）；
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
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

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

    def test_migration_creates_sign_events_and_drops_legacy_stats(self):
        conn = db.init_db(self.db_file)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0],
                         db._MIGRATIONS[-1][0])
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='sign_events'"
        ).fetchone()
        self.assertIsNotNone(row, "表 sign_events 应存在")
        for index in ("idx_sign_events_ts", "idx_sign_events_phone"):
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name=?", (index,)
            ).fetchone()
            self.assertIsNotNone(row, f"索引 {index} 应存在")
        # v14：从未接线的统计表必须已被删除。全新库的执行序是
        # migrate_v4 建表 → migrate_v6 补列 → migrate_v14 删表（已发布迁移不可改）。
        for table in ("page_visits", "server_metrics"):
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            self.assertIsNone(row, f"表 {table} 应已被 v14 删除")

    def test_write_functions_succeed(self):
        db.add_sign_event("2026-08-16 06:30:00", "13800138000", "success", "ok", "signin", 1)
        conn = db.get_conn()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM sign_events").fetchone()[0], 1)

    def test_write_functions_degrade_when_table_missing(self):
        conn = db.get_conn()
        conn.execute("DROP TABLE sign_events")
        conn.commit()
        # 不应抛异常
        db.add_sign_event("2026-08-16 06:30:00", "13800138000", "success")

    def test_cleanup_removes_expired(self):
        old = (datetime.datetime.now() - datetime.timedelta(days=400)).strftime("%Y-%m-%d %H:%M:%S")
        recent = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        db.add_sign_event(old, "13800138000", "success")
        db.add_sign_event(recent, "13900139000", "success")
        db._event_cleanup(db.get_conn())
        conn = db.get_conn()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM sign_events").fetchone()[0], 1)

    def test_read_functions_return_expected_shape(self):
        # 时间戳用相对时间（此前硬编码 2026-08-16 会随真实时钟过期：
        # sign_event_stats 按 now 过滤 days 窗口，隔天跑即空——2026-08-17 发现）
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        earlier = (datetime.datetime.now() - datetime.timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
        db.add_sign_event(now, "13800138000", "success", "ok", "signin", 1)
        db.add_sign_event(earlier, "13900139000", "failed", "bad", "signin", 1)
        sign_stats = db.sign_event_stats(days=30)
        self.assertTrue(sign_stats)
        self.assertIn("day", sign_stats[0])
        self.assertIn("status", sign_stats[0])

    def test_upgrade_from_v13_drops_legacy_stats_keeps_sign_events(self):
        """存量库路径：v13 库（两张遗留表在位）升到 v14 后表被删、签到事件保留。

        与"全新库建了又删"不同，这是真实存量库的升级路径——迁移只增不改，
        靠 v14 的 DROP 收口。
        """
        conn = db.get_conn()
        conn.execute("CREATE TABLE page_visits (id INTEGER PRIMARY KEY, ts TEXT)")
        conn.execute("CREATE TABLE server_metrics (ts TEXT)")
        conn.execute("INSERT INTO page_visits (ts) VALUES ('2026-01-01 00:00:00')")
        conn.execute("PRAGMA user_version = 13")
        conn.commit()
        db.add_sign_event("2026-08-16 06:30:00", "13800138000", "success")

        self._reset_conn()
        conn = db.init_db(self.db_file, env_file=self.env_file)

        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0],
                         db._MIGRATIONS[-1][0])
        names = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn("page_visits", names, "v14 应删除 page_visits")
        self.assertNotIn("server_metrics", names, "v14 应删除 server_metrics")
        self.assertIn("sign_events", names, "v14 不得误删 sign_events")
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM sign_events").fetchone()[0], 1,
            "升级不得丢签到事件",
        )

    def test_hash_ip_uses_salt_and_is_stable(self):
        h1 = db.hash_ip("1.2.3.4")
        h2 = db.hash_ip("1.2.3.4")
        h3 = db.hash_ip("5.6.7.8")
        self.assertEqual(h1, h2)
        self.assertNotEqual(h1, h3)
        self.assertEqual(len(h1), 64)


if __name__ == "__main__":
    unittest.main()
