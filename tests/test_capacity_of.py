# -*- coding: utf-8 -*-
"""容量口径按开关分派：`schedule.capacity_of` / `schedule.executor_count` 与四处调用点。

覆盖（对应简报 ⑤ 的容量部分）：
1. `capacity_of(..., enabled=False)` 与 `capacity_accounts(...)` 多组逐值相同（v2 侧硬门）；
2. `capacity_of(..., enabled=True)` 与 `capacity_accounts_v3(...)` 多组逐值相同；
3. `executor_count`（K 的唯一口径）的边界：夹到 `[1, 出口数]`、随 N 单调不减、
   `bucket_rate` 变小则 K 不变或变大；
4. 四处调用点在开关缺省 0 时**数值不变**：`web/services/capacity.py` 与
   `yiban/engine/runner.py` 两处用显式期望值钉住，另两处（`settings_api` / `cli`）
   断言调用签名把 `k=1`（单执行体语义）与开关分派一起传给 `capacity_of`。

依赖：假时钟与假配置快照（runner 预检不读真实 .env、不联网、不落库）。
"""
import os
import shutil
import tempfile
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest import mock

from web.services import capacity as cap_service
from yiban.engine import runner as runner_mod
from yiban.engine import schedule

DAY = "2026-09-22"
#: 固定起跑时刻：默认窗口 06:30~07:50（有效窗口 06:31~07:49），06:40 在窗口内
START = datetime(2026, 9, 22, 6, 40, 0)
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class CapacityOfDispatchTest(unittest.TestCase):
    """`capacity_of` 只是按开关分派，不改任何一个公式的取值。"""

    def test_disabled_matches_v2_formula_verbatim(self):
        for ws in (0, 2, 3, 100, 4140, 4200, 4800):
            for gap in (0, 10, 60):
                for avg in (1, 3, 8):
                    with self.subTest(ws=ws, gap=gap, avg=avg):
                        self.assertEqual(
                            schedule.capacity_of(ws, gap=gap, avg=avg, enabled=False),
                            schedule.capacity_accounts(ws, gap, avg))

    def test_disabled_ignores_v3_only_arguments(self):
        """开关关时 k/bucket_rate/util 一律不参与——v2 侧逐字不变。"""
        base = schedule.capacity_accounts(4200, 10, 3)
        self.assertEqual(
            schedule.capacity_of(4200, gap=10, avg=3, k=8, bucket_rate=4.0, util=0.1,
                                 enabled=False),
            base)

    def test_enabled_matches_v3_formula_verbatim(self):
        for ws in (0, 10, 4200, 4140):
            for k in (1, 2, 4):
                for avg in (1, 3, 8):
                    for rate in (1.0, 4.0, 0.2, 0.0):
                        with self.subTest(ws=ws, k=k, avg=avg, rate=rate):
                            self.assertEqual(
                                schedule.capacity_of(ws, k=k, avg=avg, bucket_rate=rate,
                                                     enabled=True),
                                schedule.capacity_accounts_v3(ws, k, avg, rate))

    def test_enabled_without_k_is_single_executor(self):
        """k 缺省 = 1（单执行体零额外配置），与 `capacity_accounts_v3` 的缺省一致。"""
        self.assertEqual(
            schedule.capacity_of(4200, avg=3, bucket_rate=1.0, enabled=True),
            schedule.capacity_accounts_v3(4200, 1, 3, 1.0))

    def test_enabled_defaults_to_the_switch_when_not_passed(self):
        """enabled 缺省取 `executor_v3.scheduler_v3_enabled()`——开关即回滚。"""
        for raw, expect_v3 in ((None, False), ("1", True), ("0", False)):
            env = {} if raw is None else {"YIBAN_SCHEDULER_V3": raw}
            with self.subTest(raw=raw), mock.patch.dict(os.environ, env, clear=False):
                if raw is None:
                    os.environ.pop("YIBAN_SCHEDULER_V3", None)
                got = schedule.capacity_of(4200, gap=10, avg=3)
                want = (schedule.capacity_accounts_v3(4200, 1, 3, 1.0) if expect_v3
                        else schedule.capacity_accounts(4200, 10, 3))
                self.assertEqual(got, want)


