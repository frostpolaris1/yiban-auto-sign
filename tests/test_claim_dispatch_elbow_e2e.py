# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""l1rx 第二刀（claim-at-dispatch ＋ elbow 谓词接线 ＋ 删租约族）的**不变式校验者**。

标签：B · 调度：领取/队列/执行体

**测不变式，不测编舞**（AGENTS §一 6a）。本文件钉住的每一条都是"任何时刻都不该被违反
的性质"，与调用顺序无关：

1. **领取即执行、每次一行**：执行体每次领取最多拿到一行（`_claim_one`）；`CLAIM_LIMIT=1`；
   执行体源码里没有 `limit=<批量>` 的领取。
2. **肘界是行的函数**：领取谓词下推的 SQL 与 `yiban/core/elbow.may_claim` 对同一
   `(run_at, now, elbow)` **逐例同答**；新鲜行不会被领、过期超过肘界的行会被领；
   `_elbow_allows` 适配器与纯函数一致；下推与谓词漂移时"不执行 + 退回待领"。
3. **租约族已删**：领取不写 `lease_until`；`reap_expired`/`claimed_owners` 不存在；
   崩溃自愈只剩进程句柄口径的 `reap_abandoned`；执行体没有心跳判活 / 在途豁免 /
   死主接管 / 让位探针 / 进程内队列。
4. **兜底腿带肘界**（`FALLBACK_ELBOW_SEC`）——轮内让位探针的**结构性替代**。

