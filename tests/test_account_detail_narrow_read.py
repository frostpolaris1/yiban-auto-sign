# -*- coding: utf-8 -*-
"""管理面账号详情：按目标行窄取，不整表解密（工单 dnox-B）。

标签：E · Web：认证/权限/API
覆盖：`GET /api/accounts/<idx>/detail` 的读取面，以及 `GET /api/accounts` 列表
    在本批改动前后的逐字节不变性。
对应实现：`web/routes/accounts_api.py::api_account_detail`、
    `web/services/accounts_data.py::load_account_at`、
    `yiban/adapters/persistence/accounts.py::account_row_at` / `read_account_at`。
关键断言：
    ① 详情端点成功回包与改动前**逐字节相等**（含掩码字段），列表无参回包同样逐字节相等；
    ② 详情端点每个请求最多解密 1 行；分页列表每页解密行数等于页大小；
    ③ `password` 与设备识别码原文恒不下发，只给布尔与掩码；
    ④ 非目标行的坏密文不影响目标行读取（窄取的结构性证据）；
    ⑤ `sort_order` 重值时，列表第 idx 行与详情 idx 是同一账号。
依赖：纯本地 Flask test client + 临时 `.env`/SQLite；不联网、不访问真实易班接口；无需 node。

**为什么需要**：详情端点此前用 `load_accounts()` 解密整表再取一行。5k 账号下每个详情
请求做一遍全表 AES-GCM。本文件钉住"只解密目标行"这条性质，并钉住可见回包不变。

用法（项目根目录）：
    py -m pytest tests/test_account_detail_narrow_read.py -v
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
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

import db  # noqa: E402

from yiban.adapters.persistence import accounts as accounts_store  # noqa: E402
from yiban.core.masking import mask_phone as _mask_phone  # noqa: E402

TEST_KEY = "3f9a2c7e5b1d8046a7c3e9f2b6d10485c2a7f4b9e1d63058fa4c7b2e9d15803a"
ADMIN_PASS = "TestPass1234!"

#: 改动前实拍的无参列表回包（4 个账号，含掩码手机号、掩码设备码与状态文案）。
LIST_GOLDEN = (
    b'{"accounts":[{"deleted":false,"deleted_at":"","display_name":"\\u7532",'
    b'"has_password":true,"has_phone_code":true,"index":0,"last_executor":null,'
    b'"name":"\\u7532","owner":"admin","owner_display":"\\u7ba1\\u7406\\u5458",'
    b'"phone":"138****8001","phone_code_masked":"devi\\u20260001","phone_model":"MI 9",'
    b'"reject_reason":"","status":"active","time_pref":null,"time_pref_edge":null,'
    b'"user_paused":false},{"deleted":false,"deleted_at":"","display_name":"\\u4e59",'
    b'"has_password":true,"has_phone_code":false,"index":1,"last_executor":null,'
    b'"name":"\\u4e59","owner":"use***@test.local","owner_display":"use***",'
    b'"phone":"138****8002","phone_code_masked":"","phone_model":"","reject_reason":"",'
    b'"status":"pending","time_pref":null,"time_pref_edge":null,"user_paused":false},'
    b'{"deleted":false,"deleted_at":"","display_name":"\\u4e19","has_password":false,'
    b'"has_phone_code":true,"index":2,"last_executor":null,"name":"\\u4e19",'
    b'"owner":"admin","owner_display":"\\u7ba1\\u7406\\u5458","phone":"138****8003",'
    b'"phone_code_masked":"devi\\u20260003","phone_model":"iPhone15","reject_reason":"",'
    b'"status":"active","time_pref":null,"time_pref_edge":null,"user_paused":false},'
    b'{"deleted":true,"deleted_at":"2026-10-01T00:00:00","display_name":"\\u4e01",'
    b'"has_password":true,"has_phone_code":false,"index":3,"last_executor":null,'
    b'"name":"\\u4e01","owner":"admin","owner_display":"\\u7ba1\\u7406\\u5458",'
    b'"phone":"138****8004","phone_code_masked":"","phone_model":"","reject_reason":"",'
    b'"status":"active","time_pref":null,"time_pref_edge":null,"user_paused":false}],'
    b'"config_file":"yiban.db","ok":true,"state_durs":{},'
    b'"state_msgs":{"138****8001":"\\u5f85\\u751f\\u6210","138****8002":"",'
    b'"138****8003":"\\u5f85\\u751f\\u6210","138****8004":""},"states":{}}\n'
)

#: 改动前实拍的详情回包（逐下标）。
DETAIL_GOLDEN = {
    0: (
        b'{"account":{"deleted":false,"deleted_at":"","display_name":"\\u7532",'
        b'"has_password":true,"has_phone_code":true,"index":0,"name":"\\u7532",'
        b'"owner":"admin","owner_display":"\\u7ba1\\u7406\\u5458","phone":"13800138001",'
        b'"phone_code_masked":"devi\\u20260001","phone_model":"MI 9",'
        b'"reject_reason":"","status":"active","user_paused":false},"ok":true}\n'
    ),
    1: (
        b'{"account":{"deleted":false,"deleted_at":"","display_name":"\\u4e59",'
        b'"has_password":true,"has_phone_code":false,"index":1,"name":"\\u4e59",'
        b'"owner":"user1@test.local","owner_display":"use***","phone":"13800138002",'
        b'"phone_code_masked":"","phone_model":"","reject_reason":"","status":"pending",'
        b'"user_paused":false},"ok":true}\n'
    ),
    2: (
        b'{"account":{"deleted":false,"deleted_at":"","display_name":"\\u4e19",'
        b'"has_password":false,"has_phone_code":true,"index":2,"name":"\\u4e19",'
        b'"owner":"admin","owner_display":"\\u7ba1\\u7406\\u5458","phone":"13800138003",'
        b'"phone_code_masked":"devi\\u20260003","phone_model":"iPhone15",'
        b'"reject_reason":"","status":"active","user_paused":false},"ok":true}\n'
    ),
    3: (
        b'{"account":{"deleted":true,"deleted_at":"2026-10-01T00:00:00",'
        b'"display_name":"\\u4e01","has_password":true,"has_phone_code":false,'
        b'"index":3,"name":"\\u4e01","owner":"admin",'
        b'"owner_display":"\\u7ba1\\u7406\\u5458","phone":"13800138004",'
        b'"phone_code_masked":"","phone_model":"","reject_reason":"","status":"active",'
        b'"user_paused":false},"ok":true}\n'
    ),
}

#: 夹具明文口令：断言绝不随回包出站。
PLAIN_PASSWORDS = ("AlphaPW1!", "BetaPW2!", "DeltaPW4!")


class AccountDetailNarrowReadTest(unittest.TestCase):
    """详情窄读 + 列表回包不变的守卫。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-detail-narrow-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(
                f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
                f"YIBAN_ADMIN_USER=admin\nYIBAN_ADMIN_PASSWORD={ADMIN_PASS}\n"
            )
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_ACCOUNTS_FILE"] = os.path.join(cls.tmp, "accounts.json")
        os.environ["YIBAN_USERS_FILE"] = os.path.join(cls.tmp, "users.json")
        os.environ["YIBAN_DB_FILE"] = cls.db_file
        os.environ["YIBAN_STATE_DIR"] = cls.tmp
        os.environ["YIBAN_LOG_FILE"] = os.path.join(cls.tmp, "sign.log")
        spec = importlib.util.spec_from_file_location(
            "webapp_detail_narrow", os.path.join(BASE, "web", "app.py"))
        cls.webapp = importlib.util.module_from_spec(spec)
        sys.modules["webapp_detail_narrow"] = cls.webapp
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
        db.init_db(self.db_file, env_file=self.env_file)
        self._seed()

    # ---- 夹具 ----
    def _seed(self):
        """4 个账号：甲/乙/丙/丁；乙归属普通用户，丁已软删。

        覆盖口令空与非空、设备码空与非空、owner 掩码与非掩码、deleted 分支。
        """
        db.create_user("user1@test.local", "x", role="user")
        rows = (
            ("甲", "13800138001", "AlphaPW1!", "MI 9", "devicecode-alpha-0001",
             "admin", "active"),
            ("乙", "13800138002", "BetaPW2!", "", "", "user1@test.local", "pending"),
            ("丙", "13800138003", "", "iPhone15", "devicecode-gamma-0003",
             "admin", "active"),
            ("丁", "13800138004", "DeltaPW4!", "", "", "admin", "active"),
        )
        for name, phone, pw, model, code, owner, status in rows:
            db.add_account({
                "name": name, "phone": phone, "password": pw,
                "phone_model": model, "phone_code": code,
                "owner": owner, "status": status, "reject_reason": "",
            })
        db.set_account_deleted(db.load_accounts()[3]["id"], True, "2026-10-01T00:00:00")

    def _admin_client(self):
        c = self.webapp.create_app().test_client()
        r = c.post("/api/login", json={"username": "admin", "password": ADMIN_PASS})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return c

    def _count_decryptions(self, request):
        """执行 request，返回 (响应, 被解密的行数)。

        计数点打在 persistence 层的 `_decrypt_row` 上：它是逐行 AES-GCM 的唯一入口，
        `decrypt_account_rows` 按模块全局取它，故替身对函数体内的调用可见。
        """
        real = accounts_store._decrypt_row
        rows = []

        def counting(row, env_file=None):
            rows.append(dict(row))
            return real(row, env_file)

        with mock.patch.object(accounts_store, "_decrypt_row", side_effect=counting):
            resp = request()
        return resp, len(rows)

    # ---- ① golden 逐字节相等 ----
    def test_list_no_params_golden_bytes_unchanged(self):
        c = self._admin_client()
        r = c.get("/api/accounts")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_data(), LIST_GOLDEN, "列表无参回包必须逐字节不变")
        self.assertNotIn(b'"password"', r.get_data(), "口令字段绝不下发")

    def test_detail_golden_bytes_unchanged(self):
        c = self._admin_client()
        for idx, want in DETAIL_GOLDEN.items():
            r = c.get(f"/api/accounts/{idx}/detail")
            self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
            self.assertEqual(r.get_data(), want, f"详情 idx={idx} 回包必须逐字节不变")

    def test_detail_never_returns_password_or_raw_device_code(self):
        """掩码不变量：口令与识别码原文都不出站，只给布尔与 `前4…后4`。"""
        c = self._admin_client()
        body = c.get("/api/accounts/0/detail").get_data()
        self.assertNotIn(b'"password"', body, "口令字段绝不下发")
        self.assertNotIn(b'"phone_code":', body, "识别码原文字段绝不下发")
        for pw in PLAIN_PASSWORDS:
            self.assertNotIn(pw.encode("utf-8"), body, "明文口令绝不出站")
        self.assertNotIn(b"devicecode-alpha-0001", body, "识别码原文绝不出站")

    # ---- ② 解密计数 ----
    def test_detail_decrypts_at_most_one_row(self):
        c = self._admin_client()
        r, n = self._count_decryptions(lambda: c.get("/api/accounts/1/detail"))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(n, 1, f"详情只许解密目标行；实得 {n} 行（整表为 4 行）")
        self.assertLess(n, len(db.load_accounts_raw()), "必须严格窄于整表")

    def test_paged_list_decrypts_at_most_page_size(self):
        c = self._admin_client()
        r, n = self._count_decryptions(
            lambda: c.get("/api/accounts?group=active&limit=1&offset=0"))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(n, 1, f"一页 1 行只许解密 1 行；实得 {n} 行")

    # ---- ③ 非目标行损坏不影响目标行 ----
    def test_broken_ciphertext_in_other_row_does_not_break_detail(self):
        """窄取的结构性证据：旧实现整表解密，任一行坏密文都会让所有详情 500。"""
        conn = sqlite3.connect(self.db_file)
        try:
            bad = json.dumps({"v": 1, "nonce": "00" * 12,
                              "ct": "00", "tag": "00" * 16})
            conn.execute("UPDATE accounts SET password=? WHERE phone=?",
                         (bad, "13800138001"))
            conn.commit()
        finally:
            conn.close()
        c = self._admin_client()
        r = c.get("/api/accounts/2/detail")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json()["account"]["phone"], "13800138003")
        # 目标行自身损坏仍必须响亮失败，不得静默降级
        r_bad = c.get("/api/accounts/0/detail")
        self.assertNotEqual(r_bad.status_code, 200,
                            "目标行密文损坏必须报错，不得返回错误数据")

    # ---- ④ 下标全序：列表第 idx 行与详情 idx 必须是同一账号 ----
    def test_detail_index_matches_list_order_with_duplicate_sort_order(self):
        """`sort_order` 无唯一约束：重值时列表与详情必须共用同一条全序。

        全序少了破平局键时，"整表第 idx 行"在两条查询间可以指向不同账号；
        前端正是用列表下发的 index 拼详情 URL。
        """
        conn = sqlite3.connect(self.db_file)
        try:
            conn.execute("UPDATE accounts SET sort_order=5")  # 全部重值
            conn.commit()
        finally:
            conn.close()
        c = self._admin_client()
        listed = c.get("/api/accounts").get_json()["accounts"]
        self.assertEqual([a["index"] for a in listed], [0, 1, 2, 3])
        for row in listed:
            detail = c.get(f"/api/accounts/{row['index']}/detail").get_json()["account"]
            self.assertEqual(_mask_phone(detail["phone"]), row["phone"],
                             f"idx={row['index']} 的详情与列表必须是同一账号")


if __name__ == "__main__":
    unittest.main()
