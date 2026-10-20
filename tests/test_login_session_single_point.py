# -*- coding: utf-8 -*-
"""登录后"切回签到域请求头"只有一个实现点（工单 vq97）。

为什么钉这条
------------
签到链有两个前提，顺序不可拆：

1. 先登录——旧流的登录链会把 `Origin`/`Referer` 指向 `c.uyiban.com`
   （KillYiBan 客户端域）；
2. 旧流在拉签到任务前必须把会话头改回 App 域（`app.uyiban.com`）。
   标准 App 特征流（`KILLYIBAN_HEADERS`）保持 `c.uyiban.com`，实测可用，不回切。

顺序写错或漏掉第 2 步，旧流的签到请求会带错域头上游（实测回 e001）。
这条规则曾以**逐字相同的三份代码**存在：`signin()`、`verify()`、
以及一条独立分支上的只读预演脚本。第三份是在主树之外被发现的——共享事实门
当场超标（命中 9 / 上限 8）。

判据（结构性不变式，不钉"当前有几行"）
--------------------------------------
① `APP_SIGN_HEADERS` 这个名字在 `yiban/client.py` 里**恰好出现 1 次**；
② 其余生产模块零出现（含 `docker/`）；
③ `yiban/platform.py` 只许**定义一次**（`Store` 恰好 1 次），且**任何**对
   `headers` 的写入（`update(...)`、`|=`、`headers[...] = ...`）都不得携带
   签到域头（常量名、反射取名或 App 域字面量）；
④ App 域字面量只许出现在 `yiban/platform.py` 的**模块级赋值语句**
   （`Assign`/`AnnAssign`，且必须是 `Module.body` 的直接子语句）里。
   函数体、`if`/`try`/`with` 体内、其它模块——一律红；
⑤ 扫空不得当绿：扫描面有下限，且 `docker/` 必须在面内。

**面（怎么数）**：`APP_SIGN_HEADERS` 的出现按 AST 判，覆盖属性、裸名、
`from ... import APP_SIGN_HEADERS [as X]`、反射实参、以及 docstring 与
日志实参以外的字符串常量（含 `vars(p)["APP_SIGN_HEADERS"]`、变量绑定的
字符串、f-string）。日志调用（`logging.*`/`logger.*`）的实参与 docstring
不算读者。

**已知面外（显式登记，AGENTS §28）**：把名字在**运行时拼出来**的形态——
`getattr(p, "APP_SIGN" + "_HEADERS")` 这类由多段字符串拼接、或在循环里
逐字符构造的名字——不在判定面内。理由：那属于刻意规避，不是本项目实际发生过
的回归形态（历史三份副本都是**写常量名**）。本门要抓的是"照抄一段代码"。
若日后真出现拼接形态，应改的是写法（把域头收进函数），不是继续加匹配规则。

新增入口必须复用 `YibanClient._ensure_sign_session()`，不许复制这两行。
"""
import ast
import os
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLIENT_REL = "yiban/client.py"
PLATFORM_REL = "yiban/platform.py"
SCAN_DIRS = ("yiban", "web", "scripts", "docker")
SKIP_DIR_NAMES = {"__pycache__", ".mypy_cache", ".pytest_cache"}
APP_DOMAIN_LITERAL = "https://app.uyiban.com"
HEADER_NAME = "APP_SIGN_HEADERS"
HEADERS_ATTR = "headers"
_NAME_ARG_FUNCS = frozenset(("getattr", "setattr", "hasattr", "vars", "globals"))
_LOG_FUNCS = frozenset(("debug", "info", "warning", "warn", "error",
                        "critical", "exception", "log"))


def _iter_python_files():
    """遍历扫描面里的 .py 文件（返回仓库相对路径）。"""
    out = []
    for top in SCAN_DIRS:
        base = os.path.join(REPO_ROOT, top)
        if not os.path.isdir(base):
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIR_NAMES]
            for name in filenames:
                if name.endswith(".py"):
                    full = os.path.join(dirpath, name)
                    out.append(os.path.relpath(full, REPO_ROOT).replace(os.sep, "/"))
    return sorted(out)


def _docstring_const_ids(tree):
    """模块／类／函数首个语句若是字符串常量，记下它的对象 id（docstring 不计）。"""
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = getattr(node, "body", None) or []
            if body and isinstance(body[0], ast.Expr) \
                    and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                ids.add(id(body[0].value))
    return ids


