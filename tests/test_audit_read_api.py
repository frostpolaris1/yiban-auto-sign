# -*- coding: utf-8 -*-
"""审计只读 API（`GET /api/audit-logs`）契约回归测试。

标签：G · 安全：脱敏/审计/配置注入
覆盖：五约束契约——分页显式（page/page_size 回显与边界）、过滤字段白名单、对外行
    id 不透明（16 hex 且无数字自增 id）、脱敏单出口（actor 遮罩 / detail 换行转义）、
    错误一律 JSON（未登录 401 / 普通用户 403 / 未知过滤键 400）。
对应实现：`web/routes/audit_api.py`（`api_audit_logs` / `_serialize_audit_row`）与
    `yiban/store/audit_chain.py`（`read_audit_rows` / `public_row_id`）。
关键断言：只读接口不触发 `verify_audit_chain` 全表哈希（由 test_audit_chain 侧钉链状态），
    且响应体里既无明文邮箱、也无 `audit_logs.id`。
依赖：进程内加载 `web/app.py`（importlib 隔离）+ Flask test client；临时 `.env`/SQLite，
    不联网、不访问真实易班接口。
"""
import ast
import contextlib
import importlib.util
import os
import re
import shutil
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TEST_KEY = "a" * 64
AUDIT_KEY = "b" * 64
ADMIN_PASS = "TestPass1234!"
USER_PASS = "secret1"
ROW_ID_RE = re.compile(r"^[0-9a-f]{16}$")


class AuditReadApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-audit-api-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(
                f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
                f"YIBAN_AUDIT_KEY={AUDIT_KEY}\n"
                f"YIBAN_ADMIN_USER=admin\nYIBAN_ADMIN_PASSWORD={ADMIN_PASS}\n"
            )
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.accounts_file = os.path.join(cls.tmp, "accounts.json")
        cls.users_file = os.path.join(cls.tmp, "users.json")
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
        os.environ["YIBAN_AUDIT_KEY"] = AUDIT_KEY
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_ACCOUNTS_FILE"] = cls.accounts_file
        os.environ["YIBAN_USERS_FILE"] = cls.users_file
        os.environ["YIBAN_DB_FILE"] = cls.db_file
        os.environ["YIBAN_STATE_DIR"] = cls.tmp
        os.environ["YIBAN_LOG_FILE"] = os.path.join(cls.tmp, "sign.log")
        global db
        import db
        spec = importlib.util.spec_from_file_location(
            "webapp_auditapi", os.path.join(BASE, "web", "app.py")
        )
        cls.webapp = importlib.util.module_from_spec(spec)
        sys.modules["webapp_auditapi"] = cls.webapp
        with contextlib.suppress(Exception):
            spec.loader.exec_module(cls.webapp)

    @classmethod
    def tearDownClass(cls):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k in ("YIBAN_ACCOUNTS_KEY", "YIBAN_AUDIT_KEY", "YIBAN_LOG_FILE",
                  "YIBAN_STATE_DIR", "YIBAN_ENV_FILE", "YIBAN_ACCOUNTS_FILE",
                  "YIBAN_USERS_FILE", "YIBAN_DB_FILE"):
            os.environ.pop(k, None)

    def setUp(self):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        for suffix in ("", "-wal", "-shm"):
            p = self.db_file + suffix
            if os.path.exists(p):
                os.remove(p)
        with open(self.accounts_file, "w", encoding="utf-8") as f:
            f.write("[]")
        db.init_db(self.db_file, migrate_from=self.accounts_file, env_file=self.env_file)

    def _login(self, c, username, password):
        r = c.post("/api/login", json={"username": username, "password": password})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return c.get("/api/me").get_json()["csrf_token"]

    def _admin(self):
        c = self.webapp.create_app().test_client()
        self._login(c, "admin", ADMIN_PASS)
        return c

    def test_unauthenticated_returns_401_json(self):
        c = self.webapp.create_app().test_client()
        r = c.get("/api/audit-logs")
        self.assertEqual(r.status_code, 401, r.get_data(as_text=True))
        self.assertEqual(r.mimetype, "application/json")
        self.assertNotIn("<!DOCTYPE", r.get_data(as_text=True))

    def test_normal_user_returns_403_json(self):
        db.create_user("peon@test.local", self.webapp.generate_password_hash(USER_PASS))
        c = self.webapp.create_app().test_client()
        self._login(c, "peon@test.local", USER_PASS)
        r = c.get("/api/audit-logs")
        self.assertEqual(r.status_code, 403, r.get_data(as_text=True))
        self.assertEqual(r.mimetype, "application/json")

    def test_unknown_filter_key_returns_400_json(self):
        c = self._admin()
        r = c.get("/api/audit-logs?evil=1")
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        self.assertEqual(r.mimetype, "application/json")
        self.assertEqual(r.get_json()["error"], "不支持的过滤字段: evil")

    def test_pagination_bounds_rejected(self):
        c = self._admin()
        for qs in ("page_size=201", "page_size=0", "page=0", "page=abc", "page_size=abc"):
            with self.subTest(qs=qs):
                r = c.get("/api/audit-logs?" + qs)
                self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
                self.assertEqual(r.mimetype, "application/json")

    def test_pagination_echo_and_has_more(self):
        c = self._admin()
        for i in range(3):
            db.audit("admin", "unit_test", f"t{i}", "seeded")
        r = c.get("/api/audit-logs?action=unit_test&page=1&page_size=2")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        body = r.get_json()
        self.assertEqual(body["page"], 1)
        self.assertEqual(body["page_size"], 2)
        self.assertEqual(body["total"], 3)  # total 是同过滤条件下的总数，不受分页影响
        self.assertEqual(len(body["rows"]), 2)
        self.assertTrue(body["has_more"])
        r2 = c.get("/api/audit-logs?action=unit_test&page=2&page_size=2")
        body2 = r2.get_json()
        self.assertEqual(len(body2["rows"]), 1)
        self.assertFalse(body2["has_more"])

    def test_row_id_opaque_and_redacted(self):
        c = self._admin()
        db.audit("alice@test.local", "user_delete", "13800138000", "line1\nline2")
        r = c.get("/api/audit-logs?action=user_delete")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        rows = r.get_json()["rows"]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        # 不透明定位符：不含自增 id 键，且是 16 hex
        self.assertNotIn("id", row)
        self.assertRegex(row["row_id"], ROW_ID_RE)
        # 脱敏单出口：actor 已遮罩、detail 换行已转义
        self.assertEqual(row["actor"], "ali***@test.local")
        self.assertNotIn("line1\nline2", row["detail"])
        self.assertIn("line1\\nline2", row["detail"])
        self.assertNotIn("alice@test.local", r.get_data(as_text=True))

    def test_actor_filter_matches_masked_column(self):
        c = self._admin()
        db.audit("bob@test.local", "unit_test", "t-bob", "")
        db.audit("carol@test.local", "unit_test", "t-carol", "")
        r = c.get("/api/audit-logs?actor=bob@test.local")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        rows = r.get_json()["rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["target"], "t-bob")

    # ---- M90-page：page 上界（防 int64 溢出被 except 兜成 500） ----

    def test_page_upper_bound_is_400_not_500(self):
        # 10**30 量级的 page：(page-1)*page_size 溢出 SQLite int64，绑定异常此前被
        # except 兜成 500；上界校验应先返 400。
        r = self._admin().get("/api/audit-logs?page=" + "1" + "0" * 30)
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        self.assertEqual(r.mimetype, "application/json")
        self.assertIn("error", r.get_json())
        self.assertNotIn("<!DOCTYPE", r.get_data(as_text=True))

    # ---- M90-trace：docstring 信封 + 只读端点访问留痕 ----

    def test_docstring_names_pagination_envelope(self):
        path = os.path.join(BASE, "web", "routes", "audit_api.py")
        with open(path, encoding="utf-8") as f:
            doc = ast.get_docstring(ast.parse(f.read()))
        self.assertIsNotNone(doc, "audit_api.py 缺模块 docstring")
        for token in ("rows", "page", "page_size", "total", "has_more"):
            self.assertIn(token, doc, f"docstring 未点名信封字段 {token}")

    def test_read_audit_trace_recorded_on_read(self):
        app = self.webapp.create_app()
        c = app.test_client()
        self._login(c, "admin", ADMIN_PASS)
        seen = []
        orig = app.extensions["yiban_read_audit_trace"]

        def spy(action, target=""):
            seen.append((action, target))
            return orig(action, target)

        app.extensions["yiban_read_audit_trace"] = spy
        r = c.get("/api/audit-logs")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertIn(
            "audit_logs_read", [a for a, _ in seen],
            "只读审计端点未接 read_audit_trace 访问留痕",
        )
        # 留痕行确实落库（窗口内首读必落一行，见 _read_audit_row_due）
        from yiban.store import audit_chain
        _rows, total = audit_chain.read_audit_rows(
            action="audit_logs_read", limit=10, offset=0)
        self.assertGreaterEqual(total, 1, "read_audit_trace 未生成留痕行")


if __name__ == "__main__":
    unittest.main(verbosity=2)
