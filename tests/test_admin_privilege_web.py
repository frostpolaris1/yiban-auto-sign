# -*- coding: utf-8 -*-
"""管理员目标操作权限测试（安全审查 2026-08 修复验证）。

覆盖：普通管理员不可重置/删除其他注册管理员（单条 403、批量软跳过）；
主管理员（.env 内置）可重置/删除注册管理员；普通管理员对普通用户的重置/删除不受影响。
口径与「改角色仅主管理员」一致（/api/users/<email>/role）。

全程 mock / 纯本地（Flask test client），无任何网络请求。
用法（项目根目录）：
    py -m pytest tests/test_admin_privilege_web.py -v
"""
import contextlib
import importlib.util
import json
import os
import shutil
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
NEW_PASS = "NewPass123!"


class AdminPrivilegeWebTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-admin-priv-")
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
        for k in ("YIBAN_ACCOUNTS_KEY", "YIBAN_ENV_FILE", "YIBAN_ACCOUNTS_FILE",
                  "YIBAN_USERS_FILE", "YIBAN_DB_FILE", "YIBAN_STATE_DIR"):
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
        h = self.webapp.generate_password_hash
        # 内置管理员 = .env 的 admin；注册管理员两个 + 普通用户一个
        db.create_user("admin2@test.local", h(ADMIN_PASS), role="admin")
        db.create_user("admin3@test.local", h(ADMIN_PASS), role="admin")
        db.create_user("user1@test.local", h(USER_PASS))

    def _login(self, c, username, password):
        r = c.post("/api/login", json={"username": username, "password": password})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return c.get("/api/me").get_json()["csrf_token"]

    def _csrf(self, token):
        return {"X-CSRF-Token": token}

    def _pw_version(self, email):
        u = db.find_user(email)
        return u.get("pw_version", 1) if u else None

    # ---- 单条路径 ----
    def test_regular_admin_cannot_reset_admin_password(self):
        c = self.webapp.create_app().test_client()
        token = self._login(c, "admin2@test.local", ADMIN_PASS)
        r = c.post("/api/users/admin3@test.local/password",
                   json={"password": NEW_PASS}, headers=self._csrf(token))
        self.assertEqual(r.status_code, 403, r.get_data(as_text=True))
        self.assertIn("仅主管理员", r.get_json()["error"])
        self.assertEqual(self._pw_version("admin3@test.local"), 1, "目标管理员 pw_version 不应变化")
        # 目标管理员旧密码应仍可登录（未被改动）
        c3 = self.webapp.create_app().test_client()
        r = c3.post("/api/login", json={"username": "admin3@test.local", "password": ADMIN_PASS})
        self.assertEqual(r.status_code, 200, "目标管理员旧密码应仍有效")

    def test_regular_admin_can_reset_normal_user(self):
        c = self.webapp.create_app().test_client()
        token = self._login(c, "admin2@test.local", ADMIN_PASS)
        r = c.post("/api/users/user1@test.local/password",
                   json={"password": NEW_PASS}, headers=self._csrf(token))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(self._pw_version("user1@test.local"), 2)
        c1 = self.webapp.create_app().test_client()
        r = c1.post("/api/login", json={"username": "user1@test.local", "password": NEW_PASS})
        self.assertEqual(r.status_code, 200, "普通用户新密码应可登录")

    def test_regular_admin_cannot_delete_admin(self):
        c = self.webapp.create_app().test_client()
        token = self._login(c, "admin2@test.local", ADMIN_PASS)
        for mode in ("full", "accounts_only"):
            r = c.post("/api/users/admin3@test.local/delete",
                       json={"mode": mode}, headers=self._csrf(token))
            self.assertEqual(r.status_code, 403, r.get_data(as_text=True))
        self.assertIsNotNone(db.find_user("admin3@test.local"), "管理员不应被删除")

    def test_regular_admin_can_delete_normal_user(self):
        c = self.webapp.create_app().test_client()
        token = self._login(c, "admin2@test.local", ADMIN_PASS)
        r = c.post("/api/users/user1@test.local/delete",
                   json={"mode": "full"}, headers=self._csrf(token))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertIsNone(db.find_user("user1@test.local"))

    def test_master_can_reset_and_delete_admin(self):
        c = self.webapp.create_app().test_client()
        token = self._login(c, "admin", ADMIN_PASS)
        r = c.post("/api/users/admin3@test.local/password",
                   json={"password": NEW_PASS}, headers=self._csrf(token))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(self._pw_version("admin3@test.local"), 2)
        r = c.post("/api/users/admin3@test.local/delete",
                   json={"mode": "full"}, headers=self._csrf(token))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertIsNone(db.find_user("admin3@test.local"))

    # ---- 批量路径 ----
    def test_batch_reset_skips_admin_for_regular_admin(self):
        c = self.webapp.create_app().test_client()
        token = self._login(c, "admin2@test.local", ADMIN_PASS)
        r = c.post("/api/users/batch",
                   json={"action": "reset_password",
                         "emails": ["admin3@test.local", "user1@test.local"],
                         "password": NEW_PASS},
                   headers=self._csrf(token))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertIn("已重置密码 1 个用户", r.get_json()["msg"])
        self.assertEqual(self._pw_version("admin3@test.local"), 1, "管理员目标应被跳过")
        self.assertEqual(self._pw_version("user1@test.local"), 2, "普通用户应被重置")

    def test_batch_delete_skips_admin_for_regular_admin(self):
        c = self.webapp.create_app().test_client()
        token = self._login(c, "admin2@test.local", ADMIN_PASS)
        r = c.post("/api/users/batch",
                   json={"action": "delete",
                         "emails": ["admin3@test.local", "user1@test.local"]},
                   headers=self._csrf(token))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertIn("已删除 1 个用户", r.get_json()["msg"])
        self.assertIsNotNone(db.find_user("admin3@test.local"), "管理员目标应被跳过")
        self.assertIsNone(db.find_user("user1@test.local"))

    def test_master_batch_can_include_admin(self):
        c = self.webapp.create_app().test_client()
        token = self._login(c, "admin", ADMIN_PASS)
        r = c.post("/api/users/batch",
                   json={"action": "reset_password",
                         "emails": ["admin2@test.local", "admin3@test.local"],
                         "password": NEW_PASS},
                   headers=self._csrf(token))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertIn("已重置密码 2 个用户", r.get_json()["msg"])
        self.assertEqual(self._pw_version("admin2@test.local"), 2)
        self.assertEqual(self._pw_version("admin3@test.local"), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
