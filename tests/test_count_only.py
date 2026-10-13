# -*- coding: utf-8 -*-
"""`GET /api/accounts` 与 `GET /api/users` 的「只取计数」通路（`count_only=1`）守卫
（工单 yiban-auto-sign-p37f 后端面）。

标签：E · Web：认证/权限/API
覆盖：`web/routes/accounts_api.py::api_accounts`、`web/routes/users_api.py::api_users`
    新增的 `count_only` 参数。
关键断言（测不变式，不测编舞）：
    ① 不传 `count_only` 时响应与今天**逐字相同**——列表仍在、派生映射覆盖整表；
    ② `count_only=1` 的计数等于**从整表列表自行推导**的待处理数
       （账号：非删除且 status in {pending, rejected}；用户：名下有 pending/rejected 账号的用户数）；
    ③ `count_only=1` 不下发列表（`accounts`/`users` 键缺席）——这才是"只取计数"，
       否则 5k 账号下 10 秒轮询仍每拍拉约 1MB 全量 payload；
    ④ `count_only=1` 的总数等于分页元数据里的 `total`（同一事实同一数）。
依赖：纯本地 Flask test client + 临时 `.env`/SQLite；不联网、无需 node。

**背景**：导航徽标每 10 秒轮询 `/api/accounts` 与 `/api/users`，只为算一个标量
（core.js::loadNavBadges）。5k 账号下每拍约 1MB 全量 payload（123 审查实测的卡顿根因）。
本通路让轮询只取计数。前端接线属"前端面"（本批推迟）；本文件只钉后端能力与口径。
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
ADMIN_PASS = "TestPass1234!"

PENDING = "pending"
REJECTED = "rejected"
ACTIVE = "active"


class CountOnlyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-countonly-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(
                f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
                f"YIBAN_ADMIN_USER=admin\nYIBAN_ADMIN_PASSWORD={ADMIN_PASS}\n"
            )
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.accounts_file = os.path.join(cls.tmp, "accounts.json")
        cls.users_file = os.path.join(cls.tmp, "users.json")
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_ACCOUNTS_FILE"] = cls.accounts_file
        os.environ["YIBAN_USERS_FILE"] = cls.users_file
        os.environ["YIBAN_DB_FILE"] = cls.db_file
        os.environ["YIBAN_STATE_DIR"] = cls.tmp
        os.environ["YIBAN_LOG_FILE"] = os.path.join(cls.tmp, "sign.log")
        global db
        import db
        spec = importlib.util.spec_from_file_location(
            "webapp_countonly", os.path.join(BASE, "web", "app.py"))
        cls.webapp = importlib.util.module_from_spec(spec)
        sys.modules["webapp_countonly"] = cls.webapp
        with contextlib.suppress(Exception):
            spec.loader.exec_module(cls.webapp)
        cls.webapp.migrate_admin_password_to_hash(cls.env_file)

    @classmethod
    def tearDownClass(cls):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k in ("YIBAN_ACCOUNTS_KEY", "YIBAN_LOG_FILE", "YIBAN_STATE_DIR",
                  "YIBAN_ENV_FILE", "YIBAN_ACCOUNTS_FILE", "YIBAN_USERS_FILE",
                  "YIBAN_DB_FILE"):
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
        with open(self.users_file, "w", encoding="utf-8") as f:
            f.write("[]")
        db.init_db(self.db_file, migrate_from=self.accounts_file, env_file=self.env_file)

    # ---- 夹具 ----
    def _admin_client(self):
        c = self.webapp.create_app().test_client()
        r = c.post("/api/login", json={"username": "admin", "password": ADMIN_PASS})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return c

    def _add_account(self, phone, status, owner="admin"):
        return db.add_account({
            "name": f"账号{phone}", "phone": phone, "password": "p1",
            "phone_model": "", "phone_code": "", "owner": owner,
            "status": status, "reject_reason": "",
        })

    def _seed_users(self, n):
        h = self.webapp.generate_password_hash("secret1")
        for i in range(n):
            db.create_user(f"user{i:03d}@test.local", h)

    # ---- ① 默认无参数 = 旧行为 ----
    def test_accounts_default_unchanged(self):
        self._add_account("13900000001", PENDING)
        self._add_account("13900000002", ACTIVE)
        c = self._admin_client()
        data = c.get("/api/accounts").get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(len(data["accounts"]), 2, "不传 count_only：列表仍在")
        self.assertNotIn("pending", data, "不传 count_only 不得出现计数键")

    def test_users_default_unchanged(self):
        self._seed_users(2)
        c = self._admin_client()
        data = c.get("/api/users").get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(len(data["users"]), 2, "不传 count_only：列表仍在")
        self.assertNotIn("review", data, "不传 count_only 不得出现计数键")

    # ---- ② 计数正确（= 从整表自行推导的待处理数）----
    def test_accounts_count_only_matches_derived(self):
        self._add_account("13900000001", PENDING)
        self._add_account("13900000002", PENDING)
        self._add_account("13900000003", REJECTED)
        self._add_account("13900000004", ACTIVE)
        c = self._admin_client()
        full = c.get("/api/accounts").get_json()["accounts"]
        want_pending = len([a for a in full
                            if not a.get("deleted") and a["status"] == PENDING])
        want_rejected = len([a for a in full
                             if not a.get("deleted") and a["status"] == REJECTED])
        self.assertEqual((want_pending, want_rejected), (2, 1))
        data = c.get("/api/accounts?count_only=1").get_json()
        self.assertEqual(data["pending"], want_pending, "pending 必须等于整表推导值")
        self.assertEqual(data["rejected"], want_rejected, "rejected 必须等于整表推导值")

    def test_accounts_count_excludes_deleted(self):
        """软删除账号不计入待处理（徽标口径：`!a.deleted`）。"""
        aid = self._add_account("13900000001", PENDING)
        self._add_account("13900000002", PENDING)
        db.set_account_deleted(aid, True, deleted_at="2026-01-01 00:00:00")
        c = self._admin_client()
        data = c.get("/api/accounts?count_only=1").get_json()
        self.assertEqual(data["pending"], 1, "已删除的 pending 账号不计入")

    def test_users_count_only_matches_derived(self):
        self._seed_users(3)
        # user000 名下 1 待审核；user001 名下 1 已拒绝（也算待处理）；user002 干净
        self._add_account("13900000001", PENDING, owner="user000@test.local")
        self._add_account("13900000002", REJECTED, owner="user001@test.local")
        self._add_account("13900000003", ACTIVE, owner="user002@test.local")
        c = self._admin_client()
        full = c.get("/api/users").get_json()["users"]
        want = len([u for u in full if int(u.get("review_count") or 0) > 0])
        self.assertEqual(want, 2, "有 pending/rejected 账号的用户数 = 2")
        data = c.get("/api/users?count_only=1").get_json()
        self.assertEqual(data["review"], want, "review 必须等于整表推导值")

    # ---- ③ 不下发列表 ----
    def test_count_only_omits_list(self):
        self._add_account("13900000001", PENDING)
        self._seed_users(1)
        c = self._admin_client()
        acc = c.get("/api/accounts?count_only=1").get_json()
        usr = c.get("/api/users?count_only=1").get_json()
        self.assertNotIn("accounts", acc, "count_only 不得下发账号整表")
        self.assertNotIn("states", acc, "count_only 不得下发派生状态映射")
        self.assertNotIn("state_msgs", acc)
        self.assertNotIn("state_durs", acc)
        self.assertNotIn("users", usr, "count_only 不得下发用户整表")
        self.assertNotIn("builtin_admin", usr, "count_only 不下发列表附带的展示字段")

    def test_count_only_key_set_frozen(self):
        """count_only 的顶层键集合恰好是计数面——多一个键（列表/派生映射）即红。"""
        self._add_account("13900000001", PENDING)
        self._seed_users(1)
        c = self._admin_client()
        acc = c.get("/api/accounts?count_only=1").get_json()
        usr = c.get("/api/users?count_only=1").get_json()
        self.assertEqual(set(acc), {"ok", "total", "pending", "rejected"})
        self.assertEqual(set(usr), {"ok", "total", "review"})

    # ---- ④ 总数与分页 total 同源 ----
    def test_count_only_total_matches_page_total(self):
        for i in range(4):
            self._add_account(f"1390000000{i}", ACTIVE)
        self._seed_users(3)
        c = self._admin_client()
        acc = c.get("/api/accounts?count_only=1").get_json()
        usr = c.get("/api/users?count_only=1").get_json()
        acc_page = c.get("/api/accounts?limit=1").get_json()
        usr_page = c.get("/api/users?limit=1").get_json()
        self.assertEqual(acc["total"], acc_page["total"], "count_only 的 total 必须与分页 total 同源")
        self.assertEqual(usr["total"], usr_page["total"])

    def test_count_only_total_unchanged_by_page_window(self):
        """count_only 的总数是整表大小，不随 limit/offset 变化。"""
        for i in range(5):
            self._add_account(f"1390000000{i}", ACTIVE)
        c = self._admin_client()
        a = c.get("/api/accounts?count_only=1&limit=2&offset=1").get_json()
        b = c.get("/api/accounts?count_only=1").get_json()
        self.assertEqual(a["total"], 5)
        self.assertEqual(b["total"], 5)
        self.assertNotIn("accounts", a, "count_only 与分页参数共存时仍不下发列表")


if __name__ == "__main__":
    unittest.main()
