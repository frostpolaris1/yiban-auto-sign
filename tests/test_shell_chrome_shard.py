# -*- coding: utf-8 -*-
"""core.js 拆壳的分片契约守卫（退役账「core.js 拆壳」，第一刀＋第二刀）。

## 拆出来的形状

`web/static/js/shell-chrome.js` 承载外壳**外观与外壳交互**族。按批分两刀：
  · 第一刀：主题 / 抽屉 / 服务器时钟 / 慢请求交换 / 显示偏好；
  · 第二刀（2026-10-24）：下拉菜单 / 导航分组（rail 浮层）/ 回到顶部 /
    顶部导航进度条。

`web/static/js/core.js` 保留请求层、缓存、模态栈、口令策略与受门禁提交、身份、
导航徽标、公告、分区 tab 深链。两者的载入顺序是**硬契约**：外观层必须先载入，
core.js 在 IIFE 入口捕获 `window.YB.__chrome`，并把基础工具经
`window.YB_INTERNALS` 借给它（分片的函数在**调用时**经它取 el/svgUse 等）。

## 为什么顺序不能反（这条是本刀的设计前提）

defer 脚本执行时 `document.readyState` 已是 `"interactive"`（规范：解析结束→置
interactive→跑 defer 脚本→再发 DOMContentLoaded）。core.js 的 `onReady` 因此在
**core.js 执行期间就同步跑完初始化**（主题按钮绑定、时钟校准与每秒重绘、抽屉/导航
resize 监听、进度条接续都在这时挂上）。那一刻若外观层还没载入，
`window.YB.__chrome` 不存在 —— core.js 会抛错（**响亮失败**，不是静默降级：
主题点了没反应这类失效看不见）。

## 判据（不变式）

  1. 三个外壳模板都载入 `shell-chrome.js`，且它**紧邻排在** `core.js` 之前；
  2. 外观层导出全部被转挂的能力（两刀合计 24 个名字，core.js 的公开面/调用点依赖）；
  3. core.js **不再**自己定义这些能力（避免两份实现各自漂移）；
  4. core.js 在缺分片时抛错（不许静默降级成空操作）。

守卫自证（实测突变）：
  · 第一刀（2026-10-16，四次）：删模板里的 shell-chrome 标签 ⇒ 红；两标签顺序对调
    ⇒ 红；从出口清单删 `swapOut` ⇒ 红；把缺分片判据改成 `if (false)` ⇒ 红。
  · 第二刀（2026-10-24）：从 shell-chrome.js 的出口清单删 `toggleDropdown` ⇒ 红
    （`test_shard_exports_every_capability` 点名缺项）；把 `focusItem` 的实现抄回
    core.js ⇒ 红（`test_core_no_longer_defines_the_moved_capabilities` 点名该符号）。
"""
import os
import re
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS_DIR = os.path.join(BASE, "web", "static", "js")
TEMPLATES_DIR = os.path.join(BASE, "web", "templates")

CHROME_JS = os.path.join(JS_DIR, "shell-chrome.js")
CORE_JS = os.path.join(JS_DIR, "core.js")

#: 三个外壳模板：外观层的每个消费者。
LAYOUTS = ("layout_admin.html", "layout_user.html", "layout_auth.html")

#: 外观层必须导出的能力（core.js 的公开面 / 调用点按名取用）。
EXPORTS = (
    "currentTheme", "updateThemeIcons", "applyTheme", "toggleTheme", "toggleDrawer",
    "calibrateClock", "renderClock", "serverNow", "clockString", "clockInfo",
    "swapOut", "SWAP_MS", "ownerEmailVisible", "setOwnerEmailVisible",
    # 第二刀（2026-10-24）：下拉菜单 / 导航分组 / 回顶 / 导航进度条。
    "closeDropdowns", "focusItem", "clampDropdown", "clampOpenDropdowns", "toggleDropdown",
    "isRailMode", "positionRailFlyout", "relayoutRail", "initBackToTop", "initNavProgress",
)

#: core.js 里**不许**再出现的实现痕迹（第二份实现会各自漂移）。
CORE_MUST_NOT_DEFINE = (
    "function toggleTheme(", "function applyTheme(", "function currentTheme(",
    "function updateThemeIcons(", "function toggleDrawer(", "function swapOut(",
    "function calibrateClock(", "function renderClock(", "function serverNow(",
    "function clockString(", "function clockInfo(", "function ownerEmailVisible(",
    "function setOwnerEmailVisible(", "var SWAP_MS =",
    # 第二刀：搬家后不许在 core.js 重新长出这四族。
    "function closeDropdowns(", "function toggleDropdown(", "function clampDropdown(",
    "function clampOpenDropdowns(", "function focusItem(",
    "function isRailMode(", "function relayoutRail(", "function positionRailFlyout(",
    "function initBackToTop(", "function initNavProgress(", "var NAV_PROGRESS_KEY",
    "var TO_TOP_AT",
)

_CHROME_TAG = re.compile(
    r'<script defer src="\{\{ request\.script_root \}\}/static/js/shell-chrome\.js\?v=\{\{ web_version \}\}"></script>'
)
_CORE_TAG = re.compile(
    r'<script defer src="\{\{ request\.script_root \}\}/static/js/core\.js\?v=\{\{ web_version \}\}"></script>'
)


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class ShellChromeShardTest(unittest.TestCase):
    def test_every_layout_loads_the_chrome_shard_immediately_before_core(self):
        problems = []
        for name in LAYOUTS:
            src = _read(os.path.join(TEMPLATES_DIR, name))
            m_chrome, m_core = _CHROME_TAG.search(src), _CORE_TAG.search(src)
            if not m_chrome:
                problems.append(f"  {name}: 未载入 shell-chrome.js")
                continue
            if not m_core:
                problems.append(f"  {name}: 未载入 core.js（守卫锚点失效，请同步本测试）")
                continue
            if m_chrome.start() > m_core.start():
                problems.append(f"  {name}: shell-chrome.js 排在 core.js 之后（顺序反了）")
            between = src[m_chrome.end():m_core.start()]
            if between.strip():
                problems.append(f"  {name}: shell-chrome 与 core 之间夹了别的脚本（顺序契约变脆）")
        if problems:
            self.fail(
                "外壳外观层与 core.js 的载入契约被破坏 —— core.js 会在缺分片时抛错：\n"
                + "\n".join(problems)
            )

    def test_shard_exports_every_capability(self):
        src = _read(CHROME_JS)
        missing = [n for n in EXPORTS if f"X.{n} =" not in src]
        self.assertEqual(missing, [],
                         "shell-chrome.js 缺少出口（core.js 公开面会拿到 undefined）：%r" % (missing,))

    def test_core_no_longer_defines_the_moved_capabilities(self):
        core = _read(CORE_JS)
        offenders = [m for m in CORE_MUST_NOT_DEFINE if m in core]
        self.assertEqual(
            offenders, [],
            "core.js 又长出了外壳外观层的第二份实现（两份会各自漂移）：%r" % (offenders,),
        )

    def test_core_fails_loudly_without_the_shard(self):
        core = _read(CORE_JS)
        self.assertIn("shell-chrome.js 未载入", core,
                      "core.js 缺分片时必须响亮失败，不许静默降级成空操作")
        self.assertRegex(core, r"if \(!CHROME\) \{",
                         "core.js 必须显式判缺分片（判据形状变了？请同步本测试）")


if __name__ == "__main__":
    unittest.main()
