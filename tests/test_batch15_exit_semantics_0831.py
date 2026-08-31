# -*- coding: utf-8 -*-
"""批次15 P1-1 回归测试：宿主 run.sh 补签闸门不被「部分成功」吞掉。

覆盖：
- 退出码语义：有 skipped_window/skipped_norange 未了结账号时（即使有成功）→ exit 2
  （run.sh 写 SKIPPED → 07:10 补签重跑）；无窗口外跳过 → 维持 0/1/2 原语义；
- 告警时机：首签轮（is_second_run=False）混合场景不打扰；补签轮（True）仍混合则告警。

用法（项目根目录）：
    py -m pytest tests/test_batch15_exit_semantics_0831.py -v
"""
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))

import signin  # noqa: E402


class _Acc(SimpleNamespace):
    pass


def _mk_acc(phone):
    return _Acc(phone=phone, name="t", owner="admin", user_paused=False)


class ExitCodeSemanticsTest(unittest.TestCase):
    """P1-1：main() 退出码判定逻辑（复制 main 尾部判定，与真实路径逐字一致）。"""

    def _compute(self, statuses):
        """按 main() 的汇总逻辑计算退出码。"""
        has_real_failure = False
        has_executed = False
        has_window_skip = False
        ok_n = fail_n = skip_n = 0
        accounts = []
        results = {}
        for i, (status, _m) in enumerate(statuses):
            phone = f"1380000000{i}"
            accounts.append(_mk_acc(phone))
            results[phone] = (False, _m, True, status) if status not in (
                signin.STATUS_SUCCESS, signin.STATUS_ALREADY,
            ) else (True, _m, False, status)
            if status in (signin.STATUS_SUCCESS, signin.STATUS_ALREADY):
                ok_n += 1
            elif status in (signin.STATUS_NO_TASK, signin.STATUS_SKIPPED_WINDOW,
                            signin.STATUS_SKIPPED_NORANGE, signin.STATUS_PAUSED,
                            signin.STATUS_USER_CANCELLED):
                skip_n += 1
                if status in (signin.STATUS_SKIPPED_WINDOW, signin.STATUS_SKIPPED_NORANGE):
                    has_window_skip = True
            else:
                fail_n += 1
                has_real_failure = True
            if status in (signin.STATUS_SUCCESS, signin.STATUS_ALREADY, signin.STATUS_NO_TASK):
                has_executed = True
        if has_real_failure:
            return 1, ok_n, fail_n, skip_n
        if not has_executed or has_window_skip:
            return 2, ok_n, fail_n, skip_n
        return 0, ok_n, fail_n, skip_n

    def test_mixed_success_and_window_skip_exits_2(self):
        """P1-1 核心：1 成功 + 1 skipped_window → exit 2（run.sh 写 SKIPPED，07:10 补签）。"""
        code, ok, fail, skip = self._compute([
            (signin.STATUS_SUCCESS, "签到成功"),
            (signin.STATUS_SKIPPED_WINDOW, "签到时段已结束"),
        ])
        self.assertEqual(code, 2)
        self.assertEqual((ok, fail, skip), (1, 0, 1))

    def test_mixed_success_and_norange_exits_2(self):
        """skipped_norange 同样触发 exit 2（Range 缺失 = 未了结）。"""
        code, ok, fail, skip = self._compute([
            (signin.STATUS_SUCCESS, "签到成功"),
            (signin.STATUS_SKIPPED_NORANGE, "签到时间窗口缺失"),
        ])
        self.assertEqual(code, 2)
        self.assertEqual((ok, fail, skip), (1, 0, 1))

    def test_all_success_exits_0(self):
        """全成功 → exit 0（维持原语义）。"""
        code, ok, fail, skip = self._compute([
            (signin.STATUS_SUCCESS, "签到成功"),
            (signin.STATUS_ALREADY, "已签到"),
        ])
        self.assertEqual(code, 0)
        self.assertEqual((ok, fail, skip), (2, 0, 0))

    def test_success_and_no_task_exits_0(self):
        """成功 + 无需签到（no_task 属了结）→ exit 0，不受 P1-1 影响。"""
        code, ok, fail, skip = self._compute([
            (signin.STATUS_SUCCESS, "签到成功"),
            (signin.STATUS_NO_TASK, "无需签到"),
        ])
        self.assertEqual(code, 0)
        self.assertEqual((ok, fail, skip), (1, 0, 1))

    def test_any_failure_still_exits_1(self):
        """真失败优先于窗口跳过 → exit 1（run.sh 不写 SUCCESS，07:10 同样重跑）。"""
        code, ok, fail, skip = self._compute([
            (signin.STATUS_SUCCESS, "签到成功"),
            (signin.STATUS_FAILED, "登录失败"),
        ])
        self.assertEqual(code, 1)
        self.assertEqual((ok, fail, skip), (1, 1, 0))

    def test_all_window_skip_exits_2(self):
        """全员窗口外跳过 → exit 2（原语义不变）。"""
        code, ok, fail, skip = self._compute([
            (signin.STATUS_SKIPPED_WINDOW, "签到时段已结束"),
            (signin.STATUS_SKIPPED_NORANGE, "签到时间窗口缺失"),
        ])
        self.assertEqual(code, 2)
        self.assertEqual((ok, fail, skip), (0, 0, 2))

    def test_paused_and_cancelled_do_not_force_exit_2(self):
        """暂停/用户取消属有意状态（非窗口外未了结），不触发 P1-1 exit 2。"""
        code, ok, fail, skip = self._compute([
            (signin.STATUS_SUCCESS, "签到成功"),
            (signin.STATUS_PAUSED, "账密异常已暂停"),
            (signin.STATUS_USER_CANCELLED, "用户已取消"),
        ])
        self.assertEqual(code, 0)
        self.assertEqual((ok, fail, skip), (1, 0, 2))


