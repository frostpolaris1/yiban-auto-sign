# -*- coding: utf-8 -*-
"""accounts 行重建路径的列覆盖门（工单 qmz4 · AGENTS §27 形状门）。

标签：C · 存储：迁移/导入
覆盖：两条**整行重建** INSERT —— `migrations._maybe_migrate`（JSON→SQLite 导入）
    与 `accounts.replace_accounts`（整表替换）的列清单，与 accounts 表**实际 schema**
    逐列对账。
对应实现：`yiban/adapters/persistence/migrations.py::_maybe_migrate`、
    `yiban/adapters/persistence/accounts.py::replace_accounts`。

**为什么需要**（缺陷形状，不修实例）：accounts 每加一列，这两条手写列清单就多一个
漏写点；漏写的后果不是报错，而是"导出→干净导入/整表替换后该列静默回到默认值"。
qmz4 之前 profile/profile_expose 就是这样丢的（审查中-2）。逐列打补丁只能修当下这一列，
故按形状立门：列清单必须覆盖 accounts 全部列，未覆盖者必须**逐列登记豁免理由**。

关键断言：
    ① 两条清单都能被解析出来且非空（解析器返回空集时"覆盖全部列"恒假，须显式判红
      ——扫空不得当绿，AGENTS §30）；两条清单都含 profile / profile_expose；
    ② 每条清单的未覆盖列**恰好等于**该清单的豁免登记集（逐清单登记，不取并集——
      并集口径下"从 A 清单删一列"会恰好等于 B 清单的既有缺口，门仍绿，即可绕过）；
    ③ 豁免登记集必须有理由，且两条清单的登记集并集等于理由表键集；
    ④ 突变自证：把任意一条清单里的任意一列从**源码文本**删掉，门的判定必须变红
      （不是只测谓词，而是真的过一遍源码解析）。

依赖：临时库（db.init_db 跑到链尾）+ 源码文本；不联网、不 skip。
"""
import ast
import contextlib
import os
import re
import shutil
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import db  # noqa: E402

MIGRATIONS_SRC = os.path.join(BASE, "yiban", "adapters", "persistence", "migrations.py")
ACCOUNTS_SRC = os.path.join(BASE, "yiban", "adapters", "persistence", "accounts.py")

#: 门看守的两条行重建路径：(源码文件, 函数名)。
TARGETS = ((MIGRATIONS_SRC, "_maybe_migrate"), (ACCOUNTS_SRC, "replace_accounts"))

#: 未覆盖列的**逐清单**豁免登记（AGENTS §28：每条约束要么有门、要么显式豁免）。
#: 必须逐清单而不是取并集：并集口径下"从 A 清单删掉 user_paused"恰好等于 B 清单的
#: 现有缺口，门仍绿——那就是可绕过的门。理由见 `EXEMPTION_REASONS`。
ALLOWED_UNCOVERED = {
    "_maybe_migrate": frozenset({"deleted_by", "user_paused"}),
    "replace_accounts": frozenset({"deleted_by"}),
}

#: 豁免理由（每条必须在册；是否回填由台账另立单，不在本门范围）。
EXEMPTION_REASONS = {
    "deleted_by": "历史实例：默认空串语义；回放后恢复默认（是否回填另立台账单）",
    "user_paused": "历史实例：默认 0 语义；回放后恢复默认（是否回填另立台账单）",
}

#: 结构性自动列：SQLite 生成，不可能也不需写入，因此不算"未覆盖的数据列"。
AUTO_COLUMNS = {"id": "INTEGER PRIMARY KEY AUTOINCREMENT，由 SQLite 生成"}

#: INSERT 里必须出现的列（qmz4 的回归锚：这两列曾从两条清单里缺失）。
REQUIRED_COLUMNS = {"profile", "profile_expose"}

_INSERT_ACCOUNTS = re.compile(
    r"INSERT(?:[^()]|\n)*?INTO\s+accounts\s*\(([^)]*)\)")


def _insert_column_list(source, func_name):
    """从源码文本取目标函数里 `INSERT ... INTO accounts (...)` 的列清单原文。

    只认函数体内的字符串常量（`conn.execute("INSERT ...")`），返回 `(函数源码段, 列清单原文)`。
    找不到即返回 `(None, None)`——调用方必须判红（解析失败不是"通过"）。
    """
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            for call in ast.walk(node):
                if not isinstance(call, ast.Call):
                    continue
                for arg in call.args:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        m = _INSERT_ACCOUNTS.search(arg.value)
                        if m:
                            return arg.value, m.group(1)
    return None, None


def parse_columns(text):
    """列清单文本 → 列名列表（保序去空白）。空文本回空列表，调用方须判红。"""
    return [c.strip() for c in text.split(",") if c.strip()]


