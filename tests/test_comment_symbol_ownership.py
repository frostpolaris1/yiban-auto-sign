# -*- coding: utf-8 -*-
"""注释里「本模块的 `X`」必须由本模块定义（工单 mr50 的形状门）。

标签：J · 运维：部署/备份/发布
覆盖：生产代码（`yiban/` / `web/` / `scripts/` / `docker/`）的注释与文档串里，
    每一处「本模块的 `X`」都必须能在本模块找到定义——任意深度的 `def` / `class`，
    或模块级的赋值 / 导入。`__dunder__` 是 Python 协议属性，不是本模块定义的符号。
对应实现：注释与文档串本身（本门不改行为）。
关键断言：注释点名的符号**本模块内外有别**。判据是"归属"不是"存在"：符号在别处
    定义、注释却写成本模块的，按图索骥会在本模块找不到定义，也可能在错误的模块里改
    （来历：`state_io` 的 Lock Delay 旧形态、`executor_v3` 的 `reap_expired` 归属）。
依赖：纯 AST + 文本正则；不 import 被测模块、不联网。
"""
import ast
import os
import re
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCAN_DIRS = ("yiban", "web", "scripts", "docker")

#: 「本模块的 `X`」/「本模块 X」。两种形态都认：反引号包起来的任意标识符，
#: 以及不带反引号但形如代码符号（含下划线）的名字——历史缺陷正是后者
#: （「本模块的 reap_expired」）。中文散文里「本模块的函数」这类泛指不参与判据。
_OWN_CLAIM_RES = (
    re.compile(r"本模块的?\s*`([A-Za-z_][A-Za-z0-9_]*)`"),
    re.compile(r"本模块的?\s*([a-z][a-z0-9]*_[a-z0-9_]+)(?![A-Za-z0-9_])"),
)

#: 不是符号定义、却是合法说法的条目（逐条给理由；AGENTS §28：要么有门，要么显式豁免）。
_ALLOWED_OWN_CLAIMS = {
    ("yiban/adapters/config/env_io.py", "yiban"):
        "logger 通道名（字符串常量值），不是可定义符号",
}


def _defined_names(source):
    """→ 本模块定义的符号名集合（含任意深度的 def/class 与模块级赋值/导入）。"""
    tree = ast.parse(source)
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                for sub in ast.walk(target):
                    if isinstance(sub, ast.Name):
                        names.add(sub.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
    return names


def _own_symbol_violations(source, relpath=""):
    """→ [(行号, 符号)]：注释点名"本模块的 X"但本模块没有定义 X。"""
    defined = _defined_names(source)
    bad = []
    for rx in _OWN_CLAIM_RES:
        for m in rx.finditer(source):
            symbol = m.group(1)
            if symbol.startswith("__") and symbol.endswith("__"):
                continue
            if symbol in defined:
                continue
            if (relpath, symbol) in _ALLOWED_OWN_CLAIMS:
                continue
            bad.append((source[:m.start()].count("\n") + 1, symbol))
    return sorted(set(bad))


class OwnSymbolClaimTest(unittest.TestCase):
    """生产代码里「本模块的 `X`」必须本模块可定义。"""

    def test_own_symbol_claims_resolve_in_module(self):
        offenders = {}
        scanned = 0
        claims = 0
        for sub in SCAN_DIRS:
            for dirpath, dirs, files in os.walk(os.path.join(BASE, sub)):
                dirs[:] = [d for d in dirs if d != "__pycache__"]
                for name in files:
                    if not name.endswith(".py"):
                        continue
                    path = os.path.join(dirpath, name)
                    scanned += 1
                    with open(path, encoding="utf-8", errors="ignore") as f:
                        src = f.read()
                    try:
                        bad = _own_symbol_violations(
                            src, os.path.relpath(path, BASE).replace(os.sep, "/"))
                    except SyntaxError:
                        continue
                    claims += sum(len(rx.findall(src)) for rx in _OWN_CLAIM_RES)
                    if bad:
                        rel = os.path.relpath(path, BASE).replace(os.sep, "/")
                        offenders[rel] = bad
        self.assertGreaterEqual(scanned, 100, "扫描面塌了 ⇒ 零命中不等于合规")
        self.assertGreaterEqual(claims, 10, "「本模块的 `X`」命中面塌了，门会假绿")
        self.assertEqual(
            offenders, {},
            "注释把他人定义/已删符号写成本模块的：按图索骥会在错误的模块里改")

    def test_detector_has_teeth(self):
        """同一份判据对"他人定义"必红、对"本模块定义"放行。"""
        external = '"""Fencing：本模块的 `reap_expired` 负责回收。"""\n'
        self.assertEqual(_own_symbol_violations(external), [(1, "reap_expired")])

        bare = '# 本模块的 reap_expired 负责回收。\n'
        self.assertEqual(_own_symbol_violations(bare), [(1, "reap_expired")])

        local = ('def reap_expired():\n    pass\n'
                 '# 本模块的 `reap_expired` 负责回收。\n')
        self.assertEqual(_own_symbol_violations(local), [])

        nested = ('def create_app():\n'
                  '    def _sensitive_password_gate():\n        pass\n'
                  '    # 本模块的 `_sensitive_password_gate`\n')
        self.assertEqual(_own_symbol_violations(nested), [])

        dunder = '# 本模块的 `__dict__`\n'
        self.assertEqual(_own_symbol_violations(dunder), [])


if __name__ == "__main__":
    unittest.main()
