# -*- coding: utf-8 -*-
"""一封邮件必须有整封时间预算（工单 ba-p09-01）。

标签：E · 通知：SMTP 超时
覆盖：`transport._send` 的条目 failover 循环、`send_admin_alert` 的收件人循环；
放弃时的出声；单个 socket 操作超时（15 秒）不许被当成修法改小。
关键断言：一条半开的 SMTP 端点（每个 socket 操作耗满 15 秒后才失败）不能把一次
发信拖过总预算；超预算必须**放弃并留一行可定位的告警**，不是无限往下试。
依赖：不联网——`smtplib.SMTP_SSL` 与 `config` 的条目/开关全部打桩；时钟用假单调钟
推进（`create=True` 打桩，故实现尚未 import time 时本用例仍按缺陷本身变红）。
"""
import logging
import os
import socket
import sys
import unittest
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

from yiban.mail import transport  # noqa: E402

OP_SEC = 15  # 单个 socket 操作的耗时与超时（= 现值，本单不许把它调小）
BUDGET_SEC = 45  # 契约：整封预算 = 一个条目 3 次 socket 操作 × 15 秒
OVERSHOOT = BUDGET_SEC + OP_SEC  # 允许一次在途操作越界


def _entries(n):
    return [{"host": "smtp%d.example.test" % i, "port": 465,
             "user": "u%d" % i, "pass": "p%d" % i} for i in range(1, n + 1)]


class _Clock:
    """假单调钟：只有显式 advance 会让时间走。"""

    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class MailTotalBudgetTest(unittest.TestCase):
    def setUp(self):
        self.clock = _Clock()
        self.attempts = []  # [(host, port, timeout)]
        self.records = []  # 捕获 mailer logger 的记录

        def _half_open(host, port=None, timeout=None, context=None):
            """半开端点：连接阶段耗满 15 秒后抛 socket.timeout。"""
            self.attempts.append((host, port, timeout))
            self.clock.advance(OP_SEC)
            raise socket.timeout("connect 半开")

        handler = logging.Handler()
        handler.emit = self.records.append
        self.logger = logging.getLogger("mailer")
        self.logger.addHandler(handler)
        self.addCleanup(lambda: self.logger.removeHandler(handler))
        for p in (
            mock.patch.object(transport, "time", self.clock, create=True),
            mock.patch.object(transport.config, "is_enabled", lambda: True),
            mock.patch.object(transport.config, "smtp_list", lambda: _entries(10)),
            mock.patch.object(transport.smtplib, "SMTP_SSL", _half_open),
        ):
            p.start()
            self.addCleanup(p.stop)

    def _warnings(self):
        return [r.getMessage() for r in self.records if r.levelno >= logging.WARNING]

    def _attempted_hosts(self):
        return [a[0] for a in self.attempts]

    def test_half_open_endpoint_gives_up_within_total_budget(self):
        """10 条备择条目各烧满 socket 超时：今天没有整封上限，会一路试到底。"""
        ok = transport._send("测试主题", "正文", "admin@example.test")
        self.assertFalse(ok, "半开端点最终必须按未送达收口")
        self.assertLess(len(self.attempts), 10,
                        "条目循环必须被整封预算截停（今天 10 条全试）")
        self.assertLessEqual(self.clock.now, OVERSHOOT,
                             "整封耗时不得超过预算 + 一次在途操作")
        loud = [m for m in self._warnings() if "超时" in m and "放弃" in m]
        self.assertTrue(loud, "放弃必须响亮：一行能定位的告警（今天零行）")
        self.assertIn("测试主题", loud[0], "告警要能定位是哪一封")

    def test_recipient_loop_shares_one_budget(self):
        """`send_admin_alert` 逐收件人相乘是工单点名的第二层乘数：整次调用只花一份预算。"""
        ok = transport.send_admin_alert("管理员告警", "正文",
                                        to="a@x.test,b@x.test,c@x.test,d@x.test")
        self.assertFalse(ok)
        self.assertLessEqual(self.clock.now, OVERSHOOT,
                             "4 个收件人 × 10 条目 × 15 秒 = 今天无上界")

    def test_single_op_timeout_not_shrunk_as_the_fix(self):
        """预算靠"到点放弃"实现，不是把每操作 15 秒调小（工单明令禁止的假修法）。"""
        transport._send("主题", "正文", "admin@example.test")
        self.assertTrue(self.attempts, "前置条件：至少试过一条")
        timeouts = [a[2] for a in self.attempts]
        for t in timeouts:
            self.assertGreater(t, 0, "不许用非正超时把操作瞬间饿死")
            self.assertLessEqual(t, OP_SEC, "单操作上限仍是既有的 15 秒")
        self.assertIn(OP_SEC, timeouts, "预算内的操作必须照旧拿满 15 秒")

    def test_healthy_send_still_completes_inside_budget(self):
        """正常路径不受影响：一次成功的投递照旧返回 True，且远早于预算。"""
        clock, delivered = _Clock(), []

        class _Good:
            def __init__(self, host, port=None, timeout=None, context=None):
                clock.advance(1.0)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def login(self, user, password):
                clock.advance(1.0)

            def sendmail(self, user, rcpts, msg):
                delivered.append(rcpts)
                clock.advance(1.0)
                return {}

        with mock.patch.object(transport, "time", clock), \
                mock.patch.object(transport.config, "is_enabled", lambda: True), \
                mock.patch.object(transport.config, "smtp_list", lambda: _entries(10)), \
                mock.patch.object(transport.smtplib, "SMTP_SSL", _Good):
            ok = transport._send("主题", "正文", "admin@example.test")
        self.assertTrue(ok, "健康端点必须仍然发得出去，且不换条目")
        self.assertEqual(delivered, [["admin@example.test"]])
        self.assertLess(clock.now, BUDGET_SEC)


if __name__ == "__main__":
    unittest.main()
