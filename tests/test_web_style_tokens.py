# -*- coding: utf-8 -*-
"""样式令牌普查守卫（R2.5 D4 打磨包②，来源 docs/refactor/122 §R2.3.3）。

标签：F · 前端与界面守卫
覆盖：项目层样式（`web/static/css/app.css`）与**构建产物**里第一方 CSS
     （`web/static/vue/assets/*.css`，排除 Element Plus 等第三方 chunk）。
对应实现：`:root` 的统一令牌（字号六档 / 控件高三档 / 圆角三档 / 弱化文字 --t-weak /
     表格行高 --table-row-h）；Vue SFC 与 app.css 一律消费令牌。
关键断言：
     ① 令牌定义逐字等于 122 提案的规范值；
     ② 任何裸 `font-size: Npx` 必须落在六档 {11,12,13,16,22}，或 ≥24（展示大数字档，单列
        --fs-metric）；
     ③ 控件选择器上的裸 `height`/`min-height` 必须落在三档 {32,40,44}；
     ④ 表格行高由 `--table-row-h`（44）承载，自研 `.data-table` 与 EP `.el-table` 同源；
     ⑤ 承载信息的文字不得再用 `--t-light`（白底 2.32–2.56:1）——只许两个纯装饰/豁免例外。
下一个改样式的人往令牌外写裸值，这里直接报红。

依赖：纯本地——读 CSS 源码文本（含构建产物），**不执行 CSS、无需 node**、不联网。
"""

import glob
import os
import re
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_CSS = os.path.join(BASE, "web", "static", "css", "app.css")
VUE_ASSETS = os.path.join(BASE, "web", "static", "vue", "assets")

# ---- 122 §R2.3.3 规范值（写死口径；改这里等于改设计决定） ----
FONT_TOKENS = {
    "--fs-display": "22px",
    "--fs-title": "16px",
    "--fs-body": "13px",
    "--fs-label": "12px",
    "--fs-micro": "11px",
    "--fs-metric": "44px",
}
CTL_TOKENS = {
    "--ctl-h-lg": "40px",
    "--ctl-h-md": "32px",
    "--ctl-h-touch": "44px",
}
RADIUS_TOKENS = {
    "--r-ctl": "8px",
    "--r-card": "14px",
    "--r-inner": "10px",
}
TABLE_ROW_H = 44
ALLOWED_FS = {11, 12, 13, 16, 22}
DISPLAY_FS_MIN = 24          # ≥24px 视为展示大数字档（--fs-metric 一类）
ALLOWED_CTL_H = {0, 32, 40, 44}
#: 裸 border-radius 只允许三档单值，加既有合法豁免（圆形 / 胶囊 / 复位 / 输入组单侧）。
ALLOWED_RADIUS = {"8px", "10px", "14px"}
RADIUS_EXEMPT = {"0", "50%", "999px", "0 8px 8px 0"}

#: 控件选择器：主体（最后一个 compound）命中这些类时，其 height/min-height 必须吃三档令牌。
_CTL_SEL_RE = re.compile(r"\.(btn|input|select|textarea|switch|tab|el-input|el-select|el-switch)\b")
#: 纯装饰/豁免例外：--t-light 只许出现在这两个选择器上（页脚分隔符 · / 禁用控件）。
_T_LIGHT_ALLOW = {".auth-links .sep", ".slot--off"}

_COMMENT_RE = re.compile(r"/\*.*?\*/", re.S)
_RULE_RE = re.compile(r"([^{}]+)\{([^{}]*)\}")
_FS_RE = re.compile(r"font-size\s*:\s*([0-9.]+)px")
_RADIUS_RE = re.compile(r"border-radius\s*:\s*([^;{}]+)")
_H_RE = re.compile(r"(?:^|;|\s)(min-height|height)\s*:\s*([0-9.]+)px")
_T_LIGHT_COLOR_RE = re.compile(r"(?<![-\w])color\s*:\s*var\(--t-light\)")
_TOKEN_DEF_RE = re.compile(r"(--[a-z0-9-]+)\s*:\s*([^;{}]+)\s*;")


def _subject(selector):
    """取逗号分隔的每个选择器的主体 compound（最后一个空格/组合器之后的部分）。"""
    for part in selector.split(","):
        part = part.strip()
        if not part or part.startswith("@"):
            continue
        tail = re.split(r"[\s>+~]+", part)[-1]
        if tail:
            yield tail


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _first_party_css():
    """构建产物里第一方 CSS（排除 vendor-*.css 第三方 chunk）。"""
    out = []
    for p in sorted(glob.glob(os.path.join(VUE_ASSETS, "*.css"))):
        name = os.path.basename(p)
        if name.startswith("vendor-"):
            continue
        out.append(p)
    return out


def _rules(text):
    text = _COMMENT_RE.sub("", text)
    for m in _RULE_RE.finditer(text):
        sel = m.group(1).strip()
        body = m.group(2)
        # 跳过 @规则头（@media 等）
        if sel.startswith("@") or not sel:
            continue
        yield sel, body


