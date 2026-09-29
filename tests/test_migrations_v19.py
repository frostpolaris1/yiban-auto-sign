# -*- coding: utf-8 -*-
"""v19 迁移：`sign_claims` 补 fencing token 列 `epoch`（`sign_tasks.epoch` 兜底补齐）。

覆盖三件事：
- schema 落地：`epoch INTEGER NOT NULL DEFAULT 0`，存量行取默认 0（不可为 NULL——
  领取路径要拿它做算术，NULL 会让 `epoch = epoch + 1` 静默变 NULL）；
- 幂等：重跑不报错、不重复加列（迁移失败则版本不提升，下次启动整段重跑）；
- 兜底：`sign_tasks` 缺 `epoch` 时由本迁移补上（v18 若被回退，v19 仍是可用的护栏）；
- 登记口径：v19 是**核心迁移**（迁移 fail-closed 修复改判：`try_claim`/`claim_batch` 把 `epoch`
  当硬编列名，缺列则整条 v3 领取路径静默拒跑——失败必须阻断启动，不得只告警）。
  v20 仍为可选档（补的是台账数据，延后重试即可）。

标签：C · 存储：迁移与库完整性
覆盖：`sign_claims.epoch` 的加列（NOT NULL DEFAULT 0、存量行取默认）、整段重跑的幂等、
`sign_tasks` 缺列时的兜底补齐，以及 v19（核心）/v20（可选）在迁移登记里的档位。
对应实现：`yiban/store/` 的 v19 迁移函数与迁移登记表（`_registry_up_to` 按它取前缀）。
关键断言：`epoch` 必须 NOT NULL——领取路径要拿它做 `epoch = epoch + 1` 的算术，
NULL 会让整行静默变 NULL 而不是报错，这类"约束缺失"比"列缺失"更难发现，所以
`test_existing_rows_default_to_zero` 断的是存量行的实际值而非表定义文本；
`test_v20_is_registered_as_optional` 跨版本钉登记档位，改 v20 的档位会在这里红。
本文件只覆盖**新代码打开 v17 旧库**这一方向。
依赖：手工搭到 v17、不借 `db` 全局连接；临时库与临时 `.env`，无网络、无 skip。
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

    def test_v19_is_core(self):
        """核心迁移：epoch 是 try_claim 硬编列名，失败必须阻断启动（fail-closed 改判）。"""
        narrow = self._registry_up_to(19)
        self.assertEqual(narrow[-1][0], 19, "≤19 的登记尾项应是 v19")
        self.assertIs(narrow[-1][3], True)

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


class RegistrationTierBehaviorTest(_Base):
    """框架档位语义：登记表里可选档的条目失败只告警不阻断；核心档失败阻断。

    （v19 **本体**已是核心档——见 `SchemaTest.test_v19_is_core`；本类打桩的是登记表
    对该档位标志的通用处理，不是 v19 的登记值。）
    """

    def test_optional_tier_entry_failure_does_not_block_startup(self):
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

    def test_core_tier_entry_failure_blocks_startup(self):
        def failing_v19(conn):
            raise RuntimeError("boom")

        old = db._MIGRATIONS
        db._MIGRATIONS = [(19, "v19_failing", failing_v19, True)]
        try:
            with self.assertRaises(RuntimeError):
                db.init_db(self.db_file, env_file=self.env_file, cleanup=False)
            self.assertIsNone(db._conn, "核心失败必须复位连接（阻断启动）")
        finally:
            db._MIGRATIONS = old


if __name__ == "__main__":
    unittest.main()
