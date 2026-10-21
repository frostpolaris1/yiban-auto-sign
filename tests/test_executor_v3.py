# -*- coding: utf-8 -*-
"""`yiban/engine/executor_v3.py`：v3 执行体核心（台账单池化后的唯一生产执行体）。

标签：B · 调度：领取/队列/执行体
覆盖：v3 执行体的通道数与容量口径、带 vshard 过滤的待办计数、`scheduler_v3_enabled`
   恒真与 runner 恒定转调、asyncio
   通道的并发与非阻塞到点等待、批量领取与收干判据、退避落点的有界抖动与窗口上界、终态映射与
   fencing 透传、与旧领取池
   对齐的放弃通知/日志、令牌桶三件接线、产品契约（每次尝试写
   sign-state、dry_run
   零写、计划不可用不抛）、崩溃恢复与死主分片接管的整条链路。
对应实现：yiban/engine/executor_v3.py（run_executor_v3、通道/补货/退避/收尾各路径）、yiban/engine/schedule.py（channel_count、capacity_accounts_v3）、yiban/adapters/persistence/queue_store.py（pending_count、claim_batch、reclaim_tasks、reap_abandoned）、yiban/app/runner.py
   的执行调用点、yiban/core/hrw.py 与 token_bucket.py。
关键断言：单池后 `runner` 恒定转调 `run_executor_v3`（`--only` 走显式路径：reclaim +
   claim_all + requeue_final），`round.run_queue_retry` 无生产调用点。写进
   sign_tasks.vshard 的 V 必须与执行体分片集同源且当日稳定：V
   落库后只读，行索引落在当日 v_for()
   之外也仍要被领取，否则当天计划与领取集错位就永久漏领。vshard=-1
   的历史行永不计入待办（算进去会让该日永远不了结）。崩溃回收必须排在领取循环里且带
   day。死主接管的判据是「stale」四态、只并入死主分片不改行归属（owner
   改写已裁，行由领取落到领取者名下），且不得误伤
   running/finished/idle
   的活执行体。收尾标记只在正常返回路径写，异常与中断都必须让心跳过期后判
   stale。
依赖：临时 sqlite（sign_tasks）+ 假时钟（_now/_sleep/_mono
   共用一份推进，用例不真实 sleep）+ 打桩 attempt_signin 与限速三件套；asyncio
   用例在进程内跑。不发网络请求。整文件在本机执行，无 skip。

"""
import asyncio
import contextlib
import datetime
import json
import logging
import os
import random
import shutil
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import db  # noqa: E402

from yiban import egress  # noqa: E402
from yiban.adapters.persistence import clock_meta, queue_store  # noqa: E402
from yiban.adapters.statefiles import state_io  # noqa: E402
from yiban.app import runner as runner_mod  # noqa: E402
from yiban.core import (  # noqa: E402
    clock,
    hrw,
    window,
)
from yiban.core import status as yiban_status  # noqa: E402
from yiban.engine import (  # noqa: E402
    executor_v3,
    planner,
    schedule,
    token_bucket,
)

TEST_KEY = "a" * 64
DAY = "2026-09-22"          # 周二，避开周末门
#: 固定起跑时刻：默认窗口 06:30~07:50（有效窗口 06:31~07:49），06:40 在窗口内
START = datetime.datetime(2026, 9, 22, 6, 40, 0)
OWNER = "single@testhost"
#: 队列**持有者**身份：稳定槽位名再拼本进程的进程号/代次（`egress.runtime_owner`）。
RUNTIME_OWNER = egress.runtime_owner(OWNER)
#: 出口桶键（工单 2cwd：键 = **出口标识**，不再是执行体身份）。基座未配出口 ⇒ 直连标识。
EGRESS_KEY = egress.DIRECT_EGRESS
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

    def persist(self, egress, stamp=None, now=None):
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


def _keeper_watch(box):
    """假协调循环：给定条目吐完、且没有通道在飞时置停止（**不推进假时钟**）。

    时序用例用它替代真协调循环的收干判定；领取由假 `_claim_one` 从 `box` 按序吐出。
    """
    async def _keep(ctx):
        while box or ctx.claiming:
            await asyncio.sleep(0)
        ctx.stop.set()
    return _keep


async def _never(_ctx):
    """永不返回的替身：桶状态落库循环的 10s 周期不该干扰时序用例的假时钟。"""
    await asyncio.Event().wait()


