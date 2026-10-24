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
· `tests/**/*.py`（**递归**，含 `tests/protocol/`、`tests/golden/`）：下列**日期源**
  命中即红，并报 `文件:行`：
    1. 宿主钟取"现在"后格式化成日期串——`datetime.now()` / `datetime.datetime.now()` /
       `date.today()` / `datetime.date.today()` / `datetime.today()` / `datetime.utcnow()`
       经 `.strftime(<日期格式>)`（含 %Y/%m/%d、且不含时分秒）。
    2. `time.strftime(<日期格式>, time.localtime(...))`（`localtime/gmtime` 是宿主钟）。
    3. 直接产出 date 对象的宿主调用：`datetime.now().date()`、`date.today()`、
       `date.fromtimestamp(...)`。
    4. 时刻串切片成日期：`datetime.now().strftime("%Y-%m-%d %H:%M:%S")[:10]`。
· `frontend/e2e/**/*.ts`：除 `business-day.ts`（业务日唯一助手）外，禁止
  `getFullYear/getMonth/getDate/getUTC*/toISOString/toLocaleDateString/toDateString`
  与 `Intl.DateTimeFormat`——它们都按浏览器或宿主的"今天"取日。业务日一律走
  `business-day.ts`。

明确豁免（`PY_EXEMPTIONS`，file:line + 理由）：
· 被测语义本身就是宿主钟时，期望值必须用同一宿主钟构造，否则测的就不是那条实现。
  豁免逐条登记；登记项若不再命中（代码移动/删除）⇒ 本门判红，逼人清理。

不在本门内（由审查兜底，如实登记）：
· **时刻**（mtime 回拨、耗时测量、epoch 秒）：无"今天是哪天"语义。
· `datetime.now().isoformat()`：datetime 的 isoformat 产出**时刻**串（含 `T` 与时区），
  不是 `YYYY-MM-DD`；只有 **date** 对象的 `isoformat()` 才是日期串。故只认 date 源。
· 裸 `now()` / `today()`（`from datetime import datetime as dt` 后的 `dt.now()` 别名）：
  根名不是 `datetime`/`date`，静态上无法与业务钟区分，本门不判；审查兜底。
· TS 内的正则字面量已遮罩（避免 `//` 被当行注释）。识别是启发式：`_REGEX_PREV`
  字符集 + `return/case/throw/yield/await` 关键字两种上下文。仍有无法识别的形态
  （如 `if (x) /"/`：`/` 前是 `)`）——此时其中的引号会被当字符串帧起点，但帧**遇到
  未转义换行即退出**，损害被限制在**同一行**（该行后半段可能漏检），不再吞掉文件剩余
  部分。这是已登记的边界：审查时看到形如 `)/re/` 的写法需人工确认。
· 别名形 `f = datetime.now(); f.strftime("%Y-%m-%d")` 与格式串非字面量的
  `datetime.now().strftime(fmt)`（或无格式串）不在静态判定面内（本门不做数据流/
  常量折叠）；审查兜底。测试里遇到这两种写法按宿主钟违规处理。

面外无读者核对结论（2026-10-25）：
· 前端业务日的**唯一**读者是 `frontend/e2e/*.spec.ts` 与 `business-day.ts`；
  `playwright.config.ts` 的 `timezoneId` 只在浏览器上下文生效，不产出期望值。
· Python 测试的业务日读者全部在 `tests/**`；`scripts/`、`web/`、`yiban/` 是生产面，
  不属本门扫描对象（生产侧由 `yiban.core.clock` 与 `/api/clock` 契约测试钉住）。
· `frontend/e2e/server.py` 是测试夹具（非 spec），已全程走 `clock`，无需本门再扫。

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

#: 宿主钟取"现在"的方法；接收者根名必须是 datetime / date（`clock.now()` 不在内）。
_HOST_NOW_ATTRS = {"now", "today", "utcnow"}
_HOST_ROOTS = {"datetime", "date"}
#: `time` 模块的宿主钟入口（返回 struct_time，配合 strftime 取日）。
_HOST_TIME_ATTRS = {"localtime", "gmtime"}
#: 前端直接取日期分量/日期串的调用（业务日助手之外一律禁止）。
_TS_DATE_GETTER = re.compile(
    r"\.(getFullYear|getMonth|getDate|getUTCFullYear|getUTCMonth|getUTCDate|"
    r"toISOString|toLocaleDateString|toDateString)\s*\(|Intl\.DateTimeFormat"
)
#: 日期格式：含 %Y/%m/%d 且不含时分秒。`%Y-%m-%d %H:%M:%S` 是时刻串，不算。
_DAY_ONLY_TOKENS = ("%Y", "%m", "%d")
_TIME_TOKENS = ("%H", "%M", "%S")

