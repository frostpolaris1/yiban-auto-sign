# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""重试重排与补签时刻：重试落点、槽位时刻与宿主脚本契约。

标签：B · 调度：领取/队列/执行体
覆盖：重试重排的间隔与顺序语义（失败不阻塞他号、落点区间与偏早段、窗口不足即弃、上界随收缩窗口、入队时补记原因与日志注入转义）、retry_hm
   的取值与容错（调用期读 env）、宿主 run.sh
   与容器的缺省同源、告警阈值随实际补签时刻、容器把实际触发点注入子进程、need_second_run
   判定矩阵、run.sh 进程内补签轮的两轮语义与逃生开关、宿主与容器判定一致。
对应实现：yiban/engine/schedule.py 与
   scripts/signin.py（_next_retry_at、run_queue_retry 的重试入队、retry_hm
   常量）、run.sh 与 docker/scheduler.py（SECOND 闸门、_UNDONE_STATUSES）。
关键断言：A 失败不得阻塞 B（旧行为 A→A(重试)→B
   会把整轮拖住），重试落点必须夹在[now+retry_min_interval, 有效窗口结束]
   且偏早段——缓冲留给下一次重试。补签时刻在长驻进程里必须按调用期读取（导入期缓存会让管理员改的值不生效）。宿主与容器两侧的补签判定必须完全一致且未了结集合是同一份引用，两侧各写一套就是漂移的开始；容器注入的实际触发点优先于
   .env 里的旧值。
依赖：假时钟 + 打桩 attempt_signin / 写盘 / 告警；run.sh 契约用例真起 bash
   子进程（无 bash 的机器会失败而非 skip，本机可跑），并读 run.sh 与
   docker/scheduler.py 原文做文本断言。不发网络请求。

