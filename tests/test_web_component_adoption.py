# -*- coding: utf-8 -*-
"""回归守卫（2026-09-10，V3-5c）：**组件层是唯一事实源** —— 不得再绕过它裸写。

## 为什么需要这条守卫

`app.css` 在 V3-0 已把样式 token 化（`--yb-*`），组件层（`partials/component_layer.html`）
是方案 C 的载体。但**调用点**仍有大量裸写，后果是双重的：
  · 组件层改一处，这些地方**不受影响** → "逐页视觉细化"无从下手（改了不生效）；
  · 它们各自带着缺陷与漂移（V3-5c 实测）：
      - 2 处危险按钮用 `bg-red-500`，白字只有 **3.76:1**（AA 正文需 ≥4.5）；
      - 4 个日志块各写一半（`bg-white dark:bg-zinc-900` vs `bg-zinc-50 dark:bg-zinc-800`），
        没有一套在两种模式下都可见 —— `bg-zinc-50` 在 zinc-800 卡片上（暗色）**与卡片同色**；
      - 6 处输入框是旧式「白底 + 明显边框」，与**同页已有的** `.yb-input` 填充式并存；
      - 8 处主按钮色值与 `--yb-primary` 等价（已核对）却各写各的 padding/disabled 档位。

## 钉住三条

1. **不得裸写**主按钮 / 危险按钮 / 输入框 / 带底色的日志块；
2. 组件层里 `.btn-primary` / `.btn-danger` / `.btn-ghost` / `.yb-input` / `.yb-inset` 必须都在；
3. **配色必须真的成立**（这是本文件的价值所在，不只是"禁字符串"）：
   - `.btn-danger` 白字对底色 ≥ AA；
   - `.yb-inset` 的底色在**两种模式下都与 `.yb-card` 不同** —— 否则"嵌面板"等于没有底色，
     正是 V3-5c 修掉的那个缺陷。
"""

import os
import re
import unittest

from _wcag import AA_NORMAL_TEXT, WEB, contrast, load_palette

SCAN_DIRS = (os.path.join(WEB, "templates"), os.path.join(WEB, "static", "js"))
SCAN_EXTS = (".html", ".js")
COMPONENT_LAYER = os.path.join(WEB, "templates", "partials", "component_layer.html")

# 禁用的裸写法 → 说明（应改用括号里的组件类）
FORBIDDEN = {
    "主按钮": (re.compile(r"bg-blue-500 hover:bg-blue-600 text-white"), "btn-primary"),
    "危险按钮": (re.compile(r"bg-red-500 hover:bg-red-600 text-white"), "btn-danger"),
    "输入框": (re.compile(r"w-full bg-white dark:bg-zinc-800 border border-zinc-200"), "yb-input"),
    "带底色的日志块": (re.compile(r'log-text[^"]*\bbg-(?:white|zinc-50)\b'), "yb-inset"),
}

# 组件层必须提供的类
REQUIRED_CLASSES = (".btn-primary", ".btn-danger", ".btn-ghost", ".yb-input", ".yb-inset")

NAMED_COLORS = {"white": (255, 255, 255), "black": (0, 0, 0)}

# 注释剥离：组件层的注释里会**引用**旧写法来解释"为什么改"（如 `.btn-danger` 上方
# 写了 `原为 bg-red-500 hover:bg-red-600 text-white`），不剥会自匹配。
_COMMENT_RE = re.compile(r"/\*.*?\*/|<!--.*?-->|//[^\n]*", re.S)


def _strip_comments(text):
    return _COMMENT_RE.sub(" ", text)


def _resolve(name, palette):
    return NAMED_COLORS.get(name) or palette.get(name)


def _block(css, selector):
    m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
    if not m:
        raise AssertionError(f"component_layer 里找不到 {selector} 的定义")
    return m.group(1)


