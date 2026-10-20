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

判据（结构性不变式）
--------------------
① `APP_SIGN_HEADERS` 这个名字在 `yiban/client.py` 里**恰好出现 1 次**；
② 其余生产模块零出现（含 `docker/`）；
③ **切头写入**：除 `client.py` 外，任何模块对 headers 的写入
   （`update(...)`、`setdefault(...)`、`|=`、`+=`、`headers[...] = ...`；
   接收者认 `x.headers` 与裸名 `headers`）都不得携带签到域头。
   "携带"的判定 = 实参里出现 `APP_SIGN_HEADERS` / `APP_ORIGIN` / `HEADERS`
   任一名字，或出现 App 域字面量。`client.py` 的唯一实现点由 ① 单独钉住；
④ **App 域字面量全仓恰好 1 处**，且必须是 `yiban/platform.py` 的模块级简单
   赋值（单 `Name` 目标）。新增同域请求头必须引用 `platform.APP_ORIGIN`——
   重写域名即第二份定义；
⑤ 扫空不得当绿：扫描面有下限，且 `docker/` 必须在面内。

**"字面量站点"怎么数**：docstring、日志实参、`raise` 的异常构造、`print`、
`assert` 的消息串都算**散文**，不计数——合法错误文案里写域名不会误红。
其余含 App 域字面量的字符串常量都算站点（含 dict 值、逐键赋值、函数体）。

**已知面外（显式登记，AGENTS §28）**：
  ① 名字或域名在**运行时拼出来**：`"APP_SIGN" + "_HEADERS"`、
     `"https://" + "app.uyiban.com"`、循环里逐字符构造。理由：那属于刻意规避，
     不是本项目实际发生过的回归形态（历史三份副本都写完整常量名/域名）。
     本门要抓的是"照抄一段代码"。
  ② 本门是**静态**判据：运行时 `exec`/`eval` 构造的切换看不到；这类写法在
     本仓不存在，出现即为独立缺陷，应由代码审查拦。

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
#: 写入实参里出现这些名字即视为"携带签到域头"。
SIGN_DOMAIN_NAMES = ("APP_SIGN_HEADERS", "APP_ORIGIN", "HEADERS")
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


def _prose_string_ids(tree):
    """散文语境里的字符串常量 id：docstring、日志、raise、print、assert 消息。

    这些位置的域名/常量名是**给人看的说明**，不是读者，也不构成切换。
    """
    ids = set(_docstring_const_ids(tree))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fname = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if fname in _LOG_FUNCS or fname == "print":
                for arg in list(node.args) + [kw.value for kw in node.keywords]:
                    ids |= _const_str_ids(arg)
        elif isinstance(node, ast.Raise) and node.exc is not None:
            ids |= _const_str_ids(node.exc)
        elif isinstance(node, ast.Assert) and node.msg is not None:
            ids |= _const_str_ids(node.msg)
    return ids


def _const_str_ids(node):
    return {id(sub) for sub in ast.walk(node)
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str)}


def _mentions_name(node):
    """`node` 子树是否提到 `APP_SIGN_HEADERS`（判"读者"用）。"""
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


def _mentions_sign_domain(node):
    """`node` 子树是否携带签到域头（名字、常量名或 App 域字面量）。"""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Attribute) and sub.attr in SIGN_DOMAIN_NAMES:
            return True
        if isinstance(sub, ast.Name) and sub.id in SIGN_DOMAIN_NAMES:
            return True
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            if APP_DOMAIN_LITERAL in sub.value:
                return True
            if any(n in sub.value for n in SIGN_DOMAIN_NAMES):
                return True
        if isinstance(sub, ast.JoinedStr) and any(
                isinstance(v, ast.Constant) and isinstance(v.value, str)
                and (APP_DOMAIN_LITERAL in v.value
                     or any(n in v.value for n in SIGN_DOMAIN_NAMES))
                for v in sub.values):
            return True
    return False


def _refs_of_source(src, filename="<fixture>"):
    """一段源码里 `APP_SIGN_HEADERS` 的出现位置 `[(行号, 形态)]`（判据唯一实现）。

    覆盖属性、裸名、导入别名、反射实参、下标键、变量绑定字符串、f-string
    常量段，以及散文以外的任意字符串常量。docstring／日志／raise／print／
    assert 消息不算读者。
    """
    tree = ast.parse(src, filename=filename)
    skip_ids = _prose_string_ids(tree)
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


