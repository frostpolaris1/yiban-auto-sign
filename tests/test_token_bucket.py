# -*- coding: utf-8 -*-
"""`yiban/engine/token_bucket.py` 的契约用例：GCRA/TAT 令牌桶、AIMD、全局 Λ、gap 门、EWMA。

全部用例注入 `now`（浮点秒），不真实 sleep；落库用例用临时库（与 test_store_queue 同手法）。
速率单位一律是**账号尝试/s**（attempt/s）——`rate=1` 的 T 是 1.0s，不是 1/6。
"""
import contextlib
import math
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
        # 剩余等待随 now 递减：tat=6、τ=5 ⇒ admit_at=1，now=0.5 时还差 0.5s，过了就不再等
        self.assertAlmostEqual(b.wait_sec(0.5), 0.5, places=9)
        self.assertAlmostEqual(b.wait_sec(2.0), 0.0, places=9)
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

    def test_06b_half_open_is_time_bounded(self):
        """探测成功**不**结束半开：冷却窗按时间走表，否则一次侥幸就换回 6 条并发。"""
        lim = token_bucket.EgressLimiter(rate=1.0, burst=6)
        lim.on_risk_signal("e0", 0.0)              # rate → 0.5（T=2s），半开 [0, 300)
        self.assertTrue(lim.acquire("e0", 1.0))
        lim.on_success("e0")                       # 探测成功
        self.assertTrue(lim.is_half_open("e0", 2.0), "成功不该提前结束半开")
        self.assertFalse(lim.acquire("e0", 2.0), "半开未结束 ⇒ 仍只放单通道")
        self.assertTrue(lim.acquire("e0", 3.0), "等满一个 T 后允许下一次探测")
        self.assertTrue(lim.is_half_open("e0", token_bucket.HALF_OPEN_SEC - 1.0))
        admitted = sum(1 for _ in range(20)
                       if lim.acquire("e0", float(token_bucket.HALF_OPEN_SEC)))
        self.assertEqual(admitted, 6, "冷却走完后突发额度回到 burst=6")

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

    def test_08c_downgrade_sticks_for_new_egress(self):
        """站点级降档是粘住的：降档后**新出现**的出口也按降档值起算。

        只降已建桶的出口会让站点级熔断对新出口静默失效——降档期间冒出来的出口仍按
        出厂速率放行，等于漏掉一类出口。
        """
        lim = token_bucket.EgressLimiter(rate=4.0, burst=6)
        lim.acquire("e0", 0.0)
        lim.downgrade_all(10.0)
        self.assertAlmostEqual(lim.snapshot()["e0"]["rate"], token_bucket.RATE_MIN, places=9)
        self.assertAlmostEqual(lim.bucket("e_new").rate, token_bucket.RATE_MIN, places=9,
                               msg="降档后新建的出口从降档值起算，不是出厂速率 4.0")

    def test_08b_rate_stays_within_bounds(self):
        # 固定常数种子：序列可复现，换种子会走到另一条回退/上探路径
        rng = random.Random(12345)
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

    def test_09b_apply_ewma_takes_no_alpha_parameter(self):
        """平滑系数在调用侧使用，函数本身只吃（prev, r̂, R_target）——不留口径占位形参。"""
        with self.assertRaises(TypeError):
            token_bucket.apply_ewma(1.0, 0.4, 0.2, alpha=0.2)


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

    def test_10b_non_integer_lam_rounds_up_within_a_second(self):
        """任意 1s 内的放行数是 ⌈Λ⌉（GCRA 的边界取整），不是 Λ 本身。"""
        for lam in (0.4, 1.0, 1.5, 2.0, 3.5):
            g = token_bucket.GlobalLimiter(lam)
            admitted = sum(1 for i in range(1000) if g.acquire(i * 0.001))
            self.assertEqual(admitted, math.ceil(lam), f"Λ={lam} 时首秒放行数")

    def test_10c_non_numeric_lam_warns_and_marks_invalid(self):
        """非数值 Λ 不得静默吞成「不限」：告警 + `invalid` 标记（供接线侧决定是否拒启）。"""
        with self.assertLogs("yiban.engine.token_bucket", level="WARNING") as cm:
            g = token_bucket.GlobalLimiter("abc")
        self.assertTrue(g.invalid, "「取值非法、已按不限处理」这一事实必须可读")
        self.assertIsNone(g.lam)
        self.assertTrue(all(g.acquire(0.0) for _ in range(10)))
        self.assertTrue(any("不限" in m for m in cm.output), cm.output)
        for lam in (None, 0.0, -1.0, "2.0"):
            with self.subTest(lam=lam):
                self.assertFalse(token_bucket.GlobalLimiter(lam).invalid,
                                 "未配/显式不限/正常数值都不算「非法」")


