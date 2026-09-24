# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""会话缓存表族与会话恢复：有效期判定、凭据加密、重启后恢复。

`session_cache` 表族承载 Web 会话（含「记住我」的召回令牌）；本文件并两处断言：表级
读写与业务日口径、以及重启/清理流程下的会话恢复与凭据不落明文。

功能：会话缓存与会话恢复链路的行为回归。
归属：`yiban/store` 会话缓存域 + `web/` 会话流程的交叉测试。
复用：`BASE` / `TEST_KEY` 与临时库装配助手。
通信：写临时 SQLite 与临时 `.env`，按会话 API 读写；由 pytest 收集 `unittest.TestCase`。
"""
import contextlib
import datetime
import importlib.util
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime as _datetime_RESTORE
from datetime import timedelta
from unittest import mock

import db
import signin

from yiban.infra import account_crypto

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


TEST_KEY = "a" * 64


AUDIT_KEY = "b" * 64


PHONE = "13800001234"


class _SessionCacheFixture(unittest.TestCase):
    """两组用例共用的隔离夹具（自身无 test_ 方法，不会被收集）。"""

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

    def _backdate_updated_at(self, hours, base_now):
        """把当前缓存行的 updated_at 回拨到 base_now-hours（构造过期态）。

        base_now 必须与读取时钉住的 _session_cache_now 同值（读写同钟）：
        时间戳落点完全由用例控制，不随挂钟时刻漂移。旧行为相对真实挂钟回拨——
        2026-08-31 引入"跨业务日作废"后，13:00 前运行会落到昨日、被跨日判据
        作废，判定结果取决于当天几点跑。
        """
        stale = (base_now - datetime.timedelta(hours=hours)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        conn = db.get_conn()
        conn.execute(
            "UPDATE session_cache SET updated_at=? WHERE phone=?", (stale, PHONE)
        )
        conn.commit()


class SessionCacheDbTest(_SessionCacheFixture):
    # ---- 判定钟钉死（任意墙钟时刻运行结果一致）----
    # 2026-08-31 起 get_session_cache 先判"跨业务日"再判同日 TTL。旧用例把
    # updated_at 相对真实挂钟回拨 13h：13:00 前运行会落到昨日、被跨日判据作废，
    # 用例结果取决于当天几点跑。现把判定钟钉在当日 15:00（_session_cache_now
    # 接缝，与 SessionCacheBusinessDayTest 同法）：13h 回拨恒落在当日 02:00，
    # "同日/跨日"完全由用例控制——本类显式覆盖"同日内 TTL 过期/续期"与
    # "调大 TTL 解锁不了跨业务日"两类语义。
    NOW = datetime.datetime(2026, 9, 1, 15, 0, 0)

    def _get(self):
        with mock.patch.object(db, "_session_cache_now", return_value=self.NOW):
            return db.get_session_cache(PHONE)

    # ---- 新库直达 v8，表结构齐备 ----

    def test_fresh_db_reaches_v8_with_session_cache_table(self):
        conn = db.init_db(self.db_file, env_file=self.env_file)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0],
                         db._MIGRATIONS[-1][0])
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

    # ---- TTL：过期行读时顺手清除（同日内 TTL 过期；判定钟钉死见类注释）----
    def test_ttl_expired_row_cleared_on_read(self):
        db.init_db(self.db_file, env_file=self.env_file)
        db.set_session_cache(PHONE, '{"a":"1"}', "c")
        self._backdate_updated_at(hours=13, base_now=self.NOW)  # 落在当日 02:00，默认 TTL 6h

        with self.assertLogs("yiban.store.session_cache", level="INFO") as captured:
            self.assertIsNone(self._get(), "同日内超出默认 TTL 6h 应返回 None")
        joined = "\n".join(captured.output)
        self.assertIn("同日内超出 TTL", joined, "应按同日 TTL 过期作废，而非跨业务日")
        self.assertNotIn("跨业务日", joined)
        conn = db.get_conn()
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM session_cache").fetchone()[0],
            0,
            "过期行应在读取时被顺手清除",
        )

    # ---- 作废日志不得印裸号：输出面兜底之外，调用点本身也须先脱敏 ----
    def test_void_log_masks_phone_at_call_site(self):
        db.init_db(self.db_file, env_file=self.env_file)
        db.set_session_cache(PHONE, '{"a":"1"}', "c")
        self._backdate_updated_at(hours=13, base_now=self.NOW)
        # assertLogs 用默认 formatter（只取 message），故此处断言的是调用点自身
        # 传入的文本，而非输出面 formatter 的兜底效果。
        with self.assertLogs("yiban.store.session_cache", level="INFO") as captured:
            self.assertIsNone(self._get())
        joined = "\n".join(captured.output)
        self.assertIn("会话缓存作废", joined, "作废路径应留痕")
        self.assertNotIn(PHONE, joined, "调用点日志不得含裸号")
        self.assertIn("138****1234", joined)

    # ---- TTL：YIBAN_SESSION_TTL_HOURS 环境变量覆盖（同日内放宽时长）----
    def test_ttl_env_override_extends_validity(self):
        db.init_db(self.db_file, env_file=self.env_file)
        db.set_session_cache(PHONE, '{"a":"1"}', "c")
        self._backdate_updated_at(hours=13, base_now=self.NOW)  # 落在当日 02:00
        os.environ["YIBAN_SESSION_TTL_HOURS"] = "24"  # 13h < 24h → 仍有效
        try:
            self.assertIsNotNone(
                self._get(), "TTL 配置为 24h 时同日内 13h 前的缓存应仍有效"
            )
        finally:
            os.environ.pop("YIBAN_SESSION_TTL_HOURS", None)

    # ---- TTL 调大不解锁跨业务日（与上例对照，跨业务日作废在此显式覆盖）----
    def test_ttl_override_cannot_unlock_cross_business_day(self):
        db.init_db(self.db_file, env_file=self.env_file)
        db.set_session_cache(PHONE, '{"a":"1"}', "c")
        # 钉死钟的"昨日 23:59:59"：距 NOW 仅 15 小时余，远小于 72h——若按旧口径
        # （只看小时数）应命中复用；跨业务日判据必须排在 TTL 之前将其作废。
        yesterday = (self.NOW - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
        conn = db.get_conn()
        conn.execute(
            "UPDATE session_cache SET updated_at=? WHERE phone=?",
            (f"{yesterday} 23:59:59", PHONE),
        )
        conn.commit()
        os.environ["YIBAN_SESSION_TTL_HOURS"] = "72"
        try:
            with self.assertLogs("yiban.store.session_cache", level="INFO") as captured:
                self.assertIsNone(
                    self._get(), "跨业务日缓存必须作废，调大 TTL 也解锁不了"
                )
            self.assertIn("跨业务日", "\n".join(captured.output))
        finally:
            os.environ.pop("YIBAN_SESSION_TTL_HOURS", None)
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM session_cache").fetchone()[0],
            0,
            "跨日行应在读取时被顺手清除",
        )

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
            conn.execute("PRAGMA user_version").fetchone()[0], db._MIGRATIONS[-1][0],
            "v7 旧库重启后应升级到最新版本",
        )
        # v8 表立即可用
        db.set_session_cache(PHONE, '{"a":"1"}', "c")
        self.assertIsNotNone(db.get_session_cache(PHONE))
        # 存量数据保留
        self.assertEqual(
            [a["phone"] for a in db.load_accounts_raw()], [PHONE],
            "升级不得影响既有 accounts 数据",
        )


class SessionCacheBusinessDayTest(_SessionCacheFixture):
    """2026-08-31 公测复盘：会话缓存主判据由"多少小时"改为"是否同一业务日"。

    生产当天 3 个账号复用的正是前一晚 20:35 写入、已被服务端作废的会话——旧默认
    TTL 12h 恰好横跨一夜。时间戳全部相对被钉住的 _session_cache_now 构造，
    用例不随挂钟时刻漂移（旧用例回拨 13h 的判定结果就取决于当天几点跑）。
    """

    NOW = datetime.datetime(2026, 8, 31, 6, 31, 0)

    def _ts(self, hours_ago=None, day_offset=0, at="06:31:00"):
        date = (self.NOW + datetime.timedelta(days=day_offset)).strftime("%Y-%m-%d")
        if hours_ago is not None:
            return (self.NOW - datetime.timedelta(hours=hours_ago)).strftime("%Y-%m-%d %H:%M:%S")
        return f"{date} {at}"

    def _write_with_updated_at(self, updated_at):
        db.init_db(self.db_file, env_file=self.env_file)
        db.set_session_cache(PHONE, '{"a":"1"}', "c")
        conn = db.get_conn()
        conn.execute(
            "UPDATE session_cache SET updated_at=? WHERE phone=?", (updated_at, PHONE)
        )
        conn.commit()

    def _get(self):
        with mock.patch.object(db, "_session_cache_now", return_value=self.NOW):
            return db.get_session_cache(PHONE)

    def test_default_ttl_lowered_to_six_hours(self):
        # 同日窗口最长 80 分钟，护栏只该在同日内起兜底作用；12h 是当年"横跨一夜"的元凶
        self.assertEqual(db.SESSION_CACHE_TTL_HOURS_DEFAULT, 6)

    def test_cross_day_row_dies_even_with_seventy_two_hour_ttl(self):
        """最硬的一条：调大 TTL 不再解锁跨天复用（旧口径 12h→24h 曾被用来支持它）。"""
        self._write_with_updated_at(self._ts(day_offset=-1, at="23:59:59"))  # 仅 6.5h 前
        os.environ["YIBAN_SESSION_TTL_HOURS"] = "72"
        try:
            self.assertIsNone(self._get(), "前一晚写入的缓存必须作废，与 TTL 大小无关")
        finally:
            os.environ.pop("YIBAN_SESSION_TTL_HOURS", None)
        conn = db.get_conn()
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM session_cache").fetchone()[0], 0,
            "跨日行应在读取时被顺手清除",
        )

    def test_same_day_five_hours_ago_still_reused(self):
        """同日内照常复用——否则 06:31 首轮与 07:10 兜底之间的免登录收益就没了。"""
        self._write_with_updated_at(self._ts(hours_ago=5))
        got = self._get()
        self.assertIsNotNone(got, "同日内 5 小时的缓存应仍有效")
        self.assertEqual(got["csrf"], "c", "应透明还原密文，不得因判据改造而丢内容")

    def test_same_day_beyond_ttl_guard_still_dies(self):
        self._write_with_updated_at(self._ts(hours_ago=7))  # 同一天，但超 6h 护栏
        self.assertIsNone(self._get(), "同日内仍受 TTL 护栏约束")

    def test_discard_log_names_the_real_reason(self):
        """管理员要能一眼看出"为什么今早多登录了一次"，不能只看到未命中。"""
        self._write_with_updated_at(self._ts(day_offset=-1))
        with self.assertLogs("yiban.store.session_cache", level="INFO") as captured:
            self.assertIsNone(self._get())
        joined = "\n".join(captured.output)
        self.assertIn("跨业务日", joined)
        self.assertNotIn("超出 TTL", joined.split("跨业务日")[0],
                         "跨日排在 TTL 之前判定，否则日志会把业务日问题报成秒数问题")

    def test_write_and_read_share_one_clock(self):
        """写入与判定必须同钟：宿主时区为 UTC 时否则 updated_at 凭空领先 8 小时永不过期。"""
        db.init_db(self.db_file, env_file=self.env_file)
        with mock.patch.object(db, "_session_cache_now", return_value=self.NOW):
            db.set_session_cache(PHONE, '{"a":"1"}', "c")
        conn = db.get_conn()
        row = conn.execute(
            "SELECT created_at, updated_at FROM session_cache WHERE phone=?", (PHONE,)
        ).fetchone()
        self.assertEqual(row["created_at"], "2026-08-31 06:31:00")
        self.assertEqual(row["updated_at"], "2026-08-31 06:31:00")
        self.assertIsNotNone(self._get(), "刚写入的缓存应可复用")


NEW_KEY = "b" * 64


ADMIN_PASS = "TestPass1234!"


USER_PASS = "secret1"


EMAIL = "user1@test.local"


class _Batch11WebBase(unittest.TestCase):
    """N1/N2/N3/N6 共用：隔离环境 + 告警/邮件 mock。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-batch11-")
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
        spec = importlib.util.spec_from_file_location("webapp", os.path.join(BASE, "web", "app.py"))
        cls.webapp = importlib.util.module_from_spec(spec)
        sys.modules["webapp"] = cls.webapp
        with contextlib.suppress(Exception):
            spec.loader.exec_module(cls.webapp)

    @classmethod
    def tearDownClass(cls):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k in ("YIBAN_ACCOUNTS_KEY", "YIBAN_LOG_FILE", "YIBAN_STATE_DIR"):
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
            json.dump([], f)
        db.init_db(self.db_file, migrate_from=self.accounts_file, env_file=self.env_file)
        # 告警/邮件全量 mock（conftest 已禁用真实发信；此处再拦截调用便于断言）
        self.alerts = []
        self.user_mails = []
        p1 = mock.patch.object(self.webapp, "send_notification",
                               # send_notification 新增 force=，假实现同步接收
                               # 新增 ledger=（M8 登录失败告警独立账本）
                               side_effect=lambda t, c, urgent=False, force=False, ledger=None: self.alerts.append((t, c)))
        p2 = mock.patch.object(self.webapp.mailer, "send_user",
                               side_effect=lambda to, s, c: self.user_mails.append((to, s)))
        p1.start()
        p2.start()
        self.addCleanup(p1.stop)
        self.addCleanup(p2.stop)

    # ---- 工具 ----
    def _login(self, c, username, password):
        r = c.post("/api/login", json={"username": username, "password": password})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return c.get("/api/me").get_json()["csrf_token"]

    def _csrf(self, token):
        return {"X-CSRF-Token": token}

    def _user_row(self, email):
        conn = db.get_conn()
        return conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()

    def _session_cookie(self, c):
        for header in (c.get("/api/me").headers.getlist("Set-Cookie") or []):
            name_val = header.split(";", 1)[0]
            name, _, val = name_val.partition("=")
            if name.strip() and val:
                return name.strip(), val
        self.fail("未找到会话 cookie")

    def _client_with_cookie(self, cookie_name, cookie_val):
        c = self.webapp.create_app().test_client()
        injected = False
        for kwargs in (
            {"key": cookie_name, "value": cookie_val, "domain": "localhost", "path": "/"},
            {"server_name": "localhost", "key": cookie_name, "value": cookie_val, "path": "/"},
        ):
            try:
                c.set_cookie(**kwargs)
                injected = True
                break
            except TypeError:
                continue
        if not injected:
            self.fail("当前 Werkzeug 版本无法注入测试 cookie")
        return c

    def _admin_client(self):
        c = self.webapp.create_app().test_client()
        return c, self._login(c, "admin", ADMIN_PASS)


