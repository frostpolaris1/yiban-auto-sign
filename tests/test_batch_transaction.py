# -*- coding: utf-8 -*-
"""Phase 1：批量操作事务化测试。

覆盖：
- db 批量账号操作在异常时整体回滚；
- db 批量用户操作在异常时整体回滚；
- API 批量通过时无效项软跳过；
- API 同一批量恢复同一用户多个已删除账号被拦截；
- API 批量操作数据库异常时返回 500 且数据不变。
"""
import contextlib
import importlib.util
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
ADMIN_PASS = "TestPass1234!"
USER_PASS = "secret1"


class BatchTransactionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-batch-tx-")
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

    def _admin_client(self):
        c = self.webapp.create_app().test_client()
        self._login(c, "admin", ADMIN_PASS)
        return c

    def _add_account(self, owner, phone, status="active"):
        return db.add_account({
            "name": "测试账号",
            "phone": phone,
            "password": "p1",
            "phone_model": "",
            "phone_code": "",
            "owner": owner,
            "status": status,
            "reject_reason": "",
        })

    def _account_index_by_phone(self, data, phone_masked):
        return next(a["index"] for a in data["accounts"] if a["phone"] == phone_masked)

    # ---- 1. db 批量账号操作异常整体回滚 ----
    def test_db_batch_account_ops_rollback_on_unknown_op(self):
        id1 = self._add_account("user1@test.local", "13800138001")
        id2 = self._add_account("user2@test.local", "13900139002")
        with self.assertRaises(ValueError):
            db.batch_account_ops([
                ("update_status", id1, "active", ""),
                ("bad_op", id2),
            ])
        accounts = db.load_accounts()
        by_id = {a["id"]: a for a in accounts}
        self.assertEqual(by_id[id1]["status"], "active")
        self.assertEqual(by_id[id2]["status"], "active")

    # ---- 2. db 批量用户操作异常整体回滚 ----
    def test_db_batch_user_ops_rollback_on_unknown_op(self):
        with self.assertRaises(ValueError):
            db.batch_user_ops([
                ("update_user", "user1@test.local", {"role": "admin"}),
                ("bad_op", "user2@test.local"),
            ])
        users = {u["email"]: u for u in db.load_users()}
        self.assertEqual(users["user1@test.local"]["role"], "user")
        self.assertEqual(users["user2@test.local"]["role"], "user")

    # ---- 3. API 批量通过：无效项软跳过 ----
    def test_api_batch_approve_soft_skip_mixed(self):
        id1 = self._add_account("user1@test.local", "13800138003", status="pending")
        self._add_account("user2@test.local", "13900139004", status="pending")
        # 把第一个直接置为 active，模拟“已通过”的无效项
        db.update_account_status(id1, "active", reject_reason="")
        c = self._admin_client()
        token = self._login(c, "admin", ADMIN_PASS)
        data = c.get("/api/accounts").get_json()
        ids = [self._account_index_by_phone(data, "138****8003"),
               self._account_index_by_phone(data, "139****9004")]
        r = c.post("/api/accounts/batch", json={"action": "approve", "ids": ids},
                   headers=self._csrf(token))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertIn("已通过 1 个账号", r.get_json()["msg"])
        accounts = db.load_accounts()
        by_phone = {a["phone"]: a for a in accounts}
        self.assertEqual(by_phone["13800138003"]["status"], "active")
        self.assertEqual(by_phone["13900139004"]["status"], "active")

    # ---- 4. API 同一批量恢复同一用户多个已删除账号被拦截 ----
    def test_api_batch_restore_duplicate_same_owner_blocked(self):
        id1 = self._add_account("user1@test.local", "13800138005")
        id2 = self._add_account("user1@test.local", "13900139006")
        db.set_account_deleted(id1, 1, "2026-08-16T00:00:00")
        db.set_account_deleted(id2, 1, "2026-08-16T00:00:00")
        c = self._admin_client()
        token = self._login(c, "admin", ADMIN_PASS)
        data = c.get("/api/accounts").get_json()
        ids = [self._account_index_by_phone(data, "138****8005"),
               self._account_index_by_phone(data, "139****9006")]
        r = c.post("/api/accounts/batch", json={"action": "restore", "ids": ids},
                   headers=self._csrf(token))
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        accounts = db.load_accounts()
        self.assertTrue(all(a["deleted"] for a in accounts if a["id"] in (id1, id2)))

    # ---- 5. API 批量数据库异常返回 500 且数据不变 ----
    def test_api_batch_accounts_db_error_returns_500(self):
        self._add_account("user1@test.local", "13800138007", status="pending")
        c = self._admin_client()
        token = self._login(c, "admin", ADMIN_PASS)
        data = c.get("/api/accounts").get_json()
        idx = self._account_index_by_phone(data, "138****8007")
        with mock.patch("db.batch_account_ops", side_effect=RuntimeError("boom")):
            r = c.post("/api/accounts/batch", json={"action": "approve", "ids": [idx]},
                       headers=self._csrf(token))
        self.assertEqual(r.status_code, 500, r.get_data(as_text=True))
        self.assertIn("已全部回滚", r.get_json()["error"])
        accounts = db.load_accounts()
        self.assertEqual(accounts[0]["status"], "pending")


if __name__ == "__main__":
    unittest.main()
