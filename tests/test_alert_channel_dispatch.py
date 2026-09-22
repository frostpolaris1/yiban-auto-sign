# -*- coding: utf-8 -*-
"""告警推送出口的判定与兜底（2026-09-08）。

覆盖：
- notify.is_configured()：推送通道是否「类型已设（含旧明文 URL 兼容）且密钥可解出」；
- signin 的即时告警门控由已废弃的 YIBAN_NOTIFY_URL 死键改判 is_configured
  （生产不配旧键 → 账号级失败/耗时推送此前永不发出）；源级断言不再出现
  if notify_url: 门；
- _flush_admin_mail_summary 收件人集为空时的整卷兜底：推送已配置 → 同一份汇总
  改走推送恰好一次（urgent+force）；未配置 → 仅 warning 留痕。

全程 mock，不发起网络请求。
用法（项目根目录）：python -m pytest tests/test_alert_channel_dispatch.py -v
"""
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

import signin
from test_rekey_key_source import _B14AlertGateBase

import web.security as web_security
from yiban import notify  # 推送组件实现包（旧 scripts/notify.py 壳已删除）
from yiban.infra import account_crypto

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TEST_KEY = "a" * 64
SCT_KEY = "SCT406257DISPATCHTEST0000"
NOTIFY_ENV_KEYS = (
    "YIBAN_NOTIFY_TYPE", "YIBAN_NOTIFY_SECRET_ENC", "YIBAN_NOTIFY_URL",
    "YIBAN_NOTIFY_COOLDOWN", "YIBAN_NOTIFY_URGENT_ONLY",
    "YIBAN_NOTIFY_DAILY_MAX", "YIBAN_NOTIFY_URGENT_DAILY_MAX",
)


def _notify_enc(plain):
    """按设置页同口径生成 YIBAN_NOTIFY_SECRET_ENC 的落盘值（密文对象的 JSON 串）。"""
    return json.dumps(
        account_crypto.encrypt_text(plain, account_crypto._decode_key(TEST_KEY)),
        ensure_ascii=False)


