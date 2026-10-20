# -*- coding: utf-8 -*-
"""登录后"切回签到域请求头"只有一个实现点（工单 vq97）。

为什么钉这条
------------
签到链有两个前提，顺序不可拆：

1. 先登录——登录链会把 `Origin`/`Referer` 改成 OAuth/回调域；
2. 再把旧流程的会话头切回 App 域（`APP_SIGN_HEADERS`）。

顺序写错或漏掉第 2 步，签到请求会带 OAuth 域头上游（实测回 e001）。
这条规则曾以**逐字相同的三份代码**存在：`signin()`、`verify()`、
以及 `scripts/photo_preflight.py` 手抄的一份。第三份是在主树之外被发现的
——共享事实门当场超标（命中 9 / 上限 8）。

判据取**不变式**而不是调用顺序：`APP_SIGN_HEADERS` 的引用在
`yiban/client.py` 里**恰好一处**，其余生产模块**零处**。这样无论以后新增多少
"登录后拉任务"的入口，只要它们复用同一方法就不会红；手抄第四份则立刻红。
"""
import ast
import os
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLIENT_REL = os.path.join("yiban", "client.py")
SCAN_DIRS = ("yiban", "web", "scripts")
SKIP_DIR_NAMES = {"__pycache__", ".mypy_cache", ".pytest_cache"}


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


def _header_switch_refs(path):
    """返回 `path` 里引用 `APP_SIGN_HEADERS` 的 AST 节点行号。

    只看 AST，不看文本：注释与 docstring 里的说明不算引用点，
    真正的代码引用（`yiban_platform.APP_SIGN_HEADERS` 或裸名）才算。
    """
    with open(os.path.join(REPO_ROOT, path), encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=path)
    lines = []
    for node in ast.walk(tree):
        is_ref = (
            (isinstance(node, ast.Attribute) and node.attr == "APP_SIGN_HEADERS")
            or (isinstance(node, ast.Name) and node.id == "APP_SIGN_HEADERS")
        )
        if is_ref:
            lines.append(node.lineno)
    return sorted(lines)


class SignSessionSingleDefinitionPointTest(unittest.TestCase):
    def test_scan_face_is_not_empty(self):
        """扫空不得当绿：扫描面必须真的覆盖到生产模块。"""
        files = _iter_python_files()
        self.assertGreater(len(files), 100,
                           "扫描面异常（只找到 %d 个 .py），判据会假绿" % len(files))
        self.assertIn(CLIENT_REL.replace(os.sep, "/"), files)

    def test_client_has_exactly_one_header_switch_reference(self):
        """`client.py` 里 `APP_SIGN_HEADERS` 恰好引用一次（唯一实现点）。"""
        refs = _header_switch_refs(CLIENT_REL)
        self.assertEqual(
            len(refs), 1,
            "yiban/client.py 应只有一个切回签到域请求头的实现点，实际 %d 处（行 %s）。"
            "新增入口请复用 YibanClient._ensure_sign_session()。" % (len(refs), refs))

    def test_no_other_module_hand_copies_the_switch(self):
        """其余生产模块零引用：手抄第二份会让签到头形状分叉。"""
        offenders = []
        for rel in _iter_python_files():
            if rel == CLIENT_REL.replace(os.sep, "/"):
                continue
            if rel.startswith("yiban/platform.py"):
                continue  # 定义处：APP_SIGN_HEADERS 的常量本体
            refs = _header_switch_refs(rel)
            if refs:
                offenders.append("%s:%s" % (rel, ",".join(map(str, refs))))
        self.assertEqual(
            offenders, [],
            "这些生产模块手抄了签到域请求头切换（应改调 "
            "YibanClient._ensure_sign_session()）：%s" % offenders)


if __name__ == "__main__":
    unittest.main()
