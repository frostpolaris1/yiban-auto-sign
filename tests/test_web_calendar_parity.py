# -*- coding: utf-8 -*-
"""回归守卫：签到日历只有**一份实现**（web/static/js/calendar.js）。

## 历史（为什么这条守卫存在）

签到日历原本有两份几乎独立的实现：管理端在 `pages/mine.js` 里，用户端在
`web/templates/user.html` 的内联脚本里。两份各自维护，长期漂移 —— V3-4 实测到的差异包括：

    · 日期格：user 端**没有**「休」角标、aria-label 只报「今天/已签到」，
      admin 端则只报「周末不签到/查看记录」——**两边各缺一半信息**；
    · 标题：admin `font-semibold` vs user `font-medium`；
    · 月份切换按钮：user 端有 `aria-label="上个月/下个月"`，admin 端**完全没有**；
    · 星期表头与「日历加载失败」文案：user 端曾用写反的色对（V3-3 修）。

当时的处置是把日期格抽成**两份逐字相同**的 `calDayCell(o)` 并钉住"必须一致"。那只是
把漂移从"随时发生"变成"改一边会报红"，根因（两份实现）仍在。

## 现在的判据

用户自助页迁移到 Adminator 时，日历整体外提为 `web/static/js/calendar.js`：
**全站唯一实现**，用户页与旧管理端「我的账号」都调它（`window.renderCalendar`）。
于是本测试从"两份必须一致"改成"只能有一份"：

1. `web/static/js/` 下**恰好一个**文件定义 `function calDayCell(`，且必须是 calendar.js；
2. `web/templates/` 下**零个**文件内联日历实现（模板只放挂载容器 `#cal-wrap-<key>`）；
3. 加载关系成立：user.html 引入 calendar.js、pages/user.js 调用共享渲染函数、
   旧管理端 index.html 在 mine.js 之前引入 calendar.js（classic script 共享作用域）；
4. 视觉/无障碍要点仍在共享实现里（星期表头、月份按钮读屏名、「休」角标、
   失败提示、状态类名），防止被"顺手"删掉；
5. 旧的逐字副本残留写法（`border-transparent'} flex …`）不再出现。

注意：本测试**只钉"唯一实现 + 要点在位"**，不规定视觉长什么样 —— 颜色由 app.css 的
`--cal-*` 令牌决定，对比度由 `test_web_text_contrast.py` 实测。
"""

import os
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS_DIR = os.path.join(BASE, "web", "static", "js")
TEMPLATES_DIR = os.path.join(BASE, "web", "templates")
CALENDAR_JS = os.path.join(JS_DIR, "calendar.js")
USER_TEMPLATE = os.path.join(TEMPLATES_DIR, "user.html")
USER_JS = os.path.join(JS_DIR, "pages", "user.js")
LEGACY_INDEX = os.path.join(TEMPLATES_DIR, "index.html")

DAY_CELL_MARK = "function calDayCell(o)"

# 共享实现里必须同时保留的要点（改其一即报红，说明有人只改了一处契约）
REQUIRED_IN_SHARED = (
    # 月份切换按钮的读屏名称
    'aria-label="上个月"',
    'aria-label="下个月"',
    # 星期表头（周一起始）
    '["一", "二", "三", "四", "五", "六", "日"]',
    # 「休」角标：不单靠颜色区分周末停签
    "cal-off-badge",
    # 加载失败提示（同一句文案）
    "日历加载失败，请稍后重试",
    # 状态类名（颜色由 app.css 的 --cal-* 令牌给出）
    "cal-cell--ok",
    "cal-cell--bad",
    "cal-cell--off",
    "cal-cell--today",
)

# 旧的逐字副本残留写法（应已消失）
OLD_INLINE_MARKUP = "border-transparent'} flex items-center justify-center text-xs"


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _files_defining(mark, *roots):
    hits = []
    for root in roots:
        for dirpath, _dirnames, filenames in os.walk(root):
            for name in sorted(filenames):
                if not name.endswith((".js", ".html")):
                    continue
                path = os.path.join(dirpath, name)
                if mark in _read(path):
                    hits.append(path)
    return hits


