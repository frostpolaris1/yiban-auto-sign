# -*- coding: utf-8 -*-
"""`yiban/engine/executor_v3.py`：v3 执行体核心与 `YIBAN_SCHEDULER_V3` 分流。

标签：B · 调度：领取/队列/执行体
覆盖：v3 执行体的通道数与容量口径、带 vshard 过滤的待办计数、YIBAN_SCHEDULER_V3
   真值表与 runner 分流、asyncio
   通道的并发与非阻塞到点等待、批量领取与收干判据、退避落点的有界抖动与窗口上界、终态映射与
   fencing 透传、与 v2
   对齐的放弃通知/日志、令牌桶三件接线、产品契约（每次尝试写
   sign-state、dry_run
   零写、计划不可用不抛）、崩溃恢复与死主分片接管的整条链路。
对应实现：yiban/engine/executor_v3.py（run_executor_v3、通道/补货/退避/收尾各路径）、yiban/engine/schedule.py（channel_count、capacity_accounts_v3）、yiban/store/queue_store.py（pending_count、claim_batch、reap_expired、steal_shards）、yiban/engine/runner.py
   的分流点、yiban/engine/hrw.py 与 token_bucket.py。
关键断言：开关缺省为 0 时 v2 路径必须零行为变化（只有一行之差）。写进
   sign_tasks.vshard 的 V 必须与执行体分片集同源且当日稳定：V
   落库后只读，行索引落在当日 v_for()
   之外也仍要被领取，否则当天计划与领取集错位就永久漏领。vshard=-1
   的历史行永不计入待办（算进去会让该日永远不了结）。领取池的崩溃回收必须排在领取循环里且带
   day。死主接管的判据是「stale」四态而非「偷到几行」，且不得误伤
   running/finished/idle
   的活执行体。收尾标记只在正常返回路径写，异常与中断都必须让心跳过期后判
   stale。
依赖：临时 sqlite（sign_tasks）+ 假时钟（_now/_sleep/_mono
   共用一份推进，用例不真实 sleep）+ 打桩 attempt_signin 与限速三件套；asyncio
   用例在进程内跑。不发网络请求。整文件在本机执行，无 skip。

覆盖：
- `schedule.channel_count`（通道数 M 的唯一口径）与 `capacity_accounts_v3` 的逐值回归；
- `queue_store.pending_count`（带 `vshard` 过滤的待办计数，即"当日是否了结"的闸门）；
- `scheduler_v3_enabled` 真值表与 runner 的默认 0 分流（v2 路径零行为变化）；
- asyncio 通道：M 条通道、真并发、非阻塞到点等待、批量领取、收干判据；
- 退避落点（有界抖动、窗口上界）、终态映射与 fencing 透传、令牌桶三件接线；
- 产品契约：每次尝试写 sign-state、dry_run 零写、计划不可用时不抛。

关键断言：默认 0 时 `round.run_queue_retry` 被调用且 `run_executor_v3` 零调用；
写 `sign_tasks.vshard` 的 V 与执行体分片集同源且当日稳定；`vshard=-1` 的历史行
不计入"当日待办"（把它算作未了结会让该日永远不了结）。

依赖：临时库（`sign_tasks`）、假时钟（用例不真实 sleep）、打桩的 `attempt_signin`。
"""
import asyncio
import contextlib
import datetime
import json
import logging
import math
import os
import random
import shutil
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import db  # noqa: E402

from yiban import clock, window  # noqa: E402
from yiban.engine import (  # noqa: E402
    executor_v3,
    hrw,
    planner,
    schedule,
    state_io,
    token_bucket,
)
from yiban.engine import (  # noqa: E402
    runner as runner_mod,
)
from yiban.store import clock_meta, queue_store  # noqa: E402

TEST_KEY = "a" * 64
DAY = "2026-09-22"          # 周二，避开周末门
#: 固定起跑时刻：默认窗口 06:30~07:50（有效窗口 06:31~07:49），06:40 在窗口内
START = datetime.datetime(2026, 9, 22, 6, 40, 0)
OWNER = "single@testhost"
MY_SHARDS = (0, 1, 2, 3) # 与 FOREIGN_SHARD 配对：任何顺手扫了别人分片的改动都会在这里现形
FOREIGN_SHARD = 7


def _ts(**kw):
    return (START + datetime.timedelta(**kw)).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] # 截到毫秒：库里存的就是 %.3f，多留一位会让比对差在字符串上


def _phone(i):
    return f"1380000{i:04d}"


class _FakeClock:
    """假时钟：`_now` / `_sleep` / `_mono` 三个接缝共用同一份推进。

    `sleep` **先推进再让出**：并发等待的通道因此看到"已经到点"，各通道只等自己的
    剩余时间，总推进量由最晚落点决定而不是各通道之和（与真实墙钟同语义）。
    """

    def __init__(self, t=START):
        self.t = t
        self.mono = 10_000.0 # 单调钟起点刻意非零：从 0 起会掩盖「把墙钟当单调钟用」那一类错
        self.sleeps = []

    def now(self):
        return self.t

    async def sleep(self, sec):
        self.sleeps.append(sec)
        self.t += datetime.timedelta(seconds=sec)
        self.mono += sec
        await asyncio.sleep(0)


