# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""跳过显性化门：跳过面必须有门，"跳过 ≠ 绿"。

旧 Python 栈退役后，本门只扫描 tests/ 下剩下的新栈门。判据：
  ① 每个 (文件, 标记, 理由) 三元组必须在 KNOWN_SKIP_SITES 登记；
  ② 表里不得有陈旧条目（跳过点已消失）；
  ③ 表里不得有未登记的新跳过。
"""
import ast
import io
import os
import unittest
from collections import Counter

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS_DIR = os.path.join(BASE, "tests")
DYNAMIC = "\x00DYNAMIC"

_SKIP_MARKERS = frozenset({
    "skipUnless", "skipIf", "skipif", "skip", "skipTest", "SkipTest", "importorskip",
})

KNOWN_SKIP_SITES = {
    "tests/test_dev_verify_entry.py": [
        ["skipUnless", "需要 bash：--help 要真跑入口脚本"],
        ["skipUnless", "需要 bash：--log-dir 要真跑入口脚本"],
        ["skipUnless", "需要 bash：--log-dir 要真跑入口脚本"],
    ],
    "tests/test_deploy_e2e_wiring.py": [
        ["skipTest", "bash 不在 PATH：部署 e2e 脚本语法检查跳过（CI/WSL 有 bash）"],
        ["skipTest", "bash 不在 PATH：更新演练脚本语法检查跳过（CI/WSL 有 bash）"],
    ],
    "tests/test_git_config_hygiene.py": [
        ["skipUnless", "bash 不可用：配置卫生门无法就地跑"],
        ["skipUnless", "bash 不可用：配置卫生门无法就地跑"],
        ["skipUnless", "bash 不可用：配置卫生门无法就地跑"],
        ["skipUnless", "bash 不可用：配置卫生门无法就地跑"],
        ["skipUnless", "bash 不可用：配置卫生门无法就地跑"],
        ["skipUnless", "bash 不可用：配置卫生门无法就地跑"],
        ["skipUnless", "bash 不可用：配置卫生门无法就地跑"],
    ],
    "tests/test_pr_template_gate.py": [
        ["skipUnless", "git 不可用：跳过 PR 模板守卫"],
    ],
    "tests/test_private_key_gate.py": [
        ["skipTest", "git 不可用（worktree + WSL 常见）；私钥门 DID NOT run，不算绿"],
        ["skipTest", "git 不可用（check-ignore 失败）；忽略规则 DID NOT verify"],
        ["skipTest", "需要 git 创建临时仓库；私钥门自证 DID NOT run"],
        ["skipTest", "git 不可用（check-ignore 失败）；忽略规则 DID NOT verify"],
    ],
    "tests/test_ui_signing_wiring.py": [
        ["skipTest", "SKIP: 没有 bash，UI 清单无法真跑"],
        ["skipTest", "SKIP: 没有 sha256sum，清单无法生成/校验"],
        ["skipTest", "SKIP: 没有 bash"],
        ["skipTest", "SKIP: 没有 sha256sum"],
        ["skipTest", "SKIP: 没有 bash"],
        ["skipTest", "SKIP: 没有 git，无法构造发布检出"],
        ["skipTest", "no bash"],
        ["skipTest", DYNAMIC],
        ["skipTest", DYNAMIC],
        ["skipTest", DYNAMIC],
    ],
    "tests/test_workflow_security_gate.py": [
        ["skipTest", "bash 不在 PATH"],
        ["skipTest", "bash 不在 PATH"],
    ],
}



def _reason(call):
    for kw in call.keywords:
        if (
            kw.arg == "reason"
            and isinstance(kw.value, ast.Constant)
            and isinstance(kw.value.value, str)
        ):
            return kw.value.value
    for arg in reversed(call.args):
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            return arg.value
    return DYNAMIC


def _bare_skip_name(dec):
    """裸跳过装饰器名；带括号的调用节点不在此列。"""
    if isinstance(dec, ast.Name):
        return dec.id
    if not isinstance(dec, ast.Attribute):
        return None
    if isinstance(dec.value, ast.Name) and dec.value.id in {"unittest", "pytest"}:
        return dec.attr
    if (isinstance(dec.value, ast.Attribute)
            and dec.value.attr == "mark"
            and isinstance(dec.value.value, ast.Name)
            and dec.value.value.id == "pytest"):
        return dec.attr
    return None


def _missing_entries(actual, allowed):
    """actual 中未登记的多重集差；重复条目必须逐条登记。"""
    return list((Counter(map(tuple, actual)) - Counter(map(tuple, allowed))).elements())


def _stale_entries(actual, allowed):
    """allowed 中已不存在的多重集差。"""
    return list((Counter(map(tuple, allowed)) - Counter(map(tuple, actual))).elements())


def _iter_sources():
    """tests/ 下全部 Python 文件；子目录也算，扫描面不得静默缩小。"""
    for root, _dirs, files in os.walk(TESTS_DIR):
        for name in sorted(files):
            if name.endswith(".py"):
                yield os.path.join(root, name)


def scan_tests():
    """返回 {相对路径: [[marker, reason], ...]}（按文件去重、保持出现顺序）。"""
    found = {}
    for path in sorted(_iter_sources()):
        rel = os.path.relpath(path, BASE).replace(os.sep, "/")
        tree = ast.parse(io.open(path, encoding="utf-8").read(), filename=path)
        items = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            marker = func.attr if isinstance(func, ast.Attribute) else (
                func.id if isinstance(func, ast.Name) else "")
            if marker not in _SKIP_MARKERS:
                continue
            items.append([marker, _reason(node)])
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            for dec in node.decorator_list:
                marker = _bare_skip_name(dec)
                if marker in _SKIP_MARKERS:
                    items.append([marker, DYNAMIC])
        if items:
            found[rel] = items
    return found


class SkipSurfaceRegisteredTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.found = scan_tests()

    def test_every_skip_is_registered(self):
        missing = {}
        for path, pairs in self.found.items():
            allowed = KNOWN_SKIP_SITES.get(path, [])
            extra = _missing_entries(pairs, allowed)
            if extra:
                missing[path] = extra
        self.assertEqual(missing, {}, "新增跳过未登记：%r" % missing)

    def test_registry_has_no_stale_entries(self):
        stale = {}
        for path, pairs in KNOWN_SKIP_SITES.items():
            actual = self.found.get(path, [])
            gone = _stale_entries(actual, pairs)
            if gone:
                stale[path] = gone
        self.assertEqual(stale, {}, "登记表有陈旧条目：%r" % stale)

    def test_surface_is_not_trivially_empty(self):
        total = sum(len(v) for v in self.found.values())
        self.assertGreaterEqual(total, 10, "跳过扫描面塌了")
        self.assertGreaterEqual(len(self.found), 5, "跳过扫描面塌了")


class BareDecoratorTest(unittest.TestCase):
    """@unittest.skip 不带括号会静默把函数换成 None：禁止。"""

    def test_no_bare_skip_decorator(self):
        for path in sorted(_iter_sources()):
            tree = ast.parse(io.open(path, encoding="utf-8").read(), filename=path)
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    continue
                for dec in node.decorator_list:
                    name = _bare_skip_name(dec)
                    if name in {"skip", "skipIf", "skipUnless"}:
                        self.fail("%s:%d 裸跳过装饰器" % (path, node.lineno))

    def test_pytest_mark_without_call_is_recognized(self):
        tree = ast.parse("@pytest.mark.skip\ndef f(): pass")
        self.assertEqual(_bare_skip_name(tree.body[0].decorator_list[0]), "skip")

    def test_pytest_mark_call_is_not_a_bare_decorator(self):
        tree = ast.parse("@pytest.mark.skipif(True, reason='x')\ndef f(): pass")
        self.assertIsNone(_bare_skip_name(tree.body[0].decorator_list[0]))


class RegistryDiffTest(unittest.TestCase):

    def test_duplicate_actual_is_not_masked(self):
        actual = [["skipTest", "x"], ["skipTest", "x"]]
        self.assertEqual(_missing_entries(actual, [["skipTest", "x"]]),
                         [("skipTest", "x")])

    def test_duplicate_allowed_is_not_masked(self):
        actual = [["skipTest", "x"]]
        self.assertEqual(_stale_entries(actual, [["skipTest", "x"], ["skipTest", "x"]]),
                         [("skipTest", "x")])


if __name__ == "__main__":
    unittest.main()

