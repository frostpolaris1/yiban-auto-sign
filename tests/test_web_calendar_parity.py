# -*- coding: utf-8 -*-
"""回归守卫：签到日历只有**一份实现**（现为 Vue 单组件 + 口径模块）。

标签：F · 前端与界面守卫
覆盖：签到日历「全站唯一实现」守卫——单实现总账（日期格渲染契约唯一、模板零内联、两端日历页
的资产与共享视图契约、账号页零内嵌、管理端链接、Adminator 类名不撞车、旧内联残留清零）
+ 共享实现里的无障碍与状态类名契约
对应实现：`frontend/src/calendar/{SignCalendar.vue,CalendarPage.vue,model.js,main.ts}` 与
`web/templates/pages/{user,my}_calendar.html`、`web/templates/partials/page_sign_calendar.html`
关键断言：全站**恰好一个**文件产出日期格渲染契约（`data-sc-date`，按契约而非函数名判定）
且必须是 SignCalendar.vue，`web/templates/` 下零个内联实现，连资产引入、共享视图调用与
账号页零内嵌一并钉住；Adminator 类名按 **class token 边界**匹配（`mini-cal-grid` 这类前缀
变体不误伤）
依赖：纯本地——读前端源码文本（判定前先剥注释），**不执行 JS、无需 node**、不联网。
      JS 行为（语气档映射/急停文案/周末门）由 tests/test_calendar_state_visibility.py 在
      Node 里真跑 model.js，纯口径的其余部分由 frontend/src/calendar/model.spec.ts 覆盖。

## 历史（为什么这条守卫存在）

签到日历原本有两份几乎独立的实现：管理端在 `pages/my_account.js` 里，用户端在
`web/templates/user.html` 的内联脚本里。两份各自维护，长期漂移 —— V3-4 实测到的差异包括：

    · 日期格：user 端**没有**「休」角标、aria-label 只报「今天/已签到」，
      admin 端则只报「周末不签到/查看记录」——**两边各缺一半信息**；
    · 月份切换按钮：user 端有 `aria-label="上个月/下个月"`，admin 端**完全没有**；
    · 星期表头与「日历加载失败」文案：user 端曾用写反的色对（V3-3 修）。

当时的处置是把日期格抽成**两份逐字相同**的 `calDayCell(o)` 并钉住"必须一致"；那只是把
漂移从"随时发生"变成"改一边会报红"，根因（两份实现）仍在。随后日历整体外提为
`web/static/js/calendar.js`（全站唯一实现，被两端日历页共用）。

## 现在的判据（2026-10-03 迁到 Vue 后重锚）

日历对整体迁到 Vue：`frontend/src/calendar/SignCalendar.vue` 是**日期格的唯一产出方**
（口径在 `model.js`），`CalendarPage.vue` 是页面编排，两页共用**同一入口**（calendar.html，
变体由挂载点 data-role 注入），页头 + 挂载点 + 内联状态载荷由共享 partial 渲染。
legacy 的 `calendar.js` / `sign-calendar-view.js` / `pages/*_calendar.js` 全部退役。

本测试钉住"只能有一份"，判据逐条换锚、意图不变：

1. 全站**恰好一个**文件定义日期格渲染契约（`data-sc-date`），且必须是 SignCalendar.vue；
2. `web/templates/` 下**零个**文件内联日历实现（模板只出页头、挂载点与内联载荷）；
3. 两端日历页都引共享 partial 与 manifest 资产，且**不再**引任何 legacy 日历脚本；
   共享 partial 出内联状态载荷、CalendarPage 调 SignCalendar（共享视图接口）；
4. 视觉/无障碍要点仍在共享实现里（星期表头、月份按钮读屏名、「休」角标、失败提示、
   状态类名与语气档底色类），防止被"顺手"删掉；
5. **类名前缀不得与 Adminator 撞车**：Adminator 自带事件月历（`.cal-grid` 有
   `grid-auto-rows:minmax(110px,1fr)`、`.cal-cell` 带 border-right/bottom 与
   flex-direction:column）。自研签到日历沿用同名类时，其未覆盖属性会渗透进来 ——
   实测把日期格撑成 62×110 的竖长条（宽高比失控、数字悬在空盒中央）。
   故本测试钉住：自研源码里不得出现 `.cal-grid` / `.cal-cell` / `.cal-weekdays`
   这类 Adminator 月历类名（自研一律用 `sc-` 前缀）。
"""

import os
import re
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS_DIR = os.path.join(BASE, "web", "static", "js")
TEMPLATES_DIR = os.path.join(BASE, "web", "templates")
FRONTEND_SRC = os.path.join(BASE, "frontend", "src")
CALENDAR_DIR = os.path.join(FRONTEND_SRC, "calendar")

