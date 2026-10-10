# -*- coding: utf-8 -*-
"""e2e：容量预检的总阈值按**生效执行体数 K** 缩放 + 出口预算告警（工单 5wjy）。

标签：I · 容量、熔断与账号有效性
覆盖：`runner.main` 的容量预检在多执行体下的**总阈值**口径与出口预算告警。
驱动：完整跑一遍 `runner.main`（真清单解析、真旧键口径、真容量公式、真告警接线），
      只打桩时钟、库与网络。K 的取法与派发同源，不另传参数。
外部内容：无（不联网、不写真实库、不读真实 `.env`）。

判据（工单 5wjy 原文 + 主会话修法定案 D1/D2）：
1. 清单在场：K=2、183 个账号 ⇒ 静默（现状误报 183 > 107）。
2. 清单在场：K=6、1,200 个账号 ⇒ 静默（波期每天的形状）。
3. 清单缺失（旧 worker 口径）：`YIBAN_WORKERS=2` ⇒ K=2、183 个账号 ⇒ 静默。
4. 清单缺失 + 单 worker ⇒ K=1、旧值逐值不变（107 / 122 告警 / 89 静默）。
5. 非子进程（没派发）⇒ 生效数恒 1，即使进程环境里 `YIBAN_WORKERS=2`。
6. 真超容量时告警仍响（K=1/500、K=2/1000）。
7. 出口预算不足时多报一条（K=6、出口速率 0.1、声明出口 1 ⇒ 需要 5）。

数值底稿（有效窗口 06:31~07:49，06:40 起跑 ⇒ 剩余 4140s，avg=3，gap=10）：
- 计划口径单执行体 = `capacity_accounts(4140, 10, 3)` = (4140-3)//13+1 = 319。
- reserve 口径单执行体 = `capacity_accounts(4140, 36, 3)` = (4140-3)//39+1 = 107。
"""
import logging
import os
import shutil
import tempfile
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest import mock

from yiban import egress
from yiban.engine import runner as runner_mod
from yiban.engine import schedule

DAY = "2026-09-22"
#: 固定起跑时刻：默认窗口 06:30~07:50（有效窗口 06:31~07:49），06:40 在窗口内
START = datetime(2026, 9, 22, 6, 40, 0)
#: 剩余有效窗口（秒）
REST_SEC = 4140
#: 计划口径（不折重试储备）的单执行体阈值
PLAN_SINGLE = schedule.capacity_accounts(REST_SEC, 10, 3)
#: reserve 口径（折进重试储备）的单执行体阈值
RESERVE_SINGLE = schedule.capacity_accounts(REST_SEC, 36, 3)
#: K≥2 分支下"真超容量"的账号数（K=2 总阈值 638）
K2_OVER_N = 1000
#: 出口预算不足告警的主题（与 runner 的实现逐字一致）
BUDGET_TITLE = "易班签到出口预算不足"


class _FakeClock:
    def __init__(self, t=START):
        self.t = t

    def now(self):
        return self.t


class _WarnCapture(logging.Handler):
    """收集 `yiban` 日志通道上的 WARNING 原文（已做 %-格式化）。"""

    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


class _Record:
    """一次 `runner.main` 的可观测结果。"""

    def __init__(self, code, cap_calls, ca_calls, notifies, warnings):
        self.code = code
        self.cap_calls = cap_calls      # capacity_of 的 (args, kwargs, out) 列表
        self.ca_calls = ca_calls        # capacity_accounts 的 (args, kwargs, out) 列表
        self.notifies = notifies        # notify_admin_entry 的 (args, kwargs) 列表
        self.warnings = warnings        # WARNING 原文列表

    def precheck_lines(self):
        return [w for w in self.warnings if "容量预检" in w]

    def titles(self):
        return [a[0] for a, _kw in self.notifies]


