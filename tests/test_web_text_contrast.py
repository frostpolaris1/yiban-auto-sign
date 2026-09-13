# -*- coding: utf-8 -*-
"""回归守卫（2026-09-10）：Web「次要文字」的深浅档位不得写反。

背景（V3-3 实测）：
浅色模式正文底是 zinc-50、暗色是 zinc-900（见 base.html 的 body class），因此本项目
对「次要文字」（表单提示、页面说明、空态文案等**用户需要读**的文字）的既有约定是：

    text-zinc-500 dark:text-zinc-400        ← 浅色用较深档、暗色用较浅档

实测 WCAG 对比度（调色板取自 web/static/css/app.css，见下面的 test_…premise）：

    zinc-500 on zinc-50 = 4.64:1      zinc-400 on zinc-900 = 6.65:1   → AA 通过
    zinc-400 on zinc-50 = 2.48:1      zinc-500 on zinc-900 = 3.54:1   → AA 不通过

但仓库里曾同时存在**写反**的 `text-zinc-400 dark:text-zinc-500`：它在**两个模式下
都不达标**。该形态属复制漂移而非有意，证据有三：
  ① 数量对比：修复前正确形态 157 处 vs 写反 67 处（约 5:1）；
  ② **同一文件内同一角色混用**：tabs/logs.html 的页面说明用写反形态，而同页
     L16/L24 的说明用正确形态；user.html 的「日历加载失败，请稍后重试」用写反形态，
     而 app.js 里同一句话用正确形态（该文案两份日历各有一份）；
  ③ 少数派恰是**不达标**的那个（2.48:1），多数派是达标的（4.64:1）。

本测试钉住两件事：
1. 上述对比度关系**用可执行方式证明**——若将来调色板被改动导致正确形态不再达标，
   本测试先失败，而不是让"照这个换就对了"的规则悄悄失效；
2. web/templates 与 web/static/js 下不再出现写反形态。

**注意：不要把这个规则推广成「凡 浅色档位 < 暗色档位 皆错」——那是错的判据。**
其反面（浅档配暗档，如 `text-zinc-300 dark:text-zinc-600`）对**弱化内容**是正确的：
浅色模式下贴近白底、暗色模式下贴近深底，两端同样弱。仓库里这类用法共 8 处，
都属有意弱化或 WCAG 豁免，**刻意不在扫描范围内**：
  · `cursor-not-allowed` 的禁用按钮 2 处：WCAG 1.4.3 明确豁免非活动控件；
  · login 页页脚的两个装饰性「·」分隔符：装饰内容，且两端对称弱化；
  · 签到日历里「周末不签到」的日期数字 4 处 —— **V3-4 已处理**：实测 1.42:1（浅）根本读不出日期，
    而该格仍可点（点了提示「周日无需签到」），属**有信息**的格子、不是 WCAG 1.4.3 豁免的
    非活动控件；故其配色已改为「底色表达」并纳入下方日历配色用例。

另外，本文件末尾还钉住**签到日历日期格**（V3-4 引入、2026-09-12 抽成共享实现）与
用户页状态行/时段按钮的配色：日历用「底色」表达状态（✅ 绿底 / ❌ 红底 / 周末停签 中性底 /
今天 inset ring），故其对比度是「文字 on 格底」而非「文字 on 页面底」。
这些颜色已全部收敛为 app.css 的十六进制令牌（`--cal-*` / `--state-*` / `--slot-*`），
浅色在 `:root`、深色在 `html[data-theme="dark"]`，本文件按主题解析后逐个实数。
"""

import os
import unittest

from _wcag import (
    AA_NORMAL_TEXT,
    BASE,
    BG_DARK,
    BG_LIGHT,
    WEB,
    contrast,
    load_palette,
    load_theme_colors,
)

CANONICAL = "text-zinc-500 dark:text-zinc-400"
REVERSED = "text-zinc-400 dark:text-zinc-500"

SCAN_DIRS = (os.path.join(WEB, "templates"), os.path.join(WEB, "static", "js"))
SCAN_EXTS = (".html", ".js")

# ---- 签到日历 / 状态行 / 时段按钮（V3-4 起）：状态由「底色」表达 ----
# 2026-09-12 起日历抽成共享实现（static/js/calendar.js），颜色收敛为 app.css 的
# 十六进制令牌；本组用例按「文字 on 底色」实测，浅/深两套分别取令牌值。
CALENDAR_SRC = os.path.join(WEB, "static", "js", "calendar.js")

# 共享实现里必须存在的状态类名（片段被改名/删除即报"请同步本测试"）
CALENDAR_CLASSES = ("sc-cell--ok", "sc-cell--bad", "sc-cell--off", "sc-cell--today")

