# -*- coding: utf-8 -*-
"""前端脚本装配守卫（多页 MPA + 组件化后的等价判据）。

## 为什么替换旧的「7 个连续切片」守卫

旧守卫钉的是 `web/static/js/app.js`（2765 行单页脚本）按**连续源区间**拆成 7 个
classic 切片、由 `templates/index.html` 按序加载。前端换壳为多页 MPA 后：

  · `index.html` 已无路由渲染（`/` 由 `pages/dashboard.html` + `layout_admin.html` 承载），
    它的加载顺序不再是运行前提；
  · 页面脚本重写为 IIFE 页面模块（`pages/*.js`）与共享组件（`components/*.js`），
    文件间不再有「原 app.js 的连续区间」关系，源区间连续性无从声明也无意义。

旧守卫保护的两件事仍然有效，必须继续钉住，只是载体迁移：
  ① **不靠索引误加载/漏加载**：classic script 共享同一全局词法作用域，任一实际渲染的
     页面里，`core.js` 必须先于组件与页面模块；引用的自研 JS 必须真实存在（无悬空 src）。
  ② **不因拆文件撞名**：多个文件顶层用同一个 `const/let/function/class` 名会让整段
     script SyntaxError、页面静默失去全部交互——拆得越碎越要查。

## 本文件钉住的四件事

1. `app.js` 不得复活；核心与共享组件模块必须在位。
2. 每个**活模板**（`layout_*.html` / `pages/*.html` / `login.html`）`{% block scripts %}`
   引用的 `/static/js/...` 都必须存在；且按 extends 展开后的有效加载顺序里
   `core.js` 先于其它模块。
3. 全部自研 JS（排除 `static/vendor/`）的顶层声明不得重名。
4. `pages/accounts.js`（重写对象）不得再引用已退役的单页脚本 `app.js`。
"""

import glob
import os
import re
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS_DIR = os.path.join(BASE, "web", "static", "js")
TEMPLATES_DIR = os.path.join(BASE, "web", "templates")

# 核心与共享组件模块：缺失即页面初始化失败，必须存在。
REQUIRED_MODULES = (
    "core.js",
    "calendar.js",
    "components/account-form.js",
    "components/time-pref.js",
    "components/account-table.js",
    "components/account-ops.js",
    "pages/accounts.js",
    "pages/user_accounts.js",
)

# 实际渲染的页面模板：layout_*.html（外壳，自带 core.js）+ pages/*.html（正文，含 block scripts）
# + login.html（认证页正文，extends layout_auth.html）。
def _active_templates():
    out = sorted(glob.glob(os.path.join(TEMPLATES_DIR, "layout_*.html")))
    out += sorted(glob.glob(os.path.join(TEMPLATES_DIR, "pages", "*.html")))
    login = os.path.join(TEMPLATES_DIR, "login.html")
    if os.path.isfile(login):
        out.append(login)
    return [p for p in out if os.path.basename(p) != "_stub_macro.html"]

_JS_REF_RE = re.compile(r"/static/js/([^\"'?\s]+)")
_EXTENDS_RE = re.compile(r'{%-?\s*extends\s+"([^"]+)"\s*-?%}')

# 顶层声明：只认**行首**（列 0）的声明，这才落在共享的全局词法作用域里；
# 缩进的（函数体内/IIFE 内）各自独立，不参与重名检查。
_TOP_DECL_RE = re.compile(
    r"^(?:async\s+)?function\s+(\w+)"
    r"|^(?:let|const|var)\s+(\w+)"
    r"|^class\s+(\w+)"
)


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _js_refs(path):
    return [m.group(1) for m in _JS_REF_RE.finditer(_read(path))]


def _effective_js_order(path):
    """展开 extends 链后的 /static/js 引用顺序（外壳在前、页面在后）。

    layout_*.html 均为根模板（不 extends 其它），故一层解析足够；仍保留递归以容忍
    将来出现「页面 → 中间壳 → 根壳」的层级。
    """
    src = _read(path)
    own = _js_refs(path)
    ext = _EXTENDS_RE.search(src)
    if not ext:
        return own
    parent = os.path.join(TEMPLATES_DIR, ext.group(1).replace("/", os.sep))
    if not os.path.isfile(parent):
        return own
    return _effective_js_order(parent) + own


