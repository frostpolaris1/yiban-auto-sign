# -*- coding: utf-8 -*-
"""容器形态的兜底常驻执行体：随签到窗口起停（`docker/scheduler.py`）。

宿主形态的兜底由 cron 每 5 分钟拉起 `scripts/yiban-fallback.sh`（窗口结束由进程自己
退出）。容器形态原先没有这条通道，页面只能靠 `fallback.status` 如实显示"开了却没跑
起来"。这里钉住容器调度器新接的那条：

1. **只在有效签到窗口内拉起**——窗口未开始、窗口已结束都不拉起；
2. **周末门 / 一键暂停门命中不拉起**（判据必须是 `schedule.day_off` 这一份共享实现，
   不是容器侧另写的一套）；
3. **开关打开且窗口内才拉起，且同时只拉起一个**（重复检查不叠进程）；
4. **窗口结束由进程自行退出**，调度器只回收句柄、不再拉起，也不做强杀；
5. **开关关闭时静默**——不 spawn、不产出任何日志（容器日志不得据此刷错误）。

拉起判据复用 `yiban/window.Window.is_open` 与 `yiban/engine/schedule.day_off`；
子进程入口就是宿主的 `sign --fallback`，故独立锁 `signin-run.lock.fallback` 与宿主
形状一致（本文件对锁名与命令行做显式断言）。

用法（项目根目录）：
    .venv/Scripts/python.exe -m pytest tests/test_container_fallback.py -v
"""
import contextlib
import importlib.util
import io
import os
import shutil
import subprocess as _sp
import tempfile
import unittest
from datetime import date, datetime
from unittest import mock

from yiban.engine import workers

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCHED_PATH = os.path.join(BASE, "docker", "scheduler.py")

#: 样例日的星期几在下面用断言钉住，改数据忘了改断言会当场红
WED = date(2026, 9, 2)
SAT = date(2026, 9, 5)
SUN = date(2026, 9, 6)

#: 拉起判据的基线环境（直接作为 `env` 传入，不依赖宿主环境变量）：
#: 窗口 06:30~07:50、周末签到关、无暂停、兜底开关开。
BASE_ENV = {
    "YIBAN_SIGN_START": "06:30",
    "YIBAN_SIGN_END": "07:50",
    "YIBAN_SATURDAY_SIGN": "0",
    "YIBAN_SUNDAY_SIGN": "0",
    "YIBAN_GLOBAL_PAUSE": "0",
    "YIBAN_FALLBACK_ENABLE": "1",
}


def _at(day, hm):
    return datetime(day.year, day.month, day.day, hm[0], hm[1], 0)