class ExecutorCountTest(unittest.TestCase):
    """K 的自动公式：`clamp(ceil(N×(1+r)/(W×bucket×0.8)), 1, 出口数)`。"""

    def test_clamped_into_one_and_egress_count(self):
        # 极小 N → 下界 1
        self.assertEqual(schedule.executor_count(0, 4200, bucket_rate=1.0), 1)
        self.assertEqual(schedule.executor_count(1, 4200, bucket_rate=1.0), 1)
        # 极大 N → 上界 = 出口数
        self.assertEqual(schedule.executor_count(10 ** 7, 4200, bucket_rate=1.0,
                                                 egress_count=3), 3)
        # 出口数非法/为 0 → 至少 1
        self.assertEqual(schedule.executor_count(10 ** 7, 4200, egress_count=0), 1)

    def test_exact_value_matches_formula(self):
        # T = 1000×1.2 = 1200；W×bucket×0.8 = 4200×1×0.8 = 3360 → ceil = 1
        self.assertEqual(schedule.executor_count(1000, 4200, bucket_rate=1.0,
                                                 egress_count=100), 1)
        # T = 10000×1.2 = 12000；3360 → ceil = 4
        self.assertEqual(schedule.executor_count(10000, 4200, bucket_rate=1.0,
                                                 egress_count=100), 4)
        # 桶 4/s：4200×4×0.8 = 13440 → ceil(12000/13440) = 1
        self.assertEqual(schedule.executor_count(10000, 4200, bucket_rate=4.0,
                                                 egress_count=100), 1)

    def test_monotonic_non_decreasing_in_n(self):
        prev = 0
        for n in (0, 1, 100, 1000, 5000, 10000, 50000):
            k = schedule.executor_count(n, 4200, bucket_rate=1.0, egress_count=10 ** 6)
            self.assertGreaterEqual(k, prev, f"N={n} 时 K 不得回落")
            prev = k

    def test_smaller_bucket_rate_does_not_lower_k(self):
        """桶越小，同样的账号量需要越多执行体（K 与 bucket_rate 反向）。"""
        prev = None
        for rate in (8.0, 4.0, 2.0, 1.0, 0.5, 0.25):
            k = schedule.executor_count(10000, 4200, bucket_rate=rate,
                                        egress_count=10 ** 6)
            if prev is not None:
                self.assertGreaterEqual(k, prev, f"rate={rate} 时 K 反而变小")
            prev = k

    def test_retry_ratio_widens_the_estimate(self):
        self.assertEqual(
            schedule.executor_count(10000, 4200, bucket_rate=1.0, retry_ratio=0.0,
                                    egress_count=10 ** 6), 3)
        self.assertGreaterEqual(
            schedule.executor_count(10000, 4200, bucket_rate=1.0, retry_ratio=1.0,
                                    egress_count=10 ** 6),
            schedule.executor_count(10000, 4200, bucket_rate=1.0, retry_ratio=0.0,
                                    egress_count=10 ** 6))

    def test_zero_window_floors_at_one(self):
        self.assertEqual(schedule.executor_count(1000, 0), 1)
        self.assertEqual(schedule.executor_count(1000, -5), 1)


class WebCapacityEstimateSwitchOffTest(unittest.TestCase):
    """`web/services/capacity.py::_capacity_estimate` 在开关缺省 0 时逐值不变。"""

    def _estimate(self, gap):
        return cap_service._capacity_estimate(
            gap, sign_window=lambda: ((6, 30), (7, 50)),
            edge_config=lambda: (60, 60))

    def test_explicit_values_unchanged(self):
        with mock.patch.dict(os.environ, {"YIBAN_AVG_ATTEMPT_SEC": "8"}):
            os.environ.pop("YIBAN_SCHEDULER_V3", None)
            # 有效窗口 4680s、avg=8：(4680-8)//8+1 = 585；gap=10 → (4680-8)//18+1 = 260
            self.assertEqual(self._estimate(0), 585)
            self.assertEqual(self._estimate(10), 260)

    def test_matches_the_engine_formula(self):
        import signin
        with mock.patch.dict(os.environ, {"YIBAN_AVG_ATTEMPT_SEC": "8"}):
            os.environ.pop("YIBAN_SCHEDULER_V3", None)
            for gap in (0, 10, 60):
                self.assertEqual(self._estimate(gap), signin.capacity_accounts(4680, gap))


class OtherCallSitesRouteThroughCapacityOfTest(unittest.TestCase):
    """另两处调用点（CLI `capacity` / 现场实测换算）也必须走 `capacity_of`。

    CLI 侧用显式期望值钉住（`capacity_per_executor` 的「每执行体」语义靠 `k=1`）；
    实测换算是 Flask 路由，这里以"改走 `capacity_of` 且不再直调旧公式"作源码级钉法。
    """

    def test_cli_capacity_per_executor_value_unchanged(self):
        from yiban import cli
        view = {
            "YIBAN_SIGN_START": "06:30", "YIBAN_SIGN_END": "07:50",
            "YIBAN_WINDOW_EDGE_FRONT_SEC": "60", "YIBAN_WINDOW_EDGE_BACK_SEC": "60",
            "YIBAN_ACCOUNT_GAP_MAX": "10", "YIBAN_AVG_ATTEMPT_SEC": "8",
        }
        numbers = cli._capacity_numbers(view, 0)
        self.assertEqual(numbers["window_effective_sec"], 4680)
        # 有效窗口 4680s、avg=8、gap=10：(4680-8)//18+1 = 260
        self.assertEqual(numbers["capacity_per_executor"], 260)

    def test_source_calls_go_through_capacity_of(self):
        for rel, needle in (("yiban/cli.py", "capacity_of("),
                            ("web/routes/settings_api.py", "m.signin.capacity_of(")):
            with self.subTest(rel=rel):
                with open(os.path.join(BASE, rel), encoding="utf-8") as f:
                    src = f.read()
                self.assertIn(needle, src)
                self.assertNotIn("capacity_accounts(", src, "不得再直接调旧公式")

    def test_signin_shell_forwards_capacity_of(self):
        """`scripts/signin.py` 是全量转发壳，`m.signin.capacity_of` 自动可用。"""
        import signin
        self.assertIs(signin.capacity_of, schedule.capacity_of)


