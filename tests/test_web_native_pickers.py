# -*- coding: utf-8 -*-
"""原生选择控件禁令守卫（Vue 源码侧 + 旧模板侧 + 构建产物侧）。

用户既定禁令（2026-09 起，2026-10-10 扩面）：原生 `<select>` 与
`<input type=range|time|date|datetime-local>` 的弹出层由 UA 渲染、样式不可控，
一律换 Element Plus 组件或仓内既有控件形态。

判据演进：
· 旧口径（`docs/refactor/67` B1）只扫 `web/templates/`，Vue 侧从未纳入 ⇒ 回归漏网
  （工单 yiban-auto-sign-1euo：`Audit.vue` 原生 select、`DistViz.vue` 原生 time）。
· 本单把扫描面扩到 `frontend/src/**` ＋ `web/templates/**` ＋ `web/static/vue/assets/*.js`
  （三面合一，web/templates 旧口径所辖面不再掉出自动判据）。
· 2026-10-10 用户裁决：`date`/`datetime-local` 同类也换。原「保留原生 date 做无 JS
  退化」的理由（`docs/refactor/33` §六）对 Vue 页已不成立——整页都由 JS 渲染，无 JS
  时本来就空白。故 `date`/`datetime-local` 由「登记放行」改为**禁令内、无例外**
  （`Audit.vue` 两处 + `Logs.vue` 一处同日改 `el-date-picker`）。

扫描面边界（显式登记，避免后来者以为是遗漏）：
· 覆盖：`frontend/src/**`（.vue/.ts/.js，排除 *.spec.ts 与 *.d.ts）＋ `web/templates/**`
  （.html）＋ `web/static/vue/assets/*.js`（排除 `vendor-*`）。
· **例外（不在扫描面内）**：`web/static/preview-states/dist-viz.html`。它是 noindex、全仓
  零引用的「组件方案选型/对比」原型页，刻意展示原生 `<input type=time>` 作对照（同备份
  脚本里保留的「旧写法反例」）。主会话 2026-10-10 裁决维持现状；判据范围刻意不放宽到
  全部静态 HTML，避免把对照/演示件纳入禁令造成语义混乱。该文件已列为删除候选待用户定。
· 允许面（面内排除）：EP 内部实现（构建产物 `vendor-*` chunk 整块排除）；带 `hidden`
  属性的原生 input（组件无 JS 退化契约载体，不是可见控件）。

标签：F · 前端与界面守卫
依赖：纯本地文件扫描；无需 node（构建产物已入库）
"""
import os
import re
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_DIR = os.path.join(BASE, "frontend", "src")
TEMPLATES_DIR = os.path.join(BASE, "web", "templates")
ASSETS_DIR = os.path.join(BASE, "web", "static", "vue", "assets")

# 禁令控件类型：弹出层由 UA 渲染、样式不可控。无例外（见模块 docstring）。
BANNED_INPUT_TYPES = ("range", "time", "date", "datetime-local")

_SRC_INPUT_TAG_RE = re.compile(r"<input\b[^>]*>", re.IGNORECASE)
_SRC_SELECT_RE = re.compile(r"<select\b", re.IGNORECASE)
# 静态形态：type="date" / type='date' / type=date（无引号）。
_STATIC_TYPE_RE = re.compile(
    r"(?<![\w-])type\s*=\s*(?:\"(range|time|date|datetime-local)\""
    r"|'(range|time|date|datetime-local)'"
    r"|(range|time|date|datetime-local)(?=[\s/>]))",
    re.IGNORECASE,
)
# 绑定字面量形态：:type="'date'"。
_BOUND_TYPE_RE = re.compile(
    r":type\s*=\s*\"'(range|time|date|datetime-local)'\"", re.IGNORECASE
)
_SRC_HIDDEN_RE = re.compile(r"(?<![\w-])hidden(?:\s|=|/?>)", re.IGNORECASE)

# 注释剥离：注释里的讲解文本会提到 `<select>` 等字样，不算控件。清三类：
# HTML 注释、JS 块注释、JS 行注释（行注释避开 `://`，不误伤 URL）。
# 只删注释不改行数（按 `\n` 等量回填），故报出的行号仍是真实行号。
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT_RE = re.compile(r"(?<!:)//[^\n]*")


def _strip_comments(text):
    for rx in (_HTML_COMMENT_RE, _BLOCK_COMMENT_RE, _LINE_COMMENT_RE):
        text = rx.sub(lambda m: "\n" * m.group(0).count("\n"), text)
    return text


def _match_input_type(tag):
    """从一段 `<input ...>` 标签里取被禁的 type 值；静态/无引号/绑定字面量三种写法都认。"""
    for rx in (_BOUND_TYPE_RE, _STATIC_TYPE_RE):
        m = rx.search(tag)
        if m:
            return next(g for g in m.groups() if g).lower()
    return None