依赖：临时 SQLite（真迁移链）+ 假时钟 + 替身登录/限速。不发任何网络请求。
"""
import ast
import asyncio
import contextlib
import datetime
import inspect
import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import db

from yiban import egress
from yiban.core import clock
from yiban.core import elbow as elbow_pred
from yiban.engine import executor_v3
from yiban.store import clock_meta, queue_store
from yiban.workers import workers

TEST_KEY = "a" * 64
DAY = "2026-09-22"          # 周二，避开周末门
START = datetime.datetime(2026, 9, 22, 6, 40, 0)
OWNER = "single@testhost"
RUNTIME_OWNER = egress.runtime_owner(OWNER)
SHARDS = (0,)


def _phone(i):
    return f"1380000{i:04d}"


def _ts(**kw):
    return (START + datetime.timedelta(**kw)).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def _cfg(**over):
    cfg = {"order": "sequence", "dist": "uniform", "edge_front_sec": 60,
           "edge_back_sec": 60, "block_cap": 15, "mu_min_pct": 40, "mu_max_pct": 60,
           "sigma_min_pct": 15, "sigma_max_pct": 25, "min_exec_gap": 5,
           "avg_attempt_sec": 3, "retry_min_interval": 60, "exec_gap_min": 10,
           "allow_time_pref": 0, "sign_start": (6, 30), "sign_end": (7, 50),
           "bucket_rate": 1.0, "executors": [OWNER]}
    cfg.update(over)
    return cfg


class _PermissiveLimiter:
    manual = False

    def bucket(self, egress_key):
        return SimpleNamespace(retry_after=lambda now: 0.0)

    def acquire(self, egress_key, now):
        return True

    def on_success(self, egress_key):
        pass

    def on_risk_signal(self, egress_key, now):
        pass

    def persist(self, egress_key, stamp=None):
        return True

    def restore_from_store(self, egress_key, now=None):
        return False


class _PermissiveGate:
    gap_sec = 10.0

    def allow(self, phone, now):
        return True

    def commit(self, phone, now):
        pass


async def _never(_ctx):
    import asyncio
    await asyncio.Event().wait()


class _FakeClock:
    def __init__(self, t=START):
        self.t = t
        self.mono = 10_000.0
        self.sleeps = []

    def now(self):
        return self.t

    async def sleep(self, sec):
        self.sleeps.append(sec)
        self.t += datetime.timedelta(seconds=sec)
        self.mono += sec
        import asyncio
        await asyncio.sleep(0)


class _DbBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-elbow-")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n")
        os.environ.update({
            "YIBAN_ACCOUNTS_KEY": TEST_KEY,
            "YIBAN_ENV_FILE": cls.env_file,
            "YIBAN_DB_FILE": cls.db_file,
            "YIBAN_STATE_DIR": cls.tmp,
            "YIBAN_EXECUTOR_ID": OWNER,
        })

    @classmethod
    def tearDownClass(cls):
        cls._close_conn()
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k in ("YIBAN_ACCOUNTS_KEY", "YIBAN_ENV_FILE", "YIBAN_DB_FILE",
                  "YIBAN_STATE_DIR", "YIBAN_EXECUTOR_ID"):
            os.environ.pop(k, None)

    @staticmethod
    def _close_conn():
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None

    def setUp(self):
        self._close_conn()
        for suffix in ("", "-wal", "-shm"):
            p = self.db_file + suffix
            if os.path.exists(p):
                os.remove(p)
        self.state_dir = tempfile.mkdtemp(dir=self.tmp, prefix="state-")
        os.environ["YIBAN_STATE_DIR"] = self.state_dir
        self.addCleanup(shutil.rmtree, self.state_dir, ignore_errors=True)
        db.init_db(self.db_file, env_file=self.env_file, cleanup=False)

    def tearDown(self):
        self._close_conn()

    def _add_task(self, phone, *, run_at=None, vshard=0, state="pending", owner="",
                  attempts=0, priority=5, result="", epoch=0):
        run_at = run_at or _ts(seconds=-1)
        conn = db.get_conn()
        conn.execute(
            "INSERT INTO sign_tasks (phone, day, vshard, owner, run_at, priority, "
            "state, attempts, lease_until, result, epoch, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (phone, DAY, vshard, owner, run_at, priority, state, attempts, "",
             result, epoch, _ts(seconds=-120)))
        conn.commit()

    def _row(self, phone):
        return dict(db.get_conn().execute(
            "SELECT * FROM sign_tasks WHERE phone=? AND day=?", (phone, DAY)).fetchone())

    def _ctx(self, **over):
        base = dict(runtime_id=RUNTIME_OWNER, day=DAY, shards=SHARDS, elbow_sec=0.0,
                    allowed_phones=None, reclaim=False, executor_id=OWNER,
                    log_tag="[single]", claiming=0, claim_unreadable=False,
                    queue_unreadable=False, stop=None, slot=0, m=1, inflight=0)
        base.update(over)
        return SimpleNamespace(**base)


# ---------------------------------------------------------------------------
# ① 领取即执行、每次一行
# ---------------------------------------------------------------------------
class ClaimAtDispatchTest(_DbBase):
    def test_claim_limit_is_one(self):
        self.assertEqual(queue_store.CLAIM_LIMIT, 1,
                         "claim-at-dispatch：领取行数缺省为一")
        self.assertFalse(hasattr(executor_v3, "SENTINEL_PRIORITY"),
                         "进程内队列的哨兵常量随队列退场")
        self.assertFalse(hasattr(executor_v3, "PRIORITY_ORDER_BASE"),
                         "进程内排序基准随队列退场")

    def test_claim_one_returns_at_most_one_row(self):
        for i in range(3):
            self._add_task(_phone(i), run_at=_ts(seconds=-1))
        with mock.patch.object(executor_v3, "_now", lambda: START):
            rows = executor_v3._claim_one(self._ctx())
        self.assertEqual(len(rows), 1, "每次领取只许一行")
        claimed = [p for p in (_phone(i) for i in range(3))
                   if self._row(p)["state"] == "claimed"]
        self.assertEqual(claimed, [rows[0]["phone"]], "只有返回的那一行被占住")

    def test_executor_source_has_no_batch_claim(self):
        """执行体源码里不得出现 `limit=<大于 1>` 的领取（防回退成批量预取）。"""
        src = inspect.getsource(executor_v3)
        tree = ast.parse(src)
        bad = []
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "claim_batch"):
                for kw in node.keywords:
                    if (kw.arg == "limit"
                            and isinstance(kw.value, ast.Constant)
                            and isinstance(kw.value.value, int)
                            and kw.value.value > 1):
                        bad.append(ast.unparse(kw))
        self.assertEqual(bad, [], f"执行体里出现批量领取：{bad}")


# ---------------------------------------------------------------------------
# ② 肘界谓词：下推实现与纯函数逐例同答
# ---------------------------------------------------------------------------
class ElbowPredicateTest(_DbBase):
    """`queue_store.claim_batch(elbow_sec=…)` 的 SQL 是 `elbow.may_claim` 的下推实现。"""

    def _claimable(self, offset_sec, elbow_sec):
        """在该 `run_at` 上放一行，问"这一行会不会被领"。"""
        phone = _phone(1)
        conn = db.get_conn()
        conn.execute("DELETE FROM sign_tasks")
        conn.commit()
        self._add_task(phone, run_at=_ts(seconds=offset_sec))
        now = _ts()
        rows = queue_store.claim_batch(RUNTIME_OWNER, DAY, SHARDS, now=now,
                                       elbow_sec=elbow_sec)
        self.assertIsNotNone(rows, "领取读不通（本用例不该发生）")
        return any(r["phone"] == phone for r in rows)

    def _may_claim(self, offset_sec, elbow_sec):
        now = START
        row = elbow_pred.ClaimRow(run_at=(now + datetime.timedelta(seconds=offset_sec)).timestamp())
        return elbow_pred.may_claim(row, elbow_pred.ClaimRole(elbow_sec=elbow_sec),
                                    now.timestamp())

    def test_sql_cutoff_agrees_with_predicate_for_every_boundary(self):
        for elbow in (0.0, 60.0):
            for offset in (-120.0, -61.0, -60.0, -59.0, -1.0, 0.0, 1.0, 60.0):
                with self.subTest(elbow=elbow, offset=offset):
                    self.assertEqual(
                        self._claimable(offset, elbow), self._may_claim(offset, elbow),
                        "承接谓词（SQL）与纯函数（elbow.may_claim）判反："
                        f"offset={offset}s elbow={elbow}s")

    def test_fresh_row_is_not_claimed_with_elbow(self):
        self.assertFalse(self._claimable(-10.0, 60.0), "过期 10s 的行不该被肘界 60s 的角色领")
        self.assertTrue(self._claimable(-70.0, 60.0), "过期 70s 的行必须被肘界 60s 的角色领")

    def test_adapter_agrees_with_predicate(self):
        for elbow in (0.0, 60.0):
            for offset in (-120.0, -60.0, -1.0, 0.0, 1.0):
                with self.subTest(elbow=elbow, offset=offset):
                    run_at = _ts(seconds=offset)
                    self.assertEqual(
                        executor_v3._elbow_allows(run_at, elbow, START),
                        self._may_claim(offset, elbow),
                        "适配器（台账时间串 → epoch 秒）与纯函数判反")

    def test_predicate_drift_is_returned_not_executed(self):
        """下推与谓词漂移（SQL 放行、may_claim 说不可领）时**不执行**，把行退回待领。

        造一行**确实过期超过肘界**的（下推会放行它），再把谓词打成"不可领"——此刻若
        没有那道复核，这一行就会被拿去执行（漂移不被发现）。
        """
        phone = _phone(1)
        self._add_task(phone, run_at=_ts(seconds=-120))
        with mock.patch.object(executor_v3, "_now", lambda: START), \
                mock.patch.object(executor_v3, "_elbow_allows", lambda *a: False), \
                mock.patch.object(executor_v3.logger, "error"):
            rows = executor_v3._claim_one(self._ctx(elbow_sec=60.0))
        self.assertEqual(rows, [], "谓词说不该领的行绝不许被返回去执行")
        self.assertEqual(self._row(phone)["state"], "pending",
                         "漂移的那一行必须被退回待领（不许留在 claimed）")


# ---------------------------------------------------------------------------
# ③ 租约族已删
# ---------------------------------------------------------------------------
class LeaseFamilyRemovedTest(_DbBase):
    def test_claim_writes_no_lease(self):
        self._add_task(_phone(1), run_at=_ts(seconds=-1))
        rows = queue_store.claim_batch(RUNTIME_OWNER, DAY, SHARDS, now=_ts())
        self.assertEqual(len(rows), 1)
        self.assertEqual(self._row(_phone(1))["lease_until"], "",
                         "领取不得写任务级租约（租约族已删）")

    def test_lease_family_symbols_are_gone(self):
        for name in ("reap_expired", "claimed_owners", "LEASE_SECONDS", "REAP_GRACE_SEC",
                     "CLAIM_BATCH_LIMIT"):
            self.assertFalse(hasattr(queue_store, name), f"queue_store.{name} 应随租约族删除")
        for name in ("_refiller", "_live_row_owners", "_widen_with_dead_peers"):
            self.assertFalse(hasattr(executor_v3, name), f"executor_v3.{name} 应删除")

    def test_process_handle_reaping_is_the_recovery_path(self):
        """崩溃自愈只剩进程句柄口径：`reap_abandoned` 把死亡执行体名下的在飞行回退。"""
        self._add_task(_phone(1), state="claimed", owner=RUNTIME_OWNER)
        self._add_task(_phone(2), state="claimed", owner="other@host:1:1")
        n = queue_store.reap_abandoned(OWNER, day=DAY)
        self.assertEqual(n, 1, "只回收该稳定名下的在飞行")
        self.assertEqual(self._row(_phone(1))["state"], "pending")
        self.assertEqual(self._row(_phone(2))["state"], "claimed", "别人的行不动")

    def test_run_executor_v3_signature(self):
        sig = inspect.signature(executor_v3.run_executor_v3)
        self.assertIn("elbow_sec", sig.parameters, "执行体入口必须能带肘界")
        self.assertNotIn("yield_probe", sig.parameters, "轮内让位探针已删")

    def test_executor_imports_core_elbow(self):
        tree = ast.parse(inspect.getsource(executor_v3))
        imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "yiban.core":
                imports.update(a.name for a in node.names)
        self.assertIn("elbow", imports, "执行体必须接线 yiban.core.elbow（谓词的唯一语义源）")
        calls = [n for n in ast.walk(tree)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                 and n.func.attr == "may_claim"]
        self.assertTrue(calls, "执行体必须真的调用 elbow.may_claim（接线，不只是 import）")


# ---------------------------------------------------------------------------
# ④ 兜底腿带肘界（轮内让位探针的结构性替代）
# ---------------------------------------------------------------------------
class FallbackElbowWiringTest(unittest.TestCase):
    def test_fallback_call_passes_elbow_and_no_probe(self):
        src = inspect.getsource(workers.run_fallback_worker)
        tree = ast.parse(src)
        calls = [n for n in ast.walk(tree)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                 and n.func.attr == "run_executor_v3"]
        self.assertEqual(len(calls), 1, "兜底腿应恰有一处 run_executor_v3 调用")
        kwargs = {kw.arg: kw for kw in calls[0].keywords}
        self.assertIn("elbow_sec", kwargs, "兜底腿必须带肘界")
        self.assertNotIn("yield_probe", kwargs, "轮内让位探针已删")
        self.assertGreater(executor_v3.FALLBACK_ELBOW_SEC, 0,
                           "兜底的肘界必须为正（否则退化成到期即领、抢跑回来）")


# ---------------------------------------------------------------------------
# ⑤ 端到端：一轮只领"过期超过肘界"的行
# ---------------------------------------------------------------------------
def _keeper_until(fc, seconds):
    """假协调循环：假时钟推进到 `START + seconds` 即置停止（不自己推进时钟）。"""
    async def _keep(ctx):
        limit = START + datetime.timedelta(seconds=seconds)
        while not ctx.stop.is_set() and fc.t < limit:
            await asyncio.sleep(0)
        ctx.stop.set()
    return _keep


class _RoundMixin:
    """"跑真一轮"的公用夹具：真领取（不走 items 替身）、替身登录/限速、可抑制协调循环。"""

    def _run_real_round(self, phones, *, elbow_sec=0.0, keeper_seconds=10, cfg_extra=None):
        fc = _FakeClock()
        calls = []
        cfg = _cfg(**(cfg_extra or {}))
        patches = [
            mock.patch.object(executor_v3, "_persist_loop", _never),
            mock.patch.object(executor_v3, "_now", fc.now),
            mock.patch.object(executor_v3, "_sleep", fc.sleep),
            mock.patch.object(executor_v3, "_mono", lambda: fc.mono),
            mock.patch.object(clock, "now", fc.now),
            mock.patch.object(executor_v3, "_make_limiter",
                              lambda channels, shares=1: _PermissiveLimiter()),
            mock.patch.object(executor_v3, "_make_gap_gate", lambda: _PermissiveGate()),
            mock.patch.object(executor_v3.attempts, "attempt_signin",
                              lambda acc: (calls.append(acc.phone),
                                           (True, "ok", False, "success"))[1]),
            mock.patch.object(executor_v3, "_dispatch_keeper", _keeper_until(fc, keeper_seconds)),
        ]
        with contextlib.ExitStack() as st:
            for pr in patches:
                st.enter_context(pr)
            executor_v3.run_executor_v3(
                [SimpleNamespace(phone=p, user_paused=False, owner="u@" + p) for p in phones],
                day=DAY, cfg=cfg, rng=__import__("random").Random(7),
                cred_state={}, elbow_sec=elbow_sec)
        return calls, fc


class RoundClaimsOnlyPastElbowTest(_RoundMixin, _DbBase):
    def test_round_executes_only_rows_past_elbow(self):
        """真一轮执行体（肘界 60s）：陈旧行被领走执行，肘界**之内**的行原样留着。

        "新鲜行"取 `run_at` 在未来（未到点）：它在整轮里都不可能被领，故本用例对
        假时钟推进速度不敏感——断言的是不变式，不是某一拍的时序。
        """
        stale, fresh = _phone(1), _phone(2)
        self._add_task(stale, run_at=_ts(seconds=-120))
        self._add_task(fresh, run_at=_ts(seconds=3600))
        clock_meta.set_meta(executor_v3.V_META_KEY_PREFIX + DAY, 8)
        calls, _fc = self._run_real_round([stale, fresh], elbow_sec=60.0,
                                          cfg_extra={"v": 8})
        self.assertEqual(calls, [stale], "只有过期超过肘界的行被领走执行")
        self.assertEqual(self._row(stale)["state"], "done")
        self.assertEqual(self._row(fresh)["state"], "pending", "肘界内的行必须原样留着")


# ---------------------------------------------------------------------------
# ⑥ 崩溃自愈三条路径（工单 `l1rx` 必修 R1）：判活＝进程句柄，不是时间戳
# ---------------------------------------------------------------------------
class CrashRecoveryPathsTest(_RoundMixin, _DbBase):
    """删除任务级租约后，"崩溃遗留的 `claimed` 行谁来收"必须仍有答案。

    `claim_batch` 只领 `pending`、`reclaim_tasks` 只翻终态 ⇒ 停在 `claimed` 的行当天
    **永久卡死＝漏签**。原 `reap_expired` 覆盖了这些路径；它随租约族删除后，收口改由
    **进程句柄口径**的 `reap_abandoned` 承担。三条路径各有一条能红的门：

    ① 单进程部署被 SIGKILL／异常 → 下一次起跑按本稳定名收尸（本类 test_round_start_…）；
    ② 多进程子进程未预期异常（`rc=1`）→ 监督进程轮末对**每个**已退出槽位收尸
       （本类 test_supervisor_…）；
    ③ 兜底进程被杀 → 兜底腿**下一次起跑**收自己的尸（本类 test_fallback_…）。
    """

    OLD_GEN = "99999:z"

    def test_round_start_reaps_own_stable_name_corpse(self):
        """路径①：上一次 `single@host` 崩溃留下的 `claimed` 行，下一次起跑必须救回来。"""
        stale = _phone(1)
        # 尸体行：owner 是本稳定名的**上一代**运行时串（进程已不在）
        self._add_task(stale, run_at=_ts(seconds=-120),
                       state="claimed", owner=f"{OWNER}:{self.OLD_GEN}", epoch=3)
        clock_meta.set_meta(executor_v3.V_META_KEY_PREFIX + DAY, 8)
        calls, _fc = self._run_real_round([stale], cfg_extra={"v": 8})
        self.assertEqual(calls, [stale], "起跑收尸没生效：崩溃遗留的行当天没人签（漏签）")
        self.assertEqual(self._row(stale)["state"], "done", "救回来的行照常收尾")

    def test_round_start_does_not_touch_foreign_stable_name(self):
        """反向控制：收尸**只按本稳定名**——别人的在飞行（可能活着）一行不动。"""
        mine, theirs = _phone(1), _phone(2)
        self._add_task(theirs, run_at=_ts(seconds=-120),
                       state="claimed", owner=f"worker-9@otherhost:{self.OLD_GEN}", epoch=7)
        clock_meta.set_meta(executor_v3.V_META_KEY_PREFIX + DAY, 8)
        calls, _fc = self._run_real_round([mine, theirs], cfg_extra={"v": 8})
        self.assertIn(mine, calls, "前置：本轮自己的账号照常领取执行（否则本用例失去判别力）")
        self.assertNotIn(theirs, calls, "别人的 claimed 行不得被本执行体收尸后拿去执行")
        self.assertEqual(self._row(theirs)["state"], "claimed", "别人的行一字不变")
        self.assertEqual(self._row(theirs)["epoch"], 7, "别人的行不得换代")

    def test_fallback_leg_reaps_its_own_corpse(self):
        """路径③：兜底被杀后，它自己下一次起跑必须救回 `fallback@host` 的尸体行。"""
        stale = _phone(1)
        fowner = egress.fallback_owner()
        self._add_task(stale, run_at=_ts(seconds=-120),
                       state="claimed", owner=f"{fowner}:{self.OLD_GEN}", epoch=2)
        clock_meta.set_meta(executor_v3.V_META_KEY_PREFIX + DAY, 8)
        calls = []
        fc = _FakeClock()
        cfg = _cfg(v=8)
        with mock.patch.dict(os.environ, {"YIBAN_EXECUTOR_ID": fowner}), \
                mock.patch.object(executor_v3, "_persist_loop", _never), \
                mock.patch.object(executor_v3, "_now", fc.now), \
                mock.patch.object(executor_v3, "_sleep", fc.sleep), \
                mock.patch.object(executor_v3, "_mono", lambda: fc.mono), \
                mock.patch.object(clock, "now", fc.now), \
                mock.patch.object(executor_v3, "_make_limiter",
                                  lambda channels, shares=1: _PermissiveLimiter()), \
                mock.patch.object(executor_v3, "_make_gap_gate", lambda: _PermissiveGate()), \
                mock.patch.object(executor_v3.attempts, "attempt_signin",
                                  lambda acc: (calls.append(acc.phone),
                                               (True, "ok", False, "success"))[1]), \
                mock.patch.object(executor_v3, "_dispatch_keeper", _keeper_until(fc, 10)):
            executor_v3.run_executor_v3(
                [SimpleNamespace(phone=stale, user_paused=False, owner="u@" + stale)],
                day=DAY, cfg=cfg, rng=__import__("random").Random(7),
                cred_state={}, claim_all=True, elbow_sec=executor_v3.FALLBACK_ELBOW_SEC)
        self.assertEqual(calls, [stale], "兜底腿起跑没救回自己的尸体行")
        self.assertEqual(self._row(stale)["state"], "done")

    def test_supervisor_reaps_every_slot_that_exited(self):
        """路径②：监督进程轮末对**每个**已退出槽位收尸（判据是进程已退出，不是 `rc<0`）。

        静态钉住"收尸调用不受退出码条件约束"——旧实现 `if rc is not None and rc < 0:`
        会让 `rc=1`（未预期异常）的槽位漏收。
        """
        src = inspect.getsource(workers.run_worker_supervisor)
        tree = ast.parse(src)
        reap_calls = [n for n in ast.walk(tree)
                      if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                      and n.func.id == "_reap_dead_worker"]
        self.assertTrue(reap_calls, "监督进程轮末必须收尸")
        # 不变式：收尸调用**不得受任何条件约束**（不是"不得挂在 `rc` 条件上"）。
        # 按字面变量名匹配会被一次改名绕过：本提交把循环变量从 `rc` 改成 `_rc` 之后，
        # `"rc" in names` 对 `if _rc < 0: _reap_dead_worker(...)` 恒为假 ⇒ 门静默放行
        # （实测绿）。故判据不看变量名，只看"这个调用有没有可能被跳过"。
        guards = (ast.If, ast.IfExp, ast.While, ast.BoolOp)
        # 推导式必须在**父节点**判定：`[_reap(...) for x in y if c]` 里 `elt` 与 `comprehension`
        # 是兄弟节点，先访问 `elt` 时 g 还没置位 ⇒ 只看 `ast.comprehension` 会漏掉这一形状。
        comprehensions = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
        guilty = []

        def _scan(node, guarded):
            for child in ast.iter_child_nodes(node):
                g = guarded or isinstance(child, guards)
                if isinstance(child, comprehensions) and any(gen.ifs for gen in child.generators):
                    g = True
                if (g and isinstance(child, ast.Call)
                        and isinstance(child.func, ast.Name)
                        and child.func.id == "_reap_dead_worker"):
                    guilty.append(child.lineno)
                _scan(child, g)

        _scan(tree, False)
        self.assertEqual(
            guilty, [],
            "收尸不得置于任何条件分支（if / if-表达式 / while / and-or）之下："
            "rc=1 的子进程同样会留下在飞行，判据必须是「进程已退出」而不是退出码")

    def test_dead_worker_reaping_releases_claimed_rows(self):
        """行为面：`_reap_dead_worker` 真把该槽位名下的在飞行翻回 `pending` 并换代。"""
        slot = 3
        stable = egress.worker_owner(slot)
        self._add_task(_phone(1), run_at=_ts(seconds=-120),
                       state="claimed", owner=f"{stable}:{self.OLD_GEN}", epoch=4)
        self._add_task(_phone(2), run_at=_ts(seconds=-120),
                       state="claimed", owner="worker-9@otherhost:1:a", epoch=9)
        workers._reap_dead_worker(slot)
        row = self._row(_phone(1))
        self.assertEqual(row["state"], "pending", "该槽位的在飞行必须回退待领")
        self.assertGreater(row["epoch"], 4, "回退必须换代（fence 迟到旧代写）")
        self.assertEqual(self._row(_phone(2))["state"], "claimed", "别的槽位不动")
