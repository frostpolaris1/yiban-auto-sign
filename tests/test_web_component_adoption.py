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

## 钉住的几条

1. **不得裸写**主按钮 / 危险按钮 / 带底色的日志块（字符串判据）；
2. **文本类输入元素必须带 `yb-input`**（**按元素判**，见下）；
3. **语义提示条必须走 `.yb-alert-*`**（**按语义组合判**，见下）；
4. 组件层必须提供全部组件类（`.btn-*` / `.yb-input` / `.yb-inset` / `.yb-alert*` / `.yb-banner`）；
5. **配色必须真的成立**（本文件的价值所在，不只是"禁字符串"）：
   - `.btn-danger` 白字对底色 ≥ AA；
   - `.yb-inset` 的底色在**两种模式下都与 `.yb-card` 不同** —— 否则"嵌面板"等于没有底色。

## 关于判据为什么要"按元素/按语义"而不是"认字符串"

V3-5c 的输入框判据是**认一个具体 class 串**，于是 V3-7 发现另外 4 种写法（`dark:bg-zinc-900`、
`border-zinc-300` + ring 焦点、小尺寸内距…）**全部漏网，共 18 处**；提示条同理（10 种写法）。
换成「按元素（是 input 就必须带 yb-input）」与「按语义组合（底色与边框同色系就必须走组件类）」
之后，**换任何新写法都躲不过** —— 这是本文件与 V3-5c 那版最重要的区别。
"""

import os
import re
import unittest

from _wcag import AA_NORMAL_TEXT, BG_DARK, BG_LIGHT, WEB, blend, contrast, load_palette

SCAN_DIRS = (os.path.join(WEB, "templates"), os.path.join(WEB, "static", "js"))
SCAN_EXTS = (".html", ".js")
COMPONENT_LAYER = os.path.join(WEB, "templates", "partials", "component_layer.html")

# 禁用的裸写法 → 说明（应改用括号里的组件类）
#
# ⚠ **不要再用字符串式判据去认输入框**：V3-5c 当初只认
# `w-full bg-white dark:bg-zinc-800 border border-zinc-200` 这一种写法，于是
# `dark:bg-zinc-900`、`border-zinc-300 + ring 焦点`、`bg-white dark:bg-zinc-800 ... px-2.5 py-1.5`
# 等另外 4 种写法**全部漏网**（V3-7 才发现，共 18 处）。输入框与提示条改用
# **按元素/按语义组合判**（见下面两个用例），换任何新 class 写法都躲不过。
FORBIDDEN = {
    "主按钮": (re.compile(r"bg-blue-500 hover:bg-blue-600 text-white"), "btn-primary"),
    "危险按钮": (re.compile(r"bg-red-500 hover:bg-red-600 text-white"), "btn-danger"),
    "带底色的日志块": (re.compile(r'log-text[^"]*\bbg-(?:white|zinc-50)\b'), "yb-inset"),
}

# 输入类元素：这些 type 是"选择控件/隐藏域"，不走 .yb-input
INPUT_TYPE_EXEMPT = {"checkbox", "radio", "file", "hidden"}

# 提示条的语义组合：`bg-<语义>-50` 与 `border-<语义>-<档>` 同时出现 → 必须走 .yb-alert-*
_SEMANTIC = r"(?:red|green|amber|blue)"
_ALERT_COMBO = re.compile(
    r'class="([^"]*\bbg-' + _SEMANTIC + r"-50\b[^\"]*\bborder-" + _SEMANTIC + r"-\d{3}\b[^\"]*)\""
)

# 组件层必须提供的类
REQUIRED_CLASSES = (
    ".btn-primary",
    ".btn-danger",
    ".btn-ghost",
    ".yb-input",
    ".yb-inset",
    ".yb-alert",
    ".yb-alert-sm",
    ".yb-banner",
    ".yb-alert-error",
    ".yb-alert-success",
    ".yb-alert-warning",
    ".yb-alert-info",
)

NAMED_COLORS = {"white": (255, 255, 255), "black": (0, 0, 0)}

# 注释剥离：组件层的注释里会**引用**旧写法来解释"为什么改"（如 `.btn-danger` 上方
# 写了 `原为 bg-red-500 hover:bg-red-600 text-white`），不剥会自匹配。
_COMMENT_RE = re.compile(r"/\*.*?\*/|<!--.*?-->|//[^\n]*", re.S)


def _strip_comments(text):
    return _COMMENT_RE.sub(" ", text)


def _scan_sources():
    """遍历前端源，yield (相对路径, 行号, 全文)。行号为该文件首行的编号，便于偏移换算。"""
    for root in SCAN_DIRS:
        for dirpath, _dirnames, filenames in os.walk(root):
            for name in filenames:
                if not name.endswith(SCAN_EXTS):
                    continue
                path = os.path.join(dirpath, name)
                with open(path, encoding="utf-8") as fh:
                    yield os.path.relpath(path, WEB), 1, fh.read()


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

    def test_text_inputs_must_use_yb_input(self):
        """**按元素判**：文本类 `<input>`/`<textarea>`/`<select>` 必须带 `yb-input`。

        比"认某个 class 串"强的地方：换任何新写法（新的底色变体、ring 焦点、小尺寸内距）
        都躲不过 —— V3-5c 的字符串式判据就是这么漏掉后来那 4 种写法的（共 18 处）。
        复选框/单选/文件/隐藏域是"选择控件"，不走输入框样式，故豁免。
        """
        offenders = []
        for rel, lineno, text in _scan_sources():
            for m in re.finditer(r"<(input|textarea|select)\b[^>]*?>", text, re.S):
                tag = m.group(0)
                if "yb-input" in tag:
                    continue
                typ_m = re.search(r'type="(\w+)"', tag)
                typ = typ_m.group(1) if typ_m else (
                    "textarea" if m.group(1) == "textarea" else "text"
                )
                if typ in INPUT_TYPE_EXEMPT:
                    continue
                line = lineno + text.count("\n", 0, m.start())
                offenders.append(f"  {rel}:{line}  <{m.group(1)} type={typ}> 缺 yb-input")
        if offenders:
            self.fail(
                f"发现 {len(offenders)} 个文本类输入元素没走 .yb-input ——"
                " 它们是同一个东西却各写一套底色/边框/焦点（实测曾出现 5 种写法）：\n"
                + "\n".join(offenders)
            )

    def test_semantic_alert_bars_must_use_yb_alert(self):
        """**按语义组合判**：`bg-<语义>-50` + `border-<语义>-<档>` 同时出现 → 必须用 `.yb-alert-*`。

        V3-7 前 13 处提示条有 10 种写法（两种形状族、圆角 lg/xl/无、文字色 -600/-700/-800/-300）。
        这里不认具体串，只认"底色 + 边框都是同一个语义色"这个**语义特征**，
        所以新写法的告警条也躲不过。
        """
        offenders = []
        for rel, lineno, text in _scan_sources():
            if rel.endswith("component_layer.html"):
                continue  # 组件层自己就是定义处
            for m in _ALERT_COMBO.finditer(_strip_comments(text)):
                line = lineno + text.count("\n", 0, m.start())
                offenders.append(f"  {rel}:{line}  {m.group(1)[:110]}")
        if offenders:
            self.fail(
                f"发现 {len(offenders)} 处自研语义提示条（应改用 `yb-alert`/`yb-banner` + "
                "`yb-alert-{error,success,warning,info}`，形状与颜色正交、由组件层单一事实源决定）：\n"
                + "\n".join(offenders)
            )

    def test_alert_variants_meet_aa(self):
        """4 个语义提示条的「文字 on 底色」在两个模式下都达 AA（按调色板实时计算）。"""
        with open(COMPONENT_LAYER, encoding="utf-8") as fh:
            css = fh.read()
        palette = load_palette()
        problems = []
        for variant in ("error", "success", "warning", "info"):
            for mode, sel in (("light", f".yb-alert-{variant}"), ("dark", f".dark .yb-alert-{variant}")):
                body = _block(css, sel)
                bg_m = re.search(r"background-color:\s*rgb\(var\(--c-([a-z]+-\d+)\)(?:\s*/\s*([\d.]+))?\)", body)
                fg_m = re.search(r"(?<![\w-])color:\s*rgb\(var\(--c-([a-z]+-\d+)\)\)", body)
                if not bg_m or not fg_m:
                    problems.append(f"  {variant}/{mode}：解析不出底色或文字色")
                    continue
                base = palette[BG_LIGHT if mode == "light" else BG_DARK]
                alpha = float(bg_m.group(2)) if bg_m.group(2) else 1.0
                ratio = contrast(palette[fg_m.group(1)], blend(palette[bg_m.group(1)], base, alpha))
                if round(ratio, 2) < AA_NORMAL_TEXT:
                    problems.append(
                        f"  {variant}/{mode}：{fg_m.group(1)} on {bg_m.group(1)}"
                        f" = {ratio:.2f}:1 < AA {AA_NORMAL_TEXT}:1"
                    )
        if problems:
            self.fail("语义提示条配色不达标：\n" + "\n".join(problems))

    def test_palette_and_threshold_helpers_are_wired(self):
        """本文件用到的阈值应与 WCAG 一致（防被改成宽松值）。"""
        self.assertEqual(AA_NORMAL_TEXT, 4.5)

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
