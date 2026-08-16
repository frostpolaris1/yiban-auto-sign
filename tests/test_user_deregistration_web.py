# -*- coding: utf-8 -*-
"""用户自助注销 Web/API 层测试（数据库 v5 软删除对接）。

覆盖 docs/design/plan-frontend-user-deregistration.md 第 3 章 API 契约：
- 登录要求 401 / CSRF 缺失 403
- 密码确认：错误 400、连续失败达阈值锁定 429
- 防批量冷却：用户维度 60s 1 次、IP 维度 60s 5 次 → 429（不暴露秒数）
- 管理员保护：内置管理员 400、最后一个注册管理员 400
- 成功路径：软删除标记 + 账号清除 + 会话失效 + 审计留痕 + 邮箱可重新注册

全程 mock / 纯本地（Flask test client），无任何网络请求。
用法（项目根目录）：
    py -m pytest tests/test_user_deregistration_web.py -v
"""
import contextlib
import hashlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "scripts"))
sys.path.insert(0, os.path.join(BASE, "web"))

TEST_KEY = "a" * 64
ADMIN_PASS = "TestPass1234!"
USER_PASS = "secret1"


class UserDeregistrationWebTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-del-")
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
        db.create_user("admin@test.local", self.webapp.generate_password_hash(ADMIN_PASS), role="admin")
        db.create_user("user1@test.local", self.webapp.generate_password_hash(USER_PASS))
        db.add_account({"name": "U1", "phone": "13800138001", "password": "p1",
                        "status": "active", "owner": "user1@test.local"})

    def _login(self, c, username, password):
        r = c.post("/api/login", json={"username": username, "password": password})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return c.get("/api/me").get_json()["csrf_token"]

    def _delete(self, c, token, password="x"):
        return c.post("/api/me/delete", json={"password": password},
                      headers={"X-CSRF-Token": token})

    def _audit_actions(self):
        import sqlite3
        conn = sqlite3.connect(self.db_file)
        try:
            return [r[0] for r in conn.execute(
                "SELECT action FROM audit_logs ORDER BY id")]
        finally:
            conn.close()

    # ---- 基础防护 ----
    def test_requires_login(self):
        c = self.webapp.create_app().test_client()
        r = self._delete(c, "t")
        self.assertEqual(r.status_code, 401)

    def test_csrf_required(self):
        c = self.webapp.create_app().test_client()
        self._login(c, "user1@test.local", USER_PASS)
        r = c.post("/api/me/delete", json={"password": USER_PASS})
        self.assertEqual(r.status_code, 403, "缺少 X-CSRF-Token 应被全局 CSRF 校验拦截")

    def test_empty_password(self):
        c = self.webapp.create_app().test_client()
        token = self._login(c, "user1@test.local", USER_PASS)
        r = self._delete(c, token, password="")
        self.assertEqual(r.status_code, 400)
        self.assertIn("密码", r.get_json()["error"])

    def test_wrong_password(self):
        c = self.webapp.create_app().test_client()
        token = self._login(c, "user1@test.local", USER_PASS)
        r = self._delete(c, token, password="wrong-pass")
        self.assertEqual(r.status_code, 400)
        self.assertIn("密码不正确", r.get_json()["error"])
        u = db.find_user("user1@test.local")
        self.assertIsNotNone(u, "密码错误不应注销用户")
        self.assertEqual(u["deleted"], 0)

    def test_failed_password_lockout(self):
        c = self.webapp.create_app().test_client()
        token = self._login(c, "user1@test.local", USER_PASS)
        max_fails = self.webapp.LOGIN_MAX_FAILS
        for i in range(max_fails - 1):
            r = self._delete(c, token, password="bad")
            self.assertEqual(r.status_code, 400, f"第 {i + 1} 次错误应为 400")
        r = self._delete(c, token, password="bad")  # 达阈值 → 锁定 429
        self.assertEqual(r.status_code, 429)
        self.assertNotIn(str(self.webapp.LOGIN_LOCK_SECONDS), r.get_json()["error"],
                         "不应暴露锁定时长")

    # ---- 成功路径 ----
    def test_delete_success(self):
        c = self.webapp.create_app().test_client()
        token = self._login(c, "user1@test.local", USER_PASS)
        r = self._delete(c, token, password=USER_PASS)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertIn("3 天内可撤销", r.get_json()["msg"])
        # 软删除标记 + 账号清除
        u = db.find_user_any("user1@test.local")
        self.assertEqual(u["deleted"], 1)
        self.assertTrue(u["deleted_at"])
        accs = db.load_accounts_raw()
        self.assertFalse([a for a in accs if a["owner"] == "user1@test.local"],
                         "注销应清除其易班账号")
        # 会话已清（注销即登出：再访问 /api/me 未登录 → 401）
        me = c.get("/api/me")
        self.assertEqual(me.status_code, 401)
        # 审计留痕
        actions = self._audit_actions()
        self.assertIn("user_self_delete_request", actions)
        self.assertIn("user_self_delete_confirm", actions)
        # 邮箱可重新注册（软删除后 find_user 查无此人）
        r = c.post("/api/register", json={"email": "user1@test.local", "password": "newpass1234"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

    # ---- 管理员视图（v0.20.1：注销不发通知，改主动查看）----
    def test_admin_sees_deleted_users(self):
        c = self.webapp.create_app().test_client()
        token = self._login(c, "user1@test.local", USER_PASS)
        self._delete(c, token, password=USER_PASS)
        # 管理员查看已注销列表
        c2 = self.webapp.create_app().test_client()
        self._login(c2, "admin", ADMIN_PASS)
        r = c2.get("/api/users/deleted")
        self.assertEqual(r.status_code, 200)
        items = r.get_json()["items"]
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["email"], "user1@test.local")
        self.assertEqual(item["status"], "cooling")
        self.assertGreaterEqual(item["remaining_days"], 2, "刚注销应剩约 3 天（整天向下取整 ≥2）")
        self.assertTrue(item["deleted_at"])

    def test_deleted_users_expired_shows_purge_pending(self):
        # 构造已过宽限期的注销用户 → purge_pending + remaining_days=0
        db.create_user("expired@test.local", "hash")
        db.soft_delete_user_with_accounts("expired@test.local")
        conn = db.get_conn()
        old = (datetime.now() - timedelta(days=4)).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute("UPDATE users SET deleted_at=? WHERE email=?", (old, "expired@test.local"))
        conn.commit()
        c = self.webapp.create_app().test_client()
        self._login(c, "admin", ADMIN_PASS)
        r = c.get("/api/users/deleted")
        items = r.get_json()["items"]
        item = next(i for i in items if i["email"] == "expired@test.local")
        self.assertEqual(item["status"], "purge_pending")
        self.assertEqual(item["remaining_days"], 0)

    def test_deleted_users_requires_admin(self):
        c = self.webapp.create_app().test_client()
        self._login(c, "user1@test.local", USER_PASS)
        r = c.get("/api/users/deleted")
        self.assertEqual(r.status_code, 403, "普通用户无权查看已注销列表")

    def test_deleted_users_empty(self):
        c = self.webapp.create_app().test_client()
        self._login(c, "admin", ADMIN_PASS)
        r = c.get("/api/users/deleted")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["items"], [])

    # ---- 管理员保护 ----
    def test_builtin_admin_cannot_delete(self):
        c = self.webapp.create_app().test_client()
        token = self._login(c, "admin", ADMIN_PASS)
        r = self._delete(c, token, password=ADMIN_PASS)
        self.assertEqual(r.status_code, 400)
        self.assertIn("不可注销", r.get_json()["error"])

    def test_last_registered_admin_cannot_delete(self):
        # user1 提升为管理员，且原测试管理员软删 → user1 是最后一个活跃注册管理员
        db.update_user("user1@test.local", {"role": "admin"})
        db.soft_delete_user_with_accounts("admin@test.local")
        c = self.webapp.create_app().test_client()
        token = self._login(c, "user1@test.local", USER_PASS)
        r = self._delete(c, token, password=USER_PASS)
        self.assertEqual(r.status_code, 400)
        self.assertIn("最后一个管理员", r.get_json()["error"])
        self.assertEqual(db.find_user("user1@test.local")["deleted"], 0)

    # ---- 防批量冷却 ----
    def test_cooldown_per_user(self):
        db.record_user_delete_request("user1@test.local", ip_hash="x")
        c = self.webapp.create_app().test_client()
        token = self._login(c, "user1@test.local", USER_PASS)
        r = self._delete(c, token, password=USER_PASS)
        self.assertEqual(r.status_code, 429)
        self.assertNotIn("60", r.get_json()["error"], "不应暴露冷却秒数")

    def test_cooldown_per_ip(self):
        ip_hash = hashlib.sha256(b"127.0.0.1").hexdigest()
        for i in range(5):
            db.record_user_delete_request(f"other{i}@test.local", ip_hash=ip_hash)
        c = self.webapp.create_app().test_client()
        token = self._login(c, "user1@test.local", USER_PASS)
        r = self._delete(c, token, password=USER_PASS)
        self.assertEqual(r.status_code, 429)


if __name__ == "__main__":
    unittest.main(verbosity=2)
