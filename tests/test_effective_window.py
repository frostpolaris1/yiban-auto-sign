# -*- coding: utf-8 -*-
"""有效窗口单一口径（排计划 / 判关闭 / 算容量同源）。

标签：A · 调度：计划与分片
覆盖：窗口单一口径的三条入口一致性（bounds / from_env /
   引擎与网页共用的remaining_sec、capacity_accounts）、缓冲吃空时的退化处置与容量非零、窗口关闭收尾对当日已有结论的
   CAS 保护、零请求收尾轮的退出码、手动链路（schedule
   为空）的逐账号窗口钳制、补签轮对 no_task 的「已了结」判定、业务钟（北京
   +8，与宿主 TZ 无关）驱动窗口判定与按日留痕、F4 非 5
   分钟整数倍窗口的自选尾片。
对应实现：yiban/window.py（Window、bounds、from_env、remaining_sec、full_sec）、yiban/clock.py（beijing_now、today、ts）、scripts/signin.py（_schedule_config、_schedule_blocks、_window_closed、capacity_accounts、run_queue_retry
   的窗口收尾、main 的退出码）、yiban/db 会话缓存时钟。
关键断言：窗口只准算一次：排计划与判关闭必须取自同一份有效窗口，两条入口（env /
   cfg）必须逐值相等——否则有完整计划却整轮判「时段已结束」。缓冲吃空时保留管理员的窗口、只收缩缓冲，容量不得显示
   0、不得回退内置默认窗口。剩余窗口必须扣掉已流逝时间，迟启动要能被发现。窗口关闭的收尾轮不得改写当日已记录的
   failed / no_position（真实原因与失败告警一起丢），落盘前还要 CAS
   拦下并发写入；退出码按账号的真实结论算。窗口判定只认业务钟：UTC 主机上的北京
   06:40 必须照签，北京 08:10 必须判结束。
依赖：纯本地：临时状态目录 + 打桩 attempt_signin / time.sleep /
   _update_cred_state，不建库、不发网络请求。整文件在本机全部执行，无 skip。

**缺陷**：窗口被算了四遍且各不相同——排计划（`_schedule_blocks`，含"裁剪吃空则回退
默认窗口"）、判关闭（`_window_closed`，只看 sign_end−edge_back）、引擎容量预检
（内联算一遍，无回退也不扣流逝时间）、网页预估（自己读 env 再算一遍）。于是：

- 裁剪吃空时计划按回退窗口排（有 80 分钟计划），判定却按原始配置算
  → 有完整计划却整轮判"时段已结束"、零请求；容量还显示 0；
- 预检按完整窗口算，迟启动时按满容量放行且不告警，超出的账号落
  skipped_window；
- 补签轮起跑时窗口已关闭 → 整轮零请求，却把首轮已记录的
  failed/no_position 无条件改写成 skipped_window（真实原因与失败告警一起丢掉）。

**修法**：`yiban/window.py` 为唯一事实源，引擎与网页共用。
"""
import datetime as _dt
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime
from unittest import mock

import signin

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(0, os.path.join(BASE, "scripts"))

from yiban import (  # noqa: E402
    clock,
    window,
)

PHONE = "13800138000"


