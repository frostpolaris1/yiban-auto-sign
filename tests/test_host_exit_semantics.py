# -*- coding: utf-8 -*-
"""回归测试：宿主 run.sh 补签闸门不被「部分成功」吞掉。

覆盖：
- 退出码语义：有 skipped_window/skipped_norange 未了结账号时（即使有成功）→ exit 2
  （run.sh 写 SKIPPED → 07:10 补签重跑）；无窗口外跳过 → 维持 0/1/2 原语义；
- 告警时机（2026-09-16 改判据）：**窗口还开着 + 后面还有人接着跑**（补签轮未到或兜底执行体
  在跑）才抑制；窗口已关、或没人接着跑时必须告警。

用法（项目根目录）：
    py -m pytest tests/test_batch15_exit_semantics_0831.py -v
"""
import os
import shutil
import tempfile
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest import mock

import signin


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
        ok_n = 1
        fail_n = 0
        skip_n = 1
        signin._write_sched_done({"ok_n": ok_n, "fail_n": fail_n, "skip_n": skip_n})
        m_done.assert_called_once_with({"ok_n": 1, "fail_n": 0, "skip_n": 1})


class AlertSuppressionFactsTest(unittest.TestCase):
    """告警抑制改为**事实判定**（`63` §2）：窗口还开着 + 后面还有人接着跑 才抑制。

    以前的判据是"猜这是第几轮"（is_second_run + 当前时刻比补签点早），多执行体下不再
    可靠：兜底执行体会一直重试到窗口关闭，此时挂着"补签轮"身份也没必要告警；反之
    没兜底、补签点也过了，就必须告警（当天不会再有触发了）。
    """

    @staticmethod
    def _accounts_and_results():
        accs = [SimpleNamespace(phone="13900000001", owner="", user_paused=False),
                SimpleNamespace(phone="13900000002", owner="", user_paused=False)]
        results = {
            "13900000001": (True, "签到成功", False, signin.STATUS_SUCCESS),
            "13900000002": (False, "未在签到时间内", True, signin.STATUS_SKIPPED_WINDOW),
        }
        return accs, results

    def _call(self, *, now, retry_hm, alive, window_closed):
        accs, results = self._accounts_and_results()
        with mock.patch.object(signin.clock, "now", return_value=now),                 mock.patch.object(signin.window, "retry_hm", return_value=retry_hm),                 mock.patch.object(signin, "fallback_alive", return_value=(alive, 1.0)),                 mock.patch.object(signin, "_window_closed", return_value=window_closed),                 mock.patch.object(signin, "_collect_admin_mail") as mail:
            got = signin._maybe_alert_zero_success(accs, results, ok_n=1,
                                                  is_second_run=False)
        return got, mail.call_count

    def test_suppressed_when_window_open_and_later_round_exists(self):
        """首签轮（补签点还没到）+ 窗口开着 → 不打扰（补签轮会重跑）。"""
        got, mails = self._call(now=datetime(2026, 9, 16, 6, 35), retry_hm=(7, 12),
                                alive=False, window_closed=False)
        self.assertFalse(got)
        self.assertEqual(mails, 0)

    def test_alerts_when_no_later_round_and_window_closed(self):
        """补签点已过、没有兜底、窗口也关了 → 必须告警（当天不再有触发）。"""
        got, mails = self._call(now=datetime(2026, 9, 16, 7, 50), retry_hm=(7, 12),
                                alive=False, window_closed=True)
        self.assertTrue(got)
        self.assertEqual(mails, 1)

    def test_fallback_alive_suppresses_even_after_retry_point(self):
        """兜底执行体还在跑 → 即便补签点已过也不告警（它还在重试，会自己收敛）。"""
        got, _mails = self._call(now=datetime(2026, 9, 16, 7, 20), retry_hm=(7, 12),
                                 alive=True, window_closed=False)
        self.assertFalse(got)

    def test_alerts_when_window_closed_even_with_fallback(self):
        """窗口已关：兜底也做不了什么了 → 告警（让管理员当天知情）。"""
        got, _mails = self._call(now=datetime(2026, 9, 16, 7, 50), retry_hm=(7, 12),
                                 alive=True, window_closed=True)
        self.assertTrue(got)

    def test_zero_success_always_alerts(self):
        """零成功 + 存在窗口外跳过：任何轮次都告警（全员窗口外=当天可能无签）。"""
        accs, results = self._accounts_and_results()
        with mock.patch.object(signin.clock, "now",
                               return_value=datetime(2026, 9, 16, 6, 35)),                 mock.patch.object(signin.window, "retry_hm", return_value=(7, 12)),                 mock.patch.object(signin, "fallback_alive", return_value=(False, None)),                 mock.patch.object(signin, "_window_closed", return_value=False),                 mock.patch.object(signin, "_collect_admin_mail") as mail:
            signin._maybe_alert_zero_success(accs, results, ok_n=0)
        self.assertEqual(mail.call_count, 1)


class FallbackHeartbeatTest(unittest.TestCase):
    """兜底执行体的心跳：存在且新鲜=在跑；过期=进程被强杀（不能只看文件在不在）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-hb-")
        self._old = os.environ.get("YIBAN_STATE_DIR")
        os.environ["YIBAN_STATE_DIR"] = self.tmp

    def tearDown(self):
        if self._old is None:
            os.environ.pop("YIBAN_STATE_DIR", None)
        else:
            os.environ["YIBAN_STATE_DIR"] = self._old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_fresh_heartbeat_means_alive(self):
        signin._write_fallback_alive(datetime(2026, 9, 16, 6, 40, 0))
        alive, age = signin.fallback_alive(interval_sec=60, now=datetime(2026, 9, 16, 6, 40, 30))
        self.assertTrue(alive)
        self.assertAlmostEqual(age, 30, delta=1)

    def test_stale_heartbeat_means_dead(self):
        """kill -9 时不会执行清理：心跳过期必须判"已停"，否则永远报"在跑"。"""
        signin._write_fallback_alive(datetime(2026, 9, 16, 6, 0, 0))
        alive, _age = signin.fallback_alive(interval_sec=60, now=datetime(2026, 9, 16, 6, 40, 0))
        self.assertFalse(alive)

    def test_missing_file_means_not_alive(self):
        self.assertEqual(signin.fallback_alive(), (False, None))
        signin._write_fallback_alive()
        self.assertTrue(signin.fallback_alive()[0])
        signin._clear_fallback_alive()
        self.assertEqual(signin.fallback_alive(), (False, None))


if __name__ == "__main__":
    unittest.main(verbosity=2)