def _item(phone, run_at=None, attempts=0, epoch=1):
    """一条"被领到"的行——`claim_batch` 返回的行形状（claim-at-dispatch 下每次一行）。"""
    return {"phone": phone, "run_at": run_at or _ts(seconds=-1),
            "attempts": attempts, "epoch": epoch}


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
        # 出口配置键清干净：`_Ctx.egress` 按出口解析（工单 2cwd），继承来的 YIBAN_PROXY
        # 会让出口标识漂到别的值、把 `EGRESS_KEY` 的断言变成假红。
        for k in ("YIBAN_PROXY", "YIBAN_PROXY_LIST", "YIBAN_PROXY_FALLBACK",
                  "YIBAN_EXECUTORS", "YIBAN_FALLBACK_ENABLE"):
            os.environ.pop(k, None)
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
        """已由本执行体领取的行（等价于 `claim_batch` 刚返回它）。

        owner 用**运行时身份**：持有者一列落 PID/代次（同一稳定槽位名的两个进程因此
        可分辨），收尾/重排按它做作用域校验。
        """
        self._add_task(phone, vshard=vshard, state="claimed", owner=RUNTIME_OWNER,
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

    def _stale_heartbeat(self, index, role=egress.ROLE_WORKER):
        """写一个已过新鲜度门的心跳（`stale`：有开始、无收尾、心跳过期）。

        这是"崩溃执行体"的忠实表示：进程起跑时写过心跳、随后被杀，文件留下且时间戳变旧。
        心跳**只服务执行体页的存活展示**（工单 `l1rx`：判活与回收改走**进程句柄**，
        `queue_store.reap_abandoned` 不再读心跳时间戳）。
        """
        state_io.mark_worker_started(
            index, now=self.fc.t - datetime.timedelta(
                seconds=2 * state_io.WORKER_HEARTBEAT_SEC + 5), role=role)

    def _run_v3(self, accounts, items=None, *, cfg=None, rng=None, limiter=None,
                gate=None, delegated=None, cred_state=None, event_sink=None,
                dry_run=False, notify_url="", requeue_final=False, claim_all=False,
                reclaim=False, requeue_during_run=False, round_no=None,
                shares_seen=None):
        """跑一轮 v3。

        `items` 给了就用"一次性投递 + 哨兵"的假补货（时序完全可控，行需已领取）；
        不给则用真实补货（走 `claim_batch`，行由它置 owner/epoch）。
        `shares_seen` 给了就收下 `_make_limiter` 收到的 `shares`（出口预算均分的接线断言）。
        """
        cfg = cfg or _cfg()
        limiter = limiter if limiter is not None else _PermissiveLimiter()
        gate = gate if gate is not None else _PermissiveGate()
        patches = [mock.patch.object(executor_v3, "_persist_loop", _never)]
        if items is not None:
            # 夹具把行预置成"本进程已领取"（owner＝本进程运行时身份）——那是**起跑之后**
            # 才可能出现的状态；而执行体起跑第一件事就是按本稳定名收自己的尸（工单 `l1rx`
            # 必修 R1），会把夹具行翻回 pending。故 items 模式把起跑收尸替身为 no-op；
            # **R1 本身**由 `tests/test_claim_dispatch_elbow_e2e.py` 的三条路径用例正面钉住。
            patches.append(mock.patch.object(queue_store, "reap_abandoned",
                                             lambda *a, **k: 0))
            box = list(items)

            def _claim(ctx, _box=box):
                # 吐完即置停止（免得空轮里各通道白睡几拍推着假时钟往前走——那会让
                # 依赖窗口余量的用例随并行度抖动），但**要等没有通道在飞**才停。
                if _box:
                    return [_box.pop(0)]
                if ctx.claiming == 0:
                    ctx.stop.set()
                return []

            patches.append(mock.patch.object(executor_v3, "_claim_one", _claim))
            patches.append(
                mock.patch.object(executor_v3, "_dispatch_keeper", _keeper_watch(box)))

        def _mk(channels, shares=1):
            if shares_seen is not None:
                shares_seen.append(shares)
            return limiter

        patches += [
            mock.patch.object(executor_v3, "_make_limiter", _mk),
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
                dry_run=dry_run, notify_url=notify_url, requeue_final=requeue_final,
                claim_all=claim_all, reclaim=reclaim,
                requeue_during_run=requeue_during_run, round_no=round_no)
        finally:
            for p in reversed(patches):
                p.stop()


# ---------------------------------------------------------------------------
# 执行体启用判定（单池后恒真）
# ---------------------------------------------------------------------------
class SchedulerV3EnabledTest(unittest.TestCase):
    """台账单池化后 `scheduler_v3_enabled` 恒真；双轨开关的键与缺省常量已删除。"""




class ManualReclaimTest(_Base):
    """手动 `--only` 的 `reclaim`：已了结（done）行被翻回 pending 并再次真实执行。"""

    def _attempts(self, calls):
        return mock.patch.object(
            executor_v3.attempts, "attempt_signin",
            lambda acc: (calls.append(acc.phone), (True, "ok", False, "success"))[1])

    def test_done_row_is_reclaimed_and_reexecuted(self):
        self._add_task(_phone(1), state="done", owner="w:1:1", attempts=1)
        calls = []
        with self._attempts(calls):
            self._run_v3(self._accounts(_phone(1)), reclaim=True)
        self.assertEqual(calls, [_phone(1)], "reclaim 必须让已了结账号再执行一次")
        self.assertEqual(self._row(_phone(1))["state"], "done", "执行后被收尾成 done")

    def test_without_reclaim_done_row_stays_done(self):
        self._add_task(_phone(1), state="done", owner="w:1:1", attempts=1)
        calls = []
        with self._attempts(calls):
            self._run_v3(self._accounts(_phone(1)))
        self.assertEqual(calls, [], "普通轮不得复活已了结账号")
        self.assertEqual(self._row(_phone(1))["state"], "done")


class ManualReclaimIsolationTest(_Base):
    """手动 `--only`（reclaim）**只碰本轮账号**：库里别人的行与 sign-state 一字不变。

    回归点（越界收尾 ⇒ 静默漏签）：`reclaim` 曾配通配宽分片（`claim_all`），于是轮首
    `requeue_failed(include_final=True)` 把当日**所有**账号的 `failed` 行翻回 `pending`、
    `claim_batch` 再把当日**所有** `pending` 领走；凡不在 `ctx.accounts` 的行在 `_attempt`
    走 `acc is None` 分支被 `_finish(done/user_cancelled)`——不登录却判成了结，补签轮随后
    据 `pending_count=0` 判"已了结"。修法：领取/回炉/待办计数收窄到本轮账号的虚分片 +
    账号允许集，且 `phone ∉ accounts` 的行绝不了结。
    """

    V = 64

    def _other_vshard_for(self, phone):
        """别人的行放进"与本轮账号同分片"——不带允许集就会被 greedy 领走（判别力所在）。"""
        return hrw.vshard_of(phone, DAY, self.V)

    def test_other_accounts_rows_and_state_untouched(self):
        a, b, c = _phone(1), _phone(2), _phone(3)
        self._seed_v(self.V)
        sh = self._other_vshard_for(a)
        # B：同分片、pending、已到期（修法前会被领走并误判 done）
        self._add_task(b, vshard=sh, state="pending", run_at=_ts(seconds=-5))
        # C：同分片、retry: 档 failed（修法前会被 require_final 一并回炉后领走）
        self._add_task(c, vshard=sh, state="failed", result="retry:skipped_window",
                       run_at=_ts(seconds=-5))
        # A：本轮账号，已了结（reclaim 应把它翻回并重签）
        self._add_task(a, vshard=sh, state="done", owner="w:1:1", attempts=1)
        state_io._write_sign_state(b, "pending", "计划 06:31")
        before_b, before_c = self._row(b), self._row(c)
        before_state_b = self._read_state().get(b)
        calls = []
        with mock.patch.object(executor_v3.attempts, "attempt_signin",
                               lambda acc: (calls.append(acc.phone),
                                            (True, "ok", False, "success"))[1]):
            self._run_v3(self._accounts(a), reclaim=True, requeue_final=True)
        self.assertEqual(calls, [a], "只该执行本轮账号")
        self.assertEqual(self._row(b), before_b, "别人的 pending 行不得被领走/改动")
        self.assertEqual(self._row(c), before_c, "别人的 retry: 档 failed 行不得被回炉")
        self.assertEqual(self._read_state().get(b), before_state_b,
                         "别人的 sign-state 不得被改写成 user_cancelled")
        self.assertEqual(self._row(a)["state"], "done", "本轮账号被重签并收尾")

    def test_manual_reclaim_recovers_stale_claimed_rows(self):
        """手工 `--only` 的**逃生口**（工单 `l1rx` 必修 R2）：卡死的 `claimed` 行必须能被
        "立即签到"救回来。

        任务级租约删除后，"持有者已死"不再由时间戳判定。手工路径若也不翻 `claimed`，
        用户点"立即签到"对卡死行就**永久无效**（旧逃生口＝手工跑一轮也一并失效）。
        本用例钉两点：① 本轮账号自己的陈旧 `claimed` 被翻回并重新执行；② 别人的
        `claimed` 行一字不动（不得跨账号写）。`epoch` 换代使原持有者的迟到写仍被 fence。
        """
        a, b = _phone(1), _phone(2)
        self._seed_v(self.V)
        sh = self._other_vshard_for(a)
        self._add_task(b, vshard=sh, state="claimed", owner="other:1:1",
                       lease_until=_ts(seconds=-600), epoch=5)
        self._add_task(a, vshard=sh, state="claimed", owner="me:1:1",
                       lease_until=_ts(seconds=-600), epoch=3)
        before_b = self._row(b)
        calls = []
        with mock.patch.object(executor_v3.attempts, "attempt_signin",
                               lambda acc: (calls.append(acc.phone),
                                            (True, "ok", False, "success"))[1]):
            self._run_v3(self._accounts(a), reclaim=True, requeue_final=True)
        self.assertEqual(calls, [a], "手工路径必须把卡死的 claimed 行救回来并执行")
        self.assertEqual(self._row(a)["state"], "done", "救回来的行照常收尾")
        self.assertEqual(self._row(b), before_b, "别人的 claimed 行一字不变")


class ChannelCountTest(unittest.TestCase):
    def test_channel_count_exact_values(self):
        self.assertEqual(schedule.channel_count(1.0, 3), 6)
        self.assertEqual(schedule.channel_count(4.0, 3), 16)
        self.assertEqual(schedule.channel_count(0.2, 3), 2)
        self.assertEqual(schedule.channel_count(0.0, 3), 6, "非法速率回退出厂速率 1.0")

    def test_channel_count_never_below_one(self):
        for rate in (0.01, 0.05):
            self.assertGreaterEqual(schedule.channel_count(rate, 1), 1)



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
        self.assertEqual(v, 64, "建计划落库的 V 应是定档值 64（= hrw.v_for(n)）")
        self.assertEqual(v, hrw.v_for(len(accounts)))
        rows = [r["vshard"] for r in db.get_conn().execute(
            "SELECT vshard FROM sign_tasks WHERE day=?", (DAY,)).fetchall()]
        self.assertEqual(len(rows), 3)
        for vshard in rows:
            self.assertLess(vshard, v, "计划行的索引必须落在扫描范围内")
        covered = set()
        for ex in (OWNER, other):
            covered |= set(hrw.shards_of(ex, cfg["executors"], DAY, v))
        self.assertEqual(covered, set(range(v)), "每个分片都要有执行体认领，否则该行无人领")

    def test_plan_covers_chunks_large_account_set(self):
        """1000 账号（> `SQL_VAR_CHUNK`）仍能正确判覆盖：按块查询并汇总 DISTINCT 计数。

        整段拼 `IN (...)` 在 1~2 千账号（项目目标规模）下会越过 SQLite 老版本的 999 个
        绑定变量上限，异常被吞成"未覆盖"⇒ 每轮重写计划并刷 warning，覆盖面判据在大站
        永久失明（工单 ba-p03-10）。
        """
        phones = [_phone(i) for i in range(1000)]
        accounts = [SimpleNamespace(phone=p, user_paused=False, owner="u@" + p)
                    for p in phones]
        self.assertGreater(len(phones), queue_store.SQL_VAR_CHUNK)
        conn = db.get_conn()
        conn.executemany(
            "INSERT INTO sign_tasks (phone, day, vshard, owner, run_at, priority, "
            "state, attempts, lease_until, result, epoch, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [(p, DAY, 0, "", _ts(seconds=-1), 5, "pending", 0, "", "", 0,
              _ts(seconds=-60)) for p in phones])
        conn.commit()
        self.assertTrue(executor_v3._plan_covers(DAY, accounts))
        conn.execute("DELETE FROM sign_tasks WHERE phone=? AND day=?", (phones[-1], DAY))
        conn.commit()
        self.assertFalse(executor_v3._plan_covers(DAY, accounts),
                         "少一个账号就必须判未覆盖（否则其余账号当天没有计划行）")

    def test_stored_v_wins_over_recomputed_v(self):
        """V 落库后只读：行索引在 `v_for(当日账号数)` 之外也必须仍被领取（反例）。

        V 定档 64 后"当日账号数"已不参与选档，但本用例的命题不变：只要重算/替换 V，
        范围外的行就永远领不到。
        """
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

                async def fake_lane(ctx, lane_id, _started=started):
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