def _cfg(**env):
    """按给定 env 覆盖生成配置 dict（其余键取默认）。"""
    old = {k: os.environ.get(k) for k in env}
    try:
        for k, v in env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = str(v)
        return signin._schedule_config() # 走真实解析器而不是手搓 dict：配置映射本身也得被核对一遍
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class WindowSourceTest(unittest.TestCase):
    def test_default_window(self):
        cfg = _cfg(YIBAN_SIGN_START=None, YIBAN_SIGN_END=None,
                   YIBAN_WINDOW_EDGE_FRONT_SEC=None, YIBAN_WINDOW_EDGE_BACK_SEC=None,
                   YIBAN_WINDOW_EDGE_SEC=None)
        win = window.bounds(cfg)
        self.assertEqual((win.start_min, win.end_min), (390, 470))
        self.assertEqual((win.lo_min, win.hi_min), (391.0, 469.0))
        self.assertFalse(win.fell_back)

    def test_legacy_edge_key_maps_symmetrically(self):
        cfg = _cfg(YIBAN_WINDOW_EDGE_SEC="120", YIBAN_WINDOW_EDGE_FRONT_SEC=None,
                   YIBAN_WINDOW_EDGE_BACK_SEC=None)
        self.assertEqual((cfg["edge_front_sec"], cfg["edge_back_sec"]), (120, 120))

    def test_sch3_trimmed_empty_window_clamps_edges_for_both_plan_and_gate(self):
        """缓冲过大时只收缩缓冲（窗口保留），排计划与判关闭必须都按收缩后的有效窗口。"""
        cfg = _cfg(YIBAN_SIGN_START="07:00", YIBAN_SIGN_END="07:01",
                   YIBAN_WINDOW_EDGE_FRONT_SEC="300", YIBAN_WINDOW_EDGE_BACK_SEC="300")
        win = window.bounds(cfg)
        self.assertTrue(win.edges_clamped, "缓冲合计 >= 窗口宽度应收缩缓冲")
        self.assertFalse(win.fell_back, "窗口可用时不得回退默认窗口")
        # 窗口 1 分钟保留；缓冲等比收缩到合计 12s（各 6s）→ 有效窗口 48s
        self.assertEqual((win.start_min, win.end_min), (420, 421))
        self.assertEqual((win.front_sec, win.back_sec), (6, 6))
        self.assertEqual((win.lo_min, win.hi_min), (420.1, 420.9))
        # 计划（_schedule_blocks）与判定（_window_closed）同源
        blocks, eff_lo, eff_hi = signin._schedule_blocks(cfg) # 计划侧的 eff_lo/eff_hi 与上面的 win 同源，这一行就是「单一口径」的落点
        self.assertGreater(len(blocks), 0, "收缩后窗口下应有完整计划")
        self.assertEqual((eff_lo, eff_hi), (win.lo_min, win.hi_min))
        self.assertFalse(
            signin._window_closed(cfg, _dt.datetime(2026, 9, 15, 7, 0)),
            "07:00 在有效窗口起点之前，不得判『时段已结束』（按原始配置算 → 全员零请求）",
        )
        self.assertFalse(signin._window_closed(cfg, _dt.datetime(2026, 9, 15, 7, 0, 20)))
        self.assertTrue(signin._window_closed(cfg, _dt.datetime(2026, 9, 15, 9, 0)))

    def test_capacity_nonzero_train_on_trimmed_empty_window(self):
        """缓冲过大时容量不再显示 0（收缩后仍有 48 秒有效窗口）。"""
        cfg = _cfg(YIBAN_SIGN_START="07:00", YIBAN_SIGN_END="07:01",
                   YIBAN_WINDOW_EDGE_FRONT_SEC="300", YIBAN_WINDOW_EDGE_BACK_SEC="300")
        win = window.bounds(cfg)
        self.assertGreater(signin.capacity_accounts(win.full_sec(), 10, 3), 0)

    def test_sch4_remaining_sec_deducts_elapsed(self):
        """剩余窗口 = 有效窗口结束 − 当前时刻（引擎预检口径）。"""
        cfg = _cfg(YIBAN_SIGN_START="06:30", YIBAN_SIGN_END="07:50",
                   YIBAN_WINDOW_EDGE_FRONT_SEC="60", YIBAN_WINDOW_EDGE_BACK_SEC="60")
        win = window.bounds(cfg)
        # eff_hi = 07:49；07:40 起跑 → 剩 9 分钟
        rest = win.remaining_sec(_dt.datetime(2026, 9, 15, 7, 40))
        self.assertAlmostEqual(rest, 9 * 60, delta=2)
        self.assertGreater(win.full_sec(), rest, "完整窗口必须大于剩余窗口")
        # 起跑已过窗口 → 剩余为负，调用方据此给出"本轮不会执行"的明确告警
        self.assertLess(win.remaining_sec(_dt.datetime(2026, 9, 15, 8, 10)), 0)

    def test_from_env_matches_cfg_path(self):
        """两条入口（env / cfg）必须得到同一结果，否则网页与引擎又会各算各的。"""
        env = {"YIBAN_SIGN_START": "06:40", "YIBAN_SIGN_END": "07:40",
               "YIBAN_WINDOW_EDGE_FRONT_SEC": "90", "YIBAN_WINDOW_EDGE_BACK_SEC": "30"}
        cfg = _cfg(**env)
        a, b = window.from_env(env), window.bounds(cfg) # 两条入口各算一遍再比：只钉一条就等于放任另一条自由漂移
        self.assertEqual((a.start_min, a.end_min, a.lo_min, a.hi_min),
                         (b.start_min, b.end_min, b.lo_min, b.hi_min))