功能：重试重排与补签时刻的调度回归。
归属：`yiban/engine/schedule.py` 与容器调度入口的交叉测试。
复用：`FakeNow` 假时钟、`_acc()` 账号构造助手、`BASE` 常量。
通信：以假时钟驱动调度纯函数，并读 `run.sh` / `docker/scheduler.py` 文本做契约断言；
由 pytest 收集。
"""
import io
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from datetime import datetime as _dt
from datetime import datetime as clock_cls
from types import SimpleNamespace
from unittest import mock

import scheduler
import signin

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(0, os.path.join(BASE, "scripts"))

from yiban import clock, window  # noqa: E402


class FakeNow:
    NOW = _dt(2026, 8, 27, 7, 0, 0)

    @classmethod
    def now(cls):
        return cls.NOW # 子类只改 NOW 就能换时刻：两套时点共用同一组打桩


def _acc(phone):
    return signin.Account(phone=phone, password="p")


class RetryRescheduleTest(unittest.TestCase):
    def setUp(self):
        for k in ("YIBAN_RETRY_MIN_INTERVAL", "YIBAN_SIGN_START", "YIBAN_SIGN_END",
                  "YIBAN_WINDOW_EDGE_SEC", "YIBAN_MIN_EXEC_GAP", "YIBAN_EXEC_GAP_MIN"):
            os.environ.pop(k, None)

    def test_failure_does_not_block_other_accounts(self):
        """P4：A 失败不阻塞 B——执行顺序 A→B→A(重试)；旧行为会是 A→A(重试)→B。"""
        calls = []

        def fake_attempt(acc):
            calls.append(acc.phone) # 调用顺序本身就是断言主体（A→B→A），不是「两个号都跑过」
            if acc.phone == "13800138000" and calls.count(acc.phone) == 1:
                return (False, "网络超时", False, signin.STATUS_FAILED)
            return (True, "ok", False, signin.STATUS_SUCCESS)

        sched = {
            "13800138000": _dt(2026, 8, 27, 6, 40),   # 过点（A，首次失败）
            "13800138001": _dt(2026, 8, 27, 7, 0, 0),  # 到点（B，成功）
        }
        with mock.patch.object(signin.clock, "now", FakeNow.now), \
             mock.patch.object(signin, "attempt_signin", side_effect=fake_attempt), \
             mock.patch.object(signin, "_write_sign_state"), \
             mock.patch.object(signin, "_update_cred_state"), \
             mock.patch.object(signin.time, "monotonic", return_value=100.0), \
             mock.patch.object(signin.time, "sleep"):
            signin.run_queue_retry(
                [_acc("13800138000"), _acc("13800138001")],
                "", 0, 0, schedule=sched,
            )
        self.assertEqual(calls, ["13800138000", "13800138001", "13800138000"],
                         "A 失败后 B 应立即执行（重试挂起不阻塞）")

    def test_retry_slot_within_window_and_min_interval(self):
        """P1/P2：重试落点 ∈ [now+retry_min_interval, eff_hi]，且限偏早段（≤ 60% 剩余窗口）。"""
        with mock.patch.dict(os.environ, {
            "YIBAN_RETRY_MIN_INTERVAL": "60",
            "YIBAN_SIGN_START": "06:30",
            "YIBAN_SIGN_END": "07:50",
            "YIBAN_WINDOW_EDGE_SEC": "0",
        }, clear=False):
            cfg = signin._schedule_config()
            now = FakeNow.NOW  # 07:00
            lo = now.replace(minute=1, second=0, microsecond=0)
            eff_hi = now.replace(hour=7, minute=50, second=0, microsecond=0)
            for seed in range(100):
                nxt = signin._next_retry_at(now, cfg, rng=__import__("random").Random(seed)) # 逐个换固定 seed 扫遍抖动分布：失败信息能指到具体是哪种随机序列
                self.assertIsNotNone(nxt)
                self.assertGreaterEqual(nxt, lo, f"seed {seed}: 早于下界")
                self.assertLessEqual(nxt, eff_hi, f"seed {seed}: 越过窗口末端")
                # 偏早段：nxt ≤ lo + 60% 剩余窗口
                self.assertLessEqual(
                    nxt, lo + (eff_hi - lo) * 0.6 + __import__("datetime").timedelta(seconds=1),
                    f"seed {seed}: 落点应偏早",
                )

    def test_retry_gives_up_when_window_insufficient(self):
        """P5：窗口剩余不足 retry_min_interval → 不重试，直接判失败（不硬冲）。"""
        with mock.patch.dict(os.environ, {
            "YIBAN_RETRY_MIN_INTERVAL": "60",
            "YIBAN_SIGN_START": "06:30",
            "YIBAN_SIGN_END": "07:50",
            "YIBAN_WINDOW_EDGE_SEC": "0",
        }, clear=False):

            class LateNow(FakeNow):
                NOW = _dt(2026, 8, 27, 7, 49, 50)  # 距 eff_hi=07:50 不足 60s

            sched = {"13800138000": _dt(2026, 8, 27, 7, 49, 0)}
            with mock.patch.object(signin.clock, "now", LateNow.now), \
                 mock.patch.object(signin, "attempt_signin",
                                   return_value=(False, "网络超时", False, signin.STATUS_FAILED)) as attempt, \
                 mock.patch.object(signin, "classify_failure", return_value=2), \
                 mock.patch.object(signin, "_write_sign_state"), \
                 mock.patch.object(signin, "_update_cred_state"), \
                 mock.patch.object(signin, "send_notification"):
                results = signin.run_queue_retry(
                    [_acc("13800138000")], "", 0, 0, schedule=sched,
                )
            self.assertEqual(attempt.call_count, 1, "窗口不足不应重试")
            self.assertFalse(results["13800138000"][0], "窗口不足应判失败")

    def test_retry_upper_bound_pinned_to_sign_end_minus_edge(self):
        """正常窗口（无回退）：上界逐值等于 `sign_end - edge_back`（07:50 - 60s = 07:49）。

        采样上限用"uniform 恒回上界"的替身 rng 钉死，故断言的是精确时刻而非区间：
        07:01 + 60% × (07:49 - 07:01) = 07:29:48。
        """
        class _MaxRng:
            def uniform(self, lo, hi):
                return hi

        with mock.patch.dict(os.environ, {
            "YIBAN_RETRY_MIN_INTERVAL": "60",
            "YIBAN_SIGN_START": "06:30",
            "YIBAN_SIGN_END": "07:50",
            "YIBAN_WINDOW_EDGE_FRONT_SEC": "60",
            "YIBAN_WINDOW_EDGE_BACK_SEC": "60",
        }, clear=False):
            cfg = signin._schedule_config()
            win = window.bounds(cfg)
            self.assertFalse(win.fell_back)
            self.assertEqual(win.hi_min, 7 * 60 + 49)
            self.assertEqual(win.hi_min,
                             cfg["sign_end"][0] * 60 + cfg["sign_end"][1]
                             - cfg["edge_back_sec"] / 60.0)
            nxt = signin._next_retry_at(_dt(2026, 8, 27, 7, 0, 0), cfg, rng=_MaxRng())
            self.assertEqual(nxt, _dt(2026, 8, 27, 7, 29, 48))

    def test_retry_upper_bound_follows_clamped_window(self):
        """缓冲过大被收缩：上界取收缩后的有效窗口结束（07:09），窗口还开着就不得放弃重试。

        配置 07:00~07:10 各 300s（合计 >= 窗口宽度）⇒ 缓冲收缩为各 60s、窗口保留；
        此刻（07:06）已越过"原始配置的上界"07:05，但有效窗口仍开着——按原始配置算会把
        重试判成"放不下"而直接放弃，窗口内的重试机会白白丢掉。
        """
        class _MaxRng:
            def uniform(self, lo, hi):
                return hi

        with mock.patch.dict(os.environ, {
            "YIBAN_RETRY_MIN_INTERVAL": "60",
            "YIBAN_SIGN_START": "07:00",
            "YIBAN_SIGN_END": "07:10",
            "YIBAN_WINDOW_EDGE_FRONT_SEC": "300",
            "YIBAN_WINDOW_EDGE_BACK_SEC": "300",
        }, clear=False):
            cfg = signin._schedule_config()
            win = window.bounds(cfg)
            self.assertTrue(win.edges_clamped)
            self.assertFalse(win.fell_back)
            self.assertEqual(win.hi_min, 7 * 60 + 9)
            now = _dt(2026, 8, 27, 7, 6, 0)
            self.assertFalse(win.is_closed(now), "前提：有效窗口仍开着")
            nxt = signin._next_retry_at(now, cfg, rng=_MaxRng())
            self.assertIsNotNone(nxt, "窗口还开着，重试不得判放不下")
            self.assertEqual(nxt, _dt(2026, 8, 27, 7, 8, 12))
            # 放弃语义保留，但改按有效窗口判定：下界越过 07:09 才放弃
            self.assertIsNone(signin._next_retry_at(_dt(2026, 8, 27, 7, 9, 0), cfg))

    def _run_status(self, reason, schedule):
        """统一驱动：让某账号连续失败直到进入重试入队分支，返回 (_write_sign_state 调用, logger mock)。"""
        def fake_attempt(acc):
            return (False, reason, False, signin.STATUS_FAILED)

        logmock = mock.Mock()
        with mock.patch.object(signin.clock, "now", FakeNow.now), \
             mock.patch.object(signin, "attempt_signin", side_effect=fake_attempt), \
             mock.patch.object(signin, "_write_sign_state") as ws, \
             mock.patch.object(signin, "_update_cred_state"), \
             mock.patch.object(signin, "logger", logmock), \
             mock.patch.object(signin, "send_notification"), \
             mock.patch.object(signin, "send_user_fail_mail"), \
             mock.patch.object(signin, "_collect_admin_mail"), \
             mock.patch.object(signin.time, "monotonic", return_value=100.0), \
             mock.patch.object(signin.time, "sleep"):
            signin.run_queue_retry([_acc("13800138000")], "", 0, 0, schedule=schedule)
        return ws, logmock

    def test_schedule_retry_state_and_log_include_reason(self):
        """需求1（调度分支）：失败账号入队重试时，状态文件与 warning 日志都补记失败原因。"""
        reason = "获取签到任务失败: 未登录或登录已经超时"
        sched = {"13800138000": _dt(2026, 8, 27, 6, 40)}
        ws, logmock = self._run_status(reason, sched)
        retry_msgs = [c.args[2] for c in ws.call_args_list
                      if c.args[1] == signin.STATUS_RETRYING]
        self.assertTrue(retry_msgs, "未捕获到重试入队的状态写入")
        self.assertTrue(all(reason in m for m in retry_msgs), "状态文件未补记失败原因")
        warn_lines = [c.args[0] for c in logmock.warning.call_args_list
                      if str(c.args[0]).startswith("[13800138000] ⏳ 待重试")]
        self.assertTrue(warn_lines, "未捕获到重试入队日志")
        self.assertTrue(all(reason in w for w in warn_lines), "warning 日志未补记失败原因")

    def test_queue_retry_state_and_log_include_reason(self):
        """需求1（队列回队尾分支）：同上，覆盖无计划（手动/列表）模式。"""
        reason = "请求被 WAF 风控拦截，请配置 YIBAN_PROXY 代理后重试"
        ws, logmock = self._run_status(reason, None)
        retry_msgs = [c.args[2] for c in ws.call_args_list
                      if c.args[1] == signin.STATUS_RETRYING]
        self.assertTrue(retry_msgs, "未捕获到重试入队的状态写入")
        self.assertTrue(all(reason in m for m in retry_msgs), "状态文件未补记失败原因")
        warn_lines = [c.args[0] for c in logmock.warning.call_args_list
                      if str(c.args[0]).startswith("[13800138000] ⏳ 待重试")]
        self.assertTrue(warn_lines, "未捕获到重试入队日志")
        self.assertTrue(all(reason in w for w in warn_lines), "warning 日志未补记失败原因")

    def test_retry_reason_sanitized_no_log_injection(self):
        """需求1：含换行的原因经 _sanitize_text 转义，状态文件/日志不会被拆成多行。"""
        reason = "获取签到任务失败\n未登录或登录已经超时"
        sched = {"13800138000": _dt(2026, 8, 27, 6, 40)}
        ws, logmock = self._run_status(reason, sched)
        retry_msgs = [c.args[2] for c in ws.call_args_list
                      if c.args[1] == signin.STATUS_RETRYING]
        self.assertTrue(retry_msgs)
        self.assertTrue(all(signin._sanitize_text(reason) in m for m in retry_msgs),
                        "重试状态 message 应包含转义后的原因")
        self.assertTrue(all("\n" not in m for m in retry_msgs), "原因含真实换行会污染状态文件")
        warn_lines = [c.args[0] for c in logmock.warning.call_args_list
                      if str(c.args[0]).startswith("[13800138000] ⏳ 待重试")]
        self.assertTrue(all("\n" not in w for w in warn_lines),
                        "原因换行会把日志拆成多行")


class RetryHmAtoMidTest(unittest.TestCase):
    """取值与容错：与窗口起止同用 parse_hhmm（接受 `7:30` 与 `07:30`）。"""

    def test_default_matches_documented_retry_cron(self):
        self.assertEqual(window.retry_hm({}), (7, 12))
        self.assertEqual(window.retry_hm({}), window.DEFAULT_RETRY_HM)

    def test_env_override(self):
        self.assertEqual(window.retry_hm({"YIBAN_SECOND_RUN_TIME": "07:30"}), (7, 30))
        self.assertEqual(window.retry_hm({"YIBAN_SECOND_RUN_TIME": "7:30"}), (7, 30))
        self.assertEqual(window.retry_hm({"YIBAN_SECOND_RUN_TIME": " 06:05 "}), (6, 5))

    def test_invalid_falls_back(self):
        for raw in ("", "abc", "25:00", "07:60", "07", None, "07:12:30"):
            with self.subTest(raw=raw):
                self.assertEqual(window.retry_hm({"YIBAN_SECOND_RUN_TIME": raw}),
                                 window.DEFAULT_RETRY_HM)

    def test_reads_env_at_call_time(self):
        """导入期缓存会让"管理员改了补签时刻"在长驻进程里不生效。"""
        with mock.patch.dict(os.environ, {"YIBAN_SECOND_RUN_TIME": "07:40"}):
            self.assertEqual(window.retry_hm(), (7, 40))
        os.environ.pop("YIBAN_SECOND_RUN_TIME", None)
        self.assertEqual(window.retry_hm(), window.DEFAULT_RETRY_HM)


class HostRunShContractTest(unittest.TestCase):
    """宿主 run.sh 是同键的另一个取用点：缺省必须同源，且要传给子进程。"""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(BASE, "run.sh"), encoding="utf-8") as f:
            cls.src = f.read()

    def test_default_literal_matches_shared_constant(self):
        m = re.search(r'\$\{YIBAN_SECOND_RUN_TIME:-(\d{1,2}:\d{2})\}', self.src)
        self.assertIsNotNone(m, "run.sh 未按 SECOND_HHMM=\"${YIBAN_SECOND_RUN_TIME:-HH:MM}\" 取补签时刻")
        hh, mm = m.group(1).split(":")
        self.assertEqual((int(hh), int(mm)), window.DEFAULT_RETRY_HM,
                         "run.sh 默认补签时刻与 yiban.window.DEFAULT_RETRY_HM 漂移")

    def test_exports_key_to_child(self):
        """不导出时子进程读不到 .env 之外的实际取值（signin 会退回默认）。"""
        self.assertRegex(self.src, r'(?m)^export YIBAN_SECOND_RUN_TIME=')


class SigninAlertThresholdTest(unittest.TestCase):
    """告警抑制按**当前**补签时刻判断，且双向都按实际值走。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-retry-slot-")
        p = mock.patch.dict(os.environ, {"YIBAN_STATE_DIR": self.tmp})
        p.start()
        self.addCleanup(p.stop)
        signin._mail_summary.clear()
        self.addCleanup(signin._mail_summary.clear)
        self.accounts = [SimpleNamespace(phone="13800000001"),
                         SimpleNamespace(phone="13800000002")]
        self.results = {
            "13800000001": (True, "已签到", False, "success"),
            "13800000002": (False, "签到时段已结束", True, "skipped_window"),
        }

    def _alert(self, hm):
        """注入固定时刻跑一次告警判定（该路径只取 clock.now() 的时分）。"""
        moment = clock_cls(2026, 9, 15, hm[0], hm[1])
        with mock.patch.object(signin.clock, "now", lambda: moment):
            return signin._maybe_alert_zero_success(
                self.accounts, self.results, ok_n=1, is_second_run=False)

    def test_late_retry_slot_suppresses(self):
        """补签时刻被配到 07:30：07:20 的首签轮仍有下一轮兜底 → 不打扰。"""
        with mock.patch.dict(os.environ, {"YIBAN_SECOND_RUN_TIME": "07:30"}):
            self.assertFalse(self._alert((7, 20)))

    def test_early_retry_slot_alerts(self):
        """补签时刻被配到 07:05：07:20 已是当天最后一轮 → 必须告警（防静默漏报）。"""
        with mock.patch.dict(os.environ, {"YIBAN_SECOND_RUN_TIME": "07:05"}):
            self.assertTrue(self._alert((7, 20)))

    def test_default_slot_boundary(self):
        """缺省 07:12：正好到点即为最后一轮（06:31 关机后 07:12 才起跑的经典场景）。"""
        os.environ.pop("YIBAN_SECOND_RUN_TIME", None)
        self.assertFalse(self._alert((7, 11)))
        self.assertTrue(self._alert((7, 12)))


