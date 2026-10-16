# -*- coding: utf-8 -*-
"""并发保真门：线程不得冒充跨进程并发。

标签：A · 判据：并发保真

**本门挡的缺陷形状**：一条用例宣称验证"跨进程 / 跨执行体"的互斥性质，却在**同一个
进程内**用线程（或重导入模块）制造"两个执行体"的错觉。这种用例**测不到它声称的
东西**——两把**进程内锁**先把同进程的并发串行化了。

**根因（两条都必须成立，门才立得住）**：
  ① `yiban/adapters/persistence/connection.py` 的 `_conn_lock` 是模块级 `threading.RLock`；所有
     库读写经它串行化。同进程两线程调 `claim_batch` 因此**排队**，不构成争用。
  ② `yiban/infra/locks.py` 每路径一把进程内 `RLock`（`_LOCKS`），`file_lock` 在
     `_acquire`（真 `flock`）**之前**先取它。同进程两线程取同一路径的锁因此先在
     进程内排队，**根本碰不到 flock 那一维**。
  ⇒ 两条根因任一成立，用线程验"跨进程"就无判别力。本门把两条都写成判据：
     A 组钉两把锁的**形式**（是进程内锁、且在 flock 之前取），并**行为上**证明它们
     把同进程线程串行化；B 组钉**用例面**——宣称跨进程却只造进程内错觉的用例必须
     **被点名**（见 `NAMED_SMOKE`）。

**A 组的牙**：把两把锁之一换成"永远可获取"的假锁，A 组的行为判据转红
（`ConcurrencyFidelityTeethTest` 实测）。
**B 组的牙**：把某条已点名用例从 `NAMED_SMOKE` 摘掉，或往表里塞一条假条目，
`ConcurrencyFidelityGateTest` 转红。

对应实现：yiban/adapters/persistence/connection.py（`_conn_lock`）、yiban/infra/locks.py（`_LOCKS`
   每路径 RLock 与 `file_lock` 的取锁次序）、yiban/adapters/persistence/queue_store.py（`claim_batch`
   在 `_conn_lock` 下执行）。
关键断言：门钉**任何时刻都不该被违反的性质**——"进程内锁把同进程线程串行化"与
   "宣称跨进程的用例要么真起进程、要么被显式点名为冒烟"；不钉任何调用顺序。
依赖：真线程（证进程内串行化）＋ 真源文件 AST（钉取锁次序）；无网络、无子进程。
   非 POSIX 与 Windows 均照跑。
"""
import ast
import os
import threading
import time
import unittest
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS_DIR = os.path.join(BASE, "tests")

# ---- 判据用词表 ----

#: 宣称"跨进程 / 跨执行体"语义的词。只认**用例自身 docstring**里的词，不看类/模块
#: docstring——后者常把"跨进程"当背景交代，按它判定会误伤（实证：`test_locks.py`
#: 的模块 docstring 通篇讲跨进程，而其中有用例明确只测进程内）。
_CLAIM_WORDS = (
    "跨进程", "cross-process", "两个进程", "两进程", "多进程",
    "多执行体", "两个执行体", "两执行体", "独立进程", "不共享锁",
)

#: 真并发机制：独立进程。出现在**模块**里即认为本模块的并发用例可能真起进程
#: （口径：模块级，见报告"未验/保留"——同模块混合时本判据偏宽，宁可漏判不误判）。
_PROCESS_MODULES = (
    "subprocess", "multiprocessing", "ProcessPoolExecutor", "drill_multi_exec",
)

#: 进程内并发错觉：线程。
_THREAD_CALLS = ("Thread", "ThreadPoolExecutor", "Barrier")