#: 显式豁免：`(相对路径, 行号)` → 理由。行号漂移即失效（本门会报"陈旧豁免"）。
PY_EXEMPTIONS = {
    ("tests/test_log_masking_formatter.py", 126):
        "被测 MaskingFormatter 的 asctime 走宿主 localtime；期望日必须用同一宿主钟构造，"
        "否则测不到『asctime 未被误伤』。此处取日是被测语义本身，不是业务日推导",
}

#: 正则字面量可出现的上一有效字符（启发式；用于把 `/.../ ` 与除法区分）。
_REGEX_PREV = set("(=:[,!&|?{};+-*%<>~^")
#: 正则字面量可出现的上一关键字（`return /re/`、`case /re/:` 等）。
_REGEX_KEYWORDS = {"return", "case", "throw", "yield", "await"}


def _prev_word(chars, index):
    """`chars[index]` 之前最近的一个标识符（跨空白；无则空串）。"""
    j = index - 1
    while j >= 0 and chars[j].isspace():
        j -= 1
    end = j
    while j >= 0 and (chars[j].isalnum() or chars[j] in "_$"):
        j -= 1
    return "".join(chars[j + 1:end + 1])


def _root_name(node):
    """表达式的最左标识符（`datetime.datetime` → `datetime`）。"""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return _root_name(node.value)
    return ""


def _is_host_time_source(node):
    """表达式是否从**宿主钟**取时刻/日期对象。

    覆盖：`datetime.now()`、`date.today()`、`datetime.datetime.now()`、
    `datetime.now().date()`、`date.fromtimestamp(...)`、`time.localtime(...)`，
    以及它们的 `± timedelta` 运算（取左操作数）。
    """
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Attribute):
            if func.attr in _HOST_NOW_ATTRS and _root_name(func.value) in _HOST_ROOTS:
                return True
            if func.attr == "date" and _is_host_time_source(func.value):
                return True
            if func.attr == "fromtimestamp" and _root_name(func.value) in _HOST_ROOTS:
                return True
            if func.attr in _HOST_TIME_ATTRS and _root_name(func.value) in ("time", ""):
                return True
        if isinstance(func, ast.Name) and func.id in _HOST_TIME_ATTRS:
            return True
    if isinstance(node, ast.BinOp):
        return _is_host_time_source(node.left)
    return False


def _is_host_date_source_call(node):
    """该调用**本身**产出 date 对象且来自宿主钟（D3）。

    · `date.today()` / `datetime.date.today()`
    · `datetime.now().date()`（及 `.utcnow().date()`）
    · `date.fromtimestamp(...)`
    `datetime.today()` / `datetime.fromtimestamp(...)` 产出 datetime，不在此列。
    """
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return False
    func = node.func
    callee = ast.unparse(func)
    if callee in ("date.today", "datetime.date.today"):
        return True
    if func.attr == "date" and _is_host_time_source(func.value):
        return True
    return func.attr == "fromtimestamp" and _root_name(func.value) == "date"


def _is_day_only_format(value):
    if not isinstance(value, str):
        return False
    if not all(tok in value for tok in _DAY_ONLY_TOKENS):
        return False
    return not any(tok in value for tok in _TIME_TOKENS)


def _is_len10_slice(node):
    """`expr[:10]`：把时刻串切掉时间部分，等价于取日期串。"""
    if not isinstance(node, ast.Subscript):
        return False
    sl = node.slice
    return (
        isinstance(sl, ast.Slice)
        and sl.lower is None
        and sl.step is None
        and isinstance(sl.upper, ast.Constant)
        and sl.upper.value == 10
    )


def _is_host_strftime(node):
    """`<宿主时刻>.strftime(...)` 或 `time.strftime(..., time.localtime(...))`。"""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "strftime"
    )


def _is_host_timestamp_string(node):
    """宿主时刻经 `strftime(...)` / `isoformat()` 产出的**时刻串**（可用于 `[:10]`）。"""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in ("strftime", "isoformat")
        and _is_host_time_source(node.func.value)
    )


