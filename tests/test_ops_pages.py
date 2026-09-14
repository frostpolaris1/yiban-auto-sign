"""运营面收口：robots.txt、自定义 404/500 错误页、静态/接口 404 的分流。

- /robots.txt：只放行登录页与静态资源，其余全禁；子路径部署下带挂载前缀
- 404：匿名 → 给「去登录」；已登录管理员 → 给「返回总览」；用户 → 「返回签到日历」
- 404 分流：/api/ 返回 JSON、静态扩展名返回空响应（都不渲染 HTML 页面）
- 500：非 RuntimeError 的未捕获异常渲染错误页；/api/ 仍返回 JSON
"""
import contextlib
import importlib.util
import os
import shutil
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEST_KEY = "a" * 64
ADMIN_PASS = "TestPass12345"
SUBPATH = "/tool/yiban-auto-sign/demo"


class OpsPagesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-ops-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_DB_FILE"] = os.path.join(cls.tmp, "yiban.db")
        os.environ["YIBAN_STATE_DIR"] = os.path.join(cls.tmp, "state")
        os.environ["YIBAN_LOG_FILE"] = os.path.join(cls.tmp, "sign.log")
        os.environ["YIBAN_DISABLE_PURGE_LOOP"] = "1"
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(
                f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
                f"YIBAN_ADMIN_USER=admin\nYIBAN_ADMIN_PASSWORD={ADMIN_PASS}\n"
            )
        spec = importlib.util.spec_from_file_location(
            "webapp_ops", os.path.join(BASE, "web", "app.py"))
        cls.webapp = importlib.util.module_from_spec(spec)
        sys.modules["webapp_ops"] = cls.webapp
        with contextlib.suppress(Exception):
            spec.loader.exec_module(cls.webapp)
        cls.app = cls.webapp.create_app()
        cls.app.testing = False  # 走真实错误处理路径（TESTING 下异常会直接抛出）
        cls.client = cls.app.test_client()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k in ("YIBAN_ENV_FILE", "YIBAN_DB_FILE", "YIBAN_STATE_DIR",
                  "YIBAN_LOG_FILE", "YIBAN_DISABLE_PURGE_LOOP"):
            os.environ.pop(k, None)

    def _login(self, username="admin", password=ADMIN_PASS, client=None):
        c = client or self.app.test_client()
        r = c.post("/api/login", json={"username": username, "password": password})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True)[:200])
        return c

    # ---- robots.txt ----

    def test_robots_disallows_private_pages(self):
        r = self.client.get("/robots.txt")
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/plain", r.headers.get("Content-Type", ""))
        body = r.get_data(as_text=True)
        self.assertIn("User-agent: *", body)
        self.assertIn("Disallow: /", body)
        self.assertIn("Allow: /login", body)
        self.assertIn("Allow: /static/", body)

    def test_robots_under_subpath_carries_prefix(self):
        r = self.client.get(SUBPATH + "/robots.txt")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True)[:200])
        body = r.get_data(as_text=True)
        self.assertIn(f"Allow: {SUBPATH}/login", body)
        self.assertIn(f"Allow: {SUBPATH}/static/", body)

    # ---- 404 ----

    def test_404_anonymous_offers_login(self):
        r = self.client.get("/no-such-page")
        self.assertEqual(r.status_code, 404)
        body = r.get_data(as_text=True)
        self.assertIn("404", body)
        self.assertIn("页面不存在", body)
        self.assertIn('href="/login"', body)
        self.assertNotIn("返回总览", body)

    def test_404_admin_offers_dashboard(self):
        c = self._login()
        body = c.get("/no-such-page").get_data(as_text=True)
        self.assertIn('href="/data/dashboard"', body)
        self.assertIn("返回总览", body)

    def test_404_api_returns_json(self):
        # 未知 /api/ 路径：未登录先被鉴权守卫拦成 401，登录后才是 404 JSON
        self.assertEqual(self.client.get("/api/no-such-endpoint").status_code, 401)
        c = self._login()
        r = c.get("/api/no-such-endpoint")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.mimetype, "application/json")
        self.assertIn("接口不存在", r.get_json()["error"])

    def test_404_static_like_path_is_opaque(self):
        # 静态资源 404 不渲染整页 HTML：浏览器/爬虫只需空响应
        r = self.client.get("/static/vendor/not-here.png")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.get_data(), b"")
        self.assertNotIn("text/html", r.headers.get("Content-Type", ""))

    # ---- 500 ----

    def _fresh_app_with_boom(self, path, endpoint):
        """500 用例需要未处理异常的处理器：Flask 禁止首个请求后再 add_url_rule，
        故每条用例单独建 app 实例（路由在首个请求前注册）。"""
        app = self.webapp.create_app()
        app.testing = False

        def _boom():
            raise ValueError("boom")

        app.add_url_rule(path, endpoint, _boom)
        return app

    def test_500_renders_error_page(self):
        r = self._fresh_app_with_boom("/__boom__", "boom_page").test_client().get("/__boom__")
        self.assertEqual(r.status_code, 500)
        body = r.get_data(as_text=True)
        self.assertIn("服务器内部错误", body)
        self.assertIn("500", body)

    def test_500_api_returns_json(self):
        c = self._fresh_app_with_boom("/api/__boom__", "boom_api").test_client()
        self._login(client=c)
        r = c.get("/api/__boom__")
        self.assertEqual(r.status_code, 500)
        self.assertEqual(r.mimetype, "application/json")
        self.assertIn("服务器内部错误", r.get_json()["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
