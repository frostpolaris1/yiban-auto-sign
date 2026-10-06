# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""N2a 内核进度事件打点层 e2e：`run_events` 表 + 统一 reporter 打点。

**契约就是表里的行**：三个落行类（`NodeRowsTest` / `RunLevelNodesTest` /
`RetentionAndRosterTest`）经 `_rows()` 读原始 SQL，断言值写成本文件的字面量
——"节点叫什么、字段有没有、顺序对不对"是表级事实，不该靠实现常量自证（实现改名
时这些用例要能红）。另两类引用模块名字，各有其因：`ReportingFailureIsolationTest`
**故意**不读表，它经模块契约面（直接调 `run_events.report_many`）注入写失败，验的是
"观测面坏了主流程照常"；`NodeVocabularyTest` 与 `MessageSanitizationTest` 测的正是
模块自己的契约面（值域只此一处、入表前净化），故直接引用模块名字。

覆盖的四条预期行为（N2a 工单口径）：
1. 一轮含成功与失败账号的运行逐节点落行：领取/开始/成功/失败各成行，字段完整
   （执行体身份、账号、业务日、节点、时刻），先后与实际执行顺序一致；
2. 全局暂停（GLOBAL_PAUSE）与收尾节点同样落行；
3. 事件表按保留期在既有每日清理路径退役，且建表属迁移产物（缺表＝拒启，不是静默零写）；
4. 打点写入异常被隔离，签到主流程照常跑完（打点是观测面，不是业务面）。

标签：B · 调度：领取/队列/执行体
覆盖：`yiban/store/run_events.py`（表写入与保留期清理）、`yiban/store/migrations.py`
    的 v21 建表与产物登记、`yiban/engine/executor_v3.py` 与 `yiban/engine/runner.py`
    的打点调用点、`yiban/store/cleanup.py` 的每日清理编排。
对应实现：同覆盖清单；事件表列序见 `yiban/store/run_events.py`。
关键断言：节点值集合与逐节点行数、每行身份/账号/业务日/时刻非空、同账号的
    领取→开始→终态先后、旧行退役新行保留、缺表时迁移完整性拒启、
    以及删表后签到仍了结（主流程不被观测面拖垮）。
依赖：临时 SQLite（真实迁移链）+ 假时钟（不真实 sleep）+ `attempt_signin` 替身；
    不发任何网络请求。单文件内自足，无 skip。
