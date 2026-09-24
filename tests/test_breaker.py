# -*- coding: utf-8 -*-
"""账密熔断器（circuit breaker）测试：v0.18.4 核心行为防回归。

标签：I · 容量、熔断与账号有效性
覆盖：账密熔断（连续 3 天失败→暂停 + 试探日、成功清除、网络类失败不计数）、`run_queue_retry` 的绕过与解冻、cred-state 的增量合并与跨进程锁、慢签到的耗时留痕与告警收敛
对应实现：`scripts/signin.py` 的 `attempt_signin` / `_load_cred_state` / `_save_cred_state` / `_write_sign_state` 与 cred-state 存储的 `update` / `clear`；web 侧编辑账号的清熔断路径
关键断言：同一天多次失败只计 1 天；试探日「登录已成功但窗口外 / 无 Range 被跳过」要解冻而非再冻 7 天，凭据类失败仍保持暂停并顺延试探日；调用方持有的空 dict 必须**就地**收到 `fail_days`（runner 收尾保存的正是同一个 dict，否则计数永不落盘）；旧快照不得复活被 Web 清掉的暂停；Web 清除与签到保存共用同一把文件锁，因而不可能交错
依赖：纯本地——临时目录与文件、`attempt_signin` 打桩（不联网）。既可 `pytest` 收集，也可 `python tests/test_breaker.py` 直接运行。无需 node

用法（在项目根目录）：
    py -m pytest tests/test_breaker.py -v        # 需要 pytest
    py tests/test_breaker.py                     # 无 pytest 也可直接运行

逐项明细：
- 凭据失败计数：连续 3 天 → 暂停 + 试探日；同一天多次失败只计 1 天
- 成功清除计数；网络类失败不计数
- run_queue_retry：暂停中零请求；--only 手动签到绕过；半开试探日执行并恢复
- web：编辑账号（改密码）清除 cred-state 暂停记录
"""
import contextlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock as mock
from datetime import datetime

import signin

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(0, os.path.join(BASE, "scripts"))

from yiban import cred_state  # noqa: E402


class BreakerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-brk-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write("YIBAN_ADMIN_USER=admin\nYIBAN_ADMIN_PASSWORD=TestPass1234!\n")
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_ACCOUNTS_KEY"] = "a" * 64
        os.environ["YIBAN_ACCOUNTS_FILE"] = os.path.join(cls.tmp, "accounts.json")
        os.environ["YIBAN_USERS_FILE"] = os.path.join(cls.tmp, "users.json")
        os.environ["YIBAN_DB_FILE"] = os.path.join(cls.tmp, "yiban.db")
        os.environ["YIBAN_STATE_DIR"] = cls.tmp
        os.environ["YIBAN_LOG_FILE"] = os.path.join(cls.tmp, "sign.log")
        global db, signin
        import db
        import signin
        with open(os.environ["YIBAN_ACCOUNTS_FILE"], "w", encoding="utf-8") as f:
            json.dump([], f)
        db.init_db(os.environ["YIBAN_DB_FILE"],
                   migrate_from=os.environ["YIBAN_ACCOUNTS_FILE"], env_file=cls.env_file)
        db.add_account({"name": "A", "phone": "13800138000", "password": "p1",
                        "status": "active", "owner": "admin"})

    @classmethod
    def tearDownClass(cls):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k in ("YIBAN_ENV_FILE", "YIBAN_ACCOUNTS_KEY", "YIBAN_ACCOUNTS_FILE",
                  "YIBAN_USERS_FILE", "YIBAN_DB_FILE", "YIBAN_STATE_DIR", "YIBAN_LOG_FILE"):
            os.environ.pop(k, None)

    # ---- 1. _update_cred_state 计数逻辑 ----
    # 周一~周三（2026-08-17/18/19），避开周日（周日签到开关默认关闭会提前退出）
    D1, D2, D3, D4 = "2026-08-17", "2026-08-18", "2026-08-19", "2026-08-20"

    def test_three_days_fail_pauses_with_probe(self):
        cs = {}
        signin._update_cred_state(cs, "13800138000", False, "登录失败: 账号或密码错误", self.D1)
        self.assertEqual(cs["13800138000"]["fail_days"], 1)
        signin._update_cred_state(cs, "13800138000", False, "登录失败: 账号或密码错误", self.D1)
        self.assertEqual(cs["13800138000"]["fail_days"], 1, "同一天重复失败只计 1 天")
        signin._update_cred_state(cs, "13800138000", False, "登录失败: 账号或密码错误", self.D2)
        self.assertEqual(cs["13800138000"]["fail_days"], 2)
        signin._update_cred_state(cs, "13800138000", False, "登录失败: 账号或密码错误", self.D3)
        cred = cs["13800138000"]
        self.assertEqual(cred["fail_days"], 3)
        self.assertEqual(cred["paused_since"], self.D3)
        self.assertEqual(cred["probe_date"], "2026-08-26", "暂停日 + 7 天 = 试探日")

    def test_success_clears_record(self):
        cs = {"13800138000": {"fail_days": 3, "paused_since": self.D3}}
        signin._update_cred_state(cs, "13800138000", True, "签到成功", self.D4)
        self.assertNotIn("13800138000", cs)

    def test_network_failure_not_counted(self):
        cs = {}
        signin._update_cred_state(cs, "13800138000", False, "Connection timed out", self.D4)
        self.assertNotIn("13800138000", cs, "网络类失败不应计数")

    # ---- 2. run_queue_retry / main 行为 ----
    def _run_main(self, fixed_dt, cred_state, only=False, attempt_result=None):
        class FakeDT(datetime):
            _f = fixed_dt

            @classmethod
            def now(cls, tz=None):
                return cls._f

        calls = self._calls

        def fake_login(self):
            self.logged_in = True

        def fake_signin(self):
            calls.append(self.account.phone)
            return (True, "签到成功", False, signin.STATUS_SUCCESS)

        argv = ["signin.py"] + (["--only", "13800138000"] if only else [])
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(signin.clock, "now", FakeDT.now))
            stack.enter_context(mock.patch.object(signin.YibanClient, "login_killyiban", fake_login))
            stack.enter_context(mock.patch.object(signin.YibanClient, "signin", fake_signin))
            if attempt_result is not None:
                # 直接指定 attempt_signin 的返回（credential failure 等场景）
                self._attempt_signin = stack.enter_context(
                    mock.patch.object(signin, "attempt_signin", return_value=attempt_result))
            stack.enter_context(mock.patch.object(signin.time, "sleep"))
            stack.enter_context(mock.patch.object(signin, "_load_cred_state", return_value=cred_state))
            # 捕获收尾保存的 dict（F5 回归据此断言"计数真的落盘了"）
            self._saved_cred_state = stack.enter_context(
                mock.patch.object(signin, "_save_cred_state"))
            stack.enter_context(mock.patch.object(sys, "argv", argv))
            # main() 可能以 SystemExit 收尾：吞掉它，本类断言的是调用序列与保存下来的 dict
            stack.enter_context(contextlib.suppress(SystemExit))
            signin.main()

    def setUp(self):
        self._calls = []
        self._saved_cred_state = None
        self._attempt_signin = None
        # 领取池由 conftest 的自动夹具统一清空（本类多条用例固定在同一天、同一账号
        # 上跑队列，池子会记住"当日已了结"而让后续用例领不到账号）。
        # ⚠ 注意：本类只能有一个 setUp——重复定义会静默覆盖，我在此踩过一次。

    def test_paused_account_zero_requests(self):
        cred = {"13800138000": {"fail_days": 3, "last_fail": self.D3,
                                 "paused_since": self.D3, "probe_date": "2026-08-26"}}
        self._run_main(datetime(2026, 8, 19, 6, 40), dict(cred))
        self.assertEqual(self._calls, [], "暂停中不应发起任何请求")

    def test_only_bypasses_pause(self):
        """标签：命令行 `--only <手机号>` 的手动签到路径——它不受熔断暂停约束，
        名字里的 only 指的是这个参数，不是「只有一个账号」。"""
        cred = {"13800138000": {"fail_days": 3, "last_fail": self.D3,
                                 "paused_since": self.D3, "probe_date": "2026-08-26"}}
        self._run_main(datetime(2026, 8, 19, 6, 40), dict(cred), only=True)
        self.assertEqual(self._calls, ["13800138000"], "--only 手动签到应绕过暂停")

    def test_probe_day_executes_and_recovers(self):
        cred = {"13800138000": {"fail_days": 3, "last_fail": self.D3,
                                 "paused_since": self.D3, "probe_date": "2026-08-26"}}
        self._run_main(datetime(2026, 8, 26, 6, 40), dict(cred))
        self.assertEqual(self._calls, ["13800138000"], "试探日应执行一次")

    def _run_probe_with_result(self, attempt_result):
        """试探日跑一次单账号队列，attempt_signin 返回指定结果，返回变动后的 cred_state。"""
        class FakeDT(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 8, 26, 6, 40)  # = probe_date，试探日

        cred = {"13800138000": {"fail_days": 3, "last_fail": self.D3,
                                 "paused_since": self.D3, "probe_date": "2026-08-26"}}
        acc = signin.Account(phone="13800138000", password="p")
        with mock.patch.object(signin.clock, "now", FakeDT.now), \
             mock.patch.object(signin, "attempt_signin", return_value=attempt_result), \
             mock.patch.object(signin, "_write_sign_state"), \
             mock.patch.object(signin.time, "sleep"):
            signin.run_queue_retry([acc], None, 0, 0, cred_state=cred)
        return cred

    def test_probe_day_window_skip_unfreezes(self):
        """试探日登录已成功但被签到时段跳过（窗口外）→ 凭据证实可用，应解冻而非再冻 7 天。"""
        cred = self._run_probe_with_result(
            (False, "未在签到时间内（08:00 ~ 22:00）", True, signin.STATUS_SKIPPED_WINDOW)
        )
        self.assertNotIn("13800138000", cred, "窗口跳过（登录已成功）应解除暂停")

    def test_probe_day_norange_skip_unfreezes(self):
        """试探日登录已成功但签到窗口缺失（Range 为空）→ 同样视为凭据可用，应解冻。"""
        cred = self._run_probe_with_result(
            (False, "签到时间窗口缺失（无 Range），已跳过", True, signin.STATUS_SKIPPED_NORANGE)
        )
        self.assertNotIn("13800138000", cred, "窗口缺失跳过（登录已成功）应解除暂停")

    def test_probe_day_real_credential_failure_stays_paused(self):
        """对照组：试探日仍是凭据类失败 → 保持暂停并顺延试探日（不受解冻修复影响）。"""
        cred = self._run_probe_with_result(
            (False, "登录失败: 账号或密码错误", False, signin.STATUS_FAILED)
        )
        self.assertIn("13800138000", cred, "凭据类失败应保持暂停")
        self.assertEqual(cred["13800138000"]["probe_date"], "2026-09-02",
                         "试探失败应顺延 7 天（8-26 + 7）")

    # ---- 2b. F5 回归：全新系统熔断计数必须写回调用方持有的 dict ----
    # 症状（2026-09-21 测试机 47 E2E）：`run_queue_retry` 里 `cred_state = cred_state
    # or {}` 对空 dict 重新绑定新对象——全新系统（cred-state.json 不存在，
    # `_load_cred_state()` 返回 {}）时轮内失败计数写进新 dict，runner/workers 收尾
    # 保存的仍是自己的空 dict：熔断计数永不落盘、"连续 3 天失败暂停"永不触发，
    # 错密码账号被每日无限次真实登录（易班侧照实计数，加重风控）。
    def test_fresh_system_fail_count_written_back_inplace(self):
        """单元级：调用方持有的空 dict（= 全新系统 read() 的返回值）必须就地收到
        fail_days——runner/workers 收尾保存的正是这个 dict。"""
        class FakeDT(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 8, 17, 6, 40)  # 周一 D1，窗口 06:30~07:50 内

        # 全新系统：状态文件不存在 → 读入口返回空 dict；调用方持有该引用
        state_path = signin._cred_state_path()
        if os.path.exists(state_path):
            os.remove(state_path)
        cred = signin._load_cred_state()
        self.assertEqual(cred, {}, "前置条件：全新系统读到的应为空 dict")
        acc = signin.Account(phone="13800138000", password="p")
        with mock.patch.object(signin.clock, "now", FakeDT.now), \
             mock.patch.object(
                 signin, "attempt_signin",
                 return_value=(False, "登录失败: 账号或密码错误", False, signin.STATUS_FAILED)), \
             mock.patch.object(signin, "_write_sign_state"), \
             mock.patch.object(signin.time, "sleep"):
            signin.run_queue_retry([acc], None, 0, 0, cred_state=cred)
        self.assertIn("13800138000", cred,
                      "轮内失败计数必须写进调用方持有的 dict（收尾据此落盘）")
        self.assertEqual(cred["13800138000"]["fail_days"], 1)

    def test_full_run_fresh_system_saves_fail_count(self):
        """runner 级：全新系统（_load_cred_state 返回 {}）跑一轮全量签到，
        收尾 _save_cred_state 收到的 dict 必须含 fail_days（否则计数永不落盘）。"""
        fresh = {}  # 全新系统：磁盘上没有任何熔断记录
        self._run_main(
            datetime(2026, 8, 17, 6, 40), fresh,
            attempt_result=(False, "登录失败: 账号或密码错误", False, signin.STATUS_FAILED),
        )
        self.assertEqual(self._attempt_signin.call_count, 1, "账号应真实执行一次")
        save_mock = self._saved_cred_state
        self.assertIsNotNone(save_mock, "收尾必须调用 _save_cred_state")
        self.assertTrue(save_mock.called, "收尾必须保存熔断状态")
        saved = save_mock.call_args[0][0]
        self.assertIn("13800138000", saved,
                      "全新系统的失败计数必须随收尾落盘（否则 3 天暂停永不触发）")
        self.assertEqual(saved["13800138000"]["fail_days"], 1)

    # ---- 3. web 编辑账号清除 cred-state ----
    def test_account_edit_clears_cred_state(self):
        spec = importlib.util.spec_from_file_location("webapp", os.path.join(BASE, "web", "app.py"))
        webapp = importlib.util.module_from_spec(spec)
        sys.modules["webapp"] = webapp
        spec.loader.exec_module(webapp)
        app = webapp.create_app()
        client = app.test_client()
        r = client.post("/api/login", json={"username": "admin", "password": "TestPass1234!"})
        self.assertEqual(r.status_code, 200)
        with client.session_transaction() as sess:
            sess["csrf_token"] = "t"
        state_path = os.path.join(self.tmp, "cred-state.json")
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump({"13800138000": {"fail_days": 3, "paused_since": self.D3}}, f)
        r = client.put("/api/accounts/0",
                       json={"name": "A", "phone": "13800138000", "password": "newpass1234",
                             "confirm_password": "TestPass1234!"},
                       headers={"X-CSRF-Token": "t"})
        self.assertEqual(r.status_code, 200, r.get_json())
        # 唯一入口的统一语义：清空后文件被删除（"无暂停 = 文件不存在"）
        cs = {}
        if os.path.exists(state_path):
            with open(state_path, encoding="utf-8") as f:
                cs = json.load(f)
        self.assertNotIn("13800138000", cs, "编辑账号（改密码）应清除暂停记录")

    # ---- 4. BOM 容错：带 BOM 的状态文件应正常读取 ----
    def test_load_cred_state_tolerates_bom(self):
        state_path = os.path.join(self.tmp, "cred-state.json")
        with open(state_path, "w", encoding="utf-8-sig") as f:
            json.dump({"13800138000": {"fail_days": 3}}, f)
        self.assertEqual(signin._load_cred_state()["13800138000"]["fail_days"], 3)

    # ---- 5. 空状态删除 + dur 耗时字段（2026-08-16，P5b/P6）----
    def test_save_cred_state_empty_removes_file(self):
        state_path = signin._cred_state_path()
        signin._save_cred_state({"13800138000": {"fail_days": 3}})
        self.assertTrue(os.path.exists(state_path), "有暂停记录时应存在")
        signin._save_cred_state({})
        self.assertFalse(os.path.exists(state_path), "无暂停记录时应删除文件（无暂停=不存在语义）")

    def test_write_sign_state_records_dur(self):
        signin._write_sign_state("13800138000", "success", "签到成功", dur=3.45)
        with open(os.path.join(self.tmp, "sign-state-" + datetime.now().strftime("%Y-%m-%d") + ".json"),
                  encoding="utf-8") as f:
            data = json.load(f)
        entry = data["13800138000"]
        self.assertEqual(entry["dur"], 3.45, "应记录单次尝试耗时（P6）")
        self.assertEqual(entry["status"], "success")

    # ---- 6. P6 耗时告警（2026-08-16）：超阈值 → warning + 并入汇总；每账号每轮最多 1 次 ----
    def test_slow_sign_warns_and_collects(self):
        """单次尝试耗时超阈值（31s > 30s）→ warning + 一条汇总条目，不发即时推送。"""
        import unittest.mock as mock

        accs = [signin.Account(phone="13800138001", password="p")]
        signin._mail_summary.clear()
        with mock.patch.object(signin, "time") as tm, \
             mock.patch.object(signin, "attempt_signin") as attempt, \
             mock.patch.object(signin, "_write_sign_state"), \
             mock.patch.object(signin, "_update_cred_state"), \
             mock.patch.object(signin.notify, "is_configured", return_value=True), \
             mock.patch.object(signin, "send_notification") as sn:
            tm.monotonic.side_effect = [100.0, 131.0]  # t0=100, last_done=131 → dur=31s
            tm.sleep = lambda *a, **k: None
            attempt.return_value = (True, "签到成功", False, signin.STATUS_SUCCESS)
            signin.run_queue_retry(accs, "http://notify.invalid", 0, 0)
        sn.assert_not_called()  # 耗时属"事后可读"的慢信号：只进汇总，不即时推送
        self.assertEqual(len(signin._mail_summary), 1, f"实际 {signin._mail_summary}")
        subject, fields = signin._mail_summary[0]
        self.assertIn("耗时", subject)
        by_label = dict(fields)
        self.assertIn("31.0", by_label["耗时"], "汇总条目应含实际耗时")
        self.assertEqual(by_label["账号"], "138****8001", "汇总条目应含脱敏账号")
        self.assertIn("签到成功", by_label["结果"], "汇总条目应含结果说明")

    def test_slow_sign_throttled_per_round(self):
        """同一账号两次慢尝试（失败重试）→ 耗时条目只收 1 条（防重试连击刷屏）。"""
        import unittest.mock as mock

        accs = [signin.Account(phone="13800138001", password="p")]
        signin._mail_summary.clear()
        # 两次尝试：31s / 32s 均超阈值；第 3 个采样点是重试回队时的间隔对齐探测
        # （gap_max=0 → 对齐差值必 ≤0，不产生等待，仅消耗一个时间点）
        seq = iter([100.0, 131.0, 150.0, 200.0, 232.0])
        with mock.patch.object(signin, "time") as tm, \
             mock.patch.object(signin, "attempt_signin") as attempt, \
             mock.patch.object(signin, "_write_sign_state"), \
             mock.patch.object(signin, "_update_cred_state"), \
             mock.patch.object(signin, "classify_failure", return_value=2) as cf, \
             mock.patch.object(signin.notify, "is_configured", return_value=True), \
             mock.patch.object(signin, "send_notification") as sn:
            tm.monotonic.side_effect = lambda: next(seq)
            tm.sleep = lambda *a, **k: None
            attempt.return_value = (False, "登录失败", False, signin.STATUS_FAILED)
            signin.run_queue_retry(accs, "http://notify.invalid", 0, 0)
        cf.assert_called()  # 失败确实走了分级
        # 2 次尝试 → 只有最终放弃那一条即时通知（耗时条目未连收）
        self.assertEqual(sn.call_count, 1, "只有最终放弃的失败通知一条即时推送")
        self.assertEqual(sn.call_args_list[0].args[0], "易班签到失败")
        self.assertEqual([s for s, _ in signin._mail_summary].count("易班签到耗时告警"), 1,
                         f"两次慢尝试只收一条耗时条目，实际 {signin._mail_summary}")
        self.assertIn("31.0", dict(signin._mail_summary[0][1])["耗时"])




