# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""结构守卫：产品代码的「现在」只许取自 `yiban.core.clock`。

背景（2026-10-25 P0）：生产业务时间是北京时间（UTC+8）。
`datetime.now()` 取宿主本地时区。
部署在 UTC 主机时，北京时间 00:00–08:00 与宿主日期不同。
按天日志名、状态文件名、备份归档名、日历取样日会错一天。
测试面已有 `test_business_day_source_gate.py`。
本门补产品面：`yiban/`、`web/`、`scripts/`、`docker/` 的 `.py`。
`yiban/core/clock.py` 是唯一时点来源，不在扫描面内。

判定面（静态，只读源码，不取真实时钟）：
· 宿主钟取「现在」的调用，命中即报 `文件:行`：
    1. `datetime.now()`（含 `datetime.datetime.now()`）；
    2. `datetime.utcnow()`（含别名导入）；
    3. `date.today()` / `datetime.date.today()` / `datetime.today()`；
    4. `datetime.now(<tz>)` 是**带时区**取时，不算；本门放行。
· `naive` 时刻上的 `.astimezone()`：按宿主时区解释北京墙钟。
  这是 P0 的原始缺陷形状（`clock.now().astimezone()`）。
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
· 别名变量（`t = datetime.now(); t.strftime(...)`）：本门不做数据流。
· 变量接收者的 `.astimezone()`：静态不可判是否 aware。
· 同一形状出现两处以上即按形状立门（AGENTS §27）。

扫空不得当绿：四个产品目录各自的 `.py` 文件数都必须大于 0。
"""

import ast
import os
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
#: 产品面目录（相对仓库根）。测试面不在内：见 test_business_day_source_gate.py。
PRODUCT_DIRS = ("yiban", "web", "scripts", "docker")
#: 业务时钟本尊：它必须读宿主钟并换算出 +8，是唯一豁免文件。
CLOCK_MODULE = "yiban/core/clock.py"
#: 扫描面下限（AGENTS §30）。本树实测 157 个文件；下限取 100。
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
    ("scripts/dev_visual_seed.py", 239): {
        "kind": "datetime.now",
        "code": 'db.set_account_deleted(aid, True, '
                'deleted_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"), '
                'deleted_by="admin")  # noqa: DTZ005 开发演示种子数据，非判定路径',
        "reason": "开发机演示种子。非部署件、非 CI、非判定路径",
    },
    ("scripts/dev_visual_seed.py", 243): {
        "kind": "datetime.now",
        "code": "now = datetime.now()  # noqa: DTZ005 开发演示种子数据，非判定路径",
        "reason": "开发机演示种子。非部署件、非 CI、非判定路径",
    },
    ("scripts/state_cleanup.py", 98): {
        "kind": "datetime.now",
        "code": 'stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")'
                '  # noqa: DTZ005 日志行展示时刻，刻意跟随宿主钟（见模块头注 MF-109）',
        "reason": "清理日志的展示时刻，跟随宿主钟。删除判据另走北京时间",
    },
    ("docker/scheduler.py", 280): {
        "kind": "datetime.now",
        "code": '"at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},'
                '  # noqa: DTZ005 心跳旁路观测件，与 mtime 同钟，故意用系统钟（见上方注释）',
        "reason": "心跳旁路观测件。与 mtime 同钟；业务钟不参与观测链路",
    },
}

#: `datetime` 类的宿主取时方法。`today` 在 date 类上同名，一并收口。
_NOW_ATTRS = {"now", "utcnow", "today"}


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


def _is_datetime_attr(node, classes, modules, cls_name, attr):
    """`node` 是否是「datetime 类的 attr 属性」（含模块前缀写法）。"""
    if not isinstance(node, ast.Attribute) or node.attr != attr:
        return False
    if isinstance(node.value, ast.Name) and node.value.id in classes:
        return True
    if isinstance(node.value, ast.Attribute) and node.value.attr == cls_name:
        base = node.value.value
        return isinstance(base, ast.Name) and base.id in modules
    return False


def _has_tz_arg(call):
    return bool(call.args) or any(k.arg == "tz" for k in call.keywords)


def _host_clock_kind(node, classes, modules):
    """宿主钟取时调用 → 类别名（`datetime.now` 等）；不是则 None。"""
    if not isinstance(node, ast.Call):
        return None
    for cls_name in ("datetime", "date"):
        for attr in _NOW_ATTRS:
            if not _is_datetime_attr(node.func, classes, modules, cls_name, attr):
                continue
            if attr == "now" and _has_tz_arg(node):
                continue
            return "%s.%s" % (cls_name, attr)
    return None


def _is_clock_now(node):
    """`clock.now()`：业务钟契约明示返回 naive 北京时间。"""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "now"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "clock"
    )


def _is_naive_expr(node, classes, modules):
    """可证为 naive 的时刻表达式（不含变量：不做数据流）。"""
    if _host_clock_kind(node, classes, modules) or _is_clock_now(node):
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
            # strptime 无 tz 参数，产物恒为 naive。
            return True
        if attr == "fromtimestamp":
            return not (len(node.args) >= 2 or any(
                k.arg in ("tz", "tzinfo") for k in node.keywords))
        if attr == "combine":
            return not (len(node.args) >= 3 or any(
                k.arg == "tzinfo" for k in node.keywords))
    if isinstance(node, ast.BinOp):
        return _is_naive_expr(node.left, classes, modules)
    return False


def _offenders_source(source, filename="<memory>"):
    """返回命中列表：`(行号, 类别, 源码行)`，按行号升序。"""
    tree = ast.parse(source, filename=filename)
    classes = _class_aliases(tree)
    modules = _module_aliases(tree)
    lines = source.splitlines()
    hits = []
    for node in ast.walk(tree):
        kind = None
        if _host_clock_kind(node, classes, modules):
            kind = _host_clock_kind(node, classes, modules)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "astimezone"
            and _is_naive_expr(node.func.value, classes, modules)
        ):
            kind = "naive astimezone"
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
    "无 tzinfo 的 combine 后 astimezone": (
        "from datetime import datetime, date, time\n"
        "x = datetime.combine(date(2026, 1, 1), time(0)).astimezone()\n"),
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
    "带 tzinfo 的 combine 后 astimezone": (
        "from datetime import datetime, date, time, timezone\n"
        "x = datetime.combine(date(2026, 1, 1), time(0), timezone.utc).astimezone()\n"),
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
        """扫空判红：四个目录都为空时 `_surface_problem` 必须给理由。"""
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
