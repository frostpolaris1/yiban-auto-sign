# -*- coding: utf-8 -*-
"""兜底常驻执行体的三道门与窗口边界（2026-09-17 实测缺陷的钉版回归）。

**缺陷**：`--fallback` 在 `runner.main` 的分支顺序里排在周末门/一键暂停门**之前**，
于是这两道门被兜底全部绕过——管理员在网页关掉周末签到、或点了一键暂停，兜底照样
把账号签掉（周六/周日/暂停三种情形均已实测复现）。另外 `window.Window` 只有
"关没关"（`is_closed`），含不住"还没开"，而兜底按 cron 模板提前拉起（06:05 起、
窗口 06:30 开），于是**窗口外每个账号都白登陆一次**（`run_queue_retry` 的手动链路
本身不判本项目的窗口，实际请求会真的发出去）。

钉的四条：
1. 周末门未开时，兜底一轮都不扫（也不写心跳）；
2. 一键暂停时同上；
3. 窗口未开始时**等**（不扫），窗口一开就扫，窗口关闭即退出；
4. 反向控制：周末签到**已开**时兜底照跑（门没误伤），窗口内照跑。

用法（项目根目录）：
    py -m pytest tests/test_fallback_gates.py -v
"""
import datetime as _dt
import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from yiban.engine import cli_support, runner, schedule, workers

#: 2026-09 的三个样例日（下面的 weekday 断言保证它们仍是周三/周六/周日）
WED = _dt.date(2026, 9, 2)
SAT = _dt.date(2026, 9, 5)
SUN = _dt.date(2026, 9, 6)

#: 测试基线环境：窗口 06:30~07:50（默认）、两天周末签到都关、无暂停。
#: 显式给全是为了**不依赖宿主环境变量**（开发者机器上带 YIBAN_GLOBAL_PAUSE 也会读进去）
BASE_ENV = {
    "YIBAN_SIGN_START": "06:30",
    "YIBAN_SIGN_END": "07:50",
    "YIBAN_SATURDAY_SIGN": "0",
    "YIBAN_SUNDAY_SIGN": "0",
    "YIBAN_GLOBAL_PAUSE": "0",
    "YIBAN_FALLBACK_INTERVAL": "60",
}


def _at(day, hm):
    return _dt.datetime(day.year, day.month, day.day, hm[0], hm[1], 0)


class _ClockSeq:
    """`clock.now` 替身：按序列返回时刻，序列用尽后重复最后一个。

    重复到 `max_calls` 就抛异常——**兜底主循环不退出时测试会挂死**（几十万次空转），
    挂死的用例在 CI 上只会表现为超时，说不出原因；这里换成一条明确的失败信息。
    """

    def __init__(self, *times, max_calls=50):
        self._times = list(times)
        self._max = max_calls
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if self.calls > self._max:
            raise AssertionError(
                f"兜底主循环没有在 {self._max} 次内退出（门/窗口判定失效？）")
        if len(self._times) > 1:
            return self._times.pop(0)
        return self._times[0]


class _WeekdayGuard(unittest.TestCase):
    def setUp(self):
        # 样例日的星期几是测试数据的隐含前提，先钉住（改数据忘了改断言会静默失效）
        self.assertEqual(WED.weekday(), 2, "样例日不再是周三，改测试数据")
        self.assertEqual(SAT.weekday(), 5, "样例日不再是周六，改测试数据")
        self.assertEqual(SUN.weekday(), 6, "样例日不再是周日，改测试数据")


class DayOffGateTest(_WeekdayGuard):
    """门本身：`schedule.day_off` 的判定表（定时轮与兜底共用的唯一实现）。"""

    def _off(self, day, hm=(6, 35), env=None, **kw):
        with mock.patch.dict(os.environ, {**BASE_ENV, **(env or {})}, clear=False):
            return schedule.day_off(_at(day, hm), **kw)

    def test_weekday_is_go(self):
        self.assertEqual(self._off(WED), "")

    def test_weekend_off_by_default(self):
        self.assertEqual(self._off(SAT), schedule.DAY_OFF_SATURDAY)
        self.assertEqual(self._off(SUN), schedule.DAY_OFF_SUNDAY)

    def test_weekend_on_when_enabled(self):
        self.assertEqual(self._off(SAT, env={"YIBAN_SATURDAY_SIGN": "1"}), "")
        self.assertEqual(self._off(SUN, env={"YIBAN_SUNDAY_SIGN": "true"}), "")
        # 显式传入（定时轮用导入期快照常量，见 runner.SATURDAY_SIGN）
        self.assertEqual(self._off(SAT, sat=True), "")

    def test_pause_blocks_any_day(self):
        self.assertEqual(self._off(WED, env={"YIBAN_GLOBAL_PAUSE": "1"}),
                         schedule.DAY_OFF_PAUSED)
        self.assertEqual(self._off(WED, env={"YIBAN_GLOBAL_PAUSE": "ON"}),
                         schedule.DAY_OFF_PAUSED)

    def test_weekend_takes_precedence_over_pause(self):
        """顺序与历史行为一致（周日 → 周六 → 暂停）：原因串决定日志措辞。"""
        self.assertEqual(self._off(SAT, env={"YIBAN_GLOBAL_PAUSE": "1"}),
                         schedule.DAY_OFF_SATURDAY)

    def test_non_truthy_literals_are_off(self):
        for raw in ("0", "false", "off", "no", "", "yes please", "2"):
            with self.subTest(raw=raw):
                self.assertEqual(self._off(WED, env={"YIBAN_GLOBAL_PAUSE": raw}), "")


