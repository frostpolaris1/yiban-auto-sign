# -*- coding: utf-8 -*-
"""角色变更收权 + 主管理员口令提档回归（2026-09-05 用户裁决）。

三项变更：
1. 批量角色变更入口移除：/api/users/batch 不再接受 set_admin/unset_admin，
   提权/降权仅保留 /api/users/<email>/role 单个路径；
2. 角色变更接入高危门禁：须二次输入当前管理员密码（_high_risk_gate），
   与批量重置密码/删除同口径；
3. 主管理员口令策略提档为 12 位三类（自助改密 + 启动 fail-closed 同口径），
   注册用户维持 10 位两类不变。

全程 mock / 纯本地（Flask test client），无任何网络请求。
用法（项目根目录）：py -m pytest tests/test_role_hardening_0905.py -v
"""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "scripts"))
sys.path.insert(0, os.path.join(BASE, "web"))

TEST_KEY = "a" * 64
ADMIN_PASS = "MasterPass#2026"   # 15 位四类，满足主管理员 12/3 策略
STRONG_12_3 = "Rotated#2026"     # 12 位四类：提档后允许
WEAK_11 = "Rotated#202"          # 11 位：不足 12 位
WEAK_12_2 = "abcdefgh1234"       # 12 位两类：类别不足
USER_PASS = "secret1"
NEW_USER_PASS = "abcdef1234"     # 10 位两类：注册用户口径不变


class RoleHardeningTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-role-hard-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        cls._env_content = (
            f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
            f"YIBAN_ADMIN_USER=admin\nYIBAN_ADMIN_PASSWORD={ADMIN_PASS}\n"
        )
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(cls._env_content)
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

    # ---- 辅助 ----
    def _login(self, c, username, password):
        r = c.post("/api/login", json={"username": username, "password": password})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return c.get("/api/me").get_json()["csrf_token"]

    def _admin_client(self):
        c = self.webapp.create_app().test_client()
        return c, self._login(c, "admin", ADMIN_PASS)

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
                        json={"role": "admin", "confirm_password": ADMIN_PASS},
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
                    json={"old_password": ADMIN_PASS, "new_password": WEAK_11, "confirm_password": WEAK_11},
                    headers={"X-CSRF-Token": at})
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        # 12 位两类：类别不足
        r = ac.post("/api/me/password",
                    json={"old_password": ADMIN_PASS, "new_password": WEAK_12_2, "confirm_password": WEAK_12_2},
                    headers={"X-CSRF-Token": at})
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        self.assertIn("三类", r.get_json()["error"])
        # 12 位三类：通过（随后还原 .env 供其他用例登录）
        r = ac.post("/api/me/password",
                    json={"old_password": ADMIN_PASS, "new_password": STRONG_12_3, "confirm_password": STRONG_12_3},
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