class NotifyIsConfiguredTest(unittest.TestCase):
    """notify.is_configured()：与 send() 自身的未配置短路同一口径。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-dispatch-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n")
        cls._old_env = {k: os.environ.get(k) for k in
                        ("YIBAN_ENV_FILE", "YIBAN_STATE_DIR", "YIBAN_ACCOUNTS_KEY",
                         *NOTIFY_ENV_KEYS)}
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_STATE_DIR"] = cls.tmp
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
        for k in NOTIFY_ENV_KEYS:
            os.environ.pop(k, None)

    @classmethod
    def tearDownClass(cls):
        for k, v in cls._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_true_for_encrypted_serverchan(self):
        os.environ["YIBAN_NOTIFY_TYPE"] = "serverchan"
        os.environ["YIBAN_NOTIFY_SECRET_ENC"] = _notify_enc(SCT_KEY)
        self.assertTrue(notify.is_configured())

    def test_true_for_legacy_plaintext_url(self):
        os.environ.pop("YIBAN_NOTIFY_TYPE", None)
        os.environ.pop("YIBAN_NOTIFY_SECRET_ENC", None)
        os.environ["YIBAN_NOTIFY_URL"] = "https://hook.example.com/abc"
        self.assertTrue(notify.is_configured(), "旧明文 URL 是既有兼容配置，同样算已配置")

    def test_false_when_unset(self):
        for k in NOTIFY_ENV_KEYS:
            os.environ.pop(k, None)
        self.assertFalse(notify.is_configured())

    def test_false_when_secret_unresolvable(self):
        os.environ["YIBAN_NOTIFY_TYPE"] = "serverchan"
        os.environ["YIBAN_NOTIFY_SECRET_ENC"] = "not-a-json-ciphertext"
        self.assertFalse(notify.is_configured(), "类型在而密文解不出 = 通道实际不可用")


class SigninAlertGateTest(unittest.TestCase):
    """signin 即时告警门控与汇总兜底：按推送通道是否真的已配置判定。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-dispatch-signin-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n")
        cls._old_env = {k: os.environ.get(k) for k in
                        ("YIBAN_ENV_FILE", "YIBAN_STATE_DIR", "YIBAN_DB_FILE",
                         *NOTIFY_ENV_KEYS)}
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_STATE_DIR"] = cls.tmp
        os.environ["YIBAN_DB_FILE"] = os.path.join(cls.tmp, "yiban.db")
        for k in NOTIFY_ENV_KEYS:
            os.environ.pop(k, None)
        global signin
        import signin

    @classmethod
    def tearDownClass(cls):
        for k, v in cls._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        signin._mail_summary.clear()

    def tearDown(self):
        signin._mail_summary.clear()

    def test_slow_sign_alert_fires_only_when_push_configured(self):
        """_alert_slow_sign 的即时推送按 is_configured 门控（不再看死键 notify_url）。"""
        for configured, want_calls in ((True, 1), (False, 0)):
            with self.subTest(configured=configured):
                signin._mail_summary.clear()
                with mock.patch.object(signin.notify, "is_configured",
                                       return_value=configured), \
                     mock.patch.object(signin.notify, "send") as m_send:
                    signin._alert_slow_sign("13800138001", 30.0, 20, "ok", "有点慢", "")
                self.assertEqual(m_send.call_count, want_calls)
                if want_calls:
                    self.assertEqual(m_send.call_args.args[0], "易班签到耗时告警")
                self.assertEqual(len(signin._mail_summary), 1, "汇总邮件收集不受推送门控影响")

    def test_no_dead_notify_url_gate_remains_in_source(self):
        """源级断言：即时告警门不得再挂在 YIBAN_NOTIFY_URL 死键上。

        门随执行码迁进了引擎（`yiban/engine/round.py` 的放弃分支、`alerts.py` 的耗时
        告警），兼容壳 `scripts/signin.py` 只剩转发——因此扫整个引擎目录加壳，
        而不是只看旧入口文件。
        """
        src = ""
        engine_dir = os.path.join(BASE, "yiban", "engine")
        for name in sorted(os.listdir(engine_dir)):
            if not name.endswith(".py"):
                continue
            with open(os.path.join(engine_dir, name), encoding="utf-8") as f:
                src += f.read()
        with open(os.path.join(BASE, "scripts", "signin.py"), encoding="utf-8") as f:
            src += f.read()
        self.assertNotIn("if notify_url:", src,
                         "生产无 YIBAN_NOTIFY_URL，该门 = 告警永久哑火")
        self.assertIn("notify.is_configured()", src)

    def test_empty_recipients_summary_pushes_once_when_push_configured(self):
        """收件人集为空且推送已配置：同一份汇总改走推送恰好一次（urgent+force）。"""
        signin._collect_admin_mail("当日签到异常告警", "零成功且窗口外")
        with mock.patch.object(signin.db, "admin_mail_recipients", return_value=[]), \
             mock.patch.object(signin.mailer, "send_admin_alert") as m_mail, \
             mock.patch.object(signin.notify, "is_configured", return_value=True), \
             mock.patch.object(signin.notify, "send", return_value=True) as m_send, \
             self.assertLogs("yiban", level="WARNING") as logs:
            signin._flush_admin_mail_summary()
        m_mail.assert_not_called()
        m_send.assert_called_once()
        self.assertEqual(m_send.call_args.args[0], "易班签到汇总")
        self.assertTrue(m_send.call_args.args[1].startswith("邮件无可用收件人，改推："))
        self.assertTrue(m_send.call_args.kwargs.get("urgent"))
        self.assertTrue(m_send.call_args.kwargs.get("force"))
        self.assertTrue(any("无可用收件人" in msg for msg in logs.output),
                        "warning 留痕在改推兜底下仍须保留")
        self.assertEqual(signin._mail_summary, [])

    def test_empty_recipients_without_push_stays_warning_only(self):
        """收件人集为空且推送也未配置：无事可做，仅 warning 留痕。"""
        signin._collect_admin_mail("当日签到异常告警", "零成功且窗口外")
        with mock.patch.object(signin.db, "admin_mail_recipients", return_value=[]), \
             mock.patch.object(signin.mailer, "send_admin_alert") as m_mail, \
             mock.patch.object(signin.notify, "is_configured", return_value=False), \
             mock.patch.object(signin.notify, "send") as m_send, \
             self.assertLogs("yiban", level="WARNING") as logs:
            signin._flush_admin_mail_summary()
        m_mail.assert_not_called()
        m_send.assert_not_called()
        self.assertTrue(any("无可用收件人" in msg for msg in logs.output))
        self.assertEqual(signin._mail_summary, [])


