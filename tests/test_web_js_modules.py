# -*- coding: utf-8 -*-
"""管理端脚本分模块的守卫（2026-09-10，A4-2）。

## 背景
`web/static/js/app.js`（2765 行）已按**连续区间**拆成 7 个模块（原文件删除），
在 `templates/index.html` 里按序以 classic `<script src>` 加载。
拆分方式见 `web/static/js/` 下每个文件的头 3 行；当时的逐字节等价证明见 A4-2 的提交信息
（7 个文件去掉 3 行文件头后按序拼接 == 原 app.js）。原文件已删，那份证明无法在测试里复现，
故本文件改为钉住**拆分之后新引入的、最容易静默炸掉的风险**。

## 本文件钉住的四件事

1. **`app.js` 不应复活、原 7 个切片模块必须在位** —— 否则说明有人回滚了一半。
2. **`index.html` 的加载顺序必须与期望一致** —— classic script 共享全局作用域，
   顺序错了会出现"函数还没定义就被调用"（只在运行时、且可能只在某个 tab 才暴露）。
3. **跨模块顶层声明不得重名** —— 这是分模块**新引入**的头号风险：
   同一份文件里不可能重名，但拆成多份后，两个文件顶层用同一个 `const/let/function/class` 名
   → **整个脚本 SyntaxError 直接不执行**，页面静默失去全部交互。静态扫描即可抓住。
4. **各切片模块头声明的源区间必须连续递增** —— 保证"分区切割、无重叠无遗漏"这件事仍然成立，
   也让人一眼能看出每个模块对应原文件的哪一段。

## 2026-09-12：新增模块与旧切片的区分

签到日历被抽成 `web/static/js/calendar.js`（全站唯一实现，用户页与旧管理端的
「我的账号」共用），加载在 `mine.js` 之前。它是**新代码、不是原 `app.js` 的切片**，
因此没有 `L<起>-L<止>` 源区间声明，第 4 条只约束 `EXPECTED_MODULES`；
但它与旧模块同处一个全局词法作用域（同为 classic script），故第 3 条的重名检查
必须把它一并纳入（见 `EXTRA_MODULES`）。
"""

import os
import re
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS_DIR = os.path.join(BASE, "web", "static", "js")
INDEX = os.path.join(BASE, "web", "templates", "index.html")

# 原 app.js 的 7 个连续切片（含头 3 行声明的源区间；改动前请同步本表并说明原因）
EXPECTED_MODULES = (
    ("core.js", 1, 245),
    ("pages/accounts.js", 246, 869),
    ("pages/logs.js", 870, 1058),
    ("pages/settings.js", 1059, 1934),
    ("shared-ui.js", 1935, 2093),
    ("pages/users.js", 2094, 2359),
    ("pages/mine.js", 2360, 2765),
)

# 新架构下新增、与原 app.js 无切片关系的共享模块（无源区间声明，但同处全局作用域）
EXTRA_MODULES = ("calendar.js",)

# index.html 里 /static/js/ 的期望加载顺序（新模块排在它所服务的模块之前）
LOAD_ORDER = (
    "core.js",
    "calendar.js",
    "pages/accounts.js",
    "pages/logs.js",
    "pages/settings.js",
    "shared-ui.js",
    "pages/users.js",
    "pages/mine.js",
)

# 顶层声明：只认**行首**（列 0）的声明，这才落在共享的全局词法作用域里；
# 缩进的（函数体内）各自独立，不参与重名检查。
_TOP_DECL_RE = re.compile(
    r"^(?:async\s+)?function\s+(\w+)"
    r"|^(?:let|const|var)\s+(\w+)"
    r"|^class\s+(\w+)"
)


def _module_path(rel):
    return os.path.join(JS_DIR, rel.replace("/", os.sep))


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class JsModuleSplitTest(unittest.TestCase):
    def test_app_js_is_gone_and_modules_exist(self):
        """`app.js` 不应复活；7 个模块都必须在。"""
        problems = []
        if os.path.exists(os.path.join(JS_DIR, "app.js")):
            problems.append("  web/static/js/app.js 又出现了 —— 拆分被回滚了一半？")
        for rel, _start, _end in EXPECTED_MODULES:
            if not os.path.exists(_module_path(rel)):
                problems.append(f"  web/static/js/{rel} 缺失")
        for rel in EXTRA_MODULES:
            if not os.path.exists(_module_path(rel)):
                problems.append(f"  web/static/js/{rel} 缺失（新增的共享模块）")
        if problems:
            self.fail("管理端脚本模块清单不对：\n" + "\n".join(problems))

    def test_index_loads_modules_in_the_expected_order(self):
        """index.html 的 classic script 顺序必须与期望一致（顺序错了只在运行时才炸）。"""
        html = _read(INDEX)
        found = [
            m.group(1)
            for m in re.finditer(r'<script src="[^"]*/static/js/([^"?]+)\?', html)
        ]
        self.assertEqual(
            found,
            list(LOAD_ORDER),
            "index.html 里 /static/js/ 的加载顺序与期望不符 ——"
            " classic script 共享全局作用域，顺序改动会让「后加载者依赖的定义」落空：\n"
            f"  实际：{found}\n  期望：{list(LOAD_ORDER)}",
        )

    def test_no_duplicate_top_level_declarations_across_modules(self):
        """跨模块顶层声明不得重名（重名 → 整个脚本 SyntaxError，页面静默失去交互）。"""
        seen = {}
        dupes = []
        order = [rel for rel, _s, _e in EXPECTED_MODULES] + list(EXTRA_MODULES)
        for rel in order:
            for lineno, line in enumerate(_read(_module_path(rel)).split("\n"), 1):
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
                "发现跨模块重名的顶层声明 —— classic script 共享同一个全局词法作用域，"
                "同名 const/let 会让**整个脚本直接 SyntaxError**（页面静默失去全部交互）：\n"
                + "\n".join(dupes)
            )

    def test_module_headers_declare_contiguous_source_ranges(self):
        """原 app.js 的切片模块，头 3 行声明的源区间必须连续递增（证明「分区切割」未被破坏）。

        只约束 `EXPECTED_MODULES`：`EXTRA_MODULES`（如 calendar.js）是新写的共享模块，
        不是原 `app.js` 的一段，没有也不应有源区间声明。
        """
        expected_start = 1
        problems = []
        for rel, start, end in EXPECTED_MODULES:
            text = _read(_module_path(rel))
            head = "\n".join(text.split("\n")[:3])
            m = re.search(r"L(\d+)-L(\d+)", head)
            if not m:
                problems.append(f"  {rel}：头 3 行里没有 `L<起>-L<止>` 形式的源区间声明")
                continue
            got = (int(m.group(1)), int(m.group(2)))
            if got != (start, end):
                problems.append(f"  {rel}：声明 {got}，期望 {(start, end)}")
            if start != expected_start:
                problems.append(f"  {rel}：起点 {start}，但上一段结束于 {expected_start - 1}（不连续）")
            expected_start = end + 1
        if problems:
            self.fail("模块头声明的源区间不对：\n" + "\n".join(problems))


if __name__ == "__main__":
    unittest.main()
