# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""结构守卫：测试的「业务日」必须取自 `yiban.core.clock`，不得用宿主/浏览器时钟现算。

背景（2026-10-25 P0）：生产与测试原本混用两套"今天"——
· 生产：`yiban.core.clock`（固定北京 +8；run.sh / backup.sh 走
  `scripts/lib/host_paths.sh::business_day`）与前端校准钟。
· 部分测试：`datetime.now()` / `date.today()`（宿主时区）。
UTC 16:00–24:00（＝北京 00:00–08:00）窗口内两者差一天：按天日志名、状态文件名、
备份归档名、日历取样日全部对不上，CI 在窗口内整片假红。同形出现 ≥2 次，故立门而非
逐实例补丁（AGENTS §27）。

判定面（静态，只读源码，不取真实时钟）：
· `tests/*.py`：对**宿主钟**取日期串的表达式——`datetime.now()` / `datetime.datetime.now()`
  / `date.today()` / `datetime.date.today()` / `datetime.today()` / `datetime.utcnow()`
  经 `.strftime(<日期格式>)`（含 %Y/%m/%d、且不含时分秒）或 `.isoformat()`
  产出 `YYYY-MM-DD` 形态。命中即红并报 `文件:行`。
· `frontend/e2e/**/*.ts`：除 `business-day.ts`（业务日唯一助手）外，禁止
  `getFullYear/getMonth/getDate/getUTC*/toISOString`——这些都会按浏览器或宿主的"今天"取日。
  业务日一律走 `business-day.ts`。

不在本门内（由审查兜底）：
· `datetime.now().timestamp()` 一类的**时刻**（mtime 回拨、耗时测量、epoch 秒）：它们
  不含"今天是哪天"的语义，与业务日无关，改了反而错。
· 只用 `.isoformat(timespec="seconds")` 取时刻串的表达式。

扫空不得当绿：两侧扫描面都断言文件数 > 0。
"""

import ast
import os
import re
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS_DIR = os.path.join(BASE, "tests")
E2E_DIR = os.path.join(BASE, "frontend", "e2e")
#: 业务日助手本身必须读时区并做日期运算，是唯一豁免文件。
TS_ALLOW = {"business-day.ts"}

#: 宿主钟的取"现在"方法；接收者根名必须是 datetime / date（`clock.now()` 不在内）。
_HOST_NOW_ATTRS = {"now", "today", "utcnow"}
_HOST_ROOTS = {"datetime", "date"}
#: 前端直接取日期分量/日期串的调用（业务日助手之外一律禁止）。
_TS_DATE_GETTER = re.compile(
    r"\.(getFullYear|getMonth|getDate|getUTCFullYear|getUTCMonth|getUTCDate|toISOString)\s*\(")


def _root_name(node):
    """表达式的最左标识符（`datetime.datetime` → `datetime`）。"""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return _root_name(node.value)
    return ""


def _is_host_clock_call(node):
    """`datetime.now()` / `datetime.datetime.now()` / `date.today()` / `...utcnow()`。"""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if not isinstance(func, ast.Attribute) or func.attr not in _HOST_NOW_ATTRS:
        return False
    return _root_name(func.value) in _HOST_ROOTS


def _contains_host_clock(node):
    return any(_is_host_clock_call(n) for n in ast.walk(node))


def _is_day_only_format(value):
    """日期格式：含 %Y/%m/%d，且不含时分秒。`%Y-%m-%d %H:%M:%S` 是时刻串，不算。"""
    if not isinstance(value, str):
        return False
    if not all(tok in value for tok in ("%Y", "%m", "%d")):
        return False
    return not any(tok in value for tok in ("%H", "%M", "%S"))


def _python_offenders(path):
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=path)
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        receiver = node.func.value
        if not _contains_host_clock(receiver):
            continue
        day_string = (
            node.func.attr == "strftime" and bool(node.args)
            and _is_day_only_format(getattr(node.args[0], "value", None))
        ) or (
            node.func.attr == "isoformat" and not node.args and not node.keywords
        )
        if day_string:
            hits.append(node.lineno)
    return sorted(hits)


def _strip_ts_comments(text):
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"//[^\n]*", "", text)


def _ts_offenders(path):
    with open(path, encoding="utf-8") as f:
        text = _strip_ts_comments(f.read())
    hits = []
    for i, line in enumerate(text.splitlines(), 1):
        if _TS_DATE_GETTER.search(line):
            hits.append(i)
    return hits


class BusinessDaySourceGateTest(unittest.TestCase):
    def test_python_tests_do_not_derive_business_day_from_host_clock(self):
        offenders, scanned = [], 0
        for name in sorted(os.listdir(TESTS_DIR)):
            if not name.endswith(".py"):
                continue
            path = os.path.join(TESTS_DIR, name)
            scanned += 1
            for line in _python_offenders(path):
                offenders.append(f"{os.path.relpath(path, BASE)}:{line}")
        self.assertGreater(scanned, 0, "tests/ 为空：守卫会静默空扫")
        self.assertEqual(
            offenders, [],
            "测试用宿主钟推导业务日。生产业务日唯一来源是 `yiban.core.clock`；"
            "UTC 16:00–24:00 窗口内宿主日与业务日差一天（P0 2026-10-25）。"
            "改用 `clock.today()` / `(clock.now() ± timedelta).strftime(...)`。违例：\n  "
            + "\n  ".join(offenders))

    def test_e2e_specs_derive_dates_only_from_business_day_helper(self):
        offenders, scanned = [], 0
        for dirpath, _dirnames, filenames in os.walk(E2E_DIR):
            for name in sorted(filenames):
                if not name.endswith(".ts") or name in TS_ALLOW:
                    continue
                path = os.path.join(dirpath, name)
                scanned += 1
                for line in _ts_offenders(path):
                    offenders.append(f"{os.path.relpath(path, BASE)}:{line}")
        self.assertGreater(scanned, 0, "frontend/e2e 未扫到 .ts：守卫会静默空扫")
        self.assertEqual(
            offenders, [],
            "e2e 用例按浏览器/宿主「今天」取日期。业务日一律走 `business-day.ts`"
            "（固定 +8，与后端 `yiban.core.clock` 同口径）；浏览器时区只在"
            "`playwright.config.ts` 固定，不是断言的事实源。违例：\n  "
            + "\n  ".join(offenders))


if __name__ == "__main__":
    unittest.main()