def _python_offenders_source(source, filename="<memory>"):
    """返回命中行号列表（升序去重）。"""
    tree = ast.parse(source, filename=filename)
    hits = set()
    for node in ast.walk(tree):
        if _is_host_date_source_call(node):
            hits.add(node.lineno)
            continue
        if not _is_host_strftime(node):
            continue
        args = node.args
        if args and _is_day_only_format(getattr(args[0], "value", None)):
            sources = [node.func.value, *args[1:]]
            if any(_is_host_time_source(s) for s in sources):
                hits.add(node.lineno)
    # 切片形单独走一遍（外层 Subscript 不是 Call）：
    #   `now().strftime("%Y-%m-%d %H:%M:%S")[:10]` 与 `now().isoformat()[:10]` 都取日期串。
    for node in ast.walk(tree):
        if _is_len10_slice(node) and _is_host_timestamp_string(node.value):
            hits.add(node.lineno)
    return sorted(hits)


def _iter_python_files():
    for dirpath, _dirnames, filenames in os.walk(TESTS_DIR):
        for name in sorted(filenames):
            if name.endswith(".py"):
                yield os.path.join(dirpath, name)


def _python_offenders(path):
    with open(path, encoding="utf-8") as f:
        return _python_offenders_source(f.read(), filename=path)


def _mask_ts(text):
    """把 TS 源码里的字符串/模板/正则/注释替换成空格，**保留换行**。

    保留换行是行号正确的前提（D4）：块注释跨多行时，后续命中行的行号不偏移。
    字符串必须遮罩（D1）：`"http://127.0.0.1"` 里的 `//` 不是行注释，旧实现会把
    后半行当注释切掉，从而漏掉同一行后面的真违规。模板字符串的 `${...}` 内部是代码，
    必须揭开继续扫描。
    字符串/模板帧遇**未转义换行**即退出（M-1）：JS 允许换行的只有模板，而本仓 e2e
    无跨行模板；把引号误判成帧起点时，损害因此只留在当前行，不会吞掉文件剩余部分。
    """
    out = list(text)
    n = len(text)
    i = 0
    frames = [{"kind": "code", "depth": 0, "tpl": False}]
    last_sig = ""

    def blank(j):
        if 0 <= j < n and out[j] != "\n":
            out[j] = " "

    def top():
        return frames[-1]

    while i < n:
        c = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        frame = top()

        if frame["kind"] == "code":
            if frame["tpl"]:
                if c == "{":
                    frame["depth"] += 1
                    last_sig = "{"
                    i += 1
                    continue
                if c == "}":
                    if frame["depth"] == 0:
                        frames.pop()
                        blank(i)
                        i += 1
                        continue
                    frame["depth"] -= 1
                    last_sig = "}"
                    i += 1
                    continue
            if c == "/" and nxt == "/":
                blank(i)
                blank(i + 1)
                i += 2
                while i < n and text[i] != "\n":
                    blank(i)
                    i += 1
                continue
            if c == "/" and nxt == "*":
                blank(i)
                blank(i + 1)
                i += 2
                while i < n:
                    if text[i] == "*" and i + 1 < n and text[i + 1] == "/":
                        blank(i)
                        blank(i + 1)
                        i += 2
                        break
                    blank(i)
                    i += 1
                continue
            if c in ("'", '"'):
                frames.append({"kind": "str", "quote": c})
                blank(i)
                i += 1
                continue
            if c == "`":
                frames.append({"kind": "tpl"})
                blank(i)
                i += 1
                continue
            if c == "/" and nxt not in ("/", "*") and (
                not last_sig or last_sig in _REGEX_PREV
                or _prev_word(out, i) in _REGEX_KEYWORDS
            ):
                # 正则字面量：遮到未转义的 `/`（字符类内的 `/` 不结束）。
                blank(i)
                i += 1
                in_class = False
                while i < n and text[i] != "\n":
                    ch = text[i]
                    if ch == "\\":
                        blank(i)
                        blank(i + 1)
                        i += 2
                        continue
                    if ch == "[":
                        in_class = True
                    elif ch == "]":
                        in_class = False
                    elif ch == "/" and not in_class:
                        blank(i)
                        i += 1
                        break
                    blank(i)
                    i += 1
                last_sig = "/"
                continue
            if not c.isspace():
                last_sig = c
            i += 1
            continue

        if frame["kind"] == "str":
            if c == "\\":
                blank(i)
                blank(i + 1)
                i += 2
                continue
            if c == frame["quote"]:
                frames.pop()
                blank(i)
                i += 1
                continue
            if c == "\n":
                # JS 裸换行不能出现在字符串里 ⇒ 这一定不是字符串（多半是正则里的引号）。
                # 退出帧，把损害限制在当前行：不退出会把文件剩余部分全部遮掉（M-1）。
                frames.pop()
                i += 1
                continue
            blank(i)
            i += 1
            continue

        # template literal
        if c == "\\":
            blank(i)
            blank(i + 1)
            i += 2
            continue
        if c == "`":
            frames.pop()
            blank(i)
            i += 1
            continue
        if c == "$" and nxt == "{":
            blank(i)
            blank(i + 1)
            frames.append({"kind": "code", "depth": 0, "tpl": True})
            last_sig = "{"
            i += 2
            continue
        if c == "\n":
            # 同上：未闭合的模板帧多半是正则里的反引号。退出帧，把损害限制在当前行
            # （本仓 e2e 无跨行模板：多行模板会被当代码扫描，见 docstring 边界）。
            frames.pop()
            i += 1
            continue
        blank(i)
        i += 1

    return "".join(out)


