# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""退出码表等值门：cli.md 码表、机读分族与代码常量逐码对拍。

标签：G · 安全：脱敏/审计/配置注入
覆盖：`yiban.engine.cli_support._EXIT_KINDS`、`SECOND_RUN_CHECK_NEED`、
    `yiban.app.runner.SECOND_RUN_CHECK_NEED`、`yiban.workers.workers._SECOND_RUN_CHECK_NEED`
    与 `docs/dev/cli.md` §3 码表、§3.1 `error_kind` 表。
对应实现：`cli_support` 是退出码与机读分族的唯一定义点；`runner` / `workers` 只取别名。
关键断言：
  ① §3 码表的码集合 == `_EXIT_KINDS` 的键集合（改码不改文档即红）；
  ② `_EXIT_KINDS` 的每个分族名都出现在 §3.1 `error_kind` 表；
  ③ runner / workers 的 10 常量与 `cli_support.SECOND_RUN_CHECK_NEED` 相等；
  ④ README 不另立第四份数字码表（登记豁免：只链接 cli.md）；
  ⑤ 解析器对合成码表报出差异、空文本回空表（扫空不得当绿）。
依赖：只读文档与模块级常量；不起服务、不发网络请求。
"""

import os
import re
import unittest

from yiban.app import runner
from yiban.engine import cli_support
from yiban.workers import workers

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLI_MD = os.path.join(BASE, "docs", "dev", "cli.md")
README = os.path.join(BASE, "README.md")

#: §3 退出码表：到 §3.1 为止（§3.1 的表第一列不是数字）。
_SECTION = re.compile(r"^## 3\. 退出码表.*?(?=^### 3\.1 )", re.MULTILINE | re.DOTALL)
_CODE_ROW = re.compile(r"^\|\s*(\d+)\s*\|", re.MULTILINE)
#: §3.1 的 error_kind 表：`| \`ok\` | …`。
_ERROR_KIND_ROW = re.compile(r"^\|\s*`([^`]+)`\s*\|", re.MULTILINE)
#: README 若另立数字码表，会命中这种表行；本仓登记为"无第四份表"。
README_CODE_ROW = re.compile(r"^\|\s*(?:0|1|2|3|4|10)\s*\|", re.MULTILINE)

#: 第四载体豁免登记：README 只给人类用法并链接契约，不重复数字表。
CARRIER_EXEMPTIONS = {
    "README.md": "只链接 docs/dev/cli.md，不另立数字码表；新增表行会触发本门",
}


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def section_text(text):
    """cli.md 全文 → §3 原文；小节缺失回空串。"""
    m = _SECTION.search(text)
    return m.group(0) if m else ""


def parse_codes(text):
    """§3 表文本 → 码集合；空文本回空集（调用方判红）。"""
    return {int(m.group(1)) for m in _CODE_ROW.finditer(text)}


def parse_error_kinds(text):
    """cli.md 全文 → §3.1 的 `error_kind` 名集合。"""
    start = text.find("### 3.1")
    if start < 0:
        return set()
    return {m.group(1) for m in _ERROR_KIND_ROW.finditer(text[start:])}


class ExitCodeTableTest(unittest.TestCase):
    """文档码表与代码常量必须逐码一致。"""

    @classmethod
    def setUpClass(cls):
        cls.doc = _read(CLI_MD)
        cls.codes = parse_codes(section_text(cls.doc))

    def test_documented_codes_equal_code_definition(self):
        self.assertTrue(self.codes, "docs/dev/cli.md 缺 §3 退出码表（扫空不得当绿）")
        self.assertEqual(self.codes, set(cli_support._EXIT_KINDS),
                         "cli.md §3 码表与 cli_support._EXIT_KINDS 不一致")

    def test_every_kind_is_documented(self):
        kinds = parse_error_kinds(self.doc)
        self.assertTrue(kinds, "docs/dev/cli.md 缺 §3.1 error_kind 表")
        self.assertLessEqual(set(cli_support._EXIT_KINDS.values()), kinds,
                             "有退出码分族未登记进 §3.1 error_kind 表")

    def test_second_run_need_has_one_definition(self):
        self.assertEqual(cli_support.SECOND_RUN_CHECK_NEED, 10)
        self.assertEqual(runner.SECOND_RUN_CHECK_NEED,
                         cli_support.SECOND_RUN_CHECK_NEED)
        self.assertEqual(workers._SECOND_RUN_CHECK_NEED,
                         cli_support.SECOND_RUN_CHECK_NEED)

    def test_readme_does_not_carry_a_second_numeric_table(self):
        text = _read(README)
        self.assertIn("docs/dev/cli.md", text,
                      "README 未链接契约文档：豁免登记失效")
        self.assertFalse(README_CODE_ROW.search(text),
                         "README 另立了数字退出码表：请改为引用 cli.md，或更新豁免登记")
        self.assertTrue(CARRIER_EXEMPTIONS["README.md"].strip())


class ParserTeethTest(unittest.TestCase):
    """解析器必须能报出少码/多码；空文本必须回空集。"""

    def test_parser_reports_a_removed_code(self):
        text = "| 码 | 含义 |\n|----|------|\n| 0 | ok |\n| 10 | need |\n"
        self.assertEqual(parse_codes(text), {0, 10})
        self.assertNotEqual(parse_codes(text.replace("| 10 |", "| 11 |")), {0, 10})

    def test_empty_text_parses_empty(self):
        self.assertEqual(parse_codes(""), set())


if __name__ == "__main__":
    unittest.main()