def uncovered(columns, schema, exempt=AUTO_COLUMNS):
    """schema 里既不在列清单、也不在结构自动列里的列（按 schema 顺序）。"""
    return [c for c in schema if c not in columns and c not in exempt]


class AccountRowRebuildCoverageTest(unittest.TestCase):
    """两条整行重建 INSERT 的列覆盖对账。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-rowcov-")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write("YIBAN_ACCOUNTS_KEY=" + "a" * 64 + "\n")
        os.environ["YIBAN_ACCOUNTS_KEY"] = "a" * 64
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_DB_FILE"] = cls.db_file
        os.environ["YIBAN_STATE_DIR"] = cls.tmp
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        conn = db.init_db(cls.db_file, env_file=cls.env_file, cleanup=False)
        cls.schema = [r["name"] for r in
                      conn.execute("PRAGMA table_info(accounts)").fetchall()]
        cls.sources = {}
        for path, func in TARGETS:
            with open(path, encoding="utf-8") as f:
                cls.sources[(path, func)] = f.read()

    @classmethod
    def tearDownClass(cls):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k in ("YIBAN_ACCOUNTS_KEY", "YIBAN_ENV_FILE", "YIBAN_DB_FILE",
                  "YIBAN_STATE_DIR"):
            os.environ.pop(k, None)

    # ---- ① 解析器有牙 ----
    def test_both_lists_parse_and_are_non_empty(self):
        for path, func in TARGETS:
            with self.subTest(func=func):
                _sql, cols_text = _insert_column_list(self.sources[(path, func)], func)
                self.assertIsNotNone(cols_text, f"{func} 的 INSERT 列清单必须可解析")
                cols = parse_columns(cols_text)
                self.assertGreaterEqual(len(cols), 10,
                                        f"{func} 的列清单解析结果过短（解析器可能失效）")
                self.assertTrue(REQUIRED_COLUMNS.issubset(set(cols)),
                                f"{func} 的列清单必须覆盖 {sorted(REQUIRED_COLUMNS)}")

    def test_schema_contains_the_profile_columns(self):
        """schema 侧自证：门比的真是"迁移跑完后的 accounts 表"，不是手抄清单。"""
        self.assertIn("profile", self.schema)
        self.assertIn("profile_expose", self.schema)

    # ---- ②③ 未覆盖列 = 该清单的豁免登记 ----
    def test_registry_entries_all_carry_reasons(self):
        union = set()
        for func, allowed in ALLOWED_UNCOVERED.items():
            with self.subTest(func=func):
                self.assertTrue(allowed, f"{func} 的豁免集不得为空（空集会掩盖真实缺口）")
                union.update(allowed)
        self.assertEqual(union, set(EXEMPTION_REASONS),
                         "豁免登记表与理由表必须等集：登记了却没写理由＝静默放宽")

    def test_uncovered_columns_are_exactly_the_registered_exemptions(self):
        for path, func in TARGETS:
            with self.subTest(func=func):
                _sql, cols_text = _insert_column_list(self.sources[(path, func)], func)
                cols = parse_columns(cols_text)
                missing = set(uncovered(cols, self.schema))
                allowed = ALLOWED_UNCOVERED[func]
                self.assertEqual(sorted(missing - allowed), [],
                                 f"{func} 的列清单漏了未登记的列："
                                 f"{sorted(missing - allowed)}（漏写 = 回放后该列静默回默认值）")
                self.assertEqual(sorted(allowed - missing), [],
                                 f"{func} 的豁免登记过期：{sorted(allowed - missing)} 已被覆盖，"
                                 "应从登记表删掉")

    # ---- ④ 突变自证：源码里删任一列必须红 ----
    def test_removing_any_column_from_any_list_turns_the_gate_red(self):
        for path, func in TARGETS:
            source = self.sources[(path, func)]
            _sql, cols_text = _insert_column_list(source, func)
            cols = parse_columns(cols_text)
            for victim in cols:
                with self.subTest(func=func, column=victim):
                    remaining = [c for c in cols if c != victim]
                    self.assertEqual(source.count(cols_text), 1,
                                     "列清单原文在源码里必须唯一，否则突变替换的不是被测那条")
                    mutated = source.replace(cols_text, ", ".join(remaining), 1)
                    self.assertNotEqual(mutated, source, "突变未生效（文本未替换）")
                    _m_sql, m_text = _insert_column_list(mutated, func)
                    self.assertIsNotNone(m_text, "突变后仍须可解析")
                    missing = set(uncovered(parse_columns(m_text), self.schema))
                    self.assertNotEqual(missing, ALLOWED_UNCOVERED[func],
                                        f"从 {func} 删掉 {victim} 后门必须红，"
                                        f"实得未覆盖={sorted(missing)}")


if __name__ == "__main__":
    unittest.main()
