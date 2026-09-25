# -*- coding: utf-8 -*-
"""子路径 / 独立子域 前缀自适应部署契约回归测试。

标签：J · 运维：部署/备份/发布
覆盖：前缀自动探测（含图标与旧路径书签）、`SCRIPT_NAME` 透传、`YIBAN_BASE_PATH` 显式
    覆盖与不匹配时的回落、根路径部署不回归、子路径下跳转/静态/API 都带前缀、
    子路径登录后可正常渲染数据总览。
对应实现：`web/app.py` 的 `BasePathMiddleware`（`_detect_prefix`、`_ROOT_MARKERS`）。
关键断言：① 根路径行为与改造前完全一致（不回归）；② 元测试从 `app.url_map` 自动推导
    根级路由——新增路由忘了登记前缀清单立刻报红，否则线上表现为"根路径可用、
    生产子路径 404"。
依赖：进程内加载 `web/app.py`（importlib 隔离）+ Flask test client；临时目录与临时
    .env；不起子进程、不需 bash/docker/网络。

反向代理只需把完整 URI 原样透传，中间件自己感知前缀。
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
        os.environ.pop("YIBAN_BASE_PATH", None)  #清掉继承值：本机 .env 配了前缀的话，"根路径不回归"那组会整组红
        spec = importlib.util.spec_from_file_location("webapp", os.path.join(BASE, "web", "app.py"))  #按路径隔离加载：与真实 web 模块同名导入会串环境
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
        self.assertEqual(det("/tools/yiban-auto-sign/demo/favicon.png"), P)      # 站标
        self.assertEqual(det("/tools/yiban-auto-sign/demo/gongan-beian.png"), P)  # 备案图标
        self.assertEqual(det("/tools/yiban-auto-sign/demo/logs"), P)             # 旧路径书签
        self.assertEqual(det("/tools/yiban-auto-sign/demo/mine/calendar"), P)
        self.assertEqual(det("/login"), "")                               # 根路径
        self.assertEqual(det("/api/me"), "")
        self.assertEqual(det("/static/x.js"), "")
        self.assertEqual(det("/"), "")
        self.assertEqual(det("/foo"), "")                                 # 根路径 404 不误伤

    def test_every_root_route_is_reachable_under_subpath(self):
        """根级路由（含图标与旧路径重定向）在子路径部署下都必须被前缀探测识别。

        从 app.url_map 自动推导：新增根级路由却忘了登记 _ROOT_MARKERS 时立即报红——
        否则线上表现为「根路径可用、生产子路径 404」（本用例即由此缺陷补入）。
        """
        det = self.webapp.BasePathMiddleware._detect_prefix
        checked = 0
        for rule in self.app.url_map.iter_rules():
            if rule.arguments:
                continue  # 含变量的路由（/static/<path:filename> 等）由前缀清单覆盖
            path = str(rule.rule)
            with self.subTest(rule=path):
                self.assertEqual(det(P + path), P)
            checked += 1  #计数是元测试的自锁：一条路由都没推导出来时断言就白给
        self.assertGreaterEqual(checked, 10, "根级路由数量异常，元测试可能失效")

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
            # 显式配置时，**不带尾斜杠**的子路径根也能进首页：前缀切完剩空串 → 归一成 "/"，
            # 由 root_page 按登录态转（否则用户手输 `https://host/tools/.../demo` 会 404）
            mw({"PATH_INFO": P}, lambda *a, **k: None)
            self.assertEqual(captured, {"SCRIPT_NAME": P, "PATH_INFO": "/"})
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
        self.assertIn('const BASE = "";', body)  # tojson 渲染：空串带双引号
        # 承载断言改用当前登录页真实引用的自托管资源（换壳后旧栈 tailwind.js 已退役）。
        # 意图不变：登录页的静态资源必须走 /static/ 且能取到。
        self.assertIn('src="/static/js/core.js', body)
        self.assertIn('href="/static/vendor/adminator/adminator.css', body)
        self.assertEqual(self.c.get("/foo").status_code, 404)      # 未知路径仍 404
        self.assertEqual(self.c.get("/static/js/core.js").status_code, 200)
        self.assertEqual(self.c.get("/api/me").status_code, 401)

    # ---- 3. 子路径部署契约 ----
    def test_subpath_redirect_uses_prefix(self):
        r = self.c.get(P + "/")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.headers.get("Location"), P + "/login")
        # 旧路径 /user 先 302 到新路径（前缀必须带上），再由页面守卫转登录页
        r = self.c.get(P + "/user")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.headers.get("Location"), P + "/user/account")

    def test_subpath_page_static_api_prefixed(self):
        r = self.c.get(P + "/login")
        self.assertEqual(r.status_code, 200)
        body = r.get_data(as_text=True)
        self.assertIn(f'const BASE = "{P}";', body)  # tojson 渲染：JSON 字符串带双引号
        self.assertIn(f'src="{P}/static/js/core.js', body)
        self.assertIn(f'href="{P}/static/vendor/adminator/adminator.css', body)
        self.assertIn(f'href="{P}/terms"', body)
        self.assertEqual(self.c.get(P + "/static/js/core.js").status_code, 200)
        self.assertEqual(self.c.get(P + "/api/me").status_code, 401)
        self.assertEqual(self.c.get(P + "/foo").status_code, 404)

    def test_subpath_login_then_index(self):
        # 前端在子路径下用 BASE 拼接登录接口；登录后访问根路径应转到数据总览并渲染 200
        r = self.c.post(P + "/api/login", json={"username": "admin", "password": "TestPass12345"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True)[:120])
        r = self.c.get(P + "/")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.headers.get("Location"), P + "/data/dashboard")
        r = self.c.get(P + "/data/dashboard")
        self.assertEqual(r.status_code, 200)
        self.assertIn(f'const BASE = "{P}";', r.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main(verbosity=2)
