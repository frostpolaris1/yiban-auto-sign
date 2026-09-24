# -*- coding: utf-8 -*-
"""容器内签到调度器（docker/scheduler.py）回归测试。

标签：B · 调度：领取/队列/执行体
覆盖：容器调度器的两代修复：build_child_env 每次触发重解析 .env、首签/补签闸门改以
   sched-run-<date>.json 全量标记为准（含标记损坏/非 dict/缺失的 fail-safe
   分档、failed/retrying/pending/window-skip
   判未了结、全员了结才跳过）、main_loop
   与闸门谓词的一致性（探针周期尝试照常触发）、兜底常驻的四道关判定与拉起/回收/不叠进程、主循环接线、web
   层会话与锚点修复回归、签到子进程挂起时循环留痕继续。
对应实现：docker/scheduler.py（_full_run_done_today、_has_undone_today、main_loop、build_child_env、_fallback_should_run、_tick_fallback、_run_signin_child）、web/app.py
   的会话/锚点路径、yiban/engine/workers.py。
关键断言：闸门判据必须是「当日全量是否收尾」而不是「任一账号是否成功」——旧语义下用户手动签到或首签部分成功都会压制
   06:31 首签与 07:10
   补签，让失败账号失去当日兜底。标记缺失或损坏一律按「未跑过」放行（宁可重跑，不可漏签；signin
   内部幂等）。窗口结束由引擎自行判定退出，调度器只回收、不重启、不强杀。判据只有一份：容器侧 `_UNDONE_STATUSES` 是 `signin.UNDONE_STATUSES` 的别名、`_full_run_done_today` 转调 signin 的实现。本文件的用例直接调容器侧谓词，所以「容器侧另写一套门」这种回退要靠读源文件才发现，没有用例拦得住。
依赖：按文件路径加载 docker/scheduler.py（它不在包内）；临时状态目录 +
   subprocess/time 模块桩（真循环靠 sleep 抛异常打断）。web 类用例起临时库与
   Flask test client。不发网络请求。整文件在本机执行，无 skip。

用法（在项目根目录）：
    py -m pytest tests/test_container_scheduler.py -v
    py tests/test_container_scheduler.py          # 无 pytest 也可直接运行

覆盖两代审查修复：

- `build_child_env()` 必须在每次触发时重新解析 .env（Web 后台
  改的全局暂停/周日/探针开关即时生效）。
- 调度器闸门改为「全量运行标记 sched-run-<date>.json」语义——
  旧 `_signed_today()` 以「任一账号 success」判定已签，用户手动签到或首签部分
  成功都会压制全站 06:31 首签与 07:10 补签（失败账号失去当日兜底）。
  新语义：手动签到（--only）不写标记，不再影响调度器判定。
"""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess as _sp
import sys
import tempfile
import unittest
from datetime import date, datetime
from types import SimpleNamespace
from unittest import mock

import db

from yiban.engine import workers

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_SCHED_PATH = os.path.join(BASE, "docker", "scheduler.py")


