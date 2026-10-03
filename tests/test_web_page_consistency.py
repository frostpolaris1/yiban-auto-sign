# -*- coding: utf-8 -*-
"""页面级一致性守卫：含分区页面的 WAI-ARIA tabs 与 roving tabindex 初始态。

标签：F · 前端与界面守卫
覆盖：分区 tab 的服务端渲染初始结构——role/aria 齐全（tab/tabpanel 互指）且 roving 初始值正确（未隐藏 tab 里 tabindex="0" 恰好 1 个、其余含 hidden 全 -1）；并反查「深链自管」页不再借 legacy 标记（设置页整页迁 Vue，自管页签在 SettingsPage.vue 里）
对应实现：`web/templates/pages/*.html` 的 `.tabs` 分区 markup、`frontend/src/settings/SettingsPage.vue` 的自管页签
关键断言：core.js 只负责切换后的 aria/tabindex 同步，**初始结构**必须由模板给出——缺失会让键盘用户在 JS 生效前（或 JS 失败时）面对一串都在 Tab 键序里的分区标签，读屏也拿不到 tab/tabpanel 语义
依赖：纯本地——读模板源码文本，**不执行 JS、无需 node**、不联网

> 批 6c3-A 说明：旧 `PageConsistencyTest` 两条静态守卫（整页唯一元素「恰好一次」
> 按 `frontend_source` 聚合扫描、页级入口/tab 显隐机制文本判据）为「精确源码 grep」
> 型守卫；整页结构由 `test_web_render_golden` 真渲染钉住，按对表裁撤。
"""

import os
import re
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES = os.path.join(BASE, "web", "templates")

# 新管理端页面模板（换壳后路由实际渲染的载体）——分区 tab 只出现在这些正文里。
# 2026-10-03：`pages/data_logs.html` 移出本清单——该页已迁到 Vue（分区改由 el-tabs
# 渲染），服务端不再出 tab 标记，故本文件的「服务端初始 ARIA 结构」守卫对它不再适用；
# 其分区行为由 Vitest（format.spec）+ Playwright（e2e/logs.spec）覆盖。
# 2026-10-03：`pages/data_dashboard.html` 移出本清单——数据看板页整页迁到 Vue，
# 正文改由组件渲染（本页服务端只出挂载点与内联载荷），故「服务端初始 ARIA 结构」
# 这条判据对它不再适用（同 data_logs / work_users 的处置）；页面行为由
# frontend/src/dashboard/model.spec.ts 与 tests/test_web_dashboard_page.py 覆盖。
ADMIN_PAGES = (
    # 2026-10-03：`pages/work_accounts.html` 移出本清单——该页已整页迁到 Vue，分区改由组件
    # 渲染（frontend/src/accounts/Accounts.vue），服务端不再出 tab 标记，故「服务端初始
    # ARIA 结构」这条判据对它不再适用（同 data_logs / work_users 的处置）；分区行为由
    # Vitest（accounts/model.spec）+ Playwright（e2e/logs.spec 的账号管理页断言段）覆盖。
    # 2026-10-03：`pages/work_users.html` 移出本清单——该页已迁到 Vue，分区改由组件渲染
    # （且刻意不再使用 core.js 的 data-tab-target 契约，以免两套机制争抢同一批 DOM），
    # 故「服务端初始 ARIA 结构」这条判据对它不再适用（同 data_logs 的处置）。
    # 2026-10-03：`pages/work_settings.html` 最后移出——设置页整页迁到 Vue，分区由
    # SettingsPage.vue 自管（同样**刻意不用** core.js 的 data-tab-target 契约：那套的
    # document 级点击委托会先于本页的未保存改动守卫切换分区，legacy 的守卫因此形同虚设）。
    # 分区行为由 frontend/src/settings/model.spec.ts 与 e2e（logs.spec 的设置段）覆盖。
    "pages/my_account.html",
    "pages/my_calendar.html",
)


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


# ---- 分区 tab 的 WAI-ARIA tabs 模式 + roving tabindex（服务端渲染初始态） ----
# core.js 运行时同步 aria-selected/tabindex；这里钉的是模板初始结构：
# role/aria 齐全且 roving 初始值正确 —— 缺了会让键盘用户在 JS 生效前（或 JS 失败时）
# 面对一串都进 Tab 键序的分区标签（每页 N 个停止点），读屏也拿不到 tab/tabpanel 语义。
_TAB_TAG_RE = re.compile(r"<a\b[^>]*\bdata-tab-target=\"([^\"]+)\"[^>]*>", re.S)
_TAB_PANEL_RE = re.compile(r"<div\b[^>]*\bdata-tab-id=\"([^\"]+)\"[^>]*>", re.S)
# 设置页整页迁 Vue 后，「深链自管」由 SettingsPage.vue 自己实现（syncTabUrl +
# history.replaceState），不再有服务端模板承接该 legacy 标记（data-tab-url-own）。
SETTINGS_PAGE_VUE = os.path.join(
    BASE, "frontend", "src", "settings", "SettingsPage.vue")


class TabAriaRovingTest(unittest.TestCase):
    def test_tab_partitions_render_aria_tabs_with_roving_tabindex(self):
        """含分区的页面必须以 WAI-ARIA tabs 模式渲染，且 roving tabindex 初始态正确。

        判据（每页）：
          ① 每个 .tab 有 role="tab" 与 aria-controls，指向的 id 真实存在、
             是 role="tabpanel" 且 aria-labelledby 回指该 tab；
          ② roving 初始：未隐藏的 tab 里 tabindex="0" 恰好 1 个，其余（含 hidden）全 -1；
          ③ 这些模板不得带 data-tab-url-own —— 深链自管的设置页已移出本清单（其自管逻辑
             在 SettingsPage.vue，由 test_settings_page_self_manages_deep_link 反查）。
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
            if "data-tab-url-own" in text:
                problems.append(f"  {name}: 不应带 data-tab-url-own（带上后 ?tab= 不随切换同步）")
        if problems:
            self.fail(
                "分区 tab 的 WAI-ARIA/roving 结构不对 —— 模板必须服务端渲染 role=tablist/"
                "tab/tabpanel 与 aria-controls/aria-labelledby，roving tabindex 初始态为"
                "活动 0、其余 -1（core.js 只负责切换后的同步，初始结构缺失会在 JS 生效前"
                "让 Tab 键序里出现多个分区停止点）：\n" + "\n".join(problems)
            )

    def test_settings_page_self_manages_deep_link(self):
        """设置页深链自管的反查锚：自管逻辑必须在 SettingsPage.vue 里，而非 legacy 标记。

        设置页整页迁到 Vue 且**刻意不用** core.js 的 `data-tab-target` 契约（那套有
        document 级点击委托，会先于页面的未保存改动守卫切换分区）。故"自管深链"这条不变量
        改指向组件：切分区时自行写 `?tab=`（history.replaceState），且不引用 legacy 的
        `data-tab-url-own` / `data-tab-target` 标记。
        """
        src = _read(SETTINGS_PAGE_VUE)
        self.assertIn("syncTabUrl(", src, "设置页缺少自管深链写入函数")
        self.assertIn("history.replaceState(", src, "自管深链必须自己写 ?tab= 的 URL")
        self.assertIn("data-settings-tab", src, "设置页应使用自管页签标记")
        self.assertNotIn("data-tab-url-own", src, "自管页签不再借 legacy 的 data-tab-url-own 标记")
        self.assertNotIn("data-tab-target=", src, "自管页签刻意不用 core.js 的 data-tab-target 契约")


if __name__ == "__main__":
    unittest.main()
