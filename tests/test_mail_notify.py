# -*- coding: utf-8 -*-
"""邮箱通知 B 线（用户签到失败邮件）+ 用户开关测试。

覆盖：
- db 迁移 v9：新库 users 含 mail_notify 列；旧库（v8）缺列自动补齐；
- B 线 send_user_fail_mail：owner 空 / 用户不存在 / 开关关闭 → 不发送；
  开关开启 → 发送且内容脱敏（手机号打码、不含完整号）；
- Web API /api/my-mail-notify：未登录默认开、401/400 防护、保存生效、审计留痕。

全程本地（临时 sqlite + Flask test client + mock mailer），无真实网络请求。
用法（项目根目录）：py -m pytest tests/test_mail_notify.py -v
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
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "scripts"))
sys.path.insert(0, os.path.join(BASE, "web"))

TEST_KEY = "b" * 64
ADMIN_PASS = "TestPass1234!"
USER_PASS = "secret1"


def _reset_db():
    if db._conn is not None:
        with contextlib.suppress(Exception):
            db._conn.close()
        db._conn = None


def _remove_db_files(db_file):
    for suffix in ("", "-wal", "-shm"):
        p = db_file + suffix
        if os.path.exists(p):
            os.remove(p)


class DbMailNotifyMigrationTest(unittest.TestCase):
    """v9 迁移：users.mail_notify 列（默认开启）。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-mail-mig-")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write("YIBAN_ACCOUNTS_KEY=" + TEST_KEY + "\n")
        os.environ["YIBAN_DB_FILE"] = cls.db_file
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        global db
        import db

    @classmethod
    def tearDownClass(cls):
        _reset_db()
        os.environ.pop("YIBAN_DB_FILE", None)
        os.environ.pop("YIBAN_ENV_FILE", None)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        _reset_db()
        _remove_db_files(self.db_file)

    def _columns(self, table):
        conn = sqlite3.connect(self.db_file)
        try:
            return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
        finally:
            conn.close()

    def test_new_db_has_mail_notify(self):
        db.init_db(self.db_file, env_file=self.env_file)
        self.assertIn("mail_notify", self._columns("users"))
        v = db.get_conn().execute("PRAGMA user_version").fetchone()[0]
        self.assertGreaterEqual(v, 9, "新库应执行到 v9")

    def test_old_db_auto_adds_column(self):
        conn = sqlite3.connect(self.db_file)
        conn.executescript(
            "CREATE TABLE users ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT NOT NULL, "
            "password_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'user', "
            "created_at TEXT NOT NULL DEFAULT '', pw_version INTEGER NOT NULL DEFAULT 1, "
            "deleted INTEGER NOT NULL DEFAULT 0, deleted_at TEXT NOT NULL DEFAULT '');"
        )
        conn.execute("PRAGMA user_version = 8")
        conn.commit()
        conn.close()
        db.init_db(self.db_file, env_file=self.env_file)
        self.assertIn("mail_notify", self._columns("users"), "旧库应自动补齐 mail_notify 列")


