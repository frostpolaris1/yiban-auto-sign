# -*- coding: utf-8 -*-
"""签到轮守卫回归（2026-09-08）。

逐项活体复现 + 修复钉版：
1. 补签轮只重跑未了结账号（当日已 success/already 不再二次登录）
2. 一键暂停/周末签到关闭期间探针跳过（门在探针分支内部判定）
3. 死号（自取消/熔断暂停）先判后睡：不再先睡满账号间隔/时段槽位再跳过
4. SIGTERM 超时终止前冲刷已收集的管理员告警汇总（汇总不随进程死亡）
5. 疑似首签轮但已过补签触发点：部分成功+窗口外仍告警（无第三次兜底）
"""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import signal
import sys
import tempfile
import time
import unittest
import unittest.mock
from datetime import datetime
from types import SimpleNamespace
from unittest import mock

import signin

# 邮件正文入参已放宽为 layout.Mail | str，断言前统一渲染成文本
from _mail_body import render_body


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
    """补签轮（YIBAN_SECOND_RUN=1）只重跑未了结账号；全部了结时静默结束。

    本组用例钉的是补签过滤逻辑，不是周末门。signin.main() 会先过周日/周六门
    （signin.py:3374/3380，周末且开关默认关闭即 exit 2），把"今天周几"这个外部
    输入留成真实值，会让整组用例在周日/周六运行时必然变红。因此在测试内把
    signin 读到的时刻固定为一个工作日（周二），状态文件名同用该固定日期，
    使用例与运行当天的星期彻底解耦。
    """

    # 固定执行时刻：2026-09-08 为周二，避开周六/周日门；与 state 文件名共用
    _FAKE_DATE = "2026-09-08"
    _FAKE_DT = (2026, 9, 8, 7, 12)

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-second-run-")
        fixed = type("_FixedDT", (_FakeDT,),
                     {"_date": self._FAKE_DT[:3], "_hm": self._FAKE_DT[3:]})
        p = mock.patch.object(signin.clock, "now", fixed.now)
        p.start()
        self.addCleanup(p.stop)
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
        path = os.path.join(self.tmp, f"sign-state-{self._FAKE_DATE}.json")
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
        with mock.patch.object(signin.clock, "now", fake.now), \
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
        body = render_body(kwargs.get("body") or (args[1] if len(args) > 1 else ""))
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
        """注入固定时刻跑判定；补签时刻显式钉住 07:10（不随缺省值漂移）。"""
        fake = type("_DT", (_FakeDT,), {"_hm": hm})
        with mock.patch.dict(os.environ, {"YIBAN_SECOND_RUN_TIME": "07:10"}), \
             mock.patch.object(signin.clock, "now", fake.now):
            return signin._maybe_alert_zero_success(
                self.accounts, self.results, ok_n=1, is_second_run=False)

    def test_after_second_trigger_time_alerts(self):
        """07:10 之后仍挂着首签身份：不再有第三轮兜底 → 必须告警。"""
        self.assertTrue(self._alert((7, 12)), "部分成功+窗口外在当天最后一轮必须告警")

    def test_before_second_trigger_time_stays_silent(self):
        """07:10 之前的首签轮：补签会重跑，维持既有不打扰语义。"""
        self.assertFalse(self._alert((6, 50)))