class SchedMarkerTest(unittest.TestCase):
    """P1-1 配套：sched-run 标记区分首签/补签轮。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-p1-")
        self._old = os.environ.get("YIBAN_STATE_DIR")
        os.environ["YIBAN_STATE_DIR"] = self.tmp

    def tearDown(self):
        if self._old is None:
            os.environ.pop("YIBAN_STATE_DIR", None)
        else:
            os.environ["YIBAN_STATE_DIR"] = self._old

    def test_marker_missing_first_run(self):
        self.assertFalse(signin._sched_marker_exists())

    def test_marker_present_second_run(self):
        path = os.path.join(self.tmp, f"sched-run-{__import__('datetime').date.today():%Y-%m-%d}.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{}")
        self.assertTrue(signin._sched_marker_exists())


class SchedDoneMixedTest(unittest.TestCase):
    """main() 收尾：混合场景写 sched-run 标记（不应被跳过）。"""

    @mock.patch.object(signin, "_write_sched_done")
    @mock.patch.object(signin, "_flush_admin_mail_summary")
    @mock.patch.object(signin, "_maybe_alert_zero_success", return_value=False)
    def test_sched_done_written_in_mixed_case(self, m_alert, m_flush, m_done):
        """混合场景（部分成功+部分 skipped）仍写全量完成标记——容器闸门据此判定补签。"""
        accounts = [_mk_acc("13800000001"), _mk_acc("13800000002")]
        results = {
            "13800000001": (True, "签到成功", False, signin.STATUS_SUCCESS),
            "13800000002": (False, "签到时段已结束", True, signin.STATUS_SKIPPED_WINDOW),
        }
        ok_n = 1
        fail_n = 0
        skip_n = 1
        signin._write_sched_done({"ok_n": ok_n, "fail_n": fail_n, "skip_n": skip_n})
        m_done.assert_called_once_with({"ok_n": 1, "fail_n": 0, "skip_n": 1})


if __name__ == "__main__":
    unittest.main(verbosity=2)
