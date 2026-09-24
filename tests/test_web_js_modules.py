# -*- coding: utf-8 -*-
"""前端脚本装配守卫（多页 MPA + 组件化后的等价判据）。

标签：F · 前端与界面守卫
覆盖：前端脚本装配守卫——`app.js` 不得复活、活模板引用的自研 JS 真实存在、`id` 不重、core.js 先加载、顶层声明不重名、innerHTML 与裸 fetch 约束、页面脚本用到的组件必须引入、前端批量上限与后端同值、tab 深链只由 core 承担、自助改密只有一份实现
对应实现：`web/static/js/core.js` 与 `pages/*.js`、`components/*.js`、各 `layout_*.html` / `pages/*.html` / `login.html`；后端 `BATCH_OP_LIMIT`
关键断言：按 extends 展开后的有效加载顺序里 `core.js` 必须先于其它自研模块——classic script 共享同一全局词法作用域，顶层重名会让整段 SyntaxError、页面**静默**失去全部交互；`pages/*.js` 零 `.innerHTML`、不得裸用 `fetch`（`YB.api` 才带 CSRF/统一错误/401 跳转）；`components/user-ops.js` 不得出现 `data.msg`（后端成功 msg 含完整邮箱）；加载态必须有终止（fetch 挂 AbortController 超时、导航进度条带兜底收尾）
依赖：纯本地——读模板与 JS 源码文本（含 extends 链解析），**不执行 JS、无需 node**、不联网

## 为什么替换旧的「7 个连续切片」守卫

旧守卫钉的是 `web/static/js/app.js`（2765 行单页脚本）按**连续源区间**拆成 7 个
classic 切片、由旧单页壳 `templates/index.html` 按序加载。前端换壳为多页 MPA 后：
  · P4（2026-09-13）旧栈整体退役——`index.html`/`base.html`/`tabs/*`/`partials/modals/*`
    与 `shared-ui.js` 均已删除，旧加载顺序不再存在；
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
4. `pages/work_accounts.js`（重写对象）不得再引用已退役的单页脚本 `app.js`。
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
    "components/row-menu.js",
    "components/account-table.js",
    "components/account-ops.js",
    "components/user-ops.js",
    # 个人域共享组件（/user/account 与 /my/account 共用，禁止第二份实现）
    "components/change-password.js",
    "components/my-accounts.js",
    "components/my-mail-notify.js",
    "components/my-accounts-page.js",
    "components/time-field.js",
    "components/select-field.js",
    "components/range-field.js",
    "components/date-field.js",
    "components/sign-calendar-view.js",
    "components/settings-schedule.js",
    "components/settings-health.js",
    "components/settings-notify.js",
    "components/settings-mail.js",
    "components/settings-quota.js",
    "components/settings-executors.js",
    "components/settings-switches.js",
    "pages/work_accounts.js",
    "pages/user_account.js",
    "pages/work_users.js",
    "pages/work_settings.js",
    "pages/my_account.js",
    "pages/my_calendar.js",
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

# 组件里 innerHTML 赋值的右值：必须是常量 SVG（svg(...)）或清空容器（""/''）
_INNERHTML_ASSIGN_RE = re.compile(r"\.innerHTML\s*=\s*([^\n;]+)")
_SVG_RHS_RE = re.compile(r"^\s*svg\(")

# user-ops.js 的 LIMIT 与 web/app.py 的 BATCH_OP_LIMIT 必须同源
_JS_LIMIT_RE = re.compile(r"\bvar\s+LIMIT\s*=\s*(\d+)\s*;")
_PY_BATCH_LIMIT_RE = re.compile(r"^BATCH_OP_LIMIT\s*=\s*(\d+)\s*$", re.M)


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

    def test_active_templates_have_no_duplicate_element_ids(self):
        """活模板内 `id="..."` 不得重复 —— 同 id 会让 `YB.$`（getElementById）只取文档序第一个。

        `pages/work_settings.html` 曾同时存在 `<section id="set-announcement">` 与
        `<textarea id="set-announcement">`：回填写进 section（textarea 恒空）、保存读
        `section.value`（undefined）→ 每次保存都等同清空公告。浏览器对此零报错，
        写代码时也看不出，只有本守卫能静态拦下。
        Jinja 占位 id（值含 `{`）由调用方各自传入，静态无法比较，跳过。
        """
        id_re = re.compile(r'id="([^"]*)"')
        problems = []
        for tpl in _active_templates():
            counts = {}
            for m in id_re.finditer(_read(tpl)):
                value = m.group(1)
                if not value or "{" in value:
                    continue
                counts[value] = counts.get(value, 0) + 1
            for value, n in counts.items():
                if n > 1:
                    problems.append(
                        f"  {os.path.relpath(tpl, BASE)}: id={value!r} 出现 {n} 次"
                    )
        if problems:
            self.fail(
                "活模板内出现重复 id —— getElementById 只取文档序第一个，"
                "读写回填会落到错误节点上（数据静默丢失）：\n" + "\n".join(problems)
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
        """重写后的 `pages/work_accounts.js` 不得再引用已退役的 `app.js` 或旧切片区间标记。"""
        src = _read(os.path.join(JS_DIR, "pages", "work_accounts.js"))
        for bad in ("app.js", "L246-L869", "L870-L1058"):
            self.assertNotIn(
                bad, src,
                f"pages/work_accounts.js 仍引用退役的旧栈标记 {bad!r}：页面脚本应为独立 IIFE 模块",
            )

    def test_pages_never_use_innerhtml(self):
        """`pages/*.js` 全量不得出现 `.innerHTML`（P4 后零允许清单）。

        旧栈页面脚本（dashboard/login）的历史 innerHTML 常量 SVG 已在 P4 就地改为
        DOM 构造（`YB.iconEl` / 页面自建 `svgIcon`），允许清单随之删除；此后任何页面
        新增 `.innerHTML` 都会在这里报红——动态文本一律走 YB.el/textContent，数据脱敏
        不能依赖「先拼 HTML 再转义」。
        """
        offenders = []
        for path in sorted(glob.glob(os.path.join(JS_DIR, "pages", "*.js"))):
            name = os.path.basename(path)
            if ".innerHTML" in _read(path):
                offenders.append(f"  pages/{name}")
        if offenders:
            self.fail(
                "页面脚本出现 .innerHTML —— 动态文本必须走 YB.el/textContent，"
                "禁止把（哪怕是脱敏后的）数据拼进 HTML：\n" + "\n".join(offenders)
            )

    def test_components_innerhtml_only_from_constant_svg(self):
        """`components/*.js` 的 innerHTML 赋值右值只允许常量 SVG 或清空容器。"""
        problems = []
        for path in sorted(glob.glob(os.path.join(JS_DIR, "components", "*.js"))):
            name = os.path.basename(path)
            for lineno, line in enumerate(_read(path).split("\n"), 1):
                m = _INNERHTML_ASSIGN_RE.search(line)
                if not m:
                    continue
                rhs = m.group(1).strip()
                if rhs in ('""', "''"):
                    continue  # 清空容器，不含数据
                if not _SVG_RHS_RE.match(m.group(1)):
                    problems.append(
                        f"  components/{name}:{lineno} innerHTML 右值非 svg(...)：{rhs[:80]}"
                    )
        if problems:
            self.fail(
                "组件用 innerHTML 写入了非常量 SVG（可能引入注入面）：\n"
                + "\n".join(problems)
            )

    def test_user_ops_never_puts_server_msg_on_screen(self):
        """`components/user-ops.js` 不得出现 `data.msg`（钉住单目标 PII 修复）。

        后端 role/password/delete 的成功 msg 含**完整邮箱**，一旦用后端 msg 上屏，
        完整邮箱就进入 DOM；本组件只允许 batch/purge（msg 仅数量）使用后端 msg。
        """
        src = _read(os.path.join(JS_DIR, "components", "user-ops.js"))
        self.assertNotIn(
            "data.msg", src,
            "user-ops.js 出现 data.msg —— 单目标成功提示会把完整邮箱经 toast 写入 DOM；"
            "本组件只允许 batch/purge 的计数型 msg 上屏",
        )

    # 唯一的裸 fetch 例外：日志导出是**文件下载**（blob），YB.api 只处理 JSON 响应，
    # 无法替代。登记在此并在判据里说明原因，避免把"绕过 CSRF"的写法混进来。
    _BARE_FETCH_ALLOW = frozenset({"data_logs.js"})

    def test_pages_and_components_do_not_use_bare_fetch(self):
        """`pages/*.js` 与 `components/*.js` 不得裸用 `fetch` —— 必须走 `YB.api`。

        `YB.api` 承担 CSRF 头、统一错误与 401 跳转；裸 fetch 会静默绕过这几层
        （写请求尤其危险）。日志导出的 blob 下载是唯一例外，见 `_BARE_FETCH_ALLOW`。
        """
        offenders = []
        for sub in ("pages", "components"):
            for path in sorted(glob.glob(os.path.join(JS_DIR, sub, "*.js"))):
                name = os.path.basename(path)
                if name in self._BARE_FETCH_ALLOW:
                    continue
                if re.search(r"\bfetch\s*\(", _read(path)):
                    offenders.append(f"  {sub}/{name}")
        if offenders:
            self.fail(
                "页面/组件裸用 fetch（绕过 YB.api 的 CSRF、统一错误与 401 处理）：\n"
                + "\n".join(offenders)
                + "\n确需下载文件（blob）时，请在本测试的 _BARE_FETCH_ALLOW 里显式登记并说明原因"
            )

    def test_loading_states_always_have_an_end(self):
        """加载态必须有终止保证：请求带超时信号、导航进度条带兜底收尾。

        背景（用户实拍：总览页有概率"一直加载、一直不完成"，不可稳定复现）：
        ① fetch 默认没有超时，网络静默掉线或服务端线程占满时 Promise 永久挂起 →
          骨架/遮罩/在途按钮永远不会结束；② 顶部导航进度条由新页面的 core.js 收尾，
          那次点击若没真正导航（被页面逻辑拦下或浏览器取消）就停在 90%。
        两处都必须存在上界，否则同样的卡死会静默回归（浏览器侧无法在单测里复现）。
        """
        core = _read(os.path.join(JS_DIR, "core.js"))
        self.assertIn("AbortController", core, "YB.api 的 fetch 必须挂可中止的超时信号")
        self.assertRegex(
            core, r"setTimeout\s*\(\s*function\s*\(\s*\)\s*\{\s*ctl\.abort\(\)",
            "超时到点必须 abort 本次请求（否则请求无上界）",
        )
        self.assertRegex(
            core, r"navGuardTimer\s*=\s*setTimeout",
            "导航进度条必须有兜底计时器（否则点击未导航时停在 90% 不消失）",
        )
        self.assertRegex(
            core, r'addEventListener\("pagehide",\s*finishNavProgress\)',
            "真实导航发生时须取消兜底（由新页面负责收尾）",
        )

    def test_settings_mail_never_backfills_masked_values(self):
        """SMTP 行内编辑不得把脱敏值写进输入框 —— 打码值只允许作 placeholder。

        后端 GET /api/mail-config 下发的 smtps[].user / has_pass 是打码或占位串；
        一旦作为 `value` 回填，保存时会按字面落盘并损坏配置（或把打码串当授权码）。
        判据落在**脱敏字段专用构造器**上：`maskedCellInput` 体内不得出现 `.value`，
        且 user / pass 两列必须走它（host / port 是非敏感字段，允许回填）。
        """
        src = _read(os.path.join(JS_DIR, "components", "settings-mail.js"))
        m = re.search(r"function maskedCellInput\(.*?\n  \}", src, re.S)
        self.assertIsNotNone(
            m, "settings-mail.js 未找到 maskedCellInput（脱敏字段专用构造器，写法变了？请同步本测试）"
        )
        self.assertNotIn(
            ".value", m.group(0),
            "maskedCellInput 回填了 value —— 脱敏值只允许作 placeholder，不得写进输入框",
        )
        self.assertIn('maskedCellInput("user"', src,
                      "发件账号列必须走 maskedCellInput（后端已打码，不得回填）")
        self.assertIn('maskedCellInput("pass"', src,
                      "授权码列必须走 maskedCellInput（绝不回显）")
        self.assertIn("function clean(", src, "settings-mail.js 缺少打码值清洗函数 clean()")

    # 组件导出的公开面：`YB.<name> = ...`（components/*.js）→ name 由哪个组件提供
    _YB_EXPORT_RE = re.compile(r"\bYB\.([A-Za-z_$][\w$]*)\s*=")
    # 调用点：`YB.<name>(...)` 或 `YB.<name>.prop(...)`（组件公开面多为 `YB.xxx.mount(...)`）
    _YB_USE_RE = re.compile(r"\bYB\.([A-Za-z_$][\w$]*)\s*[.(]")

    def _component_exports(self):
        out = {}
        for path in sorted(glob.glob(os.path.join(JS_DIR, "components", "*.js"))):
            for m in self._YB_EXPORT_RE.finditer(_read(path)):
                out.setdefault(m.group(1), os.path.basename(path))
        return out

    def test_page_scripts_load_the_components_they_use(self):
        """页面脚本调用的 `YB.<组件>` 必须在同一页的脚本顺序里被引入。

        漏引一个 `<script>` 的后果不是"某功能缺失"，而是页面脚本里
        `YB.xxx.mount(...)` 首行就 TypeError —— **整页交互静默全死**、DOM 停在服务端骨架。
        实测：`/user` 与 `/mine` 曾漏引 `components/my-accounts-page.js`，账号区既无空态
        也无提交按钮，而既有守卫只查"引用的文件是否存在"、查不出"该引的没引"。
        """
        exports = self._component_exports()
        problems = []
        for tpl in _active_templates():
            order = _effective_js_order(tpl)
            loaded = {os.path.basename(r) for r in order}
            for ref in order:
                if not ref.startswith(("pages/", "components/")):
                    continue
                path = os.path.join(JS_DIR, ref.replace("/", os.sep))
                if not os.path.isfile(path):
                    continue  # 悬空引用由另一条守卫负责
                for m in self._YB_USE_RE.finditer(_read(path)):
                    owner = exports.get(m.group(1))
                    if owner and owner not in loaded:
                        problems.append(
                            f"  {os.path.relpath(tpl, BASE)}: {ref} 调用 YB.{m.group(1)}，"
                            f"但未引入 components/{owner}"
                        )
        if problems:
            self.fail(
                "页面脚本调用了未引入的共享组件 —— 会在首行 TypeError、整页交互静默全死：\n"
                + "\n".join(sorted(set(problems)))
            )

    def test_user_ops_batch_limit_matches_backend(self):
        """前端 LIMIT 必须与后端 BATCH_OP_LIMIT 同值（防两处上限漂移）。"""
        ops = _read(os.path.join(JS_DIR, "components", "user-ops.js"))
        m = _JS_LIMIT_RE.search(ops)
        self.assertIsNotNone(m, "user-ops.js 未找到 `var LIMIT = <n>;`")
        app_src = _read(os.path.join(BASE, "web", "app.py"))
        m2 = _PY_BATCH_LIMIT_RE.search(app_src)
        self.assertIsNotNone(m2, "web/app.py 未找到顶层 `BATCH_OP_LIMIT = <n>`")
        self.assertEqual(
            m.group(1), m2.group(1),
            f"前端 LIMIT={m.group(1)} 与后端 BATCH_OP_LIMIT={m2.group(1)} 不一致，"
            "超限请求会直接落到后端 400",
        )


# 分区（tab）机制的共享面：深链助手 + roving tabindex 必须只住在 core.js，
# 页面脚本只允许调用（YB.tabDeepLink / YB.selectTab），不允许再抄一份实现。
# 历史：?tab= 深链最早由 pages/work_settings.js 自带一份（参数名/replaceState 时机
# 页面私有），accounts/users/logs 三页则完全没有 —— 提炼进 core.js 后四页统一，
# 本守卫防止"哪天又有人把 URLSearchParams/replaceState 抄回页面"。
_TAB_DEEPLINK_HELPERS = ("tabFromUrl", "tabSyncUrl", "tabVisible", "selectTab", "tabDeepLink")
# 页面/组件里的第二份深链实现特征（读参数、写参数各一个口径）
_TAB_DEEPLINK_PRIVATE_MARKERS = (
    'URLSearchParams(location.search).get("tab")',
    'searchParams.set("tab"',
)
# 三个"无页面级 tab 管理"的分区页：深链启用只许这一行（settings 自管深链走 YB.selectTab）
_TAB_DEEPLINK_PAGES = ("pages/work_accounts.js", "pages/work_users.js", "pages/data_logs.js")


class TabDeepLinkGuardTest(unittest.TestCase):
    def test_tab_deeplink_and_roving_live_only_in_core(self):
        """`?tab=` 深链与 roving tabindex 必须只由 core.js 承担（全站唯一一份实现）。

        core.js 要提供：tabFromUrl/tabSyncUrl/tabVisible/selectTab/tabDeepLink 五个共享
        助手、参数名钉在 `tab`、activateTab 内同步 roving tabindex（活动 0 其余 -1）、
        键盘方向键走 instant（高频键盘切换跳过面板进入动效）。页面脚本出现第二份
        URLSearchParams/replaceState 深链实现即报红 —— 两份实现会各自漂移（settings 的
        原页面私有版本就是这样长出来的）。
        """
        core = _read(os.path.join(JS_DIR, "core.js"))
        for helper in _TAB_DEEPLINK_HELPERS:
            self.assertIn(
                f"function {helper}(", core,
                f"core.js 缺少共享分区助手 {helper}() —— 深链/可见性校验只允许这一份实现",
            )
        self.assertIn(
            'var TAB_URL_PARAM = "tab";', core,
            "core.js 深链参数名必须钉在 `tab`（settings 原实现的参数名，URL 已对外可见）",
        )
        self.assertIn(
            't.setAttribute("tabindex", on ? "0" : "-1")', core,
            "core.js activateTab 必须同步 roving tabindex（活动 tab 0、其余 -1）——"
            "WAI-ARIA tabs 模式下 Tab 键序只应停在活动分区一个停止点",
        )
        self.assertIn(
            "{ instant: true }", core,
            "core.js 键盘方向键切换必须带 instant（跳过面板进入动效）——"
            "键盘高频切换播 160ms 动画会让面板反复浮动",
        )
        offenders = []
        for dirpath, _dirnames, filenames in os.walk(JS_DIR):
            if os.sep + "vendor" + os.sep in dirpath + os.sep:
                continue
            for name in sorted(filenames):
                if not name.endswith(".js"):
                    continue
                path = os.path.join(dirpath, name)
                rel = os.path.relpath(path, JS_DIR)
                if rel == "core.js":
                    continue
                src = _read(path)
                for marker in _TAB_DEEPLINK_PRIVATE_MARKERS:
                    if marker in src:
                        offenders.append(f"  {rel}: 含 {marker!r}")
        if offenders:
            self.fail(
                "core.js 之外出现 ?tab= 深链的私有实现 —— 读参数/写 URL 只允许"
                " core.js 的 tabFromUrl/tabSyncUrl 一份，页面请改用 YB.tabDeepLink()/"
                "YB.selectTab()（两份实现会各自漂移，历史上 settings 就曾页面私有）：\n"
                + "\n".join(offenders)
            )

    def test_partition_pages_enable_deeplink_via_shared_helper(self):
        """三个分区页必须在 init 里调用 YB.tabDeepLink()（一行启用，不自带实现）。"""
        missing = []
        for rel in _TAB_DEEPLINK_PAGES:
            path = os.path.join(JS_DIR, rel.replace("/", os.sep))
            if "YB.tabDeepLink()" not in _read(path):
                missing.append(f"  {rel}")
        if missing:
            self.fail(
                "分区页未调用 YB.tabDeepLink() —— ?tab= 直链在这些页面会失效"
                "（刷新/分享不保留当前分区）：\n" + "\n".join(missing)
            )


# 改密弹窗的唯一实现钉在 components/change-password.js（YB.changePassword.open）。
# 历史上旧 SPA 外壳（templates/tabs/，P4 已整体退役）曾有两份三字段表单（saveMyPassword /
# saveMinePassword），页面 JS 退役后按钮 onclick 悬空成死 UI；本守卫防止第二份
# 实现借任何载体复活 —— 两份三字段表单会各自漂移校验口径/文案，且新表单不再走
# 统一弹窗的错误显示与 toast/会话轮换流向。
_PASSWORD_CHANGE_ID = "components/change-password.js"
# 自助改密的后端提交口（前端唯一点）：old_password 三元组只允许在唯一实现里出现
_PASSWORD_FIELDS = ("old_password", "new_password")
# 模板层第二份实现的特征：唯一实现的 label 不经模板下发，模板出现即手抄表单
_TEMPLATE_FORM_MARKERS = ("确认新密码", "再次输入新密码", "saveMyPassword", "saveMinePassword")


class ChangePasswordSingleImplementationTest(unittest.TestCase):
    def test_password_change_submit_lives_only_in_shared_component(self):
        """/api/me/password 的请求字段只允许出现在 change-password.js。

        自助改密的校验（口令策略/两次一致）、错误显示（弹窗内 role=alert）与
        成功流向（会话轮换后跳登录页）都耦合在提交逻辑里 —— 绕开共享组件直接
        POST 意味着这些行为各写一份。
        """
        offenders = []
        for dirpath, _dirnames, filenames in os.walk(JS_DIR):
            if os.sep + "vendor" + os.sep in dirpath + os.sep:
                continue
            for name in sorted(filenames):
                if not name.endswith(".js"):
                    continue
                path = os.path.join(dirpath, name)
                rel = os.path.relpath(path, JS_DIR).replace(os.sep, "/")
                if rel == _PASSWORD_CHANGE_ID:
                    continue
                src = _read(path)
                hits = [f for f in _PASSWORD_FIELDS if f in src]
                if hits:
                    offenders.append(f"  static/js/{rel}: 含 {', '.join(hits)}")
        if offenders:
            self.fail(
                "改密请求字段（old_password/new_password）出现在共享组件之外 —— "
                "自助改密只允许 components/change-password.js 一份实现，"
                "新入口请改调 YB.changePassword.open({ builtinAdmin, policyOk? })：\n"
                + "\n".join(offenders)
            )

    def test_templates_host_no_inline_password_change_form(self):
        """模板层不得再出现三字段改密表单或旧 onclick 函数名。"""
        offenders = []
        for dirpath, _dirnames, filenames in os.walk(TEMPLATES_DIR):
            for name in sorted(filenames):
                if not name.endswith(".html"):
                    continue
                path = os.path.join(dirpath, name)
                rel = os.path.relpath(path, BASE)
                # 剥 HTML 注释再匹配：注释里的说明性提及不是实现，只抓活的 markup
                src = re.sub(r"<!--.*?-->", " ", _read(path), flags=re.S)
                for marker in _TEMPLATE_FORM_MARKERS:
                    if marker in src:
                        offenders.append(f"  {rel}: 含 {marker!r}")
        if offenders:
            self.fail(
                "模板里出现改密表单残留/第二实现（「确认新密码」等三字段表单特征或 "
                "已退役的 saveMyPassword/saveMinePassword）—— 弹窗形态、口令策略提示、"
                "成功/失败文案与流向都钉在 components/change-password.js，"
                "改密入口一律给按钮接 YB.changePassword.open(...)：\n"
                + "\n".join(offenders)
            )


if __name__ == "__main__":
    unittest.main()
