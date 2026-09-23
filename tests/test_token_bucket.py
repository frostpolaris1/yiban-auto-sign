# -*- coding: utf-8 -*-
"""`yiban/engine/token_bucket.py` 的契约用例：GCRA/TAT 令牌桶、AIMD、全局 Λ、gap 门、EWMA。

全部用例注入 `now`（浮点秒），不真实 sleep；落库用例用临时库（与 test_store_queue 同手法）。
速率单位一律是**账号尝试/s**（attempt/s）——`rate=1` 的 T 是 1.0s，不是 1/6。
"""
import contextlib
import os
import random
import shutil
import tempfile
import unittest
from unittest import mock

import db

from yiban.engine import token_bucket
from yiban.store import queue_store

TEST_KEY = "a" * 64


class ConstantsTest(unittest.TestCase):
    """模块常量的取值本身是设计：改动它们等于改设计，必须被用例挡住。"""

    def test_constants_match_contract(self):
        self.assertEqual(token_bucket.RATE_MIN, 0.2)
        self.assertEqual(token_bucket.RATE_MAX, 4.0)
        self.assertEqual(token_bucket.RATE_DEFAULT, 1.0)
        self.assertEqual(token_bucket.SUCCESS_STREAK, 200)
        self.assertEqual(token_bucket.HALF_OPEN_SEC, 300)
        self.assertEqual(token_bucket.EWMA_ALPHA, 0.2)
        self.assertEqual(token_bucket.EWMA_BETA, 0.5)
        self.assertEqual(token_bucket.MAX_STEP, 0.20)


class BucketTest(unittest.TestCase):
    """单出口桶的放行判据与突发额度。"""

    def test_01_zero_burst_strict_interval(self):
        """burst=0：严格 1/T 间隔（首个尝试仍放行，不是"必须先等一个 T"）。"""
        b = token_bucket.EgressBucket("e0", rate=1.0, burst=0)
        self.assertTrue(b.try_acquire(0.0))
        self.assertFalse(b.try_acquire(0.5))
        self.assertTrue(b.try_acquire(1.0))

    def test_02_burst_allowance_then_retry(self):
        """burst=6：now=0 连放 6 条，第 7 条失败且 retry_after ≈ 1.0s。"""
        b = token_bucket.EgressBucket("e0", rate=1.0, burst=6)
        self.assertAlmostEqual(b.burst_sec(), 5.0, places=9,
                               msg="容量 6 的桶 ⇔ τ=(burst−1)·T=5s")
        for i in range(6):
            self.assertTrue(b.try_acquire(0.0), f"第 {i + 1} 次突发应放行")
        self.assertFalse(b.try_acquire(0.0), "第 7 次超出突发额度")
        self.assertAlmostEqual(b.retry_after(0.0), 1.0, places=9)
        self.assertAlmostEqual(b.wait_sec(0.0), b.retry_after(0.0), places=9)
        self.assertTrue(b.try_acquire(1.0), "等满 retry_after 后必放行")

    def test_03_tat_not_accumulating(self):
        """`tat = max(now, tat) + T`：长期空闲后仍只放行 burst 条，不"积攒"上千条。"""
        fresh = token_bucket.EgressBucket("e0", rate=1.0, burst=6)
        self.assertEqual(sum(1 for _ in range(1000) if fresh.try_acquire(1000.0)), 6)
        used = token_bucket.EgressBucket("e0", rate=1.0, burst=6)
        for _ in range(6):
            used.try_acquire(0.0)
        self.assertEqual(sum(1 for _ in range(1000) if used.try_acquire(1000.0)), 6)