def _ts_offenders_text(text):
    masked = _mask_ts(text)
    return [i for i, line in enumerate(masked.splitlines(), 1)
            if _TS_DATE_GETTER.search(line)]


def _ts_offenders(path):
    with open(path, encoding="utf-8") as f:
        return _ts_offenders_text(f.read())


class BusinessDaySourceGateTest(unittest.TestCase):
    def test_python_tests_do_not_derive_business_day_from_host_clock(self):
        offenders, scanned = [], 0
        for path in _iter_python_files():
            scanned += 1
            rel = os.path.relpath(path, BASE).replace(os.sep, "/")
            for line in _python_offenders(path):
                offenders.append((rel, line))
        self.assertGreater(scanned, 0, "tests/ 为空：守卫会静默空扫")
        # 豁免必须**命中**才有效：登记项陈旧（代码移动/删除）同样判红。
        used = set()
        unexempted = []
        for rel, line in offenders:
            key = (rel, line)
            if key in PY_EXEMPTIONS:
                used.add(key)
                continue
            unexempted.append("%s:%d" % key)
        stale = sorted(set(PY_EXEMPTIONS) - used)
        self.assertEqual(
            stale, [],
            "显式豁免已不再命中（行号漂移或代码已删）：请更新或删除登记。陈旧项：\n  "
            + "\n  ".join("%s:%d" % k for k in stale))
        self.assertEqual(
            unexempted, [],
            "测试用宿主钟推导业务日。生产业务日唯一来源是 `yiban.core.clock`；"
            "UTC 16:00–24:00 窗口内宿主日与业务日差一天（P0 2026-10-25）。"
            "改用 `clock.today()` / `(clock.now() ± timedelta).strftime(...)`。违例：\n  "
            + "\n  ".join(unexempted))

    def test_e2e_specs_derive_dates_only_from_business_day_helper(self):
        offenders, scanned = [], 0
        for dirpath, _dirnames, filenames in os.walk(E2E_DIR):
            for name in sorted(filenames):
                if not name.endswith(".ts") or name in TS_ALLOW:
                    continue
                path = os.path.join(dirpath, name)
                scanned += 1
                rel = os.path.relpath(path, BASE).replace(os.sep, "/")
                for line in _ts_offenders(path):
                    offenders.append("%s:%d" % (rel, line))
        self.assertGreater(scanned, 0, "frontend/e2e 未扫到 .ts：守卫会静默空扫")
        self.assertEqual(
            offenders, [],
            "e2e 用例按浏览器/宿主「今天」取日期。业务日一律走 `business-day.ts`"
            "（固定 +8，与后端 `yiban.core.clock` 同口径）；浏览器时区只在"
            "`playwright.config.ts` 固定，不是断言的事实源。违例：\n  "
            + "\n  ".join(offenders))


