# -*- coding: utf-8 -*-
"""回归守卫（2026-09-10，V3-5）：设计令牌的两类"静默失败"。

## 一、调色板完整性：用了但没定义的档位 → 属性**静默丢失**

本项目的 Tailwind 用自定义色板：`tailwind_config.html` 声明
`blue: {200:'rgb(var(--c-blue-200)/<alpha-value>)', …}`，`app.css` 给出
`--c-blue-200:191 219 254`。**两者缺一，类就不生效**：
  · 只在 config 里声明、调色板没有 → 变量未定义 → 计算值无效 → 属性丢失；
  · 只在调色板里有、config 没声明 → **Tailwind 根本不生成这个类**。
两者都不报错、不警告 —— 页面上只是"某处少了个底色/边框"，极难发现。

V3-5 实测到的三例（都因此静默失效）：
  · `bg-blue-50`   ×3 —— 管理员角色徽标 → **底色为空**；
  · `hover:bg-green-100` ×2、`hover:bg-red-100` ×2 —— 日历格 hover 无效果
    （blue 族从 200 起、green/red 从 200 起，都没有 50/100）。
已按 Tailwind 官方标准值补齐，并由本文件的两个用例钉住"再也不能缺"。

## 二、状态徽标必须是**单一事实源**（不得再出现内联写法）

徽标这段 class 串此前被复制 **14 处、9 种写法**，伴随四项缺陷（`whitespace-nowrap`
7 有 7 无；error 用 `text-red-600` = 4.41:1 不达 AA；neutral 用 `text-zinc-500` on
`bg-zinc-100` = 4.40:1 不达 AA；info 用不存在的 `bg-blue-50`）。V3-5 收敛为组件层的
`.yb-badge` + 5 个语义变体，本文件钉住：① 内联写法不得复活；② 变体配色在两个模式下
都达 AA（对比度由 `_wcag` 从调色板实时算出）。
"""

import os
import re
import unittest

from _wcag import (
    AA_NON_TEXT,
    AA_NORMAL_TEXT,
    BG_DARK,
    BG_LIGHT,
    COLOR_FAMILIES,
    WEB,
    blend,
    contrast,
    load_config_shades,
    load_palette,
)

SCAN_DIRS = (os.path.join(WEB, "templates"), os.path.join(WEB, "static", "js"))
SCAN_EXTS = (".html", ".js")

COMPONENT_LAYER = os.path.join(WEB, "templates", "partials", "component_layer.html")

# 徽标语义变体 → 该变体代表的含义（供失败信息可读）
BADGE_VARIANTS = ("neutral", "info", "success", "warning", "error")

# 任意「工具-色族-档位」（允许 dark:/hover: 等前缀）
_UTIL_RE = re.compile(
    r"(?<![\w.-])(?:bg|text|border|ring|from|to|via|decoration|outline|"
    r"accent|caret|fill|stroke|divide|placeholder)-([a-z]+-\d{2,3})"
)
_SHADE_RE = re.compile(r"^([a-z]+)-(50|100|200|300|400|500|600|700|800|900|950)$")

# 内联徽标写法（V3-5 起禁止）
_INLINE_BADGE_RE = re.compile(r"inline-flex items-center rounded-full bg-[a-z]+-\d{2,3}")


def _scan_frontend_sources():
    """遍历 web 前端源，yield (相对路径, 行号, 行内容)。"""
    for root in SCAN_DIRS:
        for dirpath, _dirnames, filenames in os.walk(root):
            for name in filenames:
                if not name.endswith(SCAN_EXTS):
                    continue
                path = os.path.join(dirpath, name)
                rel = os.path.relpath(path, WEB)
                with open(path, encoding="utf-8") as fh:
                    for lineno, line in enumerate(fh, 1):
                        yield rel, lineno, line


def _used_shades():
    """前端用到的 `<色族>-<档位>` 集合（含 dark:/hover: 前缀的用法）。"""
    used = {}
    for rel, lineno, line in _scan_frontend_sources():
        for m in _UTIL_RE.finditer(line):
            token = m.group(1)
            mm = _SHADE_RE.match(token)
            if mm and mm.group(1) in COLOR_FAMILIES:
                used.setdefault(token, []).append(f"{rel}:{lineno}")
    return used