class AimdTest(unittest.TestCase):
    """每出口独立的 AIMD：事件式上探/回退 + 半开。"""

    def test_04_success_streak_grows_rate_to_cap(self):
        lim = token_bucket.EgressLimiter(rate=1.0, burst=6)
        for _ in range(token_bucket.SUCCESS_STREAK - 1):
            lim.on_success("e0")
        self.assertAlmostEqual(lim.snapshot()["e0"]["rate"], 1.0, places=9,
                               msg="不满 200 次不上探")
        self.assertAlmostEqual(lim.on_success("e0"), 1.2, places=9)
        for _ in range(token_bucket.SUCCESS_STREAK):
            lim.on_success("e0")
        self.assertAlmostEqual(lim.snapshot()["e0"]["rate"], 1.44, places=9)
        for _ in range(100):                       # 再推 100 轮
            for _ in range(token_bucket.SUCCESS_STREAK):
                lim.on_success("e0")
        self.assertAlmostEqual(lim.snapshot()["e0"]["rate"], token_bucket.RATE_MAX,
                               places=9, msg="上探封顶 4.0 attempt/s")

    def test_05_risk_signal_halves_rate_to_floor(self):
        lim = token_bucket.EgressLimiter(rate=1.6, burst=6)
        self.assertAlmostEqual(lim.on_risk_signal("e0", 0.0), 0.8, places=9)
        for t in range(1, 12):
            lim.on_risk_signal("e0", float(t))
        self.assertAlmostEqual(lim.snapshot()["e0"]["rate"], token_bucket.RATE_MIN,
                               places=9, msg="回退下限 0.2 attempt/s，不再下降")

    def test_06_half_open_window_and_single_lane(self):
        lim = token_bucket.EgressLimiter(rate=1.0, burst=6)
        lim.on_risk_signal("e0", 0.0)
        self.assertTrue(lim.is_half_open("e0", 1.0))
        self.assertFalse(lim.is_half_open("e0", token_bucket.HALF_OPEN_SEC))
        self.assertFalse(lim.is_half_open("e0", 301.0))

        half = token_bucket.EgressLimiter(rate=1.0, burst=6)
        half.on_risk_signal("e0", 0.0)             # rate → 0.5 且半开
        lane1 = half.acquire("e0", 1.0)
        lane2 = half.acquire("e0", 1.0)            # 第二条通道
        self.assertTrue(lane1)
        self.assertFalse(lane2, "半开期同一出口只允许 1 条并发探测")

    def test_07_risk_signal_resets_success_streak(self):
        """风控信号清零 streak：其后的 199 次成功**不**算作上一轮的续集。"""
        lim = token_bucket.EgressLimiter(rate=1.0, burst=6)
        for _ in range(token_bucket.SUCCESS_STREAK - 1):
            lim.on_success("e0")
        lim.on_risk_signal("e0", 0.0)
        after_risk = lim.snapshot()["e0"]["rate"]
        self.assertAlmostEqual(after_risk, 0.5, places=9)
        for _ in range(token_bucket.SUCCESS_STREAK - 1):
            lim.on_success("e0")
        self.assertAlmostEqual(lim.snapshot()["e0"]["rate"], after_risk, places=9,
                               msg="199 次成功不足以触发上探（streak 已从 0 重计）")

    def test_08_downgrade_all_to_floor(self):
        """站点级熔断的降档入口：全体出口降到下限并半开。"""
        lim = token_bucket.EgressLimiter(rate=4.0, burst=6)
        for egress in ("e0", "e1"):
            for _ in range(6):
                lim.acquire(egress, 0.0)
        got = lim.downgrade_all(10.0)
        self.assertEqual(got, {"e0": token_bucket.RATE_MIN, "e1": token_bucket.RATE_MIN})
        self.assertTrue(lim.is_half_open("e0", 11.0))
        self.assertTrue(lim.is_half_open("e1", 11.0))

    def test_08b_rate_stays_within_bounds(self):
        rng = random.Random(20260923)
        lim = token_bucket.EgressLimiter(rate=1.0, burst=6)
        for i in range(500):
            # 30% 风控信号（事件式回退）、其余无风控（事件式上探）
            got = (lim.on_risk_signal("e0", float(i)) if rng.random() < 0.3
                   else lim.on_success("e0"))
            self.assertGreaterEqual(got, token_bucket.RATE_MIN)
            self.assertLessEqual(got, token_bucket.RATE_MAX)
            self.assertGreaterEqual(lim.snapshot()["e0"]["rate"], token_bucket.RATE_MIN)
            self.assertLessEqual(lim.snapshot()["e0"]["rate"], token_bucket.RATE_MAX)


