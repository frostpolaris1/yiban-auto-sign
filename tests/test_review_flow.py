# -*- coding: utf-8 -*-
"""账号审核流转 API 测试（锁定 2026-08-16 审查轮 ACCOUNT_STATUS_* 改名行为）。

覆盖：普通用户提交（pending）→ 管理员通过（active）/ 拒绝（rejected+理由）→
被拒用户编辑重新提交（回 pending）→ 批量通过。全部断言走 API 层真实返回。

用法（项目根目录）：
    py -m pytest tests/test_review_flow.py -v
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


class ReviewFlowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-review-")
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
        db.create_user("user1@test.local", self.webapp.generate_password_hash(USER_PASS))
        db.create_user("user2@test.local", self.webapp.generate_password_hash(USER_PASS))

    # ---- 工具 ----
    def _login(self, c, username, password):
        r = c.post("/api/login", json={"username": username, "password": password})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return c.get("/api/me").get_json()["csrf_token"]

    def _csrf(self, token):
        return {"X-CSRF-Token": token}

    def _submit(self, c, token, phone):
        """用户提交账号，返回响应。"""
        return c.post("/api/my-accounts", json={
            "name": "测试账号", "phone": phone, "password": "p1",
        }, headers=self._csrf(token))

    def _admin_accounts(self):
        c = self.webapp.create_app().test_client()
        self._login(c, "admin", ADMIN_PASS)
        data = c.get("/api/accounts").get_json()
        return c, data

    # ---- 1. 提交 → 待审核 → 管理员通过 ----
    def test_submit_approve_flow(self):
        c = self.webapp.create_app().test_client()
        token = self._login(c, "user1@test.local", USER_PASS)
        r = self._submit(c, token, "13800138001")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        # 管理员视角：pending
        _c, data = self._admin_accounts()
        acc = next(a for a in data["accounts"] if a["phone"] == "138****8001")
        self.assertEqual(acc["status"], "pending")
        idx = acc["index"]
        # 通过
        r = _c.post(f"/api/accounts/{idx}/review", json={"action": "approve"},
                    headers=self._csrf(self._login(_c, "admin", ADMIN_PASS)))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        _c2, data2 = self._admin_accounts()
        acc2 = next(a for a in data2["accounts"] if a["phone"] == "138****8001")
        self.assertEqual(acc2["status"], "active")
        # 重复通过 → 400（无需审核）
        r = _c2.post(f"/api/accounts/{idx}/review", json={"action": "approve"},
                     headers=self._csrf(self._login(_c2, "admin", ADMIN_PASS)))
        self.assertEqual(r.status_code, 400)

    # ---- 2. 提交 → 拒绝（附理由）→ 用户可见理由 ----
    def test_submit_reject_with_reason(self):
        c = self.webapp.create_app().test_client()
        token = self._login(c, "user1@test.local", USER_PASS)
        self._submit(c, token, "13800138002")
        _c, data = self._admin_accounts()
        acc = next(a for a in data["accounts"] if a["phone"] == "138****8002")
        r = _c.post(f"/api/accounts/{acc['index']}/review",
                    json={"action": "reject", "reason": "设备信息不符"},
                    headers=self._csrf(self._login(_c, "admin", ADMIN_PASS)))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        # 管理员视角：rejected + 理由
        _c2, data2 = self._admin_accounts()
        acc2 = next(a for a in data2["accounts"] if a["phone"] == "138****8002")
        self.assertEqual(acc2["status"], "rejected")
        self.assertEqual(acc2["reject_reason"], "设备信息不符")
        # 用户视角：my-accounts 也能看到拒绝理由
        c2 = self.webapp.create_app().test_client()
        self._login(c2, "user1@test.local", USER_PASS)
        mine = c2.get("/api/my-accounts").get_json()["accounts"]
        self.assertEqual(mine[0]["status"], "rejected")
        self.assertEqual(mine[0]["reject_reason"], "设备信息不符")

    # ---- 3. 被拒用户编辑 → 重新提交（回 pending，清除理由） ----
    def test_rejected_edit_resubmits_pending(self):
        c = self.webapp.create_app().test_client()
        token = self._login(c, "user1@test.local", USER_PASS)
        self._submit(c, token, "13800138003")
        _c, data = self._admin_accounts()
        acc = next(a for a in data["accounts"] if a["phone"] == "138****8003")
        r = _c.post(f"/api/accounts/{acc['index']}/review",
                    json={"action": "reject", "reason": "资料不全"},
                    headers=self._csrf(self._login(_c, "admin", ADMIN_PASS)))
        self.assertEqual(r.status_code, 200)
        # 用户编辑（改密码）→ 自动回 pending
        c2 = self.webapp.create_app().test_client()
        token2 = self._login(c2, "user1@test.local", USER_PASS)
        mine = c2.get("/api/my-accounts").get_json()["accounts"]
        r = c2.put(f"/api/my-accounts/{mine[0]['index']}",
                   json={"name": "测试账号", "phone": mine[0]["phone"], "password": "newpass123"},
                   headers=self._csrf(token2))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        _c3, data3 = self._admin_accounts()
        acc3 = next(a for a in data3["accounts"] if a["phone"] == "138****8003")
        self.assertEqual(acc3["status"], "pending")
        self.assertEqual(acc3["reject_reason"], "")

    # ---- 4. 批量通过 ----
    def test_batch_approve(self):
        for email, phone in (("user1@test.local", "13800138004"), ("user2@test.local", "13900139005")):
            c = self.webapp.create_app().test_client()
            token = self._login(c, email, USER_PASS)
            r = self._submit(c, token, phone)
            self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        _c, data = self._admin_accounts()
        ids = [a["index"] for a in data["accounts"]]
        self.assertEqual(len(ids), 2)
        r = _c.post("/api/accounts/batch", json={"action": "approve", "ids": ids},
                    headers=self._csrf(self._login(_c, "admin", ADMIN_PASS)))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        _c2, data2 = self._admin_accounts()
        self.assertTrue(all(a["status"] == "active" for a in data2["accounts"]))
        # 再次批量通过 → 0 个（全部已 active）
        r = _c2.post("/api/accounts/batch", json={"action": "approve", "ids": ids},
                     headers=self._csrf(self._login(_c2, "admin", ADMIN_PASS)))
        self.assertIn("0 个", r.get_json()["msg"])

    # ---- 5. 软删除账号不可审核通过 ----
    def test_deleted_account_cannot_approve(self):
        c = self.webapp.create_app().test_client()
        token = self._login(c, "user1@test.local", USER_PASS)
        self._submit(c, token, "13800138006")
        _c, data = self._admin_accounts()
        acc = next(a for a in data["accounts"] if a["phone"] == "138****8006")
        # 管理员删除（软删除）
        r = _c.delete(f"/api/accounts/{acc['index']}",
                      headers=self._csrf(self._login(_c, "admin", ADMIN_PASS)))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        # 审核通过 → 400（deleted 账号不参与审核流转）
        r = _c.post(f"/api/accounts/{acc['index']}/review", json={"action": "approve"},
                    headers=self._csrf(self._login(_c, "admin", ADMIN_PASS)))
        self.assertEqual(r.status_code, 400)

    # ---- 6. 设置保存审计（2026-08-16，P8：设置变更留痕）----
    def test_settings_save_writes_audit(self):
        c = self.webapp.create_app().test_client()
        token = self._login(c, "admin", ADMIN_PASS)
        r = c.post("/api/settings", json={"start_delay_max": 30, "gap_max": 5},
                   headers=self._csrf(token))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        # 审计记录应包含 settings_save 动作
        import sqlite3
        conn = sqlite3.connect(self.db_file)
        row = conn.execute(
            "SELECT action, detail FROM audit_logs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        conn.close()
        self.assertIsNotNone(row, "设置保存应写审计")
        self.assertEqual(row[0], "settings_save")
        self.assertIn("启动延迟=30", row[1])


if __name__ == "__main__":
    unittest.main()
