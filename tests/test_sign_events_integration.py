# -*- coding: utf-8 -*-
"""signin.py 写入 sign_events 的集成测试（P1）。

覆盖：
- 状态口径映射；
- _write_sign_event 写入 DB（含 account_id/dur_sec/finished_at）。
"""
import contextlib
import os
import shutil
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "scripts"))

import db  # noqa: E402
import signin  # noqa: E402

TEST_KEY = "a" * 64


class SignEventsIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-sign-events-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.accounts_file = os.path.join(cls.tmp, "accounts.json")
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_ACCOUNTS_FILE"] = cls.accounts_file
        os.environ["YIBAN_DB_FILE"] = cls.db_file
        db.init_db(cls.db_file, env_file=cls.env_file)

    @classmethod
    def tearDownClass(cls):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        os.environ.pop("YIBAN_ACCOUNTS_KEY", None)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_status_mapping(self):
        cases = {
            signin.STATUS_SUCCESS: "success",
            signin.STATUS_ALREADY: "success",
            signin.STATUS_FAILED: "failed",
            signin.STATUS_RETRYING: "failed",
            signin.STATUS_SKIPPED_WINDOW: "skipped",
            signin.STATUS_SKIPPED_NORANGE: "skipped",
            signin.STATUS_PAUSED: "paused",
            signin.STATUS_USER_CANCELLED: "paused",
            signin.STATUS_NO_TASK: "no_task",
        }
        for src, expected in cases.items():
            self.assertEqual(signin._sign_event_status(src), expected)

    def test_write_sign_event_writes_db(self):
        acc = signin.Account(phone="13800138000", password="p", id=123)
        signin._write_sign_event(
            acc,
            signin.STATUS_SUCCESS,
            "签到成功",
            stage="signin",
            attempt=1,
            dur_sec=1.5,
            finished_at="2026-08-16 06:30:02",
        )
        conn = db.get_conn()
        row = conn.execute("SELECT * FROM sign_events").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "success")
        self.assertEqual(row["account_id"], 123)
        self.assertEqual(row["dur_sec"], 1.5)
        self.assertEqual(row["finished_at"], "2026-08-16 06:30:02")


if __name__ == "__main__":
    unittest.main()
