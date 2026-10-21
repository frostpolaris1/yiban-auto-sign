# -*- coding: utf-8 -*-
"""数据看板「账号终态」口径的 JS 行为测试（6l5w 单口径重锚版）。

标签：F · 前端与界面守卫
覆盖：数据看板的唯一口径 = 账号终态。每个账号每天只有一个最终状态，按日分桶后日内相加
    合法、重试不重复计数。事件（尝试）口径 `daily_stats.row_cnt` **不进入看板**，过程
    数据只进日志页。
对应实现：`frontend/src/dashboard/model.js` 的 `statusKind` / `acceptAccounts` /
    `rateOf`（**真跑**），以及 `trendView` / `distView` / `calendarView` /
    `rateKpiView`（口径静态钉）。
关键断言：
    ① `statusKind` 与页面**同源**（从 model.js 里抽真函数在 node 真跑）；
       词表外的未知码必须落 `unknown` 档而不是静默 skip（否则成功率只抬不降）；
    ② `rateOf` 的分母只含 成功+失败：跳过与未知都不进（真跑断言）；
    ③ 防探针混算的唯一防线——请求路径常量里 `days=30&stage=sign`（静态钉死）；
    ④ 事件聚合函数已删除：模块内不得再出现 `normalizeDaily`，趋势/成功率/日历不得读
       `dailyMap`；历史教训是「953 vs 真值 94」的跨维直加。
依赖：⚠ **需要 node 真跑**——三个函数按花括号配对从源码抽出后交给 node 执行；
    `shutil.which("node")` 取不到时整类 `skipUnless`。不联网
"""
import json
import os
import re
import shutil
import subprocess
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DASH_JS = os.path.join(BASE, "frontend", "src", "dashboard", "model.js")
#: 状态词表的唯一事实源（2026-10-04 收敛）：dashboard 的 STATUS_LABEL 由它的
#: `STATUS_VOCAB[*].short` 派生。本守卫随之重锚——从该对象在 node 里重建
#: STATUS_LABEL（与运行时同一派生式），口径钉点不变。
VOCAB_JS = os.path.join(BASE, "frontend", "src", "lib", "status-vocab.js")
NODE = shutil.which("node")

DAY = "2026-09-20"
DAY2 = "2026-09-21"


#: `accounts_stats`（后端终值，来源＝按日状态文件的
#: `web/services/logs.py::accounts_terminal_summary`）：
#: - total=2：窗口内出现过的账号并集——「签到账号总数」的唯一来源；
#: - by_status：success 桶含 A、B（=2），user_cancelled 桶含 A（=1）——
#:   **桶间有交集**（A 两个桶都进），桶合计 3 ≠ total 2；
#: - by_day：DAY 的日终态 = A 是 user_cancelled、B 是 success。
ACCOUNTS = {
    "total": 2,
    "by_status": {"user_cancelled": 1, "success": 2},
    "by_day": [
        {"day": DAY, "status": "user_cancelled", "accounts": 1},
        {"day": DAY, "status": "success", "accounts": 1},
        {"day": DAY2, "status": "success", "accounts": 1},
    ],
}