def _log_string_ids(tree):
    """日志调用实参里的字符串常量 id（日志里提名字不是读者）。"""
    ids = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and getattr(node.func, "attr", None) in _LOG_FUNCS):
            continue
        for arg in list(node.args) + [kw.value for kw in node.keywords]:
            for sub in ast.walk(arg):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                    ids.add(id(sub))
    return ids


def _mentions_header_name(node):
    """`node` 子树是否以任何静态形态提到 `APP_SIGN_HEADERS`。

    覆盖：属性、裸名、反射实参（字符串）、`vars()[...]` 下标键、
    f-string 常量段、以及任意非日志字符串常量。
    """
    for sub in ast.walk(node):
        if isinstance(sub, ast.Attribute) and sub.attr == HEADER_NAME:
            return True
        if isinstance(sub, ast.Name) and sub.id == HEADER_NAME:
            return True
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str) \
                and HEADER_NAME in sub.value:
            return True
        if isinstance(sub, ast.JoinedStr) and any(
                isinstance(v, ast.Constant) and isinstance(v.value, str)
                and HEADER_NAME in v.value for v in sub.values):
            return True
    return False


def _refs_of_source(src, filename="<fixture>"):
    """一段源码里 `APP_SIGN_HEADERS` 的出现位置 `[(行号, 形态)]`（判据唯一实现）。

    docstring 与日志实参不计；其余字符串常量都算（含反射实参、下标键、
    变量绑定的字符串、f-string 常量段）。
    """
    tree = ast.parse(src, filename=filename)
    skip_ids = _docstring_const_ids(tree) | _log_string_ids(tree)
    refs = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == HEADER_NAME:
            refs.append((node.lineno, "属性"))
        elif isinstance(node, ast.Name) and node.id == HEADER_NAME:
            refs.append((node.lineno, "裸名"))
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                if a.name == HEADER_NAME:
                    refs.append((node.lineno, "导入别名"))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and id(node) not in skip_ids and HEADER_NAME in node.value:
            refs.append((node.lineno, "字符串常量"))
        elif isinstance(node, ast.JoinedStr):
            for v in node.values:
                if isinstance(v, ast.Constant) and isinstance(v.value, str) \
                        and id(v) not in skip_ids and HEADER_NAME in v.value:
                    refs.append((node.lineno, "f-string 常量段"))
    return sorted(set(refs))


def _header_refs(path):
    """`path` 里的引用点（读文件后走判据唯一实现）。"""
    with open(os.path.join(REPO_ROOT, path), encoding="utf-8") as fh:
        return _refs_of_source(fh.read(), filename=path)


def _module_assign_const_ids(tree):
    """模块级**赋值语句**里的字符串常量 id。

    只认 `Module.body` 的直接子语句且类型为 `Assign`／`AnnAssign`：
    `if`／`try`／`with` 体内的赋值属于"导入时执行的控制流"，
    不是常量定义区，故不计入（它们里的字面量会被判红）。
    """
    ids = set()
    for stmt in tree.body:
        if not isinstance(stmt, (ast.Assign, ast.AnnAssign)):
            continue
        for node in ast.walk(stmt):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                ids.add(id(node))
    return ids


def _touches_headers(node):
    """该表达式链里是否出现 `...headers...`（接收者或下标目标）。"""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Attribute) and sub.attr == HEADERS_ATTR:
            return True
    return False


def _app_domain_offenders_of_source(src, rel):
    """App 域字面量的越界出现位置 `[(行号, 原因)]`。"""
    tree = ast.parse(src, filename=rel)
    skip_ids = _docstring_const_ids(tree) | _log_string_ids(tree)
    allowed = _module_assign_const_ids(tree) if rel == PLATFORM_REL else set()
    out = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        if id(node) in skip_ids or APP_DOMAIN_LITERAL not in node.value:
            continue
        if rel != PLATFORM_REL:
            out.append((node.lineno, "生产模块内联 App 域字面量"))
        elif id(node) not in allowed:
            out.append((node.lineno, "App 域字面量不在模块级赋值里"))
    return sorted(set(out))