# (说明, 文字令牌, 底色令牌) —— 两者都在 :root / html[data-theme=dark] 里定义
CONTRAST_PAIRS = (
    ("日历·已签到", "cal-ok-fg", "cal-ok-bg"),
    ("日历·签到失败", "cal-bad-fg", "cal-bad-bg"),
    ("日历·周末停签", "cal-off-fg", "cal-off-bg"),
    ("日历·无记录格（落在卡片底）", "cal-cell-fg", "cal-cell-bg"),
    ("状态行·成功（落在卡片底）", "state-ok-fg", "cal-cell-bg"),
    ("状态行·警示（落在卡片底）", "state-warn-fg", "cal-cell-bg"),
    ("状态行·失败（落在卡片底）", "state-bad-fg", "cal-cell-bg"),
    ("时段·满员", "slot-full-fg", "slot-full-bg"),
    ("时段·部分裁剪", "slot-partial-fg", "slot-partial-bg"),
    # 账号管理页（/accounts）新引入的文字色对：表头/次要单元格、搜索 placeholder、
    # 批量条、页头副标题、计数强调。全部为既有语义令牌（--t-* / --bg-*），
    # 改令牌值或改这几条规则用色都会在这里被重新实测。
    ("账号页·表头与次要单元格", "t-muted", "bg-card"),
    ("账号页·搜索 placeholder", "t-muted", "bg-card"),
    ("账号页·批量条文字", "t-sub", "bg-muted"),
    ("账号页·批量条计数强调", "t-base", "bg-muted"),
    ("账号页·页头副标题", "t-sub", "bg-body"),
    # 状态徽标语义色调档位（`.badge--ok/bad/warn/info/muted`，令牌定义在 app.css 19.6
    # 通用小件，浅/深两套独立取值）；日志页事件时间线在用。改色值或令牌名会被重新实测。
    ("日志页·事件徽标·成功", "badge-ok-fg", "badge-ok-bg"),
    ("日志页·事件徽标·失败", "badge-bad-fg", "badge-bad-bg"),
    ("日志页·事件徽标·警示", "badge-warn-fg", "badge-warn-bg"),
    ("日志页·事件徽标·信息", "badge-info-fg", "badge-info-bg"),
    ("日志页·事件徽标·中性", "badge-muted-fg", "badge-muted-bg"),
    # 用户管理页（/users）新引入的文字色对：表头 / 计数 / 时间 / 参与者提示（落在卡片底）、
    # 批量条文字与计数（落在 inset 底）、冷却中/待清除状态徽标。均为既有语义令牌，
    # 改令牌值或改这几条规则用色都会被这里重新实测。
    ("用户管理页·表头与次要单元格", "t-muted", "bg-card"),
    ("用户管理页·主管理员行次要文字", "t-sub", "bg-muted"),
    ("用户管理页·批量条文字", "t-sub", "bg-muted"),
    ("用户管理页·批量条计数强调", "t-base", "bg-muted"),
    ("用户管理页·状态徽标·冷却中", "badge-warn-fg", "badge-warn-bg"),
    ("用户管理页·状态徽标·待清除", "badge-bad-fg", "badge-bad-bg"),
    # 系统设置页（/settings）新引入的文字色对：信息浮层文字（落在卡片底）、危险区
    # 警示文字（落在卡片底）、权限/就地提示（落在 inset 底）、调度容量警示（软底）。
    # 均为既有语义令牌，改令牌值或改这几条规则用色都会被这里重新实测。
    ("设置页·信息浮层文字", "t-base", "bg-card"),
    ("设置页·危险区警示文字", "state-bad-fg", "bg-card"),
    ("设置页·权限与提示文字", "t-sub", "bg-muted"),
    ("设置页·调度容量警示", "badge-warn-fg", "badge-warn-bg"),
    # 本批对比度修复新增：容量标题/字段帮助落在 inset 底（--t-muted 仅 4.34:1 → --t-sub）、
    # 超限容量标题落在软红底（→ --badge-bad-fg）、全站表单错误文字覆盖 vendor --danger
    # （白卡上仅 3.76:1 → --state-bad-fg）。改令牌值或把规则改回旧色都会在这里报红。
    ("设置页·容量标题", "t-sub", "bg-muted"),
    ("设置页·字段帮助文字", "t-sub", "bg-muted"),
    ("设置页·容量超限标题", "badge-bad-fg", "badge-bad-bg"),
    ("全站·表单错误文字", "state-bad-fg", "bg-card"),
)


def _scan_reversed():
    """扫描 web 前端源里的写反形态，返回 [(相对路径, 行号, 行内容)]。"""
    hits = []
    for root in SCAN_DIRS:
        for dirpath, _dirnames, filenames in os.walk(root):
            for name in filenames:
                if not name.endswith(SCAN_EXTS):
                    continue
                if name == "index.html" or "tabs" in dirpath.split(os.sep):
                    continue   # 退役旧栈（无路由渲染）：不给死文件套活页规则
                path = os.path.join(dirpath, name)
                with open(path, encoding="utf-8") as fh:
                    for lineno, line in enumerate(fh, 1):
                        if REVERSED in line:
                            hits.append(
                                (os.path.relpath(path, BASE), lineno, line.strip()[:160])
                            )
    return hits