# ---------------------------------------------------------------------------
# claim-at-dispatch：领取即执行、每次一行、没活时按拍轮询、收干由协调循环裁决
# ---------------------------------------------------------------------------
class DispatchTest(_Base):
    def test_claim_is_one_row_at_a_time(self):
        """**不许批量预取**（工单 `l1rx`）：执行体每次领取都只领一行。"""
        phones = [_phone(i) for i in range(5)]
        for p in phones:
            self._add_task(p)
        self._seed_v(8)
        seen = []
        real = queue_store.claim_batch

        def spy(owner, day, shards, now=None, limit=queue_store.CLAIM_LIMIT,
                elbow_sec=0.0, phones=None):
            rows = real(owner, day, shards, now=now, limit=limit,
                        elbow_sec=elbow_sec, phones=phones)
            seen.append({"limit": limit, "elbow_sec": elbow_sec, "n": len(rows or [])})
            return rows

        with mock.patch.object(executor_v3.attempts, "attempt_signin",
                               lambda acc: (True, "ok", False, "success")),              mock.patch.object(queue_store, "claim_batch", spy):
            self._run_v3(self._accounts(*phones))
        self.assertTrue(seen, "执行体必须走 queue_store.claim_batch 领取")
        self.assertLessEqual(max(s["n"] for s in seen), 1, "每次领取不得超过一行（无批量预取）")
        for s in seen:
            self.assertEqual(s["limit"], 1, "领取行数缺省为 1")
        self.assertEqual({s["elbow_sec"] for s in seen}, {0.0},
                         "定时执行体的肘界为 0（到期即可领）")
        for p in phones:
            self.assertEqual(self._row(p)["state"], "done")

    def test_row_not_yet_due_is_eventually_claimed(self):
        """未到点的行不得被领取；到点后必须被领到、不许空转到窗口关闭。"""
        phone = _phone(1)
        self._add_task(phone, run_at=_ts(seconds=5))
        self._seed_v(8)
        with mock.patch.object(executor_v3.attempts, "attempt_signin",
                               lambda acc: (True, "ok", False, "success")):
            self._run_v3(self._accounts(phone))
        self.assertEqual(self._row(phone)["state"], "done")
        self.assertGreater(self.fc.t, START, "没活时按拍轮询会推进假时钟")

    def test_keeper_drains_when_only_historical_rows_remain(self):
        """M7/U7：只有 `vshard=-1` 的历史行时判「已收干」，不空转到窗口关闭。"""
        self._add_task(_phone(1), vshard=-1, state="failed")
        self._add_task(_phone(2), vshard=-1, state="pending")
        self._seed_v(8)
        ctx = SimpleNamespace(cfg=_cfg(), day=DAY, executor_id=OWNER,
                              runtime_id=RUNTIME_OWNER, m=2, inflight=0, claiming=0,
                              claim_unreadable=False, slot=0, v=8,
                              shards=tuple(range(8)), reclaim=False, allowed_phones=None,
                              requeue_during_run=False, queue_unreadable=False,
                              log_tag="[single]", stop=asyncio.Event())
        asyncio.run(executor_v3._dispatch_keeper(ctx))
        self.assertEqual(self.fc.sleeps, [], "首轮即应判收干，不进入轮询等待")
        self.assertTrue(ctx.stop.is_set(), "收干必须置停止信号")


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
            # 窗口外：还该再试 → 未了结的 failed（默认档）
            ("skipped_window", True, "签到时段已结束", 0, "failed"),
            # 用户取消：管理侧当日结论 → 终态 skipped（与 JSON 终态映射同口径）
            ("user_cancelled", True, "用户已取消签到", 0, "skipped"),
            # 无点位：预算 1 次，首试即终态
            ("no_position", False, "未找到签到位置数据", 0, "failed"),
            # 其它失败：预算 3 次，已试 3 次 → 终态
            ("failed", False, "网络抖动", 2, "failed"),
        )
        for status, skip, message, attempts, expected in cases:
            with self.subTest(status=status):
                phone, results, settled = self._run_one(
                    status, skip=skip, message=message, attempts=attempts)
                self.assertEqual(self._row(phone)["state"], expected)
                self.assertEqual(settled[0][1], expected)
                self.assertEqual(results[phone][2], skip, "skip 原样回传，不重排")

    def test_account_missing_from_round_settles_skipped_without_request(self):
        phone = _phone(9)
        self._add_claimed(phone)
        self._seed_v(8)
        calls = []
        with mock.patch.object(executor_v3.attempts, "attempt_signin",
                               lambda acc: calls.append(acc.phone) or (True, "ok", False,
                                                                       "success")):
            results = self._run_v3(self._accounts(_phone(1)), [_item(phone)])
        self.assertEqual(calls, [], "账号已不在本轮配置时不得发起请求")
        self.assertEqual(self._row(phone)["state"], "skipped",
                         "该行必须被了结（否则永远 pending），终态与 user_cancelled 的映射同口径")
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
        # 账密暂停是管理侧当日结论 → 终态 skipped（工单 48b9/vqmb）：落 failed 会让
        # 补签闸门的队列腿判"未了结"，补签轮再真实登录一次。
        self.assertEqual(self._row(phone)["state"], "skipped")

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

    def test_user_paused_already_recorded_still_skipped_in_one_shot_round(self):
        """一次性轮（`round_no` 为 `None`）即便当日已有该结论，仍须把暂停账号登记进 `results`。

        `_prescan` 的当日幂等有两个分支：常驻多轮（`round_no` 有值）在已登记时**不进**
        `results`（兜底紧循环据此判"本轮无活"，回到配置的扫描间隔）；一次性轮（定时全量、
        手动 `--only`）**必须进**——`runner` 的汇总只认 `results`，缺席的账号会落到
        `unreached` 的 `UNCLAIMED` 档 ⇒ `stay_n + 1` 且 `has_real_failure=True` ⇒
        退出码 1 与一封失败邮件。独立审查实测：反转那个 `round_no` 守卫时，全测试面
        **只有常驻轮那条变红**，本路径原先零覆盖——正是这条守卫存在的理由。
        """
        paused = SimpleNamespace(phone=_phone(1), user_paused=True)
        self._seed_v(8)
        # 入口条件＝当日状态文件里**已有**该结论（上一次全量轮或别的执行体写的）：
        # 幂等分支只在"已有"时生效，这正是本路径与常驻轮的差别所在。
        state_io._write_sign_state(_phone(1), "user_cancelled", "用户已取消签到")
        events = []
        results = self._run_v3([paused], [], event_sink=events.append)
        self.assertEqual(results[_phone(1)],
                         (False, "用户已取消签到", True, "user_cancelled"),
                         "一次性轮缺席 results ⇒ 汇总按「未执行」计失败（退出码 1）")
        self.assertEqual(events, [],
                         f"当日已有结论不得重复落事件（一次性轮同样只登记一次）：{events}")

    def test_user_paused_concurrent_first_sight_writes_and_emits_once(self):
        """两个执行体同时首见同一暂停号（都读到"无结论"）时，只落一次状态与一条事件。

        宽分片（`claim_all`）下分片集判据挡不住兜底与归属执行体同时登记，故"当日只
        登记一次"必须靠 `_write_sign_state(only_if_absent=True)` 的写侧 CAS（工单
        ba-p03-05）。本用例把两次预扫的读侧快照都钉成空，模拟"两边都还没看到结论"。
        """
        paused = SimpleNamespace(phone=_phone(1), user_paused=True)
        self._seed_v(8)
        events = []
        with mock.patch.object(state_io, "_daily_statuses", return_value={}):
            self._run_v3([paused], [], event_sink=events.append)
            second = self._run_v3([paused], [], event_sink=events.append)
        self.assertEqual([e["phone"] for e in events], [_phone(1)],
                         "CAS 输的一侧不得再发 user_cancelled 事件")
        self.assertEqual(second[_phone(1)],
                         (False, "用户已取消签到", True, "user_cancelled"),
                         "一次性轮仍必须登记结果（汇总口径不变）")

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

    def test_user_paused_owned_by_peer_is_delegated_not_registered(self):
        """暂停号只由**归属执行体**登记一次（_prescan 归属判定先于暂停登记）。

        多执行体下若每个执行体都登记自暂停号：结果/状态/事件 ×N、每份汇总各计一遍
        "跳过"——生产事件表 2026-10-01/02 实测每个暂停号每天 2 行 user_cancelled、
        两份汇总各报 "➖ 3 跳过"（实际 3 个暂停号）。
        """
        other = "worker-1@testhost"
        cfg = _cfg(executors=[OWNER, other])
        self._seed_v(64)
        mine = set(hrw.shards_of(OWNER, cfg["executors"], DAY, 64))
        peer = set(hrw.shards_of(other, cfg["executors"], DAY, 64))
        own_phone = peer_phone = None
        for i in range(400):
            p = _phone(i)
            shard = hrw.vshard_of(p, DAY, 64)
            if own_phone is None and shard in mine:
                own_phone = p
            elif peer_phone is None and shard in peer and shard not in mine:
                peer_phone = p
            if own_phone is not None and peer_phone is not None:
                break
        self.assertTrue(own_phone and peer_phone, "夹具需要两个分片各落一个号")
        accounts = [SimpleNamespace(phone=own_phone, user_paused=True),
                    SimpleNamespace(phone=peer_phone, user_paused=True)]
        events = []
        delegated = set()
        results = self._run_v3(accounts, [], cfg=cfg, delegated=delegated,
                               event_sink=events.append)
        self.assertEqual(
            results,
            {own_phone: (False, "用户已取消签到", True, "user_cancelled")},
            "只登记归属自己的暂停号，别人的归 delegated 不进 results")
        self.assertEqual(delegated, {peer_phone},
                         "别人的暂停号必须归 delegated（汇总计为'由其他执行体负责'）")
        self.assertEqual([e["phone"] for e in events], [own_phone],
                         "user_cancelled 事件只由归属执行体发一次")
        self.assertNotIn(peer_phone, self._read_state(),
                         "归属执行体之外不得写对方账号的签到状态")


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

    def test_no_room_makes_a_retryable_status_final(self):
        """窗口容不下最小间隔时，默认可回炉的状态也必须用保守档收尾。

        否则默认回炉口会把 `retry:` 行立刻翻回 pending，下一拍重新领取——真实登录
        间隔只剩 gap 门（默认 10s），正是 `YIBAN_RETRY_MIN_INTERVAL` 要防的连击
        （工单 ba-p03-04）。`skipped_norange` 在档位词表里默认可回炉（retry:）。
        """
        phone = _phone(1)
        self._add_claimed(phone, attempts=0, epoch=1)
        self._seed_v(8)
        cfg = _cfg(sign_start=(6, 30), sign_end=(6, 42), edge_back_sec=110,
                   edge_front_sec=0)  # 有效窗口至 06:40:10，剩余 10s
        with mock.patch.object(executor_v3.attempts, "attempt_signin",
                               lambda acc: (False, "窗口缺失", False,
                                            yiban_status.STATUS_SKIPPED_NORANGE)):
            self._run_v3(self._accounts(phone), [_item(phone)], cfg=cfg)
        row = self._row(phone)
        self.assertEqual(row["state"], "failed")
        self.assertTrue(row["result"].startswith(yiban_status.RESULT_FINAL_PREFIX),
                        "当日无落点 ⇒ 保守档；retry: 会立刻被回炉口复活")
        self.assertEqual(queue_store.requeue_failed(DAY, (0,), include_final=False), 0,
                         "默认回炉口不得复活它（复活即绕开最小间隔）")

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
        # 有效窗口至 06:41:40（剩余 100s）→ cap = 30s < base 60s ⇒ 放弃重试（None）：
        # 窗口尾段放不下一个合法的最小间隔，宁可当日放弃，也不把间隔压到配置值之下
        # （工单 ba-p03-04）。
        cfg = _cfg(sign_start=(6, 30), sign_end=(6, 42), edge_back_sec=20,
                   edge_front_sec=0)
        self.assertAlmostEqual(_remaining(cfg, START), 100, delta=1)
        self.assertIsNone(executor_v3.next_retry_at_v3(START, cfg, 60, random.Random(5)))
        # 把最小间隔降到 20s 后，落点仍被 cap（30s）约束且 >= base。
        cfg_low = dict(cfg, retry_min_interval=20)
        delay = (executor_v3.next_retry_at_v3(START, cfg_low, 60, random.Random(5))
                 - START).total_seconds()
        self.assertGreaterEqual(delay, 20)
        self.assertLessEqual(delay, 30)


    def test_next_retry_at_never_passes_window_end(self):
        """落点恒在有效窗口内（`cap = 剩余 × 0.3` 给出结构性保证，再夹一次窗口结束）。

        窗口只剩 10s ⇒ cap=3s 小于最小间隔 60s，函数返回 None（放弃重试），绝不出窗；
        把最小间隔降到 1s 后才能给出落点，且落点仍必须落在窗内。
        """
        cfg = _cfg(sign_start=(6, 30), sign_end=(6, 42), edge_back_sec=110,
                   edge_front_sec=0)
        self.assertAlmostEqual(_remaining(cfg, START), 10, delta=1)
        self.assertIsNone(executor_v3.next_retry_at_v3(START, cfg, 60, random.Random(5)))
        cfg_low = dict(cfg, retry_min_interval=1)
        window_end = window.to_dt(START.replace(hour=0, minute=0, second=0),
                                  window.bounds(cfg).hi_min)
        for last_delay in (60, 600, 10 ** 6):
            with self.subTest(last_delay=last_delay):
                target = executor_v3.next_retry_at_v3(
                    START, cfg_low, last_delay, random.Random(5))
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

    def test_claim_exception_is_contained(self):
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
        self.assertIn(("restore", EGRESS_KEY), trace)
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
        self.assertIn(("on_risk_signal", EGRESS_KEY), trace)
        self.assertNotIn("on_success", [k for k, _ in trace])

    def test_credential_failure_leaves_the_real_bucket_untouched(self):
        """口令错不得让整条出口降速：真实限速器下桶值与半开都不动（工单 zggs 的 e2e 钉）。

        `on_risk_signal` 会 `rate ÷= 2` 并进半开，且经 10s 落库持久化、重启装回。
        生产实证：一个口令错的账号砍掉整条出口一半速率，两天三次后 fallback 只剩 1/4。
        """
        phone = _phone(1)
        self._add_claimed(phone, attempts=1)
        self._seed_v(8)
        limiter = token_bucket.EgressLimiter(rate=1.0)
        with mock.patch.object(executor_v3.attempts, "attempt_signin",
                               lambda acc: (False, "登录失败: 账号或密码错误",
                                            False, "failed")):
            self._run_v3(self._accounts(phone), [_item(phone, attempts=1)],
                         limiter=limiter)
        self.assertAlmostEqual(limiter.bucket(EGRESS_KEY).rate, 1.0, places=9,
                               msg="口令错是账号自己的问题，不是平台限速：出口速率不得变")
        self.assertFalse(limiter.is_half_open(EGRESS_KEY, self.fc.mono),
                         "不得进半开（半开期只放单通道）")

    def test_waf_message_backs_off_the_real_bucket(self):
        """上一条的非空对照：真 WAF 文案必须让真实限速器降速，否则那条断言是空转。"""
        phone = _phone(1)
        self._add_claimed(phone, attempts=1)
        self._seed_v(8)
        limiter = token_bucket.EgressLimiter(rate=1.0)
        with mock.patch.object(executor_v3.attempts, "attempt_signin",
                               lambda acc: (False, "风险访问 拦截", False, "failed")):
            self._run_v3(self._accounts(phone), [_item(phone, attempts=1)],
                         limiter=limiter)
        self.assertAlmostEqual(limiter.bucket(EGRESS_KEY).rate, 0.5, places=9,
                               msg="WAF 文案必须触发乘性回退（这一路必须活着）")
        self.assertTrue(limiter.is_half_open(EGRESS_KEY, self.fc.mono))

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

        # 重试落点**钉死**（不依赖假时钟推进多少）：本用例的题目是"每次尝试都物化状态"，
        # 不是"退避落点算得对"（后者由 test_retry_window_day_boundary 覆盖）。不钉死会让
        # 本用例随并行度抖动——通道空轮各睡一拍会把假时钟推远，窗口余量不足时退避返回
        # `None`，状态就落成 failed 而不是 retrying。
        with mock.patch.object(executor_v3.attempts, "attempt_signin", fake_attempt),                 mock.patch.object(executor_v3, "next_retry_at_v3",
                                  lambda *a, **k: START + datetime.timedelta(seconds=30)):
            self._run_v3(self._accounts(*phones), [_item(p) for p in phones],
                         event_sink=events.append)
        state = self._read_state()
        self.assertEqual(state[_phone(1)]["status"], "success")
        self.assertEqual(state[_phone(2)]["status"], "retrying",
                         "重试入队也要物化状态（网页日历不能空窗）")
        self.assertEqual({e["phone"] for e in events}, set(phones))
        self.assertEqual({e["stage"] for e in events}, {"sign"})
        self.assertTrue(all(e["attempt"] for e in events))


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
        # 触网点必须显式替身（工单 `l1rx` 必修 R7）：不许靠"假账号没有 password 属性"
        # 与生产窗口门挡住请求——那正是 48 次请求漏站的同族形状（护栏不是判据）。
        calls = []
        with mock.patch.object(executor_v3.attempts, "attempt_signin",
                               lambda acc: calls.append(acc.phone) or (True, "ok",
                                                                       False, "success")):
            results = self._run_v3(accounts)
        self.assertEqual(calls, [], "窗口已关：一个真实请求都不许发")
        self.assertEqual(results[_phone(1)][3], "failed", "已有结论按原结论透传")
        self.assertEqual(self._read_state()[_phone(1)]["message"], "真实失败")

    def test_window_closed_preserves_unknown_status(self):
        """看不懂的状态串也是「已有结论」，窗口外跳过不得覆盖它（工单 `77s2` 迁移）。

        判据来自 `is_concluded_status` 的排除法口径：读侧可能遇到本进程不认识的
        状态串（新增状态码而读侧未同步、外部工具手写）。把它当"无记录"覆盖成
        `skipped_window`，真实结论会从告警/日历里消失。原用例住在冻结载体
        `tests/test_status_sets.py::WindowSkipPrefilterAgreementTest`。

        反向控制：条目**缺 `status` 键**（半截条目）＝无记录，窗口外跳过必须写得进去
        （否则漏记一轮）。
        """
        for recorded, want, why in (
            ({"status": "GLOBAL_PAUSED", "message": "旧版残留"}, "GLOBAL_PAUSED",
             "看不懂的状态串也是结论"),
            ({"message": "半截条目"}, yiban_status.STATUS_SKIPPED_WINDOW,
             "缺 status 的条目与 `is_concluded_status` 同口径＝无记录"),
        ):
            with self.subTest(recorded=recorded):
                self._clear_tasks()
                self._seed_v(8)
                self.fc.t = START.replace(hour=8, minute=0)
                with open(self._state_path(), "w", encoding="utf-8") as f:
                    json.dump({_phone(1): recorded}, f)
                results = self._run_v3(self._accounts(_phone(1)))
                self.assertEqual(results[_phone(1)][3], want,
                                 "预筛与落盘必须给同一判定：" + why)
                self.assertEqual(self._read_state()[_phone(1)]["status"], want,
                                 "落盘状态必须是判定结果：" + why)

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
# runner 分流：单池后恒定走 v3 执行体
# ---------------------------------------------------------------------------
class RunnerSplitTest(unittest.TestCase):
    """`runner.main`：台账单池化后生产执行体恒定是 `run_executor_v3`。"""

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

    def _run(self, argv=None, outcome=None, second_run=False):
        accounts = [SimpleNamespace(phone=_phone(0), user_paused=False, owner="u@1")]
        sched = {_phone(0): START}
        cred = {"seed": 1}
        outcome = dict(outcome) if outcome is not None else {
            _phone(0): (True, "签到成功", False, "success")}
        v3_calls = []
        with mock.patch.object(runner_mod.accounts_mod, "load_accounts",
                               return_value=accounts), \
             mock.patch.object(runner_mod.schedule_mod, "build_schedule",
                               return_value=sched), \
             mock.patch.object(runner_mod.executor_v3, "run_executor_v3",
                               side_effect=lambda *a, **kw: (v3_calls.append((a, kw)),
                                                             dict(outcome))[1]), \
             mock.patch.object(runner_mod.state_io, "_load_cred_state",
                               return_value=cred), \
             mock.patch.object(runner_mod.state_io, "_save_cred_state"), \
             mock.patch.object(runner_mod.state_io, "_is_second_run",
                               return_value=second_run), \
             mock.patch.object(runner_mod.state_io, "_second_run_drop_done",
                               return_value=accounts), \
             mock.patch.object(runner_mod.state_io, "_write_sched_done"), \
             mock.patch.object(runner_mod.db, "add_sign_events_batch"), \
             mock.patch.object(runner_mod.db, "purge_expired_deleted_accounts"), \
             mock.patch.object(runner_mod.alerts, "_maybe_alert_zero_success"), \
             mock.patch.object(runner_mod.alerts, "_flush_admin_mail_summary"):
            code = runner_mod.main(argv or [])
        return code, accounts, sched, cred, v3_calls

    def test_full_round_routes_to_v3(self):
        """单池后普通全量轮恒定走 v3 执行体（不再有旧领取池分支）。"""
        code, accounts, _sched, cred, v3_calls = self._run()
        self.assertEqual(code, 0, "退出码由 runner 按 results 汇总（成功 → 0）")
        self.assertEqual(len(v3_calls), 1)
        args, kw = v3_calls[0]
        self.assertEqual(args[0], accounts)
        self.assertEqual(kw["notify_url"], "")
        self.assertIs(kw["cred_state"], cred, "cred_state 按引用透传")
        self.assertIsInstance(kw["delegated"], set)
        self.assertTrue(callable(kw["event_sink"]))
        self.assertFalse(kw.get("reclaim"))
        self.assertFalse(kw.get("claim_all"))
        self.assertFalse(kw["requeue_final"], "普通轮不得自动复活 final: 档")

    def test_second_run_passes_requeue_final(self):
        _code, _accounts, _sched, _cred, v3_calls = self._run(second_run=True)
        self.assertEqual(len(v3_calls), 1)
        self.assertTrue(v3_calls[0][1]["requeue_final"],
                        "补签轮是显式路径：透传 requeue_final=True")

    def test_only_routes_to_v3_explicit_path(self):
        """手动 `--only` 也走 v3，但带显式路径参数（reclaim + requeue_final，**不用** claim_all）。"""
        code, _accounts, _sched, _cred, v3_calls = self._run(["--only", _phone(0)])
        self.assertEqual(len(v3_calls), 1, "--only 走 v3（单池后无第二轨）")
        kw = v3_calls[0][1]
        self.assertTrue(kw["reclaim"], "手动签到允许重签当日已了结账号")
        self.assertFalse(kw.get("claim_all"),
                         "手动轮不得用通配宽分片：会把当日别人的行一并领走/回炉")
        self.assertTrue(kw["requeue_final"], "手动是显式路径：允许重领预算耗尽档")
        self.assertEqual(code, 0)

    def test_empty_results_from_executor_yield_contract_exit_code(self):
        """执行体返回空结果（**丢结果**那一档兜底的输出）时，runner 仍给出契约内退出码。"""
        code, *_ = self._run(outcome={})
        self.assertIn(code, (0, 1, 2, 3, 10))
        self.assertEqual(code, 1, "无结果按失败汇总，不得落到契约之外")

    def test_retained_results_from_executor_yield_contract_exit_code(self):
        """执行体保留已完成结果（**只丢收尾**那一档的输出）时，退出码同样在契约内。

        窗口收尾失败返回的是"已完成账号的结果集"，不是空结果——runner 按它汇总出
        成功（0），而不是把一轮基本成功的活报成"全部未执行"（1 + 失败邮件）。
        """
        code, *_ = self._run(outcome={_phone(0): (True, "签到成功", False, "success")})
        self.assertIn(code, (0, 1, 2, 3, 10))
        self.assertEqual(code, 0, "保留的结果集按真实结论汇总，不得落到契约之外")


