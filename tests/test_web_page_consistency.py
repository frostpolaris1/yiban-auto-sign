# -*- coding: utf-8 -*-
"""页面级一致性守卫：整页唯一元素每页只出现一次 + 导航不靠客户端 tab 显隐。

## 历史起因（单页 tab 时代，2026-09-10，V3-6）

逐页截图核对时发现 admin 每页的**版本号与开源入口出现两次**：
  · 侧边栏底部一份（`index.html` 的 aside 内）；
  · 页面底部 `<footer>` 一份。
user / login 只有一份，是 index 独有的漂移。修复后统一留在页脚，
并由 `test_version_and_source_appear_exactly_once_per_page` 钉住"恰好一次"。

## 2026-09-12 换壳（Adminator + 多页 MPA + 服务端渲染外壳）后的形态变化

管理端不再有单页 tab：入口改为 `layout_admin.html` 外壳 + `pages/*.html`，
侧栏是服务端渲染的 `<a href>` 页面链接（`partials/sidebar.html`），页脚移到
`partials/footer.html` 并被外壳 include。于是原 `test_no_page_level_entry_moved_into_a_tab`
的两条判据失效：
  1. 它要求"除三个整页外任何模板都不得含版本/开源标记"——新外壳的合法页脚
     (`partials/footer.html`) 命中了这条，报红；
  2. 它防的"被塞进某个 tab、随 tab 显隐而消失"在 MPA 下已无 tab 可塞。

**新判据（保护意图不变）**：入口的"可见性"不再能依赖客户端显隐开关——
  · 导航项必须是真实 `<a href>`，且 href 指向 `web/app.py` 已注册的路由；
  · 新外壳与各页正文不得出现 `data-tab-group`/`data-tab-btn`/`switchTab` 这类
    "内容靠客户端切换显隐"的机制；
  · 版本/开源这类整页唯一条目只由共享页脚承载 **一次**，页面正文模板里 **零次**
    （按页复制才会造成某页缺失或多出——正是原缺陷的成因）。

## 保留的判据

`test_version_and_source_appear_exactly_once_per_page` 继续覆盖每个整页模板，但改用
`frontend_source` 聚合读取（模板 + extends/include 片段 + 外链自研静态资源）：
  · `index.html` —— 旧栈单页（已无路由渲染，文件暂留）；
  · `login.html` —— 已迁到 `layout_auth.html` 外壳，页脚由 `partials/footer.html` 承载；
  · `user_accounts.html` —— 用户端「账号与设置」，外壳 `layout_user.html`（复用 Adminator .shell）；
  · `user_calendar.html` —— 用户端「签到日历」，同一外壳，页脚同样来自共享片段。
只有聚合读取才能让"整页唯一条目恰好一次"在"条目搬进共享页脚"后的新形态继续成立
（否则 login/user 会读到 0 次而误报缺失）。
"""

import os
import re
import unittest

from _frontend_src import frontend_source

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES = os.path.join(BASE, "web", "templates")
APP_PY = os.path.join(BASE, "web", "app.py")

PAGE_TEMPLATES = ("index.html", "login.html", "pages/user_accounts.html", "pages/user_calendar.html")

# 新管理端外壳与页面模板（换壳后路由实际渲染的载体），用于"无 tab 显隐"判据。
ADMIN_SHELL = (
    "layout_admin.html",
    "partials/sidebar.html",
    "partials/topbar.html",
    "partials/footer.html",
)
ADMIN_PAGES = (
    "pages/dashboard.html",
    "pages/accounts.html",
    "pages/logs.html",
    "pages/users.html",
    "pages/settings.html",
    "pages/mine.html",
)

# 客户端 tab 显隐机制的特征串（多页架构下页面级内容不得靠它显隐）
TAB_MECHANISM_MARKERS = ("data-tab-group", "data-tab-btn", "data-tab-target", "switchTab(")

# 整页唯一标记 → 说明
UNIQUE_MARKERS = {
    "开源（AGPL": "开源/源码入口",
    "易班自动签到 v{{": "版本号（点击看更新日志）",
}