class _FakeClock:
    def __init__(self, t=START):
        self.t = t

    def now(self):
        return self.t


class RunnerPrecheckSwitchOffTest(unittest.TestCase):
    """`runner.main` 的容量预检在开关缺省 0 时给出显式期望值。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-capof-")
        self._env = {k: os.environ.get(k) for k in (
            "YIBAN_STATE_DIR", "YIBAN_DB_FILE", "YIBAN_LOG_FILE", "YIBAN_ENV_FILE",
            "YIBAN_SCHEDULER_V3", "YIBAN_GLOBAL_PAUSE", "YIBAN_SECOND_RUN",
            "YIBAN_EXECUTOR_ID", "YIBAN_AVG_ATTEMPT_SEC")}
        os.environ.update({
            "YIBAN_STATE_DIR": self.tmp,
            "YIBAN_DB_FILE": os.path.join(self.tmp, "yiban.db"),
            "YIBAN_LOG_FILE": os.path.join(self.tmp, "sign.log"),
            "YIBAN_AVG_ATTEMPT_SEC": "8",
        })
        for k in ("YIBAN_SCHEDULER_V3", "YIBAN_GLOBAL_PAUSE", "YIBAN_SECOND_RUN",
                  "YIBAN_EXECUTOR_ID"):
            os.environ.pop(k, None)
        self.fc = _FakeClock()
        self._p = mock.patch.object(runner_mod.clock, "now", self.fc.now)
        self._p.start()
        self.addCleanup(self._p.stop)

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def _cfg():
        return {
            "order": "sequence", "dist": "uniform",
            "edge_front_sec": 60, "edge_back_sec": 60,
            "block_cap": 15, "mu_min_pct": 40, "mu_max_pct": 60,
            "sigma_min_pct": 15, "sigma_max_pct": 25, "min_exec_gap": 5,
            "avg_attempt_sec": 8, "retry_min_interval": 60, "exec_gap_min": 10,
            "allow_time_pref": 0, "sign_start": (6, 30), "sign_end": (7, 50),
            "bucket_rate": 1.0, "executors": ["single@testhost"],
        }

    def _run(self):
        phone = "13800000001"
        accounts = [SimpleNamespace(phone=phone, user_paused=False, owner="u@1")]
        sched = {phone: START}
        seen = []
        real = schedule.capacity_of

        def spy(*a, **kw):
            out = real(*a, **kw)
            seen.append((a, kw, out))
            return out

        with mock.patch.object(runner_mod.accounts_mod, "load_accounts",
                               return_value=accounts), \
             mock.patch.object(runner_mod.schedule_mod, "build_schedule",
                               return_value=sched), \
             mock.patch.object(runner_mod.schedule_mod, "planner_config",
                               side_effect=self._cfg), \
             mock.patch.object(runner_mod.schedule_mod, "capacity_of", spy), \
             mock.patch.object(runner_mod.round_mod, "run_queue_retry",
                               return_value={phone: (True, "ok", False, "success")}), \
             mock.patch.object(runner_mod.state_io, "_load_cred_state", return_value={}), \
             mock.patch.object(runner_mod.state_io, "_save_cred_state"), \
             mock.patch.object(runner_mod.state_io, "_is_second_run", return_value=False), \
             mock.patch.object(runner_mod.state_io, "_write_sched_done"), \
             mock.patch.object(runner_mod.state_io, "_write_sign_state"), \
             mock.patch.object(runner_mod.db, "add_sign_events_batch"), \
             mock.patch.object(runner_mod.db, "purge_expired_deleted_accounts"), \
             mock.patch.object(runner_mod.alerts, "_maybe_alert_zero_success"), \
             mock.patch.object(runner_mod.alerts, "_flush_admin_mail_summary"):
            code = runner_mod.main([])
        return code, seen

    def test_precheck_value_is_explicit_and_unchanged(self):
        code, seen = self._run()
        self.assertEqual(code, 0)
        self.assertEqual(len(seen), 1, "容量预检必须走 `capacity_of`")
        args, kw, out = seen[0]
        # 剩余有效窗口 = 07:49 − 06:40 = 4140s；avg=8、gap=10 → (4140-8)//18+1 = 230
        self.assertEqual(args, (4140.0,))
        self.assertEqual(kw["gap"], 10)
        self.assertEqual(kw["avg"], 8)
        self.assertEqual(out, 230, "开关缺省 0 时预检容量逐值不变")
        self.assertEqual(out, schedule.capacity_accounts(4140, 10, 8))


if __name__ == "__main__":
    unittest.main()
