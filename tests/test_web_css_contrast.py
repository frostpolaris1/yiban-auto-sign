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


def _color_decls(css, selector):
    """取匹配 selector 的各规则体里的 `color:` 值（去空白，便于比对）。"""
    out = []
    for body in _rule_bodies(css, selector):
        for m in re.finditer(r"(?<!-)\bcolor\s*:\s*([^;}]+)", body):
            out.append(re.sub(r"\s+", "", m.group(1)))
    return out


def _decls(css, selector, prop):
    out = []
    for body in _rule_bodies(css, selector):
        for m in re.finditer(r"(?<![\w-])" + prop + r"\s*:\s*([^;}]+)", body):
            out.append(re.sub(r"\s+", "", m.group(1)))
    return out


CORE_JS = os.path.join(BASE, "web", "static", "js", "core.js")
LAYOUTS = (
    os.path.join(TEMPLATES_DIR, "layout_admin.html"),
    os.path.join(TEMPLATES_DIR, "layout_user.html"),
    os.path.join(TEMPLATES_DIR, "layout_auth.html"),
)


class BatchAContrastTokenTest(unittest.TestCase):
    """批次 A（WCAG 硬标准）的令牌消费点：正文/图形必须吃达标的 AA 令牌。

    这些位置此前停在 --t-light / --t-muted（实测 1.93–4.48:1 不等，低于正文 4.5:1
    或非文本 3:1），或根本没被声明（placeholder、.req、.set-ok）。把「哪个选择器该吃哪个
    令牌」钉成断言：令牌本身达标由下面的算术测试保证，这里保证**有人真的消费它**，
    防止回退到浅档或删规则时静默失守。
    """

    # 说明级正文：--t-sub（浅 #475569）是项目为「浅底上达 AA」定的辅助文字档。
    TEXT_SUB = (
        ".auth-aside-eyebrow",
        ".auth-aside-footer",
        ".auth-aside p",
        ".auth-points li",
        ".auth-switch-hint",
        ".auth-card .sub",
        ".panel-sub",
        ".account-meta",
        ".account-note",
        ".range-tick",
    )

    def test_sub_text_rules_use_t_sub(self):
        css = _strip_comments(_read(APP_CSS))
        bad = [s for s in self.TEXT_SUB if "var(--t-sub)" not in _color_decls(css, s)]
        self.assertFalse(bad, f"这些选择器未用 --t-sub（浅底上不达 AA）：{bad}")

    def test_icon_controls_drop_t_light(self):
        css = _strip_comments(_read(APP_CSS))
        for sel in (".input-affix .affix-btn", ".alert.auth-announce .close", ".pm-panel .pm-panel-close"):
            decls = _color_decls(css, sel)
            self.assertTrue(decls, f"{sel} 找不到 color 声明 —— 图标按钮退回 vendor 的 --t-light？")
            self.assertNotIn("var(--t-light)", decls, f"{sel} 又用回 --t-light（2.2–2.6:1 < 3:1）")
            self.assertIn("var(--t-muted)", decls, f"{sel} 未用 --t-muted（承载动作的图形需 ≥3:1）")

    def test_req_star_uses_state_bad(self):
        css = _strip_comments(_read(APP_CSS))
        decls = _color_decls(css, ".field-label .req")
        self.assertIn(
            "var(--state-bad-fg)", decls,
            "必填星号未换 --state-bad-fg —— vendor 的 --danger 在白卡上仅 3.76:1，不到正文 AA",
        )

    def test_placeholder_global_rule(self):
        css = _strip_comments(_read(APP_CSS))
        for sel in (".input::placeholder", ".select::placeholder", ".textarea::placeholder"):
            body = "".join(_rule_bodies(css, sel))
            self.assertIn("var(--t-muted)", re.sub(r"\s+", "", body),
                          f"{sel} 未收进全局 placeholder 规则（vendor 的 --t-light 仅 2.44:1）")
            self.assertIn("opacity:1", re.sub(r"\s+", "", body),
                          f"{sel} 缺 opacity:1 —— 浏览器半透明会把颜色再压暗一档")
        # 任何 ::placeholder 规则都不得再出现 --t-light
        for m in re.finditer(r"[^{}]*::placeholder[^{}]*\{([^}]*)\}", css):
            self.assertNotIn("--t-light", m.group(1), "仍有 placeholder 用 --t-light（不达正文 AA）")

    def test_set_ok_rule_exists_and_consumes_token(self):
        css = _strip_comments(_read(APP_CSS))
        decls = _color_decls(css, ".set-ok")
        self.assertIn(
            "var(--state-ok-fg)", decls,
            "缺 .set-ok 规则（或未消费 --state-ok-fg）—— 成功态就地提示会渲染成普通正文",
        )

    def test_dashboard_pills_and_card_action(self):
        css = _strip_comments(_read(APP_CSS))
        cases = {
            ".dash-page .kpi-pill.info": ("var(--badge-info-fg)", "var(--badge-info-bg)"),
            ".dash-page .kpi-pill.up": ("var(--badge-ok-fg)", "var(--badge-ok-bg)"),
            ".dash-page .kpi-pill.down": ("var(--badge-bad-fg)", "var(--badge-bad-bg)"),
            ".dash-page .kpi-pill.flat": ("var(--badge-muted-fg)", "var(--badge-muted-bg)"),
        }
        for sel, (fg, bg) in cases.items():
            self.assertIn(fg, _color_decls(css, sel), f"{sel} 未接 AA 徽标前景令牌")
            self.assertIn(bg, _decls(css, sel, "background"), f"{sel} 未接 AA 徽标底色令牌")
        self.assertIn(
            "var(--t-muted)", _color_decls(css, ".dash-page .card-action"),
            ".card-action 未降回辅助文字档 —— vendor 的链接蓝让它看起来可点",
        )

    def test_unread_dot_is_not_bare_color(self):
        """未读圆点：承载「有未读」的图形必须过非文本 3:1，且不能只靠一颗贴纸色点。"""
        css = _strip_comments(_read(APP_CSS))
        sel = ".icon-btn .count[data-announcement-dot]"
        self.assertIn("var(--primary-solid)", _decls(css, sel, "background"),
                      f"{sel} 未用 --primary-solid 实心底（#0EA5E9 对顶栏底仅 2.51:1 < 3:1）")
        border = "".join(_decls(css, sel, "border"))
        self.assertIn("2pxsolid", border, f"{sel} 未加 2px 背景色描边，圆点仍糊在铃身笔画里")

    def test_token_pairs_meet_aa_arithmetic(self):
        """令牌本体按 WCAG 相对亮度重算，两套主题都过线（不依赖任何固定期望值）。"""
        colors = load_theme_colors()
        problems = []
        for mode, label in (("light", "浅色"), ("dark", "暗色")):
            t = colors[mode]
            need = {
                ("t-muted", "bg-card"): AA_NON_TEXT,
                ("t-sub", "bg-card"): 4.5,
                ("t-sub", "bg-body"): 4.5,
                ("badge-info-fg", "badge-info-bg"): 4.5,
                ("badge-ok-fg", "badge-ok-bg"): 4.5,
                ("badge-bad-fg", "badge-bad-bg"): 4.5,
                ("badge-warn-fg", "badge-warn-bg"): 4.5,
                ("badge-muted-fg", "badge-muted-bg"): 4.5,
                ("state-ok-fg", "bg-card"): 4.5,
                ("state-bad-fg", "bg-card"): 4.5,
                # 日历格：底色即状态（五档），日期数字必须压得住每一档底
                ("cal-ok-fg", "cal-ok-bg"): 4.5,
                ("cal-bad-fg", "cal-bad-bg"): 4.5,
                ("cal-off-fg", "cal-off-bg"): 4.5,
                ("cal-muted-fg", "cal-muted-bg"): 4.5,
                ("cal-warn-fg", "cal-warn-bg"): 4.5,
                ("cal-busy-fg", "cal-busy-bg"): 4.5,
                ("cal-busy-fg", "cal-busy-hi"): 4.5,   # 呼吸全程（含白端）都可读
            }
            for (fg, bg), floor in need.items():
                if fg not in t or bg not in t:
                    problems.append(f"  {label}：缺令牌 {fg} 或 {bg}")
                    continue
                cr = contrast(t[fg], t[bg])
                if round(cr, 2) < floor:
                    problems.append(
                        f"  {label}：{fg} on {bg} = {cr:.2f}:1 < {floor}:1"
                        f"（{t[fg]} vs {t[bg]}）"
                    )
        if problems:
            self.fail("AA 令牌对不达标：\n" + "\n".join(problems))


