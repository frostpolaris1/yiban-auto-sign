# -*- coding: utf-8 -*-
"""结构守卫：签到提交触网点唯一性（128 第一刀）。

标签：E · 门禁：签到触网点唯一性
覆盖：以 AST 静态扫**生产代码面**（`yiban/` + `web/` + `scripts/` + `docker/` 的
    全部 `.py`，排除 `tests/`），钉住两条不变量：
    ① `yiban/platform.py` 的 `submit_sign_in`（登录/签到的提交原语）
       在生产代码里**恰好一个**调用点：`yiban/client.py` 的 `YibanClient.signin` 内。
    ② `yiban/client.py` 的 `YibanClient.signin`（签到客户端外观）
       在生产代码里**恰好一个**调用点：`yiban/engine/attempts.py` 的 `attempt_signin` 内。

判定面（计为命中的静态形态）：
    · 直接调用：`submit_sign_in(...)`、`obj.submit_sign_in(...)`（按调用末段名匹配）。
    · 别名导入调用：`from <mod> import <目标名> as <别名>` 之后调用 `<别名>(...)`。
    · `getattr(<任意>, "<目标名>")` 的**字符串字面量**形态。
判定面外的形态：完全动态的构造不计为命中。
    例：运行时拼名 `getattr(p, "submit_" + "sign_in")`、经其它反射路径取到的可调用对象。
    这些形态由**审查**兜底，不由本门判定。
    故本门**不声称绝对保证**：它覆盖上述三类静态可判形态，不覆盖运行时拼名。

门的定义域：本门守的是**登录/签到提交流程**的触网点唯一性。
它**不是**"任何平台请求都只有一个触网点"。
反例已在本范围外：`web/services/signstatus.py::check_connectivity()` 是可达性探测，
    其 docstring 明写"不登录，仅检查易班 API 可达性"，并直连 `requests.get`。
    它**不在**本门范围内：后人不得据此门把 `check_connectivity` 的请求也纳入唯一性断言，
    也不得为了让它"过门"而改它的形状。脚本面（`web/static/**/*.js`）是前端源码，不在本门内。

关键断言（fail-closed）：
    · 两个定义点任一找不到（被改名/删除）⇒ 红。不许静默跳过。
    · 任一调用点数 ≠ 1（增或减）⇒ 红，并逐条点名 文件:行 与宿主限定名。

立此守卫的理由（AGENTS.md §15）：签到提交的触网点是审计与限流的唯一口径锚。
    下一个改这里的人新增第二处**静态可判**的提交触网点时（直接调用、别名导入、
    getattr 字面量），本文件必须变红。

依赖：只用标准库 `ast` 读源码，不导入被测模块、不触网、不落盘。
"""
import ast
import io
import os
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 生产代码面（相对仓库根的目录）。`tests/` 明确不在面内。
#: `docker/` 入面理由：`docker/scheduler.py` 是生产容器入口，且 import 签到模块。
#: 今日它不含触网点（故未漏判），但将来加触网点时必须被本门看见。
PROD_DIRS = ("yiban", "web", "scripts", "docker")
#: 扫描时剪掉的工具/缓存目录（`.py` 不会出现在这些目录里）。
PRUNE = {"__pycache__", "node_modules", ".venv", "venv"}

# ① 提交原语 `submit_sign_in` 的实测不变量（2026-10-12 在 origin/develop 4fbcd005 核实）。
SUBMIT_DEF_FILE = "yiban/platform.py"
SUBMIT_DEF_NAME = "submit_sign_in"
SUBMIT_CALL_FILE = "yiban/client.py"
SUBMIT_CALL_QUAL = "YibanClient.signin"

# ② 签到外观 `YibanClient.signin` 的实测不变量（同一底本）。
SIGNIN_DEF_FILE = "yiban/client.py"
SIGNIN_DEF_CLASS = "YibanClient"
SIGNIN_DEF_METH = "signin"
SIGNIN_CALL_FILE = "yiban/engine/attempts.py"
SIGNIN_CALL_QUAL = "attempt_signin"


def _callee_last(func):
    """调用目标的可比名（只看末段）：`f`→f，`m.submit_sign_in`→submit_sign_in。"""
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _import_aliases(tree, target):
    """`from <mod> import <target> [as 别名]` 引入的本地名字集合。

    含原名与别名两态。调用别名（`_sis(x)`）即视为对目标名的调用。
    注意：`import <mod>`（`ast.Import`）不走本函数——那是对模块的引用，不是对目标名的引入。
    """
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == target:
                    names.add(alias.asname or alias.name)
    return names


