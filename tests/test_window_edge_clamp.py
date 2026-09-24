# -*- coding: utf-8 -*-
"""缓冲过大时收缩缓冲、保留窗口（`yiban/window.bounds` 的退化处置）。

窗口是管理员意图、缓冲只是精修：缓冲之和 >= 窗口宽度时**只等比收缩缓冲**
（有效窗口 = 窗口宽度的 80%），绝不用内置默认窗口替换管理员设的窗口。后者会让
"真实签到时段不是 06:30~07:50"的部署者在错误时段签到，而管理员在页面上看不出
任何异常。窗口本身不可用（宽度 <= 0，上游 `parse_window` 已拦截，此处为防御分支）
才回退默认窗口。

覆盖：
- 正常窗口（含生产默认 06:30~07:50）逐值不变，两个标记都为假；
- 缓冲之和 >= 窗口宽度 → 保留窗口、等比收缩缓冲、`edges_clamped` 置真；
- 收缩后的缓冲再喂回 `bounds` 不再收缩（幂等，配置自洽）；
- 窗口宽度 <= 0 → `fell_back` 回退默认（防御分支语义保留）；
- `_schedule_blocks` 的告警文案与去重（收缩与回退各一条，各只并入一次管理员邮件）。
"""
import unittest
from unittest import mock

import signin

from yiban import window


def _cfg(start, end, front, back):
    """调度配置映射（与 `signin._schedule_config()` 的窗口相关形态一致）。"""
    return {
        "sign_start": start,
        "sign_end": end,
        "edge_front_sec": front,
        "edge_back_sec": back,
    }


class BoundsClampTest(unittest.TestCase):
    def test_normal_window_values_pinned(self):
        """生产默认 06:30~07:50 + 各 60s：逐值不变、无任何标记。"""
        win = window.bounds(_cfg((6, 30), (7, 50), 60, 60))
        self.assertEqual((win.start_min, win.end_min), (390, 470))
        self.assertEqual((win.lo_min, win.hi_min), (391.0, 469.0))
        self.assertEqual((win.front_sec, win.back_sec), (60, 60))
        self.assertFalse(win.fell_back)
        self.assertFalse(win.edges_clamped)

    def test_other_normal_window_values_pinned(self):
        """非默认窗口（21:00~21:05，各 60s）：窗口与缓冲逐值不变。"""
        win = window.bounds(_cfg((21, 0), (21, 5), 60, 60))
        self.assertEqual((win.start_min, win.end_min), (1260, 1265))
        self.assertEqual((win.lo_min, win.hi_min), (1261.0, 1264.0))
        self.assertFalse(win.fell_back)
        self.assertFalse(win.edges_clamped)

    def test_overflow_clamps_edges_and_keeps_window(self):
        """缓冲之和 = 窗口宽度（10 分钟 + 各 300s）：窗口不动，缓冲等比缩到 20%。"""
        win = window.bounds(_cfg((6, 30), (6, 40), 300, 300))
        self.assertFalse(win.fell_back, "窗口可用时不得回退默认窗口")
        self.assertTrue(win.edges_clamped)
        self.assertEqual((win.start_min, win.end_min), (390, 400), "窗口必须原样保留")
        # 有效窗口 = 窗口宽度的 80% = 8 分钟；缓冲合计 = 2 分钟 → 各 60s
        self.assertEqual((win.front_sec, win.back_sec), (60, 60))
        self.assertEqual((win.lo_min, win.hi_min), (391.0, 399.0))
        self.assertLess(win.lo_min, win.hi_min)

    def test_overflow_asymmetric_scales_each_side(self):
        """单边吃满（前 300s、后 0s）同样等比收缩：前 60s、后 0s，窗口不动。"""
        win = window.bounds(_cfg((21, 0), (21, 5), 300, 0))
        self.assertTrue(win.edges_clamped)
        self.assertEqual((win.start_min, win.end_min), (1260, 1265))
        self.assertEqual((win.front_sec, win.back_sec), (60, 0))
        self.assertEqual((win.lo_min, win.hi_min), (1261.0, 1265.0))

    def test_minimum_window_still_has_positive_effective_window(self):
        """最小可用窗口（1 分钟）+ 各 300s：收缩后仍必须 lo < hi（不得留空窗口）。"""
        win = window.bounds(_cfg((7, 0), (7, 1), 300, 300))
        self.assertTrue(win.edges_clamped)
        self.assertEqual((win.start_min, win.end_min), (420, 421))
        self.assertLess(win.lo_min, win.hi_min)
        self.assertGreater(win.full_sec(), 0.0)

    def test_clamped_edges_are_idempotent(self):
        """收缩结果喂回 `bounds` 不再触发收缩：配置自洽（不会逐轮越缩越短）。"""
        win = window.bounds(_cfg((6, 30), (6, 40), 60, 60))
        self.assertFalse(win.fell_back)
        self.assertFalse(win.edges_clamped)
        self.assertEqual((win.lo_min, win.hi_min), (391.0, 399.0))

    def test_zero_width_window_falls_back_to_default(self):
        """防御分支：窗口宽度 <= 0（起 >= 止）→ 保留既有回退默认窗口语义。"""
        for start, end in (((7, 0), (6, 0)), ((7, 0), (7, 0))):
            with self.subTest(start=start, end=end):
                win = window.bounds(_cfg(start, end, 300, 300))
                self.assertTrue(win.fell_back)
                self.assertFalse(win.edges_clamped)
                self.assertEqual((win.start_min, win.end_min), (390, 470))
                self.assertEqual((win.lo_min, win.hi_min), (391.0, 469.0))
                self.assertEqual((win.front_sec, win.back_sec), (60, 60))

    def test_markers_default_false(self):
        """两个标记都是构造参数，缺省为假（外部按关键字构造不改变既有行为）。"""
        win = window.Window(390, 470, 391.0, 469.0, 60, 60)
        self.assertFalse(win.fell_back)
        self.assertFalse(win.edges_clamped)