class ContainerInjectsRetrySlotTest(unittest.TestCase):
    """容器把实际触发点（SECOND）注入子进程环境——容器不读 YIBAN_SECOND_RUN_TIME。"""

    def test_child_env_carries_actual_slot(self):
        captured = {}

        class _Proc:
            def wait(self, timeout=None):
                return 0

        def _fake_popen(cmd, cwd=None, env=None):
            captured["env"] = dict(env or {})
            return _Proc()

        with mock.patch.object(scheduler.subprocess, "Popen", _fake_popen), \
             mock.patch.object(scheduler, "build_child_env", return_value={}):
            scheduler._run_signin_child()
        expect = f"{scheduler.SECOND[0]:02d}:{scheduler.SECOND[1]:02d}"
        self.assertEqual(captured["env"].get("YIBAN_SECOND_RUN_TIME"), expect)

    def test_injection_wins_over_stale_env_key(self):
        """.env 里若写了该键，容器也不据此调度 → 必须以容器实际值覆盖，
        否则 signin 会按一个没人用的时刻判断末轮（静默漏报方向）。"""
        captured = {}

        class _Proc:
            def wait(self, timeout=None):
                return 0

        def _fake_popen(cmd, cwd=None, env=None):
            captured["env"] = dict(env or {})
            return _Proc()

        with mock.patch.object(scheduler.subprocess, "Popen", _fake_popen), \
             mock.patch.object(scheduler, "build_child_env",
                               return_value={"YIBAN_SECOND_RUN_TIME": "23:59"}):
            scheduler._run_signin_child()
        self.assertEqual(captured["env"]["YIBAN_SECOND_RUN_TIME"],
                         f"{scheduler.SECOND[0]:02d}:{scheduler.SECOND[1]:02d}")


