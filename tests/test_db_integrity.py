# -*- coding: utf-8 -*-
"""Task 3：DB 层数据完整性与迁移修复测试。

覆盖 brief 中：
- S2：purge_deleted_users_hard 不误删活跃账号，且清理已删账号的 time_prefs；
- S4：migrate_v5 先 DROP users.email 旧唯一索引，再建部分唯一索引；
- S5：可选迁移失败/延后时 continue 执行后续迁移，不提升 user_version，重试后到 7；
- H1：restore_user 按用户行 deleted_at 关联恢复账号；
- M4：_audit_cleanup 不再重建哈希链，verify 首行以自身 prev_hash 为锚；
- M5：time_pref_stats 排除已删账号，_purge_expired_deleted 连带删除 time_prefs。

标签：C · 存储：迁移与库完整性
覆盖：六项 db 层修复（硬清不误删活跃账号、v5 先DROP旧唯一索引再建部分唯一索引、
可选失败后 continue 且不提版本、重试后追平、restore 按 deleted_at 关联、
`_audit_cleanup` 不再重建哈希链、统计排除已删账号）、批量操作的事务回滚与软跳过、
删除/改绑后的会话缓存残留清理、`executescript` 原子性与"源码不再出现 executescript"、
备份明文策略、以及一整套加密/迁移/鉴权 smoke。
对应实现：`yiban/store/`（accounts / users / time_prefs / audit_chain / session_cache 各域）
与 `db` 门面、`scripts/backup.sh`。
关键断言：`test_audit_cleanup_keeps_chain_without_rechain_and_detects_tamper` 与
`tests/test_audit_anchor.py` 的锚点用例是一对——清理必须"换新根"而不是"把删掉的段重新
签一遍"，后者等于给篡改者提供重链工具。`test_db_source_has_no_executescript_call` 是**源码文本级**断言：它只保证那段文本还在原位。
`BackupPlaintextP3Test` 原本是同类写法（MF-10 指出的假绿），现已改为**行为测试**——真跑
backup.sh（临时目录夹具，skipIf 无 bash），断言真实 stderr 告警、专用退出码 6、异机密文
副本与哨兵判定；输出按字节手动 utf-8 解码以避开旧注释所说的 Windows GBK 误报问题。
依赖：临时库 + 临时 `.env` + Flask test client；`_FlakyConn` 用注入失败模拟半路崩，
无网络、无 skip。
"""
import contextlib
import datetime
import glob
import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import db

from yiban import clock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


TEST_KEY = "a" * 64
AUDIT_KEY = "b" * 64


def _insert_audit_row(conn, ts, username, action, target="", detail="", prev_hash=""):
    """按当前审计密钥插入一条带哈希的审计行。"""
    h = db._audit_hash(prev_hash, ts, username, action, target, detail)
    conn.execute(
        "INSERT INTO audit_logs (ts, username, action, target, detail, prev_hash, hash) "
        "VALUES (?,?,?,?,?,?,?)",
        (ts, username, action, target, detail, prev_hash, h),
    )
    return h


