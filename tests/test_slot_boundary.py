# -*- coding: utf-8 -*-
"""自选时间片边界回归测试（2026-08-28 审查批次 5 F4）。

用法（在项目根目录）：
    py -m pytest tests/test_slot_boundary_0828.py -v
    py tests/test_slot_boundary_0828.py

F4：窗口长度不是 5 分钟整数倍时（如 06:30~07:52，L=82），build_schedule 原以
`0 <= slot < span`（span = 有效窗口宽度 = 82 - 前裁 - 后裁 = 80）校验自选片，
而 Web 端 _pref_slots 生成的最后一片 slot=80（标签 07:50）在 UI 可点选——
两边判定不一致导致用户选的时段被静默丢弃、回退自动分配，且无任何提示。
修复：以 _slot_to_bi 的成员性为准（与 Web 端同一套可用性判定）。
"""
import os
import sys
import unittest
from datetime import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "scripts"))

import signin  # noqa: E402


class SlotBoundaryTest(unittest.TestCase):
    """F4：非 5 分钟整数倍窗口的最后一个自选片必须被尊重。"""

    def setUp(self):
        # 06:30 ~ 07:52（L=82，非 5 的整数倍）；前后裁剪各 60s
        os.environ["YIBAN_SIGN_START"] = "06:30"
        os.environ["YIBAN_SIGN_END"] = "07:52"
        os.environ["YIBAN_WINDOW_EDGE_FRONT_SEC"] = "60"
        os.environ["YIBAN_WINDOW_EDGE_BACK_SEC"] = "60"
        os.environ["YIBAN_ALLOW_TIME_PREF"] = "1"
        os.environ.pop("YIBAN_SIGN_ORDER", None)
        os.environ.pop("YIBAN_SIGN_DIST", None)
        os.environ.pop("YIBAN_SIGN_MODE", None)

    def tearDown(self):
        for k in ("YIBAN_SIGN_START", "YIBAN_SIGN_END", "YIBAN_WINDOW_EDGE_FRONT_SEC",
                  "YIBAN_WINDOW_EDGE_BACK_SEC", "YIBAN_ALLOW_TIME_PREF"):
            os.environ.pop(k, None)

    def _acc(self, phone):
        return signin.Account(phone=phone, password="pw")

    def test_last_ui_selectable_slot_is_respected(self):
        """slot=80（07:50，UI 可点选）必须被调度器采纳，而不是静默回退自动分配。"""
        base = datetime(2026, 8, 28, 0, 0)
        prefs = {"13900000001": {"slot_min": 80, "updated_at": "2026-08-01 00:00:00"}}
        schedule = signin.build_schedule(
            [self._acc("13900000001")], prefs=prefs, now=base
        )
        self.assertIn("13900000001", schedule)
        t = schedule["13900000001"]
        # 选中片 = 07:50 起的块（[07:50:00, 07:52:00)，有效窗口后裁到 07:51:00）
        self.assertGreaterEqual(
            t, base.replace(hour=7, minute=50),
            f"自选 07:50 片被静默丢弃，实际排到 {t:%H:%M:%S}",
        )
        self.assertLess(t, base.replace(hour=7, minute=52))

    def test_out_of_window_slot_still_falls_back(self):
        """真正落在窗口外的片（slot=90 > L=82）仍应回退自动分配，不崩。"""
        base = datetime(2026, 8, 28, 0, 0)
        prefs = {"13900000002": {"slot_min": 90, "updated_at": "2026-08-01 00:00:00"}}
        schedule = signin.build_schedule(
            [self._acc("13900000002")], prefs=prefs, now=base
        )
        self.assertIn("13900000002", schedule)  # 回退自动分配，账号仍被排上
        t = schedule["13900000002"]
        self.assertGreaterEqual(t, base.replace(hour=6, minute=31))
        self.assertLess(t, base.replace(hour=7, minute=52))

    def test_mid_window_slot_unchanged(self):
        """窗口中部常规自选片行为不变（回归保护）。"""
        base = datetime(2026, 8, 28, 0, 0)
        prefs = {"13900000003": {"slot_min": 40, "updated_at": "2026-08-01 00:00:00"}}
        schedule = signin.build_schedule(
            [self._acc("13900000003")], prefs=prefs, now=base
        )
        self.assertIn("13900000003", schedule)
        t = schedule["13900000003"]
        # slot=40 → 07:10 起的块
        self.assertGreaterEqual(t, base.replace(hour=7, minute=10))
        self.assertLess(t, base.replace(hour=7, minute=15))


if __name__ == "__main__":
    unittest.main(verbosity=2)