class BatchCFocusAndLiveTest(unittest.TestCase):
    """批次 C（焦点与键盘）：单一 polite 播报区 + 焦点移交/归还的守卫。

    实拍与源码复核发现：toast 容器 aria-live 与每颗 toast 的 role=status 叠成两层 live
    region（重复播报）；弹窗初始焦点恒落在右上角 ✕（文档序第一颗）；下拉打开不移入焦点、
    关闭不归还（行内菜单 portal 后还原成 display:none，焦点落回 body）；统一焦点环白名单
    漏了登录页控件与可聚焦的头像。这里只钉「形态」，行为由组件自身承担。
    """

    def test_modal_initial_focus_prefers_body_control(self):
        js = _read(CORE_JS)
        self.assertIn('querySelector(".modal-body")', js,
                      "openModal 未把初始焦点限定到正文 —— 会重新落回右上角 ✕")
        self.assertIn("focusables(scope)[0]", js,
                      "openModal 未优先取正文里的可操作控件")
        self.assertIn('initialFocus: ".modal-foot .btn--ghost"', js,
                      "confirmDialog 未把初始焦点放到「取消」安全侧")

    def test_dropdown_focus_move_in_and_return(self):
        js = _read(CORE_JS)
        # 关闭：焦点在菜单里时归还触发器
        close_body = js[js.index("function closeDropdowns"):js.index("function focusItem")]
        self.assertIn("activeElement", close_body, "closeDropdowns 不看焦点位置就关，无法归还")
        self.assertIn(".focus()", close_body, "closeDropdowns 未归还焦点到触发器")
        # 打开：焦点移入菜单
        toggle_body = js[js.index("function toggleDropdown"):]
        toggle_body = toggle_body[:toggle_body.index("\n  }")]
        self.assertIn(".dd-menu-item", toggle_body, "toggleDropdown 打开后未聚焦首个菜单项")
        self.assertIn(".focus()", toggle_body, "toggleDropdown 打开后未移入焦点")

    def test_single_polite_live_region_for_toasts(self):
        js = _read(CORE_JS)
        host_body = js[js.index("function toastHost"):js.index("function dismissToast")]
        self.assertIn('role: "status"', host_body, "toast-host 不是单一 role=status 播报区")
        # 单颗 toast 不得再自带 live 语义
        node_line = next(ln for ln in js.splitlines() if "toast toast--" in ln)
        self.assertNotIn('role: "status"', node_line,
                         "单颗 toast 仍带 role=status —— 与容器叠成两层 live region")
        for path in LAYOUTS:
            html = _read(path)
            line = [ln for ln in html.splitlines() if 'id="toast-host"' in ln]
            self.assertTrue(line, f"{os.path.basename(path)} 缺 toast-host 挂载点")
            self.assertIn('role="status"', line[0], f"{os.path.basename(path)} 的 toast-host 缺 role=status")
            self.assertIn('aria-atomic="false"', line[0],
                          f"{os.path.basename(path)} 的 toast-host 仍是 aria-atomic=true（多颗会整块重念）")

    def test_focus_ring_whitelist_covers_login_and_topbar(self):
        css = _strip_comments(_read(APP_CSS))
        m = re.search(r":where\((.*?)\):focus-visible", css, re.S)
        self.assertTrue(m, "找不到统一焦点环白名单规则")
        whitelist = m.group(1)
        for sel in (".avatar", ".input", ".input-affix .affix-btn", ".alert .close",
                    ".auth-switch-hint button", ".auth-agree-text button", ".auth-links a"):
            self.assertIn(sel, whitelist, f"焦点环白名单漏了 {sel}（键盘焦点不可见）")

    def test_sticky_actions_not_obscuring_on_narrow(self):
        """窄屏吸底操作行会齐边盖住上一块内容（WCAG 2.4.11）。720 内必须取消吸底。"""
        css = _strip_comments(_read(APP_CSS))
        found = False
        for m in re.finditer(r"@media\s*\(\s*max-width:\s*720px\s*\)\s*\{", css):
            depth, i = 1, m.end()
            while i < len(css) and depth:
                if css[i] == "{":
                    depth += 1
                elif css[i] == "}":
                    depth -= 1
                i += 1
            block = css[m.end():i - 1]
            if ".form-actions.is-sticky" in block and "position:static" in block.replace(" ", ""):
                found = True
                break
        self.assertTrue(
            found,
            "≤720 未取消 .form-actions.is-sticky 吸底 —— 不透明底衬会切掉上一整块内容并遮挡焦点",
        )