def _count_occurrences(token):
    total = 0
    for root in SCAN_DIRS:
        for dirpath, _dirnames, filenames in os.walk(root):
            for name in filenames:
                if not name.endswith(SCAN_EXTS):
                    continue
                with open(os.path.join(dirpath, name), encoding="utf-8") as fh:
                    total += fh.read().count(token)
    return total


class WebTextContrastTest(unittest.TestCase):
    def test_premise_canonical_pair_meets_aa_in_both_modes(self):
        """前提 1：正确形态（zinc-500 / dark:zinc-400）在两个模式下都达 AA。"""
        palette = load_palette()
        light = contrast(palette["zinc-500"], palette[BG_LIGHT])
        dark = contrast(palette["zinc-400"], palette[BG_DARK])
        self.assertGreaterEqual(
            round(light, 2),
            AA_NORMAL_TEXT,
            f"正确形态浅色模式对比度 {light:.2f}:1 低于 AA {AA_NORMAL_TEXT}:1",
        )
        self.assertGreaterEqual(
            round(dark, 2),
            AA_NORMAL_TEXT,
            f"正确形态暗色模式对比度 {dark:.2f}:1 低于 AA {AA_NORMAL_TEXT}:1",
        )

    def test_premise_reversed_pair_fails_aa_in_both_modes(self):
        """前提 2：写反形态在两个模式下**都**不达 AA —— 这使它不可能是有意为之。"""
        palette = load_palette()
        light = contrast(palette["zinc-400"], palette[BG_LIGHT])
        dark = contrast(palette["zinc-500"], palette[BG_DARK])
        self.assertLess(
            round(light, 2),
            AA_NORMAL_TEXT,
            f"写反形态浅色模式对比度 {light:.2f}:1 竟达 AA，本守卫的前提不成立，请复核",
        )
        self.assertLess(
            round(dark, 2),
            AA_NORMAL_TEXT,
            f"写反形态暗色模式对比度 {dark:.2f}:1 竟达 AA，本守卫的前提不成立，请复核",
        )

    def test_no_reversed_muted_text_pair_in_web_sources(self):
        """守卫本体：web/templates 与 web/static/js 里不得再有写反形态。"""
        hits = _scan_reversed()
        if hits:
            detail = "\n".join(f"  {path}:{ln}  {text}" for path, ln, text in hits)
            self.fail(
                f"发现 {len(hits)} 处写反的次要文字色对 {REVERSED!r}"
                f"（应为 {CANONICAL!r}，两个模式下都不达 AA）：\n{detail}"
            )

    def test_canonical_pair_still_in_use(self):
        """反向守卫：正确形态仍在被使用（防一次误替换把约定整体改掉）。"""
        count = _count_occurrences(CANONICAL)
        self.assertGreaterEqual(
            count,
            100,
            f"正确形态 {CANONICAL!r} 只剩 {count} 处，疑似被整体误替换，请复核",
        )

    def test_calendar_and_state_colors_meet_aa(self):
        """签到日历 / 状态行 / 时段按钮的配色：既要在源码里就位，也要在两个模式下达 AA。

        这一步同时钉住两件事：① 日历确实按「底色表达状态」实现（状态类名必须存在，
        被改掉本用例即报"请同步本测试"）；② app.css 的语义色令牌按「文字 on 底色」
        实测达标 —— 令牌缺失或色值被改到不达标都会报红。
        """
        with open(CALENDAR_SRC, encoding="utf-8") as fh:
            src = fh.read()
        colors = load_theme_colors()
        problems = []
        for cls in CALENDAR_CLASSES:
            if cls not in src:
                problems.append(f"  共享日历实现里找不到状态类名 {cls!r} —— 改名了？请同步本测试")
        if problems:
            self.fail("签到日历的实现契约变了：\n" + "\n".join(problems))

        problems = []
        for mode, label in (("light", "浅色"), ("dark", "暗色")):
            table = colors[mode]
            for name, fg_name, bg_name in CONTRAST_PAIRS:
                missing = [t for t in (fg_name, bg_name) if t not in table]
                if missing:
                    problems.append(f"  {name} / {label}：app.css 里缺令牌 {missing}")
                    continue
                ratio = contrast(table[fg_name], table[bg_name])
                if round(ratio, 2) < AA_NORMAL_TEXT:
                    problems.append(
                        f"  {name} / {label}：{fg_name} on {bg_name}"
                        f" = {ratio:.2f}:1 < AA {AA_NORMAL_TEXT}:1"
                    )
        if problems:
            self.fail("日历 / 状态行 / 时段配色不达标：\n" + "\n".join(problems))


if __name__ == "__main__":
    unittest.main()
