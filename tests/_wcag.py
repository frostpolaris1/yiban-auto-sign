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

# 页面正文底色（浅/暗）：Adminator 外壳下由语义令牌承担，此处沿用调色板名做 WCAG 底色基。
# 旧栈 base.html 的 body class 已随 P4 退役。
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


def load_theme_colors():
    """读取主题色令牌 → {'light': {...}, 'dark': {...}}（RGB 元组）。

    两个来源，按同名覆盖合并：
      1. 设计系统 `vendor/adminator/adminator.css` 的 `:root[data-theme=light|dark]`
         —— `--t-base/--t-muted/--bg-body/--bg-card/--bg-muted` 等语义令牌的实际
         声明处（app.css 只补了 `--t-sub` 等少数项目令牌，不重复声明这些）。
      2. 项目 `app.css` 的 `:root`（浅色）/ `html[data-theme="dark"]`（深色）
         —— `--cal-*` / `--state-*` / `--slot-*` / `--t-sub` 等十六进制令牌。

    用途：Adminator 换壳后新增的组件直接用这些语义令牌表达前景/背景色，
    对比度用例按「文字令牌 on 底色令牌」实测 AA；令牌缺失或色值被改到不达标都会报红。
    app.css 是手写非压缩 CSS（规则无嵌套），`选择器 { ... }` 逐块解析安全；
    adminator.css 为压缩产物，但根令牌块以 `}` 闭合、内部无花括号，同样可安全截取。
    """
    colors = {"light": {}, "dark": {}}

    adminator = os.path.join(WEB, "static", "vendor", "adminator", "adminator.css")
    if os.path.isfile(adminator):
        with open(adminator, encoding="utf-8") as fh:
            vendor_css = fh.read()
        for m in re.finditer(r':root\[data-theme=(light|dark)\]\s*\{([^}]*)\}', vendor_css):
            for name, value in re.findall(r"--([\w-]+):\s*(#[0-9A-Fa-f]{6})\b", m.group(2)):
                colors[m.group(1)][name] = _hex_to_rgb(value)

    with open(os.path.join(WEB, "static", "css", "app.css"), encoding="utf-8") as fh:
        css = fh.read()
    blocks = re.findall(r'(:root|html\[data-theme="dark"\])\s*\{([^}]*)\}', css)
    for selector, body in blocks:
        mode = "dark" if "dark" in selector else "light"
        for name, value in re.findall(r"--([\w-]+):\s*(#[0-9A-Fa-f]{6})\b", body):
            colors[mode][name] = _hex_to_rgb(value)
    return colors


def _hex_to_rgb(value):
    value = value.lstrip("#")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


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
