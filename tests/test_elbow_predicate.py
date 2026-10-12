# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""领取肘界谓词（`yiban/core/elbow.py`）的不变量与边界守卫。

标签：B · 调度与领取
覆盖：
  ① **纯函数纪律**：模块内不 import 任何时钟/随机/I-O 模块（AST 静态扫钉死）；
     也不调用 `now` / `time` / `monotonic` 一类取时函数（时钟必须由调用方注入）。
  ② **时钟注入**：判定只取形参 `now`，同一输入必得同一输出，且无跨调用状态。
  ③ **边界四条**：恰等于肘界（闭区间）、到期在未来、肘界为 0、时钟回退。
  ④ **肘界为负**：算术有定义（非合法角色配置，如实判定，不夹取）。
  ⑤ **突变靶**：边界比较符（`<=` ↔ `<`）与肘界加法（`-` ↔ `+`）各一处。
     把它们改坏，对应断言必须变红——这是本文件存在的直接理由。
对应实现：`yiban/core/elbow.py`（`ClaimRow` / `ClaimRole` / `may_claim`）。
关键断言：AST 守卫是"时钟注入"这条硬约束的机械判据；边界用例分别钉住闭区间与加法方向。
"""
import ast
import dataclasses
import io
import os
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODULE = os.path.join(BASE, "yiban", "core", "elbow.py")

#: 允许出现在核心谓词模块里的顶层导入。任何时钟/随机/I-O 来源都不在白名单内。
ALLOWED_IMPORTS = {"__future__", "dataclasses", "typing"}

#: 取时函数的属性名：模块内出现它们的调用即视为"自己取时钟"（禁止）。
CLOCK_ATTRS = {"now", "utcnow", "today", "time", "monotonic", "perf_counter",
               "time_ns", "monotonic_ns"}

from yiban.core.elbow import ClaimRole, ClaimRow, may_claim  # noqa: E402  (先写测试，模块后建)


def _parse_module():
    with io.open(MODULE, encoding="utf-8") as f:
        return ast.parse(f.read(), filename=MODULE)


def _toplevel_imports(tree):
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            mod = (node.module or "").split(".")[0]
            if mod:
                names.add(mod)
    return names


class PurityTest(unittest.TestCase):
    """① 纯函数纪律：AST 静态扫，钉死"不取时钟、不碰 I-O"。"""

    def setUp(self):
        self.assertTrue(os.path.isfile(MODULE), f"核心谓词模块不存在：{MODULE}")
        self.tree = _parse_module()

    def test_only_whitelisted_imports(self):
        got = _toplevel_imports(self.tree)
        extra = sorted(got - ALLOWED_IMPORTS)
        self.assertEqual(extra, [],
                         f"核心谓词只许导入 {sorted(ALLOWED_IMPORTS)}；多出 {extra}。"
                         "新导入必须先在白名单里说明它为什么不是时钟/随机/I-O 来源")

    def test_imports_no_clock_or_io_module(self):
        got = _toplevel_imports(self.tree)
        for banned in ("time", "datetime", "random", "os", "io", "socket",
                       "subprocess", "pathlib", "sys", "secrets", "uuid"):
            with self.subTest(module=banned):
                self.assertNotIn(banned, got,
                                 f"核心谓词不得 import {banned}（时钟/随机/I-O 来源）")

    def test_calls_no_clock_function(self):
        hits = []
        for node in ast.walk(self.tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in CLOCK_ATTRS):
                hits.append(node.func.attr)
        self.assertEqual(hits, [],
                         f"核心谓词不得自己取时钟（命中的调用：{hits}）——"
                         "时钟必须由调用方以形参 `now` 注入")


class InjectedClockTest(unittest.TestCase):
    """② 时钟注入 + 无状态：同一输入必得同一输出。"""

    def test_same_input_same_output(self):
        row, role = ClaimRow(run_at=100.0), ClaimRole(elbow_sec=10.0)
        self.assertEqual([may_claim(row, role, 130.0) for _ in range(3)],
                         [True, True, True], "同一输入必须必得同一输出（纯函数）")

    def test_no_cross_call_state(self):
        row, role = ClaimRow(run_at=100.0), ClaimRole(elbow_sec=10.0)
        self.assertTrue(may_claim(row, role, 130.0))
        self.assertFalse(may_claim(row, role, 105.0))
        # 回到同一 now 必须复现同一结论：无隐藏状态。
        self.assertTrue(may_claim(row, role, 130.0))


class BoundaryTest(unittest.TestCase):
    """③④ 边界四条 + 负肘界。"""

    def test_exactly_at_elbow_is_claimable(self):
        # run_at == now - elbow ⇒ 可领（闭区间）。改 `<=` 为 `<` 即红。
        self.assertTrue(may_claim(ClaimRow(run_at=100.0), ClaimRole(elbow_sec=40.0),
                                  now=140.0))

    def test_just_inside_elbow_is_not_claimable(self):
        # run_at 比肘界线晚 ε ⇒ 不可领。
        self.assertFalse(may_claim(ClaimRow(run_at=100.0 + 1e-6),
                                   ClaimRole(elbow_sec=40.0), now=140.0))

    def test_just_past_elbow_is_claimable(self):
        self.assertTrue(may_claim(ClaimRow(run_at=100.0 - 1e-6),
                                  ClaimRole(elbow_sec=40.0), now=140.0))

    def test_future_due_is_not_claimable(self):
        # 到期在未来 ⇒ 任何非负肘界下都不可领。改 `-` 为 `+` 即红（会提前领）。
        for elbow in (0.0, 1.0, 120.0):
            with self.subTest(elbow=elbow):
                self.assertFalse(may_claim(ClaimRow(run_at=200.0),
                                           ClaimRole(elbow_sec=elbow), now=100.0))

    def test_zero_elbow_is_due_test(self):
        # 肘界 0 = 定时执行体的语义：到期即可领。
        role = ClaimRole(elbow_sec=0.0)
        self.assertTrue(may_claim(ClaimRow(run_at=100.0), role, now=100.0))
        self.assertTrue(may_claim(ClaimRow(run_at=99.0), role, now=100.0))
        self.assertFalse(may_claim(ClaimRow(run_at=100.001), role, now=100.0))

    def test_default_role_elbow_is_zero(self):
        self.assertEqual(ClaimRole().elbow_sec, 0.0,
                         "角色默认肘界必须是 0（默认角色＝定时执行体，到期即领）")

    def test_clock_backwards_withholds_claim(self):
        # 时钟回退只会收回可领结论（fail-closed），绝不提前授予。
        row, role = ClaimRow(run_at=100.0), ClaimRole(elbow_sec=10.0)
        self.assertTrue(may_claim(row, role, 110.0))
        self.assertFalse(may_claim(row, role, 109.0))

    def test_negative_elbow_inverts_margin(self):
        # 负肘界不是合法角色配置；算术仍有定义（= 允许领到期前 |肘界| 秒的行）。
        role = ClaimRole(elbow_sec=-5.0)
        self.assertTrue(may_claim(ClaimRow(run_at=105.0), role, now=100.0))
        self.assertFalse(may_claim(ClaimRow(run_at=105.0001), role, now=100.0))

    def test_role_value_is_frozen(self):
        # 角色是冻结值对象：不可变，判定无从被就地改写。
        with self.assertRaises(dataclasses.FrozenInstanceError):
            ClaimRole(elbow_sec=1.0).elbow_sec = 2.0  # type: ignore[misc]


class SeamTest(unittest.TestCase):
    """给未来 scope/tier 维留的接缝：调用点用值对象，加维不改调用形态。"""

    def test_call_site_shape_is_role_object(self):
        # 调用点传 (行, 角色, now)：未来角色对象加 scope/tier 字段，调用点不改造型。
        row = ClaimRow(run_at=50.0)
        role = ClaimRole(elbow_sec=0.0)
        self.assertIsInstance(may_claim(row, role, 60.0), bool)

    def test_row_and_role_are_value_objects(self):
        self.assertTrue(dataclasses.is_dataclass(ClaimRow(run_at=0.0)))
        self.assertTrue(dataclasses.is_dataclass(ClaimRole()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
