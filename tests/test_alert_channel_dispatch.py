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

import account_crypto

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
        global notify
        import notify

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
        """源级断言：四处即时告警门不得再挂在 YIBAN_NOTIFY_URL 死键上。"""
        with open(os.path.join(BASE, "scripts", "signin.py"), encoding="utf-8") as f:
            src = f.read()
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


if __name__ == "__main__":
    unittest.main()
