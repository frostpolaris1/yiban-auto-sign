# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""分页档位等值门：服务端常量、契约登记表与前端档位三方互检。

标签：G · 安全：脱敏/审计/配置注入
覆盖：`web.routes` 的 `PAGE_LIMIT_MIN/MAX/DEFAULT`、`web.routes.audit_api` 的
    `_DEFAULT_PAGE_SIZE/_MAX_PAGE_SIZE`、`web.routes.run_events_api` 的
    `_DEFAULT_LIMIT/_MAX_LIMIT`、`docs/dev/api-contract.md` §1.8 登记表，以及
    `frontend/src/lib/pager.ts`、`frontend/src/audit/query.ts`、
    `frontend/src/logs/format.ts` 的档位常量。
对应实现：上述服务端常量是唯一定义点；契约表只是登记面，前端只读登记过的档位。
关键断言：
  ① 契约表三行的组键齐全（扫空不得当绿）；
  ② 每行的缺省/上限与对应服务端常量**逐一相等**；
  ③ `limit/offset` 组的前端可选档全部落在 `[PAGE_LIMIT_MIN, PAGE_LIMIT_MAX]` 内；
  ④ 审计/日志两条前端常量与各自服务端常量相等；
  ⑤ 解析器对合成漂移文本能报出差异，对空文本回空表。
