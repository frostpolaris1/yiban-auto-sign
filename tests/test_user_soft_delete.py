# -*- coding: utf-8 -*-
"""用户删除账号的软删化回归。

标签：L · 注销与软删
覆盖：用户删除自己的易班账号 → 软删（行保留、`deleted_by`=本人、凭据与事件不被清除）
    → 撤销恢复且状态保留；重复删除已删行 400；管理员删除的行用户撤销 403、
    管理员恢复 200 且清空 `deleted_by`；名下已有其他生效账号时撤销 400（每人限 1）；
    宽限期内重提同一手机号 400 带撤销提示；超期软删行仍被每日清理物理清除；
    批量删除（`batch_account_ops` 的 set_deleted）留痕 `'admin'`。
对应实现：端点在 `web/app.py`，`deleted_by` 列来自迁移 v10，
    超期软删行的每日物理清除走 `yiban/store/cleanup.py`
    （`purge_expired_deleted_accounts`）。
关键断言：两条路径的触发条件不合并——**软删可恢复**只对
    `deleted_by`=本人邮箱的行开放（管理员删除/系统连带行用户不可自撤）；
    **到期物理清除**照常发生，不因"用户可撤销"而豁免每日清理。
依赖：Flask test client + 临时 sqlite/.env（v10 迁移路径）；不触网、不发信。

语义变更：由"立即物理清除"改为"软删除 + 7 天宽限内可撤销"，与管理员侧两档语义对齐。
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
from datetime import datetime, timedelta

from _mail_body import render_body

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

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
        self._submit_and_approve(c, token)
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
        self._submit_and_approve(c, token)
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


ADMIN_PASS_SOFTDEL = "MasterPass#2026"


REG_ADMIN = "regadmin@test.local"


REG_ADMIN_PASS = "RegAdmin#2026"


USER = "stu@test.local"


USER_PASS_SOFTDEL = "Student#2026"


def _load_webapp():
    spec = importlib.util.spec_from_file_location("webapp_sdalert", os.path.join(BASE, "web", "app.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["webapp_sdalert"] = mod
    with contextlib.suppress(Exception):
        spec.loader.exec_module(mod)
    return mod


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-sdalert-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.accounts_file = os.path.join(cls.tmp, "accounts.json")
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_ACCOUNTS_FILE"] = cls.accounts_file
        os.environ["YIBAN_DB_FILE"] = cls.db_file
        os.environ["YIBAN_STATE_DIR"] = cls.tmp
        os.environ["YIBAN_LOG_FILE"] = os.path.join(cls.tmp, "sign.log")
        os.environ["YIBAN_MAIL_ENABLE"] = "0"
        cls.db = __import__("db")
        cls.webapp = _load_webapp()

    @classmethod
    def tearDownClass(cls):
        if cls.db._conn is not None:
            with contextlib.suppress(Exception):
                cls.db._conn.close()
            cls.db._conn = None
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        if self.db._conn is not None:
            with contextlib.suppress(Exception):
                self.db._conn.close()
            self.db._conn = None
        for suffix in ("", "-wal", "-shm"):
            p = self.db_file + suffix
            if os.path.exists(p):
                os.remove(p)
        # 每个用例重置 .env（限速阈值按用例调整）
        with io.open(self.env_file, "w", encoding="utf-8") as f:
            f.write(
                f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
                "YIBAN_ADMIN_USER=admin@test.local\n"
                f"YIBAN_ADMIN_PASSWORD={ADMIN_PASS_SOFTDEL}\n"
            )
        self.db.init_db(self.db_file, migrate_from=self.accounts_file, env_file=self.env_file)
        # 告警探针：替换模块级 send_notification（handler 走全局查找，运行时生效）
        self.alerts = []
        self._orig_send = self.webapp.send_notification

        def _probe(title, content, **kw):
            self.alerts.append((title, render_body(content), kw))
            return True

        self.webapp.send_notification = _probe

    def tearDown(self):
        self.webapp.send_notification = self._orig_send

    # ---- 脚手架 ----
    def _mk_registered_admin(self):
        from werkzeug.security import generate_password_hash
        self.db.create_user(REG_ADMIN, generate_password_hash(REG_ADMIN_PASS), role="admin")

    def _mk_user_with_account(self, email=USER, phone="13900000001"):
        from werkzeug.security import generate_password_hash
        self.db.create_user(email, generate_password_hash(USER_PASS_SOFTDEL), role="user")
        self.db.add_account({
            "name": "测试账号", "phone": phone, "password": "pw",
            "phone_model": "", "phone_code": "", "owner": email,
            "status": "active", "reject_reason": "",
        })

    def _login(self, username, password):
        c = self.webapp.create_app().test_client()
        r = c.post("/api/login", json={"username": username, "password": password})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        token = c.get("/api/me").get_json()["csrf_token"]
        return c, {"X-CSRF-Token": token}

    def _assert_no_alert(self):
        """管理操作即时告警已下线：这些路径不该再产生任何外发（含 webhook）。"""
        self.assertEqual(self.alerts, [], f"预期零告警，实际 {self.alerts}")


class SoftDeleteNoAlertTest(_Base):
    """软删不再外发管理员告警：留痕由审计行承担（软删可逆，不该按事故通报）。

    本类同时守住"告警下线没有连审计一起删掉"——每例都回查那条审计行。
    """

    def _audit_count(self, action):
        return len([dict(r) for r in self.db.get_conn().execute(
            "SELECT action FROM audit_logs WHERE action=?", (action,)).fetchall()])

    def test_single_soft_delete_emits_no_alert(self):
        """注册管理员单条软删 → 200、无告警、审计留痕。"""
        self._mk_registered_admin()
        self._mk_user_with_account()
        c, h = self._login(REG_ADMIN, REG_ADMIN_PASS)
        r = c.delete("/api/accounts/0", json={}, headers=h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self._assert_no_alert()
        self.assertEqual(self._audit_count("account_delete"), 1, "软删必须留审计")
        # 确认软删确实生效
        row = next(a for a in self.db.load_accounts_raw() if a["phone"] == "13900000001")
        self.assertEqual(row["deleted"], 1)

    def test_batch_soft_delete_emits_no_alert(self):
        """批量软删 → 200、无告警、审计一条（含目标清单）。"""
        self._mk_registered_admin()
        self._mk_user_with_account(USER, "13900000001")
        self.db.add_account({
            "name": "裸账号", "phone": "13900000002", "password": "pw",
            "phone_model": "", "phone_code": "", "owner": "admin",
            "status": "active", "reject_reason": "",
        })
        c, h = self._login(REG_ADMIN, REG_ADMIN_PASS)
        accts = c.get("/api/accounts").get_json()["accounts"]
        ids = [a["index"] for a in accts]
        phones = [a["phone"] for a in accts]
        r = c.post("/api/accounts/batch",
                   json={"action": "delete", "ids": ids, "phones": phones}, headers=h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self._assert_no_alert()
        rows = [dict(x) for x in self.db.get_conn().execute(
            "SELECT target, detail FROM audit_logs WHERE action='account_batch'").fetchall()]
        self.assertEqual(len(rows), 1, "批量软删必须留一条审计")
        self.assertIn(f"{len(ids)} 个", rows[0]["detail"])

    def test_soft_delete_rate_limited(self):
        """超过 YIBAN_ADMIN_DELETE_MAX 后软删返回 429（单条与批量共用同一额度）。"""
        self._mk_registered_admin()
        self._mk_user_with_account(USER, "13900000001")
        self.db.add_account({
            "name": "裸账号", "phone": "13900000002", "password": "pw",
            "phone_model": "", "phone_code": "", "owner": "admin",
            "status": "active", "reject_reason": "",
        })
        # 阈值设为 1：第一次成功，第二次 429（限速读 .env，热生效）
        self.webapp.write_env_key(self.env_file, "YIBAN_ADMIN_DELETE_MAX", "1")
        self.webapp.write_env_key(self.env_file, "YIBAN_ADMIN_DELETE_COOLDOWN_SEC", "600")
        c, h = self._login(REG_ADMIN, REG_ADMIN_PASS)
        r1 = c.delete("/api/accounts/0", json={}, headers=h)
        self.assertEqual(r1.status_code, 200, r1.get_data(as_text=True))
        r2 = c.delete("/api/accounts/1", json={}, headers=h)
        self.assertEqual(r2.status_code, 429, f"第二次软删应被限速，实际 {r2.status_code}")
        self.assertIn("频繁", r2.get_json()["error"])

    def test_user_self_delete_not_blocked_by_admin_limit(self):
        """用户自助删除自己账号不走高危限速（不得误伤普通用户）。"""
        self._mk_registered_admin()
        self._mk_user_with_account(USER, "13900000001")
        self.db.add_account({
            "name": "裸账号", "phone": "13900000002", "password": "pw",
            "phone_model": "", "phone_code": "", "owner": "admin",
            "status": "active", "reject_reason": "",
        })
        self.webapp.write_env_key(self.env_file, "YIBAN_ADMIN_DELETE_MAX", "1")
        self.webapp.write_env_key(self.env_file, "YIBAN_ADMIN_DELETE_COOLDOWN_SEC", "600")
        # 管理员先消耗掉额度（删 index=1 的裸账号，避免碰掉学生自己的账号）
        ca, ha = self._login(REG_ADMIN, REG_ADMIN_PASS)
        self.assertEqual(ca.delete("/api/accounts/1", json={}, headers=ha).status_code, 200)
        # 用户删自己（my-accounts 下标 0 = 本人唯一账号）→ 必须仍然 200
        cu, hu = self._login(USER, USER_PASS_SOFTDEL)
        ru = cu.delete("/api/my-accounts/0", json={}, headers=hu)
        self.assertEqual(ru.status_code, 200, f"用户自助删除不得被管理员额度影响: {ru.get_data(as_text=True)}")


if __name__ == "__main__":
    unittest.main()
