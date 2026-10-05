# -*- coding: utf-8 -*-
"""窗口边界 env 解析的唯一入口守卫。

标签：A · 调度：计划与分片
覆盖：`yiban.window.parse_edges` 的五格取值（两侧新键齐 / 只有一侧新键 / 只有旧键 /
    新键越界 / 三键全缺）；四个读者（`window.parse_edges`、`window.from_env`、
    `web.render.edge_config`、`schedule._schedule_config`）在同一份 env 输入下必须
    给出同一个 (front, back)；生产树的 Python 代码里按键名读这三个 env 键的语句
    只准出现在 `yiban/window.py::parse_edges` 内；`parse_edges` 的 docstring 主张的
    先后次序必须与实测取值一致。
对应实现：yiban/window.py（parse_edges、from_env、_int_or_none）、
    web/render.py（edge_config）、yiban/engine/schedule.py（_schedule_config）。
关键断言：窗口边界只有一个 env 解析入口。第二份解析接进任何读者 ⇒ 取值表当场对不上；
    第二份解析只是被复制出来、还没接线 ⇒ AST 位置判据当场点名。
    注释与 docstring 里的键名不进判定（AST 只认代码），本文件用三个带注释的模块
    自证这一点。
依赖：纯本地：打桩 os.environ，不建库、不发网络请求、不读 .env 文件。
"""
import ast
import contextlib
import os
import re
import unittest
from unittest import mock

from web import render
from yiban import window
from yiban.engine import schedule

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

EDGE_KEYS = ("YIBAN_WINDOW_EDGE_FRONT_SEC", "YIBAN_WINDOW_EDGE_BACK_SEC",
             "YIBAN_WINDOW_EDGE_SEC")
#: 唯一许可的 env 解析入口：（相对路径，函数名）。越界判据以此为轴。
SINGLE_ENTRY = ("yiban/window.py", "parse_edges")
#: AST 扫描的生产树目录，与 `scripts/check-shared-facts.sh` 的 DEFAULT_SCOPE 同族。
SCAN_DIRS = ("yiban", "web", "docker", "scripts")
SKIP_DIRS = ("__pycache__", "_vendor")
#: 只在注释里写这三个键名的生产模块；它们必须被扫到、且必须零命中。
COMMENT_ONLY_MODULES = ("web/app.py", "web/render.py", "yiban/engine/schedule.py")

#: 「新键与旧键同时存在」这一格；docstring 的次序判据也用它。
BOTH_KEYS_CASE = ({"YIBAN_WINDOW_EDGE_FRONT_SEC": "90", "YIBAN_WINDOW_EDGE_BACK_SEC": "30",
                   "YIBAN_WINDOW_EDGE_SEC": "120"}, (90, 30), "新键与旧键同时存在：新键赢")

#: (env 覆盖, 期望 (front, back), 这一格的主张)
CASES = (
    BOTH_KEYS_CASE,
    ({"YIBAN_WINDOW_EDGE_FRONT_SEC": "90", "YIBAN_WINDOW_EDGE_SEC": "120"},
      (90, 120), "只有一侧新键：旧键只补另一侧那一格"),
    ({"YIBAN_WINDOW_EDGE_SEC": "120"}, (120, 120), "只有旧键：旧键补两侧两格"),
    ({"YIBAN_WINDOW_EDGE_FRONT_SEC": "900", "YIBAN_WINDOW_EDGE_SEC": "120"},
      (120, 120), "新键越界（>300）视同缺席：旧键补两格"),
    ({}, (window.DEFAULT_EDGE_SEC, window.DEFAULT_EDGE_SEC), "三键全缺：两侧取默认"),
)


@contextlib.contextmanager
def _overlay_env(env):
    """把这一格的 env 覆盖进 `os.environ`，先把三个边界键清干净。"""
    base = {k: v for k, v in os.environ.items() if k not in EDGE_KEYS}
    base.update({k: str(v) for k, v in env.items()})
    with mock.patch.dict(os.environ, base, clear=True):
        yield


def _via_parse_edges(env):
    return window.parse_edges(env)


def _via_from_env(env):
    """引擎与网页共用的 `Window` 入口（过 `bounds`，取视图上的 front/back）。"""
    win = window.from_env(env)
    return (win.front_sec, win.back_sec)


def _via_render_edge_config(env):
    return render.edge_config(env)


def _via_schedule_config(env):
    """引擎入口：`_schedule_config` 自读 `os.environ`，故把这一格覆盖进去再取 cfg。"""
    with _overlay_env(env):
        cfg = schedule._schedule_config()
    return (cfg["edge_front_sec"], cfg["edge_back_sec"])


#: 各读者：同一个 env 映射进，同一个 (front, back) 出。
READERS = (
    ("yiban.window.parse_edges", _via_parse_edges),
    ("yiban.window.from_env", _via_from_env),
    ("web.render.edge_config", _via_render_edge_config),
    ("yiban.engine.schedule._schedule_config", _via_schedule_config),
)


def _is_edge_key(node):
    return isinstance(node, ast.Constant) and node.value in EDGE_KEYS


def _callee_name(func):
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