#: 已点名的"只是冒烟"用例：nodeid -> 点名理由。
#: 这些用例宣称跨进程语义，但**只在同一个进程内**制造并发错觉，故**不作跨进程判据**。
#: 真跨进程判据在别处（见理由列）。**本表不许留假条目**：条目不再命中判据即转红。
NAMED_SMOKE = {
    # 重导入 `yiban.notify` 模拟"新进程"，但 `yiban.infra.locks` 的每路径 RLock 与
    # 进程内全局**不随重导入复位** ⇒ 模拟出的"第二进程"与第一进程共享进程内锁，
    # 不是真进程。真跨进程账本判据在 `test_notify_ledger.py` 的
    # `test_no_process_lock_concurrent_not_overspent` 之外——见下条与本门 B 组。
    "tests/test_notify_ledger.py::NotifyLedgerDiskTest::test_two_module_instances_share_quota":
        "重导入模块冒充新进程；进程内 RLock 未复位，只作冒烟",
    # 剥掉账本自有的进程内锁，却仍在 `locks.file_lock` 的**每路径 RLock** 下串行化
    # （根因②）⇒ 同进程线程先在进程内排队，碰不到 flock。跨进程不超发**无真载体**。
    "tests/test_notify_ledger.py::test_no_process_lock_concurrent_not_overspent":
        "线程 + 每路径 RLock 遮蔽 flock；跨进程不超发无真载体，只作冒烟",
    # 8 线程冻结时钟抢同标题，宣称"跨进程契约在同进程多线程下同样成立"——但线程被
    # `locks.file_lock` 的每路径 RLock 串行化（根因②），该契约未被真跨进程验证。
    "tests/test_notify_throttle.py::test_concurrent_same_title_only_one_passes":
        "线程被每路径 RLock 串行化；跨进程节流契约无真载体，只作冒烟",
}
# 2026-10-16 饮食批：原表第 4 条
# `tests/test_vs1_claim_atomicity.py::Axis1AtomicityTest::test_两线程并发只有一个赢家`
# 整文件已删（S1 冻结载体：该文件的其余轴钉的是已冻结的 `sign_tasks` 老写口形状，
# 且真跨进程争用判据住在 `tests/test_live_claim_cross_process_contention_e2e.py`）。
# 删掉被点名用例 ⇒ 本门按"不许留假条目"报 stale，属预期红；同批摘掉该条目。


# ---- 源码分析助手 ----
def _dotted(node):
    """把 `a.b.c` 形式的属性访问还原成点串。"""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _uses_call(node, names):
    """node 子树里是否调用了名字末段在 names 里的函数。"""
    for n in ast.walk(node):
        if isinstance(n, ast.Call) and _dotted(n.func).split(".")[-1] in names:
            return True
    return False


def _module_uses_process(tree):
    """模块里是否出现真进程机制（import 或调用）。"""
    for n in ast.walk(tree):
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in n.names] if isinstance(n, ast.Import) else [n.module or ""]
            if any(any(p in (nm or "") for p in _PROCESS_MODULES) for nm in names):
                return True
        if isinstance(n, ast.Call) and _dotted(n.func).split(".")[-1] in ("Popen", "fork"):
            return True
    return False


def _iter_test_defs(tree):
    """产出 (限定名, 函数节点)：类内 test 方法与模块级 test 函数。"""
    for cls in [n for n in tree.body if isinstance(n, ast.ClassDef)]:
        for m in cls.body:
            if isinstance(m, ast.FunctionDef) and m.name.startswith("test"):
                yield f"{cls.name}::{m.name}", m
    for m in tree.body:
        if isinstance(m, ast.FunctionDef) and m.name.startswith("test"):
            yield m.name, m


def find_thread_impersonators():
    """扫 tests/，返回"线程冒充跨进程"的用例 nodeid 集合。

    判据（三条同时成立）：
      1. 用例**自身 docstring** 含跨进程/跨执行体语义词；
      2. 用例正文**没有**独立进程机制（模块级也不 import 进程机制）；
      3. 用例在进程内制造并发错觉：起线程，或操纵 `sys.modules` 模拟"新进程"。
    """
    found = set()
    for dirpath, _dirs, files in os.walk(TESTS_DIR):
        for fn in sorted(files):
            if not (fn.startswith("test_") and fn.endswith(".py")):
                continue
            path = os.path.join(dirpath, fn)
            with open(path, encoding="utf-8") as f:
                src = f.read()
            try:
                tree = ast.parse(src)
            except SyntaxError:
                continue
            if _module_uses_process(tree):
                continue
            rel = os.path.relpath(path, BASE).replace("\\", "/")
            for qual, node in _iter_test_defs(tree):
                doc = ast.get_docstring(node) or ""
                if not any(w in doc for w in _CLAIM_WORDS):
                    continue
                body = ast.get_source_segment(src, node) or ""
                threads = _uses_call(node, _THREAD_CALLS)
                reimport = "sys.modules" in body
                if threads or reimport:
                    found.add(f"{rel}::{qual}")
    return found


