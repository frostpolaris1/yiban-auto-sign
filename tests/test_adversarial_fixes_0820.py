# -*- coding: utf-8 -*-
"""2026-08-20 对抗性审查修复回归测试。

覆盖：
1. 空凭据/部分配置管理员登录拒绝（P1：verify_admin 空配置直通）
2. idx 寻址防错位校验：单账号 mutation 携带 phone 不匹配 → 409（P1：列表漂移错位操作）
3. 批量接口 phones 对齐校验 + bool 索引混淆修复
4. /api/my-accounts 日志出站脱敏（P3：与 /api/my-logs 口径统一）

全程 mock / 纯本地（Flask test client），无任何网络请求。
用法（项目根目录）：
    python -m pytest tests/test_adversarial_fixes_0820.py -v
"""
import contextlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "scripts"))
sys.path.insert(0, os.path.join(BASE, "web"))

TEST_KEY = "a" * 64
ADMIN_PASS = "TestPass1234!"
USER_PASS = "UserPass123!"


class AdversarialFixes0820Test(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-adv-fix-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.accounts_file = os.path.join(cls.tmp, "accounts.json")
        cls.users_file = os.path.join(cls.tmp, "users.json")
        cls.log_dir = os.path.join(cls.tmp, "logs")
        os.makedirs(cls.log_dir, exist_ok=True)
        cls._write_env(admin_user="admin", admin_pass=ADMIN_PASS)
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

    # ---- 环境辅助 ----
    @classmethod
    def _write_env(cls, admin_user=None, admin_pass=None):
        """按需写 .env：admin_user/admin_pass 任一为 None 即不写该行（构造部分配置场景）。"""
        lines = [f"YIBAN_ACCOUNTS_KEY={TEST_KEY}"]
        if admin_user is not None:
            lines.append(f"YIBAN_ADMIN_USER={admin_user}")
        if admin_pass is not None:
            lines.append(f"YIBAN_ADMIN_PASSWORD={admin_pass}")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

    def _reset_db(self):
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

    def _client(self):
        return self.webapp.create_app().test_client()

    def _login_admin(self, c):
        r = c.post("/api/login", json={"username": "admin", "password": ADMIN_PASS})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return c.get("/api/me").get_json()["csrf_token"]

    def _add_account(self, phone, name="A"):
        db.add_account({"name": name, "phone": phone, "password": "p1", "status": "active"})

    # ---- 1. 空凭据 / 部分配置管理员登录（P1 修复）----
    def test_fully_unconfigured_admin_empty_creds_rejected(self):
        """完全未配置管理员：空用户名+空口令不得通过 verify_admin。"""
        self._reset_db()
        self._write_env(admin_user=None, admin_pass=None)
        try:
            c = self._client()
            r = c.post("/api/login", json={"username": "", "password": ""})
            self.assertEqual(r.status_code, 401, r.get_data(as_text=True))
            r = c.post("/api/login", json={"username": "admin", "password": ""})
            self.assertEqual(r.status_code, 401, r.get_data(as_text=True))
        finally:
            self._write_env(admin_user="admin", admin_pass=ADMIN_PASS)

    def test_partial_config_admin_empty_password_rejected(self):
        """只配了用户名没配密码：空口令不得登录（原实现 compare_digest(b'',b'') 直通）。"""
        self._reset_db()
        self._write_env(admin_user="admin", admin_pass=None)
        try:
            c = self._client()
            r = c.post("/api/login", json={"username": "admin", "password": ""})
            self.assertEqual(r.status_code, 401, r.get_data(as_text=True))
            self.assertNotIn("role", r.get_json())
        finally:
            self._write_env(admin_user="admin", admin_pass=ADMIN_PASS)

    def test_configured_admin_login_unaffected(self):
        """正常完整配置：正确口令仍可登录（修复不改变正常路径）。"""
        self._reset_db()
        c = self._client()
        token = self._login_admin(c)
        self.assertTrue(token)

    # ---- 2. idx 防错位校验（P1 修复）----
    def test_delete_with_mismatched_phone_rejected_409(self):
        self._reset_db()
        self._add_account("13800138000")
        self._add_account("13900139000")
        c = self._client()
        token = self._login_admin(c)
        # phone 与服务端 idx 解析结果不一致 → 409，且未删除任何账号
        r = c.delete("/api/accounts/0", json={"phone": "13900139000"},
                     headers={"X-CSRF-Token": token})
        self.assertEqual(r.status_code, 409, r.get_data(as_text=True))
        self.assertEqual(len(db.load_accounts()), 2, "409 后不应有账号被删除")
        # phone 匹配 → 正常软删除
        r = c.delete("/api/accounts/0", json={"phone": "13800138000"},
                     headers={"X-CSRF-Token": token})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

    def test_delete_with_masked_phone_from_list_works(self):
        """模拟真实浏览器路径：/api/accounts 出站为脱敏号，前端原样回传——
        守卫必须双侧 _mask_phone 归一，否则管理端六个操作恒 409（回归审查发现）。"""
        self._reset_db()
        self._add_account("13800138000")
        self._add_account("13900139000")
        c = self._client()
        token = self._login_admin(c)
        listing = c.get("/api/accounts").get_json()["accounts"]
        masked = listing[0]["phone"]
        self.assertIn("*", masked, "列表出站应为脱敏号（前提校验）")
        # 用脱敏号（浏览器真实行为）删除 → 应成功而非 409
        r = c.delete("/api/accounts/0", json={"phone": masked},
                     headers={"X-CSRF-Token": token})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        # 用 A 的脱敏号去删 idx1（B 账号）→ 错配应被拦
        r = c.delete("/api/accounts/1", json={"phone": masked},
                     headers={"X-CSRF-Token": token})
        self.assertEqual(r.status_code, 409, "伪造/错配的脱敏号应 409")
        self.assertFalse(db.load_accounts()[1].get("deleted"), "409 后 B 不应被删除")

    def test_batch_with_masked_phones_works(self):
        """批量接口同样按脱敏号归一比对（浏览器路径）。"""
        self._reset_db()
        self._add_account("13800138000")
        self._add_account("13900139000")
        c = self._client()
        token = self._login_admin(c)
        listing = c.get("/api/accounts").get_json()["accounts"]
        phones = [a["phone"] for a in listing]
        r = c.post("/api/accounts/batch",
                   json={"action": "delete", "ids": [0, 1], "phones": phones},
                   headers={"X-CSRF-Token": token})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(all(a.get("deleted") for a in db.load_accounts()))

    def test_purge_with_mismatched_phone_rejected_409(self):
        self._reset_db()
        self._add_account("13800138000")
        id2 = self._add_account("13900139000")
        db.set_account_deleted(id2, 1, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        c = self._client()
        token = self._login_admin(c)
        # idx1 是已软删的 139 账号；携带错误 phone 应 409（防漂移后误删他人）
        # 批次14 P1-2 后 purge 先过二次鉴权，故补 confirm_password——本用例要钉的
        # 仍是"口令正确时的防错位 409"，断言与尝试次序均未放宽
        r = c.post("/api/accounts/1/purge",
                   json={"phone": "13800138000", "confirm_password": ADMIN_PASS},
                   headers={"X-CSRF-Token": token})
        self.assertEqual(r.status_code, 409, r.get_data(as_text=True))
        self.assertEqual(len(db.load_accounts()), 2, "409 后不应有账号被物理删除")

    def test_review_with_mismatched_phone_rejected_409(self):
        self._reset_db()
        db.add_account({"name": "P", "phone": "13800138000", "password": "p1",
                        "status": "pending", "owner": "u1@test.local"})
        db.add_account({"name": "Q", "phone": "13900139000", "password": "p2",
                        "status": "pending", "owner": "u2@test.local"})
        c = self._client()
        token = self._login_admin(c)
        r = c.post("/api/accounts/0/review",
                   json={"action": "approve", "phone": "13900139000"},
                   headers={"X-CSRF-Token": token})
        self.assertEqual(r.status_code, 409, r.get_data(as_text=True))
        accs = {a["phone"]: a["status"] for a in db.load_accounts()}
        self.assertEqual(accs["13800138000"], "pending", "409 后审核状态不应变化")

    def test_batch_phones_mismatch_rejected_409(self):
        self._reset_db()
        self._add_account("13800138000")
        self._add_account("13900139000")
        c = self._client()
        token = self._login_admin(c)
        r = c.post("/api/accounts/batch",
                   json={"action": "delete", "ids": [0], "phones": ["13900139000"]},
                   headers={"X-CSRF-Token": token})
        self.assertEqual(r.status_code, 409, r.get_data(as_text=True))
        self.assertTrue(all(not a.get("deleted") for a in db.load_accounts()))

    def test_batch_bool_id_not_treated_as_index(self):
        """JSON true 此前因 isinstance(True, int) 被当作索引 1（bool 混淆修复）。"""
        self._reset_db()
        self._add_account("13800138000")
        self._add_account("13900139000")
        c = self._client()
        token = self._login_admin(c)
        r = c.post("/api/accounts/batch",
                   json={"action": "delete", "ids": [True]},
                   headers={"X-CSRF-Token": token})
        self.assertEqual(r.status_code, 404, r.get_data(as_text=True))
        self.assertTrue(all(not a.get("deleted") for a in db.load_accounts()),
                        "true 不应再作用于索引 1")

    # ---- 3. my-accounts 日志出站脱敏（P3 修复）----
    def test_my_accounts_logs_masked(self):
        self._reset_db()
        h = self.webapp.generate_password_hash
        db.create_user("u1@test.local", h(USER_PASS), role="user")
        db.add_account({"name": "Mine", "phone": "13800138000", "password": "p1",
                        "status": "active", "owner": "u1@test.local"})
        # 构造今日日志行（含完整手机号）
        today = datetime.now().strftime("%Y-%m-%d")
        log_path = os.path.join(self.log_dir, f"sign-{today}.log")
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(f"[{today} 07:10:00] [INFO] yiban: [13800138000] ✅ 签到成功\n")
        self.webapp.LOG_FILE = os.path.join(self.log_dir, "sign.log")
        try:
            c = self._client()
            r = c.post("/api/login", json={"username": "u1@test.local", "password": USER_PASS})
            self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
            r = c.get("/api/my-accounts")
            self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
            accounts = r.get_json()["accounts"]
            self.assertEqual(len(accounts), 1)
            logs = accounts[0].get("logs", [])
            self.assertTrue(logs, "应能读到今日日志")
            joined = "\n".join(logs)
            self.assertNotIn("13800138000", joined, "出站日志不应含完整手机号")
            self.assertIn("138****8000", joined)
        finally:
            self.webapp.LOG_FILE = self.webapp.LOG_DEFAULT


if __name__ == "__main__":
    unittest.main()
