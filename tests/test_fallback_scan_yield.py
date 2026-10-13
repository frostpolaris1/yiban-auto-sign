# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""兜底收尾扫描的**事件产出规模**：一条批量，不按账号线性灌；无信息事件不占账号终态。

标签：B · 调度：领取/队列/执行体
覆盖：`yiban/engine/executor_v3.py` 的收尾两处出口
   —— `_prescan`（用户自暂停账号的登记）与 `_mark_unreached`（轮末"本执行体未执行"判因）。
对应实现：`yiban/engine/executor_v3.py`（`_prescan` / `_mark_unreached`）、
   `yiban/engine/workers.py`（兜底常驻把 `run_executor_v3` 反复调用，故收尾产出按**轮**累积）、
   `yiban/store/events.py`（摘要行 `phone=''` 不进账号聚合）。

**故障现场（2026-10-13 生产，工单 81xt ＋ uuqb）**：`run.sh` 07:28:35 收工后，常驻兜底
每约 5.2 秒扫一遍，直到窗口关闭（07:44:57）共约 188 轮。每轮：
- `_prescan` 把全部用户自暂停账号**重新登记一遍** ⇒ `user_cancelled` 事件与 `pause` 进度行
  各 ×4/轮（当日 752 ＋ 752）；
- `_mark_unreached` 对每账号逐条落 `pending` 事件与日志 ⇒ 当日 `pending` 49,148 行。
看板的「今日成功/失败」两卡与热力图读**每账号当日最后一条事件**的终态：收尾扫描把每个
账号的最后一条都刷成了 `pending` ⇒ 两卡显示 0/0、热力图染成一色。

**本文件钉住三条不变式**（断言钉"任何时刻都不该被违反的性质"，不钉调用顺序）：
1. 收尾扫描的 `sign_events` 产出**条数不随账号数线性增长**（同规模下 O(1)）；
2. 同一账号在同一/连续扫描里**不得被重复落多条同义事件**（会话内每账号每义至多一行）；
3. 收尾扫描的**无信息判因事件不得成为该账号当日最后一条事件**（看板终态不被它改写）。

**依赖**：临时 SQLite（真迁移链）＋真 `run_executor_v3`（只替身登录/限速/gap/时钟）＋
   假补货（一次性哨兵，时序完全确定）。不发任何网络请求。
用法（项目根目录）：
    bash scripts/dev-verify.sh --target tests/test_fallback_scan_yield.py