class ScheduleBlocksWarningTest(unittest.TestCase):
    """`_schedule_blocks` 的告警：收缩缓冲与回退窗口各一条、各只并入一次邮件。"""

    def setUp(self):
        signin._window_clamped_notified = False
        signin._window_fallback_notified = False
        signin._mail_summary.clear()

    def tearDown(self):
        signin._window_clamped_notified = False
        signin._window_fallback_notified = False
        signin._mail_summary.clear()

    def test_clamped_edges_warn_window_and_edges(self):
        """收缩缓冲：告警与邮件都要说清"窗口 X~Y + 缓冲从 fs/bs 收缩到 fs'/bs'"。"""
        cfg = _cfg((6, 30), (6, 40), 300, 300)
        with self.assertLogs("yiban", level="WARNING") as cm, \
                mock.patch.object(signin, "_collect_admin_mail") as m_mail:
            blocks1, lo1, hi1 = signin._schedule_blocks(dict(cfg))
            blocks2, lo2, hi2 = signin._schedule_blocks(dict(cfg))
        text = "\n".join(cm.output)
        self.assertIn("06:30~06:40", text)
        self.assertIn("300", text)
        self.assertIn("60", text)
        self.assertNotIn("回退默认", text, "窗口可用时不得说成回退默认窗口")
        m_mail.assert_called_once()  # 多账号/多轮调用只并入一次
        title, mail_text = m_mail.call_args[0]
        self.assertEqual(title, "签到窗口缓冲已收缩")
        self.assertIn("06:30~06:40", mail_text)
        self.assertIn("300", mail_text)
        self.assertIn("60", mail_text)
        self.assertNotIn("回退默认", mail_text)
        self.assertEqual((lo1, hi1), (391.0, 399.0))
        self.assertEqual((lo2, hi2), (391.0, 399.0))
        self.assertTrue(blocks1)
        self.assertEqual(len(blocks1), len(blocks2))

    def test_fallback_keeps_one_warning(self):
        """防御分支（窗口宽度 <= 0）：仍有一条"已回退默认窗口"的告警与邮件。"""
        cfg = _cfg((7, 0), (6, 0), 300, 300)
        with self.assertLogs("yiban", level="WARNING") as cm, \
                mock.patch.object(signin, "_collect_admin_mail") as m_mail:
            signin._schedule_blocks(dict(cfg))
        self.assertIn("回退默认窗口", "\n".join(cm.output))
        m_mail.assert_called_once()
        self.assertEqual(m_mail.call_args[0][0], "签到窗口配置异常")

    def test_normal_window_silent(self):
        """正常窗口：既不告警也不并入邮件。"""
        cfg = _cfg((6, 30), (7, 50), 60, 60)
        with self.assertNoLogs("yiban", level="WARNING"), \
                mock.patch.object(signin, "_collect_admin_mail") as m_mail:
            blocks, lo, hi = signin._schedule_blocks(cfg)
        m_mail.assert_not_called()
        self.assertTrue(blocks)
        self.assertEqual((lo, hi), (391.0, 469.0))


if __name__ == "__main__":
    unittest.main(verbosity=2)
