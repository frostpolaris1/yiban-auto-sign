# -*- coding: utf-8 -*-
"""v18 迁移：持久化任务队列 `sign_tasks` + 执行体心跳 `executor_heartbeats`
+ 出口令牌桶状态 `egress_state`。

覆盖四件事：
- schema 落地：三张表、两个领取/回收索引、`sign_tasks` 逐列（名/类型/NOT NULL/主键）匹配；
- `sign_claims` 存量数据平移进 `sign_tasks`（vshard 落 -1、run_at 取 claimed_at），
  且幂等（连跑两次不重复插入）、旧表不删且原行原样保留；
- 耐久性：迁移后连接的 `PRAGMA synchronous` 为 FULL（2）——签到的"是否已登录"判据
  落在这张表上，断电丢终态等于重复真实登录；
- 登记口径：v18 是**可选迁移**，失败不阻断启动、不提升 user_version。
"""
import contextlib
import os
import shutil
import sqlite3
import tempfile
import unittest
from typing import ClassVar

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import db  # noqa: E402

TEST_KEY = "a" * 64
DAY = "2026-09-22"

# sign_tasks 的 12 列：(列名, 类型, NOT NULL, 主键序号)，顺序即 DDL 顺序
SIGN_TASKS_COLUMNS = [
    ("phone", "TEXT", 1, 1),
    ("day", "TEXT", 1, 2),
    ("vshard", "INTEGER", 1, 0),
    ("owner", "TEXT", 1, 0),
    ("run_at", "TEXT", 1, 0),
    ("priority", "INTEGER", 1, 0),
    ("state", "TEXT", 1, 0),
    ("attempts", "INTEGER", 1, 0),
    ("lease_until", "TEXT", 1, 0),
    ("epoch", "INTEGER", 1, 0),
    ("result", "TEXT", 1, 0),
    ("created_at", "TEXT", 1, 0),
]


def _create_sign_claims(conn):
    """手工建 v17 的 sign_claims（供直接调用 migrate_v18 的用例造前置状态）。"""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS sign_claims ("
        "phone TEXT NOT NULL, "
        "day TEXT NOT NULL, "
        "owner TEXT NOT NULL, "
        "claimed_at TEXT NOT NULL, "
        "heartbeat_at TEXT NOT NULL, "
        "state TEXT NOT NULL DEFAULT 'claimed', "
        "result TEXT NOT NULL DEFAULT '', "
        "attempts INTEGER NOT NULL DEFAULT 0, "
        "PRIMARY KEY (phone, day)"
        ")"
    )


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-v18-")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n")
        os.environ.update({
            "YIBAN_ACCOUNTS_KEY": TEST_KEY,
            "YIBAN_ENV_FILE": cls.env_file,
            "YIBAN_DB_FILE": cls.db_file,
        })

    @classmethod
    def tearDownClass(cls):
        cls._close_conn()
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k in ("YIBAN_ACCOUNTS_KEY", "YIBAN_ENV_FILE", "YIBAN_DB_FILE"):
            os.environ.pop(k, None)

    @staticmethod
    def _close_conn():
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None

    def setUp(self):
        self._close_conn()
        for suffix in ("", "-wal", "-shm"):
            p = self.db_file + suffix
            if os.path.exists(p):
                os.remove(p)

    def tearDown(self):
        self._close_conn()

    def _init_full(self):
        """跑到当前最新版本（含 v18；v19 及以后的迁移在本类的断言之外）。"""
        return db.init_db(self.db_file, env_file=self.env_file, cleanup=False)

    def _init_at_v17(self):
        """先只跑到 v17——给"存量 sign_claims 行在 v18 之前已存在"造前置状态。"""
        old = db._MIGRATIONS
        db._MIGRATIONS = [m for m in old if m[0] <= 17]
        try:
            return db.init_db(self.db_file, env_file=self.env_file, cleanup=False)
        finally:
            db._MIGRATIONS = old

    def _seed_claims(self, conn, rows):
        conn.executemany(
            "INSERT INTO sign_claims (phone, day, owner, claimed_at, heartbeat_at, "
            "state, result, attempts) VALUES (?,?,?,?,?,?,?,?)", rows)
        conn.commit()


class SchemaTest(_Base):
    def test_v18_bumps_version_and_creates_tables_and_indexes(self):
        conn = self._init_full()
        self.assertGreaterEqual(conn.execute("PRAGMA user_version").fetchone()[0], 18)
        self.assertIn(18, [m[0] for m in db._MIGRATIONS], "v18 必须登记在迁移表里")
        tables = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for t in ("sign_tasks", "executor_heartbeats", "egress_state"):
            self.assertIn(t, tables, f"缺表 {t}")
        indexes = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
        for i in ("idx_tasks_pickup", "idx_tasks_lease"):
            self.assertIn(i, indexes, f"缺索引 {i}")

    def test_sign_tasks_columns_types_notnull_and_pk(self):
        conn = self._init_full()
        got = [(r["name"], r["type"], r["notnull"], r["pk"]) for r in conn.execute(
            "PRAGMA table_info(sign_tasks)").fetchall()]
        self.assertEqual(got, SIGN_TASKS_COLUMNS)

    def test_synchronous_is_full_after_migration(self):
        """claim/settle 所在表的耐久级必须是 FULL：WAL+NORMAL 会回滚终态 ⇒ 重复登录。

        先把连接降到 NORMAL(1) 再跑框架迁移，断言才有判别力——新连接默认本就是
        FULL(2)，不前置的话删掉迁移里的 PRAGMA 这个用例照样绿。
        """
        conn = self._init_at_v17()
        conn.execute("PRAGMA synchronous = NORMAL")
        self.assertEqual(conn.execute("PRAGMA synchronous").fetchone()[0], 1,
                         "前置条件：连接耐久级不是 FULL")
        db._run_migrations(conn)          # 框架路径：v18 在 BEGIN IMMEDIATE 内执行
        self.assertEqual(conn.execute("PRAGMA synchronous").fetchone()[0], 2,
                         "v18 迁移后连接必须是 FULL")