class Batch11RestoreSidTest(_Batch11WebBase):
    """N1：恢复即登录签发 sid。"""

    def _register_login_delete_restore(self, email):
        """建号→登录→注销→恢复，返回 (注销前 client, 恢复后 client)。"""
        db.create_user(email, self.webapp.generate_password_hash(USER_PASS))
        c = self.webapp.create_app().test_client()
        t = self._login(c, email, USER_PASS)
        r = c.post("/api/me/delete", json={"password": USER_PASS}, headers=self._csrf(t))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        r = c.post("/api/me/restore", json={"email": email, "password": USER_PASS})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return c

    def test_restore_session_stays_valid(self):
        c = self._register_login_delete_restore("n1a@test.local")
        r = c.get("/api/me")
        self.assertEqual(r.status_code, 200, "N1 修复：恢复后新会话必须立即可用")
        sid = (self._user_row("n1a@test.local") or {})["sid"]
        self.assertTrue(sid, "恢复后库内 sid 应为恢复时签发的新值")

    def test_stolen_cookie_dead_after_restore(self):
        email = "n1b@test.local"
        db.create_user(email, self.webapp.generate_password_hash(USER_PASS))
        c = self.webapp.create_app().test_client()
        t = self._login(c, email, USER_PASS)
        name, val = self._session_cookie(c)
        r = c.post("/api/me/delete", json={"password": USER_PASS}, headers=self._csrf(t))
        self.assertEqual(r.status_code, 200)
        stolen = self._client_with_cookie(name, val)
        self.assertEqual(stolen.get("/api/me").status_code, 401, "注销后旧 cookie 先失效")
        r = self.webapp.create_app().test_client().post(
            "/api/me/restore", json={"email": email, "password": USER_PASS})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(
            stolen.get("/api/me").status_code, 401,
            "N1 修复：注销前被窃取的 cookie 恢复后必须保持失效（恢复轮换 sid）",
        )


