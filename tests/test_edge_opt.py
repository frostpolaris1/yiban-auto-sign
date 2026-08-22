# -*- coding: utf-8 -*-
"""掐头去尾前后独立（0.22.0）调度层测试。

背景：v0.21.x 的 YIBAN_WINDOW_EDGE_SEC 为前后对称的单一秒值（UI 三档 0/60/120）。
0.22.0 拆分为 YIBAN_WINDOW_EDGE_FRONT_SEC / _BACK_SEC（各自 0~300 秒，30 的倍数 =
0.5 分钟粒度），支持"前 2 分钟 + 后 5 分钟"这类不对称配置：
- 调度：不主动把账号安排在裁剪区内；块边界为浮点分钟（0.5 分钟=30s 精确）
- 旧键兼容：YIBAN_WINDOW_EDGE_SEC 存在时映射 front=back=旧值

web 层（设置读写 / 自选分块 disabled）测试见 tests/test_time_prefs.py。
"""
import os
import random
import sys
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "scripts"))

import signin  # noqa: E402


def hm(dt):
    """datetime → 当天分钟浮点（0:00 = 0，含秒）。"""
    return dt.hour * 60 + dt.minute + dt.second / 60.0


def make_accounts(n):
    return [signin.Account(phone=str(13800000000 + i), password="p") for i in range(n)]


def pop_env(keys):
    for k in keys:
        os.environ.pop(k, None)


class EdgeScheduleTest(unittest.TestCase):
    def setUp(self):
        keys = (
            "YIBAN_SIGN_ORDER", "YIBAN_SIGN_DIST", "YIBAN_SIGN_MODE",
            "YIBAN_WINDOW_EDGE_SEC", "YIBAN_WINDOW_EDGE_FRONT_SEC",
            "YIBAN_WINDOW_EDGE_BACK_SEC", "YIBAN_SIGN_START", "YIBAN_SIGN_END",
        )
        pop_env(keys)
        # 用例各自再写入的键随测试结束一并清除：新键（FRONT/BACK）优先级高于
        # 旧键 YIBAN_WINDOW_EDGE_SEC，泄漏会静默改写后续调度测试（schedule_v2
        # 的旧键用例）的有效窗口——2026-08-22 全量跑查明的跨文件污染源
        self.addCleanup(pop_env, keys)

    def test_asymmetric_front_back(self):
        """前 30s + 后 300s：所有计划时刻 ∈ [06:30.5, 07:45]（默认窗口 06:30~07:50）。"""
        os.environ["YIBAN_WINDOW_EDGE_FRONT_SEC"] = "30"
        os.environ["YIBAN_WINDOW_EDGE_BACK_SEC"] = "300"
        sched = signin.build_schedule(
            make_accounts(10), order="sequence", dist="uniform", rng=random.Random(1))
        self.assertEqual(len(sched), 10)
        for t in sched.values():
            m = hm(t)
            self.assertGreaterEqual(m, 390.5, "不得早于窗口起点+前裁 0.5 分钟")
            self.assertLessEqual(m, 470 - 5, "不得晚于窗口终点-后裁 5 分钟")

    def test_half_minute_front_edge_precision(self):
        """0.5 分钟（30s）粒度：前裁 0.5 分钟 → 首块 lo=390.5（浮点分钟精确）。"""
        os.environ["YIBAN_WINDOW_EDGE_FRONT_SEC"] = "30"
        os.environ["YIBAN_WINDOW_EDGE_BACK_SEC"] = "0"
        cfg = signin._schedule_config()
        blocks, eff_lo, eff_hi = signin._schedule_blocks(cfg)
        self.assertEqual(eff_lo, 390.5)
        self.assertEqual(eff_hi, 470.0)
        # 首块被部分裁剪：lo=390.5，仍有 4.5 分钟可用（块存在）
        self.assertEqual(blocks[0], (390.5, 395.0))

    def test_legacy_env_symmetric_mapping(self):
        """旧键 YIBAN_WINDOW_EDGE_SEC=120 → front=back=120（升级兼容，行为不变）。"""
        os.environ["YIBAN_WINDOW_EDGE_SEC"] = "120"
        cfg = signin._schedule_config()
        self.assertEqual(cfg["edge_front_sec"], 120)
        self.assertEqual(cfg["edge_back_sec"], 120)

    def test_new_keys_override_legacy(self):
        """新键存在时优先于旧键（前后可不同）。"""
        os.environ["YIBAN_WINDOW_EDGE_SEC"] = "120"
        os.environ["YIBAN_WINDOW_EDGE_FRONT_SEC"] = "30"
        os.environ["YIBAN_WINDOW_EDGE_BACK_SEC"] = "300"
        cfg = signin._schedule_config()
        self.assertEqual(cfg["edge_front_sec"], 30)
        self.assertEqual(cfg["edge_back_sec"], 300)

    def test_front_back_sum_overflows_window_fallback(self):
        """前 5 分 + 后 5 分 超过窗口（06:30~06:40 共 10 分钟）→ 回退默认窗口，不崩溃。"""
        os.environ["YIBAN_SIGN_START"] = "06:30"
        os.environ["YIBAN_SIGN_END"] = "06:40"
        os.environ["YIBAN_WINDOW_EDGE_FRONT_SEC"] = "300"
        os.environ["YIBAN_WINDOW_EDGE_BACK_SEC"] = "300"
        sched = signin.build_schedule(
            make_accounts(3), order="sequence", dist="uniform", rng=random.Random(1))
        self.assertEqual(len(sched), 3)
        for t in sched.values():
            self.assertTrue(391 <= hm(t) <= 469, t)

    def test_pref_slot_partially_clipped_block_maps(self):
        """前 2 分钟裁剪：自选 slot 0（首片）仍可映射到块（块 0 lo=392.0），不因裁剪丢块。"""
        os.environ["YIBAN_WINDOW_EDGE_FRONT_SEC"] = "120"
        os.environ["YIBAN_WINDOW_EDGE_BACK_SEC"] = "0"
        cfg = signin._schedule_config()
        slot_to_bi = signin._slot_to_bi(cfg)
        self.assertIn(0, slot_to_bi, "部分裁剪的首片仍应可被自选选中")
        _blocks, eff_lo, _eff_hi = signin._schedule_blocks(cfg)
        self.assertEqual(eff_lo, 392.0)


if __name__ == "__main__":
    unittest.main()
