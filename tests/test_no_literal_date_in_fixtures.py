# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""结构守卫：测试夹具不得把**字面日期**当作"当前时刻"。

背景：`tests/test_audit_status_block.py` 的 `_write_snapshot` 曾把数据时刻的默认值写成
字面日期 `"2026-10-13 00:00:07"`。写入当天它是新鲜的；实现侧随后加入"快照超龄降级"
（健康快照超 36 小时即判 `indeterminate`）后，隔一天凡是不显式传 `at` 的用例集体转红
（工单 5f9q）。根因是夹具的"现在"是一个常量，它随墙钟推进必然腐坏。

本门钉一条**不变式**：测试夹具的默认参数里不得出现字面日期/时刻常量。
默认参数是"调用方不传时"的取值，即夹具对运行期真值的替身；把它写成固定日期，就是把
"现在"冻成常量。**任何时刻**这两个事实都不该同时成立：(a) 该默认值被当作"现在"，
(b) 它是常量。本门只认静态可判的那一半——默认参数位置上的字面日期。

判定面（静态 AST，只读源码，不导入被测模块、不取真实时钟）：
· 函数默认参数位置（含仅关键字参数 `kwonlyargs`）上的字符串常量，
  形如 `YYYY-MM-DD` 或 `YYYY-MM-DD HH:MM[:SS]`；
· 函数默认参数位置上的 `datetime(...)` / `date(...)` 构造调用，且全部实参为常量。
命中即红，并报 `文件:行 def 函数名(...参数=…)`。

不在本门内（由审查兜底）：字面日期先赋给变量、再**显式传参**的形态。显式传参可能是
刻意的固定测试数据（例：`now=datetime(2026, 9, 16, 6, 40, 0)` 配合打桩时钟），一律
显式即已声明"这不是现场时钟"，故不一刀切。

立此守卫的理由（AGENTS.md §15）：下一个把夹具默认值改回字面日期的人，本文件必须变红。

标签：G · 门禁：测试夹具时钟卫生
覆盖：`tests/` 与 `e2e/`（存在则含）全部 `.py` 的函数默认参数扫描。
对应实现：无（纯静态守卫）；被守形态见 `tests/test_audit_status_block.py`。
"""
import ast
import os
import re
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
#: 扫描面：全部测试树。`e2e/` 当前不存在，存在时自动纳入。
SCAN_ROOTS = ("tests", "e2e")
#: 字面日期/时刻：`YYYY-MM-DD` 或 `YYYY-MM-DD HH:MM` / `HH:MM:SS`（含 `T` 分隔）。
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2})?)?$")
#: `datetime(...)` / `date(...)` 构造（末段名匹配，兼容 `datetime.datetime`）。
_DT_CTOR_NAMES = {"datetime", "date"}


def _callee_name(call):
    """取调用表达式的末段名（`datetime.datetime(...)` → `datetime`）。"""
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _is_literal_moment(node):
    """该默认值是否为"字面时刻常量"（字符串日期 或 全常量 datetime/date 构造）。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return bool(_DATE_RE.match(node.value))
    if isinstance(node, ast.Call) and _callee_name(node) in _DT_CTOR_NAMES:
        operands = [*node.args, *(kw.value for kw in node.keywords)]
        return bool(operands) and all(isinstance(o, ast.Constant) for o in operands)
    return False


def _default_pairs(fn):
    """产出函数的 (参数名, 默认值节点)——位置默认与仅关键字默认都算。"""
    a = fn.args
    pairs = []
    if a.defaults:
        pairs.extend(zip(a.args[-len(a.defaults):], a.defaults, strict=True))
    pairs.extend((arg, d) for arg, d in zip(a.kwonlyargs, a.kw_defaults, strict=True)
                 if d is not None)
    return pairs


def _scan_source(path):
    """扫一个文件，产出 `(函数名, 参数名, 行号, 默认值源码)` 违例列表。"""
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=path)
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for arg, dflt in _default_pairs(node):
                if _is_literal_moment(dflt):
                    hits.append((node.name, arg.arg, getattr(dflt, "lineno", 0),
                                 ast.unparse(dflt)))
    return hits


class NoLiteralDateFixtureDefaultTest(unittest.TestCase):
    """测试夹具的默认参数不得写死字面日期/时刻。"""

    def test_fixture_defaults_do_not_freeze_now_as_a_literal(self):
        offenders = []
        scanned = 0
        for root in SCAN_ROOTS:
            base = os.path.join(BASE, root)
            for dirpath, _dirnames, filenames in os.walk(base):
                for name in sorted(filenames):
                    if not name.endswith(".py"):
                        continue
                    path = os.path.join(dirpath, name)
                    scanned += 1
                    for fn_name, arg_name, line, src in _scan_source(path):
                        offenders.append(
                            f"{os.path.relpath(path, BASE)}:{line} "
                            f"def {fn_name}(... {arg_name}={src})")
        # 扫描面自证：空扫会让本门静默假绿。
        self.assertGreater(scanned, 0, "测试树为空：守卫会静默空扫")
        self.assertEqual(
            offenders, [],
            "测试夹具的默认参数写死了字面日期/时刻。默认值是夹具对'现在'的替身，"
            "写死即把当前时刻冻成常量，随墙钟推进必然腐坏（工单 5f9q）。"
            "改用 `yiban.clock.now()` 相对取值，或在用例里显式传参。违例：\n  "
            + "\n  ".join(offenders))


if __name__ == "__main__":
    unittest.main()
