# -*- coding: utf-8 -*-
"""SMTP 目标的地址判据与发送侧日志粒度测试。

覆盖：
- `yiban.mail.config.check_smtp_host`：非路由/保留段（链路本地、CGNAT 元数据、组播、
  保留、未指定）一律拒；私网/回环（RFC1918 + 127/8 + ::1）无开关拒、开关为
  1/true/ON 时放行；公网域名与公网 IP 放行；方括号 IPv6 与 IPv4-mapped 正确解析；
  `localhost` 与非标准 IPv4 字面量（十进制/0x/短式）拒。
- 发送失败日志的粗分类：回显连接失败/认证失败文案，且**不含**原始异常类型名
  （ConnectionRefusedError / SMTPAuthenticationError）。
- 内网目标发送侧 WARNING 进程内只记一次，且不阻断发送（仍会尝试 SMTP）。

全程本地（mock smtplib），无真实网络请求。
用法（项目根目录）：
    .venv/Scripts/python.exe -m pytest tests/test_mail_smtp_target.py -q
"""
import io
import os
import shutil
import smtplib
import tempfile
import unittest
from typing import ClassVar
from unittest import mock

from yiban.mail import config as mailer
from yiban.mail import transport as mailer_transport

TEST_KEY = "c" * 64

# 本文件触碰的进程环境键（setUpClass 统一清理，防泄漏进同批其它测试）
_MAIL_ENV_KEYS = (
    "YIBAN_MAIL_ENABLE", "YIBAN_MAIL_SMTPS_ENC", "YIBAN_MAIL_USER", "YIBAN_MAIL_PASS",
    "YIBAN_MAIL_SMTP_HOST", "YIBAN_MAIL_SMTP_PORT", "YIBAN_MAIL_ADMIN_TO",
    "YIBAN_MAIL_ALLOW_PRIVATE_HOST",
)
_OLD_ENV = {}


class SmtpHostPredicateTest(unittest.TestCase):
    """check_smtp_host 三档判据（env 注入，绝不读进程 .env）。"""

    # 显式空映射：开关为假且不回落 .env，保证用例与运行环境无关
    ENV_OFF: ClassVar[dict] = {}

    def _reason(self, host, env=None):
        return mailer.check_smtp_host(host, self.ENV_OFF if env is None else env)

    # ---- 第 1 档：非路由/保留段一律拒（开关也救不回）----
    def test_nonroutable_always_rejected(self):
        for host in ("169.254.169.254", "100.100.100.200", "224.0.0.1",
                     "240.0.0.1", "0.0.0.0", "fe80::1"):
            with self.subTest(host=host):
                self.assertIsNotNone(self._reason(host), f"{host} 属不可路由段，必须拒")
                self.assertIsNotNone(
                    self._reason(host, {"YIBAN_MAIL_ALLOW_PRIVATE_HOST": "1"}),
                    f"{host} 即使开启私网开关也必须拒",
                )

    # ---- 第 2 档：私网/回环，默认拒、开关放行 ----
    def test_private_rejected_without_flag(self):
        for host in ("10.0.0.1", "172.16.0.1", "192.168.1.1", "127.0.0.1"):
            with self.subTest(host=host):
                self.assertIsNotNone(self._reason(host), f"{host} 无开关必须拒")

    def test_private_allowed_with_flag_literals(self):
        for literal in ("1", "true", "ON"):
            with self.subTest(literal=literal):
                env = {"YIBAN_MAIL_ALLOW_PRIVATE_HOST": literal}
                for host in ("10.0.0.1", "172.16.0.1", "192.168.1.1", "127.0.0.1"):
                    self.assertIsNone(self._reason(host, env),
                                      f"开关={literal} 时 {host} 应放行")

    # ---- 第 3 档：公网域名/公网 IP 放行 ----
    def test_public_domain_allowed(self):
        self.assertIsNone(self._reason("smtp.example.com"))

    def test_public_ip_allowed(self):
        # 192.0.0.9 是 RFC 7600 的 IPv4 dummy address（非真实主机），ipaddress 视为 global
        self.assertIsNone(self._reason("192.0.0.9"))

    # ---- 方括号 / IPv4-mapped / 伪装写法 ----
    def test_bracketed_ipv6(self):
        self.assertIsNotNone(self._reason("[::1]"), "带方括号的 :::1 必须解析为回环并拒")
        self.assertIsNone(
            self._reason("[::1]", {"YIBAN_MAIL_ALLOW_PRIVATE_HOST": "true"}),
            "开关开启时 [::1] 放行",
        )
        # 2001:20::/28 是 ORCHIDv2（非真实主机），ipaddress 视为 global
        self.assertIsNone(self._reason("[2001:20::1]"), "公网 IPv6 放行")

    def test_ipv4_mapped_loopback_treated_as_v4(self):
        self.assertIsNotNone(self._reason("::ffff:127.0.0.1"), "mapped 回环必须按 v4 拒")
        self.assertIsNone(
            self._reason("::ffff:127.0.0.1", {"YIBAN_MAIL_ALLOW_PRIVATE_HOST": "yes"}),
            "开关开启时 mapped 回环放行",
        )

    def test_localhost_and_literal_forms_rejected(self):
        for host in ("localhost", "localhost.", "127.0.0.1.", "2130706433",
                     "0x7f000001", "127.1"):
            with self.subTest(host=host):
                self.assertIsNotNone(self._reason(host), f"{host} 形同回环，必须拒")

    def test_empty_host_rejected(self):
        self.assertIsNotNone(self._reason(""))
        self.assertIsNotNone(self._reason("   "))


