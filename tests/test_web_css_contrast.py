# -*- coding: utf-8 -*-
"""WCAG 1.4.11 非文字对比度：承载"状态信息"的图形/界面部件也要 ≥3:1。

标签：F · 前端与界面守卫
覆盖：开关「关」态轨道对卡片底色、白色滑块对轨道的对比度；以及"令牌达标但没人消费"
      这类回退（`.switch .track` 必须真的吃 --switch-off-bg）
对应实现：`web/static/css/app.css` 的 --switch-off-bg 令牌与 `.switch .track` 覆盖；
          底色/原值来自 vendor `web/static/vendor/adminator/adminator.css`
关键断言：只改令牌、或只把覆盖规则改回 vendor 原始态，两种回退都在这里报红
依赖：纯本地——读 CSS 源码文本 + 自行算相对亮度，**不执行 CSS、无需 node**、不联网

> 为什么是「新文件」而不是并进某个存活文件：本仓前端样式守卫此前集中在
> `test_web_text_contrast.py` + `tests/_wcag.py`（对比度算术）与
> `test_web_design_tokens.py`（调色板完整性），三者在缩减批 6c-3-A 按对表一并裁撤。
> 剩下的 `test_web_calendar_parity.py` 是"日历单一实现"守卫、`test_web_js_modules.py`
> 是脚本面守卫，都不是样式令牌的家；硬塞进去会让那两份文件的职责名不副实。
> 本文件只做一件事且**自带算术**（不依赖已删的 `_wcag.py`）：与 6c-3 裁掉的那批
> "精确源码 grep、无行为 owner"的静态扫描不同，令牌一改就重算，不是对着一组
> 过期期望值空过。
"""

import os
import re
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_CSS = os.path.join(BASE, "web", "static", "css", "app.css")
VENDOR_CSS = os.path.join(BASE, "web", "static", "vendor", "adminator", "adminator.css")
TEMPLATES_DIR = os.path.join(BASE, "web", "templates")

AA_NON_TEXT = 3.0  # WCAG 2.1 AA 非文字（图形/界面组件）阈值


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _hex_to_rgb(value):
    m = re.match(r"#([0-9A-Fa-f]{6})\b", value.strip())
    if not m:
        return None
    h = m.group(1)
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _rel_luminance(rgb):
    """WCAG 相对亮度：sRGB 分量先线性化再按 0.2126/0.7152/0.0722 加权。"""
    chan = []
    for c in rgb:
        s = c / 255.0
        chan.append(s / 12.92 if s <= 0.03928 else ((s + 0.055) / 1.055) ** 2.4)
    return 0.2126 * chan[0] + 0.7152 * chan[1] + 0.0722 * chan[2]


