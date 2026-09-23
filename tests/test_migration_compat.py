# -*- coding: utf-8 -*-
"""升级兼容硬门：旧代码打开新库（零写入）与「既有表零变更」。

两条主张各一个用例：

1. **旧代码无感**（`docs/dev/m1-task-rearrangement-20260923.md` §7 的 I-5）：库的
   `user_version` 高于本进程迁移顶时，`_run_migrations` 整链跳过——不抛异常、不执行
   任何迁移项、不降版本。回滚路径（换回旧代码、库不动）就建立在这条性质上。
2. **既有表零变更**：完整迁移链（0→20）跑完后，`accounts`/`users`/`sign_claims`/
   `time_prefs` 逐表行数相等，`sign_claims` 的列集合只多出一个 `epoch`。

库是手工搭的（`_create_tables` + v17 建 `sign_claims`），不借 `db` 的全局连接。
"""
import contextlib
import os
import shutil
import sqlite3
import tempfile
import unittest

from yiban.store import migrations

#: 旧版本（生产 v0.4.7）的迁移顶——「旧代码」的迁移集就是 ≤ 它的那些项。
_OLD_CODE_SCHEMA_TOP = 17

_SEEDED_TABLES = ("accounts", "users", "sign_claims", "time_prefs")


class MigrationCompatTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="yiban-migcompat-")
        self.state_dir = os.path.join(self.root, "state")
        os.makedirs(self.state_dir)
        self._prev_state_dir = os.environ.get("YIBAN_STATE_DIR")
        os.environ["YIBAN_STATE_DIR"] = self.state_dir
        self.conn = sqlite3.connect(os.path.join(self.root, "yiban.db"))
        self.conn.row_factory = sqlite3.Row

    def tearDown(self):
        with contextlib.suppress(Exception):
            self.conn.close()
        if self._prev_state_dir is None:
            os.environ.pop("YIBAN_STATE_DIR", None)
        else:
            os.environ["YIBAN_STATE_DIR"] = self._prev_state_dir
        shutil.rmtree(self.root, ignore_errors=True)

    # ---- 夹具 ----
    def _count(self, table):
        return self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def _columns(self, table):
        return {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}

    def _user_version(self):
        return self.conn.execute("PRAGMA user_version").fetchone()[0]

    def _seed_production_like(self):
        """生产形态的升级前库：基线五表 + 领取池，各表都有存量行。"""
        migrations._create_tables(self.conn)
        self.conn.executemany(
            "INSERT INTO accounts (sort_order, name, phone, password, status, owner) "
            "VALUES (?,?,?,?,?,?)",
            [(i, f"账号{i}", f"1380000000{i}", "ct", "active", f"owner{i}")
             for i in range(1, 4)],
        )
        self.conn.executemany(
            "INSERT INTO users (email, password_hash, role, created_at) VALUES (?,?,?,?)",
            [("a@example.com", "h", "admin", "2026-09-01 00:00:00"),
             ("b@example.com", "h", "user", "2026-09-01 00:00:00")],
        )
        migrations.migrate_v17(self.conn)
        self.conn.executemany(
            "INSERT INTO sign_claims (phone, day, owner, claimed_at, heartbeat_at, "
            "state, result, attempts) VALUES (?,?,?,?,?,?,?,?)",
            [(f"1380000000{i}", "2026-09-20", "worker-0@host",
              "2026-09-20 06:31:00", "2026-09-20 06:31:30", "done", "签到成功", 1)
             for i in range(1, 4)],
        )
        self.conn.execute(
            "INSERT INTO time_prefs (phone, slot_min, updated_at) VALUES (?,?,?)",
            ("13800000001", 400, "2026-09-20 00:00:00"),
        )
        self.conn.commit()

    # ---- 1. 旧代码冒烟 ----
    def test_old_code_smoke_is_a_noop_on_a_higher_version_db(self):
        self._seed_production_like()
        migrations._run_migrations(self.conn)          # 0 → 20
        self.assertEqual(self._user_version(), 20)
        # 此刻起模拟"旧代码（迁移顶 v17）打开升过级的库"：迁移项换成旧集合 + 炸弹体
        calls = []

        def _bomb(_conn):
            calls.append(1)
            raise AssertionError("版本门控失效：对更高版本的库执行了迁移")

        old_registry = [(v, n, _bomb, core)
                        for (v, n, _fn, core) in migrations._MIGRATIONS
                        if v <= _OLD_CODE_SCHEMA_TOP]
        before_changes = self.conn.total_changes
        saved = migrations._MIGRATIONS
        migrations._MIGRATIONS = old_registry
        try:
            migrations._run_migrations(self.conn)      # 不得抛
        finally:
            migrations._MIGRATIONS = saved
        self.assertEqual(calls, [], "旧代码不得执行任何迁移项")
        self.assertEqual(self.conn.total_changes, before_changes, "旧代码不得写入任何行")
        self.assertEqual(self._user_version(), 20, "版本不得被降或被抬")

    def test_run_migrations_is_a_noop_when_version_is_already_at_top(self):
        self._seed_production_like()
        migrations._run_migrations(self.conn)
        counts = {t: self._count(t) for t in ("accounts", "users", "sign_claims",
                                              "sign_tasks", "time_prefs")}
        before_changes = self.conn.total_changes
        migrations._run_migrations(self.conn)
        self.assertEqual({t: self._count(t) for t in counts}, counts)
        self.assertEqual(self.conn.total_changes, before_changes)
        self.assertEqual(self._user_version(), 20)

    # ---- 2. 既有表零变更 ----
    def test_full_chain_leaves_existing_tables_untouched(self):
        self._seed_production_like()
        before = {t: self._count(t) for t in _SEEDED_TABLES}
        cols_before = self._columns("sign_claims")
        migrations._run_migrations(self.conn)
        self.assertEqual(self._user_version(), 20)
        self.assertEqual({t: self._count(t) for t in _SEEDED_TABLES}, before,
                         "既有表行数必须逐表不变")
        self.assertEqual(self._columns("sign_claims"), cols_before | {"epoch"},
                         "sign_claims 只允许新增 epoch 一列")
        self.assertEqual(self._count("sign_tasks"), before["sign_claims"],
                         "平移行数应等于平移时点的 sign_claims 行数")


if __name__ == "__main__":
    unittest.main()
