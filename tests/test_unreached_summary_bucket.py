# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""q1to 校验者：汇总把「未执行」计为「失败」+ 执行层零留痕。

标签：B · 调度：领取/队列/执行体
覆盖：`yiban/engine/runner.py`（轮末汇总的计数桶与汇总文本、退出码）、
     `yiban/engine/executor_v3.py`（轮末"未执行"判因与事件留痕）、
     `yiban/store/queue_store.py`（当日行的 `state`/`owner` 读取）。

**故障现场（2026-10-07 生产 v0.5.1）**：兜底把两个执行体的任务领走（84/100），两个
worker 到点领不到自己的行、`results` 里没有条目，汇总把它们记成"失败"（07:16 一行
"❌ 51"、07:29 一行 "❌ 33"）；同时 `sign_events` 当日只有 16 条 success + 6 条
user_cancelled——这 84 条"失败"零事件、零日志。运维从数据侧完全看不出"任务被别人领走"。

**本文件钉住的四条**：
1. 任务被别人领走 ⇒ 汇总出现**独立桶**（不是失败），并落 `sign_events` 可查记录
   （状态与文本都能与真失败区分）；
2. **「已由他人负责」收成一条批量**（工单 81xt）：事件与日志按扫描产出 O(1) 条，
   不随账号数线性增长——2026-10-10 生产该扫描对每个账号各落一行 pending 事件
   （×1830），事件表与日志被按账号数灌噪声；
3. 反向控制（红线）：真"没人接手"的账号**仍算失败**——统计口径不许被改成"看起来对"；
   这类真异常**仍逐账号留痕**（不并进批量），每条的判因文本完整可查；
4. 同源同口径：汇总计数与批量事件计数出自同一份判因（同一分桶），两者数量一致。