class EwmaTest(unittest.TestCase):
    """外环：目标跟踪的连续微调，单次幅度夹 ±20%。"""

    def test_09_apply_ewma_steps(self):
        # 1.0×(1+0.5×1.0)=1.5 → 被 ±20% 夹到 1.2
        self.assertAlmostEqual(token_bucket.apply_ewma(1.0, 0.4, 0.2), 1.2, places=9)
        self.assertAlmostEqual(token_bucket.apply_ewma(1.0, 0.2, 0.2), 1.0, places=9)
        self.assertAlmostEqual(token_bucket.apply_ewma(0.2, 0.0, 0.2), 0.2, places=9)
        self.assertAlmostEqual(token_bucket.apply_ewma(1.0, 1.0, 0.0), 1.0, places=9,
                               msg="R_target 非法时不调整（不做除零）")


class GlobalTest(unittest.TestCase):
    """全局 Λ：与出口数 K 无关的总量上界。"""

    def test_10_global_lam_caps_attempts(self):
        g = token_bucket.GlobalLimiter(2.0)
        self.assertTrue(g.acquire(0.0))
        self.assertTrue(g.acquire(0.5))
        self.assertFalse(g.acquire(0.6), "1s 内第 3 次放行必须被 Λ 拦下")
        self.assertTrue(g.acquire(1.0))

        window = token_bucket.GlobalLimiter(2.0)
        admitted = sum(1 for i in range(10) if window.acquire(i * 0.1))
        self.assertEqual(admitted, 2, "任意 1s 内放行数 ≤ Λ")

        for lam in (None, 0.0, -1.0):
            unlimited = token_bucket.GlobalLimiter(lam)
            self.assertTrue(all(unlimited.acquire(0.0) for _ in range(100)),
                            f"Λ={lam!r} 表示不限，永不失败")

        for k in (1, 10):                          # 同一时刻来抢的出口数
            shared = token_bucket.GlobalLimiter(2.0)
            self.assertEqual(sum(1 for _ in range(k) if shared.acquire(0.0)), 1,
                             "总量由 Λ 决定，与出口数 K 无关")


class GapGateTest(unittest.TestCase):
    """每账号 gap 安全件（与出口桶并存，不替代）。"""

    def test_11_account_gap_gate(self):
        gate = token_bucket.AccountGapGate(gap_sec=10)
        self.assertTrue(gate.allow("13800000001", 0.0))
        gate.commit("13800000001", 0.0)
        self.assertFalse(gate.allow("13800000001", 5.0))
        self.assertTrue(gate.allow("13800000001", 10.0))
        self.assertTrue(gate.allow("13800000002", 5.0), "不同账号互不影响")

        off = token_bucket.AccountGapGate(gap_sec=10, enabled=False)
        for t in (0.0, 0.0, 1.0):
            self.assertTrue(off.allow("13800000001", t))
            off.commit("13800000001", t)

    def test_11b_gap_gate_from_env(self):
        keys = (token_bucket.ENV_ACCOUNT_GAP_MAX, token_bucket.ENV_GAP_ENFORCE)
        saved = {k: os.environ.get(k) for k in keys}
        try:
            for k in keys:
                os.environ.pop(k, None)
            gate = token_bucket.gap_gate_from_env()
            self.assertEqual(gate.gap_sec, 10, "缺省沿用 YIBAN_ACCOUNT_GAP_MAX 的 10s")
            self.assertTrue(gate.enabled, "缺省 1=开")
            os.environ[token_bucket.ENV_GAP_ENFORCE] = "0"
            os.environ[token_bucket.ENV_ACCOUNT_GAP_MAX] = "30"
            gate = token_bucket.gap_gate_from_env()
            self.assertEqual(gate.gap_sec, 30)
            self.assertFalse(gate.enabled)
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


