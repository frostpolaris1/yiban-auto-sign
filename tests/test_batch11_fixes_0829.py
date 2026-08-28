# -*- coding: utf-8 -*-
"""批次11 修复回归（2026-08-29，外部审查报告 N1-N6 处置）。

覆盖：
- N1  注销恢复（/api/me/restore）签发 sid：恢复后新会话立即有效；注销前被窃取
      的旧 cookie 在恢复后失效（注销/恢复不轮换 sid，恢复即登录必须轮换）。
- N2  审计/事件清理接入时钟跳变守卫：时钟被拨快 >72h 时跳过清理（取证数据源
      不被一次性清空）。
- N3  /api/users/deleted/purge 收归主管理员：普通管理员 403；主管理员正常清除
      并即时告警。
- N5  ACCOUNTS_KEY 轮换工具（scripts/rekey_accounts.py）：重加密后新钥可解、
      明文一致；旧钥错误时拒绝写库。
- N6  受害者安全邮件（绕过 mail_notify 开关）与管理员告警补齐：改密/关通知/
      删号给本人邮件；重置密码/提降权/公告/邮件配置/purge 管理员告警。

用法（项目根目录）：
    py -m pytest tests/test_batch11_fixes_0829.py -v
"""
import contextlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "scripts"))
sys.path.insert(0, os.path.join(BASE, "web"))

TEST_KEY = "a" * 64
NEW_KEY = "b" * 64
ADMIN_PASS = "TestPass1234!"
USER_PASS = "secret1"
EMAIL = "user1@test.local"