class RunnerPrecheckScaleByKE2ETest(unittest.TestCase):
    """多执行体下预检总阈值 = K × 单执行体计划容量（K 与派发同源）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-precheck-k-")
        self._env = {k: os.environ.get(k) for k in (
            "YIBAN_STATE_DIR", "YIBAN_DB_FILE", "YIBAN_LOG_FILE", "YIBAN_ENV_FILE",
            "YIBAN_SCHEDULER_V3", "YIBAN_GLOBAL_PAUSE", "YIBAN_SECOND_RUN",
            "YIBAN_EXECUTOR_ID", "YIBAN_EXECUTORS", "YIBAN_WORKERS",
            "YIBAN_EGRESS_RATE", "YIBAN_ACCOUNT_GAP_MAX", "YIBAN_AVG_ATTEMPT_SEC")}
        os.environ.update({
            "YIBAN_STATE_DIR": self.tmp,
            "YIBAN_DB_FILE": os.path.join(self.tmp, "yiban.db"),
            "YIBAN_LOG_FILE": os.path.join(self.tmp, "sign.log"),
        })
        for k in ("YIBAN_SCHEDULER_V3", "YIBAN_GLOBAL_PAUSE", "YIBAN_SECOND_RUN",
                  "YIBAN_EXECUTOR_ID", "YIBAN_EXECUTORS", "YIBAN_WORKERS",
                  "YIBAN_EGRESS_RATE", "YIBAN_ACCOUNT_GAP_MAX", "YIBAN_AVG_ATTEMPT_SEC"):
            os.environ.pop(k, None)
        self.fc = _FakeClock()
        self._p = mock.patch.object(runner_mod.clock, "now", self.fc.now)
        self._p.start()
        self.addCleanup(self._p.stop)

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def _cfg(**over):
        cfg = {
            "order": "sequence", "dist": "uniform",
            "edge_front_sec": 60, "edge_back_sec": 60,
            "block_cap": 15, "mu_min_pct": 40, "mu_max_pct": 60,
            "sigma_min_pct": 15, "sigma_max_pct": 25, "min_exec_gap": 5,
            "avg_attempt_sec": 3, "retry_min_interval": 60, "exec_gap_min": 10,
            "allow_time_pref": 0, "sign_start": (6, 30), "sign_end": (7, 50),
            "bucket_rate": 1.0, "executors": ["single@testhost"],
        }
        cfg.update(over)
        return cfg

    def _run(self, *, n, k=0, workers=None, rate=None, child=True, proxies=None,
             slots=None, gap=None):
        """跑一轮 `runner.main`。

        k：清单 worker 行数（0 = 清单缺失）。
        workers：进程环境里的 `YIBAN_WORKERS`（None = 不设）。
        rate：进程环境里的 `YIBAN_EGRESS_RATE`（None = 不设，即缺省 1.0）。
        child：是否注入 `YIBAN_EXECUTOR_ID`（True = 监督进程拉起的执行体子进程）。
        proxies：清单各行的出口串（默认全空 = 全直连）。
        slots：清单各行的槽位号（默认 0..k-1；传了才建**不连续**槽位）。
        gap：`YIBAN_ACCOUNT_GAP_MAX`（None = 不设，即缺省 10）。
        """
        cfg = self._cfg()
        accounts = [SimpleNamespace(phone=f"1380{i:07d}", user_paused=False, owner="u@1")
                    for i in range(n)]
        sched = {a.phone: START for a in accounts}
        cap_calls = []
        ca_calls = []
        real_cap = schedule.capacity_of
        real_ca = schedule.capacity_accounts

        def cap_spy(*a, **kw):
            out = real_cap(*a, **kw)
            cap_calls.append((a, kw, out))
            return out

        def ca_spy(*a, **kw):
            out = real_ca(*a, **kw)
            ca_calls.append((a, kw, out))
            return out

        # 清单：k 行 worker（默认槽位 0..k-1、出口全直连 ⇒ 声明出口数 = 1）
        proxies = list(proxies) if proxies else [""] * k
        slots = list(slots) if slots else list(range(k))
        if k:
            os.environ["YIBAN_EXECUTORS"] = egress.dump_manifest(
                [{"slot": slots[i], "type": egress.TYPE_WORKER, "proxy": proxies[i]}
                 for i in range(k)])
        else:
            os.environ.pop("YIBAN_EXECUTORS", None)
        if gap is None:
            os.environ.pop("YIBAN_ACCOUNT_GAP_MAX", None)
        else:
            os.environ["YIBAN_ACCOUNT_GAP_MAX"] = str(gap)
        if workers is None:
            os.environ.pop("YIBAN_WORKERS", None)
        else:
            os.environ["YIBAN_WORKERS"] = str(workers)
        if rate is None:
            os.environ.pop("YIBAN_EGRESS_RATE", None)
        else:
            os.environ["YIBAN_EGRESS_RATE"] = str(rate)
        if child:
            # 身份由监督进程注入；不设它 `runner.main` 会先走监督派发、到不了预检
            os.environ["YIBAN_EXECUTOR_ID"] = egress.worker_owner(0)
        else:
            os.environ.pop("YIBAN_EXECUTOR_ID", None)

        notifies = []
        capture = _WarnCapture()
        logging.getLogger("yiban").addHandler(capture)
        self.addCleanup(logging.getLogger("yiban").removeHandler, capture)
        with mock.patch.object(runner_mod.accounts_mod, "load_accounts",
                               return_value=accounts), \
             mock.patch.object(runner_mod.schedule_mod, "build_schedule",
                               return_value=sched), \
             mock.patch.object(runner_mod.schedule_mod, "_schedule_config",
                               side_effect=lambda: cfg), \
             mock.patch.object(runner_mod.schedule_mod, "capacity_of", cap_spy), \
             mock.patch.object(runner_mod.schedule_mod, "capacity_accounts", ca_spy), \
             mock.patch.object(runner_mod.executor_v3, "run_executor_v3",
                               return_value={a.phone: (True, "ok", False, "success")
                                             for a in accounts}), \
             mock.patch.object(runner_mod.state_io, "_load_cred_state",
                               return_value={}), \
             mock.patch.object(runner_mod.state_io, "_save_cred_state"), \
             mock.patch.object(runner_mod.state_io, "_is_second_run",
                               return_value=False), \
             mock.patch.object(runner_mod.state_io, "_write_sched_done"), \
             mock.patch.object(runner_mod.db, "add_sign_events_batch"), \
             mock.patch.object(runner_mod.db, "purge_expired_deleted_accounts"), \
             mock.patch.object(runner_mod.alerts, "_maybe_alert_zero_success"), \
             mock.patch.object(runner_mod.alerts, "_flush_admin_mail_summary"), \
             mock.patch.object(runner_mod.alerts, "notify_admin_entry",
                               side_effect=lambda *a, **kw: notifies.append((a, kw))):
            code = runner_mod.main([])
        return _Record(code, cap_calls, ca_calls, notifies, list(capture.lines))

    # -- 底稿自检：数值不靠猜 ----------------------------------------------

    def test_fixture_numbers(self):
        """底稿：rest=4140、avg=3、gap=10 ⇒ 计划 319、reserve 107。"""
        self.assertEqual(PLAN_SINGLE, 319)
        self.assertEqual(RESERVE_SINGLE, 107)

    # -- 判据 1/2：清单在场，K 取拉起列表长度 ------------------------------

    def test_k2_183_silent(self):
        """K=2、183 个账号：总阈值 638 > 183 ⇒ 静默（现状误报 183 > 107）。"""
        r = self._run(n=183, k=2)
        self.assertEqual(r.code, 0)
        self.assertEqual(r.notifies, [], "183 未超 K=2 总阈值，必须静默")
        self.assertEqual(r.precheck_lines(), [], "未超容量不得再打容量预检告警")
        self.assertEqual(r.cap_calls, [], "K≥2 分支不得再按单执行体 reserve 口径取值")
        self.assertEqual(len(r.ca_calls), 1, "K≥2 分支按计划口径取一次单执行体容量")
        _args, kw, out = r.ca_calls[0]
        self.assertEqual(out, PLAN_SINGLE)
        self.assertEqual(kw.get("gap"), 10)
        self.assertEqual(kw.get("avg"), 3)
        self.assertGreater(2 * out, 183, "K=2 总阈值必须容得下 183")

    def test_k6_1200_silent(self):
        """K=6、1,200 个账号：总阈值 1914 > 1200 ⇒ 静默（波期每天）。"""
        r = self._run(n=1200, k=6)
        self.assertEqual(r.code, 0)
        self.assertEqual(r.notifies, [], "1200 未超 K=6 总阈值，必须静默")
        self.assertEqual(r.precheck_lines(), [])
        self.assertEqual(r.cap_calls, [])
        self.assertEqual(len(r.ca_calls), 1)
        _args, _kw, out = r.ca_calls[0]
        self.assertEqual(out, PLAN_SINGLE)
        self.assertGreater(6 * out, 1200, "K=6 总阈值必须容得下 1200")

    # -- 判据 3：清单缺失时按旧 worker 口径数生效数（D1） ------------------

    def test_no_manifest_legacy_workers2_silent(self):
        """清单缺失 + `YIBAN_WORKERS=2` ⇒ K=2、183 个账号 ⇒ 静默（D1 回归点）。"""
        r = self._run(n=183, k=0, workers=2)
        self.assertEqual(r.code, 0)
        self.assertEqual(r.notifies, [], "旧 worker 口径下 K=2 总阈值 638 > 183，必须静默")
        self.assertEqual(r.precheck_lines(), [])
        self.assertEqual(r.cap_calls, [], "K≥2 分支不得再按单执行体 reserve 口径取值")
        self.assertEqual(len(r.ca_calls), 1)
        _args, _kw, out = r.ca_calls[0]
        self.assertEqual(out, PLAN_SINGLE)
        self.assertGreater(2 * out, 183)

    def test_no_manifest_legacy_workers4_over_warns(self):
        """清单缺失 + `YIBAN_WORKERS=4`、1000 个账号 ⇒ 总阈值 1276 > 1000 静默；
        1500 个账号 ⇒ 告警（证明旧口径真参与比较，不是恒静默）。"""
        r = self._run(n=1000, k=0, workers=4)
        self.assertEqual(r.notifies, [], "1000 < 4×319=1276，必须静默")
        r2 = self._run(n=1500, k=0, workers=4)
        self.assertEqual(len(r2.notifies), 1, "1500 > 1276，必须告警")
        self.assertGreater(4 * PLAN_SINGLE, 1000)
        self.assertLess(4 * PLAN_SINGLE, 1500)

    # -- 判据 4：清单缺失 + 单 worker ⇒ 旧值逐值不变 -----------------------

    def test_no_manifest_single_worker_unchanged(self):
        """清单缺失 + 单 worker（无 `YIBAN_WORKERS`）⇒ K=1、阈值 107 逐值不变。"""
        r = self._run(n=122, k=0)
        self.assertEqual(len(r.notifies), 1, "122 账号越过阈值 107，必须告警")
        self.assertEqual(len(r.cap_calls), 1, "K=1 必须走 reserve 口径（capacity_of）")
        args, kw, out = r.cap_calls[0]
        self.assertEqual(args, (float(REST_SEC),))
        self.assertEqual(kw.get("gap"), 10)
        self.assertEqual(kw.get("avg"), 3)
        self.assertIs(kw.get("enabled"), False)
        self.assertTrue(kw.get("retry_reserve"))
        self.assertNotIn("k", kw)
        self.assertNotIn("bucket_rate", kw)
        self.assertEqual(out, RESERVE_SINGLE)
        r89 = self._run(n=89, k=0)
        self.assertEqual(r89.notifies, [], "89 账号在阈值 107 之下必须静默")
        self.assertEqual(r89.precheck_lines(), [])

    # -- 判据 5：非子进程（没派发）⇒ 生效数恒 1 ----------------------------

    def test_non_child_ignores_workers_env(self):
        """没派发就没有多个执行体：进程环境 `YIBAN_WORKERS=2` 不得把 K 抬到 2。"""
        r = self._run(n=89, k=0, workers=2, child=False)
        self.assertEqual(len(r.cap_calls), 1, "非子进程必须走 K=1 的 reserve 口径")
        _args, _kw, out = r.cap_calls[0]
        self.assertEqual(out, RESERVE_SINGLE, "非子进程阈值必须逐值 = 107")

    # -- 判据 6：真超容量时告警仍响 ----------------------------------------

    def test_k2_over_capacity_still_warns(self):
        """K=2、1000 个账号：总阈值 638 < 1000 ⇒ 告警必须响，且文案按总口径。"""
        r = self._run(n=K2_OVER_N, k=2)
        self.assertEqual(len(r.notifies), 1, "真超总容量必须并入容量超载通知")
        lines = r.precheck_lines()
        self.assertEqual(len(lines), 1)
        line = lines[0]
        self.assertIn("部分账号可能无法在窗口内完成", line)
        self.assertIn("执行体", line, "K≥2 文案必须说明阈值是执行体合计口径")
        self.assertNotIn("预留 3 次", line, "K≥2 用计划口径，不折重试储备，不得照抄 reserve 文案")

    def test_k1_over_capacity_still_warns(self):
        """K=1、500 个账号：旧 reserve 阈值 107 < 500 ⇒ 告警仍响（防矫枉过正）。"""
        r = self._run(n=500, k=0)
        self.assertEqual(len(r.notifies), 1)
        lines = r.precheck_lines()
        self.assertEqual(len(lines), 1)
        self.assertIn("部分账号可能无法在窗口内完成", lines[0])
        self.assertIn("预留 3 次尝试的重试储备", lines[0])

    # -- 判据 7：出口预算不足多报一条（D2） --------------------------------

    def test_egress_budget_short_warns(self):
        """K=6、出口速率 0.1、声明出口 1 ⇒ 桶把 0.1 夹到 0.2 ⇒ 需要 3 ⇒ 多报一条。"""
        r = self._run(n=1200, k=6, rate=0.1)
        # 容量比较本身未越限（1200 < 1914）⇒ 不得出现"部分账号可能无法完成"那条
        self.assertNotIn("部分账号可能无法在窗口内完成",
                         "\n".join(r.precheck_lines()))
        self.assertEqual(r.titles(), [BUDGET_TITLE],
                         "出口预算不足必须多报一条，且不夹带容量超载")
        _a, kw = r.notifies[0]
        self.assertIs(kw.get("level"), runner_mod.alerts.ALERT_LEVEL_CRITICAL)
        body = " ".join(f"{k}={v}" for k, v in _a[1])
        self.assertIn("出口 1 < 需要 3", body,
                      "告警正文必须用**桶实际生效**的速率（0.1 被夹到 0.2 ⇒ 需要 3，不是 5）")
        lines = [w for w in r.warnings if "出口预算不足" in w]
        self.assertEqual(len(lines), 1, "日志必须留下同一条判据")
        self.assertIn("出口 1 < 需要 3", lines[0])
        self.assertIn("出口速率 0.2 尝试/s", lines[0], "日志里的速率也必须是夹取后的值")

    def test_egress_budget_ok_silent(self):
        """K=6、出口速率缺省 1.0 ⇒ 需要 1 = 声明 1 ⇒ 不报出口预算告警。"""
        r = self._run(n=1200, k=6)
        self.assertEqual(r.notifies, [], "出口预算够用时不得多报")
        self.assertEqual([w for w in r.warnings if "出口预算不足" in w], [])

    # -- D4：λ 必须与令牌桶同域（[RATE_MIN, RATE_MAX] = [0.2, 4.0]） --------

    def test_egress_lambda_clamped_at_low_end(self):
        """λ=0.05（低于桶下限 0.2）⇒ 按 0.2 算 ⇒ 需要 10（不是 40）：少报的错方向被堵。"""
        r = self._run(n=1200, k=6, gap=0, rate=0.05)
        self.assertEqual(r.titles(), [BUDGET_TITLE])
        body = " ".join(f"{k}={v}" for k, v in r.notifies[0][0][1])
        self.assertIn("需要 10", body, "λ 低于 RATE_MIN 时必须按 RATE_MIN 算")

    def test_egress_lambda_clamped_at_high_end(self):
        """λ=5（高于桶上限 4）⇒ 按 4 算 ⇒ K=13 时需要 2 ⇒ 报警（不夹取会漏报）。"""
        r = self._run(n=200, k=13, gap=0, rate=5)
        self.assertEqual(r.titles(), [BUDGET_TITLE],
                         "λ 高于 RATE_MAX 时按 RATE_MAX 算才是桶的真实行为")
        body = " ".join(f"{k}={v}" for k, v in r.notifies[0][0][1])
        self.assertIn("需要 2", body)

    # -- D3：声明出口数按**真实 worker 行**取，槽位号可以不连续 ------------

    def test_egress_declared_exits_follow_real_row_slots(self):
        """槽位不连续（0,5）与连续（0,1）在物理等价时必须同结论。

        口径：6 执行体...此处 2 行同出口 ⇒ 真声明出口 = 1；环按 0..K-1 枚举槽位会把
        占位槽当直连多算一个 ⇒ 声明数虚高 ⇒ 漏报。
        """
        # gap=0 ⇒ 单账号周期 3s；K=2、λ=0.5 ⇒ 需要 ceil(2/3/0.5) = 2 个出口
        contiguous = self._run(n=100, k=2, gap=0, rate=0.5,
                               proxies=["http://a:1", "http://a:1"], slots=[0, 1])
        sparse = self._run(n=100, k=2, gap=0, rate=0.5,
                           proxies=["http://a:1", "http://a:1"], slots=[0, 5])
        self.assertEqual(contiguous.titles(), [BUDGET_TITLE],
                         "连续槽位 0,1 同出口 ⇒ 声明 1 < 需要 2，必须报")
        self.assertEqual(sparse.titles(), [BUDGET_TITLE],
                         "不连续槽位 0,5 与 0,1 物理等价，结论必须相同（不得多算出口）")
        body = " ".join(f"{k}={v}" for k, v in sparse.notifies[0][0][1])
        self.assertIn("出口 1 < 需要 2", body, "真声明出口 = 1（两行同一个出口）")

    def test_egress_declared_exits_two_distinct_proxies_sparse(self):
        """不连续槽位 0,5 两个**不同**出口 ⇒ 声明 2 = 需要 2 ⇒ 不报。"""
        r = self._run(n=100, k=2, gap=0, rate=0.5,
                      proxies=["http://a:1", "http://b:1"], slots=[0, 5])
        self.assertEqual(r.titles(), [], "2 个不同出口 = 需要 2，不得多报")

    def test_egress_declared_exits_dedup_by_identity_not_raw_string(self):
        """D5：同址不同凭据的两行 worker 共桶 ⇒ 声明出口数 = 1（按出口标识去重）。

        去重键必须与限速预算的持久键同口径（`egress.egress_identity`：去 userinfo 的
        寻址形态）。按原始串去重会把 `http://u1:p1@a:3128` 与 `http://u2:p2@a:3128`
        当两个出口 ⇒ 声明数虚高 ⇒ 漏报。
        """
        r = self._run(n=100, k=2, gap=0, rate=0.5,
                      proxies=["http://u1:p1@a:3128", "http://u2:p2@a:3128"],
                      slots=[0, 1])
        self.assertEqual(r.titles(), [BUDGET_TITLE],
                         "同址不同凭据 = 同一个出口（1 < 需要 2），必须报")
        body = " ".join(f"{k}={v}" for k, v in r.notifies[0][0][1])
        self.assertIn("出口 1 < 需要 2", body, "同址不同凭据只算一个出口")

    def test_egress_declared_exits_direct_counts_once(self):
        """两行都直连 ⇒ 本机出口只有一条 ⇒ 声明出口数 = 1。"""
        r = self._run(n=100, k=2, gap=0, rate=0.5, proxies=["", ""], slots=[0, 1])
        self.assertEqual(r.titles(), [BUDGET_TITLE])
        body = " ".join(f"{k}={v}" for k, v in r.notifies[0][0][1])
        self.assertIn("出口 1 < 需要 2", body, "空出口（直连）只算本机一条")

    def test_egress_budget_counts_distinct_exits_not_rows(self):
        """声明出口数按**不同出口**数：6 行共用 2 个出口 ⇒ 2 < 需要 5 ⇒ 告警；
        6 行各一个出口 ⇒ 6 ≥ 需要 5 ⇒ 静默。"""
        shared = ["http://a:1", "http://a:1", "http://a:1",
                  "http://b:1", "http://b:1", "http://b:1"]
        distinct = [f"http://e{i}:1" for i in range(6)]
        r_shared = self._run(n=1200, k=6, rate=0.1, proxies=shared)
        self.assertEqual(r_shared.titles(), [BUDGET_TITLE],
                         "6 行只用 2 个出口 ⇒ 出口预算不足（2 < 5）")
        r_distinct = self._run(n=1200, k=6, rate=0.1, proxies=distinct)
        self.assertEqual(r_distinct.titles(), [],
                         "6 行各一个出口 ⇒ 6 ≥ 5，不得多报")


if __name__ == "__main__":
    unittest.main()
