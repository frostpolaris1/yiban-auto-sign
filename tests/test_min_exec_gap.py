# -*- coding: utf-8 -*-
"""F1：schedule 模式下相邻请求最小间隔 min_exec_gap 兜底。

背景（2026-08-27 专项检查）：min_exec_gap 配置在 _schedule_config 定义但从未接线；
压缩模式（n > 块数×block_cap）块内间隔可低至 ~9.4s（n=500），请求过密有风控风险。
本测试验证：
- 相邻请求间隔 < min_exec_gap 时补足（过点/到点账号）
- 间隔已足够时不额外 sleep（行为向后兼容）
- exec_gap_min（启动对齐）语义保留
"""
import os
import sys
import unittest
from unittest import mock
from datetime import datetime as _dt

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "scripts"))

import signin  # noqa: E402


class FakeNow:
    NOW = _dt(2026, 8, 27, 7, 0, 0)

    @classmethod
    def now(cls):
        return cls.NOW


class MinExecGapTest(unittest.TestCase):
    def setUp(self):
        for k in ("YIBAN_MIN_EXEC_GAP", "YIBAN_EXEC_GAP_MIN",
                  "YIBAN_SIGN_ORDER", "YIBAN_SIGN_DIST", "YIBAN_SIGN_MODE"):
            os.environ.pop(k, None)

    def _run(self, schedule, min_gap, exec_gap, mono_vals):
        """跑 run_queue_retry（首 attempt 全成功），返回 (attempt, sleeps)。"""
        os.environ["YIBAN_MIN_EXEC_GAP"] = str(min_gap)
        os.environ["YIBAN_EXEC_GAP_MIN"] = str(exec_gap)
        accs = [signin.Account(phone=p, password="p") for p in schedule]
        sleeps = []
        try:
            with mock.patch.object(signin, "datetime", FakeNow), \
                 mock.patch.object(signin, "attempt_signin",
                                   return_value=(True, "ok", False, signin.STATUS_SUCCESS)) as attempt, \
                 mock.patch.object(signin, "_write_sign_state"), \
                 mock.patch.object(signin, "_update_cred_state"), \
                 mock.patch.object(signin.time, "monotonic", side_effect=mono_vals), \
                 mock.patch.object(signin.time, "sleep", side_effect=lambda s: sleeps.append(s)):
                signin.run_queue_retry(accs, "", 0, 0, schedule=schedule)
            return attempt, sleeps
        finally:
            os.environ.pop("YIBAN_MIN_EXEC_GAP", None)
            os.environ.pop("YIBAN_EXEC_GAP_MIN", None)

    def test_enforces_min_gap_on_past_due_accounts(self):
        """两账号均过点、与上次请求仅隔 0s → 第二次前补足 min_exec_gap(15s)。

        monotonic：t0(首)=100, last_done(首)=100, F1检查(二)=100, t0(二)=100, last_done(二)=100
        → gap = 15 - (100-100) = 15 → sleep(15)。
        """
        t0 = _dt(2026, 8, 27, 6, 40)  # 已过点
        sched = {"13800138000": t0, "13800138001": t0}
        attempt, sleeps = self._run(sched, 15, 0, [100.0] * 5)
        self.assertEqual(attempt.call_count, 2)
        self.assertTrue(any(abs(s - 15) < 1e-6 for s in sleeps), f"未补足 min_exec_gap: {sleeps}")

    def test_no_extra_sleep_when_gap_sufficient(self):
        """间隔已 ≥ min_exec_gap → 不额外 sleep（向后兼容）。

        monotonic：t0(首)=100, last_done(首)=100, F1检查(二)=130（距上次 30s > 15）→ 不补。
        """
        t0 = _dt(2026, 8, 27, 6, 40)
        sched = {"13800138000": t0, "13800138001": t0}
        attempt, sleeps = self._run(sched, 15, 0, [100.0, 100.0, 130.0, 130.0, 130.0])
        self.assertEqual(attempt.call_count, 2)
        self.assertEqual(sleeps, [], f"间隔充足时不应额外 sleep: {sleeps}")

    def test_exec_gap_min_still_respected(self):
        """过点账号启动对齐（exec_gap_min=10）语义保留：min_exec_gap=5 不削它的效果 → 补 max(5,10)=10。"""
        t0 = _dt(2026, 8, 27, 6, 40)
        sched = {"13800138000": t0, "13800138001": t0}
        attempt, sleeps = self._run(sched, 5, 10, [100.0] * 5)
        self.assertEqual(attempt.call_count, 2)
        self.assertTrue(any(abs(s - 10) < 1e-6 for s in sleeps), f"应补 exec_gap_min=10s: {sleeps}")

    def test_due_account_gets_min_gap_after_slot_sleep(self):
        """到点账号：sleep 到计划时刻后仍受 min_exec_gap 兜底（到点路径不叠加 exec_gap_min）。"""
        sched = {
            "13800138000": _dt(2026, 8, 27, 6, 40),      # 过点（A）
            "13800138001": _dt(2026, 8, 27, 7, 1, 0),    # 未来 60s（B）
        }
        attempt, sleeps = self._run(sched, 5, 10, [100.0] * 5)
        self.assertEqual(attempt.call_count, 2)
        # A 过点：last_done=None 不补；B 到点：sleep(60) 到落点，再补 min_gap=5（距上次 0s）
        self.assertTrue(any(abs(s - 60) < 1e-6 for s in sleeps), f"应 sleep 到落点: {sleeps}")
        self.assertTrue(any(abs(s - 5) < 1e-6 for s in sleeps), f"到点后应补 min_gap=5: {sleeps}")


if __name__ == "__main__":
    unittest.main()
