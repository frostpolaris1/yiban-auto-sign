# -*- coding: utf-8 -*-
"""回归守卫（2026-09-10，V3-5b）：class 属性里 `dark:` 变体的**作用域**与**重复**。

本仓大量用 Tailwind 的 `dark:` 变体，而它有两种静默出错方式 —— 都不报错、只在暗色下
看起来"怪"，很难发现：

## 一、`dark:<prop>-v` 与 `dark:hover:<prop>-v` **同值** → 前者漏写了 `hover:`

实测最典型的是侧边栏导航项：

    text-zinc-600 dark:text-zinc-300 hover:bg-zinc-100 dark:bg-zinc-700
    dark:hover:bg-zinc-700 hover:text-zinc-900 dark:text-zinc-100 dark:hover:text-zinc-100

`dark:bg-zinc-700` / `dark:text-zinc-100` 是**旧做法把"选中态"写在 HTML 里**的残留 ——
选中态现已由 JS `switchTab` 用 `classList.toggle` 管理（app.js 的 `dark:bg-zinc-700` /
`dark:text-zinc-100`）。残留造成两个后果：
  ① 暗色下**首屏**（JS 执行之前）所有导航项都像"已选中"；
  ② `dark:text-zinc-100` 与 `dark:text-zinc-300` 同属性同变体冲突，**谁生效取决于
     Tailwind 的生成顺序**，即结果不确定。

## 二、同属性 + 同变体的颜色 token 重复（如 `dark:text-zinc-500 dark:text-zinc-400`）

同样是不确定结果。实测 5 个页面各有若干处（多为复制粘贴后误改）。

## 实现注意：要按「引号片段」分段检查

class 属性里可能有**内联三元**（如 `class="text-sm ${c ? 'a dark:text-blue-400' :
'b dark:text-zinc-400'}"`）——两个分支互斥，各自出现一次 `dark:text-*` 是**正常**的。
故本测试把属性切成「单引号片段」与「去引号后的剩余文本」分别检查，
避免把互斥分支误判为重复（`user.html` 的 `userStateLine` 就是这样一处）。
"""

import os
import re
import unittest

from _wcag import WEB

SCAN_DIRS = (os.path.join(WEB, "templates"), os.path.join(WEB, "static", "js"))
SCAN_EXTS = (".html", ".js")

PROPS = ("text", "bg", "border", "ring")
VARIANTS = ("dark", "hover", "focus", "disabled", "active")
_TOKEN_RE = re.compile(
    r"^(?:(?:dark|hover|focus|disabled|active):)*(?P<prop>text|bg|border|ring)-[a-z]+-\d{2,3}$"
)


def _segments(attr):
    """把 class 属性值切成若干互斥片段：每个单引号片段一段，去引号后的剩余文本一段。"""
    quoted = re.findall(r"'([^']*)'", attr)
    rest = re.sub(r"'[^']*'", " ", attr)
    return [s for s in (*quoted, rest) if s.strip()]


def _tokens(segment):
    """→ {(variant, prop): [色值尾串]}；`dark:placeholder:text-*` 等复合 token 不计。"""
    out = {}
    for tok in segment.split():
        if "placeholder" in tok:
            continue
        m = _TOKEN_RE.match(tok)
        if not m:
            continue
        variant = ":".join(v for v in VARIANTS if v + ":" in tok)
        out.setdefault((variant, m.group("prop")), []).append(tok.rsplit(":", 1)[-1])
    return out


def _scan():
    """返回 [(相对路径, 行号, 类型, 说明, class 片段)]。"""
    hits = []
    for root in SCAN_DIRS:
        for dirpath, _dirnames, filenames in os.walk(root):
            for name in filenames:
                if not name.endswith(SCAN_EXTS):
                    continue
                path = os.path.join(dirpath, name)
                rel = os.path.relpath(path, WEB)
                with open(path, encoding="utf-8") as fh:
                    text = fh.read()
                for m in re.finditer(r'class="([^"]*)"', text):
                    attr = m.group(1)
                    lineno = text.count("\n", 0, m.start()) + 1
                    for seg in _segments(attr):
                        toks = _tokens(seg)
                        for (variant, prop), values in toks.items():
                            # 一：同变体重复
                            if len(values) > 1:
                                hits.append((rel, lineno, "重复声明",
                                             f"{variant + ':' if variant else ''}{prop}-* ×{len(values)}",
                                             seg.strip()[:150]))
                            # 二：常驻 dark 与 dark:hover 同值（漏写 hover:）
                            if variant == "dark":
                                hover_vals = toks.get(("dark:hover", prop), [])
                                for v in values:
                                    if v in hover_vals:
                                        hits.append((rel, lineno, "漏写 hover:",
                                                     f"dark:{prop}-{v} 与 dark:hover 同值",
                                                     seg.strip()[:150]))
    return hits


class ClassAttributeHygieneTest(unittest.TestCase):
    def test_no_duplicate_or_unscoped_dark_color_tokens(self):
        """class 属性里不得有同变体重复、或与 dark:hover 同值的常驻 dark 声明。"""
        hits = _scan()
        if hits:
            detail = "\n".join(
                f"  [{kind}] {rel}:{lineno}  {note}\n      {seg}" for rel, lineno, kind, note, seg in hits
            )
            self.fail(
                f"发现 {len(hits)} 处 dark: 变体作用域/重复问题 ——"
                "同属性同变体重复时，谁生效取决于 Tailwind 的生成顺序（结果不确定）；"
                "`dark:<prop>-v` 与 `dark:hover:<prop>-v` 同值则说明前者漏写了 `hover:`"
                "（会让元素在暗色下常驻高亮）：\n" + detail
            )


if __name__ == "__main__":
    unittest.main()