class SignUserFailMailTest(unittest.TestCase):
    """B 线：send_user_fail_mail 开关与脱敏。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-mail-b-")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write("YIBAN_ACCOUNTS_KEY=" + TEST_KEY + "\n")
        os.environ["YIBAN_DB_FILE"] = cls.db_file
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        # v0.24.3 起 B 线邮件有「每账号每日 1 封」的按天状态文件（STATE_DIR），
        # 必须隔离到临时目录，否则同日第二次全量跑会因残留配额而失败
        cls._old_state_dir = os.environ.get("YIBAN_STATE_DIR")
        os.environ["YIBAN_STATE_DIR"] = cls.tmp
        global db
        import db
        global signin
        import signin

    @classmethod
    def tearDownClass(cls):
        _reset_db()
        os.environ.pop("YIBAN_DB_FILE", None)
        os.environ.pop("YIBAN_ENV_FILE", None)
        if cls._old_state_dir is None:
            os.environ.pop("YIBAN_STATE_DIR", None)
        else:
            os.environ["YIBAN_STATE_DIR"] = cls._old_state_dir
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        _reset_db()
        _remove_db_files(self.db_file)
        db.init_db(self.db_file, env_file=self.env_file)
        db.create_user("owner@test.local", "x")  # mail_notify 默认 1

    def test_owner_empty_skips(self):
        with mock.patch.object(signin.mailer, "send_user") as m:
            signin.send_user_fail_mail("", "13800000000", "boom")
        m.assert_not_called()

    def test_unknown_user_skips(self):
        with mock.patch.object(signin.mailer, "send_user") as m:
            signin.send_user_fail_mail("nobody@test.local", "13800000000", "boom")
        m.assert_not_called()

    def test_notify_off_skips(self):
        db.update_user("owner@test.local", {"mail_notify": 0})
        with mock.patch.object(signin.mailer, "send_user") as m:
            signin.send_user_fail_mail("owner@test.local", "13800000000", "boom")
        m.assert_not_called()

    def test_notify_on_sends_masked(self):
        with mock.patch.object(signin.mailer, "send_user") as m:
            signin.send_user_fail_mail("owner@test.local", "13800000000", "boom")
        m.assert_called_once()
        to, subject, text = m.call_args[0]
        self.assertEqual(to, "owner@test.local")
        self.assertEqual(subject, "易班签到失败提醒")
        self.assertIn("138****0000", text)
        self.assertNotIn("13800000000", text, "邮件不得含完整手机号")


class MailNotifyApiTest(unittest.TestCase):
    """Web API：用户端邮箱通知开关读写。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-mail-api-")
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
        _reset_db()
        for k in ("YIBAN_ACCOUNTS_KEY", "YIBAN_ENV_FILE", "YIBAN_ACCOUNTS_FILE",
                  "YIBAN_USERS_FILE", "YIBAN_DB_FILE", "YIBAN_STATE_DIR"):
            os.environ.pop(k, None)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        _reset_db()
        _remove_db_files(self.db_file)
        with open(self.accounts_file, "w", encoding="utf-8") as f:
            json.dump([], f)
        db.init_db(self.db_file, migrate_from=self.accounts_file, env_file=self.env_file)
        db.create_user("admin@test.local", self.webapp.generate_password_hash(ADMIN_PASS), role="admin")
        db.create_user("user1@test.local", self.webapp.generate_password_hash(USER_PASS))

    def _login(self, c, username, password):
        r = c.post("/api/login", json={"username": username, "password": password})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return c.get("/api/me").get_json()["csrf_token"]

    def test_get_requires_login(self):
        c = self.webapp.create_app().test_client()
        r = c.get("/api/my-mail-notify")
        self.assertEqual(r.status_code, 401, "未登录访问 my-* 应被认证守卫拦截")

    def test_get_reads_current_value(self):
        c = self.webapp.create_app().test_client()
        token = self._login(c, "user1@test.local", USER_PASS)
        r = c.get("/api/my-mail-notify")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["mail_notify"], "默认开启")
        c.put("/api/my-mail-notify", json={"enabled": False},
              headers={"X-CSRF-Token": token})
        r = c.get("/api/my-mail-notify")
        self.assertFalse(r.get_json()["mail_notify"], "保存后读值应反映最新状态")

    def test_put_requires_login(self):
        c = self.webapp.create_app().test_client()
        r = c.put("/api/my-mail-notify", json={"enabled": False})
        self.assertEqual(r.status_code, 401)

    def test_put_builtin_admin_returns_404(self):
        """内置管理员（.env 账号，不在 users 表）改邮件开关必须 404 而非谎报成功。

        C-M1（2026-08-28）：update_user 对不存在的邮箱是静默 no-op，原实现返回
        ok:true——刷新后开关弹回开启，还写入一条不存在的变更审计污染审计链。
        """
        c = self.webapp.create_app().test_client()
        token = self._login(c, "admin", ADMIN_PASS)
        r = c.put("/api/my-mail-notify", json={"enabled": False},
                  headers={"X-CSRF-Token": token})
        self.assertEqual(r.status_code, 404, r.get_data(as_text=True))
        self.assertIn("内置管理员", r.get_json()["error"])

    def test_login_username_case_normalized(self):
        """混合大小写邮箱登录：会话用户名归一小写，邮件开关读写不因大小写失配而失效（2026-08-27）。"""
        c = self.webapp.create_app().test_client()
        # 库内为小写 user1@test.local；以混合大小写登录
        token = self._login(c, "User1@Test.Local", USER_PASS)
        with c.session_transaction() as sess:
            self.assertEqual(sess["username"], "user1@test.local",
                             "会话用户名应归一小写，否则大小写敏感的库查询失配")
        # 端到端：开关可正常持久化（修复前 PUT 更新 0 行、GET 仍返回默认值）
        c.put("/api/my-mail-notify", json={"enabled": False},
              headers={"X-CSRF-Token": token})
        r = c.get("/api/my-mail-notify")
        self.assertFalse(r.get_json()["mail_notify"], "混合大小写登录后开关应可持久化")

    def test_save_off_then_read(self):
        c = self.webapp.create_app().test_client()
        token = self._login(c, "user1@test.local", USER_PASS)
        r = c.put("/api/my-mail-notify", json={"enabled": False},
                  headers={"X-CSRF-Token": token})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertFalse(r.get_json()["mail_notify"])
        self.assertEqual(db.find_user("user1@test.local")["mail_notify"], 0)
        r = c.get("/api/my-mail-notify")
        self.assertFalse(r.get_json()["mail_notify"])

    def test_save_on_restores(self):
        c = self.webapp.create_app().test_client()
        token = self._login(c, "user1@test.local", USER_PASS)
        c.put("/api/my-mail-notify", json={"enabled": False},
              headers={"X-CSRF-Token": token})
        r = c.put("/api/my-mail-notify", json={"enabled": True},
                  headers={"X-CSRF-Token": token})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["mail_notify"])

    def test_invalid_value_400(self):
        c = self.webapp.create_app().test_client()
        token = self._login(c, "user1@test.local", USER_PASS)
        r = c.put("/api/my-mail-notify", json={"enabled": "yes"},
                  headers={"X-CSRF-Token": token})
        self.assertEqual(r.status_code, 400)

    def test_csrf_required(self):
        c = self.webapp.create_app().test_client()
        self._login(c, "user1@test.local", USER_PASS)
        r = c.put("/api/my-mail-notify", json={"enabled": False})
        self.assertEqual(r.status_code, 403, "缺少 X-CSRF-Token 应被全局 CSRF 校验拦截")

    def test_audit_written(self):
        c = self.webapp.create_app().test_client()
        token = self._login(c, "user1@test.local", USER_PASS)
        c.put("/api/my-mail-notify", json={"enabled": False},
              headers={"X-CSRF-Token": token})
        conn = sqlite3.connect(self.db_file)
        try:
            actions = [r[0] for r in conn.execute(
                "SELECT action FROM audit_logs WHERE action='mail_notify' ORDER BY id")]
        finally:
            conn.close()
        self.assertEqual(actions, ["mail_notify"])

    # ---- 全局邮件开关（/api/mail-config）----
    def test_mail_config_get_status(self):
        c = self.webapp.create_app().test_client()
        token = self._login(c, "admin@test.local", ADMIN_PASS)
        r = c.get("/api/mail-config")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertIn("enabled", r.get_json())
        self.assertIn("admin_to", r.get_json())

    def test_mail_config_put_requires_master_admin(self):
        c = self.webapp.create_app().test_client()
        token = self._login(c, "admin@test.local", ADMIN_PASS)  # 注册管理员，非主管理员
        r = c.put("/api/mail-config", json={"enabled": True}, headers={"X-CSRF-Token": token})
        self.assertEqual(r.status_code, 403, "仅主管理员可切换全局开关")

    def test_mail_config_put_by_master_writes_env(self):
        c = self.webapp.create_app().test_client()
        token = self._login(c, "admin", ADMIN_PASS)  # 内置主管理员（.env）
        r = c.put("/api/mail-config", json={"enabled": False}, headers={"X-CSRF-Token": token})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertFalse(r.get_json()["enabled"])
        with open(self.env_file, encoding="utf-8") as f:
            self.assertIn("YIBAN_MAIL_ENABLE=0", f.read(), "应写入 .env")

    def test_mail_config_put_admin_notify_by_master(self):
        c = self.webapp.create_app().test_client()
        token = self._login(c, "admin", ADMIN_PASS)
        r = c.put("/api/mail-config", json={"admin_notify": False}, headers={"X-CSRF-Token": token})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertFalse(r.get_json()["admin_notify"])
        with open(self.env_file, encoding="utf-8") as f:
            self.assertIn("YIBAN_MAIL_ADMIN_NOTIFY=0", f.read(), "应写入 .env")


