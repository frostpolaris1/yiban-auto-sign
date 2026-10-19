# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""崩溃语义（V-S3 写前标记 ＋ V-S2 定论路径）的校验者。

标签：B · 调度：领取/队列/执行体（执行核）
覆盖：`yiban/adapters/persistence/run_events.py` 的未定论读取面（`unresolved_starts`）、
     `yiban/engine/crash_semantics.py`（识别与告警、标记策略、定论判据）、
     `yiban/engine/executor_v3.py` 的写前标记调用点、`yiban/app/runner.py` 的
     协调器启动识别接线。

本文件钉的**不变式**（任何时刻都不该被违反，不钉调用顺序）：
  ① 写前标记先于真实请求：一次真实请求被发出时，该账号当日的 `start` 行**必已落库**；
     落库的 `start` 行数 ≥ 发出的真实请求数（本批策略=继续，见 ③）。
  ② 有标记无结果必被识别：`start` 行之后没有任何终态节点（`success`/`fail`/`pause`）
     时，识别把它列为 `unknown_outcome` 并出声；同一账号后续有了终态即不再算未定论。
  ③ 标记写入失败的行为**显式为"继续"**：观测表写不进时签到主流程照常（沿用既有契约
     "观测面坏了不得改变签到结论"），残余风险是有界且合法的（崩溃后重试是串行）。
  ④ 识别只告警、不自动重签：识别不改 `sign_tasks`、不发起任何真实请求。
  ⑤ V-S2 定论判据逐值成立：State ∈ {3,4}→只回填 already；==2→no_task；∈ {0,1}→可签；
     ==5→未定论；读不到（None/未知）→维持 unknown_outcome。

对应实现：`yiban/engine/crash_semantics.py`（本批新增）、`yiban/adapters/persistence/run_events.py`
     （读取面）、`yiban/app/runner.py`（接线）、`yiban/workers/workers.py`（多执行体监督
     进程接线）。定论判据的**单一事实源**是 `yiban/client.py` 的
     `SIGN_POSITION_STATE_STATUS`：本文件断言它被复用（改平台枚举即红）。
依赖：临时 SQLite（真实迁移链）+ 假时钟 + 限速替身 + `attempt_signin` 替身；
     不触网、不起子进程。
