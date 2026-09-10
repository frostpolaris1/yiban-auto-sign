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
  · 签到日历里「周末不签到」的日期数字 4 处：最接近禁用态，待 V3-4 日历重做时统一。
"""

import os
import re
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(BASE, "web")

CANONICAL = "text-zinc-500 dark:text-zinc-400"
REVERSED = "text-zinc-400 dark:text-zinc-500"

SCAN_DIRS = (os.path.join(WEB, "templates"), os.path.join(WEB, "static", "js"))
SCAN_EXTS = (".html", ".js")

# base.html 的 body class：bg-zinc-50 dark:bg-zinc-900
BG_LIGHT = "zinc-50"
BG_DARK = "zinc-900"

AA_NORMAL_TEXT = 4.5  # WCAG 2.1 AA 正文阈值


def _load_palette():
    """从 app.css 读取 `--c-zinc-N: R G B` 调色板（保持与实现同源）。"""
    with open(os.path.join(WEB, "static", "css", "app.css"), encoding="utf-8") as fh:
        css = fh.read()
    palette = {}
    for m in re.finditer(r"--c-(zinc-\d+):\s*(\d+)\s+(\d+)\s+(\d+)", css):
        palette[m.group(1)] = tuple(int(m.group(i)) for i in (2, 3, 4))
    return palette


def _rel_luminance(rgb):
    def channel(v):
        c = v / 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(x) for x in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(fg, bg):
    a, b = _rel_luminance(fg), _rel_luminance(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


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
        palette = _load_palette()
        light = _contrast(palette["zinc-500"], palette[BG_LIGHT])
        dark = _contrast(palette["zinc-400"], palette[BG_DARK])
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
        palette = _load_palette()
        light = _contrast(palette["zinc-400"], palette[BG_LIGHT])
        dark = _contrast(palette["zinc-500"], palette[BG_DARK])
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


if __name__ == "__main__":
    unittest.main()
