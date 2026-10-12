# -*- coding: utf-8 -*-
"""数据看板页契约测试（前端翻新 P3：整页从静态模板 + pages/data_dashboard.js 迁到 Vue）。

标签：F · 前端与界面守卫（Vue 线新增）
覆盖：/data/dashboard 渲染契约（挂载点、manifest 资产真实在盘、module 脚本、admin 守卫、
      no-store）、**legacy 资产已彻底退役**（模板不再引用 data_dashboard.js，且文件已从磁盘
      删除）、Chart.js 仍是内置 vendor（defer 先行加载，与模块入口同队列）、内联载荷
      window.YB_DASHBOARD_STATE（热力图脚注基线）、e2e 依赖的 id 钩子仍在组件里、
      零 v-html / 手机号不进 DOM（服务端单出口脱敏）。
对应实现：`web/routes/pages.py` 的 `dashboard_page` / `_dashboard_page_context` /
      `_render_vue_page`、`web/templates/pages/data_dashboard.html`、
      `frontend/src/dashboard/**`
关键断言：① 页面引用的每个 /static/vue/ 资产必须真实存在于磁盘；② 退役是"连引用一起消失"
      而不只是"不用了"；③ 零 v-html，且组件不渲染/不拼接手机号（脱敏是服务端单出口）；
      ④ e2e 依赖的 id 钩子（状态条 / 三图 / 热力图 / KPI）仍在组件里。
依赖：纯本地 Flask test client + 临时 `.env`/SQLite，不联网、无需 node（dist 已入库）。
      口径函数的**真跑**由 tests/test_dashboard_stats_caliber_js.py 抽 model.js 到 node 钉住。
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
DASH_TEMPLATE = os.path.join(BASE, "web", "templates", "pages", "data_dashboard.html")
DASH_VUE = os.path.join(BASE, "frontend", "src", "dashboard", "DashboardPage.vue")
DASH_KPI = os.path.join(BASE, "frontend", "src", "dashboard", "KpiCard.vue")
DASH_MODEL = os.path.join(BASE, "frontend", "src", "dashboard", "model.js")
LEGACY_DASH_JS = os.path.join(BASE, "web", "static", "js", "pages", "data_dashboard.js")
CHART_VENDOR = os.path.join(BASE, "web", "static", "vendor", "chartjs", "chart.min.js")

TEST_KEY = "a" * 64
ADMIN_PASS = "TestPass1234!"

#: e2e 与页面行为依赖的 id 钩子（改名即静默失守——见模块 docstring ④）。
#: 数据看板沿用 legacy 的 DOM id，故 e2e 选择器跨两栈一致。
CONTRACT_HOOKS = (
    'id="dash-status"',
    'id="dash-retry-btn"',
    'k="rate"',
    "kpi-' + k + '-value",
    'id="chart-trend"',
    'id="chart-dist"',
    'id="chart-slots"',
    'id="mini-cal"',
    'id="cal-prev"',
    'id="cal-next"',
    'id="cal-note"',
    'id="ping-btn"',
    'id="health-clock"',
)


def _asset_fs_path(site_path):
    rel = site_path[len("/static/vue/"):]
    return os.path.join(VUE_DIR, rel.replace("/", os.sep))


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class DashboardPageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-dashpage-")
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

        spec = importlib.util.spec_from_file_location("webapp_dashpage", os.path.join(BASE, "web", "app.py"))
        cls.webapp = importlib.util.module_from_spec(spec)
        sys.modules["webapp_dashpage"] = cls.webapp
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
        r = c.get("/data/dashboard")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True)[:400])
        return r.get_data(as_text=True)

    # ---- 渲染契约 ----

    def test_dashboard_renders_mount_point_and_real_assets(self):
        html = self._html()
        self.assertIn('id="vue-dashboard-app"', html, "数据看板页未渲染 Vue 挂载点")
        self.assertIn('type="module"', html, "未引入 module 脚本")
        assets = re.findall(r'(?:src|href)="([^"]*?/static/vue/[^"]+)"', html)
        self.assertTrue(assets, "数据看板页没有引用任何 /static/vue/ 资产")
        for url in assets:
            path = _asset_fs_path(url.split("?")[0])
            self.assertTrue(os.path.isfile(path), f"资产不在磁盘上：{url}")

    def test_admin_guard_and_no_store(self):
        """/data/dashboard 仍是管理端页面：未登录被弹回登录页。"""
        c = self.webapp.create_app().test_client()
        r = c.get("/data/dashboard")
        self.assertIn(r.status_code, (301, 302), "未登录访问数据看板页应被守卫重定向")
        # no-store：页面禁缓存，防浏览器缓存旧版 JS 造成登录循环
        html_resp = None
        c2 = self.webapp.create_app().test_client()
        c2.post("/api/login", json={"username": "admin", "password": ADMIN_PASS})
        html_resp = c2.get("/data/dashboard")
        self.assertIn("no-store", html_resp.headers.get("Cache-Control", ""))

    def test_inline_state_payload_and_chart_vendor(self):
        """内联载荷下发脚注基线；Chart.js 仍走内置 vendor（defer 先行加载）。"""
        html = self._html()
        self.assertIn("window.YB_DASHBOARD_STATE = {", html)
        self.assertIn('"cal_note": "', html, "内联载荷必须带 cal_note（脚注基线文案）")
        self.assertIn("/static/vendor/chartjs/chart.min.js", html, "Chart.js 仍由模板加载")
        self.assertTrue(os.path.isfile(CHART_VENDOR), "chart.min.js 被误删")
        # defer 必须在 module 之前（否则组件取不到 window.Chart）
        self.assertLess(
            html.index("/static/vendor/chartjs/chart.min.js"),
            html.index("type=\"module\""),
            "Chart.js 未排在模块入口之前——组件执行时 window.Chart 尚不存在",
        )

    # ---- 退役 ----

    def test_legacy_dashboard_asset_is_fully_retired(self):
        html = self._html()
        self.assertNotIn("pages/data_dashboard.js", html, "模板仍引用 legacy data_dashboard.js")
        self.assertFalse(os.path.exists(LEGACY_DASH_JS), "data_dashboard.js 仍留在库里")

    def test_contract_hooks_survive_in_the_component(self):
        # id 钩子分布在两个组件文件里（页面骨架 + KPI 卡子组件），合并检查
        src = _read(DASH_VUE) + "\n" + _read(DASH_KPI)
        missing = [h for h in CONTRACT_HOOKS if h not in src]
        self.assertEqual(missing, [], "组件缺少这些 id 钩子（e2e 按它们取值）：" + ", ".join(missing))

    # ---- 注入 / PII 出口面 ----

    def test_component_is_v_html_free_and_never_renders_phones(self):
        """零 v-html；组件不插值手机号（脱敏是服务端单出口，本页只计数、不展示号码）。

        与 `test_contract_hooks_survive_in_the_component` 同一口径：id 钩子与注入面都分布在
        页面骨架 + KPI 卡两个组件里，故合并扫描（只读其一会漏掉另一半）。
        """
        src = _read(DASH_VUE) + "\n" + _read(DASH_KPI)
        self.assertIsNone(re.search(r"\bv-html\s*=", src), "数据看板页不得使用 v-html")
        self.assertNotIn("innerHTML", src, "不得用 innerHTML 注入（动态文本走插值）")
        self.assertIsNone(
            re.search(r"\{\{[^}]*\bphone\b", src),
            "组件直接插值 phone —— 手机号展示必须走服务端脱敏出口，本页不应渲染号码",
        )

    def test_model_has_no_dom_dependency(self):
        """口径层必须纯（零 DOM）——它要被 Python 侧抽到 node 里真跑。

        先剥注释再查：文件头注释里会出现 `window.YB_DASHBOARD_STATE` 之类的**说明性**指代，
        那不是依赖。判据只看活代码。
        """
        src = _read(DASH_MODEL)
        code = re.sub(r"/\*.*?\*/", " ", src, flags=re.S)
        code = re.sub(r"//[^\n]*", " ", code)
        for banned in ("document.", "window.", "getComputedStyle", "innerHTML"):
            self.assertNotIn(banned, code, f"model.js 出现 {banned} —— 口径层必须保持纯函数")


if __name__ == "__main__":
    unittest.main()
