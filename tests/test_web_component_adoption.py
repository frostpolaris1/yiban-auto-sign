# -*- coding: utf-8 -*-
"""回归守卫：组件类不得再被绕过裸写。

标签：F · 前端与界面守卫
覆盖：不得裸写主/危险按钮与带底色日志块；文本类输入元素必须带输入框组件类；语义提示条必须走 `.alert` 语义组合
对应实现：Adminator（`adminator.css` 的 `.input`/`.select`/`.textarea`/`.alert`）与 `web/static/css/app.css` 的项目补丁；各页面模板与组件源码是扫描对象
关键断言：判据按**元素**（是 input/textarea/select 就必须带输入框组件类）与按**语义组合**（底色与边框同色系就必须走提示条组件类），不认具体 class 串——换任何新写法都躲不过
依赖：纯本地——读模板/JS 原文并剥注释（`_strip_comments`）；不执行 JS、无需 node、不联网

## 为什么需要这条守卫

调用点仍有裸写样式，后果是双重的：
  · 组件类改一处，这些地方**不受影响** → "逐页视觉细化"无从下手（改了不生效）；
  · 它们各自带着缺陷与漂移（V3-5c 实测）：危险按钮用 `bg-red-500` 白字只有 3.76:1；
    4 个日志块各写一半底色，没有一套在两种模式下都可见；6 处输入框与同页已有的
    `.input` 并存；8 处主按钮色值等价却各写各的 padding/disabled 档位。

## 钉住的几条

1. **不得裸写**主按钮 / 危险按钮 / 带底色的日志块（字符串判据）；
2. **文本类输入元素必须带输入框组件类**（**按元素判**）；
3. **语义提示条必须走 `.alert`**（**按语义组合判**）。

## 关于判据为什么要"按元素/按语义"而不是"认字符串"

V3-5c 的输入框判据是**认一个具体 class 串**，于是 V3-7 发现另外 4 种写法（`dark:bg-zinc-900`、
`border-zinc-300` + ring 焦点、小尺寸内距…）**全部漏网，共 18 处**；提示条同理（10 种写法）。
换成「按元素（是 input 就必须带输入框组件类）」与「按语义组合（底色与边框同色系就必须走
提示条组件类）」之后，**换任何新写法都躲不过** —— 这是本文件与 V3-5c 那版最重要的区别。
"""

import os
import re
import unittest

from _wcag import WEB

SCAN_DIRS = (os.path.join(WEB, "templates"), os.path.join(WEB, "static", "js"))
SCAN_EXTS = (".html", ".js")

# 禁用的裸写法 → 说明（应改用括号里的活组件类）
#
# ⚠ **不要再用字符串式判据去认输入框**：V3-5c 当初只认
# `w-full bg-white dark:bg-zinc-800 border border-zinc-200` 这一种写法，于是
# `dark:bg-zinc-900`、`border-zinc-300 + ring 焦点`、`bg-white dark:bg-zinc-800 ... px-2.5 py-1.5`
# 等另外 4 种写法**全部漏网**（V3-7 才发现，共 18 处）。输入框与提示条改用
# **按元素/按语义组合判**（见下面两个用例），换任何新 class 写法都躲不过。
FORBIDDEN = {
    "主按钮": (re.compile(r"bg-blue-500 hover:bg-blue-600 text-white"), "btn btn--primary"),
    "危险按钮": (re.compile(r"bg-red-500 hover:bg-red-600 text-white"), "btn btn--danger-ghost"),
    "带底色的日志块": (re.compile(r'log-text[^"]*\bbg-(?:white|zinc-50)\b'), "app.css 的日志块样式"),
}

# 输入类元素：这些 type 是"选择控件/隐藏域/区间控件"，不走文本输入框样式
# （range 由项目级 `.range` 滑杆样式承担，见 app.css「数值滑杆」）
INPUT_TYPE_EXEMPT = {"checkbox", "radio", "file", "hidden", "range"}

# 输入框组件类（Adminator 设计系统）。判据仍是"按元素"：任何文本类输入元素必须命中
# 其中之一，不得裸写样式。2026-09-12：Adminator 对 `<select>`/`<textarea>` 另有同族类
# `.select`/`.textarea`（同一条组规则定义、同一套禁用/无效态），故按元素各自接受 ——
# 否则模板正确写法会被误报。
INPUT_CLASS_TOKENS = {
    "input": ("input",),
    "select": ("select", "input"),
    "textarea": ("textarea", "input"),
}


