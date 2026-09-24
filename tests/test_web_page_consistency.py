# -*- coding: utf-8 -*-
"""页面级一致性守卫：整页唯一元素每页只出现一次 + 导航不靠客户端 tab 显隐。

标签：F · 前端与界面守卫
覆盖：整页唯一元素（版本号与开源入口）每页恰好一次、侧栏导航必须是真实 `<a href>` 且指向已注册路由、tab 标记的收窄判据、含分区页面的 WAI-ARIA tabs 与 roving tabindex 初始态
对应实现：`partials/footer.html` / `partials/sidebar.html` / `layout_*.html` / `pages/*.html`；路由清单取 `web/app.py` 与 `web/routes/*.py` 的注册源码
关键断言：「恰好一次」必须按 `_frontend_src.frontend_source` 聚合读取（模板 + extends/include 片段 + 外链自研静态资源）才成立——条目搬进共享页脚后只读单文件会读到 0 次而误报缺失；外壳/片段仍禁止全部 tab 标记，页面正文可用 `data-tab-group` 做页内分区，但每个 `data-tab-target` 必须有同文件对应的 `data-tab-id`、`.page-title` 仍恰好 1 个、`data-tab-btn` / `switchTab(` 一律禁止
依赖：纯本地——读前端源码与注册路由表文本，**不执行 JS、无需 node**、不联网

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
  · 新外壳与各页正文不得出现 `data-tab-btn`/`switchTab` 这类"整页内容靠客户端切换
    显隐"的机制；
  · 版本/开源这类整页唯一条目只由共享页脚承载 **一次**，页面正文模板里 **零次**
    （按页复制才会造成某页缺失或多出——正是原缺陷的成因）。

## 2026-09-12 收窄（判据意图不放宽，只放开合法用法）

`/work/settings` 按功能重做后改用模板 `.tabs` 做**页内分区**（六个配置分区，契约
`[data-tab-group]` + `.tab[data-tab-target]` + `.tab-panel[data-tab-id]`，切换由 core.js
承担）。原先一刀切禁止 `data-tab-group` 会误伤这个合法用法，故收窄为：

  · **外壳/片段**（layout_*/partials）：仍禁止全部 tab 标记 —— 导航必须是真实链接；
  · **页面正文**（pages/*.html）：允许 `data-tab-group` 做页内分区，但必须
    ① 每个 `data-tab-target` 在同文件内有对应 `data-tab-id`（否则分区点不开/内容消失）；
    ② 页面级标题 `.page-title` 仍恰好 1 个（分区不得把整页标题吞进某个 tab）；
    ③ `data-tab-btn` / `switchTab(` 这类整页显隐机制仍全部禁止。

即：**页内分区可，整页内容/导航藏进 tab 不可** —— 原缺陷（入口随 tab 显隐而消失）
仍在保护范围内。

## 保留的判据

`test_version_and_source_appear_exactly_once_per_page` 继续覆盖每个整页模板，但改用
`frontend_source` 聚合读取（模板 + extends/include 片段 + 外链自研静态资源）：
  · `login.html` —— 已迁到 `layout_auth.html` 外壳，页脚由 `partials/footer.html` 承载；
  · `user_account.html` —— 用户端「账号与设置」，外壳 `layout_user.html`（复用 Adminator .shell）；
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
ROUTES_DIR = os.path.join(BASE, "web", "routes")

# 认证页与用户端两页（本清单里的页面在共享页脚里恰好各出现一次）；
# 管理端七页由下方 ADMIN_PAGES 覆盖 —— 两份清单页面集互斥，不是重复断言。
PAGE_TEMPLATES = ("login.html", "pages/user_account.html", "pages/user_calendar.html")

# 新管理端外壳与页面模板（换壳后路由实际渲染的载体），用于"无 tab 显隐"判据。
ADMIN_SHELL = (
    "layout_admin.html",
    "partials/sidebar.html",
    "partials/topbar.html",
    "partials/footer.html",
)
ADMIN_PAGES = (
    "pages/data_dashboard.html",
    "pages/data_logs.html",
    "pages/work_accounts.html",
    "pages/work_users.html",
    "pages/work_settings.html",
    "pages/my_account.html",
    "pages/my_calendar.html",
)

# 客户端 tab 显隐机制的特征串。
# 外壳/片段：**全部禁止** —— 导航必须是真实 <a href> 页面链接，不能靠 data-* + JS 切换。
TAB_MECHANISM_MARKERS = ("data-tab-group", "data-tab-btn", "data-tab-target", "switchTab(")

# 页面正文：只禁「整页内容/导航藏进 tab、随 tab 显隐而消失」的老机制。
# 模板 .tabs 的**页内分区**是允许的（它是模板现成组件，非整页显隐开关），
# 但必须满足：每个 data-tab-target 在本文件有对应 data-tab-id，且页面级标题仍恰好 1 个。
PAGE_FORBIDDEN_TAB_MARKERS = ("data-tab-btn", "switchTab(")
_TAB_TARGET_RE = re.compile(r'data-tab-target="([^"]+)"')
_TAB_ID_RE = re.compile(r'data-tab-id="([^"]+)"')
_PAGE_TITLE_RE = re.compile(r'class="page-title"')

# 整页唯一标记 → 说明
UNIQUE_MARKERS = {
    "开源（AGPL": "开源/源码入口",
    "易班自动签到 v{{": "版本号（点击看更新日志）",
}

# 侧栏 items 元组：('key', '/href', 'icon', '文案')
_NAV_ITEM_RE = re.compile(r"\(\s*'([\w-]+)'\s*,\s*'(/[^']*)'\s*,")
# 页面路由：只取静态路径（含 <参数> 的动态路由不可能是导航目标）。
# 路由分域后有两种注册写法：app.py 内的 @app.route 装饰器，以及
# web/routes/*.py 的 register(app) 里 app.add_url_rule(路径, …)。
_ROUTE_RE = re.compile(
    r'(?:@app\.route\(|app\.add_url_rule\()\s*["\'](/[^"\'<>]*)["\']'
)


def _registered_page_paths():
    """全部注册源码里出现的静态页面路径：app.py + web/routes/*.py。"""
    files = [APP_PY]
    if os.path.isdir(ROUTES_DIR):
        files += [os.path.join(ROUTES_DIR, n)
                  for n in sorted(os.listdir(ROUTES_DIR)) if n.endswith(".py")]
    paths = set()
    for path in files:
        paths |= set(_ROUTE_RE.findall(_read(path)))
    return paths


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
        registered = _registered_page_paths()
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

        # ② 新外壳/片段不得靠客户端 tab 显隐（导航一律真实链接）
        offenders = []
        for name in ADMIN_SHELL:
            text = _read(os.path.join(TEMPLATES, name))
            for marker in TAB_MECHANISM_MARKERS:
                if marker in text:
                    offenders.append(f"  {name}: 含 {marker!r}")
        if offenders:
            self.fail(
                "新管理端外壳/片段出现客户端 tab 显隐机制 —— 导航与整页入口不得靠 tab 切换"
                "显隐（多页架构下这会让某些入口在特定状态消失）：\n"
                + "\n".join(offenders)
            )

        # ②b 页面正文：允许模板 .tabs 做**页内分区**（2026-09-12 /settings 重做起），
        #     仍禁止「整页内容/导航藏进 tab」的老机制；分区必须成对且不吞掉页面级标题。
        page_offenders, pair_problems = [], []
        for name in ADMIN_PAGES:
            text = _read(os.path.join(TEMPLATES, name))
            for marker in PAGE_FORBIDDEN_TAB_MARKERS:
                if marker in text:
                    page_offenders.append(f"  {name}: 含 {marker!r}")
            targets = _TAB_TARGET_RE.findall(text)
            ids = set(_TAB_ID_RE.findall(text))
            for t in targets:
                if t not in ids:
                    pair_problems.append(f"  {name}: data-tab-target={t!r} 无对应 data-tab-id")
            if targets and len(_PAGE_TITLE_RE.findall(text)) != 1:
                pair_problems.append(
                    f"  {name}: 用了页内分区，页面级标题 .page-title 应恰好 1 个"
                    f"（实际 {len(_PAGE_TITLE_RE.findall(text))}）"
                )
        if page_offenders or pair_problems:
            self.fail(
                "页面正文的分区机制不合规 —— 判据：**页内分区可、整页内容/导航藏进 tab 不可**。"
                "允许模板 .tabs（[data-tab-group] + .tab[data-tab-target] + "
                ".tab-panel[data-tab-id]）做页内分区，但每个 target 必须在本文件有对应 id，"
                "且页面级标题不得被分区吞掉；data-tab-btn / switchTab( 这类整页显隐机制"
                "仍全部禁止：\n" + "\n".join(page_offenders + pair_problems)
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


# ---- 分区 tab 的 WAI-ARIA tabs 模式 + roving tabindex（服务端渲染初始态） ----
# core.js 运行时同步 aria-selected/tabindex；这里钉的是模板初始结构：
# role/aria 齐全且 roving 初始值正确 —— 缺了会让键盘用户在 JS 生效前（或 JS 失败时）
# 面对一串都进 Tab 键序的分区标签（每页 N 个停止点），读屏也拿不到 tab/tabpanel 语义。
_TAB_TAG_RE = re.compile(r"<a\b[^>]*\bdata-tab-target=\"([^\"]+)\"[^>]*>", re.S)
_TAB_PANEL_RE = re.compile(r"<div\b[^>]*\bdata-tab-id=\"([^\"]+)\"[^>]*>", re.S)
# 自管深链的分组标记（settings：切换要过脏守卫，写 ?tab= 的时机由页面控制）
_TAB_URL_OWN_PAGE = "pages/work_settings.html"


class TabAriaRovingTest(unittest.TestCase):
    def test_tab_partitions_render_aria_tabs_with_roving_tabindex(self):
        """含分区的页面必须以 WAI-ARIA tabs 模式渲染，且 roving tabindex 初始态正确。

        判据（每页）：
          ① 每个 .tab 有 role="tab" 与 aria-controls，指向的 id 真实存在、
             是 role="tabpanel" 且 aria-labelledby 回指该 tab；
          ② roving 初始：未隐藏的 tab 里 tabindex="0" 恰好 1 个，其余（含 hidden）全 -1；
          ③ data-tab-url-own（深链自管标记）只允许 settings 的分组带 —— 其它页面
             深链走 core.js 委托路径，带上它会让 ?tab= 不再随切换同步。
        """
        problems = []
        for name in ADMIN_PAGES:
            text = _read(os.path.join(TEMPLATES, name))
            if not _TAB_TAG_RE.search(text):
                continue  # 无分区页跳过
            tabs = []
            for m in _TAB_TAG_RE.finditer(text):
                tag = m.group(0)
                tabs.append({
                    "target": m.group(1), "tag": tag,
                    "tid": (re.search(r'\bid="([^"]+)"', tag) or [None, None])[1],
                    "hidden": re.search(r"\bhidden\b", tag) is not None,
                    "role_tab": re.search(r'role="tab"', tag) is not None,
                    "controls": (re.search(r'aria-controls="([^"]+)"', tag) or [None, None])[1],
                    "tabindex": (re.search(r'tabindex="([^"]*)"', tag) or [None, None])[1],
                })
            panels = {}
            for m in _TAB_PANEL_RE.finditer(text):
                tag = m.group(0)
                panels[m.group(1)] = {
                    "role": re.search(r'role="tabpanel"', tag) is not None,
                    "labelledby": (re.search(r'aria-labelledby="([^"]+)"', tag) or [None, None])[1],
                }
            ids = set(re.findall(r'id="([^"]+)"', text))
            zero = [t for t in tabs if t["tabindex"] == "0" and not t["hidden"]]
            for t in tabs:
                label = f"{name}: tab target={t['target']!r}"
                panel = panels.get(t["target"])
                if not t["role_tab"]:
                    problems.append(f"  {label} 缺 role=\"tab\"")
                if not t["controls"]:
                    problems.append(f"  {label} 缺 aria-controls")
                elif t["controls"] not in ids:
                    problems.append(f"  {label} aria-controls={t['controls']!r} 指向不存在的 id")
                if panel is None:
                    problems.append(f"  {label} 无对应 data-tab-id 的面板")
                else:
                    if not panel["role"]:
                        problems.append(f"  {label} 的面板 data-tab-id={t['target']!r} 缺 role=\"tabpanel\"")
                    if panel["labelledby"] != t["tid"]:
                        problems.append(
                            f"  {label} 的面板 aria-labelledby={panel['labelledby']!r}"
                            f" 应回指本 tab 的 id={t['tid']!r}"
                        )
                if t["hidden"] and t["tabindex"] == "0":
                    problems.append(f"  {label} 已 hidden 却 tabindex=0")
                if t["tabindex"] not in ("0", "-1"):
                    problems.append(f"  {label} 缺 roving tabindex 初始值（0 / -1），实际 {t['tabindex']!r}")
            if len(zero) != 1:
                problems.append(
                    f"  {name}: 未隐藏 tab 里 tabindex=0 应恰好 1 个（roving 唯一停止点），"
                    f"实际 {len(zero)}"
                )
            if name == _TAB_URL_OWN_PAGE and "data-tab-url-own" not in text:
                problems.append(f"  {name}: 分区组缺 data-tab-url-own（脏守卫页必须自管深链写 URL 时机）")
            if name != _TAB_URL_OWN_PAGE and "data-tab-url-own" in text:
                problems.append(f"  {name}: 不应带 data-tab-url-own（带上后 ?tab= 不随切换同步）")
        if problems:
            self.fail(
                "分区 tab 的 WAI-ARIA/roving 结构不对 —— 模板必须服务端渲染 role=tablist/"
                "tab/tabpanel 与 aria-controls/aria-labelledby，roving tabindex 初始态为"
                "活动 0、其余 -1（core.js 只负责切换后的同步，初始结构缺失会在 JS 生效前"
                "让 Tab 键序里出现多个分区停止点）：\n" + "\n".join(problems)
            )


if __name__ == "__main__":
    unittest.main()
