# -*- coding: utf-8 -*-
"""回归守卫：签到日历只有**一份实现**（web/static/js/calendar.js）。

## 历史（为什么这条守卫存在）

签到日历原本有两份几乎独立的实现：管理端在 `pages/mine.js` 里，用户端在
`web/templates/user.html` 的内联脚本里。两份各自维护，长期漂移 —— V3-4 实测到的差异包括：

    · 日期格：user 端**没有**「休」角标、aria-label 只报「今天/已签到」，
      admin 端则只报「周末不签到/查看记录」——**两边各缺一半信息**；
    · 月份切换按钮：user 端有 `aria-label="上个月/下个月"`，admin 端**完全没有**；
    · 星期表头与「日历加载失败」文案：user 端曾用写反的色对（V3-3 修）。

当时的处置是把日期格抽成**两份逐字相同**的 `calDayCell(o)` 并钉住"必须一致"；那只是把
漂移从"随时发生"变成"改一边会报红"，根因（两份实现）仍在。

## 现在的判据

日历整体外提为 `web/static/js/calendar.js`：**全站唯一实现**，用户端「签到日历」页
（`pages/user_calendar.html` + `pages/user_calendar.js`）与管理端「我的账号」
（`pages/mine.html` 内联模式 + `components/my-accounts.js`）都调它。
本测试钉住"只能有一份"：

1. `web/static/js/` 下**恰好一个**文件定义 `function dayCell(`，且必须是 calendar.js；
2. `web/templates/` 下**零个**文件内联日历实现；
3. 加载关系成立：日历页引入 calendar.js 并由页面脚本调 `SignCalendar.render`；
   管理端「我的账号」在 my-accounts.js 之前引入 calendar.js（classic script 共享作用域）；
4. 视觉/无障碍要点仍在共享实现里（星期表头、月份按钮读屏名、「休」角标、失败提示、
   状态类名），防止被"顺手"删掉；
5. **类名前缀不得与 Adminator 撞车**：Adminator 自带事件月历（`.cal-grid` 有
   `grid-auto-rows:minmax(110px,1fr)`、`.cal-cell` 带 border-right/bottom 与
   flex-direction:column）。自研签到日历沿用同名类时，其未覆盖属性会渗透进来 ——
   实测把日期格撑成 62×110 的竖长条（宽高比失控、数字悬在空盒中央）。
   故本测试钉住：自研源码里不得出现 `.cal-grid` / `.cal-cell` / `.cal-weekdays`
   这类 Adminator 月历类名（calendar.js 一律用 `sc-` 前缀）。
"""

import os
import re
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS_DIR = os.path.join(BASE, "web", "static", "js")
TEMPLATES_DIR = os.path.join(BASE, "web", "templates")
CALENDAR_JS = os.path.join(JS_DIR, "calendar.js")
USER_CAL_PAGE = os.path.join(TEMPLATES_DIR, "pages", "user_calendar.html")
USER_CAL_JS = os.path.join(JS_DIR, "pages", "user_calendar.js")
USER_ACCOUNTS_PAGE = os.path.join(TEMPLATES_DIR, "pages", "user_accounts.html")
# 管理端「我的账号」：日历内联模式的真实载体（换了 index.html 单页之后）
MINE_PAGE = os.path.join(TEMPLATES_DIR, "pages", "mine.html")
MY_ACCOUNTS_JS = os.path.join(JS_DIR, "components", "my-accounts.js")

DAY_CELL_MARK = "function dayCell(o)"

# 共享实现里必须同时保留的要点（改其一即报红，说明有人只改了一处契约）
REQUIRED_IN_SHARED = (
    # 月份切换按钮的读屏名称
    'aria-label="上个月"',
    'aria-label="下个月"',
    # 星期表头（周一起始）
    '["一", "二", "三", "四", "五", "六", "日"]',
    # 「休」角标：不单靠颜色区分周末停签
    "sc-off",
    # 加载失败提示（同一句文案）
    "日历加载失败，请稍后重试",
    # 状态类名（颜色由 app.css 的 --cal-* 令牌给出）
    "sc-cell--ok",
    "sc-cell--bad",
    "sc-cell--off",
    "sc-cell--today",
    # 选中态：日期格与日志面板的联动标记
    "is-selected",
)

# Adminator 自带月历的类名：自研签到日历一律不得使用（见模块 docstring 第 5 条）
ADMINATOR_CALENDAR_CLASSES = ("cal-grid", "cal-cell", "cal-weekdays", "cal-main", "cal-toolbar")

# 注释剥离：calendar.js 的说明性注释里会引用这些类名来解释"为什么不能撞车"，
# 不剥会把解释本身判成违规（同 test_web_component_adoption 的做法）。
_COMMENT_RE = re.compile(r"/\*.*?\*/|<!--.*?-->|//[^\n]*", re.S)


def _strip_comments(text):
    return _COMMENT_RE.sub(" ", text)

# 旧的内联拼日期格残留写法（应已消失）
OLD_INLINE_MARKUP = "border-transparent'} flex items-center justify-center text-xs"


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _iter_sources(*roots):
    for root in roots:
        for dirpath, _dirnames, filenames in os.walk(root):
            for name in sorted(filenames):
                if name.endswith((".js", ".html")):
                    yield os.path.join(dirpath, name)


