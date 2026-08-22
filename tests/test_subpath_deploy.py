# -*- coding: utf-8 -*-
"""子路径 / 独立子域 前缀自适应部署契约回归测试（2026-08-23）。

背景：应用可部署在域名根、独立子域、或主站子路径（如 /tools/yiban-auto-sign/demo/）下。
反向代理只需把完整 URI 原样透传，BasePathMiddleware 自动感知前缀（见 web/app.py 类注释）。
本测试锁定三类关键契约：
  1. 根路径部署行为与改造前完全一致（不回归）；
  2. 子路径下跳转/静态/API 都带前缀，登录后可正常渲染；
  3. 自动探测 / SCRIPT_NAME / YIBAN_BASE_PATH 的前缀判定。

用法：py -m pytest tests/test_subpath_deploy.py -v
"""
import contextlib
import importlib.util
import os
import shutil
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "scripts"))
sys.path.insert(0, os.path.join(BASE, "web"))

TEST_KEY = "a" * 64
P = "/tools/yiban-auto-sign/demo"


class SubpathDeployTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-subpath-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n")
            f.write("YIBAN_ADMIN_USER=admin\n")
            f.write("YIBAN_ADMIN_PASSWORD=TestPass12345\n")
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_ACCOUNTS_FILE"] = os.path.join(cls.tmp, "accounts.json")
        os.environ["YIBAN_USERS_FILE"] = os.path.join(cls.tmp, "users.json")
        os.environ["YIBAN_DB_FILE"] = os.path.join(cls.tmp, "yiban.db")
        os.environ["YIBAN_STATE_DIR"] = cls.tmp
        os.environ["YIBAN_LOG_FILE"] = os.path.join(cls.tmp, "sign.log")
        os.environ.pop("YIBAN_BASE_PATH", None)
        spec = importlib.util.spec_from_file_location("webapp", os.path.join(BASE, "web", "app.py"))
        cls.webapp = importlib.util.module_from_spec(spec)
        sys.modules["webapp"] = cls.webapp
        with contextlib.suppress(Exception):
            spec.loader.exec_module(cls.webapp)
        cls.app = cls.webapp.create_app()

    def setUp(self):
        # 每个测试独立 client（避免登录态跨测试串扰）
        self.c = self.app.test_client()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k in ("YIBAN_ACCOUNTS_KEY", "YIBAN_ENV_FILE", "YIBAN_ACCOUNTS_FILE",
                  "YIBAN_USERS_FILE", "YIBAN_DB_FILE", "YIBAN_STATE_DIR", "YIBAN_LOG_FILE"):
            os.environ.pop(k, None)

    # ---- 1. 前缀自动探测（单元） ----
    def test_detect_prefix(self):
        det = self.webapp.BasePathMiddleware._detect_prefix
        self.assertEqual(det("/tools/yiban-auto-sign/demo/login"), P)
        self.assertEqual(det("/tools/yiban-auto-sign/demo/"), P)          # 子路径首页带尾斜杠
        self.assertEqual(det("/tools/yiban-auto-sign/demo/api/login"), P)  # 登录 API 不可切错
        self.assertEqual(det("/tools/yiban-auto-sign/demo/user"), P)
        self.assertEqual(det("/login"), "")                               # 根路径
        self.assertEqual(det("/api/me"), "")
        self.assertEqual(det("/static/x.js"), "")
        self.assertEqual(det("/"), "")
        self.assertEqual(det("/foo"), "")                                 # 根路径 404 不误伤

    def test_script_name_passthrough(self):
        captured = {}

        def stub(environ, start_response):
            captured["SCRIPT_NAME"] = environ.get("SCRIPT_NAME", "")
            captured["PATH_INFO"] = environ.get("PATH_INFO", "")
            start_response("200 OK", [])
            return [b""]

        mw = self.webapp.BasePathMiddleware(stub)
        mw({"PATH_INFO": "/login", "SCRIPT_NAME": P}, lambda *a, **k: None)
        self.assertEqual(captured, {"SCRIPT_NAME": P, "PATH_INFO": "/login"})

    def test_yiban_base_path_override(self):
        captured = {}
        os.environ["YIBAN_BASE_PATH"] = P
        try:
            def stub(environ, start_response):
                captured["SCRIPT_NAME"] = environ.get("SCRIPT_NAME", "")
                captured["PATH_INFO"] = environ.get("PATH_INFO", "")
                start_response("200 OK", [])
                return [b""]

            mw = self.webapp.BasePathMiddleware(stub)
            mw({"PATH_INFO": P + "/login"}, lambda *a, **k: None)
            self.assertEqual(captured, {"SCRIPT_NAME": P, "PATH_INFO": "/login"})
            # 显式配置但路径不匹配 → 回落根路径（不误伤根部署）
            mw({"PATH_INFO": "/login"}, lambda *a, **k: None)
            self.assertEqual(captured, {"SCRIPT_NAME": "", "PATH_INFO": "/login"})
        finally:
            os.environ.pop("YIBAN_BASE_PATH", None)

    # ---- 2. 根路径部署不回归 ----
    def test_root_behavior_unchanged(self):
        r = self.c.get("/")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.headers.get("Location"), "/login")
        r = self.c.get("/login")
        self.assertEqual(r.status_code, 200)
        body = r.get_data(as_text=True)
        self.assertIn("const BASE = '';", body)
        self.assertIn('src="/static/vendor/tailwind.js', body)
        self.assertEqual(self.c.get("/foo").status_code, 404)      # 未知路径仍 404
        self.assertEqual(self.c.get("/static/vendor/tailwind.js").status_code, 200)
        self.assertEqual(self.c.get("/api/me").status_code, 401)

    # ---- 3. 子路径部署契约 ----
    def test_subpath_redirect_uses_prefix(self):
        r = self.c.get(P + "/")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.headers.get("Location"), P + "/login")
        r = self.c.get(P + "/user")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.headers.get("Location"), P + "/login")

    def test_subpath_page_static_api_prefixed(self):
        r = self.c.get(P + "/login")
        self.assertEqual(r.status_code, 200)
        body = r.get_data(as_text=True)
        self.assertIn(f"const BASE = '{P}';", body)
        self.assertIn(f'src="{P}/static/vendor/tailwind.js', body)
        self.assertIn(f'href="{P}/terms"', body)
        self.assertEqual(self.c.get(P + "/static/vendor/tailwind.js").status_code, 200)
        self.assertEqual(self.c.get(P + "/api/me").status_code, 401)
        self.assertEqual(self.c.get(P + "/foo").status_code, 404)

    def test_subpath_login_then_index(self):
        # 前端在子路径下用 BASE 拼接登录接口；登录后访问子路径首页应渲染 200
        r = self.c.post(P + "/api/login", json={"username": "admin", "password": "TestPass12345"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True)[:120])
        r = self.c.get(P + "/")
        self.assertEqual(r.status_code, 200)
        self.assertIn(f"const BASE = '{P}';", r.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main(verbosity=2)
