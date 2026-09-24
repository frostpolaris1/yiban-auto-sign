# -*- coding: utf-8 -*-
"""数据总览页「账号数 / 事件数」双口径聚合的 JS 行为测试。

**为什么需要**：后端 `sign_event_stats` 同时给出两个计数口径 ——
`cnt`（按 phone 去重的**账号数**）与 `row_cnt`（原始**事件行数**）。
同一账号当日可被多个执行体各写一行（跳过发生在领取之前，不产生 claim、不占租约，
故每个 worker 都会各走一遍），按行计数会把同一账号算多次、令报表虚增。
前端因此必须把两类聚合**分开**：分布/日历按账号口径（`cnt`），
趋势/成功率按事件口径（`row_cnt`）。两边混用会让「数字」与「单位」对不上。

本测试把 `normalizeDaily` 抽出来在 node 里真跑（与 `test_schedule_edge_limit_js.py`
同一跑法），钉住两口径各自正确且互不相等，并静态钉住各视图取的是哪一列。
"""
import json
import os
import shutil
import subprocess
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DASH_JS = os.path.join(BASE, "web", "static", "js", "pages", "data_dashboard.js")
NODE = shutil.which("node")

DAY = "2026-09-20"

#: 同一天两条 daily_stats 行：
#: - 账号 A 在 user_cancelled 上被两个执行体各跳一次 → cnt=1（去重）、row_cnt=2；
#: - 账号 B 在 success 上成功一次 → cnt=1、row_cnt=1。
#: 账号总数 2，事件总数 3 —— 两者必须不同。
ROWS = [
    {"day": DAY, "status": "user_cancelled", "cnt": 1, "row_cnt": 2},
    {"day": DAY, "status": "success", "cnt": 1, "row_cnt": 1},
]


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


def _run_normalize(fn_src, rows):
    """在 node 里执行抽出的 `normalizeDaily`，返回它写回的 state。"""
    script = (
        # 只按「成功/失败/其余」三分类打桶，与页面 statusKind 同语义；
        # 抽出的函数依赖它，故在此补一个最小实现，避免连表一起抽。
        "function statusKind(st){ st = String(st || ''); "
        "return st === 'success' ? 'success' : (st === 'failed' ? 'fail' : 'skip'); }\n"
        "var state = {};\n"
        + fn_src
        + "\nvar rows = " + json.dumps(rows) + ";\n"
        + "normalizeDaily(rows);\n"
        + "console.log(JSON.stringify({byStatus: state.byStatus, dailyMap: state.dailyMap}));\n"
    )
    proc = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise AssertionError("node 执行失败：%s" % (proc.stderr or proc.stdout))
    return json.loads(proc.stdout.strip().splitlines()[-1])


@unittest.skipUnless(NODE, "node 不可用：跳过数据总览双口径聚合的 JS 行为测试")
class DashboardStatsCaliberTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(DASH_JS, encoding="utf-8") as fh:
            cls.src = fh.read()
        cls.fn_src = _extract_function(cls.src, "normalizeDaily")
        cls.out = _run_normalize(cls.fn_src, ROWS)

    def test_distribution_counts_dedup_accounts(self):
        """分布口径 = 账号数：同一账号两行只算一次（user_cancelled 计 1，不是 2）。"""
        by = self.out["byStatus"]
        self.assertEqual(by["user_cancelled"], 1, "同账号两行应去重为 1 个账号")
        self.assertEqual(by["success"], 1)
        self.assertEqual(sum(by.values()), 2, "账号总数 = 两个账号")

    def test_event_total_counts_rows(self):
        """事件口径 = 行数：两行 + 一行 = 3（重试/多执行体各写一行都是事实）。"""
        events = self.out["dailyMap"][DAY]["events"]
        self.assertEqual(events["total"], 3, "事件总数应为原始行数 3")
        self.assertEqual(events["skip"], 2, "user_cancelled 归入跳过桶，按行计 2")
        self.assertEqual(events["success"], 1)

    def test_two_calibers_are_not_equal(self):
        """账号数与事件数必须分开存放且不相等（混装正是本轮修正的缺陷）。"""
        accounts = self.out["dailyMap"][DAY]["accounts"]
        events = self.out["dailyMap"][DAY]["events"]
        self.assertEqual(accounts["total"], 2)
        self.assertEqual(events["total"], 3)
        self.assertNotEqual(accounts["total"], events["total"])

    # ---- 静态钉点：各视图取哪一列，防止有人「顺手」换回混用 ----
    def test_dist_view_reads_account_column(self):
        """分布视图的取值源必须是账号口径（byStatus，由 cnt 累加）。"""
        body = _extract_function(self.src, "normalizeDaily")
        self.assertIn("byStatus", body)
        self.assertIn("Number(r.cnt)", body)
        dist = _extract_function(self.src, "renderDist")
        self.assertIn("state.byStatus", dist)
        self.assertNotIn("row_cnt", dist, "分布视图不得按事件行数计数")

    def test_trend_view_reads_event_column(self):
        """趋势视图的堆叠必须取事件口径（events），文案用「次」。"""
        trend = _extract_function(self.src, "renderTrend")
        self.assertIn(".events", trend)
        self.assertIn("次", trend)

    def test_rate_kpi_declares_event_caliber(self):
        """成功率按事件（尝试）口径，标签/tooltip 必须写明。"""
        rate = _extract_function(self.src, "renderRateKpi")
        self.assertIn(".events", rate, "成功率应取事件口径（与「成功率」字面最贴）")
        self.assertIn("按事件", rate, "标签或 tooltip 必须写出「按事件（尝试）」口径")


if __name__ == "__main__":
    unittest.main(verbosity=2)
