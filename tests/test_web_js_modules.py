# -*- coding: utf-8 -*-
"""前端脚本安全与单一实现守卫（MPA + 组件化形态）。

标签：F · 前端与界面守卫
覆盖：`innerHTML` 与裸 fetch 约束（页面零 innerHTML、组件仅常量 SVG 白名单）、`user-ops.js` 不得上屏服务端 msg、SMTP 编辑不得回填遮罩值；`?tab=` 深链与 roving tabindex 只住 core.js（唯一实现）；自助改密只有一份实现
对应实现：`web/static/js/core.js` 与 `pages/*.js`、`components/*.js`、各 `layout_*.html` / `pages/*.html`；模板 markup
关键断言：`pages/*.js` 零 `.innerHTML`、不得裸用 `fetch`（`YB.api` 才带 CSRF/统一错误/401 跳转）；`components/user-ops.js` 不得出现 `data.msg`（后端成功 msg 含完整邮箱）；`maskedCellInput` 体内不得出现 `.value`（打码值只许作 placeholder）；core.js 之外出现第二份 URLSearchParams/replaceState 深链实现或 old_password/new_password 字段即报红
依赖：纯本地——读模板与 JS 源码文本，**不执行 JS、无需 node**、不联网

> 批 6c3-A 说明：旧 `JsAssemblyGuardTest` 里的 9 条**纯装配**静态扫描（模块清单
> 存在性、悬空 src、重复 id、core.js 加载顺序、顶层声明重名、旧栈标记、加载态
> 终止、组件引入齐全、前端 LIMIT 对拍）按对表裁撤——它们是「精确源码 grep」型
> 守卫且无行为 owner；本文件只保留注入/泄漏面（ND-6）与"单一实现"两类契约守卫。
"""

import glob
import os
import re
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS_DIR = os.path.join(BASE, "web", "static", "js")
TEMPLATES_DIR = os.path.join(BASE, "web", "templates")

# 组件里 innerHTML 赋值的右值：必须是常量 SVG（svg(...)）或清空容器（""/''）
_INNERHTML_ASSIGN_RE = re.compile(r"\.innerHTML\s*=\s*([^\n;]+)")
_SVG_RHS_RE = re.compile(r"^\s*svg\(")


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class JsAssemblyGuardTest(unittest.TestCase):
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

        单目标 role/password/delete 的成功 msg 回显遮罩邮箱；本组件仍一律只用本地无 PII
        文案，不依赖服务端脱敏口径，故不得出现 `data.msg`。batch/purge 的计数型 msg
        经 `successMsg(..., true)` 走 `data["msg"]`，不在本禁例内。
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