P1, P2 = "13800138000", "13800138001"


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-cred-")
        self.env = dict(os.environ)
        os.environ["YIBAN_STATE_DIR"] = self.tmp

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.env)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, mapping):
        with open(cred_state.path(), "w", encoding="utf-8") as f:
            json.dump(mapping, f)

    def _disk(self):
        return cred_state.read()


class IncrementalMergeTest(_Base):
    """签到侧：内存快照不得整体覆盖磁盘（否则抹掉并发写入）。"""

    def test_merge_keeps_untouched_accounts(self):
        self._write({P1: {"fail_days": 9, "paused_since": "2026-09-01"}})
        # 签到进程跑完后只声明 P2 的变化（P1 是别人并发写的，必须原样保留）
        signin._save_cred_state(
            {P2: {"fail_days": 1, "last_fail": "2026-09-15"}}, touched={P2})
        disk = self._disk()
        self.assertEqual(disk[P1]["fail_days"], 9, "未处理账号的记录不得被覆盖")
        self.assertEqual(disk[P2]["fail_days"], 1)

    def test_merge_deletes_only_touched(self):
        """本次处理账号成功 → 删除其记录；其他账号不受影响。"""
        self._write({P1: {"fail_days": 9, "paused_since": "2026-09-01"},
                     P2: {"fail_days": 2}})
        signin._save_cred_state({P2: {"fail_days": 2}}, touched={P1})
        disk = self._disk()
        self.assertNotIn(P1, disk, "本次处理且已成功的账号应清除记录")
        self.assertEqual(disk[P2]["fail_days"], 2)

    def test_stale_snapshot_cannot_resurrect_pause(self):
        """主场景：Web 端清掉暂停后，签到收尾的旧快照不得把它写回来。"""
        self._write({P1: {"fail_days": 3, "paused_since": "2026-09-01"}})
        stale_snapshot = {P1: {"fail_days": 3, "paused_since": "2026-09-01"}}  # 启动时读到的
        cred_state.clear(P1)  # 用户改密 → Web 端清除
        # 签到进程收尾：它**没有处理** P1（不在 touched 里），旧快照不得写回
        signin._save_cred_state(stale_snapshot, touched=set())
        self.assertNotIn(P1, self._disk(), "运行期间清除的暂停被旧快照复活了")

    def test_legacy_full_replace_still_works(self):
        """兼容入口（touched=None）保持整体覆盖语义；空数据删除文件。"""
        self._write({P1: {"fail_days": 1}})
        signin._save_cred_state({P2: {"fail_days": 5}})
        disk = self._disk()
        self.assertNotIn(P1, disk)
        self.assertEqual(disk[P2]["fail_days"], 5)
        signin._save_cred_state({})
        self.assertFalse(os.path.exists(cred_state.path()),
                         "无记录 = 文件不存在（既有语义）")


