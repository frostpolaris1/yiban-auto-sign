# -*- coding: utf-8 -*-
"""冻结载体使用门：`run_queue_retry` 无生产调用点；测试里的每个调用点必须显式登记。

标签：A · 判据：测试卫生

**本门挡的缺陷形状（工单 `77s2`）**：执行核已恒定走 `executor_v3.run_executor_v3`
（`sign_tasks` 单池），而 `yiban/app/round.py` 的 `run_queue_retry` 是旧领取池
（`sign_claims`）时代的实现，**生产调用点为 0**。若测试继续把它当现役执行核来测，
用例会全绿，而真路径根本不在调用图上——"全绿"证明不了任何生产性质。

**本门钉两条性质**：
  ① **生产面为零**：`yiban/`、`scripts/`、`web/`、`docker/` 的全部 `.py` 里，
     `run_queue_retry(...)` 的**调用**必须为 0（定义与文本提及不算调用；注释/docstring
     里的名字不算调用）。同批还断言定义仍然恰好存在一处——否则"零调用"会因为函数被删
     而空过（§十三.30：跳过≠绿）。
  ② **测试面必须登记**：`tests/` 里每一处调用点（`文件::类.方法`）都必须列在
     `FROZEN_CARRIER_SMOKE`，并写明"为什么留、真路径的判据在哪、缺什么"。
     反向也判：登记表里的条目必须仍然真的调用它（陈旧条目 ⇒ 红），
     防止"登记表变成尸体名册"（§十五）。

**登记表的含义是"冒烟，不是生产证据"**。条目里的用例只证明冻结载体自身没坏；
它们**不得**被引用为"生产性质已验证"。

**本门不挡什么（如实登记）**：文本级/ AST 级都只认 `.py`；bash/JS/文档里提到
`run_queue_retry` 不构成调用，不在本门判据内。`scripts/signin.py` 的兼容壳会把该名字
**转发**到模块属性上（`_forward_all_names`），那是导出面而非调用点，不判红。

依赖：纯 AST ＋ `os.walk`；不跑被测代码、不触网。
"""
import ast
import os
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FROZEN = "run_queue_retry"
PRODUCTION_ROOTS = ("yiban", "scripts", "web", "docker")
DEFINITION_FILE = "yiban/app/round.py"

#: 冻结载体的**冒烟**清单（文件相对路径::限定名 → 理由）。
#: 每条都必须回答三件事：① 这条用例钉什么；② 真路径的判据在哪；③ 为什么不能删。
FROZEN_CARRIER_SMOKE = {
    "tests/test_effective_window.py::ManualChainWindowGuardTest::test_manual_chain_stops_at_window_close":
        "钉冻结载体的手动链路逐账号窗口钳制（窗口在轮内关闭 ⇒ 剩余账号零请求）。"
        "真路径缺口：`executor_v3` 没有等价的“轮内逐账号窗口门”（它只在轮末用 "
        "`_mark_window_skips` 收尾），故本性质**没有** v3 判据；保留为冻结载体冒烟，"
        "不得引用为生产证据。缺口需要独立工单裁决（是否给 v3 补 mid-round 窗口门）。",
    "tests/test_effective_window.py::ManualChainWindowGuardTest::test_only_mode_ignores_window_guard":
        "同上一条的反向控制：`--only` 手动路径有意不受窗口钳制。"
        "真路径缺口：v3 的 `reclaim=True` 手动轮不看窗口，但**没有**用例正面钉住"
        "“窗口外仍执行”这一方向；保留为冻结载体冒烟，不构成生产证据。",
}


def _called_name(func):
    """调用表达式里的函数名（只取末段）：`x.run_queue_retry` / `run_queue_retry`。"""
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _walk_scope(node, scope, rel, calls, defs):
    """按作用域递归：记录 `FROZEN` 的调用（文件::限定名）与函数定义（文件:行）。"""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.ClassDef):
            _walk_scope(child, [*scope, child.name], rel, calls, defs)
        elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if child.name == FROZEN:
                defs.append("%s:%d" % (rel, child.lineno))
            _walk_scope(child, [*scope, child.name], rel, calls, defs)
        else:
            if isinstance(child, ast.Call) and _called_name(child.func) == FROZEN:
                calls.add("%s::%s" % (rel, "::".join(scope) or "<module>"))
            _walk_scope(child, scope, rel, calls, defs)


def scan_tree(root):
    """扫描 `root` 下全部 `.py` → (调用点集合, 定义点列表, 解析文件数)。"""
    calls, defs, parsed = set(), [], 0
    for dirpath, dirnames, files in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
        for name in sorted(files):
            if not name.endswith(".py"):
                continue
            path = os.path.join(dirpath, name)
            rel = os.path.relpath(path, root).replace(os.sep, "/")
            with open(path, encoding="utf-8") as f:
                tree = ast.parse(f.read(), filename=path)
            parsed += 1
            _walk_scope(tree, [], rel, calls, defs)
    return calls, defs, parsed