class UnitTest(unittest.TestCase):
    """单位断言：防退回"1 req/s"的字面实现（那会让实际请求量只有设计的 1/6）。"""

    def test_12_rate_unit_is_attempt_per_sec(self):
        self.assertEqual(token_bucket.RATE_DEFAULT, 1.0)
        self.assertIn("attempt/s", token_bucket.__doc__ or "")
        self.assertIn("attempt/s", token_bucket.EgressBucket.__doc__ or "")
        self.assertAlmostEqual(token_bucket.EgressBucket("e0", rate=1.0).interval, 1.0,
                               places=9, msg="rate=1 的 T 恰为 1.0s（1 attempt/s）")


class PersistTest(unittest.TestCase):
    """落库往返与重启恢复：`egress_state` 是桶状态的唯一持久化路径。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-egress-")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n")
        os.environ.update({
            "YIBAN_ACCOUNTS_KEY": TEST_KEY,
            "YIBAN_ENV_FILE": cls.env_file,
            "YIBAN_DB_FILE": cls.db_file,
        })

    @classmethod
    def tearDownClass(cls):
        cls._close_conn()
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k in ("YIBAN_ACCOUNTS_KEY", "YIBAN_ENV_FILE", "YIBAN_DB_FILE"):
            os.environ.pop(k, None)

    @staticmethod
    def _close_conn():
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None

    def setUp(self):
        self._close_conn()
        for suffix in ("", "-wal", "-shm"):
            p = self.db_file + suffix
            if os.path.exists(p):
                os.remove(p)
        db.init_db(self.db_file, env_file=self.env_file, cleanup=False)

    def tearDown(self):
        self._close_conn()

    def test_13_roundtrip_and_upsert(self):
        self.assertTrue(queue_store.save_egress_state("e0", 1.5, 6, 123.4))
        self.assertEqual(queue_store.load_egress_state("e0"),
                         {"rate": 1.5, "burst": 6.0, "tat": 123.4})
        self.assertIsNone(queue_store.load_egress_state("e404"), "无记录 → None")

        queue_store.save_egress_state("e0", 0.8, 2, 7.0)
        self.assertEqual(queue_store.load_egress_state("e0"),
                         {"rate": 0.8, "burst": 2.0, "tat": 7.0})
        conn = db.get_conn()
        n = conn.execute("SELECT COUNT(*) FROM egress_state").fetchone()[0]
        self.assertEqual(n, 1, "同一出口只留一行（UPSERT）")

    def test_13b_db_error_degrades_without_raising(self):
        with mock.patch.object(queue_store, "_queue_conn",
                               side_effect=RuntimeError("db down")):
            self.assertIsNone(queue_store.load_egress_state("e0"))
            self.assertFalse(queue_store.save_egress_state("e0", 1.0, 6, 0.0))

    def test_13c_limiter_persist_and_restore(self):
        lim = token_bucket.EgressLimiter(rate=1.0, burst=6)
        for _ in range(6):
            lim.acquire("e0", 0.0)
        self.assertTrue(lim.persist("e0"))
        saved = queue_store.load_egress_state("e0")
        self.assertAlmostEqual(saved["rate"], 1.0, places=9)
        self.assertAlmostEqual(saved["tat"], 6.0, places=9)

        fresh = token_bucket.EgressLimiter()
        self.assertTrue(fresh.restore_from_store("e0", now=6.0),
                        "崩溃重启后必须能装回速率与 TAT")
        self.assertAlmostEqual(fresh.snapshot()["e0"]["rate"], 1.0, places=9)
        self.assertAlmostEqual(fresh.snapshot()["e0"]["tat"], 6.0, places=9)

        empty = token_bucket.EgressLimiter()
        self.assertFalse(empty.restore_from_store("e404", now=0.0),
                         "无记录 → 保持出厂速率")
        self.assertEqual(empty.snapshot(), {})

    def test_13d_restore_clamps_foreign_clock_tat(self):
        """持久化的 TAT 可能与本次进程不同时钟域（如 monotonic 跨重启归零），
        超前的 TAT 会把桶误锁很久——装回时必须夹到 `now + T`。"""
        queue_store.save_egress_state("e0", 1.0, 6, 1e9)
        lim = token_bucket.EgressLimiter()
        self.assertTrue(lim.restore_from_store("e0", now=100.0))
        self.assertLessEqual(lim.snapshot()["e0"]["tat"], 100.0 + 1.0)
