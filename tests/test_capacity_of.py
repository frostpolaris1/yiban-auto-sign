# -*- coding: utf-8 -*-
"""容量口径按开关分派：`schedule.capacity_of` / `schedule.executor_count` 与四处调用点。

标签：I · 容量、熔断与账号有效性
覆盖：容量口径按 v3 开关分派（`capacity_of` 与 `capacity_accounts` / `capacity_accounts_v3` 逐值相同）、K 的唯一口径 `executor_count` 的边界，以及四处调用点在开关缺省时数值不变
对应实现：`schedule.capacity_of` / `schedule.executor_count`、`web/services/capacity.py::_capacity_estimate`、`yiban/engine/runner.py` 的容量预检、CLI `capacity` 与现场实测换算、`scripts/signin.py` 的转发壳
关键断言：`enabled=False` 时 k / bucket_rate / util 一律不参与（v2 侧逐字不变）；`executor_count` 夹在 `[1, 出口数]`、随 N 单调不减、`bucket_rate` 变小则 K 不变或变大；`enabled` 缺省取 `executor_v3.scheduler_v3_enabled()`（开关即回滚）；另两处调用点用「包住 `capacity_of` 看它收到什么」来断言 `k=1`——源码文本断言会被无关重构误伤
依赖：纯本地——假时钟与假配置快照（runner 预检不读真实 `.env`、不联网、不落库）。无需 node

覆盖（对应简报 ⑤ 的容量部分）：
1. `capacity_of(..., enabled=False)` 与 `capacity_accounts(...)` 多组逐值相同（v2 侧硬门）；
2. `capacity_of(..., enabled=True)` 与 `capacity_accounts_v3(...)` 多组逐值相同；
3. `executor_count`（K 的唯一口径）的边界：夹到 `[1, 出口数]`、随 N 单调不减、
   `bucket_rate` 变小则 K 不变或变大；
4. 四处调用点在开关缺省 0 时**数值不变**：`web/services/capacity.py` 与
   `yiban/engine/runner.py` 两处用显式期望值钉住；另两处（`settings_api` / `cli`）
   以"包住 `capacity_of` 看它收到什么"作行为断言，验证 `k=1`（单执行体语义）。

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
    """另两处调用点（CLI `capacity` / 现场实测换算）走 `capacity_of` 且按 `k=1` 口径。

    断言方式是**包住 `capacity_of` 看它收到什么**：源码文本断言会被无关重构误伤，
    也证明不了调用点真的传了 `k=1`（"每执行体"的字面语义，v3 下总容量 ≈ 该值 × 出口数）。
    """

    @staticmethod
    def _spy(seen):
        real = schedule.capacity_of

        def spy(*a, **kw):
            seen.append((a, dict(kw)))
            return real(*a, **kw)
        return spy

    def test_cli_capacity_per_executor_value_unchanged(self):
        from yiban import cli
        view = {
            "YIBAN_SIGN_START": "06:30", "YIBAN_SIGN_END": "07:50",
            "YIBAN_WINDOW_EDGE_FRONT_SEC": "60", "YIBAN_WINDOW_EDGE_BACK_SEC": "60",
            "YIBAN_ACCOUNT_GAP_MAX": "10", "YIBAN_AVG_ATTEMPT_SEC": "8",
        }
        seen = []
        with mock.patch.dict(os.environ, {}, clear=False), \
                mock.patch.object(schedule, "capacity_of", self._spy(seen)):
            os.environ.pop("YIBAN_SCHEDULER_V3", None)
            numbers = cli._capacity_numbers(view, 0)
        self.assertEqual(numbers["window_effective_sec"], 4680)
        # 有效窗口 4680s、avg=8、gap=10：(4680-8)//18+1 = 260
        self.assertEqual(numbers["capacity_per_executor"], 260)
        args, kw = seen[0]
        self.assertEqual(args, (4680,), "窗口用完整有效窗口（这套配置能容纳几个）")
        self.assertEqual(kw["gap"], 10)
        self.assertEqual(kw["avg"], 8)
        self.assertEqual(kw["k"], 1, "`k=1` 钉住「每执行体」的字面语义")

    def test_settings_api_measure_passes_k_one(self):
        """现场实测换算：真实路由体跑一遍，验证它把 `k=1` 传给了 `capacity_of`。"""
        import contextlib

        import flask

        from web.routes import settings_api
        seen = []

        class _Bounds:
            @staticmethod
            def full_sec():
                return 4680

        m = SimpleNamespace(
            ENV_FILE=".env", DEFAULT_ACCOUNT_GAP_MAX=10, MEASURE_COOLDOWN_SEC=300,
            clock=SimpleNamespace(now=datetime.now),
            db=SimpleNamespace(audit=lambda *a, **kw: None),
            _is_builtin_admin_session=lambda: True,
            _executors_window=lambda: _Bounds(),
            _in_sign_window=lambda bounds: False,
            _json_body=lambda: {},
            load_accounts=lambda: [{"phone": "13800000001"}],
            _pick_measure_account=lambda accounts, phone: {"phone": "13800000001"},
            _as_signin_account=lambda acc: SimpleNamespace(),
            _mask_phone=lambda p: "138****0001",
            load_env_int=lambda *a: 10,
            _measure_state_path=lambda: os.path.join(tempfile.gettempdir(), "m.json"),
            _read_measure_state=lambda p: {},
            _write_measure_state=lambda p, d: None,
            _measure_cooldown_remaining=lambda st, cd: 0,
            _audit_actor=lambda: "admin",
            signin=SimpleNamespace(
                _state_file_lock=lambda p: contextlib.nullcontext(),
                verify_account=lambda acc: (True, "ok"),
                capacity_of=self._spy(seen)),
        )
        with mock.patch.object(settings_api, "_appmod", lambda: m), \
                flask.Flask(__name__).app_context():
            resp = settings_api.api_scheduler_executors_measure()
        self.assertTrue(resp.get_json()["ok"])
        args, kw = seen[0]
        self.assertEqual(args, (4680,))
        self.assertEqual(kw["k"], 1, "实测换算量的是单执行体容量，`k=1` 是字面语义")

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
    """`runner.main` 的容量预检：开关关时逐值不变且不多读环境键，开关开时才带 K。"""

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
    def _cfg(**over):
        cfg = {
            "order": "sequence", "dist": "uniform",
            "edge_front_sec": 60, "edge_back_sec": 60,
            "block_cap": 15, "mu_min_pct": 40, "mu_max_pct": 60,
            "sigma_min_pct": 15, "sigma_max_pct": 25, "min_exec_gap": 5,
            "avg_attempt_sec": 8, "retry_min_interval": 60, "exec_gap_min": 10,
            "allow_time_pref": 0, "sign_start": (6, 30), "sign_end": (7, 50),
            "bucket_rate": 1.0, "executors": ["single@testhost"],
        }
        cfg.update(over)
        return cfg

    def _run(self, *, n=1, cfg=None, planner_config=None, v3=False):
        """跑一轮 `runner.main`，返回 `(退出码, capacity_of 的调用记录)`。"""
        cfg = self._cfg() if cfg is None else cfg
        accounts = [SimpleNamespace(phone=f"1380000{i:04d}", user_paused=False,
                                    owner="u@1") for i in range(n)]
        sched = {a.phone: START for a in accounts}
        seen = []
        real = schedule.capacity_of

        def spy(*a, **kw):
            out = real(*a, **kw)
            seen.append((a, kw, out))
            return out

        planner_config = planner_config or (lambda: cfg)
        with mock.patch.dict(os.environ, {}, clear=False):
            if v3:
                os.environ["YIBAN_SCHEDULER_V3"] = "1"
            else:
                os.environ.pop("YIBAN_SCHEDULER_V3", None)
            with mock.patch.object(runner_mod.accounts_mod, "load_accounts",
                                   return_value=accounts), \
                 mock.patch.object(runner_mod.schedule_mod, "build_schedule",
                                   return_value=sched), \
                 mock.patch.object(runner_mod.schedule_mod, "_schedule_config",
                                   side_effect=lambda: cfg), \
                 mock.patch.object(runner_mod.schedule_mod, "planner_config",
                                   side_effect=planner_config), \
                 mock.patch.object(runner_mod.schedule_mod, "capacity_of", spy), \
                 mock.patch.object(runner_mod.executor_v3, "run_executor_v3",
                                   return_value={a.phone: (True, "ok", False, "success")
                                                 for a in accounts}), \
                 mock.patch.object(runner_mod.round_mod, "run_queue_retry",
                                   return_value={a.phone: (True, "ok", False, "success")
                                                 for a in accounts}), \
                 mock.patch.object(runner_mod.state_io, "_load_cred_state",
                                   return_value={}), \
                 mock.patch.object(runner_mod.state_io, "_save_cred_state"), \
                 mock.patch.object(runner_mod.state_io, "_is_second_run",
                                   return_value=False), \
                 mock.patch.object(runner_mod.state_io, "_write_sched_done"), \
                 mock.patch.object(runner_mod.state_io, "_write_sign_state"), \
                 mock.patch.object(runner_mod.db, "add_sign_events_batch"), \
                 mock.patch.object(runner_mod.db, "purge_expired_deleted_accounts"), \
                 mock.patch.object(runner_mod.alerts, "_maybe_alert_zero_success"), \
                 mock.patch.object(runner_mod.alerts, "_flush_admin_mail_summary"):
                code = runner_mod.main([])
        return code, seen

    def test_switch_off_does_not_read_planner_config(self):
        """开关关时预检连 `planner_config` 都不该读。

        它比调度配置多读 `YIBAN_EGRESS_RATE` / `YIBAN_EXECUTORS`，还会对非法值告警——
        v2 路径"数值与行为完全不变"要求这些 v3 专属输入在关时根本不产生依赖。
        """
        boom = mock.Mock(side_effect=AssertionError("开关关时不得读 planner_config"))
        code, seen = self._run(planner_config=boom)
        self.assertEqual(code, 0)
        self.assertEqual(len(seen), 1, "容量预检必须走 `capacity_of`")
        self.assertFalse(boom.called, "开关关时预检不得读 planner_config")
        self.assertEqual(seen[0][2], 230, "关时数值仍逐值不变")

    def test_switch_off_value_is_explicit_and_unchanged(self):
        code, seen = self._run()
        self.assertEqual(code, 0)
        self.assertEqual(len(seen), 1, "容量预检必须走 `capacity_of`")
        args, kw, out = seen[0]
        # 剩余有效窗口 = 07:49 − 06:40 = 4140s；avg=8、gap=10 → (4140-8)//18+1 = 230
        self.assertEqual(args, (4140.0,))
        self.assertEqual(kw["gap"], 10)
        self.assertEqual(kw["avg"], 8)
        self.assertIs(kw["enabled"], False, "关时必须显式走 v2 分支，不靠默认值")
        self.assertNotIn("k", kw, "关时不得计算/传入 K")
        self.assertNotIn("bucket_rate", kw, "关时不得读桶速率")
        self.assertEqual(out, 230, "开关缺省 0 时预检容量逐值不变")
        self.assertEqual(out, schedule.capacity_accounts(4140, 10, 8))

    def test_switch_on_passes_k_from_executor_count(self):
        """开关开时 K 真的参与计算：`executor_count` 的入参口径与结果都要落到调用上。"""
        cfg = self._cfg(executors=["a@h", "b@h", "c@h", "d@h"])
        code, seen = self._run(n=10000, cfg=cfg, v3=True)
        self.assertEqual(code, 0)
        self.assertEqual(len(seen), 1, "容量预检必须走 `capacity_of`")
        args, kw, out = seen[0]
        k = schedule.executor_count(10000, 4140.0, bucket_rate=1.0, egress_count=4)
        self.assertEqual(k, 4, "夹具前提：K 要大于 1 才看得出它真的参与计算")
        self.assertEqual(args, (4140.0,))
        self.assertIs(kw["enabled"], True)
        self.assertEqual(kw["k"], k)
        self.assertEqual(kw["bucket_rate"], 1.0)
        self.assertEqual(out, schedule.capacity_accounts_v3(4140, k, 8, 1.0))
        self.assertNotEqual(out, 230, "开时走的必须是 v3 公式，不是 v2 的 230")


if __name__ == "__main__":
    unittest.main()