class Batch11PurgeMasterOnlyTest(_Batch11WebBase):
    """N3：purge 收归主管理员 + N6 即时告警。"""

    def _make_victim(self, email):
        db.create_user(email, self.webapp.generate_password_hash(USER_PASS))
        c = self.webapp.create_app().test_client()
        t = self._login(c, email, USER_PASS)
        r = c.post("/api/me/delete", json={"password": USER_PASS}, headers=self._csrf(t))
        self.assertEqual(r.status_code, 200)

    def _make_registered_admin(self, email):
        db.create_user(email, self.webapp.generate_password_hash(USER_PASS))
        return email

    def test_registered_admin_forbidden(self):
        self._make_victim("victim@test.local")
        self._make_registered_admin("admin2@test.local")
        c = self.webapp.create_app().test_client()
        self._login(c, "admin2@test.local", USER_PASS)
        r = c.post("/api/users/deleted/purge", json={"emails": ["victim@test.local"]},
                   headers=self._csrf(c.get("/api/me").get_json()["csrf_token"]))
        self.assertEqual(r.status_code, 403, "N3 修复：普通管理员不可物理清除注销用户")
        self.assertIsNotNone(self._user_row("victim@test.local"), "行未被清除")

    def test_master_can_purge_with_alert(self):
        self._make_victim("victim2@test.local")
        ac, at = self._admin_client()
        r = ac.post("/api/users/deleted/purge",
                    json={"emails": ["victim2@test.local"], "confirm_password": ADMIN_PASS},
                    headers=self._csrf(at))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertIsNone(self._user_row("victim2@test.local"), "主管理员清除生效")
        self.assertTrue(any(t == "高危管理操作告警" for t, _ in self.alerts),
                        f"purge 应即时告警，实际 {self.alerts}")


