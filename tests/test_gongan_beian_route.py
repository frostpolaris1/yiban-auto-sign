"""页脚备案图标路由：部署者自放优先、缺失 404、短缓存头。

与 tests/test_favicon_route.py 同机制。测试会临时读写
web/static/vendor/gongan-beian.png——若该位置已有部署者自放的真实图标，
先移到 .gongan-test-bak 并在结束时原样恢复，绝不破坏本地文件。
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


if __name__ == "__main__":
    unittest.main()