class SignAdminMailSummaryTest(unittest.TestCase):
    """A 线合并版：管理员邮件收集 → 任务结束汇总一封发送。"""

    @classmethod
    def setUpClass(cls):
        global signin
        import signin

    def setUp(self):
        signin._mail_summary.clear()

    def test_flush_empty_skips(self):
        with mock.patch.object(signin.mailer, "send_admin_alert") as m:
            signin._flush_admin_mail_summary()
        m.assert_not_called()

    def test_collect_and_flush_merges_one_mail(self):
        signin._collect_admin_mail("易班签到失败", "账号: 138****0001\n原因: A")
        signin._collect_admin_mail("易班签到失败", "账号: 138****0002\n原因: B")
        with mock.patch.object(signin.mailer, "send_admin_alert") as m:
            signin._flush_admin_mail_summary()
        m.assert_called_once()
        subject, text = m.call_args[0]
        self.assertEqual(subject, "易班签到汇总")
        self.assertIn("共 2 条异常", text)
        self.assertIn("138****0001", text)
        self.assertIn("138****0002", text)

    def test_flush_groups_by_subject(self):
        signin._collect_admin_mail("易班签到失败", "账号: 138****0001\n原因: A")
        signin._collect_admin_mail("易班签到耗时告警", "账号: 138****0002\n耗时: 45.2s")
        with mock.patch.object(signin.mailer, "send_admin_alert") as m:
            signin._flush_admin_mail_summary()
        subject, text = m.call_args[0]
        self.assertIn("【易班签到失败】", text)
        self.assertIn("【易班签到耗时告警】", text)
        self.assertLess(
            text.index("【易班签到失败】"), text.index("【易班签到耗时告警】"),
            "按主题分组且保持首次出现顺序",
        )

    def test_flush_clears_collector(self):
        signin._collect_admin_mail("易班签到失败", "账号: 138****0001\n原因: A")
        with mock.patch.object(signin.mailer, "send_admin_alert"):
            signin._flush_admin_mail_summary()
        self.assertEqual(signin._mail_summary, [], "发送后应清空收集器")

    def test_flush_uses_filtered_recipients(self):
        signin._collect_admin_mail("易班签到失败", "账号: 138****0001\n原因: A")
        with mock.patch.object(signin.db, "admin_mail_recipients", return_value=["a@x.com"]) as f, \
             mock.patch.object(signin.mailer, "admin_recipients", return_value=["a@x.com", "b@x.com"]), \
             mock.patch.object(signin.mailer, "send_admin_alert") as m:
            signin._flush_admin_mail_summary()
        f.assert_called_once_with(["a@x.com", "b@x.com"])
        m.assert_called_once()
        self.assertEqual(m.call_args[1].get("to"), "a@x.com", "汇总邮件应只发给合并后的收件人")

    def test_flush_skips_admin_to_when_admin_notify_off(self):
        signin._collect_admin_mail("易班签到失败", "账号: 138****0001\n原因: A")
        with mock.patch.object(signin.mailer, "admin_notify_enabled", return_value=False), \
             mock.patch.object(signin.mailer, "admin_recipients", return_value=["master@x.com"]), \
             mock.patch.object(signin.db, "admin_mail_recipients", return_value=["a@x.com"]) as f, \
             mock.patch.object(signin.mailer, "send_admin_alert") as m:
            signin._flush_admin_mail_summary()
        f.assert_called_once_with([]), "主管理员关闭收件时不应传入 ADMIN_TO"


