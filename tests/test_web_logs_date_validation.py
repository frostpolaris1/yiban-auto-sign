# -*- coding: utf-8 -*-
"""日志页日期校验的时区安全性（真实行为测试，不是静态扫描）。

## 历史缺陷（实测 Asia/Shanghai，UTC+8）

日志页的 `isValidDate` 曾用
`new Date(s + "T00:00:00").toISOString().slice(0, 10) === s`
做回环校验：日期字符串按**本地时区**解析，`toISOString()` 却转成 UTC。
UTC+8 下本地 2026-09-11 00:00 == UTC 2026-09-10 16:00，切片得 "2026-09-10"，
于是**任何**日期都被判非法——「按日期查看」按钮弹「日期格式不正确」、
`?date=` 分享 URL 完全失效。这类缺陷只在非 UTC 时区暴露，静态审查看不出。

## 本文件怎么测

把 `web/static/js/pages/logs.js` 里的 `isValidDate` 源码抽出来，交给 node 执行；
每次**运行时**改 `process.env.TZ` 后再调用（Node 会即时生效，Windows 上亦可，
已验证 `Etc/GMT-8` → 偏移 -480、`Etc/GMT+5` → +300）。
断言合法日期在任意时区都返回 true、非法日期返回 false、且结果与时区无关。

node 缺失或宿主不认这些时区名时跳过（本套件其余部分都是纯静态检查，
不引入硬性 node 依赖）。
"""

import json
import os
import re
import shutil
import subprocess
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGS_JS = os.path.join(BASE, "web", "static", "js", "pages", "logs.js")
NODE = shutil.which("node")

# TZ 名 → 期望的 getTimezoneOffset()（分钟；东八区为 -480，西五区为 +300）
# 用 POSIX 反转名的 Etc/GMT±N：Etc/GMT-8 即 UTC+8。
TIMEZONES = [("Etc/GMT-8", -480), ("Etc/GMT+5", 300)]

# 合法日期（含闰年 2/29）与非法日期（格式错 / 越界 / 不存在的日历日）
VALID_DATES = ("2026-09-11", "2026-02-28", "2024-02-29", "2026-01-01")
INVALID_DATES = ("2026-02-29", "2026-13-01", "2026-00-10", "2026-09-31",
                 "2026-9-1", "", "2026-09-11 ", "20260911")


def _extract_function(src, name):
    """从源码里按花括号配对抽出 `function <name>(...) { ... }` 整段。

    正则字面量里的 `{4}`/`{2}` 是成对出现，配对计数不受影响。
    """
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
    raise AssertionError("函数 %s 未找到匹配的右花括号" % name)


def _run_in_tz(fn_src, tz):
    """在指定时区里执行抽出函数，返回 (实际偏移, 各日期结果)。"""
    script = (
        "process.env.TZ = %s;\n" % json.dumps(tz)
        + fn_src
        + "\nconst dates = %s;\n" % json.dumps(list(VALID_DATES + INVALID_DATES))
        + "console.log(JSON.stringify({"
        + "off: new Date('2026-09-11T00:00:00').getTimezoneOffset(),"
        + "r: dates.map(function (d) { return isValidDate(d); })}));\n"
    )
    proc = subprocess.run([NODE, "-e", script], capture_output=True, text=True,
                          timeout=30)
    if proc.returncode != 0:
        raise AssertionError("node 执行失败：%s" % (proc.stderr or proc.stdout))
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _strip_line_comments(src):
    """去掉 `//` 行注释（本函数体内的说明注释正以 toISOString 作反例，不能误伤）。"""
    return "\n".join(re.sub(r"//.*$", "", ln) for ln in src.split("\n"))


@unittest.skipUnless(NODE, "node 不可用：跳过日志页日期校验的 JS 行为测试")
class LogsDateValidationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(LOGS_JS, encoding="utf-8") as fh:
            src = fh.read()
        cls.fn_src = _extract_function(src, "isValidDate")

    def test_extracted_function_is_the_timezone_safe_shape(self):
        """抽出的函数不得再用 toISOString 做回环（回归钉点，失败即缺陷复现）。"""
        code = _strip_line_comments(self.fn_src)
        self.assertNotIn(
            "toISOString", code,
            "isValidDate 又用回 UTC 回环校验——UTC+8 下会把所有日期判非法",
        )
        self.assertIn("new Date(y, m - 1, d)", code,
                      "isValidDate 应按本地年月日构造做日历校验")
        self.assertTrue(re.search(r"getFullYear\(\)\s*===\s*y", code),
                        "isValidDate 应逐项比对年月日，拒绝 2026-02-29 这类不存在的日期")

    def test_valid_and_invalid_dates_in_non_utc_timezones(self):
        """合法/非法日期在多个非 UTC 时区下结果一致且正确。"""
        expected = [True] * len(VALID_DATES) + [False] * len(INVALID_DATES)
        honored = 0
        for tz, want_off in TIMEZONES:
            got = _run_in_tz(self.fn_src, tz)
            if got["off"] != want_off:
                # 宿主不认该时区名（ICU 缺 tzdata）：跳过该档，不误报
                continue
            honored += 1
            self.assertEqual(
                got["r"], expected,
                "时区 %s（偏移 %d）下日期校验结果错误：%r" % (tz, want_off, got["r"]),
            )
        if not honored:
            self.skipTest("宿主 ICU 不认 Etc/GMT±N 时区名，无法在本机重放该时区")

    def test_result_is_timezone_independent(self):
        """同一批日期在 UTC+8 与 UTC-5 下结果必须完全相同。"""
        seen = []
        for tz, want_off in TIMEZONES:
            got = _run_in_tz(self.fn_src, tz)
            if got["off"] == want_off:
                seen.append((tz, got["r"]))
        if len(seen) < 2:
            self.skipTest("宿主未同时认下两个时区，无法比对时区无关性")
        self.assertEqual(seen[0][1], seen[1][1],
                         "日期校验结果随进程时区变化：%r" % (seen,))


if __name__ == "__main__":
    unittest.main()