class Batch11NotifyCoverageTest(_Batch11WebBase):
    """N6：受害者安全邮件 + 管理员告警补齐。"""

    def _user_with_account(self, email, phone):
        db.create_user(email, self.webapp.generate_password_hash(USER_PASS))
        c = self.webapp.create_app().test_client()
        t = self._login(c, email, USER_PASS)
        r = c.post("/api/my-accounts", json={"name": "n", "phone": phone, "password": "p"},
                   headers=self._csrf(t))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        # 审核通过（提权类用例要求"正式用户"：有生效账号且无待审核）
        ac, at = self._admin_client()
        accounts = db.load_accounts()
        idx = next(i for i, a in enumerate(accounts) if a["phone"] == phone)
        r = ac.post(f"/api/accounts/{idx}/review", json={"action": "approve"},
                    headers=self._csrf(at))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return c, t

    def test_password_change_notifies_user_and_admins(self):
        c, t = self._user_with_account(EMAIL, "13800138001")
        r = c.post("/api/me/password", json={
            "old_password": USER_PASS, "new_password": "NewPass#777",
            "confirm_password": "NewPass#777",
        }, headers=self._csrf(t))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(any(to == EMAIL for to, _ in self.user_mails),
                        "改密必须给本人发安全邮件（绕过 mail_notify 开关）")
        self.assertTrue(any(t == "账号安全事件告警" for t, _ in self.alerts),
                        f"改密应有管理员告警，实际 {self.alerts}")

    def test_mail_notify_off_sends_confirm_mail_to_owner(self):
        c, t = self._user_with_account(EMAIL, "13800138002")
        r = c.put("/api/my-mail-notify", json={"enabled": False}, headers=self._csrf(t))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(any(to == EMAIL for to, _ in self.user_mails),
                        "关闭通知必须给本人发确认邮件（不受刚关闭的开关影响）")
        self.user_mails.clear()
        r = c.put("/api/my-mail-notify", json={"enabled": True}, headers=self._csrf(t))
        self.assertEqual(r.status_code, 200)
        self.assertFalse(self.user_mails, "重新开启通知不需再发确认邮件")

    def test_account_delete_mails_owner(self):
        c, t = self._user_with_account(EMAIL, "13800138003")
        accounts = db.load_accounts()
        idx = next(i for i, a in enumerate(accounts) if a["phone"] == "13800138003")
        r = c.delete(f"/api/my-accounts/{idx}", headers=self._csrf(t))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(any(to == EMAIL for to, _ in self.user_mails),
                        "删号必须给本人发留痕邮件")

    def test_single_reset_password_alerts(self):
        self._user_with_account(EMAIL, "13800138004")
        ac, at = self._admin_client()
        r = ac.post(f"/api/users/{EMAIL}/password",
                    json={"password": "Reset#12345", "confirm_password": ADMIN_PASS},
                    headers=self._csrf(at))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(any(t == "密码重置告警" for t, _ in self.alerts),
                        f"重置密码应有告警，实际 {self.alerts}")

    def test_batch_reset_alerts_and_batch_role_removed(self):
        self._user_with_account(EMAIL, "13800138005")
        ac, at = self._admin_client()
        r = ac.post("/api/users/batch", json={
            "action": "reset_password", "emails": [EMAIL], "password": "Reset#12345",
            "confirm_password": ADMIN_PASS,
        }, headers=self._csrf(at))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(any(t == "密码重置告警" for t, _ in self.alerts),
                        f"批量重置应有告警，实际 {self.alerts}")
        self.alerts.clear()
        # 2026-09-05 用户裁决：批量角色变更入口移除（提权/降权仅保留单个路径 + 二次鉴权）
        r = ac.post("/api/users/batch", json={
            "action": "set_admin", "emails": [EMAIL],
        }, headers=self._csrf(at))
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        self.assertFalse(any(t == "权限变更告警" for t, _ in self.alerts),
                         "批量提权入口已移除，不应有告警")

    def test_role_change_alerts(self):
        self._user_with_account(EMAIL, "13800138006")
        ac, at = self._admin_client()
        # 2026-09-05：角色变更接入高危门禁，须携带当前管理员密码二次鉴权
        r = ac.post(f"/api/users/{EMAIL}/role",
                    json={"role": "admin", "confirm_password": ADMIN_PASS},
                    headers=self._csrf(at))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(any(t == "权限变更告警" for t, _ in self.alerts),
                        f"角色变更应有告警，实际 {self.alerts}")

    def test_role_change_without_reconfirm_rejected(self):
        self._user_with_account(EMAIL, "13800138007")
        ac, at = self._admin_client()
        r = ac.post(f"/api/users/{EMAIL}/role", json={"role": "admin"},
                    headers=self._csrf(at))
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        u = db.find_user(EMAIL)
        self.assertEqual(u.get("role"), "user", "未过二次鉴权，角色不得变更")

    def test_announcement_change_alerts(self):
        ac, at = self._admin_client()
        r = ac.put("/api/announcement", json={"text": "维护通知"},
                   headers=self._csrf(at))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(any(t == "公告变更告警" for t, _ in self.alerts),
                        f"公告变更应有告警，实际 {self.alerts}")

    def test_mail_config_change_alerts(self):
        ac, at = self._admin_client()
        r = ac.put("/api/mail-config", json={"enabled": True}, headers=self._csrf(at))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(any(t == "邮件配置变更告警" for t, _ in self.alerts),
                        f"邮件配置变更应有告警，实际 {self.alerts}")