class WindowSkipKeepsRecordedStatusTest(unittest.TestCase):
    """窗口关闭时的收尾不得覆盖已有当日记录。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-win-")
        self.env = dict(os.environ)
        os.environ["YIBAN_STATE_DIR"] = self.tmp
        os.environ["YIBAN_SIGN_START"] = "06:30"
        os.environ["YIBAN_SIGN_END"] = "07:50"
        os.environ.pop("YIBAN_SECOND_RUN", None)
        self.state_path = os.path.join(
            self.tmp, f"sign-state-{signin.clock.now().strftime('%Y-%m-%d')}.json")

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.env)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_state(self, mapping):
        with open(self.state_path, "w", encoding="utf-8") as f:
            json.dump(mapping, f)

    def _run(self, phones, beijing_now):
        accs = [signin.Account(phone=p, password="p") for p in phones]
        with mock.patch.object(signin.clock, "now", return_value=beijing_now), \
                mock.patch.object(signin, "attempt_signin") as attempt, \
                mock.patch.object(signin, "_update_cred_state"), \
                mock.patch.object(signin.time, "sleep"):
            results = signin.run_queue_retry(accs, "", 0, 0,
                                             schedule={p: beijing_now for p in phones})
        return attempt, results

    def test_closed_window_keeps_previous_failure_reason(self):
        """已记录 failed 的账号：窗口关闭收尾不得改写成 skipped_window。"""
        self._write_state({PHONE: {"status": signin.STATUS_FAILED, "message": "网络超时"}})
        after = signin.clock.now().replace(hour=8, minute=5, second=0, microsecond=0)
        self._run([PHONE], after)
        with open(self.state_path, encoding="utf-8") as f:
            st = json.load(f)[PHONE]["status"]
        self.assertEqual(st, signin.STATUS_FAILED, "补签轮整轮跳过不得丢掉首轮失败原因")

    def test_closed_window_marks_unrecorded_accounts(self):
        """当日无记录的账号（本轮根本没跑到）：仍记为窗口外跳过。"""
        self._write_state({})
        after = signin.clock.now().replace(hour=8, minute=5, second=0, microsecond=0)
        self._run([PHONE], after)
        with open(self.state_path, encoding="utf-8") as f:
            st = json.load(f)[PHONE]["status"]
        self.assertEqual(st, signin.STATUS_SKIPPED_WINDOW)

    def test_closed_window_carries_recorded_conclusion_into_results(self):
        """已有结论的账号必须带**真实结论**进 `results`，不能被收尾漏掉。

        汇总只认 `results`：缺席的账号落到默认 `(False, "未执行", False, pending)`
        → 计一次失败、`has_real_failure=True`、退出码 1，而本轮一个请求都没发。
        """
        for recorded in (signin.STATUS_SKIPPED_WINDOW, signin.STATUS_NO_POSITION,
                         signin.STATUS_SUCCESS):
            with self.subTest(recorded=recorded):
                self._write_state({PHONE: {"status": recorded, "message": "首轮留痕"}})
                after = signin.clock.now().replace(hour=8, minute=5, second=0,
                                                   microsecond=0)
                attempt, results = self._run([PHONE], after)
                self.assertEqual(attempt.call_count, 0, "窗口已关不得发起真实请求")
                self.assertEqual(results[PHONE][3], recorded,
                                 "results 必须带真实结论，否则汇总按未执行失败计")
                with open(self.state_path, encoding="utf-8") as f:
                    self.assertEqual(json.load(f)[PHONE]["status"], recorded,
                                     "已有结论不得被窗口外跳过改写")

    def test_closed_window_status_less_entry_is_no_record(self):
        """条目缺 `status` 键（无结论）→ 与 `_has_conclusion` 同口径写窗口外跳过。

        快照预筛若把这种条目当"已有结论"，就会拿 "None" 去汇总分组（落失败桶）；
        而锁内 CAS 判它"无结论"本就允许写入——两处口径必须一致。
        """
        self._write_state({PHONE: {"message": "半截条目"}})
        after = signin.clock.now().replace(hour=8, minute=5, second=0, microsecond=0)
        _attempt, results = self._run([PHONE], after)
        self.assertEqual(results[PHONE][3], signin.STATUS_SKIPPED_WINDOW)
        with open(self.state_path, encoding="utf-8") as f:
            self.assertEqual(json.load(f)[PHONE]["status"], signin.STATUS_SKIPPED_WINDOW)

    def test_closed_window_cas_does_not_clobber_concurrent_failure(self):
        """M9：快照读"无记录"后另一执行体刚写入 failed——落盘前必须 CAS 拦下。

        时序：本进程 `_mark_window_skip` 顶部读到空快照（此时还没人写）→ 进入
        写分支前另一执行体已把真实失败落盘 → 本进程再写会把 failed 覆盖成
        skipped_window、`has_real_failure` 变 False（失败告警被吞）。修复后
        写入走"仅当当日无结论"的锁内再判，不得覆盖。
        """
        # 模拟"另一执行体已写入 failed"：真实文件里已有 failed
        self._write_state({PHONE: {"status": signin.STATUS_FAILED, "message": "网络超时"}})
        after = signin.clock.now().replace(hour=8, minute=5, second=0, microsecond=0)
        accs = [signin.Account(phone=PHONE, password="p")]
        # _daily_statuses 打桩返回空：模拟 _mark_window_skip 顶部快照"读到无记录"
        # （快照时刻早于另一执行体的写入）——真实文件仍在锁内被读到 failed
        with mock.patch.object(signin.clock, "now", return_value=after), \
                mock.patch.object(signin, "attempt_signin"), \
                mock.patch.object(signin, "_update_cred_state"), \
                mock.patch.object(signin.time, "sleep"), \
                mock.patch.object(signin.state_io, "_daily_statuses", return_value={}):
            results = signin.run_queue_retry(accs, "", 0, 0,
                                             schedule={PHONE: after})
        with open(self.state_path, encoding="utf-8") as f:
            st = json.load(f)[PHONE]["status"]
        self.assertEqual(st, signin.STATUS_FAILED, "锁内 CAS 必须拦下覆盖")
        # 本进程不得把该账号记成"窗口外跳过"——失败保持可见（has_real_failure 不被吞）
        self.assertNotEqual(results.get(PHONE, (0, 0, 0, ""))[3],
                            signin.STATUS_SKIPPED_WINDOW,
                            "CAS 被拒后不得再标记为本轮的 skipped_window")

    # 本轮内"重试没赶上窗口"与本轮前的记录共用同一道守卫（`results` 与当日文件
    # 都查），故上面两条覆盖了该规则的两种来源。


class WindowClosedExitCodeTest(unittest.TestCase):
    """窗口已关的零请求收尾轮：退出码必须按账号的真实结论算，而不是按"未执行"算。

    run.sh 只认退出码：2 写 SKIPPED（补签会再跑），1 不写状态文件（当天留着失败语义，
    还伴随失败邮件）。已有结论的账号若被收尾漏出 `results`，汇总按默认 `pending`
    计成失败 → 退出码 1——真相却是"当日已有结论、本轮无需处理"。
    """

    PHONE = "13800138000"
    #: 固定业务时刻（2026-09-17 周四 08:05，晚于 06:30~07:50 的有效窗口 07:49）
    AFTER = _dt.datetime(2026, 9, 17, 8, 5)

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-win-exit-")
        self.env = dict(os.environ)
        os.environ.update({
            "YIBAN_STATE_DIR": self.tmp,
            "YIBAN_LOG_FILE": os.path.join(self.tmp, "sign.log"),
            "YIBAN_DB_FILE": os.path.join(self.tmp, "yiban.db"),
            "YIBAN_SIGN_START": "06:30",
            "YIBAN_SIGN_END": "07:50",
        })
        os.environ.pop("YIBAN_SECOND_RUN", None)
        os.environ.pop("YIBAN_GLOBAL_PAUSE", None)
        self.state_path = os.path.join(self.tmp, f"sign-state-{self.AFTER:%Y-%m-%d}.json")

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.env)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run_main(self, recorded):
        """预置一条当日结论后跑一轮全量（时间表非空 ⇒ 与真实全量轮同一条路径）。"""
        with open(self.state_path, "w", encoding="utf-8") as f:
            json.dump({self.PHONE: {"status": recorded, "message": "首轮留痕"}}, f)
        calls = []
        acc = signin.Account(phone=self.PHONE, password="p")
        with mock.patch.object(signin.clock, "now", return_value=self.AFTER), \
                mock.patch.object(signin, "load_accounts", return_value=[acc]), \
                mock.patch.object(signin, "build_schedule",
                                  return_value={self.PHONE: self.AFTER}), \
                mock.patch.object(signin, "attempt_signin",
                                  side_effect=lambda a: calls.append(a.phone)), \
                mock.patch.object(signin, "_acquire_run_lock", return_value=None), \
                mock.patch.object(signin, "_save_cred_state"), \
                mock.patch.object(signin, "_maybe_alert_zero_success", return_value=False), \
                mock.patch.object(signin, "_flush_admin_mail_summary"), \
                mock.patch.object(signin.time, "sleep"):
            try:
                signin.main([])
                code = 0
            except SystemExit as e:
                code = e.code
        return code, calls

    def test_exit_code_follows_recorded_conclusion(self):
        for recorded, want, why in (
            (signin.STATUS_SKIPPED_WINDOW, 2, "窗口外跳过 → 待补签，不是失败"),
            (signin.STATUS_NO_POSITION, 2, "无点位 → 跳过桶，补签闸门仍会重跑"),
            (signin.STATUS_FAILED, 1, "真失败必须继续可见"),
            (signin.STATUS_SUCCESS, 0, "当日已签到 → 无需处理，也不该报失败"),
        ):
            with self.subTest(recorded=recorded):
                code, calls = self._run_main(recorded)
                self.assertEqual(calls, [], "窗口已关：一个真实请求都不该发")
                self.assertEqual(code, want, why)


class ManualChainWindowGuardTest(unittest.TestCase):
    """M11 残留：手动链路（schedule 为空）逐账号窗口钳制。

    兜底常驻（workers）与补签轮走 `run_queue_retry` 的手动分支（schedule 为空）。
    它们只在**每轮扫描前**判过窗口，一轮扫描内部不再按账号判——一轮在 07:49 起跑、
    末尾几个账号要在 07:50 之后才发请求时，仍会发起真实登录。修复后：手动分支
    每次"准备发请求"前判窗口，已关则剩余账号全部落 `skipped_window` 并停手。
    `--only` 手动签到**有意不受限**（用户主动触发应放行），由 `window_guard`
    开关区分（兜底/补签传 True，--only 不传）。
    """

    PHONE_IN = "13800000001"
    PHONE_OUT = "13800000002"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-manual-win-")
        self.env = dict(os.environ)
        os.environ["YIBAN_STATE_DIR"] = self.tmp
        os.environ["YIBAN_SIGN_START"] = "06:30"
        os.environ["YIBAN_SIGN_END"] = "07:50"
        os.environ.pop("YIBAN_SECOND_RUN", None)
        os.environ.pop("YIBAN_GLOBAL_PAUSE", None)
        # 与各用例 mock 的时钟日期一致（固定 2026-09-17，不随运行日漂移）
        self.state_path = os.path.join(self.tmp, "sign-state-2026-09-17.json")

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.env)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _accs(self):
        return [signin.Account(phone=self.PHONE_IN, password="p"),
                signin.Account(phone=self.PHONE_OUT, password="p")]

    def test_manual_chain_stops_at_window_close(self):
        """窗口在轮内关闭：第二个账号零请求、落 skipped_window。"""
        state = {"t": _dt.datetime(2026, 9, 17, 7, 0)}  # 窗口内
        calls = []

        def _now():
            return state["t"]

        def _attempt(acc):
            calls.append(acc.phone)
            state["t"] = _dt.datetime(2026, 9, 17, 7, 55)  # 首个账号执行后窗口关闭
            return (True, "ok", False, "success")

        with mock.patch.object(signin.clock, "now", _now), \
                mock.patch.object(signin, "attempt_signin", side_effect=_attempt), \
                mock.patch.object(signin, "_update_cred_state"), \
                mock.patch.object(signin.time, "sleep"):
            results = signin.run_queue_retry(self._accs(), "", 0, 0, schedule=None,
                                             window_guard=True)
        self.assertEqual(calls, [self.PHONE_IN],
                         "窗口关闭后不得再对剩余账号发起真实登录")
        with open(self.state_path, encoding="utf-8") as f:
            st = json.load(f)[self.PHONE_OUT]["status"]
        self.assertEqual(st, signin.STATUS_SKIPPED_WINDOW,
                         "窗口已关的剩余账号应落 skipped_window")
        self.assertEqual(results[self.PHONE_OUT][3], signin.STATUS_SKIPPED_WINDOW)

    def test_only_mode_ignores_window_guard(self):
        """--only（window_guard=False）：窗口外仍执行（用户主动触发放行）。"""
        calls = []

        def _now():
            return _dt.datetime(2026, 9, 17, 7, 55)  # 全程窗口外

        def _attempt(acc):
            calls.append(acc.phone)
            return (True, "ok", False, "success")

        with mock.patch.object(signin.clock, "now", _now), \
                mock.patch.object(signin, "attempt_signin", side_effect=_attempt), \
                mock.patch.object(signin, "_update_cred_state"), \
                mock.patch.object(signin.time, "sleep"):
            results = signin.run_queue_retry(self._accs(), "", 0, 0, schedule=None,
                                             window_guard=False)
        self.assertEqual(calls, [self.PHONE_IN, self.PHONE_OUT],
                         "--only 手动签到不受窗口限制，两个账号都应执行")
        self.assertEqual(results[self.PHONE_OUT][3], "success",
                         "不受限路径执行成功，不得被标成 skipped_window")

class SecondRunDropDoneTest(unittest.TestCase):
    """补签轮剔除已完成账号时，no_task 也算"已了结"。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-win2-")
        self.env = dict(os.environ)
        os.environ["YIBAN_STATE_DIR"] = self.tmp
        self.state_path = os.path.join(
            self.tmp, f"sign-state-{signin.clock.now().strftime('%Y-%m-%d')}.json")

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.env)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_no_task_is_treated_as_done(self):
        with open(self.state_path, "w", encoding="utf-8") as f:
            json.dump({PHONE: {"status": signin.STATUS_NO_TASK}}, f)
        kept = signin._second_run_drop_done([signin.Account(phone=PHONE, password="p")])
        self.assertEqual([a.phone for a in kept], [], "no_task 当日已了结，补签轮不应重登")

    def test_failed_is_kept_for_retry(self):
        with open(self.state_path, "w", encoding="utf-8") as f:
            json.dump({PHONE: {"status": signin.STATUS_FAILED}}, f)
        kept = signin._second_run_drop_done([signin.Account(phone=PHONE, password="p")])
        self.assertEqual([a.phone for a in kept], [PHONE], "失败账号必须保留待补签")