def _load_sched():
    """按文件路径加载调度器（它位于 docker/ 而非包内）。"""
    spec = importlib.util.spec_from_file_location("container_scheduler", _SCHED_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Stop(Exception):
    """测试中用于打断 main_loop 的无限循环（在首次 sleep 处抛出）。"""


def _stop_sleep(_seconds):
    """替换 sched.time.sleep：首次调用即抛出 _Stop，用于跳出无限循环。"""
    raise _Stop() # 主循环是 while True：不打断这一觉，用例会挂死而不是失败


class _FakeProc:
    """_run_signin_child 的 Popen 桩：wait() 立即正常返回（退出码 0）。"""

    returncode = 0

    def wait(self, timeout=None):
        return 0

    def terminate(self):
        pass

    def kill(self):
        pass


def _stub_subprocess(recorder=None):
    """构造 subprocess 桩模块：Popen 返回立即退出的假进程。

    _run_signin_child 使用 Popen + wait（超时先 SIGTERM 再 SIGKILL），
    桩需同时提供 TimeoutExpired 供 except 分支引用。
    """
    import subprocess as _sp

    def _popen(cmd, **kw):
        if recorder is not None:
            recorder.append(cmd)
        return _FakeProc()

    return _stub_module(Popen=_popen, TimeoutExpired=_sp.TimeoutExpired)


def _stub_module(**members):
    """构造一个只含指定成员的模块桩，避免污染真实的 time / subprocess。"""
    return type("_Stub", (), {k: staticmethod(v) for k, v in members.items()})() # 交出列出的那几样就够：整模块打桩会污染同进程里别的用例


def _today():
    return datetime.now().strftime("%Y-%m-%d")


class FullRunGateTest(unittest.TestCase):
    """P1-1：首签/补签闸门以 sched-run-*.json 全量标记为准，手动签到不得压制。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sched-")
        self.sched = _load_sched()
        self.sched.STATEDIR = self.tmp
        self.sched.ENV_FILE = os.path.join(self.tmp, ".env")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers --
    def _marker_path(self):
        return os.path.join(self.tmp, f"sched-run-{_today()}.json")

    def _state_path(self):
        return os.path.join(self.tmp, f"sign-state-{_today()}.json")

    def _write_marker(self, **extra):
        with open(self._marker_path(), "w", encoding="utf-8") as f:
            json.dump({"completed": True, **extra}, f)

    def _write_state(self, data):
        with open(self._state_path(), "w", encoding="utf-8") as f:
            json.dump(data, f)

    # -- 首签闸门：_full_run_done_today --
    def test_no_marker_allows_first_sign(self):
        """无标记（当日全量未跑）→ 首签闸门放行。"""
        self.assertFalse(self.sched._full_run_done_today())

    def test_marker_blocks_first_sign(self):
        """全量已跑 → 首签跳过（调度器重启不再重跑）。"""
        self._write_marker()
        self.assertTrue(self.sched._full_run_done_today())

    def test_manual_sign_state_does_not_write_marker(self):
        """核心回归：手动签到只写 sign-state（success），不得生成全量标记。"""
        self._write_state({"13800000000": {"status": "success", "message": "签到成功"}})
        self.assertFalse(
            self.sched._full_run_done_today(),
            "手动签到的 success 不能再压制全站首签（P1-1 主断言）",
        )

    def test_corrupt_marker_treated_as_not_done(self):
        """标记文件损坏按「未跑过」处理（宁可重跑，不可漏签；signin 内部幂等）。"""
        with open(self._marker_path(), "w", encoding="utf-8") as f:
            f.write("{not json")
        self.assertFalse(self.sched._full_run_done_today())

    def test_marker_non_dict_treated_as_not_done(self):
        with open(self._marker_path(), "w", encoding="utf-8") as f:
            json.dump(["unexpected"], f)
        self.assertFalse(self.sched._full_run_done_today())

    # -- 补签闸门：_has_undone_today --
    def test_failed_state_needs_second_sign(self):
        """首签存在失败账号 → 补签必须重跑（旧 any-success 语义会误跳过）。"""
        self._write_marker()
        self._write_state({
            "13800000001": {"status": "failed"},
            "13800000002": {"status": "success"},
        })
        self.assertTrue(self.sched._has_undone_today())

    def test_retrying_state_needs_second_sign(self):
        self._write_marker()
        self._write_state({"13800000000": {"status": "retrying"}})
        self.assertTrue(self.sched._has_undone_today())

    def test_pending_state_counts_as_undone(self):
        """计划已写（pending）但未执行 → 补签应跑。"""
        self._write_marker()
        self._write_state({"13800000000": {"status": "pending"}})
        self.assertTrue(self.sched._has_undone_today())

    def test_all_done_skips_second_sign(self):
        """全员真正了结（success/already/no_task）→ 补签跳过，不再全天空跑两遍。

        语义修正：skipped_window/skipped_norange 不再视为"了结"
        （学校窗口晚开时全员窗口外跳过必须触发补签重跑），已移出本用例。"""
        self._write_marker()
        self._write_state({
            "13800000001": {"status": "success"},
            "13800000002": {"status": "already"},
            "13800000003": {"status": "no_task"},
        })
        self.assertFalse(self.sched._has_undone_today())

    def test_window_skip_is_undone(self):
        """窗口外跳过 = 未了结 → 补签闸门放行重跑。"""
        self._write_marker()
        self._write_state({
            "13800000001": {"status": "success"},
            "13800000002": {"status": "skipped_window"},
        })
        self.assertTrue(self.sched._has_undone_today())

    def test_manual_only_success_is_not_undone_but_first_gate_still_open(self):
        """手动签到成功后：无失败记录 → 补签不跑；但首签闸门仍开（标记缺失）。"""
        self._write_state({"13800000000": {"status": "success"}})
        self.assertFalse(self.sched._has_undone_today())
        self.assertFalse(self.sched._full_run_done_today())

    def test_missing_or_empty_state_counts_as_undone(self):
        """无状态记录 = 当天还没跑过 → 允许触发（防漏签）。"""
        self.assertTrue(self.sched._has_undone_today())
        self._write_state({})
        self.assertTrue(self.sched._has_undone_today())


class MainLoopGateTest(unittest.TestCase):
    """main_loop 集成：闸门谓词与实际子进程触发的一致性。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sched-")
        self.sched = _load_sched()
        self.sched.STATEDIR = self.tmp
        self.env_file = os.path.join(self.tmp, ".env")
        self.sched.ENV_FILE = self.env_file
        # 首签/补签触发点设为 (0,0)：hm >= (0,0) 恒真 → 首轮即全部到达触发判定
        self.sched.FIRST = (0, 0)
        self.sched.SECOND = (0, 0)
        # 探针为周期尝试（PROBE_TRY_SECONDS），首轮 last_probe_try=None 必尝试；
        # 默认视为开启（stub 返回 PROBE_ENABLE=1），未开启短路在专门用例覆盖
        self.sched.build_child_env = lambda env_file=None, base=None, **kw: {
            "YIBAN_PROBE_ENABLE": "1",
        }
        self.sched.LOGDIR = os.path.join(self.tmp, "logs")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run_loop_once(self):
        runs = []
        self.sched.subprocess = _stub_subprocess(recorder=runs)
        self.sched.time = _stub_module(sleep=_stop_sleep)
        with self.assertRaises(_Stop):
            self.sched.main_loop(sleep_seconds=1)
        return runs

    def test_marker_blocks_both_signs_probe_still_runs(self):
        """全量标记存在且全员了结 → 首签/补签都不触发，探针周期尝试照常触发。"""
        with open(os.path.join(self.tmp, f"sched-run-{_today()}.json"), "w") as f:
            json.dump({"completed": True}, f)
        with open(os.path.join(self.tmp, f"sign-state-{_today()}.json"), "w") as f:
            json.dump({"13800000000": {"status": "success"}}, f)
        runs = self._run_loop_once()
        self.assertEqual(len(runs), 1, "只应触发探针一次")
        self.assertIn("--probe", runs[0])

    def test_no_marker_triggers_both_signs(self):
        """无标记 → 首签与补签都触发（探针开启时也周期尝试一次）。"""
        runs = self._run_loop_once()
        self.assertEqual(len(runs), 3, "首签 / 补签 / 探针 三个触发点都应执行")

    def test_probe_disabled_skips_spawn(self):
        """探针未开启（.env 无/为 0）：周期尝试时不 spawn 子进程，首签/补签不受影响。"""
        self.sched.build_child_env = lambda env_file=None, base=None, **kw: {}
        runs = self._run_loop_once()
        self.assertEqual(len(runs), 2, "仅首签 / 补签触发")
        self.assertFalse(
            any("--probe" in r for r in runs), "探针未开启时不得 spawn --probe"
        )


class EnvReloadTest(unittest.TestCase):
    """F2：main_loop 必须在每次触发时重新解析 .env，而不是用启动快照。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sched-")
        self.sched = _load_sched()
        self.sched.STATEDIR = self.tmp
        self.env_file = os.path.join(self.tmp, ".env")
        self.sched.ENV_FILE = self.env_file
        self.sched.FIRST = (0, 0)
        self.sched.SECOND = (0, 0)
        self.sched.LOGDIR = os.path.join(self.tmp, "logs")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_env_reread_on_every_trigger(self):
        """每次触发都应重新读盘；否则 Web 后台改的设置要重启容器才生效。

        探针为周期尝试，同样每次重新解析 .env（未开启时不 spawn）——
        spy 写回时保留 PROBE_ENABLE=1，使首签 / 补签 / 探针三个触发点都执行。
        """
        with open(self.env_file, "w", encoding="utf-8") as f:
            f.write("YIBAN_TEST_MARKER=v1\n")

        seen = []
        real_build = self.sched.build_child_env

        def build_spy(*_a, **_kw):
            env = real_build(env_file=self.env_file, base={"PATH": "/x"})
            seen.append(env.get("YIBAN_TEST_MARKER", ""))
            with open(self.env_file, "w", encoding="utf-8") as f:
                f.write(
                    "YIBAN_PROBE_ENABLE=1\n"
                    f"YIBAN_TEST_MARKER=v{len(seen) + 1}\n"
                )
            return env

        self.sched.build_child_env = build_spy
        runs = []
        self.sched.subprocess = _stub_subprocess(recorder=runs)
        self.sched.time = _stub_module(sleep=_stop_sleep)

        with self.assertRaises(_Stop):
            self.sched.main_loop(sleep_seconds=1)

        self.assertEqual(len(runs), 3, "首签 / 补签 / 探针 三个触发点都应执行")
        self.assertEqual(
            seen, ["v1", "v2", "v3"],
            "三次触发必须各自重新解析 .env（依次读到 v1/v2/v3），"
            "若出现重复值说明仍在复用启动时的环境快照",
        )

    def test_env_file_missing_does_not_crash(self):
        """.env 缺失时安全退化为纯继承，不应抛异常。"""
        if os.path.exists(self.env_file):
            os.remove(self.env_file)
        self.sched.subprocess = _stub_subprocess()
        self.sched.time = _stub_module(sleep=_stop_sleep)
        with self.assertRaises(_Stop):
            self.sched.main_loop(sleep_seconds=1)


WED = date(2026, 9, 2)


SAT = date(2026, 9, 5)


SUN = date(2026, 9, 6)


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


def _load_sched_FALLBACK():
    """按文件路径加载调度器（它位于 docker/ 而非包内）。

    **用独立模块名**：本文件要断言模块级单例 `_fallback_proc` 的行为，与别的测试
    文件共用同一个模块对象会互相串味。
    """
    spec = importlib.util.spec_from_file_location("container_scheduler_fallback", _SCHED_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeProc_FALLBACK:
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


class _Stop_FALLBACK(Exception):
    """打断 main_loop 的无限循环（在首次 sleep 处抛出）。"""


def _stub_subprocess_FALLBACK(recorder=None, proc=None):
    """subprocess 桩模块：Popen 记录命令行与关键字参数并返回假进程。"""

    def _popen(cmd, **kw):
        if recorder is not None:
            recorder.append((cmd, kw))
        return proc if proc is not None else _FakeProc_FALLBACK(None)

    return type("_Stub", (), {
        "Popen": staticmethod(_popen),
        "TimeoutExpired": _sp.TimeoutExpired,
    })()


class _GateBase(unittest.TestCase):
    def setUp(self):
        self.assertEqual(WED.weekday(), 2, "样例日不再是周三，改测试数据")
        self.assertEqual(SAT.weekday(), 5, "样例日不再是周六，改测试数据")
        self.assertEqual(SUN.weekday(), 6, "样例日不再是周日，改测试数据")
        self.sched = _load_sched_FALLBACK()


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
        self.proc = _FakeProc_FALLBACK(None)
        self.sched.subprocess = _stub_subprocess_FALLBACK(self.spawns, self.proc)
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
        self.sched.subprocess = _stub_subprocess_FALLBACK()

        def _stop(_seconds):
            raise _Stop_FALLBACK()

        self.sched.time = type("_Stub", (), {"sleep": staticmethod(_stop)})()
        with self.assertRaises(_Stop_FALLBACK):
            self.sched.main_loop(sleep_seconds=1)
        self.assertTrue(ticks, "main_loop 没有周期检查兜底常驻")


TEST_KEY = "a" * 64


AUDIT_KEY = "b" * 64


ADMIN_PASS = "MasterPass#2026"


USER_PASS = "secret1"


EMAIL = "user1@test.local"


PHONE = "13800138001"


def _load_webapp():
    spec = importlib.util.spec_from_file_location("webapp", os.path.join(BASE, "web", "app.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["webapp"] = mod
    with contextlib.suppress(Exception):
        spec.loader.exec_module(mod)
    return mod


class Batch9WebTest(unittest.TestCase):
    """web 层修复回归（P3-1/2/4/5/7/8）。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="batch9-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        cls._env_content = (
            f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
            f"YIBAN_AUDIT_KEY={AUDIT_KEY}\n"
            "YIBAN_ADMIN_USER=admin@test.local\n"
            f"YIBAN_ADMIN_PASSWORD={ADMIN_PASS}\n"
        )
        with io.open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(cls._env_content)
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.accounts_file = os.path.join(cls.tmp, "accounts.json")
        cls.state_dir = os.path.join(cls.tmp, "state")
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
        os.environ["YIBAN_AUDIT_KEY"] = AUDIT_KEY
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_ACCOUNTS_FILE"] = cls.accounts_file
        os.environ["YIBAN_DB_FILE"] = cls.db_file
        os.environ["YIBAN_STATE_DIR"] = cls.state_dir
        os.environ["YIBAN_LOG_FILE"] = os.path.join(cls.tmp, "sign.log")
        cls.webapp = _load_webapp()

    @classmethod
    def tearDownClass(cls):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k in ("YIBAN_ACCOUNTS_KEY", "YIBAN_AUDIT_KEY", "YIBAN_LOG_FILE", "YIBAN_STATE_DIR"):
            os.environ.pop(k, None)

    def setUp(self):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        for suffix in ("", "-wal", "-shm"):
            p = self.db_file + suffix
            if os.path.exists(p):
                os.remove(p)
        with io.open(self.accounts_file, "w", encoding="utf-8") as f:
            json.dump([], f)
        with io.open(self.env_file, "w", encoding="utf-8") as f:
            f.write(self._env_content)
        db.init_db(self.db_file, migrate_from=self.accounts_file, env_file=self.env_file)
        os.makedirs(self.state_dir, exist_ok=True)

    # ---- 工具 ----
    def _login(self, c, username, password):
        r = c.post("/api/login", json={"username": username, "password": password})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return c.get("/api/me").get_json()["csrf_token"]

    def _csrf(self, token):
        return {"X-CSRF-Token": token}

    def _session_cookie(self, c):
        """从登录后的 client 提取会话 cookie（模拟复制被盗 cookie）。"""
        for header in (c.get("/api/me").headers.getlist("Set-Cookie") or []):
            name_val = header.split(";", 1)[0]
            name, _, val = name_val.partition("=")
            if name.strip() and val:
                return name.strip(), val
        self.fail("未找到会话 cookie")

    def _client_with_cookie(self, cookie_name, cookie_val):
        """构造携带指定会话 cookie 的 client（注入 jar，兼容多版本 Werkzeug 签名）。"""
        c = self.webapp.create_app().test_client()
        injected = False
        for kwargs in (
            {"key": cookie_name, "value": cookie_val, "domain": "localhost", "path": "/"},
            {"server_name": "localhost", "key": cookie_name, "value": cookie_val, "path": "/"},
        ):
            try:
                c.set_cookie(**kwargs)
                injected = True
                break
            except TypeError:
                continue
        if not injected:
            self.fail("当前 Werkzeug 版本无法注入测试 cookie")
        return c

    def _user_with_account(self):
        h = self.webapp.generate_password_hash
        db.create_user(EMAIL, h(USER_PASS))
        c = self.webapp.create_app().test_client()
        t = self._login(c, EMAIL, USER_PASS)
        r = c.post("/api/my-accounts", json={"name": "n1", "phone": PHONE, "password": "p"},
                   headers=self._csrf(t))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        ac = self.webapp.create_app().test_client()
        at = self._login(ac, "admin@test.local", ADMIN_PASS)
        idx = next(i for i, a in enumerate(db.load_accounts()) if a["phone"] == PHONE)
        r = ac.post(f"/api/accounts/{idx}/review", json={"action": "approve"},
                    headers=self._csrf(at))
        self.assertEqual(r.status_code, 200)
        return c, t

    # ---- P3-1 锚点缺失检测 ----
    def test_anchor_missing_after_recorded_is_detected(self):
        db.audit("admin", "op1", "t", "d")
        db.record_audit_anchor()
        anchor = db.audit_anchor_path()
        self.assertTrue(os.path.exists(anchor))
        ok, msg = db.verify_audit_anchor()
        self.assertTrue(ok, msg)
        os.remove(anchor)
        ok, msg = db.verify_audit_anchor()
        self.assertFalse(ok, "app_meta 记录过锚点而文件消失必须可检出")
        self.assertIn("锚点文件缺失", msg)
        # 恢复链路：重新记录后恢复健康
        db.record_audit_anchor()
        ok, msg = db.verify_audit_anchor()
        self.assertTrue(ok, msg)

    # ---- P3-2 遗留事务安全回滚 ----
    def test_leaked_transaction_rolled_back_not_fatal(self):
        h = self.webapp.generate_password_hash
        db.create_user(EMAIL, h(USER_PASS))
        acc_id = db.add_account({
            "name": "n", "phone": PHONE, "password": "p",
            "owner": EMAIL, "status": "active",
        })
        conn = db.get_conn()
        with db._conn_lock:
            conn.execute(
                "INSERT INTO users (email, password_hash, role, created_at, pw_version) "
                "VALUES ('ghost@test.local', 'h', 'user', '2026-01-01 00:00:00', 1)"
            )
            # 故意不提交 → 模拟某写路径泄漏的半事务
        # 走 _begin_immediate 防御的写路径：回滚遗留事务后正常执行（原先直接抛
        # "within a transaction" 连锁锁死全部写路径）
        db.batch_account_ops([("set_deleted", acc_id, 1, "2026-01-02 00:00:00")])
        ghost = db.get_conn().execute(
            "SELECT email FROM users WHERE email='ghost@test.local'"
        ).fetchone()
        self.assertIsNone(ghost, "被泄漏的半事务必须被安全回滚，不得被顺带发布")
        row = next(a for a in db.load_accounts() if a["id"] == acc_id)
        self.assertEqual(row["deleted"], 1, "正常业务写入不受遗留事务影响")

    # ---- P3-4 rechain 分批 ----
    def test_rechain_keeps_long_chain_consistent(self):
        for i in range(30):
            db.audit("admin", f"op{i}", "t", "d")
        conn = db.get_conn()
        with db._conn_lock, conn:
            conn.execute("UPDATE audit_logs SET hash='', prev_hash=''")
        db._rechain_audit_logs(conn)
        ok, broken, _ = db.verify_audit_chain()
        self.assertTrue(ok, f"分批重链后链必须自洽（broken={broken}）")
        self.assertEqual(broken, 0)

    # ---- P3-5 会话吊销 ----
    def test_logout_revokes_stolen_cookie(self):
        c, t = self._user_with_account()
        name, val = self._session_cookie(c)
        stolen = self._client_with_cookie(name, val)
        # 未登出前：被盗 cookie 有效
        r = stolen.get("/api/me")
        self.assertEqual(r.status_code, 200)
        # 登出 → sid 轮换 → 被盗 cookie 重放失效
        r = c.post("/api/logout", json={}, headers=self._csrf(t))
        self.assertEqual(r.status_code, 200)
        r = stolen.get("/api/me")
        self.assertEqual(r.status_code, 401, "登出后旧 cookie 副本必须失效（P3-5 主断言）")
        # 重新登录后一切正常
        t2 = self._login(c, EMAIL, USER_PASS)
        r = c.get("/api/me", headers=self._csrf(t2))
        self.assertEqual(r.status_code, 200)

    def test_admin_password_reset_revokes_target_sessions(self):
        c, _t = self._user_with_account()
        name, val = self._session_cookie(c)
        ac = self.webapp.create_app().test_client()
        at = self._login(ac, "admin@test.local", ADMIN_PASS)
        r = ac.post(f"/api/users/{EMAIL}/password",
                    json={"password": "NewPass#777", "confirm_password": ADMIN_PASS},
                    headers=self._csrf(at))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        stolen2 = self._client_with_cookie(name, val)
        r = stolen2.get("/api/me")
        self.assertEqual(r.status_code, 401, "被重置密码后旧会话必须失效（sid 已轮换）")

    def test_self_password_change_keeps_current_session(self):
        c, t = self._user_with_account()
        r = c.post("/api/me/password", json={
            "old_password": USER_PASS,
            "new_password": "NewPass#888",
            "confirm_password": "NewPass#888",
        }, headers=self._csrf(t))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        # 既有产品语义：改密即当前会话失效（响应文案"下次登录使用新密码"）；
        # P3-5 增量是 sid 已轮换——改密前的被盗 cookie 副本同样失效
        r = c.get("/api/me")
        self.assertEqual(r.status_code, 401)
        row = db.get_conn().execute(
            "SELECT sid FROM users WHERE email=?", (EMAIL,)
        ).fetchone()
        self.assertNotEqual(row["sid"], "", "改密后 sid 必须已重新签发")

    # ---- P3-7 混合大小写主管理员自助改密 ----
    def test_mixed_case_admin_can_change_password(self):
        original = io.open(self.env_file, encoding="utf-8").read()
        try:
            with io.open(self.env_file, "w", encoding="utf-8") as f:
                f.write(
                    f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
                    f"YIBAN_AUDIT_KEY={AUDIT_KEY}\n"
                    "YIBAN_ADMIN_USER=Admin@Test.Local\n"
                    f"YIBAN_ADMIN_PASSWORD={ADMIN_PASS}\n"
                )
            c = self.webapp.create_app().test_client()
            t = self._login(c, "Admin@Test.Local", ADMIN_PASS)
            r = c.post("/api/me/password", json={
                "old_password": ADMIN_PASS,
                "new_password": "Rotated#2026x",
                "confirm_password": "Rotated#2026x",
            }, headers=self._csrf(t))
            self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        finally:
            with io.open(self.env_file, "w", encoding="utf-8") as f:
                f.write(original)

    # ---- P3-8 恢复的每 IP 聚合失败窗口 ----
    def test_restore_per_ip_aggregate_rate_limit(self):
        # 性能优化（2026-09-01）：本用例被测对象是「每 IP 聚合限速窗口」，而非
        # 密码校验本身；40 次对不存在账号的请求每次都会走 scrypt 时延拉平
        # （_constant_time_dummy，安全设计约 0.35s/次）→ 全量串行 14s。mock 掉
        # scrypt 比对为常数时间开销，限速行为判定不受影响（429 来自
        # user_delete_requests 每 IP 计数，与密码校验结果无关）。
        # 注：patch 对象必须是 self.webapp（模块名 "webapp"，非 "web.app"）。
        with mock.patch.object(
                self.webapp, "_constant_time_dummy", lambda pwd: None), \
             mock.patch.object(
                self.webapp, "check_password_hash", lambda h, p: False):
            c = self.webapp.create_app().test_client()
            got_429 = False
            for i in range(40):
                r = c.post("/api/me/restore", json={
                    "email": f"ghost{i}@x.test", "password": "WrongPass#1",
                })
                if r.status_code == 429:
                    got_429 = True
                    break
        self.assertTrue(got_429, "跨邮箱喷洒恢复请求必须在每 IP 聚合窗口处被 429")

    # ---- P3-11 探针清熔断 ----
    def test_probe_healthy_clears_fuse_pause(self):
        import signin as signin_mod

        cred = {PHONE: {"paused_since": "2026-08-01", "probe_date": "2026-01-01"}}
        with io.open(os.path.join(self.state_dir, "cred-state.json"), "w",
                     encoding="utf-8") as f:
            json.dump(cred, f)
        with mock.patch.object(signin_mod, "PROBE_ENABLE", True), \
             mock.patch.object(signin_mod, "_health_probe_due", lambda: True), \
             mock.patch.object(signin_mod, "verify_account",
                               lambda acc: (True, "账号健康，可正常签到")), \
             mock.patch.object(signin_mod, "db") as fake_db, \
             mock.patch.dict(os.environ, {"YIBAN_STATE_DIR": self.state_dir}):
            fake_db.is_initialized.return_value = False  # 探针内不触碰会话缓存
            acc = SimpleNamespace(phone=PHONE, owner="o", password="p",
                                  phone_model="", phone_code="")
            signin_mod.run_probe([acc])
        p = os.path.join(self.state_dir, "cred-state.json")
        saved = json.load(io.open(p, encoding="utf-8")) if os.path.exists(p) else {}
        self.assertNotIn(PHONE, saved, "探针确认健康必须清除熔断暂停")


class SchedulerTimeoutTest(unittest.TestCase):
    """P3-14：签到子进程挂起时调度循环留痕继续，不永久卡死。"""

    def test_timeout_does_not_kill_loop(self):
        import subprocess as sp

        sched_path = os.path.join(BASE, "docker", "scheduler.py")
        spec = importlib.util.spec_from_file_location("container_scheduler2", sched_path)
        sched = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sched)

        class _HangProc:
            """wait(timeout) 恒超时（模拟挂起子进程）；kill 后的无超时 wait 正常返回。"""

            returncode = -9

            def wait(self, timeout=None):
                if timeout is not None:
                    raise sp.TimeoutExpired(cmd="signin", timeout=timeout)
                return self.returncode

            def terminate(self):
                pass

            def kill(self):
                pass

        sched.subprocess = type(
            "_Stub", (), {"Popen": staticmethod(lambda cmd, **kw: _HangProc()),
                          "TimeoutExpired": sp.TimeoutExpired})()
        sched._run_signin_child()  # 不应抛异常（TimeoutExpired 被捕获留痕）


if __name__ == "__main__":
    unittest.main(verbosity=2)