def _files_defining(mark, *roots):
    return [p for p in _iter_sources(*roots) if mark in _read(p)]


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
            "模板里出现了内联的日历实现 —— 签到日历只在 web/static/js/calendar.js 里实现：\n"
            + "\n".join(f"  {os.path.relpath(p, BASE)}" for p in hits),
        )

    def test_calendar_page_loads_the_shared_module(self):
        """日历页必须引入共享日历，并由页面脚本调用共享渲染接口。"""
        html = _read(USER_CAL_PAGE)
        self.assertIn("/static/js/calendar.js", html, "签到日历页未引入共享 calendar.js")
        self.assertIn("/static/js/pages/user_calendar.js", html, "签到日历页未引入 pages/user_calendar.js")
        js = _read(USER_CAL_JS)
        self.assertIn("SignCalendar.render", js, "pages/user_calendar.js 未调用共享渲染接口")
        self.assertNotIn("dayCell", js, "pages/user_calendar.js 不应自带日期格实现")

    def test_accounts_page_does_not_embed_a_calendar(self):
        """账号与设置页不得再内嵌日历（日历已独立成页，避免同一组件两处维护）。"""
        html = _read(USER_ACCOUNTS_PAGE)
        for mark in ("data-sc-mount", "data-sc-log", "calendar.js"):
            self.assertNotIn(mark, html, f"账号与设置页不应出现日历相关标记：{mark}")

    def test_admin_mine_page_wires_the_shared_calendar(self):
        """管理端「我的账号」必须把共享日历排在调用它的组件之前。

        换壳后管理端不再有 index.html 单页：`/mine` 的日历改为**内联模式** ——
        `pages/mine.html` 引入 `calendar.js`，`components/my-accounts.js` 在生效账号卡里
        调 `window.SignCalendar.render`。classic script 共享全局作用域：calendar.js 未先
        加载时该调用会在运行时 ReferenceError。

        判据意图（管理端必须接线共享日历、且顺序正确）不变，仅载体从退役的
        `index.html` + `pages/mine.js` 换到 `pages/mine.html` + `components/my-accounts.js`。
        """
        html = _read(MINE_PAGE)
        cal = html.find("/static/js/calendar.js")
        acct = html.find("/static/js/components/my-accounts.js")
        self.assertNotEqual(cal, -1, "pages/mine.html 未引入共享日历 calendar.js")
        self.assertNotEqual(acct, -1, "pages/mine.html 未引入 components/my-accounts.js")
        self.assertLess(cal, acct, "calendar.js 必须在 my-accounts.js 之前加载（共享作用域依赖）")
        self.assertIn(
            "SignCalendar.render", _read(MY_ACCOUNTS_JS),
            "components/my-accounts.js 未调用共享日历渲染接口 SignCalendar.render",
        )

    def test_shared_implementation_keeps_the_a11y_and_state_contract(self):
        """共享实现必须保留星期表头、月份读屏名、「休」角标、失败提示与状态类名。"""
        src = _read(CALENDAR_JS)
        missing = [s for s in REQUIRED_IN_SHARED if s not in src]
        if missing:
            self.fail(
                "共享日历实现缺少这些契约片段（被改动或删除？）：\n"
                + "\n".join(f"  {s!r}" for s in missing)
            )

    def test_no_adminator_calendar_class_names_in_our_sources(self):
        """自研源码不得使用 Adminator 事件月历的类名（属性渗透会让日期格失控）。

        判据按 **class token 边界**匹配（前后不得是 `\\w`/`-`）：`mini-cal-grid` 这类
        项目自有的近似名不误伤；注释里对该类名的解释性引用也不算违规。
        """
        offenders = []
        for path in _iter_sources(JS_DIR, TEMPLATES_DIR):
            src = _strip_comments(_read(path))
            for cls in ADMINATOR_CALENDAR_CLASSES:
                if re.search(r"(?<![\w-])" + re.escape(cls) + r"(?![\w-])", src):
                    offenders.append(f"  {os.path.relpath(path, BASE)}: 出现 {cls!r}")
        if offenders:
            self.fail(
                "签到日历复用了 Adminator 事件月历的类名 —— 其未被覆盖的属性（grid-auto-rows、"
                "border-right/bottom、flex-direction）会渗透进来，实测会把日期格撑成竖长条。"
                "自研日历一律用 sc- 前缀：\n" + "\n".join(offenders)
            )

    def test_old_inline_cell_markup_is_gone(self):
        """旧的内联拼日期格写法不得复活。"""
        offenders = [
            os.path.relpath(p, BASE)
            for p in _iter_sources(JS_DIR, TEMPLATES_DIR)
            if OLD_INLINE_MARKUP in _read(p)
        ]
        self.assertEqual(
            offenders,
            [],
            "仍有文件残留内联拼日期格的旧写法，应改为调用共享的 dayCell/render：\n"
            + "\n".join(f"  {p}" for p in offenders),
        )


if __name__ == "__main__":
    unittest.main()
