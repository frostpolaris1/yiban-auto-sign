# -*- coding: utf-8 -*-
"""调度 v2（S1 demo）build_schedule 统一填充框架测试。

用法（在项目根目录）：
    py -m pytest tests/test_schedule_v2.py -v   # 需要 pytest
    py tests/test_schedule_v2.py                # 无 pytest 也可直接运行

覆盖（对应 docs/design/plan-scheduler-v2.md 第 3/6 章）：
- 小人数（n≤3）免分块：直接有效窗口内随机时刻
- 顺序×均匀：线性填块（50 人 → 前 4 块，块内等分）
- 随机×均匀：循环填块（每块人数均衡、铺满窗口）
- 顺序×正态：z_i 锚点稳定（hash(phone)，两天波动有界）；全局钟形
- 随机×正态：每天重排（两次运行结果不同）
- 首尾缓冲：所有组合 × 多 seed 全部 ∈ [06:31, 07:49]
- σ_eff 封顶：n 大时 ≤ 有效窗口/3
- 压缩模式：n=300 全部账号拿到时间点且不越界
- 兼容映射：旧 YIBAN_SIGN_MODE=normal → 顺序×正态
- 固定 seed 可复现
"""
import os
import random
import sys
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "scripts"))

import signin  # noqa: E402

# 有效窗口（默认配置）：[06:31, 07:49] = 分钟 [391, 469]
EFF_LO = 391
EFF_HI = 469


def hm(dt):
    """datetime → 当天分钟数（0:00 = 0）。"""
    return dt.hour * 60 + dt.minute


def make_accounts(n):
    accs = []
    for i in range(n):
        a = signin.Account(phone=str(13800000000 + i), password="p")
        accs.append(a)
    return accs


