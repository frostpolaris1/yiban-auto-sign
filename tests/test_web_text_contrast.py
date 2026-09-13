# -*- coding: utf-8 -*-
"""回归守卫（2026-09-10）：Web「次要文字」的深浅档位不得写反。

背景（V3-3 实测）：
浅色模式正文底是 zinc-50、暗色是 zinc-900（旧栈 body class；P4 后由 Adminator 的
`--bg-body` 等语义令牌承担），因此本项目
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
    # P11（2026-09-13）：管理端旧档位徽标（.badge.success/.danger/.warning）统一到
    # `.badge--*`（account-table.js / data_dashboard.js），文案与语义不变，仅换档位。
    # 档位令牌与上面各页相同，此处按调用点补对照：改回旧档位或改色值都会报红。
    ("账号页·状态徽标·正常", "badge-ok-fg", "badge-ok-bg"),
    ("账号页·状态徽标·待审核", "badge-warn-fg", "badge-warn-bg"),
    ("账号页·状态徽标·已拒绝/待删除", "badge-bad-fg", "badge-bad-bg"),
    ("总览·健康徽标·运行/可达/时钟同步", "badge-ok-fg", "badge-ok-bg"),
    ("总览·健康徽标·暂停/不可达/检测失败", "badge-bad-fg", "badge-bad-bg"),
    ("总览·健康徽标·时钟小偏差", "badge-warn-fg", "badge-warn-bg"),
)

# P16 图四（2026-09-13）：深色模式下「白字压主色实心面」的对比度修复。
# vendor 深色 --primary 提亮为 #60A5FA（服务主色文字/描边/焦点环），白字压上只有
# 2.54:1。处置走「改背景不改文字色」：新增 --primary-solid / --primary-solid-hover
# （app.css §33，两主题同值深一档蓝），替换 .btn--primary / ::selection /
# .pager__btn.is-active / .check 勾选 / .switch 勾选 / .brand-logo 等白字白图形实心面；
# 无白字的复用面（progress-fill、tab 激活文字、焦点环等）保留浅蓝。
SOLID_SURFACE_TOKENS = ("primary-solid", "primary-solid-hover")
WHITE = (255, 255, 255)
DARK_PRIMARY_KEEP = (0x60, 0xA5, 0xFA)  # --primary 在深色下必须仍是浅蓝（前景场景用）


def _scan_reversed():
    """扫描 web 前端源里的写反形态，返回 [(相对路径, 行号, 行内容)]。"""
    hits = []
    for root in SCAN_DIRS:
        for dirpath, _dirnames, filenames in os.walk(root):
            for name in filenames:
                if not name.endswith(SCAN_EXTS):
                    continue
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
        """反向守卫：正确形态仍未被整体误替换（防一次全局替换把约定悄悄清空）。

        P4（2026-09-13）旧栈退役后，Tailwind 的 `text-zinc-500 dark:text-zinc-400`
        约定只余 `macros/ui.html` 空态宏一处——旧栈（tabs/index/modals/shared-ui）原带
        97 处随文件退役，新 MPA 页面改用 Adminator 语义令牌（`--t-sub`/`--t-muted`），
        不再走这套 Tailwind 色对。故下限从 98 收敛为 1：约定若被整体清掉仍会报红。
        """
        count = _count_occurrences(CANONICAL)
        self.assertGreaterEqual(
            count,
            1,
            f"正确形态 {CANONICAL!r} 已归零，疑似被整体误替换，请复核",
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

    def test_primary_solid_surface_meets_aa(self):
        """P16 图四：承载白字的主色实心面在两个主题下都必须达 AA。

        app.css §33 引入 --primary-solid / --primary-solid-hover 承载白字（白图形）
        的主色表面：改前深色下 .btn--primary 白字压 #60A5FA 仅 2.54:1（hover 压
        #3B82F6 为 3.68:1）；改后两令牌固定为深一档蓝（白字 5.17 / 6.70:1），
        light 下与 vendor --primary/--primary-dark 同值（零影响）。
        同时钉住深色 --primary 保持 #60A5FA 不动——浅蓝仍服务主色文字/描边/焦点环。
        """
        colors = load_theme_colors()
        problems = []
        for mode, label in (("light", "浅色"), ("dark", "暗色")):
            for token in SOLID_SURFACE_TOKENS:
                if token not in colors[mode]:
                    problems.append(f"  {label}：app.css 缺令牌 --{token}")
                    continue
                ratio = contrast(WHITE, colors[mode][token])
                if round(ratio, 2) < AA_NORMAL_TEXT:
                    problems.append(
                        f"  {label}：白字 on --{token}"
                        f" = {ratio:.2f}:1 < AA {AA_NORMAL_TEXT}:1"
                    )
        if problems:
            self.fail("主色实心面（白字）对比度不达标：\n" + "\n".join(problems))

        dark_primary = colors["dark"].get("primary")
        self.assertEqual(
            dark_primary, DARK_PRIMARY_KEEP,
            f"深色 --primary 应保持 {DARK_PRIMARY_KEEP}（浅蓝服务文字色/描边/焦点环），"
            f"实测 {dark_primary}——若确要改主色令牌，请同步复核 §33 的复用面清单",
        )

    def test_dark_primary_solid_usages_in_css(self):
        """守卫本体：白字/白图形压主色的实心面必须走 --primary-solid，不得回流 --primary。"""
        import re

        with open(os.path.join(WEB, "static", "css", "app.css"), encoding="utf-8") as fh:
            css = fh.read()
        # 这些规则带白字/白图形，vendor 原用 --primary，必须被 §33 覆盖为 --primary-solid
        required_overrides = (
            ".btn--primary", "::selection", ".pager__btn.is-active",
            ".check input:checked + .box", ".switch input:checked + .track",
            ".brand-logo",
        )
        missing = [sel for sel in required_overrides
                   if not re.search(re.escape(sel) + r"[^{]*\{[^}]*--primary-solid", css)]
        if missing:
            self.fail("以下白字/白图形主色实心面未使用 --primary-solid（会回流出 2.54:1）:\n  "
                      + "\n  ".join(missing))


if __name__ == "__main__":
    unittest.main()
