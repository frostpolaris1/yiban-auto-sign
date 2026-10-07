# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""viewport 契约守卫：软键盘用 interactive-widget=resizes-content。

标签：F · 前端与界面守卫
覆盖：三个 viewport 载体面。一面是 `frontend/*.html` 的 10 个 MPA 入口（Vite dev/preview 壳）；
      另一面是 `web/templates/layout_*.html` 的三个生产外壳（admin / user / auth）；
      第三面是 `web/static/vue/*.html` 的重建产物（生产实际输出的重建产物）。
对应实现：`frontend/*.html` 的 `<meta name="viewport">`、`web/templates/layout_*.html` 的同名标签、
      `web/static/vue/*.html` 的同名标签。
关键断言：每个载体的 viewport content 都必须含 `interactive-widget=resizes-content`。
      该属性让软键盘弹出时收缩布局视口。输入框因此保持可见。缺失即回到旧行为（键盘遮住输入框）。
      产物面单独钉住"前端改对但漏重建"这条漂移路径：源改对而产物没重建时，本面直接红。
依赖：纯本地——只读源文件文本，不执行 JS、无需 node、不联网。

## 为什么三个面都要钉（2026-10-07 盘查结论）

生产页面的 `<head>` 由 Flask 模板给出。所有迁移页模板都 extends `layout_*` 外壳。
`frontend/*.html` 只是 Vite 的本地壳，生产不服务它们。只改一面会让另一面漂移：
生产用户看不到修复，或 dev/preview 验证不到真实行为。三面具名断言才能拦住单边改动。

## 来历

真机验证：占位环境双版对照（实验版带该属性 vs 对照版现状）。用户真机 Chrome 结论：
实验版软键盘行为符合预期（输入框保持可见）。结论来自工单 yiban-auto-sign-13ho。
"""
import os
import re
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRONTEND_DIR = os.path.join(BASE, "frontend")
TEMPLATES_DIR = os.path.join(BASE, "web", "templates")
VUE_DIR = os.path.join(BASE, "web", "static", "vue")

TOKEN = "interactive-widget=resizes-content"

_META_TAG_RE = re.compile(r"<meta\b[^>]*\bname=\"viewport\"[^>]*>", re.I)
_CONTENT_RE = re.compile(r"\bcontent=\"([^\"]*)\"")


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _viewport_contents(text):
    """返回文本里全部 viewport meta 的 content 值（按出现顺序）。"""
    return [
        m.group(1)
        for tag in _META_TAG_RE.findall(text)
        for m in [_CONTENT_RE.search(tag)]
        if m
    ]


class ViewportInteractiveWidgetTest(unittest.TestCase):
    def test_frontend_entry_htmls_carry_token(self):
        """`frontend/` 的每个 MPA 入口 HTML 都必须带该属性（dev/preview 壳）。"""
        names = sorted(n for n in os.listdir(FRONTEND_DIR) if n.endswith(".html"))
        self.assertTrue(names, "frontend/ 下没找到任何 .html 入口（目录搬走了？）")

        bad = []
        for name in names:
            contents = _viewport_contents(_read(os.path.join(FRONTEND_DIR, name)))
            if not contents or not all(TOKEN in c for c in contents):
                bad.append(f"frontend/{name}（viewport content={contents}）")
        self.assertEqual(bad, [], f"以下入口 HTML 的 viewport 缺 {TOKEN}：{bad}")

    def test_layout_shells_carry_token(self):
        """三个生产外壳模板都必须带该属性（生产页面真正输出的 `<head>`）。"""
        names = sorted(n for n in os.listdir(TEMPLATES_DIR) if re.match(r"layout_.*\.html$", n))
        self.assertTrue(names, "web/templates/ 下没找到 layout_*.html 外壳（改名了？）")

        bad = []
        for name in names:
            contents = _viewport_contents(_read(os.path.join(TEMPLATES_DIR, name)))
            if not contents or not all(TOKEN in c for c in contents):
                bad.append(f"web/templates/{name}（viewport content={contents}）")
        self.assertEqual(bad, [], f"以下生产外壳的 viewport 缺 {TOKEN}：{bad}")

    def test_vue_built_htmls_carry_token(self):
        """`web/static/vue/` 的重建产物 HTML 都必须带该属性（生产实际输出的重建产物）。

        这一面封的是"前端改对但漏重建"的漂移路径。源改了而产物没重建时，本断言红。
        承重用突变证：删任一产物的属性（如在 /tmp 副本上删）→ 本断言红。
        """
        names = sorted(n for n in os.listdir(VUE_DIR) if n.endswith(".html"))
        self.assertTrue(names, "web/static/vue/ 下没找到任何 .html 产物（没构建？）")

        bad = []
        for name in names:
            contents = _viewport_contents(_read(os.path.join(VUE_DIR, name)))
            if not contents or not all(TOKEN in c for c in contents):
                bad.append(f"web/static/vue/{name}（viewport content={contents}）")
        self.assertEqual(bad, [], f"以下 Vue 产物的 viewport 缺 {TOKEN}：{bad}")


if __name__ == "__main__":
    unittest.main()
