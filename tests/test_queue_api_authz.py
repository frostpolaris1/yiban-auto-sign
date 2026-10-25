# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""工作队列端点鉴权与行形状守卫（lcpx 试点，2026-10-25）。

标签：G · 安全：脱敏/审计/配置注入
覆盖：`GET /api/queue` 与 `GET /api/queue/<idx>/events` 的鉴权边界与行字段。
对应实现：`web/routes/queue_api.py`；鉴权在 `web/app.py` 的 `require_login`。
关键断言：
  ① 未登录 → 401；
  ② 普通用户 → 403，且落 `forbidden_path` 审计（越权尝试必须留痕）；
  ③ 管理员 → 200，行内手机号已遮罩、**不含 `phone_code_masked`**（审查 2：
     本端点读未解密快照，该字段会是密文切片），响应体不含密文片段；
  ④ 不存在的下标 → 404（不是 200 空表，也不是 500）。
依赖：纯本地 Flask test client + 临时 `.env`/SQLite；不联网、无需 node。

为什么必须有这道门（审查 3）：新端点的鉴权完全依赖"路径不在普通用户白名单"这一
隐式事实。白名单里加一个前缀、或 `require_login` 的放行分支多一条，普通用户就能读到
全站账号的当日终态——没有任何测试会红。本文件把"匿名 401 / 普通用户 403 / 管理员 200"
钉成三条断言；突变自证见 PR：把 `/api/queue` 加进普通用户白名单后本文件必红。
"""
import contextlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest

from werkzeug.security import generate_password_hash

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEST_KEY = "a" * 64
ADMIN_PASS = "TestPass1234!"
USER_PASS = "secret1"
USER_EMAIL = "user1@test.local"
PHONE_FULL = "13912345678"
PHONE_MASKED = "139****5678"


class QueueApiAuthzTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-queueauthz-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(
                f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
                f"YIBAN_ADMIN_USER=admin\nYIBAN_ADMIN_PASSWORD={ADMIN_PASS}\n"
            )
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.accounts_file = os.path.join(cls.tmp, "accounts.json")
        cls.users_file = os.path.join(cls.tmp, "users.json")
        cls.log_file = os.path.join(cls.tmp, "sign.log")
        for k, v in (("YIBAN_ACCOUNTS_KEY", TEST_KEY), ("YIBAN_ENV_FILE", cls.env_file),
                     ("YIBAN_ACCOUNTS_FILE", cls.accounts_file),
                     ("YIBAN_USERS_FILE", cls.users_file),
                     ("YIBAN_DB_FILE", cls.db_file), ("YIBAN_STATE_DIR", cls.tmp),
                     ("YIBAN_LOG_FILE", cls.log_file)):
            os.environ[k] = v
        global db
        import db  # 裸模块名：sys.path 已含 scripts（conftest 保证）
        cls.db = db
        spec = importlib.util.spec_from_file_location(
            "webapp_queueauthz", os.path.join(BASE, "web", "app.py"))
        cls.webapp = importlib.util.module_from_spec(spec)
        sys.modules["webapp_queueauthz"] = cls.webapp
        with contextlib.suppress(Exception):
            spec.loader.exec_module(cls.webapp)
        assert callable(getattr(cls.webapp, "create_app", None)), \
            "web/app.py 加载失败（exec_module 异常被抑制）"

    @classmethod
    def tearDownClass(cls):
        if cls.db._conn is not None:
            with contextlib.suppress(Exception):
                cls.db._conn.close()
            cls.db._conn = None
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k in ("YIBAN_ACCOUNTS_KEY", "YIBAN_ENV_FILE", "YIBAN_ACCOUNTS_FILE",
                  "YIBAN_USERS_FILE", "YIBAN_DB_FILE", "YIBAN_STATE_DIR",
                  "YIBAN_LOG_FILE"):
            os.environ.pop(k, None)

    def setUp(self):
        if self.db._conn is not None:
            with contextlib.suppress(Exception):
                self.db._conn.close()
            self.db._conn = None
        for suffix in ("", "-wal", "-shm"):
            path = self.db_file + suffix
            if os.path.exists(path):
                os.remove(path)
        with open(self.accounts_file, "w", encoding="utf-8") as f:
            json.dump([], f)
        self.db.init_db(self.db_file, migrate_from=self.accounts_file,
                        env_file=self.env_file)
        self.db.create_user(USER_EMAIL, generate_password_hash(USER_PASS))
        # 一个生效、当日无结论的账号 ⇒ 落在 unsigned 桶（无需种状态文件）
        self.db.add_account({"name": "queue-authz", "phone": PHONE_FULL,
                             "password": "p", "status": "active", "owner": "admin"})

    # ---- 工具 ----
    def _admin_client(self):
        c = self.webapp.create_app().test_client()
        r = c.post("/api/login", json={"username": "admin", "password": ADMIN_PASS})
        self.assertEqual(r.status_code, 200)
        return c

    def _user_client(self):
        c = self.webapp.create_app().test_client()
        r = c.post("/api/login", json={"username": USER_EMAIL, "password": USER_PASS})
        self.assertEqual(r.status_code, 200)
        return c

    def _forbidden_rows(self):
        return self.db.get_conn().execute(
            "SELECT detail FROM audit_logs WHERE action='forbidden_path'").fetchall()

    # ---- ① 匿名：401 ----
    def test_anonymous_gets_401(self):
        anon = self.webapp.create_app().test_client()
        self.assertEqual(anon.get("/api/queue?bucket=unsigned").status_code, 401)
        self.assertEqual(anon.get("/api/queue/0/events").status_code, 401)

    # ---- ② 普通用户：403 + forbidden_path 留痕 ----
    def test_normal_user_gets_403_and_is_audited(self):
        c = self._user_client()
        self.assertEqual(c.get("/api/queue?bucket=unsigned").status_code, 403)
        self.assertEqual(c.get("/api/queue/0/events").status_code, 403)
        details = [row["detail"] for row in self._forbidden_rows()]
        self.assertTrue(
            any(d.startswith("/api/queue") for d in details),
            f"普通用户越权访问队列端点应留 forbidden_path 审计，实得：{details}",
        )

    # ---- ③ 管理员：200，行已遮罩且不含识别码字段 ----
    def test_admin_gets_rows_without_device_code_field(self):
        c = self._admin_client()
        r = c.get("/api/queue?bucket=unsigned&limit=20&offset=0")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True)[:400])
        body = r.get_json()
        self.assertTrue(body["rows"], "生效且无结论的账号必须出现在 unsigned 桶")
        row = body["rows"][0]
        self.assertEqual(row["phone"], PHONE_MASKED, "行内手机号必须是服务端遮罩值")
        self.assertNotIn("phone_code_masked", row,
                         "队列行不得下发 phone_code_masked（未解密快照下是密文切片）")
        # 响应体里不得出现完整号，也不得出现密文 JSON 片段
        raw_text = r.get_data(as_text=True)
        self.assertNotIn(PHONE_FULL, raw_text, "响应体泄漏完整手机号")
        self.assertNotIn('\\"ct\\"', raw_text, "响应体泄漏密文片段")
        # 分页四键随 limit/offset 出现，摘要与计数同车
        for key in ("total", "limit", "offset", "returned"):
            self.assertIn(key, body)
        for key in ("today", "weekday", "window", "counts"):
            self.assertIn(key, body)
        self.assertEqual(
            set(body["counts"]), {"pending", "rejected", "active", "failed", "unsigned"})

    def test_admin_events_for_unknown_index_is_404(self):
        c = self._admin_client()
        self.assertEqual(c.get("/api/queue/999/events").status_code, 404)


if __name__ == "__main__":
    unittest.main()
