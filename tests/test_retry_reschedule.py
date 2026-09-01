# -*- coding: utf-8 -*-
"""调度 v2 时间驱动队列（2026-08-27 阶段 2）：重试重新尊重计划。

覆盖：
- P4：失败账号重试非阻塞——其他账号立即执行（不再原地 sleep 60~90s 堵全队）
- P1/P2：重试落点 = 剩余窗口内重新采样（≥ now+retry_min_interval，≤ eff_hi，偏早段）
- P5：窗口不足 → 重试直接放弃（不再硬冲/无限等）
"""
import os
import sys
import unittest
from datetime import datetime as _dt
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "scripts"))

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


if __name__ == "__main__":
    unittest.main()
