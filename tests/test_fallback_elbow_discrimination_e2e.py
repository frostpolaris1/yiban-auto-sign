# -*- coding: utf-8 -*-
"""兜底腿肘界的**取值大小**门：让位机制必须有行为判别力（140 §六-2）。

标签：B · 调度：领取/队列/执行体

**本门补哪一维**：`docs/refactor/140` §五 S4 的变异对照已证死——把
`yiban/engine/workers.py` 兜底腿接线里的 `elbow_sec=executor_v3.FALLBACK_ELBOW_SEC`
改成 `elbow_sec=0.0`，四个肘界族文件（`test_claim_dispatch_elbow_e2e.py` 19 passed、
`test_fallback_gates.py` 27 passed、`test_elbow_predicate.py` 16 passed、
`test_elbow_shadow.py` 9 passed）**全绿**。⇒ 肘界取值大小在整个时序族里**无判别力**，
族里只钉了"关键字在场"。让位机制破了不红。

**本门钉的性质（一句话）**：兜底腿**结构上**领不走"计划轮此刻该领的新鲜工作"——
新鲜行必须留给计划轮；只有"已过期超过肘界"的行才归兜底。

**两半判据，缺一不可**（140 §九 批次三的"配对声明"形态）：
  · **行为半**：真库 ＋ 真执行体。兜底身份跑一轮，种子有新鲜行与过期行两条；
    必须**只**领走过期那条。同时给一条**对照**：同一份新鲜行，计划轮身份必须领得走
    ——否则"兜底没领"可能只是"这条行谁也领不到"，判据就没牙。
  · **静态半**：生产兜底接线（`workers.py` 里 `claim_all=True` 的那次
    `run_executor_v3` 调用）必须把 `elbow_sec` 传成 `executor_v3.FALLBACK_ELBOW_SEC`，
    且该常量 > 0。行为半证明"这个值有判别力"，静态半证明"接线真的用了这个值"。
    **两半合起来**才等价于"让位机制在当前代码里成立"——只有一半时，改接线仍可静默。

**牙**：把 `elbow_sec` 传成 `0.0`（普查用的同一条变异）⇒ 行为半的新鲜行被兜底领走 ⇒ 红；
把接线改成 `elbow_sec=0.0` ⇒ 静态半红；把 `FALLBACK_ELBOW_SEC` 改成 0 ⇒ 两半都红。

对应实现：yiban/engine/workers.py（兜底常驻的 `run_executor_v3` 调用）、
    yiban/engine/executor_v3.py（`FALLBACK_ELBOW_SEC`、`_claim_one` 的肘界下推）、
    yiban/store/queue_store.py（`claim_batch` 的 `run_at <= now - elbow` 判据）、
    yiban/core/elbow.py（谓词的唯一事实源）。
关键断言：钉"新鲜工作不被兜底领走""计划轮领得走新鲜工作""接线用的是那个常量"；
    不钉任何调用顺序。
依赖：临时 sqlite ＋ 真执行体（靶场见 `tests/_live_executor_harness.py`）；
    上游与限速打成恒成功/恒放行，无网络、无子进程。整文件在本机执行，无 skip。
"""
import ast
import os
import shutil
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from _live_executor_harness import (  # noqa: E402
    DAY,
    OWNER,
    PHONE,
    PHONE2,
    START,
    Harness,
)

from yiban.engine import executor_v3  # noqa: E402
from yiban.store import queue_store  # noqa: E402

#: 新鲜行的 `run_at` 偏移（秒，相对假时钟）：刚到点，计划轮该领的就是它。
FRESH_OFFSET = -1
#: 过期行的偏移：比肘界再早 60 秒，故"已过期超过肘界"成立。
STALE_OFFSET = -(executor_v3.FALLBACK_ELBOW_SEC + 60)

WORKERS_PY = os.path.join(BASE, "yiban", "engine", "workers.py")


def _fallback_claim_elbow_arg(path=WORKERS_PY):
    """取生产兜底接线里 `run_executor_v3(..., claim_all=True, ...)` 的 `elbow_sec` 实参。

    返回归一化后的实参文本（点串，如 `executor_v3.FALLBACK_ELBOW_SEC`）；找不到返回 None。
    判定锚在 `claim_all=True` 上：那是兜底腿的**身份**（`run_fallback` 的循环体），
    不是某个函数名。
    """
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=path)
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name != "run_executor_v3":
            continue
        kwargs = {kw.arg: kw.value for kw in node.keywords if kw.arg}
        claim_all = kwargs.get("claim_all")
        if not (isinstance(claim_all, ast.Constant) and claim_all.value is True):
            continue
        found.append(kwargs.get("elbow_sec"))
    if len(found) != 1:
        return None, len(found)
    return _dotted(found[0]), 1