#: run.sh 的按日文件（sign-status-<date>.txt / sign-<date>.log）与库内当日事实查询
#: 都用宿主 `date +%Y-%m-%d`（run.sh:183 等），跑 run.sh 的用例须与宿主日对齐。
TODAY = datetime.now().strftime("%Y-%m-%d")

#: signin/scheduler 的 sched-run-<date>.json / sign-state-<date>.json 取业务钟
#: （yiban.clock，北京 +8），跨宿主 TZ 时与 TODAY 可能不同日——这两类消费方必须用 BIZ_TODAY。
BIZ_TODAY = clock.today()


_MISSING = object()  # 哨兵：区分"不创建该文件"


STUB_SIGNIN = '''# -*- coding: utf-8 -*-
"""测试桩：替代真实 scripts/signin.py。

- 带 --second-run-check：按 $STATE_DIR/check_exit 文件的值退出（缺省 10=需要补跑）
- 否则：把本轮调用追加到 $STATE_DIR/rounds.log（记录 YIBAN_SECOND_RUN），
  并把 $STATE_DIR/sched-run-<today>.json 写成 completed=true，
  再按 $STATE_DIR/round_exit 的值退出（缺省 0）
"""
import json, os, sys
from datetime import datetime, timedelta

state = os.environ.get("YIBAN_STATE_DIR", ".")
# 模拟生产 signin 的按日留痕（yiban.clock 北京钟）：固定 +8，不随宿主 TZ 漂移
today = (datetime.utcnow() + timedelta(hours=8)).strftime("%Y-%m-%d")


def _read_int(name, default):
    try:
        with open(os.path.join(state, name), encoding="utf-8") as f:
            return int(f.read().strip() or default)
    except (OSError, ValueError):
        return default


if "--second-run-check" in sys.argv:
    sys.exit(_read_int("check_exit", 10))

with open(os.path.join(state, "rounds.log"), "a", encoding="utf-8") as f:
    f.write("round second_run=%s\\n" % os.environ.get("YIBAN_SECOND_RUN", ""))
with open(os.path.join(state, "sched-run-%s.json" % today), "w", encoding="utf-8") as f:
    json.dump({"completed": True}, f)
sys.exit(_read_int("round_exit", 0))
'''