class ScheduleV2Test(unittest.TestCase):
    def setUp(self):
        for k in (
            "YIBAN_SIGN_ORDER", "YIBAN_SIGN_DIST", "YIBAN_SIGN_MODE",
            "YIBAN_WINDOW_EDGE_SEC", "YIBAN_BLOCK_CAP", "YIBAN_SCHEDULE_MIN_ACCOUNTS",
            "YIBAN_MIN_EXEC_GAP", "YIBAN_SIGN_START", "YIBAN_SIGN_END",
        ):
            os.environ.pop(k, None)

    def test_small_n_uses_blocks(self):
        """小人数复用分块：顺序×均匀 n=2 → 线性填块同块（块 0）；随机×均匀 → 循环填块分块。"""
        accs = make_accounts(2)
        s_seq = signin.build_schedule(
            accs, order="sequence", dist="uniform", rng=random.Random(1))
        for t in s_seq.values():
            self.assertTrue(391 <= hm(t) < 395, t)  # 线性填块：两人都在块 0 [06:31,06:35)
        s_rnd = signin.build_schedule(
            accs, order="random", dist="uniform", rng=random.Random(1))
        blocks = sorted(hm(t) for t in s_rnd.values())
        self.assertLess(blocks[0], 395)      # 循环填块：块 0
        self.assertGreaterEqual(blocks[1], 395)  # 块 1
        self.assertTrue(all(EFF_LO <= t <= EFF_HI for t in blocks))

    def test_small_n_sequence_same_block(self):
        """小人数 + 顺序排序：块确定（可预期）——不同 seed 仍在同一块。"""
        accs = make_accounts(2)
        s1 = signin.build_schedule(accs, order="sequence", dist="uniform", rng=random.Random(1))
        s2 = signin.build_schedule(accs, order="sequence", dist="uniform", rng=random.Random(999))
        for t1, t2 in zip(s1.values(), s2.values(), strict=True):
            self.assertEqual((hm(t1) - 391) // 5, (hm(t2) - 391) // 5)  # 同一块（时刻略有抖动）

    def test_small_n_random_differs(self):
        """小人数 + 随机排序：循环填块每天重排（不同 seed 结果不同）。"""
        accs = make_accounts(2)
        s1 = signin.build_schedule(accs, order="random", dist="uniform", rng=random.Random(1))
        s2 = signin.build_schedule(accs, order="random", dist="uniform", rng=random.Random(2))
        self.assertNotEqual(s1, s2)

    def test_sequence_uniform_linear_blocks(self):
        """顺序×均匀：线性填块，50 人 → 前 4 块，块内等分。"""
        accs = make_accounts(50)
        sched = signin.build_schedule(
            accs, order="sequence", dist="uniform", rng=random.Random(2))
        self.assertEqual(len(sched), 50)
        p0 = hm(sched[accs[0].phone])
        p14 = hm(sched[accs[14].phone])
        p15 = hm(sched[accs[15].phone])
        p29 = hm(sched[accs[29].phone])
        p30 = hm(sched[accs[30].phone])
        p49 = hm(sched[accs[49].phone])
        # 块0 [06:31,06:35) 15 人；块1 [06:35,06:40)；块2 [06:40,06:45)；块3 [06:45,06:50)
        self.assertTrue(391 <= p0 < 395)
        self.assertTrue(391 <= p14 < 395)
        self.assertTrue(395 <= p15 < 400)
        self.assertTrue(395 <= p29 < 400)
        self.assertTrue(400 <= p30 < 405)
        self.assertTrue(405 <= p49 < 410)
        # 块内等分：块0 首尾间隔 ≥ 2 分钟（等分 240s/15 人，首尾差约 224s）
        self.assertGreaterEqual(p14 - p0, 2)

    def test_random_uniform_balances_blocks(self):
        """随机×均匀：循环填块，50 人铺满 16 块、每块人数均衡。"""
        accs = make_accounts(50)
        sched = signin.build_schedule(
            accs, order="random", dist="uniform", rng=random.Random(3))
        counts = {}
        for t in sched.values():
            bi = (hm(t) - 390) // 5
            counts[bi] = counts.get(bi, 0) + 1
        self.assertEqual(len(counts), 16)  # 铺满所有块
        for bi, c in counts.items():
            self.assertTrue(1 <= c <= 6, (bi, c))  # 50/16 ≈ 3.1

    def test_anchor_z_stable_and_distinct(self):
        """顺序×正态锚点：同一 phone 的 z_i 稳定；不同 phone 不同。"""
        self.assertEqual(signin._anchor_z("13800138000"), signin._anchor_z("13800138000"))
        self.assertNotEqual(signin._anchor_z("13800138000"), signin._anchor_z("13800138001"))

    def test_normal_sequence_daily_range_bounded(self):
        """顺序×正态：同一账号两天波动有界（固定 seed 确定性）。"""
        accs = make_accounts(50)
        s1 = signin.build_schedule(
            accs, order="sequence", dist="normal", rng=random.Random(4))
        s2 = signin.build_schedule(
            accs, order="sequence", dist="normal", rng=random.Random(5))
        phone = accs[7].phone
        self.assertLessEqual(abs(hm(s1[phone]) - hm(s2[phone])), 45)
        # 全局钟形：中间 4 块人数 > 首块+尾块人数
        def block_counts(sched):
            counts = {}
            for t in sched.values():
                bi = (hm(t) - 390) // 5
                counts[bi] = counts.get(bi, 0) + 1
            return counts

        c1 = block_counts(s1)
        mid = sum(c1.get(bi, 0) for bi in (6, 7, 8, 9))  # 07:01~07:21
        edges = c1.get(0, 0) + c1.get(15, 0)             # 首尾块
        self.assertGreater(mid, edges)

    def test_random_normal_shuffles_daily(self):
        """随机×正态：每天重排，两次运行结果不同。"""
        accs = make_accounts(50)
        s1 = signin.build_schedule(
            accs, order="random", dist="normal", rng=random.Random(6))
        s2 = signin.build_schedule(
            accs, order="random", dist="normal", rng=random.Random(7))
        self.assertNotEqual(s1, s2)

    def test_all_combos_within_window(self):
        """四组合 × 多 seed：所有时间 ∈ [06:31, 07:49]，不丢账号。"""
        for order in ("sequence", "random"):
            for dist in ("uniform", "normal"):
                for seed in (10, 11, 12):
                    accs = make_accounts(50)
                    sched = signin.build_schedule(
                        accs, order=order, dist=dist, rng=random.Random(seed))
                    self.assertEqual(len(sched), 50, (order, dist, seed))
                    for t in sched.values():
                        self.assertTrue(EFF_LO <= hm(t) <= EFF_HI, (order, dist, seed, t))

    def test_sigma_eff_cap(self):
        """σ_eff 封顶：n 大时 ≤ 有效窗口/3；n≤20 不变；n 大放大。"""
        self.assertLessEqual(signin._sigma_eff(20, 200, 78), 78 / 3 + 1e-9)
        self.assertEqual(signin._sigma_eff(15, 20, 78), 15)
        self.assertGreater(signin._sigma_eff(15, 80, 78), 15)

    def test_compression_300_all_scheduled(self):
        """压缩模式：n=300 超容量（240），全部账号拿到时间点且不越界。"""
        accs = make_accounts(300)
        sched = signin.build_schedule(
            accs, order="sequence", dist="uniform", rng=random.Random(8))
        self.assertEqual(len(sched), 300)
        for t in sched.values():
            self.assertTrue(EFF_LO <= hm(t) <= EFF_HI, t)

    def test_legacy_sign_mode_mapping(self):
        """兼容：旧 YIBAN_SIGN_MODE=normal → 顺序×正态。"""
        os.environ["YIBAN_SIGN_MODE"] = "normal"
        try:
            cfg = signin._schedule_config()
            self.assertEqual(cfg["order"], "sequence")
            self.assertEqual(cfg["dist"], "normal")
        finally:
            os.environ.pop("YIBAN_SIGN_MODE", None)

    def test_seed_reproducible(self):
        """固定 seed 可复现（随机性测试防 flaky 的基础）。"""
        accs = make_accounts(50)
        s1 = signin.build_schedule(
            accs, order="random", dist="normal", rng=random.Random(99))
        s2 = signin.build_schedule(
            accs, order="random", dist="normal", rng=random.Random(99))
        self.assertEqual(s1, s2)

    # ============ 对抗性审查补充（2026-08-15） ============

    def test_narrow_window_large_edge_no_crash(self):
        """对抗：窗口 06:30~06:40 + edge 600s → 有效窗口为空，不得崩溃（回退默认窗口）。"""
        os.environ["YIBAN_SIGN_START"] = "06:30"
        os.environ["YIBAN_SIGN_END"] = "06:40"
        os.environ["YIBAN_WINDOW_EDGE_SEC"] = "600"
        try:
            accs = make_accounts(3)
            sched = signin.build_schedule(
                accs, order="sequence", dist="uniform", rng=random.Random(1))
            # 不崩溃且账号仍拿到时间点（回退默认窗口 06:30~07:50）
            self.assertEqual(len(sched), 3)
            for t in sched.values():
                self.assertTrue(391 <= hm(t) <= 469, t)
        finally:
            for k in ("YIBAN_SIGN_START", "YIBAN_SIGN_END", "YIBAN_WINDOW_EDGE_SEC"):
                os.environ.pop(k, None)

    def test_edge_sec_600_default_window(self):
        """对抗：默认窗口 + edge 600s → 有效窗口 [06:40, 07:40]，正常调度不越界。"""
        os.environ["YIBAN_WINDOW_EDGE_SEC"] = "600"
        try:
            accs = make_accounts(10)
            sched = signin.build_schedule(
                accs, order="sequence", dist="uniform", rng=random.Random(2))
            self.assertEqual(len(sched), 10)
            for t in sched.values():
                self.assertTrue(400 <= hm(t) <= 469, t)  # eff_lo=400
        finally:
            os.environ.pop("YIBAN_WINDOW_EDGE_SEC", None)

    def test_edge_600_normal_blocks_aligned(self):
        """对抗：edge=600s（首块被掐）时 normal 落块不得错位（全部落在有效块内且分布正常）。"""
        os.environ["YIBAN_WINDOW_EDGE_SEC"] = "600"
        try:
            accs = make_accounts(200)
            sched = signin.build_schedule(
                accs, order="random", dist="normal", rng=random.Random(21))
            self.assertEqual(len(sched), 200)
            for t in sched.values():
                self.assertTrue(400 <= hm(t) <= 469, t)  # 有效窗口 [06:40, 07:40]
        finally:
            os.environ.pop("YIBAN_WINDOW_EDGE_SEC", None)

    def test_pref_slot_with_non_multiple_start(self):
        """对抗：窗口起点非 5 分钟倍数（06:32）→ 自选 slot 仍精确落到所选片（与 web 口径一致）。"""
        os.environ["YIBAN_SIGN_START"] = "06:32"
        os.environ["YIBAN_SIGN_END"] = "07:50"
        os.environ["YIBAN_WINDOW_EDGE_SEC"] = "60"
        try:
            accs = make_accounts(4)
            prefs = {
                accs[0].phone: {"slot_min": 0, "updated_at": "2026-08-15 08:00:00"},
                accs[1].phone: {"slot_min": 5, "updated_at": "2026-08-15 08:01:00"},
            }
            sched = signin.build_schedule(
                accs, order="sequence", dist="uniform",
                rng=random.Random(3), prefs=prefs)
            # slot0 → 块0 [06:33, 06:37)；slot5 → 块1 [06:37, 06:42)
            t0 = hm(sched[accs[0].phone])
            t1 = hm(sched[accs[1].phone])
            self.assertTrue(393 <= t0 < 397, t0)   # 06:33~06:37
            self.assertTrue(397 <= t1 < 402, t1)   # 06:37~06:42
        finally:
            for k in ("YIBAN_SIGN_START", "YIBAN_SIGN_END", "YIBAN_WINDOW_EDGE_SEC"):
                os.environ.pop(k, None)


if __name__ == "__main__":
    unittest.main(verbosity=2)
