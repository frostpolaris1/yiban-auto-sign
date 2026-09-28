# -*- coding: utf-8 -*-
"""回归守卫：设计令牌的"静默失败"。

标签：F · 前端与界面守卫
覆盖：用到的色档必须在 app.css 调色板里有 `--c-*`（否则计算值无效、属性静默丢失）；状态徽标不得再现内联整段写法；阈值常量与 WCAG 一致
对应实现：`web/static/css/app.css` 的调色板与 `.badge` 语义色调档位（`badge--ok/bad/warn/info/muted`）
关键断言：单方向一条用例（用到的档位必须在调色板里定义）；徽标内联整段写法不得复活；阈值常量与 WCAG 一致
依赖：纯本地——扫前端源码**原文且不剥注释**（故注释里出现色值或徽标 class 串会被判成违规），无需 node、不联网

## 调色板完整性：用了但没定义的档位 → 属性**静默丢失**

`app.css` 给出 `--c-blue-200:191 219 254`，前端写 `bg-blue-200` 时用的是同名变量。
调色板缺档位 → 变量未定义 → 计算值无效 → 属性丢失，不报错、不警告 —— 页面上只是
"某处少了个底色/边框"，极难发现。V3-5 实测 `bg-blue-50` ×3 与两处 hover 档位都因此
静默失效，已按 Tailwind 官方标准值补齐，并由本文件的用例钉住"再也不能缺"。

## 状态徽标必须是**单一事实源**（不得再出现内联写法）

徽标 class 串此前被复制 **14 处、9 种写法**（`whitespace-nowrap` 7 有 7 无；error
不达 AA；info 用不存在的档位）。现统一走 Adminator `.badge` + app.css 的
`badge--{ok,bad,warn,info,muted}` 语义色调档位，本文件钉住内联写法不得复活；
各档配色的 AA 由 `test_web_text_contrast.py` 从调色板实时校验。
"""

import os
import re
import unittest

from _wcag import (
    AA_NON_TEXT,
    AA_NORMAL_TEXT,
    COLOR_FAMILIES,
    WEB,
    load_palette,
)

SCAN_DIRS = (os.path.join(WEB, "templates"), os.path.join(WEB, "static", "js"))
SCAN_EXTS = (".html", ".js")

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


class BadgeContractTest(unittest.TestCase):
    def test_no_inline_badge_markup(self):
        """徽标必须走 `.badge` + 语义色调档位，不得再出现内联的整段写法。"""
        hits = [
            f"  {rel}:{lineno}  {line.strip()[:120]}"
            for rel, lineno, line in _scan_frontend_sources()
            if _INLINE_BADGE_RE.search(line)
        ]
        if hits:
            self.fail(
                f"发现 {len(hits)} 处内联徽标写法（应改为 `badge badge--<变体>`，"
                "语义色调档位见 app.css）：\n" + "\n".join(hits)
            )

    def test_badge_thresholds_are_consistent_with_wcag(self):
        """本文件用到的两个阈值应与 WCAG 一致（防被误改成宽松值）。"""
        self.assertEqual(AA_NORMAL_TEXT, 4.5)
        self.assertEqual(AA_NON_TEXT, 3.0)


if __name__ == "__main__":
    unittest.main()