def gate_violations(found, registry):
    """给定"实际命中集"与"点名表"，返回两类违规。"""
    unregistered = sorted(found - set(registry))
    stale = sorted(set(registry) - found)
    return {"unregistered": unregistered, "stale": stale}


# ---- A 组：两把进程内锁的根因前提 ----
def _max_concurrent_holders(lock, n=4, hold_sec=0.05):
    """n 个线程各在 `lock` 保护下停留 hold_sec，返回同时在临界区内的最大线程数。

    真互斥锁（RLock/Lock）恒返回 1；"永远可获取"的假锁返回接近 n。
    """
    active = [0]
    peak = [0]
    guard = threading.Lock()
    start = threading.Barrier(n)

    def worker():
        start.wait()
        with lock:
            with guard:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            time.sleep(hold_sec)
            with guard:
                active[0] -= 1

    ts = [threading.Thread(target=worker) for _ in range(n)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(10)
    return peak[0]


class _AlwaysAcquireLock:
    """假锁：获取即放行，不互斥。用于证明 A 组判据有牙。"""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def acquire(self, *a, **k):
        return True

    def release(self, *a, **k):
        return None


class InProcessLockPremiseTest(unittest.TestCase):
    """根因①：`connection._conn_lock` 是进程内锁，把同进程线程串行化。"""

    def test_conn_lock_is_in_process_rlock(self):
        from yiban.store import connection
        self.assertIsInstance(
            connection._conn_lock, type(threading.RLock()),
            "`_conn_lock` 必须是进程内 RLock：换成任何不串行化同进程线程的锁，"
            "用线程验并发就假绿（这正是本门要挡的形状）")

    def test_conn_lock_serializes_same_process_threads(self):
        from yiban.store import connection
        peak = _max_concurrent_holders(connection._conn_lock)
        self.assertEqual(
            peak, 1,
            "`_conn_lock` 未把同进程线程串行化（同时进入临界区=%d）——"
            "此时线程并发不再被它遮蔽，本门的判定前提需重估" % peak)


class PerPathRlockPremiseTest(unittest.TestCase):
    """根因②：`locks.py` 每路径 RLock 在 flock 之前取，遮蔽同进程线程。"""

    def _locks_source(self):
        with open(os.path.join(BASE, "yiban", "infra", "locks.py"), encoding="utf-8") as f:
            return f.read()

    def test_per_path_rlock_is_taken_before_flock(self):
        src = self._locks_source()
        tree = ast.parse(src)
        file_lock = next(n for n in ast.walk(tree)
                         if isinstance(n, ast.FunctionDef) and n.name == "file_lock")
        get_rlock_line = min(
            (n.lineno for n in ast.walk(file_lock)
             if isinstance(n, ast.Call) and _dotted(n.func) == "_get_rlock"), default=None)
        acquire_line = min(
            (n.lineno for n in ast.walk(file_lock)
             if isinstance(n, ast.Call) and _dotted(n.func) == "_acquire"), default=None)
        self.assertIsNotNone(get_rlock_line, "`file_lock` 必须先取每路径 RLock（找不到 `_get_rlock`）")
        self.assertIsNotNone(acquire_line, "`file_lock` 必须取真 flock（找不到 `_acquire`）")
        self.assertLess(
            get_rlock_line, acquire_line,
            "每路径 RLock 必须在 flock **之前**取（`_get_rlock` 行 %s 应小于 `_acquire` 行 %s）："
            "次序一反过来，同进程线程就能碰到 flock，用线程验文件锁跨进程互斥的前提就不成立"
            % (get_rlock_line, acquire_line))

    def test_per_path_rlock_serializes_same_process_threads(self):
        from yiban.infra import locks
        path = os.path.join(BASE, "tests", "_nonexistent_for_gate.lockpath")
        peak = _max_concurrent_holders(locks._get_rlock(path))
        self.assertEqual(
            peak, 1,
            "每路径 RLock 未把同进程线程串行化（同时进入=%d）——本门判定前提需重估" % peak)


class ConcurrencyFidelityGateTest(unittest.TestCase):
    """B 组：宣称跨进程却只造进程内错觉的用例，必须被点名。"""

    def test_no_unregistered_thread_impersonator(self):
        found = find_thread_impersonators()
        viol = gate_violations(found, NAMED_SMOKE)
        self.assertEqual(
            viol["unregistered"], [],
            "以下用例宣称跨进程语义，却只在同进程内造并发错觉，且未点名——"
            "它们测不到声称的东西（根因见本文件模块头 ①②）。"
            "要么改用独立连接/独立进程，要么登记进 NAMED_SMOKE 点名'只是冒烟'：\n  %s"
            % "\n  ".join(viol["unregistered"]))

    def test_no_stale_smoke_label(self):
        found = find_thread_impersonators()
        viol = gate_violations(found, NAMED_SMOKE)
        self.assertEqual(
            viol["stale"], [],
            "NAMED_SMOKE 里有条目已不再命中判据（可能已改成真并发，或判据已变）——"
            "陈旧的点名会让门只看一半，必须同批订正：\n  %s" % "\n  ".join(viol["stale"]))

    def test_drill_e2e_is_recognized_as_real_process_concurrency(self):
        """现役唯一真子进程并发覆盖（靶场 e2e）不得被本门误判成"只是冒烟"。

        靶场 e2e 真拉起监督进程与执行体子进程，故它是**真并发**。本门按"模块是否
        起独立进程"判真伪；这条正面钉住该判定在靶场文件上成立，免得日后判据收紧
        时把唯一的真覆盖一起误杀。
        """
        drill_path = os.path.join(TESTS_DIR, "test_multi_executor_drill_e2e.py")
        with open(drill_path, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        self.assertTrue(
            _module_uses_process(tree),
            "靶场 e2e 必须被识别为'起独立进程'——否则本门会把它误判成冒烟，"
            "丢掉现役唯一的真并发覆盖")
        self.assertFalse(
            any("test_multi_executor_drill_e2e" in n for n in find_thread_impersonators()),
            "靶场 e2e 不得出现在'线程冒充跨进程'命中集里")


class ConcurrencyFidelityTeethTest(unittest.TestCase):
    """牙：证明两组判据都不是恒绿摆设。"""

    def test_premise_has_teeth_when_conn_lock_made_reentrant(self):
        from yiban.store import connection
        with mock.patch.object(connection, "_conn_lock", _AlwaysAcquireLock()):
            peak = _max_concurrent_holders(connection._conn_lock)
        self.assertGreater(
            peak, 1,
            "把 `_conn_lock` 换成'永远可获取'的假锁后，A 组行为判据仍报同时进入=1——"
            "说明该判据测不到串行化，是恒绿摆设")

    def test_premise_has_teeth_when_per_path_rlock_made_reentrant(self):
        from yiban.infra import locks
        with mock.patch.object(locks, "_get_rlock", lambda _p: _AlwaysAcquireLock()):
            peak = _max_concurrent_holders(locks._get_rlock("/tmp/x"))
        self.assertGreater(
            peak, 1,
            "把每路径 RLock 换成'永远可获取'的假锁后，A 组行为判据仍报同时进入=1——"
            "说明该判据是恒绿摆设")

    def test_gate_has_teeth_when_an_offender_is_unregistered(self):
        found = find_thread_impersonators()
        self.assertTrue(found, "本仓至少有一条线程冒充跨进程的用例；找不到说明判据已失效")
        victim = sorted(found)[0]
        registry = {k: v for k, v in NAMED_SMOKE.items() if k != victim}
        self.assertIn(victim, gate_violations(found, registry)["unregistered"],
                      "摘掉一条已点名用例后，门未把它判成未登记——门对'新出现同类用例'无牙")

    def test_gate_has_teeth_when_label_is_stale(self):
        found = find_thread_impersonators()
        registry = dict(NAMED_SMOKE)
        registry["tests/test_does_not_exist.py::GhostTest::test_ghost"] = "假条目"
        self.assertIn("tests/test_does_not_exist.py::GhostTest::test_ghost",
                      gate_violations(found, registry)["stale"],
                      "往点名表塞一条假条目后，门未判陈旧——门会长期只看一半")


if __name__ == "__main__":
    unittest.main(verbosity=2)
