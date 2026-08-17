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
from unittest import mock

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
        # 导入错误必须直接暴露，不能吞掉
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

    def test_registered_admin_with_builtin_email_is_not_builtin_session(self):
        h = self.webapp.generate_password_hash
        db.create_user(BUILTIN_EMAIL, h(USER_PASS), role="admin")
        db.add_account({
            "name": "内置共享号", "phone": "13800138000", "password": "p1",
            "owner": "admin", "status": "active",
        })
        c = self.webapp.create_app().test_client()
        r = c.post("/api/login", json={"username": BUILTIN_EMAIL, "password": USER_PASS})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json()["role"], "admin")
        me = c.get("/api/me").get_json()
        self.assertEqual(me["role"], "admin")
        self.assertFalse(me["is_builtin_admin"], "同邮箱注册管理员不应被识别为内置主管理员")
        my_accounts = c.get("/api/my-accounts").get_json()
        self.assertEqual(my_accounts["accounts"], [], "注册管理员不应看到内置管理员的共享账号")

    def test_old_builtin_session_without_auth_source_is_unauthenticated(self):
        c = self.webapp.create_app().test_client()
        with c.session_transaction() as sess:
            sess["auth"] = True
            sess["username"] = BUILTIN_EMAIL
            sess["pw_version"] = 1
        r = c.get("/api/me")
        self.assertEqual(r.status_code, 401, r.get_data(as_text=True))

    def test_old_user_session_without_auth_source_is_unauthenticated(self):
        c = self.webapp.create_app().test_client()
        with c.session_transaction() as sess:
            sess["auth"] = True
            sess["username"] = "user@test.local"
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

    # ---- I3：.env 敏感键原子写入 ----
    def test_migrate_admin_password_to_hash_clears_plain_and_sets_hash(self):
        env_file = os.path.join(self.tmp, "migrate.env")
        with open(env_file, "w", encoding="utf-8") as f:
            f.write("YIBAN_ADMIN_PASSWORD=OldPass123!\n")
        self.webapp.migrate_admin_password_to_hash(env_file)
        with open(env_file, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("YIBAN_ADMIN_PASSWORD_HASH=", content)
        self.assertNotIn("YIBAN_ADMIN_PASSWORD=", content)

    def test_builtin_admin_password_change_updates_env_atomically(self):
        c = self.webapp.create_app().test_client()
        token = self._login(c, BUILTIN_EMAIL, ADMIN_PASS)
        new_pass = "NewPass1234!"
        try:
            r = c.post("/api/me/password", json={
                "old_password": ADMIN_PASS,
                "new_password": new_pass,
                "confirm_password": new_pass,
            }, headers=self._csrf(token))
            self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
            with open(self.env_file, encoding="utf-8") as f:
                content = f.read()
            self.assertNotIn("YIBAN_ADMIN_PASSWORD=", content)
            self.assertIn("YIBAN_ADMIN_PASSWORD_HASH=", content)
            self.assertIn("YIBAN_ADMIN_PW_VERSION=2", content)
        finally:
            self.webapp.write_env_batch(self.env_file, {
                "YIBAN_ADMIN_PASSWORD_HASH": "",
                "YIBAN_ADMIN_PW_VERSION": "",
                "YIBAN_ADMIN_PASSWORD": ADMIN_PASS,
            })

    # ---- H7 ----
    def test_rate_helpers_atomic_under_concurrency(self):
        n_threads = 8
        per_thread = 25
        window_store = {}
        fail_store = {}
        barrier_window = threading.Barrier(n_threads)
        barrier_fail = threading.Barrier(n_threads)

        def worker_window():
            barrier_window.wait()
            for _ in range(per_thread):
                self.webapp._bump_window_count(window_store, "ip", 1000.0, 60)

        def worker_fail():
            barrier_fail.wait()
            for _ in range(per_thread):
                self.webapp._bump_login_failure(fail_store, "key", 2000.0)

        threads = [
            threading.Thread(target=worker_window) for _ in range(n_threads)
        ] + [
            threading.Thread(target=worker_fail) for _ in range(n_threads)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
            self.assertFalse(t.is_alive(), "并发 helper 线程未在超时内结束")

        self.assertEqual(window_store["ip"][0], n_threads * per_thread, "窗口计数不应丢更新")
        self.assertEqual(fail_store["key"][0], n_threads * per_thread, "失败计数不应丢更新")

    def test_login_rate_window_allows_ten_then_rejects_eleventh(self):
        store = {}
        now = 1000.0
        for i in range(10):
            _cnt, _start, allowed = self.webapp._bump_window_count(
                store, "ip", now, 60, limit=10
            )
            self.assertTrue(allowed, f"第 {i + 1} 次应放行")
        _cnt, _start, allowed = self.webapp._bump_window_count(
            store, "ip", now, 60, limit=10
        )
        self.assertFalse(allowed, "第 11 次应拒绝")
        self.assertEqual(store["ip"][0], 10, "拒绝时不应递增计数")

    def test_purge_loop_disabled_by_env_does_not_start_thread(self):
        old = os.environ.get("YIBAN_DISABLE_PURGE_LOOP")
        os.environ["YIBAN_DISABLE_PURGE_LOOP"] = "1"
        try:
            before = {t.name for t in threading.enumerate()}
            with mock.patch.object(self.webapp, "_purge_loop_started", False, create=True):
                app = self.webapp.create_app()
                self.assertIsNotNone(app)
                after = {t.name for t in threading.enumerate()}
            self.assertFalse(self.webapp._purge_loop_started,
                             "YIBAN_DISABLE_PURGE_LOOP=1 时不应启动 daily-purge")
            self.assertNotIn("daily-purge", after - before)
        finally:
            if old is None:
                os.environ.pop("YIBAN_DISABLE_PURGE_LOOP", None)
            else:
                os.environ["YIBAN_DISABLE_PURGE_LOOP"] = old

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

    # ---- M10 ----
    def test_register_returns_already_registered_when_create_user_false(self):
        c = self.webapp.create_app().test_client()
        with mock.patch.object(db, "create_user", return_value=False):
            r = c.post("/api/register", json={
                "email": "race@test.local",
                "password": "UserPass123!",
            })
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        self.assertIn("该邮箱已注册", r.get_json()["error"])

    def test_account_add_auto_register_returns_already_registered_when_create_user_false(self):
        c = self.webapp.create_app().test_client()
        token = self._login(c, BUILTIN_EMAIL, ADMIN_PASS)
        with mock.patch.object(db, "create_user", return_value=False):
            r = c.post("/api/accounts", json={
                "name": "竞态邮箱",
                "phone": "13800138000",
                "password": "account-pass",
                "email": "race2@test.local",
                "initial_password": "UserPass123!",
            }, headers=self._csrf(token))
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        self.assertIn("该邮箱已注册", r.get_json()["error"])
        self.assertEqual(db.load_accounts(), [], "自动注册失败时不应新增账号")

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
