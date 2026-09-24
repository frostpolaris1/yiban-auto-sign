# -*- coding: utf-8 -*-
"""全局限速分级（用户实拍：快速切页 /api/* 触发 429）。

标签：E · Web：认证/权限/API
覆盖：全局限速的两桶分级——匿名与写路径走严格桶，已登录 GET 走放宽的独立桶
对应实现：`web/app.py` 的 `create_app` 内限速包装与 `RATE_MAX` / `RATE_MAX_AUTH_GET`
关键断言：严格阈值 +1 必现 429 且前 `RATE_MAX` 次全 200；已登录 GET 跨过旧严格阈值仍放行；已登录的写路径照旧吃 429；把放宽桶打满后严格桶仍满额可用（两桶独立计数）
依赖：纯本地 Flask test client + 临时 `.env`/SQLite，不联网、不访问真实易班接口；无需 node（口令以 scrypt 哈希预置，登录走真实校验）。限速计数是 `create_app` 的闭包状态，故每条用例新建 app。无需 node

判据：
- 匿名请求与写路径（POST/PUT/DELETE）维持严格阈值 RATE_MAX（脚本轰炸主防线）；
- 已登录 GET 走放宽的独立桶 RATE_MAX_AUTH_GET（页面首屏并发 + 快速切页），
  两桶独立计数，正常浏览不会互相挤占。
"""
import contextlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TEST_KEY = "a" * 64
ADMIN_PASS = "TestPass1234!"


class RateLimitTierTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-rate-tier-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        from werkzeug.security import generate_password_hash
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(
                f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
                f"YIBAN_ADMIN_USER=admin\n"
                # 预置哈希而非明文键：登录传明文，走的正是 verify 侧的哈希比对分支
                f"YIBAN_ADMIN_PASSWORD_HASH={generate_password_hash(ADMIN_PASS, method='scrypt')}\n"
            )
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_ACCOUNTS_FILE"] = os.path.join(cls.tmp, "accounts.json")
        os.environ["YIBAN_DB_FILE"] = os.path.join(cls.tmp, "yiban.db")
        os.environ["YIBAN_STATE_DIR"] = cls.tmp
        os.environ["YIBAN_LOG_FILE"] = os.path.join(cls.tmp, "sign.log")
        os.environ["YIBAN_DISABLE_PURGE_LOOP"] = "1"
        with open(os.environ["YIBAN_ACCOUNTS_FILE"], "w", encoding="utf-8") as f:
            json.dump([], f)
        global db
        import db
        db.init_db(os.environ["YIBAN_DB_FILE"], migrate_from=os.environ["YIBAN_ACCOUNTS_FILE"],
                   env_file=cls.env_file)
        spec = importlib.util.spec_from_file_location(
            "webapp_rate_tier", os.path.join(BASE, "web", "app.py"))
        cls.webapp = importlib.util.module_from_spec(spec)
        sys.modules["webapp_rate_tier"] = cls.webapp
        with contextlib.suppress(Exception):
            spec.loader.exec_module(cls.webapp)

    @classmethod
    def tearDownClass(cls):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k in ("YIBAN_ACCOUNTS_KEY", "YIBAN_ENV_FILE", "YIBAN_ACCOUNTS_FILE",
                  "YIBAN_DB_FILE", "YIBAN_STATE_DIR", "YIBAN_LOG_FILE",
                  "YIBAN_DISABLE_PURGE_LOOP"):
            os.environ.pop(k, None)

    def _app_client(self, login=False):
        # 每个用例新建 app：限速计数是 create_app 内的闭包字典，互不污染
        c = self.webapp.create_app().test_client()
        if login:
            r = c.post("/api/login", json={"username": "admin", "password": ADMIN_PASS})
            self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return c

    def test_anonymous_get_uses_strict_limit(self):
        c = self._app_client()
        statuses = [c.get("/api/announcement").status_code for _ in range(self.webapp.RATE_MAX + 1)]
        self.assertIn(429, statuses, "匿名 GET 超过严格阈值后必须 429")
        self.assertEqual(statuses[: self.webapp.RATE_MAX], [200] * self.webapp.RATE_MAX)

    def test_anonymous_write_uses_strict_limit(self):
        c = self._app_client()
        # 路径不存在无妨：全局限速在路由/鉴权前生效，先按写路径严格计数
        statuses = [c.post("/api/__rate_probe").status_code for _ in range(self.webapp.RATE_MAX + 1)]
        self.assertIn(429, statuses, "匿名 POST 超过严格阈值后必须 429")

    def test_authenticated_get_is_relaxed(self):
        c = self._app_client(login=True)
        statuses = [c.get("/api/me").status_code for _ in range(self.webapp.RATE_MAX + 30)]
        self.assertNotIn(429, statuses,
                         "已登录 GET 在放宽阈值内不应 429（跨过旧严格阈值 60 仍须放行）")
        self.assertTrue(all(s == 200 for s in statuses), f"期望全 200，实际 {set(statuses)}")

    def test_authenticated_write_still_strict(self):
        c = self._app_client(login=True)
        statuses = [c.post("/api/__rate_probe").status_code for _ in range(self.webapp.RATE_MAX + 1)]
        self.assertIn(429, statuses, "已登录写路径仍须维持严格阈值 429")

    def test_auth_get_bucket_does_not_consume_strict_bucket(self):
        c = self._app_client(login=True)
        for _ in range(self.webapp.RATE_MAX + 30):
            self.assertEqual(c.get("/api/me").status_code, 200)
        # 大量已登录 GET 后，写路径仍应放行（两桶独立）。登录本身占用 1 个严格槽，
        # 故此处按 RATE_MAX-1 计数（第 RATE_MAX 次写仍可过）。
        statuses = [c.post("/api/__rate_probe").status_code for _ in range(self.webapp.RATE_MAX - 1)]
        self.assertNotIn(429, statuses, "放宽桶的计数不得挤占严格桶")


if __name__ == "__main__":
    unittest.main()