def _getattr_literal_target(call):
    """`getattr(<任意>, "<名>")` 的第二参字符串字面量；非字面量返回 None。"""
    if len(call.args) >= 2:
        second = call.args[1]
        if isinstance(second, ast.Constant) and isinstance(second.value, str):
            return second.value
    return None


def _collect_calls(tree, target):
    """收集对 `target` 的调用，返回 `[(行号, 宿主限定名)]`。

    命中三类静态形态（判定面）：
      · 调用末段名 == target（`submit_sign_in(...)` / `obj.submit_sign_in(...)`）；
      · 调用 `from <mod> import <target> as <别名>` 得来的别名；
      · `getattr(<任意>, "<target>")` 的字符串字面量形态。
    不命中：docstring / 注释里的同名字样；`m.signin.worker_presence()` 这类末段非
    target 的属性调用；运行时拼名的完全动态构造（由审查兜底，不在判定面内）。
    """
    aliases = _import_aliases(tree, target) - {target}
    hits = []

    def walk(node, scope):
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            scope = [*scope, node.name]
        elif isinstance(node, ast.Call):
            name = _callee_last(node.func)
            if name == target \
                    or (isinstance(node.func, ast.Name) and node.func.id in aliases) \
                    or (name == "getattr" and _getattr_literal_target(node) == target):
                hits.append((node.lineno, ".".join(scope)))
        for child in ast.iter_child_nodes(node):
            walk(child, scope)

    walk(tree, [])
    return hits


def _def_line(tree, name):
    """模块内名为 `name` 的函数定义行；找不到返回 None（fail-closed 的判据）。"""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node.lineno
    return None


def _method_def_line(tree, cls_name, meth_name):
    """类 `cls_name` 内方法 `meth_name` 的定义行；找不到返回 None。"""
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == cls_name:
            for sub in ast.walk(node):
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                        and sub.name == meth_name:
                    return sub.lineno
    return None


def _parse(path):
    with io.open(path, encoding="utf-8") as fh:
        return ast.parse(fh.read(), filename=path)


def _scan_prod():
    """扫生产代码面，返回 `{相对路径: AST}`。"""
    trees = {}
    for sub in PROD_DIRS:
        for dirpath, dirnames, filenames in os.walk(os.path.join(BASE, sub)):
            dirnames[:] = sorted(d for d in dirnames if d not in PRUNE)
            for fn in sorted(filenames):
                if not fn.endswith(".py"):
                    continue
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, BASE).replace(os.sep, "/")
                trees[rel] = _parse(full)
    return trees


class SignTouchpointSingletonTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.trees = _scan_prod()

    # ---- 扫描面自身承重：面收缩或被改名时必须红，不许空扫 ----

    def test_scan_face_covers_touchpoint_files(self):
        for rel in (SUBMIT_DEF_FILE, SUBMIT_CALL_FILE, SIGNIN_CALL_FILE,
                    SIGNIN_DEF_FILE, "web/services/signstatus.py",
                    "docker/scheduler.py"):
            self.assertIn(rel, self.trees, "扫描面缺失关键文件：%s" % rel)
        self.assertGreaterEqual(len(self.trees), 30, "扫描面异常收缩：守卫要红给自己看")

    # ---- 定义点在场性（fail-closed：改名/删除即红） ----

    def test_submit_sign_in_definition_present(self):
        tree = self.trees.get(SUBMIT_DEF_FILE)
        self.assertIsNotNone(tree, "定义点所在文件不见了：%s" % SUBMIT_DEF_FILE)
        self.assertIsNotNone(
            _def_line(tree, SUBMIT_DEF_NAME),
            "找不到定义点 %s::%s（被改名/删除？）。本门 fail-closed，不许静默跳过。"
            % (SUBMIT_DEF_FILE, SUBMIT_DEF_NAME))

    def test_client_signin_definition_present(self):
        tree = self.trees.get(SIGNIN_DEF_FILE)
        self.assertIsNotNone(tree, "定义点所在文件不见了：%s" % SIGNIN_DEF_FILE)
        self.assertIsNotNone(
            _method_def_line(tree, SIGNIN_DEF_CLASS, SIGNIN_DEF_METH),
            "找不到定义点 %s::%s.%s（被改名/删除？）。本门 fail-closed，不许静默跳过。"
            % (SIGNIN_DEF_FILE, SIGNIN_DEF_CLASS, SIGNIN_DEF_METH))

    # ---- 触网点唯一性（核心断言） ----

    def _prod_calls(self, target):
        out = []
        for rel in sorted(self.trees):
            for lineno, qual in _collect_calls(self.trees[rel], target):
                out.append((rel, lineno, qual))
        return out

    def _assert_single(self, hits, exp_file, exp_qual, label):
        pretty = ["%s:%d  (%s)" % (f, ln, q or "<module>") for f, ln, q in hits]
        self.assertEqual(
            len(hits), 1,
            "%s 在生产代码里必须恰好一个调用点，实际 %d 个：\n%s"
            % (label, len(hits), "\n".join(pretty) or "（0 个：触网点被删或改名）"))
        f, ln, q = hits[0]
        self.assertEqual(
            f, exp_file,
            "%s 的唯一调用点应在 %s，实际落在 %s:%d" % (label, exp_file, f, ln))
        self.assertEqual(
            q, exp_qual,
            "%s 的唯一调用点宿主应为 %s，实际 %s:%d 的宿主是 %s"
            % (label, exp_qual, f, ln, q or "<module>"))

    def test_submit_sign_in_has_exactly_one_call(self):
        self._assert_single(self._prod_calls(SUBMIT_DEF_NAME),
                            SUBMIT_CALL_FILE, SUBMIT_CALL_QUAL,
                            "submit_sign_in（登录/签到提交原语）")

    def test_client_signin_has_exactly_one_call(self):
        self._assert_single(self._prod_calls(SIGNIN_DEF_METH),
                            SIGNIN_CALL_FILE, SIGNIN_CALL_QUAL,
                            "YibanClient.signin（签到客户端外观）")

    # ---- 变异体：证明上面的断言承重，不是空扫（AGENTS.md §15） ----

    def test_scanner_counts_a_planted_second_call(self):
        """给提交原语添第二处调用 ⇒ 计数变 2（证明唯一性断言真的在数数）。"""
        src = ("def a(x):\n    return x.submit_sign_in()\n\n"
               "def b(y):\n    return y.submit_sign_in()\n")
        self.assertEqual(len(_collect_calls(ast.parse(src), "submit_sign_in")), 2)

    def test_scanner_counts_an_aliased_import_call(self):
        """别名导入后调用别名 ⇒ 计为命中（`from … import … as _sis; _sis(x)` 不再是洞）。"""
        src = ("from yiban.platform import submit_sign_in as _sis\n\n"
               "def f(x):\n    return _sis(x)\n")
        self.assertEqual(len(_collect_calls(ast.parse(src), "submit_sign_in")), 1)

    def test_scanner_counts_a_getattr_literal_call(self):
        """`getattr(<任意>, "<目标名>")` 字符串字面量 ⇒ 计为命中。"""
        src = 'def f(p):\n    return getattr(p, "submit_sign_in")()\n'
        self.assertEqual(len(_collect_calls(ast.parse(src), "submit_sign_in")), 1)

    def test_scanner_ignores_a_dynamic_getattr_name(self):
        """边界：运行时拼名的 getattr 不在判定面内（docstring 的精确陈述据此承重）。"""
        src = 'def f(p):\n    return getattr(p, "submit_" + "sign_in")()\n'
        self.assertEqual(_collect_calls(ast.parse(src), "submit_sign_in"), [])

    def test_definition_probe_flags_a_renamed_function(self):
        """定义改名后在场性探针返回 None（定义点在场性断言因此变红）。"""
        self.assertIsNone(_def_line(ast.parse("def submit_sign_in_v2():\n    pass\n"),
                                    "submit_sign_in"))

    def test_definition_probe_flags_a_renamed_method(self):
        """方法改名后类内探针返回 None。"""
        src = "class YibanClient:\n    def do_signin(self):\n        pass\n"
        self.assertIsNone(_method_def_line(ast.parse(src), "YibanClient", "signin"))

    def test_scanner_ignores_module_attribute_calls(self):
        """边界：`m.signin.worker_presence()` 是对 worker_presence 的调用，不是对 signin 的。"""
        src = "def f(m):\n    return m.signin.worker_presence(1)\n"
        self.assertEqual(_collect_calls(ast.parse(src), "signin"), [])

    def test_scanner_ignores_mentions_in_docstring_and_comment(self):
        """边界：正文/注释里的同名字样不是调用点（故本门用 AST，不用文本 grep）。

        仓内确有这两处文本提及（`yiban/client.py` 模块头的 `YibanClient.signin`、
        `tests/` 里的注释），文本扫描会误计，AST 不会。
        """
        src = ('def f():\n'
               '    """见 platform.submit_sign_in 与 YibanClient.signin()."""\n'
               '    # client.signin() 说明\n'
               '    return None\n')
        tree = ast.parse(src)
        self.assertEqual(_collect_calls(tree, "submit_sign_in"), [])
        self.assertEqual(_collect_calls(tree, "signin"), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
