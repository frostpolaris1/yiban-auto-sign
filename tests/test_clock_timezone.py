# -*- coding: utf-8 -*-
"""统一业务时钟（`yiban/clock.py`）与"窗口判定只认业务钟"的回归防线。

**缺陷**：窗口/日期口径定义在北京时间上，而判定
取的是**宿主本地时间**——部署在 UTC 主机（GitHub Actions runner、海外 VPS）时，
北京时间 06:40 在宿主看来是前一日 22:40，窗口闸门直接判"时段已结束"，
**当天全部账号零请求**；按日状态文件与签到事件还会落到错的日期上。

**防线**：全系统只认 `yiban.clock`（固定 +8，无 tzdata 依赖）。本文件的
`test_window_decision_follows_beijing_clock` 让"宿主墙钟"与"业务钟"刻意背离，
钉死"判定只认业务钟"——若有人把某处改回 `datetime.now()`，该用例立刻红。
"""
import datetime as _dt
import os
import sys
import unittest
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

import signin  # noqa: E402

from yiban import clock  # noqa: E402


class _HostClock(_dt.datetime):
    """宿主墙钟替身：固定在一个**窗口外**的时刻（模拟 UTC 主机的北京 06:40）。"""

    _fixed = _dt.datetime(2026, 1, 1, 22, 40, 0)

    @classmethod
    def now(cls, tz=None):
        return cls._fixed


class ClockTest(unittest.TestCase):
    def test_now_is_beijing_plus_eight(self):
        """now() 恒为 UTC+8，与宿主 TZ 无关（本用例在任何时区的主机上都应通过）。"""
        delta = clock.now() - _dt.datetime.utcnow()
        self.assertAlmostEqual(delta.total_seconds(), 8 * 3600, delta=5)

    def test_now_is_naive(self):
        """naive：既有全部 datetime 运算都与 naive 混用，带 tzinfo 会直接 TypeError。"""
        self.assertIsNone(clock.now().tzinfo)

    def test_today_and_ts_formats(self):
        self.assertEqual(clock.today(), clock.now().strftime("%Y-%m-%d"))
        self.assertEqual(len(clock.ts()), 19)
        self.assertEqual(clock.ts()[:10], clock.today())

    def test_session_cache_clock_delegates_to_single_source(self):
        """db 的会话缓存时钟（原为独立实现的固定 +8）已收口到唯一时钟。"""
        import db
        self.assertLess(abs((db._session_cache_now() - clock.now()).total_seconds()), 5)


class WindowDecisionUsesBeijingClockTest(unittest.TestCase):
    """窗口判定必须只认业务钟（核心回归）。"""

    PHONE = "13800138000"

    def setUp(self):
        for k in ("YIBAN_RETRY_MIN_INTERVAL", "YIBAN_SIGN_START", "YIBAN_SIGN_END",
                  "YIBAN_WINDOW_EDGE_SEC", "YIBAN_MIN_EXEC_GAP", "YIBAN_EXEC_GAP_MIN"):
            os.environ.pop(k, None)

    def _run_with_clocks(self, beijing_now):
        """宿主墙钟固定在窗口外，业务钟固定在 beijing_now，返回 attempt_signin 的调用次数。"""
        acc = signin.Account(phone=self.PHONE, password="p")
        sched = {self.PHONE: beijing_now - _dt.timedelta(minutes=20)}
        with mock.patch.object(signin.clock, "now", lambda: beijing_now), \
                mock.patch.object(signin, "datetime", _HostClock), \
                mock.patch.object(signin, "attempt_signin",
                                  return_value=(True, "ok", False, signin.STATUS_SUCCESS)) as attempt, \
                mock.patch.object(signin, "_write_sign_state"), \
                mock.patch.object(signin, "_update_cred_state"), \
                mock.patch.object(signin.time, "sleep"), \
                mock.patch.object(signin.time, "monotonic", return_value=100.0):
            signin.run_queue_retry([acc], "", 0, 0, schedule=sched)
        return attempt

    def test_window_decision_follows_beijing_clock(self):
        """北京 06:40（窗口内）+ 宿主墙钟 22:40（窗口外）→ 必须发起签到。

        判定若取宿主钟（旧实现），会判"时段已结束"而整轮零请求——这正是本用例要防的。
        """
        beijing_0640 = clock.now().replace(hour=6, minute=40, second=0, microsecond=0)
        attempt = self._run_with_clocks(beijing_0640)
        self.assertEqual(attempt.call_count, 1,
                         "窗口判定取错了钟（宿主墙钟），北京时间 06:40 被误判为已结束")

    def test_window_still_closes_after_deadline(self):
        """不是"永不判关闭"：北京 08:10（已过 eff_hi）应判结束、零请求。"""
        beijing_0810 = clock.now().replace(hour=8, minute=10, second=0, microsecond=0)
        attempt = self._run_with_clocks(beijing_0810)
        self.assertEqual(attempt.call_count, 0, "已过窗口仍发起请求")

    def test_window_closed_uses_beijing_end(self):
        cfg = signin._schedule_config()
        self.assertFalse(signin._window_closed(cfg, clock.now().replace(hour=7, minute=0)))
        self.assertTrue(signin._window_closed(cfg, clock.now().replace(hour=9, minute=0)))


class LogAndStateDateUseBeijingTest(unittest.TestCase):
    """按日留痕（日志文件名 / 状态文件 / 事件时间戳）也走同一时钟。"""

    def test_signin_log_path_uses_beijing_date(self):
        """日志按日切分：宿主为 UTC 时若用宿主日期，北京时间 00:00–08:00 会写进前一日。"""
        fake_bj = _dt.datetime(2026, 3, 2, 1, 30, 0)  # 北京凌晨 = UTC 前一日 17:30
        with mock.patch.object(clock, "now", return_value=fake_bj):
            self.assertEqual(clock.today(), "2026-03-02")
            self.assertEqual(clock.ts(), "2026-03-02 01:30:00")


if __name__ == "__main__":
    unittest.main(verbosity=2)
