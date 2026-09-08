# -*- coding: utf-8 -*-
"""签到轮守卫回归（2026-09-08）。

逐项活体复现 + 修复钉版：
1. 补签轮只重跑未了结账号（当日已 success/already 不再二次登录）
2. 一键暂停/周末签到关闭期间探针跳过（门在探针分支内部判定）
3. 死号（自取消/熔断暂停）先判后睡：不再先睡满账号间隔/时段槽位再跳过
4. SIGTERM 超时终止前冲刷已收集的管理员告警汇总（汇总不随进程死亡）
5. 疑似首签轮但已过补签触发点：部分成功+窗口外仍告警（无第三次兜底）
"""
import io
import json
import os
import shutil
import signal
import sys
import tempfile
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest import mock

import signin


def _today():
    return datetime.now().strftime("%Y-%m-%d")


class _FakeDT(datetime):
    """signin.datetime 替身：now() 返回固定时刻。"""

    _date = (2026, 9, 6)
    _hm = (7, 12)

    @classmethod
    def now(cls, tz=None):
        return cls(*cls._date, *cls._hm)


# ---------------------------------------------------------------------------
# 补签轮定向重跑
# ---------------------------------------------------------------------------
class SecondRunFilterTest(unittest.TestCase):
    """补签轮（YIBAN_SECOND_RUN=1）只重跑未了结账号；全部了结时静默结束。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-second-run-")
        self._old_env = {
            k: os.environ.get(k) for k in (
                "YIBAN_SECOND_RUN", "YIBAN_ACCOUNTS_JSON", "YIBAN_DB_FILE",
                "YIBAN_STATE_DIR", "YIBAN_LOG_FILE", "YIBAN_GLOBAL_PAUSE",
            )
        }
        self._old_argv = sys.argv[:]
        os.environ["YIBAN_DB_FILE"] = os.path.join(self.tmp, "yiban.db")
        os.environ["YIBAN_STATE_DIR"] = self.tmp
        os.environ["YIBAN_LOG_FILE"] = os.path.join(self.tmp, "sign.log")
        os.environ.pop("YIBAN_GLOBAL_PAUSE", None)

    def tearDown(self):
        for k, v in self._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        sys.argv = self._old_argv
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_state(self, statuses):
        path = os.path.join(self.tmp, f"sign-state-{_today()}.json")
        with io.open(path, "w", encoding="utf-8") as f:
            json.dump(
                {p: {"status": s, "message": "", "time": "07:00:00", "task": "default"}
                 for p, s in statuses.items()}, f)

    def _run_main(self, argv=None, accounts=None):
        """执行 signin.main()，返回 (退出码, run_queue_retry 收到的账号列表)。"""
        seen = []
        sys.argv = ["signin.py"] + (argv or [])
        with mock.patch.object(signin, "load_accounts", return_value=accounts), \
             mock.patch.object(signin, "build_schedule", return_value={}), \
             mock.patch.object(signin, "run_queue_retry",
                               side_effect=lambda accs, *a, **kw: seen.append(list(accs)) or {}), \
             mock.patch.object(signin, "_save_cred_state"):
            try:
                signin.main()
                code = 0
            except SystemExit as e:
                code = e.code
        return code, (seen[0] if seen else None)

    def _accounts(self):
        return [
            mock.Mock(phone="13800000001", user_paused=False),
            mock.Mock(phone="13800000002", user_paused=False),
        ]

    def test_second_run_reruns_only_undone_accounts(self):
        """补签轮：当日已 success 的账号被剔除，仅剩 failed 账号进入重跑队列。"""
        self._write_state({"13800000001": "success", "13800000002": "failed"})
        os.environ["YIBAN_SECOND_RUN"] = "1"
        _, rerun = self._run_main(accounts=self._accounts())
        self.assertIsNotNone(rerun)
        self.assertEqual(
            [a.phone for a in rerun], ["13800000002"],
            "补签轮不得重跑当日已 success 的账号（完整登录=风控暴露）",
        )

    def test_second_run_all_done_exits_silently(self):
        """补签轮：全员已了结 → 不进入签到队列、退出码 0（静默结束）。"""
        self._write_state({"13800000001": "success", "13800000002": "already"})
        os.environ["YIBAN_SECOND_RUN"] = "1"
        code, rerun = self._run_main(accounts=self._accounts())
        self.assertEqual(code, 0)
        self.assertIsNone(rerun, "全员已了结时不应再触发任何账号")

    def test_first_run_keeps_all_accounts(self):
        """首签轮（无补签信号）：不做过滤，两个账号都进队列。"""
        self._write_state({"13800000001": "success", "13800000002": "failed"})
        os.environ.pop("YIBAN_SECOND_RUN", None)
        _, rerun = self._run_main(accounts=self._accounts())
        self.assertEqual([a.phone for a in rerun],
                         ["13800000001", "13800000002"])

    def test_second_run_missing_state_keeps_all_accounts(self):
        """状态文件缺失（如目录被清）：宁多勿漏，全量重跑。"""
        os.environ["YIBAN_SECOND_RUN"] = "1"
        _, rerun = self._run_main(accounts=self._accounts())
        self.assertEqual(len(rerun), 2)


# ---------------------------------------------------------------------------
# 探针纳入暂停/周末门
# ---------------------------------------------------------------------------
class ProbeGateTest(unittest.TestCase):
    """一键暂停 / 周末签到关闭期间，探针分支不得发起完整登录。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-probe-gate-")
        self._old_env = {
            k: os.environ.get(k) for k in (
                "YIBAN_GLOBAL_PAUSE", "YIBAN_ACCOUNTS_JSON", "YIBAN_DB_FILE",
                "YIBAN_STATE_DIR", "YIBAN_LOG_FILE",
            )
        }
        self._old_argv = sys.argv[:]
        os.environ["YIBAN_DB_FILE"] = os.path.join(self.tmp, "yiban.db")
        os.environ["YIBAN_STATE_DIR"] = self.tmp
        os.environ["YIBAN_LOG_FILE"] = os.path.join(self.tmp, "sign.log")

    def tearDown(self):
        for k, v in self._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        sys.argv = self._old_argv
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run_probe_main(self):
        sys.argv = ["signin.py", "--probe"]
        with mock.patch.object(signin, "load_accounts", return_value=[
                mock.Mock(phone="13800000000", user_paused=False)]), \
             mock.patch.object(signin, "run_probe") as m_probe:
            try:
                signin.main()
                code = 0
            except SystemExit as e:
                code = e.code
        return code, m_probe

    def test_probe_skipped_when_global_paused(self):
        """一键暂停开启：探针不发起 run_probe（完整登录=风控暴露），静默退出。"""
        os.environ["YIBAN_GLOBAL_PAUSE"] = "1"
        code, m_probe = self._run_probe_main()
        self.assertEqual(code, 0)
        m_probe.assert_not_called()

    def test_probe_skipped_saturday_when_saturday_sign_off(self):
        """周六签到关闭 + 周六：探针同样跳过（与真实签到同一组门）。"""
        os.environ.pop("YIBAN_GLOBAL_PAUSE", None)
        fake = type("_SatDT", (_FakeDT,), {"_date": (2026, 9, 5), "_hm": (10, 0)})
        with mock.patch.object(signin, "datetime", fake), \
             mock.patch.object(signin, "SATURDAY_SIGN", False), \
             mock.patch.object(signin, "SUNDAY_SIGN", False):
            code, m_probe = self._run_probe_main()
        self.assertEqual(code, 0)
        m_probe.assert_not_called()


