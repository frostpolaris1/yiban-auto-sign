# -*- coding: utf-8 -*-
"""Web 前端配色类测试共用的 WCAG 助手。

被 `test_web_text_contrast.py`（文字色对）与 `test_web_design_tokens.py`（调色板完整性、
徽标变体配色）共用 —— 避免同一套对比度算术在多个测试文件里各抄一份。

调色板一律从 `web/static/css/app.css` 的 `--c-<族>-<档>: R G B` 读取（与实现同源），
**不硬编码色值**：这样若调色板被改动，相关断言会跟着重新计算，而不是继续对着一组
过期的期望值"通过"。
"""

import os
import re

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(BASE, "web")

# base.html 的 body class：bg-zinc-50 dark:bg-zinc-900
BG_LIGHT = "zinc-50"
BG_DARK = "zinc-900"

AA_NORMAL_TEXT = 4.5  # WCAG 2.1 AA 正文阈值
AA_NON_TEXT = 3.0  # WCAG 2.1 AA 非文字（图形/界面组件）阈值

# 前端用到的色族（Tailwind 的名字）。用于把「-<族>-<档>」从任意 token 里认出来。
COLOR_FAMILIES = (
    "zinc",
    "blue",
    "amber",
    "green",
    "red",
    "orange",
    "yellow",
    "emerald",
    "teal",
    "sky",
    "indigo",
    "violet",
    "purple",
    "pink",
    "rose",
    "slate",
    "gray",
    "neutral",
    "stone",
)


def load_palette():
    """读取 `--c-<族>-<档>: R G B` → {'zinc-500': (113,113,122), ...}。"""
    with open(os.path.join(WEB, "static", "css", "app.css"), encoding="utf-8") as fh:
        css = fh.read()
    palette = {}
    for m in re.finditer(r"--c-([a-z]+-\d+):\s*(\d+)\s+(\d+)\s+(\d+)", css):
        palette[m.group(1)] = tuple(int(m.group(i)) for i in (2, 3, 4))
    return palette


def load_config_shades():
    """读取 tailwind_config.html 里各色族**已声明**的档位 → {'blue': {'200',...}, ...}。

    必要性：只有 app.css 里有 `--c-blue-50` 还不够 —— 若 config 的 `colors.blue`
    里没有 `50` 这一项，Tailwind **根本不会生成** `bg-blue-50` 这个类。
    两侧都齐才算真的可用。
    """
    with open(
        os.path.join(WEB, "templates", "partials", "tailwind_config.html"), encoding="utf-8"
    ) as fh:
        cfg = fh.read()
    declared = {}
    for fam in COLOR_FAMILIES:
        m = re.search(rf"(?<![\w-]){fam}\s*:\s*\{{([^}}]*)\}}", cfg)
        if m:
            declared[fam] = set(re.findall(r"(\d+)\s*:", m.group(1)))
    return declared


def rel_luminance(rgb):
    def channel(v):
        c = v / 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(x) for x in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(fg, bg):
    a, b = rel_luminance(fg), rel_luminance(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def blend(fg, bg, alpha):
    """把带透明度的前景色与背景色做 alpha 混合（Tailwind `bg-x/25` 的语义）。"""
    return tuple(fg[i] * alpha + bg[i] * (1 - alpha) for i in range(3))
