# -*- coding: utf-8 -*-
"""对抗性测试：用户体验不误杀 + 安全边界。

覆盖：
- 暂停/恢复/再暂停：前几次自由，不误杀好奇用户；
- 快速点选全部时间片：自由次数内不误杀；
- IDOR：用户不能操作他人账号；
- 批量赋值：不能通过请求字段越权设置 owner/deleted/status；
- 日志日期路径穿越：非法日期被拒绝。
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


class AdversarialTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-adversarial-")
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

    def _new_user(self, email, phone):
        db.create_user(email, self.webapp.generate_password_hash(USER_PASS))
        db.add_account({
            "name": "测试账号",
            "phone": phone,
            "password": "p1",
            "phone_model": "",
            "phone_code": "",
            "owner": email,
            "status": "active",
            "reject_reason": "",
        })

    def _login(self, c, username, password):
        r = c.post("/api/login", json={"username": username, "password": password})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return c.get("/api/me").get_json()["csrf_token"]

    def _csrf(self, token):
        return {"X-CSRF-Token": token}

    def test_pause_resume_pause_not_blocked_within_free_limit(self):
        self._new_user("user1@test.local", "13800138001")
        c = self.webapp.create_app().test_client()
        token = self._login(c, "user1@test.local", USER_PASS)
        for _ in range(3):
            r = c.put("/api/my-accounts/0/pause", json={"paused": 1}, headers=self._csrf(token))
            self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
            r = c.put("/api/my-accounts/0/pause", json={"paused": 0}, headers=self._csrf(token))
            self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

    def test_time_pref_rapid_all_slots_not_blocked(self):
        self._new_user("user2@test.local", "13800138002")
        c = self.webapp.create_app().test_client()
        token = self._login(c, "user2@test.local", USER_PASS)
        for slot in range(0, 80, 5):  # 16 个片
            r = c.put("/api/my-time-pref", json={"slot_min": slot}, headers=self._csrf(token))
            self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

    def test_idor_user_cannot_operate_other_account(self):
        self._new_user("alice@test.local", "13800138003")
        self._new_user("bob@test.local", "13900139004")
        c = self.webapp.create_app().test_client()
        token = self._login(c, "alice@test.local", USER_PASS)
        # alice 只有 1 个账号，index=1 应超出她的范围
        r = c.put("/api/my-accounts/1/pause", json={"paused": 1}, headers=self._csrf(token))
        self.assertEqual(r.status_code, 404, r.get_data(as_text=True))

    def test_mass_assignment_cannot_set_owner_deleted_status(self):
        c = self.webapp.create_app().test_client()
        token = self._login(c, "admin", ADMIN_PASS)
        r = c.post("/api/accounts", json={
            "name": "越权测试",
            "phone": "13800138005",
            "password": "p1",
            "owner": "attacker@evil.com",
            "deleted": True,
            "status": "active",
        }, headers=self._csrf(token))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        accounts = db.load_accounts()
        acc = next(a for a in accounts if a["phone"] == "13800138005")
        self.assertEqual(acc["owner"], "admin")
        self.assertFalse(acc["deleted"])
        self.assertEqual(acc["status"], "active")

    def test_invalid_json_returns_400(self):
        c = self.webapp.create_app().test_client()
        r = c.post("/api/login", data="{bad json", content_type="application/json")
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))

    def test_negative_and_oversized_account_indices_rejected(self):
        c = self.webapp.create_app().test_client()
        token = self._login(c, "admin", ADMIN_PASS)
        h = self._csrf(token)
        r1 = c.get("/api/accounts/-1/detail", headers=h)
        self.assertEqual(r1.status_code, 404, r1.get_data(as_text=True))
        r2 = c.get("/api/accounts/99999/detail", headers=h)
        self.assertEqual(r2.status_code, 404, r2.get_data(as_text=True))

    def test_oversized_name_rejected(self):
        c = self.webapp.create_app().test_client()
        token = self._login(c, "admin", ADMIN_PASS)
        r = c.post("/api/accounts", json={
            "name": "x" * 51,
            "phone": "13800138006",
            "password": "p1",
        }, headers=self._csrf(token))
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))

    def test_non_object_json_rejected(self):
        c = self.webapp.create_app().test_client()
        r = c.post("/api/login", data="[1,2,3]", content_type="application/json")
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))

    def test_api_me_requires_login(self):
        c = self.webapp.create_app().test_client()
        r = c.get("/api/me")
        self.assertEqual(r.status_code, 401, r.get_data(as_text=True))

    def test_register_invalid_email_rejected(self):
        c = self.webapp.create_app().test_client()
        r = c.post("/api/register", json={"email": "not-an-email", "password": "StrongPass123!"})
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))

    def test_self_delete_endpoint_soft_deletes_user_and_accounts(self):
        self._new_user("dereg@test.local", "13800138007")
        c = self.webapp.create_app().test_client()
        token = self._login(c, "dereg@test.local", USER_PASS)
        r = c.post("/api/me/delete", json={"password": USER_PASS}, headers=self._csrf(token))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertIsNone(db.find_user("dereg@test.local"))
        self.assertIsNotNone(db.find_user_any("dereg@test.local"))
        self.assertEqual(db.load_accounts(), [])

    def test_self_delete_builtin_admin_forbidden(self):
        c = self.webapp.create_app().test_client()
        token = self._login(c, "admin", ADMIN_PASS)
        r = c.post("/api/me/delete", json={"password": ADMIN_PASS}, headers=self._csrf(token))
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))

    def test_log_date_path_traversal_rejected(self):
        c = self.webapp.create_app().test_client()
        token = self._login(c, "admin", ADMIN_PASS)
        r = c.get("/api/logs?date=../../etc/passwd", headers=self._csrf(token))
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
