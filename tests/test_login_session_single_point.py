# -*- coding: utf-8 -*-
"""登录后"切回签到域请求头"只有一个实现点（工单 vq97）。

为什么钉这条
------------
签到链有两个前提，顺序不可拆：

1. 先登录——旧流的登录链会把 `Origin`/`Referer` 改成 `c.uyiban.com`（OAuth 域）；
2. 旧流在拉签到任务前必须把会话头改回 App 域（`app.uyiban.com`）。
   标准 App 特征流（`KILLYIBAN_HEADERS`）保持 `c.uyiban.com`，实测可用，不回切。

顺序写错或漏掉第 2 步，旧流的签到请求会带 OAuth 域头上游（实测回 e001）。
这条规则曾以**逐字相同的三份代码**存在：`signin()`、`verify()`、
以及一条独立分支上的只读预演脚本。第三份是在主树之外被发现的——共享事实门
当场超标（命中 9 / 上限 8）。

判据
----
四条不变式，全部按 **AST** 判（不看注释；docstring 不计）：

① `APP_SIGN_HEADERS` 这个名字在 `yiban/client.py` 里**恰好出现 1 次**——
   任何写法都算：属性引用、裸名、`from ... import APP_SIGN_HEADERS [as X]`、
   `getattr(mod, "APP_SIGN_HEADERS")` 的字符串实参；
② 其余生产模块零出现；`yiban/platform.py` **恰好 1 次**（常量定义本身），
   不许整文件豁免——否则该文件里加第二处切换会被静默放过；
③ App 域字面量 `https://app.uyiban.com` 在 `yiban/platform.py` 之外**零出现**。
   这条抓"不写常量名"的等价手抄：裸 dict、`session.headers["Origin"] = ...`；
④ 扫空不得当绿：扫描面有下限，且 `docker/` 必须在面内（容器入口也 import 签到模块）。

②③ 的槽位是**有意留的唯一的**：新增入口必须复用
`YibanClient._ensure_sign_session()`，不许复制这两行。
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


def _refs_of_source(src, filename="<fixture>"):
    """一段源码里 `APP_SIGN_HEADERS` 的出现位置 `[(行号, 形态)]`（判据唯一实现）。

    覆盖五种写法，避免"只认属性名"被等价形态绕过：

    - `mod.APP_SIGN_HEADERS`（属性）
    - `APP_SIGN_HEADERS`（裸名，含 `X = APP_SIGN_HEADERS` 的赋值别名）
    - `from mod import APP_SIGN_HEADERS [as X]`（导入别名）
    - `getattr(mod, "APP_SIGN_HEADERS")`（字符串实参）
    - docstring 以外的字符串常量里包含该名

    docstring 与注释不计：散文说明不是读者。
    """
    tree = ast.parse(src, filename=filename)
    docs = _docstring_const_ids(tree)
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
                and id(node) not in docs and HEADER_NAME in node.value:
            refs.append((node.lineno, "字符串实参"))
    return sorted(set(refs))


def _header_refs(path):
    """`path` 里的引用点（读文件后走判据唯一实现）。"""
    with open(os.path.join(REPO_ROOT, path), encoding="utf-8") as fh:
        return _refs_of_source(fh.read(), filename=path)


def _app_domain_lines(path):
    """`path` 里 App 域字面量出现的行号（docstring 与注释不计）。"""
    with open(os.path.join(REPO_ROOT, path), encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=path)
    docs = _docstring_const_ids(tree)
    lines = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and id(node) not in docs and APP_DOMAIN_LITERAL in node.value:
            lines.append(node.lineno)
    return sorted(set(lines))


class SignSessionSingleDefinitionPointTest(unittest.TestCase):
    def test_scan_face_is_not_empty(self):
        """扫空不得当绿：扫描面必须真的覆盖到生产模块与容器入口。"""
        files = _iter_python_files()
        self.assertGreater(len(files), 100,
                           "扫描面异常（只找到 %d 个 .py），判据会假绿" % len(files))
        self.assertIn(CLIENT_REL, files)
        self.assertIn(PLATFORM_REL, files)

    def test_docker_face_is_scanned(self):
        """容器入口是生产载体：`docker/` 必须在面内（兄弟门同口径）。"""
        docker_files = [f for f in _iter_python_files() if f.startswith("docker/")]
        if not os.path.isdir(os.path.join(REPO_ROOT, "docker")):
            self.skipTest("本树没有 docker/ 目录（面登记见 SCAN_DIRS）")
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

    def test_platform_has_only_the_constant_definition(self):
        """`platform.py` 只有常量定义 1 次——整文件豁免会放过第二处切换。"""
        refs = _header_refs(PLATFORM_REL)
        self.assertEqual(
            len(refs), 1,
            "yiban/platform.py 只该有 `APP_SIGN_HEADERS = {...}` 这一处，实际 %d 处：%s。"
            "第二处切换会绕过唯一实现点。" % (len(refs), refs))

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

    def test_app_domain_literal_is_confined_to_platform(self):
        """App 域字面量只在 platform.py：抓"不写常量名"的裸 dict／逐键赋值。"""
        offenders = []
        for rel in _iter_python_files():
            if rel == PLATFORM_REL:
                continue
            lines = _app_domain_lines(rel)
            if lines:
                offenders.append("%s:%s" % (rel, ",".join(map(str, lines))))
        self.assertEqual(
            offenders, [],
            "这些生产模块内联了 App 域字面量（不经 yiban/platform.APP_SIGN_HEADERS）："
            "%s" % offenders)
        # 定义处只该有两组（HEADERS 与 APP_SIGN_HEADERS 各一组 Origin/Referer）
        self.assertEqual(
            len(_app_domain_lines(PLATFORM_REL)), 4,
            "platform.py 的 App 域字面量行数变了（预期 4 行：HEADERS 与 "
            "APP_SIGN_HEADERS 各 Origin/Referer）：%s" % _app_domain_lines(PLATFORM_REL))


class DetectorHasTeethTest(unittest.TestCase):
    """判据自身有牙：五种等价写法必须被判出（防止门被悄悄收窄）。"""

    def test_all_equivalent_forms_are_caught(self):
        cases = (
            "import yiban.platform as p\np.APP_SIGN_HEADERS",
            "from yiban.platform import APP_SIGN_HEADERS as S\nS",
            "from yiban.platform import APP_SIGN_HEADERS\nAPP_SIGN_HEADERS",
            "import yiban.platform as p\ngetattr(p, 'APP_SIGN_HEADERS')",
            "TARGET = 'yiban.platform.APP_SIGN_HEADERS'",
        )
        for src in cases:
            with self.subTest(src=src):
                self.assertTrue(_refs_of_source(src),
                                "判据没有牙，等价写法未被判出：%r" % (src,))

    def test_docstring_and_comment_are_not_readers(self):
        cases = (
            '"""docstring 提到 APP_SIGN_HEADERS 不算读者。"""',
            "# 注释里提到 APP_SIGN_HEADERS 不算读者\nx = 1",
        )
        for src in cases:
            with self.subTest(src=src):
                self.assertEqual(_refs_of_source(src), [],
                                 "散文被误判成读者：%r" % (src,))


if __name__ == "__main__":
    unittest.main()