class CalendarSingleSourceTest(unittest.TestCase):
    def test_exactly_one_js_file_defines_the_day_cell(self):
        """日历日期格只能有一份实现，且必须是 calendar.js。"""
        hits = _files_defining(DAY_CELL_MARK, JS_DIR)
        self.assertEqual(
            hits,
            [CALENDAR_JS],
            "签到日历的日期格实现不唯一 —— 它已被抽成 web/static/js/calendar.js 的单一实现，"
            "不得再在页面脚本里复制一份（复制必然漂移，历史缺陷见模块 docstring）：\n"
            + "\n".join(f"  含 `{DAY_CELL_MARK}` 的文件：{os.path.relpath(p, BASE)}" for p in hits),
        )

    def test_no_template_inlines_the_calendar(self):
        """模板里不得内联日历实现（只放挂载容器，逻辑全在共享模块）。"""
        hits = _files_defining(DAY_CELL_MARK, TEMPLATES_DIR)
        self.assertEqual(
            hits,
            [],
            "模板里出现了内联的日历实现 —— 签到日历只在 web/static/js/calendar.js 里实现，"
            '页面只需提供 id="cal-wrap-<key>" 的容器：\n'
            + "\n".join(f"  {os.path.relpath(p, BASE)}" for p in hits),
        )

    def test_user_page_loads_the_shared_module(self):
        """用户页必须引入共享日历，并由页面脚本调用共享渲染函数。"""
        html = _read(USER_TEMPLATE)
        self.assertIn("/static/js/calendar.js", html, "user.html 未引入共享 calendar.js")
        self.assertIn("/static/js/pages/user.js", html, "user.html 未引入 pages/user.js")
        js = _read(USER_JS)
        self.assertIn("window.renderCalendar", js, "pages/user.js 未调用共享的 renderCalendar")
        self.assertNotIn("calDayCell", js, "pages/user.js 不应自带日期格实现")

    def test_legacy_admin_page_still_wires_the_shared_calendar(self):
        """旧管理端（index.html + pages/mine.js）仍存在，必须把共享日历排在 mine.js 之前。

        classic script 共享全局作用域：calendar.js 未先加载时，mine.js 的
        renderCalendar 调用会在运行时 ReferenceError。
        """
        html = _read(LEGACY_INDEX)
        cal = html.find("/static/js/calendar.js")
        mine = html.find("/static/js/pages/mine.js")
        self.assertNotEqual(cal, -1, "index.html 未引入共享日历 calendar.js")
        self.assertNotEqual(mine, -1, "index.html 未引入 pages/mine.js")
        self.assertLess(cal, mine, "calendar.js 必须在 mine.js 之前加载（共享作用域依赖）")
        self.assertIn("renderCalendar(", _read(os.path.join(JS_DIR, "pages", "mine.js")))

    def test_shared_implementation_keeps_the_a11y_and_state_contract(self):
        """共享实现必须保留星期表头、月份读屏名、「休」角标、失败提示与状态类名。"""
        src = _read(CALENDAR_JS)
        missing = [s for s in REQUIRED_IN_SHARED if s not in src]
        if missing:
            self.fail(
                "共享日历实现缺少这些契约片段（被改动或删除？）：\n"
                + "\n".join(f"  {s!r}" for s in missing)
            )

    def test_old_inline_cell_markup_is_gone(self):
        """旧的内联拼日期格写法不得复活。"""
        offenders = []
        for root in (JS_DIR, TEMPLATES_DIR):
            for dirpath, _dirnames, filenames in os.walk(root):
                for name in filenames:
                    if not name.endswith((".js", ".html")):
                        continue
                    path = os.path.join(dirpath, name)
                    if OLD_INLINE_MARKUP in _read(path):
                        offenders.append(os.path.relpath(path, BASE))
        self.assertEqual(
            offenders,
            [],
            "仍有文件残留内联拼日期格的旧写法，应改为调用共享的 calDayCell/renderCalendar：\n"
            + "\n".join(f"  {p}" for p in offenders),
        )


if __name__ == "__main__":
    unittest.main()
