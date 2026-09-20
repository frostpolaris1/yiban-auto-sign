# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""渲染层拆分契约：`web/render.py` 是合规文档渲染与站点展示族的唯一定义点。

合规文档渲染与站点展示族从 `web/app.py` 迁入 `web/render.py`，app.py 只保留转发。
本文件钉住四件事，任一件破了都会**静默**改变行为：

1. **名字面完整**：迁移名在 `web.app` 与 `web.render` 上都可达（routes 经
   `sys.modules[current_app.import_name].<名字>` 晚查找取用）。
2. **转发注入 app 模块级状态**：`_read_doc_html` 用的文档根 `_REPO_ROOT` / 渲染缓存
   `_doc_cache`、站点展示族用的 `.env` 路径 `ENV_FILE` 都留在 web.app 且会被测试改写
   （`mock.patch.object` / 直接赋值），render 层另存一份绑定会让改写静默失效——故转发
   必须在调用时刻现取后传入。
3. **别名加载安全**：render 层不导入 `web.app`（普通 import 会在别名加载的测试进程里
   再执行一份 app.py 副本）；别名加载的 app 副本与 `web.render` 共享同一实现。
4. **render 不持有 app 状态**：`ENV_FILE` / `_REPO_ROOT` / `_doc_cache` 不得出现在
   render 模块上，否则第 2 条的"另存绑定"会以更隐蔽的形态回归。