class DbFilterMailNotifyTest(unittest.TestCase):
    """db.filter_mail_notify：A 线收件人按个人开关过滤。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-mail-filter-")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write("YIBAN_ACCOUNTS_KEY=" + TEST_KEY + "\n")
        os.environ["YIBAN_DB_FILE"] = cls.db_file
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        global db
        import db

    @classmethod
    def tearDownClass(cls):
        _reset_db()
        os.environ.pop("YIBAN_DB_FILE", None)
        os.environ.pop("YIBAN_ENV_FILE", None)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        _reset_db()
        _remove_db_files(self.db_file)
        db.init_db(self.db_file, env_file=self.env_file)
        db.create_user("on@x.com", "x")     # mail_notify 默认 1
        db.create_user("off@x.com", "x")
        db.update_user("off@x.com", {"mail_notify": 0})

    def test_keeps_on_and_unknown_removes_off(self):
        result = db.filter_mail_notify(["on@x.com", "off@x.com", "unknown@x.com"])
        self.assertEqual(result, ["on@x.com", "unknown@x.com"], "开启者与非注册用户保留，关闭者剔除")

    def test_empty(self):
        self.assertEqual(db.filter_mail_notify([]), [])

    def test_admin_mail_recipients_merges_admins(self):
        # 普通管理员自动获得收件权；关闭 mail_notify 后剔除；与 ADMIN_TO 去重合并
        db.create_user("admin1@x.com", "x", role="admin")   # mail_notify 默认 1
        db.create_user("admin2@x.com", "x", role="admin")
        db.update_user("admin2@x.com", {"mail_notify": 0})
        result = db.admin_mail_recipients(["master@x.com", "off@x.com", "admin1@x.com"])
        self.assertEqual(
            result, ["admin1@x.com", "master@x.com"],
            "开启接收的管理员 + ADMIN_TO（过滤后）合并去重",
        )

    def test_admin_mail_recipients_no_extra(self):
        db.create_user("admin1@x.com", "x", role="admin")
        result = db.admin_mail_recipients([])
        self.assertEqual(result, ["admin1@x.com"], "无 ADMIN_TO 时仍收所有开启接收的管理员")


if __name__ == "__main__":
    unittest.main(verbosity=2)