class _Batch11WebBase(unittest.TestCase):
    """N1/N2/N3/N6 共用：隔离环境 + 告警/邮件 mock。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-batch11-")
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
        # 告警/邮件全量 mock（conftest 已禁用真实发信；此处再拦截调用便于断言）
        self.alerts = []
        self.user_mails = []
        p1 = mock.patch.object(self.webapp, "send_notification",
                               side_effect=lambda t, c: self.alerts.append((t, c)))
        p2 = mock.patch.object(self.webapp.mailer, "send_user",
                               side_effect=lambda to, s, c: self.user_mails.append((to, s)))
        p1.start(); p2.start()
        self.addCleanup(p1.stop); self.addCleanup(p2.stop)

    # ---- 工具 ----
    def _login(self, c, username, password):
        r = c.post("/api/login", json={"username": username, "password": password})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return c.get("/api/me").get_json()["csrf_token"]

    def _csrf(self, token):
        return {"X-CSRF-Token": token}

    def _user_row(self, email):
        conn = db.get_conn()
        return conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()

    def _session_cookie(self, c):
        for header in (c.get("/api/me").headers.getlist("Set-Cookie") or []):
            name_val = header.split(";", 1)[0]
            name, _, val = name_val.partition("=")
            if name.strip() and val:
                return name.strip(), val
        self.fail("未找到会话 cookie")

    def _client_with_cookie(self, cookie_name, cookie_val):
        c = self.webapp.create_app().test_client()
        injected = False
        for kwargs in (
            {"key": cookie_name, "value": cookie_val, "domain": "localhost", "path": "/"},
            {"server_name": "localhost", "key": cookie_name, "value": cookie_val, "path": "/"},
        ):
            try:
                c.set_cookie(**kwargs)
                injected = True
                break
            except TypeError:
                continue
        if not injected:
            self.fail("当前 Werkzeug 版本无法注入测试 cookie")
        return c

    def _admin_client(self):
        c = self.webapp.create_app().test_client()
        return c, self._login(c, "admin", ADMIN_PASS)


class Batch11RestoreSidTest(_Batch11WebBase):
    """N1：恢复即登录签发 sid。"""

    def _register_login_delete_restore(self, email):
        """建号→登录→注销→恢复，返回 (注销前 client, 恢复后 client)。"""
        db.create_user(email, self.webapp.generate_password_hash(USER_PASS))
        c = self.webapp.create_app().test_client()
        t = self._login(c, email, USER_PASS)
        r = c.post("/api/me/delete", json={"password": USER_PASS}, headers=self._csrf(t))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        r = c.post("/api/me/restore", json={"email": email, "password": USER_PASS})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return c

    def test_restore_session_stays_valid(self):
        c = self._register_login_delete_restore("n1a@test.local")
        r = c.get("/api/me")
        self.assertEqual(r.status_code, 200, "N1 修复：恢复后新会话必须立即可用")
        sid = (self._user_row("n1a@test.local") or {})["sid"]
        self.assertTrue(sid, "恢复后库内 sid 应为恢复时签发的新值")

    def test_stolen_cookie_dead_after_restore(self):
        email = "n1b@test.local"
        db.create_user(email, self.webapp.generate_password_hash(USER_PASS))
        c = self.webapp.create_app().test_client()
        t = self._login(c, email, USER_PASS)
        name, val = self._session_cookie(c)
        r = c.post("/api/me/delete", json={"password": USER_PASS}, headers=self._csrf(t))
        self.assertEqual(r.status_code, 200)
        stolen = self._client_with_cookie(name, val)
        self.assertEqual(stolen.get("/api/me").status_code, 401, "注销后旧 cookie 先失效")
        r = self.webapp.create_app().test_client().post(
            "/api/me/restore", json={"email": email, "password": USER_PASS})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(
            stolen.get("/api/me").status_code, 401,
            "N1 修复：注销前被窃取的 cookie 恢复后必须保持失效（恢复轮换 sid）",
        )


class Batch11PurgeMasterOnlyTest(_Batch11WebBase):
    """N3：purge 收归主管理员 + N6 即时告警。"""

    def _make_victim(self, email):
        db.create_user(email, self.webapp.generate_password_hash(USER_PASS))
        c = self.webapp.create_app().test_client()
        t = self._login(c, email, USER_PASS)
        r = c.post("/api/me/delete", json={"password": USER_PASS}, headers=self._csrf(t))
        self.assertEqual(r.status_code, 200)

    def _make_registered_admin(self, email):
        db.create_user(email, self.webapp.generate_password_hash(USER_PASS))
        return email

    def test_registered_admin_forbidden(self):
        self._make_victim("victim@test.local")
        self._make_registered_admin("admin2@test.local")
        c = self.webapp.create_app().test_client()
        self._login(c, "admin2@test.local", USER_PASS)
        r = c.post("/api/users/deleted/purge", json={"emails": ["victim@test.local"]},
                   headers=self._csrf(c.get("/api/me").get_json()["csrf_token"]))
        self.assertEqual(r.status_code, 403, "N3 修复：普通管理员不可物理清除注销用户")
        self.assertIsNotNone(self._user_row("victim@test.local"), "行未被清除")

    def test_master_can_purge_with_alert(self):
        self._make_victim("victim2@test.local")
        ac, at = self._admin_client()
        r = ac.post("/api/users/deleted/purge", json={"emails": ["victim2@test.local"]},
                    headers=self._csrf(at))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertIsNone(self._user_row("victim2@test.local"), "主管理员清除生效")
        self.assertTrue(any("高危管理操作告警" == t for t, _ in self.alerts),
                        f"purge 应即时告警，实际 {self.alerts}")


class Batch11NotifyCoverageTest(_Batch11WebBase):
    """N6：受害者安全邮件 + 管理员告警补齐。"""

    def _user_with_account(self, email, phone):
        db.create_user(email, self.webapp.generate_password_hash(USER_PASS))
        c = self.webapp.create_app().test_client()
        t = self._login(c, email, USER_PASS)
        r = c.post("/api/my-accounts", json={"name": "n", "phone": phone, "password": "p"},
                   headers=self._csrf(t))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        # 审核通过（提权类用例要求"正式用户"：有生效账号且无待审核）
        ac, at = self._admin_client()
        accounts = db.load_accounts()
        idx = next(i for i, a in enumerate(accounts) if a["phone"] == phone)
        r = ac.post(f"/api/accounts/{idx}/review", json={"action": "approve"},
                    headers=self._csrf(at))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return c, t

    def test_password_change_notifies_user_and_admins(self):
        c, t = self._user_with_account(EMAIL, "13800138001")
        r = c.post("/api/me/password", json={
            "old_password": USER_PASS, "new_password": "NewPass#777",
            "confirm_password": "NewPass#777",
        }, headers=self._csrf(t))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(any(to == EMAIL for to, _ in self.user_mails),
                        "改密必须给本人发安全邮件（绕过 mail_notify 开关）")
        self.assertTrue(any("账号安全事件告警" == t for t, _ in self.alerts),
                        f"改密应有管理员告警，实际 {self.alerts}")

    def test_mail_notify_off_sends_confirm_mail_to_owner(self):
        c, t = self._user_with_account(EMAIL, "13800138002")
        r = c.put("/api/my-mail-notify", json={"enabled": False}, headers=self._csrf(t))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(any(to == EMAIL for to, _ in self.user_mails),
                        "关闭通知必须给本人发确认邮件（不受刚关闭的开关影响）")
        self.user_mails.clear()
        r = c.put("/api/my-mail-notify", json={"enabled": True}, headers=self._csrf(t))
        self.assertEqual(r.status_code, 200)
        self.assertFalse(self.user_mails, "重新开启通知不需再发确认邮件")

    def test_account_delete_mails_owner(self):
        c, t = self._user_with_account(EMAIL, "13800138003")
        accounts = db.load_accounts()
        idx = next(i for i, a in enumerate(accounts) if a["phone"] == "13800138003")
        r = c.delete(f"/api/my-accounts/{idx}", headers=self._csrf(t))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(any(to == EMAIL for to, _ in self.user_mails),
                        "删号必须给本人发留痕邮件")

    def test_single_reset_password_alerts(self):
        self._user_with_account(EMAIL, "13800138004")
        ac, at = self._admin_client()
        r = ac.post(f"/api/users/{EMAIL}/password", json={"password": "Reset#12345"},
                    headers=self._csrf(at))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(any("密码重置告警" == t for t, _ in self.alerts),
                        f"重置密码应有告警，实际 {self.alerts}")

    def test_batch_reset_and_set_admin_alert(self):
        self._user_with_account(EMAIL, "13800138005")
        ac, at = self._admin_client()
        r = ac.post("/api/users/batch", json={
            "action": "reset_password", "emails": [EMAIL], "password": "Reset#12345",
        }, headers=self._csrf(at))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(any("密码重置告警" == t for t, _ in self.alerts),
                        f"批量重置应有告警，实际 {self.alerts}")
        self.alerts.clear()
        r = ac.post("/api/users/batch", json={
            "action": "set_admin", "emails": [EMAIL],
        }, headers=self._csrf(at))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(any("权限变更告警" == t for t, _ in self.alerts),
                        f"批量提权应有告警，实际 {self.alerts}")

    def test_role_change_alerts(self):
        self._user_with_account(EMAIL, "13800138006")
        ac, at = self._admin_client()
        r = ac.post(f"/api/users/{EMAIL}/role", json={"role": "admin"},
                    headers=self._csrf(at))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(any("权限变更告警" == t for t, _ in self.alerts),
                        f"角色变更应有告警，实际 {self.alerts}")

    def test_announcement_change_alerts(self):
        ac, at = self._admin_client()
        r = ac.put("/api/announcement", json={"text": "维护通知"},
                   headers=self._csrf(at))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(any("公告变更告警" == t for t, _ in self.alerts),
                        f"公告变更应有告警，实际 {self.alerts}")

    def test_mail_config_change_alerts(self):
        ac, at = self._admin_client()
        r = ac.put("/api/mail-config", json={"enabled": True}, headers=self._csrf(at))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(any("邮件配置变更告警" == t for t, _ in self.alerts),
                        f"邮件配置变更应有告警，实际 {self.alerts}")


class Batch11CleanupClockGuardTest(_Batch11WebBase):
    """N2：审计/事件清理接入时钟跳变守卫。"""

    def _set_guard_ref(self, key, dt):
        conn = db.get_conn()
        conn.execute("UPDATE app_meta SET value=? WHERE key=?",
                     (dt.strftime("%Y-%m-%d %H:%M:%S"), key))
        conn.commit()

    def test_audit_cleanup_skipped_on_clock_jump(self):
        for i in range(3):
            db.audit("admin", f"op{i}", "t", "d")
        conn = db.get_conn()
        with db._conn_lock:
            db._audit_cleanup(conn)  # 首次调用建立守卫参照
        n_before = conn.execute("SELECT COUNT(*) FROM audit_logs").fetchone()[0]
        self.assertGreaterEqual(n_before, 3)
        # 参照拨回 8 天前 → 下次调用视为前进 8 天（>72h）→ 跳过清理
        with db._conn_lock:
            self._set_guard_ref("audit_cleanup_clock", datetime.now() - timedelta(days=8))
            db._audit_cleanup(conn)
        n_after = conn.execute("SELECT COUNT(*) FROM audit_logs").fetchone()[0]
        self.assertEqual(n_after, n_before, "时钟跳变时审计清理必须被跳过")

    def test_event_cleanup_skipped_on_clock_jump(self):
        db.add_sign_event("2026-08-29 10:00:00", "13800138000", "success", "m")
        conn = db.get_conn()
        with db._conn_lock:
            db._event_cleanup(conn)  # 首次调用建立守卫参照
        n_before = conn.execute("SELECT COUNT(*) FROM sign_events").fetchone()[0]
        self.assertGreaterEqual(n_before, 1)
        with db._conn_lock:
            self._set_guard_ref("event_cleanup_clock", datetime.now() - timedelta(days=8))
            db._event_cleanup(conn)
        n_after = conn.execute("SELECT COUNT(*) FROM sign_events").fetchone()[0]
        self.assertEqual(n_after, n_before, "时钟跳变时事件清理必须被跳过")


class Batch11RekeyToolTest(_Batch11WebBase):
    """N5：ACCOUNTS_KEY 轮换工具。"""

    def _seed_encrypted_account(self, phone, password):
        db.create_user(EMAIL, self.webapp.generate_password_hash(USER_PASS))
        c = self.webapp.create_app().test_client()
        t = self._login(c, EMAIL, USER_PASS)
        r = c.post("/api/my-accounts", json={
            "name": "n", "phone": phone, "password": password, "phone_code": "code-x",
        }, headers=self._csrf(t))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None

    def _read_secret(self, conn, phone, col):
        raw = conn.execute(f"SELECT {col} FROM accounts WHERE phone=?", (phone,)).fetchone()[0]
        return json.loads(raw)

    def test_rekey_roundtrip(self):
        self._seed_encrypted_account("13900000001", "plain-pw-1")
        import account_crypto
        import rekey_accounts
        ok, note = rekey_accounts.rekey(self.db_file, bytes.fromhex(TEST_KEY), bytes.fromhex(NEW_KEY))
        self.assertTrue(ok, note)
        conn = sqlite3_connect(self.db_file)
        try:
            new_obj = self._read_secret(conn, "13900000001", "password")
            self.assertEqual(
                account_crypto.decrypt_password(new_obj, bytes.fromhex(NEW_KEY), "13900000001"),
                "plain-pw-1", "新钥必须能解密且明文一致",
            )
            with self.assertRaises(ValueError):
                account_crypto.decrypt_password(new_obj, bytes.fromhex(TEST_KEY), "13900000001")
            code_obj = self._read_secret(conn, "13900000001", "phone_code")
            self.assertEqual(
                account_crypto.decrypt_password(code_obj, bytes.fromhex(NEW_KEY), "13900000001"),
                "code-x",
            )
        finally:
            conn.close()

    def test_rekey_rejects_wrong_old_key(self):
        self._seed_encrypted_account("13900000002", "plain-pw-2")
        import rekey_accounts
        conn = sqlite3_connect(self.db_file)
        before = conn.execute("SELECT password FROM accounts WHERE phone=?",
                              ("13900000002",)).fetchone()[0]
        conn.close()
        ok, note = rekey_accounts.rekey(self.db_file, bytes.fromhex(NEW_KEY), bytes.fromhex("c" * 64))
        self.assertFalse(ok, "旧钥不对必须拒绝")
        conn = sqlite3_connect(self.db_file)
        after = conn.execute("SELECT password FROM accounts WHERE phone=?",
                             ("13900000002",)).fetchone()[0]
        conn.close()
        self.assertEqual(before, after, "拒绝时库必须保持原状")

    def test_update_env_key_writes_and_rotates(self):
        import rekey_accounts
        rekey_accounts.update_env_key(self.env_file, bytes.fromhex(NEW_KEY))
        content = open(self.env_file, encoding="utf-8-sig").read()
        self.assertIn(f"YIBAN_ACCOUNTS_KEY={NEW_KEY}", content)
        self.assertEqual(content.count("YIBAN_ACCOUNTS_KEY="), 1, "旧键行应被替换而非叠加")


def sqlite3_connect(path):
    import sqlite3
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


if __name__ == "__main__":
    unittest.main(verbosity=2)
