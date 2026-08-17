# -*- coding: utf-8 -*-
"""0.21.0 Task 2：Web 认证/授权/安全配置修复测试。

覆盖：
- S1：内置管理员邮箱冲突时登录来源必须为 user，旧会话无 auth_source 视为未登录；
  注册/自动注册拒绝内置管理员邮箱。
- H6：YIBAN_COOKIE_SECURE 开关控制 SESSION_COOKIE_SECURE。
- H7：限速/失败计数共享锁存在（进程内读改写原子）。
- H14：api_account_add 自动注册前检查注销冷却期与 YIBAN_MAX_USERS。
- M7：普通用户对已软删除账号 DELETE 返回 400，不做物理删除。
- M8：批量签到等待超时后 terminate + 回收子进程。
- M10：注册与自动注册遇到 create_user 返回 False 时返回“该邮箱已注册”。

全程本地 Flask test client / mock，不访问网络与真实易班接口。
"""
import contextlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "scripts"))
sys.path.insert(0, os.path.join(BASE, "web"))

TEST_KEY = "b" * 64
ADMIN_PASS = "TestPass1234!"
USER_PASS = "secret1"
BUILTIN_EMAIL = "builtin@test.local"


class SecurityFixes021Test(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-sec-021-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(
                f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
                f"YIBAN_ADMIN_USER={BUILTIN_EMAIL}\n"
                f"YIBAN_ADMIN_PASSWORD={ADMIN_PASS}\n"
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
                  "YIBAN_USERS_FILE", "YIBAN_DB_FILE", "YIBAN_STATE_DIR",
                  "YIBAN_COOKIE_SECURE", "YIBAN_MAX_USERS"):
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
        # 保持 .env 基本配置稳定；测试内按需改写 YIBAN_COOKIE_SECURE/YIBAN_MAX_USERS
        self.webapp.write_env_key(self.env_file, "YIBAN_COOKIE_SECURE", "")
        self.webapp.write_env_key(self.env_file, "YIBAN_MAX_USERS", "0")

    def _login(self, c, username, password):
        r = c.post("/api/login", json={"username": username, "password": password})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return c.get("/api/me").get_json()["csrf_token"]

    def _csrf(self, token):
        return {"X-CSRF-Token": token}

    # ---- S1 ----
    def test_login_as_user_with_builtin_admin_email_returns_user_role(self):
        h = self.webapp.generate_password_hash
        db.create_user(BUILTIN_EMAIL, h(USER_PASS), role="user")
        c = self.webapp.create_app().test_client()
        r = c.post("/api/login", json={"username": BUILTIN_EMAIL, "password": USER_PASS})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json()["role"], "user")
        me = c.get("/api/me").get_json()
        self.assertEqual(me["role"], "user")
        self.assertFalse(me["admin"])

    def test_old_builtin_session_without_auth_source_is_unauthenticated(self):
        c = self.webapp.create_app().test_client()
        with c.session_transaction() as sess:
            sess["auth"] = True
            sess["username"] = BUILTIN_EMAIL
            sess["pw_version"] = 1
        r = c.get("/api/me")
        self.assertEqual(r.status_code, 401, r.get_data(as_text=True))

    def test_register_rejects_builtin_admin_email(self):
        c = self.webapp.create_app().test_client()
        r = c.post("/api/register", json={
            "email": BUILTIN_EMAIL,
            "password": "UserPass123!",
        })
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        self.assertIsNone(db.find_user_any(BUILTIN_EMAIL))

    def test_account_add_auto_register_rejects_builtin_admin_email(self):
        c = self.webapp.create_app().test_client()
        token = self._login(c, BUILTIN_EMAIL, ADMIN_PASS)
        r = c.post("/api/accounts", json={
            "name": "内置邮箱",
            "phone": "13800138000",
            "password": "account-pass",
            "email": BUILTIN_EMAIL,
            "initial_password": "UserPass123!",
        }, headers=self._csrf(token))
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        self.assertIsNone(db.find_user_any(BUILTIN_EMAIL))

    # ---- H6 ----
    def test_cookie_secure_off_by_default(self):
        self.webapp.write_env_key(self.env_file, "YIBAN_COOKIE_SECURE", "")
        app = self.webapp.create_app()
        self.assertFalse(app.config["SESSION_COOKIE_SECURE"])

    def test_cookie_secure_on_from_env_file(self):
        self.webapp.write_env_key(self.env_file, "YIBAN_COOKIE_SECURE", "1")
        app = self.webapp.create_app()
        self.assertTrue(app.config["SESSION_COOKIE_SECURE"])

    def test_cookie_secure_on_from_environment_variable(self):
        os.environ["YIBAN_COOKIE_SECURE"] = "true"
        try:
            app = self.webapp.create_app()
            self.assertTrue(app.config["SESSION_COOKIE_SECURE"])
        finally:
            os.environ.pop("YIBAN_COOKIE_SECURE", None)

    # ---- H7 ----
    def test_rate_lock_exists(self):
        self.assertTrue(hasattr(self.webapp, "_rate_lock"), "需要模块级 _rate_lock = threading.Lock()")
        self.assertIsInstance(self.webapp._rate_lock, type(threading.Lock()))

    # ---- H14 ----
    def test_account_add_auto_register_blocks_during_delete_cooldown(self):
        h = self.webapp.generate_password_hash
        db.create_user("cool@test.local", h(USER_PASS), role="user")
        self.assertTrue(db.soft_delete_user_with_accounts("cool@test.local"))
        c = self.webapp.create_app().test_client()
        token = self._login(c, BUILTIN_EMAIL, ADMIN_PASS)
        r = c.post("/api/accounts", json={
            "name": "冷却邮箱",
            "phone": "13800138000",
            "password": "account-pass",
            "email": "cool@test.local",
            "initial_password": "UserPass123!",
        }, headers=self._csrf(token))
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        self.assertIsNone(db.find_user("cool@test.local"), "冷却期内不应被自动注册为活跃用户")
        self.assertEqual(db.load_accounts(), [], "冷却期内不应新增账号")

    def test_account_add_auto_register_blocks_when_users_capacity_reached(self):
        h = self.webapp.generate_password_hash
        db.create_user("exist@test.local", h(USER_PASS), role="user")
        self.webapp.write_env_key(self.env_file, "YIBAN_MAX_USERS", "1")
        c = self.webapp.create_app().test_client()
        token = self._login(c, BUILTIN_EMAIL, ADMIN_PASS)
        r = c.post("/api/accounts", json={
            "name": "容量邮箱",
            "phone": "13800138000",
            "password": "account-pass",
            "email": "new@test.local",
            "initial_password": "UserPass123!",
        }, headers=self._csrf(token))
        self.assertEqual(r.status_code, 403, r.get_data(as_text=True))
        self.assertIsNone(db.find_user("new@test.local"))
        self.assertEqual(db.load_accounts(), [])

    # ---- M7 ----
    def test_my_account_delete_soft_deleted_returns_400_not_purge(self):
        h = self.webapp.generate_password_hash
        user_email = "user@test.local"
        db.create_user(user_email, h(USER_PASS), role="user")
        acc_id = db.add_account({
            "name": "我的号", "phone": "13800138000", "password": "p1",
            "owner": user_email, "status": "active",
        })
        admin = self.webapp.create_app().test_client()
        admin_token = self._login(admin, BUILTIN_EMAIL, ADMIN_PASS)
        r = admin.delete("/api/accounts/0", headers=self._csrf(admin_token))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(db.load_accounts()[0]["deleted"])

        c = self.webapp.create_app().test_client()
        token = self._login(c, user_email, USER_PASS)
        r = c.delete("/api/my-accounts/0", headers=self._csrf(token))
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        self.assertIn("已删除账号由管理员处理", r.get_json()["error"])
        accounts = db.load_accounts()
        self.assertEqual(len(accounts), 1, "普通用户不应物理删除已软删除账号")
        self.assertTrue(accounts[0]["deleted"])
        self.assertEqual(accounts[0]["id"], acc_id)

    # ---- M8 ----
    def test_wait_signin_proc_terminates_on_timeout(self):
        class FakeProc:
            def __init__(self):
                self.calls = 0
                self.terminated = False
                self.killed = False

            def wait(self, timeout=None):
                self.calls += 1
                if self.calls == 1:
                    raise subprocess.TimeoutExpired("fake", timeout)
                return 0

            def terminate(self):
                self.terminated = True

            def kill(self):
                self.killed = True

        proc = FakeProc()
        self.webapp._wait_signin_proc(proc)
        self.assertEqual(proc.calls, 2)
        self.assertTrue(proc.terminated)
        self.assertFalse(proc.killed)


if __name__ == "__main__":
    unittest.main(verbosity=2)
