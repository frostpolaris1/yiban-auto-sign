# -*- coding: utf-8 -*-
"""CSP 响应头契约：`script-src` 不含 'unsafe-inline'，模板内联面归零（工单 56ed）。

标签：E · Web：认证/权限/API
覆盖两道面——
    ① **行为面**：`create_app()` 后的响应确实带 `Content-Security-Policy`；
       `script-src` 只有 'self' 与逐请求 nonce，**不含 'unsafe-inline'**；
       `style-src` 仍保留 'unsafe-inline'（模板大量行内 style 属性，不属本单）；
       其余关键指令（object-src / frame-ancestors / default-src / base-uri /
       form-action）取值不变。
    ② **结构面**：`web/templates/**` 里**没有**内联事件属性（onclick / onerror 等
       on* 族）；**没有**不带 nonce 的内联 `<script>` 块——动态数据块
       （`window.YB_*_STATE`）必须带 `nonce="{{ csp_nonce }}"`，静态引导脚本一律外置。
       三个外置脚本文件必须真实存在（theme-boot / error-page / boot-loader）。

关键断言：去掉 `script-src` 的 nonce、或把任一内联脚本/事件属性加回模板、或删掉外置
静态文件，本文件必须红。首屏不闪由 theme-boot.js 的同步加载位置保证（`<head>` 内、
先于样式表），这是该文件的注释契约，渲染金标准另钉结构。
依赖：纯本地 Flask test client + 临时 .env/SQLite + 文件读取；不触网、无 skip。
"""
import contextlib
import importlib.util
import os
import re
import shutil
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES = os.path.join(BASE, "web", "templates")

#: 一段内联事件处理器属性（on<word>=，前导空白界定属性位；覆盖全 on* 族）。
_INLINE_EVENT = re.compile(r"\son[a-z]+\s*=", re.I)
#: 任意 <script ...> 开标签。
_SCRIPT_TAG = re.compile(r"<script\b[^>]*>", re.I)

#: 必须存在的外置脚本（CSP 去掉 unsafe-inline 后它们承接了原内联脚本）。
EXTERNAL_SCRIPTS = (
    "web/static/js/theme-boot.js",
    "web/static/js/error-page.js",
    "web/static/js/boot-loader.js",
)


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _count_inline_scripts(root):
    """返回 (不带 nonce 的内联 <script> 块数, 带 nonce 的内联块数)。"""
    plain = 0
    nonce = 0
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            if not name.endswith((".html", ".htm")):
                continue
            text = _read(os.path.join(dirpath, name))
            for tag in _SCRIPT_TAG.findall(text):
                low = tag.lower()
                if "src" in low:
                    continue
                if "nonce" in low:
                    nonce += 1
                else:
                    plain += 1
    return plain, nonce


def _count_inline_events(root):
    n = 0
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            if not name.endswith((".html", ".htm")):
                continue
            n += len(_INLINE_EVENT.findall(_read(os.path.join(dirpath, name))))
    return n


def _script_src(csp):
    """从 CSP 头里取出 script-src 指令串。"""
    for part in csp.split(";"):
        part = part.strip()
        if part.startswith("script-src"):
            return part
    return ""