def _media_bodies(css, media_re):
    """取匹配 media_re 的 @media 块**完整主体**（花括号配平，允许块内含多条规则）。"""
    out = []
    for m in re.finditer(media_re, css):
        depth, i = 1, m.end()
        while i < len(css) and depth:
            if css[i] == "{":
                depth += 1
            elif css[i] == "}":
                depth -= 1
            i += 1
        out.append(css[m.end():i - 1])
    return out


def _bare_hover_selectors(css):
    """不在任何 (hover: …) 或 reduced-motion 守卫内的 `:hover` 选择器。

    触屏没有 hover：一条裸的 `X:hover { … }` 在移动端要么永不生效（装饰性），
    要么把"只有悬停才看得到"的信息饿死（功能性）。两类都必须包进
    `@media (hover: hover) and (pointer: fine)`（或本就在 reduced-motion 覆盖里，
    那类规则是移除动效、不依赖指针）。用花括号配平跟踪媒体嵌套，注释先剥掉。
    """
    text = _strip_comments(css)
    out, stack, depths = [], [], []
    depth, buf = 0, []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == "{":
            sel = "".join(buf).strip()
            buf = []
            depth += 1
            if sel.lstrip().startswith("@media"):
                stack.append(sel)
                depths.append(depth)
            else:
                out.append((sel, list(stack)))
        elif c == "}":
            if depths and depth == depths[-1]:
                stack.pop()
                depths.pop()
            depth -= 1
            buf = []
        else:
            buf.append(c)
        i += 1
    return [
        sel.replace("\n", " ").strip()
        for sel, ctx in out
        if ":hover" in sel
        and not any(("hover" in m) or ("reduced-motion" in m) for m in ctx)
    ]