# ---------------------------------------------------------------------------
# 收尾标记与心跳四态（UI 面）
# ---------------------------------------------------------------------------
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
        self.assertEqual(state_io.worker_presence(0, role=egress.ROLE_SINGLE)[0], state_io.WORKER_STATE_FINISHED,
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
        self.assertEqual(state_io.worker_presence(0, now=later, role=egress.ROLE_SINGLE)[0],
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
        self.assertEqual(state_io.worker_presence(0, now=later, role=egress.ROLE_SINGLE)[0],
                         state_io.WORKER_STATE_STALE,
                         "未预期异常不得写收尾：心跳过期后判 stale，而不是 finished")


class HolderIdentitySplitTest(_Base):
    """v3 的三个身份必须显式分开：持有者落 PID/代次，稳定名留给分片，出口桶键 = 出口标识。

    `sign_tasks.owner` 一列同时是"计划 owner"与"当前持有者"。HRW 分片成员判据
    （`hrw.shards_of` 要求身份串是 `cfg["executors"]` 的成员）要求**稳定槽位名**；而"谁在
    持有"必须含进程号与代次，否则同机两个进程拿同一个名字，接管/收尾的作用域校验就分不出人。
    限速桶键另走一路（工单 2cwd）：`_Ctx.egress = egress.egress_identity(该出口)`——按**出口**
    计数，同一出口的多个执行体共享一桶（否则该出口速率 = K×λ）。
    """

    def test_stable_name_is_the_shard_member_and_runtime_is_not(self):
        cfg = _cfg(executors=[OWNER])
        self.assertTrue(hrw.shards_of(OWNER, cfg["executors"], DAY, 8),
                        "稳定槽位名必须仍是 HRW 分片成员（否则一件活都领不到）")
        self.assertEqual(hrw.shards_of(RUNTIME_OWNER, cfg["executors"], DAY, 8), (),
                         "运行时身份不是清单成员：拿它去分片会零领取（故两个身份不能合并）")
        self.assertIn(f":{os.getpid()}:", RUNTIME_OWNER)

    def test_ctx_egress_key_is_outlet_identity_not_stable_owner(self):
        """出口桶键 = 出口标识（`egress.egress_identity(该出口)`），不再是执行体身份。"""
        proxy = "http://u:pw@127.0.0.1:3128"
        seen = {}
        real = executor_v3._run_async

        async def spy(ctx):
            seen["executor_id"] = ctx.executor_id
            seen["runtime_id"] = ctx.runtime_id
            seen["egress"] = ctx.egress
            return await real(ctx)

        with mock.patch.dict(os.environ, {"YIBAN_PROXY": proxy}), \
                mock.patch.object(executor_v3, "_run_async", spy), \
                mock.patch.object(executor_v3.attempts, "attempt_signin",
                                  lambda acc: (True, "ok", False, "success")):
            self._run_v3(self._accounts(_phone(0)))
        self.assertEqual(seen["executor_id"], OWNER, "稳定名仍是分片/展示口径")
        self.assertEqual(seen["egress"], egress.egress_identity(proxy),
                         "出口桶键 = 出口标识（去 userinfo 的 scheme://host[:port]）")
        self.assertNotEqual(seen["egress"], OWNER, "桶键不再是执行体身份")
        self.assertEqual(seen["runtime_id"], RUNTIME_OWNER,
                         "写库的持有者身份必须含本进程的 PID/代次")

    def test_ctx_passes_outlet_executor_count_as_shares(self):
        """同出口 3 个执行体（2 worker + 开启的兜底）⇒ 限速器收到 `shares=3`。"""
        os.environ["YIBAN_EXECUTORS"] = egress.dump_manifest([
            {"slot": 0, "type": "worker", "proxy": ""},
            {"slot": 1, "type": "worker", "proxy": ""},
            {"slot": 2, "type": "fallback", "proxy": ""},
        ])
        os.environ["YIBAN_FALLBACK_ENABLE"] = "1"
        seen_shares = []
        with mock.patch.object(executor_v3.attempts, "attempt_signin",
                               lambda acc: (True, "ok", False, "success")):
            self._run_v3(self._accounts(_phone(0)), shares_seen=seen_shares)
        self.assertEqual(seen_shares, [3],
                         "同出口 3 个执行体（2 worker + 兜底）⇒ 份额分母 3")

    def test_real_refill_writes_runtime_owner_to_queue(self):
        phone = _phone(0)
        with mock.patch.object(executor_v3.attempts, "attempt_signin",
                               lambda acc: (True, "ok", False, "success")):
            results = self._run_v3(self._accounts(phone))
        row = self._row(phone)
        self.assertEqual(row["owner"], RUNTIME_OWNER,
                         "真实补货领取后，持有者一列必须是运行时身份（含 PID/代次）")
        self.assertIn(phone, results)
        self.assertEqual(row["state"], "done")


class EmitEventAttemptNotNullTest(unittest.TestCase):
    """事件留痕的 attempt 列约束：sign_events.attempt NOT NULL，批量落库单事务。

    状态迁移/收尾事件没有尝试号（attempt_no=None）——None 原样进批量事务会把
    本轮**全部**事件一起回滚（整批一个约束失败即全弃）。非尝试事件必须落 0。
    """

    def test_missing_attempt_number_lands_as_zero(self):
        collected = []
        ctx = SimpleNamespace(event_sink=collected.append)
        executor_v3._emit_event(ctx, "13800000000", "success", "收尾事件")
        self.assertEqual(len(collected), 1)
        self.assertEqual(collected[0]["attempt"], 0)

    def test_real_attempt_number_passes_through(self):
        collected = []
        ctx = SimpleNamespace(event_sink=collected.append)
        executor_v3._emit_event(ctx, "13800000000", "failed", "登录失败", attempt_no=2)
        self.assertEqual(collected[0]["attempt"], 2)


class PerAccountLogLineTest(_Base):
    """每账号结果行：日志按 `[遮罩号]` 落行，`/api/my-logs` 的日历面板靠它回显。

    日志文件本身已整体脱敏（行内不落完整号），但每账号仍须至少一行结果——
    只在重试/放弃时落行的话，成功/跳过账号在面板里查无记录。
    """

    def test_success_attempt_logs_masked_result_line(self):
        phone = _phone(0)
        with mock.patch.object(executor_v3.attempts, "attempt_signin",
                               lambda acc: (True, "签到成功", False, "success")), \
                self.assertLogs("yiban", level="INFO") as cm:
            self._run_v3(self._accounts(phone))
        joined = "\n".join(cm.output)
        self.assertIn(executor_v3._mask_phone(phone), joined, "结果行必须含遮罩号")
        self.assertNotIn(phone, joined, "结果行不得落完整号（文件纪元=遮罩）")
        self.assertIn("签到成功", joined)


class LogAttributionTest(_Base):
    """执行体日志行的**归因**：每账号行带 `[角色+槽位+轮次]`，且**不含主机名**。

    多执行体并发写同一个按天日志文件（`sign-YYYY-MM-DD.log`），没有归因就只能靠时间戳猜
    "这行是谁说的"。口径取 `egress.owner_tag`（`parse_owner` 的角色+槽位）；身份原串
    （`single@{主机名}`，本文件里就是 `single@testhost`）带部署信息，**不得进日志**——
    模块 docstring 的红线。轮次只有常驻多轮的兜底会传（`round_no`），定时轮/手动轮不传。
    """

    BANNER = "v3 执行体："

    def setUp(self):
        super().setUp()
        # 横幅去重键是模块级的（同进程跨轮共享，正是降噪要的语义）：用例要断言"第一次
        # 是 INFO"，就得先清掉上一批用例留下的键，否则断言取决于执行顺序。
        self._saved_banner = executor_v3._BANNER_LAST
        executor_v3._BANNER_LAST = None
        self.addCleanup(setattr, executor_v3, "_BANNER_LAST", self._saved_banner)

    def _run_records(self, phone, identity=None, **kw):
        """跑一轮（打桩成必成功），返回 DEBUG 以上的全部日志记录。

        `identity` 给了就先换成该执行体身份（用于"换执行体不漏横幅"那一条）。
        """
        env = {"YIBAN_EXECUTOR_ID": identity} if identity else {}
        with mock.patch.dict(os.environ, env, clear=False), \
                mock.patch.object(executor_v3.attempts, "attempt_signin",
                                  lambda acc: (True, "签到成功", False, "success")), \
                self.assertLogs("yiban", level="DEBUG") as cm:
            self._run_v3(self._accounts(phone), **kw)
        return cm.records

    @staticmethod
    def _joined(records):
        return "\n".join(r.getMessage() for r in records)

    @classmethod
    def _banner_records(cls, records):
        return [r for r in records if cls.BANNER in r.getMessage()]

    def test_per_account_line_carries_role_slot_and_round(self):
        phone = _phone(0)
        joined = self._joined(self._run_records(phone, round_no=3))
        self.assertIn("[single r3] [%s]" % executor_v3._mask_phone(phone), joined,
                      "每账号行必须带执行体归因前缀（角色+槽位+轮次）")
        self.assertNotIn("testhost", joined, "日志里出现了身份原串里的主机名")

    def test_round_is_absent_when_caller_does_not_supply_one(self):
        """定时轮/手动轮一个进程只跑一轮：前缀只有角色，不凭空造轮次。"""
        joined = self._joined(self._run_records(_phone(0)))
        self.assertIn("[single] [", joined)
        self.assertNotIn("[single r", joined)

    def test_banner_is_info_once_then_debug_for_unchanged_scale(self):
        """横幅只在**规模变化**时打 INFO：常驻空转轮逐轮重复同一行只是刷屏。

        生产 2026-10-07 实测该行全天 184 次，全是同一串规模与归属。降级不是静默：
        第二次仍要有留痕（DEBUG），排障时数得清轮次。
        """
        first = self._banner_records(self._run_records(_phone(0)))
        second = self._banner_records(self._run_records(_phone(1)))
        self.assertEqual([r.levelname for r in first], ["INFO"], "首次必须打 INFO")
        self.assertEqual([r.levelname for r in second], ["DEBUG"],
                         "规模没变的第二次不得再打 INFO（生产 5 秒一对刷屏的成因）")

    def test_banner_key_includes_the_identity(self):
        """去重键含身份：换执行体（规模不变）仍要打它起跑的那条 INFO。

        键里只留规模时，同一进程里换一个执行体会被判成"没变"——去重就失去意义。
        """
        other = "fallback@testhost"
        self._run_records(_phone(0))               # 本执行体（single@testhost）
        records = self._banner_records(self._run_records(
            _phone(1), identity=other, cfg=_cfg(executors=[other]), items=[]))
        self.assertEqual([r.levelname for r in records], ["INFO"],
                         "换了执行体却没重打 INFO——去重键漏了身份")

    def test_banner_reports_scale_without_the_raw_identity(self):
        records = self._banner_records(self._run_records(_phone(0), round_no=1))
        self.assertEqual(len(records), 1)
        message = records[0].getMessage()
        self.assertNotIn("single@testhost", message,
                         "横幅回了身份原串（带主机名）——模块红线要求只回角色与槽位")
        self.assertIn("[single r1] " + self.BANNER, message, "横幅必须带归因前缀")

    #: 复审点名的"可带而未带"行 → `(模块文件, 消息片段, 该条可接受的归因标识)`。
    #: 这些行触发路径较深（队列连续读不通 / 判因不可得 / 三条收尾分支 / 出口限速），
    #: 逐条构造运行场景的成本远高于收益，故按**该条 logger 语句本身**钉住。窗口必须精确到语句本身：早先的实现取消息前后各一段定长窗口，会被相邻
    #: 分支的归因标识蒙过（复审实证假绿）——精确度靠下面的括号配对，不靠"取宽一点"。
    TAGGED_CALLS = (
        ("yiban/engine/executor_v3.py", "队列连续 %d 轮读不通", ("log_tag",)),
        ("yiban/engine/executor_v3.py", "未执行判因不可得", ("log_tag",)),
        ("yiban/engine/executor_v3.py", "非法，全局速率上界按不限处理", ("log_tag",)),
        ("yiban/engine/executor_v3.py", "v3 执行体未预期异常", ("log_tag",)),
        ("yiban/engine/executor_v3.py", "v3 窗口收尾失败", ("log_tag",)),
        ("yiban/engine/executor_v3.py", "v3 未执行判因失败", ("log_tag",)),
        ("yiban/workers/workers.py", "任务队列事件签名变化", ("tag",)),
        ("yiban/engine/token_bucket.py", "风控信号，速率按安全回退下调", ("outlet_label",)),
        ("yiban/engine/token_bucket.py", "速率 %.3f → %.3f attempt/s", ("outlet_label",)),
    )

    @staticmethod
    def _logger_call_text(src, at):
        """取含 `src[at]` 的那一条 `logger.<level>(…)` 调用的完整文本（ASCII 括号配对）。

        从消息往前找最近的 `logger.`，再配对到它自己的收尾右括号——相邻分支的实参因此
        进不了这段文本。消息与实参里的全角括号不参与配对。
        """
        start = src.rindex("logger.", 0, at + 1)
        open_at = src.index("(", start)
        depth = 0
        for i in range(open_at, len(src)):
            if src[i] == "(":
                depth += 1
            elif src[i] == ")":
                depth -= 1
                if depth == 0:
                    return src[start:i + 1]
        raise AssertionError(f"logger 调用括号不配对（偏移 {start}）")

    def test_reviewed_log_lines_keep_their_attribution(self):
        for rel, msg, names in self.TAGGED_CALLS:
            with self.subTest(msg=msg):
                with open(os.path.join(BASE, rel), encoding="utf-8") as f:
                    src = f.read()
                call = self._logger_call_text(src, src.index(msg))
                self.assertTrue(any(n in call for n in names),
                                f"{rel} 的「{msg}」又丢了归因前缀：本条调用里没有 {names}；"
                                f"实际调用文本：{call[:200]}")


# ---------------------------------------------------------------------------
# 从冻结载体（`round.run_queue_retry`）迁到真路径的判据（工单 `77s2`）
# ---------------------------------------------------------------------------
class ProbeDayUnfreezeTest(_Base):
    """半开试探日：登录成功但被窗口/无 Range 跳过 ⇒ 必须解冻。

    **来源与理由**：这两条判据原先住在 `tests/test_breaker.py`，驱动的是冻结的
    `round.run_queue_retry`。冻结载体里的语义（`round.py` 的 `probe_healthy`）是：
    `SKIPPED_WINDOW` / `SKIPPED_NORANGE` 发生在**登录成功之后**，凭据已被证实可用；
    只看 `success` 会把窗口跳过误判成试探失败，再冻一个试探周期。迁到 v3（唯一生产
    执行体）后，这条语义必须在真路径上成立——否则被暂停账号的 `probe_date` 停在过去，
    `_probe_due` 每天为真，密码确实错的账号被每天真实登录一次（风控暴露面）。

    对照组钉住反方向：试探日仍是**凭据类失败** ⇒ 保持暂停并顺延 `probe_date`。
    """

    def _cred(self, phone):
        return {phone: {"fail_days": 3, "last_fail": "2026-09-21",
                        "paused_since": "2026-09-15", "probe_date": DAY}}

    def _run_probe(self, phone, attempt_result):
        self._add_claimed(phone)
        self._seed_v(8)
        cred = self._cred(phone)
        with mock.patch.object(executor_v3.attempts, "attempt_signin",
                               lambda acc: attempt_result):
            self._run_v3(self._accounts(phone), [_item(phone)], cred_state=cred)
        return cred

    def test_probe_day_window_skip_unfreezes(self):
        for status, message in (
            (yiban_status.STATUS_SKIPPED_WINDOW, "签到时段已结束"),
            (yiban_status.STATUS_SKIPPED_NORANGE, "签到时间窗口缺失（无 Range），已跳过"),
        ):
            with self.subTest(status=status):
                phone = _phone(1)
                self._clear_tasks()
                cred = self._run_probe(phone, (False, message, True, status))
                self.assertNotIn(
                    phone, cred,
                    "试探日登录已成功、仅被窗口规则跳过 ⇒ 凭据可用，必须解冻"
                    "（否则 probe_date 停在过去，每天重登一次）")

    def test_probe_day_credential_failure_keeps_pause_and_extends_probe(self):
        phone = _phone(1)
        cred = self._run_probe(
            phone, (False, "账号或密码错误", False, yiban_status.STATUS_FAILED))
        self.assertIn(phone, cred, "凭据类失败必须保持暂停")
        self.assertGreater(cred[phone]["probe_date"], DAY,
                           "半开试探失败必须顺延 probe_date（唯一写入点是 _update_cred_state）")


class RetryReasonVisibilityTest(_Base):
    """重试入队的状态文件与日志都必须带**转义后**的失败原因（防日志注入）。

    原判据住在 `tests/test_schedule_retry.py` 的冻结载体上；v3 的对应写入点
    （`_sanitize_text` + `STATUS_RETRYING`）此前没有直接用例。
    """

    def test_retry_state_and_log_escape_newlines(self):
        phone = _phone(1)
        self._add_claimed(phone)
        self._seed_v(8)
        reason = "获取签到任务失败\n未登录或登录已经超时"
        with mock.patch.object(executor_v3.attempts, "attempt_signin",
                               lambda acc: (False, reason, False, "failed")), \
                mock.patch.object(executor_v3, "next_retry_at_v3",
                                  lambda *a, **k: START + datetime.timedelta(seconds=30)), \
                self.assertLogs("yiban", level="WARNING") as cm:
            self._run_v3(self._accounts(phone), [_item(phone)])
        state = self._read_state()[phone]
        self.assertEqual(state["status"], yiban_status.STATUS_RETRYING)
        self.assertNotIn("\n", state["message"], "状态文件 message 不得含真实换行")
        self.assertIn(executor_v3._sanitize_text(reason), state["message"],
                      "状态文件必须补记转义后的失败原因")
        retry_lines = [r.getMessage() for r in cm.records if "待重试" in r.getMessage()]
        self.assertTrue(retry_lines, "未捕获到重试入队日志")
        self.assertTrue(all("\n" not in line for line in retry_lines),
                        "原因里的换行会把日志拆成多行（注入形态）")


class HostClockNotUsedTest(unittest.TestCase):
    """窗口/当日判定只认业务钟（`yiban.core.clock`），不得直接读宿主墙钟。

    `clock.now()` 恒为 UTC+8，与宿主 TZ 无关；直接 `datetime.now()` 会让"窗口判定"
    随部署机时区漂移（旧 `round` 路径上实测过：UTC 主机把北京时间 06:40 判成窗口外，
    整轮零请求）。本门用 AST 钉住执行核四个文件里没有宿主墙钟读取——原行为用例住在
    冻结载体 `tests/test_effective_window.py::WindowDecisionUsesBeijingClockTest`。
    """

    FILES = (
        "yiban/engine/executor_v3.py",
        "yiban/engine/schedule.py",
        "yiban/engine/attempts.py",
        "yiban/core/window.py",
    )

    def test_no_direct_host_clock_read(self):
        import ast

        hits = []
        for rel in self.FILES:
            path = os.path.join(BASE, rel)
            with open(path, encoding="utf-8") as f:
                tree = ast.parse(f.read(), filename=path)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if isinstance(func, ast.Attribute) and func.attr == "now":
                    dotted = ast.unparse(func)
                    if dotted in ("datetime.now", "datetime.datetime.now"):
                        hits.append("%s:%d %s" % (rel, node.lineno, dotted))
        self.assertEqual(hits, [],
                         "执行核不得直接读宿主墙钟（业务钟唯一入口是 yiban.core.clock）：%s"
                         % hits)


if __name__ == "__main__":
    unittest.main()
