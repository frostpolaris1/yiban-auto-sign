# -*- coding: utf-8 -*-
"""注册口不得成为"超管邮箱探针"回归（MF-58 ①）。

标签：E · Web：认证/权限/API
覆盖：`POST /api/register` 对内置管理员邮箱（`YIBAN_ADMIN_USER` 取邮箱形态）必须
    与"已注册邮箱"给出**逐字同文案**的 400，并且**同样补一次 dummy scrypt**
    （耗时拉平）——此前独有文案"内置管理员邮箱不可注册"且排在口令散列之前，
    匿名者可零成本、零时延地定位超管邮箱。
对应实现：`web/routes/auth.py::api_register` 内置邮箱保留分支。
关键断言：文案与已注册分支逐字相等；`_constant_time_dummy` 在该分支被调用
    （耗时面以"同一原语被走一遍"为钉，秒表断言会因机器负载 flake）。
依赖：纯本地 Flask test client + 临时 `.env`/SQLite，不联网。
"""
import contextlib
import importlib.util
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TEST_KEY = "c" * 64
ADMIN_EMAIL = "superadmin@test.local"   # 内置管理员用户名取邮箱形态
REGISTERED = "someone@test.local"       # 假号面：仅注册形态，不含真实凭据
GOODPW = "Abcdef12345!"


class RegisterAdminEmailTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-reg4a-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(
                f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
                f"YIBAN_ADMIN_USER={ADMIN_EMAIL}\n"
                "YIBAN_ADMIN_PASSWORD=MasterPass#2026\n"
            )
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_DB_FILE"] = cls.db_file
        os.environ["YIBAN_STATE_DIR"] = cls.tmp
        os.environ["YIBAN_LOG_FILE"] = os.path.join(cls.tmp, "sign.log")
        os.environ["YIBAN_DISABLE_PURGE_LOOP"] = "1"
        global db
        import db
        spec = importlib.util.spec_from_file_location(
            "webapp_reg4a", os.path.join(BASE, "web", "app.py"))
        cls.webapp = importlib.util.module_from_spec(spec)
        sys.modules["webapp_reg4a"] = cls.webapp
        spec.loader.exec_module(cls.webapp)

    @classmethod
    def tearDownClass(cls):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        sys.modules.pop("webapp_reg4a", None)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        for suffix in ("", "-wal", "-shm"):
            p = self.db_file + suffix
            if os.path.exists(p):
                os.remove(p)
        db.init_db(self.db_file, env_file=self.env_file)
        db.create_user(REGISTERED, self.webapp.generate_password_hash(GOODPW))

    def test_builtin_admin_email_indistinguishable_from_duplicate(self):
        c = self.webapp.create_app().test_client()
        with mock.patch.object(self.webapp, "_constant_time_dummy",
                               wraps=self.webapp._constant_time_dummy) as dummy:
            r_admin = c.post("/api/register", json={
                "email": ADMIN_EMAIL, "password": GOODPW, "agree": True})
            self.assertEqual(dummy.call_count, 1,
                             "内置邮箱分支必须走一次 dummy scrypt（与重复注册同耗时面）")
            r_dup = c.post("/api/register", json={
                "email": REGISTERED, "password": GOODPW, "agree": True})
        self.assertEqual(r_admin.status_code, r_dup.status_code)
        self.assertEqual(r_admin.get_json()["error"], r_dup.get_json()["error"],
                         "文案逐字相等：注册口不得暴露'这是超管邮箱'的独有信号")
        self.assertNotIn("内置管理员", r_admin.get_json()["error"])


if __name__ == "__main__":
    unittest.main()