class Batch11CleanupClockGuardTest(_Batch11WebBase):
    """N2：审计/事件清理接入时钟跳变守卫。"""

    def _set_guard_ref(self, key, dt):
        conn = db.get_conn()
        conn.execute("UPDATE app_meta SET value=? WHERE key=?",
                     (dt.strftime("%Y-%m-%d %H:%M:%S"), key))
        conn.commit()

    def test_audit_cleanup_skipped_on_clock_jump(self):
        for i in range(3):
            db.audit("admin", f"op{i}", "t", "d")
        conn = db.get_conn()
        with db._conn_lock:
            db._audit_cleanup(conn)  # 首次调用建立守卫参照
        n_before = conn.execute("SELECT COUNT(*) FROM audit_logs").fetchone()[0]
        self.assertGreaterEqual(n_before, 3)
        # 参照拨回 8 天前 → 下次调用视为前进 8 天（>72h）→ 跳过清理
        with db._conn_lock:
            self._set_guard_ref("audit_cleanup_clock", _datetime_RESTORE.now() - timedelta(days=8))
            db._audit_cleanup(conn)
        n_after = conn.execute("SELECT COUNT(*) FROM audit_logs").fetchone()[0]
        self.assertEqual(n_after, n_before, "时钟跳变时审计清理必须被跳过")

    def test_event_cleanup_skipped_on_clock_jump(self):
        db.add_sign_event("2026-08-29 10:00:00", "13800138000", "success", "m")
        conn = db.get_conn()
        with db._conn_lock:
            db._event_cleanup(conn)  # 首次调用建立守卫参照
        n_before = conn.execute("SELECT COUNT(*) FROM sign_events").fetchone()[0]
        self.assertGreaterEqual(n_before, 1)
        with db._conn_lock:
            self._set_guard_ref("event_cleanup_clock", _datetime_RESTORE.now() - timedelta(days=8))
            db._event_cleanup(conn)
        n_after = conn.execute("SELECT COUNT(*) FROM sign_events").fetchone()[0]
        self.assertEqual(n_after, n_before, "时钟跳变时事件清理必须被跳过")