def _platform_switch_offenders(src):
    """`platform.py` 里的定义次数与任何切头写入 `[(行号, 原因)]`。"""
    tree = ast.parse(src, filename=PLATFORM_REL)
    stores = [n for n in ast.walk(tree)
              if isinstance(n, ast.Name) and n.id == HEADER_NAME
              and isinstance(n.ctx, ast.Store)]
    out = []
    if len(stores) != 1:
        out.append((stores[0].lineno if stores else 0,
                    "APP_SIGN_HEADERS 定义次数应为 1，实际 %d" % len(stores)))
    for node in ast.walk(tree):
        # headers.update(...) / headers.setdefault(...)
        if isinstance(node, ast.Call) and _touches_headers(node.func) \
                and any(_mentions_header_name(a)
                        for a in list(node.args) + [kw.value for kw in node.keywords]):
            out.append((node.lineno, "headers 调用携带签到域头"))
        # headers |= {...} / headers += ...
        elif isinstance(node, ast.AugAssign) and _touches_headers(node.target) \
                and _mentions_header_name(node.value):
            out.append((node.lineno, "headers 增量赋值携带签到域头"))
        # headers["Origin"] = "https://app.uyiban.com"
        elif isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Subscript) and _touches_headers(t.value)
                for t in node.targets) and _mentions_header_name(node.value):
            out.append((node.lineno, "headers 逐键赋值携带签到域头"))
    return sorted(set(out))


class SignSessionSingleDefinitionPointTest(unittest.TestCase):
    def test_scan_face_is_not_empty(self):
        """扫空不得当绿：扫描面必须真的覆盖到生产模块。"""
        files = _iter_python_files()
        self.assertGreater(len(files), 100,
                           "扫描面异常（只找到 %d 个 .py），判据会假绿" % len(files))
        self.assertIn(CLIENT_REL, files)
        self.assertIn(PLATFORM_REL, files)

    def test_docker_face_is_scanned(self):
        """容器入口是生产载体：`docker/` 必须在面内且真的扫到文件（兄弟门同口径）。"""
        self.assertTrue(os.path.isdir(os.path.join(REPO_ROOT, "docker")),
                        "docker/ 目录不存在——面登记与树不符，判据会静默变窄")
        docker_files = [f for f in _iter_python_files() if f.startswith("docker/")]
        self.assertGreater(len(docker_files), 0,
                           "docker/ 在面里但一个 .py 都没扫到——面写法失效")

    def test_client_has_exactly_one_header_reference(self):
        """`client.py` 里恰好 1 次（唯一实现点）。"""
        refs = _header_refs(CLIENT_REL)
        self.assertEqual(
            len(refs), 1,
            "yiban/client.py 应只有一个切回签到域请求头的实现点，实际 %d 处：%s。"
            "新增入口请复用 YibanClient._ensure_sign_session()。"
            % (len(refs), refs))

    def test_platform_defines_once_and_never_switches(self):
        """`platform.py` 只定义一次；任何切头写入都红（合法消费不误红）。"""
        with open(os.path.join(REPO_ROOT, PLATFORM_REL), encoding="utf-8") as fh:
            bad = _platform_switch_offenders(fh.read())
        self.assertEqual(bad, [],
                         "yiban/platform.py 只该定义一次 APP_SIGN_HEADERS，"
                         "且不得在其中再切会话头：%s" % bad)

    def test_no_other_module_references_the_header_set(self):
        """其余生产模块零出现（含 docker/）。"""
        offenders = []
        for rel in _iter_python_files():
            if rel in (CLIENT_REL, PLATFORM_REL):
                continue
            refs = _header_refs(rel)
            if refs:
                offenders.append("%s:%s" % (rel, ",".join(map(str, refs))))
        self.assertEqual(
            offenders, [],
            "这些生产模块手抄了签到域请求头切换（应改调 "
            "YibanClient._ensure_sign_session()）：%s" % offenders)

    def test_app_domain_literal_is_confined_to_platform_module_assign(self):
        """App 域字面量只在 platform.py 的模块级赋值里。"""
        offenders = []
        for rel in _iter_python_files():
            with open(os.path.join(REPO_ROOT, rel), encoding="utf-8") as fh:
                hits = _app_domain_offenders_of_source(fh.read(), rel)
            offenders.extend("%s:%d %s" % (rel, line, why) for line, why in hits)
        self.assertEqual(offenders, [], "App 域字面量越界：\n" + "\n".join(offenders))