"""
import contextlib
import datetime
import json
import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from yiban import clock, egress
from yiban import status as yiban_status
from yiban.engine import executor_v3, hrw, planner, schedule
from yiban.store import clock_meta, db
from yiban.store import connection as store_conn

TEST_KEY = "a" * 64
DAY = "2026-09-22"          # 周二：周末门不拦
START = datetime.datetime(2026, 9, 22, 6, 40, 0)
OWNER = egress.worker_owner(0)
#: 另一个执行体的**运行时**身份（`claim_batch` 写进 `owner` 的形态，含进程号）
PEER = egress.runtime_owner(egress.fallback_owner())

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


def _sentinel_only(items):
    """假补货：只把给定条目与 M 条哨兵一次性投进队列（时序确定，不真实轮询）。"""
    async def _refill(queue, shards, ctx):
        for it in items:
            queue.put_nowait(it)
        for _ in range(ctx.m):
            queue.put_nowait((executor_v3.SENTINEL_PRIORITY, "", "", 0, 0))
    return _refill


class _Harness(unittest.TestCase):
    """真库 + 真 `run_executor_v3`（反复调用即兜底常驻的扫描形态）。"""

    def setUp(self):
        self._close_conn()
        # 批量留痕去重键是**模块级**的（跨轮共享正是降噪要的语义）：用例间必须清掉，
        # 否则同组成的后一个用例会因上一批留下的键而漏落事件（同 `_BANNER_LAST` 的处置）。
        self._saved = {}
        for name in ("_UNREACHED_PEER_LAST", "_UNREACHED_UNCLAIMED_LAST"):
            self._saved[name] = getattr(executor_v3, name, None)
            if hasattr(executor_v3, name):
                setattr(executor_v3, name, None)
        self.addCleanup(self._restore_globals)
        self.root = tempfile.mkdtemp(prefix="yiban-scan-yield-")
        self.db_file = os.path.join(self.root, "yiban.db")
        self.state_dir = os.path.join(self.root, "state")
        os.makedirs(self.state_dir)
        self.env_file = os.path.join(self.root, ".env")
        with open(self.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n")
        self._env = {k: os.environ.get(k) for k in (
            "YIBAN_DB_FILE", "YIBAN_ENV_FILE", "YIBAN_STATE_DIR", "YIBAN_ACCOUNTS_KEY",
            "YIBAN_EXECUTOR_ID", "YIBAN_EXECUTORS", "YIBAN_LOG_FILE",
            "YIBAN_GLOBAL_PAUSE", "YIBAN_PROXY")}
        os.environ.update({
            "YIBAN_DB_FILE": self.db_file,
            "YIBAN_ENV_FILE": self.env_file,
            "YIBAN_STATE_DIR": self.state_dir,
            "YIBAN_ACCOUNTS_KEY": TEST_KEY,
            "YIBAN_LOG_FILE": os.path.join(self.root, "sign.log"),
            # 本进程是**子执行体**身份：`shards_of` 的名字必须与 `cfg["executors"]` 逐字一致
            "YIBAN_EXECUTOR_ID": OWNER,
        })
        for k in ("YIBAN_GLOBAL_PAUSE", "YIBAN_PROXY"):
            os.environ.pop(k, None)
        env_patch = mock.patch.dict(os.environ, dict(BASE_ENV), clear=False)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        self.addCleanup(self._teardown)
        self.fc = _FakeClock()

    def _restore_globals(self):
        for name, val in self._saved.items():
            if hasattr(executor_v3, name):
                setattr(executor_v3, name, val)

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
    def _seed(self, n_orphan=0, n_paused=0, peer_owned=False):
        """建当日计划行，再把非暂停行改成收尾形态。

        - `n_orphan`：行已 `done` 但**无归属执行体**（"没人接手"的真异常形状）；
        - `peer_owned=True`：`done` 行归属于**别的执行体**（跨执行体交接的正常形状）；
        - `n_paused`：用户自暂停账号（计划里本就没有它的行，`_prescan` 登记）。
        """
        self._close_conn()
        db.init_db(self.db_file, env_file=self.env_file, cleanup=False)
        self.assertEqual(store_conn.current_db_file(), self.db_file,
                         "前置：单例连接必须指向本用例的临时库")
        phones = [_phone(i) for i in range(1, n_orphan + 1)]
        paused = [_phone(100 + i) for i in range(1, n_paused + 1)]
        accounts = ([SimpleNamespace(phone=p, user_paused=False, owner="admin")
                     for p in phones]
                    + [SimpleNamespace(phone=p, user_paused=True, owner="admin")
                       for p in paused])
        cfg = schedule.planner_config()
        v = hrw.v_for(len(accounts) or 1)
        planner.write_plan(
            planner.build_plan(accounts, DAY, cfg["executors"], v=v, cfg=cfg), DAY)
        clock_meta.set_meta(executor_v3.V_META_KEY_PREFIX + DAY, v)
        if phones:
            conn = db.get_conn()
            conn.execute(
                "UPDATE sign_tasks SET state='done', owner=?, result='签到成功' WHERE day=?",
                (PEER if peer_owned else "", DAY))
            conn.commit()
        return accounts, cfg

    def _scan(self, accounts, cfg, *, passes=1, extra_events=(), unreadable=False,
              unreached_log=None):
        """跑 `passes` 轮 `run_executor_v3`（兜底常驻的扫描形态），返回逐轮 results。

        事件按执行体口径攒批落库（与 `workers.run_fallback_worker` 同一条写入路径）。
        `unreadable=True`：把判因事实源 `row_owners` 打成哨兵（`None` = 队列读不通），
        此时判因不可得、**全部**账号都落进"无人接手"档——那是唯一的 O(账号数) 现场。
        `unreached_log`：给了就逐轮把执行体填的 `{phone: 判因}` 追加进去（判因断言用）。
        """
        def _no_login(acc):
            raise AssertionError(f"本用例不该发起真实登录：{acc.phone}")

        patches = [
            mock.patch.object(executor_v3, "_persist_loop", _never),
            mock.patch.object(executor_v3, "_refiller", _sentinel_only([])),
            mock.patch.object(executor_v3, "_make_limiter", lambda channels, shares=1: _Limiter()),
            mock.patch.object(executor_v3, "_make_gap_gate", lambda: _Gate()),
            mock.patch.object(executor_v3, "_now", self.fc.now),
            mock.patch.object(executor_v3, "_sleep", self.fc.sleep),
            mock.patch.object(executor_v3, "_mono", lambda: self.fc.mono),
            mock.patch.object(clock, "now", self.fc.now),
            mock.patch.object(executor_v3.attempts, "attempt_signin", _no_login),
        ]
        if unreadable:
            patches.append(mock.patch.object(executor_v3.queue_store, "row_owners",
                                             lambda *a, **k: None))

        for ev in extra_events:
            db.add_sign_event(**ev)
        rounds = []
        for p in patches:
            p.start()
        try:
            for i in range(passes):
                rows = []
                unreached = {}
                results = executor_v3.run_executor_v3(
                    accounts, day=DAY, cfg=cfg, delegated=set(),
                    claim_all=True, event_sink=rows.append, unreached=unreached,
                    # 兜底常驻逐轮传轮次（`workers.run_fallback_worker` 同口径）：
                    # 它是"常驻多轮"的唯一标记，收尾的逐账号重扫据此幂等。
                    round_no=i + 1)
                if rows:
                    db.add_sign_events_batch(rows)
                if unreached_log is not None:
                    unreached_log.append(dict(unreached))
                rounds.append(results)
        finally:
            for p in reversed(patches):
                with contextlib.suppress(Exception):
                    p.stop()
        return rounds

    # ---- 读侧 ----
    def _fresh_db(self):
        """丢掉本用例的库（含 -wal/-shm 伴生件），让下一次 `_seed` 从零建。"""
        self._close_conn()
        for suffix in ("", "-wal", "-shm"):
            p = self.db_file + suffix
            if os.path.exists(p):
                os.remove(p)

    def _events(self):
        cur = db.get_conn().execute(
            "SELECT id, ts, phone, status, message FROM sign_events ORDER BY id")
        return [dict(r) for r in cur.fetchall()]

    def _run_events(self, node=None):
        sql = "SELECT id, node, executor, phone, message FROM run_events"
        params = []
        if node is not None:
            sql += " WHERE node=?"
            params.append(node)
        sql += " ORDER BY id"
        return [dict(r) for r in db.get_conn().execute(sql, params).fetchall()]

    def _last_event_per_phone(self):
        rows = self._events()
        last = {}
        for r in rows:
            if r["phone"]:
                last[r["phone"]] = r
        return last


class ScanScaleTest(_Harness):
    """不变式 1：收尾扫描的事件条数与账号数**解耦**（同规模下 O(1)）。"""

    def test_unreached_and_paused_do_not_scale_with_accounts(self):
        def _count(n_orphan):
            os.environ["YIBAN_STATE_DIR"] = tempfile.mkdtemp(dir=self.root)
            accounts, cfg = self._seed(n_orphan=n_orphan, n_paused=1)
            self._scan(accounts, cfg, passes=3)
            return len(self._events())

        small = _count(3)
        self._fresh_db()
        large = _count(30)
        self.assertLessEqual(
            large, small,
            f"收尾扫描的事件条数随账号数增长（{small} → {large}）：应按账号数解耦")

    def test_unreadable_queue_does_not_scale_with_accounts(self):
        """队列读不通时**全部**账号落进"无人接手"——批量后条数仍与账号数解耦。

        这是唯一的 O(账号数) 现场：判因事实源读不通时没有一条"别人负责"的行可分辨，
        逐账号落库会把整站账号各灌一行（正是 2026-10-13 生产 49,148 行的形状）。
        """
        def _count(n):
            os.environ["YIBAN_STATE_DIR"] = tempfile.mkdtemp(dir=self.root)
            accounts, cfg = self._seed(n_orphan=n)
            self._scan(accounts, cfg, passes=2, unreadable=True)
            return len(self._events())

        small = _count(4)
        self._fresh_db()
        large = _count(40)
        self.assertLessEqual(
            large, small,
            f"队列读不通时事件条数随账号数增长（{small} → {large}）：应按账号数解耦")


class NoRepeatedSameMeaningTest(_Harness):
    """不变式 2：同一账号不得被扫描重复落多条同义事件（会话内每账号每义至多一行）。"""


    def test_each_account_gets_at_most_one_scan_event(self):
        accounts, cfg = self._seed(n_orphan=4, n_paused=2)
        self._scan(accounts, cfg, passes=4)
        seen = {}
        for r in self._events():
            if not r["phone"]:
                continue
            seen.setdefault((r["phone"], r["status"], r["message"]), []).append(r["id"])
        dup = {k: v for k, v in seen.items() if len(v) > 1}
        self.assertEqual(dup, {},
                         f"同一账号被重复落同义事件（兜底每轮重扫即 ×轮数）：{dup}")


class NoInfoEventNeverLastTest(_Harness):
    """不变式 3：收尾扫描的无信息判因事件不得成为账号当日最后一条事件（看板终态）。"""

    def test_scan_does_not_overwrite_account_terminal(self):
        accounts, cfg = self._seed(n_orphan=4)
        # 先写各账号的真实终态（模拟执行体已签成）——扫描的判因行绝不许盖在它后面
        for acc in accounts:
            db.add_sign_event(ts=START.strftime("%Y-%m-%d %H:%M:%S"), phone=acc.phone,
                              status=yiban_status.STATUS_SUCCESS, message="签到成功")
        self._scan(accounts, cfg, passes=3)
        last = self._last_event_per_phone()
        for acc in accounts:
            self.assertEqual(
                last[acc.phone]["status"], yiban_status.STATUS_SUCCESS,
                f"收尾扫描的无信息事件成了 {acc.phone} 当日最后一条事件：{last[acc.phone]}")
        # 无信息判因事件若是按账号行，必然抢到 MAX(id)——这条断言是上一条的因
        per_account_scan_rows = [
            r for r in self._events()
            if r["status"] == yiban_status.STATUS_PENDING and r["phone"]
        ]
        self.assertEqual(per_account_scan_rows, [],
                         f"收尾扫描仍按账号落 pending 行：{per_account_scan_rows}")


class FallbackRoundYieldTest(_Harness):
    """uuqb：暂停账号只登记一次；后续轮无活可报，也不重复落 pause/事件。"""

    def test_paused_registered_once_and_repeat_rounds_report_no_work(self):
        accounts, cfg = self._seed(n_orphan=0, n_paused=3)
        rounds = self._scan(accounts, cfg, passes=3)
        self.assertEqual(len(rounds[0]), 3, f"首轮必须登记三个暂停号：{rounds[0]}")
        self.assertEqual(rounds[1], {}, f"第二轮仍有活 ⇒ 兜底会进 5s 紧循环：{rounds[1]}")
        self.assertEqual(rounds[2], {}, f"第三轮仍有活：{rounds[2]}")

        # 暂停账号的事件与进度行各**一条**（不是每轮一条）
        user_cancelled = [r for r in self._events()
                          if r["status"] == yiban_status.STATUS_USER_CANCELLED]
        self.assertEqual(len(user_cancelled), 3,
                         f"暂停账号被每轮重复登记：{user_cancelled}")
        pause = [r for r in self._run_events(node="pause") if r["phone"]]
        self.assertEqual(len(pause), 3, f"暂停账号的 pause 进度行按轮重复：{pause}")

        # finalize 的「本轮完成 N 个账号」只统计真实处理数：首轮 3，重复轮 0
        finals = [r["message"] for r in self._run_events(node="finalize")]
        self.assertTrue(finals, "收尾进度行缺失")
        self.assertIn("本轮完成 3 个账号", finals[0], f"首轮计数不对：{finals[0]}")
        self.assertIn("本轮完成 0 个账号", finals[-1],
                      f"重复排除被计入 finalize 计数：{finals}")

    def test_paused_account_is_not_judged_unreached_on_repeat_rounds(self):
        """D4 守卫：`_mark_unreached` 必须排除用户自暂停账号（`ctx.user_paused`）。

        不排除会怎样：常驻重复轮（`round_no` 有值）里暂停账号**不在** `results`
        （本轮无活，按设计不登记），判因便回落队列；而计划层本就剔除了它 ⇒
        `sign_tasks` 无当日行 ⇒ 判因落 `UNCLAIMED`（"队列中没有当日行"）⇒ 多一条
        批量事件，账号也进了 `unreached` ⇒ 兜底汇总的 `stay_n`（"未执行（无人接手）"）
        虚增。独立审查只读码、未做扰动，此处补钉这条承重排除。
        """
        accounts, cfg = self._seed(n_orphan=0, n_paused=2)
        unreached_rounds = []
        self._scan(accounts, cfg, passes=2, unreached_log=unreached_rounds)
        self.assertEqual(len(unreached_rounds), 2, "前置：两轮判因都要采到")
        for i, un in enumerate(unreached_rounds, start=1):
            self.assertEqual(un, {},
                             f"第 {i} 轮把用户自暂停账号判成了未执行：{un}")
        unclaimed = [r for r in self._events()
                     if "无人接手" in (r.get("message") or "")]
        self.assertEqual(unclaimed, [], f"暂停账号被补写「无人接手」批量：{unclaimed}")


if __name__ == "__main__":
    unittest.main()