class DataShiftTest(_Base):
    """sign_claims 存量行平移进 sign_tasks（旧表保留只读过渡）。"""

    ROWS: ClassVar[list] = [
        ("13800138000", DAY, "hostA:1:090000", "2026-09-22 07:00:01",
         "2026-09-22 07:00:11", "done", "签到成功", 1),
        ("13800138001", DAY, "hostB:2:090001", "2026-09-22 07:00:02",
         "2026-09-22 07:00:12", "claimed", "", 0),
        ("13800138002", DAY, "hostA:1:090000", "2026-09-22 07:00:03",
         "2026-09-22 07:00:13", "failed", "重试耗尽", 3),
    ]

    def _migrate_with_seed(self):
        conn = self._init_at_v17()
        self._seed_claims(conn, self.ROWS)
        before = [dict(r) for r in conn.execute(
            "SELECT * FROM sign_claims ORDER BY phone").fetchall()]
        db._run_migrations(conn)          # 框架路径：v18 在 BEGIN IMMEDIATE 内执行
        return conn, before

    def test_rows_shift_field_by_field(self):
        conn, _ = self._migrate_with_seed()
        self.assertGreaterEqual(conn.execute("PRAGMA user_version").fetchone()[0], 18)
        shifted = {r["phone"]: dict(r) for r in conn.execute(
            "SELECT * FROM sign_tasks").fetchall()}
        self.assertEqual(set(shifted), {r[0] for r in self.ROWS})
        for phone, _day, owner, claimed_at, heartbeat_at, state, result, attempts in self.ROWS:
            row = shifted[phone]
            self.assertEqual(row["day"], DAY)
            self.assertEqual(row["vshard"], -1, "平移行不参与分片分工")
            self.assertEqual(row["owner"], owner)
            self.assertEqual(row["run_at"], claimed_at)
            self.assertEqual(row["priority"], 5)
            self.assertEqual(row["state"], state)
            self.assertEqual(row["attempts"], attempts)
            self.assertEqual(row["lease_until"], heartbeat_at)
            self.assertEqual(row["result"], result)
            self.assertEqual(row["created_at"], claimed_at)
            self.assertEqual(row["epoch"], 0)

    def test_shift_is_idempotent(self):
        conn, _ = self._migrate_with_seed()
        db.migrate_v18(conn)
        db.migrate_v18(conn)
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM sign_tasks").fetchone()[0],
            len(self.ROWS), "重跑迁移不得重复插入")

    def test_old_table_kept_and_rows_intact(self):
        conn, before = self._migrate_with_seed()
        self.assertTrue(conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='sign_claims'"
        ).fetchone(), "sign_claims 只读过渡期内不得 DROP")
        after = [dict(r) for r in conn.execute(
            "SELECT * FROM sign_claims ORDER BY phone").fetchall()]
        # 后续迁移可能给本表补列（如 fencing 的 epoch，存量行取默认值），
        # 故只比对平移那一刻已有的列：数据平移不得改动旧表的行。
        keys = set(before[0])
        self.assertEqual([{k: r[k] for k in keys} for r in after], before)


class SynchronousAppliedByMigrationTest(_Base):
    """迁移函数本身必须把连接的耐久级提到 FULL（不依赖连接默认值）。"""

    def test_migrate_v18_raises_synchronous_to_full(self):
        conn = sqlite3.connect(self.db_file)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA synchronous = NORMAL")
            self.assertEqual(conn.execute("PRAGMA synchronous").fetchone()[0], 1,
                             "前置条件：连接耐久级不是 FULL")
            _create_sign_claims(conn)
            conn.commit()
            db.migrate_v18(conn)
            self.assertEqual(conn.execute("PRAGMA synchronous").fetchone()[0], 2)
        finally:
            conn.close()


class OptionalRegistrationTest(_Base):
    """v18 是可选迁移：失败只告警不阻断启动、不提升 user_version（防误登记为核心）。"""

    def test_v18_failure_does_not_block_startup(self):
        def failing_v18(conn):
            raise RuntimeError("boom")

        old = db._MIGRATIONS
        db._MIGRATIONS = [(18, "v18_failing", failing_v18, False)]
        try:
            conn = db.init_db(self.db_file, env_file=self.env_file, cleanup=False)
            self.assertIsNotNone(conn)
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 0)
        finally:
            db._MIGRATIONS = old


if __name__ == "__main__":
    unittest.main()
