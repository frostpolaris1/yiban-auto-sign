# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""静态资源与合规页路由：自定义优先、缺失 404、短缓存头。

覆盖 favicon / 页脚备案图标（部署者自放文件优先，测试前后原样恢复）、robots.txt 与
404/500 错误页分流（`/api/` 返 JSON、静态扩展名返空、其余渲染 HTML）、以及合规文档
（PRIVACY_POLICY / USER_AGREEMENT）的 Markdown 渲染：段落合并不得对非列表行死循环，
空模板回退中性占位文案且不泄漏 `<!-- -->` 开发注释。

功能：静态资源、错误页与合规文档渲染的路由级回归。
归属：`web/` 应用层测试；渲染真源在 `web/render.py`。
复用：`BASE` / `TEST_KEY` 与 Flask test client 装载助手。
通信：经 Flask test client 发请求，临时读写 `web/static/vendor/*` 后恢复；由 pytest 收集
`unittest.TestCase`。
"""
import contextlib
import importlib.util
import os
import shutil
import sys
import tempfile
import threading
import unittest
from unittest import mock

import web.app as web

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


TEST_KEY = "a" * 64


class FaviconRouteTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-favicon-")
        os.environ["YIBAN_ENV_FILE"] = os.path.join(cls.tmp, ".env")
        os.environ["YIBAN_DB_FILE"] = os.path.join(cls.tmp, "yiban.db")
        os.environ["YIBAN_STATE_DIR"] = os.path.join(cls.tmp, "state")
        os.environ["YIBAN_LOG_FILE"] = os.path.join(cls.tmp, "sign.log")
        os.environ["YIBAN_DISABLE_PURGE_LOOP"] = "1"
        with open(os.environ["YIBAN_ENV_FILE"], "w", encoding="utf-8") as f:
            f.write(
                f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
                f"YIBAN_ADMIN_USER=admin\nYIBAN_ADMIN_PASSWORD=TestPass1234!\n"
            )
        spec = importlib.util.spec_from_file_location(
            "webapp_favicon", os.path.join(BASE, "web", "app.py"))
        cls.webapp = importlib.util.module_from_spec(spec)
        sys.modules["webapp_favicon"] = cls.webapp
        with contextlib.suppress(Exception):
            spec.loader.exec_module(cls.webapp)
        app = cls.webapp.create_app()
        cls.client = app.test_client()
        cls.icon_path = os.path.join(app.static_folder, "vendor", "favicon.png")
        # 部署者若已放置真实图标，先移开（结束原样恢复）
        cls.bak = None
        if os.path.isfile(cls.icon_path):
            cls.bak = cls.icon_path + ".favicon-test-bak"
            os.replace(cls.icon_path, cls.bak)

    @classmethod
    def tearDownClass(cls):
        if os.path.isfile(cls.icon_path):
            os.remove(cls.icon_path)
        if cls.bak and os.path.isfile(cls.bak):
            os.replace(cls.bak, cls.icon_path)
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k in ("YIBAN_ENV_FILE", "YIBAN_DB_FILE", "YIBAN_STATE_DIR",
                  "YIBAN_LOG_FILE", "YIBAN_DISABLE_PURGE_LOOP"):
            os.environ.pop(k, None)

    def test_absent_returns_404(self):
        self.assertFalse(os.path.isfile(self.icon_path))
        r = self.client.get("/favicon.png")
        self.assertEqual(r.status_code, 404)

    def test_present_serves_png_with_short_cache(self):
        os.makedirs(os.path.dirname(self.icon_path), exist_ok=True)
        with open(self.icon_path, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\nfake-bytes-for-route-test")
        r = self.client.get("/favicon.png")
        self.assertEqual(r.status_code, 200)
        self.assertIn("image/png", r.headers.get("Content-Type", ""))
        self.assertIn("max-age=3600", r.headers.get("Cache-Control", ""))


class GonganBeianRouteTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-gongan-")
        os.environ["YIBAN_ENV_FILE"] = os.path.join(cls.tmp, ".env")
        os.environ["YIBAN_DB_FILE"] = os.path.join(cls.tmp, "yiban.db")
        os.environ["YIBAN_STATE_DIR"] = os.path.join(cls.tmp, "state")
        os.environ["YIBAN_LOG_FILE"] = os.path.join(cls.tmp, "sign.log")
        os.environ["YIBAN_DISABLE_PURGE_LOOP"] = "1"
        with open(os.environ["YIBAN_ENV_FILE"], "w", encoding="utf-8") as f:
            f.write(
                f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
                f"YIBAN_ADMIN_USER=admin\nYIBAN_ADMIN_PASSWORD=TestPass1234!\n"
            )
        spec = importlib.util.spec_from_file_location(
            "webapp_gongan", os.path.join(BASE, "web", "app.py"))
        cls.webapp = importlib.util.module_from_spec(spec)
        sys.modules["webapp_gongan"] = cls.webapp
        with contextlib.suppress(Exception):
            spec.loader.exec_module(cls.webapp)
        app = cls.webapp.create_app()
        cls.client = app.test_client()
        cls.icon_path = os.path.join(app.static_folder, "vendor", "gongan-beian.png")
        # 部署者若已放置真实图标，先移开（结束原样恢复）
        cls.bak = None
        if os.path.isfile(cls.icon_path):
            cls.bak = cls.icon_path + ".gongan-test-bak"
            os.replace(cls.icon_path, cls.bak)

    @classmethod
    def tearDownClass(cls):
        if os.path.isfile(cls.icon_path):
            os.remove(cls.icon_path)
        if cls.bak and os.path.isfile(cls.bak):
            os.replace(cls.bak, cls.icon_path)
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k in ("YIBAN_ENV_FILE", "YIBAN_DB_FILE", "YIBAN_STATE_DIR",
                  "YIBAN_LOG_FILE", "YIBAN_DISABLE_PURGE_LOOP"):
            os.environ.pop(k, None)

    def test_absent_returns_404(self):
        self.assertFalse(os.path.isfile(self.icon_path))
        r = self.client.get("/gongan-beian.png")
        self.assertEqual(r.status_code, 404)

    def test_present_serves_png_with_short_cache(self):
        os.makedirs(os.path.dirname(self.icon_path), exist_ok=True)
        with open(self.icon_path, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\nfake-bytes-for-beian-route-test")
        r = self.client.get("/gongan-beian.png")
        self.assertEqual(r.status_code, 200)
        self.assertIn("image/png", r.headers.get("Content-Type", ""))
        self.assertIn("max-age=3600", r.headers.get("Cache-Control", ""))


DEPLOY_LOCAL = os.path.join(BASE, "deploy-local")


def render_with_timeout(doc_name, seconds=5):
    """在子线程渲染；超时视为失败（捕获死循环回归）。"""
    box = {}

    def _worker():
        box["html"] = web._read_doc_html(doc_name)

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join(seconds)
    if t.is_alive():
        raise AssertionError(
            f"_read_doc_html({doc_name!r}) 超过 {seconds}s 未返回，疑似渲染死循环"
        )
    return box["html"]


class LegalDocRenderTest(unittest.TestCase):
    def test_render_bold_line_no_infinite_loop(self):
        # 回归：`**生效日期**：...` 不能被误判成列表而卡死
        html = web._render_md("**生效日期**：【2026-01-01】\n\n下一段。")
        self.assertIn("<strong>生效日期</strong>", html)
        self.assertIn("下一段。", html)

    def test_render_basic_blocks(self):
        md = "# 标题\n\n- a\n- b\n\n> 引用\n\n1. 一\n2. 二\n\n正文。"
        html = web._render_md(md)
        self.assertIn("<h1>标题</h1>", html)
        self.assertIn("<ul><li>a</li><li>b</li></ul>", html)
        self.assertIn("<blockquote>引用</blockquote>", html)
        self.assertIn("<ol><li>一</li><li>二</li></ol>", html)
        self.assertIn("<p>正文。</p>", html)

    def test_render_inline_format(self):
        html = web._render_md("**粗体** 与 `代码` 与 [链接](https://example.com)")
        self.assertIn("<strong>粗体</strong>", html)
        self.assertIn("<code>代码</code>", html)
        self.assertIn('<a href="https://example.com" target="_blank" rel="noopener">链接</a>', html)

    def test_html_comment_lines_not_rendered(self):
        # 单行与多行 <!-- --> 注释都应被跳过，不进入渲染结果
        single = web._render_md("<!-- 部署者模板说明 -->\n\n正文。")
        self.assertNotIn("<!--", single)
        self.assertNotIn("部署者模板说明", single)
        self.assertIn("正文。", single)

        multi = web._render_md(
            "<!--\n第一行说明\n第二行说明\n-->\n\n# 标题"
        )
        self.assertNotIn("<!--", multi)
        self.assertNotIn("第一行说明", multi)
        self.assertIn("<h1>标题</h1>", multi)

    def test_repo_templates_render_fallback_without_comment_leak(self):
        # 仓库内两份文档是空模板：渲染应快速返回占位文案，且绝不泄漏 <!-- 注释
        for doc in ("PRIVACY_POLICY.md", "USER_AGREEMENT.md"):
            html = render_with_timeout(doc)
            self.assertIn("该文档尚未发布", html, doc)
            self.assertNotIn("<!--", html, doc)

    def test_read_unknown_doc_returns_fallback(self):
        html = web._read_doc_html("未知.md")
        self.assertIn("<p>未知文档。</p>", html)

    def test_render_link_scheme_whitelist(self):
        # 协议白名单回归（0.21.2 审查发现）：javascript:/data:/vbscript: 链接降级为纯文本，
        # 不得输出可执行 href；http/https/mailto 正常渲染。
        blocked = web._render_md("[点我](javascript:alert(1))")
        self.assertNotIn("href", blocked, "javascript: 链接不应输出 href")
        self.assertIn("点我", blocked, "链接文本应以纯文本保留")

        blocked_data = web._render_md("[img](data:text/html,<script>alert(1)</script>)")
        self.assertNotIn('href="data:', blocked_data)
        self.assertNotIn("<script>", blocked_data, "data: 负载中的标签必须被转义")

        blocked_vb = web._render_md("[x](vbscript:msgbox(1))")
        self.assertNotIn('href="vbscript:', blocked_vb)

        ok_https = web._render_md("[链接](https://example.com/a?x=1&y=2)")
        self.assertIn('<a href="https://example.com/a?x=1&amp;y=2" target="_blank" rel="noopener">链接</a>', ok_https)

        ok_mailto = web._render_md("[邮箱](mailto:admin@example.com)")
        self.assertIn('<a href="mailto:admin@example.com"', ok_mailto)

    def test_local_full_docs_render_if_present(self):
        # 完整版仅供运营者本地/服务器使用（deploy-local/ 被 gitignore，CI 上不存在时直接跳过）
        full = os.path.join(DEPLOY_LOCAL, "PRIVACY_POLICY.md")
        if not os.path.exists(full):
            return
        html = web._render_md(open(full, "r", encoding="utf-8").read())
        self.assertIn("AES-GCM", html)
        self.assertIn("scrypt", html)
        self.assertIn("nginx", html)
        self.assertIn("14 周岁", html)

    def test_doc_page_wrapper(self):
        page = web._doc_page("用户协议", "<p>正文</p>")
        self.assertIn("<h1>用户协议</h1>", page)
        self.assertIn("<p>正文</p>", page)
        self.assertIn('href="/login"', page)
        self.assertIn("返回登录页", page)

    def test_unclosed_comment_skips_only_start_line(self):
        # 0.21.2 审查修复：未闭合 <!-- 只跳过起始行，其后正文必须继续渲染（此前会整份吞掉）
        html = web._render_md("<!-- 说明未闭合\n\n正文仍然可见。\n\n# 标题")
        self.assertIn("<p>正文仍然可见。</p>", html)
        self.assertIn("<h1>标题</h1>", html)
        self.assertNotIn("<!--", html)

    def test_single_line_comment_skips_comment_only(self):
        # 单行 <!-- ... --> 注释整行跳过；闭合块后的正文正常
        html = web._render_md("<!-- 单行说明 -->\n\n正文。")
        self.assertIn("<p>正文。</p>", html)
        self.assertNotIn("单行说明", html)

    def test_doc_page_icp_block(self):
        # 0.21.2 审查修复：#7 独立协议页显示备案信息（配置时）；f8e5676 起备案号带工信部超链接
        page = web._doc_page("隐私政策", "<p>正文</p>", "京ICP备00000000号-1")
        self.assertIn(
            '<a href="https://beian.miit.gov.cn/" target="_blank" rel="noopener">京ICP备00000000号-1</a>',
            page,
        )
        page_empty = web._doc_page("隐私政策", "<p>正文</p>", "")
        self.assertNotIn('class="doc-icp"', page_empty)

    def test_doc_html_cached_and_invalidated(self):
        # 0.21.2 审查修复：#6 渲染结果按 (mtime, size) 缓存；修改文件后 key 变化自动失效。
        # 仓库模板为空模板（渲染结果恒为占位），故用缓存 key 而非渲染内容判断失效。
        #
        # 靶子是临时目录里的副本，不是仓库里的 USER_AGREEMENT.md：旧写法往真文件追加
        # 再用文本模式写回，Windows 上 LF 被转成 CRLF，跑一次测试就把工作区弄脏
        # （文本读→文本写本身就已经改变了字节内容）。_read_doc_html 只按 _REPO_ROOT
        # 拼路径，把它指到临时目录即可，仓库文件全程只读。
        with tempfile.TemporaryDirectory() as tmp:
            shutil.copyfile(os.path.join(BASE, "USER_AGREEMENT.md"),
                            os.path.join(tmp, "USER_AGREEMENT.md"))
            target = os.path.join(tmp, "USER_AGREEMENT.md")
            with mock.patch.object(web, "_REPO_ROOT", tmp):
                web._doc_cache.clear()
                web._read_doc_html("USER_AGREEMENT.md")
                self.assertIn("USER_AGREEMENT.md", web._doc_cache, "首次渲染应写入缓存")
                key1 = web._doc_cache["USER_AGREEMENT.md"][0]
                web._read_doc_html("USER_AGREEMENT.md")
                self.assertEqual(web._doc_cache["USER_AGREEMENT.md"][0], key1,
                                 "未变更时应命中缓存")
                with open(target, "a", encoding="utf-8") as f:
                    f.write("\n<!-- 临时增量注释 -->\n")
                web._read_doc_html("USER_AGREEMENT.md")
                key2 = web._doc_cache["USER_AGREEMENT.md"][0]
                self.assertNotEqual(key1, key2, "文件变更后缓存 key 应变化并重新渲染")
        web._doc_cache.clear()


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
