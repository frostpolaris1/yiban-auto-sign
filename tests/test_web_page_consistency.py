# -*- coding: utf-8 -*-
"""页面级一致性守卫：含分区页面的 WAI-ARIA tabs 与 roving tabindex 初始态。

标签：F · 前端与界面守卫
覆盖：分区 tab 的服务端渲染初始结构——role/aria 齐全（tab/tabpanel 互指）且 roving 初始值正确（未隐藏 tab 里 tabindex="0" 恰好 1 个、其余含 hidden 全 -1）、data-tab-url-own（深链自管标记）只允许 settings 的分组带
对应实现：`web/templates/pages/*.html` 的 `.tabs` 分区 markup
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
    "pages/work_accounts.html",
    # 2026-10-03：`pages/work_users.html` 移出本清单——该页已迁到 Vue，分区改由组件渲染
    # （且刻意不再使用 core.js 的 data-tab-target 契约，以免两套机制争抢同一批 DOM），
    # 故「服务端初始 ARIA 结构」这条判据对它不再适用（同 data_logs 的处置）。
    "pages/work_settings.html",
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
