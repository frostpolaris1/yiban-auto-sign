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
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from _mail_body import render_body

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

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
            "agree": True,
        })
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        self.assertIsNone(db.find_user_any(BUILTIN_EMAIL))

    def test_register_requires_agree(self):
        # 合规文档（0.21.2）：注册必须勾选同意《用户协议》《隐私政策》，后端强制校验
        c = self.webapp.create_app().test_client()
        r = c.post("/api/register", json={
            "email": "agree@test.local",
            "password": "UserPass123!",
        })
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        self.assertIn("请先阅读并同意", r.get_json()["error"])
        self.assertIsNone(db.find_user("agree@test.local"))

    def test_register_agree_string_falsy_rejected(self):
        # 审查发现（0.21.2）：真值判断会让 "0"/"false"/"no" 等非空字符串绕过同意校验，
        # 现改为严格布尔判断（is not True），这些值必须与未勾选一样被拒绝。
        c = self.webapp.create_app().test_client()
        for falsy in ("0", "false", "no", "null", 0, False):
            r = c.post("/api/register", json={
                "email": f"agree{falsy}@test.local",
                "password": "UserPass123!",
                "agree": falsy,
            })
            self.assertEqual(r.status_code, 400, f"agree={falsy!r} 应被拒绝: {r.get_data(as_text=True)}")
            self.assertIn("请先阅读并同意", r.get_json()["error"])
            self.assertIsNone(db.find_user(f"agree{falsy}@test.local"), f"agree={falsy!r} 不应注册成功")

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

    def test_https_reverse_proxy_auto_upgrades_secure_when_unset(self):
        """**未配置** `YIBAN_COOKIE_SECURE` 时，HTTPS 反代请求应自动打开 Secure。

        原先写的是 `{"done": not cookie_secure}`，默认部署的 `done` 恒为 True → 自动升级
        分支**永不执行**，HTTPS 反代下 Cookie 一直不带 Secure。根因是把"未配置"与
        "显式关"混成了同一个 False，两者必须分开判。
        """
        self.webapp.write_env_key(self.env_file, "YIBAN_COOKIE_SECURE", "")
        app = self.webapp.create_app()
        self.assertFalse(app.config["SESSION_COOKIE_SECURE"], "起点：未配置 = 默认关")
        c = app.test_client()
        # 反代形态 = 第一跳是回环（本进程只监听回环，转发头由它覆盖设置）
        c.get("/api/clock", headers={"X-Forwarded-Proto": "https"},
              environ_base={"REMOTE_ADDR": "127.0.0.1"})
        self.assertTrue(app.config["SESSION_COOKIE_SECURE"],
                        "经可信反代的 HTTPS 请求应启用 Secure")

    def test_forwarded_proto_ignored_when_first_hop_untrusted(self):
        """第一跳不可信时**不得**采信 `X-Forwarded-Proto`，且不得粘住进程。

        直连形态（没有反代、或反代不在回环上）下客户端能自己发这个头。若采信，
        本进程的会话 Cookie 会一直带 Secure，站点退回 HTTP 后浏览器不回传 Cookie，
        表现为"登录不上"——比"少一个 Secure"更难排查。
        """
        self.webapp.write_env_key(self.env_file, "YIBAN_COOKIE_SECURE", "")
        app = self.webapp.create_app()
        c = app.test_client()
        c.get("/api/clock", headers={"X-Forwarded-Proto": "https"},
              environ_base={"REMOTE_ADDR": "203.0.113.9"})
        self.assertFalse(app.config["SESSION_COOKIE_SECURE"],
                         "伪造的转发头不得开启 Secure")

    def test_forwarded_proto_falls_back_when_header_disappears(self):
        """反代头消失后 Secure 必须能回落（逐请求判定，不粘住进程）。"""
        self.webapp.write_env_key(self.env_file, "YIBAN_COOKIE_SECURE", "")
        app = self.webapp.create_app()
        c = app.test_client()
        c.get("/api/clock", headers={"X-Forwarded-Proto": "https"},
              environ_base={"REMOTE_ADDR": "127.0.0.1"})
        self.assertTrue(app.config["SESSION_COOKIE_SECURE"])
        c.get("/api/clock", environ_base={"REMOTE_ADDR": "127.0.0.1"})
        self.assertFalse(app.config["SESSION_COOKIE_SECURE"],
                         "同进程内退回非 https 请求后不得继续发 Secure Cookie")

    def test_forwarded_proto_takes_first_hop_only(self):
        """逗号链只认最靠近客户端的那一跳（与 `_client_ip` 读 XFF 的口径一致）。"""
        self.webapp.write_env_key(self.env_file, "YIBAN_COOKIE_SECURE", "")
        app = self.webapp.create_app()
        c = app.test_client()
        c.get("/api/clock", headers={"X-Forwarded-Proto": "http, https"},
              environ_base={"REMOTE_ADDR": "127.0.0.1"})
        self.assertFalse(app.config["SESSION_COOKIE_SECURE"],
                         "链首是 http → 不得判成 https")
        c.get("/api/clock", headers={"X-Forwarded-Proto": "https, http"},
              environ_base={"REMOTE_ADDR": "127.0.0.1"})
        self.assertTrue(app.config["SESSION_COOKIE_SECURE"],
                        "链首是 https → 判定为 https")

    def test_explicit_zero_does_not_auto_upgrade(self):
        """**显式**配 `0` = 部署者明确要求不要 Secure：HTTPS 请求也不得自动打开。"""
        self.webapp.write_env_key(self.env_file, "YIBAN_COOKIE_SECURE", "0")
        app = self.webapp.create_app()
        c = app.test_client()
        c.get("/api/clock", headers={"X-Forwarded-Proto": "https"})
        self.assertFalse(app.config["SESSION_COOKIE_SECURE"],
                         "显式 0 的部署保持原行为（与未配置必须区分开）")

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

    def _restore_admin_env(self):
        """改密用例共用复位：清哈希/版本键，恢复明文口令基线。"""
        self.webapp.write_env_batch(self.env_file, {
            "YIBAN_ADMIN_PASSWORD_HASH": "",
            "YIBAN_ADMIN_PW_VERSION": "",
            "YIBAN_ADMIN_PASSWORD": ADMIN_PASS,
        })

    def test_pw_version_read_happens_inside_env_write_lock(self):
        """当前版本读取必须与落盘收进同一把 .env 写锁。

        读在 write_env_batch 取锁之前时，两个并发改密都读到旧值并写出同一个
        递增值——一次递增被吞，本应随版本失效的旧会话继续有效。用深度计数
        探针断言不变量：PW_VERSION 的现值读取发生（可重入写锁）临界区内。
        """
        depth = {"n": 0}
        reads = []
        real_lock = self.webapp._env_write_lock

        @contextlib.contextmanager
        def spy_lock(path):
            depth["n"] += 1
            try:
                with real_lock(path):
                    yield
            finally:
                depth["n"] -= 1

        real_read = self.webapp.load_env_int

        def spy_read(env_path, key, default):
            value = real_read(env_path, key, default)
            if key == "YIBAN_ADMIN_PW_VERSION":
                reads.append(depth["n"] > 0)
            return value

        new_pass = "InnerLock#2026x"
        try:
            c = self.webapp.create_app().test_client()
            token = self._login(c, BUILTIN_EMAIL, ADMIN_PASS)
            with mock.patch.object(self.webapp, "_env_write_lock", spy_lock), \
                 mock.patch.object(self.webapp, "load_env_int", spy_read):
                r = c.post("/api/me/password", json={
                    "old_password": ADMIN_PASS,
                    "new_password": new_pass,
                    "confirm_password": new_pass,
                }, headers=self._csrf(token))
            self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
            self.assertTrue(reads, "改密路径必须读取 PW_VERSION 现值一次")
            self.assertTrue(any(reads),
                            "PW_VERSION 现值读取必须发生在 .env 写锁临界区内（否则并发改密丢递增）")
        finally:
            self._restore_admin_env()

    def test_sequential_password_changes_each_bump_pw_version(self):
        """两个先后会话各改一次密：版本 1→2→3 每次落盘都递增，不留丢档。"""
        p2, p3 = "SecondPass#2026", "ThirdPass#2026"
        try:
            c1 = self.webapp.create_app().test_client()
            t1 = self._login(c1, BUILTIN_EMAIL, ADMIN_PASS)
            r = c1.post("/api/me/password", json={
                "old_password": ADMIN_PASS, "new_password": p2, "confirm_password": p2,
            }, headers=self._csrf(t1))
            self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
            with open(self.env_file, encoding="utf-8") as f:
                self.assertIn("YIBAN_ADMIN_PW_VERSION=2", f.read())
            c2 = self.webapp.create_app().test_client()
            t2 = self._login(c2, BUILTIN_EMAIL, p2)
            r = c2.post("/api/me/password", json={
                "old_password": p2, "new_password": p3, "confirm_password": p3,
            }, headers=self._csrf(t2))
            self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
            with open(self.env_file, encoding="utf-8") as f:
                content = f.read()
            self.assertIn("YIBAN_ADMIN_PW_VERSION=3", content,
                          "第二次改密必须把版本推到 3（递增丢失=应失效的会话存活）")
            self.assertNotIn("YIBAN_ADMIN_PW_VERSION=2\n", content)
        finally:
            self._restore_admin_env()

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
                "agree": True,
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
        # 2026-08-28：用户删除已软删行仍 400（不 purge）；文案改为撤销指引
        self.assertIn("待删除状态", r.get_json()["error"])
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


