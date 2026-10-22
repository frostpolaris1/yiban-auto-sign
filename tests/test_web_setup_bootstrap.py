# -*- coding: utf-8 -*-
"""首次启动引导（/setup）契约测试。

标签：F · 前端与界面守卫（首次部署）
覆盖：空管理员时 / 与 /login 都转到 /setup；/setup 渲染表单；弱口令 400 且零写入；
      合法创建写 scrypt 哈希并删除明文键；创建后自动登录；二次创建 409。
对应实现：web/routes/pages.py 的 setup_page、web/routes/auth.py 的 api_setup_admin、
      web/templates/setup.html、web/static/js/setup.js、yiban/entry/cli 的 init。
依赖：纯本地 Flask test client + 临时 .env，不联网、无需 node。
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
GOOD_PASS = "SetupPass123!"


class SetupBootstrapTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-setup-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.state_dir = os.path.join(cls.tmp, "state")
        cls.log_file = os.path.join(cls.tmp, "sign.log")
        os.makedirs(cls.state_dir, exist_ok=True)
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n")

        cls._old_env = {k: os.environ.get(k) for k in (
            "YIBAN_ACCOUNTS_KEY", "YIBAN_ENV_FILE", "YIBAN_DB_FILE",
            "YIBAN_STATE_DIR", "YIBAN_LOG_FILE",
        )}
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_DB_FILE"] = cls.db_file
        os.environ["YIBAN_STATE_DIR"] = cls.state_dir
        os.environ["YIBAN_LOG_FILE"] = cls.log_file

        spec = importlib.util.spec_from_file_location(
            "webapp_setup_bootstrap", os.path.join(BASE, "web", "app.py"))
        cls.webapp = importlib.util.module_from_spec(spec)
        sys.modules["webapp_setup_bootstrap"] = cls.webapp
        with contextlib.suppress(Exception):
            spec.loader.exec_module(cls.webapp)
        cls.app = cls.webapp.create_app()
        cls.app.config.update(TESTING=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k, v in cls._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _client(self):
        return self.app.test_client()

    @staticmethod
    def _post(client, data):
        return client.post("/api/setup/admin", json=data,
                           headers={"Origin": "http://localhost"})

    def test_1_unconfigured_site_redirects_to_setup_and_renders_form(self):
        c = self._client()
        r = c.get("/")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/setup", r.headers["Location"])
        r = c.get("/login")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/setup", r.headers["Location"])
        r = c.get("/setup")
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertIn('id="setup-username"', html)
        self.assertIn('id="setup-password"', html)
        self.assertIn('id="setup-password2"', html)
        self.assertIn('id="setup-btn"', html)
        self.assertIn("noindex", html)

    def test_2_weak_password_rejected_without_writing(self):
        c = self._client()
        before = open(self.env_file, encoding="utf-8").read()
        r = self._post(c, {"username": "admin", "password": "short"})
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True)[:300])
        self.assertIn("至少 12 位", r.get_json()["error"])
        self.assertEqual(open(self.env_file, encoding="utf-8").read(), before,
                         "校验失败不得写 .env")

    def test_3_create_admin_hashes_password_and_logs_in(self):
        c = self._client()
        r = self._post(c, {"username": "admin", "password": GOOD_PASS})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True)[:500])
        self.assertTrue(r.get_json()["ok"])
        env_text = open(self.env_file, encoding="utf-8").read()
        self.assertIn("YIBAN_ADMIN_USER=admin", env_text)
        self.assertIn("YIBAN_ADMIN_PASSWORD_HASH=scrypt:", env_text)
        self.assertNotIn("YIBAN_ADMIN_PASSWORD=", env_text, "明文口令键必须被删除")
        self.assertNotIn(GOOD_PASS, env_text, "口令明文不得落盘")

        r = c.get("/api/me")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True)[:300])
        self.assertEqual(r.get_json().get("role"), "admin")

        # 已配置后：页面转登录页，接口 409
        r = c.get("/setup")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login", r.headers["Location"])
        r = self._post(self._client(), {"username": "other", "password": GOOD_PASS})
        self.assertEqual(r.status_code, 409)
        self.assertIn("已配置", r.get_json()["error"])


if __name__ == "__main__":
    unittest.main()
