# -*- coding: utf-8 -*-
"""V-S1（l1rx）：现役领取面「谁抢到谁签」四条不变式 —— **租约族删除前的合并校验者**。

标签：B · 调度：领取/队列/执行体

**本文件的性质（l1rx「先立校验者再删」的落点）**
本文件是 `l1rx`（D3a · 删租约族）所要求"先立校验者再删"里 **V-S1 的校验者**；租约族删除
之后，它是"谁抢到谁签"保留面的**合并校验者**。故它与既有门**有意重叠**，重叠来源逐条
点名（仓规"宁可不写测试，也不要冗余"在此让位于"先立校验者"）：
  · 轴2「领后回空」＝ `tests/test_store_queue.py::ClaimBatchTest::test_second_call_returns_empty`；
  · 轴2/轴3「空分片 / 无到期行回空」＝ `tests/test_store_queue.py::ClaimBatchTest::test_empty_vshards_returns_empty`、`::test_no_due_rows_is_silent`；
  · 轴4「哨兵而非空列表」＝ `tests/test_queue_failclosed.py::FailClosedSentinelTest::test_claim_batch_读不通返回哨兵而不是空列表`、`tests/test_store_queue.py::ClaimBatchTest::test_missing_table_returns_sentinel_with_warning`；
  · 轴4/结构「异常分支口径」＝ `tests/test_queue_failclosed.py::DegradePolicyShapeTest::test_异常分支要么回哨兵要么在例外名册里`。
**保留而不删**：删租约之后本文件是保留面；把本文件拥有的覆盖删掉，方向是反的。

覆盖：现役领取面 `yiban/store/queue_store.py::claim_batch` 的**四条轴**（缺一不可）：
   轴1 **原子性**：判据（`WHERE … state='pending'`）与写入（`SET state='claimed'`）
       在**同一条语句**里——不存在"先查后写"的跨语句窗口；
   轴2 **同名重入不放行**：已持有的行对同一 owner（及任何 owner）都不可再领；
   轴3 **拿不到就跳过**：竞争失败透出的是"没活可做"（空列表），**不是**失败
       （不抛异常、不回哨兵）——失败会进重试/告警/退出码面，语义完全不同；
   轴4 **库异常 fail-closed**：返回 `None` 哨兵，绝不答"可执行"。
   另加一条结构守卫：生产代码里 `UPDATE sign_tasks … SET state` 的写点必须在受控面内
   （领取写入唯一＝`claim_batch`；参数化新写点 fail-closed）。

**轴1 的真实判别力在哪（D3 声明）**：`test_两线程并发只有一个赢家` **不承担**轴1 的行为
判据——`claim_batch` 在进程内锁下执行（`queue_store.py:253-256`），两线程被串行化，故把
它改成"先查后写"时该用例仍绿。轴1 的判别力在 `test_判据与写入在同一条语句里` 的**语句
计数断言**（一次领取发出的 `execute` 必须恰为 1 条）。并发用例只作冒烟，不得当并发判据读。

对应实现：yiban/store/queue_store.py（claim_batch 的 `UPDATE … RETURNING` 与
   `_queue_conn`）——四条不变量当前由 `claim_batch` 的单条 `UPDATE` 一处承载。
关键断言：本门钉**任何时刻都不该被违反的性质**，不是某段调用顺序。删租约族后 V-S1 是
   "谁抢到谁签"的唯一防线，故每轴都可判别：拆成"先查后写"、放行同名重入、
   竞争失败透出成失败、库异常 fail-open——各自的主用例当场红（判别力见报告）。
依赖：临时 sqlite（sign_tasks）+ 打桩 `queue_store._queue_conn`（本表连接唯一取点）；
   真线程（并发冒烟，非轴1 判据）+ 真源文件 AST（结构守卫）。无网络请求。整文件在本机执行，无 skip。
"""
import ast
import contextlib
import os
import shutil
import sqlite3
import tempfile
import threading
import unittest
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import db  # noqa: E402

from yiban.store import connection as store_connection  # noqa: E402
from yiban.store import queue_store  # noqa: E402

TEST_KEY = "a" * 64
DAY = "2026-09-22"          # 周二，避开周末门
OWNER = "single@testhost"
OTHER = "single@otherhost"
SHARDS = (0,)
RUNTIME_OWNER = "single@testhost:100:090000"
#: 业务日内固定时刻（写给 `claim_batch(now=…)`）：缺省 `clock.ts()` 是真实当日，
#: 会让"未到点"用例的 run_at 比较落到过去而失去判别力。
NOW = f"{DAY} 06:40:00.000"