def _simple_module_assign_const_ids(tree):
    """模块级**简单赋值**（单 Name 目标）值里的字符串常量 id。"""
    ids = set()

    def collect(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            ids.add(id(node))
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef,
                                  ast.ClassDef, ast.Lambda)):
                continue
            collect(child)

    for stmt in tree.body:
        is_assign = (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1
                     and isinstance(stmt.targets[0], ast.Name))
        is_ann = (isinstance(stmt, ast.AnnAssign)
                  and isinstance(stmt.target, ast.Name) and stmt.value is not None)
        if is_assign or is_ann:
            collect(stmt.value)
    return ids


def _touches_headers(node):
    """该表达式链里是否出现 headers（属性 `x.headers` 或裸名 `headers`）。"""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Attribute) and sub.attr == HEADERS_ATTR:
            return True
        if isinstance(sub, ast.Name) and sub.id == HEADERS_ATTR:
            return True
    return False


def _header_write_offenders_of_source(src, filename="<fixture>"):
    """该源码里"写入 headers 且携带签到域头"的位置 `[(行号, 原因)]`。"""
    tree = ast.parse(src, filename=filename)
    prose = _prose_string_ids(tree)
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _touches_headers(node.func):
            args = list(node.args) + [kw.value for kw in node.keywords]
            if any(_mentions_sign_domain(a) and not _only_prose(a, prose)
                   for a in args):
                out.append((node.lineno, "headers 调用携带签到域头"))
        elif isinstance(node, ast.AugAssign) and _touches_headers(node.target) \
                and _mentions_sign_domain(node.value):
            out.append((node.lineno, "headers 增量赋值携带签到域头"))
        elif isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Subscript) and _touches_headers(t.value)
                for t in node.targets) and _mentions_sign_domain(node.value):
            out.append((node.lineno, "headers 逐键赋值携带签到域头"))
    return sorted(set(out))


def _only_prose(node, prose_ids):
    """该子树的字符串常量是否全在散文语境里（避免报错文案误红）。"""
    consts = [sub for sub in ast.walk(node)
              if isinstance(sub, ast.Constant) and isinstance(sub.value, str)]
    return bool(consts) and all(id(c) in prose_ids for c in consts)


def _app_literal_sites_of_source(src, rel):
    """该文件里 App 域字面量的站点 `[(rel, 行号, 是否模块级简单赋值)]`。"""
    tree = ast.parse(src, filename=rel)
    prose = _prose_string_ids(tree)
    defined = _simple_module_assign_const_ids(tree)
    sites = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        if id(node) in prose or APP_DOMAIN_LITERAL not in node.value:
            continue
        sites.append((rel, node.lineno, id(node) in defined))
    return sites


class SignSessionSingleDefinitionPointTest(unittest.TestCase):
    def test_scan_face_is_not_empty(self):
        """扫空不得当绿：扫描面必须真的覆盖到生产模块。"""
        files = _iter_python_files()
        self.assertGreater(len(files), 100,
                           "扫描面异常（只找到 %d 个 .py），判据会假绿" % len(files))
        self.assertIn(CLIENT_REL, files)
        self.assertIn(PLATFORM_REL, files)

    def test_docker_face_is_scanned(self):
        """容器入口是生产载体：`docker/` 必须在面内且真的扫到文件。"""
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

    def test_no_header_write_carries_the_sign_domain(self):
        """除 client.py 外，任何 headers 写入都不得携带签到域头。

        `client.py` 的唯一实现点由 `test_client_has_exactly_one_header_reference`
        单独钉住；这里对其余全部生产模块（含 platform.py 自己）生效。
        """
        offenders = []
        for rel in _iter_python_files():
            if rel == CLIENT_REL:
                continue
            with open(os.path.join(REPO_ROOT, rel), encoding="utf-8") as fh:
                hits = _header_write_offenders_of_source(fh.read(), rel)
            offenders.extend("%s:%d %s" % (rel, line, why) for line, why in hits)
        self.assertEqual(offenders, [],
                         "这些位置把签到域头写进会话（应走 "
                         "YibanClient._ensure_sign_session）：\n" + "\n".join(offenders))

    def test_app_domain_has_exactly_one_literal(self):
        """App 域字面量全仓**恰好 1 处**，且必须是 platform.py 的模块级简单赋值。"""
        sites = []
        for rel in _iter_python_files():
            with open(os.path.join(REPO_ROOT, rel), encoding="utf-8") as fh:
                sites.extend(_app_literal_sites_of_source(fh.read(), rel))
        self.assertEqual(
            len(sites), 1,
            "App 域字面量应全仓恰好 1 处（唯一定义点），实际 %d 处：%s。"
            "新增同域请求头请引用 platform.APP_ORIGIN。" % (len(sites), sites))
        rel, line, in_def = sites[0]
        self.assertEqual(rel, PLATFORM_REL,
                         "唯一那处必须在 yiban/platform.py：%s:%d" % (rel, line))
        self.assertTrue(in_def,
                        "唯一那处必须是模块级简单赋值（常量定义）：%s:%d" % (rel, line))


