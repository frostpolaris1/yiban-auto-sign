# -*- coding: utf-8 -*-
"""审计日志页契约测试（前端翻新 P1 第一个原生新页面）。

标签：F · 前端与界面守卫（Vue 线新增）
覆盖：/data/audit 渲染契约（挂载点、manifest 资产真实在盘、模块脚本形态、no-store、
      admin 守卫、侧栏高亮）、导航项登记，以及 vue_assets("audit.html") 的路径契约
对应实现：`web/routes/pages.py` 的 `audit_page`、`web/services/vue_assets.py`、
      `web/templates/pages/data_audit.html`、`web/templates/partials/sidebar.html`、
      `web/static/vue/`（构建产物，入库）；数据面 `web/routes/audit_api.py`（七约束）
关键断言：① 页面引用的每个 /static/vue/ 资产必须真实存在于磁盘（manifest 漂移即红）；
      ② 审计页必须在侧栏「数据」组可达且自页高亮（aria-current）——它是真实功能页，
      与试点页（不进侧栏）的差别就在这里；③ 页面 HTML 不含任何业务数据（客户端拉取）
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
USER_EMAIL = "audit-page-user@example.com"

db = None  # setUpClass 装载（裸模块名，pyproject pythonpath 已含 scripts）


def _asset_fs_path(site_path):
    """/static/vue/assets/x.js → 磁盘绝对路径。"""
    rel = site_path[len("/static/vue/"):]
    return os.path.join(VUE_DIR, rel.replace("/", os.sep))


class AuditPageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-auditpage-")
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

        spec = importlib.util.spec_from_file_location("webapp_audit", os.path.join(BASE, "web", "app.py"))
        cls.webapp = importlib.util.module_from_spec(spec)
        sys.modules["webapp_audit"] = cls.webapp
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
        self.assertIsNotNone(u, "setUpClass 应已创建审计页测试用户")
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

    def test_audit_renders_mount_point_and_real_assets(self):
        c = self._admin_client()
        r = c.get("/data/audit")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True)[:400])
        html = r.get_data(as_text=True)
        self.assertIn('id="vue-audit-app"', html, "挂载点缺失")

        scripts = [m for m in re.findall(r'<script type="module" src="([^"]+)">', html) if "/static/vue/" in m]
        self.assertTrue(scripts, "审计页必须引入 /static/vue/ 下的 module 脚本")
        referenced = scripts + re.findall(r'href="(/static/vue/[^"]+)"', html)
        for site_path in referenced:
            self.assertTrue(
                os.path.isfile(_asset_fs_path(site_path)),
                f"页面引用的 Vue 资产不存在：{site_path}（dist 与 manifest 不同步？）",
            )

    def test_audit_page_is_no_store(self):
        c = self._admin_client()
        r = c.get("/data/audit")
        self.assertEqual(r.headers.get("Cache-Control"), "no-store", "页面必须禁缓存")

    def test_audit_data_is_client_rendered_only(self):
        """数据面全部客户端拉取：服务端 HTML 只有一个挂载点，不含任何表格行标记。"""
        c = self._admin_client()
        html = c.get("/data/audit").get_data(as_text=True)
        self.assertEqual(html.count('id="vue-audit-app"'), 1, "挂载点应恰好一处")
        self.assertNotIn("<table", html, "表格必须由 Vue 客户端渲染，不得服务端出表")
        self.assertNotIn("<tr", html, "不得服务端出表格行")

    # ---- 导航可达性（与试点页的关键差别）----

    def test_audit_is_reachable_and_highlighted_in_sidebar(self):
        c = self._admin_client()
        html = c.get("/data/audit").get_data(as_text=True)
        self.assertIn('href="/data/audit"', html, "审计页必须在侧栏可达")
        # nav_active == 'data-audit' → 该项自身高亮（aria-current="page"）
        link = re.search(r'<a class="nav-link[^"]*"[^>]*href="/data/audit"[^>]*>', html)
        self.assertIsNotNone(link, "未找到审计页导航项")
        self.assertIn('aria-current="page"', link.group(0), "当前页导航项应带 aria-current")

    # ---- 守卫 ----

    def test_audit_redirects_anonymous_to_login(self):
        c = self.webapp.create_app().test_client()
        r = c.get("/data/audit")
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers.get("Location", "").endswith("/login"))

    def test_audit_redirects_user_role_away(self):
        c = self._user_client()
        r = c.get("/data/audit")
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers.get("Location", "").endswith("/user/calendar"))

    # ---- vue_assets 契约（第二个入口）----

    def test_vue_assets_resolves_audit_entry(self):
        from web.services.vue_assets import vue_assets

        assets = vue_assets("audit.html")
        self.assertTrue(assets["js"], "manifest 存在时应能解析出审计页入口 js")
        self.assertTrue(assets["css"], "审计页应带自己的样式文件")
        for group in ("js", "preloads", "css"):
            for path in assets[group]:
                self.assertTrue(path.startswith("/static/vue/"), f"{group} 路径前缀不对：{path}")
                self.assertTrue(os.path.isfile(_asset_fs_path(path)), f"清单资产不在盘：{path}")

    # ---- 跨边界契约：前端查询形态 ↔ 后端信封（两侧单测都覆盖不到的那一段）----

    def test_audit_api_accepts_frontend_query_shape_and_envelope(self):
        """按 `frontend/src/audit/query.ts` 构造出的形态直接打真实端点。

        `URLSearchParams` 把空格编码为 `+`（表单编码），Werkzeug 应按空格解码——
        这条钉住该假设；同时钉住响应信封的五个键与 `frontend/src/audit/paging.ts`
        消费的字段逐一对应（契约第 2 条），任一侧改名即红。
        """
        c = self._admin_client()
        # 与 query.ts 的 buildAuditQuery 同形态：page/page_size + 日期补全后的 from_ts/to_ts
        qs = (
            "/api/audit-logs?page=1&page_size=50"
            "&action=login_success&actor=138****8000&target=acct-1"
            "&from_ts=2026-10-01+00%3A00%3A00&to_ts=2026-10-02+23%3A59%3A59"
        )
        r = c.get(qs)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True)[:300])
        body = r.get_json()
        for key in ("rows", "page", "page_size", "total", "has_more"):
            self.assertIn(key, body, f"信封缺少 paging.ts 消费的键：{key}")
        self.assertEqual(body["page"], 1)
        self.assertEqual(body["page_size"], 50)
        self.assertIsInstance(body["rows"], list)
        self.assertIsInstance(body["has_more"], bool)

    def test_audit_api_rejects_unknown_filter_key(self):
        """契约第 3 条：白名单外的查询键一律 400 JSON（前端据此保证只发白名单键）。"""
        c = self._admin_client()
        r = c.get("/api/audit-logs?page=1&page_size=50&evil=1")
        self.assertEqual(r.status_code, 400)
        self.assertIn("不支持的过滤字段", r.get_json()["error"])


if __name__ == "__main__":
    unittest.main()