# ---------------------------------------------------------------------------
# 死号先判后睡
# ---------------------------------------------------------------------------
class DeadAccountSkipTest(unittest.TestCase):
    """将跳过的账号（自取消/熔断暂停）不得先睡满间隔再判跳过。"""

    def setUp(self):
        # 窗口拉满全天；请求间隔下限压到 1s，隔离干扰
        env = {
            "YIBAN_SIGN_START": "00:01", "YIBAN_SIGN_END": "23:59",
            "YIBAN_MIN_EXEC_GAP": "1", "YIBAN_EXEC_GAP_MIN": "0",
        }
        p = mock.patch.dict(os.environ, env)
        p.start()
        self.addCleanup(p.stop)

    def _run(self, accounts, schedule=None, cred_state=None, gap_max=0):
        sleeps = []
        with mock.patch.object(signin.time, "sleep", side_effect=sleeps.append), \
             mock.patch.object(signin, "attempt_signin",
                               return_value=(True, "已签到", False, "already")):
            results = signin.run_queue_retry(
                accounts, "", 0, gap_max, schedule=schedule, cred_state=cred_state)
        return results, sleeps

    def test_schedule_branch_dead_account_skips_without_slot_sleep(self):
        """调度 v2 分支：熔断暂停号的槽位在 +180s，活号立即执行——
        修复前主循环会先睡到死号槽位才发现可跳过（活号被挤出窗口）。"""
        dead = SimpleNamespace(phone="13800000001", user_paused=False, owner="")
        live = SimpleNamespace(phone="13800000002", user_paused=False, owner="")
        now = datetime.now()
        schedule = {
            "13800000001": datetime.fromtimestamp(now.timestamp() + 180),
            "13800000002": datetime.fromtimestamp(now.timestamp() - 5),
        }
        cred_state = {"13800000001": {"paused_since": "2026-09-01 00:00:00",
                                      "probe_date": "2099-01-01"}}
        results, sleeps = self._run([live, dead], schedule=schedule,
                                    cred_state=cred_state)
        self.assertEqual(results["13800000001"][3], signin.STATUS_PAUSED)
        self.assertTrue(
            all(s < 60 for s in sleeps),
            f"死号不得睡满时段槽位（记录到的 sleep: {sleeps}）",
        )
        self.assertEqual(results["13800000002"][3], "already", "活号照常执行")

    def test_queue_branch_dead_account_skips_without_gap_sleep(self):
        """队列分支：[活号, 熔断暂停号]，死号不得先睡满账号间隔（gap_max=10）。"""
        dead = SimpleNamespace(phone="13800000001", user_paused=False, owner="")
        live = SimpleNamespace(phone="13800000002", user_paused=False, owner="")
        cred_state = {"13800000001": {"paused_since": "2026-09-01 00:00:00",
                                      "probe_date": "2099-01-01"}}
        results, sleeps = self._run([live, dead], cred_state=cred_state, gap_max=10)
        self.assertEqual(results["13800000001"][3], signin.STATUS_PAUSED)
        self.assertTrue(
            all(s < 5 for s in sleeps),
            f"死号不得睡满账号间隔（记录到的 sleep: {sleeps}）",
        )

    def test_queue_branch_user_cancelled_skips_without_gap_sleep(self):
        """队列分支：用户自取消账号同样先判后睡（不得消耗 10s 间隔）。"""
        dead = SimpleNamespace(phone="13800000001", user_paused=True, owner="")
        live = SimpleNamespace(phone="13800000002", user_paused=False, owner="")
        results, sleeps = self._run([live, dead], gap_max=10)
        self.assertEqual(results["13800000001"][3], signin.STATUS_USER_CANCELLED)
        self.assertTrue(all(s < 5 for s in sleeps), f"sleeps={sleeps}")