class _FallbackHarness(_WeekdayGuard):
    """主循环测试骨架：把循环跑起来并记录"睡过几次、扫了几次、写没写心跳"。"""

    def _run(self, times, env=None, accounts=None, results=None, lock_held=None):
        """跑一次兜底常驻主循环，返回 (退出码, 睡过的秒数, 心跳时刻, 扫描次数)。

        `lock_held`：全局锁探测的返回序列（缺省全 False = 没有全量轮在跑）。
        必须显式打桩：真探测会去读宿主 `YIBAN_STATE_DIR`，测试之间会互相串味。
        """
        sleeps, beats, scans = [], [], []
        held = list(lock_held) if lock_held else []
        acc = mock.Mock(phone="13800000000", user_paused=False)
        with mock.patch.dict(os.environ, {**BASE_ENV, **(env or {})}, clear=False), \
                mock.patch.object(workers, "time", SimpleNamespace(sleep=sleeps.append)), \
                mock.patch.object(workers.clock, "now", _ClockSeq(*times)), \
                mock.patch.object(workers.cli_support, "_run_lock_held",
                                  lambda *a, **k: held.pop(0) if held else False), \
                mock.patch.object(workers.state_io, "_write_fallback_alive",
                                  lambda now: beats.append(now)), \
                mock.patch.object(workers.state_io, "_clear_fallback_alive", lambda: None), \
                mock.patch.object(workers.state_io, "_load_cred_state", lambda: {}), \
                mock.patch.object(workers.accounts_mod, "load_accounts",
                                  lambda: ([acc] if accounts is None else accounts)), \
                mock.patch.object(workers.round_mod, "run_queue_retry",
                                  lambda *a, **k: (scans.append(k.get("delegated")),
                                                   results if results is not None else {})[1]):
            rc = workers.run_fallback_worker(["--fallback"])
        return rc, sleeps, beats, scans

class FallbackGateTest(_FallbackHarness):
    """门与窗口边界（缺陷的钉版回归）。"""

    def test_saturday_off_never_scans(self):
        rc, sleeps, beats, scans = self._run([_at(SAT, (6, 35))])
        self.assertEqual(rc, 0)
        self.assertEqual(scans, [], "周末签到关闭时兜底仍在扫账号——门被绕过")
        self.assertEqual(beats, [], "没在跑却写了心跳")
        self.assertEqual(sleeps, [], "该直接退出，不该进入轮询")

    def test_sunday_off_never_scans(self):
        _, _, _, scans = self._run([_at(SUN, (6, 35))])
        self.assertEqual(scans, [])

    def test_paused_never_scans(self):
        _, _, beats, scans = self._run([_at(WED, (6, 35))],
                                       env={"YIBAN_GLOBAL_PAUSE": "1"})
        self.assertEqual(scans, [], "一键暂停期间兜底仍在扫账号")
        self.assertEqual(beats, [])

    def test_weekend_on_scans_within_window(self):
        """反向控制：周末签到已开 ⇒ 兜底照跑（门没误伤正中情形）。"""
        rc, _sleeps, beats, scans = self._run(
            [_at(SAT, (6, 35)), _at(SAT, (8, 30))],
            env={"YIBAN_SATURDAY_SIGN": "1"})
        self.assertEqual(len(scans), 1, "周末签到已开却没扫")
        self.assertEqual(len(beats), 1)
        self.assertEqual(rc, 0)

    def test_waits_before_window_then_scans(self):
        """窗口 06:30 开、进程 06:05 起：先等（零扫描、无心跳），开了才扫。"""
        rc, sleeps, beats, scans = self._run(
            [_at(WED, (6, 5)), _at(WED, (6, 35)), _at(WED, (8, 30))])
        self.assertEqual(len(scans), 1, "窗口前不该扫，窗口内该扫一次")
        self.assertEqual(len(beats), 1, "窗口前不该写心跳（会被显示成「正在跑」）")
        self.assertEqual(beats[0], _at(WED, (6, 35)), "心跳该写在窗口内那一轮")
        # 第一次睡的是"等到开窗"（受扫描间隔封顶），第二次是"没活干等下一轮"
        self.assertEqual(sleeps, [60, 60])
        self.assertEqual(rc, 0)

    def test_window_closed_exits(self):
        rc, _, _, scans = self._run([_at(WED, (8, 30))])
        self.assertEqual(scans, [], "窗口已关仍扫描")
        self.assertEqual(rc, 0)


