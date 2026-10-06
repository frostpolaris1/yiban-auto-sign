# -*- coding: utf-8 -*-
"""A 线汇总邮件两级封顶的选条口径。

标签：H · 通知：邮件与推送
覆盖：`_flush_admin_mail_summary` 的条数封顶与字符封顶。
对应缺陷：`ba-p04-02`——封顶原按插入顺序切片；逐账号条目在轮中收集、
    轮级关键告警在轮末收集并落在列表尾部，恰好在全量失败日被挤出正文。
对应实现：`yiban/engine/alerts.py`（壳 `scripts/signin.py` 转发）。
关键断言：201 条普通 + 1 条高级别 ⇒ 高级别必在正文内；尾部说明必须点名
    被丢的主题与条数；正文超字符额度时尾部说明仍须在，被截的只能是明细。
依赖：进程内 mock（`mailer.send_admin_alert` / `db.admin_mail_recipients`）；
    不连 SMTP，不发真实推送（逐账号条目经 `push=False` 入口产生）。
"""
import unittest
from types import SimpleNamespace
from unittest import mock

import signin
from _mail_body import render_body

FLOOD = 201                     # 逐账号条目的量：超过 MAIL_SUMMARY_MAX_ENTRIES
CRITICAL_TITLE = "当日签到异常告警"
LONG_REASON = "登录超时，" * 240    # 上游异常消息可能整段回显，单条即 ~1440 字


class SummaryCapLevelPriorityTest(unittest.TestCase):
    """封顶先保住轮级关键告警，再按插入顺序保留逐账号明细。"""

    def setUp(self):
        self._old_entries = signin.MAIL_SUMMARY_MAX_ENTRIES
        self._old_chars = signin.MAIL_SUMMARY_MAX_CHARS
        signin._mail_summary.clear()
        self.sent = []

    def tearDown(self):
        signin.MAIL_SUMMARY_MAX_ENTRIES = self._old_entries
        signin.MAIL_SUMMARY_MAX_CHARS = self._old_chars
        signin._mail_summary.clear()

    def _flood_per_account_failures(self, n, reason="已尝试 3 次，放弃"):
        """按 `executor_v3._alert_give_up` 的真实入口灌 n 条逐账号失败条目。"""
        for i in range(n):
            signin.notify_admin_entry("易班签到失败", [
                ("账号", f"138****{i % 10000:04d}"),
                ("原因", reason),
            ], push=False)

    def _round_level_alert(self):
        """按 `runner` 轮末的真实入口产生一条轮级关键告警（落点在列表尾部）。"""
        accounts = [SimpleNamespace(phone="13800000001")]
        results = {"13800000001": (False, "签到时段已结束", True, "skipped_window")}
        self.assertTrue(signin._maybe_alert_zero_success(accounts, results, ok_n=0))

    def _flush(self):
        """收尾汇总，返回发出的纯文本正文。"""
        with mock.patch.object(signin.db, "admin_mail_recipients",
                               return_value=["admin@test.local"]), \
             mock.patch.object(signin.mailer, "send_admin_alert", return_value=True,
                               side_effect=lambda s, t, to=None: self.sent.append(t)):
            signin._flush_admin_mail_summary()
        self.assertEqual(len(self.sent), 1, "一封汇总")
        return render_body(self.sent[0])

    def _contains(self, needle, body, why):
        """正文断言：失败时只回显长度与前 200 字。

        汇总正文可达数十万字（本用例就故意撑到 20 万以上），直接用 `assertIn` 会在
        失败消息里回显整封正文——一份红日志 0.5 MB，红因反而被淹掉。
        """
        if needle not in body:
            self.fail(f"{why}（缺 {needle!r}；正文 {len(body)} 字，前 200 字：{body[:200]!r}）")

    def _absent(self, needle, body, why):
        if needle in body:
            self.fail(f"{why}（多出 {needle!r}；正文 {len(body)} 字）")

    def test_critical_alert_survives_per_account_flood(self):
        """201 条逐账号条目不得挤出轮级关键告警，被丢主题须点名。"""
        self._flood_per_account_failures(FLOOD)
        self._round_level_alert()
        self.assertEqual(len(signin._mail_summary), FLOOD + 1)

        body = self._flush()

        self._contains(f"【{CRITICAL_TITLE}】", body,
                       "轮级关键告警被条数封顶挤出正文（P04#2）")
        self._contains("共 202 条异常/预警", body, "头部计数须反映收集总量")
        self._contains("其余 2 条已截断", body, "尾部须报被丢条数")
        self._contains("被截断主题：易班签到失败 2 条", body,
                       "尾部说明只报条数 = 运维以为只是明细变长")
        self._absent("被截断主题：" + CRITICAL_TITLE, body,
                     "高级别条目不得出现在被丢名册里")
        self.assertEqual(signin._mail_summary, [], "发送后清空收集器")

    def test_char_cap_keeps_footer_and_critical(self):
        """正文超字符额度：被截的是明细，尾部说明不得被吃掉。"""
        self._flood_per_account_failures(FLOOD, reason=LONG_REASON)
        self._round_level_alert()
        cap = self._old_chars

        body = self._flush()

        self._contains("其余 2 条已截断", body,
                       f"尾部说明被字符截断吃掉（发出正文 {len(body)} 字，额度 {cap}）")
        self._contains("被截断主题：易班签到失败 2 条", body, "尾部名册须在")
        self._contains(f"【{CRITICAL_TITLE}】", body, "字符封顶同样不得丢轮级关键告警")
        self._contains("超长截断", body, "字符截断须留痕")
        self.assertLessEqual(len(body), cap,
                             f"整封正文须留在额度内：实际 {len(body)} 额度 {cap}")


if __name__ == "__main__":
    unittest.main()
