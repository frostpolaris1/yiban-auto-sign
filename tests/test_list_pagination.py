# -*- coding: utf-8 -*-
"""列表接口分页参数（`limit`/`offset`）向后兼容守卫（工单 yiban-auto-sign-wcpy）。

标签：E · Web：认证/权限/API
覆盖：`GET /api/accounts` 与 `GET /api/users` 新增的可选分页参数。
对应实现：`web/routes/__init__.py::page_args`（两域共用的唯一解析点）、
    `web/routes/accounts_api.py::api_accounts`、`web/routes/users_api.py::api_users`。
关键断言：
    ① 不传参数时响应与分页前**逐字相同**——无分页元数据键（total/limit/offset/returned），
       列表为整表，`states`/`state_msgs`/`state_durs` 覆盖全部账号；
    ② `limit`/`offset` 生效：列表按 [offset, offset+limit) 切片；
    ③ `limit` 越界被夹取到 [1, 200]（沿用日志列表 1–200 口径）；
    ④ 非法整数回落到缺省（limit=100 / offset=0），不抛 500；
    ⑤ 分页时逐账号派生映射（states/state_msgs/state_durs）只覆盖当前页，不整表下发。
依赖：纯本地 Flask test client + 临时 `.env`/SQLite；不联网、不访问真实易班接口；无需 node。

**为什么需要**：124 §A.5.1 实测 `/api/accounts` 无 limit/offset，整表返回；按 12 账号外推
5k 下响应体 ≈2.14 MiB，且前端每 10s 轮询一次。分页参数是"轮询只取当前页"的地基。
本文件钉住向后兼容边界：旧调用方（不传参数）的响应一个字节都不能变。
"""
import contextlib
import importlib.util
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

from werkzeug.security import generate_password_hash  # noqa: E402

from yiban.core import clock  # noqa: E402

TEST_KEY = "a" * 64
ADMIN_PASS = "TestPass1234!"
USER_PASS = "secret1"

#: 分页上限，与实现里的 page_args 口径一致（沿用日志列表 1–200）。
PAGE_LIMIT_MAX = 200
PAGE_LIMIT_DEFAULT = 100

#: 分页元数据键（分页时出现；不传参数时不得出现）。
PAGE_META_KEYS = {"total", "limit", "offset", "returned"}


class ListPaginationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-pagination-")
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
            "webapp_pagination", os.path.join(BASE, "web", "app.py"))
        cls.webapp = importlib.util.module_from_spec(spec)
        sys.modules["webapp_pagination"] = cls.webapp
        with contextlib.suppress(Exception):
            spec.loader.exec_module(cls.webapp)

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

    def _seed_accounts(self, n):
        """造 n 个管理员自有账号（owner=admin），手机号递增可预测。"""
        for i in range(n):
            db.add_account({
                "name": f"账号{i:03d}",
                "phone": f"1390000{i:04d}",
                "password": "p1",
                "phone_model": "",
                "phone_code": "",
                "owner": "admin",
                "status": "active",
                "reject_reason": "",
            })

    def _seed_users(self, n):
        """造 n 个注册用户；口令哈希只算一次（n 大时避免 n 次 scrypt）。"""
        h = generate_password_hash(USER_PASS)
        for i in range(n):
            db.create_user(f"user{i:03d}@test.local", h)

    def _write_sign_state(self, phones):
        """写当日按日结构化状态文件，使 load_sign_state 返回非空（states/state_durs 有值）。"""
        path = os.path.join(self.tmp, f"sign-state-{clock.today()}.json")
        data = {p: {"status": "success", "message": "签到成功", "dur": 3, "task": "default"}
                for p in phones}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))

    # ---- ① 默认无参数 = 旧行为 ----
    def test_accounts_default_unchanged(self):
        self._seed_accounts(3)
        c = self._admin_client()
        data = c.get("/api/accounts").get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(len(data["accounts"]), 3, "不传参数必须返回整表")
        self.assertFalse(PAGE_META_KEYS & set(data), "不传参数不得出现分页元数据键")
        # states/state_msgs 覆盖全部账号（无参数时不做页内收窄）
        self.assertEqual(len(data["state_msgs"]), 3, "state_msgs 应覆盖整表")

    def test_users_default_unchanged(self):
        self._seed_users(3)
        c = self._admin_client()
        data = c.get("/api/users").get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(len(data["users"]), 3, "不传参数必须返回整表")
        self.assertFalse(PAGE_META_KEYS & set(data), "不传参数不得出现分页元数据键")

    # ---- ② limit/offset 生效 ----
    def test_accounts_limit_offset_slice(self):
        self._seed_accounts(5)
        c = self._admin_client()
        full = c.get("/api/accounts").get_json()["accounts"]
        self.assertEqual(len(full), 5)
        data = c.get("/api/accounts?limit=2&offset=1").get_json()
        got = [a["phone"] for a in data["accounts"]]
        want = [a["phone"] for a in full[1:3]]
        self.assertEqual(got, want, "limit/offset 必须按 [offset, offset+limit) 切片")
        self.assertEqual(data["total"], 5)
        self.assertEqual(data["returned"], 2)
        self.assertEqual(data["limit"], 2)
        self.assertEqual(data["offset"], 1)

    def test_accounts_paged_index_stays_global(self):
        """分页只切窗口：`accounts[].index` 仍是整表绝对下标。

        前端用它拼行操作 URL（`/api/accounts/<index>/detail` 等，见 accounts-*.js），
        故第二页起 index 不得从 0 重数；否则行操作会指向错号账号。
        """
        self._seed_accounts(5)
        c = self._admin_client()
        full = c.get("/api/accounts").get_json()["accounts"]
        self.assertEqual([a["index"] for a in full], [0, 1, 2, 3, 4])
        pg = c.get("/api/accounts?limit=2&offset=1").get_json()["accounts"]
        self.assertEqual([a["index"] for a in pg], [1, 2], "分页 index 必须是整表绝对下标")
        self.assertEqual([a["index"] for a in pg],
                         [a["index"] for a in full[1:3]])
        self.assertEqual([a["phone"] for a in pg],
                         [a["phone"] for a in full[1:3]], "index 必须仍唯一定位同一账号")

    def test_users_limit_offset_slice(self):
        self._seed_users(5)
        c = self._admin_client()
        full = c.get("/api/users").get_json()["users"]
        self.assertEqual(len(full), 5)
        data = c.get("/api/users?limit=2&offset=2").get_json()
        got = [u["email"] for u in data["users"]]
        want = [u["email"] for u in full[2:4]]
        self.assertEqual(got, want)
        self.assertEqual(data["total"], 5)
        self.assertEqual(data["returned"], 2)
        self.assertEqual(data["offset"], 2)

    # ---- ③ limit 越界被夹取 ----
    def test_accounts_limit_clamped_upper(self):
        self._seed_accounts(PAGE_LIMIT_MAX + 5)  # 205 > 200
        c = self._admin_client()
        data = c.get("/api/accounts?limit=100000").get_json()
        self.assertEqual(data["limit"], PAGE_LIMIT_MAX, "limit 上限必须夹到 200")
        self.assertEqual(data["returned"], PAGE_LIMIT_MAX, "一次最多返回 200 条")

    def test_accounts_limit_clamped_lower(self):
        self._seed_accounts(3)
        c = self._admin_client()
        for bad in ("0", "-7"):
            data = c.get(f"/api/accounts?limit={bad}").get_json()
            self.assertEqual(data["limit"], 1, f"limit={bad} 必须夹到下限 1")
            self.assertEqual(data["returned"], 1, f"limit={bad} 只返回 1 条")

    def test_users_limit_clamped_upper(self):
        self._seed_users(PAGE_LIMIT_MAX + 5)
        c = self._admin_client()
        data = c.get("/api/users?limit=100000").get_json()
        self.assertEqual(data["limit"], PAGE_LIMIT_MAX)
        self.assertEqual(data["returned"], PAGE_LIMIT_MAX)

    # ---- ④ 非法值回落缺省，不抛 500 ----
    def test_accounts_invalid_params_fall_back(self):
        self._seed_accounts(3)
        c = self._admin_client()
        data = c.get("/api/accounts?limit=abc&offset=xyz").get_json()
        self.assertEqual(data["limit"], PAGE_LIMIT_DEFAULT, "非法 limit 回落缺省 100")
        self.assertEqual(data["offset"], 0, "非法 offset 回落 0")
        self.assertEqual(data["returned"], 3)

    def test_users_invalid_params_fall_back(self):
        self._seed_users(3)
        c = self._admin_client()
        data = c.get("/api/users?limit=abc").get_json()
        self.assertEqual(data["limit"], PAGE_LIMIT_DEFAULT)
        self.assertEqual(data["offset"], 0)
        self.assertEqual(data["returned"], 3)

    # ---- 只给 offset：按缺省页宽分页 ----
    def test_accounts_offset_without_limit(self):
        self._seed_accounts(5)
        c = self._admin_client()
        data = c.get("/api/accounts?offset=2").get_json()
        self.assertEqual(data["offset"], 2)
        self.assertEqual(data["limit"], PAGE_LIMIT_DEFAULT)
        self.assertEqual(data["returned"], 3)

    def test_accounts_offset_beyond_end(self):
        self._seed_accounts(3)
        c = self._admin_client()
        data = c.get("/api/accounts?limit=10&offset=999").get_json()
        self.assertEqual(data["returned"], 0)
        self.assertEqual(data["accounts"], [])
        self.assertEqual(data["total"], 3)

    def test_users_offset_beyond_end(self):
        self._seed_users(3)
        c = self._admin_client()
        data = c.get("/api/users?limit=10&offset=999").get_json()
        self.assertEqual(data["returned"], 0)
        self.assertEqual(data["users"], [])

    # ---- ⑤ 页内派生映射只覆盖当前页 ----
    def test_accounts_states_scoped_to_page(self):
        self._seed_accounts(4)
        self._write_sign_state([f"1390000{i:04d}" for i in range(4)])
        c = self._admin_client()
        full = c.get("/api/accounts").get_json()
        pg = c.get("/api/accounts?limit=1&offset=1").get_json()
        self.assertEqual(len(full["states"]), 4, "不传参数时 states 覆盖整表")
        self.assertEqual(len(pg["state_msgs"]), 1, "state_msgs 只覆盖当前页")
        self.assertEqual(len(pg["states"]), 1, "states 只覆盖当前页")
        self.assertEqual(len(pg["state_durs"]), 1, "state_durs 只覆盖当前页")
        self.assertLess(len(pg["states"]), len(full["states"]),
                        "分页后的 states 必须比整表小（轮询只取当前页）")

    def test_users_pagination_does_not_shrink_builtin_admin(self):
        self._seed_users(2)
        c = self._admin_client()
        pg = c.get("/api/users?limit=1").get_json()
        self.assertTrue(pg["builtin_admin"], "内置管理员名不随分页缺失")


    # ---- ⑥ 服务端分组与检索（F5b：5k 下不再整表取全） ----
    def _seed_grouped_accounts(self):
        # 2 正常、3 待审核、1 已拒绝；手机号/名称可预测。
        for i in range(2):
            db.add_account({
                "name": f"正常{i}", "phone": f"1391000{i:04d}", "password": "p1",
                "phone_model": "", "phone_code": "", "owner": "admin",
                "status": "active", "reject_reason": "",
            })
        for i in range(3):
            db.add_account({
                "name": f"待审{i}", "phone": f"1392000{i:04d}", "password": "p1",
                "phone_model": "", "phone_code": "", "owner": "admin",
                "status": "pending", "reject_reason": "",
            })
        db.add_account({
            "name": "已拒0", "phone": "13930000000", "password": "p1",
            "phone_model": "", "phone_code": "", "owner": "admin",
            "status": "rejected", "reject_reason": "资料不全",
        })

    def test_accounts_group_filters_and_orders_pending_first(self):
        self._seed_grouped_accounts()
        c = self._admin_client()
        data = c.get("/api/accounts?group=pending&limit=100&offset=0").get_json()
        self.assertEqual(data["total"], 4, "待处理组 = 待审核 + 已拒绝")
        self.assertEqual([a["status"] for a in data["accounts"]],
                         ["pending", "pending", "pending", "rejected"])
        self.assertEqual(data["group_totals"]["pending"], 4)
        self.assertEqual(data["group_totals"]["active"], 2)
        self.assertEqual(data["group_totals"]["deleted"], 0)

    def test_paged_list_decrypts_only_current_page(self):
        """分页只解密当前页：非当前页的坏密文不应让列表 500（5k 全量解密会撞 20s 超时）。"""
        self._seed_accounts(3)
        conn = sqlite3.connect(self.db_file)
        try:
            conn.execute("UPDATE accounts SET password=? WHERE phone=?",
                         ("not-a-json-ciphertext", "13900000001"))
            conn.commit()
        finally:
            conn.close()
        c = self._admin_client()
        counted = c.get("/api/accounts?count_only=1")
        self.assertEqual(counted.status_code, 200, counted.get_data(as_text=True))
        page = c.get("/api/accounts?group=active&limit=1&offset=0")
        self.assertEqual(page.status_code, 200, page.get_data(as_text=True))
        self.assertEqual(page.get_json()["returned"], 1)

    def test_accounts_group_search_narrows_total(self):
        self._seed_grouped_accounts()
        c = self._admin_client()
        data = c.get("/api/accounts?group=active&q=%E6%AD%A3%E5%B8%B80&limit=100&offset=0").get_json()
        self.assertEqual(data["total"], 1, "检索作用于组内，total 是过滤后总数")
        self.assertEqual(len(data["accounts"]), 1)
        self.assertEqual(data["group_totals"]["active"], 2, "组总量不随检索收窄")

    def test_accounts_group_stats_cover_all_active(self):
        self._seed_grouped_accounts()
        self._write_sign_state([f"1391000{i:04d}" for i in range(2)])
        c = self._admin_client()
        data = c.get("/api/accounts?group=active&limit=1&offset=0").get_json()
        self.assertEqual(data["stats"]["success"], 2, "KPI 覆盖全部正常账号，不随当前页收窄")
        self.assertEqual(len(data["accounts"]), 1)

    def test_accounts_group_stats_do_not_drift_with_user_paused(self):
        ids = []
        for i in range(3):
            ids.append(db.add_account({
                "name": f"暂停探针{i}", "phone": f"1395000{i:04d}", "password": "p1",
                "phone_model": "", "phone_code": "", "owner": "admin",
                "status": "active", "reject_reason": "",
            }))
        db.set_user_paused(ids[1], True)
        c = self._admin_client()
        first = c.get("/api/accounts?group=active&limit=1&offset=0").get_json()
        second = c.get("/api/accounts?group=active&limit=1&offset=1").get_json()
        self.assertEqual(first["stats"], second["stats"],
                         "KPI 覆盖全部正常账号，不得随 offset 漂移")
        self.assertEqual(first["stats"]["skipped"], 1)
        self.assertEqual(first["stats"]["waiting"], 2)

    def test_accounts_etag_revalidation_returns_304(self):
        self._seed_grouped_accounts()
        c = self._admin_client()
        first = c.get("/api/accounts?group=active&limit=1&offset=0")
        etag = first.headers.get("ETag")
        self.assertTrue(etag, "分页响应必须带 ETag")
        second = c.get("/api/accounts?group=active&limit=1&offset=0",
                        headers={"If-None-Match": etag})
        self.assertEqual(second.status_code, 304)
        self.assertEqual(second.get_data(), b"")

    def test_accounts_weak_etag_also_returns_304(self):
        self._seed_grouped_accounts()
        c = self._admin_client()
        first = c.get("/api/accounts?group=active&limit=1&offset=0")
        etag = first.headers.get("ETag")
        self.assertTrue(etag)
        weak = c.get("/api/accounts?group=active&limit=1&offset=0",
                     headers={"If-None-Match": "W/" + etag})
        self.assertEqual(weak.status_code, 304, "If-None-Match 用弱比较，W/ 前缀也要命中")

    def _seed_grouped_users(self):
        self._seed_users(3)
        # user000：只有正常账号 → normal；user001：有待审核账号 → pending；user002：无账号 → vacant。
        db.add_account({
            "name": "u0", "phone": "13940000001", "password": "p1", "phone_model": "",
            "phone_code": "", "owner": "user000@test.local", "status": "active",
            "reject_reason": "",
        })
        db.add_account({
            "name": "u1", "phone": "13940000002", "password": "p1", "phone_model": "",
            "phone_code": "", "owner": "user001@test.local", "status": "pending",
            "reject_reason": "",
        })

    def test_users_group_filters_and_totals(self):
        self._seed_grouped_users()
        c = self._admin_client()
        normal = c.get("/api/users?group=normal&limit=100&offset=0").get_json()
        pending = c.get("/api/users?group=pending&limit=100&offset=0").get_json()
        vacant = c.get("/api/users?group=vacant&limit=100&offset=0").get_json()
        self.assertEqual(normal["total"], 1)
        self.assertEqual(normal["users"][0]["email"], "user000@test.local")
        self.assertEqual(pending["total"], 1)
        self.assertEqual(pending["users"][0]["email"], "user001@test.local")
        self.assertEqual(vacant["total"], 1)
        self.assertEqual(vacant["users"][0]["email"], "user002@test.local")
        self.assertEqual(normal["group_totals"], {"pending": 1, "normal": 1, "vacant": 1})

    def test_users_search_matches_visible_masked_email(self):
        h = generate_password_hash(USER_PASS)
        db.create_user("alice@test.local", h)
        db.create_user("bob@test.local", h)
        c = self._admin_client()
        full = c.get("/api/users?q=alice%40test.local&limit=100&offset=0").get_json()
        masked = c.get("/api/users?q=ali%2A%2A%2A%40test.local&limit=100&offset=0").get_json()
        self.assertEqual(full["total"], 1)
        self.assertEqual(masked["total"], 1, "检索必须命中页面可见的遮罩邮箱")
        self.assertEqual(masked["users"][0]["email"], "alice@test.local")

    def test_users_search_audit_target_does_not_leak_raw_email(self):
        """审计 target 必须已脱敏：完整邮箱检索词不得原样进审计表。"""
        h = generate_password_hash(USER_PASS)
        db.create_user("alice@test.local", h)
        c = self._admin_client()
        resp = c.get("/api/users?q=alice%40test.local&limit=100&offset=0")
        self.assertEqual(resp.status_code, 200)
        conn = sqlite3.connect(self.db_file)
        try:
            row = conn.execute(
                "SELECT detail FROM audit_logs WHERE action=? ORDER BY id DESC LIMIT 1",
                ("users_list_read",)).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row, "列表读取必须留审计")
        self.assertNotIn("alice@test.local", row[0], "审计目标摘要不得含原始邮箱")
        self.assertIn("q=有", row[0])

    def test_users_etag_revalidation_returns_304(self):
        self._seed_grouped_users()
        c = self._admin_client()
        first = c.get("/api/users?group=normal&limit=1&offset=0")
        etag = first.headers.get("ETag")
        self.assertTrue(etag, "用户列表分页响应必须带 ETag")
        second = c.get("/api/users?group=normal&limit=1&offset=0",
                        headers={"If-None-Match": etag})
        self.assertEqual(second.status_code, 304)

    def test_accounts_search_matches_visible_masked_owner(self):
        """账号列表「归属」列显示遮罩邮箱；检索必须命中这个可见值。"""
        for name, phone, owner in (("alice-acct", "13950000001", "alice@test.local"),
                                   ("bob-acct", "13950000002", "bob@test.local")):
            db.add_account({
                "name": name, "phone": phone, "password": "p1", "phone_model": "",
                "phone_code": "", "owner": owner, "status": "active",
                "reject_reason": "",
            })
        c = self._admin_client()
        full = c.get("/api/accounts?group=active&q=alice%40test.local&limit=20&offset=0").get_json()
        masked = c.get("/api/accounts?group=active&q=ali%2A%2A%2A%40test.local&limit=20&offset=0").get_json()
        self.assertEqual(full["total"], 1)
        self.assertEqual(masked["total"], 1, "检索必须命中页面可见的遮罩归属邮箱")
        self.assertEqual(masked["accounts"][0]["name"], "alice-acct")


if __name__ == "__main__":
    unittest.main()
