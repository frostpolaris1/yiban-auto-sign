# -*- coding: utf-8 -*-
"""回归守卫：中文字体栈收口（防止中文回退到宋体 SimSun）。

## 为什么需要它

Adminator 在 **58 个组件级选择器**里声明了自己的 `font-family`，且多数以通用族结尾：

    "Inter Tight","Inter",sans-serif   -> .card-title / .kpi-value 等
    "Inter Tight",sans-serif           -> .chart-meta-value / .cal-month 等
    "JetBrains Mono",monospace         -> .chart-meta-label / .brand-tag 等

这些栈里**没有中文字体**，中文会落到通用族 `sans-serif`/`monospace`；中文 Windows 下
通用族由浏览器按系统字体设置解析，实际命中**宋体(SimSun)**。仅覆盖 `body` 不够：
组件级声明优先于继承。

首次修复（2026-09-12）按 Adminator 的选择器逐组重写，把 `"Noto Sans SC"` 插到通用族之前，
形成 `app.css` 里的「正文栈 26 选择器 + 等宽栈 32 选择器」两张收口清单。

**但那次修复漏了项目自己写的声明**：`app.css` 里后加的兼容类（`.d-footer .footer-meta`、
`.sidebar-clock [data-clock-text]`、`.pager__info`、`.log-view`、`.md-body code`）自己声明了
裸字面量栈，绕过了收口清单 —— 实测页脚的「易班自动签到 v…」「开源（AGPL-3.0）· 源码」
两处中文因此回退宋体（版本按钮与开源链接正是 `.footer-meta` 的子元素）。
根因是「字体栈散落在多处字面量」，所以本批改为**单一事实源**：`--font-sans` / `--font-mono`
两个自定义属性，其余一律 `var(--font-*)`。

## 本文件钉住的三件事

1. **令牌本身正确**：`--font-sans` / `--font-mono` 都含 `"Noto Sans SC"`，且排在通用族之前。
2. **没有旁路**：`app.css` / 模板 / 自研 JS 里凡是「以通用族结尾」的 `font-family`，必须**点名一个
   中文字体**（`"Noto Sans SC"` 或 `"Microsoft YaHei"` / `"PingFang SC"` 等系统中文字体）——
   未点名时中文 Windows 会把通用族解析成宋体。旧栈里 `pre.log-text` 就是这种形态（已修）。
   注意判据是「点名了中文字体」而非「必须用 Noto」：系统中文黑体同样是合法兜底，
   否则会把旧栈里本来正确的 `"Microsoft YaHei"` 栈误判为缺陷。
3. **对账不漏收**：`adminator.css` 里**每一个**声明了「以通用族结尾的 font-family」的选择器，
   都必须出现在两张收口清单里。Adminator 升级新增组件时会在这里报红，提示补进清单。
   实测当前为 58/58 全收（2026-09-12）。
"""

import os
import re
import subprocess
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(BASE, "web")
APP_CSS = os.path.join(WEB, "static", "css", "app.css")
ADMINATOR_CSS = os.path.join(WEB, "static", "vendor", "adminator", "adminator.css")

# 需要扫描"不得旁路"的范围（排除 vendor：第三方产物不在本次收口责任内）
SCAN_DIRS = (os.path.join(WEB, "templates"), os.path.join(WEB, "static"))
SCAN_EXTS = (".html", ".js", ".css")
GENERIC_TAIL = re.compile(r"(?:sans-serif|monospace)\s*;?\s*$")

# 中文字体白名单：栈里出现任意一个，中文就不会落到通用族（SimSun）。
# 自托管 Noto 与系统中文字体都算合法兜底。
CJK_FONTS = (
    '"Noto Sans SC"', '"Noto Sans CJK SC"', '"Microsoft YaHei"',
    '"PingFang SC"', '"Hiragino Sans GB"', '"Source Han Sans SC"',
)


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _strip_comments(css):
    """去 CSS 注释：注释里的示例声明（本项目注释里就有）不应参与判定。"""
    return re.sub(r"/\*.*?\*/", " ", css, flags=re.S)


def _blocks(css):
    """→ [(选择器文本, 声明体)]（已去注释、未处理嵌套，本项目 CSS 无嵌套规则）。"""
    return re.findall(r"([^{}]+)\{([^{}]*)\}", css)


def _norm(sel):
    return re.sub(r"\s+", " ", sel.strip())


def _generic_font_decls(css):
    """→ [(选择器列表, font-family 值)]，只保留"以通用族结尾"的声明（inherit 不算）。

    值里可能带引号（`"Inter", system-ui, sans-serif`），故不能排除引号字符。
    """
    out = []
    for sel, body in _blocks(css):
        m = re.search(r"font-family\s*:\s*([^;}]+)", body)
        if not m:
            continue
        val = m.group(1).strip()
        if GENERIC_TAIL.search(val):
            out.append((sel, val))
    return out


def _names_cjk_font(val):
    return any(f in val for f in CJK_FONTS)


