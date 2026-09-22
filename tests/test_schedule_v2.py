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
import contextlib
import importlib.util
import os
import random
import shutil
import sys
import tempfile
import unittest
from datetime import datetime as _dt
from unittest import mock

import db
import signin

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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


sys.path.insert(0, os.path.join(BASE, "scripts"))


TEST_KEY = "a" * 64


ADMIN_PASS = "TestPass1234!"


USER_PASS = "secret1"


EMAIL = "u1@test.local"


PHONE = "13800138000"


class _WebBase(unittest.TestCase):
    """临时 .env/DB + 全新 app（与既有 web 类测试同一骨架）。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-d6-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        os.environ.update({
            "YIBAN_ACCOUNTS_KEY": TEST_KEY,
            "YIBAN_ENV_FILE": cls.env_file,
            "YIBAN_DB_FILE": cls.db_file,
            "YIBAN_STATE_DIR": cls.tmp,
            "YIBAN_LOG_FILE": os.path.join(cls.tmp, "sign.log"),
        })
        spec = importlib.util.spec_from_file_location(
            "webapp", os.path.join(BASE, "web", "app.py"))
        cls.webapp = importlib.util.module_from_spec(spec)
        sys.modules["webapp"] = cls.webapp
        with contextlib.suppress(Exception):
            spec.loader.exec_module(cls.webapp)

    @classmethod
    def tearDownClass(cls):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k in ("YIBAN_ACCOUNTS_KEY", "YIBAN_ENV_FILE", "YIBAN_DB_FILE",
                  "YIBAN_STATE_DIR", "YIBAN_LOG_FILE"):
            os.environ.pop(k, None)

    def setUp(self):
        with open(self.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
                    f"YIBAN_ADMIN_USER=admin\nYIBAN_ADMIN_PASSWORD={ADMIN_PASS}\n")
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        for suffix in ("", "-wal", "-shm"):
            p = self.db_file + suffix
            if os.path.exists(p):
                os.remove(p)
        db.init_db(self.db_file, env_file=self.env_file, cleanup=False)
        db.create_user(EMAIL, self.webapp.generate_password_hash(USER_PASS))
        db.add_account({"name": "我的号", "phone": PHONE, "password": "pw",
                        "owner": EMAIL, "status": "active",
                        "phone_model": "", "phone_code": ""})
        self.app = self.webapp.create_app()
        self.c = self.app.test_client()

    def _login(self):
        r = self.c.post("/api/login", json={"username": EMAIL, "password": USER_PASS})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return self.c.get("/api/me").get_json()["csrf_token"]


class WindowRecheckAfterSleepTest(unittest.TestCase):
    """SCH-10：等待/间隔对齐之后必须再判一次窗口。"""

    def setUp(self):
        for k in ("YIBAN_RETRY_MIN_INTERVAL", "YIBAN_SIGN_START", "YIBAN_SIGN_END",
                  "YIBAN_WINDOW_EDGE_SEC", "YIBAN_WINDOW_EDGE_FRONT_SEC",
                  "YIBAN_WINDOW_EDGE_BACK_SEC", "YIBAN_MIN_EXEC_GAP", "YIBAN_EXEC_GAP_MIN"):
            os.environ.pop(k, None)
        self.tmp = tempfile.mkdtemp(prefix="yiban-sch10-")
        os.environ["YIBAN_STATE_DIR"] = self.tmp
        os.environ["YIBAN_SIGN_END"] = "07:50"

    def tearDown(self):
        for k in ("YIBAN_STATE_DIR", "YIBAN_SIGN_END"):
            os.environ.pop(k, None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_no_request_after_window_passes_during_wait(self):
        """到点时间在窗口内、但等完之后已过窗口 → 零请求且记为窗口外跳过。"""
        start = signin.clock.now().replace(hour=7, minute=0, second=0, microsecond=0)
        # 第一次取时（弹出判窗口）= 07:00；睡完之后（二次判定）= 08:00
        calls = {"n": 0}

        def fake_now():
            calls["n"] += 1
            return start if calls["n"] <= 2 else start.replace(hour=8, minute=0)

        acc = signin.Account(phone=PHONE, password="p")
        with mock.patch.object(signin.clock, "now", side_effect=fake_now), \
                mock.patch.object(signin, "attempt_signin") as attempt, \
                mock.patch.object(signin, "_update_cred_state"), \
                mock.patch.object(signin.time, "sleep"), \
                mock.patch.object(signin.time, "monotonic", return_value=100.0):
            results = signin.run_queue_retry(
                [acc], "", 0, 0,
                schedule={PHONE: start + signin.timedelta(seconds=30)})
        self.assertEqual(attempt.call_count, 0, "越过窗口后不得再发起请求")
        self.assertEqual(results[PHONE][3], signin.STATUS_SKIPPED_WINDOW)

    def test_normal_wait_still_executes(self):
        """窗口内等待后照常执行（对照组，防误拦）。"""
        at = signin.clock.now().replace(hour=7, minute=0, second=0, microsecond=0)
        acc = signin.Account(phone=PHONE, password="p")
        with mock.patch.object(signin.clock, "now", return_value=at), \
                mock.patch.object(signin, "attempt_signin",
                                  return_value=(True, "ok", False, signin.STATUS_SUCCESS)) as attempt, \
                mock.patch.object(signin, "_update_cred_state"), \
                mock.patch.object(signin.time, "sleep"), \
                mock.patch.object(signin.time, "monotonic", return_value=100.0):
            signin.run_queue_retry([acc], "", 0, 0,
                                   schedule={PHONE: at + signin.timedelta(seconds=30)})
        self.assertEqual(attempt.call_count, 1)


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
            with mock.patch.object(signin.clock, "now", FakeNow.now), \
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
    unittest.main(verbosity=2)