# 共享实现的两个载体（口径 / 视图）：日期格类名与读屏名在 model.js，属性绑定在 SignCalendar.vue
MODEL_JS = os.path.join(CALENDAR_DIR, "model.js")
SIGN_CAL = os.path.join(CALENDAR_DIR, "SignCalendar.vue")
CAL_PAGE = os.path.join(CALENDAR_DIR, "CalendarPage.vue")
CAL_ENTRY_HTML = os.path.join(BASE, "frontend", "calendar.html")
CAL_MAIN_TS = os.path.join(CALENDAR_DIR, "main.ts")

USER_CAL_PAGE = os.path.join(TEMPLATES_DIR, "pages", "user_calendar.html")
MINE_CAL_PAGE = os.path.join(TEMPLATES_DIR, "pages", "my_calendar.html")
# 两端共用的页面骨架（页头 + 挂载点 + 内联状态载荷）
CAL_PARTIAL = os.path.join(TEMPLATES_DIR, "partials", "page_sign_calendar.html")
# 两端「我的账号」页共用的正文：日历不得出现在这里
# 2026-10-03：账号页迁到 Vue 后，共享正文 partial 已退役；日历「不内嵌」与「链接指向」两条
# 规则改锚到这里——Vue 组件才是账号页正文的事实源（模板只剩挂载点）。
ACCOUNTS_VUE = os.path.join(BASE, "frontend", "src", "myaccounts", "MyAccounts.vue")
USER_ACCOUNTS_PAGE = os.path.join(TEMPLATES_DIR, "pages", "user_account.html")
MINE_PAGE = os.path.join(TEMPLATES_DIR, "pages", "my_account.html")

# 检测锚用**渲染契约**（日期格必须带 data-sc-date）而不是函数名：重命名内部函数
# 不该触发"实现不唯一"的误报，而第二个实现仍必然要产出这个属性。
# 带上 `="` 更严：只有真正把它写成属性（含 Vue 的 `:data-sc-date="..."` 绑定）才算命中，
# 注释/字符串里提到属性名不算。
DAY_CELL_MARK = 'data-sc-date="'

# legacy 日历脚本：迁移后任何页面都不得再引（"连引用一起删"的守卫范式）
LEGACY_CAL_SCRIPTS = (
    "/static/js/calendar.js",
    "/static/js/components/sign-calendar-view.js",
    "/static/js/pages/user_calendar.js",
    "/static/js/pages/my_calendar.js",
)

# 共享实现里必须保留的要点（改其一即报红，说明有人只改了一处契约）。
# 键为文件，值为该文件必须含有的片段；比对时源码会先压掉空白，故换行/空格写法变化不误报。
REQUIRED_IN_SHARED = {
    # 口径：语气档 → 底色类名（五档都在样式表里；无状态格走 --none）
    MODEL_JS: (
        "sc-cell--",
        "sc-cell--none",
        "sc-cell--off",
        "sc-cell--today",
        "is-selected",
        # 星期表头（周一起始）：用户可见契约
        '["一","二","三","四","五","六","日"]',
        # 状态表读入点：符号只是存储口径，反查完语气档即弃
        "by_symbol",
    ),
    # 视图：属性绑定 + 无障碍 + 失败态
    SIGN_CAL: (
        # 月份切换按钮的读屏名称
        'aria-label="上个月"',
        'aria-label="下个月"',
        # 「休」角标：不单靠颜色区分周末停签
        "sc-off",
        # 加载失败提示：固定前缀 + 重试入口（文案带 YB.api 的友好 reason，
        # 不再是单一整句；钉住前缀与重试按钮，防失败态退化成静默空表）
        "日历加载失败：",
        "data-sc-retry",
        # 日期格的渲染契约：唯一产出方（带 `="` 的形态见 DAY_CELL_MARK 的说明）
        'data-sc-date="',
        # 选中态：日期格与日志面板的联动标记
        "aria-pressed",
        # 类名必须走 Vue 的属性通道（`:class`）——语气档来自服务端表，
        # 绑定而非拼 HTML 是"不会把档位名当标记注入"的实现面保证
        ':class="cell.cls"',
    ),
}

# Adminator 自带月历的类名：自研签到日历一律不得使用（见模块 docstring 第 5 条）
ADMINATOR_CALENDAR_CLASSES = ("cal-grid", "cal-cell", "cal-weekdays", "cal-main", "cal-toolbar")

# 注释剥离：说明性注释里会引用这些类名来解释"为什么不能撞车"，
# 不剥会把解释本身判成违规。
_COMMENT_RE = re.compile(r"/\*.*?\*/|<!--.*?-->|//[^\n]*", re.S)