def _extract_function(src, name):
    """从源码里按花括号配对抽出 `function <name>(...) { ... }` 整段。

    测试专用、够用即可——刻意不做通用 JS 解析。已知脆弱（改动被测 JS 时若命中即抛错、
    不会静默抓错段，故失效方向是红不是假绿）：
    - 定位靠字面量 `"function <name>("：目标若被格式化（`function name (`、箭头函数、
      对象属性式 `name: function(`）或该字面量在更早的注释/字符串里先出现过，会 ValueError
      或抓错段——被抽函数须保持这一书写形式；
    - 只数 `{`/`}`、不数 `()`/`[]`：字符串/模板串/注释里的裸花括号被计入，会提前或延后闭合；
    - 参数默认值带对象解构（`function f(a, {b}=…)`）时，首个 `{` 落在参数表内、配对起点即偏。
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


def _extract_object(src, name):
    """抽 `var <name> = { ... };` 的对象字面量（statusKind 的依赖表，同源用）。

    与被抽函数同一脆弱面（字面量定位 + 只数花括号），失效方向同样是红不是假绿。
    """
    marker = "var " + name + " = {"
    i = src.index(marker) + len(marker) - 1
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[i:j + 1]
    raise AssertionError("对象 %s 未找到匹配的右花括号" % name)


#: statusKind 的依赖词表——**从唯一事实源抽**，测试不再自带重写（旧版 `already`→skip
#: 的不等价重写正是登记点名的缺口之一）。
def _status_vocab_js(src):
    with open(VOCAB_JS, encoding="utf-8") as fh:
        vocab_src = fh.read()
    return (
        "var STATUS_VOCAB = " + _extract_object(vocab_src, "STATUS_VOCAB") + ";\n"
        "var STATUS_LABEL = {};\n"
        "for (var _vk in STATUS_VOCAB) STATUS_LABEL[_vk] = STATUS_VOCAB[_vk].short;\n"
        "var SUCCESS_ST = " + _extract_object(src, "SUCCESS_ST") + ";\n"
        "var FAIL_ST = " + _extract_object(src, "FAIL_ST") + ";\n"
        + _extract_function(src, "statusKind") + "\n"
    )


def _run_page(src, tail):
    """在 node 里跑模块真函数：词表 + statusKind + 聚合函数就位后执行 `tail`。

    返回末行 JSON。`tail` 是拼在函数定义之后的调用脚本。口径函数是纯函数（返回结果，
    不改全局长态），故 harness 不再注入 `state`。
    """
    script = (
        _status_vocab_js(src)
        + _extract_function(src, "acceptAccounts") + "\n"
        + _extract_function(src, "rateOf") + "\n"
        + tail
    )
    proc = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise AssertionError("node 执行失败：%s" % (proc.stderr or proc.stdout))
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _run_accounts(src):
    """跑账号聚合函数，回读窗口总数、按状态桶与按日终态。"""
    tail = (
        "var ac = acceptAccounts(" + json.dumps(ACCOUNTS) + ");\n"
        "console.log(JSON.stringify({accountsTotal: ac.total, byStatus: ac.byStatus,"
        " dayFinal: ac.dayFinal}));\n"
    )
    return _run_page(src, tail)


@unittest.skipUnless(NODE, "node 不可用：跳过数据看板账号终态口径的 JS 行为测试")
class DashboardStatsCaliberTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(DASH_JS, encoding="utf-8") as fh:
            cls.src = fh.read()
        cls.out = _run_accounts(cls.src)

    def test_distribution_counts_dedup_accounts(self):
        """分布口径 = 后端窗口去重终值：总数取 total（2），不拿有交集的桶合计（3）冒充；
        日历格 = 日终态分桶——每账号当日恰落一桶，日内相加=当下去重账号数。"""
        self.assertEqual(self.out["accountsTotal"], 2, "总数 = 后端去重终值")
        self.assertEqual(self.out["byStatus"], ACCOUNTS["by_status"], "分状态桶原样透传")
        self.assertGreater(sum(self.out["byStatus"].values()), self.out["accountsTotal"],
                           "桶间有交集：桶合计大于总数，相加当总数必虚增（MF-55 的错法）")
        day1 = self.out["dayFinal"][DAY]
        self.assertEqual(day1["total"], 2, "DAY 涉及 2 个账号（A 终态 skip、B 终态 success）")
        self.assertEqual((day1["success"], day1["skip"]), (1, 1),
                         "每账号当日恰落一桶，不再跨状态双计")
        self.assertEqual(self.out["dayFinal"][DAY2]["success"], 1, "DAY2 只有 A 成功")

    def test_account_aggregation_ignores_event_counters(self):
        """账号聚合不读事件计数：给每行塞 cnt=999 后输出不变。"""
        mutated = json.loads(json.dumps(ACCOUNTS))
        for r in mutated["by_day"]:
            r["cnt"] = 999
            r["row_cnt"] = 999
        tail = ("console.log(JSON.stringify(acceptAccounts(" + json.dumps(mutated) + ").dayFinal));\n")
        self.assertEqual(_run_page(self.src, tail), self.out["dayFinal"],
                         "acceptAccounts 只看账号数，不得被事件计数影响")

    # ---- 静态钉点：单一口径后，事件聚合与旧取数源必须绝迹 ----
    def test_event_aggregation_is_gone(self):
        """模块里不得再出现事件聚合函数，也不得保留 dailyMap 取数。"""
        self.assertNotIn("normalizeDaily", self.src, "事件聚合函数已随单口径裁决删除")
        self.assertNotIn("state.dailyMap", self.src, "看板不得再读事件桶")
        for name in ("trendView", "rateKpiView", "calendarView"):
            self.assertNotIn("dailyMap", _extract_function(self.src, name),
                             "%s 只读账号终态 dayFinal" % name)

    def test_dist_view_reads_backend_final_value(self):
        """分布与日历的取值源必须是后端终值（statusAccounts/dayFinal）。"""
        dist = _extract_function(self.src, "distView")
        self.assertIn("state.statusAccounts", dist)
        self.assertIn("state.accountsTotal", dist, "「签到账号总数」读后端窗口去重终值")
        cal = _extract_function(self.src, "calendarView")
        self.assertIn("state.dayFinal", cal, "日历格读日终态分桶（染色不再 fail 优先）")

    def test_trend_view_reads_day_final(self):
        """趋势视图的堆叠必须取日终态账号桶，文案用「账号」。"""
        trend = _extract_function(self.src, "trendView")
        self.assertIn("state.dayFinal[day]", trend)
        self.assertIn("账号", trend)

    def test_rate_kpi_declares_account_caliber(self):
        """成功率按账号当日最终状态口径，标签/tooltip 必须写明。"""
        rate = _extract_function(self.src, "rateKpiView")
        self.assertIn("state.dayFinal[today]", rate, "成功率取账号日终态桶")
        self.assertIn("按账号", rate, "标签或 tooltip 必须写出账号口径")

    # ======== 聚焦钉（探针隔离 + 状态枚举穷举） ========
    def test_probe_exclusion_pinned_in_request_url(self):
        """防探针混算的唯一防线 = 请求路径常量里 `days=30&stage=sign`，必须钉死。

        sign_events 表混载签到与探针（后者同样写 success/failed），不带 stage 过滤
        探针就会污染统计；这条防线只剩常量字面量，改一个字符测试必须红。
        """
        m = re.search(r'SIGN_EVENTS_PATH\s*=\s*"([^"]+)"', self.src)
        self.assertIsNotNone(m, "model.js 缺少 SIGN_EVENTS_PATH 常量（请求路径唯一定义处）")
        self.assertEqual(m.group(1), "/api/admin/sign-events?days=30&stage=sign",
                         "数据看板的签到请求必须显式带 stage=sign（探针隔离的唯一防线）")

    def test_rate_denominator_excludes_skip_and_unknown(self):
        """`rateOf` 分母只含 成功+失败；`statusKind` 穷举且未知码落 unknown 不静默落 skip。

        statusKind/rateOf 都是模块真函数（同源抽出真跑）——成功率「只抬不降」的
        默认分支就在这里钉死：未知码进不了分母也进不了分子，但必须**可见**。
        """
        tail = (
            "var b = { success: 8, fail: 2, skip: 5, unknown: 1 };\n"
            "var empty = { success: 0, fail: 0, skip: 9, unknown: 3 };\n"
            "console.log(JSON.stringify({\n"
            "  rate: rateOf(b), emptyRate: rateOf(empty),\n"
            "  already: statusKind('already'), failed: statusKind('failed'),\n"
            "  knownSkip: statusKind('skipped_window'), newCode: statusKind('status_from_2035')\n"
            "}));\n"
        )
        out = _run_page(self.src, tail)
        self.assertEqual(out["rate"], 80.0, "8 ÷ (8+2)：跳过与未知都不得进分母")
        self.assertIsNone(out["emptyRate"], "无了结尝试时不出数，而不是拿跳过凑分母")
        self.assertEqual(out["already"], "success")
        self.assertEqual(out["failed"], "fail")
        self.assertEqual(out["knownSkip"], "skip")
        self.assertEqual(out["newCode"], "unknown",
                         "未知状态码必须落 unknown 档（显式可见），不许静默落 skip 抬成功率")


if __name__ == "__main__":
    unittest.main(verbosity=2)