def _cfg(**over):
    """`schedule.planner_config()` 的同形配置快照（用例不读真实 .env）。"""
    cfg = {
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
    cfg.update(over)
    return cfg


class _PermissiveLimiter:
    """不设限的限速器替身：记录调用序，恒放行（避免用例被真实 GCRA 卡住）。"""

    manual = False # 类属性即可：用例只读它判是否人工接管，从不赋值

    def __init__(self, trace=None):
        self.trace = trace if trace is not None else []
        self.persisted = 0
        self.restored = 0

    def bucket(self, egress):
        return SimpleNamespace(retry_after=lambda now: 0.0) # 只暴露被调到的那一面：替身多出一个方法，就等于宣称实现会用它

    def acquire(self, egress, now):
        self.trace.append(("acquire", egress))
        return True

    def on_success(self, egress):
        self.trace.append(("on_success", egress))

    def on_risk_signal(self, egress, now):
        self.trace.append(("on_risk_signal", egress))

    def persist(self, egress, stamp=None):
        self.persisted += 1
        return True

    def restore_from_store(self, egress, now=None):
        self.restored += 1
        self.trace.append(("restore", egress))
        return False


class _PermissiveGate:
    """gap 门替身：记录 allow/commit 的调用序。"""

    def __init__(self, gap_sec=10.0, trace=None):
        self.gap_sec = gap_sec
        self.trace = trace if trace is not None else []

    def allow(self, phone, now):
        self.trace.append(("allow", phone))
        return True

    def commit(self, phone, now):
        self.trace.append(("commit", phone))


def _refiller_pushing(items):
    """假补货：把给定条目与 M 条哨兵一次性投进队列（时序用例用它替代真实轮询）。"""
    async def _refill(queue, shards, ctx):
        for it in items:
            queue.put_nowait(it)
        for _ in range(ctx.m):
            queue.put_nowait((executor_v3.SENTINEL_PRIORITY, "", "", 0, 0))
    return _refill


async def _never(_ctx):
    """永不返回的替身：桶状态落库循环的 10s 周期不该干扰时序用例的假时钟。"""
    await asyncio.Event().wait()


def _item(phone, run_at=None, attempts=0, epoch=1,
          priority=executor_v3.PRIORITY_ORDER_BASE):
    return (priority, run_at or _ts(seconds=-1), phone, attempts, epoch)


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-v3-")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n")
        os.environ.update({
            "YIBAN_ACCOUNTS_KEY": TEST_KEY,
            "YIBAN_ENV_FILE": cls.env_file,
            "YIBAN_DB_FILE": cls.db_file,
            "YIBAN_STATE_DIR": cls.tmp,
            # 执行体身份串固定下来：`cfg["executors"]` 里的名字必须与执行体自己算出的
            # 逐字一致，否则 `shards_of` 返回空分片集（本轮不该领活）——那会让用例
            # 静默地什么都不测。生产里由监督进程注入同名变量。
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
        # 每个用例一个全新的状态目录：状态文件是跨进程事实源，而 `init_db` 的 v20
        # 补账迁移会读它把历史行写进 `sign_tasks`——共用一个目录会让上一个用例写的
        # 状态文件在本用例变成"当日已有计划"（`has_plan` 为真），计划就再也不建了。
        self.state_dir = tempfile.mkdtemp(dir=self.tmp, prefix="state-")
        os.environ["YIBAN_STATE_DIR"] = self.state_dir
        self.addCleanup(shutil.rmtree, self.state_dir, ignore_errors=True)
        db.init_db(self.db_file, env_file=self.env_file, cleanup=False)
        self.fc = _FakeClock()
        # `clock.now` 一并打桩：状态文件名、租约时刻、重排时刻都取它，只打桩
        # `executor_v3._now` 会让"执行体的一天"与"状态文件的一天"分叉。
        self._stop = [
            mock.patch.object(executor_v3, "_now", self.fc.now),
            mock.patch.object(executor_v3, "_sleep", self.fc.sleep),
            mock.patch.object(executor_v3, "_mono", lambda: self.fc.mono),
            mock.patch.object(clock, "now", self.fc.now),
        ]
        for p in self._stop:
            p.start()
        self.addCleanup(self._stop_patches)

    def _stop_patches(self):
        for p in reversed(self._stop):
            with contextlib.suppress(Exception):
                p.stop()

    def tearDown(self):
        self._close_conn()

    # ---- 夹具 ----
    def _accounts(self, *phones):
        return [SimpleNamespace(phone=p, user_paused=False, owner="u@" + p) for p in phones]

    def _add_task(self, phone, vshard=0, state="pending", run_at=None, priority=5,
                  owner="", attempts=0, lease_until="", result="", epoch=0, day=DAY):
        conn = db.get_conn()
        conn.execute(
            "INSERT INTO sign_tasks (phone, day, vshard, owner, run_at, priority, "
            "state, attempts, lease_until, result, epoch, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (phone, day, vshard, owner, run_at or _ts(seconds=-1), priority, state,
             attempts, lease_until, result, epoch, _ts(seconds=-60)))
        conn.commit()

    def _add_claimed(self, phone, vshard=0, attempts=0, epoch=1, run_at=None, priority=5):
        """已由本执行体领取的行（等价于 `claim_batch` 刚返回它）。"""
        self._add_task(phone, vshard=vshard, state="claimed", owner=OWNER,
                       run_at=run_at, priority=priority, attempts=attempts,
                       lease_until=_ts(seconds=30), epoch=epoch)

    def _row(self, phone, day=DAY):
        row = db.get_conn().execute(
            "SELECT * FROM sign_tasks WHERE phone=? AND day=?", (phone, day)).fetchone()
        return dict(row) if row else None

    def _task_count(self, day=DAY):
        return db.get_conn().execute(
            "SELECT COUNT(*) FROM sign_tasks WHERE day=?", (day,)).fetchone()[0]

    def _clear_tasks(self):
        """清空队列行：同一用例内多轮（如逐状态子用例）复用同一手机号时用。"""
        conn = db.get_conn()
        conn.execute("DELETE FROM sign_tasks")
        conn.commit()

    def _state_path(self, day=DAY):
        return os.path.join(self.state_dir, f"sign-state-{day}.json")

    def _read_state(self, day=DAY):
        try:
            with open(self._state_path(day), encoding="utf-8-sig") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _seed_v(self, v=8, day=DAY):
        """预置当日虚分片数（等价于上一轮已建过计划并落库）。"""
        clock_meta.set_meta(executor_v3.V_META_KEY_PREFIX + day, v)

    def _run_v3(self, accounts, items=None, *, cfg=None, rng=None, limiter=None,
                gate=None, delegated=None, cred_state=None, event_sink=None,
                dry_run=False, notify_url=""):
        """跑一轮 v3。

        `items` 给了就用"一次性投递 + 哨兵"的假补货（时序完全可控，行需已领取）；
        不给则用真实补货（走 `claim_batch`，行由它置 owner/epoch）。
        """
        cfg = cfg or _cfg()
        limiter = limiter if limiter is not None else _PermissiveLimiter()
        gate = gate if gate is not None else _PermissiveGate()
        patches = [mock.patch.object(executor_v3, "_persist_loop", _never)]
        if items is not None:
            patches.append(
                mock.patch.object(executor_v3, "_refiller", _refiller_pushing(items)))
        patches += [
            mock.patch.object(executor_v3, "_make_limiter", lambda channels: limiter),
            mock.patch.object(executor_v3, "_make_global_limiter",
                              lambda: token_bucket.GlobalLimiter("")),
            mock.patch.object(executor_v3, "_make_gap_gate", lambda: gate),
        ]
        for p in patches:
            p.start()
        try:
            return executor_v3.run_executor_v3(
                accounts, day=DAY, cfg=cfg, rng=rng or random.Random(7),
                delegated=delegated, cred_state=cred_state, event_sink=event_sink,
                dry_run=dry_run, notify_url=notify_url)
        finally:
            for p in reversed(patches):
                p.stop()


# ---------------------------------------------------------------------------
# 开关真值表（分流谓词的唯一来源）
# ---------------------------------------------------------------------------
class SchedulerV3FlagTest(unittest.TestCase):
    def test_default_and_falsy_values_are_off(self):
        for raw in (None, "", "0", "false", "no", "off", "nonsense", "2"):
            env = {} if raw is None else {"YIBAN_SCHEDULER_V3": raw}
            self.assertFalse(executor_v3.scheduler_v3_enabled(env), repr(raw))

    def test_truthy_values_are_on_case_insensitively(self):
        for raw in ("1", "true", "ON", "Yes", " true "):
            self.assertTrue(
                executor_v3.scheduler_v3_enabled({"YIBAN_SCHEDULER_V3": raw}), repr(raw))

    def test_env_key_and_default_constants(self):
        self.assertEqual(executor_v3.ENV_SCHEDULER_V3, "YIBAN_SCHEDULER_V3")
        self.assertEqual(executor_v3.ENV_GLOBAL_RATE, "YIBAN_GLOBAL_RATE")
        self.assertFalse(executor_v3.DEFAULT_V3)


# ---------------------------------------------------------------------------
# 通道数 M：唯一口径
# ---------------------------------------------------------------------------
class ChannelCountTest(unittest.TestCase):
    def test_channel_count_exact_values(self):
        self.assertEqual(schedule.channel_count(1.0, 3), 6)
        self.assertEqual(schedule.channel_count(4.0, 3), 16)
        self.assertEqual(schedule.channel_count(0.2, 3), 2)
        self.assertEqual(schedule.channel_count(0.0, 3), 6, "非法速率回退出厂速率 1.0")

    def test_channel_count_never_below_one(self):
        for rate in (0.01, 0.05):
            self.assertGreaterEqual(schedule.channel_count(rate, 1), 1)

    def test_capacity_accounts_v3_values_unchanged(self):
        """把 `channel_count` 接进旧公式的回归护栏：逐值与改造前的内联式子相同。"""

        def legacy(ws, k=1, avg=None, bucket_rate=1.0, util=0.8):
            avg = 3 if avg is None else max(1, int(avg))
            k = max(1, int(k))
            bucket_rate = float(bucket_rate)
            if bucket_rate <= 0:
                bucket_rate = 1.0
            util = min(max(float(util), 0.0), 1.0)
            channels = min(16, math.ceil(bucket_rate * avg * 2))
            rate_eff = min(channels / avg, bucket_rate)
            return math.floor(k * rate_eff * max(0, int(ws)) * util + 1e-9)

        for ws in (0, 10, 100, 4140, 4200, -5):
            for k in (1, 2, 4):
                for avg in (1, 3, 8):
                    for rate in (1.0, 4.0, 0.2, 0.0, 2.5):
                        with self.subTest(ws=ws, k=k, avg=avg, rate=rate):
                            self.assertEqual(
                                schedule.capacity_accounts_v3(ws, k, avg, rate),
                                legacy(ws, k, avg, rate))


# ---------------------------------------------------------------------------
# 待办计数：vshard 过滤（"当日是否了结"的闸门）
# ---------------------------------------------------------------------------
class PendingCountTest(_Base):
    def test_counts_only_pending_in_my_shards(self):
        self._add_task(_phone(1), vshard=1, state="pending")
        self._add_task(_phone(2), vshard=3, state="pending")
        self._add_task(_phone(3), vshard=2, state="claimed")
        self._add_task(_phone(4), vshard=FOREIGN_SHARD, state="pending")
        self.assertEqual(queue_store.pending_count(DAY, MY_SHARDS), 2)

    def test_empty_shards_is_zero_without_touching_db(self):
        self._add_task(_phone(1), vshard=1, state="pending")
        self.assertEqual(queue_store.pending_count(DAY, ()), 0)

    def test_missing_table_returns_zero_with_warning(self):
        conn = db.get_conn()
        conn.execute("DROP TABLE sign_tasks")
        conn.commit()
        with self.assertLogs("yiban.store.queue_store", level="WARNING") as cm:
            self.assertEqual(queue_store.pending_count(DAY, MY_SHARDS), 0)
        self.assertIn("读取当日待办任务计数失败", "\n".join(cm.output))

    def test_historical_rows_are_never_counted(self):
        """`vshard=-1`（v18 平移 / v20 补账）恒不在分片集内 ⇒ 永不计入待办。"""
        self._add_task(_phone(1), vshard=-1, state="failed")
        self._add_task(_phone(2), vshard=-1, state="pending")
        self.assertEqual(queue_store.pending_count(DAY, MY_SHARDS), 0)
        self.assertEqual(queue_store.pending_count(DAY, tuple(range(64))), 0)
        # 双保险：分片集若被误传成含 -1，历史行也必须仍被挡在闸门之外
        self.assertEqual(queue_store.pending_count(DAY, (-1,)), 0)
        # 同一个坑写成测试：day_counts 不做 vshard 过滤，当闸门用会让该日永远不了结
        self.assertGreaterEqual(queue_store.day_counts(DAY)["open"], 2)


# ---------------------------------------------------------------------------
# V 不变量：写 vshard 的 V 与执行体分片集的 V 同源、当日稳定
# ---------------------------------------------------------------------------
class VInvariantTest(_Base):
    def test_plan_and_shard_set_share_the_same_stored_v(self):
        """自建计划时显式传 v，同一个 v 再给 `shards_of`；该 v 落库供后续轮次只读。"""
        other = "worker-1@testhost"
        cfg = _cfg(executors=[OWNER, other])
        accounts = self._accounts(*[_phone(i) for i in range(3)])
        with mock.patch.object(executor_v3.attempts, "attempt_signin",
                               lambda acc: (True, "ok", False, "success")):
            self._run_v3(accounts, cfg=cfg)
        v = int(clock_meta.get_meta(executor_v3.V_META_KEY_PREFIX + DAY, ""))
        self.assertEqual(v, hrw.v_for(3))
        rows = [r["vshard"] for r in db.get_conn().execute(
            "SELECT vshard FROM sign_tasks WHERE day=?", (DAY,)).fetchall()]
        self.assertEqual(len(rows), 3)
        for vshard in rows:
            self.assertLess(vshard, v, "计划行的索引必须落在扫描范围内")
        covered = set()
        for ex in (OWNER, other):
            covered |= set(hrw.shards_of(ex, cfg["executors"], DAY, v))
        self.assertEqual(covered, set(range(v)), "每个分片都要有执行体认领，否则该行无人领")

    def test_stored_v_wins_over_recomputed_v(self):
        """V 落库后只读：行索引在 `v_for(当日账号数)` 之外也必须仍被领取（反例）。"""
        accounts = self._accounts(*[_phone(i) for i in range(3)])
        self.assertEqual(hrw.v_for(len(accounts)), 64)
        self._seed_v(128)
        # 该行索引 100 落在 v_for(3)=64 之外——若每轮重算 V，它永远不会被领取
        self._add_task(_phone(0), vshard=100, state="pending")
        with mock.patch.object(executor_v3.attempts, "attempt_signin",
                               lambda acc: (True, "ok", False, "success")):
            results = self._run_v3(accounts)
        self.assertEqual(self._row(_phone(0))["state"], "done", "索引必须仍在扫描范围内")
        self.assertEqual(results[_phone(0)][3], "success")

    def test_missing_v_with_existing_plan_falls_back_to_max_vshard(self):
        """读不到落库的 V：留 error 并按当日已写行的最大分片号兜底（保证不漏领）。"""
        accounts = self._accounts(*[_phone(i) for i in range(3)])
        self._add_task(_phone(0), vshard=40, state="pending")
        with self.assertLogs("yiban", level="ERROR") as cm, \
             mock.patch.object(executor_v3.attempts, "attempt_signin",
                               lambda acc: (True, "ok", False, "success")):
            self._run_v3(accounts)
        self.assertIn("虚分片数不可用", "\n".join(cm.output))
        self.assertEqual(self._row(_phone(0))["state"], "done")
        self.assertEqual(
            clock_meta.get_meta(executor_v3.V_META_KEY_PREFIX + DAY, ""), "41")

    def test_stored_v_below_written_shard_is_widened(self):
        """落库的 V 比已写行的最大分片号还小 ⇒ 有行在扫描范围外，按最大分片号放宽。"""
        accounts = self._accounts(*[_phone(i) for i in range(3)])
        self._seed_v(8)
        self._add_task(_phone(0), vshard=40, state="pending")
        with self.assertLogs("yiban", level="ERROR") as cm, \
             mock.patch.object(executor_v3.attempts, "attempt_signin",
                               lambda acc: (True, "ok", False, "success")):
            self._run_v3(accounts)
        self.assertIn("虚分片数不可用", "\n".join(cm.output))
        self.assertEqual(self._row(_phone(0))["state"], "done", "该行不得被静默漏掉")
        self.assertEqual(
            clock_meta.get_meta(executor_v3.V_META_KEY_PREFIX + DAY, ""), "41")

    def test_only_historical_rows_are_treated_as_no_usable_plan(self):
        """当日只剩历史惰性行（`vshard=-1`）时视为"无可用计划"：留 error 并照常建计划。

        只按"当日有任意行"判"有计划"，就会既跳过建计划、又领不到任何行——本轮零领取、
        零请求，被汇总成"全部未执行"。历史行按设计保持原样，只是不再充当"计划已就绪"
        的证据。
        """
        phones = [_phone(i) for i in range(3)]
        self._add_task(_phone(99), vshard=-1, state="failed")
        with self.assertLogs("yiban", level="ERROR") as cm, \
             mock.patch.object(executor_v3.attempts, "attempt_signin",
                               lambda acc: (True, "ok", False, "success")):
            results = self._run_v3(self._accounts(*phones))
        self.assertIn("无 vshard >= 0 的计划行", "\n".join(cm.output))
        v = int(clock_meta.get_meta(executor_v3.V_META_KEY_PREFIX + DAY, ""))
        for p in phones:
            row = self._row(p)
            self.assertIsNotNone(row, "计划必须被补建，否则本轮领不到任何行")
            self.assertGreaterEqual(row["vshard"], 0)
            self.assertLess(row["vshard"], v, "计划行的索引必须落在扫描范围内")
            self.assertEqual(row["state"], "done", "账号必须真的被领取并执行")
        self.assertEqual(set(results), set(phones), "本轮不得空转")
        self.assertEqual(self._row(_phone(99))["vshard"], -1, "历史惰性行原样不动")

    def test_real_plan_row_is_never_rewritten(self):
        """当日已有真实计划行时逐字不变：不重写计划（幂等/开销），V 也不动。"""
        phone = _phone(1)
        self._add_claimed(phone)
        self._seed_v(8)
        with mock.patch.object(executor_v3.planner, "write_plan") as m_write, \
             mock.patch.object(executor_v3.attempts, "attempt_signin",
                               lambda acc: (True, "ok", False, "success")):
            results = self._run_v3(self._accounts(phone), [_item(phone)])
        m_write.assert_not_called()
        self.assertEqual(results[phone], (True, "ok", False, "success"))
        self.assertEqual(
            clock_meta.get_meta(executor_v3.V_META_KEY_PREFIX + DAY, ""), "8")

    def test_rebuilt_plan_rows_share_the_same_v(self):
        """补建的计划行与执行体分片集同源：每个分片都有执行体认领，否则该行无人领。"""
        other = "worker-1@testhost"
        cfg = _cfg(executors=[OWNER, other])
        phones = [_phone(i) for i in range(3)]
        self._add_task(_phone(99), vshard=-1, state="failed")
        with mock.patch.object(executor_v3.attempts, "attempt_signin",
                               lambda acc: (True, "ok", False, "success")):
            self._run_v3(self._accounts(*phones), cfg=cfg)
        v = int(clock_meta.get_meta(executor_v3.V_META_KEY_PREFIX + DAY, ""))
        covered = set()
        for ex in (OWNER, other):
            covered |= set(hrw.shards_of(ex, cfg["executors"], DAY, v))
        self.assertEqual(covered, set(range(v)), "每个分片都要有执行体认领")
        for p in phones:
            self.assertLess(self._row(p)["vshard"], v)


# ---------------------------------------------------------------------------
# 通道：M 条、真并发、非阻塞到点
# ---------------------------------------------------------------------------
class LaneTest(_Base):
    def test_lane_count_and_thread_pool_follow_bucket_rate(self):
        for rate, expect in ((1.0, 6), (4.0, 16)):
            with self.subTest(rate=rate):
                self._close_conn()
                for suffix in ("", "-wal", "-shm"):
                    p = self.db_file + suffix
                    if os.path.exists(p):
                        os.remove(p)
                db.init_db(self.db_file, env_file=self.env_file, cleanup=False)
                self._seed_v(8)
                started = []
                pool_sizes = []

                async def fake_lane(queue, lane_id, ctx, _started=started):
                    _started.append(lane_id)

                real_pool = executor_v3._new_thread_pool

                def spy_pool(max_workers, _sizes=pool_sizes, _real=real_pool):
                    _sizes.append(max_workers)
                    return _real(max_workers)

                cfg = _cfg(bucket_rate=rate)
                with mock.patch.object(executor_v3, "_lane", fake_lane), \
                     mock.patch.object(executor_v3, "_new_thread_pool", spy_pool):
                    self._run_v3(self._accounts(_phone(0)), [], cfg=cfg)
                self.assertEqual(len(started), expect, f"rate={rate} 应起 {expect} 条通道")
                self.assertEqual(pool_sizes, [expect], "线程池上限 = 通道数 M")

    def test_lanes_run_attempts_concurrently(self):
        """真并发：假 attempt_signin 在线程锁下记峰值并发，串行实现峰值恒为 1。"""
        phones = [_phone(i) for i in range(10)]
        accounts = self._accounts(*phones)
        for p in phones:
            self._add_task(p)
        self._seed_v(8)
        lock = threading.Lock()
        state = {"cur": 0, "peak": 0}
        barrier = threading.Barrier(2, timeout=3)

        def fake_attempt(acc):
            with lock:
                state["cur"] += 1
                state["peak"] = max(state["peak"], state["cur"])
            try:
                barrier.wait()
            except threading.BrokenBarrierError:
                pass
            finally:
                with lock:
                    state["cur"] -= 1
            return (True, "ok", False, "success")

        with mock.patch.object(executor_v3.attempts, "attempt_signin", fake_attempt):
            self._run_v3(accounts)
        self.assertGreater(state["peak"], 1, "M 条通道必须真正并发提交尝试")
        for p in phones:
            self.assertEqual(self._row(p)["state"], "done")

    def test_sleep_until_waits_on_the_live_clock(self):
        """到点等待非阻塞：三条通道共享同一假时钟，总推进量由最晚落点决定。"""
        phones = [_phone(i) for i in range(3)]
        accounts = self._accounts(*phones)
        for p in phones:
            self._add_claimed(p, run_at=_ts(seconds=0.2))
        self._seed_v(8)
        items = [_item(p, run_at=_ts(seconds=0.2)) for p in phones]
        before = self.fc.t
        t_wall = time.monotonic()
        with mock.patch.object(executor_v3.attempts, "attempt_signin",
                               lambda acc: (True, "ok", False, "success")):
            self._run_v3(accounts, items)
        advance = (self.fc.t - before).total_seconds()
        self.assertLess(advance, 0.35,
                        "各通道只等自己的剩余时间（若每条各睡满 0.2s，总推进会到 0.6s）")
        self.assertGreaterEqual(advance, 0.2)
        self.assertLess(time.monotonic() - t_wall, 1.0, "等待必须走 asyncio 而非阻塞睡眠")
        for p in phones:
            self.assertEqual(self._row(p)["state"], "done")


# ---------------------------------------------------------------------------
# 批量领取与收干判据
# ---------------------------------------------------------------------------
class RefillerTest(_Base):
    def test_claim_batch_is_called_with_batch_and_lease(self):
        self._add_task(_phone(1), run_at=_ts(seconds=-1))
        self._seed_v(8)
        claim_kwargs = []

        async def fake_lane(queue, lane_id, ctx):
            while True:
                if (await queue.get())[0] >= executor_v3.SENTINEL_PRIORITY:
                    return

        real_claim = queue_store.claim_batch

        def spy_claim(*a, **kw):
            claim_kwargs.append(kw)
            return real_claim(*a, **kw)

        with mock.patch.object(queue_store, "claim_batch", spy_claim), \
             mock.patch.object(executor_v3, "_lane", fake_lane), \
             mock.patch.object(executor_v3, "_persist_loop", _never):
            executor_v3.run_executor_v3(self._accounts(_phone(1)), day=DAY, cfg=_cfg())
        self.assertTrue(claim_kwargs, "补货必须走 queue_store.claim_batch")
        for kw in claim_kwargs:
            self.assertEqual(kw["limit"], queue_store.CLAIM_BATCH_LIMIT)
            self.assertEqual(kw["lease_sec"], queue_store.LEASE_SECONDS)

    def test_claimed_items_dequeue_by_run_at(self):
        phones = [_phone(i) for i in range(3)]
        for i, sec in enumerate((-30, -20, -10)):
            self._add_claimed(phones[i], run_at=_ts(seconds=sec))
        self._seed_v(8)
        order = []

        async def fake_lane(queue, lane_id, ctx):
            while True:
                item = await queue.get()
                if item[0] >= executor_v3.SENTINEL_PRIORITY:
                    return
                order.append(item[2])

        # 故意乱序投递：出队顺序由 (priority, run_at) 决定，先到点先出队
        items = [_item(phones[2], run_at=_ts(seconds=-10)),
                 _item(phones[0], run_at=_ts(seconds=-30)),
                 _item(phones[1], run_at=_ts(seconds=-20))]
        with mock.patch.object(executor_v3, "_lane", fake_lane):
            self._run_v3(self._accounts(*phones), items)
        self.assertEqual(order, phones, "先到点先出队")

    def test_empty_claim_does_not_end_the_round(self):
        """首轮 claim 返回空（下次到点在 5s 之后）时补货不得判"收干"。"""
        phone = _phone(1)
        self._add_task(phone, run_at=_ts(seconds=5))
        self._seed_v(8)
        seen = []

        async def fake_lane(queue, lane_id, ctx):
            while True:
                item = await queue.get()
                if item[0] >= executor_v3.SENTINEL_PRIORITY:
                    return
                seen.append(item[2])

        with mock.patch.object(executor_v3, "_lane", fake_lane):
            self._run_v3(self._accounts(phone))
        self.assertEqual(seen, [phone], "未到点的待办行最终必须被领到，不能空转到窗口关闭")
        self.assertIn(executor_v3.REFILL_SEC, self.fc.sleeps, "补货按 5s 轮询")
        self.assertGreater(self.fc.t, START, "假时钟被补货轮询推进")

    def test_refiller_drains_when_only_historical_rows_remain(self):
        """M7/U7：只有 `vshard=-1` 的历史行时判"已收干"，不空转到窗口关闭。"""
        self._add_task(_phone(1), vshard=-1, state="failed")
        self._add_task(_phone(2), vshard=-1, state="pending")
        self._seed_v(8)
        ctx = SimpleNamespace(cfg=_cfg(), day=DAY, executor_id=OWNER, m=2,
                              inflight=0, busy=0)
        queue = asyncio.PriorityQueue()
        asyncio.run(executor_v3._refiller(queue, tuple(range(8)), ctx))
        self.assertEqual(self.fc.sleeps, [], "首轮即应判收干，不进入轮询等待")
        sentinels = 0
        while not queue.empty():
            if queue.get_nowait()[0] >= executor_v3.SENTINEL_PRIORITY:
                sentinels += 1
        self.assertEqual(sentinels, 2)


# ---------------------------------------------------------------------------
# 终态映射与 fencing 透传
# ---------------------------------------------------------------------------
class TerminalStateTest(_Base):
    def _run_one(self, status, *, success=False, skip=False, message="m",
                 attempts=0, epoch=1):
        phone = _phone(1)
        self._clear_tasks()
        self._add_claimed(phone, attempts=attempts, epoch=epoch)
        self._seed_v(8)
        settled = []
        real_settle = queue_store.settle_tasks

        def spy_settle(owner, day, outcomes, state="done", epochs=None):
            settled.append((list(outcomes), state, dict(epochs or {})))
            return real_settle(owner, day, outcomes, state=state, epochs=epochs)

        with mock.patch.object(executor_v3.attempts, "attempt_signin",
                               lambda acc: (success, message, skip, status)), \
             mock.patch.object(queue_store, "settle_tasks", spy_settle):
            results = self._run_v3(self._accounts(phone),
                                   [_item(phone, attempts=attempts, epoch=epoch)])
        return phone, results, settled

    def test_done_statuses_settle_done(self):
        for status in ("success", "already", "no_task"):
            with self.subTest(status=status):
                phone, results, settled = self._run_one(status, success=True)
                self.assertEqual(self._row(phone)["state"], "done")
                self.assertEqual(settled[0][1], "done")
                self.assertEqual(settled[0][2], {phone: 1}, "fencing token 必须原样回传")
                self.assertEqual(results[phone][3], status)

    def test_skip_and_exhausted_settle_failed(self):
        cases = (
            ("skipped_window", True, "签到时段已结束", 0),
            ("user_cancelled", True, "用户已取消签到", 0),
            # 无点位：预算 1 次，首试即终态
            ("no_position", False, "未找到签到位置数据", 0),
            # 其它失败：预算 3 次，已试 3 次 → 终态
            ("failed", False, "网络抖动", 2),
        )
        for status, skip, message, attempts in cases:
            with self.subTest(status=status):
                phone, results, settled = self._run_one(
                    status, skip=skip, message=message, attempts=attempts)
                self.assertEqual(self._row(phone)["state"], "failed")
                self.assertEqual(settled[0][1], "failed")
                self.assertEqual(results[phone][2], skip, "skip 原样回传，不重排")

    def test_account_missing_from_round_settles_done_without_request(self):
        phone = _phone(9)
        self._add_claimed(phone)
        self._seed_v(8)
        calls = []
        with mock.patch.object(executor_v3.attempts, "attempt_signin",
                               lambda acc: calls.append(acc.phone) or (True, "ok", False,
                                                                       "success")):
            results = self._run_v3(self._accounts(_phone(1)), [_item(phone)])
        self.assertEqual(calls, [], "账号已不在本轮配置时不得发起请求")
        self.assertEqual(self._row(phone)["state"], "done", "该行必须被了结，否则永远 pending")
        self.assertEqual(results[phone][3], "user_cancelled")

    def test_paused_credential_skips_without_request(self):
        phone = _phone(1)
        self._add_claimed(phone)
        self._seed_v(8)
        cred_state = {phone: {"paused_since": DAY, "probe_date": "2999-01-01"}}
        calls = []
        with mock.patch.object(executor_v3.attempts, "attempt_signin",
                               lambda acc: calls.append(acc.phone) or (True, "ok", False,
                                                                       "success")):
            results = self._run_v3(self._accounts(phone), [_item(phone)],
                                   cred_state=cred_state)
        self.assertEqual(calls, [])
        self.assertEqual(results[phone][3], "paused")
        self.assertEqual(self._row(phone)["state"], "failed")

    def test_cred_state_is_mutated_in_place(self):
        phone = _phone(1)
        self._add_claimed(phone)
        self._seed_v(8)
        cred_state = {}
        with mock.patch.object(executor_v3.attempts, "attempt_signin",
                               lambda acc: (False, "账号或密码错误", False, "failed")):
            self._run_v3(self._accounts(phone), [_item(phone)], cred_state=cred_state)
        self.assertEqual(cred_state[phone]["fail_days"], 1,
                         "必须就地改传入的 dict（调用方持有同一引用并据此保存）")

    def test_user_paused_account_is_reported_as_skipped(self):
        """用户自暂停的账号计划里没有行，汇总口径必须仍是"跳过"而不是"未执行"。"""
        paused = SimpleNamespace(phone=_phone(1), user_paused=True)
        self._seed_v(8)
        results = self._run_v3([paused], [])
        self.assertEqual(results[_phone(1)], (False, "用户已取消签到", True, "user_cancelled"))

    def test_other_shard_accounts_are_delegated(self):
        other = "worker-1@testhost"
        cfg = _cfg(executors=[OWNER, other])
        phones = [_phone(i) for i in range(40)]
        accounts = self._accounts(*phones)
        self._seed_v(64)
        for p in phones:
            self._add_task(p)
        delegated = set()
        with mock.patch.object(executor_v3.attempts, "attempt_signin",
                               lambda acc: (True, "ok", False, "success")):
            self._run_v3(accounts, cfg=cfg, delegated=delegated)
        mine = set(hrw.shards_of(OWNER, cfg["executors"], DAY, 64))
        expect = {p for p in phones if hrw.vshard_of(p, DAY, 64) not in mine}
        self.assertEqual(delegated, expect, "不在本执行体分片集内的账号归 delegated")
        self.assertTrue(expect, "本用例需要至少一个属于别的执行体的账号")


# ---------------------------------------------------------------------------
# 重试与退避
# ---------------------------------------------------------------------------
class RetryTest(_Base):
    def test_risk_failure_requeues_then_exhausts_budget(self):
        phone = _phone(1)
        self._add_claimed(phone, attempts=0, epoch=1)
        self._seed_v(8)
        with mock.patch.object(executor_v3.attempts, "attempt_signin",
                               lambda acc: (False, "e003 风险访问", False, "failed")):
            results = self._run_v3(self._accounts(phone), [_item(phone, attempts=0)],
                                   rng=random.Random(11))
        row = self._row(phone)
        self.assertEqual(row["state"], "pending", "风控类预算 2：第一次失败应重排")
        self.assertEqual(row["attempts"], 1)
        self.assertEqual(row["priority"], 6, "priority 恰好 +1")
        self.assertNotIn(phone, results, "重试中的账号不进 results（最终态才进）")
        delta = (datetime.datetime.strptime(row["run_at"], "%Y-%m-%d %H:%M:%S.%f")
                 - START).total_seconds()
        self.assertGreaterEqual(delta, 60)
        self.assertLessEqual(delta, 180)
        self.assertEqual(self._read_state()[phone]["status"], "retrying")

        # 第二次失败：预算用尽 → 终态 failed
        item = _item(phone, attempts=row["attempts"], epoch=row["epoch"])
        with mock.patch.object(executor_v3.attempts, "attempt_signin",
                               lambda acc: (False, "e003 风险访问", False, "failed")):
            results = self._run_v3(self._accounts(phone), [item])
        self.assertEqual(self._row(phone)["state"], "failed")
        self.assertIn(phone, results)

    def test_no_room_left_settles_failed(self):
        """窗口放不下重试 → 直接终态，不留 pending 行。"""
        phone = _phone(1)
        # 窗口只剩 40s：cap = 12s，落点仍放得下；把窗口设为已过则直接判失败
        self._add_claimed(phone, attempts=0, epoch=1)
        self._seed_v(8)
        cfg = _cfg(sign_end=(6, 40), edge_back_sec=0)
        with mock.patch.object(executor_v3.attempts, "attempt_signin",
                               lambda acc: (False, "网络抖动", False, "failed")):
            results = self._run_v3(self._accounts(phone), [_item(phone)], cfg=cfg)
        self.assertEqual(self._row(phone)["state"], "failed")
        self.assertIn(phone, results)

    def test_next_retry_at_is_bounded_and_reproducible(self):
        cfg = _cfg()
        a = executor_v3.next_retry_at_v3(START, cfg, 60, random.Random(3))
        b = executor_v3.next_retry_at_v3(START, cfg, 60, random.Random(3))
        self.assertEqual(a, b, "同种子可复现")
        delay = (a - START).total_seconds()
        self.assertGreaterEqual(delay, 60)
        self.assertLessEqual(delay, 180)
        self.assertLessEqual(delay, executor_v3.RETRY_CAP_SEC)

    def test_next_retry_at_shrinks_with_remaining_window(self):
        # 有效窗口至 06:41:40（剩余 100s）→ cap = 30s
        cfg = _cfg(sign_start=(6, 30), sign_end=(6, 42), edge_back_sec=20,
                   edge_front_sec=0)
        self.assertAlmostEqual(_remaining(cfg, START), 100, delta=1)
        delay = (executor_v3.next_retry_at_v3(START, cfg, 60, random.Random(5))
                 - START).total_seconds()
        self.assertLessEqual(delay, 30)

    def test_next_retry_at_caps_at_retry_cap(self):
        # 有效窗口至 07:47:40（剩余 4060s）→ cap 应为硬上限 600s
        cfg = _cfg(sign_start=(6, 30), sign_end=(7, 47), edge_back_sec=20,
                   edge_front_sec=0)
        self.assertGreater(_remaining(cfg, START), 3600)
        delay = (executor_v3.next_retry_at_v3(START, cfg, 600, random.Random(5))
                 - START).total_seconds()
        self.assertLessEqual(delay, executor_v3.RETRY_CAP_SEC)

    def test_next_retry_at_never_passes_window_end(self):
        """落点恒在有效窗口内（`cap = 剩余 × 0.3` 给出结构性保证，再夹一次窗口结束）。

        这条是**契约护栏**：`cap` 与窗口结束必须同时成立，任一处被改坏（例如 cap 不再
        按剩余窗口收缩）都会让落点越过窗口——那时本断言会红。
        """
        cfg = _cfg(sign_start=(6, 30), sign_end=(6, 42), edge_back_sec=110,
                   edge_front_sec=0)
        self.assertAlmostEqual(_remaining(cfg, START), 10, delta=1)
        window_end = window.to_dt(START.replace(hour=0, minute=0, second=0),
                                  window.bounds(cfg).hi_min)
        for last_delay in (60, 600, 10 ** 6):
            with self.subTest(last_delay=last_delay):
                target = executor_v3.next_retry_at_v3(
                    START, cfg, last_delay, random.Random(5))
                self.assertGreater(target, START)
                self.assertLessEqual(target, window_end)

    def test_next_retry_at_none_when_window_spent(self):
        cfg = _cfg(sign_start=(6, 30), sign_end=(6, 40), edge_back_sec=0,
                   edge_front_sec=0)
        self.assertEqual(_remaining(cfg, START), 0)
        self.assertIsNone(executor_v3.next_retry_at_v3(START, cfg, 60))


def _remaining(cfg, now_dt):
    """有效窗口剩余秒数（用例侧独立取值，避免与被测模块的内部量互相印证）。"""
    return window.bounds(cfg).remaining_sec(now_dt)


# ---------------------------------------------------------------------------
# 最终放弃时的通知：与 v2 的放弃路径同一函数、同一触发条件
# ---------------------------------------------------------------------------
class GiveUpNotifyTest(_Base):
    """v2 在"重试耗尽 / 窗口放不下重试"时通知管理员与用户各一次；v3 必须对齐。

    触发条件只有**最终放弃**：重试中的失败不通知（否则每次尝试都发一封）。
    `no_position` 是唯一例外——易班侧没有点位非账号/凭据问题，v2 刻意不告警
    （管理员无从修复，重试也拿不到）。
    """

    def _spy_run(self, status, *, message, attempts, skip=False, success=False,
                 notify_url="https://hook.example/x"):
        phone = _phone(1)
        self._clear_tasks()
        self._add_claimed(phone, attempts=attempts, epoch=1)
        self._seed_v(8)
        with mock.patch.object(executor_v3.alerts, "notify_admin_entry") as m_admin, \
             mock.patch.object(executor_v3.alerts, "send_user_fail_mail") as m_user, \
             mock.patch.object(executor_v3.attempts, "attempt_signin",
                               lambda acc: (success, message, skip, status)):
            results = self._run_v3(self._accounts(phone),
                                   [_item(phone, attempts=attempts)],
                                   notify_url=notify_url)
        return phone, results, m_admin, m_user

    def test_final_give_up_notifies_admin_and_user_like_v2(self):
        phone, results, m_admin, m_user = self._spy_run(
            "failed", message="网络抖动", attempts=2)
        self.assertIn(phone, results, "预算用尽即终态")
        m_admin.assert_called_once_with("易班签到失败", [
            ("账号", "138****0001"),
            ("原因", "网络抖动"),
        ], "https://hook.example/x")
        m_user.assert_called_once_with("u@" + phone, phone, "网络抖动")

    def test_retry_only_notifies_nothing(self):
        phone, results, m_admin, m_user = self._spy_run(
            "failed", message="e003 风险访问", attempts=0)
        self.assertNotIn(phone, results, "重试中的账号不是终态")
        self.assertEqual(self._row(phone)["state"], "pending")
        m_admin.assert_not_called()
        m_user.assert_not_called()

    def test_no_room_left_notifies_like_v2(self):
        phone = _phone(1)
        self._add_claimed(phone, attempts=0, epoch=1)
        self._seed_v(8)
        cfg = _cfg(sign_end=(6, 40), edge_back_sec=0)
        with mock.patch.object(executor_v3.alerts, "notify_admin_entry") as m_admin, \
             mock.patch.object(executor_v3.alerts, "send_user_fail_mail") as m_user, \
             mock.patch.object(executor_v3.attempts, "attempt_signin",
                               lambda acc: (False, "网络抖动", False, "failed")):
            results = self._run_v3(self._accounts(phone), [_item(phone)], cfg=cfg)
        self.assertIn(phone, results)
        m_admin.assert_called_once()
        m_user.assert_called_once()

    def test_no_position_does_not_notify(self):
        phone, results, m_admin, m_user = self._spy_run(
            "no_position", message="未找到签到位置数据", attempts=0)
        self.assertIn(phone, results)
        m_admin.assert_not_called()
        m_user.assert_not_called()


# ---------------------------------------------------------------------------
# 最终放弃的留痕：级别与语义与 v2 的放弃路径对齐
# ---------------------------------------------------------------------------
class GiveUpLogTest(_Base):
    """v2 的放弃路径留两条日志，v3 必须同级别同语义地补回。

    无点位只 warning（易班侧没有数据，非账号/凭据问题，管理员无从修复）；其余失败才
    error。两条都只在**最终放弃**时打——重试中的失败不打放弃日志（否则每次尝试刷一条）。
    """

    def _give_up(self, status, *, message, attempts, skip=False, success=False):
        phone = _phone(1)
        self._clear_tasks()
        self._add_claimed(phone, attempts=attempts, epoch=1)
        self._seed_v(8)
        with mock.patch.object(executor_v3.attempts, "attempt_signin",
                               lambda acc: (success, message, skip, status)):
            return phone, self._run_v3(self._accounts(phone),
                                       [_item(phone, attempts=attempts)])

    def test_final_give_up_logs_error_like_v2(self):
        with self.assertLogs("yiban", level="ERROR") as cm:
            phone, results = self._give_up("failed", message="网络抖动", attempts=2)
        self.assertIn(phone, results, "预算用尽即终态")
        joined = "\n".join(cm.output)
        self.assertIn("已尝试 3 次，放弃", joined)
        self.assertIn("网络抖动", joined)
        self.assertNotIn(phone, joined, "手机号必须脱敏后落日志")

    def test_no_position_logs_warning_not_error(self):
        with self.assertLogs("yiban", level="WARNING") as cm:
            phone, results = self._give_up(
                "no_position", message="未找到签到位置数据", attempts=0)
        self.assertIn(phone, results)
        joined = "\n".join(cm.output)
        self.assertIn("易班未返回签到点位", joined)
        self.assertIn("未找到签到位置数据", joined)
        self.assertNotIn(phone, joined, "手机号必须脱敏后落日志")
        self.assertFalse([r for r in cm.records if r.levelno >= logging.ERROR],
                         "无点位不是 error 级，与 v2 同口径")

    def test_retry_only_logs_no_give_up(self):
        with self.assertLogs("yiban", level="WARNING") as cm:
            phone, results = self._give_up("failed", message="e003 风险访问", attempts=0)
        self.assertNotIn(phone, results, "重试中的账号不是终态")
        joined = "\n".join(cm.output)
        self.assertNotIn("放弃", joined)
        self.assertNotIn("易班未返回签到点位", joined)

    def test_window_exhausted_keeps_only_its_own_error(self):
        """窗口放不下重试时 v2 只打"窗口剩余不足"，不得再补一条放弃日志。"""
        phone = _phone(1)
        self._add_claimed(phone, attempts=0, epoch=1)
        self._seed_v(8)
        cfg = _cfg(sign_end=(6, 40), edge_back_sec=0)
        with self.assertLogs("yiban", level="ERROR") as cm, \
             mock.patch.object(executor_v3.attempts, "attempt_signin",
                               lambda acc: (False, "网络抖动", False, "failed")):
            self._run_v3(self._accounts(phone), [_item(phone)], cfg=cfg)
        joined = "\n".join(cm.output)
        self.assertIn("窗口剩余不足", joined)
        self.assertNotIn("放弃", joined, "与 v2 同分支：只留窗口不足这一条")


# ---------------------------------------------------------------------------
# 顶层兜底：未预期异常不外逃
# ---------------------------------------------------------------------------
class UnexpectedErrorTest(_Base):
    """顶层兜底的两档边界：**丢结果**与**只丢收尾**。

    - 拿结果那一段（ctx 构建 → 预扫 → 装桶 → `asyncio.run`）失败：本轮一个请求都
      没走完，只能记 error 并返回空结果——逃逸成 traceback 会让退出码落到契约
      （0/1/2/3/10）之外，处置与"计划不可用"同一口径；
    - 窗口收尾（`_mark_window_skips`）失败：结果集已经成型，**必须原样返回**，
      只把这一段记 error——把已完成的成功/失败改成"无结果"会让 runner 把一轮基本
      成功的活汇总成"全部未执行"（退出码 1 + 失败邮件），与事实相反。
    """

    def test_refiller_exception_is_contained(self):
        phone = _phone(1)
        self._add_claimed(phone)
        self._seed_v(8)
        with mock.patch.object(queue_store, "claim_batch",
                               side_effect=RuntimeError("claim 失败 13800000001")), \
             self.assertLogs("yiban", level="ERROR") as cm:
            results = self._run_v3(self._accounts(phone))
        self.assertEqual(results, {}, "未预期异常按无结果收尾")
        joined = "\n".join(cm.output)
        self.assertIn("未预期异常", joined)
        self.assertNotIn("13800000001", joined, "异常文本落日志前必须脱敏手机号")

    def test_window_skip_failure_keeps_completed_results(self):
        """窗口收尾失败只丢收尾：已完成账号的结果照常返回，不得变成空结果。"""
        phone = _phone(1)
        self._add_claimed(phone)
        self._seed_v(8)
        with mock.patch.object(executor_v3.attempts, "attempt_signin",
                               lambda acc: (True, "签到成功", False, "success")), \
             mock.patch.object(executor_v3, "_mark_window_skips",
                               side_effect=RuntimeError("收尾炸了 13800000001")), \
             self.assertLogs("yiban", level="ERROR") as cm:
            results = self._run_v3(self._accounts(phone), [_item(phone)])
        self.assertEqual(results.get(phone), (True, "签到成功", False, "success"),
                         "窗口收尾失败不得丢掉已完成的账号结果")
        joined = "\n".join(cm.output)
        self.assertIn("v3 窗口收尾失败", joined, "收尾异常必须留下 ERROR 日志")
        self.assertNotIn("13800000001", joined, "异常文本落日志前必须脱敏手机号")

    def test_unexpected_error_does_not_swallow_contract_signals(self):
        """`except Exception` 不得吞掉 KeyboardInterrupt/SystemExit（非 Exception 子类）。"""
        phone = _phone(1)
        self._add_claimed(phone)
        self._seed_v(8)
        with mock.patch.object(queue_store, "claim_batch",
                               side_effect=KeyboardInterrupt), \
             self.assertRaises(KeyboardInterrupt):
            executor_v3.run_executor_v3(self._accounts(phone), day=DAY, cfg=_cfg())



# ---------------------------------------------------------------------------
# dry_run 影子模式：零落库 / 零领取 / 零请求
# ---------------------------------------------------------------------------
class DryRunTest(_Base):
    def test_shadow_stats_reports_plan_distribution(self):
        accounts = self._accounts(*[_phone(i) for i in range(12)])
        calls = []
        with mock.patch.object(queue_store, "claim_batch",
                               side_effect=lambda *a, **kw: calls.append("claim") or []), \
             mock.patch.object(planner, "write_plan",
                               side_effect=lambda *a, **kw: calls.append("write")), \
             mock.patch.object(executor_v3.attempts, "attempt_signin",
                               side_effect=lambda acc: calls.append("attempt")), \
             mock.patch.object(state_io, "_write_sign_state",
                               side_effect=lambda *a, **kw: calls.append("state")):
            stats = executor_v3.shadow_stats(accounts, day=DAY, cfg=_cfg())
        for key in ("n", "hist", "peak_per_sec", "rate_peak", "lam", "slot_width_ms"):
            self.assertIn(key, stats)
        self.assertEqual(stats["n"], 12)
        self.assertGreaterEqual(stats["peak_per_sec"], 1)
        self.assertEqual(calls, [], "影子模式不得领取/落库/发请求/写状态")
        self.assertEqual(self._task_count(), 0, "影子模式不写 sign_tasks")

    def test_run_executor_v3_dry_run_returns_empty_and_writes_nothing(self):
        accounts = self._accounts(*[_phone(i) for i in range(5)])
        calls = []
        with mock.patch.object(queue_store, "claim_batch",
                               side_effect=lambda *a, **kw: calls.append("claim") or []), \
             mock.patch.object(planner, "write_plan",
                               side_effect=lambda *a, **kw: calls.append("write")), \
             mock.patch.object(executor_v3.attempts, "attempt_signin",
                               side_effect=lambda acc: calls.append("attempt")):
            results = executor_v3.run_executor_v3(
                accounts, day=DAY, dry_run=True, cfg=_cfg())
        self.assertEqual(results, {})
        self.assertEqual(calls, [])
        self.assertEqual(self._task_count(), 0)
        self.assertFalse(os.path.exists(self._state_path()), "dry_run 不写 sign-state")


# ---------------------------------------------------------------------------
# 令牌桶三件接线（按最终语义）
# ---------------------------------------------------------------------------
class LimiterWiringTest(_Base):
    def test_limiter_and_gate_are_used_around_every_attempt(self):
        phone = _phone(1)
        self._add_claimed(phone)
        self._seed_v(8)
        trace = []
        limiter = _PermissiveLimiter(trace)
        gate = _PermissiveGate(trace=trace)
        with mock.patch.object(executor_v3.attempts, "attempt_signin",
                               lambda acc: trace.append(("attempt", acc.phone))
                               or (True, "ok", False, "success")):
            self._run_v3(self._accounts(phone), [_item(phone)], limiter=limiter,
                         gate=gate)
        self.assertEqual(limiter.restored, 1, "起跑装回一次桶状态")
        self.assertIn(("restore", OWNER), trace)
        kinds = [k for k, _ in trace]
        self.assertEqual(kinds[:3], ["restore", "acquire", "allow"],
                         "取额度 → 判 gap → 才发起尝试")
        self.assertLess(kinds.index("commit"), kinds.index("attempt"),
                        "gap 在真正发起尝试时才推进")
        self.assertIn("on_success", kinds)

    def test_risk_message_triggers_backoff_hook(self):
        phone = _phone(1)
        self._add_claimed(phone, attempts=1)
        self._seed_v(8)
        trace = []
        limiter = _PermissiveLimiter(trace)
        with mock.patch.object(executor_v3.attempts, "attempt_signin",
                               lambda acc: (False, "风险访问 拦截", False, "failed")):
            self._run_v3(self._accounts(phone), [_item(phone, attempts=1)],
                         limiter=limiter)
        self.assertIn(("on_risk_signal", OWNER), trace)
        self.assertNotIn("on_success", [k for k, _ in trace])

    def test_persist_loop_writes_every_interval(self):
        limiter = _PermissiveLimiter()
        ctx = SimpleNamespace(limiter=limiter, egress=OWNER)
        seen = []

        async def fake_sleep(sec):
            seen.append(sec)
            if len(seen) >= 3:
                raise _Stop

        with mock.patch.object(executor_v3, "_sleep", fake_sleep), \
             self.assertRaises(_Stop):
            asyncio.run(executor_v3._persist_loop(ctx))
        self.assertEqual(seen, [executor_v3.EGRESS_PERSIST_SEC] * 3)
        self.assertEqual(limiter.persisted, 2, "每 10s 落库一次（第三次睡到就被打断）")
        self.assertEqual(executor_v3.EGRESS_PERSIST_SEC, 10) # 连周期常量一起钉：改了它，上面按 3 次 sleep 推出的 2 次落库就失去意义

    def test_manual_rate_is_a_ceiling_but_risk_still_backs_off(self):
        with mock.patch.dict(os.environ, {"YIBAN_EGRESS_RATE": "0.5"}):
            limiter = token_bucket.limiter_from_env(channels=6)
        self.assertTrue(limiter.manual, "显式写了速率 = 人工接管")
        for _ in range(token_bucket.SUCCESS_STREAK + 50):
            limiter.on_success(OWNER)
        self.assertAlmostEqual(limiter.bucket(OWNER).rate, 0.5, places=6,
                               msg="人工接管下上探 no-op")
        limiter.on_risk_signal(OWNER, 1000.0)
        self.assertAlmostEqual(limiter.bucket(OWNER).rate, 0.25, places=6,
                               msg="风控的乘性回退照做")

    def test_global_limiter_semantics(self):
        with mock.patch.object(token_bucket.logger, "warning") as warn:
            empty = token_bucket.GlobalLimiter(os.environ.get("YIBAN_GLOBAL_RATE", ""))
        self.assertIsNone(empty.lam)
        self.assertFalse(empty.invalid)
        warn.assert_not_called()

        with self.assertLogs("yiban.engine.token_bucket", level="WARNING") as cm:
            bad = token_bucket.GlobalLimiter("abc")
        self.assertTrue(bad.invalid)
        self.assertIsNone(bad.lam)
        self.assertIn("非法", "\n".join(cm.output))

        lam2 = token_bucket.GlobalLimiter("2")
        allowed = sum(1 for t in (0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9)
                      if lam2.acquire(t))
        self.assertLessEqual(allowed, 2, "Λ=2 时 1 秒内放行不超过 2 条")

    def test_global_limiter_env_key_is_wired(self):
        self.assertEqual(executor_v3.ENV_GLOBAL_RATE, "YIBAN_GLOBAL_RATE")
        with mock.patch.dict(os.environ, {"YIBAN_GLOBAL_RATE": "3"}):
            self.assertAlmostEqual(executor_v3._make_global_limiter().lam, 3.0)
        with mock.patch.dict(os.environ, {"YIBAN_GLOBAL_RATE": "abc"}):
            bad = executor_v3._make_global_limiter()
        self.assertTrue(bad.invalid, "非法值告警 + invalid，但不拒绝启动")
        self.assertIsNone(bad.lam)

    def test_invalid_global_rate_does_not_block_the_round(self):
        phone = _phone(1)
        self._add_claimed(phone)
        self._seed_v(8)
        with mock.patch.dict(os.environ, {"YIBAN_GLOBAL_RATE": "abc"}), \
             mock.patch.object(executor_v3.attempts, "attempt_signin",
                               lambda acc: (True, "ok", False, "success")):
            results = self._run_v3(self._accounts(phone), [_item(phone)])
        self.assertEqual(results[phone][3], "success")


class _Stop(Exception):
    """打断 `_persist_loop` 的用例信号（不依赖 asyncio 的取消语义）。"""


# ---------------------------------------------------------------------------
# 产品契约：状态物化与快照
# ---------------------------------------------------------------------------
class ProductContractTest(_Base):
    def test_every_attempt_materialises_sign_state(self):
        phones = [_phone(1), _phone(2)]
        for p in phones:
            self._add_claimed(p)
        self._seed_v(8)
        events = []

        def fake_attempt(acc):
            if acc.phone == _phone(1):
                return (True, "签到成功", False, "success")
            return (False, "e003 风险访问", False, "failed")

        with mock.patch.object(executor_v3.attempts, "attempt_signin", fake_attempt):
            self._run_v3(self._accounts(*phones), [_item(p) for p in phones],
                         event_sink=events.append)
        state = self._read_state()
        self.assertEqual(state[_phone(1)]["status"], "success")
        self.assertEqual(state[_phone(2)]["status"], "retrying",
                         "重试入队也要物化状态（网页日历不能空窗）")
        self.assertEqual({e["phone"] for e in events}, set(phones))
        self.assertEqual({e["stage"] for e in events}, {"sign"})
        self.assertTrue(all(e["attempt"] for e in events))

    def test_dry_run_leaves_state_file_untouched(self):
        accounts = self._accounts(_phone(1))
        with open(self._state_path(), "w", encoding="utf-8") as f:
            json.dump({_phone(1): {"status": "success", "message": "旧"}}, f)
        before = self._read_state()
        executor_v3.run_executor_v3(accounts, day=DAY, dry_run=True, cfg=_cfg())
        self.assertEqual(self._read_state(), before)

    def test_v3_does_not_write_sched_snapshot(self):
        """分流点选在 runner 的执行调用处：快照仍由 runner 的写盘点承担，v3 不重复写。"""
        phone = _phone(1)
        self._add_claimed(phone)
        self._seed_v(8)
        with mock.patch.object(executor_v3.attempts, "attempt_signin",
                               lambda acc: (True, "ok", False, "success")):
            self._run_v3(self._accounts(phone), [_item(phone)])
        self.assertFalse(
            os.path.exists(os.path.join(self.state_dir, f"sched-snapshot-{DAY}.json")))

    def test_window_closed_marks_remaining_accounts_skipped(self):
        """起跑即窗口已过：本分片集内的账号落 skipped_window（退出码 2 的前提）。"""
        accounts = self._accounts(*[_phone(i) for i in range(3)])
        self._seed_v(8)
        self.fc.t = START.replace(hour=8, minute=0)
        calls = []
        with mock.patch.object(executor_v3.attempts, "attempt_signin",
                               lambda acc: calls.append(acc.phone) or (True, "ok",
                                                                       False, "success")):
            results = self._run_v3(accounts)
        self.assertEqual(calls, [], "窗口外不得发起任何请求")
        for acc in accounts:
            self.assertEqual(results[acc.phone][3], "skipped_window")
            self.assertTrue(results[acc.phone][2], "跳过类不进失败计数")
        self.assertEqual(self._read_state()[_phone(0)]["status"], "skipped_window")

    def test_window_closed_keeps_existing_conclusion(self):
        accounts = self._accounts(_phone(1))
        self._seed_v(8)
        self.fc.t = START.replace(hour=8, minute=0)
        with open(self._state_path(), "w", encoding="utf-8") as f:
            json.dump({_phone(1): {"status": "failed", "message": "真实失败"}}, f)
        results = self._run_v3(accounts)
        self.assertEqual(results[_phone(1)][3], "failed", "已有结论按原结论透传")
        self.assertEqual(self._read_state()[_phone(1)]["message"], "真实失败")

    def test_write_plan_failure_is_contained(self):
        accounts = self._accounts(*[_phone(i) for i in range(3)])
        calls = []
        with mock.patch.object(planner, "write_plan", side_effect=RuntimeError("库坏了")), \
             mock.patch.object(executor_v3.attempts, "attempt_signin",
                               lambda acc: calls.append(acc.phone) or (True, "ok",
                                                                       False, "success")), \
             self.assertLogs("yiban", level="ERROR") as cm:
            results = executor_v3.run_executor_v3(accounts, day=DAY, cfg=_cfg())
        self.assertEqual(results, {})
        self.assertEqual(calls, [], "没有队列就不发请求")
        self.assertIn("当日计划不可用", "\n".join(cm.output))


# ---------------------------------------------------------------------------
# runner 分流：缺省 0 时 v2 路径逐字不变
# ---------------------------------------------------------------------------
class RunnerSplitTest(unittest.TestCase):
    """`runner.main` 的分流点：开关缺省关时只有一行之差（执行体实现）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-v3-runner-")
        self._env = {k: os.environ.get(k) for k in (
            "YIBAN_STATE_DIR", "YIBAN_DB_FILE", "YIBAN_LOG_FILE",
            "YIBAN_ENV_FILE", "YIBAN_SCHEDULER_V3", "YIBAN_GLOBAL_PAUSE",
            "YIBAN_SECOND_RUN", "YIBAN_EXECUTOR_ID")}
        os.environ.update({
            "YIBAN_STATE_DIR": self.tmp,
            "YIBAN_DB_FILE": os.path.join(self.tmp, "yiban.db"),
            "YIBAN_LOG_FILE": os.path.join(self.tmp, "sign.log"),
        })
        for k in ("YIBAN_SCHEDULER_V3", "YIBAN_GLOBAL_PAUSE", "YIBAN_SECOND_RUN",
                  "YIBAN_EXECUTOR_ID"):
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

    def _run(self, argv=None, outcome=None):
        accounts = [SimpleNamespace(phone=_phone(0), user_paused=False, owner="u@1")]
        sched = {_phone(0): START}
        cred = {"seed": 1}
        # 两个执行体替身返回**同形**的结果：退出码由 runner 的汇总算出，与谁执行无关
        outcome = dict(outcome) if outcome is not None else {
            _phone(0): (True, "签到成功", False, "success")}
        retry_calls = []
        v3_calls = []
        with mock.patch.object(runner_mod.accounts_mod, "load_accounts",
                               return_value=accounts), \
             mock.patch.object(runner_mod.schedule_mod, "build_schedule",
                               return_value=sched), \
             mock.patch.object(runner_mod.round_mod, "run_queue_retry",
                               side_effect=lambda *a, **kw: (retry_calls.append((a, kw)),
                                                             dict(outcome))[1]), \
             mock.patch.object(runner_mod.executor_v3, "run_executor_v3",
                               side_effect=lambda *a, **kw: (v3_calls.append((a, kw)),
                                                             dict(outcome))[1]), \
             mock.patch.object(runner_mod.state_io, "_load_cred_state",
                               return_value=cred), \
             mock.patch.object(runner_mod.state_io, "_save_cred_state"), \
             mock.patch.object(runner_mod.state_io, "_is_second_run",
                               return_value=False), \
             mock.patch.object(runner_mod.state_io, "_write_sched_done"), \
             mock.patch.object(runner_mod.db, "add_sign_events_batch"), \
             mock.patch.object(runner_mod.db, "purge_expired_deleted_accounts"), \
             mock.patch.object(runner_mod.alerts, "_maybe_alert_zero_success"), \
             mock.patch.object(runner_mod.alerts, "_flush_admin_mail_summary"):
            code = runner_mod.main(argv or [])
        return code, accounts, sched, cred, retry_calls, v3_calls

    def test_default_off_keeps_the_v2_call_verbatim(self):
        code, accounts, sched, cred, retry_calls, v3_calls = self._run()
        self.assertEqual(code, 0, "退出码由 runner 按 results 汇总（成功 → 0）")
        self.assertEqual(v3_calls, [], "缺省 0 时 run_executor_v3 零调用")
        self.assertEqual(len(retry_calls), 1)
        args, kw = retry_calls[0]
        self.assertEqual(args[0], accounts)
        self.assertEqual(args[1], "")
        self.assertIs(kw["schedule"], sched, "schedule 按引用透传，不复制")
        self.assertIs(kw["cred_state"], cred, "cred_state 按引用透传")
        self.assertIsInstance(kw["delegated"], set)
        self.assertTrue(callable(kw["event_sink"]))
        self.assertFalse(kw["reclaim"])

    def test_switch_on_routes_to_v3_and_skips_v2(self):
        os.environ["YIBAN_SCHEDULER_V3"] = "1"
        code, accounts, _sched, cred, retry_calls, v3_calls = self._run()
        self.assertEqual(retry_calls, [], "开关打开时不再走 v2 执行体")
        self.assertEqual(len(v3_calls), 1)
        args, kw = v3_calls[0]
        self.assertEqual(args[0], accounts)
        self.assertEqual(kw["notify_url"], "")
        self.assertIs(kw["cred_state"], cred)
        self.assertIsInstance(kw["delegated"], set)
        self.assertTrue(callable(kw["event_sink"]))
        self.assertEqual(code, 0, "退出码汇总仍归 runner，与执行体实现无关")

    def test_only_never_routes_to_v3(self):
        os.environ["YIBAN_SCHEDULER_V3"] = "1"
        _code, _accounts, _sched, _cred, retry_calls, v3_calls = self._run(
            ["--only", _phone(0)])
        self.assertEqual(v3_calls, [], "--only 是用户主动触发，不走 v3")
        self.assertEqual(len(retry_calls), 1)
        self.assertTrue(retry_calls[0][1]["reclaim"], "手动签到允许重签当日已了结账号")

    def test_falsy_switch_value_keeps_v2(self):
        for raw in ("0", "false", "nonsense"):
            os.environ["YIBAN_SCHEDULER_V3"] = raw
            with self.subTest(raw=raw):
                _, _, _, _, retry_calls, v3_calls = self._run()
                self.assertEqual(v3_calls, [])
                self.assertEqual(len(retry_calls), 1)

    def test_empty_results_from_executor_yield_contract_exit_code(self):
        """执行体返回空结果（**丢结果**那一档兜底的输出）时，runner 仍给出契约内退出码。"""
        os.environ["YIBAN_SCHEDULER_V3"] = "1"
        code, *_ = self._run(outcome={})
        self.assertIn(code, (0, 1, 2, 3, 10))
        self.assertEqual(code, 1, "无结果按失败汇总，不得落到契约之外")

    def test_retained_results_from_executor_yield_contract_exit_code(self):
        """执行体保留已完成结果（**只丢收尾**那一档的输出）时，退出码同样在契约内。

        窗口收尾失败返回的是"已完成账号的结果集"，不是空结果——runner 按它汇总出
        成功（0），而不是把一轮基本成功的活报成"全部未执行"（1 + 失败邮件）。
        """
        os.environ["YIBAN_SCHEDULER_V3"] = "1"
        code, *_ = self._run(outcome={_phone(0): (True, "签到成功", False, "success")})
        self.assertIn(code, (0, 1, 2, 3, 10))
        self.assertEqual(code, 0, "保留的结果集按真实结论汇总，不得落到契约之外")


# ---------------------------------------------------------------------------
# 崩溃恢复接线：起跑回收 + 崩溃行被重新领取 + 文件心跳四态
# ---------------------------------------------------------------------------
class RecoveryWiringTest(_Base):
    """v3 的崩溃恢复：回收必须在领取循环里被调用，否则崩溃行永不被重领。

    另外让 v3 写**既有文件心跳**，使 `/api/scheduler/executors*` 的四态判定对 v3
    也成立（否则单进程路径没有心跳记录，页面把正在跑的 v3 显示成 `idle`）。
    """

    def test_round_start_reaps_expired_claims(self):
        phone = _phone(1)
        self._add_claimed(phone)
        self._seed_v(8)
        calls = []
        real = queue_store.reap_expired

        def spy(*a, **kw):
            calls.append(kw)
            return real(*a, **kw)

        with mock.patch.object(queue_store, "reap_expired", spy), \
             mock.patch.object(executor_v3.attempts, "attempt_signin",
                               lambda acc: (True, "ok", False, "success")):
            self._run_v3(self._accounts(phone), [_item(phone)])
        self.assertEqual(len(calls), 1, "每轮起跑必须调用一次回收")

    def test_reap_is_scoped_to_the_business_day(self):
        """回收只碰本业务日的行：跨日回收会把历史行与跨午夜长轮次的在飞行一起清掉。

        不带 `day` 时，次日进程会把**昨天**的过期 `claimed` 行回退成 `pending`——而
        `claim_batch` 只按当日领取，历史行只会变成"看着有活、其实无人领"的空转行；
        跨午夜仍在飞的当日行也会被次日进程重置（同一账号重复真实登录）。
        """
        today, stale = _phone(1), _phone(2)
        self._add_task(today, vshard=0, state="claimed", owner="worker-9@testhost",
                       lease_until=_ts(seconds=-180), epoch=1, run_at=_ts(seconds=-60))
        self._add_task(stale, vshard=0, state="claimed", owner="worker-9@testhost",
                       lease_until=_ts(seconds=-180), epoch=1, run_at=_ts(seconds=-60),
                       day="2026-09-21")
        self._seed_v(8)
        calls = []
        real = queue_store.reap_expired

        def spy(*a, **kw):
            calls.append(kw)
            return real(*a, **kw)

        with mock.patch.object(queue_store, "reap_expired", spy), \
             mock.patch.object(executor_v3.attempts, "attempt_signin",
                               lambda acc: (True, "ok", False, "success")):
            self._run_v3(self._accounts(today))
        self.assertTrue(calls, "本轮必须调用回收")
        self.assertEqual([c.get("day") for c in calls], [DAY] * len(calls),
                         "回收必须带本业务日，否则会跨业务日回收历史行")
        self.assertEqual(self._row(today)["state"], "done", "同日的过期行按宽限期回收")
        old = self._row(stale, day="2026-09-21")
        self.assertEqual((old["state"], old["owner"], old["epoch"]),
                         ("claimed", "worker-9@testhost", 1),
                         "异日的过期 claimed 行不得被回收")

    def test_refill_loop_reap_is_scoped_to_the_business_day(self):
        """补货循环里那次回收也必须带 `day`：它是长轮次里唯一的周期回收点。

        起跑回收与补货循环回收是**两处调用**。只钉起跑那处的话，补货那处丢了 `day` 也
        不会变红（短轮次在补货首轮的计时差恒为 0，真实 `RECOVER_SEC` 下根本走不到），
        而它正是跨日回收历史行、重置跨午夜在飞行的入口。故把恢复间隔置 0 驱动它执行。
        """
        phone = _phone(1)
        self._add_claimed(phone)
        self._seed_v(8)
        calls = []
        real = queue_store.reap_expired

        def spy(*a, **kw):
            calls.append(kw)
            return real(*a, **kw)

        with mock.patch.object(executor_v3, "RECOVER_SEC", 0), \
             mock.patch.object(queue_store, "reap_expired", spy), \
             mock.patch.object(executor_v3.attempts, "attempt_signin",
                               lambda acc: (True, "ok", False, "success")):
            self._run_v3(self._accounts(phone))
        self.assertGreaterEqual(len(calls), 2,
                                "起跑与补货循环各调一次回收（置 0 后补货那处必被执行）")
        self.assertEqual([c.get("day") for c in calls], [DAY] * len(calls),
                         "两处回收都必须带本业务日，否则会跨业务日回收历史行")

    def test_expired_claim_is_reclaimed_and_completed(self):
        """崩溃执行体留下的过期 `claimed` 行（已过回收宽限期）必须被回收、重领、跑完。"""
        phone = _phone(1)
        self._add_task(phone, vshard=0, state="claimed", owner="worker-9@testhost",
                       lease_until=_ts(seconds=-180), epoch=1, run_at=_ts(seconds=-60))
        self._seed_v(8)
        with mock.patch.object(executor_v3.attempts, "attempt_signin",
                               lambda acc: (True, "ok", False, "success")):
            results = self._run_v3(self._accounts(phone))
        row = self._row(phone)
        self.assertEqual(row["state"], "done", "崩溃行必须被重新领取并完成")
        self.assertEqual(row["owner"], OWNER)
        self.assertGreater(row["epoch"], 1, "重领同样自增 epoch")
        self.assertIn(phone, results)

    def test_claim_just_expired_is_not_reclaimed_this_round(self):
        """租约刚过期（仍在宽限期内）的行**不回收**：持有者可能还在飞。

        若按"过期即回收"，本轮会把它回退 `pending` 再领一次 ⇒ 同一账号两条通道并发
        登录；`epoch+1` 只挡迟到的结论写回，挡不住这次重复真实登录。
        """
        phone = _phone(1)
        self._add_task(phone, vshard=0, state="claimed", owner="worker-9@testhost",
                       lease_until=_ts(seconds=-30), epoch=1, run_at=_ts(seconds=-60))
        self._seed_v(8)
        with mock.patch.object(executor_v3.attempts, "attempt_signin",
                               mock.MagicMock(return_value=(True, "ok", False,
                                                            "success"))) as attempt:
            results = self._run_v3(self._accounts(phone))
        row = self._row(phone)
        self.assertEqual((row["state"], row["owner"], row["epoch"]),
                         ("claimed", "worker-9@testhost", 1), "在飞的行不得被回收重领")
        self.assertEqual(attempt.call_count, 0, "本轮不得对该账号再发一次真实登录")
        self.assertNotIn(phone, results)

    def test_heartbeat_reports_running_then_finished(self):
        phone = _phone(1)
        self._add_claimed(phone)
        self._seed_v(8)
        seen = []
        real = executor_v3._run_async

        async def spy(ctx):
            seen.append(state_io.worker_presence(0))
            return await real(ctx)

        with mock.patch.object(executor_v3, "_run_async", spy), \
             mock.patch.object(executor_v3.attempts, "attempt_signin",
                               lambda acc: (True, "ok", False, "success")):
            self._run_v3(self._accounts(phone), [_item(phone)])
        self.assertEqual([s[0] for s in seen], [state_io.WORKER_STATE_RUNNING],
                         "起跑写心跳：执行体页必须能判 running（不再显示 idle）")
        self.assertEqual(state_io.worker_presence(0)[0], state_io.WORKER_STATE_FINISHED,
                         "收尾正常路径写心跳：判 finished")


class WorkerFinishMarkTest(_Base):
    """收尾标记只在**正常返回路径**写：异常/中断不得被记成"正常跑完"。

    与监督进程同口径——被信号杀掉时不写收尾，留"有开始、无收尾"让四态判 `stale`，
    用户才会注意到"疑似被强杀"。
    """

    def test_normal_return_marks_finished(self):
        phone = _phone(1)
        self._add_claimed(phone)
        self._seed_v(8)
        with mock.patch.object(executor_v3.attempts, "attempt_signin",
                               lambda acc: (True, "ok", False, "success")):
            self._run_v3(self._accounts(phone), [_item(phone)])
        self.assertEqual(state_io.worker_presence(0)[0], state_io.WORKER_STATE_FINISHED,
                         "正常跑完必须写收尾标记")

    def test_interrupt_does_not_write_finish_mark(self):
        phone = _phone(1)
        self._add_claimed(phone)
        self._seed_v(8)

        async def boom(ctx):
            raise KeyboardInterrupt

        with mock.patch.object(executor_v3, "_run_async", boom), \
                self.assertRaises(KeyboardInterrupt):
            self._run_v3(self._accounts(phone), [_item(phone)])
        later = self.fc.now() + datetime.timedelta(
            seconds=3 * state_io.WORKER_HEARTBEAT_SEC)
        self.assertEqual(state_io.worker_presence(0, now=later)[0],
                         state_io.WORKER_STATE_STALE,
                         "中断不得写收尾：心跳过期后要判 stale，而不是 finished")

    def test_unexpected_exception_does_not_write_finish_mark(self):
        """未预期 `Exception` 也走"丢结果"档：不写收尾，心跳过期后判 `stale`。

        本用例钉的是**单进程直跑**这一支（没有监督进程替它写收尾）。受监督路径同口径由
        `workers._await_workers` 决定：子进程以退出码正常退出（含"本轮失败"的 1）时会写
        `finished`，失败信息靠退出码与告警体现——那是另一条路径，不改变这里的契约。
        """
        phone = _phone(1)
        self._add_claimed(phone)
        self._seed_v(8)

        async def boom(ctx):
            raise RuntimeError("未预期的内部异常")

        with mock.patch.object(executor_v3, "_run_async", boom):
            out = self._run_v3(self._accounts(phone), [_item(phone)])
        self.assertEqual(out, {}, "未预期异常按无结果收尾（不外逃）")
        later = self.fc.now() + datetime.timedelta(
            seconds=3 * state_io.WORKER_HEARTBEAT_SEC)
        self.assertEqual(state_io.worker_presence(0, now=later)[0],
                         state_io.WORKER_STATE_STALE,
                         "未预期异常不得写收尾：心跳过期后判 stale，而不是 finished")


class DeadPeerTakeoverTest(_Base):
    """死主分片接管的接线：`steal_shards` 要**两个身份**（接管者 + 死主），别传错。"""

    def test_dead_peer_pending_rows_are_taken_over_and_shards_widened(self):
        peer = "worker-2@testhost"
        v = 8
        self._seed_v(v)
        cfg = _cfg(executors=[OWNER, peer])
        peer_shards = hrw.shards_of(peer, cfg["executors"], DAY, v)
        self.assertTrue(peer_shards, "夹具前提：死主必须有分片，否则本用例什么都不测")
        shard = peer_shards[0]
        self._add_task(_phone(1), vshard=shard, state="pending", owner=peer, epoch=1)
        self._add_task(_phone(2), vshard=shard, state="pending", owner=OWNER, epoch=1)
        # 死主的心跳：有开始记录、无收尾且已过期（`worker_presence` 判 stale）
        state_io.mark_worker_started(
            executor_v3._worker_slot(peer),
            now=self.fc.now() - datetime.timedelta(seconds=5 * state_io.WORKER_HEARTBEAT_SEC))
        ctx = SimpleNamespace(executor_id=OWNER, cfg=cfg, day=DAY, v=v)

        out = executor_v3._widen_with_dead_peers(ctx, ())

        self.assertEqual(self._row(_phone(1))["owner"], OWNER,
                         "死主的 pending 行必须改归本执行体（dead_owner 要传死主）")
        self.assertEqual(self._row(_phone(2))["owner"], OWNER, "本执行体自己的行不动")
        self.assertEqual(self._row(_phone(2))["epoch"], 1, "自己的行不得被自增 epoch")
        self.assertEqual(out, tuple(sorted(peer_shards)), "死主分片并入本轮领取范围")


class DeadPeerTakeoverChainTest(_Base):
    """接管链路要一路走到"本轮真的领到"：判死 → 改归 → 并入领取集 → `claim_batch` 领出。

    `steal_shards` 的单测只证明行被改归本执行体。只改归属、不把死主分片并入领取集，
    本执行体不会去扫那些分片，行就成了"改了却领不到"——单测看不出的那一跳。故本用例跑
    **真实补货**（不注入假条目），断言死主的行在本轮被领取并执行。

    `RECOVER_SEC` 置 0 保留：起跑那次接管（`DeadPeerTakeoverOnStartTest`）会在本用例的
    死主上先命中，但补货循环里的那次接管仍是**长轮次中途死主**的唯一出路，置 0 让它在
    每次补货迭代都执行到，覆盖"补货循环真的会调接管"这一跳。起跑触发另由
    `DeadPeerTakeoverOnStartTest` 单独钉住（不 patch `RECOVER_SEC`）。
    """

    def test_dead_peer_rows_are_claimed_and_executed_this_round(self):
        peer = "worker-2@testhost"
        v = 8
        self._seed_v(v)
        cfg = _cfg(executors=[OWNER, peer])
        peer_shards = hrw.shards_of(peer, cfg["executors"], DAY, v)
        self.assertTrue(peer_shards, "夹具前提：死主必须有分片，否则本用例什么都不测")
        phones = [_phone(1), _phone(2)]
        for i, phone in enumerate(phones):
            self._add_task(phone, vshard=peer_shards[i % len(peer_shards)],
                           state="pending", owner=peer, epoch=1, run_at=_ts(seconds=-30))
        # 死主的心跳：有开始记录、无收尾且已过期（`worker_presence` 判 stale）
        state_io.mark_worker_started(
            executor_v3._worker_slot(peer),
            now=self.fc.now() - datetime.timedelta(seconds=5 * state_io.WORKER_HEARTBEAT_SEC))
        ran = []

        def _attempt(acc):
            ran.append(acc.phone)
            return (True, "ok", False, "success")

        # 补货循环的恢复间隔置 0：首轮就触发"判死 + 并入"（真实间隔是 60s，用例不真等）
        with mock.patch.object(executor_v3, "RECOVER_SEC", 0), \
             mock.patch.object(executor_v3.attempts, "attempt_signin", _attempt):
            results = self._run_v3(self._accounts(*phones), cfg=cfg)
        self.assertEqual(sorted(ran), sorted(phones),
                         "死主的行必须在本轮被领取并执行（并入领取集那一跳不能少）")
        for phone in phones:
            row = self._row(phone)
            self.assertEqual(row["owner"], OWNER, "接管后 owner 是本执行体")
            self.assertGreater(row["epoch"], 1, "接管自增 epoch（fencing）")
            self.assertEqual(row["state"], "done")
            self.assertIn(phone, results)


class DeadPeerTakeoverOnStartTest(_Base):
    """起跑就接管：短轮次（干完自己的活即收干退出）也必须领到死主分片的 `pending` 行。

    补货循环里的接管要活满 `RECOVER_SEC`（60s）才触发，而补货首轮的计时差恒为 0——
    本执行体干完自己的活就退出，等不到那一刻；补签轮沿用同一套分片划分，死主的行仍
    无人领 ⇒ **静默漏签**。故起跑阶段（回收之后、首轮领取之前）就要判死接管。
    本用例**不 patch `RECOVER_SEC`**：能通过只可能是起跑那次接管生效（补货循环的
    接管在本轮的时长内根本不会触发）。
    """

    def test_dead_peer_rows_claimed_this_short_round_without_recover_patch(self):
        peer = "worker-2@testhost"
        v = 8
        self._seed_v(v)
        cfg = _cfg(executors=[OWNER, peer])
        peer_shards = hrw.shards_of(peer, cfg["executors"], DAY, v)
        self.assertTrue(peer_shards, "夹具前提：死主必须有分片，否则本用例什么都不测")
        phones = [_phone(1), _phone(2)]
        for i, phone in enumerate(phones):
            self._add_task(phone, vshard=peer_shards[i % len(peer_shards)],
                           state="pending", owner=peer, epoch=1, run_at=_ts(seconds=-30))
        # 死主的心跳：有开始记录、无收尾且已过期（`worker_presence` 判 stale）
        state_io.mark_worker_started(
            executor_v3._worker_slot(peer),
            now=self.fc.now() - datetime.timedelta(seconds=5 * state_io.WORKER_HEARTBEAT_SEC))
        ran = []

        def _attempt(acc):
            ran.append(acc.phone)
            return (True, "ok", False, "success")

        # 真实补货（不注入假条目）、真实 RECOVER_SEC：短轮次不等 60s 也要接管
        with mock.patch.object(executor_v3.attempts, "attempt_signin", _attempt):
            results = self._run_v3(self._accounts(*phones), cfg=cfg)
        self.assertEqual(sorted(ran), sorted(phones),
                         "死主的行必须在本轮被领取并执行（起跑接管那一跳不能少）")
        for phone in phones:
            row = self._row(phone)
            self.assertEqual(row["owner"], OWNER, "接管后 owner 是本执行体")
            self.assertGreater(row["epoch"], 1, "接管自增 epoch（fencing）")
            self.assertEqual(row["state"], "done")
            self.assertIn(phone, results)


class DeadPeerClaimedOnlyTakeoverTest(_Base):
    """死主把分片内的待办**全领成 `claimed` 后崩**：判死即并入，不能以"偷到几行"为门。

    `reap_expired` 把过期 `claimed` 行回退成 `pending` 时**清空 `owner`**，此时该分片
    内已没有 `owner=<死主>` 的 `pending` 行，`steal_shards` 返回 0。若把"并入死主分片"
    挂在 `taken > 0` 上，分片就不进本轮领取集 ⇒ 刚被回收成 `pending` 的行无人可领 =
    "崩溃即卡死"复现（补货循环那次接管判的是同一个 peer，同样 `taken=0`，救不回来）。
    故并入与"偷到多少行"解耦：`steal_shards` 的返回值只用于日志与归属修正。
    """

    def test_claimed_only_dead_peer_shards_are_reclaimed_and_executed(self):
        peer = "worker-2@testhost"
        v = 8
        self._seed_v(v)
        cfg = _cfg(executors=[OWNER, peer])
        peer_shards = hrw.shards_of(peer, cfg["executors"], DAY, v)
        self.assertTrue(peer_shards, "夹具前提：死主必须有分片，否则本用例什么都不测")
        phones = [_phone(1), _phone(2)]
        for i, phone in enumerate(phones):
            # 死主把分片内的行**全领成 `claimed`**（分片内没有 pending），随后崩溃：
            # 租约早已过回收宽限期，但 owner 仍是死主（回收前 `steal_shards` 偷不到）。
            self._add_task(phone, vshard=peer_shards[i % len(peer_shards)],
                           state="claimed", owner=peer, epoch=1,
                           lease_until=_ts(seconds=-180), run_at=_ts(seconds=-60))
        # 死主的心跳：有开始记录、无收尾且已过期（`worker_presence` 判 stale）
        state_io.mark_worker_started(
            executor_v3._worker_slot(peer),
            now=self.fc.now() - datetime.timedelta(seconds=5 * state_io.WORKER_HEARTBEAT_SEC))
        ran = []

        def _attempt(acc):
            ran.append(acc.phone)
            return (True, "ok", False, "success")

        # 不 patch `RECOVER_SEC`：本用例钉的是**短轮次**里起跑那次接管——真实间隔下补货
        # 循环的接管在本轮时长内不会触发，能通过只可能是判死即并入生效了。
        with mock.patch.object(executor_v3.attempts, "attempt_signin", _attempt):
            results = self._run_v3(self._accounts(*phones), cfg=cfg)
        self.assertEqual(sorted(ran), sorted(phones),
                         "死主分片里被回收的 claimed 行必须在本轮被领取并执行")
        for phone in phones:
            row = self._row(phone)
            self.assertEqual(row["state"], "done", "回收 + 并入后必须跑完，不能留 pending")
            self.assertEqual(row["owner"], OWNER, "回收后由本执行体持有")
            self.assertIn(phone, results)


class DeadPeerTakeoverSkipLiveTest(_Base):
    """起跑接管不得误伤：只有 `stale`（有开始、无收尾且心跳过期）才算死。

    活着的执行体（`running` / `finished`）与"今日还没跑"（`idle`）都不是死主，其分片
    集内的 `pending` 行**不得**改归本执行体——否则会把别人的在飞/待办抢过来。
    """

    def _run_with_peer_state(self, state, peer):
        """按给定四态准备 peer 心跳后跑一轮，返回 peer 的行与是否执行过。"""
        v = 8
        self._seed_v(v)
        cfg = _cfg(executors=[OWNER, peer])
        peer_shards = hrw.shards_of(peer, cfg["executors"], DAY, v)
        self.assertTrue(peer_shards, "夹具前提：peer 必须有分片，否则本用例什么都不测")
        phone = _phone(1)
        self._add_task(phone, vshard=peer_shards[0], state="pending", owner=peer,
                       epoch=1, run_at=_ts(seconds=-30))
        slot = executor_v3._worker_slot(peer)
        if state == state_io.WORKER_STATE_RUNNING:
            state_io.mark_worker_started(slot, now=self.fc.now())
        elif state == state_io.WORKER_STATE_FINISHED:
            state_io.mark_worker_started(slot, now=self.fc.now())
            state_io.mark_worker_finished(slot, now=self.fc.now())
        # idle：什么都不写（本业务日无该槽位记录）
        ran = []

        def _attempt(acc):
            ran.append(acc.phone)
            return (True, "ok", False, "success")

        with mock.patch.object(executor_v3.attempts, "attempt_signin", _attempt):
            self._run_v3(self._accounts(phone), cfg=cfg)
        return phone, ran

    def test_running_peer_is_not_taken_over(self):
        phone, ran = self._run_with_peer_state(state_io.WORKER_STATE_RUNNING,
                                               "worker-2@testhost")
        row = self._row(phone)
        self.assertEqual(ran, [], "活着的执行体的待办不得被领取")
        self.assertEqual(row["owner"], "worker-2@testhost", "不得改归本执行体")
        self.assertEqual(row["epoch"], 1, "不得自增 epoch")
        self.assertEqual(row["state"], "pending", "行保持原样")

    def test_finished_peer_is_not_taken_over(self):
        phone, ran = self._run_with_peer_state(state_io.WORKER_STATE_FINISHED,
                                               "worker-2@testhost")
        row = self._row(phone)
        self.assertEqual(ran, [], "正常跑完的执行体的待办不得被领取")
        self.assertEqual(row["owner"], "worker-2@testhost", "不得改归本执行体")
        self.assertEqual(row["state"], "pending", "行保持原样")

    def test_idle_peer_is_not_taken_over(self):
        phone, ran = self._run_with_peer_state(state_io.WORKER_STATE_IDLE,
                                               "worker-2@testhost")
        row = self._row(phone)
        self.assertEqual(ran, [], "今日无心跳记录不算死，不得被领取")
        self.assertEqual(row["owner"], "worker-2@testhost", "不得改归本执行体")
        self.assertEqual(row["state"], "pending", "行保持原样")


if __name__ == "__main__":
    unittest.main()