# 旧的内联拼日期格残留写法（应已消失）
OLD_INLINE_MARKUP = "border-transparent'} flex items-center justify-center text-xs"

_SOURCE_EXT = (".js", ".ts", ".vue", ".html")


def _strip_comments(text):
    return _COMMENT_RE.sub(" ", text)


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _iter_sources(*roots):
    for root in roots:
        for dirpath, _dirnames, filenames in os.walk(root):
            for name in sorted(filenames):
                if name.endswith(_SOURCE_EXT):
                    yield os.path.join(dirpath, name)


def _files_defining(mark, *roots):
    return [p for p in _iter_sources(*roots) if mark in _read(p)]


class CalendarSingleSourceTest(unittest.TestCase):
    # ---------------- 单实现总账：7 条规则，每规则一个 subTest ----------------
    def _rule_day_cell_defined_once(self):
        hits = _files_defining(DAY_CELL_MARK, JS_DIR, TEMPLATES_DIR, FRONTEND_SRC)
        self.assertEqual(
            hits,
            [SIGN_CAL],
            "签到日历的日期格实现不唯一 —— 它已被抽成 frontend/src/calendar/SignCalendar.vue"
            "（口径在 model.js）的单一实现，不得再在别处复制一份（复制必然漂移，历史缺陷见"
            "模块 docstring）：\n"
            + "\n".join(f"  含 `{DAY_CELL_MARK}` 的文件：{os.path.relpath(p, BASE)}" for p in hits),
        )

    def _rule_no_template_inlines(self):
        hits = _files_defining(DAY_CELL_MARK, TEMPLATES_DIR)
        self.assertEqual(
            hits,
            [],
            "模板里出现了内联的日历实现 —— 模板只出页头、挂载点与内联状态载荷，"
            "日期格一律由 frontend/src/calendar/SignCalendar.vue 渲染：\n"
            + "\n".join(f"  {os.path.relpath(p, BASE)}" for p in hits),
        )

    def _rule_pages_share_the_partial_and_assets(self):
        """两端日历页：共用同一 partial、引 manifest 资产，且不再引任何 legacy 日历脚本。

        2026-10-03：日历对迁到 Vue 后，两页共用**同一入口**（frontend/calendar.html，
        变体由挂载点 data-role 注入）；页头与内联状态载荷由共享 partial 渲染，
        日历本体是共享的 SignCalendar.vue。
        """
        for page, role, title in ((USER_CAL_PAGE, "user", "签到日历"),
                                  (MINE_CAL_PAGE, "admin", "我的日历")):
            name = os.path.basename(page)
            html = _read(page)
            self.assertIn('from "partials/page_sign_calendar.html" import body', html,
                          f"{name} 未引用共享 partial（两页正文必须同一份）")
            self.assertIn(f"'{role}'", html, f"{name} 未把角色配置交给共享 partial")
            self.assertIn(title, html, f"{name} 未把本页页头文案交给共享 partial")
            self.assertIn("vue_js", html, f"{name} 未引入 manifest 解析出的入口资产")
            self.assertIn("vue_preloads", html, f"{name} 未引入依赖预载（modulepreload）")
            for legacy in LEGACY_CAL_SCRIPTS:
                self.assertNotIn(legacy, html,
                                 f"{name} 仍引入 legacy 日历脚本 {legacy}（应随迁移一起删）")

        partial = _read(CAL_PARTIAL)
        self.assertIn("vue-calendar-app", partial, "共享 partial 未提供挂载点")
        self.assertIn("window.YB_CALENDAR_STATE", partial,
                      "共享 partial 未渲染内联状态载荷（显示表单一源的页面侧入口）")
        self.assertIn("calendar_state | tojson", partial, "内联载荷必须经 tojson 转义")

        # 入口与共享视图：一个入口服务两页；编排层调共享日历组件、不自带日期格
        for path in (CAL_ENTRY_HTML, CAL_MAIN_TS):
            self.assertTrue(os.path.isfile(path), f"缺少日历入口文件：{path}")
        self.assertIn("vue-calendar-app", _read(CAL_MAIN_TS), "入口未挂载到共享挂载点")
        page_src = _read(CAL_PAGE)
        self.assertIn("SignCalendar", page_src, "页面编排未调用共享日历组件")
        self.assertNotIn(DAY_CELL_MARK, page_src, "页面编排不应自带日期格实现")

    def _rule_legacy_files_are_gone(self):
        """legacy 日历脚本必须**从磁盘删掉**，而不只是不再被引用。

        "连引用一起删"的另一半：只改引用、留着文件，等于把死代码留在库里等人误用
        （legacy 的 calendar.js 曾是"全站唯一实现"的载体，留着最容易被再引一次）。
        """
        leftovers = [os.path.relpath(p, BASE) for p in (
            os.path.join(JS_DIR, "calendar.js"),
            os.path.join(JS_DIR, "components", "sign-calendar-view.js"),
            os.path.join(JS_DIR, "pages", "my_calendar.js"),
            os.path.join(JS_DIR, "pages", "user_calendar.js"),
        ) if os.path.exists(p)]
        self.assertEqual(leftovers, [], "legacy 日历文件仍留在库里（应随迁移删除）：\n"
                         + "\n".join(f"  {p}" for p in leftovers))

    def _rule_accounts_pages_do_not_embed(self):
        """两端「我的账号」页不得内嵌日历。

        日历已独立成页；账号页只放「签到日历」链接（calendar_href），
        避免同一组件两处维护（历史缺陷见模块 docstring）。
        """
        for path in (USER_ACCOUNTS_PAGE, MINE_PAGE, ACCOUNTS_VUE):
            text = _read(path)
            for mark in ("data-sc-mount", "data-sc-log", "calendar.js"):
                self.assertNotIn(
                    mark, text,
                    f"{os.path.relpath(path, BASE)} 不应出现日历相关标记：{mark}",
                )

    def _rule_admin_mine_links_to_admin_calendar(self):
        # 管理端账号页的「签到日历」链接由挂载点的 data-calendar-href 注入（Vue 不写死路径）
        html = _read(MINE_PAGE)
        self.assertIn("/my/calendar", html, "管理端账号页的日历链接未指向 /my/calendar")
        self.assertIn("data-calendar-href", html, "日历链接须经 data-calendar-href 注入")
        vue = _read(ACCOUNTS_VUE)
        self.assertIn("calendarHref", vue, "Vue 账号页须消费注入的日历链接")

    def _rule_no_adminator_class_names(self):
        """自研源码不得使用 Adminator 事件月历的类名（属性渗透会让日期格失控）。

        判据按 **class token 边界**匹配（前后不得是 `\\w`/`-`）：`mini-cal-grid` 这类
        项目自有的近似名不误伤；注释里对该类名的解释性引用也不算违规。
        """
        offenders = []
        for path in _iter_sources(JS_DIR, TEMPLATES_DIR, FRONTEND_SRC):
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

    def _rule_old_inline_markup_gone(self):
        offenders = [
            os.path.relpath(p, BASE)
            for p in _iter_sources(JS_DIR, TEMPLATES_DIR, FRONTEND_SRC)
            if OLD_INLINE_MARKUP in _read(p)
        ]
        self.assertEqual(
            offenders,
            [],
            "仍有文件残留内联拼日期格的旧写法，应改为走共享口径/组件：\n"
            + "\n".join(f"  {p}" for p in offenders),
        )

    def test_single_implementation_rules(self):
        """单实现八规则总账（原七条静态扫描逐条同构，6c3-D D-3 并参数化）。"""
        rules = (
            ("日期格实现唯一（必须是 SignCalendar.vue）", self._rule_day_cell_defined_once),
            ("模板零内联日历", self._rule_no_template_inlines),
            ("两端日历页共享 partial/资产、零 legacy 脚本", self._rule_pages_share_the_partial_and_assets),
            ("legacy 日历文件已从磁盘删除", self._rule_legacy_files_are_gone),
            ("账号页零内嵌日历", self._rule_accounts_pages_do_not_embed),
            ("管理端账号页链接指向 /my/calendar", self._rule_admin_mine_links_to_admin_calendar),
            ("自研源码零 Adminator 月历类名", self._rule_no_adminator_class_names),
            ("旧内联日期格写法清零", self._rule_old_inline_markup_gone),
        )
        for label, rule in rules:
            with self.subTest(rule=label):
                rule()

    def test_shared_implementation_keeps_the_a11y_and_state_contract(self):
        """共享实现必须保留星期表头、月份读屏名、「休」角标、失败提示与状态类名。"""
        for path, required in REQUIRED_IN_SHARED.items():
            src = _read(path)
            # 逐条按字面子串比对；同时比对"压掉空白"的版本，
            # 避免只因换行/空格调整就判红（星期表头的数组字面量即此类）
            compact = re.sub(r"\s+", "", src)
            missing = [s for s in required if s not in src and s not in compact]
            if missing:
                self.fail(
                    f"{os.path.relpath(path, BASE)} 缺少这些契约片段（被改动或删除？）：\n"
                    + "\n".join(f"  {s!r}" for s in missing)
                )


if __name__ == "__main__":
    unittest.main()
