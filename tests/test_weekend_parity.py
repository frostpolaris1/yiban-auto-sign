# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""周末停签判定对拍：前端 `offDayOf` 与后端 `schedule.day_off` 逐组同结论。

标签：B · 调度：领取/队列/执行体
覆盖：`frontend/src/calendar/model.js::offDayOf`（node 真跑）与
    `yiban/engine/schedule.py::day_off`（真函数）在 7 天 × 4 组开关上的结论。
对应实现：后端 `day_off` 是周末门与暂停门的唯一实现；前端 `offDayOf` 是同口径的
    展示副本（只负责日历置灰）。两份实现漂移时，面板会标错"今天停签/照签"。
关键断言：
  ① 7 天 × 4 组（周六/周日开关）逐组结论相同：`日` ↔ `sunday`、`六` ↔ `saturday`、空 ↔ 空；
  ② 天数与组数非零（扫空不得当绿）；
  ③ 前后端在"两天都关/两天都开"的边界上也不分叉。
边界：一键暂停（`DAY_OFF_PAUSED`）只在服务端判定，前端日历无此维度，故不在对拍面内。
依赖：**需要 node**（跳过理由逐字登记在 `tests/test_skip_visibility_gate.py` 的
`KNOWN_SKIP_SITES`，跳过不是绿）；无网络。
"""

import datetime
import json
import os
import shutil
import subprocess
import unittest

from yiban.engine import schedule

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODEL_JS = os.path.join(BASE, "frontend", "src", "calendar", "model.js")
NODE = shutil.which("node")

#: 2026-10-19（周一）..2026-10-25（周日）：一周七天各一格。
DATES = tuple((2026, 10, day) for day in range(19, 26))
#: 周六/周日开关全组合：(sat, sun)。
FLAG_COMBOS = ((False, False), (False, True), (True, False), (True, True))
#: 服务端原因串 → 前端角标（空串=照签）。
_JS_BADGE = {
    schedule.DAY_OFF_SUNDAY: "日",
    schedule.DAY_OFF_SATURDAY: "六",
    "": "",
}


def extract_js_function(src, name):
    """按花括号配对抽出 `function <name>(...) { ... }` 整段。"""
    start = src.index("function " + name + "(")
    i = src.index("{", start)
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[start:j + 1]
    raise AssertionError("JS 函数 %s 未找到匹配的右花括号" % name)


def _js_off_days(fn, cases):
    script = (
        fn + "\nvar cases = " + json.dumps(cases) + ";\n"
        + "console.log(JSON.stringify(cases.map(function (c) {"
        + " return offDayOf(c.date, c.flags); })));\n"
    )
    proc = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise AssertionError("node 执行失败：%s" % (proc.stderr or proc.stdout))
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _py_off_day(date, sat, sun):
    reason = schedule.day_off(
        now=datetime.datetime(date[0], date[1], date[2], 12, 0),
        sat=sat, sun=sun, env={})
    if reason not in _JS_BADGE:
        raise AssertionError("day_off 返回未登记原因：%r" % (reason,))
    return _JS_BADGE[reason]


@unittest.skipUnless(NODE, "node 不可用：跳过周末停签前后端对拍（跳过不是绿）")
class WeekendParityTest(unittest.TestCase):
    """7 天 × 4 组开关逐组对结论。"""

    @classmethod
    def setUpClass(cls):
        with open(MODEL_JS, encoding="utf-8") as fh:
            cls.js_fn = extract_js_function(fh.read(), "offDayOf")

    def test_weekend_gate_agrees_on_every_combination(self):
        cases = []
        for date in DATES:
            iso = "%04d-%02d-%02d" % date
            for sat, sun in FLAG_COMBOS:
                cases.append({"date": iso, "flags": {"saturday": sat, "sunday": sun},
                              "sat": sat, "sun": sun})
        self.assertEqual(len(cases), 28, "对拍面应为 7 天 × 4 组")
        front = _js_off_days(self.js_fn, cases)
        self.assertEqual(len(front), len(cases))
        problems = []
        for case, got in zip(cases, front, strict=True):
            date = tuple(int(x) for x in case["date"].split("-"))
            want = _py_off_day(date, case["sat"], case["sun"])
            if got != want:
                problems.append(
                    "  %s sat=%s sun=%s：前端 %r != 后端 %r"
                    % (case["date"], case["sat"], case["sun"], got, want))
        if problems:
            self.fail("周末停签判定前后端不一致：\n" + "\n".join(problems))

    def test_both_closed_and_both_open_have_no_divergence(self):
        """两天都关/两天都开的边界单独再钉一次（最容易漂移的两端）。"""
        iso = lambda d: "%04d-%02d-%02d" % d  # noqa: E731
        cases = [{"date": iso(d), "flags": {"saturday": sat, "sunday": sun},
                  "sat": sat, "sun": sun}
                 for d in DATES for sat, sun in ((False, False), (True, True))]
        front = _js_off_days(self.js_fn, cases)
        for case, got in zip(cases, front, strict=True):
            date = tuple(int(x) for x in case["date"].split("-"))
            self.assertEqual(got, _py_off_day(date, case["sat"], case["sun"]),
                             "边界组合分叉：%s sat=%s sun=%s"
                             % (case["date"], case["sat"], case["sun"]))

    def test_badge_mapping_is_complete(self):
        """原因串映射必须覆盖后端全部周末原因（新增原因时本门红）。"""
        self.assertEqual(set(_JS_BADGE),
                         {schedule.DAY_OFF_SUNDAY, schedule.DAY_OFF_SATURDAY, ""})


if __name__ == "__main__":
    unittest.main()
