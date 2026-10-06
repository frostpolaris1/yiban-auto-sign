# -*- coding: utf-8 -*-
"""队列**读不通**不得被任何读者判成「今天没活了」（ba-p01-01）。

标签：A · 调度：领取失败判定
覆盖：`queue_store` 五枚影响「退出/了结」判定的读点（`claim_batch`、`pending_count`、
   `open_count`、`day_counts`、`requeue_failed`）在库读不通时回**可判的失败值**——`None`
   哨兵。哨兵沿用同文件 `claimed_owners`→`reap_expired` 的既有惯例，不新造第三种形状。
   「空（真的没活）」与「坏（读不出来）」两条路径的返回值必须不同形。执行体补货循环
   不把「坏」当「收干」；连续 N 轮读不通后有界停止并留下响亮痕迹。`open_count` 跨载体
   读者链（`queue_store.open_count` → `db.task_open_count` 门面别名 →
   `state_io.has_undone_accounts_today` → 宿主 run.sh / 容器调度器）逐环不把「读不到」
   当「已了结」。`day_counts` 的展示读者（签到进度端点）读不通时 200 + 标未知，不 500、
   不装作全 0。
对应实现：yiban/store/queue_store.py（claim_batch、pending_count、open_count、
   day_counts、requeue_failed）、yiban/store/db.py（task_open_count / task_stats 别名）、
   yiban/engine/executor_v3.py（`_refiller` 收干判据、`QUEUE_UNREADABLE_MAX_ROUNDS`、
   `_alert_queue_unreadable`）、yiban/engine/state_io.py（has_undone_accounts_today）、
   web/routes/settings_api.py（api_scheduler_executors_progress）。
关键断言：库读不通 ⇒ 执行体**不**进入「完成」分支。修前是第一轮就 break——全天零签到、
   现场只有两条 WARNING。修后必须连续读满 N 轮才停（不许无限空转），并同时留下一条
   ERROR 与一条管理员告警；中途只要有一轮读通，计数归零、不得告警。通向 `break` 的
   那条收干判据里，`rows` 与 `pending` 两个闸门值都必须先被 `is not None` 排除过哨兵。
   这条是结构守卫：判据退回「按真假取闸门值」当场红。`open_count` 链逐环可判：别名与源
   函数同一身份；「状态文件说都签完了 + 库读不通」必须答「仍有未了结」（修前答「没有」
   ＝补签轮不跑＝漏签）。降级口径的**例外名册**也是判据：`queue_store` 里任何异常分支
   回非 None 值都必须显式登记，新加一枚 fail-open 读点当场红。
依赖：临时 sqlite（sign_tasks）+ 打桩 `queue_store._queue_conn`（本表连接的唯一取点）
   + 假时钟（`_now`/`_sleep`/`_mono`）。不发网络请求、不真发信（conftest 已关邮件与推送）。
   整文件在本机执行，无 skip。
"""
import ast
import asyncio
import contextlib
import datetime
import inspect
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import db  # noqa: E402

from yiban import clock, egress  # noqa: E402
from yiban.engine import alerts, executor_v3, state_io  # noqa: E402
from yiban.store import queue_store  # noqa: E402

TEST_KEY = "a" * 64
DAY = "2026-09-22"          # 周二，避开周末门
#: 窗口 06:30~07:50 之内：窗口关是另一条 break 判据，会掩盖本组用例要判的东西
START = datetime.datetime(2026, 9, 22, 6, 40, 0)
OWNER = "single@testhost"
RUNTIME_OWNER = egress.runtime_owner(OWNER)
SHARDS = (0,)
#: 「库读不通」的注入方式：本表连接的唯一取点抛锁等待超时。生产里长事务占锁超过
#: busy_timeout 就是这个异常（store/audit_chain.py 自己点名 replace_accounts 整表重插）
LOCK_ERR = sqlite3.OperationalError("database is locked")
#: 空转上限的**独立**判据：明显大于 N 的实际取值，但必须是有限数。它不从
#: `QUEUE_UNREADABLE_MAX_ROUNDS` 推导——否则「把 N 改成无穷」这一发突变会把判据
#: 一起改成无穷，守卫跟着失效。
SPIN_CAP = 200


def _phone(i):
    return f"1380000{i:04d}"


