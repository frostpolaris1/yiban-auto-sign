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
        global db
        import db
        global signin
        import signin

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