def contrast(fg, bg):
    a, b = _rel_luminance(fg), _rel_luminance(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def _tokens(block):
    """从一段 CSS 声明块里取 `--name: #hex` → {'name': (r, g, b)}（键去掉 `--` 前缀）。"""
    out = {}
    for m in re.finditer(r"--([a-z0-9-]+)\s*:\s*(#[0-9A-Fa-f]{6})\b", block):
        rgb = _hex_to_rgb(m.group(2))
        if rgb:
            out[m.group(1)] = rgb
    return out


def _strip_comments(text):
    """去掉 CSS 注释——否则注释会被粘到选择器前面，让选择器匹配失配。"""
    return re.sub(r"/\*.*?\*/", "", text, flags=re.S)


def _blocks(text, selector_re):
    """取某个选择器的**全部**声明块（项目层有多个 `:root`，只看第一个会漏令牌）。"""
    return [m.group(1) for m in re.finditer(selector_re + r"\s*\{([^}]*)\}", text)]


def _merge_tokens(text, selector_re):
    out = {}
    for b in _blocks(text, selector_re):
        out.update(_tokens(b))
    return out


def load_theme_colors():
    """两主题的有效令牌表 = vendor 基线 + app.css 覆盖（后者优先级更高）。

    主题基线（--bg-card 等）在 vendor adminator.css 里按 `:root[data-theme=…]`
    声明；项目自己的语义令牌（--switch-off-bg 等）散在 app.css 的多个 `:root` 与
    `html[data-theme="dark"]` 块里。两处都要读、且要读全，否则拿不到完整的一组。
    """
    vendor = _read(VENDOR_CSS)
    app = _read(APP_CSS)
    out = {}
    for mode, vendor_sel, app_sel in (
        ("light", r":root\[data-theme=light\]", r":root"),
        ("dark", r":root\[data-theme=dark\]", r"html\[data-theme=\"dark\"\]"),
    ):
        table = _merge_tokens(vendor, vendor_sel)
        table.update(_merge_tokens(app, app_sel))
        out[mode] = table
    return out


def _rule_bodies(css, selector):
    """取出项目层里匹配该选择器的规则体（供"是否真的消费令牌"断言）。"""
    css = _strip_comments(css)
    bodies = []
    for m in re.finditer(r"([^{}]+)\{([^}]*)\}", css):
        sels = [s.strip() for s in m.group(1).split(",")]
        if selector in sels:
            bodies.append(m.group(2))
    return bodies


class SwitchNonTextContrastTest(unittest.TestCase):
    """开关关态轨道：WCAG 1.4.11。

    实拍发现（浅色 `05-work-settings.png`、暗色 `D3-settings-schedule.png`）：关态
    轨道几乎看不见。原因是 vendor 的 `.switch .track` 直接吃
    `--bg-muted`(#F1F5F9) + `--border`，对白卡只有 **1.10:1**（深色 1.08:1），
    而"开/关"这个状态**完全**由轨道承载（勾号在滑块上、位置在轨道上）。
    处置：新增语义令牌 `--switch-off-bg`，并让 `.switch .track` 消费它。
    """

    # 白色滑块（vendor `.switch .track::after` 的底色）——滑块落在哪一端是
    # "开/关"的第二个线索，故滑块对轨道也要 ≥3:1，否则移动端看不出滑块位置。
    THUMB = (255, 255, 255)

    def test_switch_off_tokens_meet_non_text_aa(self):
        """令牌本体：两主题下轨道对卡片 ≥3:1、白色滑块对轨道 ≥3:1。"""
        colors = load_theme_colors()
        problems = []
        for mode, label in (("light", "浅色"), ("dark", "暗色")):
            table = colors[mode]
            if "switch-off-bg" not in table:
                problems.append(f"  {label}：缺令牌 --switch-off-bg")
                continue
            if "bg-card" not in table:
                problems.append(f"  {label}：缺底色令牌 --bg-card（vendor adminator.css）")
                continue
            track, card = table["switch-off-bg"], table["bg-card"]
            on_card = contrast(track, card)
            if round(on_card, 2) < AA_NON_TEXT:
                problems.append(
                    f"  {label}：轨道 on 卡片 = {on_card:.2f}:1 < {AA_NON_TEXT}:1"
                    f"（--switch-off-bg {track} vs --bg-card {card}）"
                )
            on_thumb = contrast(self.THUMB, track)
            if round(on_thumb, 2) < AA_NON_TEXT:
                problems.append(
                    f"  {label}：白色滑块 on 轨道 = {on_thumb:.2f}:1 < {AA_NON_TEXT}:1"
                    f"（--switch-off-bg {track}）"
                )
        if problems:
            self.fail("开关关态的非文本对比度不达标：\n" + "\n".join(problems))

    def test_switch_track_consumes_the_token(self):
        """消费点：`.switch .track` 的 **background** 必须真的用 --switch-off-bg。

        防的是"令牌定义了但没人用"——vendor 的 `.switch .track` 仍是
        `--bg-muted` + `--border`（1.10:1），一旦本层这条覆盖被删或被改回，
        令牌再达标也白搭。

        刻意只认 `background` 而不认"规则体里出现过该令牌"：轨道底色才是承载
        开/关状态的那一层，border-color 只是描边。若只查"出现过"，把 background
        单独换回 `--bg-muted`、留着 border-color 引用令牌就能蒙混过关。
        """
        bodies = _rule_bodies(_read(APP_CSS), ".switch .track")
        self.assertTrue(
            bodies,
            "app.css 里找不到 `.switch .track` 规则 —— 关态轨道的覆盖被删了？"
            "vendor 原值对卡片仅 1.10:1，会回流出不达标的关态。",
        )
        uses = [b for b in bodies
                if re.search(r"(?<!-)\bbackground\s*:[^;}]*var\(\s*--switch-off-bg\s*\)", b)]
        if not uses:
            self.fail(
                "`.switch .track` 的 background 未消费 --switch-off-bg：\n  "
                + "\n  ".join(f".switch .track {{ {b.strip()} }}" for b in bodies)
                + "\n修法：`.switch .track { background: var(--switch-off-bg);"
                " border-color: var(--switch-off-bg); }`"
            )


class SkeletonScopeTest(unittest.TestCase):
    """组件尺寸规则不得挂在页级祖先下（加载态可见性）。

    `.dash-skel` 的尺寸规则原写作 `.dash-page .dash-skel`，而 work_accounts.html /
    work_settings.html 也用这个骨架类、却没有 `.dash-page` 祖先 → 那两页加载期
    的占位 span 保持 `display:inline`，width/height 全部失效（浏览器实测
    getBoundingClientRect().width === 0），**页面上看不到任何加载指示**。
    用户看到的是"页面空着"，容易重复点击（WCAG/可用性上都算缺反馈）。
    收口到裸类名后与祖先无关；`.dash-page` 内的表现不变（同一声明）。
    """

    def _users_of(self, cls):
        out = []
        for dirpath, _dirs, files in os.walk(TEMPLATES_DIR):
            for fn in files:
                if not fn.endswith(".html"):
                    continue
                path = os.path.join(dirpath, fn)
                with open(path, encoding="utf-8") as fh:
                    text = fh.read()
                if cls in text:
                    out.append((path, "dash-page" in text))
        return out

    def test_skeleton_size_rule_not_scoped_to_page_ancestor(self):
        css = _strip_comments(_read(APP_CSS))
        self.assertFalse(
            _rule_bodies(css, ".dash-page .dash-skel"),
            "`.dash-page .dash-skel` 回潮了 —— 它只对数据总览页生效，"
            "账号页/设置页的骨架会再次退化成零尺寸、加载期毫无占位",
        )
        self.assertTrue(
            _rule_bodies(css, ".dash-skel"),
            "找不到裸类名 `.dash-skel` 的尺寸规则 —— 组件层必须有一条与祖先无关的声明",
        )

    def test_skeleton_class_is_not_orphaned(self):
        """`.dash-skel` 至少还有一个模板在用——否则上面那条规则就是死 CSS。

        与「删死代码」同一方向：规则与使用者必须成对存在，只剩其一时报红，
        避免留下"看着在管事、实际没人消费"的样式。
        """
        users = self._users_of("dash-skel")
        self.assertTrue(
            users,
            "没有任何模板使用 .dash-skel —— app.css 里那条骨架尺寸规则已成死样式，"
            "应连同本类一并删除（而不是留着占位）",
        )


if __name__ == "__main__":
    unittest.main()
