# -*- coding: utf-8 -*-
"""`transport._send` 的"静默失败"契约回归：坏字符不再把异常抛回锁内调用方。

标签：H · 通知：邮件与推送
覆盖：正文/主题携带**孤立代理对**（utf-8 编码必然失败）时 `_send` 只记日志、返回
    False，绝不抛异常；构造级失败**不**逐条目重试（换条目重投的是同一封，必然同样
    失败）；运输级失败（连接/SMTP 异常）的主备 failover 行为逐字不变。
对应实现：`yiban/mail/transport.py::_send`（排版定稿进 try + MIME 构造进 try +
    Exception 收口分支）。
关键断言：本层既有契约"发送异常只记日志、绝不抛出"（`yiban/mail/__init__.py` 模块
    头）此前对 UnicodeEncodeError 不成立——排版/构造在 try 外，一次坏字符会沿
    `send_user`/`send_notification` 抛回**持 `_file_lock` 同步发信**的路由（锁内 500，
    且把排在其后的审计留痕一起带走；`ba-p05-01` 起锁内发信改走 `run_after_file_lock`，
    本契约照旧承重——其余调用方仍按"不抛出"收口）。修复后同输入必须返回 False 且不触 SMTP。
依赖：mock `config.is_enabled/_get/smtp_list` 与 `smtplib.SMTP_SSL`；不触网、
    不发真实邮件、不落盘。
"""
import contextlib
import unittest
from unittest import mock

from yiban.mail import config as mailer
from yiban.mail import transport as mailer_transport

_LONE_SURROGATE = "\ud800"  # 孤立高代理：任何 utf-8 encode 在此必抛 UnicodeEncodeError


def _entry(host="smtp.example.com"):
    return {"host": host, "port": 465, "user": "a@example.com", "pass": "secret"}


class _NeverReachedSMTP:
    """被实例化即记一次；测试据此断言"构造级失败不得触碰 SMTP"。"""

    calls = 0

    def __init__(self):
        _NeverReachedSMTP.calls += 1

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def login(self, u, p):
        pass

    def sendmail(self, f, t, msg):
        pass


class SendNeverRaisesTest(unittest.TestCase):
    def tearDown(self):
        _NeverReachedSMTP.calls = 0

    def _send_with_body(self, subject, body):
        with mock.patch.object(mailer, "is_enabled", lambda: True), \
             mock.patch.object(mailer, "smtp_list", lambda: [_entry()]), \
             mock.patch.object(mailer_transport.smtplib, "SMTP_SSL", _NeverReachedSMTP):
            return mailer_transport._send(subject, body, "to@example.com")

    def test_lone_surrogate_in_body_returns_false_without_raise(self):
        """修复前：`_message_id` 在 try 外 encode 正文 → UnicodeEncodeError 抛回调用方。"""
        self.assertFalse(self._send_with_body("标题", f"正文带坏字符{_LONE_SURROGATE}"))
        self.assertEqual(_NeverReachedSMTP.calls, 0, "排版定稿失败就不该触碰 SMTP")

    def test_lone_surrogate_in_subject_returns_false_without_raise(self):
        self.assertFalse(self._send_with_body(f"坏主题{_LONE_SURROGATE}", "正文"))
        self.assertEqual(_NeverReachedSMTP.calls, 0)

    def test_serialize_failure_does_not_retry_other_entries(self):
        """构造级失败（此处注入到 as_string 消费点）换条目重投必然同样失败：
        只触碰第一条，按"本封未送达"收口。"""
        entries = [_entry("first.example"), _entry("second.example")]
        seen = []

        class _ExplodingSMTP:
            def __init__(self, host, port, timeout=0, context=None, **kw):
                seen.append(host)
                raise UnicodeEncodeError("utf-8", host, 0, 1, "simulated 构造级失败")

        with mock.patch.object(mailer, "is_enabled", lambda: True), \
             mock.patch.object(mailer, "smtp_list", lambda: entries), \
             mock.patch.object(mailer_transport.smtplib, "SMTP_SSL", _ExplodingSMTP):
            ok = mailer_transport._send("标题", "正文", "to@example.com")
        self.assertFalse(ok)
        self.assertEqual(seen, ["first.example"], "第二条目不该接手一封必坏的信")

    def test_transport_failure_still_fails_over(self):
        """回归防线：运输级失败（连接错误）的主备 failover 与本修复前逐字一致。"""
        attempts = []
        payload = {}

        class _FlakySMTP(contextlib.AbstractContextManager):
            def __init__(self, host, port, timeout=0, context=None, **kw):
                attempts.append(host)
                if host == "first.example":
                    raise OSError("第一条目连不上")

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def login(self, u, p):
                pass

            def sendmail(self, f, t, msg):
                payload["msg"] = msg

        with mock.patch.object(mailer, "is_enabled", lambda: True), \
             mock.patch.object(mailer, "smtp_list",
                               lambda: [_entry("first.example"), _entry("second.example")]), \
             mock.patch.object(mailer_transport.smtplib, "SMTP_SSL", _FlakySMTP):
            ok = mailer_transport._send("标题", "正文", "to@example.com")
        self.assertTrue(ok)
        self.assertEqual(attempts, ["first.example", "second.example"])
        # 正文在 MIME 里是 base64 段，不逐字可比；可比的判据是"第二条目真的把这封
        # 投了出去"（sendmail 被调用）与"重投的是同一封"（同 Message-ID 幂等键）。
        self.assertIn("msg", payload)
        import email
        sent = email.message_from_string(payload["msg"])
        self.assertEqual(sent["Message-ID"],
                         mailer_transport._message_id("标题", "to@example.com", "正文"),
                         "failover 后仍是同一封：幂等键必须一致")


if __name__ == "__main__":
    unittest.main(verbosity=2)
