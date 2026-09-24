# -*- coding: utf-8 -*-
"""设置页滑块量程与后端夹取口径的 JS 行为对拍（缓冲单边上限）。

标签：F · 前端与界面守卫
覆盖：设置页缓冲滑块上限 `edgeMaxMin(winSec)` 与服务端夹取口径的逐值对拍，以及上限落到 `data-max`、回填消费服务端异常提示的源码形态
对应实现：`web/static/js/components/settings-schedule.js` 的 `edgeMaxMin`；`yiban.window.edge_cap_sec`
关键断言：前端上限（分钟）逐值等于 `edge_cap_sec / 60`——前端宽服务端窄会让用户「保存后数字变小」，反之合法值存不进；窗口未知（0）时退回既有量程 5 分钟而不是把滑块锁成 0；上限确实 `setAttribute("data-max"`，回填时消费 `window_fallback_text`
依赖：⚠ **需要 node 真跑**——`edgeMaxMin` 抽出后交给 node 执行，`shutil.which("node")` 取不到时整类 `skipUnless`；另需能导入 `yiban.window`（纯本地计算，不联网）

**为什么需要**：前端滑块的可选上限与服务端保存时的夹取必须是同一条式子。前端宽、
服务端窄 → 用户点了保存却看到数字被改小（"保存坏了"）；前端窄、服务端宽 → 用户
存不进合法值。两边各写一遍必然漂移，故这里把 JS 里那条式子抽出来在 node 里真跑，
与 `yiban.window.edge_cap_sec` 逐值比对。

覆盖：
- `edgeMaxMin(winSec)` 与 `edge_cap_sec(winSec) / 60` 在多个窗口宽度下逐值一致；
- 窗口未知（0/负）时退回既有量程 5 分钟（不因"还没填窗口"把滑块锁成 0）；
- 源码形态：上限确实写回 `data-max`，且回填时消费服务端的窗口异常提示。
"""
import json
import os
import shutil
import subprocess
import unittest

from yiban import window

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCHEDULE_JS = os.path.join(BASE, "web", "static", "js", "components",
                           "settings-schedule.js")
NODE = shutil.which("node")  # 取不到 ⇒ 本文件的对拍用例整类 skip，不是「没测到」

#: 与 JS 对拍用的窗口宽度（秒）；0 是"窗口未知"的特例，另行断言
WINDOW_SECS = (120, 300, 600, 900, 1500, 1800, 3000)


def _extract_function(src, name):
    """从源码里按花括号配对抽出 `function <name>(...) { ... }` 整段。"""
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


def _run_edge_max(fn_src, window_secs):
    """在 node 里执行抽出的 `edgeMaxMin`，返回各窗口宽度下的上限（分钟）。"""
    script = (
        fn_src
        + "\nconst ws = %s;\n" % json.dumps(list(window_secs))
        + "console.log(JSON.stringify(ws.map(function (w) { return edgeMaxMin(w); })));\n"
    )
    proc = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise AssertionError("node 执行失败：%s" % (proc.stderr or proc.stdout))
    return json.loads(proc.stdout.strip().splitlines()[-1])


@unittest.skipUnless(NODE, "node 不可用：跳过滑块量程的 JS 行为测试")
class EdgeLimitParityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(SCHEDULE_JS, encoding="utf-8") as fh:
            cls.src = fh.read()
        cls.fn_src = _extract_function(cls.src, "edgeMaxMin")

    def test_edge_max_matches_server_cap(self):
        """前端上限（分钟）必须逐值等于服务端 `edge_cap_sec / 60`。"""
        got = _run_edge_max(self.fn_src, WINDOW_SECS)
        want = [window.edge_cap_sec(w) / 60.0 for w in WINDOW_SECS]
        self.assertEqual(got, want, "滑块上限与服务端夹取口径分叉：%r != %r" % (got, want))

    def test_unknown_window_keeps_full_range(self):
        """窗口未知（0）时退回既有量程 5 分钟，不把滑块锁成 0。"""
        got = _run_edge_max(self.fn_src, (0, -1))
        self.assertEqual(got, [5, 5])

    def test_limits_are_written_to_data_max(self):
        """上限确实落到 `data-max`（弹窗数字框/滑杆的量程读的就是它）。"""
        self.assertIn('setAttribute("data-max"', self.src)
        self.assertIn('data-range-field', self.src)

    def test_apply_consumes_window_fallback_text(self):
        """回填时消费服务端的窗口异常提示（否则该异常在设置页看不见）。"""
        self.assertIn("window_fallback_text", self.src)
        self.assertIn("fallbackText", self.src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