#: 检测器自测样本：每条命中形一份正样本、每条豁免形一份负样本（防回归）。
PY_BAD_SAMPLES = {
    "strftime 宿主钟取日": 'x = datetime.now().strftime("%Y-%m-%d")\n',
    "time.strftime + localtime": 'x = time.strftime("%Y-%m-%d", time.localtime(0))\n',
    "now().date()": "x = datetime.now().date()\n",
    "date.fromtimestamp": "x = date.fromtimestamp(0)\n",
    "date.today": "x = datetime.date.today()\n",
    "utcnow + 8h": 'x = (datetime.utcnow() + timedelta(hours=8)).strftime("%Y-%m-%d")\n',
    "时刻串切片 [:10]": 'x = datetime.now().strftime("%Y-%m-%d %H:%M:%S")[:10]\n',
    "datetime isoformat 切片 [:10]": "x = datetime.now().isoformat()[:10]\n",
}
PY_GOOD_SAMPLES = {
    "datetime.now().isoformat() 是时刻串": "x = datetime.now().isoformat()\n",
    "时刻串不切片": 'x = datetime.now().strftime("%Y-%m-%d %H:%M:%S")\n',
    "业务钟取日": 'x = clock.now().strftime("%Y-%m-%d")\n',
    "业务钟 today": "x = clock.today()\n",
    "业务钟 now().date()": "x = clock.now().date()\n",
}
TS_BAD_SAMPLES = {
    "字符串内 // 不得吞掉同行违规":
        'const u = "http://x/y"; const s = new Date().getFullYear();\n',
    "toLocaleDateString": "const s = d.toLocaleDateString();\n",
    "toDateString": "const s = d.toDateString();\n",
    "Intl.DateTimeFormat": 'const f = new Intl.DateTimeFormat("en-US");\n',
    "getUTCDate": "const s = d.getUTCDate();\n",
    "模板字符串内插代码": "const s = `${new Date().getFullYear()}`;\n",
    "正则字面量后的违规": "const r = /a\\/\\/b/; const s = d.getFullYear();\n",
    "return 后的正则含引号": 'function f() { return /"/.test(s); }\nconst y = d.getFullYear();\n',
    "case 后的正则含引号": "switch (x) { case /'/.test(s): break; }\nconst y = d.getFullYear();\n",
}
TS_GOOD_SAMPLES = {
    "字符串内日期字样": 'const a = "getFullYear("; const b = "http://x/y";\n',
    "行注释": "// new Date().getFullYear()\nconst x = 1;\n",
    "块注释": "/* d.getFullYear()\n   still comment */\nconst x = 1;\n",
}


class GateSelfTest(unittest.TestCase):
    """检测器自测：每条命中形都有样本，且负样本不许误报（防回归）。"""

    def test_python_positive_samples_hit(self):
        for name, src in PY_BAD_SAMPLES.items():
            with self.subTest(name=name):
                self.assertTrue(_python_offenders_source(src),
                                "样本未被检出：%s" % name)

    def test_python_negative_samples_do_not_hit(self):
        for name, src in PY_GOOD_SAMPLES.items():
            with self.subTest(name=name):
                self.assertEqual(_python_offenders_source(src), [],
                                 "样本被误报：%s" % name)

    def test_ts_positive_samples_hit(self):
        for name, src in TS_BAD_SAMPLES.items():
            with self.subTest(name=name):
                self.assertTrue(_ts_offenders_text(src), "样本未被检出：%s" % name)

    def test_ts_negative_samples_do_not_hit(self):
        for name, src in TS_GOOD_SAMPLES.items():
            with self.subTest(name=name):
                self.assertEqual(_ts_offenders_text(src), [], "样本被误报：%s" % name)

    def test_ts_line_numbers_survive_multiline_comment(self):
        """D4：块注释跨行后，命中行号必须是源码真实行号。"""
        src = ("const a = 1;\n"
               "/* 多行\n"
               "   注释\n"
               "*/\n"
               "const s = d.getFullYear();\n")
        self.assertEqual(_ts_offenders_text(src), [5])

    def test_ts_string_with_double_slash_does_not_swallow_violation(self):
        """D1：字符串内 `//` 不得把同一行后半段当注释切掉。"""
        src = 'const u = "http://x/y"; const y = new Date().getFullYear();\n'
        self.assertEqual(_ts_offenders_text(src), [1])

    def test_ts_unrecognized_regex_masks_only_its_own_line(self):
        """M-1：无法识别的正则（`/` 前是 `)`）里的引号只能遮到行尾，不得吞掉后文。

        `if (x) /"/.test(s);` 的 `/` 前是 `)`，既不在 `_REGEX_PREV` 也不是关键字，
        故其中的 `"` 会被当字符串帧起点。字符串帧遇未转义换行必须退出，否则后续
        `getFullYear()` 全部不可见（审查员实跑曾返回空列表）。
        """
        src = 'if (x) /"/.test(s);\nconst y = d.getFullYear();\n'
        self.assertEqual(_ts_offenders_text(src), [2])


if __name__ == "__main__":
    unittest.main()