**依赖**：临时 SQLite（真迁移链）+ 真 `runner.main` + 真执行体补货循环（只替身登录与
限速）+ 假时钟。不发任何网络请求。
"""
import contextlib
import datetime
import json
import logging
import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import db

from yiban import clock, egress
from yiban import status as yiban_status
from yiban.engine import (
    executor_v3,
    hrw,
    planner,
    runner,
    schedule,
    token_bucket,
)
from yiban.store import clock_meta, queue_store
from yiban.store import connection as store_conn  # current_db_file 的唯一定义点

TEST_KEY = "a" * 64
DAY = "2026-09-22"          # 周二：周末门不拦
START = datetime.datetime(2026, 9, 22, 6, 40, 0)
WORKER0 = egress.worker_owner(0)
FALLBACK = egress.fallback_owner()
#: 同批"另一个执行体"的**运行时**身份（`claim_batch` 写进 `owner` 的形态，含进程号）
PEER = egress.runtime_owner(FALLBACK)

BASE_ENV = {
    "YIBAN_SIGN_START": "06:30",
    "YIBAN_SIGN_END": "07:50",
    "YIBAN_SATURDAY_SIGN": "0",
    "YIBAN_SUNDAY_SIGN": "0",
    "YIBAN_GLOBAL_PAUSE": "0",
    "YIBAN_EXECUTORS": json.dumps(
        [{"slot": 0, "type": "worker", "proxy": ""}],
        ensure_ascii=False, separators=(",", ":")),
}


def _phone(i):
    return f"1380000{i:04d}"


class _FakeClock:
    """`_now` / `_sleep` / `_mono` / `clock.now` 共用一份推进（不真睡）。"""

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


class _Limiter:
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


class _Gate:
    gap_sec = 10.0

    def allow(self, phone, now):
        return True

    def commit(self, phone, now):
        pass


async def _never(_ctx):
    import asyncio
    await asyncio.Event().wait()


class _LogCapture(logging.Handler):
    """收集 `yiban` 日志的正文（汇总行是运维读的那一面，必须被断言）。"""

    def __init__(self):
        super().__init__(level=logging.INFO)
        self.texts = []

    def emit(self, record):
        with contextlib.suppress(Exception):
            self.texts.append(record.getMessage())

    def joined(self):
        return "\n".join(self.texts)


class _Harness(unittest.TestCase):
    """真 `runner.main` + 真库 + 真执行体；只替身登录/限速/时钟/运行锁。"""

    ACCOUNTS = 6

    def setUp(self):
        # 共享 DB 单例教训（2026-10-07 复核 F1）：上一个文件/用例留下的 `db._conn` 会让
        # 本用例的 `db.init_db` 复用旧连接、读到陈旧库。setUp 第一件事就把它关掉。
        self._close_conn()
        # 批量留痕去重键是模块级的（`_mark_unreached` 跨轮共享，正是降噪要的语义）：
        # 用例间必须清掉，否则同组成的后一个用例会因上一批留下的键而漏落事件
        # （同 `_BANNER_LAST` 的处置）。
        self._saved_unreached = getattr(executor_v3, "_UNREACHED_PEER_LAST", None)
        executor_v3._UNREACHED_PEER_LAST = None
        self.addCleanup(setattr, executor_v3, "_UNREACHED_PEER_LAST",
                        self._saved_unreached)
        self.root = tempfile.mkdtemp(prefix="yiban-unreach-")
        self.db_file = os.path.join(self.root, "yiban.db")
        self.state_dir = os.path.join(self.root, "state")
        os.makedirs(self.state_dir)
        self.env_file = os.path.join(self.root, ".env")
        with open(self.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n")
        self.phones = [_phone(i) for i in range(1, self.ACCOUNTS + 1)]
        self._env = {k: os.environ.get(k) for k in (
            "YIBAN_DB_FILE", "YIBAN_ENV_FILE", "YIBAN_STATE_DIR", "YIBAN_ACCOUNTS_KEY",
            "YIBAN_EXECUTOR_ID", "YIBAN_EXECUTORS", "YIBAN_LOG_FILE",
            "YIBAN_GLOBAL_PAUSE", "YIBAN_ACCOUNTS_JSON")}
        os.environ.update({
            "YIBAN_DB_FILE": self.db_file,
            "YIBAN_ENV_FILE": self.env_file,
            "YIBAN_STATE_DIR": self.state_dir,
            "YIBAN_ACCOUNTS_KEY": TEST_KEY,
            "YIBAN_LOG_FILE": os.path.join(self.root, "sign.log"),
            # 本进程是**子执行体**身份（监督进程注入的那一个）：`runner.main` 据此不再派发。
            "YIBAN_EXECUTOR_ID": WORKER0,
            "YIBAN_ACCOUNTS_JSON": json.dumps(
                [{"phone": p, "password": "pw-" + p} for p in self.phones],
                ensure_ascii=False),
        })
        for k in ("YIBAN_GLOBAL_PAUSE",):
            os.environ.pop(k, None)
        # 窗口/清单/门必须**在夹具期**就生效：计划行的 owner 由 `YIBAN_EXECUTORS` 决定，
        # 少了它计划会写给 `single@…`，判因随后把"自己的行"判成别人的。
        env_patch = mock.patch.dict(os.environ, dict(BASE_ENV), clear=False)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        self.addCleanup(self._teardown)

    def _teardown(self):
        self._close_conn()
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.root, ignore_errors=True)

    @staticmethod
    def _close_conn():
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None

    # ---- 夹具 ----
    def _plan(self):
        """真计划层写出当日计划行（owner = 计划执行体，state = pending）。"""
        # 先关掉进程里的单例连接（共享 DB 单例教训，2026-10-07 复核 F1）：`db.init_db`
        # 在 `_conn` 非空时**直接复用旧连接**，那样本用例会写进／读出一个陈旧库。
        # 两个守卫都做：关连接 + 断言连接确实指向本用例的临时库。
        self._close_conn()
        db.init_db(self.db_file, env_file=self.env_file, cleanup=False)
        self.assertEqual(store_conn.current_db_file(), self.db_file,
                         "前置：单例连接必须指向本用例的临时库（残留连接＝读到陈旧库）")
        accounts = [SimpleNamespace(phone=p, user_paused=False, owner="admin")
                    for p in self.phones]
        cfg = schedule.planner_config()
        v = hrw.v_for(len(self.phones))
        planner.write_plan(
            planner.build_plan(accounts, DAY, cfg["executors"], v=v, cfg=cfg), DAY)
        clock_meta.set_meta(executor_v3.V_META_KEY_PREFIX + DAY, v)
        return v

    def _peer_settles_all(self):
        """另一个执行体把这些行**领走并了结**（10-07 兜底的形状：owner 含进程号）。"""
        conn = db.get_conn()
        conn.execute(
            "UPDATE sign_tasks SET state='done', owner=?, result='签到成功' WHERE day=?",
            (PEER, DAY))
        conn.commit()

    def _plan_all_due(self):
        """当日计划 + 把全部行的 `run_at` 压到过去（全部到点，本轮都领得到）。

        判因守卫要的是"本执行体**真的跑过**每个账号"：计划层默认把 `run_at` 错峰铺在
        整个窗口内，只压到过去才能在一轮里全领到、全跑一遍。
        """
        v = self._plan()
        past = (START - datetime.timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S") + ".000"
        conn = db.get_conn()
        conn.execute("UPDATE sign_tasks SET run_at=? WHERE day=?", (past, DAY))
        conn.commit()
        return v

    # ---- 读侧 ----
    def _events(self):
        cur = db.get_conn().execute(
            "SELECT ts, phone, status, message, stage, attempt FROM sign_events "
            "ORDER BY id")
        return [dict(r) for r in cur.fetchall()]

    def _run(self, *, attempt=None, claim_batch=None, row_owners="__unset__"):
        """跑一轮真 `runner.main`，返回 (rc, 日志正文)。

        `claim_batch` / `row_owners`：给值就打桩对应原语（`row_owners` 传 `None` 即
        模拟"判因读不通"的哨兵；不传 = 走真实现）。
        """
        cap = _LogCapture()
        logger = logging.getLogger("yiban")
        logger.addHandler(cap)
        self.addCleanup(logger.removeHandler, cap)
        # 级别必须显式压在 INFO：pytest 的 logging 插件把 root 级别设成 WARNING，而
        # `_setup_cli_logging` 的 `basicConfig(level=…)` 在 root 已有 handler（pytest 那个）
        # 时不再改级别——不压级就会漏掉汇总行这类 INFO 记录（本用例断的正是它）。
        _prev_level = logger.level
        logger.setLevel(logging.INFO)
        self.addCleanup(logger.setLevel, _prev_level)
        fc = _FakeClock()

        def _fake_attempt(acc):
            if attempt is None:
                raise AssertionError(f"本用例不该发起真实登录：{acc.phone}")
            return attempt(acc)

        patches = [
            # 运行锁不是本用例的题目（跨进程 flock 在 Windows 上不可用）；本用例断的是
            # 汇总口径与留痕，锁的探测/持有由 tests/test_run_lock_fail_closed.py 覆盖。
            mock.patch.object(runner.cli_support, "_acquire_run_lock",
                              lambda *a, **k: None),
            mock.patch.object(executor_v3, "_persist_loop", _never),
            mock.patch.object(executor_v3, "_make_limiter", lambda channels, shares=1: _Limiter()),
            mock.patch.object(executor_v3, "_make_gap_gate", lambda: _Gate()),
            mock.patch.object(executor_v3, "_make_global_limiter",
                              lambda: token_bucket.GlobalLimiter("")),
            mock.patch.object(executor_v3, "_now", fc.now),
            mock.patch.object(executor_v3, "_sleep", fc.sleep),
            mock.patch.object(executor_v3, "_mono", lambda: fc.mono),
            mock.patch.object(clock, "now", fc.now),
            mock.patch.object(executor_v3.attempts, "attempt_signin", _fake_attempt),
            mock.patch.object(runner.state_io, "_write_sched_done", lambda *a, **k: None),
        ]
        if claim_batch is not None:
            patches.append(mock.patch.object(queue_store, "claim_batch", claim_batch))
        if row_owners != "__unset__":
            patches.append(mock.patch.object(executor_v3.queue_store, "row_owners",
                                             lambda *a, **k: row_owners))
        for p in patches:
            p.start()
        try:
            rc = runner.main([])
        finally:
            for p in reversed(patches):
                with contextlib.suppress(Exception):
                    p.stop()
        return rc, cap.joined()


class PeerClaimedWorkerTest(_Harness):
    """判据 B/C 正文：任务被别人领走 ⇒ 独立桶 + 可查留痕，且不再计为失败。"""

    def test_peer_claimed_rows_are_an_own_bucket_not_failures(self):
        self._plan()
        self._peer_settles_all()
        rc, log = self._run()

        summary = [ln for ln in log.splitlines() if "==== 签到汇总" in ln]
        self.assertEqual(len(summary), 1, f"没找到汇总行：\n{log}")
        self.assertIn("❌ 0 失败", summary[0],
                      f"被别的执行体领走的账号仍被计成失败：{summary[0]}")
        self.assertIn("已由其他执行体领取", summary[0],
                      f"汇总没有独立桶（未执行与真失败没分列）：{summary[0]}")
        self.assertIn(str(len(self.phones)), summary[0],
                      "独立桶的条数必须等于被领走的账号数")
        self.assertNotEqual(rc, 1, "全被别人领走时不得报「有真失败」（那是假失败）")

    def test_peer_claimed_leaves_one_batch_event_not_per_account(self):
        """判据 B2：被别人领走 ⇒ **一条批量**事件（O(1)），不逐账号落库。

        故障现场（2026-10-10 生产）：兜底收尾扫描对每个被别人领走的账号各落一行
        `sign_events`（status=pending）×1830，当天 pending 事件 1893 条、日志 1934 行
        ——事件表与日志被按账号数线性灌噪声，看板凭空多出一个 pending 桶。
        本用例只断言"事件条数与账号数解耦"：6 个账号全部被别人领走，事件仍是 1 条。
        """
        self._plan()
        self._peer_settles_all()
        self._run()

        rows = self._events()
        self.assertEqual(len(rows), 1,
                         f"「已由他人负责」必须收成一条批量事件（O(1)），实得 {len(rows)} 行：{rows}")
        row = rows[0]
        self.assertEqual(row["stage"], "sign")
        self.assertNotEqual(row["status"], yiban_status.STATUS_FAILED,
                            "留痕不得写成真失败（两者必须可区分）")
        self.assertEqual(row["status"], yiban_status.STATUS_PENDING,
                         "未执行应当落 pending 档，而不是成功或失败")
        self.assertIn("已由其他执行体领取", row["message"],
                      f"批量事件无法区分「被别人领走」与真失败：{row}")
        self.assertIn(str(len(self.phones)), row["message"],
                      f"批量事件必须带计数（运维据此知道有多少个账号）：{row}")
        self.assertEqual(row["phone"], "",
                         f"批量事件是摘要行（无单一账号），phone 必须留空：{row}")


class TrulyUnreachedStillFailsTest(_Harness):
    """反向控制（红线）：没人接手的账号**仍算失败**——口径不许被改成"看起来对"。

    "仍算失败"分两层落地：① 汇总里它有自己的桶（`⏳ N 未执行（无人接手）`），**不并入
    `❌ N 失败`**——真失败是"跑了但没签上"，这两件事的处置动作不同（前者查队列/执行体，
    后者查账号与风控）；② 它照样让退出码落 1（`has_real_failure`），绝不因为"分列了"
    就变得无声。少了 ②，本用例就是"把统计改好看"本身。
    """

    def test_unreachable_queue_counts_as_failure_and_leaves_trace(self):
        """队列读不通：本执行体领不到自己的行，也没有别人接手 ⇒ 必须报失败。"""
        self._plan()
        rc, log = self._run(claim_batch=lambda *a, **k: None)
        summary = [ln for ln in log.splitlines() if "==== 签到汇总" in ln]
        self.assertEqual(len(summary), 1, f"没找到汇总行：\n{log}")
        self.assertIn(f"⏳ {len(self.phones)} 未执行", summary[0],
                      f"没人接手的账号没有独立桶：{summary[0]}")
        self.assertIn("❌ 0 失败", summary[0],
                      f"「未执行」不得与真失败混同：{summary[0]}")
        self.assertNotIn(f"✅ {len(self.phones)}", summary[0],
                         "没人接手的账号不得被算成成功（藏进成功即掩盖）")
        self.assertEqual(rc, 1, "存在无人接手的账号必须是真失败（退出码 1）")
        rows = self._events()
        self.assertEqual(len(rows), len(self.phones), "无人接手的账号同样要留痕")
        self.assertTrue(all(r["status"] == yiban_status.STATUS_PENDING for r in rows),
                        f"「未执行」的留痕状态不得写成 failed/success：{rows}")
        self.assertTrue(all("未执行" in r["message"] for r in rows),
                        f"留痕文本必须能读出「本执行体未执行」：{rows}")


class RetryRequeuedIsNotUnreachedTest(_Harness):
    """守卫 F2-①：本执行体**跑过**、回炉待重试的账号不进「未执行」桶，也不进 peer 桶。

    为什么这条必须独立存在：判因的 `retry` 档只看一个事实——`ctx.attempted`（轮内真的
    发过请求）。删掉那句登记（突变）后，同一个账号会回落队列判因（行 `pending`、归属本
    执行体）⇒ 被计入 `⏳ 未执行（无人接手）`，而它其实**跑过**、只是没成。没有本守卫，
    这类删除在批内全绿（复核 F2 实测）。
    """

    def test_attempted_but_requeued_counts_as_failure(self):
        def _retryable(_acc):
            return (False, "网络抖动", False, "failed")

        self._plan_all_due()
        real_claim = queue_store.claim_batch
        seen = {"n": 0}

        def _once_then_unreadable(*a, **k):
            # 第一拍照常领取（每个账号真的被跑一次并回炉成 `retry:` 档），其后模拟读不通：
            # 轮次以"读不通"收尾时窗口仍开着、回炉行留在 `pending`——这正是"跑过但没收尾"。
            seen["n"] += 1
            return real_claim(*a, **k) if seen["n"] == 1 else None

        rc, log = self._run(attempt=_retryable, claim_batch=_once_then_unreadable)
        summary = next(ln for ln in log.splitlines() if "==== 签到汇总" in ln)
        self.assertGreater(seen["n"], 1, "前置：补货循环应当反复尝试领取（本用例的收尾条件）")
        self.assertIn(f"❌ {self.ACCOUNTS} 失败", summary,
                      f"跑过但回炉的账号应当计真失败：{summary}")
        self.assertNotIn("未执行", summary, f"跑过的账号被报成「未执行」：{summary}")
        self.assertNotIn("已由其他执行体领取", summary, f"跑过的账号被报成 peer：{summary}")
        self.assertEqual(rc, 1)
        rows = self._events()
        # 一次尝试落两行（尝试结论 + 回炉迁移），故 2×账号数；关键是**不许多写「未执行」行**
        # ——判因一旦回落队列，本用例前四条断言先红，这条再钉住留痕侧。
        self.assertEqual(len(rows), 2 * self.ACCOUNTS, f"每次尝试的留痕行数不对：{rows}")
        self.assertEqual({r["status"] for r in rows},
                         {yiban_status.STATUS_FAILED, yiban_status.STATUS_RETRYING},
                         f"回炉档的留痕只应有失败与重试两态：{rows}")
        self.assertTrue(all("未执行" not in r["message"] for r in rows),
                        f"跑过的账号不得被补写「未执行」留痕：{rows}")


class SentinelDirectionGuardTest(_Harness):
    """守卫 F2-②：判因**读不通**时必须按「无人接手」计，不得折成「别人领走了」。

    哨兵方向只有消费方这一步会做错：`row_owners` 回 `None` 时把账号说成"已被别人领取"
    等于把故障掩盖成正常（且 `peer` 桶不计真失败 ⇒ 退出码从 1 落到 2，账号当天从此无声）。

    夹具同时打桩两处：`claim_batch` 回哨兵（领不到活）与 `row_owners` 回哨兵（判因不可得）
    ——只打桩后者时执行体会先真领到活并跑一遍，判因根本轮不到（那样本用例断的是另一条路）。
    突变验证：把 `_mark_unreached` 里 `rows is None` 的方向翻成 `UNREACHED_PEER` ⇒ 本用例红。
    """

    def test_unreadable_judgement_is_unclaimed_not_peer(self):
        self._plan()
        self.assertEqual(len(self._day_rows()), len(self.phones),
                         "前置：计划行已在库（表读得通）")
        rc, log = self._run(claim_batch=lambda *a, **k: None, row_owners=None)
        summary = next(ln for ln in log.splitlines() if "==== 签到汇总" in ln)
        self.assertIn(f"⏳ {len(self.phones)} 未执行", summary,
                      f"判因读不通没有落「无人接手」桶：{summary}")
        self.assertNotIn("已由其他执行体领取", summary,
                         f"读不通被说成「别人做了」——那是掩盖：{summary}")
        self.assertEqual(rc, 1, "读不通不得把账号从真失败里划走")
        rows = self._events()
        self.assertTrue(rows, "读不通同样要留痕")
        self.assertTrue(all("判因不可得" in r["message"] for r in rows),
                        f"留痕必须写明判因不可得：{rows}")
        self.assertTrue(all(r["status"] == yiban_status.STATUS_PENDING for r in rows),
                        f"未执行的留痕不得写成成功或失败：{rows}")

    def _day_rows(self):
        """当日任务行（只读配对；判因守卫的前置断言用）。"""
        return db.get_conn().execute(
            "SELECT phone FROM sign_tasks WHERE day=?", (DAY,)).fetchall()


class CaliberConsistencyTest(_Harness):
    """同源同口径：汇总计数、批量事件计数、账号数三者出自同一份判因。"""

    def test_summary_bucket_count_equals_batch_event_count(self):
        self._plan()
        self._peer_settles_all()
        _rc, log = self._run()
        summary = next(ln for ln in log.splitlines() if "==== 签到汇总" in ln)
        events = self._events()
        import re
        # 「已由他人负责」收成一条批量（O(1)）：汇总的独立桶数字、批量事件里的计数、
        # 账号数三者必须同源。改一处不改另一处即红。
        self.assertEqual(len(events), 1, f"应恰有一条批量事件：{events}")
        m_sum = re.search(r"(\d+) 已由其他执行体领取", summary)
        self.assertIsNotNone(m_sum, f"汇总缺独立桶：{summary}")
        m_ev = re.search(r"(\d+) 个账号", events[0]["message"])
        self.assertIsNotNone(m_ev, f"批量事件缺计数：{events[0]}")
        self.assertEqual(int(m_sum.group(1)), int(m_ev.group(1)),
                         "汇总计数与批量事件计数不一致（两套口径）")
        self.assertEqual(int(m_sum.group(1)), len(self.phones),
                         "计数必须等于被领走的账号数")


class QueueOwnershipReadTest(_Harness):
    """判因的事实源（`queue_store` 读侧）：读不通必须回哨兵，不得折成"没有"。"""

    def test_row_owners_reads_state_and_owner(self):
        self._plan()
        got = queue_store.row_owners(DAY, self.phones)
        self.assertEqual(set(got), set(self.phones))
        self.assertTrue(all(v[0] == "pending" for v in got.values()),
                        f"新计划行应当是 pending：{got}")
        self._peer_settles_all()
        got = queue_store.row_owners(DAY, self.phones)
        self.assertTrue(all(v == ("done", PEER) for v in got.values()),
                        f"了结后应当读到 done + 持有者：{got}")

    def test_row_owners_returns_none_when_table_missing(self):
        """表未落地（库被换掉）⇒ 哨兵 `None`，绝不回空字典。"""
        self._plan()
        db.get_conn().execute("DROP TABLE sign_tasks")
        db.get_conn().commit()
        self.assertIsNone(queue_store.row_owners(DAY, self.phones),
                          "读不通被折成了「什么都没有」——判因会静默把别人的活当我自己的失败")


class PeerBatchDedupTest(unittest.TestCase):
    """LOW-1 守卫（工单 81xt 返修）：同一组成的批量留痕**只落一次**。

    兜底常驻循环每 ~5s 一拍、窗口内可上百轮。同一批被别人领走的账号在每轮组成不变
    （账号数与分因明细都不变），旧实现每轮各落一条日志 + 一条摘要事件——按轮数线性
    灌噪声。修法是进程内模块级去重键，按 `(执行体, 账号数, 分因明细)` 变化才落；签名
    不变则整段跳过。组成一变（换执行体、账号数变、或同数下分因明细变）立刻回到留痕。

    直接调真 `_mark_unreached`：本用例的题目是留痕的**去重**，不引入库与网络，
    队列归属用打桩事实喂入。
    """

    TAG = "[fallback]"
    LOG_MARK = "未领取：任务已由其他执行体领取"

    def setUp(self):
        # 去重键是模块级的（同进程跨轮共享）：用例要断言"第一拍落"，就得先清键，
        # 否则断言取决于执行顺序（同 `_BANNER_LAST` 的做法）。
        self._saved = getattr(executor_v3, "_UNREACHED_PEER_LAST", None)
        executor_v3._UNREACHED_PEER_LAST = None
        self.addCleanup(setattr, executor_v3, "_UNREACHED_PEER_LAST", self._saved)

    def _scan(self, phones, owners, executor_id=WORKER0, attempted=()):
        """跑一拍 `_mark_unreached`，返回 (摘要事件列表, 本拍批量日志文本)。"""
        events = []
        ctx = SimpleNamespace(
            attempted=set(attempted), results={}, delegated=(), day=DAY,
            executor_id=executor_id, runtime_id=executor_id,
            log_tag=self.TAG, unreached={}, event_sink=events.append)
        cap = _LogCapture()
        logger = logging.getLogger("yiban")
        logger.addHandler(cap)
        self.addCleanup(logger.removeHandler, cap)
        _prev = logger.level
        logger.setLevel(logging.INFO)
        self.addCleanup(logger.setLevel, _prev)
        with mock.patch.object(executor_v3.queue_store, "row_owners",
                               lambda *a, **k: owners):
            executor_v3._mark_unreached(
                ctx, [SimpleNamespace(phone=p) for p in phones])
        return events, [t for t in cap.texts if self.LOG_MARK in t]

    @staticmethod
    def _owners(phones, state="done"):
        return {p: (state, PEER) for p in phones}

    def test_repeated_same_composition_emits_one_log_and_one_event(self):
        """同一组成连扫两拍 ⇒ 只落 1 条日志 + 1 条事件（LOW-1 的刷屏）。"""
        phones = [_phone(i) for i in range(1, 4)]
        owners = self._owners(phones)

        ev1, log1 = self._scan(phones, owners)
        self.assertEqual(len(ev1), 1, f"第一拍应落一条摘要事件：{ev1}")
        self.assertEqual(len(log1), 1, f"第一拍应落一条批量日志：{log1}")

        ev2, log2 = self._scan(phones, owners)
        self.assertEqual(len(ev2), 0,
                         f"同一组成再扫一拍不得重复落事件（按轮刷屏）：{ev2}")
        self.assertEqual(len(log2), 0, "同一组成再扫一拍不得重复落日志")

    def test_composition_change_emits_again(self):
        """组成变（账号数变 / 同数下分因明细变）⇒ 必须再落一条，不许吞掉真变化。"""
        first = [_phone(i) for i in range(1, 4)]
        ev1, log1 = self._scan(first, self._owners(first))
        self.assertEqual((len(ev1), len(log1)), (1, 1), "第一拍应落一条")

        more = [*first, _phone(4)]
        ev2, log2 = self._scan(more, self._owners(more))
        self.assertEqual((len(ev2), len(log2)), (1, 1),
                         "账号数由 3 变 4 必须再落一条")
        self.assertIn("共 4 个账号", ev2[0]["message"])

        ev3, log3 = self._scan(more, self._owners(more, state="canceled"))
        self.assertEqual((len(ev3), len(log3)), (1, 1),
                         "同数下分因明细变化（done→canceled）必须再落一条")

    def test_identity_change_emits_per_executor(self):
        """同进程换执行体、组成相同 ⇒ 两个执行体各落一条（身份在去重键里）。

        去重键不含身份时，第二个执行体会被判成"没变"而漏掉它唯一的一条批量——
        与 `_log_banner` 的 `_BANNER_LAST` 同一形状（同文件姊妹做法）：同进程换执行体
        必须各自留痕，不许互相压掉。
        """
        phones = [_phone(i) for i in range(1, 4)]
        owners = self._owners(phones)

        ev1, log1 = self._scan(phones, owners, executor_id=WORKER0)
        self.assertEqual((len(ev1), len(log1)), (1, 1), "第一个执行体应落一条")

        other = egress.worker_owner(1)
        self.assertNotEqual(other, WORKER0, "前置：两个执行体身份不同")
        ev2, log2 = self._scan(phones, owners, executor_id=other)
        self.assertEqual((len(ev2), len(log2)), (1, 1),
                         "换执行体后同一组成必须再落一条（身份被判成没变）")

    def test_zero_composition_resets_so_reappearance_emits(self):
        """组成 N→0→N：中间的 0 拍把键清空，N 再现必须再落一条。"""
        phones = [_phone(i) for i in range(1, 4)]
        owners = self._owners(phones)

        ev1, log1 = self._scan(phones, owners)
        self.assertEqual((len(ev1), len(log1)), (1, 1), "第一拍应落一条")

        # 0 拍：全部账号本执行体跑过（retry 档）⇒ 无 peer 批量，键被清回 None
        ev0, log0 = self._scan(phones, {}, attempted=set(phones))
        self.assertEqual((len(ev0), len(log0)), (0, 0), "peer 为 0 时不得落批量")

        ev2, log2 = self._scan(phones, owners)
        self.assertEqual((len(ev2), len(log2)), (1, 1),
                         "组成再现必须再落一条（同组成 N→0→N 不得吞第三次留痕）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
