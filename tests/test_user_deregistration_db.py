# -*- coding: utf-8 -*-
"""用户主动注销：数据库层测试（软删除 + 宽限期 + 邮箱复用）。

覆盖：
- 迁移 v5：users 软删除列、部分唯一索引、注销请求表；
- 软注销清理账号和 time_prefs；
- 撤销注销；
- 邮箱复用；
- find_user 只返回有效用户；
- 最后注册管理员判断；
- 注销请求计数；
- 超过宽限期 purge。
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


class UserDeregistrationDbTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-user-dereg-")
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

    def _reset_conn(self):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None

    def _add_account(self, owner, phone):
        db.add_account({
            "name": "测试账号",
            "phone": phone,
            "password": "p1",
            "phone_model": "",
            "phone_code": "",
            "owner": owner,
            "status": "active",
            "reject_reason": "",
        })

    def test_migration_v5_schema(self):
        conn = db.init_db(self.db_file)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 9)
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
        self.assertIn("deleted", cols)
        self.assertIn("deleted_at", cols)
        idx = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_users_email_live'"
        ).fetchone()
        self.assertIsNotNone(idx)
        t = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='user_delete_requests'"
        ).fetchone()
        self.assertIsNotNone(t)
        # v7：注销请求表 kind 列（delete/restore 分流计数）
        rcols = {
            r["name"] for r in conn.execute("PRAGMA table_info(user_delete_requests)").fetchall()
        }
        self.assertIn("kind", rcols)

    def test_soft_delete_marks_accounts_deleted(self):
        """安全审查 2026-08-16：注销时账号改软删除（7 天保留，可随用户一起恢复）。"""
        db.create_user("user1@test.local", "hash", created_at="2026-08-16")
        self._add_account("user1@test.local", "13800138001")
        db.set_time_pref("13800138001", 0, "2026-08-16 10:00:00")
        ok = db.soft_delete_user_with_accounts("user1@test.local")
        self.assertTrue(ok)
        self.assertIsNone(db.find_user("user1@test.local"))
        u_any = db.find_user_any("user1@test.local")
        self.assertIsNotNone(u_any)
        self.assertEqual(u_any["deleted"], 1)
        # 账号软删除保留（不再物理删除）：原始行存在且 deleted=1、deleted_at 非空
        accs = db.load_accounts_raw()
        row = next(a for a in accs if a["phone"] == "13800138001")
        self.assertTrue(row["deleted"])
        self.assertTrue(row["deleted_at"])
        self.assertIsNone(db.get_time_pref("13800138001"))

    def test_restore_user(self):
        db.create_user("user2@test.local", "hash")
        self._add_account("user2@test.local", "13800138002")
        db.soft_delete_user_with_accounts("user2@test.local")
        self.assertTrue(db.restore_user("user2@test.local"))
        u = db.find_user("user2@test.local")
        self.assertIsNotNone(u)
        self.assertEqual(u["deleted"], 0)
        # 安全审查 2026-08-16：恢复联动账号（deleted=0），保证完整接管
        accs = db.load_accounts_raw()
        row = next(a for a in accs if a["phone"] == "13800138002")
        self.assertFalse(row["deleted"])
        self.assertEqual(row["deleted_at"], "")

    def test_restore_blocked_when_active_same_email_exists(self):
        db.create_user("user3@test.local", "hash1")
        db.soft_delete_user_with_accounts("user3@test.local")
        # 邮箱复用：重新注册同一邮箱
        db.create_user("user3@test.local", "hash2")
        self.assertFalse(db.restore_user("user3@test.local"))
        # 活跃用户应保持有效
        self.assertIsNotNone(db.find_user("user3@test.local"))

    def test_email_reuse_after_soft_delete(self):
        db.create_user("user4@test.local", "hash1")
        db.soft_delete_user_with_accounts("user4@test.local")
        db.create_user("user4@test.local", "hash2")
        u = db.find_user("user4@test.local")
        self.assertIsNotNone(u)
        self.assertEqual(u["password_hash"], "hash2")

    def test_load_users_excludes_deleted(self):
        db.create_user("user5@test.local", "hash")
        db.soft_delete_user_with_accounts("user5@test.local")
        db.create_user("user6@test.local", "hash")
        emails = [u["email"] for u in db.load_users()]
        self.assertNotIn("user5@test.local", emails)
        self.assertIn("user6@test.local", emails)

    def test_is_last_registered_admin(self):
        db.create_user("admin1@test.local", "hash", role="admin")
        db.create_user("admin2@test.local", "hash", role="admin")
        self.assertFalse(db.is_last_registered_admin("admin1@test.local"))
        db.soft_delete_user_with_accounts("admin2@test.local")
        self.assertTrue(db.is_last_registered_admin("admin1@test.local"))

    def test_delete_request_count_and_record(self):
        since = (datetime.datetime.now() - datetime.timedelta(minutes=1)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        db.record_user_delete_request("user@test.local", "iphash1")
        db.record_user_delete_request("user@test.local", "iphash1")
        self.assertEqual(db.count_user_delete_requests(username="user@test.local", since_ts=since), 2)
        self.assertEqual(db.count_user_delete_requests(ip_hash="iphash1", since_ts=since), 2)
        self.assertEqual(db.count_user_delete_requests(username="other@test.local", since_ts=since), 0)

    def test_purge_deleted_users(self):
        db.create_user("old@test.local", "hash")
        db.soft_delete_user_with_accounts("old@test.local")
        # 把 deleted_at 改成超过 3 天
        conn = db.get_conn()
        old = (datetime.datetime.now() - datetime.timedelta(days=4)).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute("UPDATE users SET deleted_at=? WHERE email=?", (old, "old@test.local"))
        conn.commit()
        db.purge_deleted_users(days=3)
        self.assertIsNone(db.find_user_any("old@test.local"))

    def test_purge_old_delete_requests(self):
        """对抗审查 2026-08-16：注销请求记录超保留期应被清除（防表无限膨胀）。"""
        db.record_user_delete_request("user@test.local", "iphash1")
        db.record_user_delete_request("user@test.local", "iphash1")
        # 未过期：不清
        db.purge_old_delete_requests(days=30)
        self.assertEqual(
            db.count_user_delete_requests(username="user@test.local"), 2,
            "保留期内记录不应被清理")
        # 改成 31 天前 → 清除
        conn = db.get_conn()
        old = (datetime.datetime.now() - datetime.timedelta(days=31)).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute("UPDATE user_delete_requests SET created_at=?", (old,))
        conn.commit()
        db.purge_old_delete_requests(days=30)
        self.assertEqual(
            db.count_user_delete_requests(username="user@test.local"), 0,
            "超过保留期的请求记录应被清除")


if __name__ == "__main__":
    unittest.main()