def _phone(i):
    return f"1380000{i:04d}"


def _row(**over):
    """sign_tasks 的一行（列全给，缺省值对齐 v18 DDL）。"""
    base = dict(phone=_phone(1), day=DAY, vshard=0, owner="",
                run_at=f"{DAY} 06:40:00.000", priority=5, state="pending",
                attempts=0, lease_until="", result="", epoch=0,
                created_at=f"{DAY} 06:30:00.000")
    base.update(over)
    return base


class _SpyConn:
    """真连接的记录代理：记下 `claim_batch` 期间执行过的每一条语句。

    只代理 `execute` / `executemany`；其余属性透传。`claim_batch` 的执行面只有
    `execute` + `commit` 两个动作，故这份记录即"领取动作发出了几条语句"。
    """

    def __init__(self, real):
        self._real = real
        self.calls = []           # [(kind, sql), ...]

    def execute(self, sql, *args, **kwargs):
        self.calls.append(("execute", sql))
        return self._real.execute(sql, *args, **kwargs)

    def executemany(self, sql, *args, **kwargs):
        self.calls.append(("executemany", sql))
        return self._real.executemany(sql, *args, **kwargs)

    def commit(self):
        return self._real.commit()

    def __getattr__(self, name):
        return getattr(self._real, name)