class WebConcurrentEditTest(_Base):
    """Web 侧：清除熔断不得抹掉并发写入的其他账号记录。"""

    def _clear_via_web(self, phone):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "webapp", os.path.join(BASE, "web", "app.py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules.setdefault("webapp", mod)
        with mock.patch.dict(os.environ, {"YIBAN_STATE_DIR": self.tmp}):
            spec.loader.exec_module(mod)
        mod.clear_fuse_pause(phone)

    def test_web_clear_blocks_on_same_lock_as_signin(self):
        """Web 的清除与签到的保存共用同一把锁 → 两者不可能交错（根治手段）。

        验证方式：先在主线程持有该文件锁（等价于"签到进程正在保存"），再从另一线程
        调 Web 的清除——它必须**等锁**而不是直接读改写；释放后清除完成，
        且等待期间别的进程写进来的记录仍然在。
        """
        import threading

        from yiban.infra import locks
        self._write({P1: {"fail_days": 3, "paused_since": "2026-09-01"}})
        done = threading.Event()

        def _clear():
            self._clear_via_web(P1)
            done.set()

        with locks.file_lock(cred_state.path()):
            # 锁持有期间（签到在保存）：写入 P2 模拟其落盘结果
            disk = cred_state.read()
            disk[P2] = {"fail_days": 4, "paused_since": "2026-09-15"}
            with open(cred_state.path(), "w", encoding="utf-8") as f:
                json.dump(disk, f)
            t = threading.Thread(target=_clear, daemon=True)
            t.start()
            self.assertFalse(done.wait(0.3), "Web 清除必须在同一把锁上等待，不得绕过")
        self.assertTrue(done.wait(5), "释放锁后 Web 清除应完成")
        t.join(timeout=5)
        final = self._disk()
        self.assertNotIn(P1, final, "目标账号的暂停应被清除")
        self.assertIn(P2, final, "锁外写入的其他账号记录不得被 Web 覆盖")

    def test_clear_missing_entry_is_noop(self):
        """文件不存在/无该账号：静默无操作（用户每次编辑账号都会走这里）。"""
        self.assertFalse(cred_state.clear(P1))
        self._write({P1: {"fail_days": 1}})
        self.assertFalse(cred_state.clear(P2))
        self.assertIn(P1, self._disk())


class LockIsUsedTest(_Base):
    """整段读-改-写必须在同一把跨进程锁内完成。"""

    def test_update_holds_file_lock(self):
        from yiban.infra import locks
        self._write({P1: {"fail_days": 1}})
        seen = []
        real_lock = locks.file_lock

        def spy(path, *a, **kw):
            seen.append(os.path.basename(path))
            return real_lock(path, *a, **kw)

        with mock.patch.object(cred_state.locks if hasattr(cred_state, "locks") else locks,
                               "file_lock", side_effect=spy):
            cred_state.clear(P1)
        self.assertIn("cred-state.json", seen, "读-改-写须经统一文件锁原语")

    def test_signin_save_path_also_locks(self):
        from yiban.infra import locks
        seen = []
        real_lock = locks.file_lock

        def spy(path, *a, **kw):
            seen.append(os.path.basename(path))
            return real_lock(path, *a, **kw)

        with mock.patch.object(locks, "file_lock", side_effect=spy):
            signin._save_cred_state({P1: {"fail_days": 1}}, touched={P1})
        self.assertIn("cred-state.json", seen)


if __name__ == "__main__":
    unittest.main(verbosity=2)
