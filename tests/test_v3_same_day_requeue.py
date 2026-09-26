# -*- coding: utf-8 -*-
"""v3 当日失败任务回炉口与兜底腿的 v2/v3 分流（补签链灰度硬前置的反例钉）。

标签：B · 调度：领取/队列/执行体
覆盖：v3 弃权收尾写 `retry:`/`final:` 档位前缀（沿用领取层的同一份协议常量）、
   `queue_store.requeue_failed` 的档位过滤（默认档自动回炉、保守档只由显式路径）、
   state+epoch 门复用（终态/在飞行绝不复活）、分片与业务日作用域、
   `run_executor_v3` 轮首回炉与 `requeue_final` 显式档、会话内恢复周期的默认档回炉
   （`requeue_during_run`）、`claim_all` 全分片扫尾（兜底身份的领取范围）、
   兜底 worker 按 `YIBAN_SCHEDULER_V3` 分流（v3 走任务队列执行体、v2 走领取池路径）、
   `runner` 补签轮把 `requeue_final` 透传给 v3。
对应实现：yiban/store/queue_store.py（requeue_failed）、yiban/engine/executor_v3.py
   （_tier_prefix、run_executor_v3 的 requeue_final/claim_all/requeue_during_run/slot）、
   yiban/engine/workers.py（run_fallback_worker 的分流）、yiban/engine/runner.py（分流点透传）。
关键断言：`retry:` 档 failed 必须当日被普通轮回炉（`claim_batch` 只取 pending，不回炉
   就是"当日无人接手"）；`final:` 档与普通 unprefixed 历史行绝不自动复活——复活等于
   风控账号每轮重登，只有显式路径（`requeue_final=True`，对齐补签轮的 `retry_failed`）
   放行。回炉必须走 `requeue_task` 的 state+epoch 门（被接管/在飞的行写不进去），
   不另造第二套协议。兜底腿的分流只换执行体实现，档位纪律不变（默认档）。
依赖：临时 sqlite（sign_tasks）+ 假时钟 + 打桩 attempt_signin/限速三件套；兜底分流用例
   打桩主循环外部依赖（不起子进程、不发网络请求）。整文件在本机执行，无 skip。

用法（项目根目录）：
    py -m pytest tests/test_v3_same_day_requeue.py -v
"""
import asyncio
import contextlib
import datetime
import os
import random
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from typing import ClassVar
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import db  # noqa: E402

from yiban import clock, egress  # noqa: E402
from yiban.engine import executor_v3, hrw, token_bucket, workers  # noqa: E402
from yiban.engine import runner as runner_mod  # noqa: E402
from yiban.store import claims, clock_meta, queue_store  # noqa: E402

TEST_KEY = "a" * 64
DAY = "2026-09-22"          # 周二，避开周末门
START = datetime.datetime(2026, 9, 22, 6, 40, 0)
OWNER = "single@testhost"
OTHER = "worker-1@testhost"
RUNTIME_OWNER = egress.runtime_owner(OWNER)


def _ts(**kw):
    return (START + datetime.timedelta(**kw)).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def _phone(i):
    return f"1380000{i:04d}"


class _FakeClock:
    """`_now`/`_sleep`/`_mono` 共用一份推进（与 test_executor_v3 同口径）。"""

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
        await asyncio.sleep(0)


def _cfg(**over):
    cfg = {
        "order": "sequence", "dist": "uniform",
        "edge_front_sec": 60, "edge_back_sec": 60, "block_cap": 15,
        "mu_min_pct": 40, "mu_max_pct": 60, "sigma_min_pct": 15, "sigma_max_pct": 25,
        "min_exec_gap": 5, "avg_attempt_sec": 3, "retry_min_interval": 60,
        "exec_gap_min": 10, "allow_time_pref": 0,
        "sign_start": (6, 30), "sign_end": (7, 50),
        "bucket_rate": 1.0, "executors": [OWNER],
    }
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
    await asyncio.Event().wait()


def _item(phone, run_at=None, attempts=0, epoch=1,
          priority=executor_v3.PRIORITY_ORDER_BASE):
    return (priority, run_at or _ts(seconds=-1), phone, attempts, epoch)


