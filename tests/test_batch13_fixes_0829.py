# -*- coding: utf-8 -*-
"""批次13 被盗号滥用面加固回归测试（2026-08-29）。

针对「盗号 → 反复批量删除用户 → 耗尽告警邮件额度」攻击链的三层加固：

- 加固1 高危告警邮件节流：同类标题在窗口内只发一封邮件（YIBAN_MAIL_ALERT_COOLDOWN，
  默认 300s），webhook 保持实时逐条推送，防被盗会话耗尽 SMTP 发件额度。
- 加固2 高危删除操作冷却：同一管理员窗口内批量删除/彻底清除/完全删除超限返回 429
  （YIBAN_ADMIN_DELETE_MAX 次 / YIBAN_ADMIN_DELETE_COOLDOWN_SEC 秒，0=关闭）。
- 加固3 高危操作二次鉴权：删除类操作须重新输入当前管理员密码，失败与登录/改密共用
  失败计数，达阈值（LOGIN_FAIL_NOTIFY=3）告警、锁定（LOGIN_MAX_FAILS=5）。

用法（项目根目录）：
    py -m pytest tests/test_batch13_fixes_0829.py -v
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

TEST_KEY = "c" * 64
ADMIN_PASS = "MasterPass#2026"
USER_PASS = "secret1"


class _B13WebBase(unittest.TestCase):
    """隔离环境 + 可选告警 mock 的公共基类。"""

    PATCH_NOTIFY = True  # 子类可关闭（邮件节流测试需真实 send_notification）

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-batch13-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(
                f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
                f"YIBAN_ADMIN_USER=admin\nYIBAN_ADMIN_PASSWORD={ADMIN_PASS}\n"
                f"YIBAN_MAIL_ADMIN_TO=admin@test.local\n"
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
        spec = importlib.util.spec_from_file_location("webapp_b13", os.path.join(BASE, "web", "app.py"))
        cls.webapp = importlib.util.module_from_spec(spec)
        sys.modules["webapp_b13"] = cls.webapp
        with contextlib.suppress(Exception):
            spec.loader.exec_module(cls.webapp)

    @classmethod
    def tearDownClass(cls):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k in ("YIBAN_ACCOUNTS_KEY", "YIBAN_LOG_FILE", "YIBAN_STATE_DIR",
                  "YIBAN_ENV_FILE", "YIBAN_ACCOUNTS_FILE", "YIBAN_USERS_FILE",
                  "YIBAN_DB_FILE"):
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
        self.alerts = []
        if self.PATCH_NOTIFY:
            p = mock.patch.object(
                self.webapp, "send_notification",
                side_effect=lambda t, c, urgent=False: self.alerts.append((t, c)),
            )
            p.start()
            self.addCleanup(p.stop)

    # ---- 工具 ----
    def _login(self, c, username, password):
        r = c.post("/api/login", json={"username": username, "password": password})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return c.get("/api/me").get_json()["csrf_token"]

    def _csrf(self, token):
        return {"X-CSRF-Token": token}

    def _make_user(self, email, password=None):
        db.create_user(email, self.webapp.generate_password_hash(password or USER_PASS))
        return email


class MailAlertThrottleTest(_B13WebBase):
    """加固1：高危告警邮件按标题节流，webhook 保持实时。"""

    PATCH_NOTIFY = False  # 需要真实 send_notification 验证节流

    def setUp(self):
        super().setUp()
        self.webapp._mail_alert_ts.clear()  # 模块级节流状态复位
        with open(self.env_file, "a", encoding="utf-8") as f:
            f.write("YIBAN_MAIL_ALERT_COOLDOWN=60\n")

    def test_same_title_email_throttled_webhook_kept(self):
        mails = []
        hooks = []
        # 邮件节流不应作用于 webhook：此处关闭 webhook 节流（YIBAN_NOTIFY_COOLDOWN=0）
        # 隔离验证；webhook 组件（notify.py）自身节流测试见 test_notify_0829.py
        with open(self.env_file, "a", encoding="utf-8") as f:
            f.write("YIBAN_NOTIFY_COOLDOWN=0\n")
        with mock.patch.object(self.webapp.mailer, "send_admin_alert",
                               side_effect=lambda t, c, to=None: mails.append((t, c))), \
             mock.patch.object(self.webapp.notify, "_send_custom",
                               side_effect=lambda url, t, c: hooks.append(url)):
            os.environ["YIBAN_NOTIFY_URL"] = "https://example.com/hook"
            try:
                self.webapp.send_notification("高危管理操作告警", "第一次")
                self.webapp.send_notification("高危管理操作告警", "第二次（窗口内，应仅 webhook）")
                self.webapp.send_notification("密码重置告警", "不同标题不受节流")
            finally:
                os.environ.pop("YIBAN_NOTIFY_URL", None)
        self.assertEqual(
            len(mails), 2,
            f"同标题窗口内应只发一封邮件，实际 {[m[0] for m in mails]}",
        )
        self.assertEqual(len(hooks), 3, "webhook 节流关闭时应逐条推送")

    def test_mail_alert_due_respects_window(self):
        due = self.webapp._mail_alert_due
        self.assertTrue(due("高危管理操作告警"))
        self.assertFalse(due("高危管理操作告警"), "窗口内同标题应被节流")
        self.assertTrue(due("密码重置告警"), "不同标题可发送")

    def test_mail_alert_due_window_zero_disables(self):
        with open(self.env_file, "a", encoding="utf-8") as f:
            f.write("YIBAN_MAIL_ALERT_COOLDOWN=0\n")
        self.webapp._mail_alert_ts.clear()
        self.assertTrue(self.webapp._mail_alert_due("高危管理操作告警"))
        self.assertTrue(self.webapp._mail_alert_due("高危管理操作告警"), "0=关闭节流")


class HighRiskDeleteTest(_B13WebBase):
    """加固2+3：高危删除冷却 + 二次鉴权。"""

    def test_batch_delete_requires_password(self):
        self._make_user("u1@test.local")
        c = self.webapp.create_app().test_client()
        t = self._login(c, "admin", ADMIN_PASS)
        r = c.post("/api/users/batch",
                   json={"action": "delete", "emails": ["u1@test.local"]},
                   headers=self._csrf(t))
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        self.assertIn("当前密码不正确", r.get_json()["error"])
        self.assertIsNotNone(db.find_user("u1@test.local"), "未通过鉴权不得删除")

    def test_batch_delete_wrong_password_alerts_and_blocks(self):
        self._make_user("u2@test.local")
        c = self.webapp.create_app().test_client()
        t = self._login(c, "admin", ADMIN_PASS)
        for i in range(3):
            r = c.post("/api/users/batch",
                       json={"action": "delete", "emails": ["u2@test.local"],
                             "confirm_password": "wrong-pass"},
                       headers=self._csrf(t))
            self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        self.assertIsNotNone(db.find_user("u2@test.local"), "未通过鉴权不得删除")
        self.assertTrue(any("高危操作二次鉴权失败告警" == x for x, _ in self.alerts),
                        f"第 3 次失败应告警，实际 {self.alerts}")

    def test_batch_delete_correct_password_200(self):
        self._make_user("u3@test.local")
        c = self.webapp.create_app().test_client()
        t = self._login(c, "admin", ADMIN_PASS)
        r = c.post("/api/users/batch",
                   json={"action": "delete", "emails": ["u3@test.local"],
                         "confirm_password": ADMIN_PASS},
                   headers=self._csrf(t))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertIsNone(db.find_user("u3@test.local"))

    def test_registered_admin_uses_own_password(self):
        db.create_user("radmin@test.local",
                       self.webapp.generate_password_hash(ADMIN_PASS), role="admin")
        self._make_user("ru1@test.local")
        c = self.webapp.create_app().test_client()
        t = self._login(c, "radmin@test.local", ADMIN_PASS)
        r = c.post("/api/users/batch",
                   json={"action": "delete", "emails": ["ru1@test.local"],
                         "confirm_password": ADMIN_PASS},
                   headers=self._csrf(t))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertIsNone(db.find_user("ru1@test.local"))

    def test_delete_cooldown_429(self):
        with open(self.env_file, "a", encoding="utf-8") as f:
            f.write("YIBAN_ADMIN_DELETE_MAX=2\n")
        c = self.webapp.create_app().test_client()
        t = self._login(c, "admin", ADMIN_PASS)
        for i in range(3):
            self._make_user(f"cd{i}@test.local")
            r = c.post("/api/users/batch",
                       json={"action": "delete", "emails": [f"cd{i}@test.local"],
                             "confirm_password": ADMIN_PASS},
                       headers=self._csrf(t))
            if i < 2:
                self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
            else:
                self.assertEqual(r.status_code, 429, r.get_data(as_text=True))
        self.assertIsNotNone(db.find_user("cd2@test.local"), "限速拒绝的删除不应生效")

    def test_single_full_delete_requires_password(self):
        self._make_user("s1@test.local")
        c = self.webapp.create_app().test_client()
        t = self._login(c, "admin", ADMIN_PASS)
        r = c.post("/api/users/s1@test.local/delete",
                   json={"mode": "full"},
                   headers=self._csrf(t))
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        self.assertIsNotNone(db.find_user("s1@test.local"))
        # accounts_only（仅清空账号，保留用户）不受二次鉴权约束
        r2 = c.post("/api/users/s1@test.local/delete",
                    json={"mode": "accounts_only"},
                    headers=self._csrf(t))
        self.assertEqual(r2.status_code, 200, r2.get_data(as_text=True))

    def test_purge_requires_password(self):
        self._make_user("p1@test.local")
        db.soft_delete_user_with_accounts("p1@test.local")
        c = self.webapp.create_app().test_client()
        t = self._login(c, "admin", ADMIN_PASS)
        r = c.post("/api/users/deleted/purge",
                   json={"emails": ["p1@test.local"]},
                   headers=self._csrf(t))
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        self.assertIsNotNone(db.find_user_any("p1@test.local"), "未鉴权不得物理清除")
        # 正确密码可清除
        r2 = c.post("/api/users/deleted/purge",
                    json={"emails": ["p1@test.local"], "confirm_password": ADMIN_PASS},
                    headers=self._csrf(t))
        self.assertEqual(r2.status_code, 200, r2.get_data(as_text=True))
        self.assertIsNone(db.find_user_any("p1@test.local"))


class NotifyConfigApiTest(_B13WebBase):
    """Webhook 组件配置 API：加密落盘、权限、清除、测试端点。"""

    def setUp(self):
        super().setUp()
        # env 文件跨用例共享，复位 YIBAN_NOTIFY_*（含环境变量与模块节流态）
        env_path = self.env_file
        with open(env_path, encoding="utf-8") as f:
            lines = f.read().splitlines()
        lines = [ln for ln in lines if not ln.strip().startswith("YIBAN_NOTIFY_")]
        with open(env_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        for k in list(os.environ):
            if k.startswith("YIBAN_NOTIFY_"):
                os.environ.pop(k, None)
        self.webapp.notify._throttle_ts.clear()
        # 批次14：每日额度已拆成非紧急 / 紧急两本账，复位时两本都要清
        self.webapp.notify._general_daily["state"].update({"date": "", "count": 0})
        self.webapp.notify._urgent_daily["state"].update({"date": "", "count": 0})

    def test_get_config_default_off(self):
        c = self.webapp.create_app().test_client()
        self._login(c, "admin", ADMIN_PASS)
        data = c.get("/api/notify-config").get_json()
        self.assertFalse(data["enabled"])
        self.assertFalse(data["configured"])

    def test_put_serverchan_encrypts_and_persists(self):
        c = self.webapp.create_app().test_client()
        t = self._login(c, "admin", ADMIN_PASS)
        # 批次14 P1-1：携带新密钥（换钥）属高危动作 → 须带二次口令；断言意图不变
        r = c.put("/api/notify-config", json={
            "type": "serverchan", "secret": "SCT406257TESTTESTTESTTESTTEST",
            "confirm_password": ADMIN_PASS,
        }, headers=self._csrf(t))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(r.get_json()["enabled"])
        env = open(self.env_file, encoding="utf-8").read()
        self.assertIn("YIBAN_NOTIFY_TYPE=serverchan", env)
        self.assertIn("YIBAN_NOTIFY_SECRET_ENC=", env)
        self.assertNotIn("SCT406257TESTTESTTESTTESTTEST", env, "明文 SendKey 不得落盘")
        self.assertEqual(
            self.webapp.notify.get_secret(), "SCT406257TESTTESTTESTTESTTEST",
            "加密存储应能解密回读",
        )

    def test_put_requires_master_admin(self):
        self._make_user("u@test.local")
        c = self.webapp.create_app().test_client()
        t = self._login(c, "u@test.local", USER_PASS)
        r = c.put("/api/notify-config", json={
            "type": "serverchan", "secret": "SCTxxxx",
        }, headers=self._csrf(t))
        self.assertEqual(r.status_code, 403)

    def test_put_bad_type_400(self):
        c = self.webapp.create_app().test_client()
        t = self._login(c, "admin", ADMIN_PASS)
        r = c.put("/api/notify-config", json={"type": "wechat", "secret": "x"},
                  headers=self._csrf(t))
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))

    def test_put_custom_rejects_unsafe_url(self):
        c = self.webapp.create_app().test_client()
        t = self._login(c, "admin", ADMIN_PASS)
        r = c.put("/api/notify-config", json={
            "type": "custom", "secret": "http://example.com/hook",
        }, headers=self._csrf(t))
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))

    def test_put_clear_disables(self):
        c = self.webapp.create_app().test_client()
        t = self._login(c, "admin", ADMIN_PASS)
        # 批次14 P1-1：换钥与关闭通道均为高危 → 须带二次口令；"关得掉"的断言意图不变
        c.put("/api/notify-config", json={
            "type": "serverchan", "secret": "SCT406257TESTTESTTESTTESTTEST",
            "confirm_password": ADMIN_PASS,
        }, headers=self._csrf(t))
        r = c.put("/api/notify-config", json={"type": "", "confirm_password": ADMIN_PASS},
                  headers=self._csrf(t))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertFalse(r.get_json()["enabled"])

    def test_put_urgent_only_partial_preserves_channel(self):
        c = self.webapp.create_app().test_client()
        t = self._login(c, "admin", ADMIN_PASS)
        c.put("/api/notify-config", json={
            "type": "serverchan", "secret": "SCT406257TESTTESTTESTTESTTEST",
            "confirm_password": ADMIN_PASS,  # 批次14 P1-1：换钥须二次口令
        }, headers=self._csrf(t))
        # 仅保存「仅重要告警」，不应清空已配置的通道与密钥（部分更新）
        # 批次14 P1-1：纯开关改动刻意不带口令——仍须 200（不给正常路径加摩擦）
        r = c.put("/api/notify-config", json={"urgent_only": True}, headers=self._csrf(t))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        data = r.get_json()
        self.assertTrue(data["urgent_only"])
        self.assertTrue(data["enabled"])
        self.assertEqual(
            self.webapp.notify.get_secret(), "SCT406257TESTTESTTESTTESTTEST",
            "部分更新不得清除已配置密钥",
        )
        env = open(self.env_file, encoding="utf-8").read()
        self.assertIn("YIBAN_NOTIFY_URGENT_ONLY=1", env)
        self.assertIn("YIBAN_NOTIFY_TYPE=serverchan", env)

    def test_put_urgent_only_off(self):
        c = self.webapp.create_app().test_client()
        t = self._login(c, "admin", ADMIN_PASS)
        c.put("/api/notify-config", json={"urgent_only": True}, headers=self._csrf(t))
        r = c.put("/api/notify-config", json={"urgent_only": False}, headers=self._csrf(t))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertFalse(r.get_json()["urgent_only"])

    def test_put_urgent_only_requires_master(self):
        self._make_user("u@test.local")
        c = self.webapp.create_app().test_client()
        t = self._login(c, "u@test.local", USER_PASS)
        r = c.put("/api/notify-config", json={"urgent_only": True}, headers=self._csrf(t))
        self.assertEqual(r.status_code, 403)

    def test_notify_test_requires_config(self):
        c = self.webapp.create_app().test_client()
        t = self._login(c, "admin", ADMIN_PASS)
        r = c.post("/api/notify-test", headers=self._csrf(t))
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main(verbosity=2)