PROD_STALE_MSG = "获取签到任务失败: 未登录或登录已经超时"


class LoginAlertUrgencyTest(_B14AlertGateBase):
    """R2：只有喷洒特征才升级紧急。"""

    def setUp(self):
        super().setUp()
        # 2026-09-01 性能修复：登录失败用例每次都走 scrypt 时延拉平
        # （_constant_time_dummy / check_password_hash，安全设计约 0.6s/次），
        # spray 用例 10 次请求 ≈ 6.6s。本类被测对象是「告警分级与喷洒识别」，
        # 与密码校验结果无关——统一 patch 掉 scrypt 为常数开销。
        # 注：patch 对象是 self.webapp（模块名 "webapp"，非 "web.app"）。
        p1 = mock.patch.object(self.webapp, "_constant_time_dummy", lambda pwd: None)
        p1.start()
        self.addCleanup(p1.stop)
        # p2 覆盖**注册用户**路径（路由经 `m.check_password_hash` 取 app 侧绑定）。
        p2 = mock.patch.object(self.webapp, "check_password_hash", lambda h, p: False)
        p2.start()
        self.addCleanup(p2.stop)
        # p3 覆盖**内置管理员**路径：`web/security.py` 的 verify_admin 用的是该模块自己
        # 从 werkzeug 导入的 check_password_hash，app 侧绑定到不了它。只打 p2 会让上面
        # 那句"patch 掉 scrypt"对 admin 用户静默失效（本类用例打的全是 "admin" 与不存在
        # 的邮箱，真实 scrypt 正是从 security 侧发出）。打桩目标须落在真正决定校验的那份
        # 绑定上，否则用例看着绿、开销照付。
        p3 = mock.patch.object(web_security, "check_password_hash", lambda h, p: False)
        p3.start()
        self.addCleanup(p3.stop)

    def _alerts(self):
        return [a for a in self.alerts if a[0] == "登录失败告警"]

    def test_below_threshold_sends_nothing(self):
        c = self._client()
        for _ in range(self.webapp.LOGIN_FAIL_NOTIFY - 1):
            c.post("/api/login", json={"username": "admin", "password": "WrongPass#111"})
        self.assertEqual(self._alerts(), [])

    def test_same_user_repeated_mistake_is_not_urgent(self):
        """本人忘密码：连续 3 次输错 → 仍告警（可追溯），但走非紧急账不占手机额度。"""
        c = self._client()
        for _ in range(self.webapp.LOGIN_FAIL_NOTIFY):
            c.post("/api/login", json={"username": "admin", "password": "WrongPass#111"})
        got = self._alerts()
        self.assertEqual(len(got), 1, f"每轮应只告警一次：{got}")
        self.assertFalse(got[0][2], "单账号反复输错不得占用紧急额度")

    def test_spray_across_users_is_urgent(self):
        """同一 IP 打多个用户名且某账号已到阈值 → 撞库特征，升级紧急。"""
        c = self._client()
        users = ["a1@beta.local", "a2@beta.local", "a3@beta.local"]
        for u in users:
            for _ in range(self.webapp.LOGIN_FAIL_NOTIFY - 1):
                c.post("/api/login", json={"username": u, "password": "WrongPass#111"})
        # 第 3 个账号的第 3 次失败触发告警：此时该 IP 已试过 3 个不同用户名
        c.post("/api/login", json={"username": users[-1], "password": "WrongPass#111"})
        got = self._alerts()
        self.assertEqual(len(got), 1, f"仅命中阈值那一次告警：{got}")
        self.assertTrue(got[0][2], "跨账号喷洒必须升级紧急")
        self.assertIn("不同用户名", got[0][1])
        self.assertIn(f"{self.webapp.LOGIN_SPRAY_USERS} 个", got[0][1],
                      "正文须交代升级依据，否则管理员无从判断是不是误报")