def production_call_sites(root=BASE):
    """生产面（`PRODUCTION_ROOTS`）里的调用点集合（相对路径::限定名）。"""
    calls = set()
    for sub in PRODUCTION_ROOTS:
        target = os.path.join(root, sub)
        if not os.path.isdir(target):
            continue
        sub_calls, _defs, _n = scan_tree(target)
        calls |= {sub + "/" + c for c in sub_calls}
    return calls


def reconcile(found, registry):
    """→ (未登记, 陈旧条目)：两个方向都要判，登记表不许有假条目。"""
    return sorted(set(found) - set(registry)), sorted(set(registry) - set(found))


class FrozenCarrierProductionGateTest(unittest.TestCase):
    """生产面零调用；定义仍在（零调用不得因"函数被删"而空过）。"""

    def test_no_production_call_site(self):
        hits = production_call_sites()
        self.assertEqual(hits, set(),
                         "冻结载体不得被生产代码调用（生产执行体只许是 executor_v3）：%s"
                         % sorted(hits))

    def test_definition_still_exists_exactly_once(self):
        _calls, defs, parsed = scan_tree(os.path.join(BASE, "yiban"))
        self.assertGreater(parsed, 50,
                           "yiban/ 解析到的 .py 数量异常（%d）：本门会因扫描面为空而假绿"
                           % parsed)
        self.assertEqual(defs, ["app/round.py:%d" % _def_line()],
                         "`run_queue_retry` 的定义点必须恰好一处（%s）：%s"
                         % (DEFINITION_FILE, defs))


def _def_line(path=None):
    path = path or os.path.join(BASE, DEFINITION_FILE)
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=path)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == FROZEN:
            return node.lineno
    raise AssertionError("定义点不存在：%s" % path)


class FrozenCarrierTestRegistryTest(unittest.TestCase):
    """测试面：每处调用都登记；登记表无陈旧条目；调用点集合非空。"""

    def test_every_test_call_site_is_registered(self):
        found, _defs, parsed = scan_tree(os.path.join(BASE, "tests"))
        found = {"tests/" + c for c in found}
        self.assertGreater(parsed, 50,
                           "tests/ 解析到的 .py 数量异常（%d）：本门会因扫描面为空而假绿"
                           % parsed)
        unregistered, stale = reconcile(found, FROZEN_CARRIER_SMOKE)
        self.assertEqual(unregistered, [],
                         "以下用例调用了冻结载体却没登记（删掉，或改测真路径，"
                         "或在 FROZEN_CARRIER_SMOKE 写明理由）：%s" % unregistered)
        self.assertEqual(stale, [],
                         "登记表有条目已不再调用冻结载体（陈旧条目必须删）：%s" % stale)

    def test_registry_matches_scan_when_non_empty(self):
        """扫空不得当绿：登记表非空时，扫描必须命中它（否则是扫描器坏了）。

        登记表为空（冻结载体已完全退场）时本判据无事可做——生产面那道门仍然生效；
        这里用 `return` 而不是 `skipTest`：跳过点必须显式登记（§十三.30），
        而"没有条目"不是环境不满足。
        """
        if not FROZEN_CARRIER_SMOKE:
            return
        found, _defs, _n = scan_tree(os.path.join(BASE, "tests"))
        found = {"tests/" + c for c in found}
        self.assertTrue(found & set(FROZEN_CARRIER_SMOKE),
                        "登记表非空但扫描不到任何对应调用点：扫描器坏了或文件被移走")


class FrozenCarrierGateTeethTest(unittest.TestCase):
    """门的牙：假生产调用必红；未登记/陈旧条目必红；注释与 docstring 不算调用。"""

    def test_fake_production_call_is_detected(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "yiban"))
            with open(os.path.join(root, "yiban", "bad.py"), "w", encoding="utf-8") as f:
                f.write("from yiban.app import round as r\n"
                        "def work():\n"
                        "    return r.run_queue_retry([], None, 0, 0)\n")
            self.assertEqual(production_call_sites(root),
                             {"yiban/bad.py::work"},
                             "生产面的真实调用必须被识别（否则门无牙）")

    def test_text_mentions_are_not_calls(self):
        with tempfile.TemporaryDirectory() as root:
            with open(os.path.join(root, "x.py"), "w", encoding="utf-8") as f:
                f.write('"""run_queue_retry 只是文档里的名字。"""\n'
                        "# run_queue_retry(...) 在注释里也不算调用\n"
                        "NAME = 'run_queue_retry'\n")
            calls, _defs, _n = scan_tree(root)
            self.assertEqual(calls, set(), "注释/docstring/字符串不是调用点")

    def test_unregistered_and_stale_entries_are_detected(self):
        unregistered, stale = reconcile({"tests/a.py::T::t1", "tests/a.py::T::t2"},
                                        {"tests/a.py::T::t1", "tests/b.py::Gone::old"})
        self.assertEqual(unregistered, ["tests/a.py::T::t2"])
        self.assertEqual(stale, ["tests/b.py::Gone::old"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
