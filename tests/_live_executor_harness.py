# -*- coding: utf-8 -*-
"""活执行体靶场：真临时 sqlite ＋ 真 `executor_v3` ＋ 真 `queue_store`。

**为什么是共享件而不是各用例自带一份**：`tests/test_claim_write_path_single_source.py`
与 `tests/test_fallback_elbow_discrimination_e2e.py` 要验的是**同一条**执行体接线
（三条写入口的角色参数、真库、真领取原语），只是判据不同。各写一份会让"角色参数"
出现两个定义点——本仓已实测过"同一判据多定义、只改一处"这一形状（AGENTS.md §十三.27）。

**本件不定义判据**：只提供靶场与观察面（语句流、被执行的账号）。判据住在各用例里。

**替身口径**（AGENTS.md §十三.32 要求明说替身保住了哪一维）：
  · 保住：领取写路径、队列状态机、执行体的通道与收尾——真库真原语，未打桩。
  · 未保：上游 HTTP。`attempts.attempt_signin` 打成"恒成功"，故本靶场测**不是**协议
    保真，只测领取/让位/合流这些**协调**性质。
  · 未保：限速。三件限速器打成恒放行（真实 GCRA 只会拖时间，与判据无关）。
  · 时钟：`_now`/`_sleep`/`_mono`/`clock.now` 打成一份假时钟，故用例不真实 sleep；
    假时钟从 `START`（周二 06:40，窗口内）起步。
"""
import asyncio
import contextlib
import datetime
import os
import random
import tempfile
from types import SimpleNamespace
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import db  # noqa: E402

from yiban import clock  # noqa: E402
from yiban.engine import executor_v3  # noqa: E402

TEST_KEY = "a" * 64
DAY = "2026-09-22"          # 周二，避开周末门
START = datetime.datetime(2026, 9, 22, 6, 40, 0)
OWNER = "single@testhost"
PHONE = "13800000001"
PHONE2 = "13800000002"
OTHER_OWNER = "other@host:1:a"

#: 三条写入口的角色参数（真调用点逐字对齐）：
#: · 调度器：`yiban/engine/runner.py` 的定时全量分支（`requeue_final=_second_run`）；
#: · 网页手动：`runner` 的 `--only` 分支（`reclaim=True, requeue_final=True`）；
#: · 兜底常驻：`yiban/engine/workers.py` 的 `run_fallback` 循环体（`claim_all` ＋
#:   `requeue_during_run` ＋ `elbow_sec=FALLBACK_ELBOW_SEC`）。
ENTRIES = {
    "scheduler": dict(requeue_final=False),
    "manual": dict(reclaim=True, requeue_final=True),
    "fallback": dict(claim_all=True, requeue_during_run=True,
                     elbow_sec=executor_v3.FALLBACK_ELBOW_SEC),
}

#: 每条入口的种子行状态与 run_at 偏移（秒，相对假时钟）。手动入口靠 `reclaim` 把
#: 已了结的行翻回 pending，故它种 `done`；兜底只领"已过期超过肘界"的行，故它种更早。
ENTRY_SEED = {
    "scheduler": ("pending", -1),
    "manual": ("done", -1),
    "fallback": ("pending", -(executor_v3.FALLBACK_ELBOW_SEC + 60)),
}


def close_conn():
    """关掉进程级单例连接：用例之间必须换一份新库。"""
    if db._conn is not None:
        with contextlib.suppress(Exception):
            db._conn.close()
        db._conn = None


def planner_cfg(executors=None):
    """`schedule.planner_config()` 的同形快照（用例不读真实 .env）。"""
    return {
        "order": "sequence", "dist": "uniform",
        "edge_front_sec": 60, "edge_back_sec": 60,
        "block_cap": 15, "mu_min_pct": 40, "mu_max_pct": 60,
        "sigma_min_pct": 15, "sigma_max_pct": 25, "min_exec_gap": 5,
        "avg_attempt_sec": 3, "retry_min_interval": 60, "exec_gap_min": 10,
        "allow_time_pref": 0, "sign_start": (6, 30), "sign_end": (7, 50),
        "bucket_rate": 1.0, "executors": list(executors or [OWNER]),
    }


