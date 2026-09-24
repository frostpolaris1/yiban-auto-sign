# -*- coding: utf-8 -*-
"""SMTP 目标的地址判据与发送侧日志粒度。

标签：H · 通知：邮件与推送
覆盖：`yiban.mail.config.check_smtp_host`——非路由/保留段（链路本地、CGNAT 元数据、
    组播、保留、未指定）一律拒；私网与回环（RFC1918 + 127/8 + ::1）无开关时拒、
    开关为 1/true/ON 时放行；公网域名与公网 IP 放行；方括号 IPv6 与 IPv4-mapped
    解析；`localhost` 与非标准 IPv4 字面量（十进制/0x/短式）拒。发送失败日志的
    粗分类与内网目标 WARNING 的进程内一次性。
对应实现：`yiban/mail/config.py`（`check_smtp_host`）、`yiban/mail/transport.py`
    （发送与日志）。
关键断言：日志回显"连接失败/认证失败"文案但**不含原始异常类型名**
    （`ConnectionRefusedError`、`SMTPAuthenticationError`）；内网目标的 WARNING
    只记一次且**不阻断发送**——判据说的是"这台机器该不该连它"，不是替用户决定不连。
依赖：mock `smtplib.SMTP_SSL`（十余处）与 caplog，地址判定走 `ipaddress` 纯计算；
    不真连 SMTP、不触网、不需 DNS。
"""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import smtplib
import sys
import tempfile
import unittest
from typing import ClassVar
from unittest import mock

from yiban.infra import account_crypto, env_io
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


BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


TEST_KEY_FAILOVER = "a" * 64


ADMIN_PASS = "MasterPass#2026"


_MAIL_ENV_KEYS_FAILOVER = (
    "YIBAN_MAIL_ENABLE", "YIBAN_MAIL_SMTPS_ENC", "YIBAN_MAIL_USER", "YIBAN_MAIL_PASS",
    "YIBAN_MAIL_SMTP_HOST", "YIBAN_MAIL_SMTP_PORT", "YIBAN_MAIL_ADMIN_TO",
)