class TouchAndMotionRegressionTest(unittest.TestCase):
    """批次 B（触屏命中区 / 裸 hover）与批次 H（自研控件动效）+ 批次 E（忙碌态）回归。

    这些是"包媒体查询"改动最容易静默回退的地方：hover 包了但信息只留给 hover、
    命中区名单写了死类名、`transform` 没进 transition、退场动效漏进 reduce ——
    静态文本断言即可钉住，不需要跑浏览器。
    """

    def test_no_bare_hover_selectors(self):
        """任何 `:hover` 增强都必须有指针/减动效守卫，否则触屏用户会被饿死。"""
        bare = _bare_hover_selectors(_read(APP_CSS))
        self.assertEqual(
            bare, [],
            "这些 `:hover` 规则没有包进 @media (hover: hover) and (pointer: fine)：\n  "
            + "\n  ".join(bare),
        )

    def test_touch_block_uses_real_class_names(self):
        """触屏 44px 命中区名单不得再出现全库不存在的类名（旧版空转的根因）。"""
        css = _strip_comments(_read(APP_CSS))
        blocks = [b for b in _media_bodies(css, r"@media\s*\(\s*hover:\s*none\s*\)\s*\{")]
        self.assertTrue(blocks, "找不到 @media (hover: none) 触屏命中区块")
        joined = "".join(blocks)
        for dead in (".auth-agree-label", ".announce-close"):
            self.assertNotIn(dead, joined, f"触屏命中区名单里仍是死类名 {dead}")
        for real in (".auth-switch-hint button", ".auth-agree-text button",
                     ".alert.auth-announce .close", ".info-tip", ".sc-nav-btn"):
            self.assertIn(real, joined, f"触屏命中区漏了真实目标 {real}")

    def test_self_made_triggers_have_transform_transition_and_pressed_state(self):
        """四类自研触发器的 transition 必须含 transform，并有一条 :active 按压态。"""
        css = _strip_comments(_read(APP_CSS))
        for sel in (".select-trigger", ".range-trigger", ".time-trigger", ".date-trigger"):
            bodies = _rule_bodies(css, sel)
            self.assertTrue(any("transform" in b for b in bodies),
                            f"{sel} 的 transition 未含 transform（按压态无法过渡）")
            active = _rule_bodies(css, sel + ":active")
            self.assertTrue(any("scale(" in b for b in active),
                            f"{sel}:active 缺按压位移（scale）")

    def test_declaration_popups_have_exit_animation(self):
        """`.select-menu` / `.date-pop` 的退场必须与进场同参数反向，且被 reduce 关停。"""
        css = _strip_comments(_read(APP_CSS))
        for sel in (".select-menu.is-closing", ".date-pop.is-closing"):
            bodies = _rule_bodies(css, sel)
            self.assertTrue(bodies, f"{sel} 缺失 —— 展开是淡入、收起仍是硬跳")
            self.assertIn("animation", "".join(bodies), f"{sel} 未声明退场 animation")
        # 退场关键帧必须存在
        self.assertIn("@keyframes select-menu-out", css)
        # reduce 下必须关停退场（与进场同处一块；.select-menu 与 .date-pop 各在自己的 reduce 块）
        reduce_blocks = _media_bodies(
            css, r"@media\s*\(\s*prefers-reduced-motion:\s*reduce\s*\)\s*\{")
        self.assertTrue(
            any(".select-menu.is-closing" in b for b in reduce_blocks)
            and any(".date-pop.is-closing" in b for b in reduce_blocks),
            "prefers-reduced-motion 分支未同时关停 .select-menu/.date-pop 的退场动画",
        )
        # 触发器按压位移也要在 reduce 里归零
        self.assertTrue(
            any(".select-trigger:active" in b and "transform:none" in b.replace(" ", "")
                for b in reduce_blocks),
            "prefers-reduced-motion 分支未归零触发器按压位移",
        )

    def test_theme_crossfade_single_duration_token(self):
        """整页换肤的表面/文字/深色极光必须共用同一个时长令牌（三种时长曾不同步）。"""
        css = _strip_comments(_read(APP_CSS))
        self.assertIn("--theme-dur:", css, "缺单一换肤时长令牌 --theme-dur")
        for stale in ("--theme-dur-surface", "--theme-dur-text"):
            self.assertNotIn(stale, css, f"换肤时长未收敛：仍存在 {stale}")
        # 深色极光覆盖层的过渡也吃同一令牌
        before = _rule_bodies(css, ".auth-aside::before")
        self.assertTrue(any("var(--theme-dur)" in b for b in before),
                        ".auth-aside::before 的交叉淡入未消费 --theme-dur")

    def test_btn_busy_ring_is_centered_not_inline(self):
        """`.btn.is-busy` 的环必须绝对定位居中、标签透明占位 —— 否则整组重新居中、标签右跳。

        旧实现把 12px 环 + 6px margin 作为首个 flex 项，标签右移约 9px，与
        「宽度不变、标签留在原位」的注释相反。断言实现形态，防止回退。
        """
        css = _strip_comments(_read(APP_CSS))
        busy = "".join(_rule_bodies(css, ".btn.is-busy"))
        self.assertIn("transparent", busy, ".btn.is-busy 未让标签透明占位")
        before = "".join(_rule_bodies(css, ".btn.is-busy::before"))
        self.assertIn("position:absolute", before.replace(" ", ""),
                      ".btn.is-busy::before 不是绝对定位 —— 会参与 flex 排版把标签挤走")
        self.assertNotIn("margin-right", before,
                         ".btn.is-busy::before 仍有 margin-right（旧的行内占位写法）")
        # 减动效下环保持静态可见（现有行为不回退）
        reduce_blocks = _media_bodies(
            css, r"@media\s*\(\s*prefers-reduced-motion:\s*reduce\s*\)\s*\{")
        self.assertTrue(
            any(".btn.is-busy::before" in b and "animation:none" in b.replace(" ", "")
                for b in reduce_blocks),
            "prefers-reduced-motion 未把忙碌态环改为静态环",
        )