class FakeClock:
    """假时钟：`_now` / `_sleep` / `_mono` 共用一份推进（用例不真实 sleep）。"""

    def __init__(self, t=START):
        self.t = t
        self.mono = 10_000.0        # 起点刻意非零：从 0 起会掩盖"把墙钟当单调钟用"

    def now(self):
        return self.t

    async def sleep(self, sec):
        self.t += datetime.timedelta(seconds=sec)
        self.mono += sec
        await asyncio.sleep(0)

    def stamp(self, offset_sec=0):
        return (self.t + datetime.timedelta(seconds=offset_sec)).strftime(
            "%Y-%m-%d %H:%M:%S.%f")[:-3]


class PermissiveLimiter:
    """不设限的出口限速器替身：恒放行。"""

    manual = False
    invalid = False

    def bucket(self, egress):
        return SimpleNamespace(retry_after=lambda now: 0.0)

    def acquire(self, egress, now):
        return True

    def on_success(self, egress):
        return None

    def on_risk_signal(self, egress, now):
        return None

    def persist(self, egress, stamp=None):
        return True

    def restore_from_store(self, egress, now=None):
        return False


class PermissiveGlobalLimiter:
    """全局上界替身：`acquire(now)` 恒放行（接口与出口桶不同，见 `_throttle`）。"""

    invalid = False
    lam = 1.0

    def acquire(self, now):
        return True


class PermissiveGate:
    """每账号 gap 门替身：恒放行。"""

    gap_sec = 10.0

    def allow(self, phone, now):
        return True

    def commit(self, phone, now):
        return None


async def never_loop(_ctx):
    """桶状态落库循环的替身：它的 10s 周期不该干扰假时钟。"""
    await asyncio.Event().wait()