"""
import contextlib
import datetime
import os
import shutil
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import db  # pyproject.toml 的 pythonpath 把 scripts/ 加进 sys.path

from yiban import client as yiban_client
from yiban.adapters.persistence import run_events
from yiban.core import clock
from yiban.core import status as yiban_status
from yiban.engine import crash_semantics, executor_v3, token_bucket

TEST_KEY = "a" * 64
DAY = "2026-09-23"          # 周三：周末门不会拦下用例
START = datetime.datetime(2026, 9, 23, 6, 40, 0)
OWNER = "single@testhost"

NODE_START = "start"
NODE_SUCCESS = "success"
NODE_FAIL = "fail"
NODE_PAUSE = "pause"


def _phone(i):
    return f"1380000{i:04d}"


def _ts(**kw):
    return (START + datetime.timedelta(**kw)).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


class _FakeClock:
    def __init__(self, t=START):
        self.t = t
        self.mono = 10_000.0

    def now(self):
        return self.t

    async def sleep(self, sec):
        self.t += datetime.timedelta(seconds=sec)
        self.mono += sec
        import asyncio
        await asyncio.sleep(0)


def _cfg():
    return {
        "order": "sequence",
        "dist": "uniform",
        "edge_front_sec": 60,
        "edge_back_sec": 60,
        "block_cap": 15,
        "mu_min_pct": 40,
        "mu_max_pct": 60,
        "sigma_min_pct": 15,
        "sigma_max_pct": 25,
        "min_exec_gap": 5,
        "avg_attempt_sec": 3,
        "retry_min_interval": 60,
        "exec_gap_min": 10,
        "allow_time_pref": 0,
        "sign_start": (6, 30),
        "sign_end": (7, 50),
        "bucket_rate": 1.0,
        "executors": [OWNER],
    }


class _Limiter:
    manual = False

    def bucket(self, egress):
        return SimpleNamespace(retry_after=lambda now: 0.0)

    def acquire(self, egress, now):
        return True

    def on_success(self, egress):
        pass

    def on_risk_signal(self, egress, now):
        pass

    def persist(self, egress, stamp=None):
        return True

    def restore_from_store(self, egress, now=None):
        return False


class _Gate:
    def __init__(self, gap_sec=10.0):
        self.gap_sec = gap_sec

    def allow(self, phone, now):
        return True

    def commit(self, phone, now):
        pass


async def _never(_ctx):
    import asyncio
    await asyncio.Event().wait()


class _Base(unittest.TestCase):
    """临时库 + 临时状态目录 + 假时钟。"""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="yiban-crash-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self._close_conn()
        self.env_file = os.path.join(self.root, ".env")
        with open(self.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n")
        self.tmp = self.root
        self.db_file = os.path.join(self.tmp, "yiban.db")
        self.state_dir = os.path.join(self.tmp, "state")
        os.makedirs(self.state_dir)
        self._env = {k: os.environ.get(k) for k in (
            "YIBAN_DB_FILE", "YIBAN_ENV_FILE", "YIBAN_STATE_DIR",
            "YIBAN_ACCOUNTS_KEY", "YIBAN_EXECUTOR_ID", "YIBAN_GLOBAL_PAUSE",
            "YIBAN_LOG_FILE")}
        os.environ.update({
            "YIBAN_DB_FILE": self.db_file,
            "YIBAN_ENV_FILE": self.env_file,
            "YIBAN_STATE_DIR": self.state_dir,
            "YIBAN_ACCOUNTS_KEY": TEST_KEY,
            "YIBAN_EXECUTOR_ID": OWNER,
            "YIBAN_LOG_FILE": os.path.join(self.tmp, "sign.log"),
        })
        for k in ("YIBAN_GLOBAL_PAUSE",):
            os.environ.pop(k, None)
        db.init_db(self.db_file, env_file=self.env_file, cleanup=False)
        self.fc = _FakeClock()
        self._patches = [
            mock.patch.object(executor_v3, "_now", self.fc.now),
            mock.patch.object(executor_v3, "_sleep", self.fc.sleep),
            mock.patch.object(executor_v3, "_mono", lambda: self.fc.mono),
            mock.patch.object(clock, "now", self.fc.now),
        ]
        for p in self._patches:
            p.start()
        self.addCleanup(self._stop_patches)
        self.addCleanup(self._restore_env)

    def _stop_patches(self):
        for p in reversed(self._patches):
            with contextlib.suppress(Exception):
                p.stop()

    def _restore_env(self):
        self._close_conn()
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    @staticmethod
    def _close_conn():
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None

    # ---- 夹具 ----
    def _accounts(self, *phones):
        return [SimpleNamespace(phone=p, user_paused=False, owner="u@" + p)
                for p in phones]

    def _add_task(self, phone, vshard=0, attempts=0, run_at=None, day=DAY):
        conn = db.get_conn()
        conn.execute(
            "INSERT INTO sign_tasks (phone, day, vshard, owner, run_at, priority, "
            "state, attempts, lease_until, result, epoch, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (phone, day, vshard, "", run_at or _ts(seconds=-1), 5, "pending",
             attempts, "", "", 0, _ts(seconds=-60)))
        conn.commit()

    def _seed_v(self, v=8, day=DAY):
        from yiban.adapters.persistence import clock_meta
        clock_meta.set_meta(executor_v3.V_META_KEY_PREFIX + day, v)

    def _task_state(self, phone, day=DAY):
        row = db.get_conn().execute(
            "SELECT state FROM sign_tasks WHERE phone=? AND day=?",
            (phone, day)).fetchone()
        return row["state"] if row else None

    def _insert_event(self, node, phone="", day=DAY, ts=None):
        conn = db.get_conn()
        conn.execute(
            "INSERT INTO run_events (ts, day, node, executor, phone, message) "
            "VALUES (?,?,?,?,?,?)",
            (ts or _ts(), day, node, OWNER, phone, ""))
        conn.commit()

    def _run_v3(self, accounts, *, cred_state=None, delegated=None, cfg=None):
        patches = [
            mock.patch.object(executor_v3, "_persist_loop", _never),
            mock.patch.object(executor_v3, "_make_limiter",
                              lambda channels, shares=1: _Limiter()),
            mock.patch.object(executor_v3, "_make_gap_gate", lambda: _Gate()),
            mock.patch.object(executor_v3, "_make_global_limiter",
                              lambda: token_bucket.GlobalLimiter("")),
        ]
        for p in patches:
            p.start()
        try:
            return executor_v3.run_executor_v3(
                accounts, day=DAY, cfg=cfg or _cfg(), delegated=delegated,
                cred_state=cred_state)
        finally:
            for p in reversed(patches):
                p.stop()


# ===========================================================================
# V-S2：定论判据逐值（不钉编舞，钉"State → 处置"的映射不变式）
# ===========================================================================
class ResolveStateTest(unittest.TestCase):
    """`crash_semantics.resolve_state` 的五条分支：平台 State → 处置与状态码。"""

    def test_state_3_and_4_mean_already_signed_backfill_only(self):
        for state in (3, 4):
            with self.subTest(state=state):
                disposition, status = crash_semantics.resolve_state(state)
                self.assertEqual(disposition, crash_semantics.RESOLVE_ALREADY)
                self.assertEqual(status, yiban_status.STATUS_ALREADY)
                self.assertFalse(
                    crash_semantics.disposition_submits(disposition),
                    "已签到只回填结果，不得再提交一次真实签到")

    def test_state_2_converges_to_no_task(self):
        disposition, status = crash_semantics.resolve_state(2)
        self.assertEqual(disposition, crash_semantics.RESOLVE_NO_TASK)
        self.assertEqual(status, yiban_status.STATUS_NO_TASK)
        self.assertFalse(crash_semantics.disposition_submits(disposition))

    def test_state_0_and_1_are_signable_again(self):
        for state in (0, 1):
            with self.subTest(state=state):
                disposition, status = crash_semantics.resolve_state(state)
                self.assertEqual(disposition, crash_semantics.RESOLVE_SIGNABLE)
                self.assertIsNone(status, "可签不是结论端点：没有可回填的状态码")
                self.assertTrue(
                    crash_semantics.disposition_submits(disposition),
                    "未签到要按可签重新纳入")

    def test_state_5_stays_undetermined_no_submit_no_failure(self):
        disposition, status = crash_semantics.resolve_state(5)
        self.assertEqual(disposition, crash_semantics.RESOLVE_UNDETERMINED)
        self.assertEqual(status, yiban_status.STATUS_SUPPLEMENTING)
        self.assertFalse(crash_semantics.disposition_submits(disposition))
        self.assertNotEqual(status, yiban_status.STATUS_FAILED,
                            "补签中结果未定：不得记失败")

    def test_unreadable_state_stays_unknown_outcome(self):
        for state in (None, 6, 99):
            with self.subTest(state=state):
                disposition, status = crash_semantics.resolve_state(state)
                self.assertEqual(disposition, crash_semantics.RESOLVE_UNKNOWN)
                self.assertIsNone(status)
                self.assertFalse(crash_semantics.disposition_submits(disposition),
                                 "读不到就维持 unknown_outcome，不擅自重签")

    def test_agrees_with_client_judgement_for_every_enum_value(self):
        """派生守卫：`resolve_state` 只许是 `client._judge_by_state` 的**投影**。

        遍历 `SIGN_POSITION_STATE_STATUS` 的**全部取值**逐值比对：状态码必须与
        `_judge_by_state` 同源；处置必须是同一结论的换名。若有人在 `resolve_state` 里
        **另判一遍**（把 state→status 的映射再写一份），或两处任一侧改了枚举/映射，
        本用例必红——这就把"两处判据"的漂移面封成一条可执行断言。
        """
        expected_disposition = {
            yiban_status.STATUS_ALREADY: crash_semantics.RESOLVE_ALREADY,
            yiban_status.STATUS_NO_TASK: crash_semantics.RESOLVE_NO_TASK,
            yiban_status.STATUS_SUPPLEMENTING: crash_semantics.RESOLVE_UNDETERMINED,
        }
        for state in yiban_client.SIGN_POSITION_STATE_STATUS:
            with self.subTest(state=state):
                verdict = yiban_client._judge_by_state(state)
                disposition, status = crash_semantics.resolve_state(state)
                if verdict is None:
                    self.assertEqual(
                        (disposition, status),
                        (crash_semantics.RESOLVE_SIGNABLE, None),
                        f"state={state}: 客户端判'尚无结论' ⇒ 可签")
                    continue
                self.assertEqual(
                    status, verdict[3],
                    f"state={state}: 状态码必须与 _judge_by_state 同源（不许自成一份判据）")
                self.assertEqual(
                    disposition, expected_disposition[verdict[3]],
                    f"state={state}: 处置必须由 _judge_by_state 的结论投影而来")


# ===========================================================================
# V-S3 ①：写前标记先于真实请求
# ===========================================================================
class MarkerPrecedesRequestTest(_Base):
    def test_start_row_is_persisted_before_the_real_request(self):
        """真实请求被发出时，该账号当日的 `start` 行**必已落库**（先标记后请求）。

        用**独立连接**在请求替身里读表：读到的必须是已提交的 `start` 行——若实现把
        标记挪到请求之后（或放进 try/finally），这里读不到，用例翻红。
        """
        phone = _phone(1)
        self._add_task(phone)
        self._seed_v(8)
        seen = []

        def fake_attempt(acc):
            conn = sqlite3.connect(self.db_file)
            try:
                n = conn.execute(
                    "SELECT COUNT(*) FROM run_events WHERE day=? AND phone=? AND node=?",
                    (DAY, acc.phone, NODE_START)).fetchone()[0]
            finally:
                conn.close()
            seen.append((acc.phone, n))
            return (True, "签到成功", False, "success")

        with mock.patch.object(executor_v3.attempts, "attempt_signin", fake_attempt):
            results = self._run_v3(self._accounts(phone))

        self.assertEqual(results[phone][3], "success")
        self.assertEqual(len(seen), 1, f"恰好一次真实请求: {seen}")
        self.assertGreaterEqual(seen[0][1], 1,
                                "真实请求发出时，写前标记必须已落库（先标记后请求）")

    def test_marker_count_is_not_fewer_than_request_count(self):
        """标记数 ≥ 真实请求数：每个被发出的请求都有一条对应的 `start` 行。"""
        phones = [_phone(i) for i in (1, 2, 3)]
        for p in phones:
            self._add_task(p)
        self._seed_v(8)
        calls = []

        def fake_attempt(acc):
            calls.append(acc.phone)
            return (True, "签到成功", False, "success")

        with mock.patch.object(executor_v3.attempts, "attempt_signin", fake_attempt):
            self._run_v3(self._accounts(*phones))

        starts = db.get_conn().execute(
            "SELECT COUNT(*) FROM run_events WHERE day=? AND node=?",
            (DAY, NODE_START)).fetchone()[0]
        self.assertGreaterEqual(starts, len(calls),
                                "每个真实请求之前都必须落一条 start 行")


# ===========================================================================
# V-S3 ③：标记写入失败的行为=继续（显式判据）
# ===========================================================================
class MarkerWriteFailurePolicyTest(_Base):
    def test_policy_is_explicitly_continue(self):
        self.assertEqual(
            crash_semantics.MARKER_WRITE_FAILURE_POLICY, "continue",
            "本批裁决：标记写入失败=继续（沿用'观测面坏了不得改变签到结论'）")

    def test_signing_proceeds_when_event_table_write_fails(self):
        """观测表写不进时签到照常并了结：标记失败不得反噬主流程。"""
        conn = db.get_conn()
        conn.execute("DROP TABLE run_events")
        conn.commit()
        phone = _phone(1)
        self._add_task(phone)
        self._seed_v(8)
        with mock.patch.object(executor_v3.attempts, "attempt_signin",
                               lambda acc: (True, "签到成功", False, "success")):
            results = self._run_v3(self._accounts(phone))
        self.assertEqual(results[phone][3], "success",
                         "标记写失败时行为显式为'继续'：不得改变签到结论")
        self.assertEqual(self._task_state(phone), "done", "行仍必须被了结")


# ===========================================================================
# V-S3 ②④：有标记无结果必被识别；只告警、不自动重签
# ===========================================================================
class UnknownOutcomeDetectionTest(_Base):
    def _conn(self):
        return db.get_conn()

    def _ids(self):
        rows = self._conn().execute(
            "SELECT phone FROM run_events WHERE node=?", (NODE_START,)).fetchall()
        return sorted(r["phone"] for r in rows)

    def test_start_without_terminal_is_detected(self):
        phone = _phone(1)
        self._insert_event(NODE_START, phone)
        found = run_events.unresolved_starts(DAY)
        self.assertEqual([r["phone"] for r in found], [phone],
                         f"有 start 无终态必被识别: {found}")

    def test_start_followed_by_terminal_is_not_flagged(self):
        phone = _phone(1)
        self._insert_event(NODE_START, phone)
        self._insert_event(NODE_SUCCESS, phone)
        self.assertEqual(run_events.unresolved_starts(DAY), [],
                         "同一账号后续有了终态，就不是未定论")

    def test_start_followed_by_fail_is_not_flagged(self):
        """`fail` 是终态：start 后接 fail 即已定论，不得再算未定论（守卫 fail 分支）。"""
        phone = _phone(2)
        self._insert_event(NODE_START, phone)
        self._insert_event(NODE_FAIL, phone)
        self.assertEqual(run_events.unresolved_starts(DAY), [],
                         "start 后接 fail 已定论，不得再算未定论")

    def test_start_followed_by_pause_is_not_flagged(self):
        """`pause` 是终态：start 后接 pause 即已定论，不得再算未定论（守卫 pause 分支）。"""
        phone = _phone(3)
        self._insert_event(NODE_START, phone)
        self._insert_event(NODE_PAUSE, phone)
        self.assertEqual(run_events.unresolved_starts(DAY), [],
                         "start 后接 pause 已定论，不得再算未定论")

    def test_other_days_are_ignored(self):
        phone = _phone(1)
        self._insert_event(NODE_START, phone, day="2026-09-22")
        self.assertEqual(run_events.unresolved_starts(DAY), [],
                         "识别只看当日：昨日遗留不是本日事实")

    def test_detection_reports_and_does_not_auto_resign(self):
        phone = _phone(1)
        self._insert_event(NODE_START, phone)
        self._add_task(phone)          # 有当日计划行：识别不得动它
        before = self._task_state(phone)
        alerts = []
        with mock.patch.object(crash_semantics.alerts, "notify_admin_entry",
                               side_effect=lambda *a, **k: alerts.append(a)), \
             mock.patch.object(executor_v3.attempts, "attempt_signin",
                               side_effect=AssertionError("识别不得发起任何真实请求")), \
             self.assertLogs(crash_semantics.logger, level="WARNING") as logs:
            found = crash_semantics.report_unknown_outcomes(DAY)
        self.assertEqual([r["phone"] for r in found], [phone])
        self.assertTrue(alerts, f"识别必须出声（管理员告警）: {logs.output}")
        self.assertEqual(self._task_state(phone), before,
                         "识别只告警、不自动重签：不得改台账")
        self.assertTrue(any(phone in line or "unknown" in line.lower()
                            for line in logs.output), logs.output)

    def test_no_unknown_outcomes_is_quiet(self):
        with self.assertNoLogs(crash_semantics.logger, level="WARNING"):
            found = crash_semantics.report_unknown_outcomes(DAY)
        self.assertEqual(found, [], "没有未定论行就静默返回空表，不出声")

    def test_coordinator_startup_identifies_unknown_outcomes(self):
        """协调器启动（`runner.main`）时，有标记无结果的行必被识别（接线存在）。"""
        from yiban.app import runner as runner_mod
        phone = _phone(1)
        self._insert_event(NODE_START, phone)
        seen = []
        with mock.patch.object(runner_mod.crash_semantics, "report_unknown_outcomes",
                               side_effect=lambda day, **k: seen.append(day)):
            self._run_runner()
        self.assertTrue(seen, "协调器启动必须调用识别（有标记无结果的行必被识别）")

    def test_multiexecutor_supervisor_identifies_but_skips_readonly_rounds(self):
        """多执行体监督进程（协调器）：签发的轮次识别；只读轮次不识别（不产生推送副作用）。"""
        from yiban.workers import workers
        with mock.patch.object(workers.accounts_mod, "load_accounts",
                               return_value=self._accounts(_phone(1))), \
             mock.patch.object(workers.cli_support, "_acquire_run_lock",
                               return_value=mock.MagicMock()), \
             mock.patch.object(workers.crash_semantics,
                               "report_unknown_outcomes") as det, \
             mock.patch.object(workers.subprocess, "Popen",
                               side_effect=OSError("no spawn in test")), \
             mock.patch.object(workers, "_await_workers", return_value=[]), \
             mock.patch.object(workers.state_io, "_write_sched_done"):
            workers.run_worker_supervisor(2, ["--check-config"], slots=[0, 1])
            det.assert_not_called()
            workers.run_worker_supervisor(2, [], slots=[0, 1])
            det.assert_called_once()

    def test_readonly_round_criterion_is_shared(self):
        from yiban.workers import workers
        self.assertTrue(workers._is_readonly_round(["--check-config"]))
        self.assertTrue(workers._is_readonly_round(["--probe"]))
        self.assertTrue(workers._is_readonly_round(["--second-run-check"]))
        self.assertFalse(workers._is_readonly_round([]))
        self.assertFalse(workers._is_readonly_round(["--only", "13800000001"]))

    # ---- runner 接线夹具（复用 RunLevelNodesTest 的口径）----
    def _run_runner(self):
        from yiban.app import runner as runner_mod
        accounts = self._accounts(_phone(1))
        outcome = {_phone(1): (True, "签到成功", False, "success")}
        # 单执行体/手动轮的协调器身份：不带 `YIBAN_EXECUTOR_ID`（`_already_child` 为假，
        # 识别才会跑）。带该键的形态是"被监督进程拉起的子执行体"，由监督进程单点识别。
        os.environ.pop("YIBAN_EXECUTOR_ID", None)
        with mock.patch.object(runner_mod.accounts_mod, "load_accounts",
                               return_value=accounts), \
             mock.patch.object(runner_mod.schedule_mod, "build_schedule",
                               return_value={_phone(1): START}), \
             mock.patch.object(runner_mod.executor_v3, "run_executor_v3",
                               return_value=dict(outcome)), \
             mock.patch.object(runner_mod.state_io, "_load_cred_state",
                               return_value={}), \
             mock.patch.object(runner_mod.state_io, "_save_cred_state"), \
             mock.patch.object(runner_mod.state_io, "_is_second_run",
                               return_value=False), \
             mock.patch.object(runner_mod.state_io, "_write_sched_done"), \
             mock.patch.object(runner_mod.db, "purge_expired_deleted_accounts"), \
             mock.patch.object(runner_mod.alerts, "_maybe_alert_zero_success"), \
             mock.patch.object(runner_mod.alerts, "_flush_admin_mail_summary"), \
             mock.patch.object(runner_mod.alerts, "notify_admin_entry"):
            return runner_mod.main([])


if __name__ == "__main__":
    unittest.main(verbosity=2)