class _DbBase(unittest.TestCase):
    """一个临时库（sign_tasks 由 `db.init_db` 的迁移建出）。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-vs1-")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n")
        os.environ.update({
            "YIBAN_ACCOUNTS_KEY": TEST_KEY,
            "YIBAN_ENV_FILE": cls.env_file,
            "YIBAN_DB_FILE": cls.db_file,
            "YIBAN_STATE_DIR": cls.tmp,
            "YIBAN_LOG_FILE": os.path.join(cls.tmp, "sign.log"),
        })

    @classmethod
    def tearDownClass(cls):
        cls._close_conn()
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k in ("YIBAN_ACCOUNTS_KEY", "YIBAN_ENV_FILE", "YIBAN_DB_FILE",
                  "YIBAN_STATE_DIR", "YIBAN_LOG_FILE"):
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
        db.init_db(self.db_file, env_file=self.env_file, cleanup=False)

    def tearDown(self):
        self._close_conn()

    def _add_task(self, **over):
        row = _row(**over)
        conn = db.get_conn()
        conn.execute(
            "INSERT INTO sign_tasks (phone, day, vshard, owner, run_at, priority, "
            "state, attempts, lease_until, result, epoch, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (row["phone"], row["day"], row["vshard"], row["owner"], row["run_at"],
             row["priority"], row["state"], row["attempts"], row["lease_until"],
             row["result"], row["epoch"], row["created_at"]))
        conn.commit()
        return row["phone"]

    def _claim(self, owner=RUNTIME_OWNER, **kw):
        kw.setdefault("now", NOW)
        return queue_store.claim_batch(owner, DAY, SHARDS, **kw)

    def _spy(self):
        spy = _SpyConn(store_connection.get_conn())
        patch = mock.patch.object(
            queue_store, "_queue_conn",
            lambda: (spy, store_connection._conn_lock))
        return spy, patch


# ---------------------------------------------------------------------------
# 轴1 · 原子性：判据与写入在同一条语句里
# ---------------------------------------------------------------------------
class Axis1AtomicityTest(_DbBase):
    """领取必须是**单条** `UPDATE sign_tasks … SET state='claimed' WHERE state='pending'`。

    判别力（两条突变路径都必须让本轴红）：
      · 拆成"先查后写"：`execute` 计数变 2 ⇒ 红；
      · 判据搬走：单条 UPDATE 里找不到 `state='pending'` ⇒ 红。
    本轴的**行为判据**是 `test_判据与写入在同一条语句里`；本类的并发用例只作冒烟
    （见模块头 D3 声明：进程内锁把两线程串行化，并发用例对"先查后写"不敏感）。
    """

    def test_判据与写入在同一条语句里(self):
        self._add_task()
        spy, patch = self._spy()
        with patch:
            rows = self._claim()
        self.assertEqual(len(rows), 1, "前置：这一条应当被领到，否则后面的断言无意义")
        stmts = [sql for _kind, sql in spy.calls if isinstance(sql, str)]
        sql_all = " ".join(stmts)
        self.assertEqual(len(stmts), 1,
                         "领取发出的语句数必须恰好 1：%d 条就是'比较'与'设置'之间有"
                         "跨语句窗口（先查后写），多执行体下两个执行体会同时放行同一账号"
                         % len(stmts))
        self.assertTrue(stmts[0].lstrip().upper().startswith("UPDATE"),
                        "唯一的领取语句必须是一条 UPDATE，实际：%r" % stmts[0][:60])
        self.assertIn("state='claimed'", sql_all, "SET 侧必须写入 claimed 态")
        self.assertIn("state='pending'", sql_all,
                      "WHERE 侧必须带 pending 判据——判据搬走就等于无条件改写")

    @staticmethod
    def _worker(owner, results, barrier):
        """一个竞位者：拉齐起跑线后领取该行。闭包显式传参（不捕获循环变量）。"""
        barrier.wait()
        results.append(queue_store.claim_batch(owner, DAY, SHARDS, now=NOW))

    def test_两线程并发只有一个赢家(self):
        """并发冒烟：同一行两线程抢，恰一个赢家。

        **不承担**轴1 的行为判据（模块头 D3 声明）：`claim_batch` 在进程内锁下执行
        （`queue_store.py:253-256`），两线程被串行化，故把它改成"先查后写"时本用例
        仍绿。跨进程的原子性由领取语句本身保证，判别力在 `test_判据与写入在同一条
        语句里` 的语句计数断言。此处只作"不会双领"的冒烟。
        """
        for i in range(5):           # 多轮：单轮偶然命中不构成判据
            phone = self._add_task(phone=_phone(i))   # 每轮一条新行，避开 (phone, day) 唯一键
            results = []
            barrier = threading.Barrier(2)
            threads = [threading.Thread(target=self._worker, args=(o, results, barrier))
                       for o in (RUNTIME_OWNER, OTHER)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            winners = [r for r in results if r]
            self.assertEqual(len(winners), 1,
                             "同一行只能有一个赢家，实际赢家数=%d：并发放行＝同一账号并发持有"
                             "＝上游风控红线" % len(winners))
            self.assertEqual(len(winners[0]), 1)
            self.assertEqual(winners[0][0]["phone"], phone)


# ---------------------------------------------------------------------------
# 轴2 · 同名重入不放行
# ---------------------------------------------------------------------------
class Axis2ReentryTest(_DbBase):
    """已持有的行不可再领；拒绝**不看 owner 身份**（没有"自己人就放行"的快捷键）。

    旧领取池 `claims.py` 的等价纪律是"重入必须出示上一代 epoch"；现役面把这条换成
    纯状态判据——`owner` 串不进领取 WHERE，故"同名即自己人"的旁路不存在。
    """

    def test_同名重入不放行(self):
        self._add_task()
        first = self._claim(owner=RUNTIME_OWNER)
        self.assertEqual([r["phone"] for r in first], [_phone(1)])
        self.assertEqual(self._claim(owner=RUNTIME_OWNER), [],
                         "同一 owner 二次领取必须回空，不得放行重入")

    def test_重入拒绝不看owner身份(self):
        self._add_task(state="claimed", owner=RUNTIME_OWNER)   # 已由 RUNTIME_OWNER 持有
        self.assertEqual(self._claim(owner=RUNTIME_OWNER), [],
                         "持有者本人不得再领（无 owner 旁路）")
        self.assertEqual(self._claim(owner=OTHER), [],
                         "任何 owner 都不得领走别人持有的行")
        # 反向守卫：守的是**状态**不是 owner 串——合法回到 pending 的行仍可再领，
        # 证明判据没有退化成"owner 见过就永久拉黑"。
        conn = db.get_conn()
        conn.execute("UPDATE sign_tasks SET state='pending' WHERE phone=?", (_phone(1),))
        conn.commit()
        self.assertEqual(len(self._claim(owner=OTHER)), 1,
                         "合法重排回 pending 的行可被再领，领取门不得比状态机更严")


# ---------------------------------------------------------------------------
# 轴3 · 拿不到就跳过（不是报失败）
# ---------------------------------------------------------------------------
class Axis3SkipNotFailureTest(_DbBase):
    """竞争失败在调用方眼里必须与"确实没活"同形：空列表，不抛异常、不回哨兵。"""

    def test_抢不到与真的没活同形(self):
        self._add_task(state="claimed", owner=OTHER)                  # 竞争失败：别人持有
        lost = self._claim()
        self._add_task(phone=_phone(2), run_at=f"{DAY} 23:59:00.000")  # 真的没活：未到点
        none_due = self._claim()
        self.assertIsInstance(lost, list, "竞争失败不得透出成失败（异常/哨兵）——"
                                         "失败会进重试/告警/退出码面")
        self.assertEqual(lost, [], "竞争失败＝跳过，必须回空列表")
        self.assertEqual(lost, none_due,
                         "竞争失败与真的没活必须同形，调用方无从区分、也不该区分")

    def test_抢不到不抛异常(self):
        self._add_task(state="claimed", owner=OTHER)
        try:
            got = self._claim()
        except Exception as e:
            self.fail("竞争失败抛了异常（%r）：调用方会把它当'失败'而不是'跳过'" % e)
        self.assertIsInstance(got, list)


# ---------------------------------------------------------------------------
# 轴4 · 库异常 fail-closed（不答"可执行"）
# ---------------------------------------------------------------------------
class Axis4FailClosedTest(_DbBase):
    """库读不通回 `None` 哨兵：绝不答"可执行"，也不把异常抛给调用方当可执行。"""

    def _unreadable(self):
        return mock.patch.object(
            queue_store, "_queue_conn",
            side_effect=sqlite3.OperationalError("database is locked"))

    def test_库异常回None哨兵而不是空列表(self):
        self._add_task()
        with self._unreadable(), self.assertLogs("yiban.store.queue_store", "WARNING"):
            got = self._claim()
        self.assertIsNone(got, "库异常必须回 None（读不通）；回 [] 会与'真的没活'同形，"
                               "调用方据此判'收干'即全天零签到")
        self.assertNotEqual(got, [], "None 与 [] 必须是两个可判的值")

    def test_库异常不外抛且调用方看到不可执行(self):
        """行为验证：异常不得穿出领取面被调用方当成"可以执行"。"""
        self._add_task()
        with self._unreadable(), self.assertLogs("yiban.store.queue_store", "WARNING"):
            try:
                got = self._claim()
            except Exception as e:
                self.fail("库异常穿出了领取面（%r）：调用方会把异常当'本账号可执行'，"
                          "多执行体下两个执行体同时放行同一账号" % e)
        self.assertNotIsInstance(got, list, "哨兵不得是列表：列表＝'这些行归你执行'")
        self.assertIsNone(got)


# ---------------------------------------------------------------------------
# 结构守卫 · 领取写入只有一处（三条写入口合流于此）
# ---------------------------------------------------------------------------
class SingleClaimWriteSiteTest(unittest.TestCase):
    """`UPDATE sign_tasks … SET state` 的生产写点必须在受控面内（fail-closed）。

    判定**先归一化**：折叠全部空白、转小写、剥 `'` 与 `"`，并折叠 `+` 拼接与 f-string
    字面段。故改大小写、`=` 两侧加空格、换引号、拆字符串常量这些写法都挡得住。两层判定：
      ① **领取写入唯一**：把 state 置为 claimed 的写点（归一化模式 `…setstate=claimed`）
         必须恰是 `claim_batch` 一处，且必须是**字面量**——把它参数化成 `SET state=?`
         后判定面里就看不见了 ⇒ ① 红。
      ② **写点白名单（参数化 fail-closed）**：凡出现 `UPDATE sign_tasks … SET state` 的
         函数必须全在白名单里；名单外的（含**新加的** `SET state=?` 写点）一律红——
         "值不在判定面内即红"，不是放过。
    三条写入口（兜底常驻 `workers` / 网页手动 `signin_api → runner` / 调度器 `runner`）
    都经 `executor_v3.run_executor_v3` 落到 `claim_batch` 这一条语句。

    **已知挡不住**（静态面之限，报告里如实登记）：白名单内的参数化写点若被喂进一个
    "claimed" 值（值来自别处常量、静态不可见），本门不报。故白名单只登记本仓既有的
    收尾/回收写点，新增写点必须显式登记。
    """

    _PROD_DIRS = ("yiban", "web", "scripts")   # tests/ 与 e2e/ 刻意排除
    #: 归一化模式：`UPDATE sign_tasks SET state`
    _STATE_WRITE = "updatesign_taskssetstate"
    #: 归一化模式：领取写入（字面量 claimed）
    _CLAIM_WRITE = _STATE_WRITE + "=claimed"
    #: 允许出现 `UPDATE sign_tasks … SET state` 的**非领取**写点（fail-closed 例外名册）。
    #: 只登记本仓既有的收尾/回收写点：它们写 pending/done/failed，值由调用方给或走 CASE，
    #: 故不是领取写入。新增任何 state 写点都要在此显式登记。
    _ALLOWED_STATE_WRITERS = frozenset({
        ("yiban/store/queue_store.py", "settle_tasks"),    # 收尾：state 由调用方给（done/failed）
        ("yiban/store/queue_store.py", "requeue_task"),    # 重排回落 pending
        ("yiban/store/queue_store.py", "reclaim_tasks"),   # 显式重签：终态翻回 pending
        ("yiban/store/queue_store.py", "reap_abandoned"),  # 死亡执行体名下在飞行回退 pending
    })
    _CLAIM_SITE = ("yiban/store/queue_store.py", "claim_batch")

    @staticmethod
    def _norm(text):
        return "".join(ch for ch in text.lower() if ch not in " \t\r\n'\"")

    @classmethod
    def _fold(cls, node):
        """把表达式折叠成归一化文本；折不动（含变量）返回 None。"""
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return cls._norm(node.value)
        if isinstance(node, ast.JoinedStr):
            parts = []
            for part in node.values:
                folded = cls._fold(part)
                parts.append(folded if folded is not None else "\x00")  # 占位隔断
            return "".join(parts)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left, right = cls._fold(node.left), cls._fold(node.right)
            if left is None or right is None:
                return None
            return left + right                  # 折叠 `+` 拼接，令拆开的常量重连
        return None

    def _state_write_sites(self):
        """返回 {函数键: 归一化 SQL 文本块}，只含出现 `SET state` 写点的函数。

        只看函数**代码**（跳过 docstring），并折叠 `+` 拼接与 f-string 字面段；f-string
        的变量位用 `\\x00` 隔断，避免模式跨占位假配。
        """
        sites = {}
        for top in self._PROD_DIRS:
            root = os.path.join(BASE, top)
            for dirpath, _dirs, files in os.walk(root):
                if os.sep + "test" in dirpath:
                    continue
                for name in files:
                    if not name.endswith(".py"):
                        continue
                    path = os.path.join(dirpath, name)
                    with open(path, encoding="utf-8") as fh:
                        tree = ast.parse(fh.read(), filename=path)
                    rel = os.path.relpath(path, BASE).replace(os.sep, "/")
                    for node in ast.walk(tree):
                        if not isinstance(node, ast.FunctionDef):
                            continue
                        body = node.body
                        if (body and isinstance(body[0], ast.Expr)
                                and isinstance(body[0].value, ast.Constant)
                                and isinstance(body[0].value.value, str)):
                            body = body[1:]                       # 跳过 docstring
                        blob = "\x00".join(
                            folded for folded in
                            (self._fold(sub) for stmt in body for sub in ast.walk(stmt))
                            if folded is not None)
                        if self._STATE_WRITE in blob:
                            sites[(rel, node.name)] = blob
        return sites

    def test_领取写入只有claim_batch一处(self):
        sites = self._state_write_sites()
        claim_sites = {k for k, blob in sites.items() if self._CLAIM_WRITE in blob}
        self.assertEqual(
            claim_sites, {self._CLAIM_SITE},
            "把 state 置为 claimed 的生产写点必须恰是 claim_batch 一处，且为字面量。"
            "参数化成 `SET state=?` 会让它从判定面消失 ⇒ 红；改大小写/空格/引号/拆常量"
            "也都被归一化挡下。实际：%s" % sorted(claim_sites))

    def test_state写点全在受控面内_参数化新写点fail_closed(self):
        sites = self._state_write_sites()
        allowed = set(self._ALLOWED_STATE_WRITERS) | {self._CLAIM_SITE}
        extra = sorted(set(sites) - allowed)
        self.assertEqual(
            extra, [],
            "这些函数写了 sign_tasks 的 state 却不在受控面内：%s。新增 state 写点"
            "（含参数化 `SET state=?`）必须显式登记——值不在判定面内即红。" % extra)
        for key in sorted(self._ALLOWED_STATE_WRITERS):
            self.assertIn(key, sites,
                          "白名单登记了 %s，但它已不再写 state——名册要同步删" % (key,))
            self.assertNotIn(self._CLAIM_WRITE, sites[key],
                             "%s 变成了领取写入（写死 claimed）：领取写点只许 claim_batch" % (key,))


if __name__ == "__main__":
    unittest.main()
