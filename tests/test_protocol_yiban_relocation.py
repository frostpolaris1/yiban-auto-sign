# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""洁净室协议库搬家后的守卫（仓库结构整理批 A2）。

标签：K · 登录协议与自研链路
覆盖：本批把洁净室协议库从 `yiban/protocol/` 搬到 `yiban/adapters/protocol_yiban/`
    （目标架构契约 `docs/dev/architecture-target.md` 第 3 节）。本文件钉四条不变式：
    ① **旧坐标消失**：`yiban/protocol/` 不存在——留着它就是第二份实现。
    ② **许可与规格随包**：`LICENSE`（MIT）与 `SPEC.md` 在新坐标下在位。
    ③ **洁净室仍然洁净**：新坐标下每个 `.py` 都不引仓内任何东西。判据是 AST
       扫 import 语句，加一条**字符串面**扫描（动态导入的模块名）。
       为什么另加 AST 判据：import-linter 走静态图，`importlib.import_module("yiban.x")`
       这类字符串拼接不在它的判定面内（执行器 docstring 已如实登记该弱化点）。
    ④ **消费方指向新坐标**：`yiban/platform.py` / `yiban/challenge.py` /
       `yiban/client.py` 只引新坐标，旧坐标字样清零。
对应实现：`yiban/adapters/protocol_yiban/`（库本体）、`.importlinter` 的
    `protocol-clean-room` 契约（图判据）、`tests/test_provenance_guard.py`（单点定义）。
关键断言：① ② ④ 读文件系统与源码文本；③ 用 `ast` 解析每个 `.py`。
依赖：只读仓库文件，不导入库、不联网、不建库。无 skip。
"""
import ast
import io
import os
import re
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NEW_DIR = os.path.join("yiban", "adapters", "protocol_yiban")
OLD_DIR = os.path.join("yiban", "protocol")

#: 仓内顶层包名：洁净室不许引它们中的任何一个。
IN_REPO_ROOTS = ("yiban", "web", "scripts", "tests", "docker", "frontend", "deploy", "config")

#: 消费方（生产树里引本库的文件）。目标树把适配器留给调用方注入策略。
CONSUMERS = ("yiban/platform.py", "yiban/challenge.py", "yiban/client.py")

#: 旧坐标的字样（正则）：`yiban.protocol` 后面不接 `_yiban`。
OLD_DOTTED_RE = re.compile(r"\byiban\.protocol(?!_yiban)")
OLD_SLASH_RE = re.compile(r"\byiban/protocol(?!_yiban)")


def _read(rel):
    with io.open(os.path.join(BASE, rel), encoding="utf-8") as f:
        return f.read()


def _py_files(rel_dir):
    root = os.path.join(BASE, rel_dir)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for name in sorted(filenames):
            if name.endswith(".py"):
                yield os.path.relpath(os.path.join(dirpath, name), BASE).replace(os.sep, "/")


class CleanRoomRelocationTest(unittest.TestCase):
    """①② 坐标与随包产物。"""

    def test_old_coordinate_is_gone(self):
        self.assertFalse(
            os.path.exists(os.path.join(BASE, OLD_DIR)),
            f"{OLD_DIR}/ 仍在：那是协议库的第二份实现（或一份转发壳）。"
            "洁净室不得引仓内任何东西，故转发壳会把洁净室判据自己弄红。")

    def test_library_and_its_license_live_at_the_new_coordinate(self):
        for name in ("__init__.py", "LICENSE", "SPEC.md", "crypto.py", "forms.py",
                     "envelopes.py", "position.py", "page.py", "device.py",
                     "identity.py", "location.py", "errors.py"):
            with self.subTest(name=name):
                self.assertTrue(os.path.isfile(os.path.join(BASE, NEW_DIR, name)),
                                f"新坐标缺 {NEW_DIR}/{name}")

    def test_only_one_copy_of_the_library_exists_under_yiban(self):
        """`yiban/` 下只许一处 SPEC.md：两处即第二份实现。"""
        found = []
        for dirpath, dirnames, filenames in os.walk(os.path.join(BASE, "yiban")):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            if "SPEC.md" in filenames:
                found.append(os.path.relpath(os.path.join(dirpath, "SPEC.md"), BASE)
                             .replace(os.sep, "/"))
        self.assertEqual(found, [f"{NEW_DIR}/SPEC.md"],
                         "yiban/ 下的 SPEC.md 不止一处（或不在新坐标）：" + "; ".join(found))


class CleanRoomPurityTest(unittest.TestCase):
    """③ 洁净室不引仓内任何东西（AST 判据 + 字符串面判据）。"""

    def _imported_names(self, path):
        """AST 里出现的绝对导入模块名（含 `from X import y` 的 X）。"""
        with io.open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=path)
        names = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level:      # 包内相对导入：洁净室允许
                    continue
                if node.module:
                    names.append(node.module)
        return names

    def test_no_module_in_the_library_imports_this_repo(self):
        bad = []
        for rel in _py_files(NEW_DIR):
            for name in self._imported_names(os.path.join(BASE, rel)):
                if name.split(".")[0] in IN_REPO_ROOTS:
                    bad.append(f"{rel} → {name}")
        self.assertEqual(bad, [], "洁净室引了仓内模块（契约 protocol-clean-room 的语义）："
                                  + "; ".join(bad))

    def test_no_dynamic_import_string_names_this_repo(self):
        """字符串面判据：`importlib.import_module("yiban.x")` 这类写法不在图判据面内。"""
        bad = []
        for rel in _py_files(NEW_DIR):
            for m in re.finditer(r"""["']([A-Za-z_][\w.]*)["']""", _read(rel)):
                if m.group(1).split(".")[0] in IN_REPO_ROOTS:
                    bad.append(f"{rel}: {m.group(1)}")
        self.assertEqual(bad, [], "洁净室的字符串面里出现仓内模块名："
                                  + "; ".join(bad))

    def test_scan_surface_is_alive(self):
        """防废门：扫描面必须真的走到库文件。"""
        self.assertGreaterEqual(len(list(_py_files(NEW_DIR))), 10,
                                f"{NEW_DIR} 下扫到的 .py 太少：扫描面废了")


class ConsumerCoordinateTest(unittest.TestCase):
    """④ 生产消费方只引新坐标。"""

    def test_consumers_use_the_new_coordinate(self):
        for rel in CONSUMERS:
            with self.subTest(rel=rel):
                src = _read(rel)
                self.assertIn("yiban.adapters.protocol_yiban", src,
                              f"{rel} 没有引用新坐标")

    def test_consumers_have_no_reference_to_the_old_coordinate(self):
        for rel in CONSUMERS:
            with self.subTest(rel=rel):
                src = _read(rel)
                self.assertIsNone(OLD_DOTTED_RE.search(src),
                                  f"{rel} 里仍有旧坐标 `yiban.protocol` 字样")
                self.assertIsNone(OLD_SLASH_RE.search(src),
                                  f"{rel} 里仍有旧坐标 `yiban/protocol` 路径")


if __name__ == "__main__":
    unittest.main(verbosity=2)
