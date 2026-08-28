# -*- coding: utf-8 -*-
"""容器内签到调度器（docker/scheduler.py）回归测试。

用法（在项目根目录）：
    py -m pytest tests/test_container_scheduler.py -v
    py tests/test_container_scheduler.py          # 无 pytest 也可直接运行

覆盖两代审查修复：

- **批次5 F2**：`build_child_env()` 必须在每次触发时重新解析 .env（Web 后台
  改的全局暂停/周日/探针开关即时生效）。
- **批次7 P1-1**：调度器闸门改为「全量运行标记 sched-run-<date>.json」语义——
  旧 `_signed_today()` 以「任一账号 success」判定已签，用户手动签到或首签部分
  成功都会压制全站 06:31 首签与 07:10 补签（失败账号失去当日兜底）。
  新语义：手动签到（--only）不写标记，不再影响调度器判定。
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "scripts"))

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
    raise _Stop()


def _stub_module(**members):
    """构造一个只含指定成员的模块桩，避免污染真实的 time / subprocess。"""
    return type("_Stub", (), {k: staticmethod(v) for k, v in members.items()})()


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

        批次12 B12-2 语义修正：skipped_window/skipped_norange 不再视为"了结"
        （学校窗口晚开时全员窗口外跳过必须触发补签重跑），已移出本用例。"""
        self._write_marker()
        self._write_state({
            "13800000001": {"status": "success"},
            "13800000002": {"status": "already"},
            "13800000003": {"status": "no_task"},
        })
        self.assertFalse(self.sched._has_undone_today())

    def test_window_skip_is_undone(self):
        """批次12 B12-2：窗口外跳过 = 未了结 → 补签闸门放行重跑。"""
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
        # 三个触发点全部设为 (0,0)：hm >= (0,0) 恒真 → 首轮即全部到达触发判定
        self.sched.FIRST = (0, 0)
        self.sched.SECOND = (0, 0)
        self.sched.PROBE_AT = (0, 0)
        self.sched.LOGDIR = os.path.join(self.tmp, "logs")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run_loop_once(self):
        runs = []
        self.sched.subprocess = _stub_module(run=lambda cmd, **kw: runs.append(cmd))
        self.sched.time = _stub_module(sleep=_stop_sleep)
        with self.assertRaises(_Stop):
            self.sched.main_loop(sleep_seconds=1)
        return runs

    def test_marker_blocks_both_signs_probe_still_runs(self):
        """全量标记存在且全员了结 → 首签/补签都不触发，探针照常触发。"""
        with open(os.path.join(self.tmp, f"sched-run-{_today()}.json"), "w") as f:
            json.dump({"completed": True}, f)
        with open(os.path.join(self.tmp, f"sign-state-{_today()}.json"), "w") as f:
            json.dump({"13800000000": {"status": "success"}}, f)
        runs = self._run_loop_once()
        self.assertEqual(len(runs), 1, "只应触发探针一次")
        self.assertIn("--probe", runs[0])

    def test_no_marker_triggers_both_signs(self):
        """无标记 → 首签与补签都触发（探针因探针未到触发时间在子进程内零请求退出）。"""
        runs = self._run_loop_once()
        self.assertEqual(len(runs), 3, "首签 / 补签 / 探针 三个触发点都应执行")


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
        self.sched.PROBE_AT = (0, 0)
        self.sched.LOGDIR = os.path.join(self.tmp, "logs")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_env_reread_on_every_trigger(self):
        """每次触发都应重新读盘；否则 Web 后台改的设置要重启容器才生效。"""
        with open(self.env_file, "w", encoding="utf-8") as f:
            f.write("YIBAN_TEST_MARKER=v1\n")

        seen = []
        real_build = self.sched.build_child_env

        def build_spy(*_a, **_kw):
            env = real_build(env_file=self.env_file, base={"PATH": "/x"})
            seen.append(env.get("YIBAN_TEST_MARKER", ""))
            with open(self.env_file, "w", encoding="utf-8") as f:
                f.write(f"YIBAN_TEST_MARKER=v{len(seen) + 1}\n")
            return env

        self.sched.build_child_env = build_spy
        runs = []
        self.sched.subprocess = _stub_module(run=lambda cmd, **kw: runs.append(cmd))
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
        self.sched.subprocess = _stub_module(run=lambda cmd, **kw: None)
        self.sched.time = _stub_module(sleep=_stop_sleep)
        with self.assertRaises(_Stop):
            self.sched.main_loop(sleep_seconds=1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
