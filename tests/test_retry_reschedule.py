# -*- coding: utf-8 -*-
"""调度 v2 时间驱动队列（2026-08-27 阶段 2）：重试重新尊重计划。

覆盖：
- P4：失败账号重试非阻塞——其他账号立即执行（不再原地 sleep 60~90s 堵全队）
- P1/P2：重试落点 = 剩余窗口内重新采样（≥ now+retry_min_interval，≤ eff_hi，偏早段）
- P5：窗口不足 → 重试直接放弃（不再硬冲/无限等）
"""
import os
import unittest
from datetime import datetime as _dt
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import signin  # noqa: E402


class FakeNow:
    NOW = _dt(2026, 8, 27, 7, 0, 0)

    @classmethod
    def now(cls):
        return cls.NOW


def _acc(phone):
    return signin.Account(phone=phone, password="p")


class RetryRescheduleTest(unittest.TestCase):
    def setUp(self):
        for k in ("YIBAN_RETRY_MIN_INTERVAL", "YIBAN_SIGN_START", "YIBAN_SIGN_END",
                  "YIBAN_WINDOW_EDGE_SEC", "YIBAN_MIN_EXEC_GAP", "YIBAN_EXEC_GAP_MIN"):
            os.environ.pop(k, None)

    def test_failure_does_not_block_other_accounts(self):
        """P4：A 失败不阻塞 B——执行顺序 A→B→A(重试)；旧行为会是 A→A(重试)→B。"""
        calls = []

        def fake_attempt(acc):
            calls.append(acc.phone)
            if acc.phone == "13800138000" and calls.count(acc.phone) == 1:
                return (False, "网络超时", False, signin.STATUS_FAILED)
            return (True, "ok", False, signin.STATUS_SUCCESS)

        sched = {
            "13800138000": _dt(2026, 8, 27, 6, 40),   # 过点（A，首次失败）
            "13800138001": _dt(2026, 8, 27, 7, 0, 0),  # 到点（B，成功）
        }
        with mock.patch.object(signin, "datetime", FakeNow), \
             mock.patch.object(signin, "attempt_signin", side_effect=fake_attempt), \
             mock.patch.object(signin, "_write_sign_state"), \
             mock.patch.object(signin, "_update_cred_state"), \
             mock.patch.object(signin.time, "monotonic", return_value=100.0), \
             mock.patch.object(signin.time, "sleep"):
            signin.run_queue_retry(
                [_acc("13800138000"), _acc("13800138001")],
                "", 0, 0, schedule=sched,
            )
        self.assertEqual(calls, ["13800138000", "13800138001", "13800138000"],
                         "A 失败后 B 应立即执行（重试挂起不阻塞）")

    def test_retry_slot_within_window_and_min_interval(self):
        """P1/P2：重试落点 ∈ [now+retry_min_interval, eff_hi]，且限偏早段（≤ 60% 剩余窗口）。"""
        with mock.patch.dict(os.environ, {
            "YIBAN_RETRY_MIN_INTERVAL": "60",
            "YIBAN_SIGN_START": "06:30",
            "YIBAN_SIGN_END": "07:50",
            "YIBAN_WINDOW_EDGE_SEC": "0",
        }, clear=False):
            cfg = signin._schedule_config()
            now = FakeNow.NOW  # 07:00
            lo = now.replace(minute=1, second=0, microsecond=0)
            eff_hi = now.replace(hour=7, minute=50, second=0, microsecond=0)
            for seed in range(100):
                nxt = signin._next_retry_at(now, cfg, rng=__import__("random").Random(seed))
                self.assertIsNotNone(nxt)
                self.assertGreaterEqual(nxt, lo, f"seed {seed}: 早于下界")
                self.assertLessEqual(nxt, eff_hi, f"seed {seed}: 越过窗口末端")
                # 偏早段：nxt ≤ lo + 60% 剩余窗口
                self.assertLessEqual(
                    nxt, lo + (eff_hi - lo) * 0.6 + __import__("datetime").timedelta(seconds=1),
                    f"seed {seed}: 落点应偏早",
                )

    def test_retry_gives_up_when_window_insufficient(self):
        """P5：窗口剩余不足 retry_min_interval → 不重试，直接判失败（不硬冲）。"""
        with mock.patch.dict(os.environ, {
            "YIBAN_RETRY_MIN_INTERVAL": "60",
            "YIBAN_SIGN_START": "06:30",
            "YIBAN_SIGN_END": "07:50",
            "YIBAN_WINDOW_EDGE_SEC": "0",
        }, clear=False):

            class LateNow(FakeNow):
                NOW = _dt(2026, 8, 27, 7, 49, 50)  # 距 eff_hi=07:50 不足 60s

            sched = {"13800138000": _dt(2026, 8, 27, 7, 49, 0)}
            with mock.patch.object(signin, "datetime", LateNow), \
                 mock.patch.object(signin, "attempt_signin",
                                   return_value=(False, "网络超时", False, signin.STATUS_FAILED)) as attempt, \
                 mock.patch.object(signin, "classify_failure", return_value=2), \
                 mock.patch.object(signin, "_write_sign_state"), \
                 mock.patch.object(signin, "_update_cred_state"), \
                 mock.patch.object(signin, "send_notification"):
                results = signin.run_queue_retry(
                    [_acc("13800138000")], "", 0, 0, schedule=sched,
                )
            self.assertEqual(attempt.call_count, 1, "窗口不足不应重试")
            self.assertFalse(results["13800138000"][0], "窗口不足应判失败")

    def _run_status(self, reason, schedule):
        """统一驱动：让某账号连续失败直到进入重试入队分支，返回 (_write_sign_state 调用, logger mock)。"""
        def fake_attempt(acc):
            return (False, reason, False, signin.STATUS_FAILED)

        logmock = mock.Mock()
        with mock.patch.object(signin, "datetime", FakeNow), \
             mock.patch.object(signin, "attempt_signin", side_effect=fake_attempt), \
             mock.patch.object(signin, "_write_sign_state") as ws, \
             mock.patch.object(signin, "_update_cred_state"), \
             mock.patch.object(signin, "logger", logmock), \
             mock.patch.object(signin, "send_notification"), \
             mock.patch.object(signin, "send_user_fail_mail"), \
             mock.patch.object(signin, "_collect_admin_mail"), \
             mock.patch.object(signin.time, "monotonic", return_value=100.0), \
             mock.patch.object(signin.time, "sleep"):
            signin.run_queue_retry([_acc("13800138000")], "", 0, 0, schedule=schedule)
        return ws, logmock

    def test_schedule_retry_state_and_log_include_reason(self):
        """需求1（调度分支）：失败账号入队重试时，状态文件与 warning 日志都补记失败原因。"""
        reason = "获取签到任务失败: 未登录或登录已经超时"
        sched = {"13800138000": _dt(2026, 8, 27, 6, 40)}
        ws, logmock = self._run_status(reason, sched)
        retry_msgs = [c.args[2] for c in ws.call_args_list
                      if c.args[1] == signin.STATUS_RETRYING]
        self.assertTrue(retry_msgs, "未捕获到重试入队的状态写入")
        self.assertTrue(all(reason in m for m in retry_msgs), "状态文件未补记失败原因")
        warn_lines = [c.args[0] for c in logmock.warning.call_args_list
                      if str(c.args[0]).startswith("[13800138000] ⏳ 待重试")]
        self.assertTrue(warn_lines, "未捕获到重试入队日志")
        self.assertTrue(all(reason in w for w in warn_lines), "warning 日志未补记失败原因")

    def test_queue_retry_state_and_log_include_reason(self):
        """需求1（队列回队尾分支）：同上，覆盖无计划（手动/列表）模式。"""
        reason = "请求被 WAF 风控拦截，请配置 YIBAN_PROXY 代理后重试"
        ws, logmock = self._run_status(reason, None)
        retry_msgs = [c.args[2] for c in ws.call_args_list
                      if c.args[1] == signin.STATUS_RETRYING]
        self.assertTrue(retry_msgs, "未捕获到重试入队的状态写入")
        self.assertTrue(all(reason in m for m in retry_msgs), "状态文件未补记失败原因")
        warn_lines = [c.args[0] for c in logmock.warning.call_args_list
                      if str(c.args[0]).startswith("[13800138000] ⏳ 待重试")]
        self.assertTrue(warn_lines, "未捕获到重试入队日志")
        self.assertTrue(all(reason in w for w in warn_lines), "warning 日志未补记失败原因")

    def test_retry_reason_sanitized_no_log_injection(self):
        """需求1：含换行的原因经 _sanitize_text 转义，状态文件/日志不会被拆成多行。"""
        reason = "获取签到任务失败\n未登录或登录已经超时"
        sched = {"13800138000": _dt(2026, 8, 27, 6, 40)}
        ws, logmock = self._run_status(reason, sched)
        retry_msgs = [c.args[2] for c in ws.call_args_list
                      if c.args[1] == signin.STATUS_RETRYING]
        self.assertTrue(retry_msgs)
        self.assertTrue(all(signin._sanitize_text(reason) in m for m in retry_msgs),
                        "重试状态 message 应包含转义后的原因")
        self.assertTrue(all("\n" not in m for m in retry_msgs), "原因含真实换行会污染状态文件")
        warn_lines = [c.args[0] for c in logmock.warning.call_args_list
                      if str(c.args[0]).startswith("[13800138000] ⏳ 待重试")]
        self.assertTrue(all("\n" not in w for w in warn_lines),
                        "原因换行会把日志拆成多行")


if __name__ == "__main__":
    unittest.main()