TEST_KEY_CWGATE = "a" * 64


ADMIN_PASS_CWGATE = "Master-Test-2026!"


SUB_PASS = "Subadmin-Test-2026!"


OWNER = "student@example.cn"


PHONE = "13800008000"


OLD_PW = "OldPass1234!"


NEW_PW = "AttackerPass123!"


class CredentialWriteGateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-cred-gate-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY_CWGATE}\n"
                    "YIBAN_ADMIN_USER=admin\n"
                    f"YIBAN_ADMIN_PASSWORD={ADMIN_PASS_CWGATE}\n")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.accounts_file = os.path.join(cls.tmp, "accounts.json")
        for k, v in {
            "YIBAN_ACCOUNTS_KEY": TEST_KEY_CWGATE, "YIBAN_ENV_FILE": cls.env_file,
            "YIBAN_ACCOUNTS_FILE": cls.accounts_file,
            "YIBAN_USERS_FILE": os.path.join(cls.tmp, "users.json"),
            "YIBAN_DB_FILE": cls.db_file, "YIBAN_STATE_DIR": cls.tmp,
            "YIBAN_LOG_FILE": os.path.join(cls.tmp, "sign.log"),
        }.items():
            os.environ[k] = v
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
                  "YIBAN_USERS_FILE", "YIBAN_DB_FILE", "YIBAN_STATE_DIR", "YIBAN_LOG_FILE"):
            os.environ.pop(k, None)

    def setUp(self):
        from werkzeug.security import generate_password_hash
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
        db.init_db(db_file=self.db_file, migrate_from=self.accounts_file, env_file=self.env_file)
        db.create_user(OWNER, generate_password_hash("Stu-Test-2026!", method=self.webapp.SCRYPT_METHOD),
                       role="user", created_at="2026-09-18 00:00:00", pw_version=1)
        db.create_user("sub.admin@example.cn",
                       generate_password_hash(SUB_PASS, method=self.webapp.SCRYPT_METHOD),
                       role="admin", created_at="2026-09-18 00:00:00", pw_version=1)
        db.add_account({"name": "小明", "phone": PHONE, "password": OLD_PW,
                        "phone_model": "", "phone_code": "", "owner": OWNER,
                        "status": self.webapp.ACCOUNT_STATUS_ACTIVE})
        self.app = self.webapp.create_app()
        self.sent = []
        self._orig_send = self.webapp.mailer.send_user
        self.webapp.mailer.send_user = lambda to, subject, text: self.sent.append((to, subject, render_body(text)))

    def tearDown(self):
        self.webapp.mailer.send_user = self._orig_send

    def _login(self, user, pw):
        c = self.app.test_client()
        r = c.post("/api/login", json={"username": user, "password": pw})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return c, {"X-CSRF-Token": c.get("/api/me").get_json()["csrf_token"]}

    def _sub(self):
        return self._login("sub.admin@example.cn", SUB_PASS)

    def _password_of(self, idx=0):
        return self.webapp.load_accounts()[idx].get("password")

    def _status_of(self, idx=0):
        return self.webapp.load_accounts()[idx].get("status")

    # ---- 二次鉴权 ----
    def test_subadmin_rewriting_password_without_confirm_is_refused(self):
        c, h = self._sub()
        r = c.put("/api/accounts/0", json={"phone": PHONE, "password": NEW_PW}, headers=h)
        self.assertIn(r.status_code, (400, 403), r.get_data(as_text=True))
        self.assertEqual(self._password_of(), OLD_PW, "鉴权未通过时凭据不得被改写")
        self.assertEqual(self.sent, [], "未生效的编辑不该发通知")

    def test_subadmin_rewriting_password_with_confirm_succeeds_and_notifies(self):
        c, h = self._sub()
        r = c.put("/api/accounts/0",
                  json={"phone": PHONE, "password": NEW_PW, "confirm_password": SUB_PASS},
                  headers=h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(self._password_of(), NEW_PW)
        self.assertEqual(len(self.sent), 1, "当事人必须收到一封变更信")
        to, subject, text = self.sent[0]
        self.assertEqual(to, OWNER)
        self.assertIn("管理员修改", subject)
        self.assertNotIn(NEW_PW, text, "变更信不得回显新口令")
        self.assertNotIn(PHONE, text, "变更信里的手机号必须是脱敏形态")
        self.assertIn("138****8000", text)

    def test_rebind_phone_also_counts_as_credential_write(self):
        c, h = self._sub()
        r = c.put("/api/accounts/0",
                  json={"_snapshot": {"phone": PHONE}, "phone": "13900009000"}, headers=h)
        self.assertIn(r.status_code, (400, 403), f"改绑手机号同样要二次鉴权: {r.get_data(as_text=True)}")
        self.assertEqual(self._password_of(), OLD_PW)

    def test_bare_rebind_without_identity_is_rejected(self):
        """不带原始标识的"改绑"与 idx 漂移不可区分——必须先被拒，不能进改写判定。"""
        c, h = self._sub()
        r = c.put("/api/accounts/0", json={"phone": "13900009000"}, headers=h)
        self.assertEqual(r.status_code, 409, r.get_data(as_text=True))
        self.assertEqual(self._password_of(), OLD_PW)

    def test_note_only_edit_needs_no_second_factor(self):
        """只改备注/设备型号不算改写凭据——不该被要求多输一次口令。"""
        c, h = self._sub()
        r = c.put("/api/accounts/0", json={"phone": PHONE, "name": "改名", "phone_model": "iPhone"},
                  headers=h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(self._password_of(), OLD_PW)
        self.assertEqual(self.sent, [])

    def test_bare_account_rewrite_needs_gate_and_sends_no_mail(self):
        """owner='admin' 的代管账号没有当事人可通知，但鉴权一档不降。"""
        db.add_account({"name": "代管", "phone": "13700007000", "password": OLD_PW,
                        "phone_model": "", "phone_code": "", "owner": "admin",
                        "status": self.webapp.ACCOUNT_STATUS_ACTIVE})
        c, h = self._sub()
        r = c.put("/api/accounts/1", json={"phone": "13700007000", "password": NEW_PW}, headers=h)
        self.assertIn(r.status_code, (400, 403))
        r2 = c.put("/api/accounts/1",
                   json={"phone": "13700007000", "password": NEW_PW, "confirm_password": SUB_PASS},
                   headers=h)
        self.assertEqual(r2.status_code, 200, r2.get_data(as_text=True))
        self.assertEqual(self.sent, [], "无当事人的行不发信，也不因此报错")

    # ---- idx 错位 fail-closed ----
    def test_edit_without_any_identifier_is_rejected(self):
        c, h = self._sub()
        r = c.put("/api/accounts/0", json={"name": "悄悄改"}, headers=h)
        self.assertEqual(r.status_code, 409, r.get_data(as_text=True))
        self.assertEqual(self._password_of(), OLD_PW)

    def test_snapshot_is_identity_only_and_phone_still_required(self):
        """快照只用来核对 idx，顶层 `phone` 仍是必填：该请求死在字段校验（400）。

        顺带记一句给后来人——本路由的 fail-closed 守卫**没有授权增量**：不带 phone 的请求
        原本也会因"手机号为必填项"被拒，改动只是把拒绝点从字段校验提前到错位判定
        （400 → 409）。真正堵上改写口子的是 `creds_written` 那道 `_high_risk_gate`。
        """
        c, h = self._sub()
        r = c.put("/api/accounts/0",
                  json={"_snapshot": json.dumps({"phone": PHONE}), "name": "备注而已"}, headers=h)
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        self.assertIn("手机号", r.get_json()["error"])
        self.assertEqual(self.webapp.load_accounts()[0].get("name"), "小明")

    def test_mismatched_snapshot_is_rejected(self):
        c, h = self._sub()
        r = c.put("/api/accounts/0",
                  json={"_snapshot": json.dumps({"phone": "13911112222"}), "name": "x"}, headers=h)
        self.assertEqual(r.status_code, 409, r.get_data(as_text=True))

    # ---- 管理员自提交一律待审核 ----
    def test_admin_self_submit_is_pending_and_review_promotes(self):
        c, h = self._sub()
        r = c.post("/api/my-accounts",
                   json={"phone": "13600006000", "password": "SomePass123!", "name": "自交"},
                   headers=h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(self._status_of(1), self.webapp.ACCOUNT_STATUS_PENDING,
                         "管理员提交不再按角色免审即生效")
        cm, hm = self._login("admin", ADMIN_PASS_CWGATE)
        r2 = cm.post("/api/accounts/1/review",
                     json={"action": "approve", "phone": "13600006000"}, headers=hm)
        self.assertEqual(r2.status_code, 200, r2.get_data(as_text=True))
        self.assertEqual(self._status_of(1), self.webapp.ACCOUNT_STATUS_ACTIVE,
                         "审核通道仍能让管理员把代管号放行")


TEST_KEY_ROLE = "a" * 64


ADMIN_PASS_ROLE = "MasterPass#2026"   # 15 位四类，满足主管理员 12/3 策略


STRONG_12_3 = "Rotated#2026"     # 12 位四类：提档后允许


WEAK_11 = "Rotated#202"          # 11 位：不足 12 位


WEAK_12_2 = "abcdefgh1234"       # 12 位两类：类别不足


NEW_USER_PASS = "abcdef1234"     # 10 位两类：注册用户口径不变


class RoleHardeningTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-role-hard-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        cls._env_content = (
            f"YIBAN_ACCOUNTS_KEY={TEST_KEY_ROLE}\n"
            f"YIBAN_ADMIN_USER=admin\nYIBAN_ADMIN_PASSWORD={ADMIN_PASS_ROLE}\n"
        )
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(cls._env_content)
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.accounts_file = os.path.join(cls.tmp, "accounts.json")
        cls.users_file = os.path.join(cls.tmp, "users.json")
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY_ROLE
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

    # ---- 辅助 ----
    def _login(self, c, username, password):
        r = c.post("/api/login", json={"username": username, "password": password})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return c.get("/api/me").get_json()["csrf_token"]

    def _admin_client(self):
        c = self.webapp.create_app().test_client()
        return c, self._login(c, "admin", ADMIN_PASS_ROLE)

    def _make_formal_user(self, email, phone):
        """构造「正式用户」：注册 + 提交账号 + 管理员审核通过。"""
        db.create_user(email, self.webapp.generate_password_hash(USER_PASS))
        c = self.webapp.create_app().test_client()
        t = self._login(c, email, USER_PASS)
        r = c.post("/api/my-accounts", json={"name": "n", "phone": phone, "password": "p"},
                   headers={"X-CSRF-Token": t})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        ac, at = self._admin_client()
        accounts = db.load_accounts()
        idx = next(i for i, a in enumerate(accounts) if a["phone"] == phone)
        r = ac.post(f"/api/accounts/{idx}/review", json={"action": "approve"},
                    headers={"X-CSRF-Token": at})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

    # ---- 1. 批量角色变更入口移除 ----
    def test_batch_set_admin_removed(self):
        self._make_formal_user("u1@test.local", "13800138001")
        ac, at = self._admin_client()
        for action in ("set_admin", "unset_admin"):
            r = ac.post("/api/users/batch", json={"action": action, "emails": ["u1@test.local"]},
                        headers={"X-CSRF-Token": at})
            self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        u = db.find_user("u1@test.local")
        self.assertEqual(u.get("role"), "user", "批量角色变更入口已移除，角色不得变更")

    # ---- 2. 角色变更须二次鉴权 ----
    def test_role_without_reconfirm_rejected(self):
        self._make_formal_user("u2@test.local", "13800138002")
        ac, at = self._admin_client()
        r = ac.post("/api/users/u2@test.local/role", json={"role": "admin"},
                    headers={"X-CSRF-Token": at})
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        self.assertEqual(db.find_user("u2@test.local").get("role"), "user")

    def test_role_wrong_reconfirm_rejected(self):
        self._make_formal_user("u3@test.local", "13800138003")
        ac, at = self._admin_client()
        r = ac.post("/api/users/u3@test.local/role",
                    json={"role": "admin", "confirm_password": "WrongPass#999"},
                    headers={"X-CSRF-Token": at})
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        self.assertIn("当前密码不正确", r.get_json()["error"])
        self.assertEqual(db.find_user("u3@test.local").get("role"), "user")

    def test_role_with_reconfirm_ok_and_audited(self):
        self._make_formal_user("u4@test.local", "13800138004")
        ac, at = self._admin_client()
        with mock.patch.object(self.webapp, "send_notification") as notify:
            r = ac.post("/api/users/u4@test.local/role",
                        json={"role": "admin", "confirm_password": ADMIN_PASS_ROLE},
                        headers={"X-CSRF-Token": at})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(db.find_user("u4@test.local").get("role"), "admin")
        subjects = [call.args[0] for call in notify.call_args_list]
        self.assertIn("权限变更告警", subjects, "提权仍须即时告警")
        rows = db.audit_rows(50) if hasattr(db, "audit_rows") else []
        if rows:  # 审计留痕（允许无此辅助函数的环境跳过细查）
            self.assertTrue(any(x.get("action") == "user_role" for x in rows))

    def test_regular_admin_cannot_change_role(self):
        self._make_formal_user("u5@test.local", "13800138005")
        db.create_user("reg-admin@test.local", self.webapp.generate_password_hash(USER_PASS), role="admin")
        c = self.webapp.create_app().test_client()
        t = self._login(c, "reg-admin@test.local", USER_PASS)
        r = c.post("/api/users/u5@test.local/role",
                   json={"role": "admin", "confirm_password": USER_PASS},
                   headers={"X-CSRF-Token": t})
        self.assertEqual(r.status_code, 403, r.get_data(as_text=True))

    # ---- 3. 主管理员口令 12 位三类 ----
    def test_builtin_password_change_enforces_admin_policy(self):
        ac, at = self._admin_client()
        # 11 位：长度不足
        r = ac.post("/api/me/password",
                    json={"old_password": ADMIN_PASS_ROLE, "new_password": WEAK_11, "confirm_password": WEAK_11},
                    headers={"X-CSRF-Token": at})
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        # 12 位两类：类别不足
        r = ac.post("/api/me/password",
                    json={"old_password": ADMIN_PASS_ROLE, "new_password": WEAK_12_2, "confirm_password": WEAK_12_2},
                    headers={"X-CSRF-Token": at})
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        self.assertIn("三类", r.get_json()["error"])
        # 12 位三类：通过（随后还原 .env 供其他用例登录）
        r = ac.post("/api/me/password",
                    json={"old_password": ADMIN_PASS_ROLE, "new_password": STRONG_12_3, "confirm_password": STRONG_12_3},
                    headers={"X-CSRF-Token": at})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        env_now = self.webapp.read_env(self.env_file)
        self.assertEqual(env_now.get("YIBAN_ADMIN_PASSWORD", ""), "", "改密成功后明文应被清空")
        self.assertTrue(env_now.get("YIBAN_ADMIN_PASSWORD_HASH", ""), "改密成功后应写入哈希")
        # 还原口令环境（写回原明文，清掉哈希与 PW_VERSION）
        with io.open(self.env_file, "w", encoding="utf-8") as f:
            f.write(self._env_content)

    def test_registered_user_policy_unchanged(self):
        db.create_user("plain@test.local", self.webapp.generate_password_hash(USER_PASS))
        c = self.webapp.create_app().test_client()
        t = self._login(c, "plain@test.local", USER_PASS)
        # 10 位两类对注册用户仍然合法（不被主管理员提档策略误伤）
        r = c.post("/api/me/password",
                   json={"old_password": USER_PASS, "new_password": NEW_USER_PASS, "confirm_password": NEW_USER_PASS},
                   headers={"X-CSRF-Token": t})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

    def test_reject_default_admin_password_enforces_12_3(self):
        def _env_with(pw):
            path = os.path.join(self.tmp, f"env-{abs(hash(pw))}.env")
            with io.open(path, "w", encoding="utf-8") as f:
                f.write(f"YIBAN_ADMIN_USER=admin\nYIBAN_ADMIN_PASSWORD={pw}\n")
            return path

        for weak in ("admin123", WEAK_11, WEAK_12_2):
            with self.assertRaises(SystemExit, msg=weak):
                self.webapp.reject_default_admin_password(_env_with(weak))
        strong = _env_with(STRONG_12_3)
        self.assertIsNone(self.webapp.reject_default_admin_password(strong), "12 位三类应允许启动")


    # ---- 7. 「内置管理员是否还进得来」判据（批 3 §4.4）----
    def _rewrite_env(self, body):
        with io.open(self.env_file, "w", encoding="utf-8") as f:
            f.write(body)

    def test_builtin_loginable_matches_verify_admin(self):
        """兜底判据必须与登录判据同口径：一处说"还有人兜底"、另一处拒登录＝全员锁死。

        原先"至少保留 1 个管理员"只看 `YIBAN_ADMIN_USER` 非空，而 verify_admin 对
        下面三种态都 fail-closed 拒绝——三种态都必须在两侧同时判 False。
        """
        good = self.webapp.generate_password_hash(ADMIN_PASS_ROLE, self.webapp.SCRYPT_METHOD)
        head = (f"YIBAN_ACCOUNTS_KEY={TEST_KEY_ROLE}\nYIBAN_ADMIN_USER=admin\n")
        states = (
            (f"{head}YIBAN_ADMIN_PASSWORD_HASH={good}\n", True, "哈希恰一行"),
            (f"{head}YIBAN_ADMIN_PASSWORD={ADMIN_PASS_ROLE}\n", False, "只剩明文（迁移失败态）"),
            (f"{head}YIBAN_ADMIN_PASSWORD_HASH={good}\n"
             f"YIBAN_ADMIN_PASSWORD_HASH={good}\n", False, "哈希多行歧义"),
            (head, False, "凭据全缺"),
        )
        try:
            for body, expect, name in states:
                with self.subTest(state=name):
                    self._rewrite_env(body)
                    self.assertEqual(self.webapp._builtin_admin_loginable(), expect, name)
                    self.assertEqual(
                        self.webapp.verify_admin("admin", ADMIN_PASS_ROLE), expect,
                        f"{name}：兜底判据与 verify_admin 漂移")
        finally:
            self._rewrite_env(self._env_content)


TEST_KEY_SGATE = "a" * 64


WRONG_PASS = "WrongPass999!"


def _load_webapp():
    """**独立名字**加载 web/app.py：它在导入期把 ENV_FILE 等读成模块级常量，与别的
    测试文件共用同一模块对象会读到另一个 `.env`（单跑绿、全量红的老坑）。"""
    spec = importlib.util.spec_from_file_location(
        "webapp_sensitive_gate", os.path.join(BASE, "web", "app.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["webapp_sensitive_gate"] = mod
    with contextlib.suppress(Exception):
        spec.loader.exec_module(mod)
    return mod


class _GateBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-sensitive-gate-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(
                f"YIBAN_ACCOUNTS_KEY={TEST_KEY_SGATE}\n"
                f"YIBAN_ADMIN_USER=admin\nYIBAN_ADMIN_PASSWORD={ADMIN_PASS}\n"
            )
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.accounts_file = os.path.join(cls.tmp, "accounts.json")
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY_SGATE
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_ACCOUNTS_FILE"] = cls.accounts_file
        os.environ["YIBAN_USERS_FILE"] = os.path.join(cls.tmp, "users.json")
        os.environ["YIBAN_DB_FILE"] = cls.db_file
        os.environ["YIBAN_STATE_DIR"] = cls.tmp
        os.environ["YIBAN_LOG_FILE"] = os.path.join(cls.tmp, "sign.log")
        os.environ["YIBAN_DISABLE_PURGE_LOOP"] = "1"
        global db
        import db
        cls.webapp = _load_webapp()

    @classmethod
    def tearDownClass(cls):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k in ("YIBAN_ACCOUNTS_KEY", "YIBAN_ENV_FILE", "YIBAN_ACCOUNTS_FILE",
                  "YIBAN_USERS_FILE", "YIBAN_DB_FILE", "YIBAN_STATE_DIR",
                  "YIBAN_LOG_FILE", "YIBAN_DISABLE_PURGE_LOOP"):
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
        db.init_db(self.db_file, migrate_from=self.accounts_file,
                   env_file=self.env_file)
        # 每用例回到"两开关均未配置（=开放）+ 门禁旋钮走默认值"基线；B 档三键同清——
        # 它们是豁免用例的"真变更"载体，上一条用例留下的值会让下一条根本不进门禁
        self.webapp.write_env_batch(self.env_file, {
            "YIBAN_GLOBAL_PAUSE": "", "YIBAN_REGISTRATION_PAUSE": "",
            "YIBAN_PW_CONFIRM_TTL": "", "YIBAN_PW_CONFIRM_COOLDOWN_SEC": "",
            "YIBAN_SIGN_ORDER": "", "YIBAN_SIGN_DIST": "", "YIBAN_SIGN_MODE": "",
        })
        self.alerts = []
        patcher = mock.patch.object(
            self.webapp, "send_notification",
            side_effect=lambda t, c, urgent=False, force=False, ledger=None:
            self.alerts.append((t, render_body(c), urgent)))
        patcher.start()
        self.addCleanup(patcher.stop)

    # ---- 工具 ----
    def _login(self, username="admin", password=None, app=None):
        c = (app or self.webapp.create_app()).test_client()
        r = c.post("/api/login", json={
            "username": username, "password": password or ADMIN_PASS})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        c.csrf = c.get("/api/me").get_json()["csrf_token"]
        return c

    def _hdr(self, c):
        return {"X-CSRF-Token": c.csrf}

    def _env_has(self, needle):
        with open(self.env_file, encoding="utf-8-sig") as f:
            return needle in f.read()

    def _rows(self, action):
        return db.get_conn().execute(
            "SELECT detail FROM audit_logs WHERE action=?", (action,)
        ).fetchall()

    def _fail_gate_times(self, c, n, *, endpoint="switch"):
        """连续 n 次用错误口令撞门禁，返回状态码列表。"""
        codes = []
        for _ in range(n):
            codes.append(self._attempt(c, endpoint, WRONG_PASS).status_code)
        return codes

    # 三种门禁落点（系统开关 / 执行体写 / 高危二次鉴权）在测试里等价可驱动
    def _attempt(self, c, endpoint, password):
        if endpoint == "switch":
            return c.post("/api/settings",
                          json={"global_pause": 1, "confirm_password": password},
                          headers=self._hdr(c))
        if endpoint == "executors":
            return c.post("/api/scheduler/executors/rows",
                          json={"proxy": "http://n:1", "confirm_password": password},
                          headers=self._hdr(c))
        if endpoint == "batch":  # 高危二次鉴权（always_required）
            return c.post("/api/users/batch",
                          json={"action": "delete", "emails": ["ghost@test.local"],
                                "confirm_password": password},
                          headers=self._hdr(c))
        raise AssertionError(endpoint)

    def _switch_with(self, c, password=None):
        body = {"global_pause": 1}
        if password is not None:
            body["confirm_password"] = password
        return c.post("/api/settings", json=body, headers=self._hdr(c))


class CooldownTest(_GateBase):
    """独立计数 + 首达阈值告警 + 门禁级冷却（回归护栏 ①②）。"""

    def test_threshold_failure_alerts_once_and_next_try_429(self):
        """①错口令 3 次 → 每次 403、告警恰 1 条；第 4 次进冷却返回 429。"""
        n = self.webapp.LOGIN_FAIL_NOTIFY
        c = self._login()
        codes = self._fail_gate_times(c, n)
        self.assertEqual(codes, [403] * n, f"阈值前每次都是口令不符 403，实际 {codes}")
        fails = [a for a in self.alerts if "二次鉴权失败" in a[0]]
        self.assertEqual(len(fails), 1, f"首达阈值只告警一次，实际 {self.alerts}")
        self.assertTrue(fails[0][2], "复核失败告警必须 urgent")
        self.assertIn("系统开关", fails[0][1], "告警须写明是哪个门禁")
        self.assertNotIn(WRONG_PASS, fails[0][1], "告警不得回显口令")
        r = self._attempt(c, "switch", WRONG_PASS)
        self.assertEqual(r.status_code, 429, r.get_data(as_text=True))

    def test_missing_password_denied_but_not_counted(self):
        """「没带口令」不占冷却预算：它一次散列都不做，计入阈值等于让攻击者用空请求
        把合法管理员的敏感操作预算刷光（与 _admin_delete_limited 修掉的运维 DoS 同类）。
        """
        c = self._login()
        codes = [self._switch_with(c, None).status_code for _ in range(6)]
        self.assertEqual(codes, [403] * 6, f"缺口令的拒绝仍是 403，实际 {codes}")
        self.assertEqual(self.alerts, [], "缺口令不得触发复核失败告警")
        # 预算完整：错口令仍是从第 4 次起才进冷却
        self.assertEqual(self._fail_gate_times(c, self.webapp.LOGIN_FAIL_NOTIFY),
                         [403] * self.webapp.LOGIN_FAIL_NOTIFY)
        self.assertEqual(self._attempt(c, "switch", WRONG_PASS).status_code, 429)

    def test_missing_and_wrong_password_have_different_copy(self):
        """「没输口令」与「口令输错」文案必须分开（前端据此决定是弹口令框还是报错）。

        状态码两档相同（沿用历史契约：配置类 403、高危类 400），只有 error 文案与
        机器可读的 reason 不同——缺口令说"需要输入"，错口令说"不正确"。
        """
        c = self._login()
        miss = self._switch_with(c, None)
        self.assertEqual(miss.get_json()["reason"], "password_required")
        self.assertIn("需要输入当前口令", miss.get_json()["error"])
        self.assertNotIn("不正确", miss.get_json()["error"],
                         "用户根本没输口令，不能说他输错了")
        wrong = self._switch_with(c, WRONG_PASS)
        self.assertEqual(wrong.get_json()["reason"], "password_incorrect")
        self.assertIn("口令校验未通过", wrong.get_json()["error"])
        self.assertEqual(miss.status_code, wrong.status_code, "两档状态码不得因此改变")

    def test_cooldown_rejects_correct_password_too(self):
        """②冷却期内正确口令也不放行——否则"改用对口令"就绕过了冷却。"""
        c = self._login()
        self._fail_gate_times(c, self.webapp.LOGIN_FAIL_NOTIFY)
        r = self._attempt(c, "switch", ADMIN_PASS)
        self.assertEqual(r.status_code, 429, "冷却期内对口令同样拒绝")
        self.assertFalse(self._env_has("YIBAN_GLOBAL_PAUSE=1"), "冷却期不得落盘")
        self.assertTrue(self._rows("sensitive_pw_cooldown"), "进入冷却必须留审计")

    def test_cooldown_spans_all_reconfirm_endpoints(self):
        """一处撞满阈值，另两处落点同样进冷却（同一入口、同一计数）。"""
        c = self._login()
        self._fail_gate_times(c, self.webapp.LOGIN_FAIL_NOTIFY, endpoint="switch")
        for endpoint in ("executors", "batch"):
            r = self._attempt(c, endpoint, ADMIN_PASS)
            self.assertEqual(r.status_code, 429,
                             f"{endpoint} 应与系统开关共用冷却（同一门禁入口）")

    def test_cooldown_leaves_login_reads_and_ungated_writes_alone(self):
        """冷却只封"需要复核的写"：登录、只读 GET、普通页与普通写操作一律照常。"""
        c = self._login()
        self._fail_gate_times(c, self.webapp.LOGIN_FAIL_NOTIFY)
        self.assertEqual(c.get("/api/me").status_code, 200, "只读 GET 不受冷却影响")
        self.assertEqual(c.get("/api/users").status_code, 200, "管理页数据不受冷却影响")
        self.assertEqual(c.get("/api/clock").status_code, 200, "公开只读接口不受冷却影响")
        # 值未变更 → 不在门禁范围内（不要求复核），冷却不该顺手把它也停了
        # （用 A 档的 sunday_sign 提交它的现值 0：档位高低都不该让"没改"的保存多一道口令）
        r = c.post("/api/settings", json={"sunday_sign": 0}, headers=self._hdr(c))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        # 登录完全可用：新会话用正确口令照常登录，用错误口令仍是"口令错"而不是"锁定"
        c2 = self.webapp.create_app().test_client()
        r = c2.post("/api/login", json={"username": "admin", "password": ADMIN_PASS})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        r = c2.post("/api/login", json={"username": "admin", "password": WRONG_PASS})
        self.assertEqual(r.status_code, 401, "门禁冷却不得占用登录锁定，错口令仍 401")

    def test_cooldown_expires_and_gate_reopens(self):
        """冷却窗口过后门禁重新可用（可配的短窗口，免睡长秒）。"""
        self.webapp.write_env_batch(self.env_file, {"YIBAN_PW_CONFIRM_COOLDOWN_SEC": "1"})
        c = self._login()
        self._fail_gate_times(c, self.webapp.LOGIN_FAIL_NOTIFY)
        self.assertEqual(self._attempt(c, "switch", ADMIN_PASS).status_code, 429)
        time.sleep(1.2)
        r = self._attempt(c, "switch", ADMIN_PASS)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(self._env_has("YIBAN_GLOBAL_PAUSE=1"))

    def test_cooldown_rearms_on_next_failure_after_expiry(self):
        """冷却到期后再错一次必须立刻重新布防。

        计数窗口（900 秒）比冷却长得多：若只在"恰好第 3 次"布防，冷却到期后的第
        4、5… 次失败既不再告警也不再被挡，攻击者就等于拿到了每 300 秒一次的免费
        口令散列批次——正是本次要堵的那个洞。
        """
        self.webapp.write_env_batch(self.env_file, {"YIBAN_PW_CONFIRM_COOLDOWN_SEC": "1"})
        c = self._login()
        self._fail_gate_times(c, self.webapp.LOGIN_FAIL_NOTIFY)
        time.sleep(1.2)
        self.assertEqual(self._attempt(c, "switch", WRONG_PASS).status_code, 403,
                         "冷却到期后这一次是「第 4 次失败」，仍要散列比对一次")
        r = self._attempt(c, "switch", ADMIN_PASS)
        self.assertEqual(r.status_code, 429, "第 4 次失败应立刻重新布防冷却")

    def test_zero_cooldown_keeps_alert_but_no_lockout(self):
        """冷却窗口 0 = 关闭冷却（告警与独立计数照旧），用于运维按 .env 降级。"""
        self.webapp.write_env_batch(self.env_file, {"YIBAN_PW_CONFIRM_COOLDOWN_SEC": "0"})
        c = self._login()
        self._fail_gate_times(c, self.webapp.LOGIN_FAIL_NOTIFY)
        self.assertEqual(self._attempt(c, "switch", ADMIN_PASS).status_code, 200)


class P18IsolationTest(_GateBase):
    """门禁失败绝不写 `_login_fails`（P18：被窃会话不得把管理员锁出登录）。"""

    def test_gate_failures_never_touch_login_counter(self):
        """门禁失败绝不写 `_login_fails`（P18）——且必须**在同一个 app 实例内**验证：
        两张计数表都是 create_app 的闭包字典，换 app 探测等于换了个内存桶，测不出串味。
        """
        app = self.webapp.create_app()
        c = self._login(app=app)
        # 远超登录锁定阈值（LOGIN_MAX_FAILS）次数的门禁失败
        codes = self._fail_gate_times(c, self.webapp.LOGIN_MAX_FAILS + 2)
        self.assertIn(429, codes, "阈值后应进冷却")
        self.assertNotIn(401, codes)
        probe = app.test_client()
        for _ in range(2):
            r = probe.post("/api/login", json={"username": "admin", "password": WRONG_PASS})
            self.assertEqual(r.status_code, 401,
                             f"登录侧的账没被门禁污染过，错口令仍应是 401：{r.get_data(as_text=True)}")
        r = probe.post("/api/login", json={"username": "admin", "password": ADMIN_PASS})
        self.assertEqual(r.status_code, 200, "管理员始终没被锁出登录")

    def test_reconfirm_path_no_longer_shares_login_bucket(self):
        """登录侧锁定不再牵连高危运维：旧实现读共享 `_login_fails` 的 lock_until，
        于是同出口的登录失败会把主管理员一并锁在**所有高危操作**之外（运维 DoS）；
        现在门禁只看自己的冷却表，登录锁着也照样能用正确口令做高危操作。"""
        c = self._login()
        for _ in range(self.webapp.LOGIN_MAX_FAILS):
            r = c.post("/api/login", json={"username": "admin", "password": WRONG_PASS})
        self.assertEqual(r.status_code, 429, "前置：登录侧此刻已锁定")
        r = c.post("/api/users/ghost@test.local/delete",
                   json={"mode": "full", "confirm_password": ADMIN_PASS},
                   headers=self._hdr(c))
        self.assertEqual(r.status_code, 404,
                         f"门禁应放行（登录侧锁定不得牵连高危运维），实际 {r.status_code} "
                         f"{r.get_data(as_text=True)}")
        self.assertIn("用户不存在", r.get_json()["error"])


class ExemptionTest(_GateBase):
    """配置类动作的"刚复核过就免再输"豁免，及其边界（IP / TTL / always_required）。"""

    def test_config_gate_exempt_within_ttl(self):
        """③先正确口令过一次，TTL 内改另一个**可豁免档位**的值不必再输口令。"""
        c = self._login()
        self.assertEqual(self._switch_with(c, None).status_code, 403,
                         "前置：未复核过的真变更必须要口令")
        self.assertEqual(self._switch_with(c, ADMIN_PASS).status_code, 200)
        # B 档真变更，但本会话刚复核过 → 免口令
        r = c.post("/api/settings", json={"sign_order": "random"}, headers=self._hdr(c))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(self._env_has("YIBAN_SIGN_ORDER=random"))
        # 同一份豁免对 A 档不生效（A 档必须当次输口令）
        r = c.post("/api/settings", json={"registration_pause": 1}, headers=self._hdr(c))
        self.assertEqual(r.status_code, 403, "A 档不得被豁免放行")
        self.assertFalse(self._env_has("YIBAN_REGISTRATION_PAUSE=1"))

    def test_executor_gate_shares_the_exemption(self):
        """豁免跨落点生效（同一入口的同一份会话凭据）。"""
        c = self._login()
        self.assertEqual(self._switch_with(c, ADMIN_PASS).status_code, 200)
        r = c.post("/api/scheduler/executors/rows", json={"proxy": "http://n:1"},
                   headers=self._hdr(c))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

    def test_exemption_not_granted_from_a_different_ip(self):
        """出口 IP 变了必须重新输口令——被窃 Cookie 换个出口就免检是不可接受的。

        载体用 B 档 sign_order（可豁免动作）：拿 A 档键测等于在测 always_required，
        豁免这条边界就没人管了。
        """
        c = self._login()
        self.assertEqual(self._switch_with(c, ADMIN_PASS).status_code, 200)
        with mock.patch.object(self.webapp, "_client_ip", return_value="203.0.113.9"):
            r = c.post("/api/settings", json={"sign_order": "random"},
                       headers=self._hdr(c))
        self.assertEqual(r.status_code, 403, "跨 IP 不得沿用豁免")
        self.assertFalse(self._env_has("YIBAN_SIGN_ORDER=random"))

    def test_exemption_disabled_by_ttl_zero(self):
        """`YIBAN_PW_CONFIRM_TTL=0` = 关闭豁免（每次都要口令）。"""
        self.webapp.write_env_batch(self.env_file, {"YIBAN_PW_CONFIRM_TTL": "0"})
        c = self._login()
        self.assertEqual(self._switch_with(c, ADMIN_PASS).status_code, 200)
        r = c.post("/api/settings", json={"sign_order": "random"}, headers=self._hdr(c))
        self.assertEqual(r.status_code, 403, "TTL=0 时不得豁免")

    def test_exemption_expires_after_ttl(self):
        """超过 TTL 后回到"每次都要口令"。"""
        self.webapp.write_env_batch(self.env_file, {"YIBAN_PW_CONFIRM_TTL": "1"})
        c = self._login()
        self.assertEqual(self._switch_with(c, ADMIN_PASS).status_code, 200)
        time.sleep(1.2)
        r = c.post("/api/settings", json={"sign_order": "random"}, headers=self._hdr(c))
        self.assertEqual(r.status_code, 403, "豁免到期后须重新复核")

    def _grant_exemption(self, c, i):
        """建立"本会话刚复核过口令"的豁免态：交替写 global_pause 的真变更 + 正确口令。
        （同值提交不进门禁，故必须交替，否则第二次起就没在建立豁免了。）"""
        r = c.post("/api/settings",
                   json={"global_pause": 1 if i % 2 == 0 else 0,
                         "confirm_password": ADMIN_PASS}, headers=self._hdr(c))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

    def test_exemption_never_granted_for_irreversible_or_alerting_actions(self):
        """③不可逆清除 / 角色变更 / 重置他人口令 / 关闭告警通道 / 改主管理员口令
        ——即便本会话刚复核过，也必须当次输口令。

        每例各建新 app：门禁冷却表是 per-app 的，共用一份会让第 4 例撞进冷却、
        把"豁免不该放行"这件事掩盖成 429。
        """
        cases = (
            ("POST", "/api/users/batch",
             {"action": "delete", "emails": ["ghost@test.local"]}),
            ("POST", "/api/users/ghost@test.local/role", {"role": "admin"}),
            ("POST", "/api/users/ghost@test.local/password", {"password": ADMIN_PASS}),
            ("POST", "/api/users/ghost@test.local/delete", {"mode": "full"}),
            ("POST", "/api/users/ghost@test.local/delete", {"mode": "accounts_only"}),
            ("POST", "/api/users/deleted/purge", {"emails": ["ghost@test.local"]}),
            ("POST", "/api/accounts/batch",
             {"action": "purge", "ids": [1], "h_idx": 0, "h_phone": "138****0000"}),
            ("PUT", "/api/mail-config", {"enabled": False}),
            ("PUT", "/api/notify-config", {"type": ""}),
        )
        for i, (method, path, body) in enumerate(cases):
            with self.subTest(path=path, body=body):
                c = self._login()
                self._grant_exemption(c, i)
                self.alerts.clear()
                r = c.open(path, method=method, json=body, headers=self._hdr(c))
                self.assertEqual(
                    r.status_code, 400,
                    f"{path} 属必须当次复核的动作，豁免不得放行：{r.get_data(as_text=True)}")
                # 用例本身就没带 confirm_password，故是"缺口令"档文案
                self.assertEqual(r.get_json()["reason"], "password_required")
                self.assertEqual(self.alerts, [], "被拒的高危动作不得发出任何变更告警")

    def test_self_password_change_never_uses_exemption(self):
        """改主管理员口令不是"配置写入"：豁免存在时仍须给出正确的当前口令。"""
        c = self._login()
        self.assertEqual(self._switch_with(c, ADMIN_PASS).status_code, 200)
        r = c.post("/api/me/password", json={
            "old_password": WRONG_PASS, "new_password": "FreshPass123!",
            "confirm_password": "FreshPass123!"}, headers=self._hdr(c))
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))

    def test_gate_params_clamped(self):
        """旋钮钳制：TTL 0~900（越界夹回），冷却非负；避免误配出一个永久豁免。"""
        for raw, want in (("99999", 900), ("-5", 0), ("120", 120), ("", 300)):
            with self.subTest(raw=raw):
                self.webapp.write_env_batch(self.env_file,
                                            {"YIBAN_PW_CONFIRM_TTL": raw})
                ttl, _cd = self.webapp._sensitive_gate_params(self.env_file)
                self.assertEqual(ttl, want)


class StoreHygieneTest(_GateBase):
    """门禁状态不得无界增长（海量不同键打不爆内存）。"""

    def test_gate_stores_trim_and_stay_bounded(self):
        limit = self.webapp._IP_STORE_LIMIT
        captured = []
        real_trim = self.webapp._ip_store_trim

        def spy(store, max_age):
            captured.append((store, max_age))
            return real_trim(store, max_age)

        c = self._login()
        with mock.patch.object(self.webapp, "_ip_store_trim", side_effect=spy):
            self._fail_gate_times(c, self.webapp.LOGIN_FAIL_NOTIFY)
        # 门禁表的键是 (ip, 用户名) 二元组；通用限速表是裸 IP 串，据此筛出门禁自己的表
        gate_stores = {id(s): s for s, _ in captured
                       if any(isinstance(k, tuple) and len(k) == 2 for k in s)}
        self.assertTrue(gate_stores, "门禁写入路径必须对计数表做 trim")
        now = time.time()
        self.assertTrue(
            any(v[-1] > now for s in gate_stores.values() for v in s.values()),
            "冷却状态表必须存在且同样被 trim（末位是未来时刻的解锁时间点）")
        stale = now - (self.webapp._IP_STORE_MAX_AGE + 3600)
        for store in gate_stores.values():
            for i in range(limit + 1):
                store[(f"203.0.113.{i % 251}", f"u{i}")] = (1, stale)
            self.assertGreater(len(store), limit, "前置：先撑出超限")
        # 最后一击换个出口 IP：否则该会话仍在冷却里，门禁只**读**不写计数表，
        # trim 不会被调用，测的就不是"写入路径有界"而是"拒绝路径提前返回"了。
        with mock.patch.object(self.webapp, "_ip_store_trim", side_effect=spy), \
             mock.patch.object(self.webapp, "_client_ip", return_value="198.51.100.7"):
            r = self._attempt(c, "switch", WRONG_PASS)
        self.assertEqual(r.status_code, 403, "前置：新出口 IP 不受旧键冷却影响")
        for store in gate_stores.values():
            self.assertLess(len(store), 10,
                            f"过期条目应被 trim 回收，实际残留 {len(store)} 条")


if __name__ == "__main__":
    unittest.main(verbosity=2)