def _px(css, selector, prop):
    """取匹配选择器第一条声明块里的 `<prop>: Npx` 数值（用于同心圆角算术）。"""
    for body in _rule_bodies(css, selector):
        m = re.search(r"(?<![\w-])" + prop + r"\s*:\s*([0-9.]+)px", body)
        if m:
            return float(m.group(1))
    return None


class BatchDControlOutlineTest(unittest.TestCase):
    """批次 D（视觉一致性）：控件描边令牌、同心圆角链、图标线宽收口。

    实拍与源码复核：vendor 的 --border(#E4E8EF / #222C42) 对白/深卡仅 1.10–1.28:1，
    而输入框与四类自研触发器的边框是"这里能点、能填"的唯一线索（WCAG 1.4.11 需 ≥3:1），
    .btn--danger-ghost 的半透明红描边更低（1.87:1）。这里把"令牌存在且达标"与
    "有人真的消费它"都钉住 —— 只定义令牌、或把覆盖删回 vendor 都会报红。
    """

    def test_control_border_tokens_meet_non_text_aa(self):
        colors = load_theme_colors()
        problems = []
        for mode, label in (("light", "浅色"), ("dark", "暗色")):
            t = colors[mode]
            for tok in ("control-border", "danger-border"):
                if tok not in t:
                    problems.append(f"  {label}：缺令牌 --{tok}")
                    continue
                if "bg-card" not in t:
                    problems.append(f"  {label}：缺底色令牌 --bg-card")
                    continue
                cr = contrast(t[tok], t["bg-card"])
                if round(cr, 2) < AA_NON_TEXT:
                    problems.append(
                        f"  {label}：--{tok} on --bg-card = {cr:.2f}:1 < {AA_NON_TEXT}:1"
                        f"（{t[tok]} vs {t['bg-card']}）"
                    )
        if problems:
            self.fail("控件描边令牌不达标（WCAG 1.4.11）：\n" + "\n".join(problems))

    def test_control_outline_consumers_use_token(self):
        css = _strip_comments(_read(APP_CSS))
        for sel in (".select-trigger", ".range-trigger", ".time-trigger", ".date-trigger",
                    ".input", ".select", ".textarea", ".input-group"):
            decls = _decls(css, sel, "border-color") + _decls(css, sel, "border")
            self.assertIn(
                "var(--control-border)", "".join(decls),
                f"{sel} 未吃 --control-border —— 描边退回 vendor 的 --border（约 1.1–1.3:1）",
            )

    def test_danger_ghost_border_uses_semantic_token(self):
        css = _strip_comments(_read(APP_CSS))
        bodies = "".join(_rule_bodies(css, ".btn--danger-ghost"))
        self.assertIn("var(--danger-border)", bodies,
                      ".btn--danger-ghost 描边未走 --danger-border（旧 rgba 红对白卡仅 1.87:1）")
        self.assertIn("var(--state-bad-fg)", bodies,
                      ".btn--danger-ghost 文字未走 --state-bad-fg（旧字面量 #B91C1C 绕开了令牌）")
        self.assertNotIn("#B91C1C", bodies, ".btn--danger-ghost 仍有字面量 #B91C1C")

    def test_select_disabled_root_has_style(self):
        css = _strip_comments(_read(APP_CSS))
        self.assertTrue(
            _rule_bodies(css, ".select-field.is-disabled"),
            "单选下拉的 .is-disabled 根没有任何样式（多选有 opacity:.6，单选是空操作）",
        )
        self.assertTrue(_rule_bodies(css, ".multiselect-field.is-disabled"),
                        "多选下拉的禁用样式被删了")

    def test_concentric_radius_chains(self):
        """内层圆角必须 = 外层圆角 − 外层 padding（否则内层"顶"出外弧、读作两个盒子）。"""
        css = _strip_comments(_read(APP_CSS))
        chains = (
            (".select-menu", ".select-option"),
            (".date-pop", ".date-day"),
            (".auth-tabs.auth-seg", ".auth-tabs.auth-seg .tab"),
        )
        problems = []
        for outer, inner in chains:
            ro = _px(css, outer, "border-radius")
            pi = _px(css, outer, "padding")
            ri = _px(css, inner, "border-radius")
            if None in (ro, pi, ri):
                problems.append(f"  {outer} / {inner}：取不到值 radius/padding = {ro}/{pi}，内层 {ri}")
                continue
            if abs(ri - (ro - pi)) > 0.01:
                problems.append(
                    f"  {outer} {ro}px − padding {pi}px = {ro - pi}px ≠ {inner} {ri}px"
                )
        self.assertFalse(
            problems,
            "同心圆角链不符（内层应 = 外层 − padding）：\n" + "\n".join(problems),
        )

    def test_icon_stroke_is_tokenized(self):
        css = _strip_comments(_read(APP_CSS))
        self.assertIn("--icon-stroke:", css, "缺图标描边统一令牌 --icon-stroke")
        literals = re.findall(r"stroke-width\s*:\s*(?!var\()([0-9.]+)", css)
        self.assertFalse(
            literals,
            f"仍有字面量 stroke-width（应统一走 var(--icon-stroke)）：{sorted(set(literals))}",
        )
        producers = re.findall(r"stroke-width\s*:\s*var\(\s*--icon-stroke\s*\)", css)
        self.assertGreaterEqual(
            len(producers), 15,
            f"消费 --icon-stroke 的规则只有 {len(producers)} 条 —— 收口被部分回退？",
        )


class AnnouncementUnreadStateTest(unittest.TestCase):
    """G05#8 余项：公告未读的「已读态 + 第二载体（数字徽标/aria-label）」。

    实拍：蓝点随公告文本有无切换（core.js 只写 dot.hidden = !text），点开读完后圆点
    仍挂着，读屏也拿不到任何未读信息。这里钉住三重载体与已读记录的存在。
    """

    def test_read_state_and_second_carriers(self):
        js = _read(CORE_JS)
        self.assertIn("yiban-announce-read", js, "core.js 缺公告已读记录键")
        self.assertIn("markAnnouncementRead", js, "core.js 缺标记已读的函数")
        self.assertIn("data-announcement-count", js, "core.js 未回填数字徽标 second carrier")
        self.assertIn("1 条未读", js, "铃铛 aria-label 未写入未读数")
        for rel in ("partials/topbar.html", "partials/topbar_user.html"):
            html = _read(os.path.join(TEMPLATES_DIR, rel))
            self.assertIn("data-announcement-count", html,
                          f"{rel} 缺数字徽标挂点（未读只有颜色一个载体）")


if __name__ == "__main__":
    unittest.main()