class DetectorHasTeethTest(unittest.TestCase):
    """判据自身有牙：危险形态必红；合法形态不得误红。"""

    def test_all_static_forms_are_caught(self):
        cases = (
            "import yiban.platform as p\np.APP_SIGN_HEADERS",
            "from yiban.platform import APP_SIGN_HEADERS as S\nS",
            "from yiban.platform import APP_SIGN_HEADERS\nAPP_SIGN_HEADERS",
            "import yiban.platform as p\ngetattr(p, 'APP_SIGN_HEADERS')",
            "import yiban.platform as p\nhasattr(p, 'APP_SIGN_HEADERS')",
            "import yiban.platform as p\nvars(p)['APP_SIGN_HEADERS']",
            "import yiban.platform as p\n_N = 'APP_SIGN_HEADERS'\ngetattr(p, _N)",
            "import yiban.platform as p\ngetattr(p, f'APP_SIGN_HEADERS')",
        )
        for src in cases:
            with self.subTest(src=src):
                self.assertTrue(_refs_of_source(src),
                                "判据没有牙，等价写法未被判出：%r" % (src,))

    def test_prose_and_log_strings_are_not_readers(self):
        cases = (
            '"""docstring 提到 APP_SIGN_HEADERS 不算读者。"""',
            "# 注释里提到 APP_SIGN_HEADERS 不算读者\nx = 1",
            "import logging\nlogging.debug('using APP_SIGN_HEADERS')",
            "import logging\nlogging.getLogger('x').info('APP_SIGN_HEADERS')",
        )
        for src in cases:
            with self.subTest(src=src):
                self.assertEqual(_refs_of_source(src), [],
                                 "散文／日志被误判成读者：%r" % (src,))

    def test_platform_switch_detected_in_every_write_form(self):
        bad = (
            "def f(s):\n    s.headers.update(APP_SIGN_HEADERS)",
            "def f(s):\n    s.headers |= APP_SIGN_HEADERS",
            "def f(s):\n    s.headers['Origin'] = 'x' if APP_SIGN_HEADERS else 'y'",
            "def f(s):\n    s.headers.update(getattr(__import__('yiban.platform'),"
            " 'APP_SIGN_HEADERS'))",
            "def f(s):\n    s.headers.update({'Origin': 'https://app.uyiban.com'})",
            "APP_SIGN_HEADERS = {}\nAPP_SIGN_HEADERS = {}",
        )
        for src in bad:
            with self.subTest(src=src):
                self.assertTrue(_platform_switch_offenders(src),
                                "platform.py 里的切头写入未被判出：%r" % (src,))
        good = (
            "APP_SIGN_HEADERS = {'Origin': 'x'}",
            "APP_SIGN_HEADERS = {'Origin': 'x'}\nC = APP_SIGN_HEADERS.copy()",
            "APP_SIGN_HEADERS = {'Origin': 'x'}\n"
            "def f():\n    return APP_SIGN_HEADERS.copy()",
            "APP_SIGN_HEADERS = {'Origin': 'x'}\n"
            "def f(s):\n    s.headers.update({'X': 'y'})",
        )
        for src in good:
            with self.subTest(src=src):
                self.assertEqual(_platform_switch_offenders(src), [],
                                 "合法形态被误红：%r" % (src,))

    def test_app_domain_fixtures(self):
        # 越界：其它模块；platform.py 的函数体；platform.py 的 if 体内
        self.assertTrue(_app_domain_offenders_of_source(
            'X = {"Origin": "https://app.uyiban.com"}', "yiban/other.py"))
        self.assertTrue(_app_domain_offenders_of_source(
            'def f():\n    return "https://app.uyiban.com"', PLATFORM_REL))
        self.assertTrue(_app_domain_offenders_of_source(
            'if True:\n    S.headers.update({"Origin": "https://app.uyiban.com"})',
            PLATFORM_REL))
        # 合法：platform.py 模块级赋值（含去重常量与同域新键）
        self.assertEqual(_app_domain_offenders_of_source(
            'A = {"Origin": "https://app.uyiban.com"}\n'
            'B = {"Referer": "https://app.uyiban.com/"}', PLATFORM_REL), [])
        self.assertEqual(_app_domain_offenders_of_source(
            'O = "https://app.uyiban.com"\nA = {"Origin": O}\nB = {"Origin": O}',
            PLATFORM_REL), [])


if __name__ == "__main__":
    unittest.main()