sys.path.insert(0, os.path.join(BASE, "scripts"))


class _HostClock(_dt.datetime):
    """宿主墙钟替身：固定在一个**窗口外**的时刻（模拟 UTC 主机的北京 06:40）。"""

    _fixed = _dt.datetime(2026, 1, 1, 22, 40, 0)

    @classmethod
    def now(cls, tz=None):
        return cls._fixed


class ClockTest(unittest.TestCase):
    def test_now_is_beijing_plus_eight(self):
        """now() 恒为 UTC+8，与宿主 TZ 无关（本用例在任何时区的主机上都应通过）。"""
        delta = clock.now() - _dt.datetime.utcnow()
        self.assertAlmostEqual(delta.total_seconds(), 8 * 3600, delta=5)

    def test_now_is_naive(self):
        """naive：既有全部 datetime 运算都与 naive 混用，带 tzinfo 会直接 TypeError。"""
        self.assertIsNone(clock.now().tzinfo)

    def test_today_and_ts_formats(self):
        self.assertEqual(clock.today(), clock.now().strftime("%Y-%m-%d"))
        self.assertEqual(len(clock.ts()), 19)
        self.assertEqual(clock.ts()[:10], clock.today())

    def test_session_cache_clock_delegates_to_single_source(self):
        """db 的会话缓存时钟（原为独立实现的固定 +8）已收口到唯一时钟。"""
        import db
        self.assertLess(abs((db._session_cache_now() - clock.now()).total_seconds()), 5)


