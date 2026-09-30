# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""正态 μ/σ 当日取值：计划层与执行层同源（缺陷 B1 回归）。

标签：C · 引擎调度与计划
覆盖：`schedule.day_mu_sigma_pct` 的确定性（同一 day/cfg 恒定、三种 day 形态等价、
     落在配置区间内、跨天变化）与非法区间的既有回退语义；`hrw.u01` 是 `planner._u`
      的归一化单源；`planner._density` 与 `schedule.build_schedule` 取同一业务日键
对应实现：`yiban/engine/schedule.py` 的 `day_mu_sigma_pct` / `build_schedule`；
      `yiban/engine/planner.py` 的 `_u` / `_density`；`yiban/engine/hrw.py` 的 `u01`
关键断言：μ/σ 定义的是区间，落在区间的哪一点只由 `day` 决定——计划（06:31 生成）与
      实际执行必须取到同一个点；执行层不得再用 `rng.uniform` 重采样（那会与计划层的
      确定性推导分叉，让"计划时刻"与"签到时刻"对不上，峰值速率整形也作用在错值上）。
      非法区间（lo >= hi）仍按 `_schedule_config` 的既有行为回退默认 40~60 / 15~25。
依赖：纯本地、无网络、无 DB；`build_schedule` 用固定 seed 的 `random.Random` 注入。
"""

import datetime
import os
import random
import unittest
from unittest import mock

from yiban.engine import hrw, planner, schedule

DAY = "2026-09-30"
DAY_DT = datetime.datetime(2026, 9, 30, 6, 31, 0)
_ENV_KEYS = ("YIBAN_SCHEDULE_MU_MIN_PCT", "YIBAN_SCHEDULE_MU_MAX_PCT",
             "YIBAN_SCHEDULE_SIGMA_MIN_PCT", "YIBAN_SCHEDULE_SIGMA_MAX_PCT")


def _cfg(mu=(40, 60), sigma=(15, 25), bucket_rate=1.0):
    """最小 cfg：只含 `_density` 与本组用例读到的键。"""
    return {"mu_min_pct": mu[0], "mu_max_pct": mu[1],
            "sigma_min_pct": sigma[0], "sigma_max_pct": sigma[1],
            "bucket_rate": bucket_rate}


class _EnvIsolated(unittest.TestCase):
    """μ/σ 四键的进程环境隔离：本组用例不依赖也不污染外部 env（同类文件有过未清理的先例）。"""

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in _ENV_KEYS}
        for k in _ENV_KEYS:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class U01SingleSourceTest(_EnvIsolated):
    def test_planner_u_delegates_to_hrw_u01(self):
        """`planner._u` 的归一化实现单源在 `hrw.u01`（两侧共用才可能取到同一点）。"""
        for parts in (("2026-09-30", "mu"), ("13800138000", "2026-09-30", "phase"),
                      ("x",)):
            self.assertEqual(planner._u(*parts), hrw.u01(*parts))

    def test_u01_stays_in_unit_interval(self):
        for i in range(200):
            v = hrw.u01("p%03d" % i, "mu")
            self.assertGreaterEqual(v, 0.0)
            self.assertLess(v, 1.0)


class DayMuSigmaTest(_EnvIsolated):
    def test_same_day_same_value(self):
        """同一 cfg、同一 day → 恒定（可重放、崩溃恢复不漂移）。"""
        cfg = _cfg()
        self.assertEqual(schedule.day_mu_sigma_pct(cfg, DAY),
                         schedule.day_mu_sigma_pct(cfg, DAY))

    def test_day_forms_agree(self):
        """`date` / `datetime` / 字符串（含空白）归一化到同一业务日，取值必须相同。"""
        cfg = _cfg()
        want = schedule.day_mu_sigma_pct(cfg, DAY)
        self.assertEqual(schedule.day_mu_sigma_pct(cfg, datetime.date(2026, 9, 30)), want)
        self.assertEqual(schedule.day_mu_sigma_pct(cfg, DAY_DT), want)
        self.assertEqual(schedule.day_mu_sigma_pct(cfg, " 2026-09-30 "), want)

    def test_within_configured_interval(self):
        cfg = _cfg((30, 34), (10, 12))
        for day in ("2026-09-28", "2026-09-29", "2026-09-30", "2026-10-01"):
            mu, sg = schedule.day_mu_sigma_pct(cfg, day)
            self.assertTrue(30 <= mu <= 34, mu)
            self.assertTrue(10 <= sg <= 12, sg)

    def test_value_changes_across_days(self):
        cfg = _cfg()
        vals = {schedule.day_mu_sigma_pct(cfg, "2026-09-%02d" % d) for d in range(1, 29)}
        self.assertGreater(len(vals), 1)

    def test_env_keys_take_effect(self):
        os.environ["YIBAN_SCHEDULE_MU_MIN_PCT"] = "35"
        os.environ["YIBAN_SCHEDULE_MU_MAX_PCT"] = "45"
        os.environ["YIBAN_SCHEDULE_SIGMA_MIN_PCT"] = "12"
        os.environ["YIBAN_SCHEDULE_SIGMA_MAX_PCT"] = "18"
        cfg = schedule._schedule_config()
        self.assertEqual((cfg["mu_min_pct"], cfg["mu_max_pct"]), (35, 45))
        self.assertEqual((cfg["sigma_min_pct"], cfg["sigma_max_pct"]), (12, 18))

    def test_invalid_interval_falls_back_to_defaults(self):
        """lo >= hi → `_schedule_config` 告警并回退默认（μ 40~60 / σ 15~25），行为不变。"""
        os.environ["YIBAN_SCHEDULE_MU_MIN_PCT"] = "70"
        os.environ["YIBAN_SCHEDULE_MU_MAX_PCT"] = "60"
        os.environ["YIBAN_SCHEDULE_SIGMA_MIN_PCT"] = "30"
        os.environ["YIBAN_SCHEDULE_SIGMA_MAX_PCT"] = "20"
        cfg = schedule._schedule_config()
        self.assertEqual((cfg["mu_min_pct"], cfg["mu_max_pct"]), (40, 60))
        self.assertEqual((cfg["sigma_min_pct"], cfg["sigma_max_pct"]), (15, 25))
        mu, sg = schedule.day_mu_sigma_pct(cfg, DAY)
        self.assertTrue(40 <= mu <= 60)
        self.assertTrue(15 <= sg <= 25)


class PlanExecuteSameSourceTest(_EnvIsolated):
    def test_density_uses_day_mu_sigma(self):
        """planner 密度用的 μ/σ 逐字等于 `day_mu_sigma_pct` 的当日取值。"""
        cfg = _cfg((35, 45), (12, 18))
        n, span_sec = 50, 4200.0
        mu_min, sigma_min, _alpha, _phi = planner._density(n, cfg, DAY, span_sec)
        mu_pct, sg_pct = schedule.day_mu_sigma_pct(cfg, DAY)
        span_min = span_sec / 60.0
        self.assertAlmostEqual(mu_min, span_min * mu_pct / 100.0, places=9)
        self.assertAlmostEqual(
            sigma_min, schedule._sigma_eff(span_min * sg_pct / 100.0, n, span_min),
            places=9)

    def test_planner_and_build_schedule_share_the_day_key(self):
        """验收核心：计划层与执行层都从**同一业务日键**派生 μ/σ（同源）。

        以 `hrw.u01` 为观测点：`planner._density(n, cfg, DAY, …)` 与
        `build_schedule(…, now=<同一业务日>)` 必须各自产生一次 `("2026-09-30", "mu")`
        与 `("2026-09-30", "sigma")` 的调用——只要两侧都走 `day_mu_sigma_pct`，
        计划里排出的时刻与实际签到时刻就落在同一个 μ/σ 上。
        """
        cfg = _cfg()
        accs = [mock.Mock(phone="13800138%03d" % i, user_paused=False) for i in range(30)]
        calls = []
        orig = hrw.u01

        def spy(*parts):
            calls.append(parts)
            return orig(*parts)

        with mock.patch.object(hrw, "u01", spy):
            planner._density(30, cfg, DAY, 4200.0)
            schedule.build_schedule(accs, order="sequence", dist="normal",
                                    now=DAY_DT, rng=random.Random(1))
        mu_days = {c[0] for c in calls if c and c[-1] == "mu"}
        sg_days = {c[0] for c in calls if c and c[-1] == "sigma"}
        self.assertEqual(mu_days, {DAY}, "μ 当日键在计划层/执行层不一致")
        self.assertEqual(sg_days, {DAY}, "σ 当日键在计划层/执行层不一致")

    def test_build_schedule_not_resampling_mu_sigma(self):
        """执行层不再按 `rng.uniform` 抽 μ/σ：换 seed 不改当日键（只影响抖动/打乱）。

        旧实现里 μ/σ 每次调用都被 `rng.uniform` 重采样，同一 (day) 不同进程取到不同
        中心；现在换 seed 也走同一 `day_mu_sigma_pct`，故两次调度取到的 σ 相同。
        """
        cfg = _cfg((40, 60), (15, 25))
        seen = []
        orig = schedule.day_mu_sigma_pct

        def spy(cfg_, day):
            seen.append(orig(cfg_, day))
            return orig(cfg_, day)

        accs = [mock.Mock(phone="13800138%03d" % i, user_paused=False) for i in range(20)]
        with mock.patch.object(schedule, "day_mu_sigma_pct", spy):
            schedule.build_schedule(accs, order="sequence", dist="normal",
                                    now=DAY_DT, rng=random.Random(1))
            schedule.build_schedule(accs, order="sequence", dist="normal",
                                    now=DAY_DT, rng=random.Random(999))
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[0], seen[1], "换 seed 不得改变当日 μ/σ")


if __name__ == "__main__":
    unittest.main(verbosity=2)
