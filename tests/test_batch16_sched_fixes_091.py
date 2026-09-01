# -*- coding: utf-8 -*-
"""批次16 调度修复回归测试（2026-09-01）。

覆盖：
- P2-4：YIBAN_SECOND_RUN=1（run.sh 补签轮 / 容器 scheduler SECOND 时段注入）
  优先于 sched-run 标记判定补签轮——首签子进程被宿主 timeout 击杀（exit 124，
  未写标记）时，"部分成功+窗口外"仍触发告警（B12-2 分支复发修复）；
- P2-5：docker/scheduler.py `_child_timeout` 键优先级 .env（env dict）优先于
  进程环境（os.environ），与 build_child_env 口径一致；
- P2-6：--only 部分命中时对每个未命中号码落 warning 日志，不再静默吞号。

用法（项目根目录）：
    py -m pytest tests/test_batch16_sched_fixes_091.py -v
"""
import importlib.util
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "scripts"))

import signin  # noqa: E402

_SCHED_PATH = os.path.join(BASE, "docker", "scheduler.py")


def _load_sched():
    """按文件路径加载调度器（它位于 docker/ 而非包内，与 test_container_scheduler 同法）。"""
    spec = importlib.util.spec_from_file_location("container_scheduler", _SCHED_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Acc(SimpleNamespace):
    pass


def _mk_acc(phone):
    return _Acc(phone=phone, name="t", owner="admin", user_paused=False)


class OnlyFilterTest(unittest.TestCase):
    """P2-6：--only 部分命中不再静默吞号。"""

    def test_partial_match_logs_missing_warning(self):
        accounts = [_mk_acc("13800000001"), _mk_acc("13800000002")]
        with self.assertLogs(signin.logger, level="WARNING") as cm:
            filtered, missing = signin._apply_only_filter(
                accounts, "13800000001,13899999999"
            )
        self.assertEqual([a.phone for a in filtered], ["13800000001"])
        self.assertEqual(missing, ["13899999999"])
        self.assertTrue(
            any("13899999999" in line for line in cm.output),
            f"warning 日志应包含未命中号码，实际: {cm.output}",
        )

    def test_all_missing_returns_empty(self):
        """全不命中 → 过滤结果为空（main() 据此报错退出，既有行为保持）。"""
        accounts = [_mk_acc("13800000001")]
        filtered, missing = signin._apply_only_filter(accounts, "13900000000")
        self.assertEqual(filtered, [])
        self.assertEqual(missing, ["13900000000"])

    def test_all_match_no_warning(self):
        """全部命中 → 无 warning。"""
        accounts = [_mk_acc("13800000001"), _mk_acc("13800000002")]
        with mock.patch.object(signin.logger, "warning") as m_warn:
            filtered, missing = signin._apply_only_filter(
                accounts, "13800000001, 13800000002"
            )
        self.assertEqual(len(filtered), 2)
        self.assertEqual(missing, [])
        m_warn.assert_not_called()

    def test_whitespace_only_ignored(self):
        """逗号间空白/空段不产生未命中告警。"""
        accounts = [_mk_acc("13800000001")]
        filtered, missing = signin._apply_only_filter(accounts, "13800000001,, ")
        self.assertEqual([a.phone for a in filtered], ["13800000001"])
        self.assertEqual(missing, [])


class SecondRunEnvTest(unittest.TestCase):
    """P2-4：YIBAN_SECOND_RUN=1 优先于 sched-run 标记判定补签轮。"""

    def setUp(self):
        self._old = os.environ.get("YIBAN_SECOND_RUN")
        os.environ.pop("YIBAN_SECOND_RUN", None)

    def tearDown(self):
        os.environ.pop("YIBAN_SECOND_RUN", None)
        if self._old is not None:
            os.environ["YIBAN_SECOND_RUN"] = self._old

    def test_env_overrides_missing_marker(self):
        """核心场景：标记缺失（exit 124 首签被杀）+ YIBAN_SECOND_RUN=1 → 判为补签轮。"""
        with mock.patch.object(signin, "_sched_marker_exists", return_value=False), \
             mock.patch.dict(os.environ, {"YIBAN_SECOND_RUN": "1"}):
            self.assertTrue(signin._is_second_run())

    def test_marker_fallback_without_env(self):
        """无环境变量时回退 sched-run 标记（容器首签正常收尾场景）。"""
        with mock.patch.object(signin, "_sched_marker_exists", return_value=True):
            self.assertTrue(signin._is_second_run())

    def test_first_run_when_no_signal(self):
        """既无环境变量也无标记 → 首签轮。"""
        with mock.patch.object(signin, "_sched_marker_exists", return_value=False):
            self.assertFalse(signin._is_second_run())

    def test_alert_on_partial_success_window_skip_second_run(self):
        """部分成功 + 窗口外：补签轮（YIBAN_SECOND_RUN=1，标记缺失）必须告警。"""
        accounts = [_mk_acc("13800000001"), _mk_acc("13800000002")]
        results = {
            "13800000001": (True, "签到成功", False, signin.STATUS_SUCCESS),
            "13800000002": (False, "签到时段已结束", True, signin.STATUS_SKIPPED_WINDOW),
        }
        with mock.patch.object(signin, "_collect_admin_mail") as m_mail, \
             mock.patch.object(signin, "_sched_marker_exists", return_value=False), \
             mock.patch.dict(os.environ, {"YIBAN_SECOND_RUN": "1"}):
            is_second = signin._is_second_run()
            alerted = signin._maybe_alert_zero_success(
                accounts, results, 1, is_second_run=is_second
            )
        self.assertTrue(is_second, "YIBAN_SECOND_RUN=1 时应判为补签轮")
        self.assertTrue(alerted, "补签轮部分成功+窗口外必须告警")
        m_mail.assert_called_once()

    def test_no_alert_partial_success_first_run(self):
        """部分成功 + 窗口外：首签轮（无环境变量且标记缺失）不告警（避免误报噪音）。"""
        accounts = [_mk_acc("13800000001"), _mk_acc("13800000002")]
        results = {
            "13800000001": (True, "签到成功", False, signin.STATUS_SUCCESS),
            "13800000002": (False, "签到时段已结束", True, signin.STATUS_SKIPPED_WINDOW),
        }
        with mock.patch.object(signin, "_collect_admin_mail") as m_mail, \
             mock.patch.object(signin, "_sched_marker_exists", return_value=False):
            is_second = signin._is_second_run()
            alerted = signin._maybe_alert_zero_success(
                accounts, results, 1, is_second_run=is_second
            )
        self.assertFalse(is_second)
        self.assertFalse(alerted)
        m_mail.assert_not_called()


class ChildTimeoutPrecedenceTest(unittest.TestCase):
    """P2-5：docker/scheduler.py `_child_timeout` 键优先级 .env 优先于进程环境。"""

    def setUp(self):
        self.sched = _load_sched()
        self._old = os.environ.get("YIBAN_RUN_TIMEOUT_SEC")
        os.environ.pop("YIBAN_RUN_TIMEOUT_SEC", None)

    def tearDown(self):
        os.environ.pop("YIBAN_RUN_TIMEOUT_SEC", None)
        if self._old is not None:
            os.environ["YIBAN_RUN_TIMEOUT_SEC"] = self._old

    def test_env_file_wins_over_process_env(self):
        """.env（env dict）900s 优先于进程环境 500s → 900。"""
        os.environ["YIBAN_RUN_TIMEOUT_SEC"] = "500"
        self.assertEqual(self.sched._child_timeout({"YIBAN_RUN_TIMEOUT_SEC": "900"}), 900)

    def test_process_env_used_when_env_file_empty(self):
        """.env 未设置时回退进程环境（500 低于下限 600 → 钳到 600）。"""
        os.environ["YIBAN_RUN_TIMEOUT_SEC"] = "500"
        self.assertEqual(self.sched._child_timeout({}), 600)

    def test_process_env_value_above_floor(self):
        """进程环境 800s 正常生效（>= 下限）。"""
        os.environ["YIBAN_RUN_TIMEOUT_SEC"] = "800"
        self.assertEqual(self.sched._child_timeout({}), 800)

    def test_both_absent_dynamic(self):
        """均未设置 → 按窗口动态计算（>= 下限 600）。"""
        self.assertGreaterEqual(self.sched._child_timeout({}), 600)


if __name__ == "__main__":
    unittest.main(verbosity=2)
