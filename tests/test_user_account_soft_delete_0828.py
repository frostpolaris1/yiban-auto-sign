# -*- coding: utf-8 -*-
"""用户删除账号软删化（2026-08-28 批次7 用户裁决）回归测试。

语义变更：用户删除自己的易班账号由「立即物理清除」改为「软删除 + 7 天宽限内可撤销」，
与管理员侧两档语义对齐；deleted_by（v10 迁移）留痕删除来源——
仅本人自删行（deleted_by=本人邮箱）可自行撤销，管理员删除/系统连带行用户不可恢复。

覆盖：
1. 用户删除 → 软删（行保留、deleted_by=本人、凭据/事件不被清除）→ 撤销恢复（状态保留）
2. 对已删除行重复删除 → 400
3. 管理员删除的行：用户撤销 → 403；管理员恢复 → 200 且 deleted_by 清空
4. 名下已有其他生效账号时撤销 → 400（每人限 1）
5. 宽限期内重提同一手机号 → 400 带撤销提示
6. 超期软删行仍被每日清理物理清除（purge_expired_deleted_accounts 不受影响）
7. 批量删除（batch_account_ops set_deleted）留痕 'admin'

用法（项目根目录）：
    py -m pytest tests/test_user_account_soft_delete_0828.py -v
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

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "scripts"))
sys.path.insert(0, os.path.join(BASE, "web"))

TEST_KEY = "a" * 64
ADMIN_PASS = "TestPass1234!"
USER_PASS = "secret1"
EMAIL = "user1@test.local"
PHONE = "13800138001"


class UserAccountSoftDeleteTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-softdel-")
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
        db.create_user(EMAIL, self.webapp.generate_password_hash(USER_PASS))

    # ---- 工具 ----
    def _login(self, c, username, password):
        r = c.post("/api/login", json={"username": username, "password": password})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return c.get("/api/me").get_json()["csrf_token"]

    def _csrf(self, token):
        return {"X-CSRF-Token": token}

    def _user_client(self):
        # 登录限速为进程内存态（跨测试共享）：类级缓存客户端，避免反复登录触发 429。
        # 每个测试的 setUp 会重建同邮箱/同密码用户，旧会话 cookie 依然有效。
        if not hasattr(self, "_uc"):
            c = self.webapp.create_app().test_client()
            self._uc, self._utoken = c, self._login(c, EMAIL, USER_PASS)
        return self._uc, self._utoken

    def _admin_client(self):
        if not hasattr(self, "_ac"):
            c = self.webapp.create_app().test_client()
            self._ac, self._atoken = c, self._login(c, "admin", ADMIN_PASS)
        return self._ac, self._atoken

    def _submit_and_approve(self, uclient, utoken, phone=PHONE):
        """用户提交 + 管理员通过，返回生效账号。"""
        r = uclient.post("/api/my-accounts", json={
            "name": "测试账号", "phone": phone, "password": "p1",
        }, headers=self._csrf(utoken))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        ac, atoken = self._admin_client()
        accounts = db.load_accounts()
        idx = next(i for i, a in enumerate(accounts) if a["phone"] == phone)
        r = ac.post(f"/api/accounts/{idx}/review", json={"action": "approve"},
                    headers=self._csrf(atoken))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return next(a for a in db.load_accounts() if a["phone"] == phone)

    def _row(self, phone=PHONE):
        return next(a for a in db.load_accounts() if a["phone"] == phone)

    # ---- 1. 删除 → 软删 → 撤销恢复 ----
    def test_user_delete_soft_then_undo(self):
        c, token = self._user_client()
        acc = self._submit_and_approve(c, token)
        # 删除 → 软删：行保留，deleted_by=本人，状态字段原样保留
        r = c.delete("/api/my-accounts/0", headers=self._csrf(token))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertIn("撤销", r.get_json()["msg"])
        row = self._row()
        self.assertEqual(row["deleted"], 1)
        self.assertNotEqual(row["deleted_at"], "")
        self.assertEqual(row["deleted_by"], EMAIL)
        self.assertEqual(row["status"], "active")  # 状态保留，恢复后原样回来
        # 用户视图：deleted + deleted_by_me
        mine = c.get("/api/my-accounts").get_json()["accounts"]
        self.assertTrue(mine[0]["deleted"])
        self.assertTrue(mine[0]["deleted_by_me"])
        # 撤销恢复
        r = c.post("/api/my-accounts/0/restore", json={}, headers=self._csrf(token))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        row = self._row()
        self.assertEqual(row["deleted"], 0)
        self.assertEqual(row["deleted_at"], "")
        self.assertEqual(row["deleted_by"], "")  # 恢复即清空来源留痕
        self.assertEqual(row["status"], "active")

    # ---- 2. 已删除行重复删除 → 400 ----
    def test_delete_on_deleted_row_rejected(self):
        c, token = self._user_client()
        self._submit_and_approve(c, token)
        r = c.delete("/api/my-accounts/0", headers=self._csrf(token))
        self.assertEqual(r.status_code, 200)
        r = c.delete("/api/my-accounts/0", headers=self._csrf(token))
        self.assertEqual(r.status_code, 400)
        self.assertIn("待删除", r.get_json()["error"])

    # ---- 3. 管理员删除的行：用户不可撤销，管理员可恢复 ----
    def test_undo_rejected_for_admin_deleted(self):
        c, token = self._user_client()
        acc = self._submit_and_approve(c, token)
        ac, atoken = self._admin_client()
        idx = next(i for i, a in enumerate(db.load_accounts()) if a["phone"] == PHONE)
        r = ac.delete(f"/api/accounts/{idx}", headers=self._csrf(atoken))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        row = self._row()
        self.assertEqual(row["deleted_by"], "admin")
        # 用户视图可见但不可自行撤销
        mine = c.get("/api/my-accounts").get_json()["accounts"]
        self.assertTrue(mine[0]["deleted"])
        self.assertFalse(mine[0]["deleted_by_me"])
        r = c.post("/api/my-accounts/0/restore", json={}, headers=self._csrf(token))
        self.assertEqual(r.status_code, 403)
        # 管理员恢复不受影响，且恢复后清空 deleted_by
        idx = next(i for i, a in enumerate(db.load_accounts()) if a["phone"] == PHONE)
        r = ac.post(f"/api/accounts/{idx}/restore", json={}, headers=self._csrf(atoken))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        row = self._row()
        self.assertEqual(row["deleted"], 0)
        self.assertEqual(row["deleted_by"], "")

    # ---- 4. 名下已有其他生效账号时撤销 → 400（每人限 1） ----
    def test_undo_blocked_when_owner_has_other_live(self):
        c, token = self._user_client()
        self._submit_and_approve(c, token)
        r = c.delete("/api/my-accounts/0", headers=self._csrf(token))
        self.assertEqual(r.status_code, 200)
        # 宽限期内提交第二个账号（不同手机号）并通过审核 → 名下出现生效账号
        r = c.post("/api/my-accounts", json={
            "name": "第二个", "phone": "13800138002", "password": "p2",
        }, headers=self._csrf(token))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        ac, atoken = self._admin_client()
        accounts = db.load_accounts()
        idx = next(i for i, a in enumerate(accounts) if a["phone"] == "13800138002")
        r = ac.post(f"/api/accounts/{idx}/review", json={"action": "approve"},
                    headers=self._csrf(atoken))
        self.assertEqual(r.status_code, 200)
        # 撤销第一个（已软删）账号 → 每人限 1 拦截
        r = c.post("/api/my-accounts/0/restore", json={}, headers=self._csrf(token))
        self.assertEqual(r.status_code, 400)
        self.assertIn("每人限 1", r.get_json()["error"])

    # ---- 5. 宽限期内重提同一手机号 → 400 带撤销提示 ----
    def test_resubmit_same_phone_gets_undo_hint(self):
        c, token = self._user_client()
        self._submit_and_approve(c, token)
        r = c.delete("/api/my-accounts/0", headers=self._csrf(token))
        self.assertEqual(r.status_code, 200)
        r = c.post("/api/my-accounts", json={
            "name": "重提", "phone": PHONE, "password": "p3",
        }, headers=self._csrf(token))
        self.assertEqual(r.status_code, 400)
        self.assertIn("撤销删除", r.get_json()["error"])

    # ---- 6. 超期软删行仍被每日清理物理清除 ----
    def test_expired_self_deleted_row_purged(self):
        c, token = self._user_client()
        self._submit_and_approve(c, token)
        r = c.delete("/api/my-accounts/0", headers=self._csrf(token))
        self.assertEqual(r.status_code, 200)
        # 回拨 deleted_at 超过保留期
        stale = (datetime.now() - timedelta(days=db.SOFT_DELETE_RETENTION_DAYS + 1)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        conn = db.get_conn()
        with db._conn_lock, conn:
            conn.execute("UPDATE accounts SET deleted_at=? WHERE phone=?", (stale, PHONE))
            conn.commit()
        db.purge_expired_deleted_accounts()
        self.assertFalse(
            any(a["phone"] == PHONE for a in db.load_accounts()),
            "超期软删行应被物理清除",
        )

    # ---- 7. 批量删除（set_deleted）留痕 admin ----
    def test_batch_set_deleted_marks_admin(self):
        c, token = self._user_client()
        acc = self._submit_and_approve(c, token)
        db.batch_account_ops([("set_deleted", acc["id"], 1, "2026-01-01 00:00:00")])
        self.assertEqual(self._row()["deleted_by"], "admin")

    # ---- 8. v10 迁移幂等：旧库补列默认空串（fail-closed，不可被用户撤销） ----
    def test_migration_v10_idempotent(self):
        conn = db.get_conn()
        cols = {r[1] for r in conn.execute("PRAGMA table_info(accounts)").fetchall()}
        self.assertIn("deleted_by", cols)
        # 幂等重跑不抛错
        db.migrate_v10(conn)


if __name__ == "__main__":
    unittest.main()