def _load_webapp(tag):
    import db as _db
    spec = importlib.util.spec_from_file_location(f"webapp_{tag}", os.path.join(BASE, "web", "app.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"webapp_{tag}"] = mod
    with contextlib.suppress(Exception):
        spec.loader.exec_module(mod)
    return _db, mod


class _Base(unittest.TestCase):
    """共享脚手架：临时 .env/DB + webapp 加载 + 主管理员登录（照抄 batch19）。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-failover-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with io.open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(
                f"YIBAN_ACCOUNTS_KEY={TEST_KEY_FAILOVER}\n"
                "YIBAN_ADMIN_USER=admin@test.local\n"
                f"YIBAN_ADMIN_PASSWORD={ADMIN_PASS}\n"
            )
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.accounts_file = os.path.join(cls.tmp, "accounts.json")
        cls._old_env = {k: os.environ.get(k) for k in (*_MAIL_ENV_KEYS_FAILOVER, "YIBAN_ENV_FILE")}
        for k in _MAIL_ENV_KEYS_FAILOVER:
            os.environ.pop(k, None)
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY_FAILOVER
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_ACCOUNTS_FILE"] = cls.accounts_file
        os.environ["YIBAN_DB_FILE"] = cls.db_file
        os.environ["YIBAN_STATE_DIR"] = cls.tmp
        os.environ["YIBAN_LOG_FILE"] = os.path.join(cls.tmp, "sign.log")
        cls.db, cls.webapp = _load_webapp(cls.__name__)

    @classmethod
    def tearDownClass(cls):
        if cls.db._conn is not None:
            with contextlib.suppress(Exception):
                cls.db._conn.close()
            cls.db._conn = None
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k, v in cls._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def setUp(self):
        if self.db._conn is not None:
            with contextlib.suppress(Exception):
                self.db._conn.close()
            self.db._conn = None
        for suffix in ("", "-wal", "-shm"):
            p = self.db_file + suffix
            if os.path.exists(p):
                os.remove(p)
        self.db.init_db(self.db_file, migrate_from=self.accounts_file, env_file=self.env_file)

    def _reset_env_file(self, extra=""):
        with io.open(self.env_file, "w", encoding="utf-8") as f:
            f.write(
                f"YIBAN_ACCOUNTS_KEY={TEST_KEY_FAILOVER}\n"
                "YIBAN_ADMIN_USER=admin@test.local\n"
                f"YIBAN_ADMIN_PASSWORD={ADMIN_PASS}\n" + extra
            )

    def _master(self):
        c = self.webapp.create_app().test_client()
        r = c.post("/api/login", json={"username": "admin@test.local", "password": ADMIN_PASS})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        t = c.get("/api/me").get_json()["csrf_token"]
        return c, {"X-CSRF-Token": t}

    def _read_enc_entries(self):
        """从 .env 读出 YIBAN_MAIL_SMTPS_ENC 并解密回列表（独立于 mailer 校验落盘格式）。"""
        raw = env_io.parse_env_file(self.env_file).get("YIBAN_MAIL_SMTPS_ENC", "")
        self.assertTrue(raw, "应写入 YIBAN_MAIL_SMTPS_ENC")
        entry = json.loads(raw)
        plain = account_crypto.decrypt_text(entry, account_crypto.load_key(self.env_file))
        return json.loads(plain)


class MailFailoverTest(_Base):
    """发送 failover：首条失败 → 尝试下一条；全败 → 失败；条目 port 非法回退 465 记一次性告警。"""

    def _write_two_entries(self):
        smtps = [
            {"host": "down.example.com", "port": 465, "user": "a@x.com", "pass": "p1", "admin_to": ""},
            {"host": "ok.example.com", "port": 465, "user": "b@x.com", "pass": "p2", "admin_to": ""},
        ]
        enc = account_crypto.encrypt_text(
            json.dumps(smtps, ensure_ascii=False), account_crypto.load_key(self.env_file))
        self._reset_env_file(f"YIBAN_MAIL_SMTPS_ENC={json.dumps(enc, ensure_ascii=False)}\n")

    def test_failover_second_entry_succeeds(self):
        self._write_two_entries()
        os.environ["YIBAN_MAIL_ENABLE"] = "1"
        constructed = []

        class FakeOK:
            def __init__(self, *a, **k):
                self.creds = None

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def login(self, u, p):
                self.creds = (u, p)

            def sendmail(self, frm, to, msg):
                pass

        def factory(*a, **k):
            # 首条构造即抛 SMTPException，次条返回可用服务（模拟主备 failover）
            constructed.append("down" if not constructed else "ok")
            if constructed[-1] == "down":
                raise smtplib.SMTPException("connection down")
            return FakeOK()

        with mock.patch("smtplib.SMTP_SSL", side_effect=factory):
            # send_user → _send：第 1 条 SMTPException → 第 2 条成功
            self.assertTrue(mailer_transport.send_user("to@x.com", "subject", "body"))
        self.assertEqual(constructed, ["down", "ok"], "应按顺序尝试两条 SMTP 条目")

    def test_failover_logs_both_attempts_and_no_credentials(self):
        self._write_two_entries()
        os.environ["YIBAN_MAIL_ENABLE"] = "1"

        class FakeOK:
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

        def factory(*a, **k):
            if not getattr(factory, "failed", False):
                factory.failed = True
                raise smtplib.SMTPException("connection down")
            return FakeOK()

        with mock.patch("smtplib.SMTP_SSL", side_effect=factory) as m, \
             self.assertLogs("mailer", level="WARNING") as logs:
            self.assertTrue(mailer_transport.send_user("to@x.com", "subject", "body"))
        self.assertEqual(m.call_count, 2, "两次尝试（首条失败 + 次条成功）")
        joined = "\n".join(logs.output)
        self.assertIn("1/2", joined, "失败日志应含条目序号")
        self.assertIn("down.example.com", joined)
        self.assertNotIn("p1", joined, "日志不得含授权码")
        self.assertNotIn("p2", joined, "日志不得含授权码")

    def test_all_entries_fail_returns_false(self):
        self._write_two_entries()
        os.environ["YIBAN_MAIL_ENABLE"] = "1"

        class Boom:
            def __init__(self, *a, **k):
                raise OSError("network unreachable")

        with mock.patch("smtplib.SMTP_SSL", Boom), \
             self.assertLogs("mailer", level="WARNING"):
            self.assertFalse(mailer_transport.send_user("to@x.com", "subject", "body"))

    def test_invalid_entry_port_falls_back_and_warns_once(self):
        """条目 port 非法：发送回退 465 并只记一次告警（第二次发送不再告警）。"""
        self._reset_env_file()
        smtps = [{"host": "smtp.x.com", "port": "abc", "user": "a@x.com",
                  "pass": "p1", "admin_to": ""}]
        enc = account_crypto.encrypt_text(
            json.dumps(smtps, ensure_ascii=False), account_crypto.load_key(self.env_file))
        self._reset_env_file(f"YIBAN_MAIL_SMTPS_ENC={json.dumps(enc, ensure_ascii=False)}\n")
        os.environ["YIBAN_MAIL_ENABLE"] = "1"
        mailer_transport._entry_port_warned = False  # 模块级一次性旗，测试间复位

        class FakeOK:
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

        with mock.patch("smtplib.SMTP_SSL", side_effect=FakeOK) as m, \
             self.assertLogs("mailer", level="WARNING") as logs:
            self.assertTrue(mailer_transport.send_user("to@x.com", "subject", "body"))
        self.assertEqual(m.call_args[0][1], 465, "非法端口应回退 465 发送")
        self.assertIn("不是合法端口", "\n".join(logs.output), "应记端口告警")
        with mock.patch("smtplib.SMTP_SSL", side_effect=FakeOK), \
             self.assertNoLogs("mailer", level="WARNING"):
            self.assertTrue(mailer_transport.send_user("to@x.com", "subject", "body"))


class MailChannelStateReportTest(_Base):
    """日报邮件通道行由三态 smtp_channel_state 渲染：「已开启」只给 ok，broken 带病因。"""

    def _lines(self):
        return "\n".join(self.webapp._channel_status_lines())

    def test_ok_channel_reports_enabled(self):
        self._reset_env_file("YIBAN_MAIL_ADMIN_TO=admin@test.local\n")
        with mock.patch.dict(os.environ, {"YIBAN_MAIL_ENABLE": "1",
                                          "YIBAN_MAIL_USER": "alert@test.local",
                                          "YIBAN_MAIL_PASS": "smtp-auth-code-fake"}):
            lines = self._lines()
        self.assertIn("邮件通道：已开启", lines)
        self.assertNotIn("已开启但不可用", lines)

    def test_broken_undecryptable_blob_names_the_cause(self):
        """密文解不开且旧键为空：日报须点名病因并标 ⚠，不得报「已开启」。"""
        self._reset_env_file("YIBAN_MAIL_SMTPS_ENC=not-a-json-ciphertext\n")
        with mock.patch.dict(os.environ, {"YIBAN_MAIL_ENABLE": "1"}):
            lines = self._lines()
        self.assertIn("邮件通道：⚠ 已开启但不可用", lines)
        self.assertIn("无法解密", lines)

    def test_broken_entries_missing_credentials_names_the_cause(self):
        """条目缺发件账号/授权码：日报须点名病因（与"未配置"区分处置）。"""
        enc = account_crypto.encrypt_text(
            json.dumps([{"host": "smtp.x.com", "port": 465, "user": "", "pass": ""}],
                       ensure_ascii=False),
            account_crypto.load_key(self.env_file))
        self._reset_env_file(
            f"YIBAN_MAIL_SMTPS_ENC={json.dumps(enc, ensure_ascii=False)}\n")
        with mock.patch.dict(os.environ, {"YIBAN_MAIL_ENABLE": "1"}):
            lines = self._lines()
        self.assertIn("邮件通道：⚠ 已开启但不可用", lines)
        self.assertIn("缺发件账号/授权码", lines)

    def test_empty_decrypted_list_reports_cleared_not_key_mismatch(self):
        """解密成功但列表为空（端点允许 smtps: [] 的合法清空）→ 病因报「未配置/已清空」。

        与"密文解不开"必须区分：清空列表是设置页的合法操作，误诊成
        「换钥后密钥不匹配」会把 ops 引去排查密钥。三态仍为 broken
        （邮件确实一封发不出），只是 detail 不得指向密钥。
        """
        enc = account_crypto.encrypt_text(
            json.dumps([], ensure_ascii=False), account_crypto.load_key(self.env_file))
        self._reset_env_file(
            f"YIBAN_MAIL_SMTPS_ENC={json.dumps(enc, ensure_ascii=False)}\n")
        with mock.patch.dict(os.environ, {"YIBAN_MAIL_ENABLE": "1"}):
            state, detail = mailer.smtp_channel_state()
        self.assertEqual(state, "broken")
        self.assertIn("未配置", detail)
        self.assertNotIn("换钥", detail, "合法清空不得误诊为密钥失配")
        self.assertNotIn("无法解密", detail)


if __name__ == "__main__":
    unittest.main(verbosity=2)
