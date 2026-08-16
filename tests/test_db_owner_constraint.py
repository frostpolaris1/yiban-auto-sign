# -*- coding: utf-8 -*-
"""Phase 2：每人限 1 账号 DB 约束 + 可区分错误码测试。

覆盖：
- 新库迁移到版本 2 并创建唯一索引；
- 历史重复数据导致迁移延后，清理后自动建索引；
- db.add_account 手机号/owner 重复抛可区分异常；
- 软删除账号不占名额；
- admin 可多个账号；
- API 添加重复 owner 返回 400。
"""
import contextlib
import importlib.util
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "scripts"))
sys.path.insert(0, os.path.join(BASE, "web"))

TEST_KEY = "a" * 64
ADMIN_PASS = "TestPass1234!"
USER_PASS = "secret1"


class DbOwnerConstraintTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-owner-constraint-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(
                f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
                f"YIBAN_ADMIN_USER=admin\nYIBAN_ADMIN_PASSWORD={ADMIN_PASS}\n"
            )
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.accounts_file = os.path.join(cls.tmp, "accounts.json")
        cls.users_file = os.path.join(cls.tmp, "users.json")
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_ACCOUNTS_FILE"] = cls.accounts_file
        os.environ["YIBAN_USERS_FILE"] = cls.users_file
        os.environ["YIBAN_DB_FILE"] = cls.db_file
        os.environ["YIBAN_STATE_DIR"] = cls.tmp
        os.environ["YIBAN_LOG_FILE"] = os.path.join(cls.tmp, "sign.log")
        global db
        import db
        spec = importlib.util.spec_from_file_location("webapp", os.path.join(BASE, "web", "app.py"))
        cls.webapp = importlib.util.module_from_spec(spec)
        sys.modules["webapp"] = cls.webapp
        with contextlib.suppress(Exception):
            spec.loader.exec_module(cls.webapp)

    @classmethod
    def tearDownClass(cls):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k in ("YIBAN_ACCOUNTS_KEY", "YIBAN_LOG_FILE", "YIBAN_STATE_DIR"):
            os.environ.pop(k, None)

    def setUp(self):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        for suffix in ("", "-wal", "-shm"):
            p = self.db_file + suffix
            if os.path.exists(p):
                os.remove(p)
        with open(self.accounts_file, "w", encoding="utf-8") as f:
            json.dump([], f)
        db.init_db(self.db_file, migrate_from=self.accounts_file, env_file=self.env_file)
        db.create_user("user1@test.local", self.webapp.generate_password_hash(USER_PASS))

    def _reset_conn(self):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None

    def _add_account(self, owner, phone, status="active", deleted=0):
        return db.add_account({
            "name": "测试账号",
            "phone": phone,
            "password": "p1",
            "phone_model": "",
            "phone_code": "",
            "owner": owner,
            "status": status,
            "reject_reason": "",
            "deleted": deleted,
        })

    def _login(self, c, username, password):
        r = c.post("/api/login", json={"username": username, "password": password})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return c.get("/api/me").get_json()["csrf_token"]

    def _csrf(self, token):
        return {"X-CSRF-Token": token}

    # ---- 1. 新库版本 2 + 唯一索引存在 ----
    def test_new_db_has_version_2_and_index(self):
        self._reset_conn()
        conn = db.init_db(self.db_file)
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        self.assertEqual(version, 5)
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_accounts_owner_live'"
        ).fetchone()
        self.assertIsNotNone(row)

    # ---- 2. 历史重复数据：迁移延后，清理后自动建索引 ----
    def test_duplicate_owner_defers_migration_then_creates_after_cleanup(self):
        self._reset_conn()
        # 清掉 setUp 已建的库，手工构造一个 user_version=1 且已有重复 owner 的库
        for suffix in ("", "-wal", "-shm"):
            p = self.db_file + suffix
            if os.path.exists(p):
                os.remove(p)
        conn = sqlite3.connect(self.db_file)
        try:
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
                  deleted_at TEXT NOT NULL DEFAULT '',
                  user_paused INTEGER NOT NULL DEFAULT 0
                );
                """
            )
            conn.execute("PRAGMA user_version = 1")
            conn.execute(
                "INSERT INTO accounts (sort_order, name, phone, owner, status) VALUES (1,'A','13800138001','user1@test.local','active')"
            )
            conn.execute(
                "INSERT INTO accounts (sort_order, name, phone, owner, status) VALUES (2,'B','13900139002','user1@test.local','active')"
            )
            conn.commit()
        finally:
            conn.close()

        # 第一次启动：检测到重复，延后，不建索引，版本保持 1
        conn = db.init_db(self.db_file)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_accounts_owner_live'"
        ).fetchone()
        self.assertIsNone(row)

        # 人工清理：删掉一个重复账号
        conn.execute("DELETE FROM accounts WHERE phone='13900139002'")
        conn.commit()
        self._reset_conn()

        # 第二次启动：无重复，自动建索引，版本升到 2
        conn = db.init_db(self.db_file)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 5)
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_accounts_owner_live'"
        ).fetchone()
        self.assertIsNotNone(row)

    # ---- 3. 手机号重复抛 DuplicatePhoneError ----
    def test_add_account_duplicate_phone(self):
        self._add_account("admin", "13800138010")
        with self.assertRaises(db.DuplicatePhoneError):
            self._add_account("admin", "13800138010")

    # ---- 4. 同一 owner 第二个未删除账号抛 DuplicateOwnerError ----
    def test_add_account_duplicate_owner(self):
        self._add_account("user1@test.local", "13800138011")
        with self.assertRaises(db.DuplicateOwnerError):
            self._add_account("user1@test.local", "13900139012")

    # ---- 5. 软删除账号不占名额 ----
    def test_soft_deleted_does_not_block_new_account(self):
        acc_id = self._add_account("user1@test.local", "13800138013")
        db.set_account_deleted(acc_id, 1, "2026-08-16T00:00:00")
        new_id = self._add_account("user1@test.local", "13900139014")
        self.assertGreater(new_id, 0)

    # ---- 6. admin 可多个账号 ----
    def test_admin_multiple_accounts_allowed(self):
        self._add_account("admin", "13800138015")
        self._add_account("admin", "13900139016")

    # ---- 7. API 添加重复 owner 返回 400 ----
    def test_api_add_duplicate_owner_returns_400(self):
        self._add_account("user1@test.local", "13800138017")
        c = self.webapp.create_app().test_client()
        token = self._login(c, "admin", ADMIN_PASS)
        r = c.post(
            "/api/accounts",
            json={
                "name": "测试账号",
                "phone": "13900139018",
                "password": "p1",
                "email": "user1@test.local",
                "initial_password": "InitialPass123!",
            },
            headers=self._csrf(token),
        )
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        self.assertIn("已有一个账号", r.get_json()["error"])


if __name__ == "__main__":
    unittest.main()
