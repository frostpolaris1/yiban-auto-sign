# -*- coding: utf-8 -*-
"""Web 前端注入面契约（2026-09-08）。

1. police_link()：YIBAN_POLICE_LINK 经 scheme 白名单（复用 _SAFE_LINK_SCHEMES）
   校验后进 href——该值直接渲染在公开登录页，配置 javascript:/data: 即点击型
   XSS；非白名单（含空值）一律回落公安部通用门户。
2. 模板内联上下文（静态文本断言，显式 utf-8 读模板）：
   - request.script_root 落在 <script> 字符串字面量里必须经 tojson（裸插入遇
     以 /\\ 结尾的前缀会产生 `const BASE = '\\';` 语法错误，整页脚本瘫痪）；
   - onclick/onchange 属性不再拼用户可控值（jsEscape 只处理反斜杠/反引号/${，
     不转义引号，且属性值经 HTML 解码后引号复原）：改 data-* 属性 + 事件委托，
     属性值不进 JS 解析器，普通 HTML 转义即安全。

police_link 用例沿用 test_batch18_fixes_0905.py 的 webapp importlib 装载方式。
"""
import contextlib
import importlib.util
import io
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TEST_KEY = "a" * 64


def _read(*parts):
    with io.open(os.path.join(BASE, *parts), encoding="utf-8") as f:
        return f.read()


class PoliceLinkSchemeTest(unittest.TestCase):
    """YIBAN_POLICE_LINK scheme 白名单校验。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-police-link-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        cls._old_env = {k: os.environ.get(k) for k in
                        ("YIBAN_ENV_FILE", "YIBAN_DB_FILE", "YIBAN_STATE_DIR",
                         "YIBAN_ACCOUNTS_FILE", "YIBAN_ACCOUNTS_KEY")}
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_DB_FILE"] = os.path.join(cls.tmp, "yiban.db")
        os.environ["YIBAN_STATE_DIR"] = cls.tmp
        os.environ["YIBAN_ACCOUNTS_FILE"] = os.path.join(cls.tmp, "accounts.json")
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
        import db as _db
        cls._db = _db
        spec = importlib.util.spec_from_file_location("webapp_police", os.path.join(BASE, "web", "app.py"))
        cls.webapp = importlib.util.module_from_spec(spec)
        sys.modules["webapp_police"] = cls.webapp
        with contextlib.suppress(Exception):
            spec.loader.exec_module(cls.webapp)

    @classmethod
    def tearDownClass(cls):
        if cls._db._conn is not None:
            with contextlib.suppress(Exception):
                cls._db._conn.close()
            cls._db._conn = None
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k, v in cls._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _link(self, value):
        with io.open(self.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n")
            if value is not None:
                f.write(f"YIBAN_POLICE_LINK={value}\n")
        with mock.patch.object(self.webapp, "ENV_FILE", self.env_file):
            return self.webapp.police_link()

    def test_https_passthrough(self):
        self.assertEqual(self._link("https://beian.example.gov.cn/?q=123"),
                         "https://beian.example.gov.cn/?q=123")

    def test_http_passthrough(self):
        self.assertEqual(self._link("http://www.beian.gov.cn/portal/registerSystemInfo"),
                         "http://www.beian.gov.cn/portal/registerSystemInfo")

    def test_javascript_rejected_falls_back_to_default(self):
        self.assertEqual(self._link("javascript:alert(1)"), "https://beian.mps.gov.cn/")

    def test_javascript_case_insensitive_rejected(self):
        self.assertEqual(self._link("JavaScript:alert(1)"), "https://beian.mps.gov.cn/")

    def test_data_rejected(self):
        self.assertEqual(self._link("data:text/html,<script>alert(1)</script>"),
                         "https://beian.mps.gov.cn/")

    def test_empty_or_missing_falls_back_to_default(self):
        self.assertEqual(self._link(None), "https://beian.mps.gov.cn/")
        self.assertEqual(self._link(""), "https://beian.mps.gov.cn/")


class TemplateInlineContextTest(unittest.TestCase):
    """模板静态契约：<script> 内 script_root 用 tojson；onclick/onchange 不拼用户可控值。"""

    @classmethod
    def setUpClass(cls):
        cls.index = _read("web", "templates", "index.html")
        cls.login = _read("web", "templates", "login.html")
        cls.user = _read("web", "templates", "user.html")

    def test_base_uses_tojson_in_all_templates(self):
        for name, tpl in (("index", self.index), ("login", self.login), ("user", self.user)):
            self.assertIn("const BASE = {{ request.script_root | tojson }};", tpl,
                          f"{name}.html 的 BASE 须经 tojson 转义")
            self.assertNotIn("const BASE = '{{ request.script_root }}'", tpl,
                             f"{name}.html 的 BASE 裸插入仍存在")

    def test_no_user_value_in_inline_handlers(self):
        # 原实现 onclick="purgeDeletedUser('${jsEscape(u.email)}')"：jsEscape 不转义引号，
        # 属性值经 HTML 解码后引号复原，逃逸即成立
        self.assertNotIn('onclick="purgeDeletedUser(', self.index)
        self.assertNotIn("onchange=\"toggleRow('${key}', '${esc(u.email)}', this)\"", self.index)

    def test_delegated_data_attribute_form_present(self):
        # 正确形态：data-* 属性（普通 HTML 转义即安全）+ 事件委托读 dataset
        self.assertIn('data-purge-email="${esc(u.email)}"', self.index)
        self.assertIn('data-batch-key="${key}" data-batch-id="${esc(u.email)}"', self.index)
        self.assertIn("e.target.closest('[data-purge-email]')", self.index)
        self.assertIn("el.matches('[data-batch-key]')", self.index)


if __name__ == "__main__":
    unittest.main(verbosity=2)