class Batch11RekeyToolTest(_Batch11WebBase):
    """N5：ACCOUNTS_KEY 轮换工具。"""

    def _seed_encrypted_account(self, phone, password):
        db.create_user(EMAIL, self.webapp.generate_password_hash(USER_PASS))
        c = self.webapp.create_app().test_client()
        t = self._login(c, EMAIL, USER_PASS)
        r = c.post("/api/my-accounts", json={
            "name": "n", "phone": phone, "password": password, "phone_code": "code-x",
        }, headers=self._csrf(t))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None

    def _read_secret(self, conn, phone, col):
        raw = conn.execute(f"SELECT {col} FROM accounts WHERE phone=?", (phone,)).fetchone()[0]
        return json.loads(raw)

    def test_rekey_roundtrip(self):
        self._seed_encrypted_account("13900000001", "plain-pw-1")
        import rekey_accounts

        from yiban.infra import account_crypto
        ok, note = rekey_accounts.rekey(self.db_file, bytes.fromhex(TEST_KEY), bytes.fromhex(NEW_KEY))
        self.assertTrue(ok, note)
        conn = sqlite3_connect(self.db_file)
        try:
            new_obj = self._read_secret(conn, "13900000001", "password")
            self.assertEqual(
                account_crypto.decrypt_password(new_obj, bytes.fromhex(NEW_KEY), "13900000001"),
                "plain-pw-1", "新钥必须能解密且明文一致",
            )
            with self.assertRaises(ValueError):
                account_crypto.decrypt_password(new_obj, bytes.fromhex(TEST_KEY), "13900000001")
            code_obj = self._read_secret(conn, "13900000001", "phone_code")
            self.assertEqual(
                account_crypto.decrypt_password(code_obj, bytes.fromhex(NEW_KEY), "13900000001"),
                "code-x",
            )
        finally:
            conn.close()

    def test_rekey_rejects_wrong_old_key(self):
        self._seed_encrypted_account("13900000002", "plain-pw-2")
        import rekey_accounts
        conn = sqlite3_connect(self.db_file)
        before = conn.execute("SELECT password FROM accounts WHERE phone=?",
                              ("13900000002",)).fetchone()[0]
        conn.close()
        ok, _note = rekey_accounts.rekey(self.db_file, bytes.fromhex(NEW_KEY), bytes.fromhex("c" * 64))
        self.assertFalse(ok, "旧钥不对必须拒绝")
        conn = sqlite3_connect(self.db_file)
        after = conn.execute("SELECT password FROM accounts WHERE phone=?",
                             ("13900000002",)).fetchone()[0]
        conn.close()
        self.assertEqual(before, after, "拒绝时库必须保持原状")

    def test_update_env_key_writes_and_rotates(self):
        import rekey_accounts
        rekey_accounts.update_env_key(self.env_file, bytes.fromhex(NEW_KEY))
        content = open(self.env_file, encoding="utf-8-sig").read()
        self.assertIn(f"YIBAN_ACCOUNTS_KEY={NEW_KEY}", content)
        self.assertEqual(content.count("YIBAN_ACCOUNTS_KEY="), 1, "旧键行应被替换而非叠加")