def _bg_names(body):
    """从一段声明里取出（浅色 bg, 暗色 dark:bg）的 Tailwind 名。"""
    dark = re.search(r"\bdark:bg-([a-z]+(?:-\d+)?)\b", body)
    light = re.search(r"(?<![\w:-])(?<!dark:)bg-([a-z]+(?:-\d+)?)\b", re.sub(r"dark:bg-[\w-]+", " ", body))
    return (light.group(1) if light else None), (dark.group(1) if dark else None)


class ComponentAdoptionTest(unittest.TestCase):
    def test_no_raw_markup_bypassing_component_layer(self):
        """不得绕过组件层裸写（失败时给出文件:行 + 该改用的组件类）。"""
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
                    stripped = _strip_comments(text)
                    for label, (pat, replacement) in FORBIDDEN.items():
                        for m in pat.finditer(stripped):
                            line = stripped.count("\n", 0, m.start()) + 1
                            hits.append(f"  [{label}] {rel}:{line} → 应改用 `{replacement}`")
        if hits:
            self.fail(
                f"发现 {len(hits)} 处绕过组件层的裸写法 ——"
                "组件层是方案 C 的唯一事实源，绕过它会导致「改组件层不生效」，"
                "并且这些地方已经各自长出缺陷/漂移：\n" + "\n".join(hits)
            )

    def test_component_layer_provides_required_classes(self):
        """组件层必须齐全（缺了就会被裸写法替代）。"""
        with open(COMPONENT_LAYER, encoding="utf-8") as fh:
            css = fh.read()
        missing = [c for c in REQUIRED_CLASSES if c not in css]
        if missing:
            self.fail(f"component_layer 缺少这些类：{missing}")

    def test_danger_button_text_meets_aa(self):
        """危险按钮：白字对底色的对比度必须达 AA（原 `bg-red-500` 只有 3.76:1）。"""
        with open(COMPONENT_LAYER, encoding="utf-8") as fh:
            css = fh.read()
        body = _block(css, ".btn-danger")
        m = re.search(r"background-color:\s*rgb\(var\(--c-([a-z]+-\d+)\)\)", body)
        self.assertIsNotNone(m, ".btn-danger 的底色应来自调色板变量")
        ratio = contrast((255, 255, 255), load_palette()[m.group(1)])
        self.assertGreaterEqual(
            round(ratio, 2),
            AA_NORMAL_TEXT,
            f".btn-danger 白字 on {m.group(1)} = {ratio:.2f}:1 < AA {AA_NORMAL_TEXT}:1",
        )

    def test_inset_differs_from_card_in_both_modes(self):
        """嵌面板底色在两种模式下都必须**不等于**卡片底色（否则等于没有底色）。"""
        with open(COMPONENT_LAYER, encoding="utf-8") as fh:
            css = fh.read()
        palette = load_palette()

        card_light_name, card_dark_name = _bg_names(_block(css, ".yb-card"))
        inset_light_m = re.search(
            r"background-color:\s*rgb\(var\(--c-([a-z]+-\d+)\)\)", _block(css, ".yb-inset")
        )
        inset_dark_m = re.search(
            r"background-color:\s*rgb\(var\(--c-([a-z]+-\d+)\)\)", _block(css, ".dark .yb-inset")
        )
        self.assertIsNotNone(inset_light_m, ".yb-inset 应设浅色底色")
        self.assertIsNotNone(inset_dark_m, ".dark .yb-inset 应设暗色底色")

        pairs = (
            ("浅色", _resolve(inset_light_m.group(1), palette), _resolve(card_light_name, palette)),
            ("暗色", _resolve(inset_dark_m.group(1), palette), _resolve(card_dark_name, palette)),
        )
        for mode, inset, card in pairs:
            self.assertIsNotNone(inset, f".yb-inset {mode} 底色解析失败")
            self.assertIsNotNone(card, f".yb-card {mode} 底色解析失败")
            self.assertNotEqual(
                inset,
                card,
                f"{mode}：.yb-inset 与 .yb-card 同为 {inset} —— 嵌面板会**完全看不见**"
                "（这正是 V3-5c 修掉的缺陷：日志块曾用与卡片同色的底）",
            )


if __name__ == "__main__":
    unittest.main()