def _dotted(node):
    """把 `a.b.c` 形式的属性访问还原成点串；非属性/名字返回 None。"""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts)) if parts else None


class FallbackElbowValueTest(unittest.TestCase):
    """行为半之一：**肘界取值本身**必须把新鲜行挡在门外（同一时刻、同一行）。

    这是普查 §六-2 的直接落点：族里原有的用例只钉"关键字在场"，取值大小无判别力。
    本用例不跑整轮（整轮会推进假时钟，让"新鲜行"随时间自然变旧），只在**一个时刻**
    上对同一行问两次——这正是肘界判据的定义域。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-elbowval-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_肘界值把新鲜行挡在门外(self):
        with Harness(self.tmp) as h:
            h.add_task(phone=PHONE, state="pending", run_at_offset=FRESH_OFFSET)
            now = h.fc.stamp()
            got = queue_store.claim_batch(OWNER, DAY, (0,), now=now,
                                          elbow_sec=executor_v3.FALLBACK_ELBOW_SEC)
            self.assertEqual(
                got, [],
                "刚到点的行必须被兜底肘界挡住（它是计划轮的活）。实际领到 %r" % (got,))
            self.assertEqual(h.row(PHONE)["state"], "pending",
                             "被挡住的行必须原样留在 pending")

    def test_肘界值放行过期行(self):
        with Harness(self.tmp) as h:
            h.add_task(phone=PHONE, state="pending", run_at_offset=STALE_OFFSET)
            now = h.fc.stamp()
            got = queue_store.claim_batch(OWNER, DAY, (0,), now=now,
                                          elbow_sec=executor_v3.FALLBACK_ELBOW_SEC)
            self.assertEqual(
                len(got), 1,
                "已过期超过肘界的行必须能被兜底领走——否则让位机制把活全丢了"
                "（漏签），实际 %r" % (got,))

    def test_牙_肘界置零则同一时刻放行新鲜行(self):
        """牙：同一行、同一时刻，`elbow_sec=0.0` ⇒ 领得走。

        证明上面两条断言真的在判**取值**，而不是在判"这一行谁也领不到"。
        """
        with Harness(self.tmp) as h:
            h.add_task(phone=PHONE, state="pending", run_at_offset=FRESH_OFFSET)
            now = h.fc.stamp()
            got = queue_store.claim_batch(OWNER, DAY, (0,), now=now, elbow_sec=0.0)
            self.assertEqual(
                len(got), 1,
                "肘界置零后必须能领走新鲜行——若它仍领不走，上面那条'挡在门外'的"
                "断言测不到肘界取值，是恒绿摆设")


class FallbackElbowBehaviorTest(unittest.TestCase):
    """行为半之二：整轮里，兜底腿**在肘界到期之前**不领新鲜行。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-elbow-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_兜底腿在肘界到期前不领新鲜行(self):
        """种子：一条新鲜行（计划轮的活）＋ 一条过期行（兜底的活）。

        兜底是**常驻无界循环**：它会一直重扫，直到新鲜行也过了肘界才接手——那是
        让位机制的正常结果（否则新鲜工作永远没人做）。故判据不是"永不领新鲜行"，
        而是**次序与时刻**：先领过期行；新鲜行若被领，必发生在肘界到期之后。
        """
        with Harness(self.tmp) as h:
            h.add_task(phone=PHONE2, state="pending", run_at_offset=FRESH_OFFSET)
            h.add_task(phone=PHONE, state="pending", run_at_offset=STALE_OFFSET)
            h.run_entry("fallback", phones=(PHONE, PHONE2))
            self.assertGreaterEqual(
                len(h.claim_statements()), 1,
                "前置就绪：兜底腿本轮必须真的发出过领取语句，否则本用例是空转")
            self.assertEqual(
                h.attempted[0], PHONE,
                "兜底腿必须**先**领那条已过期超过肘界的行。实际执行顺序：%r"
                % ([p for p, _t in h.attempt_log],))
            fresh_times = [t for p, t in h.attempt_log if p == PHONE2]
            for t in fresh_times:
                self.assertGreaterEqual(
                    (t - START).total_seconds(), executor_v3.FALLBACK_ELBOW_SEC,
                    "新鲜行只允许在**肘界到期之后**才被兜底接手，实际在 +%ss 就领了"
                    "（肘界 %ss）：让位机制失效＝兜底抢走计划轮此刻该领的新鲜工作"
                    % ((t - START).total_seconds(), executor_v3.FALLBACK_ELBOW_SEC))

    def test_计划轮领得走同一份新鲜行(self):
        """对照：同一条新鲜行，计划轮身份必须领得走。

        没有这条对照，"兜底没领"就可能只是"这条行谁也领不到"（例如 run_at 域写错、
        分片集没覆盖），判据会变成恒绿。
        """
        with Harness(self.tmp) as h:
            h.add_task(phone=PHONE, state="pending", run_at_offset=FRESH_OFFSET)
            h.run_entry("scheduler")
            self.assertGreaterEqual(len(h.claim_statements()), 1,
                                    "前置就绪：计划轮本轮必须真的发出过领取语句")
            self.assertEqual(
                h.attempted, [PHONE],
                "计划轮必须领得走刚到点的行（肘界 0）。实际执行：%r" % (h.attempted,))
            self.assertEqual(h.row(PHONE)["state"], "done")

    def test_牙_肘界置零则新鲜工作被兜底领走(self):
        """牙：把兜底的 `elbow_sec` 传成 `0.0`（普查用的同一条变异）⇒ 新鲜行被领走。

        证明上面那条断言真的在判**肘界取值**，而不是在判"兜底什么都不领"。
        """
        with Harness(self.tmp) as h:
            h.add_task(phone=PHONE, state="pending", run_at_offset=FRESH_OFFSET)
            h.run_entry("fallback", overrides={"elbow_sec": 0.0})
            self.assertEqual(
                h.attempted, [PHONE],
                "肘界置零后，兜底必须能领走新鲜行——若它仍领不走，说明上面那条"
                "'兜底领不走新鲜工作'的断言测不到肘界取值，是恒绿摆设")