def _iter_authored_js():
    for dirpath, _dirnames, filenames in os.walk(JS_DIR):
        if os.sep + "vendor" + os.sep in dirpath + os.sep:
            continue
        for name in sorted(filenames):
            if name.endswith(".js"):
                yield os.path.join(dirpath, name)


class JsAssemblyGuardTest(unittest.TestCase):
    def test_app_js_is_gone_and_required_modules_exist(self):
        """`app.js` 不应复活；核心与共享组件模块必须在位。"""
        problems = []
        if os.path.exists(os.path.join(JS_DIR, "app.js")):
            problems.append("  web/static/js/app.js 又出现了 —— 单页脚本已退役，不应回滚")
        for rel in REQUIRED_MODULES:
            if not os.path.exists(os.path.join(JS_DIR, rel.replace("/", os.sep))):
                problems.append(f"  web/static/js/{rel} 缺失")
        if problems:
            self.fail("前端脚本模块清单不对：\n" + "\n".join(problems))

    def test_active_templates_reference_only_existing_scripts(self):
        """活模板引用的自研 JS 必须真实存在（无悬空 src）。"""
        problems = []
        for tpl in _active_templates():
            for ref in _effective_js_order(tpl):
                disk = os.path.join(JS_DIR, ref.replace("/", os.sep))
                if not os.path.isfile(disk):
                    problems.append(
                        f"  {os.path.relpath(tpl, BASE)}: /static/js/{ref} 悬空（磁盘无此文件）"
                    )
        if problems:
            self.fail(
                "模板引用了不存在的自研脚本 —— classic script 共享全局作用域，"
                "悬空 src 会让后续依赖它的页面脚本在运行时才炸：\n" + "\n".join(problems)
            )

    def test_core_js_loads_before_components_and_page_modules(self):
        """按 extends 展开后的有效顺序里，`core.js` 必须先于其它自研模块。"""
        problems = []
        for tpl in _active_templates():
            order = _effective_js_order(tpl)
            if "core.js" not in order:
                problems.append(f"  {os.path.relpath(tpl, BASE)}: 有效顺序里没有 core.js")
                continue
            core_at = order.index("core.js")
            for i, ref in enumerate(order):
                if ref == "core.js":
                    continue
                if i < core_at:
                    problems.append(
                        f"  {os.path.relpath(tpl, BASE)}: {ref} 排在 core.js 之前"
                    )
        if problems:
            self.fail(
                "core.js（YB 命名空间与共享能力）必须在组件/页面模块之前加载 ——"
                " 顺序错了只在运行时暴露，且可能只在某个页面才炸：\n" + "\n".join(problems)
            )

    def test_no_duplicate_top_level_declarations_across_authored_js(self):
        """全部自研 JS 顶层声明不得重名（重名 → 整个 script SyntaxError，页面静默失交互）。"""
        seen = {}
        dupes = []
        for path in _iter_authored_js():
            rel = os.path.relpath(path, JS_DIR)
            for lineno, line in enumerate(_read(path).split("\n"), 1):
                m = _TOP_DECL_RE.match(line)
                if not m:
                    continue
                name = next(g for g in m.groups() if g)
                if name in seen:
                    dupes.append(
                        f"  {name}：已定义于 {seen[name][0]}:{seen[name][1]}，"
                        f"又在 {rel}:{lineno} 重复声明"
                    )
                else:
                    seen[name] = (rel, lineno)
        if dupes:
            self.fail(
                "发现跨文件重名的顶层声明 —— classic script 共享同一个全局词法作用域，"
                "同名 const/let 会让**整个脚本直接 SyntaxError**（页面静默失去全部交互）：\n"
                + "\n".join(dupes)
            )

    def test_accounts_page_module_does_not_reference_retired_app_js(self):
        """重写后的 `pages/accounts.js` 不得再引用已退役的 `app.js` 或旧切片区间标记。"""
        src = _read(os.path.join(JS_DIR, "pages", "accounts.js"))
        for bad in ("app.js", "L246-L869", "L870-L1058"):
            self.assertNotIn(
                bad, src,
                f"pages/accounts.js 仍引用退役的旧栈标记 {bad!r}：页面脚本应为独立 IIFE 模块",
            )


if __name__ == "__main__":
    unittest.main()