class ConfigTest(unittest.TestCase):
    """配置面收口：`YIBAN_MIN_EXEC_GAP` → 突发额度；速率与突发一次读全。"""

    @contextlib.contextmanager
    def _env(self, key, raw):
        """临时把 `key` 设为 `raw`（None = 不设该键），退出时还原。"""
        saved = os.environ.get(key)
        try:
            if raw is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = raw
            yield
        finally:
            if saved is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = saved

    def test_14_min_exec_gap_maps_into_burst(self):
        cap = token_bucket.burst_cap
        self.assertEqual(cap(rate=1.0, gap_sec=5, channels=6), 6.0,
                         "缺省 gap=5s、rate=1 ⇒ 恰好等于通道数 M")
        self.assertEqual(cap(rate=1.0, gap_sec=1, channels=6), 2.0,
                         "收紧 gap ⇒ 突发收紧（1 + 1×1）")
        self.assertEqual(cap(rate=4.0, gap_sec=5, channels=16), 16.0,
                         "gap 宽时由通道数封顶")
        self.assertEqual(cap(rate=0.2, gap_sec=5, channels=2), 2.0)
        self.assertEqual(cap(rate=1.0, gap_sec=5, channels=0), 1.0, "通道数为 0 仍有 1 条")

        for raw, want in ((None, 6.0), ("1", 2.0), ("60", 6.0)):
            with self.subTest(gap=raw), self._env(token_bucket.ENV_MIN_EXEC_GAP, raw):
                self.assertEqual(token_bucket.burst_from_env(6, 1.0), want)

    def test_15_limiter_from_env_reads_rate_and_burst(self):
        with (self._env("YIBAN_EGRESS_RATE", "2.0"),
              self._env(token_bucket.ENV_MIN_EXEC_GAP, None)):
            lim = token_bucket.limiter_from_env(channels=8)
            self.assertAlmostEqual(lim.rate, 2.0, places=9, msg="rate = YIBAN_EGRESS_RATE")
            self.assertAlmostEqual(lim.burst, 8.0, places=9, msg="gap=5s 宽 ⇒ 通道数封顶")
        with (self._env("YIBAN_EGRESS_RATE", "2.0"),
              self._env(token_bucket.ENV_MIN_EXEC_GAP, "1")):
            self.assertAlmostEqual(token_bucket.limiter_from_env(channels=8).burst, 3.0,
                                   places=9, msg="1 + 1s × 2.0 attempt/s")

    def test_16_manual_rate_stops_aimd_but_keeps_safety(self):
        """`.env` 显式配速率 = 人工接管：AIMD 不再改写速率，安全反应照做。

        取舍：管理员手写的速率就该按它跑（否则"我配的值第二天自己变了"无从解释）；
        但半开、站点级降档这类**安全**反应不随之停——停掉会让一次手写错值变成
        无保护的全速放行。
        """
        lim = token_bucket.EgressLimiter(rate=1.0, burst=6, manual=True)
        for _ in range(token_bucket.SUCCESS_STREAK * 3):
            lim.on_success("e0")
        self.assertAlmostEqual(lim.snapshot()["e0"]["rate"], 1.0, places=9,
                               msg="人工接管：上探不改写速率")
        with self.assertLogs("yiban.engine.token_bucket", level="WARNING"):
            got = lim.on_risk_signal("e0", 0.0)
        self.assertAlmostEqual(got, 1.0, places=9, msg="人工接管：风控不改写速率")
        self.assertTrue(lim.is_half_open("e0", 1.0), "安全反应不停：风控仍触发半开")
        self.assertTrue(lim.acquire("e0", 1.0))
        self.assertFalse(lim.acquire("e0", 1.0), "半开期仍只放单通道探测")
        lim.downgrade_all(5.0)
        self.assertAlmostEqual(lim.snapshot()["e0"]["rate"], token_bucket.RATE_MIN, places=9,
                               msg="站点级降档是安全反应，人工接管不阻止它")

    def test_16b_limiter_from_env_marks_manual_only_when_rate_configured(self):
        with self._env("YIBAN_EGRESS_RATE", None):
            self.assertFalse(token_bucket.limiter_from_env(channels=6).manual,
                             "未配速率 ⇒ 自适应照常")
        with self._env("YIBAN_EGRESS_RATE", "2.0"):
            lim = token_bucket.limiter_from_env(channels=6)
            self.assertTrue(lim.manual, "显式配了速率 ⇒ 人工接管")
            self.assertAlmostEqual(lim.rate, 2.0, places=9)


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

    def test_11c_gap_enforce_invalid_value_falls_back_to_on(self):
        """安全件的非法值回退到**开**：`abc` 不是「关闭」的同义词。

        该键语义是"缺省开"，手写错值只能读成"没表达清楚"，不能读成"关掉安全件"。
        """
        for raw in ("abc", "maybe"):
            with self.subTest(raw=raw), \
                    mock.patch.dict(os.environ, {token_bucket.ENV_GAP_ENFORCE: raw}), \
                    self.assertLogs("yiban.engine.token_bucket", level="WARNING") as cm:
                gate = token_bucket.gap_gate_from_env()
                self.assertTrue(gate.enabled, "安全件不得因手写错值而消失")
                self.assertTrue(any(token_bucket.ENV_GAP_ENFORCE in m for m in cm.output),
                                cm.output)


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

        self.assertTrue(lim.persist("e0", stamp="2026-09-23 07:00:00"))
        row = db.get_conn().execute(
            "SELECT updated_at FROM egress_state WHERE egress='e0'").fetchone()
        self.assertEqual(row["updated_at"], "2026-09-23 07:00:00",
                         "persist 的 stamp 是墙钟写入时刻，不是桶的浮点时钟")

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
