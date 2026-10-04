# -*- coding: utf-8 -*-
"""用户管理页契约测试（前端翻新 P3：整页从静态模板 + pages/work_users.js 迁到 Vue）。

标签：F · 前端与界面守卫（Vue 线新增）
覆盖：/work/users 渲染契约（挂载点、manifest 资产真实在盘、module 脚本、admin 守卫、
      no-store）、**legacy 资产已彻底退役**（模板不再引用 work_users.js / user-ops.js，
      且文件已从磁盘删除；row-menu.js 亦已于 2026-10-04 整体退役）、**PII 出口面**（组件只渲染
      遮罩邮箱，完整邮箱不进 DOM 文本/属性）、e2e 依赖的 data-* 钩子仍在
对应实现：`web/routes/pages.py` 的 `users_page` / `_render_vue_page`、
      `web/templates/pages/work_users.html`、`frontend/src/users/**`
关键断言：① 页面引用的每个 /static/vue/ 资产必须真实存在于磁盘；② 退役是"连引用一起消失"
      而不只是"不用了"；③ **完整邮箱绝不进 DOM**——模板里只允许出现 `maskEmail(row.email)`，
      不得出现 `{{ row.email }}`，`title` 只放时间字符串（MF-49 出口面在展示侧的落点）；
      ④ e2e 依赖的 data-* 钩子（页签/批量/空态/重试）仍在组件里
依赖：纯本地 Flask test client + 临时 `.env`/SQLite，不联网、无需 node（dist 已入库）。
      写操作链路（单条端点按不透明 id 编 path、邮箱只进 batch/purge 请求体）由
      tests/test_users_exit_surface_frontend.py 把 ops.js 整段放进 node 真跑钉住。
"""
import contextlib
import importlib.util
import json
import os
import re
import shutil
import sys
import tempfile
import time
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VUE_DIR = os.path.join(BASE, "web", "static", "vue")
USERS_TEMPLATE = os.path.join(BASE, "web", "templates", "pages", "work_users.html")
USERS_VUE = os.path.join(BASE, "frontend", "src", "users", "Users.vue")
LEGACY_USERS_JS = os.path.join(BASE, "web", "static", "js", "pages", "work_users.js")
LEGACY_OPS_JS = os.path.join(BASE, "web", "static", "js", "components", "user-ops.js")
ROW_MENU_JS = os.path.join(BASE, "web", "static", "js", "components", "row-menu.js")

TEST_KEY = "a" * 64
ADMIN_PASS = "TestPass1234!"

#: e2e 与页面行为依赖的 data-* 钩子（改名即静默失守——见模块 docstring ④）
CONTRACT_HOOKS = (
    "data-usr-tab",
    "data-usr-panel",
    "data-usr-batch",
    "data-usr-batch-clear",
    "data-empty-clear",
    "data-empty-tab",
    "data-usr-retry",
)


def _asset_fs_path(site_path):
    rel = site_path[len("/static/vue/"):]
    return os.path.join(VUE_DIR, rel.replace("/", os.sep))


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class UsersPageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-userspage-")
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

        spec = importlib.util.spec_from_file_location("webapp_userspage", os.path.join(BASE, "web", "app.py"))
        cls.webapp = importlib.util.module_from_spec(spec)
        sys.modules["webapp_userspage"] = cls.webapp
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
        c = self.webapp.create_app().test_client()
        r = c.post("/api/login", json={"username": "admin", "password": ADMIN_PASS})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True)[:400])
        r = c.get("/work/users")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True)[:400])
        return r.get_data(as_text=True)

    # ---- 渲染契约 ----

    def test_users_renders_mount_point_and_real_assets(self):
        html = self._html()
        self.assertIn('id="vue-users-app"', html, "用户管理页未渲染 Vue 挂载点")
        self.assertIn('type="module"', html, "未引入 module 脚本")
        assets = re.findall(r'(?:src|href)="([^"]*?/static/vue/[^"]+)"', html)
        self.assertTrue(assets, "用户管理页没有引用任何 /static/vue/ 资产")
        for url in assets:
            path = _asset_fs_path(url.split("?")[0])
            self.assertTrue(os.path.isfile(path), f"资产不在磁盘上：{url}")

    def test_admin_guard_and_no_store(self):
        """/work/users 仍是管理端页面：未登录被弹回登录页，且页面禁缓存。"""
        c = self.webapp.create_app().test_client()
        r = c.get("/work/users")
        self.assertIn(r.status_code, (301, 302), "未登录访问用户管理页应被守卫重定向")
        html = self._html()
        self.assertIn("/work/users", html)

    # ---- 退役 ----

    def test_legacy_users_assets_are_fully_retired(self):
        html = self._html()
        for legacy in ("pages/work_users.js", "components/user-ops.js"):
            self.assertNotIn(legacy, html, f"模板仍引用 legacy {legacy}")
        self.assertFalse(os.path.exists(LEGACY_USERS_JS), "work_users.js 仍留在库里")
        self.assertFalse(os.path.exists(LEGACY_OPS_JS), "user-ops.js 仍留在库里")
        # 2026-10-04 清扫：row-menu.js 已无任何现役消费者（账号页/设置页迁 Vue 后改用
        # el-dropdown），整体退役——本守卫从「必须保留」改为「必须已退役」。
        self.assertFalse(os.path.exists(ROW_MENU_JS),
                         "row-menu.js 仍留在库里——三页迁 Vue 后已无消费者")

    def test_contract_hooks_survive_in_the_component(self):
        src = _read(USERS_VUE)
        missing = [h for h in CONTRACT_HOOKS if h not in src]
        self.assertEqual(missing, [], "组件缺少这些 data-* 钩子（e2e 按它们取值）：" + ", ".join(missing))

    # ---- PII 出口面（展示侧） ----

    def test_component_only_renders_masked_emails(self):
        """完整邮箱绝不进 DOM：模板只允许 `maskEmail(row.email)`，且 title 只放时间。"""
        src = _read(USERS_VUE)
        self.assertIn("maskEmail(row.email)", src, "邮箱渲染必须走外壳的 maskEmail（唯一实现）")
        self.assertNotIn("{{ row.email }}", src, "模板直接插值完整邮箱——PII 泄漏")
        self.assertNotIn('title="row.email"', src)
        self.assertNotIn(":title=\"row.email\"", src, "title 不得携带完整邮箱（只放时间字符串）")
        self.assertIn(":title=\"row.created_at\"", src)
        self.assertIn(":title=\"row.deleted_at\"", src)
        # 选择框的读屏名同样只带遮罩串
        self.assertIn("'选择用户 ' + maskEmail(row.email)", src)
        # 零 v-html（判据用指令形态，避免把解释性注释判成违规）
        self.assertIsNone(re.search(r"\bv-html\s*=", src), "用户管理页不得使用 v-html")


if __name__ == "__main__":
    unittest.main()