class LoginAlertRealChannelTest(_B14AlertGateBase):
    """不替换 send_notification：钉住"降级"改的是账本归属，不是把通知整条跳过。"""

    PATCH_NOTIFY = False

    def test_login_failure_still_reaches_the_notification_layer(self):
        """降级只降"推不推手机"的账本归属，不得在应用层就把通知整条跳过。"""
        c = self._client()
        with mock.patch.object(self.webapp.notify, "send") as send_mock:
            for _ in range(self.webapp.LOGIN_FAIL_NOTIFY):
                c.post("/api/login", json={"username": "admin", "password": "WrongPass#111"})
            self.assertEqual(send_mock.call_count, 1,
                             "非紧急仍须调用 notify.send（是否推手机由通道侧决定）")
            self.assertIs(send_mock.call_args.kwargs.get("urgent"), False,
                          "传入的 urgent 必须与判据一致")


class NewApplicationAlertTest(_B14AlertGateBase):
    """R3：申请入库后管理员必须被通知到。"""

    EMAIL = "beta.tester@qq.com"
    PASSWORD = "BetaUser#2026x"

    def _submit_account(self):
        c = self._client()
        r = c.post("/api/register",
                   json={"email": self.EMAIL, "password": self.PASSWORD, "agree": True})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        token = self._login(c, self.EMAIL, self.PASSWORD)
        return c.post(
            "/api/my-accounts",
            json={"name": "小李的手机", "phone": "13800001234", "password": "Yiban#pw123",
                  "phone_model": "", "phone_code": ""},
            headers=self._csrf(token),
        )

    def test_admin_gets_non_urgent_notice_on_new_application(self):
        r = self._submit_account()
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        got = [a for a in self.alerts if a[0] == "新账号申请待审核"]
        self.assertEqual(len(got), 1, f"新申请须且只须一条告警：{self.alerts}")
        self.assertFalse(got[0][2], "新申请属日常事务，不得占用紧急额度")
        self.assertIn("1234", got[0][1], "正文须含脱敏手机号尾号供管理员定位")
        self.assertNotIn("13800001234", got[0][1], "告警正文不得外泄完整手机号")

    def test_notice_failure_does_not_break_the_submission(self):
        """通知通道炸掉时，已入库的申请仍须返回成功（不得退化成 500 让用户重交）。"""
        c = self._client()
        r = c.post("/api/register",
                   json={"email": self.EMAIL, "password": self.PASSWORD, "agree": True})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        token = self._login(c, self.EMAIL, self.PASSWORD)
        with mock.patch.object(self.webapp, "send_notification",
                               side_effect=RuntimeError("通道炸了")):
            r2 = c.post(
                "/api/my-accounts",
                json={"name": "", "phone": "13800005678", "password": "Yiban#pw123",
                      "phone_model": "", "phone_code": ""},
                headers=self._csrf(token),
            )
        self.assertEqual(r2.status_code, 200, r2.get_data(as_text=True))
        accs = [a for a in self.webapp.load_accounts() if a.get("phone") == "13800005678"]
        self.assertEqual(len(accs), 1, "申请须已入库且状态待审核")
        self.assertEqual(accs[0].get("status"), self.webapp.ACCOUNT_STATUS_PENDING)


if __name__ == "__main__":
    unittest.main()