class NeedSecondRunTest(unittest.TestCase):
    """判定矩阵：与容器 docker/scheduler.py 的 SECOND 闸门同语义。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="b20-2nd-")
        os.environ["YIBAN_STATE_DIR"] = self.tmp

    def tearDown(self):
        os.environ.pop("YIBAN_STATE_DIR", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, name, payload):
        with io.open(os.path.join(self.tmp, name), "w", encoding="utf-8") as f:
            if isinstance(payload, str):
                f.write(payload)
            else:
                json.dump(payload, f)

    def test_no_sched_marker_needs_second(self):
        """当日全量未收尾（标记缺失）→ 需要补跑。"""
        self.assertTrue(signin.need_second_run(self.tmp, BIZ_TODAY))

    def test_done_and_all_success_no_second(self):
        """已收尾 + 全部 success → 不需要补跑。"""
        self._write(f"sched-run-{BIZ_TODAY}.json", {"completed": True})
        self._write(f"sign-state-{BIZ_TODAY}.json", {
            "13800000001": {"status": "success"},
            "13800000002": {"status": "already"},
            "13800000003": {"status": "no_task"},
        })
        self.assertFalse(signin.need_second_run(self.tmp, BIZ_TODAY))

    def test_done_with_failed_needs_second(self):
        """已收尾但有 failed 账号 → 需要补跑（补签轮的核心价值）。"""
        self._write(f"sched-run-{BIZ_TODAY}.json", {"completed": True})
        self._write(f"sign-state-{BIZ_TODAY}.json", {
            "13800000001": {"status": "success"},
            "13800000002": {"status": "failed"},
        })
        self.assertTrue(signin.need_second_run(self.tmp, BIZ_TODAY))

    def test_done_with_window_skip_needs_second(self):
        """已收尾但有 skipped_window（学校窗口晚于本地配置）→ 需要补跑。"""
        self._write(f"sched-run-{BIZ_TODAY}.json", {"completed": True})
        self._write(f"sign-state-{BIZ_TODAY}.json", {"13800000001": {"status": "skipped_window"}})
        self.assertTrue(signin.need_second_run(self.tmp, BIZ_TODAY))

    def test_completed_false_needs_second(self):
        """标记存在但 completed=false（首轮被 timeout 击杀）→ 需要补跑。"""
        self._write(f"sched-run-{BIZ_TODAY}.json", {"completed": False})
        self._write(f"sign-state-{BIZ_TODAY}.json", {"13800000001": {"status": "success"}})
        self.assertTrue(signin.need_second_run(self.tmp, BIZ_TODAY))

    def test_missing_state_file_fails_safe(self):
        """标记已写但状态文件缺失 → 按"未了结"处理（宁多跑一轮，不漏签）。"""
        self._write(f"sched-run-{BIZ_TODAY}.json", {"completed": True})
        self.assertTrue(signin.need_second_run(self.tmp, BIZ_TODAY))

    def test_corrupted_state_file_fails_safe(self):
        """状态文件损坏 → 同样按"需要补跑"。"""
        self._write(f"sched-run-{BIZ_TODAY}.json", {"completed": True})
        self._write(f"sign-state-{BIZ_TODAY}.json", "{ 不是合法 JSON")
        self.assertTrue(signin.need_second_run(self.tmp, BIZ_TODAY))

    def test_empty_state_dict_fails_safe(self):
        """状态文件是空对象 → 按"需要补跑"。"""
        self._write(f"sched-run-{BIZ_TODAY}.json", {"completed": True})
        self._write(f"sign-state-{BIZ_TODAY}.json", {})
        self.assertTrue(signin.need_second_run(self.tmp, BIZ_TODAY))

    def test_cli_exit_code_contract(self):
        """CLI 契约：需要补跑 → 10；不需要 → 0（run.sh 依赖该退出码）。"""
        env = dict(os.environ)
        env["YIBAN_STATE_DIR"] = self.tmp
        cmd = [sys.executable, os.path.join(BASE, "scripts", "signin.py"), "--second-run-check"]

        r = subprocess.run(cmd, capture_output=True, env=env, cwd=BASE)
        self.assertEqual(r.returncode, signin.SECOND_RUN_CHECK_NEED,
                         "无标记时应返回 10（需要补跑）")
        self.assertEqual(signin.SECOND_RUN_CHECK_NEED, 10, "退出码契约不得改动")

        self._write(f"sched-run-{BIZ_TODAY}.json", {"completed": True})
        self._write(f"sign-state-{BIZ_TODAY}.json", {"13800000001": {"status": "success"}})
        r2 = subprocess.run(cmd, capture_output=True, env=env, cwd=BASE)
        self.assertEqual(r2.returncode, signin.SECOND_RUN_CHECK_SKIP,
                         "已收尾且无未了结账号时应返回 0")


class RunshSecondRoundTest(unittest.TestCase):
    """真实执行 run.sh：验证进程内补签轮的两轮语义。"""

    @classmethod
    def setUpClass(cls):
        cls.bash = shutil.which("bash")
        if not cls.bash:
            raise unittest.SkipTest("无 bash 环境")

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="b20-runsh-")
        self.app = os.path.join(self.tmp, "app")
        self.state = os.path.join(self.tmp, "state")
        self.lock = os.path.join(self.tmp, "lock")
        self.bin = os.path.join(self.tmp, "bin")
        for d in (os.path.join(self.app, "scripts"), os.path.join(self.app, ".venv", "bin"),
                  self.state, self.lock, self.bin):
            os.makedirs(d, exist_ok=True)
        # 应用脚本（桩）+ .env
        with io.open(os.path.join(self.app, "scripts", "signin.py"), "w", encoding="utf-8") as f:
            f.write(STUB_SIGNIN)
        with io.open(os.path.join(self.app, ".env"), "w", encoding="utf-8") as f:
            f.write("YIBAN_SIGN_END=07:50\n")
        # .venv/bin/python3 包装器（run.sh 优先用它）
        py_wrap = os.path.join(self.app, ".venv", "bin", "python3")
        with io.open(py_wrap, "w", encoding="utf-8", newline="\n") as f:
            f.write('#!/bin/sh\nexec "%s" "$@"\n' % sys.executable.replace("\\", "/"))
        os.chmod(py_wrap, os.stat(py_wrap).st_mode | stat.S_IEXEC)
        # shim：flock 恒成功、timeout 忽略时长参数（本用例不测锁与超时）
        for name, body in (("flock", "#!/bin/sh\nexit 0\n"),
                           ("timeout", '#!/bin/sh\nshift\nexec "$@"\n')):
            p = os.path.join(self.bin, name)
            with io.open(p, "w", encoding="utf-8", newline="\n") as f:
                f.write(body)
            os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, extra_env=None):
        env = dict(os.environ)
        env.update({
            "PATH": self.bin + os.pathsep + env.get("PATH", ""),
            "YIBAN_APP_DIR": self.app,
            "YIBAN_STATE_DIR": self.state,
            "YIBAN_LOCK_DIR": self.lock,
            "YIBAN_LOG_FILE": os.path.join(self.state, "sign.log"),
            # 补签时刻设为 00:01（已过）→ 不触发等待，立即补跑
            "YIBAN_SECOND_RUN_TIME": "00:01",
        })
        env.pop("YIBAN_SECOND_RUN", None)
        env.update(extra_env or {})
        return subprocess.run([self.bash, os.path.join(BASE, "run.sh")],
                              capture_output=True, env=env, cwd=self.app)

    def _rounds(self):
        p = os.path.join(self.state, "rounds.log")
        if not os.path.exists(p):
            return []
        with io.open(p, encoding="utf-8") as f:
            return [ln.strip() for ln in f if ln.strip()]

    def _write_ctl(self, name, value):
        with io.open(os.path.join(self.state, name), "w", encoding="utf-8") as f:
            f.write(str(value))

    def test_two_rounds_when_second_needed(self):
        """需要补跑 → 两轮，且第二轮带 YIBAN_SECOND_RUN=1。

        首轮退出码取 2（存在窗口外/未了结账号）——这正是生产里会走到补签的形态：
        状态非 SUCCESS 且状态文件含未了结账号。
        """
        self._write_ctl("check_exit", 10)
        self._write_ctl("round_exit", 2)
        r = self._run()
        self.assertEqual(r.returncode, 2, r.stderr.decode("utf-8", "replace"))
        rounds = self._rounds()
        self.assertEqual(len(rounds), 2, f"应跑两轮，实际 {rounds}")
        self.assertEqual(rounds[0], "round second_run=", "首轮不得带补签标记")
        self.assertEqual(rounds[1], "round second_run=1", "第二轮必须带补签标记")
        with io.open(os.path.join(self.state, "sign-status-%s.txt" % TODAY), encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), "SKIPPED", "补签轮仍是未了结 → 状态应为 SKIPPED")

    def test_single_round_when_not_needed(self):
        """不需要补跑 → 只跑一轮，且不留补签痕迹。"""
        self._write_ctl("check_exit", 0)
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stderr.decode("utf-8", "replace"))
        self.assertEqual(self._rounds(), ["round second_run="])

    def test_success_status_short_circuits_second_round(self):
        """首轮 SUCCESS 时不补跑（防御）：状态已是成功，再跑一轮纯属多余登录。

        注：真实路径下 SUCCESS 与"需要补跑"不会同时成立（exit 0 蕴含无未了结账号），
        本用例锁住的是"矛盾输入下取保守且不浪费"的一侧。

        MF-82 同批更新：SUCCESS 现在须与库内当日事实交叉核对才采信——给一个种了
        当日 done 行的临时库，代表"真成功"（桩 timeout 直落桩 signin，不写库，
        事实由用例预置）。
        """
        self._write_ctl("check_exit", 10)
        self._write_ctl("round_exit", 0)
        db = os.path.join(self.tmp, "facts.db")
        con = sqlite3.connect(db)
        try:
            con.execute("CREATE TABLE sign_tasks (phone TEXT, day TEXT, state TEXT)")
            con.execute("INSERT INTO sign_tasks VALUES (?,?,?)",
                        ("138****0000", TODAY, "done"))
            con.commit()
        finally:
            con.close()
        r = self._run({"YIBAN_DB_FILE": db})
        self.assertEqual(r.returncode, 0, r.stderr.decode("utf-8", "replace"))
        self.assertEqual(self._rounds(), ["round second_run="], "SUCCESS 后不应补跑")

    def test_forged_success_does_not_short_circuit_second_round(self):
        """MF-82 进程内侧翼：桩首轮"成功"写出的 SUCCESS 若与库内当日事实相悖
        （库不存在/无行）⇒ 不采信，补签轮照跑——伪造件不得吃掉当天兜底。"""
        self._write_ctl("check_exit", 10)
        self._write_ctl("round_exit", 0)
        r = self._run({"YIBAN_DB_FILE": os.path.join(self.tmp, "no-such.db")})
        self.assertEqual(r.returncode, 0, r.stderr.decode("utf-8", "replace"))
        rounds = self._rounds()
        self.assertEqual(len(rounds), 2,
                         f"无库内事实的 SUCCESS 不得短路补签轮，实际: {rounds}")
        self.assertEqual(rounds[1], "round second_run=1")
        log_path = os.path.join(self.state, "sign-%s.log" % TODAY)
        with io.open(log_path, encoding="utf-8", errors="replace") as f:
            self.assertIn("拒绝采信", f.read())

    def test_second_round_can_be_disabled(self):
        """逃生开关 YIBAN_HOST_SECOND_ROUND=0 → 即使判定需要也只跑一轮。"""
        self._write_ctl("check_exit", 10)
        self._write_ctl("round_exit", 2)
        r = self._run({"YIBAN_HOST_SECOND_ROUND": "0"})
        self.assertEqual(r.returncode, 2, r.stderr.decode("utf-8", "replace"))
        self.assertEqual(self._rounds(), ["round second_run="])

    def test_global_pause_skips_second_round(self):
        """全站暂停时补跑无意义：signin 立即 exit 2，只跑一轮（不为空跑延长持锁）。"""
        self._write_ctl("check_exit", 10)
        self._write_ctl("round_exit", 2)
        r = self._run({"YIBAN_GLOBAL_PAUSE": "1"})
        self.assertEqual(self._rounds(), ["round second_run="])
        with io.open(os.path.join(self.state, "sign-status-%s.txt" % TODAY), encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), "GLOBAL_PAUSED")
        self.assertEqual(r.returncode, 2)

    def test_no_third_round_when_already_second_run(self):
        """本轮本身就是补签轮（07:12 兜底 cron 接管）→ 不再评估第三轮。"""
        self._write_ctl("check_exit", 10)
        self._write_ctl("round_exit", 2)
        r = self._run({"YIBAN_SECOND_RUN": "1"})   # 模拟当日已触发过 → 补签轮身份
        self.assertEqual(r.returncode, 2, r.stderr.decode("utf-8", "replace"))
        self.assertEqual(self._rounds(), ["round second_run=1"],
                         "只跑一轮，且该轮已带补签身份")

    def test_settled_marker_short_circuits_later_invocation(self):
        """收尾标记落地后，再次调用 run.sh 直接跳过（07:12 兜底 cron 不再多跑）。"""
        self._write_ctl("check_exit", 10)
        self._write_ctl("round_exit", 2)
        self._run()
        self.assertEqual(len(self._rounds()), 2)
        # 第二次调用（模拟 07:12 的 cron；不带 YIBAN_SECOND_RUN 也应被收尾标记挡住）
        self._run()
        self.assertEqual(len(self._rounds()), 2, "收尾后不得再产生新的签到轮次")
        self.assertTrue(
            any(n.startswith("yiban-settled-") for n in os.listdir(self.state)),
            "应生成当日收尾标记",
        )


class HostContainerAgreementTest(unittest.TestCase):
    """宿主与容器的补签判定必须完全一致（防两侧实现漂移）。

    容器 docker/scheduler.py 现在直接复用 signin 的判定函数；本用例在多种状态文件
    组合下同时调用两侧入口并比对结果——一旦有人只改一边（或重新内联实现），此处即红。
    """

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="b20-agree-")
        cls._old_env = {k: os.environ.get(k) for k in ("YIBAN_STATE_DIR", "YIBAN_ENV_FILE")}
        os.environ["YIBAN_STATE_DIR"] = cls.tmp
        os.environ["YIBAN_ENV_FILE"] = os.path.join(cls.tmp, ".env")
        with io.open(os.environ["YIBAN_ENV_FILE"], "w", encoding="utf-8") as f:
            f.write("")
        # scheduler 读 env 在导入期，必须先设好环境再导入
        sys.path.insert(0, os.path.join(BASE, "scripts"))
        sys.path.insert(0, os.path.join(BASE, "docker"))
        import scheduler
        cls.scheduler = scheduler
        # 全量跑时 scheduler 可能已被别的用例导入过，模块级 STATEDIR 绑定的是当时
        # 那套环境——此处显式改指本类的临时目录再比对，否则是拿两个不同目录作对比
        # （曾造成「已收尾+全成功」「已收尾+暂停」两个子用例在全量下误报不一致）。
        cls._old_statedir = scheduler.STATEDIR
        scheduler.STATEDIR = cls.tmp

    @classmethod
    def tearDownClass(cls):
        cls.scheduler.STATEDIR = cls._old_statedir
        for k, v in cls._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _setup(self, sched_payload, state_payload):
        for name in os.listdir(self.tmp):
            if name.startswith(("sched-run-", "sign-state-")):
                os.remove(os.path.join(self.tmp, name))
        if sched_payload is not _MISSING:
            with io.open(os.path.join(self.tmp, f"sched-run-{BIZ_TODAY}.json"), "w",
                         encoding="utf-8") as f:
                f.write(sched_payload if isinstance(sched_payload, str)
                        else json.dumps(sched_payload))
        if state_payload is not _MISSING:
            with io.open(os.path.join(self.tmp, f"sign-state-{BIZ_TODAY}.json"), "w",
                         encoding="utf-8") as f:
                f.write(state_payload if isinstance(state_payload, str)
                        else json.dumps(state_payload))

    def _assert_agree(self, sched_payload, state_payload, expected):
        self._setup(sched_payload, state_payload)
        host = signin.need_second_run(self.tmp, BIZ_TODAY)
        container = (not self.scheduler._full_run_done_today()) or \
            self.scheduler._has_undone_today()
        self.assertEqual(host, container,
                         "宿主判定与容器判定不一致（semantics 漂移）")
        self.assertEqual(host, expected, "判定结果与预期不符")

    def test_matrix_agrees(self):
        cases = [
            ("无任何状态文件（fail-safe 应补跑）", _MISSING, _MISSING, True),
            ("已收尾 + 全成功", {"completed": True},
             {"a": {"status": "success"}}, False),
            ("已收尾 + 一处失败", {"completed": True},
             {"a": {"status": "failed"}}, True),
            ("已收尾 + 窗口外跳过", {"completed": True},
             {"a": {"status": "skipped_window"}}, True),
            ("已收尾 + 暂停（paused 不算未了结）", {"completed": True},
             {"a": {"status": "paused"}}, False),
            ("completed=false（首轮被击杀）", {"completed": False},
             {"a": {"status": "success"}}, True),
            ("标记在但状态文件缺失", {"completed": True}, _MISSING, True),
        ]
        for label, sp, stp, exp in cases:
            with self.subTest(case=label):
                self._assert_agree(sp, stp, exp)

    def test_undone_status_set_is_single_source(self):
        """容器引用的未了结集合必须就是 signin 的那一份（不是复制品）。"""
        self.assertIs(self.scheduler._UNDONE_STATUSES, signin.UNDONE_STATUSES)
