# -*- coding: utf-8 -*-
"""Task 3：DB 层数据完整性与迁移修复测试。

覆盖 brief 中：
- S2：purge_deleted_users_hard 不误删活跃账号，且清理已删账号的 time_prefs；
- S4：migrate_v5 先 DROP users.email 旧唯一索引，再建部分唯一索引；
- S5：可选迁移失败/延后时 continue 执行后续迁移，不提升 user_version，重试后到 7；
- H1：restore_user 按用户行 deleted_at 关联恢复账号；
- M4：_audit_cleanup 不再重建哈希链，verify 首行以自身 prev_hash 为锚；
- M5：time_pref_stats 排除已删账号，_purge_expired_deleted 连带删除 time_prefs；
- M23：page_visit_active_users 仅统计登录用户（user_id 非空），匿名 ip_hash 不计入。
"""
import contextlib
import datetime
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "scripts"))

import db  # noqa: E402

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
            conn.execute("PRAGMA user_version").fetchone()[0], 7, "下次启动应重试到最新版本"
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
        old_ts = (datetime.datetime.now() - datetime.timedelta(days=200)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        h1 = _insert_audit_row(conn, old_ts, "admin", "old1", prev_hash="")
        _insert_audit_row(conn, old_ts, "admin", "old2", prev_hash=h1)
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
        old = (datetime.datetime.now() - datetime.timedelta(days=8)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        db.set_account_deleted(account_id, 1, old)

        db._purge_expired_deleted(db.get_conn())

        self.assertIsNone(db.get_time_pref("13800000031"), "过期账号清除时 time_prefs 应连带删除")

    # ---- M23：活跃用户数仅统计登录用户 ----
    def test_page_visit_active_users_counts_only_logged_in_users(self):
        db.init_db(self.db_file, env_file=self.env_file)
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        db.add_page_visit(now, "admin", "/", ip_hash="a", user_id=1)
        db.add_page_visit(now, "admin", "/", ip_hash="a", user_id=None)
        db.add_page_visit(now, "admin", "/", ip_hash="b", user_id=1)
        db.add_page_visit(now, "admin", "/", ip_hash="b", user_id=None)
        db.add_page_visit(now, "admin", "/", ip_hash="c", user_id=None)

        # 匿名访问（user_id 为空）只留 ip_hash，不应计入活跃登录用户
        self.assertEqual(db.page_visit_active_users(days=30), 1)