class FallbackElbowWiringTest(unittest.TestCase):
    """静态半：生产兜底接线必须把肘界传成那个常量，且该常量 > 0。"""

    def test_兜底接线传的是肘界常量(self):
        arg, count = _fallback_claim_elbow_arg()
        self.assertEqual(
            count, 1,
            "`workers.py` 里必须恰有一次 `claim_all=True` 的 `run_executor_v3` 调用"
            "（兜底腿的身份）。找不到或找到多次，本判据的前提就不成立——"
            "此时判红而不是放过（fail-closed）")
        self.assertEqual(
            arg, "executor_v3.FALLBACK_ELBOW_SEC",
            "兜底接线的 `elbow_sec` 必须是 `executor_v3.FALLBACK_ELBOW_SEC`，实际 %r。"
            "改成字面量 0.0 等于取消让位机制：兜底会抢走计划轮此刻该领的新鲜工作"
            "（10-07 型启动竞态）" % (arg,))

    def test_肘界常量大于零(self):
        self.assertGreater(
            executor_v3.FALLBACK_ELBOW_SEC, 0,
            "`FALLBACK_ELBOW_SEC` 必须 > 0：它一归零，兜底与计划轮就没有让位差，"
            "两者会在同一拍抢同一批行")

    def test_静态半有牙(self):
        """把一份"接线写成 0.0"的合成源码喂进同一判定函数：必须读出 0.0。"""
        src = (
            "def run_fallback(accounts):\n"
            "    results = executor_v3.run_executor_v3(\n"
            "        accounts, claim_all=True, requeue_during_run=True,\n"
            "        elbow_sec=0.0)\n"
            "    return results\n"
        )
        tree = ast.parse(src)
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                 and getattr(n.func, "attr", "") == "run_executor_v3"]
        self.assertEqual(len(calls), 1)
        kw = {k.arg: k.value for k in calls[0].keywords}
        self.assertIsInstance(kw.get("elbow_sec"), ast.Constant)
        self.assertEqual(kw["elbow_sec"].value, 0.0)
        self.assertNotEqual(_dotted(kw["elbow_sec"]), "executor_v3.FALLBACK_ELBOW_SEC",
                            "判定函数对'接线写成字面量 0.0'无牙")


if __name__ == "__main__":
    unittest.main(verbosity=2)