# ---------------------------------------------------------------------------
# 窗口外起跑的全量轮：跳过 ≠ 失败
# ---------------------------------------------------------------------------
class WindowClosedRoundTest(unittest.TestCase):
    """窗口外起跑的全量轮：被跳过的账号必须真的进 `results`，不能算成"失败"。

    **实测缺陷**（2026-09-17 在测试机上复现，base 提交 c696aaf 同样如此）：排计划阶段
    会给每个账号写 `pending`（"计划 HH:MM"）到当日状态文件，而 `_mark_window_skip` 把
    "状态文件里有记录"一律当作"已有结论"跳过——于是窗口外起跑时**一个账号都进不了
    `results`**，汇总按"未执行"把它们算成失败（❌ N 失败 / 退出码 1 / 发失败邮件），
    真相却是一个请求都没发；`run.sh` 也因此写不出 SKIPPED，补签链跟着断掉。
    """

    PHONE = "13800000009"

    class _WindowClosedDT(_FakeDT):
        """固定为 2026-09-17 08:30（周四）：配合"早已结束"的窗口，任何时刻跑都成立。"""

        _date = (2026, 9, 17)
        _hm = (8, 30)

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-window-skip-")
        p = mock.patch.object(signin.clock, "now", self._WindowClosedDT.now)
        p.start()
        self.addCleanup(p.stop)
        self._old_env = {
            k: os.environ.get(k) for k in (
                "YIBAN_STATE_DIR", "YIBAN_LOG_FILE", "YIBAN_DB_FILE",
                "YIBAN_SIGN_START", "YIBAN_SIGN_END")
        }
        os.environ.update({
            "YIBAN_STATE_DIR": self.tmp,
            "YIBAN_LOG_FILE": os.path.join(self.tmp, "sign.log"),
            "YIBAN_DB_FILE": os.path.join(self.tmp, "yiban.db"),
            # 窗口 00:10~00:20：配合上面的固定时刻，永远落在窗口外
            "YIBAN_SIGN_START": "00:10",
            "YIBAN_SIGN_END": "00:20",
        })

    def tearDown(self):
        for k, v in self._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, recorded=None):
        """跑一轮；时间表非空 ⇒ 走"计划"分支（与真实全量轮同一条路径）。"""
        if recorded:
            signin._write_sign_state(self.PHONE, recorded, "测试留痕")
        acc = SimpleNamespace(phone=self.PHONE, user_paused=False)
        return signin.run_queue_retry(
            [acc], "", 0, 0,
            schedule={self.PHONE: datetime(2026, 9, 17, 0, 11)}, cred_state={})

    def test_pending_account_is_marked_window_skip(self):
        res = self._run(recorded=signin.STATUS_PENDING)
        got = res.get(self.PHONE)
        self.assertIsNotNone(got, "排计划留下的 pending 不该让这个账号漏掉窗口外标记")
        self.assertEqual(got[3], signin.STATUS_SKIPPED_WINDOW)
        self.assertTrue(got[2], "窗口外跳过必须 skip=True（退出码 2，而不是算失败）")

    def test_real_conclusion_is_not_overwritten(self):
        """已有真实结论（failed）的账号仍不得被改写成"窗口外"，且失败保持可见。"""
        res = self._run(recorded=signin.STATUS_FAILED)
        got = res.get(self.PHONE)
        self.assertIsNotNone(got, "已有结论的账号也必须进 results，否则汇总按未执行失败计")
        self.assertEqual(got[3], signin.STATUS_FAILED, "真实失败原因必须透传")
        with io.open(os.path.join(self.tmp, "sign-state-2026-09-17.json"),
                     encoding="utf-8") as f:
            self.assertEqual(json.load(f)[self.PHONE]["status"], signin.STATUS_FAILED,
                             "真实失败原因不该被窗口外覆盖")


BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


TEST_KEY = "a" * 64


ADMIN_PASS = "TestPass1234!"


class _FakeProc:
    """fake subprocess.Popen 返回值：wait 立即返回，terminate/kill 空操作。"""

    def __init__(self):
        self.returncode = 0

    def wait(self, timeout=None):
        return 0

    def terminate(self):
        pass

    def kill(self):
        pass


class BatchSignCooldownTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-batch-cooldown-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(
                f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
                f"YIBAN_ADMIN_USER=admin\nYIBAN_ADMIN_PASSWORD={ADMIN_PASS}\n"
            )
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.accounts_file = os.path.join(cls.tmp, "accounts.json")
        cls.users_file = os.path.join(cls.tmp, "users.json")
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_ACCOUNTS_FILE"] = cls.accounts_file
        os.environ["YIBAN_USERS_FILE"] = cls.users_file
        os.environ["YIBAN_DB_FILE"] = cls.db_file
        os.environ["YIBAN_STATE_DIR"] = cls.tmp
        global db
        import db
        spec = importlib.util.spec_from_file_location(
            "webapp", os.path.join(BASE, "web", "app.py"))
        cls.webapp = importlib.util.module_from_spec(spec)
        sys.modules["webapp"] = cls.webapp
        with contextlib.suppress(Exception):
            spec.loader.exec_module(cls.webapp)
        # 主审修复：Popen patch 提升到类级——整个测试类（含后台队列线程的
        # 任意调度时刻）都处于 fake Popen 之下。此前测试级 setUp/tearDown patch 在
        # tearDown stop 时若后台线程尚未执行 _spawn_signin_many（全量负载下线程调度
        # 延迟），会真实 spawn signin 子进程并持有 db 连接，下一个测试 setUp 删
        # yiban.db 报 PermissionError（全量 test_single_signin 失败根因）。
        cls._popen_patch = unittest.mock.patch.object(
            cls.webapp.subprocess, "Popen", return_value=_FakeProc())
        cls._popen_patch.start()

    @classmethod
    def tearDownClass(cls):
        cls._popen_patch.stop()
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k in ("YIBAN_ACCOUNTS_KEY", "YIBAN_ENV_FILE", "YIBAN_ACCOUNTS_FILE",
                  "YIBAN_USERS_FILE", "YIBAN_DB_FILE", "YIBAN_STATE_DIR"):
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
        with open(self.accounts_file, "w", encoding="utf-8") as f:
            json.dump([], f)
        db.init_db(self.db_file, migrate_from=self.accounts_file,
                   env_file=self.env_file)
        # 清掉 .env 中可能残留的冷却与速率上限键（回到默认值）
        self._set_cooldown(None)
        self._set_rate_limit(None)
        # 插入 3 个 active 账号（owner=admin 直属，不占注册用户配额）
        for i, phone in enumerate(("13800000001", "13800000002", "13800000003")):
            db.add_account({
                "phone": phone, "password": f"accpass{i}",
                "owner": "admin", "name": f"t{i}", "status": "active",
                "phone_code": "",
            })

    def tearDown(self):
        # db 连接由 setUp 与 tearDownClass 管理；这里只释放单测实例资源
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None

    def _set_cooldown(self, value):
        """写/删 .env 的冷却键（None=删键回默认）。"""
        with open(self.env_file, encoding="utf-8-sig") as f:
            lines = f.read().splitlines()
        lines = [ln for ln in lines
                 if not ln.strip().startswith("YIBAN_BATCH_SIGN_COOLDOWN_SEC=")]
        if value is not None:
            lines.append(f"YIBAN_BATCH_SIGN_COOLDOWN_SEC={value}".rstrip())
        with open(self.env_file, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

    def _set_rate_limit(self, limit, window=None):
        """写/删 .env 的全局速率上限键（`limit=None` = 删键回默认，`limit=0` = 关闭）。"""
        keys = ("YIBAN_SIGNIN_RATE_MAX", "YIBAN_SIGNIN_RATE_WINDOW_SEC")
        with open(self.env_file, encoding="utf-8-sig") as f:
            lines = f.read().splitlines()
        lines = [ln for ln in lines
                 if not any(ln.strip().startswith(k + "=") for k in keys)]
        if limit is not None:
            lines.append(f"YIBAN_SIGNIN_RATE_MAX={limit}")
        if window is not None:
            lines.append(f"YIBAN_SIGNIN_RATE_WINDOW_SEC={window}")
        with open(self.env_file, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

    def _trigger_single(self, c, csrf, phone):
        return c.post("/api/signin", json={"phone": phone},
                      headers={"X-CSRF-Token": csrf})

    def _login(self, c):
        r = c.post("/api/login", json={"username": "admin", "password": ADMIN_PASS})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return c.get("/api/me").get_json()["csrf_token"]

    def _trigger_batch(self, c, csrf):
        """触发批量签到（Popen 已在 setUp 级 patch，防真实 spawn 且覆盖后台线程）。"""
        return c.post("/api/signin/batch", json={
            "ids": [0, 1, 2],
            "phones": ["13800000001", "13800000002", "13800000003"],
        }, headers={"X-CSRF-Token": csrf})

    def _trigger_until_batch_done(self, c, csrf, deadline_s=15):
        """触发批量签到，若后台队列尚未完成（429"正在执行"）则轮询重试。

        全量测试负载下后台线程可能晚于固定 sleep 完成——固定 sleep 会让第二次
        触发撞上"队列正在执行"而非"冷却中"，断言错位（主审修复：时间敏感
        测试改为轮询等待）。队列完成后返回本次触发响应。
        """
        deadline = time.time() + deadline_s
        while True:
            r = self._trigger_batch(c, csrf)
            if r.status_code != 429:
                return r
            err = r.get_json().get("error", "")
            if "正在执行" in err and time.time() < deadline:
                time.sleep(0.5)
                continue
            return r

    # ---- 冷却 ----
    def test_cooldown_blocks_immediate_retry(self):
        """默认冷却（60s）：队列完成后立即再触发 → 429。"""
        c = self.webapp.create_app().test_client()
        csrf = self._login(c)
        r1 = self._trigger_batch(c, csrf)
        self.assertEqual(r1.status_code, 200, r1.get_data(as_text=True))
        # 轮询等待后台队列线程完成（更新冷却时间戳）后再次触发 → 冷却 429
        r2 = self._trigger_until_batch_done(c, csrf)
        self.assertEqual(r2.status_code, 429, r2.get_data(as_text=True))
        self.assertIn("冷却中", r2.get_json()["error"])

    def test_cooldown_zero_disables(self):
        """YIBAN_BATCH_SIGN_COOLDOWN_SEC=0 → 关闭冷却，可立即再次触发。"""
        self._set_cooldown(0)
        c = self.webapp.create_app().test_client()
        csrf = self._login(c)
        r1 = self._trigger_batch(c, csrf)
        self.assertEqual(r1.status_code, 200, r1.get_data(as_text=True))
        # 冷却关闭：队列完成后可再次触发（200）
        r2 = self._trigger_until_batch_done(c, csrf)
        self.assertEqual(r2.status_code, 200, r2.get_data(as_text=True))

    def test_cooldown_short_window(self):
        """自定义短冷却（如 2s）：窗口内 429，窗口过后可再次触发。"""
        self._set_cooldown(2)
        c = self.webapp.create_app().test_client()
        csrf = self._login(c)
        self.assertEqual(self._trigger_batch(c, csrf).status_code, 200)
        # 队列完成后（轮询）再触发 → 2s 冷却窗口内 429
        r = self._trigger_until_batch_done(c, csrf)
        self.assertEqual(r.status_code, 429, "2s 冷却窗口内应拒绝")
        time.sleep(2.5)  # 等窗口过期
        r2 = self._trigger_batch(c, csrf)
        self.assertEqual(r2.status_code, 200, "冷却窗口过后应恢复")

    def test_single_signin_shares_global_cooldown(self):
        """行为变化：单账号手动签到与批量共用同一全局冷却——
        批量 spawn 成功后窗口内再触发单号 → 429 冷却提示（a8e9c43 威胁模型：
        被盗会话循环触发单号真实登录同样打爆易班风控）。"""
        c = self.webapp.create_app().test_client()
        csrf = self._login(c)
        self.assertEqual(self._trigger_batch(c, csrf).status_code, 200)
        # 等批量队列完成（冷却基准已在 spawn 成功时刻挂上），再触发单账号签到
        self._trigger_until_batch_done(c, csrf)
        r = c.post("/api/signin", json={"phone": "13800000001"},
                   headers={"X-CSRF-Token": csrf})
        self.assertEqual(r.status_code, 429, r.get_data(as_text=True))
        self.assertIn("冷却中", r.get_json()["error"],
                      "单账号签到应被全局冷却拦截并给出冷却提示")

    def test_single_signin_blocked_after_single_trigger(self):
        """验收：单条手动签到成功后立刻再触发任意号 → 429 冷却提示；
        另一账号同样被拦（共用同一计数，非 per-phone）。"""
        c = self.webapp.create_app().test_client()
        csrf = self._login(c)
        r1 = c.post("/api/signin", json={"phone": "13800000001"},
                    headers={"X-CSRF-Token": csrf})
        self.assertEqual(r1.status_code, 200, r1.get_data(as_text=True))
        r2 = c.post("/api/signin", json={"phone": "13800000002"},
                    headers={"X-CSRF-Token": csrf})
        self.assertEqual(r2.status_code, 429, r2.get_data(as_text=True))
        self.assertIn("冷却中", r2.get_json()["error"])

    # ---- 全局速率上限（冷却之外的积分节流）----
    def test_rate_limit_blocks_after_max_triggers(self):
        """冷却关闭 + 上限 2 次：第 3 次触发 → 429，文案与"冷却中"区分开。"""
        self._set_cooldown(0)
        self._set_rate_limit(2, window=600)
        c = self.webapp.create_app().test_client()
        csrf = self._login(c)
        for phone in ("13800000001", "13800000002"):
            r = self._trigger_single(c, csrf, phone)
            self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        r3 = self._trigger_single(c, csrf, "13800000003")
        self.assertEqual(r3.status_code, 429, r3.get_data(as_text=True))
        err = r3.get_json()["error"]
        self.assertIn("过于频繁", err, f"应命中次数上限：{err}")
        self.assertNotIn("冷却中", err, "两道闸的文案必须能分辨")

    def test_rate_limit_applies_to_batch_endpoint_too(self):
        """上限 1 次：单条已用掉额度 → 批量触发同样 429（单条/批量共用同一计数）。"""
        self._set_cooldown(0)
        self._set_rate_limit(1, window=600)
        c = self.webapp.create_app().test_client()
        csrf = self._login(c)
        self.assertEqual(self._trigger_single(c, csrf, "13800000001").status_code, 200)
        r = self._trigger_batch(c, csrf)
        self.assertEqual(r.status_code, 429, r.get_data(as_text=True))
        self.assertIn("过于频繁", r.get_json()["error"])

    def test_rate_limit_zero_disables(self):
        """上限 0 = 关闭：冷却也关闭时连发不受拦。"""
        self._set_cooldown(0)
        self._set_rate_limit(0)
        c = self.webapp.create_app().test_client()
        csrf = self._login(c)
        for phone in ("13800000001", "13800000002", "13800000003"):
            r = self._trigger_single(c, csrf, phone)
            self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

    def test_cooldown_checked_before_rate_limit(self):
        """顺序契约：冷却先判——上限 1 且冷却默认时，第 2 次报"冷却中"而非"过于频繁"。

        被冷却拒掉的请求没有发起真实登录，不该消耗次数额度（否则合法运维连点几次
        就把整个窗口的次数预算花光）。
        """
        self._set_rate_limit(1, window=600)
        c = self.webapp.create_app().test_client()
        csrf = self._login(c)
        self.assertEqual(self._trigger_single(c, csrf, "13800000001").status_code, 200)
        r2 = self._trigger_single(c, csrf, "13800000002")
        self.assertEqual(r2.status_code, 429, r2.get_data(as_text=True))
        self.assertIn("冷却中", r2.get_json()["error"],
                      "冷却未过期时不该由次数上限抢先拒绝")


if __name__ == "__main__":
    unittest.main(verbosity=2)