def _scan_source_tree(root, extensions, skip_suffixes=()):
    """扫一棵源码树，返回原生 select/input 违规点（相对 root 的路径:行号）。"""
    violations = []
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            if not name.endswith(tuple(extensions)):
                continue
            if name.endswith(tuple(skip_suffixes)):
                continue
            path = os.path.join(dirpath, name)
            with open(path, encoding="utf-8") as fh:
                text = _strip_comments(fh.read())
            rel = os.path.relpath(path, root).replace(os.sep, "/")
            for m in _SRC_SELECT_RE.finditer(text):
                violations.append(f"<select> @ {rel}:{text[:m.start()].count(chr(10)) + 1}")
            for m in _SRC_INPUT_TAG_RE.finditer(text):
                tag = m.group(0)
                if _SRC_HIDDEN_RE.search(tag):
                    continue  # 隐藏契约 input，不是可见控件
                typ = _match_input_type(tag)
                if typ:
                    violations.append(
                        f"input[type={typ}] @ {rel}:{text[:m.start()].count(chr(10)) + 1}"
                    )
    return violations


# 构建产物侧：Vue 把原生元素编译成 createElementVNode("<元素名>", {...}) 或
# createElementVNode("<元素名>", _mergeProps({...}))，元素名是**字符串字面量**；
# EP 组件则是 createVNode(<标识符>, {...})，其 `type:"date"` 只是 el-date-picker 的 prop，
# 不是原生 input。故原生 select/input 一律以「元素名字符串字面量」甄别，避免误伤 EP 属性。
_BUILT_SELECT_RE = re.compile(r"\(\s*[`\"']select[`\"']\s*,\s*(?:\{|null)")
_BUILT_INPUT_ELEM_RE = re.compile(
    r"[`\"']input[`\"']\s*,\s*(?:(?P<merge>\w*mergeProps)\s*\(\s*)?(?P<open>[{(])"
)
_BUILT_TYPE_RE = re.compile(
    r"(?<![\w-])type\s*:\s*[`\"'](range|time|date|datetime-local)[`\"']"
)


def _balanced(text, open_idx):
    """从 open_idx 处的花括号/圆括号起做配对，返回其子串（含两端）。"""
    opener = text[open_idx]
    closer = "}" if opener == "{" else ")"
    depth = 0
    i = open_idx
    while i < len(text):
        c = text[i]
        if c == opener:
            depth += 1
        elif c == closer:
            depth -= 1
            if depth == 0:
                return text[open_idx:i + 1]
        i += 1
    return text[open_idx:]


def _native_input_types(text):
    """取出原生 `<input>`（元素名字符串字面量）自身的 type 值，忽略 EP 组件的同名字符串 prop。"""
    found = []
    for m in _BUILT_INPUT_ELEM_RE.finditer(text):
        if m.group("merge"):
            # v-bind 展开：createElementVNode("input", _mergeProps({...}, {...}))，取整个实参段。
            props = _balanced(text, text.index("(", m.start("merge")))
        else:
            props = _balanced(text, m.start("open"))
        tm = _BUILT_TYPE_RE.search(props)
        if tm:
            found.append(tm.group(1))
    return found


class NativePickerSourceTest(unittest.TestCase):
    def test_frontend_src_has_no_native_pickers(self):
        """`frontend/src` 下不得出现可见原生 select/range/time/date/datetime-local 控件。"""
        violations = _scan_source_tree(SRC_DIR, (".vue", ".ts", ".js"), (".spec.ts", ".d.ts"))
        self.assertEqual(
            violations, [],
            "frontend/src 出现原生选择控件（UA 渲染弹层，样式不可控）——换 EP 组件："
            f"{violations}",
        )

    def test_web_templates_have_no_native_pickers(self):
        """`web/templates`（旧口径 docs/refactor/67 B1 所辖面）不得出现原生控件。"""
        violations = _scan_source_tree(TEMPLATES_DIR, (".html",))
        self.assertEqual(
            violations, [],
            "web/templates 出现原生选择控件（UA 渲染弹层，样式不可控）——换 EP/自研锚点："
            f"{violations}",
        )


class NativePickerBuildTest(unittest.TestCase):
    def test_built_assets_have_no_native_pickers(self):
        """构建产物（入库 dist）里不得出现原生控件；防"源码修了、dist 没重编"。"""
        self.assertTrue(os.path.isdir(ASSETS_DIR), f"构建产物目录不存在：{ASSETS_DIR}")
        violations = []
        for name in sorted(os.listdir(ASSETS_DIR)):
            if not name.endswith(".js") or name.startswith("vendor-"):
                continue  # vendor-* 是 EP 内部实现，属允许面
            with open(os.path.join(ASSETS_DIR, name), encoding="utf-8") as fh:
                text = fh.read()
            violations += [f"<select> @ {name}"] * len(_BUILT_SELECT_RE.findall(text))
            violations += [f"input[type={t}] @ {name}" for t in _native_input_types(text)]
        self.assertEqual(
            violations, [],
            "构建产物出现原生选择控件（dist 未随源码重建？）："
            f"{violations}",
        )


if __name__ == "__main__":
    unittest.main()