def _input_class_ok(tag, tag_name):
    """标签的 class 属性里是否含该元素对应的输入框组件类（按 token 精确匹配）。"""
    m = re.search(r'class="([^"]*)"', tag)
    if not m:
        return False
    tokens = m.group(1).split()
    return any(t in INPUT_CLASS_TOKENS[tag_name] for t in tokens)

# 提示条的语义组合：`bg-<语义>-50` 与 `border-<语义>-<档>` 同时出现 → 必须走 `.alert`
_SEMANTIC = r"(?:red|green|amber|blue)"
_ALERT_COMBO = re.compile(
    r'class="([^"]*\bbg-' + _SEMANTIC + r"-50\b[^\"]*\bborder-" + _SEMANTIC + r"-\d{3}\b[^\"]*)\""
)

# 注释剥离：注释里会**引用**旧写法来解释"为什么改"（如 `.btn--danger-ghost` 上方
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


class ComponentAdoptionTest(unittest.TestCase):
    def test_no_raw_markup_bypassing_component_classes(self):
        """不得绕过组件类裸写（失败时给出文件:行 + 该改用的组件类）。"""
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
                f"发现 {len(hits)} 处绕过组件类的裸写法 ——"
                "组件类是唯一事实源，绕过它会导致「改组件类不生效」，"
                "并且这些地方已经各自长出缺陷/漂移：\n" + "\n".join(hits)
            )

    def test_text_inputs_must_use_input_class(self):
        """**按元素判**：文本类 `<input>`/`<textarea>`/`<select>` 必须带输入框组件类。

        比"认某个 class 串"强的地方：换任何新写法（新的底色变体、ring 焦点、小尺寸内距）
        都躲不过 —— V3-5c 的字符串式判据就是这么漏掉后来那 4 种写法的（共 18 处）。
        复选框/单选/文件/隐藏域是"选择控件"，不走输入框样式，故豁免。

        判据只认"是该元素自带的输入框组件类"，不认裸写的底色/边框组合。
        """
        offenders = []
        for rel, lineno, text in _scan_sources():
            # 剥注释：JS/CSS 注释里会**提到** `<select>` 这类标签名来解释取舍
            # （如 components/time-field.js 写「取代原生 input[type=time] 与 <select>」），
            # 那不是真控件。与上面判据一致地先剥注释。
            text = _strip_comments(text)
            for m in re.finditer(r"<(input|textarea|select)\b[^>]*?>", text, re.S):
                tag = m.group(0)
                tag_name = m.group(1)
                if _input_class_ok(tag, tag_name):
                    continue
                typ_m = re.search(r'type="(\w+)"', tag)
                typ = typ_m.group(1) if typ_m else (
                    "textarea" if tag_name == "textarea" else "text"
                )
                if typ in INPUT_TYPE_EXEMPT:
                    continue
                line = lineno + text.count("\n", 0, m.start())
                offenders.append(
                    f"  {rel}:{line}  <{tag_name} type={typ}> 缺 "
                    + "/".join(INPUT_CLASS_TOKENS[tag_name])
                )
        if offenders:
            self.fail(
                f"发现 {len(offenders)} 个文本类输入元素没走输入框组件类 ——"
                " 它们是同一个东西却各写一套底色/边框/焦点（实测曾出现 5 种写法）：\n"
                + "\n".join(offenders)
            )

    def test_semantic_alert_bars_must_use_alert(self):
        """**按语义组合判**：`bg-<语义>-50` + `border-<语义>-<档>` 同时出现 → 必须用 `.alert`。

        V3-7 前 13 处提示条有 10 种写法（两种形状族、圆角 lg/xl/无、文字色 -600/-700/-800/-300）。
        这里不认具体串，只认"底色 + 边框都是同一个语义色"这个**语义特征**，
        所以新写法的告警条也躲不过。活提示条类：Adminator `.alert` + `.info/.success/.warning/.danger`。
        """
        offenders = []
        for rel, lineno, text in _scan_sources():
            for m in _ALERT_COMBO.finditer(_strip_comments(text)):
                line = lineno + text.count("\n", 0, m.start())
                offenders.append(f"  {rel}:{line}  {m.group(1)[:110]}")
        if offenders:
            self.fail(
                f"发现 {len(offenders)} 处自研语义提示条（应改用 `.alert` + "
                "`.info/.success/.warning/.danger`，形状与颜色正交、由组件类单一事实源决定）：\n"
                + "\n".join(offenders)
            )


if __name__ == "__main__":
    unittest.main()