"""
import contextlib
import datetime
import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import db  # pyproject.toml 的 pythonpath 把 scripts/ 加进 sys.path

from yiban import clock, egress
from yiban.engine import executor_v3, hrw, token_bucket
from yiban.engine import runner as runner_mod
from yiban.store import migrations, run_events

TEST_KEY = "a" * 64
DAY = "2026-09-22"          # 周二：周末门不会拦下用例
START = datetime.datetime(2026, 9, 22, 6, 40, 0)   # 默认窗口 06:31~07:49 内
OWNER = "single@testhost"

#: 节点值域（契约面）：字面量写死，实现改名即红。
NODE_CLAIM = "claim"
NODE_START = "start"
NODE_SUCCESS = "success"
NODE_FAIL = "fail"
NODE_PAUSE = "pause"
NODE_FINALIZE = "finalize"


def _phone(i):
    return f"1380000{i:04d}"


def _ts(**kw):
    return (START + datetime.timedelta(**kw)).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


class _FakeClock:
    """假时钟：`_now` / `_sleep` / `_mono` 与 `clock.now` 共用一份推进。"""

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
    """不设限的限速替身：只暴露实现会调到的那几面。"""

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
    """桶状态落库循环的替身：10s 周期不该干扰假时钟。"""
    import asyncio
    await asyncio.Event().wait()


class _E2EBase(unittest.TestCase):
    """临时库 + 临时状态目录 + 假时钟；每个用例一套，互不串。"""

    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix="yiban-progress-")
        cls.env_file = os.path.join(cls.root, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def setUp(self):
        self._close_conn()
        self.tmp = tempfile.mkdtemp(dir=self.root, prefix="case-")
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
            # 日志文件必须一并重定向：`runner.main` → `cli_support._setup_cli_logging`
            # 按本键在 `dirname` 下开 `sign-<业务日>.log`，宿主/CI 导出该键时会把
            # 日志落到测试目录之外（同族先例：tests/test_scheduler_gate.py 显式指向
            # cls.tmp）。快照里登记了却不在 setUp 设值，等于"只存不设"。
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
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def _close_conn():
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None

    # ---- 读事件表的唯一手段：原始 SQL ----
    def _rows(self, day=DAY):
        """→ 该业务日的事件行（按 id 升序 = 写入顺序）。表不存在即抛（这就是红）。"""
        cur = db.get_conn().execute(
            "SELECT id, ts, day, node, executor, phone, message FROM run_events "
            "WHERE day=? ORDER BY id", (day,))
        return [dict(r) for r in cur.fetchall()]

    def _nodes(self, day=DAY):
        return [r["node"] for r in self._rows(day)]

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
        from yiban.store import clock_meta
        clock_meta.set_meta(executor_v3.V_META_KEY_PREFIX + day, v)

    def _task_state(self, phone, day=DAY):
        row = db.get_conn().execute(
            "SELECT state FROM sign_tasks WHERE phone=? AND day=?", (phone, day)).fetchone()
        return row["state"] if row else None

    def _run_v3(self, accounts, *, cred_state=None, delegated=None, cfg=None):
        """跑一轮真实执行体：真实补货领取（claim_batch）+ 替身登录。"""
        patches = [
            mock.patch.object(executor_v3, "_persist_loop", _never),
            mock.patch.object(executor_v3, "_make_limiter", lambda channels: _Limiter()),
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


# ---------------------------------------------------------------------------
# 行为一：逐节点落行、字段完整、顺序一致
# ---------------------------------------------------------------------------
class NodeRowsTest(_E2EBase):
    def test_claim_start_success_fail_rows_in_execution_order(self):
        ok, bad = _phone(1), _phone(2)
        self._add_task(ok)
        # attempts 预置到远超重试预算：失败账号当次即弃权，落一行 fail 而不是排队重试
        self._add_task(bad, attempts=99)
        self._seed_v(8)
        # 夹具前提（真守卫，会红）：本用例的 cfg 只列一个执行体，故本执行体拿到全部
        # 8 个虚分片，两个账号必被领到。谁把 cfg 改成多执行体、或 HRW 的分片语义变了，
        # 这条先出声——写成 `assertIn(vshard_of(p), shards)` 是恒真的（单成员集的
        # shards_of 返回全部分片），那种断言不承担守卫。
        shards = hrw.shards_of(OWNER, _cfg()["executors"], DAY, 8)
        self.assertEqual(len(shards), 8, "单执行体集下分片集必须是全部 8 片")

        def fake_attempt(acc):
            if acc.phone == bad:
                return (False, "网络抖动", False, "failed")
            return (True, "签到成功", False, "success")

        with mock.patch.object(executor_v3.attempts, "attempt_signin", fake_attempt):
            results = self._run_v3(self._accounts(ok, bad))

        self.assertEqual(results[ok][3], "success")
        self.assertEqual(results[bad][3], "failed")
        self.assertEqual(self._task_state(ok), "done")
        self.assertEqual(self._task_state(bad), "failed")

        rows = self._rows()
        # 账号节点（带 phone）与运行级节点（不带 phone）分开数：收尾是运行级的
        account_rows = [r for r in rows if r["phone"]]
        run_rows = [r for r in rows if not r["phone"]]
        nodes = [r["node"] for r in account_rows]
        self.assertEqual(len(account_rows), 6,
                         f"两个账号（一成一败）应落 6 行（领取×2/开始×2/终态×2）: {rows}")
        counts = {n: nodes.count(n) for n in set(nodes)}
        self.assertEqual(counts, {NODE_CLAIM: 2, NODE_START: 2,
                                  NODE_SUCCESS: 1, NODE_FAIL: 1}, f"节点分布: {rows}")
        self.assertEqual([r["node"] for r in run_rows], [NODE_FINALIZE],
                         f"运行级只落一行收尾，且不挂账号: {run_rows}")

        # 字段完整：执行体身份、账号、业务日、节点、时刻
        for r in rows:
            with self.subTest(row=r):
                self.assertEqual(r["executor"], OWNER, "每行都要带执行体身份")
                self.assertEqual(r["day"], DAY, "每行都要带业务日")
                self.assertIn(r["node"], (NODE_CLAIM, NODE_START, NODE_SUCCESS,
                                          NODE_FAIL, NODE_PAUSE, NODE_FINALIZE))
                self.assertTrue(str(r["ts"]).strip(), "每行都要带时刻")
        for r in account_rows:
            with self.subTest(row=r):
                self.assertIn(r["phone"], (ok, bad), "账号节点必须带账号")
        self.assertEqual([r["ts"] for r in rows], sorted(r["ts"] for r in rows),
                         "行序（id）必须与时刻先后一致")

        # 顺序：同账号 领取 → 开始 → 终态
        for phone, terminal in ((ok, NODE_SUCCESS), (bad, NODE_FAIL)):
            seq = [r["node"] for r in rows if r["phone"] == phone]
            self.assertEqual(seq, [NODE_CLAIM, NODE_START, terminal],
                             f"{phone} 的节点先后必须与实际执行顺序一致: {seq}")
        # 领取行必须先于任何开始行落地（领取是开始之前的事实）
        self.assertLess(max(i for i, n in enumerate(nodes) if n == NODE_CLAIM),
                        min(i for i, n in enumerate(nodes) if n == NODE_START))

    def test_run_finalize_row_is_recorded(self):
        ok = _phone(1)
        self._add_task(ok)
        self._seed_v(8)
        with mock.patch.object(executor_v3.attempts, "attempt_signin",
                               lambda acc: (True, "签到成功", False, "success")):
            self._run_v3(self._accounts(ok))
        finals = [r for r in self._rows() if r["node"] == NODE_FINALIZE]
        self.assertEqual(len(finals), 1, f"一次执行体会话结束落一行收尾: {self._rows()}")
        self.assertTrue(str(finals[0]["ts"]).strip())
        self.assertEqual(finals[0]["executor"], OWNER)
        self.assertTrue(finals[0]["message"].startswith("执行体会话收尾："),
                        f"执行体侧的收尾行必须带本层前缀（与轮次收尾同节点不同层）: "
                        f"{finals[0]}")

    def test_user_paused_and_window_skip_accounts_are_recorded(self):
        """不经队列的两类终态也要落行：用户自暂停（pause）与窗口外跳过（fail）。

        这两条走 `_prescan` / `_mark_window_skips`——账号没有计划行、不产生 claim/start，
        进度流里只出现一行终态是正确形态；覆盖它们是为了"打点覆盖执行体的全部结论点"。
        """
        paused, skipped = _phone(3), _phone(4)
        self._seed_v(8)
        # 窗口判为已关：补货循环立刻收工（两个账号都领不到），收尾由
        # `_mark_window_skips` 落 skipped_window。
        with mock.patch.object(executor_v3.schedule, "_window_closed",
                               lambda cfg, now: True):
            self._run_v3([SimpleNamespace(phone=paused, user_paused=True,
                                          owner="u@" + paused),
                          SimpleNamespace(phone=skipped, user_paused=False,
                                          owner="u@" + skipped)])
        rows = {(r["phone"], r["node"]) for r in self._rows()}
        self.assertIn((paused, NODE_PAUSE), rows,
                      f"用户自暂停必须落 pause 行: {self._rows()}")
        self.assertIn((skipped, NODE_FAIL), rows,
                      f"窗口外跳过必须落 fail 行: {self._rows()}")
        self.assertNotIn((paused, NODE_START), rows)
        self.assertNotIn((skipped, NODE_START), rows)

    def test_paused_credential_is_recorded_as_pause(self):
        """账密熔断的账号不发起请求，落 pause 而不是 start。"""
        phone = _phone(1)
        self._add_task(phone)
        self._seed_v(8)
        cred = {phone: {"paused_since": DAY, "probe_date": "2999-01-01"}}
        calls = []
        with mock.patch.object(executor_v3.attempts, "attempt_signin",
                               lambda acc: calls.append(acc.phone) or (True, "ok",
                                                                      False, "success")):
            self._run_v3(self._accounts(phone), cred_state=cred)
        self.assertEqual(calls, [], "暂停账号不得发起请求")
        nodes = [r["node"] for r in self._rows() if r["phone"] == phone]
        self.assertIn(NODE_PAUSE, nodes, f"暂停必须落 pause 行: {self._rows()}")
        self.assertNotIn(NODE_START, nodes, "没发起请求就不该有开始行")


# ---------------------------------------------------------------------------
# 行为二：全局暂停与收尾
# ---------------------------------------------------------------------------
class RunLevelNodesTest(_E2EBase):
    """runner 层节点：GLOBAL_PAUSE（暂停）与一轮结束（收尾）。"""

    def _run_runner(self):
        accounts = self._accounts(_phone(1))
        outcome = {_phone(1): (True, "签到成功", False, "success")}
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
             mock.patch.object(runner_mod.alerts, "_flush_admin_mail_summary"):
            return runner_mod.main([])

    def test_global_pause_is_recorded_and_nothing_runs(self):
        os.environ["YIBAN_GLOBAL_PAUSE"] = "1"
        code = self._run_runner()
        self.assertEqual(code, 2, "全局暂停按 SKIPPED 语义退出")
        nodes = self._nodes()
        self.assertIn(NODE_PAUSE, nodes, f"GLOBAL_PAUSE 生效必须落 pause 行: {self._rows()}")
        self.assertNotIn(NODE_START, nodes, "暂停的一轮不得有任何开始行")
        self.assertNotIn(NODE_SUCCESS, nodes)
        pause_row = next(r for r in self._rows() if r["node"] == NODE_PAUSE)
        self.assertTrue(str(pause_row["executor"]).strip(), "暂停行同样要带执行体身份")
        self.assertTrue(str(pause_row["ts"]).strip())

    def test_identity_falls_back_to_the_single_executor_name(self):
        """无 `YIBAN_EXECUTOR_ID` 时（单执行体/监督进程自身）落 egress 的单执行体稳定名。

        与执行体在同一路径上算出的身份同源——否则编排层的收尾行与执行体的节点行
        分属两条线，进度流拼不起来。
        """
        os.environ.pop("YIBAN_EXECUTOR_ID", None)
        os.environ["YIBAN_GLOBAL_PAUSE"] = "1"
        self.assertEqual(self._run_runner(), 2)
        rows = [r for r in self._rows() if r["node"] == NODE_PAUSE]
        self.assertEqual(len(rows), 1, f"暂停行必须落一行: {self._rows()}")
        self.assertEqual(rows[0]["executor"], egress.single_owner(),
                         "身份回退必须与执行体侧同源")

    def test_completed_run_is_recorded_as_finalize(self):
        code = self._run_runner()
        self.assertIn(code, (0, 1, 2))
        finals = [r for r in self._rows() if r["node"] == NODE_FINALIZE]
        self.assertEqual(len(finals), 1, f"跑完的一轮落一行轮次收尾: {self._rows()}")
        # 单执行体路径下"执行体会话收尾"与"轮次收尾"同 (业务日, 执行体, 节点)：
        # 两层是不同的事实，靠 message 前缀分层，消费方不得对 finalize 计数求和。
        self.assertTrue(finals[0]["message"].startswith("轮次收尾："),
                        f"runner 侧必须带轮次前缀，否则与会话收尾无法区分: {finals[0]}")
        self.assertNotIn(NODE_PAUSE, self._nodes(), "没暂停就不该有暂停行")


# ---------------------------------------------------------------------------
# 校验者：节点值域是唯一定义点（改名即改契约）
# ---------------------------------------------------------------------------
class NodeVocabularyTest(unittest.TestCase):
    """下游（SSE 端点、日志页）按节点取值分支；值域只许一处定义、只许这六个值。"""

    def test_vocabulary_is_exactly_the_six_contract_values(self):
        self.assertEqual(
            set(run_events.NODES),
            {NODE_CLAIM, NODE_START, NODE_SUCCESS, NODE_FAIL, NODE_PAUSE, NODE_FINALIZE},
            "节点值域是下游契约面：增删改名都要同批改消费者（本单立点只在 run_events）")

    def test_unknown_node_is_rejected_and_does_not_raise(self):
        """写未知节点：告警 + 丢弃，绝不抛（观测面不得反噬调用方）。"""
        with self.assertLogs("yiban.store.run_events", level="WARNING") as logs:
            written = run_events.report("no_such_node", day=DAY, executor=OWNER)
        self.assertFalse(written)
        self.assertTrue(any("no_such_node" in line for line in logs.output), logs.output)


class MessageSanitizationTest(unittest.TestCase):
    """message 常是易班服务端原文：入表前必须与 sign_events 同一份净化口径。

    净化点收在 `run_events` 的唯一写入处（不是各调用点各净化一遍）——逐调用点
    净化的形状是"漏一个就泄漏"，而这条泄漏路径直通日志页/SSE 回显。
    """

    def test_server_text_is_escaped_and_masked_before_insert(self):
        from yiban.masking import sanitize_text
        raw = "13800000002 登录失败\n第二行 password=abc123"
        self.assertEqual(
            run_events._row(NODE_FAIL, DAY, OWNER, "13800000002", raw)[5],
            sanitize_text(raw)[:200],
            "表里的 message 必须等于 sign_events 的净化结果（同一份 sanitize_text）")
        stored = run_events._row(NODE_FAIL, DAY, OWNER, "13800000002", raw)[5]
        self.assertNotIn("13800000002", stored, "服务端原文里回显的裸号不得入表")
        self.assertNotIn("\n", stored, "换行必须转义（否则日志页会被注入伪造行）")
        self.assertNotIn("abc123", stored, "凭据字面量必须抹除")


# ---------------------------------------------------------------------------
# 行为三：保留期退役 + 建表属迁移产物
# ---------------------------------------------------------------------------
class RetentionAndRosterTest(_E2EBase):
    def _insert(self, day, node=NODE_SUCCESS, phone=""):
        conn = db.get_conn()
        conn.execute(
            "INSERT INTO run_events (ts, day, node, executor, phone, message) "
            "VALUES (?,?,?,?,?,?)",
            (f"{day} 06:40:00", day, node, OWNER, phone, ""))
        conn.commit()

    def test_old_rows_retire_through_daily_cleanup_fresh_rows_stay(self):
        today = clock.now().strftime("%Y-%m-%d")
        ancient = (clock.now() - datetime.timedelta(days=400)).strftime("%Y-%m-%d")
        self._insert(ancient)
        self._insert(today)
        db.run_daily_cleanup()
        kept = [r["day"] for r in self._rows(ancient)] + [r["day"] for r in self._rows(today)]
        self.assertEqual(kept, [today],
                         "每日清理必须退役超期事件行、保留当日行（按业务日判定）")

    def test_table_is_registered_as_a_migration_artifact(self):
        self.assertIn(21, migrations._ARTIFACTS,
                      "新表的档位必须登记进迁移产物名册（缺表要能被 fail-closed 抓到）")
        self.assertIn(("run_events", None), migrations._ARTIFACTS[21])

    def test_missing_table_refuses_startup_instead_of_silent_zero_write(self):
        conn = db.get_conn()
        conn.execute("DROP TABLE run_events")
        conn.commit()
        with self.assertRaises(migrations.MigrationIntegrityError) as cm:
            migrations._run_migrations(conn)
        self.assertIn("run_events", str(cm.exception),
                      "拒启的异常文本必须点名缺了哪张表")


# ---------------------------------------------------------------------------
# 行为四：打点失败不拖垮签到主流程
# ---------------------------------------------------------------------------
class ReportingFailureIsolationTest(_E2EBase):
    def test_signing_completes_when_event_table_write_fails(self):
        """事件表缺失（schema 半升级/漂移）时，签到照常跑完并了结。"""
        conn = db.get_conn()
        conn.execute("DROP TABLE run_events")
        conn.commit()
        phones = [_phone(i) for i in (1, 2)]
        for p in phones:
            self._add_task(p)
        self._seed_v(8)
        with mock.patch.object(executor_v3.attempts, "attempt_signin",
                               lambda acc: (True, "签到成功", False, "success")), \
             self.assertLogs("yiban.store.run_events", level="WARNING") as logs:
            results = self._run_v3(self._accounts(*phones))
        for p in phones:
            self.assertEqual(results[p][3], "success", "观测面坏了不得改变签到结论")
            self.assertEqual(self._task_state(p), "done", "行仍必须被了结")
        self.assertTrue(any("run_events" in line for line in logs.output),
                        f"写失败必须出声定位: {logs.output}")

    def test_malformed_event_entry_does_not_raise(self):
        """条目不是映射（e.get 会抛 AttributeError）时同样只告警：构造也在保护区内。"""
        with self.assertLogs("yiban.store.run_events", level="WARNING"):
            written = run_events.report_many(["not-a-mapping", None])
        self.assertEqual(written, 0, "整批都不可用时不写、也不抛")

    def test_account_cascade_tolerates_the_missing_table(self):
        """删号连带清理不得因观测表缺席而回滚（v21 是可选档，表可以合法地不存在）。"""
        phone = _phone(1)
        self._add_task(phone)
        conn = db.get_conn()
        conn.commit()
        conn.execute("DROP TABLE run_events")
        conn.commit()
        db._cascade_phone_owned(conn, [phone])   # 缺表不得抛
        conn.commit()
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM sign_tasks WHERE phone=?",
                         (phone,)).fetchone()[0], 0,
            "缺观测表不得让同族的删号连带清理整体失效")


if __name__ == "__main__":
    unittest.main(verbosity=2)