"""
import contextlib
import importlib.util
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TEST_KEY = "a" * 64

#: 迁出名（实现唯一在 `web/render.py`；web.app 上必须是可达的兼容面）
MOVED_NAMES = (
    "_inline_md",
    "_render_md",
    "_read_doc_html",
    "_doc_page",
    "_DOC_FILES",
    "_SAFE_LINK_SCHEMES",
    "_LINK_RE",
    "email_domain_error",
    "icp_info",
    "police_info",
    "police_link",
    "site_description",
    "site_image",
    "edge_config",
    "edge_front_sec",
)

#: 纯再导出（不读 app 模块级状态）：两边必须是同一对象
PURE_REEXPORTS = ("_inline_md", "_render_md", "_DOC_FILES", "_SAFE_LINK_SCHEMES", "_LINK_RE")

#: app.py 自己持有、由转发注入 render 的模块级状态
APP_HELD_STATE = ("ENV_FILE", "_REPO_ROOT", "_doc_cache")


class WebRenderSplitContractTest(unittest.TestCase):
    """以别名加载的 app 副本为靶子（routes 与测试实际看到的那一份）。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-render-split-")
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
        spec = importlib.util.spec_from_file_location(
            "webapp_render", os.path.join(BASE, "web", "app.py"))
        cls.webapp = importlib.util.module_from_spec(spec)
        sys.modules["webapp_render"] = cls.webapp
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

    def _write_env(self, body):
        with io.open(self.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n" + body)

    # ------------------------------------------------------------------
    # 1. 名字面
    # ------------------------------------------------------------------
    def test_every_moved_name_reachable_on_app_and_render(self):
        from web import render as render_mod
        missing_app = [n for n in MOVED_NAMES if not hasattr(self.webapp, n)]
        missing_render = [n for n in MOVED_NAMES if not hasattr(render_mod, n)]
        self.assertEqual(missing_app, [], "web.app 兼容面缺失迁出名")
        self.assertEqual(missing_render, [], "web/render.py 缺定义")

    def test_pure_reexports_are_same_object(self):
        from web import render as render_mod
        for name in PURE_REEXPORTS:
            self.assertIs(getattr(self.webapp, name), getattr(render_mod, name),
                          f"{name} 应为同一个对象（web.app 只是再导出）")

    def test_forwarders_keep_original_call_arity(self):
        """routes / 既有测试按原实参个数调用，转发不得改签名。"""
        self._write_env("")
        self.assertIsInstance(self.webapp._read_doc_html("USER_AGREEMENT.md"), str)
        self.assertIsInstance(self.webapp._doc_page("t", "<p>b</p>"), str)
        self.assertEqual(self.webapp.icp_info(), "")
        self.assertEqual(self.webapp.edge_config(), (60, 60))
        self.assertIsNone(self.webapp.email_domain_error("a@qq.com"))

    # ------------------------------------------------------------------
    # 2. 转发注入 app 模块级状态（代表性打桩往返）
    # ------------------------------------------------------------------
    def test_env_file_stub_round_trip(self):
        """`web.app.ENV_FILE` 改写后，站点展示族必须读到新 .env（现取而非副本绑定）。"""
        self._write_env(
            "YIBAN_ICP_INFO= 京ICP备测试号 \n"
            "YIBAN_POLICE_INFO= 公安备测试号 \n"
            "YIBAN_POLICE_LINK=https://beian.example.gov.cn/p\n"
            "YIBAN_SITE_DESCRIPTION= 自定义摘要 \n"
            "YIBAN_SITE_IMAGE=https://img.example.com/a.png\n"
            "YIBAN_WINDOW_EDGE_FRONT_SEC=90\n"
            "YIBAN_WINDOW_EDGE_BACK_SEC=30\n"
            "YIBAN_EMAIL_DOMAIN_ALLOWLIST=qq.com\n"
        )
        with mock.patch.object(self.webapp, "ENV_FILE", self.env_file):
            self.assertEqual(self.webapp.icp_info(), "京ICP备测试号")
            self.assertEqual(self.webapp.police_info(), "公安备测试号")
            self.assertEqual(self.webapp.police_link(), "https://beian.example.gov.cn/p")
            self.assertEqual(self.webapp.site_description(), "自定义摘要")
            self.assertEqual(self.webapp.site_image(), "https://img.example.com/a.png")
            self.assertEqual(self.webapp.edge_config(), (90, 30))
            self.assertEqual(self.webapp.edge_front_sec(), 90)
            self.assertEqual(self.webapp.email_domain_error("a@qq.com"), None)
            self.assertIsNotNone(self.webapp.email_domain_error("a@163.com"))

    def test_site_image_rejects_non_https_round_trip(self):
        self._write_env("YIBAN_SITE_IMAGE=http://img.example.com/a.png\n")
        with mock.patch.object(self.webapp, "ENV_FILE", self.env_file):
            self.assertEqual(self.webapp.site_image(), "")

    def test_doc_root_and_cache_injected(self):
        """`_REPO_ROOT` 改写要生效、缓存要落在 web.app 的 `_doc_cache` 上。"""
        sub = os.path.join(self.tmp, "docs")
        os.makedirs(sub, exist_ok=True)
        with io.open(os.path.join(sub, "USER_AGREEMENT.md"), "w", encoding="utf-8") as f:
            f.write("# 用户协议\n\n正文。\n")
        with mock.patch.object(self.webapp, "_REPO_ROOT", sub):
            self.webapp._doc_cache.clear()
            html = self.webapp._read_doc_html("USER_AGREEMENT.md")
            self.assertIn("<h1>用户协议</h1>", html)
            self.assertIn("USER_AGREEMENT.md", self.webapp._doc_cache, "缓存应写回 web.app._doc_cache")
            key_first = self.webapp._doc_cache["USER_AGREEMENT.md"][0]
            self.webapp._read_doc_html("USER_AGREEMENT.md")
            self.assertEqual(self.webapp._doc_cache["USER_AGREEMENT.md"][0], key_first,
                             "文件未变更应命中缓存")
        self.webapp._doc_cache.clear()

    def test_doc_page_default_description_uses_app_state(self):
        """`_doc_page` 摘要留空时取 `site_description()`（同样受 ENV_FILE 打桩影响）。"""
        self._write_env("YIBAN_SITE_DESCRIPTION= 独立页摘要 \n")
        with mock.patch.object(self.webapp, "ENV_FILE", self.env_file):
            page = self.webapp._doc_page("用户协议", "<p>正文</p>")
        self.assertIn('content="独立页摘要"', page)

    def test_edge_config_patch_name_face(self):
        """`mock.patch.object(webapp, "edge_config")` 仍是可打桩的名字面。"""
        with mock.patch.object(self.webapp, "edge_config", return_value=(0, 0)):
            self.assertEqual(self.webapp.edge_config(), (0, 0))

    # ------------------------------------------------------------------
    # 3/4. 别名加载安全与状态归属
    # ------------------------------------------------------------------
    def test_alias_loaded_app_shares_render_module(self):
        from web import render as render_mod
        self.assertIs(self.webapp._render, render_mod,
                      "别名加载的 app 副本必须复用同一个 web.render（不得再执行一份）")

    def test_render_module_holds_no_app_state(self):
        from web import render as render_mod
        for name in APP_HELD_STATE:
            self.assertFalse(hasattr(render_mod, name),
                             f"render 层不得持有 {name}（应由 web.app 调用时刻注入）")

    def test_render_source_does_not_import_app(self):
        path = os.path.join(BASE, "web", "render.py")
        with io.open(path, encoding="utf-8") as f:
            src = f.read()
        self.assertIsNone(re.search(r"^\s*(?:import|from)\s+web\.app\b", src, re.M),
                          "render 层禁止 import web.app（别名加载会执行副本模块）")

    def test_importing_render_does_not_execute_web_app(self):
        """全新解释器里只 import web.render：不得把 web.app 拉进 sys.modules。"""
        code = (
            "import sys;"
            f"sys.path[:0]=[{os.path.join(BASE, 'scripts')!r}, {BASE!r}];"
            "import web.render;"
            "print('APP_LOADED' if 'web.app' in sys.modules else 'RENDER_ONLY_OK')"
        )
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        r = subprocess.run([sys.executable, "-c", code], cwd=BASE, env=env,
                           capture_output=True, text=True, timeout=60)
        self.assertIn("RENDER_ONLY_OK", r.stdout, r.stderr[-600:])
        self.assertEqual(r.returncode, 0, r.stderr[-600:])


if __name__ == "__main__":
    unittest.main(verbosity=2)