class DetectorHasTeethTest(unittest.TestCase):
    """判据自身有牙：危险形态必红；散文与合法用法不得误红。"""

    def _offenders(self, src, rel=PLATFORM_REL):
        return _header_write_offenders_of_source(src, rel)

    def test_sign_domain_writes_are_caught_in_every_form(self):
        cases = (
            "def f(headers):\n    headers.update(APP_SIGN_HEADERS)",
            "def f(headers):\n    headers.update({'Origin': APP_ORIGIN})",
            "def f(headers):\n    headers.update(HEADERS)",
            "def f(headers):\n    headers.update({'Origin': HEADERS['Origin']})",
            "def f(headers):\n    headers |= APP_SIGN_HEADERS",
            "def f(headers):\n    headers |= {'Origin': APP_ORIGIN}",
            "def f(headers):\n    headers += APP_SIGN_HEADERS",
            "def f(headers):\n    headers.setdefault('Origin', APP_ORIGIN)",
            "def f(headers):\n    headers['Origin'] = APP_ORIGIN",
            "def f(s):\n    s.headers.update({'Origin': 'https://app.uyiban.com'})",
            "def f(s):\n    s.headers.update(getattr(__import__('yiban.platform'),"
            " 'APP_SIGN_HEADERS'))",
        )
        for src in cases:
            with self.subTest(src=src):
                self.assertTrue(self._offenders(src),
                                "切头写入未被判出：%r" % (src,))

    def test_prose_is_not_a_write_nor_a_literal_site(self):
        cases = (
            '"""docstring 提到 APP_SIGN_HEADERS 与 https://app.uyiban.com 不算读者。"""',
            "# 注释里提到 APP_SIGN_HEADERS 与 https://app.uyiban.com\ndef f():\n    pass",
            "import logging\nlogging.warning('https://app.uyiban.com APP_SIGN_HEADERS')",
            "def f():\n    raise RuntimeError('cannot reach https://app.uyiban.com')",
            "def f():\n    print('https://app.uyiban.com')",
            "def f(x):\n    assert x, 'https://app.uyiban.com 不可达'",
        )
        for src in cases:
            with self.subTest(src=src):
                self.assertEqual(self._offenders(src), [],
                                 "散文被误判：%r" % (src,))

    def test_legal_consumption_is_not_flagged(self):
        cases = (
            "APP_ORIGIN = 'https://app.uyiban.com'\n"
            "A = {'Origin': APP_ORIGIN}\nB = {'Referer': APP_ORIGIN + '/'}",
            "APP_SIGN_HEADERS = {}\nC = APP_SIGN_HEADERS.copy()",
            "def f():\n    return APP_SIGN_HEADERS.copy()",
            "def f(s):\n    s.headers.update({'X': 'y'})",
        )
        for src in cases:
            with self.subTest(src=src):
                self.assertEqual(self._offenders(src), [],
                                 "合法形态被误红：%r" % (src,))

    def test_literal_must_be_the_single_definition(self):
        ok = ('APP_ORIGIN = "https://app.uyiban.com"\n'
              'A = {"Origin": APP_ORIGIN}')
        sites = _app_literal_sites_of_source(ok, PLATFORM_REL)
        self.assertEqual(len(sites), 1)
        self.assertTrue(sites[0][2])
        for src, rel in (
            ('def f():\n    return "https://app.uyiban.com"', PLATFORM_REL),
            ('if True:\n    S.headers.update({"Origin": "https://app.uyiban.com"})',
             PLATFORM_REL),
            ('X = {"Origin": "https://app.uyiban.com"}', "yiban/other.py"),
            ('S.headers["Origin"] = "https://app.uyiban.com"', PLATFORM_REL),
        ):
            with self.subTest(src=src):
                sites = _app_literal_sites_of_source(src, rel)
                self.assertFalse(
                    len(sites) == 1 and sites[0][2] and rel == PLATFORM_REL,
                    "越界的 App 域字面量被当成唯一定义：%r" % (src,))


if __name__ == "__main__":
    unittest.main()
