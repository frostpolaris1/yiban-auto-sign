# -*- coding: utf-8 -*-
"""SMTP 发信条目列表化（2026-09-07）：smtp_list 回落/ENC 解析、PUT /api/mail-config
smtps 字段、发送 failover、GET 脱敏。

覆盖：
- mailer.smtp_list()：YIBAN_MAIL_SMTPS_ENC 缺失时回落旧单条键（USER+PASS 均非空 →
  单元素；否则 []）；ENC 写入后解密回读列表；解密列表含非 dict 元素被过滤
- PUT /api/mail-config（smtps + confirm_password）：落盘 YIBAN_MAIL_SMTPS_ENC 且可
  解密回读；pass/user 留空按索引保留旧值；无 confirm → 400
- 发送 failover：mock smtplib，首条 SMTPException、次条成功 → send 成功且日志含两次尝试；
  条目 port 非法回退 465 并只记一次告警
- GET /api/mail-config：smtps 条目 has_pass=true、user/admin_to 打码，响应全文不含
  pass 明文与完整邮箱

脚手架照抄 tests/test_batch19_features_0907.py 的 _Base（临时 .env/DB/webapp/_master）。

用法（项目根目录）：
    py -m pytest tests/test_mail_failover_0907.py -v
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
from unittest import mock

import account_crypto
import env_io
import mailer

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TEST_KEY = "a" * 64
ADMIN_PASS = "MasterPass#2026"

# 本文件内可能触碰的 YIBAN_MAIL_* 环境变量（tearDownClass 统一还原，防泄漏到后续测试）
_MAIL_ENV_KEYS = (
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
                f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
                "YIBAN_ADMIN_USER=admin@test.local\n"
                f"YIBAN_ADMIN_PASSWORD={ADMIN_PASS}\n"
            )
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.accounts_file = os.path.join(cls.tmp, "accounts.json")
        cls._old_env = {k: os.environ.get(k) for k in (*_MAIL_ENV_KEYS, "YIBAN_ENV_FILE")}
        for k in _MAIL_ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
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
                f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
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


class SmtpListTest(_Base):
    """smtp_list：旧键回落与 ENC 解析。"""

    def test_fallback_single_entry_from_legacy_keys(self):
        self._reset_env_file("YIBAN_MAIL_USER=sender@qq.com\nYIBAN_MAIL_PASS=secret\n")
        entries = mailer.smtp_list()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["host"], "smtp.qq.com")  # HOST 未配置 → 默认
        self.assertEqual(entries[0]["port"], 465)
        self.assertEqual(entries[0]["user"], "sender@qq.com")
        self.assertEqual(entries[0]["pass"], "secret")

    def test_fallback_empty_when_no_config(self):
        self._reset_env_file()
        self.assertEqual(mailer.smtp_list(), [])

    def test_enc_roundtrip(self):
        self._reset_env_file()
        smtps = [
            {"host": "smtp1.example.com", "port": 465, "user": "a@x.com", "pass": "p1", "admin_to": ""},
            {"host": "smtp2.example.com", "port": 587, "user": "b@x.com", "pass": "p2",
             "admin_to": "boss@x.com"},
        ]
        enc = account_crypto.encrypt_text(
            json.dumps(smtps, ensure_ascii=False), account_crypto.load_key(self.env_file))
        with io.open(self.env_file, "w", encoding="utf-8") as f:
            f.write(
                f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
                f"YIBAN_MAIL_SMTPS_ENC={json.dumps(enc, ensure_ascii=False)}\n"
            )
        self.assertEqual(mailer.smtp_list(), smtps)

    def test_enc_non_dict_entries_filtered(self):
        """解密列表中的非 dict 元素被过滤（脏数据不让调用方 e.get 崩溃）。"""
        self._reset_env_file()
        good = {"host": "smtp.x.com", "port": 465, "user": "a@x.com", "pass": "p1", "admin_to": ""}
        enc = account_crypto.encrypt_text(
            json.dumps(["garbage", good, 42], ensure_ascii=False),
            account_crypto.load_key(self.env_file))
        with io.open(self.env_file, "w", encoding="utf-8") as f:
            f.write(
                f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
                f"YIBAN_MAIL_SMTPS_ENC={json.dumps(enc, ensure_ascii=False)}\n"
            )
        self.assertEqual(mailer.smtp_list(), [good])


class MailConfigSmtpsApiTest(_Base):
    """PUT /api/mail-config smtps 字段：落盘加密、旧 pass/user 保留、口令门禁、GET 脱敏。"""

    def smtps(self):
        return [{"host": "smtp.x.com", "user": "a@x.com", "pass": "topsecret",
                 "admin_to": "boss@x.com"}]

    def test_put_smtps_writes_enc_and_roundtrips(self):
        self._reset_env_file()
        c, h = self._master()
        r = c.put("/api/mail-config",
                  json={"smtps": self.smtps(), "confirm_password": ADMIN_PASS}, headers=h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        entries = self._read_enc_entries()
        self.assertEqual(entries, [{
            "host": "smtp.x.com", "port": 465, "user": "a@x.com",
            "pass": "topsecret", "admin_to": "boss@x.com",
        }])

    def test_put_smtps_empty_pass_keeps_old(self):
        self._reset_env_file()
        c, h = self._master()
        r = c.put("/api/mail-config",
                  json={"smtps": self.smtps(), "confirm_password": ADMIN_PASS}, headers=h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        # 再次保存：host/user 不变、pass 留空 → 旧 pass 保留
        r = c.put("/api/mail-config",
                  json={"smtps": [{"host": "smtp.x.com", "user": "a@x.com", "pass": ""}],
                        "confirm_password": ADMIN_PASS},
                  headers=h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        entries = self._read_enc_entries()
        self.assertEqual(entries[0]["pass"], "topsecret", "pass 留空应保留旧授权码")

    def test_put_smtps_empty_user_keeps_old(self):
        self._reset_env_file()
        c, h = self._master()
        r = c.put("/api/mail-config",
                  json={"smtps": self.smtps(), "confirm_password": ADMIN_PASS}, headers=h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        # 再次保存：GET 已打码（不回显完整地址），user 留空 → 旧 user 保留
        r = c.put("/api/mail-config",
                  json={"smtps": [{"host": "smtp.x.com", "user": "", "pass": ""}],
                        "confirm_password": ADMIN_PASS},
                  headers=h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        entries = self._read_enc_entries()
        self.assertEqual(entries[0]["user"], "a@x.com", "user 留空应保留旧发件账号")

    def test_put_smtps_requires_confirm_password(self):
        self._reset_env_file()
        c, h = self._master()
        r = c.put("/api/mail-config", json={"smtps": self.smtps()}, headers=h)
        self.assertEqual(r.status_code, 400)

    def test_put_smtps_invalid_entries_400(self):
        self._reset_env_file()
        c, h = self._master()
        for bad in (
            {"host": "", "user": "a@x.com"},
            {"host": "smtp.x.com", "user": "a@x.com", "port": 70000},
            {"host": "smtp.x.com", "user": "a@x.com", "port": "abc"},
        ):
            r = c.put("/api/mail-config",
                      json={"smtps": [bad], "confirm_password": ADMIN_PASS}, headers=h)
            self.assertEqual(r.status_code, 400, bad)
        r = c.put("/api/mail-config", json={"smtps": "not-a-list",
                                            "confirm_password": ADMIN_PASS}, headers=h)
        self.assertEqual(r.status_code, 400)

    def test_put_smtps_null_fields_not_persisted_as_none(self):
        """JSON null 入参兜底：host=null → 400；user/pass=null 视为留空（按索引保留旧值），
        均不得经 str(None) 落盘为 "None"。"""
        self._reset_env_file()
        c, h = self._master()
        r = c.put("/api/mail-config",
                  json={"smtps": self.smtps(), "confirm_password": ADMIN_PASS}, headers=h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        # host=null：修复前 str(None)="None" 非空会通过校验并落盘，必须 400
        r = c.put("/api/mail-config",
                  json={"smtps": [{"host": None, "user": "a@x.com"}],
                        "confirm_password": ADMIN_PASS},
                  headers=h)
        self.assertEqual(r.status_code, 400, "host=null 应拒绝（不得落盘为 \"None\"）")
        # user/pass=null：视为留空 → 按索引保留旧条目的 user/pass
        r = c.put("/api/mail-config",
                  json={"smtps": [{"host": "smtp.x.com", "user": None, "pass": None}],
                        "confirm_password": ADMIN_PASS},
                  headers=h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        entries = self._read_enc_entries()
        self.assertEqual(entries[0]["user"], "a@x.com", "user=null 应视为留空并保留旧值")
        self.assertEqual(entries[0]["pass"], "topsecret", "pass=null 应视为留空并保留旧值")
        self.assertNotIn("None", (entries[0]["host"], entries[0]["user"]),
                         "任何字段都不得落盘为字符串 \"None\"")

    def test_get_smtps_masking_and_never_echoes_pass(self):
        self._reset_env_file()
        c, h = self._master()
        r = c.put("/api/mail-config",
                  json={"smtps": self.smtps(), "confirm_password": ADMIN_PASS}, headers=h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        r = c.get("/api/mail-config", headers=h)
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertEqual(len(data["smtps"]), 1)
        self.assertEqual(data["smtps"][0]["host"], "smtp.x.com")
        # user/admin_to 打码（与顶层字段同口径）：不含完整邮箱
        self.assertEqual(data["smtps"][0]["user"], "a***@x.com")
        self.assertEqual(data["smtps"][0]["admin_to"], "bos***@x.com")
        self.assertTrue(data["smtps"][0]["has_pass"])
        self.assertNotIn("pass", data["smtps"][0], "pass 键不得出现在响应条目中")
        body = r.get_data(as_text=True)
        self.assertNotIn("topsecret", body, "响应全文不得含 pass 明文")
        self.assertNotIn("a@x.com", body, "响应全文不得含完整发件账号")
        self.assertNotIn("boss@x.com", body, "响应全文不得含完整 admin_to 地址")


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
            self.assertTrue(mailer.send_user("to@x.com", "subject", "body"))
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
            self.assertTrue(mailer.send_user("to@x.com", "subject", "body"))
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
            self.assertFalse(mailer.send_user("to@x.com", "subject", "body"))

    def test_invalid_entry_port_falls_back_and_warns_once(self):
        """条目 port 非法：发送回退 465 并只记一次告警（第二次发送不再告警）。"""
        self._reset_env_file()
        smtps = [{"host": "smtp.x.com", "port": "abc", "user": "a@x.com",
                  "pass": "p1", "admin_to": ""}]
        enc = account_crypto.encrypt_text(
            json.dumps(smtps, ensure_ascii=False), account_crypto.load_key(self.env_file))
        self._reset_env_file(f"YIBAN_MAIL_SMTPS_ENC={json.dumps(enc, ensure_ascii=False)}\n")
        os.environ["YIBAN_MAIL_ENABLE"] = "1"
        mailer._entry_port_warned = False  # 模块级一次性旗，测试间复位

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
            self.assertTrue(mailer.send_user("to@x.com", "subject", "body"))
        self.assertEqual(m.call_args[0][1], 465, "非法端口应回退 465 发送")
        self.assertIn("不是合法端口", "\n".join(logs.output), "应记端口告警")
        with mock.patch("smtplib.SMTP_SSL", side_effect=FakeOK), \
             self.assertNoLogs("mailer", level="WARNING"):
            self.assertTrue(mailer.send_user("to@x.com", "subject", "body"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
