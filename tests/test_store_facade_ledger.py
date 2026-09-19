# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""db 门面总账：转发面逐名落真定义、再导出模块清单与 docstring 一致、门面形态不漂移。

表级访问已按域拆入同包模块，`yiban/store/db.py` 只余门面（启动编排、密钥来源解析、写
事务入口）与跨域粘合助手；各域模块反向经门面按属性取粘合函数，兼容面靠
`_FORWARDED_STATE` 的读写转发维持。六份分域契约各自钉住本域名字，本文件做**跨域总账**，
防三类静默漂移：

1. **转发集塞错名字**：`_FORWARDED_STATE` 的每一项都必须在目标模块真定义、且不得同时被
   db 自己持有——否则转发是假的（读取落在一份陈旧副本上、写入落不到真状态）。
2. **docstring 清单失真**：模块 docstring 列出的"再导出同包模块"必须与实际 `import` 的
   store 子模块逐一对应（多列是假话，少列让读者找不到定义点）。
3. **门面形态漂移**：db.py 顶层只剩门面与粘合函数；docstring 点名的留守名字必须真的还在
   本模块定义，顶层定义集也必须与预期一致（新增/迁出都得是有意识的一步）。
"""
import ast
import os
import re
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from yiban.store import db as impl  # noqa: E402

DB_PATH = os.path.join(BASE, "yiban", "store", "db.py")

# 兼容面转发项数：扩缩转发集必须同步此数（分域契约钉名字，这里钉总量）
FORWARDED_COUNT = 57

# db.py 顶层函数定义：门面 + 跨域粘合助手 + PEP 562 转发钩子
EXPECTED_MODULE_FUNCS = {
    "__getattr__",
    "init_db",
    "resolve_env_file",
    "require_existing_env_file",
    "_begin_immediate",
    "_clock_jump_guard",
    "_record_purge_event",
    "_table_min_max",
    "_cascade_phone_owned",
    "_clear_session_cache_by_phones",
}
# db.py 顶层类定义：唯一一个模块类（转发写入/撤销）
EXPECTED_MODULE_CLASSES = {"_StateForwardingModule"}


def _source():
    with open(DB_PATH, encoding="utf-8") as f:
        return f.read()


def _module_defs():
    """db.py 顶层函数名与类名（不含 import 与赋值）。"""
    funcs, classes = set(), set()
    for node in ast.parse(_source()).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            funcs.add(node.name)
        elif isinstance(node, ast.ClassDef):
            classes.add(node.name)
    return funcs, classes


def _reexported_store_modules():
    """`from yiban.store import X as _X`（顶层）收集到的子模块名。"""
    mods = set()
    for node in ast.parse(_source()).body:
        if isinstance(node, ast.ImportFrom) and node.module == "yiban.store":
            for alias in node.names:
                mods.add(alias.name)
    return mods


def _docstring_listed_modules():
    """模块 docstring 中「再导出的同包模块」清单里的反引号模块名。"""
    return set(re.findall(r"(?m)^- `([a-z_]+)`：", impl.__doc__ or ""))


def _docstring_retained_names():
    """「本模块自身仍持有」段里点名的裸标识符（反引号包裹、无点无括号）。"""
    doc = impl.__doc__ or ""
    idx = doc.find("本模块自身仍持有")
    if idx < 0:
        return set()
    return set(re.findall(r"`([A-Za-z_][A-Za-z0-9_]*)`", doc[idx:]))


class ForwardedStateLedgerTest(unittest.TestCase):
    """跨域转发面总账：每一项都必须是"真定义在别处、门面只转发"。"""

    def test_forwarded_count_is_pinned(self):
        self.assertEqual(
            len(impl._FORWARDED_STATE), FORWARDED_COUNT,
            "兼容面转发集项数变化：确认是有意扩缩后同步 FORWARDED_COUNT 与分域契约",
        )

    def test_every_forwarded_name_defined_at_target(self):
        bad = []
        for name, mod in impl._FORWARDED_STATE.items():
            if name in vars(impl):
                bad.append(f"{name}: 同时被 db 自己持有（转发面是假的）")
            elif not hasattr(mod, name):
                bad.append(f"{name}: 在目标模块 {mod.__name__} 未定义")
            elif getattr(impl, name) is not getattr(mod, name):
                bad.append(f"{name}: db 与 {mod.__name__} 上的不是同一对象")
        self.assertEqual(
            bad, [],
            "转发集与真定义点不一致（防往转发集里塞错名字）：\n" + "\n".join(bad),
        )

    def test_no_hidden_forwarded_names_leak(self):
        """`_FORWARDED_STATE_HIDDEN` 是 delattr 的临时摘名表，用例间不得有残留。"""
        self.assertEqual(
            set(impl._FORWARDED_STATE_HIDDEN), set(),
            "有转发名停在隐藏态（delattr 后未恢复）：后续读取会误报缺失，打桩撤销会残留",
        )


class FacadeDocstringLedgerTest(unittest.TestCase):
    """docstring 的"再导出模块"与"留守"两张清单必须与实际一致。"""

    def test_docstring_lists_every_reexported_store_module(self):
        self.assertEqual(
            _docstring_listed_modules(), _reexported_store_modules(),
            "docstring 的再导出模块清单与实际 `from yiban.store import …` 不一致",
        )

    def test_retained_names_are_defined_here(self):
        names = _docstring_retained_names()
        self.assertTrue(names, "docstring 未点名任何留守名字（清单被删或改写了格式）")
        for name in names:
            self.assertIn(name, vars(impl), f"docstring 说 {name} 留守，但 db 未定义它")
            self.assertEqual(
                getattr(vars(impl)[name], "__module__", None), "yiban.store.db",
                f"docstring 说 {name} 留守，但它的定义点不在本模块",
            )

    def test_module_defines_only_facade_and_glue(self):
        funcs, classes = _module_defs()
        self.assertEqual(
            funcs, EXPECTED_MODULE_FUNCS,
            "db.py 顶层函数定义集变化：新迁出/迁入都要有意识（并同步此表与 docstring）",
        )
        self.assertEqual(
            classes, EXPECTED_MODULE_CLASSES,
            "db.py 顶层类定义集变化：转发模块类应唯一",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