class _Base(unittest.TestCase):
    """临时库 + 假时钟 + 替身三件套（沿用 v3 执行体测试的夹具口径）。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-v3-requeue-")
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
        self.fc = _FakeClock()
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

    def _row(self, phone, day=DAY):
        row = db.get_conn().execute(
            "SELECT * FROM sign_tasks WHERE phone=? AND day=?", (phone, day)).fetchone()
        return dict(row) if row else None

    def _seed_v(self, v=8, day=DAY):
        clock_meta.set_meta(executor_v3.V_META_KEY_PREFIX + day, v)

    def _run_v3(self, accounts, items=None, *, cfg=None, rng=None, limiter=None,
                gate=None, delegated=None, cred_state=None, event_sink=None,
                dry_run=False, notify_url="", **extra):
        cfg = cfg or _cfg()
        limiter = limiter if limiter is not None else _PermissiveLimiter()
        gate = gate if gate is not None else _PermissiveGate()
        patches = [mock.patch.object(executor_v3, "_persist_loop", _never)]
        if items is not None:
            async def _refill(queue, shards, ctx):
                for it in items:
                    queue.put_nowait(it)
                for _ in range(ctx.m):
                    queue.put_nowait((executor_v3.SENTINEL_PRIORITY, "", "", 0, 0))
            patches.append(mock.patch.object(executor_v3, "_refiller", _refill))
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
                dry_run=dry_run, notify_url=notify_url, **extra)
        finally:
            for p in reversed(patches):
                p.stop()


# ---------------------------------------------------------------------------
# ⑤ 写侧：v3 弃权收尾必须带档位前缀（与领取层同一份协议）
# ---------------------------------------------------------------------------
class TierPrefixSettleTest(_Base):
    """failed 收尾写 `retry:`/`final:` 前缀——档位是回炉口的唯一判据，写侧必须先成立。"""

    def _settle_one(self, status, *, success=False, skip=False, message="m",
                    attempts=0, epoch=1, cred_state=None):
        phone = _phone(1)
        self._add_claimed_one(phone, attempts=attempts, epoch=epoch)
        self._seed_v(8)
        with mock.patch.object(executor_v3.attempts, "attempt_signin",
                               lambda acc: (success, message, skip, status)):
            results = self._run_v3(self._accounts(phone),
                                   [_item(phone, attempts=attempts, epoch=epoch)],
                                   cred_state=cred_state)
        return phone, results

    def _add_claimed_one(self, phone, vshard=0, attempts=0, epoch=1):
        self._add_task(phone, vshard=vshard, state="claimed", owner=RUNTIME_OWNER,
                       lease_until=_ts(seconds=30), attempts=attempts, epoch=epoch)

    def test_window_skip_carries_retry_prefix(self):
        phone, _res = self._settle_one("skipped_window", skip=True, message="签到时段已结束")
        row = self._row(phone)
        self.assertEqual(row["state"], "failed")
        self.assertTrue(row["result"].startswith(claims.RESULT_RETRY_PREFIX),
                        "窗口外属默认档：不写 retry: 前缀，当日回炉口就认不出它")

    def test_no_position_carries_retry_prefix(self):
        phone, _res = self._settle_one("no_position", message="未找到签到位置数据", attempts=0)
        self.assertTrue(self._row(phone)["result"].startswith(claims.RESULT_RETRY_PREFIX))

    def test_exhausted_budget_carries_final_prefix(self):
        phone, _res = self._settle_one("failed", message="网络抖动", attempts=2)
        self.assertTrue(self._row(phone)["result"].startswith(claims.RESULT_FINAL_PREFIX))

    def test_paused_zero_request_carries_final_prefix(self):
        phone = _phone(1)
        self._add_claimed_one(phone)
        self._seed_v(8)
        cred_state = {phone: {"paused_since": DAY, "probe_date": "2999-01-01"}}
        with mock.patch.object(executor_v3.attempts, "attempt_signin",
                               lambda acc: (True, "ok", False, "success")):
            self._run_v3(self._accounts(phone), [_item(phone)], cred_state=cred_state)
        row = self._row(phone)
        self.assertEqual(row["state"], "failed")
        self.assertTrue(row["result"].startswith(claims.RESULT_FINAL_PREFIX),
                        "账密暂停不是默认档：自动回炉会绕开熔断（保守档）")

    def test_done_result_has_no_prefix(self):
        phone, _res = self._settle_one("success", success=True, message="签到成功")
        row = self._row(phone)
        self.assertEqual(row["state"], "done")
        self.assertFalse(row["result"].startswith(claims.RESULT_RETRY_PREFIX))
        self.assertFalse(row["result"].startswith(claims.RESULT_FINAL_PREFIX))

    def test_tier_authority_is_the_same_set_as_v2(self):
        """分档判据只有一份：与 v2 `_settle_claims` 共用 `RETRYABLE_GIVE_UP_STATUSES`。"""
        for st in claims.RETRYABLE_GIVE_UP_STATUSES:
            with self.subTest(status=st):
                self.assertEqual(executor_v3._tier_prefix(st), claims.RESULT_RETRY_PREFIX)
        self.assertEqual(executor_v3._tier_prefix("failed"), claims.RESULT_FINAL_PREFIX)
        self.assertEqual(executor_v3._tier_prefix("paused"), claims.RESULT_FINAL_PREFIX)


# ---------------------------------------------------------------------------
# ⑤ 读侧：queue_store.requeue_failed（沿用 requeue_task 的 state+epoch 门）
# ---------------------------------------------------------------------------
class RequeueFailedStoreTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-requeue-store-")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n")
        os.environ.update({
            "YIBAN_ACCOUNTS_KEY": TEST_KEY,
            "YIBAN_ENV_FILE": cls.env_file,
            "YIBAN_DB_FILE": cls.db_file,
            "YIBAN_STATE_DIR": cls.tmp,
        })

    @classmethod
    def tearDownClass(cls):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k in ("YIBAN_ACCOUNTS_KEY", "YIBAN_ENV_FILE", "YIBAN_DB_FILE",
                  "YIBAN_STATE_DIR"):
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
        db.init_db(self.db_file, env_file=self.env_file, cleanup=False)

    def _add(self, phone, day=DAY, vshard=0, state="failed", result="", epoch=2,
             attempts=1):
        conn = db.get_conn()
        conn.execute(
            "INSERT INTO sign_tasks (phone, day, vshard, owner, run_at, priority, "
            "state, attempts, lease_until, result, epoch, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (phone, day, vshard, "", f"{day} 06:40:00.000", 5, state, attempts,
             "", result, epoch, f"{day} 06:30:00.000"))
        conn.commit()

    def _row(self, phone, day=DAY):
        row = db.get_conn().execute(
            "SELECT * FROM sign_tasks WHERE phone=? AND day=?", (phone, day)).fetchone()
        return dict(row) if row else None

    def test_default_tier_flips_conservative_needs_explicit(self):
        retry_p, final_p, legacy_p = _phone(1), _phone(2), _phone(3)
        self._add(retry_p, result=claims.RESULT_RETRY_PREFIX + "skipped_window")
        self._add(final_p, result=claims.RESULT_FINAL_PREFIX + "网络抖动")
        self._add(legacy_p, result="网络抖动")   # 历史无前缀 = 保守档
        flipped = queue_store.requeue_failed(
            DAY, (0,), run_at=f"{DAY} 07:00:00.000")
        self.assertEqual(flipped, 1, "默认只回炉 retry: 档")
        self.assertEqual(self._row(retry_p)["state"], "pending")
        self.assertEqual(self._row(retry_p)["result"], "", "重排后 result 不再代表当前状态")
        self.assertEqual(self._row(retry_p)["run_at"], f"{DAY} 07:00:00.000")
        self.assertEqual(self._row(final_p)["state"], "failed")
        self.assertEqual(self._row(legacy_p)["state"], "failed")
        # 显式路径：final 与无前缀行一并回炉
        flipped = queue_store.requeue_failed(
            DAY, (0,), include_final=True, run_at=f"{DAY} 07:00:00.000")
        self.assertEqual(flipped, 2)
        self.assertEqual(self._row(final_p)["state"], "pending")
        self.assertEqual(self._row(legacy_p)["state"], "pending")

    def test_attempts_and_priority_advance_through_requeue_task(self):
        p = _phone(1)
        self._add(p, result=claims.RESULT_RETRY_PREFIX + "no_position", attempts=1)
        queue_store.requeue_failed(DAY, (0,), run_at=f"{DAY} 07:00:00.000")
        row = self._row(p)
        self.assertEqual(row["attempts"], 2, "回炉一次=一次跨执行体重试预算，必须记账")
        self.assertEqual(row["priority"], 6, "回炉排在新任务之后（requeue_task 同一条纪律）")

    def test_primitive_reuse_and_epoch_gate(self):
        """回炉必须经 requeue_task 原语并带上 SELECT 时的 epoch（不另造第二套写路径）。"""
        p = _phone(1)
        self._add(p, result=claims.RESULT_RETRY_PREFIX + "x", epoch=7)
        seen = []
        real = queue_store.requeue_task

        def spy(phone, day, run_at, priority_delta=1, result="", epoch=None):
            seen.append((phone, day, epoch))
            return real(phone, day, run_at, priority_delta=priority_delta,
                        result=result, epoch=epoch)

        with mock.patch.object(queue_store, "requeue_task", spy):
            queue_store.requeue_failed(DAY, (0,), run_at=f"{DAY} 07:00:00.000")
        self.assertEqual(seen, [(p, DAY, 7)], "必须逐行带领取时读到的 epoch 走 requeue_task")
        # 迟到的回炉：行已被人重领（epoch 变了）→ 写不进去
        self._add(_phone(2), result=claims.RESULT_RETRY_PREFIX + "x", epoch=3)
        queue_store.requeue_task(_phone(2), DAY, f"{DAY} 07:00:00.000", epoch=3)  # → pending
        conn = db.get_conn()
        conn.execute("UPDATE sign_tasks SET state='claimed', epoch=epoch+1 "
                     "WHERE phone=?", (_phone(2),))   # 模拟被接管：epoch 再进一代
        conn.commit()
        self.assertEqual(queue_store.requeue_failed(
            DAY, (0,), run_at=f"{DAY} 07:10:00.000"), 0)

    def test_claimed_and_terminal_never_flipped(self):
        """在飞（claimed）与终态（done/skipped）绝不回炉——前者重复登录、后者谎报了结。"""
        self._add(_phone(1), state="claimed", result="", epoch=4)
        self._add(_phone(2), state="done", result="签到成功", epoch=4)
        self._add(_phone(3), state="skipped", result="用户已取消签到", epoch=4)
        self.assertEqual(queue_store.requeue_failed(
            DAY, (0,), include_final=True, run_at=f"{DAY} 07:00:00.000"), 0)
        self.assertEqual(self._row(_phone(1))["state"], "claimed")
        self.assertEqual(self._row(_phone(2))["state"], "done")
        self.assertEqual(self._row(_phone(3))["state"], "skipped")

    def test_scope_shard_day_and_history_rows(self):
        self._add(_phone(1), vshard=3, result=claims.RESULT_RETRY_PREFIX + "x")
        self._add(_phone(2), vshard=5, result=claims.RESULT_RETRY_PREFIX + "x")
        self._add(_phone(3), day="2026-09-21", result=claims.RESULT_RETRY_PREFIX + "x")
        self._add(_phone(4), vshard=-1, result=claims.RESULT_RETRY_PREFIX + "x")
        flipped = queue_store.requeue_failed(DAY, (3, 4), run_at=f"{DAY} 07:00:00.000")
        self.assertEqual(flipped, 1)
        self.assertEqual(self._row(_phone(1))["state"], "pending")
        self.assertEqual(self._row(_phone(2))["state"], "failed", "分片集外的行不碰")
        self.assertEqual(self._row(_phone(3), "2026-09-21")["state"], "failed",
                         "跨业务日的行不碰（历史 failed 回炉=昨日的号今天再登录）")
        self.assertEqual(self._row(_phone(4))["state"], "failed",
                         "vshard=-1 的历史行不属于任何分片集")

    def test_idempotent_second_pass_zero(self):
        self._add(_phone(1), result=claims.RESULT_RETRY_PREFIX + "x")
        stamp = f"{DAY} 07:00:00.000"
        self.assertEqual(queue_store.requeue_failed(DAY, (0,), run_at=stamp), 1)
        self.assertEqual(queue_store.requeue_failed(DAY, (0,), run_at=stamp), 0)

    def test_empty_shards_does_not_touch_db(self):
        with mock.patch.object(queue_store, "_queue_conn",
                               side_effect=AssertionError("不该取连接")):
            self.assertEqual(queue_store.requeue_failed(DAY, ()), 0)


# ---------------------------------------------------------------------------
# ⑤ 会话接线：轮首回炉（自动档/显式档）+ 会话内周期 + claim_all 扫尾
# ---------------------------------------------------------------------------
class SessionRequeueTest(_Base):
    """反例 (a)(b)：retry: 档当日被普通轮回炉；final: 档只由显式路径回炉。"""

    def _failed_row(self, phone, prefix, vshard=0, day=DAY):
        self._add_task(phone, vshard=vshard, state="failed", day=day,
                       result=prefix + "e2e-mock", epoch=1)

    def test_retry_tier_is_reflown_by_normal_round(self):
        """反例 (a)：retry: 档 failed → 普通轮轮首回炉 → 本轮领到并签成。"""
        phone = _phone(1)
        self._failed_row(phone, claims.RESULT_RETRY_PREFIX)
        self._seed_v(8)
        calls = []
        with mock.patch.object(executor_v3.attempts, "attempt_signin",
                               lambda acc: calls.append(acc.phone)
                               or (True, "回炉后签到成功", False, "success")):
            self._run_v3(self._accounts(phone))
        self.assertEqual(calls, [phone], "retry: 档无人回炉就是 v3 的\"当日失败没人接手\"")
        self.assertEqual(self._row(phone)["state"], "done")

    def test_final_tier_needs_explicit_path(self):
        """反例 (b)：final: 档普通轮绝不自动复活；显式路径（requeue_final=True）才回炉。"""
        phone = _phone(2)
        self._failed_row(phone, claims.RESULT_FINAL_PREFIX)
        self._seed_v(8)
        calls = []
        with mock.patch.object(executor_v3.attempts, "attempt_signin",
                               lambda acc: calls.append(acc.phone)
                               or (True, "ok", False, "success")):
            self._run_v3(self._accounts(phone))
        self.assertEqual(calls, [], "final: 档被普通轮复活 = 风控账号每轮重登（Task 4 档位纪律）")
        self.assertEqual(self._row(phone)["state"], "failed")
        # 显式路径：补签轮语义
        with mock.patch.object(executor_v3.attempts, "attempt_signin",
                               lambda acc: calls.append(acc.phone)
                               or (True, "显式回炉成功", False, "success")):
            self._run_v3(self._accounts(phone), requeue_final=True)
        self.assertEqual(calls, [phone])
        self.assertEqual(self._row(phone)["state"], "done")

    def test_unprefixed_history_row_is_conservative(self):
        """历史无前缀行按保守档处置（与领取层"无前缀=需显式路径"同一判据）。"""
        phone = _phone(3)
        self._failed_row(phone, "")
        self._seed_v(8)
        calls = []
        with mock.patch.object(executor_v3.attempts, "attempt_signin",
                               lambda acc: calls.append(acc.phone)
                               or (True, "ok", False, "success")):
            self._run_v3(self._accounts(phone))
        self.assertEqual(calls, [])
        self.assertEqual(self._row(phone)["state"], "failed")

    def test_other_day_failed_not_touched(self):
        phone = _phone(4)
        self._failed_row(phone, claims.RESULT_RETRY_PREFIX, day="2026-09-21")
        self._seed_v(8)
        calls = []
        # 不把这个号放进本轮账号列表：在册账号缺当日计划行时，建计划会**正当**补
        # 新行并领取（那是"今日该签"的职责，不是回炉复活）——本反例盯的只是
        # "昨日 failed 不被回炉越日翻态"。
        with mock.patch.object(executor_v3.attempts, "attempt_signin",
                               lambda acc: calls.append(acc.phone)
                               or (True, "ok", False, "success")):
            self._run_v3([])
        self.assertEqual(calls, [], "轮首回炉只碰本业务日")
        self.assertEqual(self._row(phone, "2026-09-21")["state"], "failed")

    def test_delegated_shards_are_swept_with_claim_all(self):
        """兜底身份不在 HRW 候选集：claim_all 让 v3 兜底扫全部分片（否则零领取=静默空转）。"""
        cfg = _cfg(executors=[OWNER, OTHER])
        self._seed_v(64)
        mine = set(hrw.shards_of(OWNER, cfg["executors"], DAY, 64))
        foreign = next(s for s in range(64) if s not in mine)
        phone = _phone(5)
        self._add_task(phone, vshard=foreign, state="pending")
        calls = []
        with mock.patch.object(executor_v3.attempts, "attempt_signin",
                               lambda acc: calls.append(acc.phone)
                               or (True, "兜底扫尾成功", False, "success")):
            results = self._run_v3(self._accounts(phone), cfg=cfg, claim_all=True)
        self.assertEqual(calls, [phone], "兜底 v3 必须领得到别人分片里还没了的活")
        self.assertEqual(results[phone][3], "success")

    def test_requeue_during_run_recovers_retry_tier_mid_session(self):
        """兜底 v3 会话内：恢复周期把刚弃权的 retry: 档回炉，不等下一场会话。"""
        self._seed_v(8)
        phone = _phone(6)
        due = _ts(seconds=70)              # 稍后到点的一行：把循环撑过首个恢复周期
        self._add_task(_phone(7), run_at=due, state="pending")
        self._add_task(phone, state="failed", result=claims.RESULT_RETRY_PREFIX + "x",
                       epoch=1)
        seen = []
        flips = []
        real = queue_store.requeue_failed

        def spy(day, shards, include_final=False, run_at=None):
            seen.append({"day": day, "shards": tuple(shards),
                         "include_final": include_final})
            flips.append(real(day, shards, include_final=include_final, run_at=run_at))
            return flips[-1]

        async def fake_lane(queue, lane_id, ctx):
            while True:
                if (await queue.get())[0] >= executor_v3.SENTINEL_PRIORITY:
                    return

        ctx = SimpleNamespace(cfg=_cfg(), day=DAY, executor_id=OWNER,
                              runtime_id=RUNTIME_OWNER, m=1, inflight=0, busy=0,
                              slot=0, shards=tuple(range(8)), v=8,
                              requeue_during_run=True)
        queue = asyncio.PriorityQueue()
        with mock.patch.object(executor_v3.queue_store, "requeue_failed", spy), \
             mock.patch.object(executor_v3, "_lane", fake_lane), \
             mock.patch.object(executor_v3, "_persist_loop", _never):
            asyncio.run(executor_v3._refiller(queue, tuple(range(8)), ctx))
        self.assertTrue(seen, "requeue_during_run 的会话必须按恢复周期回炉")
        self.assertTrue(all(c["include_final"] is False for c in seen),
                        "会话内周期只回炉默认档——final: 档的第二次机会只留给有界显式路径")
        self.assertIn(1, flips, "刚弃权的 retry: 档必须在恢复周期被翻回 pending")
        # 回炉的目的地是"本轮就被领走"：本用例没起通道（claimed 行无消费者、不会
        # 落库了结），行翻回 pending 后会被下一次 claim_batch 领成 claimed，租约
        # 到期又由 reap_expired 放回来——终态停在 pending/claimed 之一都是"已回炉
        # 且在本轮领取集里"。断言"永远 pending"反而要求"回炉了但绝不接手"，与
        # requeue_during_run 的存在意义相反。
        self.assertIn(self._row(phone)["state"], ("pending", "claimed"),
                      "回炉=当日接手：不得停留在 failed")


# ---------------------------------------------------------------------------
# B：runner 透传 requeue_final；兜底腿按开关分流
# ---------------------------------------------------------------------------
class RunnerRequeueFinalTest(unittest.TestCase):
    """`runner.main` 的 v3 分流点：补签轮身份 = 显式路径，透传给 `requeue_final`。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-requeue-runner-")
        self._env = {k: os.environ.get(k) for k in (
            "YIBAN_STATE_DIR", "YIBAN_DB_FILE", "YIBAN_LOG_FILE", "YIBAN_ENV_FILE",
            "YIBAN_SCHEDULER_V3", "YIBAN_GLOBAL_PAUSE", "YIBAN_SECOND_RUN",
            "YIBAN_EXECUTOR_ID")}
        os.environ.update({
            "YIBAN_STATE_DIR": self.tmp,
            "YIBAN_DB_FILE": os.path.join(self.tmp, "yiban.db"),
            "YIBAN_LOG_FILE": os.path.join(self.tmp, "sign.log"),
            "YIBAN_SCHEDULER_V3": "1",
        })
        for k in ("YIBAN_GLOBAL_PAUSE", "YIBAN_SECOND_RUN", "YIBAN_EXECUTOR_ID"):
            os.environ.pop(k, None)
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _run(self, second_run):
        accounts = [SimpleNamespace(phone=_phone(0), user_paused=False, owner="u@1")]
        v3_calls = []
        with mock.patch.dict(os.environ,
                             {"YIBAN_SECOND_RUN": "1" if second_run else "0"}), \
                mock.patch.object(runner_mod.accounts_mod, "load_accounts",
                                  return_value=accounts), \
                mock.patch.object(runner_mod.schedule_mod, "build_schedule",
                                  return_value={}), \
                mock.patch.object(runner_mod.schedule_mod, "day_off",
                                  lambda *a, **k: None), \
                mock.patch.object(runner_mod.executor_v3, "run_executor_v3",
                                  side_effect=lambda *a, **kw: (
                                      v3_calls.append(kw), dict())[1]), \
                mock.patch.object(runner_mod.state_io, "_load_cred_state",
                                  return_value={}), \
                mock.patch.object(runner_mod.state_io, "_save_cred_state"), \
                mock.patch.object(runner_mod.state_io, "_is_second_run",
                                  return_value=second_run), \
                mock.patch.object(runner_mod.state_io, "_second_run_drop_done",
                                  return_value=accounts), \
                mock.patch.object(runner_mod.state_io, "_write_sched_done"), \
                mock.patch.object(runner_mod.cli_support, "_acquire_run_lock",
                                  return_value=contextlib.suppress), \
                mock.patch.object(runner_mod.db, "add_sign_events_batch"), \
                mock.patch.object(runner_mod.db, "purge_expired_deleted_accounts"), \
                mock.patch.object(runner_mod.alerts, "_maybe_alert_zero_success"), \
                mock.patch.object(runner_mod.alerts, "_flush_admin_mail_summary"):
            runner_mod.main([])
        self.assertEqual(len(v3_calls), 1)
        return v3_calls[0]

    def test_first_run_is_not_explicit(self):
        self.assertFalse(self._run(second_run=False).get("requeue_final"),
                         "普通轮不得自动复活 final: 档")

    def test_second_run_is_explicit(self):
        self.assertTrue(self._run(second_run=True).get("requeue_final"),
                        "补签轮就是 v3 的显式路径（对齐 v2 的 retry_failed=_second_run）")