class PaletteCompletenessTest(unittest.TestCase):
    def test_every_used_shade_is_defined_in_palette(self):
        """用到的每个档位都必须在 app.css 里有 `--c-<族>-<档>`（否则计算值无效）。"""
        palette = load_palette()
        missing = {k: v for k, v in _used_shades().items() if k not in palette}
        if missing:
            detail = "\n".join(
                f"  {k}  出现在 {len(v)} 处，例如 {v[0]}" for k, v in sorted(missing.items())
            )
            self.fail(
                "以下档位被前端使用，但 app.css 的调色板里没有定义 ——"
                "变量未定义会让该属性**静默丢失**：\n" + detail
            )

    def test_every_used_shade_is_declared_in_tailwind_config(self):
        """用到的每个档位也必须在 tailwind_config 的 colors 里声明（否则类不会被生成）。"""
        declared = load_config_shades()
        missing = []
        for token, where in sorted(_used_shades().items()):
            fam, shade = token.rsplit("-", 1)
            if fam not in declared:
                missing.append(f"  {token}  色族 {fam!r} 未在 colors 里声明（{where[0]}）")
            elif shade not in declared[fam]:
                missing.append(
                    f"  {token}  {fam} 已声明但缺 {shade} 档"
                    f"（现有 {sorted(declared[fam], key=int)}，见 {where[0]}）"
                )
        if missing:
            self.fail("以下档位未被 tailwind_config 声明 —— Tailwind 不会生成对应类：\n" + "\n".join(missing))


class BadgeContractTest(unittest.TestCase):
    def test_no_inline_badge_markup(self):
        """徽标必须走 .yb-badge 系列类，不得再出现内联的整段写法。"""
        hits = [
            f"  {rel}:{lineno}  {line.strip()[:120]}"
            for rel, lineno, line in _scan_frontend_sources()
            if _INLINE_BADGE_RE.search(line)
        ]
        if hits:
            self.fail(
                f"发现 {len(hits)} 处内联徽标写法（应改为 `yb-badge yb-badge-<变体>`，"
                "见 web/templates/partials/component_layer.html 的说明）：\n" + "\n".join(hits)
            )

    def test_badge_base_class_is_present(self):
        """基础类必须存在，且显式 nowrap（此前 7 处有 7 处无）。"""
        with open(COMPONENT_LAYER, encoding="utf-8") as fh:
            css = fh.read()
        m = re.search(r"\.yb-badge\s*\{([^}]*)\}", css)
        self.assertIsNotNone(m, "component_layer 里找不到 .yb-badge 基础类")
        body = m.group(1)
        self.assertIn("white-space: nowrap", body, ".yb-badge 必须显式 nowrap（防长徽标折行）")
        self.assertIn("border-radius: 9999px", body, ".yb-badge 必须保持胶囊形（rounded-full）")

    def test_badge_variants_meet_aa(self):
        """5 个变体的「文字 on 徽标底」在两个模式下都达 AA（按调色板实时计算）。"""
        with open(COMPONENT_LAYER, encoding="utf-8") as fh:
            css = fh.read()
        palette = load_palette()

        blocks = {}
        for m in re.finditer(r"([^{}]*?)\.yb-badge-(\w+)\s*\{([^}]*)\}", css):
            selector, variant, body = m.group(1), m.group(2), m.group(3)
            if variant not in BADGE_VARIANTS:
                continue
            mode = "dark" if ".dark" in selector else "light"
            blocks[(variant, mode)] = body

        problems = []
        for variant in BADGE_VARIANTS:
            for mode in ("light", "dark"):
                body = blocks.get((variant, mode))
                if body is None:
                    problems.append(f"  {variant} / {mode}：component_layer 里找不到该变体的定义")
                    continue
                bg_m = re.search(
                    r"background-color:\s*rgb\(var\(--c-([a-z]+-\d+)\)(?:\s*/\s*([\d.]+))?\)", body
                )
                # ⚠ 必须锚定行首式 `color:`：`border-color:` 也以 `color:` 结尾，
                # 不锚定会把边框色当成文字色（会让本用例误报）。
                fg_m = re.search(r"(?<![\w-])color:\s*rgb\(var\(--c-([a-z]+-\d+)\)\)", body)
                if not bg_m or not fg_m:
                    problems.append(f"  {variant} / {mode}：解析不出底色或文字色")
                    continue
                fg = palette[fg_m.group(1)]
                bases = [palette[BG_LIGHT]] if mode == "light" else [
                    palette[BG_DARK], palette["zinc-800"]  # 徽标既在页面底也在卡片底上
                ]
                for base in bases:
                    alpha = float(bg_m.group(2)) if bg_m.group(2) else 1.0
                    bg = blend(palette[bg_m.group(1)], base, alpha)
                    ratio = contrast(fg, bg)
                    if round(ratio, 2) < AA_NORMAL_TEXT:
                        problems.append(
                            f"  {variant} / {mode}：{fg_m.group(1)} on {bg_m.group(1)}"
                            f" = {ratio:.2f}:1 < AA {AA_NORMAL_TEXT}:1"
                        )
        if problems:
            self.fail("状态徽标配色不达标：\n" + "\n".join(problems))

    def test_badge_thresholds_are_consistent_with_wcag(self):
        """本文件用到的两个阈值应与 WCAG 一致（防被误改成宽松值）。"""
        self.assertEqual(AA_NORMAL_TEXT, 4.5)
        self.assertEqual(AA_NON_TEXT, 3.0)


if __name__ == "__main__":
    unittest.main()