class StyleTokenCensusTest(unittest.TestCase):
    def setUp(self):
        self.assertTrue(os.path.isfile(APP_CSS), f"缺 app.css：{APP_CSS}")
        self.app = _read(APP_CSS)
        self.built = _first_party_css()
        self.assertTrue(
            self.built,
            f"未找到第一方构建产物 CSS（{VUE_ASSETS}/*.css，排除 vendor-*）——"
            "先跑 `npm run build` 并提交产物")

    def test_tokens_defined_with_canonical_values(self):
        """令牌定义逐字等于 122 提案规范值（改令牌值即设计决定，必须同步改这里的口径）。"""
        defs = {}
        for m in _TOKEN_DEF_RE.finditer(self.app):
            defs.setdefault(m.group(1), m.group(2).strip())
        expected = {}
        expected.update(FONT_TOKENS)
        expected.update(CTL_TOKENS)
        expected.update(RADIUS_TOKENS)
        expected["--table-row-h"] = f"{TABLE_ROW_H}px"
        expected["--t-weak"] = "var(--t-muted)"
        for name, want in expected.items():
            self.assertIn(name, defs, f"缺令牌定义 {name}")
            self.assertEqual(defs[name], want, f"{name} = {defs[name]!r}，应为 {want!r}")

    def test_no_off_token_font_size(self):
        """裸 font-size 必须落在六档或展示大数字档（≥24px）。"""
        offenders = []
        for path in [APP_CSS, *self.built]:
            text = _read(path)
            for m in _FS_RE.finditer(text):
                v = float(m.group(1))
                if v in ALLOWED_FS or v >= DISPLAY_FS_MIN:
                    continue
                offenders.append(f"{os.path.relpath(path, BASE)}: font-size {m.group(1)}px")
        self.assertEqual(offenders, [], "字号越界（不在六档 / 非展示大数字档）：\n" + "\n".join(offenders))

    def test_control_height_uses_tokens(self):
        """控件选择器上的裸 height/min-height 必须落在 {32,40,44}（0 为复位）。"""
        offenders = []
        for sel, body in _rules(self.app):
            for sub in _subject(sel):
                if "::" in sub or not _CTL_SEL_RE.search(sub):
                    continue
                for m in _H_RE.finditer(body):
                    v = float(m.group(2))
                    if v not in ALLOWED_CTL_H:
                        offenders.append(f"{sub} {{ {m.group(1)}: {m.group(2)}px }}")
        self.assertEqual(sorted(set(offenders)), [], "控件高越界（不在 32/40/44 三档）：\n" + "\n".join(sorted(set(offenders))))

    def test_no_off_token_border_radius(self):
        """裸 border-radius 只允许三档单值 {8,10,14}，或既有合法豁免（圆形/胶囊/复位/输入组单侧）。

        消费面普查：app.css 与第一方构建产物（排除 vendor）。var(--r-*) 消费不在此限。
        """
        offenders = []
        for path in [APP_CSS, *self.built]:
            text = _read(path)
            for m in _RADIUS_RE.finditer(text):
                val = " ".join(m.group(1).split())
                if val.startswith("var(") or val in ALLOWED_RADIUS or val in RADIUS_EXEMPT:
                    continue
                offenders.append(f"{os.path.relpath(path, BASE)}: border-radius {val}")
        self.assertEqual(
            offenders, [],
            "圆角越界（不在 8/10/14 三档，也非合法豁免）：\n" + "\n".join(offenders))

    def test_table_row_height_token_consumed(self):
        """两族表（自研 .data-table 与 EP .el-table）行高都由 --table-row-h 承载。"""
        self.assertIn("--table-row-h", self.app)
        joined = "\n".join(
            f"{sel}{{{body}}}" for sel, body in _rules(self.app)
        )
        self.assertRegex(
            joined, r"\.data-table tbody td\s*\{[^}]*var\(--table-row-h\)",
            "自研 .data-table 的行高未消费 --table-row-h")
        self.assertRegex(
            joined, r"\.el-table[^{}]*el-table__cell\s*\{[^}]*var\(--table-row-h\)",
            "EP .el-table 的行高未消费 --table-row-h")

    def test_no_t_light_as_text_color(self):
        """承载信息的文字不得再用 --t-light（只许纯装饰分隔符与禁用控件例外）。"""
        offenders = []
        for sel, body in _rules(self.app):
            if not _T_LIGHT_COLOR_RE.search(body):
                continue
            if sel.strip() in _T_LIGHT_ALLOW:
                continue
            offenders.append(sel.strip())
        self.assertEqual(
            offenders, [],
            "--t-light 当正文用（白底 2.32–2.56:1，低于 AA）：\n" + "\n".join(offenders))


if __name__ == "__main__":
    unittest.main()