# 侧栏 items 元组：('key', '/href', 'icon', '文案')
_NAV_ITEM_RE = re.compile(r"\(\s*'(\w+)'\s*,\s*'(/[^']*)'\s*,")
# 页面路由：只取静态路径（含 <参数> 的动态路由不可能是导航目标）
_ROUTE_RE = re.compile(r'@app\.route\(\s*["\'](/[^"\'<>]*)["\']')


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class PageConsistencyTest(unittest.TestCase):
    def test_version_and_source_appear_exactly_once_per_page(self):
        """每个整页（含其共享外壳/页脚）里，版本号与开源入口都必须**恰好 1 处**。

        按聚合源码判定：整页唯一条目可以住在共享页脚（登录页即如此），
        只要该页最终渲染出的整份源码里恰好出现一次。
        """
        problems = []
        for name in PAGE_TEMPLATES:
            text = frontend_source(name)
            for marker, label in UNIQUE_MARKERS.items():
                n = len(re.findall(re.escape(marker), text))
                if n != 1:
                    problems.append(
                        f"  {name}: {label}（{marker!r}）出现 {n} 次，应为 1 次"
                        + ("（重复：同页会出现两个入口）" if n > 1 else "（缺失：页脚入口不见了？）")
                    )
        if problems:
            self.fail(
                "整页唯一元素的数量不对 —— 这类重复只在特定断点暴露，"
                "静态检查不报，靠本测试钉住：\n" + "\n".join(problems)
            )

    def test_no_page_level_entry_moved_into_a_tab(self):
        """导航必须是真实页面链接；整页唯一条目不得按页复制、不得靠 tab 显隐。

        原保护目标：版本/开源入口不得挪进 tab 片段（会随 tab 显隐而消失）。
        新保护目标（等效，见模块 docstring）：入口可见性不依赖客户端显隐开关 ——
        侧栏导航 href 全部指向已注册路由；新外壳/正文无 tab 显隐机制；整页唯一条目
        只由共享页脚承载一次、正文模板零次。
        """
        # ① 侧栏导航：真实 <a href>，且 href 指向已注册路由
        sidebar = _read(os.path.join(TEMPLATES, "partials", "sidebar.html"))
        nav = _NAV_ITEM_RE.findall(sidebar)
        self.assertTrue(nav, "侧栏未解析到 nav 条目（items 列表结构变了？）")
        registered = set(_ROUTE_RE.findall(_read(APP_PY)))
        missing = [href for _key, href in nav if href not in registered]
        self.assertEqual(
            missing,
            [],
            f"侧栏导航指向未注册的路由：{missing}（已注册：{sorted(registered)}）——"
            "多页架构下导航必须是可直达的真实页面链接，不能是前端自造的假目标",
        )
        self.assertIn(
            '<a class="nav-link',
            sidebar,
            "侧栏导航项必须是 <a> 页面链接（不是 tab 按钮）",
        )
        self.assertIn(
            'href="{{ request.script_root }}{{ href }}"',
            sidebar,
            "侧栏导航项必须用 href 指向真实路由（不能靠 data-* + JS 切换）",
        )

        # ② 新外壳/正文不得靠客户端 tab 显隐
        offenders = []
        for name in ADMIN_SHELL + ADMIN_PAGES:
            text = _read(os.path.join(TEMPLATES, name))
            for marker in TAB_MECHANISM_MARKERS:
                if marker in text:
                    offenders.append(f"  {name}: 含 {marker!r}")
        if offenders:
            self.fail(
                "新管理端出现客户端 tab 显隐机制 —— 页面级内容与入口不得靠 tab 切换"
                "显隐（多页架构下这会让某些入口/正文在特定状态消失）：\n"
                + "\n".join(offenders)
            )

        # ③ 整页唯一条目：共享页脚一次、页面正文零次
        footer = _read(os.path.join(TEMPLATES, "partials", "footer.html"))
        for marker, label in UNIQUE_MARKERS.items():
            n = len(re.findall(re.escape(marker), footer))
            self.assertEqual(
                n, 1,
                f"共享页脚里 {label}（{marker!r}）应恰好 1 次，实际 {n} 次",
            )
            for name in ADMIN_PAGES:
                self.assertNotIn(
                    marker,
                    _read(os.path.join(TEMPLATES, name)),
                    f"{name} 正文含 {label} —— 整页唯一条目只应由共享页脚承载，"
                    "按页复制会在某页缺失或重复（原缺陷即由此而来）",
                )


if __name__ == "__main__":
    unittest.main()