def _cfg(**over):
    """`schedule.planner_config()` 的同形快照（用例不读真实 .env）。"""
    cfg = {"order": "sequence", "dist": "uniform", "edge_front_sec": 60,
           "edge_back_sec": 60, "block_cap": 15, "mu_min_pct": 40, "mu_max_pct": 60,
           "sigma_min_pct": 15, "sigma_max_pct": 25, "min_exec_gap": 5,
           "avg_attempt_sec": 3, "retry_min_interval": 60, "exec_gap_min": 10,
           "allow_time_pref": 0, "sign_start": (6, 30), "sign_end": (7, 50),
           "bucket_rate": 1.0, "executors": [OWNER]}
    cfg.update(over)
    return cfg


class _DbBase(unittest.TestCase):
    """一个临时库 + 一个临时状态目录：状态文件是补签闸门的第二事实源，必须能写。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-failclosed-")
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
            path = self.db_file + suffix
            if os.path.exists(path):
                os.remove(path)
        self.state_dir = tempfile.mkdtemp(dir=self.tmp, prefix="state-")
        os.environ["YIBAN_STATE_DIR"] = self.state_dir
        self.addCleanup(shutil.rmtree, self.state_dir, ignore_errors=True)
        db.init_db(self.db_file, env_file=self.env_file, cleanup=False)

    def tearDown(self):
        self._close_conn()

    def _add_task(self, phone, vshard=0, state="pending"):
        conn = db.get_conn()
        conn.execute(
            "INSERT INTO sign_tasks (phone, day, vshard, owner, run_at, priority, "
            "state, attempts, lease_until, result, epoch, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (phone, DAY, vshard, "", f"{DAY} 06:40:00.000", 5, state, 0,
             "", "", 0, f"{DAY} 06:30:00.000"))
        conn.commit()

    @contextlib.contextmanager
    def _unreadable(self):
        """把本表连接取点换成必然抛：等价于锁等待超时 / 库损坏 / 打不开的库。"""
        with mock.patch.object(queue_store, "_queue_conn", side_effect=LOCK_ERR):
            yield


# ---------------------------------------------------------------------------
# ① 存储层：五枚读点必须把「坏」和「空」分成两个可判的值
# ---------------------------------------------------------------------------
class FailClosedSentinelTest(_DbBase):
    """`None` 是「读不出来」；`[]` / `0` / 全 0 字典是「真的没活」。"""

    def test_claim_batch_读不通返回哨兵而不是空列表(self):
        with self._unreadable(), self.assertLogs("yiban.store.queue_store",
                                                 "WARNING") as cm:
            got = queue_store.claim_batch(RUNTIME_OWNER, DAY, SHARDS)
        self.assertIsNone(got, "返回 [] 与「表在、但无到期行」同形，调用方无从区分")
        self.assertIn("批量领取签到任务失败", "\n".join(cm.output))

    def test_pending_count_读不通返回哨兵而不是零(self):
        with self._unreadable(), self.assertLogs("yiban.store.queue_store",
                                                 "WARNING") as cm:
            got = queue_store.pending_count(DAY, SHARDS)
        self.assertIsNone(got, "0 会让调用方走「没有待办」的收干分支")
        self.assertIn("读取当日待办任务计数失败", "\n".join(cm.output))

    def test_open_count_读不通返回哨兵而不是零(self):
        with self._unreadable(), self.assertLogs("yiban.store.queue_store",
                                                 "WARNING") as cm:
            got = queue_store.open_count(DAY)
        self.assertIsNone(got, "0 会被无分片上下文的读者当成「当日已了结」")
        self.assertIn("读取当日未了结任务计数失败", "\n".join(cm.output))

    def test_day_counts_读不通返回哨兵而不是全零(self):
        with self._unreadable(), self.assertLogs("yiban.store.queue_store",
                                                 "WARNING") as cm:
            got = queue_store.day_counts(DAY)
        self.assertIsNone(got, "全 0 字典里的 open=0 就是「没有未了结」，与「读不出来」同形")
        self.assertIn("读取当日签到任务计数失败", "\n".join(cm.output))

    def test_requeue_failed_读不通返回哨兵而不是零(self):
        with self._unreadable(), self.assertLogs("yiban.store.queue_store",
                                                 "WARNING") as cm:
            got = queue_store.requeue_failed(DAY, SHARDS)
        self.assertIsNone(got, "0 与「没有 failed 行要回炉」同形：failed 行没翻回 pending，"
                               "就永远不会出现在 pending_count 里")
        self.assertIn("读取当日弃用任务失败", "\n".join(cm.output))


class LegitEmptyStillEmptyTest(_DbBase):
    """反向守卫：不许把「空」也一并改成 `None`。

    空被改成哨兵会让执行体在「今天真的没活」时也撞上有界放弃闸门，把正常收干报成故障。
    """

    def test_表里没有行时空值照旧(self):
        self.assertEqual(queue_store.claim_batch(RUNTIME_OWNER, DAY, SHARDS), [])
        self.assertEqual(queue_store.pending_count(DAY, SHARDS), 0)
        self.assertEqual(queue_store.open_count(DAY), 0)
        self.assertEqual(queue_store.requeue_failed(DAY, SHARDS), 0)
        counts = queue_store.day_counts(DAY)
        self.assertIsInstance(counts, dict)
        self.assertEqual(counts["total"], 0)

    def test_有行但都已了结时空值照旧(self):
        self._add_task(_phone(1), state="done")
        self._add_task(_phone(2), state="skipped")
        self.assertEqual(queue_store.claim_batch(RUNTIME_OWNER, DAY, SHARDS), [])
        self.assertEqual(queue_store.pending_count(DAY, SHARDS), 0)
        self.assertEqual(queue_store.open_count(DAY), 0)

    def test_空分片集不算故障且不取连接(self):
        """vshards=() / 空允许集 = 本轮不该领活，与库无关（既有口径不得漂）。"""
        boom = mock.patch.object(queue_store, "_queue_conn",
                                 side_effect=AssertionError("不该取连接"))
        with boom:
            self.assertEqual(queue_store.claim_batch(RUNTIME_OWNER, DAY, ()), [])
            self.assertEqual(queue_store.pending_count(DAY, ()), 0)
            self.assertEqual(queue_store.requeue_failed(DAY, ()), 0)
            self.assertEqual(queue_store.claim_batch(RUNTIME_OWNER, DAY, SHARDS,
                                                     phones=()), [])
            self.assertEqual(queue_store.pending_count(DAY, SHARDS, phones=()), 0)


# ---------------------------------------------------------------------------
# ② 执行体：读不通不得判收干；连续 N 轮必须有界停止并出声
# ---------------------------------------------------------------------------
class _SpinGuard:
    """假 `_sleep`：数拍数并推进单调钟。超过 `SPIN_CAP` 拍 ⇒ 上界没生效，判红。"""

    def __init__(self, mono_step=0.0):
        self.n = 0
        self.mono = 10_000.0  # 起点非零：从 0 起会掩盖「把墙钟当单调钟用」那一类错
        self.mono_step = mono_step

    async def sleep(self, sec):
        self.n += 1
        if self.n > SPIN_CAP:
            raise AssertionError(f"补货循环已空转 {self.n} 拍仍未停止：读不通没有上界")
        self.mono += self.mono_step
        await asyncio.sleep(0)


class RefillerFailClosedTest(_DbBase):
    """补货循环的收干判据：坏值不得被折叠成「活已了结」，也不得无限空转。"""

    def setUp(self):
        super().setUp()
        self.collected = []
        self.errors = []

    def _ctx(self, **over):
        # 替身必须带**全部真字段**（含 `queue_unreadable`）：用 getattr 兜底会让缺字段
        # 静默通过，正是「豁免在缺字段时静默失效」那一类错
        base = dict(cfg=_cfg(), day=DAY, executor_id=OWNER, runtime_id=RUNTIME_OWNER,
                    m=2, inflight=0, busy=0, slot=0, held=set(), v=8,
                    reclaim=True, allowed_phones=None, requeue_during_run=False,
                    queue_unreadable=False)
        base.update(over)
        return SimpleNamespace(**base)

    def _run(self, ctx, extra=(), mono_step=0.0):
        """跑真实 `_refiller`，返回（走了多少拍, 管理员告警主题, ERROR 文案, 哨兵条数）。"""
        guard = _SpinGuard(mono_step=mono_step)
        queue = asyncio.PriorityQueue()
        patches = [
            mock.patch.object(executor_v3, "_now", lambda: START),
            mock.patch.object(executor_v3, "_sleep", guard.sleep),
            mock.patch.object(executor_v3, "_mono", lambda: guard.mono),
            mock.patch.object(clock, "now", lambda: START),
            mock.patch.object(queue_store.logger, "warning"),
            # 替身的形参必须跟真函数同步：`alerts` 分级单（ba-p04-02）给
            # `_collect_admin_mail` 新增了 `level=`。窄签名会把「真函数增参」
            # 报成测试红，掩盖本用例要验的收干判据。
            mock.patch.object(alerts, "_collect_admin_mail",
                              side_effect=lambda s, e, level=None: self.collected.append(s)),
            mock.patch.object(executor_v3.logger, "error",
                              side_effect=lambda *a: self.errors.append(
                                  " ".join(str(x) for x in a))),
            *extra,
        ]
        with contextlib.ExitStack() as st:
            for p in patches:
                st.enter_context(p)
            asyncio.run(executor_v3._refiller(queue, SHARDS, ctx))
        sentinels = 0
        while not queue.empty():
            if queue.get_nowait()[0] >= executor_v3.SENTINEL_PRIORITY:
                sentinels += 1
        return guard.n, self.collected, self.errors, sentinels

    def test_读通且确实没活时第一轮就退(self):
        """反向基线：真的没活 ⇒ 立刻收干、不发故障告警（不许狼来了）。"""
        rounds, collected, errors, sentinels = self._run(self._ctx())
        self.assertEqual(rounds, 0)
        self.assertEqual(sentinels, 2, "收干仍要放行 M 条通道的哨兵，否则整轮卡死")
        self.assertEqual(collected, [])
        self.assertEqual(errors, [])

    def test_读不通不进完成分支且有界停止(self):
        """修前的行为＝第一轮 break（0 拍、零告警）＝全天零签到而现场只有两条 WARNING。"""
        rounds, collected, errors, sentinels = self._run(
            self._ctx(), extra=[mock.patch.object(queue_store, "_queue_conn",
                                                  side_effect=LOCK_ERR)])
        self.assertGreater(rounds, 0, "读不通不得被当成「活已了结」立刻退出")
        self.assertEqual(rounds, executor_v3.QUEUE_UNREADABLE_MAX_ROUNDS - 1,
                         "必须连续读满 N 轮才停：早一轮是提前放弃，晚一轮是空转")
        self.assertLess(rounds, SPIN_CAP, "停止必须有界")
        self.assertEqual(sentinels, 2)
        self.assertEqual(len(errors), 1, "必须留下一条 ERROR")
        self.assertEqual(len(collected), 1, "必须留下一条管理员告警（响亮，不是静默 spin）")

    def test_读通一轮即清零不发告警(self):
        """判据是「**连续** N 轮」：中途读通一次，计数归零，不得报故障。"""
        bad = executor_v3.QUEUE_UNREADABLE_MAX_ROUNDS - 1
        with mock.patch.object(queue_store, "claim_batch",
                               side_effect=[None] * bad + [[]]), \
                mock.patch.object(queue_store, "pending_count", return_value=0):
            rounds, collected, errors, _sent = self._run(self._ctx())
        self.assertEqual(rounds, bad, "读通的那一轮应当正常收干")
        self.assertEqual(collected, [])
        self.assertEqual(errors, [])

    def test_轮内回炉读不通同样计入上界(self):
        """`requeue_failed` 是第 5 枚降级点：它的「坏」必须进同一条闸门。

        只改存储层、不改这个消费点，就是「同批只改了一半」。
        """
        ctx = self._ctx(reclaim=False, requeue_during_run=True)
        with mock.patch.object(queue_store, "claim_batch", return_value=[]), \
                mock.patch.object(queue_store, "pending_count", return_value=1), \
                mock.patch.object(queue_store, "requeue_failed", return_value=None), \
                mock.patch.object(queue_store, "reap_expired", return_value=0), \
                mock.patch.object(executor_v3, "_live_row_owners", return_value=()):
            # 每拍推进一个回收周期，保证每一轮都真的走到回炉那一句
            rounds, collected, errors, _sent = self._run(
                ctx, mono_step=executor_v3.RECOVER_SEC)
        self.assertLess(rounds, SPIN_CAP, "回炉读不通也必须有界停止")
        self.assertEqual(len(collected), 1, "回炉读不通同样要留下一条管理员告警")
        self.assertEqual(len(errors), 1)

    def test_轮首回炉读不通计入同一闸门(self):
        """`run_executor_v3` 轮首那次回炉（另一个生产调用点）不得把「坏」当「没行要回炉」。"""
        rounds, collected, errors, _sent = self._run(
            self._ctx(queue_unreadable=True),
            extra=[mock.patch.object(queue_store, "_queue_conn", side_effect=LOCK_ERR)])
        self.assertEqual(rounds, executor_v3.QUEUE_UNREADABLE_MAX_ROUNDS - 2,
                         "轮首那一轮已经计入，循环里只需再读满 N-1 轮")
        self.assertEqual(len(collected), 1)
        self.assertEqual(len(errors), 1)

    def test_领取失败时不把哨兵当行投进队列(self):
        """哨兵不可迭代：`for r in rows` 直接 TypeError 会把整轮签到打断。

        工单硬约束「不许把读不通退化成抛异常打断整轮而不加出声」正面钉在这里。
        """
        with mock.patch.object(queue_store, "claim_batch", return_value=None), \
                mock.patch.object(queue_store, "pending_count", return_value=1):
            rounds, _collected, _errors, sentinels = self._run(self._ctx())
        self.assertEqual(rounds, executor_v3.QUEUE_UNREADABLE_MAX_ROUNDS - 1,
                         "领取面读不通而待办面恒为 1：只能走到上界才停")
        self.assertEqual(sentinels, 2, "停止时只放哨兵，队列里不得有真条目")

    def test_待办面读不通时不得判收干(self):
        """领取面读通、待办面读不通：不许走收干分支。

        这是本单的原始形状：收干闸门拿 `pending_count` 的 0 当"活已了结"，而它的 0 与
        `None` 修前同形。两枚闸门值必须各自被排除过哨兵才能收干。
        """
        with mock.patch.object(queue_store, "claim_batch", return_value=[]), \
                mock.patch.object(queue_store, "pending_count", return_value=None):
            rounds, collected, errors, sentinels = self._run(self._ctx())
        self.assertEqual(rounds, executor_v3.QUEUE_UNREADABLE_MAX_ROUNDS - 1,
                         "待办面回哨兵 ⇒ 这一拍不得收干，只能走到上界才停")
        self.assertEqual(len(collected), 1)
        self.assertEqual(len(errors), 1)
        self.assertEqual(sentinels, 2)


# ---------------------------------------------------------------------------
# ③ 结构守卫：收干判据里不许出现「异常值与空值同形」的写法
# ---------------------------------------------------------------------------
def _is_none_constant(node):
    return isinstance(node, ast.Constant) and node.value is None


def _break_tests(fn):
    """取函数体里所有「块内含 break」的 if 测试节点。"""
    out = []
    for node in ast.walk(ast.parse(inspect.getsource(fn))):
        if isinstance(node, ast.If) and any(isinstance(b, ast.Break) for b in node.body):
            out.append(node.test)
    return out


def _names_in(node):
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _proven_not_none(test_node):
    """`X is not None` 里的 X 集合。"""
    return {c.left.id for c in ast.walk(test_node)
            if isinstance(c, ast.Compare) and isinstance(c.left, ast.Name)
            and len(c.ops) == 1 and isinstance(c.ops[0], ast.IsNot)
            and _is_none_constant(c.comparators[0])}


class ExitPredicateShapeTest(unittest.TestCase):
    """把边界钉成一条静态判据：闸门值必须先排除哨兵，再判空。

    行为守卫管「存储层退回 return []/return 0」，本守卫管「执行体端摘掉显式判据」。
    把 `rows is not None` / `pending is not None` 任一摘掉（退回只按 `not rows` 或只留
    `== 0`）当场红。
    """

    def test_收干判据对两个闸门值都显式排除哨兵(self):
        gate = [t for t in _break_tests(executor_v3._refiller)
                if {"rows", "pending"} <= _names_in(t)]
        self.assertEqual(
            len(gate), 1,
            "补货循环里必须恰有一条同时读领取面（rows）与待办面（pending）的收干判据")
        proven = _proven_not_none(gate[0])
        self.assertIn("rows", proven,
                      "领取结果必须先排除哨兵：哨兵与空列表同形就是本单的根因")
        self.assertIn("pending", proven,
                      "待办计数必须先排除哨兵：0 与读不通同形同样是根因")


class DegradePolicyShapeTest(unittest.TestCase):
    """`queue_store` 的降级口径**只有一条**：异常分支回 `None` 哨兵；例外必须登记。

    行为守卫逐枚管「这五枚今天回哨兵」，本守卫管**名册本身**：新加一枚异常时回空值的
    读点，或把某一枚改回 `return []` / `return 0` / 全 0 字典，当场红。放行它就得在
    例外名册里显式写出来，并交代它为什么不参与退出/了结判定。
    """

    #: 不取哨兵的读/写点（补偿动作与纯展示读面）。失败方向是「这一轮少做一次」或
    #: 「页面少显示一块」，不参与任何退出/了结判定；理由逐枚见 `queue_store` 模块头。
    NOT_FAIL_CLOSED = (
        "settle_tasks", "requeue_task", "reclaim_tasks", "claimed_owners",
        "reap_expired", "reap_abandoned", "load_egress_state", "save_egress_state",
        "purge", "fallback_event", "latest_day", "owners_for_day", "owners_since",
        "activity",
    )

    @staticmethod
    def _handlers():
        tree = ast.parse(inspect.getsource(queue_store))
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef):
                continue
            for sub in ast.walk(node):
                if isinstance(sub, ast.Try):
                    for handler in sub.handlers:
                        yield node.name, handler

    @staticmethod
    def _returns_non_none(handler):
        for stmt in handler.body:
            if (isinstance(stmt, ast.Return) and stmt.value is not None
                    and not _is_none_constant(stmt.value)):
                return True
        return False

    def test_异常分支要么回哨兵要么在例外名册里(self):
        offenders = sorted({name for name, handler in self._handlers()
                            if name not in self.NOT_FAIL_CLOSED
                            and self._returns_non_none(handler)})
        self.assertEqual(
            offenders, [],
            "这些读点的异常分支回了空值：退回 `return None` 哨兵，"
            "或在 NOT_FAIL_CLOSED 里登记并写明它为什么不参与退出/了结判定")

    def test_五枚哨兵读点都还在(self):
        names = {n.name for n in ast.parse(inspect.getsource(queue_store)).body
                 if isinstance(n, ast.FunctionDef)}
        missing = [n for n in ("claim_batch", "pending_count", "open_count",
                               "day_counts", "requeue_failed") if n not in names]
        self.assertEqual(missing, [], "本单框定的哨兵读点被改名或删除：名册必须同批更新")


# ---------------------------------------------------------------------------
# ④ open_count 跨载体读者链：每一环都不许把「读不到」当「已了结」
# ---------------------------------------------------------------------------
class OpenCountReaderChainTest(_DbBase):
    """`queue_store.open_count` → `db.task_open_count` → `state_io.has_undone_accounts_today`。"""

    def test_门面别名与源函数同一身份(self):
        """别名被换成「包一层吞掉哨兵」的壳，链上第二环就漏改了。"""
        self.assertIs(db.task_open_count, queue_store.open_count)
        self.assertIs(db.task_stats, queue_store.day_counts)

    def _clean_state_file(self):
        """状态文件写「三个账号全签成」——单看它就是「已了结」。"""
        path = os.path.join(self.state_dir, f"sign-state-{DAY}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({_phone(i): {"status": "success"} for i in (1, 2, 3)}, f)

    def test_库读不通且状态文件干净时必须仍答未了结(self):
        """修前：open_count 回 0 ⇒ 只看状态文件 ⇒ 答「无未了结」⇒ 补签轮不跑 ⇒ 漏签。"""
        self._clean_state_file()
        with self._unreadable():
            undone = state_io.has_undone_accounts_today(self.state_dir, DAY)
        self.assertTrue(undone, "读不通＝状态未知，不得据「数不出未了结行」判成了结")

    def test_读不通时判据必须出声(self):
        """出声是给人定位用的：静默 fail-closed 会变成「补签轮莫名其妙天天跑」。"""
        self._clean_state_file()
        with self._unreadable(), self.assertLogs("yiban", level="WARNING") as cm:
            state_io.has_undone_accounts_today(self.state_dir, DAY)
        self.assertIn("按未了结处理", "\n".join(cm.output),
                      "本函数自己要出声，不能只靠 queue_store 那条查询失败告警")

    def test_补签闸门继承该判定(self):
        self._clean_state_file()
        with mock.patch.object(state_io, "full_run_done_today", return_value=True), \
                self._unreadable():
            self.assertTrue(state_io.need_second_run(self.state_dir, DAY))

    def test_库读得通且确实没有未了结行时答已了结(self):
        """反向守卫：不许把「确实干净」也判成「还有活」，否则补签轮每天空跑。"""
        self._clean_state_file()
        self._add_task(_phone(1), state="done")
        self.assertFalse(state_io.has_undone_accounts_today(self.state_dir, DAY))

    def test_队列里有未了结行时答未了结(self):
        self._clean_state_file()
        self._add_task(_phone(9), state="failed")
        self.assertTrue(state_io.has_undone_accounts_today(self.state_dir, DAY))


# ---------------------------------------------------------------------------
# ⑤ day_counts 的展示读者：读不通不 500，也不许装作「全站都是 0」
# ---------------------------------------------------------------------------
class ProgressEndpointUnreadableTest(unittest.TestCase):
    """`db.task_stats`（= `day_counts`）回 `None` 时签到进度端点的行为。"""

    def _call(self, task_stats):
        from flask import Flask

        from web.routes import settings_api

        m = SimpleNamespace(
            _is_builtin_admin_session=lambda: True,
            clock=SimpleNamespace(today=lambda: DAY),
            db=SimpleNamespace(task_latest_day=lambda: DAY, task_stats=task_stats),
            _executor_activity=lambda day: ([], {"claimed": 0, "failed": 0,
                                                 "done": 0, "total": 0}),
            _executors_window=lambda: ((6, 30), (7, 50)),
            _in_run_period=lambda bounds: False,
        )
        with Flask("probe").test_request_context(), \
                mock.patch.object(settings_api, "_appmod", return_value=m):
            resp = settings_api.api_scheduler_executors_progress()
        if isinstance(resp, tuple):
            self.fail(f"进度端点读不通不得报错：{resp[1]}")
        return resp.get_json()

    def test_读不通返回200且明说是未知(self):
        """库打不开 / 未初始化 / 锁住 / 损坏都走这条：200 + 标未知，绝不 500。

        全 0 占位是给页面画空条用的，`totals_unreadable` 才是「读数不可用」这个事实。
        """
        body = self._call(lambda day: None)
        self.assertTrue(body["ok"])
        self.assertIs(True, body.get("totals_unreadable"),
                      "读不通必须与「当日无记录（全 0）」分成两件事交给运维看")
        self.assertTrue(all(v == 0 for v in body["totals"].values()),
                        "占位全 0：页面画空条，不把哨兵泄成 null/字符串")
        self.assertIn("读不通", body["note"], "note 直接显示给运维，要说清这不是真实进度")

    def test_读取抛异常时也不变成错误响应(self):
        """`task_stats` 抛异常（库打不开这类形态）同样不许把面板打成 5xx。

        `day_counts` 自己吞异常回哨兵，故这条是**兜底**：别名哪天被换成会外抛的壳，
        面板也不该从「一个计数读不到」退化成「整页打不开」。
        """
        def boom(day):
            raise RuntimeError("unable to open database file")

        body = self._call(boom)
        self.assertTrue(body["ok"])
        self.assertIs(True, body.get("totals_unreadable"))

    def test_未初始化或当日无记录时仍200且不打未知标记(self):
        """新部署（库刚建、当日无行）与正常空态：全 0 是**事实**，不是「读不通」。"""
        counts = dict.fromkeys(queue_store.STATES, 0)
        counts.update({"settled": 0, "open": 0, "total": 0})
        body = self._call(lambda day: counts)
        self.assertTrue(body["ok"], "未初始化/空态不得 500")
        self.assertFalse(body.get("totals_unreadable"),
                         "「当日无记录」是正常空态，不许标成读不通")
        self.assertNotIn("读不通", body["note"])


if __name__ == "__main__":
    unittest.main()
