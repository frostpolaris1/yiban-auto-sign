# -*- coding: utf-8 -*-
"""时间字段 `norm` 的范围校验（形状 **与** 00:00–23:59 同一处）。

标签：F · 前端与界面守卫
覆盖：`web/static/js/components/time-field.js` 的 `norm` 在 node 里真跑——越界值必须回退
对应实现：`norm(v, fallback)`；消费点 `openSingle` / `openPair` / `set` / `apply`
关键断言：`"25:00"` / `"23:60"` / `"24:00"` 必须回退到 fallback（而不是原样返回）
依赖：⚠ **需要 node 真跑**——`norm` 抽出后交给 node 执行，`shutil.which("node")` 取不到时整类 skip

**为什么需要**：`norm` 原先只查 `^\\d{2}:\\d{2}$` 形状。越界值（如 `25:00`）于是
"合法"通过：触发器文案与隐藏 input 都显示/落盘 `25:00`，而滚轮 `indexOf` 与
服务端 `parse_hhmm` 各自夹到 `23:00`——同一个字段的显示值、落盘值、生效值三段不等。
范围校验必须与形状校验收在同一处，否则三段各自为政。
"""
import json
import os
import shutil
import subprocess
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TIME_FIELD_JS = os.path.join(BASE, "web", "static", "js", "components", "time-field.js")
NODE = shutil.which("node")


def _extract_function(src, name):
    """按花括号配对抽出 `function <name>(...) { ... }` 整段（测试专用，够用即可）。"""
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


def _run_norm(pairs):
    """在 node 里对每对 (输入, fallback) 调 `norm`，返回各结果。"""
    with open(TIME_FIELD_JS, encoding="utf-8") as fh:
        fn_src = _extract_function(fh.read(), "norm")
    script = (
        fn_src
        + "\nconst pairs = %s;\n" % json.dumps(pairs)
        + "console.log(JSON.stringify(pairs.map(function (p) { return norm(p[0], p[1]); })));\n"
    )
    proc = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise AssertionError("node 执行失败：%s" % (proc.stderr or proc.stdout))
    return json.loads(proc.stdout.strip().splitlines()[-1])


@unittest.skipUnless(NODE, "node 不可用：跳过时间字段范围校验的 JS 行为测试")
class TimeFieldNormRangeTest(unittest.TestCase):
    def test_out_of_range_falls_back(self):
        """超 24 小时制的值必须回退，不得原样返回（否则显示/落盘/生效三段不等）。"""
        got = _run_norm([
            ("25:00", "06:30"),
            ("24:00", "06:30"),
            ("23:60", "06:30"),
            ("99:99", "06:30"),
        ])
        self.assertEqual(got, ["06:30"] * 4)

    def test_valid_shape_and_values_pass(self):
        """合法值原样返回；缺位/非数字等形状不符仍回退。"""
        got = _run_norm([
            ("23:59", "x"),
            ("00:00", "x"),
            ("06:30", "x"),
            ("6:30", "x"),
            ("25:0", "x"),
            ("abc", "x"),
            ("", "x"),
            (None, "x"),
        ])
        self.assertEqual(got, ["23:59", "00:00", "06:30", "x", "x", "x", "x", "x"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