依赖：只读仓库文件与模块级常量；不起 Flask 服务、不发网络请求。
"""

import os
import re
import unittest

from web.routes import PAGE_LIMIT_DEFAULT, PAGE_LIMIT_MAX, PAGE_LIMIT_MIN, audit_api, run_events_api

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONTRACT = os.path.join(BASE, "docs", "dev", "api-contract.md")
PAGER_TS = os.path.join(BASE, "frontend", "src", "lib", "pager.ts")
AUDIT_QUERY_TS = os.path.join(BASE, "frontend", "src", "audit", "query.ts")
LOGS_FORMAT_TS = os.path.join(BASE, "frontend", "src", "logs", "format.ts")
DATA_PY = os.path.join(BASE, "web", "routes", "data.py")

#: 契约表的小节范围：到下一个同级或更高级标题为止。
_SECTION = re.compile(r"^### §1\.8 分页档位登记.*?(?=^### |^## )", re.MULTILINE | re.DOTALL)
#: 表行：`| \`组键\` | 端点 | 缺省 | 上限 | 前端可选档 |`。
_ROW = re.compile(
    r"^\|\s*`([^`]+)`\s*\|\s*([^|]+?)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|"
    r"\s*([^|]+?)\s*\|\s*$",
    re.MULTILINE,
)
PAGE_SIZES_RE = re.compile(r"export const PAGE_SIZES\s*=\s*\[([^\]]*)\]")
DEFAULT_PAGE_SIZE_RE = re.compile(r"export const DEFAULT_PAGE_SIZE\s*=\s*(\d+)")
EVENT_PAGE_SIZES_RE = re.compile(r"export const EVENT_PAGE_SIZES\s*=\s*\[([^\]]*)\]")
LOG_PAGE_SIZE_RE = re.compile(r"export const LOG_PAGE_SIZE\s*=\s*(\d+)")
AUDIT_MAX_RE = re.compile(r"export const MAX_PAGE_SIZE\s*=\s*(\d+)")
AUDIT_DEFAULT_RE = re.compile(r"export const DEFAULT_PAGE_SIZE\s*=\s*(\d+)")

EXPECTED_GROUPS = {"limit/offset", "page/page_size", "limit"}


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def section_text() -> str:
    """契约文档 §1.8 原文；小节缺失回空串（调用方必须判红）。"""
    m = _SECTION.search(_read(CONTRACT))
    return m.group(0) if m else ""


def parse_registry(text):
    """§1.8 表文本 → `{组键: {"default": int, "max": int, "front": [int, …]}}`。

    空文本回空字典；调用方按组键齐全判红（扫空不得当绿）。
    """
    out = {}
    for m in _ROW.finditer(text):
        group, _endpoints, default, cap, front = m.groups()
        out[group] = {
            "default": int(default),
            "max": int(cap),
            "front": [int(n) for n in re.findall(r"\d+", front)],
        }
    return out


def _nums(pattern, text, path):
    m = pattern.search(text)
    if m is None:
        raise AssertionError(f"{os.path.relpath(path, BASE)} 未找到目标常量")
    if m.re.groups and m.lastindex:
        return [int(n) for n in re.findall(r"\d+", m.group(1))]
    return [int(m.group(1))]


class PaginationContractRegistryTest(unittest.TestCase):
    """契约表与三方代码真值的等值断言。"""

    @classmethod
    def setUpClass(cls):
        cls.text = section_text()
        cls.registry = parse_registry(cls.text)

    def test_registry_is_not_empty_and_has_every_group(self):
        self.assertTrue(self.text, "docs/dev/api-contract.md 缺 §1.8 分页档位登记节")
        self.assertEqual(set(self.registry), EXPECTED_GROUPS,
                         "§1.8 登记表的组键不全（改节后必须保留三组机读键）")

    def test_limit_offset_group_matches_server_constants(self):
        row = self.registry["limit/offset"]
        self.assertEqual(row["default"], PAGE_LIMIT_DEFAULT)
        self.assertEqual(row["max"], PAGE_LIMIT_MAX)

    def test_audit_page_size_group_matches_server_constants(self):
        row = self.registry["page/page_size"]
        self.assertEqual(row["default"], audit_api._DEFAULT_PAGE_SIZE)
        self.assertEqual(row["max"], audit_api._MAX_PAGE_SIZE)

    def test_run_events_group_matches_server_constants(self):
        row = self.registry["limit"]
        self.assertEqual(row["default"], run_events_api._DEFAULT_LIMIT)
        self.assertEqual(row["max"], run_events_api._MAX_LIMIT)

    def test_frontend_page_sizes_fit_server_cap(self):
        sizes = _nums(PAGE_SIZES_RE, _read(PAGER_TS), PAGER_TS)
        default = _nums(DEFAULT_PAGE_SIZE_RE, _read(PAGER_TS), PAGER_TS)[0]
        self.assertTrue(sizes, "pager.ts 的 PAGE_SIZES 解析为空")
        self.assertGreaterEqual(min(sizes), PAGE_LIMIT_MIN)
        self.assertLessEqual(max(sizes), PAGE_LIMIT_MAX,
                             "前端可选档超过服务端上限：后端会静默夹小，末页丢行")
        self.assertIn(default, sizes, "前端缺省档不在可选档内")
        self.assertLessEqual(default, PAGE_LIMIT_MAX)

    def test_logs_frontend_sizes_fit_server_cap(self):
        event_sizes = _nums(EVENT_PAGE_SIZES_RE, _read(LOGS_FORMAT_TS), LOGS_FORMAT_TS)
        body_size = _nums(LOG_PAGE_SIZE_RE, _read(LOGS_FORMAT_TS), LOGS_FORMAT_TS)[0]
        self.assertTrue(event_sizes, "logs/format.ts 的 EVENT_PAGE_SIZES 解析为空")
        self.assertLessEqual(max(event_sizes), PAGE_LIMIT_MAX)
        self.assertEqual(body_size, PAGE_LIMIT_MAX,
                         "日志正文页大小与 /api/logs 的服务端上限漂移")

    def test_audit_frontend_constants_match_server(self):
        text = _read(AUDIT_QUERY_TS)
        self.assertEqual(_nums(AUDIT_DEFAULT_RE, text, AUDIT_QUERY_TS)[0],
                         audit_api._DEFAULT_PAGE_SIZE)
        self.assertEqual(_nums(AUDIT_MAX_RE, text, AUDIT_QUERY_TS)[0],
                         audit_api._MAX_PAGE_SIZE)


#: `data.py` 的 sign-events 端点必须引用的共享常量名（按名字，不按值）。
_REQUIRED_LIMIT_NAMES = ("_PAGE_LIMIT_DEFAULT", "_PAGE_LIMIT_MAX", "_PAGE_LIMIT_MIN")


def missing_limit_constants(text):
    """→ 文本里缺失的共享常量名列表；空列表=合规。

    只认**名字**：回填同值字面量时值仍相等，只有名字消失时本函数才报红；但名字
    import 通常仍在，故真正的牙是调用点的两条 `assertNotRegex`——分别禁掉
    `args.get("limit", 100)` 与 `min(max(..., 1), 200)` 两种回填形态。
    """
    return [name for name in _REQUIRED_LIMIT_NAMES if name not in text]


class SignEventsSourceFormTest(unittest.TestCase):
    """`data.py` 的 sign-events 分页必须引用共享常量，不得回填字面量。"""

    def test_data_py_references_shared_limit_constants(self):
        text = _read(DATA_PY)
        self.assertEqual(missing_limit_constants(text), [],
                         "data.py 未引用分页上下限常量（回填同值字面量会让契约互检失明）")
        self.assertNotRegex(text, r'args\.get\(\s*"limit"\s*,\s*100\s*\)')
        self.assertNotRegex(
            text, r'min\(\s*max\(\s*int\(request\.args\.get\("limit"[^\n]*,\s*1\s*\)\s*,\s*200\s*\)')

    def test_detector_reds_on_same_value_literals(self):
        literal = 'limit = min(max(int(request.args.get("limit", 100)), 1), 200)'
        self.assertEqual(missing_limit_constants(literal),
                         list(_REQUIRED_LIMIT_NAMES))
        self.assertEqual(missing_limit_constants(_read(DATA_PY)), [])


class ParserTeethTest(unittest.TestCase):
    """解析器必须能报出漂移、空文本必须回空表（否则上面的等值断言可能是恒真）。"""

    def test_parser_reports_a_single_side_drift(self):
        text = section_text()
        self.assertTrue(text, "§1.8 缺失，无法自证解析器")
        mutated = text.replace("| 100 | 200 |", "| 100 | 199 |", 1)
        self.assertNotEqual(mutated, text, "合成漂移未生效（表行形态变了？）")
        self.assertEqual(parse_registry(mutated)["limit/offset"]["max"], 199)

    def test_empty_text_parses_to_empty_registry(self):
        self.assertEqual(parse_registry(""), {})


if __name__ == "__main__":
    unittest.main()