# ---------------------------------------------------------------------------
# SIGTERM 超时终止前冲刷告警汇总
# ---------------------------------------------------------------------------
class SigtermFlushTest(unittest.TestCase):
    """签到轮被超时击杀（SIGTERM）时，已收集的管理员告警汇总先发出再退出。"""

    def setUp(self):
        self._summary_backup = list(signin._mail_summary)
        signin._mail_summary.clear()

    def tearDown(self):
        signin._mail_summary.clear()
        signin._mail_summary.extend(self._summary_backup)

    def test_sigterm_flushes_collected_admin_mail(self):
        signin._mail_summary.append(("易班签到失败", "账号: 138****0000\n原因: 登录失败"))
        with mock.patch.object(signin.mailer, "send_admin_alert",
                               return_value=True) as m_send, \
             mock.patch.object(signin.db, "admin_mail_recipients",
                               return_value=["a@b.c"]), \
             self.assertRaises(SystemExit):
            signin._flush_mail_on_sigterm(15, None)
        self.assertEqual(m_send.call_count, 1, "终止前必须把已收集的告警发出")
        args, kwargs = m_send.call_args
        body = kwargs.get("body") or (args[1] if len(args) > 1 else "")
        self.assertIn("超时", body, "汇总需标明本轮被超时终止")
        self.assertEqual(signin._mail_summary, [], "发送后清空，正常收尾不重复")

    def test_sigterm_noop_without_pending_mail(self):
        """无待发告警：不产生任何发送（不重复告警）。"""
        with mock.patch.object(signin.mailer, "send_admin_alert") as m_send, \
             self.assertRaises(SystemExit):
            signin._flush_mail_on_sigterm(15, None)
        m_send.assert_not_called()

    def test_main_registers_sigterm_handler(self):
        """main() 注册 SIGTERM 处理器（run.sh timeout / 手动 terminate 均经此路径）。"""
        previous = signal.getsignal(signal.SIGTERM)
        self.addCleanup(signal.signal, signal.SIGTERM, previous)
        tmp = tempfile.mkdtemp(prefix="yiban-sigterm-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        old_env = {k: os.environ.get(k) for k in (
            "YIBAN_GLOBAL_PAUSE", "YIBAN_DB_FILE", "YIBAN_STATE_DIR",
            "YIBAN_LOG_FILE")}

        def _restore_env():
            for k, v in old_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.addCleanup(_restore_env)
        old_argv = sys.argv[:]
        self.addCleanup(setattr, sys, "argv", old_argv)
        os.environ["YIBAN_GLOBAL_PAUSE"] = "1"
        os.environ["YIBAN_DB_FILE"] = os.path.join(tmp, "yiban.db")
        os.environ["YIBAN_STATE_DIR"] = tmp
        os.environ["YIBAN_LOG_FILE"] = os.path.join(tmp, "sign.log")
        sys.argv = ["signin.py"]
        with self.assertRaises(SystemExit):
            signin.main()
        self.assertIs(signal.getsignal(signal.SIGTERM), signin._flush_mail_on_sigterm)