class _FallbackHarness(unittest.TestCase):
    """兜底主循环驱动（打桩口径与 tests/test_fallback_gates.py 的夹具一致）。"""

    WED = datetime.date(2026, 9, 2)
    BASE_ENV: ClassVar = {
        "YIBAN_SIGN_START": "06:30", "YIBAN_SIGN_END": "07:50",
        "YIBAN_SATURDAY_SIGN": "0", "YIBAN_SUNDAY_SIGN": "0",
        "YIBAN_GLOBAL_PAUSE": "0", "YIBAN_FALLBACK_INTERVAL": "60",
    }

    def setUp(self):
        self._env = {k: os.environ.get(k) for k in (
            "YIBAN_SCHEDULER_V3", "YIBAN_STATE_DIR", "YIBAN_EXECUTOR_ID")}
        self.tmp = tempfile.mkdtemp(prefix="yiban-fb-dispatch-")
        os.environ["YIBAN_STATE_DIR"] = self.tmp
        os.environ.pop("YIBAN_EXECUTOR_ID", None)
        os.environ.pop("YIBAN_SCHEDULER_V3", None)

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def _at(hm):
        return datetime.datetime(2026, 9, 2, hm[0], hm[1], 0)

    def _drive(self, *, v3_enabled):
        sleeps = []
        v2_kwargs = []
        v3_kwargs = []
        acc = SimpleNamespace(phone="13800000000", user_paused=False)
        results = {"13800000000": (True, "ok", False, "success")}

        def _retry(*a, **kw):
            v2_kwargs.append(kw)
            return results

        def _v3(*a, **kw):
            v3_kwargs.append(kw)
            return results

        times = iter([self._at((6, 35)), self._at((8, 30))])

        def _now():
            t = next(times, None)
            if t is None:
                raise AssertionError("兜底主循环没有在窗口关闭后退出")
            return t

        with mock.patch.dict(os.environ, {**self.BASE_ENV,
                                          "YIBAN_SCHEDULER_V3": "1" if v3_enabled else "0"},
                             clear=False), \
                mock.patch.object(workers, "time", SimpleNamespace(sleep=sleeps.append)), \
                mock.patch.object(workers.clock, "now", _now), \
                mock.patch.object(workers.cli_support, "_run_lock_held",
                                  lambda *a, **k: False), \
                mock.patch.object(workers.state_io, "_write_fallback_alive",
                                  lambda at=None: None), \
                mock.patch.object(workers.state_io, "_clear_fallback_alive", lambda: None), \
                mock.patch.object(workers.state_io, "_load_cred_state", lambda: {}), \
                mock.patch.object(workers.state_io, "_save_cred_state",
                                  lambda data, touched=None: None), \
                mock.patch.object(workers.db, "pool_db_declared", lambda: True), \
                mock.patch.object(workers.db, "is_initialized", lambda: True), \
                mock.patch.object(workers.accounts_mod, "load_accounts",
                                  lambda *a, **k: [acc]), \
                mock.patch.object(workers.round_mod, "run_queue_retry", _retry), \
                mock.patch.object(workers.executor_v3, "run_executor_v3", _v3):
            rc = workers.run_fallback_worker(["--fallback"])
        return rc, sleeps, v2_kwargs, v3_kwargs

    def test_v3_enabled_fallback_routes_to_v3_executor(self):
        """反例 (c)：`YIBAN_SCHEDULER_V3=1` 的兜底走任务队列执行体，不再硬编 v2/sign_claims。"""
        rc, _sleeps, v2_kwargs, v3_kwargs = self._drive(v3_enabled=True)
        self.assertEqual(v2_kwargs, [], "--fallback 仍硬编 v2 路径（兜底对 v3 队列零作用）")
        self.assertEqual(len(v3_kwargs), 1)
        kw = v3_kwargs[0]
        self.assertTrue(kw.get("claim_all"),
                        "兜底身份不在 HRW 候选集：不 claim_all 就是零领取的空转")
        self.assertTrue(kw.get("requeue_during_run"),
                        "会话内周期回炉是兜底腿\"失败当日接手\"的 v3 等价物")
        self.assertNotIn("retry_failed", kw)
        self.assertIsNone(kw.get("requeue_final"),
                          "兜底是无界常驻：不得作为显式路径复活 final: 档")
        self.assertEqual(rc, 0)

    def test_v3_off_fallback_keeps_v2_path_and_default_tier(self):
        """开关关时兜底逐字走旧路径（不新增 v3 调用、不传 retry_failed）。"""
        rc, _sleeps, v2_kwargs, v3_kwargs = self._drive(v3_enabled=False)
        self.assertEqual(v3_kwargs, [])
        self.assertEqual(len(v2_kwargs), 1)
        self.assertFalse(v2_kwargs[0].get("retry_failed"),
                         "兜底腿不得作为显式重领路径（Task 4 档位钉不变）")
        self.assertTrue(v2_kwargs[0].get("window_guard"))
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
