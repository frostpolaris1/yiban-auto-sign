# -*- coding: utf-8 -*-
"""会话 Cookie 缓存（v8，session_cache 表）测试。

覆盖 docs/research-lumjiel-core-sign-20260822.md §七：
- CRUD：写入/读取（密文透明还原）/UPSERT（created_at 保留、updated_at 刷新）/清除幂等；
- TTL：过期行读时顺手清除；YIBAN_SESSION_TTL_HOURS 环境变量覆盖默认 12h；
- 密文落库：库内不得出现明文 cookie（AES-GCM 密文对象）；AAD=phone 绑定
  （密文跨账号搬运解密失败 → 按未命中清除，不阻断重登）；
- 迁移：新库直达 v8；v7 旧库升级到 v8（建表 + 存量数据保留）。
"""
import contextlib
import datetime
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "scripts"))

import account_crypto  # noqa: E402
import db  # noqa: E402

TEST_KEY = "a" * 64
AUDIT_KEY = "b" * 64
PHONE = "13800001234"


class SessionCacheDbTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-session-cache-")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(
                f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
                f"YIBAN_AUDIT_KEY={AUDIT_KEY}\n"
            )
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
        os.environ["YIBAN_AUDIT_KEY"] = AUDIT_KEY
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_DB_FILE"] = cls.db_file

    @classmethod
    def tearDownClass(cls):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        for key in (
            "YIBAN_ACCOUNTS_KEY", "YIBAN_AUDIT_KEY", "YIBAN_ENV_FILE",
            "YIBAN_DB_FILE", "YIBAN_SESSION_TTL_HOURS",
        ):
            os.environ.pop(key, None)
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
        os.environ.pop("YIBAN_SESSION_TTL_HOURS", None)

    def _backdate_updated_at(self, hours):
        """把当前缓存行的 updated_at 回拨指定小时数（构造 TTL 过期态）。"""
        stale = (
            datetime.datetime.now() - datetime.timedelta(hours=hours)
        ).strftime("%Y-%m-%d %H:%M:%S")
        conn = db.get_conn()
        conn.execute(
            "UPDATE session_cache SET updated_at=? WHERE phone=?", (stale, PHONE)
        )
        conn.commit()

    # ---- 新库直达 v8，表结构齐备 ----
    def test_fresh_db_reaches_v8_with_session_cache_table(self):
        conn = db.init_db(self.db_file, env_file=self.env_file)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 10)
        cols = {
            r["name"]
            for r in conn.execute("PRAGMA table_info(session_cache)").fetchall()
        }
        self.assertEqual(
            cols, {"phone", "cookies_ct", "csrf", "created_at", "updated_at"}
        )

    # ---- CRUD：写读回环 / UPSERT 语义 / 清除幂等 ----
    def test_session_cache_crud_roundtrip(self):
        db.init_db(self.db_file, env_file=self.env_file)
        self.assertIsNone(db.get_session_cache(PHONE), "未写入时返回 None")

        cookie_json = json.dumps({"csrf_token": "abc", "sessionid": "sid-1"})
        db.set_session_cache(PHONE, cookie_json, "csrf-1")
        got = db.get_session_cache(PHONE)
        self.assertEqual(got["cookies"], cookie_json, "cookies 应透明解密还原")
        self.assertEqual(got["csrf"], "csrf-1")

        # UPSERT：手工回拨两列时间 → 再写一次，created_at 保留、updated_at 刷新
        conn = db.get_conn()
        conn.execute(
            "UPDATE session_cache SET created_at='2026-01-01 00:00:00', "
            "updated_at='2026-01-01 00:00:01' WHERE phone=?",
            (PHONE,),
        )
        conn.commit()
        db.set_session_cache(PHONE, json.dumps({"k": "v"}), "csrf-2")
        row = conn.execute(
            "SELECT csrf, created_at, updated_at FROM session_cache WHERE phone=?", (PHONE,)
        ).fetchone()
        self.assertEqual(row["created_at"], "2026-01-01 00:00:00")
        self.assertGreater(row["updated_at"], "2026-01-01 00:00:01")
        got = db.get_session_cache(PHONE)
        self.assertEqual(got["csrf"], "csrf-2")

        db.clear_session_cache(PHONE)
        self.assertIsNone(db.get_session_cache(PHONE))
        db.clear_session_cache(PHONE)  # 幂等：行不存在时不报错

    # ---- TTL：过期行读时顺手清除 ----
    def test_ttl_expired_row_cleared_on_read(self):
        db.init_db(self.db_file, env_file=self.env_file)
        db.set_session_cache(PHONE, '{"a":"1"}', "c")
        self._backdate_updated_at(hours=13)  # 默认 TTL 12h

        self.assertIsNone(db.get_session_cache(PHONE), "超过默认 12h 应返回 None")
        conn = db.get_conn()
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM session_cache").fetchone()[0],
            0,
            "过期行应在读取时被顺手清除",
        )

    # ---- TTL：YIBAN_SESSION_TTL_HOURS 环境变量覆盖 ----
    def test_ttl_env_override_extends_validity(self):
        db.init_db(self.db_file, env_file=self.env_file)
        db.set_session_cache(PHONE, '{"a":"1"}', "c")
        self._backdate_updated_at(hours=13)
        os.environ["YIBAN_SESSION_TTL_HOURS"] = "24"  # 13h < 24h → 仍有效
        try:
            got = db.get_session_cache(PHONE)
            self.assertIsNotNone(got, "TTL 配置为 24h 时 13h 前的缓存应仍有效")
        finally:
            os.environ.pop("YIBAN_SESSION_TTL_HOURS", None)

    # ---- 密文落库：库内不得出现明文 cookie ----
    def test_cookies_stored_encrypted_not_plaintext(self):
        db.init_db(self.db_file, env_file=self.env_file)
        cookie_json = json.dumps(
            {"csrf_token": "SECRET-CSRF-VALUE", "sessionid": "SECRET-SESSION-XYZ"}
        )
        db.set_session_cache(PHONE, cookie_json, "csrf-plain")

        conn = db.get_conn()
        raw = conn.execute(
            "SELECT cookies_ct FROM session_cache WHERE phone=?", (PHONE,)
        ).fetchone()["cookies_ct"]
        self.assertNotIn("SECRET-CSRF-VALUE", raw, "库内不得出现明文 cookie 值")
        self.assertNotIn("SECRET-SESSION-XYZ", raw)
        obj = json.loads(raw)
        self.assertTrue(account_crypto.is_encrypted(obj), "cookies_ct 应为 AES-GCM 密文对象")
        # 密文不含明文 jar 结构（仅密文 hex）
        self.assertNotIn("csrf_token", raw)

    # ---- AAD=phone 绑定：密文跨账号搬运解密失败 → 按未命中清除 ----
    def test_ciphertext_bound_to_phone_aad(self):
        db.init_db(self.db_file, env_file=self.env_file)
        db.set_session_cache(PHONE, '{"sessionid":"sid"}', "c")
        # 模拟把 A 号密文搬到 B 号（改行主键，AAD 仍为 A 号手机号）
        conn = db.get_conn()
        conn.execute(
            "UPDATE session_cache SET phone='13999999999' WHERE phone=?", (PHONE,)
        )
        conn.commit()

        self.assertIsNone(
            db.get_session_cache("13999999999"),
            "AAD 不匹配的密文应解密失败并按未命中处理",
        )
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM session_cache").fetchone()[0],
            0,
            "解密失败行应被顺手清除",
        )

    # ---- 迁移：v7 旧库升级到 v8（建表 + 存量数据保留）----
    def test_upgrade_from_v7_creates_session_cache(self):
        old_migrations = db._MIGRATIONS
        db._MIGRATIONS = [m for m in old_migrations if m[0] <= 7]
        try:
            db.init_db(self.db_file, env_file=self.env_file)
            conn = db.get_conn()
            self.assertEqual(
                conn.execute("PRAGMA user_version").fetchone()[0], 7,
                "截断迁移列表后应停在 v7",
            )
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("SELECT * FROM session_cache").fetchone()
            db.add_account({
                "name": "存量账号",
                "phone": PHONE,
                "password": "p1",
                "phone_model": "",
                "phone_code": "",
                "owner": "admin",
                "status": "active",
                "reject_reason": "",
            })
        finally:
            db._MIGRATIONS = old_migrations
            if db._conn is not None:
                with contextlib.suppress(Exception):
                    db._conn.close()
                db._conn = None

        conn = db.init_db(self.db_file, env_file=self.env_file)
        self.assertEqual(
            conn.execute("PRAGMA user_version").fetchone()[0], 10,
            "v7 旧库重启后应升级到 v8",
        )
        # v8 表立即可用
        db.set_session_cache(PHONE, '{"a":"1"}', "c")
        self.assertIsNotNone(db.get_session_cache(PHONE))
        # 存量数据保留
        self.assertEqual(
            [a["phone"] for a in db.load_accounts_raw()], [PHONE],
            "升级不得影响既有 accounts 数据",
        )


if __name__ == "__main__":
    unittest.main()
