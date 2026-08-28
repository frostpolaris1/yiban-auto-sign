# -*- coding: utf-8 -*-
"""邮箱域名黑白名单审查测试（2026-08-28 注册预拦截机制）。

覆盖：
- email_policy 模块单元行为：内置保留域名 / 伪 TLD / 数据文件黑名单 / 子域名
  匹配 / 白名单模式优先级 / 部署追加黑名单 / 数据文件缺失兜底 / mtime 缓存热更新
- web 注册入口集成：开放注册与管理员自动注册路径命中即 400、用户不落库；
  白名单 .env 配置经 email_domain_error 生效

用法（项目根目录）：
    py -m pytest tests/test_email_domain_review.py -v
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

import email_policy  # noqa: E402

TEST_KEY = "a" * 64
ADMIN_PASS = "TestPass1234!"
ERR_BLOCKED = email_policy._ERR_BLOCKED
ERR_NOT_ALLOWLISTED = email_policy._ERR_NOT_ALLOWLISTED


class EmailPolicyUnitTest(unittest.TestCase):
    """email_policy.review_email 纯函数行为。"""

    def test_reserved_domains_blocked(self):
        # RFC 2606/6761 保留域名 + 教程占位域名（用户点名的 example.com/demo.com）
        for email in (
            "user@example.com", "a@example.net", "b@example.org", "c@example.edu",
            "user@demo.com", "user@test.com", "user@test.net",
            "x@foo.com", "y@bar.com", "z@baz.com",
            "user@yourdomain.com", "user@yoursite.com",
        ):
            self.assertEqual(email_policy.review_email(email), ERR_BLOCKED, email)

    def test_reserved_subdomain_blocked(self):
        # 子域名匹配：example.com 命中时 a.b.example.com 一并拦截
        for email in ("user@mail.example.com", "user@a.b.example.com"):
            self.assertEqual(email_policy.review_email(email), ERR_BLOCKED, email)

    def test_pseudo_tld_blocked(self):
        # 伪 TLD 以裸标签入名单，由祖先匹配覆盖：foo.test / x.invalid / mail.localhost
        for email in ("user@foo.test", "user@x.invalid", "user@mail.localhost", "user@any.example"):
            self.assertEqual(email_policy.review_email(email), ERR_BLOCKED, email)

    def test_disposable_domains_blocked(self):
        # 数据文件黑名单：典型一次性邮箱域名及其子域名
        for email in ("a@mailinator.com", "b@10minutemail.com", "c@sub.mailinator.com"):
            self.assertEqual(email_policy.review_email(email), ERR_BLOCKED, email)

    def test_normal_domains_pass(self):
        # 真实常用服务商与内网 .local 域名不受影响（防止误伤存量测试/内网部署惯例）
        for email in (
            "user@gmail.com", "user@qq.com", "user@163.com", "user@126.com",
            "user@outlook.com", "user@hotmail.com", "user@foxmail.com",
            "user@sina.com", "user@sohu.com", "user1@test.local",
        ):
            self.assertIsNone(email_policy.review_email(email), email)

    def test_case_and_space_normalized(self):
        self.assertEqual(email_policy.review_email("U@Example.COM "), ERR_BLOCKED)
        self.assertIsNone(email_policy.review_email(" U@QQ.COM "))

    def test_malformed_input_passthrough(self):
        # 无 @ / 空域名属格式问题，交由调用方 EMAIL_RE 处理，此处不拦截
        self.assertIsNone(email_policy.review_email(""))
        self.assertIsNone(email_policy.review_email("not-an-email"))
        self.assertIsNone(email_policy.review_email("user@"))

    def test_allowlist_mode_exact_match(self):
        allow = "gmail.com,qq.com"
        self.assertIsNone(email_policy.review_email("a@gmail.com", allowlist=allow))
        self.assertIsNone(email_policy.review_email("a@QQ.com", allowlist=allow))
        self.assertEqual(email_policy.review_email("a@163.com", allowlist=allow), ERR_NOT_ALLOWLISTED)

    def test_allowlist_takes_precedence_over_blocklist(self):
        # 白名单显式放行的域名优先于黑名单（管理员意志最高）
        self.assertIsNone(email_policy.review_email("a@mailinator.com", allowlist="mailinator.com"))

    def test_blocklist_extra_with_subdomain(self):
        extra = "mycompany.cn, blockedco.net"
        self.assertEqual(email_policy.review_email("a@mycompany.cn", blocklist_extra=extra), ERR_BLOCKED)
        self.assertEqual(email_policy.review_email("a@sub.blockedco.net", blocklist_extra=extra), ERR_BLOCKED)
        self.assertIsNone(email_policy.review_email("a@gmail.com", blocklist_extra=extra))

    def test_data_file_missing_falls_back_to_reserved(self):
        # 数据文件缺失：仅内置保留名单生效，不阻塞注册
        orig = email_policy._DATA_FILE
        email_policy._DATA_FILE = os.path.join(tempfile.gettempdir(), "no-such-blocklist.conf")
        email_policy._cache["key"] = None
        try:
            self.assertEqual(email_policy.review_email("a@example.com"), ERR_BLOCKED)  # 内置兜底
            self.assertIsNone(email_policy.review_email("a@mailinator.com"))  # 文件名单失效
        finally:
            email_policy._DATA_FILE = orig
            email_policy._cache["key"] = None

    def test_data_file_mtime_cache_hot_reload(self):
        # mtime+size 缓存：替换数据文件后无需重启即生效
        # （测试域名避开 .example/.test 等内置保留伪 TLD，确保命中的确来自数据文件）
        tmpdir = tempfile.mkdtemp(prefix="yiban-policy-")
        orig = email_policy._DATA_FILE
        fake = os.path.join(tmpdir, "blocklist.conf")
        try:
            with open(fake, "w", encoding="utf-8") as f:
                f.write("# 注释行\nfirst.mailfake.cn\n")
            email_policy._DATA_FILE = fake
            email_policy._cache["key"] = None
            self.assertEqual(email_policy.review_email("a@first.mailfake.cn"), ERR_BLOCKED)
            self.assertIsNone(email_policy.review_email("a@second.mailfake.cn"))
            # 内容变更（追加域名）→ key 变化 → 重新加载
            with open(fake, "w", encoding="utf-8") as f:
                f.write("first.mailfake.cn\nsecond.mailfake.cn\n")
            self.assertEqual(email_policy.review_email("a@second.mailfake.cn"), ERR_BLOCKED)
        finally:
            email_policy._DATA_FILE = orig
            email_policy._cache["key"] = None
            shutil.rmtree(tmpdir, ignore_errors=True)


class EmailDomainReviewWebTest(unittest.TestCase):
    """注册入口集成：开放注册 / 管理员自动注册均受域名审查约束。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-email-policy-")
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
        self.webapp.write_env_key(self.env_file, "YIBAN_EMAIL_DOMAIN_ALLOWLIST", "")
        self.webapp.write_env_key(self.env_file, "YIBAN_EMAIL_DOMAIN_BLOCKLIST_EXTRA", "")

    # ---- 工具 ----
    def _login(self, c, username, password):
        r = c.post("/api/login", json={"username": username, "password": password})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return c.get("/api/me").get_json()["csrf_token"]

    def _csrf(self, token):
        return {"X-CSRF-Token": token}

    # ---- 开放注册 /api/register ----
    def test_register_blocks_placeholder_domain(self):
        c = self.webapp.create_app().test_client()
        r = c.post("/api/register", json={
            "email": "newuser@example.com", "password": "UserPass123!", "agree": True,
        })
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        self.assertEqual(r.get_json()["error"], ERR_BLOCKED)
        self.assertIsNone(db.find_user_any("newuser@example.com"))

    def test_register_blocks_demo_and_disposable_domain(self):
        c = self.webapp.create_app().test_client()
        for email in ("newuser@demo.com", "newuser@mailinator.com"):
            r = c.post("/api/register", json={
                "email": email, "password": "UserPass123!", "agree": True,
            })
            self.assertEqual(r.status_code, 400, f"{email}: {r.get_data(as_text=True)}")
            self.assertIsNone(db.find_user_any(email))

    def test_register_allows_real_provider(self):
        # 阳性对照：真实服务商域名正常注册成功
        c = self.webapp.create_app().test_client()
        r = c.post("/api/register", json={
            "email": "realuser@qq.com", "password": "UserPass123!", "agree": True,
        })
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertIsNotNone(db.find_user("realuser@qq.com"))

    def test_register_allowlist_from_env(self):
        # .env 白名单：名单外域名一律拒绝（含本可通过黑名单的真实域名）
        self.webapp.write_env_key(self.env_file, "YIBAN_EMAIL_DOMAIN_ALLOWLIST", "qq.com,163.com")
        c = self.webapp.create_app().test_client()
        r = c.post("/api/register", json={
            "email": "wl@gmail.com", "password": "UserPass123!", "agree": True,
        })
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        self.assertEqual(r.get_json()["error"], ERR_NOT_ALLOWLISTED)
        self.assertIsNone(db.find_user_any("wl@gmail.com"))
        r2 = c.post("/api/register", json={
            "email": "wl@qq.com", "password": "UserPass123!", "agree": True,
        })
        self.assertEqual(r2.status_code, 200, r2.get_data(as_text=True))
        self.assertIsNotNone(db.find_user("wl@qq.com"))

    def test_register_blocklist_extra_from_env(self):
        # .env 追加黑名单：部署自定义域名（含子域名）被拦截
        self.webapp.write_env_key(self.env_file, "YIBAN_EMAIL_DOMAIN_BLOCKLIST_EXTRA", "blocked.example")
        c = self.webapp.create_app().test_client()
        for email in ("x@blocked.example", "y@sub.blocked.example"):
            r = c.post("/api/register", json={
                "email": email, "password": "UserPass123!", "agree": True,
            })
            self.assertEqual(r.status_code, 400, f"{email}: {r.get_data(as_text=True)}")
            self.assertEqual(r.get_json()["error"], ERR_BLOCKED)

    # ---- 管理员自动注册路径 /api/accounts ----
    def test_admin_add_auto_register_blocks_placeholder_domain(self):
        c = self.webapp.create_app().test_client()
        token = self._login(c, "admin", ADMIN_PASS)
        r = c.post("/api/accounts", json={
            "name": "占位邮箱",
            "phone": "13800138000",
            "password": "account-pass",
            "email": "owner@example.com",
            "initial_password": "UserPass123!",
        }, headers=self._csrf(token))
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        self.assertEqual(r.get_json()["error"], ERR_BLOCKED)
        self.assertIsNone(db.find_user_any("owner@example.com"))

    def test_admin_add_auto_register_allows_real_provider(self):
        # 阳性对照：真实域名走自动注册成功（账号 + 用户同时创建）
        c = self.webapp.create_app().test_client()
        token = self._login(c, "admin", ADMIN_PASS)
        r = c.post("/api/accounts", json={
            "name": "真实邮箱",
            "phone": "13800138001",
            "password": "account-pass",
            "email": "owner2@qq.com",
            "initial_password": "UserPass123!",
        }, headers=self._csrf(token))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertIsNotNone(db.find_user("owner2@qq.com"))


if __name__ == "__main__":
    unittest.main()
