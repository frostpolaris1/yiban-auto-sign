# -*- coding: utf-8 -*-
"""登录页契约测试（前端翻新 P2：整页从静态模板 + pages/login.js 迁到 Vue）。

标签：F · 前端与界面守卫（Vue 线新增）
覆盖：/login 渲染契约（挂载点、manifest 资产真实在盘、module 脚本、认证外壳、
      no-store）、**legacy 资产已彻底退役**（模板不再引用 pages/login.js，且文件已从磁盘删除）、
      服务端仍渲染两份协议正文（惰性 `<template>`，Vue 侧零 v-html 的来源）、
      e2e 依赖的契约 id 仍在组件源码里
对应实现：`web/routes/pages.py` 的 `login_page` / `_render_auth_vue_page`、
      `web/templates/login.html`、`frontend/src/login/**`
关键断言：① 页面引用的每个 /static/vue/ 资产必须真实存在于磁盘；② 退役是"连引用一起消失"
      而不只是"不用了"——否则旧脚本会继续被下载、也无人发现它已成死代码；
      ③ **契约 id 必须还在**（e2e 的真表单登录与错误路径按 #username/#password/#login-btn/
      #error-box 取值，改名等于让守卫静默失守，且不会报红）；
      ④ 协议正文仍由服务端渲染进页面（迁到 Vue 后最容易的"顺手"就是改成前端拼 HTML，
      那会把服务端唯一出口的正文变成前端字符串拼接）
依赖：纯本地 Flask test client + 临时 `.env`/SQLite，不联网、无需 node（dist 已入库）
"""
import contextlib
import importlib.util
import json
import os
import re
import shutil
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VUE_DIR = os.path.join(BASE, "web", "static", "vue")
LOGIN_TEMPLATE = os.path.join(BASE, "web", "templates", "login.html")
LEGACY_LOGIN_JS = os.path.join(BASE, "web", "static", "js", "pages", "login.js")
LOGIN_VUE = os.path.join(BASE, "frontend", "src", "login", "Login.vue")

TEST_KEY = "a" * 64
ADMIN_PASS = "TestPass1234!"

#: e2e 与页面行为依赖的契约 id（改名即静默失守——见模块 docstring ③）
CONTRACT_IDS = (
    "username", "password", "login-btn", "error-box",
    "reg-email", "reg-password", "reg-password2", "reg-agree",
    "register-btn", "reg-error-box", "restore-btn", "register-paused-hint",
)


