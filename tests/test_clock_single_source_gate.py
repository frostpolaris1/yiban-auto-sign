# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""结构守卫：产品代码的「现在」只许取自 `yiban.core.clock`。

背景（2026-10-25 P0）：生产业务时间是北京时间（UTC+8）。
`datetime.now()` 取宿主本地时区。
部署在 UTC 主机时，北京时间 00:00–08:00 与宿主日期不同。
按天日志名、状态文件名、备份归档名、日历取样日会错一天。
测试面已有 `test_business_day_source_gate.py`。
本门补产品面：`yiban/`、`web/`、`scripts/`、`docker/`、`tools/` 的 `.py`。
`yiban/core/clock.py` 是唯一时点来源，不在扫描面内。

判定面（静态，只读源码，不取真实时钟）：
· 宿主钟取「现在」的调用，命中即报 `文件:行`：
    1. `datetime.now()`（含 `datetime.datetime.now()`）；
    2. `datetime.utcnow()`（含别名导入）；
    3. `date.today()` / `datetime.date.today()` / `datetime.today()`；
    4. `datetime.now(tz=None)`：等价宿主钟 naive 取时，判红。
· `datetime.now(<tz>)` 只认 `clock.TZ`（含 `clock` 的模块别名与 `TZ` 的 from 导入名）。
  其他时区给的是**别的时区的日期**，不是业务日。
  只在下游**取日**时判红，先归一链路再判：
    `.replace(...)` 继承墙钟归属；`.astimezone(业务时区)` 把 aware 源换回业务日；
    其余 `astimezone` 目标、以及换上前的外来墙钟，都给非业务日期；BinOp 判两侧。
  取日形状：`.date()`；含日期占位符的 `.strftime(...)`；
  `.isoformat()[<覆盖日期段的切片>]`；`str(<时刻>)[<覆盖日期段的切片>]`；
  `f"{<时刻>:<含日期占位符>}"`；`.isoformat().split("T")[0]`。
  仅作时刻、不做取日时放行。
  伪造归属有粘性：换成别的 tzinfo 后，再 `.astimezone(业务时区)` 也不得洗白。
· 业务钟的别名写法同判：`from yiban.core import clock as X`、`X.now()`；
  `from yiban.core.clock import now [as Y]`、`Y()`；`import yiban.core.clock as Z`；
  星号导入同时绑定 `now` 与 `TZ`。
· `naive` 时刻上的 `.astimezone()`：按宿主时区解释北京墙钟。
  这是 P0 的原始缺陷形状（`clock.now().astimezone()`）。
  可证为 naive 的来源：
    `clock.now()`、宿主钟取时、`replace(tzinfo=None)`、
    `strptime`（格式串无 `%z`/`%Z`）、`fromtimestamp`（无 `tz`）、
    `utcfromtimestamp`、`fromisoformat`（实参无偏移）、
    `combine`（time 实参无 tzinfo）、任一操作数为 naive 的 `BinOp`。
  只判**可证为 naive** 的接收者；变量接收者静态不可判，登记为边界。

明确豁免（`EXEMPTIONS`，file:line + 代码片段 + 理由）：
· 命中「只求唯一」的标记位（进程代次、执行体身份、备份文件名）。
· 命中「物理时刻」语义（与文件 mtime 或上游 epoch 比对）。
· 命中「展示时刻」语义（日志行、心跳观测件）。
· 逐条登记。登记项必须**恰好命中**；行号漂移或代码改动即判红。
  这道陈旧检查逼人重审理由，不许豁免腐烂。

不在本门内（如实登记）：
· `time.monotonic()` / `time.perf_counter()`：性能计时，无「今天是哪天」语义。
· `time.time()` / `.timestamp()`：epoch 秒。epoch 与宿主时区无关。
· `datetime.fromtimestamp(ts)` 无 `tz`：它按宿主时区造**时刻**。
  本门不判；审查兜底。
· `datetime.fromisoformat(<非字面量>)`：实参可能带偏移，静态不可判。
  按无偏移处理（保守判 naive）；确有偏移时登记豁免。
· `datetime.now(<tz>)` 后接变量格式串的 `.strftime(...)`：占位符静态不可判。
· f-string 的变量格式说明（`f"{<时刻>:{fmt}}"`）：占位符静态不可判。
  与 `.strftime(<变量>)` 同属一类边界。
· 无格式说明的 f-string（`f"{<时刻>}"`）：形状不是「取日」的显式判据。
· 不带 `as` 的整链导入（`import yiban.core` / `import yiban.core.clock`）后的链式名
  （`yiban.core.clock.now()`）：不在别名表。
· 别名变量（`t = datetime.now(); t.strftime(...)`）：本门不做数据流。
· 变量接收者的 `.astimezone()`：静态不可判是否 aware。
· 同一形状出现两处以上即按形状立门（AGENTS §27）。

