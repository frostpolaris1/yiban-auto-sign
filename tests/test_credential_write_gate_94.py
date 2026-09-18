# -*- coding: utf-8 -*-
"""管理员改写他人易班凭据的二次鉴权 + 当事人知情回归。

威胁模型：被窃（或本身恶意）的**注册管理员**会话。此前 `PUT /api/accounts/<idx>`
只要 role=='admin' 就能把某用户的易班密码换成攻击者自己的凭据——此后签到在攻击者侧
完成、真用户被静默挤出，且当事人不知情；改绑手机号也只需一次 PUT。

口径（用户裁决）：
- 请求体写入非空 `password` 或改绑 `phone` → 必须过 `_high_risk_gate`（当次口令 + 高危额度）；
- 改写成功后绕过当事人的 `mail_notify` 开关发一封变更信（与自助改密、审核拒绝同一口径）；
- 拿不出任何可核对标识（既无 `_snapshot` 又无 `phone`）的编辑请求按错位拒绝（fail-closed）；
- 只改备注/设备型号不算改写凭据，不得因此多一道口令；
- `POST /api/my-accounts` 不再按提交者角色分叉出"管理员提交即生效"，一律待审核。

全程 Flask test_client + 临时 .env/DB，无网络请求。
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

# 告警/邮件正文入参已放宽为 layout.Mail | str，捕获点统一渲染成文本
from _mail_body import render_body  # noqa: E402

TEST_KEY = "a" * 64
ADMIN_PASS = "Master-Test-2026!"
SUB_PASS = "Subadmin-Test-2026!"
OWNER = "student@example.cn"
PHONE = "13800008000"
OLD_PW = "OldPass1234!"
NEW_PW = "AttackerPass123!"


class CredentialWriteGateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-cred-gate-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
                    "YIBAN_ADMIN_USER=admin\n"
                    f"YIBAN_ADMIN_PASSWORD={ADMIN_PASS}\n")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.accounts_file = os.path.join(cls.tmp, "accounts.json")
        for k, v in {
            "YIBAN_ACCOUNTS_KEY": TEST_KEY, "YIBAN_ENV_FILE": cls.env_file,
            "YIBAN_ACCOUNTS_FILE": cls.accounts_file,
            "YIBAN_USERS_FILE": os.path.join(cls.tmp, "users.json"),
            "YIBAN_DB_FILE": cls.db_file, "YIBAN_STATE_DIR": cls.tmp,
            "YIBAN_LOG_FILE": os.path.join(cls.tmp, "sign.log"),
        }.items():
            os.environ[k] = v
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
                  "YIBAN_USERS_FILE", "YIBAN_DB_FILE", "YIBAN_STATE_DIR", "YIBAN_LOG_FILE"):
            os.environ.pop(k, None)

    def setUp(self):
        from werkzeug.security import generate_password_hash
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
        db.init_db(db_file=self.db_file, migrate_from=self.accounts_file, env_file=self.env_file)
        db.create_user(OWNER, generate_password_hash("Stu-Test-2026!", method=self.webapp.SCRYPT_METHOD),
                       role="user", created_at="2026-09-18 00:00:00", pw_version=1)
        db.create_user("sub.admin@example.cn",
                       generate_password_hash(SUB_PASS, method=self.webapp.SCRYPT_METHOD),
                       role="admin", created_at="2026-09-18 00:00:00", pw_version=1)
        db.add_account({"name": "小明", "phone": PHONE, "password": OLD_PW,
                        "phone_model": "", "phone_code": "", "owner": OWNER,
                        "status": self.webapp.ACCOUNT_STATUS_ACTIVE})
        self.app = self.webapp.create_app()
        self.sent = []
        self._orig_send = self.webapp.mailer.send_user
        self.webapp.mailer.send_user = lambda to, subject, text: self.sent.append((to, subject, render_body(text)))

    def tearDown(self):
        self.webapp.mailer.send_user = self._orig_send

    def _login(self, user, pw):
        c = self.app.test_client()
        r = c.post("/api/login", json={"username": user, "password": pw})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return c, {"X-CSRF-Token": c.get("/api/me").get_json()["csrf_token"]}

    def _sub(self):
        return self._login("sub.admin@example.cn", SUB_PASS)

    def _password_of(self, idx=0):
        return self.webapp.load_accounts()[idx].get("password")

    def _status_of(self, idx=0):
        return self.webapp.load_accounts()[idx].get("status")

    # ---- 二次鉴权 ----
    def test_subadmin_rewriting_password_without_confirm_is_refused(self):
        c, h = self._sub()
        r = c.put("/api/accounts/0", json={"phone": PHONE, "password": NEW_PW}, headers=h)
        self.assertIn(r.status_code, (400, 403), r.get_data(as_text=True))
        self.assertEqual(self._password_of(), OLD_PW, "鉴权未通过时凭据不得被改写")
        self.assertEqual(self.sent, [], "未生效的编辑不该发通知")

    def test_subadmin_rewriting_password_with_confirm_succeeds_and_notifies(self):
        c, h = self._sub()
        r = c.put("/api/accounts/0",
                  json={"phone": PHONE, "password": NEW_PW, "confirm_password": SUB_PASS},
                  headers=h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(self._password_of(), NEW_PW)
        self.assertEqual(len(self.sent), 1, "当事人必须收到一封变更信")
        to, subject, text = self.sent[0]
        self.assertEqual(to, OWNER)
        self.assertIn("管理员修改", subject)
        self.assertNotIn(NEW_PW, text, "变更信不得回显新口令")
        self.assertNotIn(PHONE, text, "变更信里的手机号必须是脱敏形态")
        self.assertIn("138****8000", text)

    def test_rebind_phone_also_counts_as_credential_write(self):
        c, h = self._sub()
        r = c.put("/api/accounts/0",
                  json={"_snapshot": {"phone": PHONE}, "phone": "13900009000"}, headers=h)
        self.assertIn(r.status_code, (400, 403), f"改绑手机号同样要二次鉴权: {r.get_data(as_text=True)}")
        self.assertEqual(self._password_of(), OLD_PW)

    def test_bare_rebind_without_identity_is_rejected(self):
        """不带原始标识的"改绑"与 idx 漂移不可区分——必须先被拒，不能进改写判定。"""
        c, h = self._sub()
        r = c.put("/api/accounts/0", json={"phone": "13900009000"}, headers=h)
        self.assertEqual(r.status_code, 409, r.get_data(as_text=True))
        self.assertEqual(self._password_of(), OLD_PW)

    def test_note_only_edit_needs_no_second_factor(self):
        """只改备注/设备型号不算改写凭据——不该被要求多输一次口令。"""
        c, h = self._sub()
        r = c.put("/api/accounts/0", json={"phone": PHONE, "name": "改名", "phone_model": "iPhone"},
                  headers=h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(self._password_of(), OLD_PW)
        self.assertEqual(self.sent, [])

    def test_bare_account_rewrite_needs_gate_and_sends_no_mail(self):
        """owner='admin' 的代管账号没有当事人可通知，但鉴权一档不降。"""
        db.add_account({"name": "代管", "phone": "13700007000", "password": OLD_PW,
                        "phone_model": "", "phone_code": "", "owner": "admin",
                        "status": self.webapp.ACCOUNT_STATUS_ACTIVE})
        c, h = self._sub()
        r = c.put("/api/accounts/1", json={"phone": "13700007000", "password": NEW_PW}, headers=h)
        self.assertIn(r.status_code, (400, 403))
        r2 = c.put("/api/accounts/1",
                   json={"phone": "13700007000", "password": NEW_PW, "confirm_password": SUB_PASS},
                   headers=h)
        self.assertEqual(r2.status_code, 200, r2.get_data(as_text=True))
        self.assertEqual(self.sent, [], "无当事人的行不发信，也不因此报错")

    # ---- idx 错位 fail-closed ----
    def test_edit_without_any_identifier_is_rejected(self):
        c, h = self._sub()
        r = c.put("/api/accounts/0", json={"name": "悄悄改"}, headers=h)
        self.assertEqual(r.status_code, 409, r.get_data(as_text=True))
        self.assertEqual(self._password_of(), OLD_PW)

    def test_snapshot_is_identity_only_and_phone_still_required(self):
        """快照只用来核对 idx，顶层 `phone` 仍是必填：该请求死在字段校验（400）。

        顺带记一句给后来人——本路由的 fail-closed 守卫**没有授权增量**：不带 phone 的请求
        原本也会因"手机号为必填项"被拒，改动只是把拒绝点从字段校验提前到错位判定
        （400 → 409）。真正堵上改写口子的是 `creds_written` 那道 `_high_risk_gate`。
        """
        c, h = self._sub()
        r = c.put("/api/accounts/0",
                  json={"_snapshot": json.dumps({"phone": PHONE}), "name": "备注而已"}, headers=h)
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        self.assertIn("手机号", r.get_json()["error"])
        self.assertEqual(self.webapp.load_accounts()[0].get("name"), "小明")

    def test_mismatched_snapshot_is_rejected(self):
        c, h = self._sub()
        r = c.put("/api/accounts/0",
                  json={"_snapshot": json.dumps({"phone": "13911112222"}), "name": "x"}, headers=h)
        self.assertEqual(r.status_code, 409, r.get_data(as_text=True))

    # ---- 管理员自提交一律待审核 ----
    def test_admin_self_submit_is_pending_and_review_promotes(self):
        c, h = self._sub()
        r = c.post("/api/my-accounts",
                   json={"phone": "13600006000", "password": "SomePass123!", "name": "自交"},
                   headers=h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(self._status_of(1), self.webapp.ACCOUNT_STATUS_PENDING,
                         "管理员提交不再按角色免审即生效")
        cm, hm = self._login("admin", ADMIN_PASS)
        r2 = cm.post("/api/accounts/1/review",
                     json={"action": "approve", "phone": "13600006000"}, headers=hm)
        self.assertEqual(r2.status_code, 200, r2.get_data(as_text=True))
        self.assertEqual(self._status_of(1), self.webapp.ACCOUNT_STATUS_ACTIVE,
                         "审核通道仍能让管理员把代管号放行")


if __name__ == "__main__":
    unittest.main()