def sqlite3_connect(path):
    import sqlite3
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


PROD_STALE_MSG = "获取签到任务失败: 未登录或登录已经超时"


class SessionStaleBudgetTest(unittest.TestCase):
    """R1：会话陈旧类失败必须收敛到 2 次并清除会话缓存。"""

    def test_prod_stale_message_gets_two_attempts_and_cache_clear(self):
        self.assertEqual(signin._retry_budget(PROD_STALE_MSG), (2, True))

    def test_stale_is_not_classified_as_risk(self):
        # 钉住"收敛不来自风控表"：若有人图省事把关键词塞进 RISK_FAIL_KEYWORDS，
        # 该消息会被 _is_credential_failure 的邻居逻辑误累计成账密熔断天数。
        self.assertEqual(signin.classify_failure(PROD_STALE_MSG), signin.MAX_ATTEMPTS)
        self.assertFalse(signin._is_credential_failure(PROD_STALE_MSG),
                         "会话陈旧不是凭据问题，不得参与账密熔断累计")

    def test_risk_and_device_binding_still_clear_cache(self):
        # 2026-09-05 起确定性认证失败（账号或密码错误）收敛为终态 1 次不重试
        # （详见 SigninRetryBudgetTest（本文件）），此处钉住
        # WAF 风控与"授权设备"两类仍保留清缓存与既有预算
        self.assertEqual(signin._retry_budget("请求被 WAF 风控拦截"), (2, True))
        self.assertEqual(signin._retry_budget("签到失败: 请使用授权设备进行签到"),
                         (signin.MAX_ATTEMPTS, True))

    def test_network_failure_keeps_full_budget_without_clearing(self):
        # 真瞬时故障保持 3 次上限且不动缓存（缓存本身没问题，清了反而多打一次登录）
        self.assertEqual(signin._retry_budget("HTTPSConnectionPool 读超时"),
                         (signin.MAX_ATTEMPTS, False))

    def test_network_budget_is_three_not_four(self):
        # 2026-08-31：窗口为全体账号共享、每次重试间隔≥60s，重试越多越拖长队列
        # （实证：把首轮拖过 07:10 兜底）。网络类上限由 4 收敛为 3。
        self.assertEqual(signin.MAX_ATTEMPTS, 3)

    def test_no_position_fails_fast_without_clearing_cache(self):
        # 易班侧无点位是数据问题：1 次即止；会话本身没问题，不得清缓存
        msg = "未找到签到位置数据（易班未返回该账号的签到点位，非账号密码问题）"
        self.assertEqual(signin._retry_budget(msg),
                         (signin.NO_POSITION_MAX_ATTEMPTS, False))
        self.assertEqual(signin.NO_POSITION_MAX_ATTEMPTS, 1)

    def test_success_message_is_not_stale(self):
        self.assertFalse(signin._is_session_stale_failure("今日已签到（无需重复签到）"))
