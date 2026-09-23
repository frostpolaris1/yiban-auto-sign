# -*- coding: utf-8 -*-
"""v19 迁移：`sign_claims` 补 fencing token 列 `epoch`（`sign_tasks.epoch` 兜底补齐）。

覆盖三件事：
- schema 落地：`epoch INTEGER NOT NULL DEFAULT 0`，存量行取默认 0（不可为 NULL——
  领取路径要拿它做算术，NULL 会让 `epoch = epoch + 1` 静默变 NULL）；
- 幂等：重跑不报错、不重复加列（可选迁移失败后下次启动整段重跑）；
- 兜底：`sign_tasks` 缺 `epoch` 时由本迁移补上（v18 若被回退，v19 仍是可用的护栏）；
- 登记口径：v19 是**可选迁移**，失败不阻断启动、不提升 user_version。
"""
import contextlib
import os
import shutil
import sqlite3
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import db  # noqa: E402

TEST_KEY = "a" * 64
DAY = "2026-09-22"


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-v19-")
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
        return db.init_db(self.db_file, env_file=self.env_file, cleanup=False)

    def _init_at_v17(self):
        """只跑到 v17——给"存量 sign_claims 行在 v19 之前已存在"造前置状态。"""
        old = db._MIGRATIONS
        db._MIGRATIONS = [m for m in old if m[0] <= 17]
        try:
            return db.init_db(self.db_file, env_file=self.env_file, cleanup=False)
        finally:
            db._MIGRATIONS = old

    @staticmethod
    def _registry_up_to(top):
        """登记表里 ≤ top 的那些项——断言"某一版的登记口径"时不跟着表尾跑。

        表尾会被后续迁移继续追加，取 `_MIGRATIONS[-1]` 等于改测那一项：把本版改成核心
        迁移，断言照样绿。
        """
        return [m for m in db._MIGRATIONS if m[0] <= top]


class SchemaTest(_Base):
    def test_v19_bumps_version_and_adds_epoch_column(self):
        conn = self._init_full()
        # 断言对齐迁移登记表的顶，而不是写死 19——本文件不必随新迁移再改
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0],
                         db._MIGRATIONS[-1][0])
        self.assertIn(19, [m[0] for m in db._MIGRATIONS], "v19 应在迁移登记表中")
        col = {r["name"]: r for r in conn.execute(
            "PRAGMA table_info(sign_claims)").fetchall()}["epoch"]
        self.assertEqual(col["type"], "INTEGER")
        self.assertEqual(col["notnull"], 1, "epoch 必须 NOT NULL：NULL 会让自增算术静默失效")
        self.assertEqual(str(col["dflt_value"]), "0")

    def test_v19_is_optional(self):
        """可选迁移：失败只告警不阻断启动（epoch 属护栏，不是启动必需能力）。"""
        narrow = self._registry_up_to(19)
        self.assertEqual(narrow[-1][0], 19, "≤19 的登记尾项应是 v19")
        self.assertIs(narrow[-1][3], False)

    def test_v20_is_registered_as_optional(self):
        """v20 同为可选迁移（补账失败只告警，下次启动整段重跑收敛）。"""
        narrow = self._registry_up_to(20)
        self.assertEqual(narrow[-1][0], 20, "≤20 的登记尾项应是 v20")
        self.assertIs(narrow[-1][3], False)

    def test_existing_rows_default_to_zero(self):
        """存量行（v17 时代建的）取默认 0——迁移不得要求重写业务行。"""
        conn = self._init_at_v17()
        conn.execute(
            "INSERT INTO sign_claims (phone, day, owner, claimed_at, heartbeat_at, "
            "state, result, attempts) VALUES (?,?,?,?,?,?,?,?)",
            ("13800138000", DAY, "hostA:1:090000", "2026-09-22 07:00:01",
             "2026-09-22 07:00:11", "claimed", "", 0))
        conn.commit()
        db._run_migrations(conn)          # 框架路径：v18 + v19 在 BEGIN IMMEDIATE 内执行
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0],
                         db._MIGRATIONS[-1][0], "迁移链应跑到登记表的顶")
        row = conn.execute(
            "SELECT epoch FROM sign_claims WHERE phone=?", ("13800138000",)).fetchone()
        self.assertEqual(row["epoch"], 0)


class IdempotenceTest(_Base):
    def test_rerunning_v19_does_not_raise_nor_duplicate(self):
        """可选迁移失败后下次启动整段重跑，故必须幂等（重复 ADD COLUMN 会报错）。"""
        conn = self._init_full()
        db.migrate_v19(conn)
        db.migrate_v19(conn)
        cols = [r["name"] for r in conn.execute(
            "PRAGMA table_info(sign_claims)").fetchall()]
        self.assertEqual(cols.count("epoch"), 1)

    def test_v19_adds_epoch_to_sign_tasks_when_missing(self):
        """`sign_tasks.epoch` 本由 v18 建好；v18 被回退时 v19 是兜底护栏。"""
        conn = sqlite3.connect(self.db_file)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute(
                "CREATE TABLE sign_claims (phone TEXT NOT NULL, day TEXT NOT NULL, "
                "owner TEXT NOT NULL, claimed_at TEXT NOT NULL, heartbeat_at TEXT NOT NULL, "
                "state TEXT NOT NULL DEFAULT 'claimed', result TEXT NOT NULL DEFAULT '', "
                "attempts INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (phone, day))")
            conn.execute(
                "CREATE TABLE sign_tasks (phone TEXT NOT NULL, day TEXT NOT NULL, "
                "vshard INTEGER NOT NULL, owner TEXT NOT NULL DEFAULT '', "
                "run_at TEXT NOT NULL, priority INTEGER NOT NULL DEFAULT 5, "
                "state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0, "
                "lease_until TEXT NOT NULL DEFAULT '', result TEXT NOT NULL DEFAULT '', "
                "created_at TEXT NOT NULL, PRIMARY KEY (phone, day))")
            conn.commit()
            db.migrate_v19(conn)
            for table in ("sign_claims", "sign_tasks"):
                with self.subTest(table=table):
                    cols = {r["name"] for r in conn.execute(
                        f"PRAGMA table_info({table})").fetchall()}
                    self.assertIn("epoch", cols)
        finally:
            conn.close()


class OptionalRegistrationTest(_Base):
    """v19 失败不得阻断启动、不得提升 user_version。"""

    def test_v19_failure_does_not_block_startup(self):
        def failing_v19(conn):
            raise RuntimeError("boom")

        old = db._MIGRATIONS
        db._MIGRATIONS = [(19, "v19_failing", failing_v19, False)]
        try:
            conn = db.init_db(self.db_file, env_file=self.env_file, cleanup=False)
            self.assertIsNotNone(conn)
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 0)
        finally:
            db._MIGRATIONS = old


if __name__ == "__main__":
    unittest.main()