class Harness:
    """一次真跑一轮的最小靶场。

    `statements` 收的是 **SQL 边界**上的语句（`sqlite3` 的 `set_trace_callback`），
    参数已由 sqlite 展开——故它按"发出的语句"而不是按任何 Python 名字观察行为。
    """

    def __init__(self, tmp, executor_id=OWNER):
        self.tmp = tmp
        self.executor_id = executor_id
        self.db_file = os.path.join(tmp, "yiban.db")
        self.env_file = os.path.join(tmp, ".env")
        self.state_dir = tempfile.mkdtemp(dir=tmp, prefix="state-")
        with open(self.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n")
        os.environ.update({
            "YIBAN_ACCOUNTS_KEY": TEST_KEY,
            "YIBAN_ENV_FILE": self.env_file,
            "YIBAN_DB_FILE": self.db_file,
            "YIBAN_STATE_DIR": self.state_dir,
            "YIBAN_EXECUTOR_ID": executor_id,
        })
        for k in ("YIBAN_PROXY", "YIBAN_PROXY_LIST", "YIBAN_PROXY_FALLBACK",
                  "YIBAN_EXECUTORS", "YIBAN_FALLBACK_ENABLE"):
            os.environ.pop(k, None)
        self.statements = []
        self.attempted = []          # 被执行的手机号，按发生顺序
        self.attempt_log = []        # [(手机号, 假墙钟时刻), ...]——判"什么时候才领的"
        self.fc = FakeClock()
        self._patches = []

    def __enter__(self):
        close_conn()
        for suffix in ("", "-wal", "-shm"):      # 每次真跑一轮都要一份全新的库
            path = self.db_file + suffix
            if os.path.exists(path):
                os.remove(path)
        db.init_db(self.db_file, env_file=self.env_file, cleanup=False)
        db.get_conn().set_trace_callback(self.statements.append)

        def _stub_attempt(acc):
            self.attempted.append(acc.phone)
            self.attempt_log.append((acc.phone, self.fc.t))
            return (True, "ok", False, "success")

        self._patches = [
            mock.patch.object(executor_v3, "_now", self.fc.now),
            mock.patch.object(executor_v3, "_sleep", self.fc.sleep),
            mock.patch.object(executor_v3, "_mono", lambda: self.fc.mono),
            mock.patch.object(clock, "now", self.fc.now),
            mock.patch.object(executor_v3.attempts, "attempt_signin", _stub_attempt),
            mock.patch.object(executor_v3, "_persist_loop", never_loop),
            mock.patch.object(executor_v3, "_make_limiter",
                              lambda channels, shares=1: PermissiveLimiter()),
            mock.patch.object(executor_v3, "_make_global_limiter",
                              lambda: PermissiveGlobalLimiter()),
            mock.patch.object(executor_v3, "_make_gap_gate", lambda: PermissiveGate()),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            with contextlib.suppress(Exception):
                p.stop()
        with contextlib.suppress(Exception):
            db.get_conn().set_trace_callback(None)
        close_conn()
        for k in ("YIBAN_ACCOUNTS_KEY", "YIBAN_ENV_FILE", "YIBAN_DB_FILE",
                  "YIBAN_STATE_DIR", "YIBAN_EXECUTOR_ID"):
            os.environ.pop(k, None)
        return False

    # ---- 夹具 ----
    def add_task(self, phone=PHONE, state="pending", run_at_offset=-1, day=DAY,
                 vshard=0):
        stamp = self.fc.stamp(run_at_offset)
        conn = db.get_conn()
        conn.execute(
            "INSERT INTO sign_tasks (phone, day, vshard, owner, run_at, priority, "
            "state, attempts, lease_until, result, epoch, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (phone, day, vshard, "", stamp, 5, state, 0, "", "", 0, stamp))
        conn.commit()

    def row(self, phone=PHONE, day=DAY):
        r = db.get_conn().execute(
            "SELECT state, owner, epoch, run_at FROM sign_tasks WHERE phone=? AND day=?",
            (phone, day)).fetchone()
        return dict(r) if r else None

    def claim_statements(self):
        return claim_statements(self.statements)

    # ---- 跑一轮 ----
    def run_entry(self, entry, phones=(PHONE,), overrides=None):
        """按某条入口的角色参数跑一轮；`overrides` 用于变异对照（如 `elbow_sec=0.0`）。"""
        kwargs = dict(ENTRIES[entry])
        kwargs.update(overrides or {})
        accounts = [SimpleNamespace(phone=p, user_paused=False, owner="u@" + p)
                    for p in phones]
        return executor_v3.run_executor_v3(
            accounts, day=DAY, cfg=planner_cfg(), rng=random.Random(7),
            cred_state={}, event_sink=[].append, unreached={}, **kwargs)


# ---- SQL 边界观察面 ----
#: 领取写入：一条语句的 **SET 侧**写死 `claimed`。必须锚在 `SET state=` 上：
#: `reap_abandoned` 的 WHERE 侧也含 `state='claimed'`（参数展开后的值），只按值匹配会误认。
_CLAIM_SET_MARK = "set state='claimed'"

#: 事务控制语句：`sqlite3` 的 trace 回调也会报它们，判定语句数时必须剔除。
_TXN_WORDS = frozenset({"BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT", "RELEASE", "END"})


def data_statements(statements):
    """只留真正对库下命令的语句（剔掉 BEGIN/COMMIT 这类事务控制）。"""
    return [s for s in statements if s.split(None, 1)[0].upper() not in _TXN_WORDS]


def claim_statements(statements):
    """从语句流里挑出**领取写入**（SET 侧写死 claimed 的那一条）。"""
    out = []
    for s in data_statements(statements):
        if "sign_tasks" not in s:
            continue
        head = s.lower().split("where", 1)[0]        # SET 侧在 WHERE 之前
        if _CLAIM_SET_MARK in head:
            out.append(s)
    return out


def shape(sql):
    """语句形状：字符串字面量与数字换成占位符、折叠空白、转小写。

    为什么按形状比而不按原文比：trace 回调给的是**参数已展开**的语句，故持有者身份、
    分片集、肘界截断值都逐字出现在文本里。那些正是三条入口**有意不同**的入参
    （肘界下推／宽分片／显式回收），不是"走了另一条写路径"。形状相同 ⇒ 同一条语句。
    """
    import re
    s = re.sub(r"'[^']*'", "?", sql)
    s = re.sub(r"\b\d+\b", "#", s)
    return re.sub(r"\s+", " ", s).strip().lower()