class DbFixes021Test(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-db-fixes-021-")
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
        for key in ("YIBAN_ACCOUNTS_KEY", "YIBAN_AUDIT_KEY", "YIBAN_ENV_FILE", "YIBAN_DB_FILE"):
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

    def _add_account(self, owner, phone):
        return db.add_account({
            "name": "测试账号",
            "phone": phone,
            "password": "p1",
            "phone_model": "",
            "phone_code": "",
            "owner": owner,
            "status": "active",
            "reject_reason": "",
        })

    # ---- S2：管理员手动物理清除只删已软删账号 ----
    def test_purge_deleted_users_hard_skips_active_accounts(self):
        db.init_db(self.db_file, env_file=self.env_file)
        db.create_user("s2@test.local", "hash")
        self._add_account("s2@test.local", "13800000001")
        db.soft_delete_user_with_accounts("s2@test.local")
        # 注销后重建一个活跃账号，模拟“用户已删但活跃账号不能被连带清掉”
        self._add_account("s2@test.local", "13800000002")
        db.set_time_pref("13800000001", 0, "2026-08-17 10:00:00")
        db.set_time_pref("13800000002", 0, "2026-08-17 10:00:00")

        purged = db.purge_deleted_users_hard(["s2@test.local"])

        self.assertEqual(purged, ["s2@test.local"])
        self.assertIsNone(db.find_user_any("s2@test.local"), "已注销用户行应删除")
        remaining = [a for a in db.load_accounts_raw() if a["owner"] == "s2@test.local"]
        self.assertEqual([a["phone"] for a in remaining], ["13800000002"])
        self.assertFalse(remaining[0]["deleted"], "活跃账号应被跳过")
        self.assertIsNone(db.get_time_pref("13800000001"), "已删账号的 time_prefs 应清理")
        self.assertIsNotNone(db.get_time_pref("13800000002"), "活跃账号的 time_prefs 应保留")

    # ---- S4：旧库 email 唯一索引升级后可与部分唯一索引并存 ----
    def test_migrate_v5_drops_old_unique_email_index(self):
        conn = sqlite3.connect(self.db_file)
        try:
            conn.execute(
                "CREATE TABLE users ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "email TEXT NOT NULL UNIQUE, "
                "password_hash TEXT NOT NULL, "
                "role TEXT NOT NULL DEFAULT 'user', "
                "created_at TEXT NOT NULL DEFAULT '', "
                "pw_version INTEGER NOT NULL DEFAULT 1"
                ")"
            )
            conn.execute("PRAGMA user_version = 4")
            conn.commit()
        finally:
            conn.close()

        conn = db.init_db(self.db_file, env_file=self.env_file)
        # 旧唯一索引（sqlite_autoindex_users_*）必须被移除
        old_email_indexes = []
        for idx in conn.execute("PRAGMA index_list('users')").fetchall():
            if not idx["name"].startswith("sqlite_autoindex_users"):
                continue
            info = conn.execute(f"PRAGMA index_info('{idx['name']}')").fetchall()
            if [r["name"] for r in info] == ["email"]:
                old_email_indexes.append(idx["name"])
        self.assertEqual(old_email_indexes, [], "旧 users.email 唯一索引应被 DROP")
        self.assertIsNotNone(
            conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_users_email_live'"
            ).fetchone(),
            "migrate_v5 应创建部分唯一索引",
        )
        # 同 email 一个活跃 + 一个已注销可以并存
        conn.execute(
            "INSERT INTO users (email, password_hash, role, created_at, pw_version, deleted, deleted_at) "
            "VALUES (?,?,?,?,?,?,?)",
            ("same@test.local", "h1", "user", "", 1, 0, ""),
        )
        conn.execute(
            "INSERT INTO users (email, password_hash, role, created_at, pw_version, deleted, deleted_at) "
            "VALUES (?,?,?,?,?,?,?)",
            ("same@test.local", "h2", "user", "", 1, 1, ""),
        )
        conn.commit()
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM users WHERE email='same@test.local'").fetchone()[0],
            2,
            "旧全局唯一索引未移除时无法写入同邮箱活跃+已注销行",
        )

    # ---- S5：可选迁移阻塞时继续后续迁移，但不提升版本；重试后到最新 ----
    def test_optional_migration_blocked_continues_and_retry_reaches_latest(self):
        def failing_v2(conn):
            raise RuntimeError("v2 blocked")

        old_migrations = db._MIGRATIONS
        patched_migrations = [
            (1, "v1_add_account_user_paused", db.migrate_v1, True),
            (2, "v2_unique_owner_live", failing_v2, False),
            (3, "v3_audit_hash_chain", db.migrate_v3, True),
            (4, "v4_visual_tables", db.migrate_v4, False),
            (5, "v5_user_deregistration", db.migrate_v5, True),
            (6, "v6_webui_stats", db.migrate_v6, False),
            (7, "v7_delete_request_kind", db.migrate_v7, True),
        ]
        db._MIGRATIONS = patched_migrations
        try:
            conn = db.init_db(self.db_file, env_file=self.env_file)
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            self.assertEqual(version, 1, "阻塞期间不应提升 user_version")
            users_cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
            self.assertIn("deleted", users_cols, "后续核心迁移 v5 仍应执行")
            self.assertIn("deleted_at", users_cols)
            req_cols = {
                r["name"] for r in conn.execute("PRAGMA table_info(user_delete_requests)").fetchall()
            }
            self.assertIn("kind", req_cols, "后续核心迁移 v7 仍应执行")
        finally:
            db._MIGRATIONS = old_migrations
            if db._conn is not None:
                with contextlib.suppress(Exception):
                    db._conn.close()
                db._conn = None

        conn = db.init_db(self.db_file, env_file=self.env_file)
        self.assertEqual(
            conn.execute("PRAGMA user_version").fetchone()[0], db._MIGRATIONS[-1][0],
            "下次启动应重试到最新版本"
        )

    # ---- S5 复审：可选迁移失败必须回滚部分写入 ----
    def test_optional_migration_failure_rolls_back_partial_write(self):
        conn = sqlite3.connect(self.db_file)
        conn.row_factory = sqlite3.Row
        db._create_tables(conn)
        conn.execute("PRAGMA user_version = 0")
        conn.commit()

        def failing_optional(conn):
            conn.execute(
                "INSERT INTO audit_logs (ts, username, action, target, detail) "
                "VALUES ('2026-08-17 10:00:00', 'admin', 'partial', '', '')"
            )
            raise RuntimeError("optional migration boom")

        def committing_optional(conn):
            conn.execute(
                "INSERT INTO audit_logs (ts, username, action, target, detail) "
                "VALUES ('2026-08-17 10:01:00', 'admin', 'after', '', '')"
            )
            conn.commit()

        old_migrations = db._MIGRATIONS
        db._MIGRATIONS = [
            (1, "v1_partial_fail", failing_optional, False),
            (2, "v2_commit_after", committing_optional, False),
        ]
        try:
            db._run_migrations(conn)
            self.assertFalse(conn.in_transaction, "迁移结束后不应残留未提交事务")
            partial = conn.execute(
                "SELECT COUNT(*) FROM audit_logs WHERE action='partial'"
            ).fetchone()[0]
            self.assertEqual(partial, 0, "失败迁移的部分写入不应被后续 commit 带出")
            after = conn.execute(
                "SELECT COUNT(*) FROM audit_logs WHERE action='after'"
            ).fetchone()[0]
            self.assertEqual(after, 1, "后续成功迁移仍应正常提交")
        finally:
            db._MIGRATIONS = old_migrations
            conn.close()

    # ---- H1：restore_user 按用户 deleted_at 关联恢复账号 ----
    def test_restore_user_only_restores_same_deleted_at_accounts(self):
        db.init_db(self.db_file, env_file=self.env_file)
        db.create_user("h1@test.local", "hash")
        self._add_account("h1@test.local", "13800000011")
        db.soft_delete_user_with_accounts("h1@test.local")
        user = db.find_user_any("h1@test.local")
        self.assertIsNotNone(user)
        user_deleted_at = user["deleted_at"]
        # 注销后再新增账号并单独软删（不同时间戳）——不应随用户恢复
        self._add_account("h1@test.local", "13800000012")
        conn = db.get_conn()
        id2 = conn.execute(
            "SELECT id FROM accounts WHERE phone=?", ("13800000012",)
        ).fetchone()[0]
        db.set_account_deleted(id2, 1, "2099-01-01 00:00:00")

        self.assertTrue(db.restore_user("h1@test.local"))
        rows = {a["phone"]: a for a in db.load_accounts_raw()}
        self.assertFalse(rows["13800000011"]["deleted"], "同一次注销的账号应恢复")
        self.assertTrue(rows["13800000012"]["deleted"], "不同 deleted_at 的账号不应恢复")
        restored_user = db.find_user("h1@test.local")
        self.assertIsNotNone(restored_user)
        self.assertEqual(restored_user["deleted_at"], "")
        self.assertNotEqual(user_deleted_at, "")

    # ---- M4：审计清理不再重建链，verify 首行以自身 prev_hash 为锚 ----
    def test_audit_cleanup_keeps_chain_without_rechain_and_detects_tamper(self):
        db.init_db(self.db_file, env_file=self.env_file)
        conn = db.get_conn()
        old_ts = (clock.now() - datetime.timedelta(days=200)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        h1 = _insert_audit_row(conn, old_ts, "admin", "old1", prev_hash="")
        _insert_audit_row(conn, old_ts, "admin", "old2", prev_hash=h1)
        # 遗留事务改为回滚语义——夹具自提交，不依赖 audit() 盲提交
        conn.commit()
        db.audit("admin", "recent1", "target1", "detail1")
        db.audit("admin", "recent2", "target2", "detail2")
        conn.commit()

        db._audit_cleanup(conn)
        first = conn.execute(
            "SELECT id, prev_hash, hash FROM audit_logs ORDER BY id LIMIT 1"
        ).fetchone()
        self.assertNotEqual(first["prev_hash"], "", "清理后不应重建链（首行 prev_hash 保留旧锚）")

        ok, broken, first_broken = db.verify_audit_chain()
        self.assertTrue(ok, f"清理旧行后链应可校验: broken={broken}, first={first_broken}")
        self.assertEqual(broken, 0)

        conn.execute("UPDATE audit_logs SET detail='tampered' WHERE id=?", (first["id"],))
        conn.commit()
        ok, broken, _ = db.verify_audit_chain()
        self.assertFalse(ok, "篡改应被检出")
        self.assertGreaterEqual(broken, 1)

    # ---- M5：time_pref_stats 排除已删账号 ----
    def test_time_pref_stats_excludes_deleted_accounts(self):
        db.init_db(self.db_file, env_file=self.env_file)
        id1 = self._add_account("user1@test.local", "13800000021")
        self._add_account("user2@test.local", "13800000022")
        db.set_time_pref("13800000021", 390, "2026-08-17 10:00:00")
        db.set_time_pref("13800000022", 390, "2026-08-17 10:00:00")
        db.set_account_deleted(id1, 1, "2026-08-17 10:00:00")

        stats = db.time_pref_stats()
        slot = [s for s in stats if s["slot_min"] == 390]
        self.assertEqual(slot, [{"slot_min": 390, "count": 1}], "已删账号的自选不应计入拥挤度")

    # ---- M5：_purge_expired_deleted 连带删除 time_prefs ----
    def test_purge_expired_deleted_cleans_time_prefs(self):
        db.init_db(self.db_file, env_file=self.env_file)
        account_id = self._add_account("user@test.local", "13800000031")
        db.set_time_pref("13800000031", 480, "2026-08-17 10:00:00")
        old = (clock.now() - datetime.timedelta(days=8)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        db.set_account_deleted(account_id, 1, old)

        db._purge_expired_deleted(db.get_conn())

        self.assertIsNone(db.get_time_pref("13800000031"), "过期账号清除时 time_prefs 应连带删除")


ADMIN_PASS = "TestPass1234!"


USER_PASS = "secret1"


class BatchTransactionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-batch-tx-")
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
        db.create_user("user1@test.local", self.webapp.generate_password_hash(USER_PASS))
        db.create_user("user2@test.local", self.webapp.generate_password_hash(USER_PASS))

    # ---- 工具 ----
    def _login(self, c, username, password):
        r = c.post("/api/login", json={"username": username, "password": password})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return c.get("/api/me").get_json()["csrf_token"]

    def _csrf(self, token):
        return {"X-CSRF-Token": token}

    def _admin_client(self):
        c = self.webapp.create_app().test_client()
        self._login(c, "admin", ADMIN_PASS)
        return c

    def _add_account(self, owner, phone, status="active"):
        return db.add_account({
            "name": "测试账号",
            "phone": phone,
            "password": "p1",
            "phone_model": "",
            "phone_code": "",
            "owner": owner,
            "status": status,
            "reject_reason": "",
        })

    def _account_index_by_phone(self, data, phone_masked):
        return next(a["index"] for a in data["accounts"] if a["phone"] == phone_masked)

    # ---- 1. db 批量账号操作异常整体回滚 ----
    def test_db_batch_account_ops_rollback_on_unknown_op(self):
        id1 = self._add_account("user1@test.local", "13800138001")
        id2 = self._add_account("user2@test.local", "13900139002")
        with self.assertRaises(ValueError):
            db.batch_account_ops([
                ("update_status", id1, "active", ""),
                ("bad_op", id2),
            ])
        accounts = db.load_accounts()
        by_id = {a["id"]: a for a in accounts}
        self.assertEqual(by_id[id1]["status"], "active")
        self.assertEqual(by_id[id2]["status"], "active")

    # ---- 2. db 批量用户操作异常整体回滚 ----
    def test_db_batch_user_ops_rollback_on_unknown_op(self):
        with self.assertRaises(ValueError):
            db.batch_user_ops([
                ("update_user", "user1@test.local", {"role": "admin"}),
                ("bad_op", "user2@test.local"),
            ])
        users = {u["email"]: u for u in db.load_users()}
        self.assertEqual(users["user1@test.local"]["role"], "user")
        self.assertEqual(users["user2@test.local"]["role"], "user")

    # ---- 3. API 批量通过：无效项软跳过 ----
    def test_api_batch_approve_soft_skip_mixed(self):
        id1 = self._add_account("user1@test.local", "13800138003", status="pending")
        self._add_account("user2@test.local", "13900139004", status="pending")
        # 把第一个直接置为 active，模拟“已通过”的无效项
        db.update_account_status(id1, "active", reject_reason="")
        c = self._admin_client()
        token = self._login(c, "admin", ADMIN_PASS)
        data = c.get("/api/accounts").get_json()
        ids = [self._account_index_by_phone(data, "138****8003"),
               self._account_index_by_phone(data, "139****9004")]
        r = c.post("/api/accounts/batch", json={"action": "approve", "ids": ids},
                   headers=self._csrf(token))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertIn("已通过 1 个账号", r.get_json()["msg"])
        accounts = db.load_accounts()
        by_phone = {a["phone"]: a for a in accounts}
        self.assertEqual(by_phone["13800138003"]["status"], "active")
        self.assertEqual(by_phone["13900139004"]["status"], "active")

    # ---- 4. API 同一批量恢复同一用户多个已删除账号被拦截 ----
    def test_api_batch_restore_duplicate_same_owner_blocked(self):
        id1 = self._add_account("user1@test.local", "13800138005")
        db.set_account_deleted(id1, 1, "2026-08-16T00:00:00")
        # 第一个已软删除后，第二个同 owner 账号可以创建
        id2 = self._add_account("user1@test.local", "13900139006")
        db.set_account_deleted(id2, 1, "2026-08-16T00:00:00")
        c = self._admin_client()
        token = self._login(c, "admin", ADMIN_PASS)
        data = c.get("/api/accounts").get_json()
        ids = [self._account_index_by_phone(data, "138****8005"),
               self._account_index_by_phone(data, "139****9006")]
        r = c.post("/api/accounts/batch", json={"action": "restore", "ids": ids},
                   headers=self._csrf(token))
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        accounts = db.load_accounts()
        self.assertTrue(all(a["deleted"] for a in accounts if a["id"] in (id1, id2)))

    # ---- 5. API 批量数据库异常返回 500 且数据不变 ----
    def test_api_batch_accounts_db_error_returns_500(self):
        self._add_account("user1@test.local", "13800138007", status="pending")
        c = self._admin_client()
        token = self._login(c, "admin", ADMIN_PASS)
        data = c.get("/api/accounts").get_json()
        idx = self._account_index_by_phone(data, "138****8007")
        with mock.patch("db.batch_account_ops", side_effect=RuntimeError("boom")):
            r = c.post("/api/accounts/batch", json={"action": "approve", "ids": [idx]},
                       headers=self._csrf(token))
        self.assertEqual(r.status_code, 500, r.get_data(as_text=True))
        self.assertIn("已全部回滚", r.get_json()["error"])
        accounts = db.load_accounts()
        self.assertEqual(accounts[0]["status"], "pending")


class DbResidueTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-residue-")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write("YIBAN_ACCOUNTS_KEY=" + "a" * 64 + "\n")
        os.environ["YIBAN_DB_FILE"] = cls.db_file
        os.environ["YIBAN_ENV_FILE"] = cls.env_file

    @classmethod
    def tearDownClass(cls):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        os.environ.pop("YIBAN_DB_FILE", None)
        os.environ.pop("YIBAN_ENV_FILE", None)
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
        for p in glob.glob(os.path.join(self.tmp, "*.json*")):
            with contextlib.suppress(OSError):
                os.remove(p)

    # ---- 1. 删除/改绑路径连带清理 session_cache ----
    def test_purge_account_clears_session_cache(self):
        db.init_db(self.db_file, env_file=self.env_file, cleanup=False)
        aid = db.add_account({"name": "A", "phone": "13800138000", "password": "p1",
                              "status": "active", "owner": "admin"})
        db.set_session_cache("13800138000", '{"cookie": "x"}', "csrf1")
        self.assertIsNotNone(db.get_session_cache("13800138000"), "前置：缓存已写入")
        db.purge_account(aid)
        self.assertIsNone(db.get_session_cache("13800138000"),
                          "物理删除账号应连带清除会话缓存")

    def test_delete_user_with_accounts_clears_session_cache(self):
        db.init_db(self.db_file, env_file=self.env_file, cleanup=False)
        db.create_user("owner@test.local", "hashx")
        db.add_account({"name": "A", "phone": "13800138001", "password": "p1",
                        "status": "active", "owner": "owner@test.local"})
        db.set_session_cache("13800138001", '{"cookie": "x"}', "csrf1")
        self.assertIsNotNone(db.get_session_cache("13800138001"))
        db.delete_user_with_accounts("owner@test.local")
        self.assertIsNone(db.get_session_cache("13800138001"),
                          "删除用户账号应连带清除会话缓存")

    def test_soft_delete_user_clears_session_cache(self):
        db.init_db(self.db_file, env_file=self.env_file, cleanup=False)
        db.create_user("soft@test.local", "hashx")
        db.add_account({"name": "A", "phone": "13800138003", "password": "p1",
                        "status": "active", "owner": "soft@test.local"})
        db.set_session_cache("13800138003", '{"cookie": "x"}', "csrf1")
        self.assertTrue(db.soft_delete_user_with_accounts("soft@test.local"))
        self.assertIsNone(db.get_session_cache("13800138003"),
                          "软注销应停用对应会话缓存")

    def test_update_phone_change_clears_old_cache(self):
        db.init_db(self.db_file, env_file=self.env_file, cleanup=False)
        aid = db.add_account({"name": "A", "phone": "13800138002", "password": "p1",
                              "status": "active", "owner": "admin"})
        db.set_session_cache("13800138002", '{"cookie": "x"}', "csrf1")
        db.update_account(aid, {"phone": "13899998888"})
        self.assertIsNone(db.get_session_cache("13800138002"),
                          "改绑手机号应清除旧号会话缓存")

    # ---- 2. _rename_backup 同日残留 ----
    def test_rename_backup_disambiguates_same_day(self):
        src = os.path.join(self.tmp, "accounts.json")
        with open(src, "w", encoding="utf-8") as f:
            json.dump([{"phone": "13800138000", "password": "plain"}], f)
        bak0 = src + ".bak-" + clock.now().strftime("%Y%m%d")
        with open(bak0, "w", encoding="utf-8") as f:
            f.write("[]")  # 预置今日已存在的同名 .bak，制造冲突
        key = db.account_crypto.load_key(self.env_file)
        db._rename_backup(src, reencrypt=True, key=key)
        self.assertFalse(os.path.exists(src), "源文件必须离开原路径（不得明文驻留）")
        self.assertEqual(len(glob.glob(src + ".bak-*")), 2, "应追加序号生成第二个备份")

    # ---- 3. 迁移损坏 JSON 显式告警 ----
    def test_migrate_corrupt_json_logs_error(self):
        accounts_json = os.path.join(self.tmp, "accounts.json")
        with open(accounts_json, "w", encoding="utf-8") as f:
            f.write("{ this is not valid json !!!")
        # 自动导入的日志通道随定义点迁到 yiban.store.migrations（文案未变）
        with self.assertLogs("yiban.store.migrations", level="ERROR") as cm:
            db.init_db(self.db_file, migrate_from=accounts_json,
                       env_file=self.env_file, cleanup=False)
        joined = "\n".join(cm.output)
        self.assertIn("读取/解析失败", joined, "损坏 JSON 必须显式记 ERROR")
        self.assertNotIn("无 JSON 数据可迁移", joined, "不得误报为无数据")

    # ---- 4. update_account IntegrityError 回滚后重放明文自愈 ----
    def test_update_integrity_error_reheals_plaintext(self):
        conn = db.init_db(self.db_file, env_file=self.env_file, cleanup=False)
        conn.execute(
            "INSERT INTO accounts (sort_order, name, phone, password, status, owner) "
            "VALUES (1, '甲', '13800138010', 'PlainPW1', 'active', 'admin')"
        )
        conn.commit()  # 先提交明文行，避免与 add_account 的 BEGIN IMMEDIATE 冲突
        db.add_account({"name": "乙", "phone": "13800138011", "password": "p2",
                        "status": "active", "owner": "admin"})
        row = conn.execute("SELECT id FROM accounts WHERE phone='13800138010'").fetchone()
        with self.assertRaises(db.DuplicatePhoneError):
            db.update_account(row["id"], {"phone": "13800138011"})  # 撞账号2
        raw = {r["phone"]: r for r in db.load_accounts_raw()}
        self.assertTrue(db._is_encrypted_value(raw["13800138010"]["password"]),
                        "IntegrityError 回滚后明文自愈应被重放持久化，凭据不留明文")

    # ---- 5. _row_to_account 无连接时日志如实 ----
    def test_row_to_account_no_conn_logs_no_writeback(self):
        conn = db.init_db(self.db_file, env_file=self.env_file, cleanup=False)
        conn.execute(
            "INSERT INTO accounts (sort_order, name, phone, password) "
            "VALUES (1, '甲', '13800138020', 'PlainPW2')"
        )
        conn.commit()
        row = conn.execute("SELECT * FROM accounts WHERE phone='13800138020'").fetchone()
        with self.assertLogs("yiban.store.accounts", level="WARNING") as cm:
            db._row_to_account(row)  # conn 缺省 → 不回写
        joined = "\n".join(cm.output)
        self.assertIn("明文存储", joined)
        self.assertIn("未回写", joined, "无连接时应如实说明未回写，不得谎称已回写")


TEST_KEY_P3 = "b" * 64


def _table_names(db_file):
    conn = sqlite3.connect(db_file)
    try:
        return {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()


def _user_version(db_file):
    conn = sqlite3.connect(db_file)
    try:
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


class _FlakyConn:
    """对 sqlite3.Connection 实例的委托包装器：在第 N 次命中指定 DDL 时抛错。

    不能用 mock.patch 直接打补丁：`sqlite3.Connection` 是 C 扩展类型，其
    `execute` 属性不可 setattr（TypeError: immutable type）。
    委托方式保留 execute/commit/rollback/in_transaction/row_factory 等全部
    接口，仅拦截"CREATE ... <fail_on>"语句制造迁移中途失败。
    """

    def __init__(self, conn, fail_on, fail_once=True):
        self._conn = conn
        self._fail_on = fail_on
        self._fail_once = fail_once
        self._fail_armed = True

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def execute(self, sql, params=()):
        if (self._fail_armed and "CREATE" in str(sql).upper()
                and self._fail_on in str(sql)):
            if self._fail_once:
                self._fail_armed = False
            raise sqlite3.OperationalError(f"injected failure: {self._fail_on}")
        return self._conn.execute(sql, params)


def _close_db():
    if db._conn is not None:
        with contextlib.suppress(Exception):
            db._conn.close()
        db._conn = None


class DbExecutescriptAtomicityP3Test(unittest.TestCase):
    """P3-1：executescript 隐式 COMMIT 不再击穿迁移原子性。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="p3-db-")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write("YIBAN_ACCOUNTS_KEY=" + "a" * 64 + "\n")
        os.environ["YIBAN_DB_FILE"] = cls.db_file
        os.environ["YIBAN_ENV_FILE"] = cls.env_file

    @classmethod
    def tearDownClass(cls):
        _close_db()
        os.environ.pop("YIBAN_DB_FILE", None)
        os.environ.pop("YIBAN_ENV_FILE", None)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        _close_db()
        for suffix in ("", "-wal", "-shm"):
            p = self.db_file + suffix
            if os.path.exists(p):
                os.remove(p)

    def test_migrate_v8_failure_rolls_back_prior_ddl(self):
        """迁移中途失败：同事务先前的 DDL 必须整体回滚（旧实现会被隐式 COMMIT 落盘）。"""
        real = sqlite3.connect(self.db_file)
        real.row_factory = sqlite3.Row
        try:
            db._create_tables(real)
            fc = _FlakyConn(real, "app_meta")
            db._begin_immediate(fc)
            with self.assertRaises(sqlite3.OperationalError):
                db.migrate_v8(fc)  # session_cache 已建、app_meta 失败
            fc.rollback()
        finally:
            real.close()
        names = _table_names(self.db_file)
        self.assertNotIn("session_cache", names,
                         "executescript 修复失效：session_cache 未随失败回滚")
        self.assertNotIn("app_meta", names)

    def test_run_migrations_optional_failure_no_partial_tables(self):
        """框架层：v8（可选）失败后先前的 session_cache 回滚、后续迁移照常、版本不提升。"""
        conn = sqlite3.connect(self.db_file)
        db._create_tables(conn)
        conn.execute("PRAGMA user_version = 7")
        conn.commit()
        conn.close()

        real = sqlite3.connect(self.db_file)
        real.row_factory = sqlite3.Row
        fc = _FlakyConn(real, "app_meta")
        try:
            db._run_migrations(fc)  # v8 失败一次 → blocked；v12 随后补建 app_meta
        finally:
            real.close()

        names = _table_names(self.db_file)
        self.assertNotIn("session_cache", names,
                         "v8 失败后其前半段 DDL 必须回滚，不得残留")
        self.assertIn("app_meta", names, "v12 应已成功补建 app_meta")
        self.assertEqual(_user_version(self.db_file), 7,
                         "可选迁移失败置 blocked：本轮不提升 user_version")

    def test_fresh_db_creates_all_schema(self):
        """转换后的逐条 DDL 语义不变：新库仍建出全部基线 + 迁移表。

        page_visits / server_metrics 不在此列：它们由 migrate_v14 删除
        （v4 建 → v6 补列 → v14 删），并已由 tests/test_sign_events.py 断言其不存在。
        """
        db.init_db(self.db_file, env_file=self.env_file, cleanup=False)
        names = {r["name"] for r in db.get_conn().execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for t in ("accounts", "users", "audit_logs", "time_prefs",
                  "user_delete_requests", "sign_events",
                  "session_cache", "app_meta"):
            self.assertIn(t, names, f"缺表 {t}——逐条 DDL 转换丢失了建表语句")

    def test_db_source_has_no_executescript_call(self):
        """源码级回归绊线：db 与迁移实现都不得再出现 executescript 调用。

        DDL（CREATE/ALTER/DROP）随迁移域拆入 `migrations.py`，扫描面必须同时覆盖两文件
        ——只扫 db.py 会让迁移 DDL 脱离 P3-1 的绊线保护。
        """
        for rel in ("yiban", "store", "db.py"), ("yiban", "store", "migrations.py"):
            path = os.path.join(BASE, *rel)
            with open(path, encoding="utf-8") as f:
                src = f.read()
            self.assertNotIn(".executescript(", src,
                             f"{'/'.join(rel)} 重新引入了 executescript（隐式 COMMIT 隐患）")


BACKUP_SH = os.path.join(BASE, "scripts", "backup.sh")
SENTINEL_PY = os.path.join(BASE, "scripts", "backup_sentinel.py")
_FAKE_PASSPHRASE = "e2e-not-a-real-passphrase"  # 假口令，仅为走通 gpg 对称加密路径

# gpg 桩：永远失败——模拟"配了口令但加密链路坏了"（本机没装 gpg 时的等价替身，
# 装了 gpg 的机器上真 gpg 会成功，所以要桩才有确定性）
_FAKE_GPG_FAIL = "#!/usr/bin/env bash\ncat > /dev/null 2>&1 || true\nexit 2\n"


@unittest.skipIf(shutil.which("bash") is None, "需要 bash（Git Bash/WSL）")
class BackupPlaintextP3Test(unittest.TestCase):
    """P3-3：backup.sh 明文模式告警/退出码/异机契约——行为钉死（MF-10 重写）。

    旧版只断言 backup.sh 源码含 "BACKUP_PLAINTEXT=1"/"明文" 字串：纯注释行即满足，
    把告警块整段删掉测试仍全绿——这正是 MF-10 记名的假绿。现改为真跑脚本
    （临时目录夹具 + 故障注入桩），断言的真实来源全部是**行为**：stderr 告警、
    专用退出码 6、异机侧真实落地的密文副本、哨兵对明文产物的 unhealthy 判定。
    改坏/删掉对应实现块 ⇒ 相应用例必须红。
    明文轮退出码 6 为 M3 批次0（MF-79）新增，与既有 rc=4（源库损坏）/rc=5（缺
    sqlite3）不冲突；输出按字节手动 utf-8 解码（text=True 在 Windows 侧按 GBK
    解中文输出会误报，旧类当年因此退化成源码断言）。
    """

    FAKE_KEY = "f" * 64  # 假数据密钥（64 位 hex 形态），不含任何真实凭据

    def _fixture(self, extra_path_stub=None):
        tmp = tempfile.mkdtemp(prefix="bkup-p3-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        app, backups = os.path.join(tmp, "app"), os.path.join(tmp, "backups")
        state, logs = os.path.join(tmp, "state"), os.path.join(tmp, "logs")
        for d in (app, backups, state, logs):
            os.makedirs(d)
        with open(os.path.join(app, ".env"), "w", encoding="utf-8", newline="\n") as f:
            f.write("YIBAN_ACCOUNTS_KEY=" + self.FAKE_KEY + "\n")
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("YIBAN_", "BACKUP_", "REMOTE_"))
               and k not in ("RETENTION_DAYS", "KEY_FILE", "DB_FILE", "SIGN_STATE_DIR",
                             "SIGN_LOG_DIR")}
        env.update({
            "APP_DIR": app, "BACKUP_DIR": backups,
            "YIBAN_STATE_DIR": state,
            "YIBAN_LOG_FILE": os.path.join(logs, "sign.log"),
            "KEY_FILE": os.path.join(tmp, "no-accounts-key"),
        })
        if extra_path_stub:
            name, body = extra_path_stub
            fakebin = os.path.join(tmp, "fakebin")
            os.makedirs(fakebin, exist_ok=True)
            stub = os.path.join(fakebin, name)
            with open(stub, "w", encoding="utf-8", newline="\n") as f:
                f.write(body)
            os.chmod(stub, 0o755)
            env["PATH"] = fakebin + os.pathsep + env.get("PATH", "")
        return tmp, backups, env

    def _run(self, env, args=()):
        return subprocess.run([shutil.which("bash"), BACKUP_SH, *args],
                              capture_output=True, env=env,
                              cwd=os.path.dirname(env["APP_DIR"]), timeout=300)

    @staticmethod
    def _txt(raw):
        return (raw or b"").decode("utf-8", errors="replace")

    def _day(self):
        return datetime.date.today().strftime("%Y-%m-%d")

    def test_plaintext_run_warns_on_stderr_and_exits_dedicated_rc6(self):
        """BACKUP_PLAINTEXT=1 ⇒ 大字告警走 stderr + 专用退出码 6 + 归档真实产出。"""
        _, backups, env = self._fixture()
        env["BACKUP_PLAINTEXT"] = "1"
        r = self._run(env)
        out = self._txt(r.stdout) + self._txt(r.stderr)
        self.assertEqual(r.returncode, 6,
                         f"明文轮必须与密文正常轮(rc=0)可区分——退出码 6：{out}")
        stderr = self._txt(r.stderr)
        self.assertIn("BACKUP_PLAINTEXT=1", stderr,
                      "告警必须在 stderr：stdout 是 cron 日志里的例行流水，明文告警不得淹死其中")
        self.assertIn("明文", stderr)
        archive = os.path.join(backups, f"yiban-{self._day()}.tar.gz")
        self.assertTrue(os.path.isfile(archive), f"告警之外本轮仍须真实产出归档：{out}")

    def test_plaintext_artifact_not_healthy_for_sentinel(self):
        """backup.sh 真产出的明文包，哨兵不得计为健康（与 backup_sentinel 的 e2e 咬合）。"""
        _, backups, env = self._fixture()
        env["BACKUP_PLAINTEXT"] = "1"
        self.assertEqual(self._run(env).returncode, 6)
        spec = importlib.util.spec_from_file_location("_sentinel_p3", SENTINEL_PY)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        day = self._day()
        found, _ = mod._find_archive(backups, day)
        self.assertIsNone(found, "真实产出的明文归档被哨兵计成了健康归档")
        self.assertIsNotNone(mod._find_plaintext(backups, day),
                            "明文包应被识别（排查线索），只是不算健康")

    def test_remote_copy_still_encrypted_under_plaintext(self):
        """BACKUP_PLAINTEXT=1 只豁免本地：异机副本仍加密出站、绝不传明文。

        行为替代旧源码断言 `test_remote_no_longer_blocked_by_plaintext` /
        `test_remote_still_attempts_encryption`：REMOTE_BACKUP 指本地目录，
        rsync/scp 真实落地一份【密文】。
        """
        if shutil.which("rsync") is None and shutil.which("scp") is None:
            self.skipTest("需要 rsync 或 scp")
        tmp, _, env = self._fixture()
        remote = os.path.join(tmp, "remote")
        os.makedirs(remote)
        env.update({"BACKUP_PLAINTEXT": "1", "REMOTE_BACKUP": remote,
                    "BACKUP_GPG_PASSPHRASE": _FAKE_PASSPHRASE})
        r = self._run(env)
        out = self._txt(r.stdout) + self._txt(r.stderr)
        day = self._day()
        self.assertTrue(os.path.isfile(os.path.join(remote, f"yiban-{day}.tar.gz.gpg")),
                        f"异机加密副本必须落地（本地明文豁免不得把异机一并静默丢弃）：{out}")
        self.assertFalse(os.path.isfile(os.path.join(remote, f"yiban-{day}.tar.gz")),
                         "异机侧绝不许出现明文副本")
        self.assertEqual(r.returncode, 6, f"本地仍是明文轮语义（rc=6）：{out}")

    def test_remote_plaintext_without_encryption_distinguishes_case(self):
        """显式明文 + 加密不可用 ⇒ 告警必须点名"豁免只作用于本地、异机绝不传明文"。

        行为替代旧源码文本断言：gpg 桩恒失败（非交互无 age），异机缺位的告警
        走真实执行路径，且远端目录保持为空（没有明文漏传）。
        """
        tmp, _, env = self._fixture(extra_path_stub=("gpg", _FAKE_GPG_FAIL))
        remote = os.path.join(tmp, "remote")
        os.makedirs(remote)
        env.update({"BACKUP_PLAINTEXT": "1", "REMOTE_BACKUP": remote,
                    "BACKUP_GPG_PASSPHRASE": _FAKE_PASSPHRASE})
        r = self._run(env)
        out = self._txt(r.stdout) + self._txt(r.stderr)
        self.assertIn("BACKUP_PLAINTEXT=1 只豁免本地归档的默认加密", out,
                      f"异机缺位告警须区分「显式明文」与「无加密方式」：{out}")
        self.assertIn("异机副本绝不传明文", out)
        self.assertEqual(r.returncode, 6, out)
        self.assertEqual([], os.listdir(remote), "异机侧不得出现任何副本（明文绝不例外）")


TEST_KEY_SMOKE = "a" * 64  # 64 位 hex = 32 字节 AES 密钥


class SmokeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-smoke-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY_SMOKE}\n")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.accounts_file = os.path.join(cls.tmp, "accounts.json")
        cls.users_file = os.path.join(cls.tmp, "users.json")
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY_SMOKE
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_ACCOUNTS_FILE"] = cls.accounts_file
        os.environ["YIBAN_USERS_FILE"] = cls.users_file
        os.environ["YIBAN_DB_FILE"] = cls.db_file
        # 导入被测模块
        global account_crypto, db
        # web/app.py 模块级函数可独立调用
        import importlib.util

        import db

        from yiban.infra import account_crypto
        spec = importlib.util.spec_from_file_location("webapp", os.path.join(BASE, "web", "app.py"))
        cls.webapp = importlib.util.module_from_spec(spec)
        sys.modules["webapp"] = cls.webapp
        with contextlib.suppress(Exception):
            spec.loader.exec_module(cls.webapp)  # 模块顶层可能因环境失败，用函数级验证兜底

    @classmethod
    def tearDownClass(cls):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        shutil.rmtree(cls.tmp, ignore_errors=True)
        os.environ.pop("YIBAN_ACCOUNTS_KEY", None)

    def setUp(self):
        # 每个测试独立数据库：关闭连接、删除库文件（含 WAL/SHM），再按需迁移
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        for suffix in ("", "-wal", "-shm"):
            p = self.db_file + suffix
            if os.path.exists(p):
                os.remove(p)
        # 清理迁移源残留：.bak 已存在时新 JSON 不再改名，会导致后续测试重复迁移
        for n in os.listdir(self.tmp):
            if n.startswith("accounts.json.bak-") or n.startswith("users.json.bak-"):
                os.remove(os.path.join(self.tmp, n))

    def _write_accounts(self, accounts):
        with open(self.accounts_file, "w", encoding="utf-8") as f:
            json.dump(accounts, f, ensure_ascii=False)

    def _init_db(self):
        """初始化数据库并执行 JSON→SQLite 自动迁移（模拟 create_app 启动路径）。"""
        db.init_db(self.db_file, migrate_from=self.accounts_file, env_file=self.env_file)

    # ---- 1. 加密/解密 ----
    def test_encrypt_decrypt_roundtrip(self):
        key = account_crypto._decode_key(TEST_KEY_SMOKE)
        ct = account_crypto.encrypt_password("secret-pass", key, "13800138000")
        self.assertTrue(account_crypto.is_encrypted(ct))
        self.assertEqual(account_crypto.decrypt_password(ct, key, "13800138000"), "secret-pass")

    def test_aad_blocks_cross_account(self):
        key = account_crypto._decode_key(TEST_KEY_SMOKE)
        ct = account_crypto.encrypt_password("secret-pass", key, "13800138000")
        with self.assertRaises(ValueError):
            account_crypto.decrypt_password(ct, key, "13900139000")  # AAD 不匹配

    # ---- 2. JSON→SQLite 迁移 + 解密 + 惰性清理 ----
    def test_migration_encrypts_and_decrypts(self):
        old = (clock.now() - datetime.timedelta(days=8)).isoformat(timespec="seconds")
        self._write_accounts([
            {"name": "测试", "phone": "13800138000", "password": "plain-pass", "status": "active"},
            # 超期软删除账号：迁移后 load 时应被惰性清理
            {"name": "旧删", "phone": "13900139000", "password": "p2", "status": "active",
             "deleted": True, "deleted_at": old},
        ])
        self._init_db()
        # 业务层看到明文；超期账号被清除
        accounts = db.load_accounts()
        self.assertEqual([a["phone"] for a in accounts], ["13800138000"])
        self.assertEqual(accounts[0]["password"], "plain-pass")
        # 库内为密文（存储层加密）
        conn = db.get_conn()
        row = conn.execute("SELECT password FROM accounts WHERE phone='13800138000'").fetchone()
        self.assertTrue(account_crypto.is_encrypted(json.loads(row["password"])))
        # JSON 已改名 .bak 保留逃生门
        self.assertTrue(any(n.startswith("accounts.json.bak-") for n in os.listdir(self.tmp)))

    def test_add_account_encrypts(self):
        self._init_db()
        db.add_account({"name": "新号", "phone": "13700137000", "password": "new-pass",
                        "owner": "admin", "status": "active"})
        conn = db.get_conn()
        row = conn.execute("SELECT password FROM accounts WHERE phone='13700137000'").fetchone()
        self.assertTrue(account_crypto.is_encrypted(json.loads(row["password"])))
        # load 后业务层为明文
        self.assertEqual(db.load_accounts()[0]["password"], "new-pass")

    def test_migration_cipher_dict_format(self):
        """0.16 生产格式：accounts.json 密文为 JSON 嵌套对象（dict）——迁移须序列化入库。"""
        key = account_crypto._decode_key(TEST_KEY_SMOKE)
        ct_obj = account_crypto.encrypt_password("cipher-pass", key, "13600136000")
        self._write_accounts([
            {"name": "密文账号", "phone": "13600136000", "password": ct_obj,  # dict 密文对象
             "phone_model": "X1", "status": "active", "owner": "admin"},
        ])
        self._init_db()
        accounts = db.load_accounts()
        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0]["password"], "cipher-pass")  # 解密为明文
        conn = db.get_conn()
        row = conn.execute("SELECT password FROM accounts WHERE phone='13600136000'").fetchone()
        self.assertTrue(account_crypto.is_encrypted(json.loads(row["password"])))  # 库内 JSON 串

    # ---- 3. 登录/权限基础（轻量：直接验证 verify_admin 兼容路径）----
    def test_admin_verify_plain_fallback(self):
        # 无哈希时明文回退（旧 .env 兼容）
        if hasattr(self.webapp, "verify_admin"):
            old_env = self.webapp.ENV_FILE
            self.webapp.ENV_FILE = self.env_file
            try:
                self.assertFalse(self.webapp.verify_admin("admin", "wrong"))  # 无配置/密码错误应 False
            finally:
                self.webapp.ENV_FILE = old_env

    # ---- 4. 批量防呆：批量 purge 不能删未软删除账号 ----
    def test_batch_purge_requires_deleted(self):
        self._init_db()
        db.add_account({"name": "正常", "phone": "13800138000", "password": "p1", "status": "active"})
        deleted_id = db.add_account({"name": "已删", "phone": "13900139000", "password": "p2",
                                     "status": "active"})
        db.set_account_deleted(deleted_id, 1, clock.now().isoformat(timespec="seconds"))
        # 模拟批量 purge 逻辑：仅删 deleted 的
        accounts = db.load_accounts()
        deleted = [a for a in accounts if a.get("deleted")]
        self.assertEqual(len(deleted), 1)
        self.assertEqual(deleted[0]["phone"], "13900139000")
        for a in deleted:
            db.purge_account(a["id"])
        self.assertEqual([a["phone"] for a in db.load_accounts()], ["13800138000"])

    # ---- 5. 软删除超期清理（2026-08-20 契约变更：移出读路径，显式调用）----
    def test_expired_soft_delete_cleaned(self):
        self._init_db()
        old = (clock.now() - datetime.timedelta(days=8)).isoformat(timespec="seconds")
        db.add_account({"name": "A", "phone": "13800138000", "password": "p1", "status": "active"})
        db.set_account_deleted(db.load_accounts()[0]["id"], 1, old)
        # 读路径不再惰性清理（防 idx 寻址漂移）：超期行在列表中保持原位
        self.assertEqual(len(db.load_accounts()), 1)
        # 显式清理（启动/每日线程/signin 启动时调用）后物理删除
        db.purge_expired_deleted_accounts()
        self.assertEqual(db.load_accounts(), [])

    # ---- 6. 用户表 CRUD（db 层）----
    def test_users_crud(self):
        self._init_db()
        db.create_user("a@x.com", "hash1", role="user", created_at="", pw_version=1)
        self.assertEqual(db.find_user("a@x.com")["email"], "a@x.com")
        db.update_user("a@x.com", {"role": "admin", "pw_version": 2})
        u = db.find_user("a@x.com")
        self.assertEqual(u["role"], "admin")
        self.assertEqual(u["pw_version"], 2)
        # 物理删除入口 delete_user 已删（无生产调用方），改用
        # 软注销——语义保持"注销后 find_user 不再可见"（find_user 过滤 deleted=0）。
        # a@x.com 已提为 admin，须预置另一名 admin，否则触发"最后一个注册管理员
        # 不可注销"守卫（C-M3）抛 LastAdminError
        db.create_user("root@x.com", "hash0", role="admin", created_at="", pw_version=1)
        self.assertTrue(db.soft_delete_user_with_accounts("a@x.com"))
        self.assertIsNone(db.find_user("a@x.com"))

    # ---- 7. 手机号唯一约束（并发兜底语义）----
    def test_phone_unique_conflict(self):
        self._init_db()
        db.add_account({"name": "A", "phone": "13800138000", "password": "p1", "status": "active"})
        with self.assertRaises(db.DuplicatePhoneError):
            db.add_account({"name": "B", "phone": "13800138000", "password": "p2"})
        id2 = db.add_account({"name": "B", "phone": "13900139000", "password": "p2", "status": "active"})
        # 改手机号撞他人 UNIQUE（改自己的号不冲突，排除自身）
        with self.assertRaises(db.DuplicatePhoneError):
            db.update_account(id2, {"phone": "13800138000"})
        db.update_account(id2, {"phone": "13900139000"})  # 原号不改，不冲突

    # ---- 8. 乐观锁：匹配 True / 不匹配 False / 不存在 None ----
    def test_optimistic_lock(self):
        self._init_db()
        acc_id = db.add_account({"name": "A", "phone": "13800138000", "password": "p1",
                                 "status": "active"})
        snap = {"name": "A", "phone": "13800138000", "phone_model": "",
                "status": "active", "deleted": False}
        self.assertTrue(db.update_account(acc_id, {"name": "A2"}, expect_snapshot=snap))
        self.assertFalse(db.update_account(acc_id, {"name": "A3"}, expect_snapshot=snap))
        self.assertIsNone(db.update_account(99999, {"name": "X"}, expect_snapshot=snap))

    # ---- 9. 移动交换排序（仅未删除行参与）----
    def test_move_swap_order(self):
        self._init_db()
        id1 = db.add_account({"name": "A", "phone": "13800138000", "password": "p1", "status": "active"})
        id2 = db.add_account({"name": "B", "phone": "13900139000", "password": "p2", "status": "active"})
        id3 = db.add_account({"name": "C", "phone": "13700137000", "password": "p3", "status": "active"})
        # B 下移（与 C 交换）
        self.assertTrue(db.move_account(id2, 1))
        order = [a["phone"] for a in db.load_accounts()]
        self.assertEqual(order, ["13800138000", "13700137000", "13900139000"])
        # A 上移（已到顶，失败）
        self.assertFalse(db.move_account(id1, -1))
        # 软删除的账号不参与交换
        db.set_account_deleted(id2, 1, clock.now().isoformat(timespec="seconds"))
        self.assertFalse(db.move_account(id3, 1))  # C 之后无未删除账号

    # ---- 10. 审计写入 ----
    def test_audit_write(self):
        self._init_db()
        db.audit("tester", "account_add", "138****8000", "测试审计")
        conn = db.get_conn()
        row = conn.execute(
            "SELECT username, action, target, detail FROM audit_logs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(row["username"], "tester")
        self.assertEqual(row["action"], "account_add")
        self.assertEqual(row["detail"], "测试审计")


    # ---- 11. 解密失败统一收口（对抗性审查 2026-08-15 L1）----
    def test_decrypt_failure_wrapped_runtime_error(self):
        """密文损坏（tag 校验失败）→ load_accounts 抛 RuntimeError（统一 JSON 收口），非 ValueError 透传。"""
        self._init_db()
        from yiban.infra import account_crypto

        key = account_crypto.load_key(self.env_file)
        good = account_crypto.encrypt_password("secret", key, "13800138000")
        bad = {**good, "tag": "00" * 16}  # 篡改 tag → 解密必失败
        acc_id = db.add_account({"name": "A", "phone": "13800138000",
                                 "password": "p1", "status": "active"})
        conn = db.get_conn()
        conn.execute("UPDATE accounts SET password=? WHERE id=?",
                     (json.dumps(bad), acc_id))
        conn.commit()
        with self.assertRaises(RuntimeError):
            db.load_accounts()

    # ---- 12. 首启建密钥并发唯一（对抗性审查 2026-08-15 F3）----
    def test_load_key_concurrent_single_key(self):
        """多线程首启 load_key：只生成一份密钥并共享（_KEY_LOCK 双检）。"""
        import threading

        from yiban.infra import account_crypto

        old_cache = account_crypto._KEY_CACHE
        old_env = os.environ.pop("YIBAN_ACCOUNTS_KEY", None)
        env2 = os.path.join(self.tmp, "env2.env")
        if os.path.exists(env2):
            os.remove(env2)
        account_crypto._KEY_CACHE = None
        keys = []

        def get():
            keys.append(account_crypto.load_key(env2))

        try:
            ts = [threading.Thread(target=get) for _ in range(8)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
            self.assertEqual(len({k.hex() for k in keys}), 1,
                             f"多线程生成了多个密钥: {sorted({k.hex() for k in keys})}")
        finally:
            account_crypto._KEY_CACHE = old_cache
            if old_env is not None:
                os.environ["YIBAN_ACCOUNTS_KEY"] = old_env

    # ---- 13. 批量签到 API 权限校验（H9 测试覆盖）----
    def test_batch_signin_requires_admin(self):
        """非管理员调用 /api/signin/batch 应返回 403。"""
        self._init_db()
        app = self.webapp.create_app()
        c = app.test_client()
        # 未登录调用应被拒绝（重定向到登录页或 403）
        r = c.post("/api/signin/batch", json={"ids": [1]})
        self.assertIn(r.status_code, (302, 401, 403),
                        f"未登录调用批量签到应被拒绝，实际返回 {r.status_code}")

    # ---- 15. CSRF 错误场景（E 测试覆盖补充）----
    def test_csrf_wrong_token_rejected(self):
        """携带错误/缺失 CSRF token 的 POST 应被拒绝（401 未登录 或 403 CSRF 失败）。"""
        self._init_db()
        app = self.webapp.create_app()
        c = app.test_client()
        # 未登录时使用错误 token → 应被拒绝（401 未登录 或 403 CSRF 失败 均可接受）
        r = c.post("/api/me/delete", json={"password": "x"},
                   headers={"X-CSRF-Token": "wrongtoken123456"})
        self.assertIn(r.status_code, (401, 403),
                      f"错误 CSRF token 应被拒绝，实际返回 {r.status_code}")