class WindowDecisionUsesBeijingClockTest(unittest.TestCase):
    """窗口判定必须只认业务钟（核心回归）。"""

    PHONE = "13800138000"

    def setUp(self):
        for k in ("YIBAN_RETRY_MIN_INTERVAL", "YIBAN_SIGN_START", "YIBAN_SIGN_END",
                  "YIBAN_WINDOW_EDGE_SEC", "YIBAN_MIN_EXEC_GAP", "YIBAN_EXEC_GAP_MIN"):
            os.environ.pop(k, None)

    def _run_with_clocks(self, beijing_now):
        """宿主墙钟固定在窗口外，业务钟固定在 beijing_now，返回 attempt_signin 的调用次数。"""
        acc = signin.Account(phone=self.PHONE, password="p")
        sched = {self.PHONE: beijing_now - _dt.timedelta(minutes=20)}
        with mock.patch.object(signin.clock, "now", lambda: beijing_now), \
                mock.patch.object(signin, "datetime", _HostClock), \
                mock.patch.object(signin, "attempt_signin",
                                  return_value=(True, "ok", False, signin.STATUS_SUCCESS)) as attempt, \
                mock.patch.object(signin, "_write_sign_state"), \
                mock.patch.object(signin, "_update_cred_state"), \
                mock.patch.object(signin.time, "sleep"), \
                mock.patch.object(signin.time, "monotonic", return_value=100.0):
            signin.run_queue_retry([acc], "", 0, 0, schedule=sched)
        return attempt

    def test_window_decision_follows_beijing_clock(self):
        """北京 06:40（窗口内）+ 宿主墙钟 22:40（窗口外）→ 必须发起签到。

        判定若取宿主钟（旧实现），会判"时段已结束"而整轮零请求——这正是本用例要防的。
        """
        beijing_0640 = clock.now().replace(hour=6, minute=40, second=0, microsecond=0)
        attempt = self._run_with_clocks(beijing_0640)
        self.assertEqual(attempt.call_count, 1,
                         "窗口判定取错了钟（宿主墙钟），北京时间 06:40 被误判为已结束")

    def test_window_still_closes_after_deadline(self):
        """不是"永不判关闭"：北京 08:10（已过 eff_hi）应判结束、零请求。"""
        beijing_0810 = clock.now().replace(hour=8, minute=10, second=0, microsecond=0)
        attempt = self._run_with_clocks(beijing_0810)
        self.assertEqual(attempt.call_count, 0, "已过窗口仍发起请求")

    def test_window_closed_uses_beijing_end(self):
        cfg = signin._schedule_config()
        self.assertFalse(signin._window_closed(cfg, clock.now().replace(hour=7, minute=0)))
        self.assertTrue(signin._window_closed(cfg, clock.now().replace(hour=9, minute=0)))