class YieldToFullRoundTest(_FallbackHarness):
    """全量轮在跑时**让位**：不抢账号。

    否则兜底按列表顺序一路签下去，会把全量轮的错峰计划（按账号计划时刻逐个领取）与
    多执行体的分工一起冲掉——它抢的正是"还没被领走"的那些账号。
    """

    def test_yields_while_round_running_then_takes_over(self):
        rc, sleeps, beats, scans = self._run(
            [_at(WED, (6, 35)), _at(WED, (6, 35)), _at(WED, (8, 30))],
            lock_held=[True, False])
        self.assertEqual(len(scans), 1, "全量轮在跑时该让位，跑完才该接手")
        self.assertEqual(sleeps[0], workers._YIELD_POLL_SEC, "让位期间的轮询间隔不对")
        # 让位那一轮不算"在跑"：不写心跳（页面不该显示成一个在干活的进程）
        self.assertEqual(len(beats), 1)
        self.assertEqual(beats[0], _at(WED, (6, 35)))
        self.assertEqual(rc, 0)

    def test_yields_repeatedly_while_round_running(self):
        """全量轮一直在跑 ⇒ 兜底一直不扫（只轮询），不会偷偷插进去签账号。"""
        _, _, beats, scans = self._run(
            [_at(WED, (6, 35))] * 5 + [_at(WED, (8, 30))],
            lock_held=[True] * 5)
        self.assertEqual(scans, [])
        self.assertEqual(beats, [])


class RunLockProbeTest(unittest.TestCase):
    """`cli_support._run_lock_held`：全局锁探测（让位判据的事实源）。"""

    def test_probe_sees_held_lock_and_frees_after(self):
        if cli_support.fcntl is None:
            self.skipTest("跨进程 flock 仅 POSIX 可用（Windows 上探测退回 False）")
        tmp = tempfile.mkdtemp(prefix="yiban-lock-probe-")
        self.addCleanup(shutil.rmtree, tmp, True)
        with mock.patch.dict(os.environ, {"YIBAN_STATE_DIR": tmp}, clear=False):
            self.assertFalse(cli_support._run_lock_held(), "腾空状态下不该报有人在跑")
            held = cli_support._acquire_run_lock(True,
                                                name=cli_support.GLOBAL_RUN_LOCK_NAME)
            try:
                self.assertTrue(cli_support._run_lock_held(), "锁被持有时没探出来")
            finally:
                held.close()
            self.assertFalse(cli_support._run_lock_held(), "锁释放后仍报有人在跑")

    def test_probe_ignores_own_lock_name_env(self):
        """探测必须**显式**用全局锁名：兜底自己的环境变量里放的是它自己的锁名。"""
        tmp = tempfile.mkdtemp(prefix="yiban-lock-probe-")
        self.addCleanup(shutil.rmtree, tmp, True)
        with mock.patch.dict(
                os.environ,
                {"YIBAN_STATE_DIR": tmp,
                 "YIBAN_RUN_LOCK_NAME": workers.FALLBACK_LOCK_NAME}, clear=False):
            own = cli_support._acquire_run_lock(True, name=workers.FALLBACK_LOCK_NAME)
            try:
                self.assertFalse(cli_support._run_lock_held(),
                                 "把兜底自己的锁当成全量轮在跑（会永远让位）")
            finally:
                own.close()


class SharedGateWiringTest(_WeekdayGuard):
    """门是共享的：定时轮也走 `schedule.day_off`（改一处两边同时变）。"""

    def test_runner_delegates_to_shared_gate(self):
        calls = []

        def _fake_day_off(now=None, **kw):
            calls.append((now, kw))
            return schedule.DAY_OFF_PAUSED

        with mock.patch.object(schedule, "day_off", _fake_day_off), \
                mock.patch.object(runner.clock, "now", lambda: _at(WED, (6, 35))), \
                mock.patch.object(runner, "SATURDAY_SIGN", False), \
                mock.patch.object(runner, "SUNDAY_SIGN", False), \
                mock.patch.object(runner.accounts_mod, "load_accounts",
                                  lambda: [mock.Mock(phone="13800000000")]):
            rc = runner.main([])
        self.assertEqual(rc, 2, "定时轮没把门当回事（应为 SKIPPED 语义的 2）")
        self.assertEqual(len(calls), 1, "定时轮没走共享门 `schedule.day_off`")
        # 导入期快照常量必须照传（既有的周六/周日开关测试按它注入）
        self.assertEqual(set(calls[0][1]), {"sat", "sun"})

    def test_runner_still_returns_2_on_saturday_off(self):
        with mock.patch.dict(os.environ, BASE_ENV, clear=False), \
                mock.patch.object(runner.clock, "now", lambda: _at(SAT, (6, 35))), \
                mock.patch.object(runner, "SATURDAY_SIGN", False), \
                mock.patch.object(runner, "SUNDAY_SIGN", False), \
                mock.patch.object(runner.accounts_mod, "load_accounts",
                                  lambda: [mock.Mock(phone="13800000000")]):
            self.assertEqual(runner.main([]), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