# ---------------------------------------------------------------------------
# 疑似首签轮但已过补签触发点仍告警
# ---------------------------------------------------------------------------
class LateFirstRunAlertTest(unittest.TestCase):
    """06:31 关机、07:10 起的轮次挂首签身份却是当天最后一轮：
    部分成功 + 窗口外不得被首签标签抑制。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-late-first-")
        p = mock.patch.dict(os.environ, {"YIBAN_STATE_DIR": self.tmp})
        p.start()
        self.addCleanup(p.stop)
        os.environ.pop("YIBAN_SECOND_RUN", None)
        self.addCleanup(os.environ.pop, "YIBAN_SECOND_RUN", None)
        self.accounts = [SimpleNamespace(phone="13800000001"),
                         SimpleNamespace(phone="13800000002")]
        self.results = {
            "13800000001": (True, "已签到", False, "success"),
            "13800000002": (False, "签到时段已结束", True, "skipped_window"),
        }

    def _alert(self, hm):
        fake = type("_DT", (_FakeDT,), {"_hm": hm})
        with mock.patch.object(signin, "datetime", fake):
            return signin._maybe_alert_zero_success(
                self.accounts, self.results, ok_n=1, is_second_run=False)

    def test_after_second_trigger_time_alerts(self):
        """07:10 之后仍挂着首签身份：不再有第三轮兜底 → 必须告警。"""
        self.assertTrue(self._alert((7, 12)), "部分成功+窗口外在当天最后一轮必须告警")

    def test_before_second_trigger_time_stays_silent(self):
        """07:10 之前的首签轮：补签会重跑，维持既有不打扰语义。"""
        self.assertFalse(self._alert((6, 50)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