class LogAndStateDateUseBeijingTest(unittest.TestCase):
    """按日留痕（日志文件名 / 状态文件 / 事件时间戳）也走同一时钟。"""

    def test_signin_log_path_uses_beijing_date(self):
        """日志按日切分：宿主为 UTC 时若用宿主日期，北京时间 00:00–08:00 会写进前一日。"""
        fake_bj = _dt.datetime(2026, 3, 2, 1, 30, 0)  # 北京凌晨 = UTC 前一日 17:30
        with mock.patch.object(clock, "now", return_value=fake_bj):
            self.assertEqual(clock.today(), "2026-03-02")
            self.assertEqual(clock.ts(), "2026-03-02 01:30:00")


class SlotBoundaryTest(unittest.TestCase):
    """F4：非 5 分钟整数倍窗口的最后一个自选片必须被尊重。"""

    def setUp(self):
        # 06:30 ~ 07:52（L=82，非 5 的整数倍）；前后裁剪各 60s
        os.environ["YIBAN_SIGN_START"] = "06:30"
        os.environ["YIBAN_SIGN_END"] = "07:52"
        os.environ["YIBAN_WINDOW_EDGE_FRONT_SEC"] = "60"
        os.environ["YIBAN_WINDOW_EDGE_BACK_SEC"] = "60"
        os.environ["YIBAN_ALLOW_TIME_PREF"] = "1"
        os.environ.pop("YIBAN_SIGN_ORDER", None)
        os.environ.pop("YIBAN_SIGN_DIST", None)
        os.environ.pop("YIBAN_SIGN_MODE", None)

    def tearDown(self):
        for k in ("YIBAN_SIGN_START", "YIBAN_SIGN_END", "YIBAN_WINDOW_EDGE_FRONT_SEC",
                  "YIBAN_WINDOW_EDGE_BACK_SEC", "YIBAN_ALLOW_TIME_PREF"):
            os.environ.pop(k, None)

    def _acc(self, phone):
        return signin.Account(phone=phone, password="pw")

    def test_last_ui_selectable_slot_is_respected(self):
        """slot=80（07:50，UI 可点选）必须被调度器采纳，而不是静默回退自动分配。"""
        base = datetime(2026, 8, 28, 0, 0)
        prefs = {"13900000001": {"slot_min": 80, "updated_at": "2026-08-01 00:00:00"}}
        schedule = signin.build_schedule(
            [self._acc("13900000001")], prefs=prefs, now=base
        )
        self.assertIn("13900000001", schedule)
        t = schedule["13900000001"]
        # 选中片 = 07:50 起的块（[07:50:00, 07:52:00)，有效窗口后裁到 07:51:00）
        self.assertGreaterEqual(
            t, base.replace(hour=7, minute=50),
            f"自选 07:50 片被静默丢弃，实际排到 {t:%H:%M:%S}",
        )
        self.assertLess(t, base.replace(hour=7, minute=52))

    def test_out_of_window_slot_still_falls_back(self):
        """真正落在窗口外的片（slot=90 > L=82）仍应回退自动分配，不崩。"""
        base = datetime(2026, 8, 28, 0, 0)
        prefs = {"13900000002": {"slot_min": 90, "updated_at": "2026-08-01 00:00:00"}}
        schedule = signin.build_schedule(
            [self._acc("13900000002")], prefs=prefs, now=base
        )
        self.assertIn("13900000002", schedule)  # 回退自动分配，账号仍被排上
        t = schedule["13900000002"]
        self.assertGreaterEqual(t, base.replace(hour=6, minute=31))
        self.assertLess(t, base.replace(hour=7, minute=52))

    def test_mid_window_slot_unchanged(self):
        """窗口中部常规自选片行为不变（回归保护）。"""
        base = datetime(2026, 8, 28, 0, 0)
        prefs = {"13900000003": {"slot_min": 40, "updated_at": "2026-08-01 00:00:00"}}
        schedule = signin.build_schedule(
            [self._acc("13900000003")], prefs=prefs, now=base
        )
        self.assertIn("13900000003", schedule)
        t = schedule["13900000003"]
        # slot=40 → 07:10 起的块
        self.assertGreaterEqual(t, base.replace(hour=7, minute=10))
        self.assertLess(t, base.replace(hour=7, minute=15))


if __name__ == "__main__":
    unittest.main(verbosity=2)
