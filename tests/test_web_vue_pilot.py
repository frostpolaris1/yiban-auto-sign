# -*- coding: utf-8 -*-
"""Vue 翻新试点页契约测试（P0 管线守卫，docs/refactor/29-frontend-vue-refactor-plan.md）。

标签：F · 前端与界面守卫（Vue 线新增，33 文件处置表之外的第一个新范式测试）
覆盖：/work/pilot 渲染契约（挂载点、manifest 解析出的哈希资产真实在盘、模块脚本形态、
      no-store、admin 守卫）与 dist 静态 CSP 检查（产物内无 eval / new Function）
对应实现：`web/routes/pages.py` 的 `vue_pilot_page`、`web/services/vue_assets.py`、
      `web/templates/pages/work_vue_pilot.html`、`web/static/vue/`（构建产物，入库）
关键断言：① 试页 HTML 引用的每个 /static/vue/ 资产都必须真实存在于磁盘（manifest 漂移
      即报错，防「模板引用了没构建的 chunk」）；② 产物 JS 零 eval/new Function——
      CSP `script-src 'self'`（无 unsafe-eval）的静态前提，runtime-only 构建被换掉时此测试先红
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
import time
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VUE_DIR = os.path.join(BASE, "web", "static", "vue")

TEST_KEY = "a" * 64
ADMIN_PASS = "TestPass1234!"
USER_PASS = "UserPass123!"
USER_EMAIL = "pilot-user@example.com"

# ---- CSP 静态检查：runtime-only 构建的硬前提 ----
_EVAL_RE = re.compile(r"\beval\s*\(|\bnew\s+Function\b")

db = None  # setUpClass 装载（裸模块名，pyproject pythonpath 已含 scripts）


def _asset_fs_path(site_path):
    """/static/vue/assets/x.js → 磁盘绝对路径。"""
    rel = site_path[len("/static/vue/"):]
    return os.path.join(VUE_DIR, rel.replace("/", os.sep))


class VuePilotPageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-vuepilot-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.accounts_file = os.path.join(cls.tmp, "accounts.json")
        cls.users_file = os.path.join(cls.tmp, "users.json")
        cls.state_dir = os.path.join(cls.tmp, "state")
        os.makedirs(cls.state_dir, exist_ok=True)
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(
                f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
                f"YIBAN_ADMIN_USER=admin\nYIBAN_ADMIN_PASSWORD={ADMIN_PASS}\n"
            )
        with open(cls.accounts_file, "w", encoding="utf-8") as f:
            json.dump([], f)

        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_ACCOUNTS_FILE"] = cls.accounts_file
        os.environ["YIBAN_USERS_FILE"] = cls.users_file
        os.environ["YIBAN_DB_FILE"] = cls.db_file
        os.environ["YIBAN_STATE_DIR"] = cls.state_dir

        global db
        import db  # 裸模块名：pyproject 的 pythonpath 已含 scripts

        spec = importlib.util.spec_from_file_location("webapp_pilot", os.path.join(BASE, "web", "app.py"))
        cls.webapp = importlib.util.module_from_spec(spec)
        sys.modules["webapp_pilot"] = cls.webapp
        with contextlib.suppress(Exception):
            spec.loader.exec_module(cls.webapp)

        db.init_db(cls.db_file, migrate_from=cls.accounts_file, env_file=cls.env_file)
        db.create_user(USER_EMAIL, cls.webapp.generate_password_hash(USER_PASS))

    @classmethod
    def tearDownClass(cls):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k in (
            "YIBAN_ACCOUNTS_KEY", "YIBAN_ENV_FILE", "YIBAN_ACCOUNTS_FILE",
            "YIBAN_USERS_FILE", "YIBAN_DB_FILE", "YIBAN_STATE_DIR",
        ):
            os.environ.pop(k, None)

    def _admin_client(self):
        c = self.webapp.create_app().test_client()
        r = c.post("/api/login", json={"username": "admin", "password": ADMIN_PASS})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True)[:400])
        return c

    def _user_client(self):
        u = db.find_user(USER_EMAIL)
        self.assertIsNotNone(u, "setUpClass 应已创建试点用户")
        c = self.webapp.create_app().test_client()
        with c.session_transaction() as s:
            s["auth"] = True
            s["role"] = "user"
            s["username"] = USER_EMAIL.lower()
            s["auth_source"] = "user"
            s["pw_version"] = u.get("pw_version", 1)
            s["login_ts"] = int(time.time())
            s["sid"] = "0" * 32
        return c

    # ---- 渲染契约 ----

    def test_pilot_renders_mount_point_and_real_assets(self):
        c = self._admin_client()
        r = c.get("/work/pilot")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True)[:400])
        html = r.get_data(as_text=True)
        self.assertIn('id="vue-pilot-app"', html, "挂载点缺失")

        # 模块脚本：入口 js 引用必须来自 manifest（哈希文件名、/static/vue/ 下）
        script_re = re.compile(r'<script type="module" src="([^"]+)">')
        scripts = [m for m in script_re.findall(html) if "/static/vue/" in m]
        self.assertTrue(scripts, "试点页必须引入 /static/vue/ 下的 module 脚本")

        # 引用到的每个资产都必须真实在盘（manifest 漂移 → 这里红）
        referenced = scripts + re.findall(r'href="(/static/vue/[^"]+)"', html)
        self.assertTrue(referenced)
        for site_path in referenced:
            self.assertTrue(
                os.path.isfile(_asset_fs_path(site_path)),
                f"页面引用的 Vue 资产不存在：{site_path}（dist 与 manifest 不同步？）",
            )

    def test_pilot_page_is_no_store(self):
        c = self._admin_client()
        r = c.get("/work/pilot")
        self.assertEqual(r.headers.get("Cache-Control"), "no-store", "页面必须禁缓存")

    # ---- 守卫 ----

    def test_pilot_redirects_anonymous_to_login(self):
        c = self.webapp.create_app().test_client()
        r = c.get("/work/pilot")
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers.get("Location", "").endswith("/login"))

    def test_pilot_redirects_user_role_away(self):
        c = self._user_client()
        r = c.get("/work/pilot")
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers.get("Location", "").endswith("/user/calendar"))

    # ---- dist 静态 CSP 检查 ----

    def test_dist_has_no_eval_or_new_function(self):
        assets_dir = os.path.join(VUE_DIR, "assets")
        js_files = [f for f in os.listdir(assets_dir) if f.endswith(".js")] if os.path.isdir(assets_dir) else []
        self.assertTrue(js_files, "web/static/vue/assets 下没有 JS 产物（前端未构建入库？）")
        for name in js_files:
            with open(os.path.join(assets_dir, name), encoding="utf-8", errors="replace") as fh:
                content = fh.read()
            hit = _EVAL_RE.search(content)
            self.assertIsNone(
                hit,
                f"{name} 命中 {_EVAL_RE.pattern} 的 {hit.group(0) if hit else ''}——"
                "CSP 无 unsafe-eval 的前提被破坏（是否换成了浏览器内模板编译的 Vue full build？）",
            )

    # ---- vue_assets 助手契约 ----

    def test_vue_assets_paths_and_missing_entry(self):
        from web.services.vue_assets import vue_assets

        assets = vue_assets("index.html")
        self.assertTrue(assets["js"], "manifest 存在时应能解析出入口 js")
        for group in ("js", "preloads", "css"):
            for path in assets[group]:
                self.assertTrue(path.startswith("/static/vue/"), f"{group} 路径必须以 /static/vue/ 开头：{path}")
                self.assertTrue(os.path.isfile(_asset_fs_path(path)), f"清单资产不在盘：{path}")
        self.assertEqual(vue_assets("no-such-entry.html")["js"], [], "未知入口必须返回空表")


if __name__ == "__main__":
    unittest.main()
