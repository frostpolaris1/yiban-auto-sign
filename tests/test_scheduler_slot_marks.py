# -*- coding: utf-8 -*-
"""容器调度器守卫回归（2026-09-08）。

逐项活体复现 + 修复钉版：
1. 容器调度触发点落盘标记：同一时段重启后不二次触发（追加轮=必然
   skipped_window 的全站负载）
2. _child_timeout 动态下限不截断晚到重跑的自了结（钉版证明）
"""
import importlib.util
import io
import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_SCHED_PATH = os.path.join(BASE, "docker", "scheduler.py")


def _load_sched(unique_suffix=""):
    """按文件路径加载容器调度器（docker/ 非包内），每次调用返回全新模块实例。"""
    spec = importlib.util.spec_from_file_location(
        f"container_scheduler_marks{unique_suffix}", _SCHED_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Stop(Exception):
    """打断 main_loop 的无限循环。"""


def _today():
    return datetime.now().strftime("%Y-%m-%d")


class _FakeDT(datetime):
    """datetime 替身：now() 返回固定时刻。"""

    _date = (2026, 9, 6)
    _hm = (7, 12)

    @classmethod
    def now(cls, tz=None):
        return cls(*cls._date, *cls._hm)


class _FakeProc:
    def __init__(self):
        self.returncode = 0

    def wait(self, timeout=None):
        return 0

    def terminate(self):
        pass

    def kill(self):
        pass


def _stub_module(**members):
    return type("_Stub", (), {k: staticmethod(v) for k, v in members.items()})()


# ---------------------------------------------------------------------------
# 容器调度触发点落盘标记（重启不二次触发）
# ---------------------------------------------------------------------------
class SlotMarkerRestartTest(unittest.TestCase):
    """hm >= FIRST/SECOND 无上界 + 闩锁仅存内存：容器重启会追加必然
    skipped_window 的全站负载。触发点落盘后，同一时段重启不再二次触发。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-slot-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _prepare(self, sched):
        sched.STATEDIR = self.tmp
        sched.ENV_FILE = os.path.join(self.tmp, ".env")
        sched.FIRST = (0, 0)
        sched.SECOND = (0, 0)
        sched.LOGDIR = os.path.join(self.tmp, "logs")
        sched.build_child_env = lambda env_file=None, base=None, **kw: {}
        sched.subprocess = _stub_module(Popen=lambda cmd, **kw: _FakeProc())
        sched.time = _stub_module(sleep=lambda s: (_ for _ in ()).throw(_Stop()))

    def _tick(self, suffix):
        sched = _load_sched(suffix)
        self._prepare(sched)
        spawns = []
        sched._run_signin_child = lambda extra=None, env=None: spawns.append(extra)
        with self.assertRaises(_Stop):
            sched.main_loop(sleep_seconds=1)
        return sched, spawns

    def test_restart_does_not_retrigger_same_slot(self):
        """首轮正常跑完后容器重启（新进程、当日已触发过）：不得再追加全站轮。"""
        _, first = self._tick("_p1")
        self.assertEqual(len(first), 2, "首签+补签各触发一次")
        # 模拟运行结束后的当日状态：全量标记 + 一个 failed 账号（补签闸门恒真）
        with io.open(os.path.join(self.tmp, f"sched-run-{_today()}.json"), "w") as f:
            json.dump({"completed": True}, f)
        with io.open(os.path.join(self.tmp, f"sign-state-{_today()}.json"), "w") as f:
            json.dump({"13800000000": {"status": "failed"}}, f)
        _, second = self._tick("_p2")
        self.assertEqual(
            second, [],
            "同一时段重启后不得二次触发（追加轮=必然 skipped_window 的全站负载）",
        )

    def test_unfinished_first_run_not_respawned_after_restart(self):
        """首签子进程被杀后重启：首签槽位当日已触发不重跑；
        未了结账号仍由 07:10 补签闸门兜底（补签槽位未触发）。"""
        _, first = self._tick("_q1")
        self.assertEqual(len(first), 2)
        # 不写 sched-run 标记（模拟首签被 timeout 击杀）
        with io.open(os.path.join(self.tmp, f"sign-state-{_today()}.json"), "w") as f:
            json.dump({"13800000000": {"status": "failed"}}, f)
        sched2, second = self._tick("_q2")
        self.assertEqual(second, [], "首签槽位当日已触发过，不得重跑")
        # 补签闸门仍可判定为需要补签（谓词本身不受影响）
        self.assertTrue(sched2._has_undone_today())

    def test_next_day_slot_marker_expires(self):
        """标记按日命名：跨日自动失效（次日仍可正常触发）。"""
        self._tick("_r1")
        files = [n for n in os.listdir(self.tmp) if n.startswith("sched-slot-")]
        self.assertEqual(len(files), 2, "首签/补签各写一个当日槽位标记")
        for n in files:
            self.assertIn(_today(), n, "标记须按日命名，跨日失效")


# ---------------------------------------------------------------------------
# _child_timeout 不截断晚到重跑的自了结（钉版证明）
# ---------------------------------------------------------------------------
class ChildTimeoutBoundTest(unittest.TestCase):
    """晚到触发的重跑进程：动态超时恒大于「距窗口关闭的剩余时间」，
    子进程总能在窗口关闭时自了结（剩余账号 skipped_window 后退出），
    600s 下限只在窗口已关闭时生效——彼时剩余合法工作≈0，截断无害。"""

    def test_timeout_always_exceeds_time_to_window_close(self):
        sched = _load_sched("_t")
        env = {"YIBAN_SIGN_END": "07:50"}
        for hm in ((6, 31), (7, 0), (7, 10), (7, 40), (7, 49), (8, 0)):
            fake = type(f"_DT{hm[0]}{hm[1]}", (_FakeDT,), {"_hm": hm})
            with mock.patch.object(sched, "datetime", fake):
                timeout = sched._child_timeout(env)
            now = datetime.now().replace(year=2026, month=9, day=6,
                                         hour=hm[0], minute=hm[1], second=0)
            end = now.replace(hour=7, minute=50)
            remaining = max(0, (end - now).total_seconds())
            self.assertGreater(
                timeout, remaining,
                f"{hm[0]:02d}:{hm[1]:02d} 触发的重跑必须能活到窗口关闭自行了结",
            )

    def test_late_trigger_floor_is_600(self):
        """窗口已关闭的晚到触发：下限 600s，此时子进程即刻全员窗口外跳过退出。"""
        sched = _load_sched("_u")
        fake = type("_DTLate", (_FakeDT,), {"_hm": (9, 0)})
        with mock.patch.object(sched, "datetime", fake):
            self.assertEqual(sched._child_timeout({"YIBAN_SIGN_END": "07:50"}), 600)


if __name__ == "__main__":
    unittest.main(verbosity=2)