class CspHeaderContractTest(unittest.TestCase):
    """① 行为面：真实响应带 CSP 头，script-src 不含 'unsafe-inline'。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-csp-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(
                "YIBAN_ACCOUNTS_KEY=" + "c" * 64 + "\n"
                "YIBAN_ADMIN_USER=admin\nYIBAN_ADMIN_PASSWORD=MasterPass#2026\n"
            )
        os.environ["YIBAN_ACCOUNTS_KEY"] = "c" * 64
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_DB_FILE"] = cls.db_file
        os.environ["YIBAN_STATE_DIR"] = cls.tmp
        os.environ["YIBAN_LOG_FILE"] = os.path.join(cls.tmp, "sign.log")
        os.environ["YIBAN_DISABLE_PURGE_LOOP"] = "1"
        global db
        import db
        spec = importlib.util.spec_from_file_location(
            "webapp_csp", os.path.join(BASE, "web", "app.py"))
        cls.webapp = importlib.util.module_from_spec(spec)
        sys.modules["webapp_csp"] = cls.webapp
        spec.loader.exec_module(cls.webapp)
        db.init_db(cls.db_file, env_file=cls.env_file)
        cls.app = cls.webapp.create_app()

    @classmethod
    def tearDownClass(cls):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        sys.modules.pop("webapp_csp", None)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _csp(self, path="/"):
        return self.app.test_client().get(path).headers.get("Content-Security-Policy")

    def test_response_carries_the_csp_header_with_expected_directives(self):
        csp = self._csp()
        self.assertIsNotNone(csp, "任意响应都必须带 Content-Security-Policy（after_request 全站下发）")
        for directive in (
            "default-src 'self'",
            "img-src 'self'",
            "font-src 'self'",
            "connect-src 'self'",
            "frame-ancestors 'none'",
            "base-uri 'self'",
            "form-action 'self'",
            "object-src 'none'",
        ):
            self.assertIn(directive, csp,
                          f"CSP 缺少/改动了关键指令 {directive!r}（当前: {csp!r}）")

    def test_script_src_has_no_unsafe_inline(self):
        csp = self._csp()
        script_src = _script_src(csp)
        self.assertTrue(script_src, f"CSP 缺少 script-src（当前: {csp!r}）")
        self.assertIn("'self'", script_src)
        self.assertNotIn("'unsafe-inline'", script_src,
                         f"script-src 不得含 'unsafe-inline'（当前: {script_src!r}）")

    def test_script_src_nonce_is_per_request_and_matches_rendered_block(self):
        """动态数据块必须拿到与响应头一致的 nonce（否则会被浏览器拦掉）。"""
        c = self.app.test_client()
        r = c.post("/api/login", json={"username": "admin",
                                       "password": "MasterPass#2026"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        r = c.get("/work/settings", environ_base={"REMOTE_ADDR": "127.0.0.1"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        script_src = _script_src(r.headers["Content-Security-Policy"])
        mm = re.search(r"'nonce-([^']+)'", script_src)
        self.assertIsNotNone(mm, f"script-src 必须携带逐请求 nonce（当前: {script_src!r}）")
        self.assertIn(f'nonce="{mm.group(1)}"', r.get_data(as_text=True),
                      "渲染出的数据块 nonce 必须与响应头一致")

    def test_style_src_keeps_unsafe_inline(self):
        """style-src 保留 'unsafe-inline'：模板行内 style 属性不在本单收敛面内。"""
        self.assertIn("style-src 'self' 'unsafe-inline'", self._csp())


class CspTemplateStructureTest(unittest.TestCase):
    """② 结构面：模板内联面归零，外置脚本在场。"""

    def test_no_inline_event_attributes(self):
        self.assertEqual(_count_inline_events(TEMPLATES), 0,
                         "模板里不得再出现 onclick / onerror 等内联事件属性"
                         "（CSP 不会执行它们；改用 data-action 委托或捕获阶段监听）")

    def test_no_inline_script_without_nonce(self):
        plain, nonce = _count_inline_scripts(TEMPLATES)
        self.assertEqual(plain, 0,
                         f"仍有 {plain} 个不带 nonce 的内联 <script> 块；"
                         "静态引导脚本必须外置，动态数据块必须带 nonce")
        self.assertGreaterEqual(nonce, 3,
                                "至少 3 个动态数据块（dashboard / calendar / settings）"
                                "应带 nonce；数量骤降说明模板被回退")

    def test_external_scripts_exist(self):
        for rel in EXTERNAL_SCRIPTS:
            with self.subTest(rel=rel):
                self.assertTrue(os.path.isfile(os.path.join(BASE, rel)),
                                f"{rel} 不存在：CSP 去掉 unsafe-inline 后它承接了原内联脚本")

    def test_theme_boot_exposes_global_base(self):
        """core.js 以裸名 `BASE` 读挂载前缀；外置脚本必须把它挂到 window。"""
        src = _read(os.path.join(BASE, "web/static/js/theme-boot.js"))
        self.assertIn("window.BASE", src,
                      "theme-boot.js 必须暴露全局 BASE（否则子路径部署下 core.js "
                      "读不到前缀，所有 URL 掉回站点根）")

    def test_theme_boot_is_synchronous_in_head(self):
        """主题脚本必须同步加载（无 defer/async），否则首屏会闪。"""
        src = _read(os.path.join(BASE, "web/templates/partials/theme_boot.html"))
        self.assertIn("theme-boot.js", src)
        tag = re.search(r"<script\b[^>]*theme-boot\.js[^>]*>", src, re.I)
        self.assertIsNotNone(tag)
        low = tag.group(0).lower()
        self.assertNotIn("defer", low)
        self.assertNotIn("async", low)


if __name__ == "__main__":
    unittest.main(verbosity=2)