def _load_sched():
    """按文件路径加载调度器（它位于 docker/ 而非包内）。

    **用独立模块名**：本文件要断言模块级单例 `_fallback_proc` 的行为，与别的测试
    文件共用同一个模块对象会互相串味。
    """
    spec = importlib.util.spec_from_file_location("container_scheduler_fallback", _SCHED_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeProc:
    """兜底常驻子进程桩：`rc=None` 表示仍在跑，`rc=0` 表示已自行退出。"""

    def __init__(self, rc=None):
        self._rc = rc
        self.terminated = False
        self.killed = False

    def poll(self):
        return self._rc

    def wait(self, timeout=None):
        return self._rc if self._rc is not None else 0

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


class _Stop(Exception):
    """打断 main_loop 的无限循环（在首次 sleep 处抛出）。"""


def _stub_subprocess(recorder=None, proc=None):
    """subprocess 桩模块：Popen 记录命令行与关键字参数并返回假进程。"""

    def _popen(cmd, **kw):
        if recorder is not None:
            recorder.append((cmd, kw))
        return proc if proc is not None else _FakeProc(None)

    return type("_Stub", (), {
        "Popen": staticmethod(_popen),
        "TimeoutExpired": _sp.TimeoutExpired,
    })()


class _GateBase(unittest.TestCase):
    def setUp(self):
        self.assertEqual(WED.weekday(), 2, "样例日不再是周三，改测试数据")
        self.assertEqual(SAT.weekday(), 5, "样例日不再是周六，改测试数据")
        self.assertEqual(SUN.weekday(), 6, "样例日不再是周日，改测试数据")
        self.sched = _load_sched()


class FallbackGateTest(_GateBase):
    """`_fallback_should_run`：四道关的判定表（纯函数，不 spawn）。"""

    def _run(self, env, day, hm):
        return self.sched._fallback_should_run(env, _at(day, hm))

    def test_in_window_with_switch_on(self):
        self.assertTrue(self._run(BASE_ENV, WED, (6, 35)))

    def test_before_window_not_started(self):
        """窗口还没开（06:30 前）不拉起——容器不提前挂常驻，且窗口外请求是白登陆。"""
        self.assertFalse(self._run(BASE_ENV, WED, (6, 5)))
        # 有效窗口 06:31 起（默认前后各让 60 秒）：06:30 整也还不算窗口内
        self.assertFalse(self._run(BASE_ENV, WED, (6, 30)))

    def test_after_window_closed(self):
        self.assertFalse(self._run(BASE_ENV, WED, (8, 30)))
        self.assertFalse(self._run(BASE_ENV, WED, (7, 55)))

    def test_weekend_gate(self):
        self.assertFalse(self._run(BASE_ENV, SAT, (6, 35)))
        self.assertFalse(self._run(BASE_ENV, SUN, (6, 35)))
        # 反向控制：周末签到已开 ⇒ 照拉（门没误伤正中情形）
        self.assertTrue(self._run({**BASE_ENV, "YIBAN_SATURDAY_SIGN": "1"}, SAT, (6, 35)))
        self.assertTrue(self._run({**BASE_ENV, "YIBAN_SUNDAY_SIGN": "true"}, SUN, (6, 35)))

    def test_pause_gate(self):
        self.assertFalse(self._run({**BASE_ENV, "YIBAN_GLOBAL_PAUSE": "1"}, WED, (6, 35)))
        self.assertFalse(self._run({**BASE_ENV, "YIBAN_GLOBAL_PAUSE": "ON"}, WED, (6, 35)))

    def test_switch_off_never_starts(self):
        """开关关：即使正在窗口内也不拉起（未设/0/false/写错的词都算关）。"""
        for raw in (None, "", "0", "false", "off", "no", "yes please"):
            env = {k: v for k, v in BASE_ENV.items() if k != "YIBAN_FALLBACK_ENABLE"}
            if raw is not None:
                env["YIBAN_FALLBACK_ENABLE"] = raw
            with self.subTest(raw=raw):
                self.assertFalse(self._run(env, WED, (6, 35)))

    def test_delegates_to_shared_gate_and_window(self):
        """判据必须走共享实现（打桩即证明容器侧没有另写一套门/窗口判定）。"""
        calls = {}

        def _fake_day_off(now, env=None, **kw):
            calls["day_off"] = now
            return self.sched.schedule.DAY_OFF_PAUSED

        with mock.patch.object(self.sched.schedule, "day_off", _fake_day_off), \
                mock.patch.object(self.sched, "window") as fake_win:
            self.assertFalse(self.sched._fallback_should_run(BASE_ENV, _at(WED, (6, 35))))
        self.assertEqual(calls["day_off"], _at(WED, (6, 35)),
                         "容器侧没走共享门 schedule.day_off")
        fake_win.from_env.assert_not_called()

        class _OpenBounds:
            def is_open(self, now):
                return True

            def is_closed(self, now):
                return False

        with mock.patch.object(self.sched.window, "from_env",
                               return_value=_OpenBounds()) as from_env:
            self.assertTrue(self.sched._fallback_should_run(
                {**BASE_ENV, "YIBAN_GLOBAL_PAUSE": "0"}, _at(WED, (6, 35))))
        from_env.assert_called_once()


class FallbackTickTest(_GateBase):
    """`_tick_fallback`：拉起/回收/不叠进程，以及子进程的命令行与独立锁。"""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="sched-fb-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.sched.STATEDIR = self.tmp
        self.sched.ENV_FILE = os.path.join(self.tmp, ".env")
        with open(self.sched.ENV_FILE, "w", encoding="utf-8") as f:
            f.write("YIBAN_FALLBACK_ENABLE=1\n")
        self.env = dict(BASE_ENV)
        self.spawns = []
        self.proc = _FakeProc(None)
        self.sched.subprocess = _stub_subprocess(self.spawns, self.proc)
        self.sched._fallback_proc = None

    def test_in_window_spawns_exactly_one(self):
        self.assertTrue(self.sched._tick_fallback(_at(WED, (6, 35)), env=self.env))
        # 重复检查（进程仍在跑）不再拉起
        self.assertFalse(self.sched._tick_fallback(_at(WED, (6, 40)), env=self.env))
        self.assertFalse(self.sched._tick_fallback(_at(WED, (6, 45)), env=self.env))
        self.assertEqual(len(self.spawns), 1, "窗口内重复检查叠了进程")
        self.assertIs(self.sched._fallback_proc, self.proc)

    def test_outside_window_does_not_spawn(self):
        self.assertFalse(self.sched._tick_fallback(_at(WED, (6, 5)), env=self.env))
        self.assertFalse(self.sched._tick_fallback(_at(WED, (8, 30)), env=self.env))
        self.assertEqual(self.spawns, [])
        self.assertIsNone(self.sched._fallback_proc)

    def test_gates_block_spawn(self):
        self.assertFalse(self.sched._tick_fallback(_at(SAT, (6, 35)), env=self.env))
        self.assertFalse(self.sched._tick_fallback(
            _at(WED, (6, 35)), env={**self.env, "YIBAN_GLOBAL_PAUSE": "1"}))
        self.assertEqual(self.spawns, [])

    def test_switch_off_is_silent(self):
        """开关关：不 spawn、不打印任何东西（cron/容器日志不得被刷错误）。"""
        for env in ({}, {"YIBAN_FALLBACK_ENABLE": "0"}):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                self.assertFalse(self.sched._tick_fallback(_at(WED, (6, 35)), env=env))
            self.assertEqual(buf.getvalue(), "", "开关关闭时不该产出任何日志")
        self.assertEqual(self.spawns, [])

    def test_child_command_and_independent_lock(self):
        self.assertTrue(self.sched._tick_fallback(_at(WED, (6, 35)), env=self.env))
        cmd, kw = self.spawns[0]
        self.assertEqual(cmd, ["python3", "scripts/signin.py", "--fallback"],
                         "容器拉起入口必须与宿主同一条（sign --fallback）")
        self.assertEqual(kw.get("cwd"), "/app")
        self.assertEqual(kw["env"].get("YIBAN_RUN_LOCK_NAME"), workers.FALLBACK_LOCK_NAME,
                         "容器形态必须持与宿主相同的独立锁名")
        self.assertEqual(workers.FALLBACK_LOCK_NAME, "signin-run.lock.fallback")

    def test_window_end_process_exits_then_no_respawn(self):
        """窗口结束：进程**自行退出**（引擎判定），调度器只回收、不重启、不强杀。"""
        self.assertTrue(self.sched._tick_fallback(_at(WED, (6, 35)), env=self.env))
        self.proc._rc = 0          # 引擎按"窗口已结束"自行退出
        self.assertFalse(self.sched._tick_fallback(_at(WED, (8, 30)), env=self.env))
        self.assertIsNone(self.sched._fallback_proc, "退出后应回收句柄")
        self.assertEqual(len(self.spawns), 1, "窗口外不得重新拉起")
        self.assertFalse(self.proc.terminated or self.proc.killed, "容器侧不得强杀兜底进程")

    def test_respawn_after_exit_while_still_in_window(self):
        """窗口内进程异常退出 → 下一次检查重新拉起（同时仍只有一个）。"""
        self.assertTrue(self.sched._tick_fallback(_at(WED, (6, 35)), env=self.env))
        self.proc._rc = 1          # 异常退出
        self.assertTrue(self.sched._tick_fallback(_at(WED, (6, 40)), env=self.env))
        self.assertEqual(len(self.spawns), 2)

    def test_start_failure_logs_and_keeps_no_handle(self):
        """拉起失败（OSError）不抛、不留句柄，下一次检查可重试。"""
        def _boom(cmd, **kw):
            raise OSError("no such file")

        self.sched.subprocess = type("_Stub", (), {
            "Popen": staticmethod(_boom), "TimeoutExpired": _sp.TimeoutExpired})()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertFalse(self.sched._tick_fallback(_at(WED, (6, 35)), env=self.env))
        self.assertIsNone(self.sched._fallback_proc)
        self.assertIn("拉起兜底常驻执行体失败", buf.getvalue())


class MainLoopFallbackWiringTest(_GateBase):
    """主循环接线：周期检查确实被调用（否则判据再好也没人用）。"""

    def test_main_loop_ticks_fallback(self):
        self.sched.STATEDIR = tempfile.mkdtemp(prefix="sched-fb-loop-")
        self.addCleanup(shutil.rmtree, self.sched.STATEDIR, True)
        self.sched.ENV_FILE = os.path.join(self.sched.STATEDIR, ".env")
        self.sched.FIRST = self.sched.SECOND = (0, 0)
        self.sched.build_child_env = lambda env_file=None, base=None, **kw: {}
        ticks = []
        self.sched._tick_fallback = lambda now=None, env=None: ticks.append(now)
        self.sched.subprocess = _stub_subprocess()

        def _stop(_seconds):
            raise _Stop()

        self.sched.time = type("_Stub", (), {"sleep": staticmethod(_stop)})()
        with self.assertRaises(_Stop):
            self.sched.main_loop(sleep_seconds=1)
        self.assertTrue(ticks, "main_loop 没有周期检查兜底常驻")


if __name__ == "__main__":
    unittest.main(verbosity=2)