class _SiteFinder(ast.NodeVisitor):
    """记录「按键名从 env 取值」的位置：`.get(KEY)`、`obj[KEY]`（读）、`_int_or_none(_, KEY)`。

    写侧（`updates[KEY] = ...`）与纯键名清单（转发白名单的元组字面量）都不算取值，
    故不进判定；注释与 docstring 由 AST 天然排除。
    """

    def __init__(self, rel):
        self.rel = rel
        self.sites = []
        self._stack = []

    def visit_FunctionDef(self, node):
        self._stack.append(node.name)
        self.generic_visit(node)
        self._stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def _record(self, node, key):
        self.sites.append((self.rel, self._stack[-1] if self._stack else "<module>",
                           node.lineno, key))

    def visit_Call(self, node):
        if _callee_name(node.func) in ("get", "_int_or_none"):
            for arg in list(node.args) + [kw.value for kw in node.keywords]:
                if _is_edge_key(arg):
                    self._record(node, arg.value)
        self.generic_visit(node)

    def visit_Subscript(self, node):
        if isinstance(node.ctx, ast.Load) and _is_edge_key(node.slice):
            self._record(node, node.slice.value)
        self.generic_visit(node)


def _scan_production_tree():
    """扫生产树的 .py，返回（命中位置列表, 扫过的相对路径集合）。"""
    sites, scanned = [], set()
    for root in SCAN_DIRS:
        abs_root = os.path.join(BASE, root)
        if not os.path.isdir(abs_root):
            continue
        for dirpath, dirnames, filenames in os.walk(abs_root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for name in filenames:
                if not name.endswith(".py"):
                    continue
                path = os.path.join(dirpath, name)
                rel = os.path.relpath(path, BASE).replace(os.sep, "/")
                with open(path, encoding="utf-8") as fh:
                    tree = ast.parse(fh.read(), filename=rel)
                finder = _SiteFinder(rel)
                finder.visit(tree)
                sites.extend(finder.sites)
                scanned.add(rel)
    return sites, scanned


class EdgeValueSingleSourceTest(unittest.TestCase):
    def test_every_reader_returns_the_same_pair(self):
        """同一份 env 输入，各读者必须得到同一个 (front, back)。"""
        for env, expected, note in CASES:
            for name, read in READERS:
                with self.subTest(case=note, reader=name):
                    self.assertEqual(expected, read(env),
                                     "读者 %s 在「%s」下应得 %s" % (name, note, expected))


class SingleParseEntryTest(unittest.TestCase):
    def test_only_parse_edges_reads_the_edge_keys(self):
        """第二份解析入口（接线与否都算）必须被点名。"""
        sites, _scanned = _scan_production_tree()
        outside = [s for s in sites if (s[0], s[1]) != SINGLE_ENTRY]
        detail = "; ".join("%s:%d 在 %s() 读 %s" % (r, ln, fn, k)
                           for r, fn, ln, k in outside)
        self.assertEqual(
            [], outside,
            "窗口边界的 env 键只准在 yiban/window.py::parse_edges 里读；"
            "以下位置各算各的：" + detail)

    def test_the_gate_is_not_an_empty_gate(self):
        """防废门：入口仍在读三个键，且带注释的模块确实被扫到且零命中。"""
        sites, scanned = _scan_production_tree()
        inside = sorted(k for r, fn, _ln, k in sites if (r, fn) == SINGLE_ENTRY)
        self.assertEqual(sorted(EDGE_KEYS), inside,
                         "parse_edges 必须仍在读这三个 env 键，否则本门恒绿")
        for rel in COMMENT_ONLY_MODULES:
            self.assertIn(rel, scanned, "%s 没进扫描面，判据不可信" % rel)
            self.assertEqual([], [s for s in sites if s[0] == rel],
                             "%s 只在注释里写键名，不该被判命中" % rel)


#: docstring 里主张先后次序的说法（出现即必须与实测一致）。
PRECEDENCE_WORDS = ("优先", "覆盖", "取代", "胜过", "压过")


class DocstringTruthTest(unittest.TestCase):
    def test_docstring_precedence_matches_behavior(self):
        """docstring 主张哪一族键赢，实测就必须是哪一族键赢。"""
        env, expected, _note = BOTH_KEYS_CASE
        new_wins = window.parse_edges(env) == expected
        doc = window.parse_edges.__doc__ or ""
        for sentence in re.split(r"[。；;\n]", doc):
            if not any(word in sentence for word in PRECEDENCE_WORDS):
                continue
            names_new = ("EDGE_FRONT_SEC" in sentence) or ("EDGE_BACK_SEC" in sentence)
            names_legacy = "YIBAN_WINDOW_EDGE_SEC" in sentence
            if names_new == names_legacy:
                continue  # 同一句里两族都点名或都没点名：判不出它主张谁，跳过
            claimed = "新键" if names_new else "旧键"
            self.assertEqual(
                claimed == "新键", new_wins,
                "docstring 主张「%s优先」，实测却是「%s」生效：%s"
                % (claimed, "新键" if new_wins else "旧键", sentence.strip()))


if __name__ == "__main__":
    unittest.main()
