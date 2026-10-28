# -*- coding: utf-8 -*-
"""模块级日期常量门：夹具里的"日期常量"必须逐个说清是哪一类（140 §六-5）。

标签：D · 状态词汇与账号生命周期

**本门补哪一维**：既有门 `tests/test_no_literal_date_in_fixtures.py` 只覆盖**默认参数位**
的字面日期（5f9q 立；它挡的正是"夹具默认值写死 `2026-10-13` ＋ 快照超龄降级 ⇒ 隔夜
3 处转红"那次）。140 §六-5 指出的另一面是**模块级常量**（`DAY = "2026-09-22"`）。

**口径订正（现量，不抄普查）**：普查写"模块级常量 88 文件、368 处"。现量（命令见下）
是 **28 文件、32 处**——那 368 是**所有**日期字符串字面量（含内联串、字典值、函数实参），
不是模块级常量。本门按**模块级常量**这一面建门，量按现量取。

**本门钉的性质（两条）**：
  ① **常量名必须登记过**：模块级日期常量的**名字**必须在 `DATE_CONSTANT_KINDS` 里，
     并标明它属于哪一类——"固定的业务日夹具"（永不腐坏，因为它本来就指某一天）还是
     "刻意的过去时刻"（超龄/陈旧样本）。**没登记的名字一律红**：`TODAY` / `NOW` /
     `CURRENT_DAY` 这类名字正是"把现在写成常量"的形状，它们必须被挡下。
  ② **总量不得静默增长**：处数与文件数冻结成基线；多一处、多一个文件即红，逼人回来看
     "这一枚是固定业务日，还是把'现在'写死了"。

**为什么按"名字＋总量"而不按"逐处登记"**：逐处登记 30 条会把门变成纸面工作，而真正
有判别力的信号只有两个——**名字像不像"现在"**、**量有没有涨**。两者都不覆盖的
"同一文件内一增一删"如实登记为**未覆盖**。

**本门不挡什么（如实登记）**：
  · 它**不**判断某个常量是否**真的**腐坏（"固定业务日"与"写死的今天"在静态上同形）。
  · 默认参数位那一面由既有门覆盖，本门**有意不重复**。

对应实现：`tests/**` 的模块级 `Assign`（AST）；既有门 `test_no_literal_date_in_fixtures.py`。
关键断言：钉"日期常量有名有类""总量不静默增长"两条性质；不钉任何用例行为。
依赖：纯静态 AST；不跑子进程、不触网。整文件在本机执行，无 skip。

现量命令（WSL，本仓根）：
    python - <<'EOF'
    import ast, os, re, collections
    DATE = re.compile(r"^20\\d\\d-\\d\\d-\\d\\d")
    ...（对 tests/** 的 tree.body 里的 Assign 取 ast.Constant 字符串）
    EOF
"""
import ast
import os
import re
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS_DIR = os.path.join(BASE, "tests")

_DATE = re.compile(r"^20\d\d-\d\d-\d\d")

#: 日期常量名 → 类别判定。三类：固定业务日夹具 / 刻意的过去时刻 / 其它已核项。
#: 新增一个名字之前先问一句："它是固定的某一天，还是把'现在'写死了？"
#: 后者必须改成从时钟取（`yiban.core.clock.now()`），不许登记。
DATE_CONSTANT_KINDS = {
    "DAY": "固定业务日夹具（周二/周三等，避开周末门）——它本来就指某一天，不随真实今天漂移",
    "DAY2": "同上，第二个业务日（跨日/相邻日对照）",
    "NEXT_DAY": "同上，次日（跨日边界对照）",
    "HIST_DATE": "刻意的历史日（历史行/补账样本）",
    "STALE_STAMP": "刻意的过去时刻（超龄/陈旧样本）",
    "LOG_DATE": "固定日志日（日志分日查询样本）",
    "CAL_DATE": "固定日历日（日历面板样本）",
}

#: 基线（现量冻结）：处数、文件数。
#: 32/28 的增量来自 tests/test_emit_ledger.py 的 `DAY`/`NEXT_DAY`（单 3zr5）：
#: 两者都是**固定业务日夹具**（跨日边界对照），不随真实今天漂移。
#: 33/29 的增量来自 tests/test_cleanup_dotenv_parity_e2e.py 的 `STALE_STAMP`
#: （2026-10-24，R3-019）：清理 cron 的"过期按日件"样本，取 2020-01-01，
#: 远早于默认 365 天保留期——属**刻意的过去时刻**，不随真实今天漂移。
BASELINE_SITES = 33
BASELINE_FILES = 29