def _asset_fs_path(site_path):
    """/static/vue/assets/x.js → 磁盘绝对路径。"""
    rel = site_path[len("/static/vue/"):]
    return os.path.join(VUE_DIR, rel.replace("/", os.sep))


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class LoginPageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-loginpage-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.accounts_file = os.path.join(cls.tmp, "accounts.json")
        cls.users_file = os.path.join(cls.tmp, "users.json")
        cls.state_dir = os.path.join(cls.tmp, "state")
        cls.log_file = os.path.join(cls.tmp, "sign.log")
        os.makedirs(cls.state_dir, exist_ok=True)
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(
                f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
                f"YIBAN_ADMIN_USER=admin\nYIBAN_ADMIN_PASSWORD={ADMIN_PASS}\n"
            )
        with open(cls.accounts_file, "w", encoding="utf-8") as f:
            json.dump([], f)

        cls._old_env = {k: os.environ.get(k) for k in (
            "YIBAN_ACCOUNTS_KEY", "YIBAN_ENV_FILE", "YIBAN_ACCOUNTS_FILE",
            "YIBAN_USERS_FILE", "YIBAN_DB_FILE", "YIBAN_STATE_DIR", "YIBAN_LOG_FILE",
        )}
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_ACCOUNTS_FILE"] = cls.accounts_file
        os.environ["YIBAN_USERS_FILE"] = cls.users_file
        os.environ["YIBAN_DB_FILE"] = cls.db_file
        os.environ["YIBAN_STATE_DIR"] = cls.state_dir
        os.environ["YIBAN_LOG_FILE"] = cls.log_file

        spec = importlib.util.spec_from_file_location("webapp_loginpage", os.path.join(BASE, "web", "app.py"))
        cls.webapp = importlib.util.module_from_spec(spec)
        sys.modules["webapp_loginpage"] = cls.webapp
        with contextlib.suppress(Exception):
            spec.loader.exec_module(cls.webapp)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k, v in cls._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _html(self):
        """匿名访问 /login 的渲染产物（未登录 → 不触发登录页循环守卫的 302 分支）。"""
        c = self.webapp.create_app().test_client()
        r = c.get("/login")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True)[:400])
        return r.get_data(as_text=True)

    # ---- 渲染契约 ----

    def test_login_renders_mount_point_and_real_assets(self):
        html = self._html()
        self.assertIn('id="vue-login-app"', html, "登录页未渲染 Vue 挂载点")
        self.assertIn('type="module"', html, "登录页未引入 module 脚本")
        # 认证外壳：data-page="auth" 让 core.js 跳过 /api/me 与 /api/clock（匿名页不制造 401 噪音）
        self.assertIn('data-page="auth"', html)
        # 资产必须在盘：哈希文件名写错/漏入库时，这里报红而不是线上 404
        assets = re.findall(r'(?:src|href)="([^"]*?/static/vue/[^"]+)"', html)
        self.assertTrue(assets, "登录页没有引用任何 /static/vue/ 资产")
        for url in assets:
            path = _asset_fs_path(url.split("?")[0])
            self.assertTrue(os.path.isfile(path), f"资产不在磁盘上：{url}")

    def test_doc_templates_are_server_rendered(self):
        """协议/隐私正文必须仍由服务端渲染进惰性 <template>（Vue 侧零 v-html 的来源）。"""
        html = self._html()
        for kind in ("agreement", "privacy"):
            m = re.search(r'<template id="doc-%s">(.*?)</template>' % kind, html, re.S)
            self.assertIsNotNone(m, f"缺少服务端渲染的 doc-{kind} 模板")
            self.assertTrue(m.group(1).strip(), f"doc-{kind} 正文为空（_read_doc_html 未接上？）")
        src = _read(LOGIN_VUE)
        # 判据用**指令形态**（`v-html=`）而不是裸串：组件注释里会解释"本页零 v-html"，
        # 按裸串判会把解释本身判成违规（同 test_web_calendar_parity 剥注释的取舍）。
        self.assertIsNone(re.search(r"\bv-html\s*=", src),
                          "登录页不得使用 v-html（正文来自服务端，只做 cloneNode）")

    def test_legacy_login_js_is_fully_retired(self):
        """退役是"连引用一起消失"：模板不再引用，且文件已从磁盘删除。"""
        html = self._html()
        self.assertNotIn("pages/login.js", html, "登录页仍引用 legacy login.js")
        self.assertFalse(os.path.exists(LEGACY_LOGIN_JS),
                         "web/static/js/pages/login.js 仍留在库里（应随迁移删除）")
        # 新载体必须真的被引入（否则"删了旧的、没接新的"）
        self.assertIn("/static/vue/", html)
        self.assertIn("vue-login-app", _read(os.path.join(BASE, "frontend", "src", "login", "main.ts")),
                      "入口未挂载到共享挂载点")

    def test_contract_ids_survive_in_the_component(self):
        """e2e 依赖的契约 id 必须还在组件源码里（改名即静默失守，不会报红）。"""
        src = _read(LOGIN_VUE)
        missing = [i for i in CONTRACT_IDS if f'id="{i}"' not in src]
        self.assertEqual(missing, [],
                         "登录页组件缺少这些契约 id（e2e 的真表单登录/错误路径按它们取值）："
                         + ", ".join(missing))

    def test_auth_shell_still_loads_the_shared_shell_layer(self):
        """认证外壳仍引 core.js：本页的口令策略/模态/请求层全部复用外壳的单一实现。"""
        layout = _read(os.path.join(BASE, "web", "templates", "layout_auth.html"))
        self.assertIn("/static/js/core.js", layout, "认证外壳未引入 core.js（本页依赖它的公开面）")
        # 本页**不得**再自带一份策略常量（唯一实现在 core.js，跨层对拍见 test_rekey_key_source）
        for forbidden in ("PW_CLASS_PATTERNS", "PW_MIN_LEN =", "PW_POLICY_HINT ="):
            self.assertNotIn(forbidden, _read(LOGIN_TEMPLATE),
                             f"登录页模板出现了策略定义 {forbidden!r}（策略只在 core.js 定义一处）")


if __name__ == "__main__":
    unittest.main()
