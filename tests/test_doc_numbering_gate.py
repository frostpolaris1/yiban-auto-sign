# -*- coding: utf-8 -*-
"""文档编号唯一性巡检门（工单 b7la）。

标签：J · 运维：部署/备份/发布
覆盖：`scripts/check-doc-numbering.py` 与真实 `docs/` 树。
关键断言：① 同号文件/目录判红并逐组点名；② 唯一编号退 0；③ 空目录/不存在的根
    退 2（扫空不得当绿）；④ 数值比较（`007` 与 `7` 撞号）；⑤ 真树 `docs/` 可扫描
    且无撞号（本仓的编号文稿在 `docs/refactor/`；该目录按约定不入库，检出里可能
    不存在，此时校验器仍须显式打印 `带编号 0 个` 而不是假装扫过）。
依赖：只读文件系统；无网络、无第三方库。
"""
import contextlib
import importlib.util
import io
import os
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GATE = os.path.join(BASE, "scripts", "check-doc-numbering.py")


def _load():
    spec = importlib.util.spec_from_file_location("check_doc_numbering", GATE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run(mod, argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = mod.main(argv)
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 2
    return code, out.getvalue() + err.getvalue()


class NumberingGateTest(unittest.TestCase):
    def test_duplicate_numbers_are_red_and_named(self):
        with tempfile.TemporaryDirectory(prefix="yiban-docnum-") as root:
            for name in ("102-a.md", "102-b.md", "103-c.md"):
                open(os.path.join(root, name), "w", encoding="utf-8").close()
            rc, out = _run(_load(), ["--root", root])
            self.assertEqual(rc, 1, out)
            self.assertIn("撞号 102", out)
            self.assertIn("102-a.md", out)
            self.assertIn("102-b.md", out)

    def test_unique_numbers_are_green(self):
        with tempfile.TemporaryDirectory(prefix="yiban-docnum-") as root:
            for name in ("1-a.md", "2-b.md"):
                open(os.path.join(root, name), "w", encoding="utf-8").close()
            rc, out = _run(_load(), ["--root", root])
            self.assertEqual(rc, 0, out)
            self.assertIn("带编号 2 个", out)

    def test_file_and_directory_with_same_number_collide(self):
        with tempfile.TemporaryDirectory(prefix="yiban-docnum-") as root:
            os.makedirs(os.path.join(root, "84-assets"))
            open(os.path.join(root, "84-plan.md"), "w", encoding="utf-8").close()
            rc, out = _run(_load(), ["--root", root])
            self.assertEqual(rc, 1, out)
            self.assertIn("撞号 84", out)

    def test_numeric_comparison_catches_zero_padding(self):
        with tempfile.TemporaryDirectory(prefix="yiban-docnum-") as root:
            for name in ("7-a.md", "007-b.md"):
                open(os.path.join(root, name), "w", encoding="utf-8").close()
            rc, out = _run(_load(), ["--root", root])
            self.assertEqual(rc, 1, out)
            self.assertIn("撞号 7", out)

    def test_empty_dir_is_an_environment_error(self):
        with tempfile.TemporaryDirectory(prefix="yiban-docnum-") as root:
            rc, out = _run(_load(), ["--root", root])
            self.assertEqual(rc, 2, out)
            self.assertIn("扫空不得当绿", out)

    def test_missing_root_is_an_environment_error(self):
        rc, out = _run(_load(), ["--root", os.path.join(BASE, "no-such-docs-root")])
        self.assertEqual(rc, 2, out)
        self.assertIn("根目录不存在", out)

    def test_real_docs_tree_is_scannable(self):
        rc, out = _run(_load(), ["--root", os.path.join(BASE, "docs")])
        self.assertEqual(rc, 0, out)
        self.assertIn("扫描", out)


if __name__ == "__main__":
    unittest.main()