def scan_source(src):
    """返回该源码里模块级日期常量 `[(名字, 值), ...]`（只看 `tree.body` 的 Assign）。"""
    out = []
    for node in ast.parse(src).body:
        if not isinstance(node, ast.Assign):
            continue
        value = node.value
        if not (isinstance(value, ast.Constant) and isinstance(value.value, str)):
            continue
        if not _DATE.match(value.value):
            continue
        for t in node.targets:
            if isinstance(t, ast.Name):
                out.append((t.id, value.value))
    return out


def scan_tests():
    """返回 `({相对路径: [(名字, 值), ...]}, 解析失败文件)`。

    第二个返回值供 **fail-closed** 用：解析不了的文件**不许静默跳过**——跳过等于把它
    从判定面里悄悄摘掉（AGENTS.md §十三.29）。调用方必须断言它为空。
    """
    found, unparsed = {}, []
    for dirpath, _dirs, files in os.walk(TESTS_DIR):
        for fn in sorted(files):
            if not fn.endswith(".py"):
                continue
            path = os.path.join(dirpath, fn)
            rel = os.path.relpath(path, BASE).replace(os.sep, "/")
            try:
                with open(path, encoding="utf-8") as f:
                    src = f.read()
                hits = scan_source(src)
            except (SyntaxError, UnicodeDecodeError):
                unparsed.append(rel)
                continue
            if hits:
                found[rel] = hits
    return found, unparsed


class ModuleDateConstantTest(unittest.TestCase):
    """夹具里的模块级日期常量：名字有类、总量有界。"""

    def setUp(self):
        self.found, self.unparsed = scan_tests()

    def test_前置就绪_扫面非空且全部文件都解析成功(self):
        self.assertEqual(self.unparsed, [],
                         "这些用例件解析不了，判定面已静默缩小（漏扫＝门有洞）：%s"
                         % self.unparsed)
        self.assertGreater(
            sum(len(v) for v in self.found.values()), 0,
            "一个模块级日期常量都没扫到：判定面已失效（现量有 %d 处），门在空转"
            % BASELINE_SITES)
        self.assertGreater(len(self.found), 0, "扫面文件数为 0：门在空转")

    def test_常量名必须登记过(self):
        names = sorted({n for hits in self.found.values() for n, _v in hits})
        unknown = [n for n in names if n not in DATE_CONSTANT_KINDS]
        self.assertEqual(
            unknown, [],
            "这些模块级日期常量的名字没登记过：%s。\n"
            "先问一句：它是**固定的某一天**（业务日夹具，不随真实今天漂移），还是"
            "**把'现在'写死了**？后者必须改成从时钟取（`yiban.core.clock.now()`），"
            "不许登记——`TODAY`/`NOW` 这类名字正是隔夜腐坏的形状（AGENTS.md §十三 形状④）。"
            "确认是前者，再加进 `DATE_CONSTANT_KINDS`。" % unknown)

    def test_总量不得静默增长(self):
        sites = sum(len(v) for v in self.found.values())
        files = len(self.found)
        self.assertEqual(
            (sites, files), (BASELINE_SITES, BASELINE_FILES),
            "模块级日期常量的总量变了：处数 %d→%d、文件数 %d→%d。\n"
            "多出来的那一枚是新加的日期常量——回去看它是'固定业务日'还是'写死的今天'；"
            "确认前者之后，把基线改成新的实测值（这一步是逼你看一眼，不是形式）。"
            % (BASELINE_SITES, sites, BASELINE_FILES, files))

    def test_登记表无多余条目(self):
        names = {n for hits in self.found.values() for n, _v in hits}
        extra = sorted(set(DATE_CONSTANT_KINDS) - names)
        self.assertEqual(
            extra, [],
            "登记表里有名字已不存在（常量被删/改名）：%s——条目要同步删" % extra)

    def test_检查器有牙(self):
        """合成源码：`TODAY = "2026-10-13"` 必须被扫到且因未登记而红。"""
        hits = scan_source('TODAY = "2026-10-13"\n')
        self.assertEqual(hits, [("TODAY", "2026-10-13")],
                         "扫描器对模块级日期常量无牙")
        self.assertNotIn("TODAY", DATE_CONSTANT_KINDS,
                         "`TODAY` 这类名字不得出现在登记表里（它就是把'现在'写死的形状）")
        self.assertEqual(scan_source('X = "not-a-date"\n'), [],
                         "扫描器把非日期串也当日期常量——判定面过宽")


if __name__ == "__main__":
    unittest.main(verbosity=2)