class FontStackClosureTest(unittest.TestCase):
    def test_token_definitions_are_correct(self):
        """`--font-sans` / `--font-mono` 必须含 Noto Sans SC，且排在通用族之前。"""
        css = _strip_comments(_read(APP_CSS))
        for token, generic in (("--font-sans", "sans-serif"), ("--font-mono", "monospace")):
            m = re.search(re.escape(token) + r"\s*:\s*([^;]+);", css)
            self.assertIsNotNone(m, f"app.css 缺少字体栈令牌 {token}")
            val = m.group(1)
            self.assertIn(
                '"Noto Sans SC"', val,
                f"{token} 未包含 \"Noto Sans SC\"：中文会落到通用族并回退宋体。值={val}"
            )
            self.assertLess(
                val.index('"Noto Sans SC"'), val.rindex(generic),
                f"{token} 里 \"Noto Sans SC\" 必须排在通用族 {generic} 之前，否则永远轮不到它"
            )

    def test_no_generic_font_family_without_cjk_font(self):
        """凡是「以通用族结尾」的 font-family，都必须点名一个中文字体。

        这正是本次实测缺陷的形态：兼容类/旧栈自己写了一份裸等宽栈（如
        `ui-monospace, SFMono-Regular, Consolas, monospace`），没有中文字体，
        中文 Windows 下通用族被解析成宋体。
        允许两种写法：走令牌 `var(--font-sans|mono)`，或值里点名了中文字体。
        """
        offenders = []
        for dirpath, dirnames, filenames in os.walk(WEB):
            dirnames[:] = [d for d in dirnames if d not in ("vendor", "__pycache__")]
            for name in filenames:
                if not name.endswith(SCAN_EXTS):
                    continue
                path = os.path.join(dirpath, name)
                rel = os.path.relpath(path, BASE)
                text = _strip_comments(_read(path)) if name.endswith(".css") else _read(path)
                for m in re.finditer(r"font-family\s*:\s*([^;}]+)", text):
                    val = m.group(1).strip()
                    if not GENERIC_TAIL.search(val):
                        continue  # 不以通用族结尾（inherit 等）：不参与
                    if val.startswith("var(--font-"):
                        continue  # 合法：走令牌（令牌本身由 test_token_definitions 保证）
                    if _names_cjk_font(val):
                        continue  # 合法：点名了中文字体
                    line = text.count("\n", 0, m.start()) + 1
                    offenders.append(f"  {rel}:{line}  font-family: {val[:80]}")
        if offenders:
            self.fail(
                "发现未点名中文字体、以通用族结尾的字体栈 —— 中文 Windows 下会回退宋体"
                "（SimSun），表现为「部分文字变宋体」：\n" + "\n".join(offenders)
                + "\n\n修法：改用 var(--font-sans) / var(--font-mono)"
                "（app.css 的 :root，内含 \"Noto Sans SC\"），"
                "或在栈里通用族之前插入 \"Noto Sans SC\" / \"Microsoft YaHei\"。"
            )

    def test_all_adminator_generic_selectors_are_covered(self):
        """Adminator 里每个"通用族结尾"的选择器都必须出现在收口清单中（对账）。"""
        adm = _generic_font_decls(_strip_comments(_read(ADMINATOR_CSS)))
        adminator_selectors = set()
        for sel, _val in adm:
            for part in sel.split(","):
                p = _norm(part)
                if p:
                    adminator_selectors.add(p)
        self.assertTrue(adminator_selectors, "未能从 adminator.css 解析出字体选择器（解析逻辑失效？）")

        # 收口清单：值走 var(--font-*) 的规则，其选择器即为覆盖集
        closure = set()
        for sel, body in _blocks(_strip_comments(_read(APP_CSS))):
            m = re.search(r"font-family\s*:\s*([^;}]+)", body)
            if not m or not m.group(1).strip().startswith("var(--font-"):
                continue
            for part in sel.split(","):
                p = _norm(part)
                if p and not p.startswith("@"):
                    closure.add(p)

        missing = sorted(adminator_selectors - closure)
        if missing:
            self.fail(
                "以下 Adminator 选择器声明了以通用族结尾的字体栈，但未出现在 app.css 的"
                "字体收口清单里 —— 这些组件内的中文会回退宋体（Adminator 升级新增组件时最易发生）：\n"
                + "\n".join(f"  {m}" for m in missing)
                + "\n\n修法：把它们按栈尾族分别并入 app.css 第 X 节的正文栈/等宽栈清单。"
            )


class FontSliceOutdirGuardTest(unittest.TestCase):
    """分片脚本的 outdir 是位置参数、随后被整目录 `rmtree`：白名单外必须拒绝。

    起子进程断言进程级行为（退出码 + 目标目录未被动过）。脚本导入期即依赖
    fonttools（**构建期**依赖，不在 requirements 里），缺依赖时跳过本组。
    """

    SCRIPT = os.path.join(BASE, "scripts", "build_cjk_font_slices.py")

    @classmethod
    def setUpClass(cls):
        import importlib.util
        if importlib.util.find_spec("fontTools") is None:
            raise unittest.SkipTest("fonttools 未安装（构建期依赖，本组跳过）")

    def _run(self, outdir):
        return subprocess.run(
            [sys.executable, self.SCRIPT, outdir, "--font", "fake.ttf"],
            capture_output=True, text=True, timeout=120, cwd=BASE)

    def test_outside_repo_is_refused_and_untouched(self):
        with tempfile.TemporaryDirectory() as d:
            keep = os.path.join(d, "keep.txt")
            with open(keep, "w", encoding="utf-8") as f:
                f.write("sentinel")
            r = self._run(d)
            self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
            self.assertIn("白名单", r.stderr)
            self.assertTrue(os.path.isfile(keep), "被拒绝的输出目录不得被动过")
            self.assertTrue(os.path.isdir(d), "被拒绝的输出目录不得被删")

    def test_whitelist_root_and_prefix_sibling_refused(self):
        """白名单根自身与"前缀相似"的同级目录都不算命中（后者防字符串前缀误判）。"""
        for rel in ("web/static", "web/static-evil"):
            with self.subTest(rel=rel):
                r = self._run(rel)
                self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
                self.assertIn("白名单", r.stderr)

    def test_under_whitelist_passes_the_guard(self):
        """白名单内的合法落点必须过闸（后续因假字体失败，但不再是白名单拒绝）。"""
        r = self._run("web/static/vendor/fonts/notosanssc-guardprobe")
        self.assertNotEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertNotIn("白名单", r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