class _TransportBase(unittest.TestCase):
    """发送侧测试脚手架：隔离 .env + 复位模块级一次性旗。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-smtp-target-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with io.open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n")
        _OLD_ENV["YIBAN_ENV_FILE"] = os.environ.get("YIBAN_ENV_FILE")
        for k in _MAIL_ENV_KEYS:
            _OLD_ENV[k] = os.environ.pop(k, None)
        os.environ["YIBAN_ENV_FILE"] = cls.env_file

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k, v in _OLD_ENV.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def setUp(self):
        mailer_transport._private_target_warned = False
        mailer_transport._entry_port_warned = False
        for k in _MAIL_ENV_KEYS:
            os.environ.pop(k, None)

    def _enable(self, host="smtp.example.com", port="465"):
        os.environ["YIBAN_MAIL_ENABLE"] = "1"
        os.environ["YIBAN_MAIL_USER"] = "sender@example.com"
        os.environ["YIBAN_MAIL_PASS"] = "auth-code-fake"
        os.environ["YIBAN_MAIL_SMTP_HOST"] = host
        os.environ["YIBAN_MAIL_SMTP_PORT"] = port


class SendFailureLogGranularityTest(_TransportBase):
    """失败日志只留粗分类，不留异常类型名（防端口扫描指纹）。"""

    def test_connection_error_not_leaked(self):
        self._enable()

        class Refused:
            def __init__(self, *a, **k):
                raise ConnectionRefusedError("connection refused")

        with mock.patch("smtplib.SMTP_SSL", Refused), \
             self.assertLogs("mailer", level="WARNING") as logs:
            self.assertFalse(mailer_transport.send_user("to@example.com", "s", "b"))
        joined = "\n".join(logs.output)
        self.assertIn("连接失败", joined, "应给出连接失败粗分类")
        self.assertNotIn("ConnectionRefusedError", joined, "不得回显原始异常类型名")

    def test_auth_error_not_leaked(self):
        self._enable()

        class AuthBoom:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def login(self, u, p):
                raise smtplib.SMTPAuthenticationError(535, b"auth failed")

            def sendmail(self, *a):
                pass

        with mock.patch("smtplib.SMTP_SSL", AuthBoom), \
             self.assertLogs("mailer", level="WARNING") as logs:
            self.assertFalse(mailer_transport.send_user("to@example.com", "s", "b"))
        joined = "\n".join(logs.output)
        self.assertIn("认证失败", joined, "应给出认证失败粗分类")
        self.assertNotIn("SMTPAuthenticationError", joined, "不得回显原始异常类型名")


class PrivateTargetWarningTest(_TransportBase):
    """内网目标：进程内只记一次 WARNING，且不阻断发送。"""

    class _FakeOK:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def login(self, u, p):
            pass

        def sendmail(self, frm, to, msg):
            pass

    def test_internal_target_warns_once_but_still_sends(self):
        self._enable(host="127.0.0.1")
        with mock.patch("smtplib.SMTP_SSL", side_effect=self._FakeOK) as m, \
             self.assertLogs("mailer", level="WARNING") as logs:
            self.assertTrue(mailer_transport.send_user("to@example.com", "s", "b"))
        self.assertEqual(m.call_count, 1, "内网目标不得被静默停发，仍应尝试 SMTP")
        joined = "\n".join(logs.output)
        self.assertIn("内网/保留地址", joined, "应提示目标属内网/保留地址")
        self.assertIn("YIBAN_MAIL_ALLOW_PRIVATE_HOST", joined, "应给出显式开关名")

        # 第二次发送：同一进程内不再重复告警，但仍继续尝试发送
        with mock.patch("smtplib.SMTP_SSL", side_effect=self._FakeOK) as m2, \
             self.assertNoLogs("mailer", level="WARNING"):
            self.assertTrue(mailer_transport.send_user("to@example.com", "s", "b"))
        self.assertEqual(m2.call_count, 1, "第二次仍应尝试发送")

    def test_allow_private_flag_suppresses_warning(self):
        self._enable(host="127.0.0.1")
        os.environ["YIBAN_MAIL_ALLOW_PRIVATE_HOST"] = "true"
        with mock.patch("smtplib.SMTP_SSL", side_effect=self._FakeOK), \
             self.assertNoLogs("mailer", level="WARNING"):
            self.assertTrue(mailer_transport.send_user("to@example.com", "s", "b"))

    def test_public_target_no_warning(self):
        self._enable(host="smtp.example.com")
        with mock.patch("smtplib.SMTP_SSL", side_effect=self._FakeOK), \
             self.assertNoLogs("mailer", level="WARNING"):
            self.assertTrue(mailer_transport.send_user("to@example.com", "s", "b"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