扫空不得当绿：五个产品目录各自的 `.py` 文件数都必须大于 0。
"""

import ast
import os
import re
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
#: 产品面目录（相对仓库根）。测试面不在内：见 test_business_day_source_gate.py。
PRODUCT_DIRS = ("yiban", "web", "scripts", "docker", "tools")
#: 业务时钟本尊：它必须读宿主钟并换算出 +8，是唯一豁免文件。
CLOCK_MODULE = "yiban/core/clock.py"
#: 扫描面下限（AGENTS §30）。B1 收口态实测 158 个文件（含 `clock.py` 共 159，
#: 本尊不扫；口径＝`git ls-files`，ref＝`origin/develop` `aa75e2b9` 加 B1 新增件）；下限取 100。
MIN_FILES = 100

#: 显式豁免：`(相对路径, 行号)` → 登记项。
#: `code` 是该行的规范化源码。行号或代码变化都会让登记项**陈旧**，本门判红。
EXEMPTIONS = {
    ("yiban/client.py", 552): {
        "kind": "datetime.now",
        "code": "now_ts = int(datetime.now().timestamp())  # noqa: DTZ005 真时间戳语义：与易班服务端 Range 时间戳同钟，刻意不走业务钟",
        "reason": "与易班服务端 Range 的 epoch 秒比对。epoch 与宿主时区无关；"
                  "业务钟转 epoch 会差 8 小时，此处必须用真时间戳",
    },
    ("yiban/egress.py", 191): {
        "kind": "datetime.now",
        "code": '_RUNTIME_GEN = datetime.datetime.now().strftime("%Y%m%d%H%M%S")'
                '  # noqa: DTZ005 进程代次标记，只求同机唯一，与业务钟无关',
        "reason": "进程代次标记，只求同机唯一。不参与业务日判定",
    },
    ("yiban/adapters/persistence/claims.py", 154): {
        "kind": "datetime.now",
        "code": 'return f"{prefix}{socket.gethostname()}:{os.getpid()}:'
                '{datetime.datetime.now():%H%M%S}"  # noqa: DTZ005 执行体身份里的'
                '启动时刻标记，只求唯一，与业务钟无关',
        "reason": "执行体身份里的启动时刻，只求重启后变化。不参与业务日判定",
    },
    ("yiban/adapters/statefiles/state_gc.py", 291): {
        "kind": "datetime.now",
        "code": "threshold = ((now or datetime.datetime.now()) - datetime.timedelta(  # noqa: DTZ005 与文件 mtime 比对的物理时刻，刻意留宿主钟（见上方注释）",
        "reason": "与文件 mtime 的 epoch 秒比对，属物理时刻语义。"
                  "业务日截止时刻另走 clock，见同函数",
    },
    ("yiban/engine/db_maintenance.py", 321): {
        "kind": "datetime.now",
        "code": 'stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")'
                '  # noqa: DTZ005 备份文件名戳，只求唯一（含同秒后缀），与业务钟无关',
        "reason": "备份文件名戳，只求唯一。不参与业务日判定",
    },
    ("tools/dev_visual_seed.py", 239): {
        "kind": "datetime.now",
        "code": 'db.set_account_deleted(aid, True, '
                'deleted_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"), '
                'deleted_by="admin")  # noqa: DTZ005 开发演示种子数据，非判定路径',
        "reason": "CI fast 车道经 tests/test_visual_seed_generation.py 子进程跑 seed()"
                  "（lane-roster.tsv:345）；宿主钟只写演示数据时间戳、不进任何断言；非部署件",
    },
    ("tools/dev_visual_seed.py", 243): {
        "kind": "datetime.now",
        "code": "now = datetime.now()  # noqa: DTZ005 开发演示种子数据，非判定路径",
        "reason": "CI fast 车道经 tests/test_visual_seed_generation.py 子进程跑 seed()"
                  "（lane-roster.tsv:345）；宿主钟只写演示数据时间戳、不进任何断言；非部署件",
    },
    ("scripts/state_cleanup.py", 98): {
        "kind": "datetime.now",
        "code": 'stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")'
                '  # noqa: DTZ005 日志行展示时刻，刻意跟随宿主钟（见模块头注 MF-109）',
        "reason": "清理日志的展示时刻，跟随宿主钟。删除判据另走北京时间",
    },
    ("docker/scheduler.py", 282): {
        "kind": "datetime.now",
        "code": '"at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},'
                '  # noqa: DTZ005 心跳旁路观测件，与 mtime 同钟，故意用系统钟（见上方注释）',
        "reason": "心跳旁路观测件。与 mtime 同钟；业务钟不参与观测链路",
    },
}

#: `datetime` 类的宿主取时方法。`today` 在 date 类上同名，一并收口。
_NOW_ATTRS = {"now", "utcnow", "today"}

#: 「无实参」的哨兵：区分 `now()` 与 `now(None)`。
_MISSING = object()

#: `strftime` 中表示**日期**的转换符。`%H:%M:%S` 只有时刻，不算。
_DATE_STRFTIME_TOKENS = frozenset("YymdejxDFGgVUWCcuw")

#: ISO 串末尾的时区偏移。`fromisoformat` 接受 `Z`、`±HH`、`±HH:MM`、
#: `±HHMM`、`±HH:MM:SS`、`±HHMMSS` 六种写法（py3.11+）。
#: 偏移必须跟在时间部分之后：日期串里的 `-01` 不是偏移。
_ISO_OFFSET_RE = re.compile(
    r"[T ]\d{2}(?::?\d{2}(?::?\d{2}(?:[.,]\d+)?)?)?"
    r"(?:Z|[+-]\d{2}(?::?\d{2}(?::?\d{2})?)?)$"
)

#: 日期归属三态。`FORGED` = 墙钟取自别的时区、却被换成业务时区的 tzinfo。
_BUSINESS = "business"
_FOREIGN = "foreign"
_FORGED = "forged"


def _class_aliases(tree):
    """`from datetime import datetime/date [as X]` 得到的类名。"""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "datetime":
            for a in node.names:
                if a.name in ("datetime", "date"):
                    out.add(a.asname or a.name)
    return out


def _module_aliases(tree):
    """`import datetime [as X]` 得到的模块名。"""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name == "datetime":
                    out.add(a.asname or "datetime")
    return out


def _clock_aliases(tree):
    """业务钟的绑定名：`clock` 模块名、`TZ` 名、`now` 名。

    识别四种写法（与 `_class_aliases` 同口径，只按 AST 取名字）：
    · `from yiban.core import clock [as X]` → X 是模块名；
    · `from yiban.core.clock import TZ/now [as X]` → X 是属性名；
    · `from yiban.core.clock import *` → 同时绑定 `TZ` 与 `now`；
    · `import yiban.core.clock as X` → X 是模块名。
    原名 `clock` 恒在集合内：样本与代码常省略 import 行。
    `import yiban.core` 不带 `as` 的链式名不在上表，登记为边界。
    """
    clock_names = {"clock"}
    tz_names = set()
    now_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module == "yiban.core":
                for a in node.names:
                    if a.name == "clock":
                        clock_names.add(a.asname or "clock")
            elif node.module == "yiban.core.clock":
                for a in node.names:
                    if a.name == "TZ":
                        tz_names.add(a.asname or "TZ")
                    elif a.name == "now":
                        now_names.add(a.asname or "now")
                    elif a.name == "*":
                        tz_names.add("TZ")
                        now_names.add("now")
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name == "yiban.core.clock" and a.asname:
                    clock_names.add(a.asname)
    return clock_names, tz_names, now_names


class _Ctx:
    """单文件解析上下文：datetime 与业务钟的绑定名。"""

    __slots__ = ("classes", "clock_names", "modules", "now_names", "tz_names")

    def __init__(self, tree):
        self.classes = _class_aliases(tree)
        self.modules = _module_aliases(tree)
        self.clock_names, self.tz_names, self.now_names = _clock_aliases(tree)


def _is_datetime_attr(node, ctx, cls_name, attr):
    """`node` 是否是「datetime 类的 attr 属性」（含模块前缀写法）。"""
    if not isinstance(node, ast.Attribute) or node.attr != attr:
        return False
    if isinstance(node.value, ast.Name) and node.value.id in ctx.classes:
        return True
    if isinstance(node.value, ast.Attribute) and node.value.attr == cls_name:
        base = node.value.value
        return isinstance(base, ast.Name) and base.id in ctx.modules
    return False


def _is_none(node):
    return isinstance(node, ast.Constant) and node.value is None


def _is_clock_tz(node, ctx):
    """是否是业务时区：`clock.TZ`、模块别名后的 `X.TZ`、或 `TZ` 的 from 导入名。"""
    if isinstance(node, ast.Name):
        return node.id in ctx.tz_names
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "TZ"
        and isinstance(node.value, ast.Name)
        and node.value.id in ctx.clock_names
    )


def _tz_argument(call):
    """`datetime.now(...)` 的 tz 实参；未给返回 `_MISSING`。"""
    if call.args:
        return call.args[0]
    for k in call.keywords:
        if k.arg == "tz":
            return k.value
    return _MISSING


def _call_arg(call, keyword, index):
    """按位置或关键字取实参；未给返回 None。"""
    if len(call.args) > index:
        return call.args[index]
    for k in call.keywords:
        if k.arg == keyword:
            return k.value
    return None


def _is_datetime_now_call(node, ctx):
    return (
        isinstance(node, ast.Call)
        and _is_datetime_attr(node.func, ctx, "datetime", "now")
    )


def _strftime_tokens(fmt):
    """逐个产出未转义的转换符：跳过 `%%`、宽度与 `E`/`O` 修饰。"""
    i = 0
    n = len(fmt)
    while i < n:
        if fmt[i] != "%":
            i += 1
            continue
        i += 1
        while i < n and (
            fmt[i] in "-_0^# +" or fmt[i].isdigit() or fmt[i] in "EO"
        ):
            i += 1
        if i >= n:
            return
        tok = fmt[i]
        i += 1
        if tok == "%":
            continue
        yield tok


def _has_date_token(fmt):
    """格式串是否含日期转换符。"""
    return any(tok in _DATE_STRFTIME_TOKENS for tok in _strftime_tokens(fmt))


def _has_zone_token(fmt):
    """格式串是否含时区转换符（`%z` / `%Z`）：含则 strptime 产物是 aware。"""
    return any(tok in "zZ" for tok in _strftime_tokens(fmt))


def _iso_has_offset(node):
    """字面量 ISO 串是否自带时区偏移。非字面量返回 False（保守判无偏移）。"""
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and bool(_ISO_OFFSET_RE.search(node.value.strip()))
    )


def _is_aware_time(node):
    """`combine` 的 time 实参是否**可证为 aware**。

    判据：`time.fromisoformat(<带偏移的字面量>)`，或 `time(...)` 的 tzinfo
    实参（第 5 个位置参数或关键字）非 None。
    """
    if node is None:
        return False
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "fromisoformat"
    ):
        return _iso_has_offset(_call_arg(node, "date_string", 0))
    if isinstance(node, ast.Call):
        func = node.func
        is_time = (
            (isinstance(func, ast.Name) and func.id == "time")
            or (isinstance(func, ast.Attribute) and func.attr == "time")
        )
        if is_time:
            tzinfo = _call_arg(node, "tzinfo", 4)
            if tzinfo is not None:
                return not _is_none(tzinfo)
    return False


def _host_clock_kind(node, ctx):
    """宿主钟取时调用 → 类别名（`datetime.now` 等）；不是则 None。"""
    if not isinstance(node, ast.Call):
        return None
    for cls_name in ("datetime", "date"):
        for attr in _NOW_ATTRS:
            if not _is_datetime_attr(node.func, ctx, cls_name, attr):
                continue
            if attr == "now":
                tz = _tz_argument(node)
                if tz is _MISSING:
                    return "datetime.now"
                if _is_none(tz):
                    return "datetime.now(None)"
                # 带 tz：`clock.TZ` 放行；其他时区交给下游取日判定。
                continue
            return "%s.%s" % (cls_name, attr)
    return None


def _slice_starts_at_date(node):
    """切片是否覆盖日期段（前 10 个字符）。

    `[:10]`、`[:]`、无下界都算；`[11:]` 只取时刻，不算。
    下界不可判时保守算取日。
    """
    if not isinstance(node, ast.Slice):
        return False
    lower = node.lower
    if lower is None:
        return True
    if isinstance(lower, ast.Constant) and isinstance(lower.value, int):
        return lower.value == 0
    return True


def _isoformat_source(node):
    """`x.isoformat()` 或 `str(x)` 的源表达式；不是则 None。"""
    if not isinstance(node, ast.Call):
        return None
    if isinstance(node.func, ast.Attribute) and node.func.attr == "isoformat":
        return node.func.value
    if isinstance(node.func, ast.Name) and node.func.id == "str" and node.args:
        return node.args[0]
    return None


def _joined_str_text(node):
    """`JoinedStr` 的全部字面量文本；含非常量片段时返回 None。"""
    if not isinstance(node, ast.JoinedStr):
        return None
    parts = []
    for value in node.values:
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            parts.append(value.value)
        else:
            return None
    return "".join(parts)


def _iso_split_date_source(node):
    """`x.isoformat().split("T")[0]` 里的 `x`；不是该形状则 None。"""
    idx = node.slice
    if not (
        isinstance(idx, ast.Constant)
        and not isinstance(idx.value, bool)
        and idx.value == 0
    ):
        return None
    call = node.value
    if not (
        isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr == "split"
    ):
        return None
    sep = _call_arg(call, "sep", 0)
    if not (isinstance(sep, ast.Constant) and sep.value == "T"):
        return None
    inner = call.func.value
    if not (
        isinstance(inner, ast.Call)
        and isinstance(inner.func, ast.Attribute)
        and inner.func.attr == "isoformat"
    ):
        return None
    return inner.func.value


def _foreign_tz_date_kind(node, ctx):
    """从非业务时区的时刻**取日** → 违例类别；否则 None。

    先归一链路再判定（AGENTS §14 根因）：`.replace(...)`、`.astimezone(...)`
    与 `BinOp` 都按日期归属穿透。取日形状：
    · `.date()`；
    · `.strftime(<含日期占位符的格式串>)`；
    · `.isoformat()[<覆盖日期段的切片>]`；
    · `str(<时刻>)[<覆盖日期段的切片>]`；
    · `f"{<时刻>:<含日期占位符>}"`；
    · `.isoformat().split("T")[0]`。
    只作时刻、不取日时放行：aware 时刻本身与时区无关。
    """
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        attr = node.func.attr
        if attr not in ("date", "strftime"):
            return None
        if _tz_class(node.func.value, ctx) not in (_FOREIGN, _FORGED):
            return None
        if attr == "date":
            return "非业务时区取日"
        fmt = _call_arg(node, "format", 0)
        if (
            isinstance(fmt, ast.Constant)
            and isinstance(fmt.value, str)
            and _has_date_token(fmt.value)
        ):
            return "非业务时区取日"
        return None
    if isinstance(node, ast.JoinedStr):
        for value in node.values:
            if not isinstance(value, ast.FormattedValue):
                continue
            if _tz_class(value.value, ctx) not in (_FOREIGN, _FORGED):
                continue
            spec = _joined_str_text(value.format_spec)
            if spec is not None and _has_date_token(spec):
                return "非业务时区取日"
        return None
    if isinstance(node, ast.Subscript):
        if _slice_starts_at_date(node.slice):
            source = _isoformat_source(node.value)
            if source is not None and _tz_class(source, ctx) in (_FOREIGN, _FORGED):
                return "非业务时区取日"
        source = _iso_split_date_source(node)
        if source is not None and _tz_class(source, ctx) in (_FOREIGN, _FORGED):
            return "非业务时区取日"
        return None
    return None


def _is_clock_now(node, ctx):
    """业务钟取「现在」：`clock.now()`、模块别名、`now` 的 from 导入名。"""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr == "now":
        return isinstance(func.value, ast.Name) and func.value.id in ctx.clock_names
    return isinstance(func, ast.Name) and func.id in ctx.now_names


def _tz_class(node, ctx):
    """时刻表达式的**日期归属**：`_BUSINESS` / `_FOREIGN` / `_FORGED` / None。

    `None` = 静态不可判，或可证 naive 的构造（另有 astimezone 判据负责）。
    `replace` 不改墙钟，故继承；`astimezone(业务时区)` 把 aware 源换算回业务日；
    其余 `astimezone` 目标给的是别的时区的日期。
    """
    if _is_clock_now(node, ctx):
        return _BUSINESS
    if _is_datetime_now_call(node, ctx):
        tz = _tz_argument(node)
        if tz is _MISSING or _is_none(tz):
            return None
        return _BUSINESS if _is_clock_tz(tz, ctx) else _FOREIGN
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        attr = node.func.attr
        recv = node.func.value
        if attr == "replace":
            cls = _tz_class(recv, ctx)
            if cls is None:
                return None
            tzinfo = _call_arg(node, "tzinfo", 4)
            if tzinfo is None or _is_none(tzinfo):
                return cls
            if cls is _BUSINESS and _is_clock_tz(tzinfo, ctx):
                return _BUSINESS  # 业务墙钟贴业务时区标签：日期不变
            # 换 tzinfo 不改墙钟：归属被伪造，后续换算不得洗白。
            return _FORGED
        if attr == "astimezone":
            cls = _tz_class(recv, ctx)
            if cls is None:
                return None
            if cls is _FORGED:
                return _FORGED  # 伪造归属粘性（三审攻击一）
            target = _call_arg(node, "tz", 0)
            if target is None or _is_none(target):
                return _FOREIGN  # 转到宿主时区
            return _BUSINESS if _is_clock_tz(target, ctx) else _FOREIGN
        return None
    if isinstance(node, ast.BinOp):
        sides = (_tz_class(node.left, ctx), _tz_class(node.right, ctx))
        for cls in (_FORGED, _FOREIGN, _BUSINESS):
            if cls in sides:
                return cls
    return None


def _is_naive_expr(node, ctx):
    """可证为 naive 的时刻表达式（不含变量：不做数据流）。"""
    if _host_clock_kind(node, ctx) or _is_clock_now(node, ctx):
        return True
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        attr = node.func.attr
        if attr == "replace":
            return any(
                k.arg == "tzinfo" and isinstance(k.value, ast.Constant)
                and k.value.value is None
                for k in node.keywords
            )
        if attr == "strptime":
            # 格式串含 %z/%Z 时产物 aware；其余（含变量格式串）按 naive。
            fmt = _call_arg(node, "format", 1)
            return not (
                isinstance(fmt, ast.Constant)
                and isinstance(fmt.value, str)
                and _has_zone_token(fmt.value)
            )
        if attr == "fromtimestamp":
            tz = _call_arg(node, "tz", 1)
            if tz is None:
                tz = _call_arg(node, "tzinfo", 1)
            return tz is None or _is_none(tz)
        if attr == "utcfromtimestamp":
            # 恒为 naive（弃用形状，仍按宿主 UTC 解释）。
            return True
        if attr == "fromisoformat":
            return not _iso_has_offset(_call_arg(node, "date_string", 0))
        if attr == "combine":
            # 产物 tzinfo 取自 time 实参；显式 tzinfo 实参优先。
            explicit = _call_arg(node, "tzinfo", 2)
            if explicit is not None:
                return _is_none(explicit)
            return not _is_aware_time(_call_arg(node, "time", 1))
    if isinstance(node, ast.BinOp):
        # 任一侧 naive 则结果 naive：`timedelta(0) + clock.now()` 必须判红。
        return (
            _is_naive_expr(node.left, ctx)
            or _is_naive_expr(node.right, ctx)
        )
    return False


def _offenders_source(source, filename="<memory>"):
    """返回命中列表：`(行号, 类别, 源码行)`，按行号升序。"""
    tree = ast.parse(source, filename=filename)
    ctx = _Ctx(tree)
    lines = source.splitlines()
    hits = []
    for node in ast.walk(tree):
        kind = None
        if _host_clock_kind(node, ctx):
            kind = _host_clock_kind(node, ctx)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "astimezone"
            and _is_naive_expr(node.func.value, ctx)
        ):
            kind = "naive astimezone"
        else:
            kind = _foreign_tz_date_kind(node, ctx)
        if kind is None:
            continue
        src = lines[node.lineno - 1].strip() if node.lineno <= len(lines) else ""
        hits.append((node.lineno, kind, src))
    return sorted(hits)


def _iter_product_files(base=BASE):
    """产出 `(相对路径, 绝对路径)`；跳过时钟本尊。"""
    for rel_dir in PRODUCT_DIRS:
        root = os.path.join(base, rel_dir)
        for dirpath, _dirnames, filenames in os.walk(root):
            for name in sorted(filenames):
                if not name.endswith(".py"):
                    continue
                path = os.path.join(dirpath, name)
                rel = os.path.relpath(path, base).replace(os.sep, "/")
                if rel == CLOCK_MODULE:
                    continue
                yield rel, path


def _scan_product(base=BASE):
    """返回 `(offenders, counts)`。

    `offenders` 是 `(相对路径, 行号, 类别, 源码行)` 列表。
    `counts` 是各产品目录被扫描的文件数。
    """
    offenders = []
    counts = {d: 0 for d in PRODUCT_DIRS}
    for rel, path in _iter_product_files(base):
        counts[rel.split("/", 1)[0]] += 1
        with open(path, encoding="utf-8") as f:
            source = f.read()
        for line, kind, src in _offenders_source(source, filename=path):
            offenders.append((rel, line, kind, src))
    return offenders, counts


def _surface_problem(counts):
    """扫描面不合格 → 返回理由；合格 → None。

    扫空必须判红（AGENTS §30）：目录写错或树被剪空时，空 offenders
    会让后面的断言恒真。
    """
    if sum(counts.values()) < MIN_FILES:
        return "产品面扫描文件过少（守卫会静默空扫）：%r" % (counts,)
    for rel_dir in PRODUCT_DIRS:
        if counts.get(rel_dir, 0) <= 0:
            return "%s/ 未扫到 .py 文件：路径写错会静默空扫" % rel_dir
    return None


def _judge(offenders, exemptions=EXEMPTIONS):
    """把命中与豁免对账。返回 `(unexempted, mismatched, stale)`。

    `unexempted`：未登记的命中。
    `mismatched`：登记项还对得上行号，但类别或代码已变。
    `stale`：登记项在本轮完全没有命中（行号漂移或代码已删）。
    三者任一非空都必须判红。
    """
    used = set()
    unexempted = []
    mismatched = []
    for rel, line, kind, src in offenders:
        key = (rel, line)
        entry = exemptions.get(key)
        if entry is None:
            unexempted.append("%s:%d [%s] %s" % (rel, line, kind, src))
            continue
        used.add(key)
        if entry["kind"] != kind:
            mismatched.append(
                "%s:%d 的豁免理由与命中类别不符（登记 %s，实际 %s）"
                % (rel, line, entry["kind"], kind))
        elif _normalize(entry["code"]) != _normalize(src):
            mismatched.append(
                "%s:%d 代码已变，豁免理由需重审。登记：%s 实际：%s"
                % (rel, line, entry["code"], src))
    stale = sorted(set(exemptions) - used)
    return unexempted, mismatched, stale


def _normalize(line):
    return " ".join(line.split())


class ClockSingleSourceGateTest(unittest.TestCase):
    def test_product_code_takes_now_only_from_clock_module(self):
        offenders, counts = _scan_product()
        problem = _surface_problem(counts)
        self.assertIsNone(problem, problem or "")

        unexempted, mismatched, stale = _judge(offenders)
        self.assertEqual(
            mismatched, [],
            "豁免登记与现状不符（类别或代码已变）：\n  " + "\n  ".join(mismatched))
        self.assertEqual(
            stale, [],
            "显式豁免已不再命中（行号漂移或代码已删）：请更新或删除登记。"
            "陈旧项：\n  " + "\n  ".join("%s:%d" % k for k in stale))
        self.assertEqual(
            unexempted, [],
            "产品代码用宿主钟取『现在』。业务时间唯一来源是 `yiban.core.clock`；"
            "UTC 16:00–24:00 窗口内宿主日与业务日差一天（P0 2026-10-25）。"
            "改用 `clock.now()` / `clock.today()` / `clock.ts()`。"
            "确有物理时刻或唯一性语义时才登记豁免。违例：\n  "
            + "\n  ".join(unexempted))


#: 检测器自测样本：每条命中形一份正样本、每条放行形一份负样本（防回归）。
BAD_SAMPLES = {
    "类导入 datetime.now": ("from datetime import datetime\n"
                            "x = datetime.now()\n"),
    "模块导入 datetime.datetime.now": ("import datetime\n"
                                       "x = datetime.datetime.now()\n"),
    "utcnow": ("from datetime import datetime\n"
               "x = datetime.utcnow()\n"),
    "date.today": ("from datetime import date\n"
                   "x = date.today()\n"),
    "datetime.date.today": ("import datetime\n"
                            "x = datetime.date.today()\n"),
    "datetime.today": ("import datetime\n"
                       "x = datetime.datetime.today()\n"),
    "重命名导入": ("from datetime import datetime as dt\n"
                   "x = dt.now()\n"),
    "clock.now().astimezone": ("x = clock.now().astimezone()\n"),
    "宿主 now 的 astimezone": ("from datetime import datetime\n"
                               "x = datetime.now().astimezone()\n"),
    "replace(tzinfo=None) 后 astimezone": ("from datetime import datetime\n"
                                           "x = datetime.now().replace(tzinfo=None).astimezone()\n"),
    "strptime 后 astimezone": ("from datetime import datetime\n"
                               'x = datetime.strptime("2026-01-01", "%Y-%m-%d").astimezone()\n'),
    "无 tz 的 fromtimestamp 后 astimezone": (
        "from datetime import datetime\n"
        "x = datetime.fromtimestamp(0).astimezone()\n"),
    "utcfromtimestamp 后 astimezone": (
        "from datetime import datetime\n"
        "x = datetime.utcfromtimestamp(0).astimezone()\n"),
    "无偏移的 fromisoformat 后 astimezone": (
        "from datetime import datetime\n"
        'x = datetime.fromisoformat("2026-01-01T00:00:00").astimezone()\n'),
    "无 tzinfo 的 combine 后 astimezone": (
        "from datetime import datetime, date, time\n"
        "x = datetime.combine(date(2026, 1, 1), time(0)).astimezone()\n"),
    "BinOp 左侧 naive 后 astimezone": (
        "from datetime import timedelta\n"
        "x = (clock.now() + timedelta(0)).astimezone()\n"),
    "BinOp 右侧 naive 后 astimezone": (
        "from datetime import timedelta\n"
        "x = (timedelta(0) + clock.now()).astimezone()\n"),
    "now(tz=None) 的宿主钟取时": (
        "from datetime import datetime\n"
        "x = datetime.now(tz=None)\n"),
    "非业务时区 strftime 取日": (
        "from datetime import datetime, timezone\n"
        'x = datetime.now(timezone.utc).strftime("%Y-%m-%d")\n'),
    "非业务时区 date 取日": (
        "from datetime import datetime, timezone\n"
        "x = datetime.now(timezone.utc).date()\n"),
    "非业务时区 replace 后 strftime 取日": (
        "from datetime import datetime, timezone\n"
        'x = datetime.now(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%d")\n'),
    "非业务时区 replace 后 date 取日": (
        "from datetime import datetime, timezone\n"
        "x = datetime.now(timezone.utc).replace(microsecond=0).date()\n"),
    "业务时区转别的时区后 date 取日": (
        "from datetime import datetime, timezone\n"
        "x = datetime.now(clock.TZ).astimezone(timezone.utc).date()\n"),
    "业务时区转别的时区后 strftime 取日": (
        "from datetime import datetime, timezone\n"
        'x = datetime.now(clock.TZ).astimezone(timezone.utc).strftime("%Y-%m-%d")\n'),
    "非业务时区 BinOp 后 date 取日": (
        "from datetime import datetime, timedelta, timezone\n"
        "x = (datetime.now(timezone.utc) + timedelta(minutes=1)).date()\n"),
    "非业务时区 isoformat 切片取日": (
        "from datetime import datetime, timezone\n"
        "x = datetime.now(timezone.utc).isoformat()[:10]\n"),
    "非业务时区 str 切片取日": (
        "from datetime import datetime, timezone\n"
        "x = str(datetime.now(timezone.utc))[:10]\n"),
    "非业务时区 %u 取日": (
        "from datetime import datetime, timezone\n"
        'x = datetime.now(timezone.utc).strftime("%u")\n'),
    "非业务时区 %w 取日": (
        "from datetime import datetime, timezone\n"
        'x = datetime.now(timezone.utc).strftime("%w")\n'),
    "非业务时区 f-string 取日": (
        "from datetime import datetime, timezone\n"
        'x = f"{datetime.now(timezone.utc):%Y-%m-%d}"\n'),
    "非业务时区 isoformat split 取日": (
        "from datetime import datetime, timezone\n"
        'x = datetime.now(timezone.utc).isoformat().split("T")[0]\n'),
    "洗白攻击一：伪造归属后连转业务时区": (
        "from datetime import datetime, timezone\n"
        "x = datetime.now(timezone.utc).replace(tzinfo=clock.TZ)"
        ".astimezone(clock.TZ).astimezone(clock.TZ).date()\n"),
    "洗白攻击二：业务墙钟贴 UTC 标签后转回": (
        "from datetime import timezone\n"
        "x = clock.now().replace(tzinfo=timezone.utc).astimezone(clock.TZ).date()\n"),
    "clock 模块别名后 astimezone": (
        "from yiban.core import clock as _clock\n"
        "x = _clock.now().astimezone()\n"),
    "now 的 from 导入后 astimezone": (
        "from yiban.core.clock import now\n"
        "x = now().astimezone()\n"),
    "星号导入 now 后 astimezone": (
        "from yiban.core.clock import *\n"
        "x = now().astimezone()\n"),
    "date 重命名导入": ("from datetime import date as d\n"
                       "x = d.today()\n"),
    "import 模块别名": ("import yiban.core.clock as clk\n"
                        "x = clk.now().astimezone()\n"),
}
GOOD_SAMPLES = {
    "带时区的 now": ("from datetime import datetime, timezone\n"
                     "x = datetime.now(timezone.utc)\n"),
    "带时区的 now（关键字）": ("from datetime import datetime\n"
                               "x = datetime.now(tz=clock.TZ)\n"),
    "业务钟": ("x = clock.now()\ny = clock.today()\nz = clock.ts()\n"),
    "epoch 秒": ("import time\nx = time.time()\ny = time.monotonic()\n"
                 "z = time.perf_counter()\n"),
    "known aware 的 astimezone": ("import datetime\n"
                                  "x = datetime.datetime.now(datetime.timezone.utc).astimezone()\n"),
    "带 tz 的 fromtimestamp 后 astimezone": (
        "from datetime import datetime, timezone\n"
        "x = datetime.fromtimestamp(0, timezone.utc).astimezone()\n"),
    "带偏移的 fromisoformat 后 astimezone": (
        "from datetime import datetime\n"
        'x = datetime.fromisoformat("2026-01-01T00:00:00+08:00").astimezone()\n'),
    "strptime 含 %z 后 astimezone": (
        "from datetime import datetime\n"
        'x = datetime.strptime("2026-01-01T00:00:00+08:00",'
        ' "%Y-%m-%dT%H:%M:%S%z").astimezone()\n'),
    "combine tz-aware time 后 astimezone": (
        "from datetime import datetime, date, time, timezone\n"
        "x = datetime.combine(date(2026, 1, 1), time(0, tzinfo=timezone.utc)).astimezone()\n"),
    "带 tzinfo 的 combine 后 astimezone": (
        "from datetime import datetime, date, time, timezone\n"
        "x = datetime.combine(date(2026, 1, 1), time(0), timezone.utc).astimezone()\n"),
    "clock.TZ 的 now 取日": (
        "from datetime import datetime\n"
        'x = datetime.now(clock.TZ).strftime("%Y-%m-%d")\n'
        "y = datetime.now(tz=clock.TZ).date()\n"),
    "TZ 的 from 导入": (
        "from datetime import datetime\n"
        "from yiban.core.clock import TZ\n"
        "x = datetime.now(TZ)\n"
        "y = datetime.now(tz=TZ).date()\n"),
    "clock 模块别名取日": (
        "from datetime import datetime\n"
        "from yiban.core import clock as clk\n"
        "x = datetime.now(clk.TZ).date()\n"),
    "aware 外部时刻转回业务时区取日": (
        "from datetime import datetime, timezone\n"
        "x = datetime.now(timezone.utc).astimezone(clock.TZ).date()\n"),
    "非业务时区仅取时刻切片": (
        "from datetime import datetime, timezone\n"
        "x = datetime.now(timezone.utc).isoformat()[11:]\n"),
    "非业务时区 f-string 仅取时刻": (
        "from datetime import datetime, timezone\n"
        'x = f"{datetime.now(timezone.utc):%H:%M}"\n'),
    "非业务时区 isoformat split 取时刻": (
        "from datetime import datetime, timezone\n"
        'x = datetime.now(timezone.utc).isoformat().split("T")[1]\n'),
    "业务钟的 f-string 取日": ('x = f"{clock.now():%Y-%m-%d}"\n'),
    "业务钟的 isoformat split 取日": (
        'x = clock.now().isoformat().split("T")[0]\n'),
    "业务钟的时刻切片": ("x = str(clock.now())[:10]\n"),
    "两位小时偏移的 fromisoformat": (
        "from datetime import datetime\n"
        'x = datetime.fromisoformat("2026-01-01T00:00+08").astimezone()\n'),
    "全秒偏移的 fromisoformat": (
        "from datetime import datetime\n"
        'x = datetime.fromisoformat("2026-01-01T00:00:00+08:00:00").astimezone()\n'),
    "非业务时区仅作时刻": (
        "from datetime import datetime, timezone\n"
        'x = datetime.now(timezone.utc).strftime("%H:%M:%S")\n'
        "y = datetime.now(timezone.utc).timestamp()\n"),
    "变量接收者不做数据流": ("x = t.astimezone()\n"),
    "注释与字符串不算": ('"""datetime.now() 只是文字"""\n# datetime.now()\nx = "datetime.now()"\n'),
}


class GateSelfTest(unittest.TestCase):
    """检测器自测：每条命中形都有样本，放行形不得误报（防回归）。"""

    def test_positive_samples_hit(self):
        for name, src in BAD_SAMPLES.items():
            with self.subTest(name=name):
                self.assertTrue(_offenders_source(src), "样本未被检出：%s" % name)

    def test_negative_samples_do_not_hit(self):
        for name, src in GOOD_SAMPLES.items():
            with self.subTest(name=name):
                self.assertEqual(_offenders_source(src), [],
                                 "样本被误报：%s" % name)

    def test_line_numbers_are_source_lines(self):
        src = ("from datetime import datetime\n"
               "\n"
               "\n"
               "x = datetime.now()\n")
        self.assertEqual([h[0] for h in _offenders_source(src)], [4])

    def test_detector_sees_alias_imports(self):
        """`from datetime import datetime as dt` 必须与原名同判。"""
        self.assertEqual(len(_offenders_source(
            "from datetime import datetime as dt\nx = dt.now()\n")), 1)

    def test_empty_scan_surface_is_red(self):
        """扫空判红：五个目录都为空时 `_surface_problem` 必须给理由。"""
        self.assertIsNotNone(_surface_problem({d: 0 for d in PRODUCT_DIRS}))

    def test_scan_of_missing_tree_is_red(self):
        """目录名写错时 walks 得零文件；守卫不得当成「没有违规」。"""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            _offenders, counts = _scan_product(tmp)
        self.assertIsNotNone(_surface_problem(counts))

    def test_exemption_must_match_line_and_code(self):
        """豁免的三种失效各自判红：未登记、行号漂移、同位置代码已变。"""
        exemptions = {
            ("pkg/mod.py", 10): {"kind": "datetime.now", "code": "x = datetime.now()"},
        }
        ok = [("pkg/mod.py", 10, "datetime.now", "x = datetime.now()")]
        self.assertEqual(_judge(ok, exemptions), ([], [], []))
        # 行号漂移：旧登记陈旧 + 新位置未登记。
        drifted = [("pkg/mod.py", 11, "datetime.now", "x = datetime.now()")]
        unexempted, mismatched, stale = _judge(drifted, exemptions)
        self.assertEqual(len(unexempted), 1)
        self.assertEqual(mismatched, [])
        self.assertEqual(stale, [("pkg/mod.py", 10)])
        # 同一行代码已变：登记项不再描述现状。
        changed = [("pkg/mod.py", 10, "datetime.now", "x = datetime.now().date()")]
        _unexempted, mismatched, stale = _judge(changed, exemptions)
        self.assertEqual(len(mismatched), 1)
        self.assertEqual(stale, [])


if __name__ == "__main__":
    unittest.main()
