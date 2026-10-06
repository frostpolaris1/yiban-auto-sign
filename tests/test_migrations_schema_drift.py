# -*- coding: utf-8 -*-
"""迁移链的"版本说过了、schema 里没有"守卫（工单 ba-p01-02）。

缺陷形状：v6 的回填 UPDATE 失败时，自保 `conn.rollback()` 把同一事务里刚补出的
三列两索引一起吃掉；v6 不抛异常 ⇒ 框架走 bump 分支写记录、提 user_version ⇒
版本声称过了 v6 而产物不存在，此后 sign_events 的两条写入路径永久静默失败。

分条说明：
- `V6BackfillRollbackTest`：回填失败只回滚那条 UPDATE；列与索引必须存活；必须出声。
- `FrameworkCommitGateTest`：框架持事务期间迁移体不得提前落盘（原子性承诺）。
- `NoBareCommitGuardTest`：AST 扫 `migrate_*` 函数体，不得出现裸 conn.commit/rollback。
- `ArtifactGateTest`：user_version 过了某档，该档登记的产物必须存在（非核心档也算）。
- `EventWriteLoudTest`：写不进 sign_events 时告警必须能定位缺的产物，且不得抛出。

标签：C · 存储：迁移与库完整性
覆盖：v6 回填失败后的库态（列/索引/记录/版本）、迁移体内提前提交的消失、
链尾产物门对非核心档的可见性、events 单条与批量两条写入路径的出声口径。
对应实现：`yiban/store/migrations.py`（migrate_v6 的保存点、`_commit_if_free` 闸、
`_ARTIFACTS` 登记表与 `_verify_migration_integrity`）、`yiban/store/events.py`
（`add_sign_event` / `add_sign_events_batch` 的告警文本）。
关键断言：断言**库态**而不是日志格式——列在不在、索引在不在、user_version 是几、
记录有没有；日志只断言"点名了缺哪个产物"。旧库形态由用例手工 DROP 出来，
且把 user_version 退回 5，保证被测的 v6 真的重跑（不是被后续迁移补上）。
依赖：临时库/临时 .env/临时状态目录；SQLite 3.35+ 的 ALTER TABLE DROP COLUMN。
"""
import ast
import contextlib
import inspect
import os
import shutil
import sqlite3
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import db  # noqa: E402  (tests/conftest 注入 yiban/store 到 sys.path)

from yiban.store import migrations  # noqa: E402

TEST_KEY = "a" * 64
PHONE = "13800000001"

#: 链尾产物门必须登记的档：(版本号, 表, 列|None=只要表)。
#: 依据是各迁移里的建表/补列 DDL，逐档见 yiban/store/migrations.py。
REQUIRED_ARTIFACTS = (
    (4, "sign_events", None),
    (6, "sign_events", "account_id"),
    (6, "sign_events", "dur_sec"),
    (6, "sign_events", "finished_at"),
    (8, "session_cache", None),
    (8, "app_meta", None),
    (12, "app_meta", None),
    (15, "verify_jobs", None),
    (16, "verify_jobs", "prev_status"),
)


def _tables(conn):
    return {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def _indexes(conn):
    return {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}


def _cols(conn, table):
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def _user_version(conn):
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def _recorded_versions(conn):
    return {r[0] for r in conn.execute("SELECT version FROM schema_migrations").fetchall()}


def _close_global_conn():
    if db._conn is not None:
        with contextlib.suppress(Exception):
            db._conn.close()
        db._conn = None


class _DbTemp(unittest.TestCase):
    """每用例一套临时 库/.env/状态目录；单例连接在用例间重置。"""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="yiban-mig-drift-")
        self.db_file = os.path.join(self.root, "yiban.db")
        self.env_file = os.path.join(self.root, ".env")
        self.state_dir = os.path.join(self.root, "state")
        os.makedirs(self.state_dir)
        with open(self.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n")
        self._prev_env = {}
        for k, v in (("YIBAN_DB_FILE", self.db_file),
                     ("YIBAN_ENV_FILE", self.env_file),
                     ("YIBAN_ACCOUNTS_KEY", TEST_KEY),
                     ("YIBAN_STATE_DIR", self.state_dir)):
            self._prev_env[k] = os.environ.get(k)
            os.environ[k] = v
        _close_global_conn()
        self._migrations_backup = list(migrations._MIGRATIONS)

    def tearDown(self):
        _close_global_conn()
        migrations._MIGRATIONS = self._migrations_backup
        for k, v in self._prev_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.root, ignore_errors=True)

    def _init(self, upto=None):
        old = db._MIGRATIONS
        if upto is not None:
            db._MIGRATIONS = [m for m in old if m[0] <= upto]
        try:
            return db.init_db(self.db_file, env_file=self.env_file, cleanup=False)
        finally:
            db._MIGRATIONS = old

    def _reopen(self):
        conn = sqlite3.connect(self.db_file)
        conn.row_factory = sqlite3.Row
        return conn

    def _drop_column(self, conn, table, column):
        """连带删掉引用该列的索引后再 DROP COLUMN（SQLite 不允许删被索引的列）。"""
        for idx in conn.execute(f"PRAGMA index_list({table})").fetchall():
            name = idx["name"].replace("'", "''")
            used = {r["name"] for r in conn.execute(f"PRAGMA index_info('{name}')").fetchall()}
            if column in used:
                conn.execute(f'DROP INDEX IF EXISTS "{idx["name"]}"')
        conn.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
        conn.commit()


class V6BackfillRollbackTest(_DbTemp):
    """v6 的自保回滚只能回滚那条回填 UPDATE，不能吃自己的 DDL。"""

    def _legacy_db_ready_for_v6(self):
        """停在 v5 且 sign_events 缺 v6 三列的旧库（现网形态）。"""
        conn = self._init(upto=5)
        for col in ("account_id", "dur_sec", "finished_at"):
            self._drop_column(conn, "sign_events", col)
        conn.execute("INSERT INTO accounts (sort_order, name, phone, password, owner, status) "
                     "VALUES (1, 'A', ?, 'enc', 'admin', 'active')", (PHONE,))
        conn.execute("INSERT INTO sign_events (ts, phone, status) VALUES "
                     "('2026-09-01 07:00:00', ?, 'success')", (PHONE,))
        conn.commit()
        self.assertEqual(_user_version(conn), 5)
        return conn

    def _make_backfill_update_fail(self, conn):
        """给 sign_events 装一条必然失败的 UPDATE 触发器（模拟锁超时/磁盘满）。

        RAISE(ABORT) 只中止该条语句，外层事务与保存点都活着——即"保存点修法"能覆盖
        的那一半。
        """
        conn.execute(
            "CREATE TRIGGER force_backfill_fail BEFORE UPDATE ON sign_events "
            "BEGIN SELECT RAISE(ABORT, 'forced backfill failure'); END"
        )
        conn.commit()

    def _make_backfill_update_kill_transaction(self, conn):
        """让回填 UPDATE 以"SQLite 自动回滚**整条事务**"的方式失败。

        用进度回调返回非 0 触发 SQLITE_INTERRUPT：该错误码与磁盘满/IO 错误同属
        SQLite 会自动回滚整个事务的一族，保存点随之消失、保存点**之前**刚补的 DDL
        一并消失（实测：随后 `ROLLBACK TO SAVEPOINT` 报 no such savepoint）。
        只为命中 v6 那一条回填 UPDATE 而装：追踪回调见到该 SQL 才置位，命中一次后
        不再触发，后续迁移与收尾语句不受影响。
        """
        state = {"armed": False, "fired": False}

        def _trace(sql):
            if "UPDATE sign_events SET account_id" in sql:
                state["armed"] = True

        def _progress():
            if state["armed"] and not state["fired"]:
                state["fired"] = True
                return 1
            return 0

        conn.set_trace_callback(_trace)
        conn.set_progress_handler(_progress, 5)

    def test_backfill_failure_keeps_v6_columns(self):
        conn = self._legacy_db_ready_for_v6()
        self._make_backfill_update_fail(conn)
        migrations._run_migrations(conn)  # v6 不抛异常（可选档，失败只告警）
        self.assertIn("account_id", _cols(conn, "sign_events"),
                      "回填失败把刚补的 account_id 一起回滚了——版本却已提到 6")
        self.assertIn("dur_sec", _cols(conn, "sign_events"))
        self.assertIn("finished_at", _cols(conn, "sign_events"))

    def test_backfill_failure_keeps_v6_indexes(self):
        conn = self._legacy_db_ready_for_v6()
        self._make_backfill_update_fail(conn)
        migrations._run_migrations(conn)
        idx = _indexes(conn)
        self.assertIn("idx_sign_events_account_ts", idx)
        self.assertIn("idx_sign_events_phone_ts", idx)

    def test_backfill_failure_still_bumps_version_and_record(self):
        """DDL 存活 + 版本提到 ≥6 ⇒ 版本与 schema 不再脱节（这才是本单要的形状）。"""
        conn = self._legacy_db_ready_for_v6()
        self._make_backfill_update_fail(conn)
        migrations._run_migrations(conn)
        self.assertGreaterEqual(_user_version(conn), 6)
        self.assertIn(6, _recorded_versions(conn))

    def test_backfill_failure_is_loud_and_names_the_gap(self):
        conn = self._legacy_db_ready_for_v6()
        self._make_backfill_update_fail(conn)
        with self.assertLogs("yiban.store.migrations", level="WARNING") as logs:
            migrations._run_migrations(conn)
        text = "\n".join(logs.output)
        self.assertIn("sign_events", text)
        self.assertIn("account_id", text)
        self.assertIn("forced backfill failure", text)

    def test_transaction_auto_rollback_defers_v6_instead_of_bumping(self):
        """整条事务被 SQLite 自动回滚时：v6 必须延后，不得提版本、不得写记录。

        只做 `ROLLBACK TO SAVEPOINT` 不够——该错误族让保存点连同它之前刚补的列一起
        消失，此时迁移若正常返回，框架照走 bump 分支 ⇒ 重演"版本说过了 v6、schema
        里没有它补的列"（本单要消灭的形态，且是工单点名的"磁盘满"那一半）。
        """
        conn = self._legacy_db_ready_for_v6()
        self._make_backfill_update_kill_transaction(conn)
        migrations._run_migrations(conn)
        self.assertNotIn("account_id", _cols(conn, "sign_events"),
                         "前置事实：这一族错误让 SQLite 整体回滚，刚补的列随之消失")
        self.assertLess(_user_version(conn), 6,
                        "产物已不在库里时不得提升版本——否则重演版本与 schema 脱节")
        self.assertNotIn(6, _recorded_versions(conn),
                         "产物已不在库里时不得写 v6 完成记录")

    def test_backfill_success_path_still_backfills(self):
        """正向对照：没有故障时回填照旧生效（修法不得把回填本身关掉）。"""
        conn = self._init(upto=5)
        for col in ("account_id", "dur_sec", "finished_at"):
            self._drop_column(conn, "sign_events", col)
        conn.execute("INSERT INTO accounts (sort_order, name, phone, password, owner, status) "
                     "VALUES (1, 'A', ?, 'enc', 'admin', 'active')", (PHONE,))
        conn.execute("INSERT INTO sign_events (ts, phone, status) VALUES "
                     "('2026-09-01 07:00:00', ?, 'success')", (PHONE,))
        conn.commit()
        migrations._run_migrations(conn)
        row = conn.execute("SELECT account_id FROM sign_events").fetchone()
        self.assertEqual(row["account_id"], 1, "回填必须把 phone 对应的 account_id 补上")


class FrameworkCommitGateTest(_DbTemp):
    """框架持事务期间，迁移体不得提前落盘——否则半途失败留下已提交的半段 schema。"""

    def test_v4_ddl_does_not_survive_a_failure_inside_the_same_migration(self):
        """原子性的作用域是**单个迁移**：迁移体抛错 ⇒ 它自己刚建的表整体回滚。

        旧写法里 migrate_v4 末尾那句裸 `conn.commit()` 会先关掉框架的 BEGIN
        IMMEDIATE，于是抛错之后半段 schema 已经落盘。
        """
        self._init(upto=3)
        _close_global_conn()

        def v4_then_boom(conn):
            migrations.migrate_v4(conn)
            raise RuntimeError("boom inside v4")

        migrations._MIGRATIONS = [(4, "v4_then_boom", v4_then_boom, True)]
        with self.assertRaises(RuntimeError):
            db.init_db(self.db_file, env_file=self.env_file, cleanup=False)
        fresh = self._reopen()
        try:
            self.assertNotIn("sign_events", _tables(fresh),
                             "v4 体内的裸 conn.commit() 让半途 schema 落盘，原子性承诺落空")
            self.assertNotEqual(_user_version(fresh), 4, "失败的迁移不得提升版本")
        finally:
            fresh.close()

    def test_direct_call_migration_still_commits(self):
        """直调语义保持不变：登记表之外直接调迁移函数时，迁移自己提交。"""
        conn = self._init(upto=3)
        migrations.migrate_v4(conn)
        fresh = self._reopen()
        try:
            self.assertIn("sign_events", _tables(fresh))
        finally:
            fresh.close()


class NoBareCommitGuardTest(unittest.TestCase):
    """结构守卫：migrate_* 函数体内不得出现裸 conn.commit()/conn.rollback()。"""

    def _bypassing_calls(self):
        tree = ast.parse(inspect.getsource(migrations))
        bad = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef) or not node.name.startswith("migrate_"):
                continue
            for inner in ast.walk(node):
                if not isinstance(inner, ast.Call) or not isinstance(inner.func, ast.Attribute):
                    continue
                base = inner.func.value
                if isinstance(base, ast.Name) and base.id == "conn" \
                        and inner.func.attr in ("commit", "rollback"):
                    bad.append(f"{node.name}:{inner.lineno} conn.{inner.func.attr}()")
        return bad

    def test_migration_bodies_have_no_bare_conn_commit(self):
        self.assertEqual(self._bypassing_calls(), [],
                         "迁移体内的裸 conn.commit()/rollback() 绕开 _commit_if_free 的闸")


class ArtifactGateTest(_DbTemp):
    """链尾 fail-closed 门：登记了产物的档，无论核心/可选，产物缺失一律拒启。"""

    def test_missing_artifact_refuses_startup(self):
        for version, table, column in REQUIRED_ARTIFACTS:
            with self.subTest(version=version, table=table, column=column):
                root = tempfile.mkdtemp(prefix="yiban-mig-gate-")
                try:
                    conn = self._full_chain_in(root)
                    if column is None:
                        conn.execute(f"DROP TABLE IF EXISTS {table}")
                    else:
                        self._drop_column(conn, table, column)
                    conn.commit()
                    with self.assertRaises(migrations.MigrationIntegrityError) as cm:
                        migrations._run_migrations(conn)
                    text = str(cm.exception)
                    self.assertIn(table, text, "错误文本必须点名缺失的表")
                    if column is not None:
                        self.assertIn(column, text, "错误文本必须点名缺失的列")
                    conn.close()
                finally:
                    shutil.rmtree(root, ignore_errors=True)

    def test_new_artifacts_refuse_a_pre_existing_v6_drift(self):
        """本单那枚缺陷的兜底：v6 产物缺失时，门必须点名 v6（不是只有 v17/v19 可见）。"""
        root = tempfile.mkdtemp(prefix="yiban-mig-gate-")
        try:
            conn = self._full_chain_in(root)
            self._drop_column(conn, "sign_events", "account_id")
            with self.assertRaises(migrations.MigrationIntegrityError) as cm:
                migrations._run_migrations(conn)
            self.assertIn("v6", str(cm.exception))
            conn.close()
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_healthy_full_chain_passes_the_gate(self):
        """对照组：正常库不得被收紧的门拒起（否则每次合法重启都要人工干预）。"""
        root = tempfile.mkdtemp(prefix="yiban-mig-gate-")
        try:
            conn = self._full_chain_in(root)
            migrations._run_migrations(conn)  # 不抛即通过
            self.assertGreaterEqual(_user_version(conn),
                                    max(m[0] for m in migrations._MIGRATIONS))
            conn.close()
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def _full_chain_in(self, root):
        db_file = os.path.join(root, "yiban.db")
        env_file = os.path.join(root, ".env")
        with open(env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n")
        os.environ["YIBAN_DB_FILE"] = db_file
        os.environ["YIBAN_ENV_FILE"] = env_file
        os.environ["YIBAN_STATE_DIR"] = os.path.join(root, "state")
        os.makedirs(os.environ["YIBAN_STATE_DIR"])
        _close_global_conn()
        return db.init_db(db_file, env_file=env_file, cleanup=False)


class EventWriteLoudTest(_DbTemp):
    """写不进 sign_events 时：告警必须可定位，且绝不打断签到主流程。"""

    def _db_missing_v6_column(self):
        conn = self._init()
        self._drop_column(conn, "sign_events", "account_id")
        return conn

    def test_single_write_failure_names_the_missing_artifact(self):
        self._db_missing_v6_column()
        with self.assertLogs("yiban.store.events", level="WARNING") as logs:
            db.add_sign_event("2026-10-06 07:00:00", PHONE, "success", stage="sign")
        text = "\n".join(logs.output)
        self.assertIn("sign_events", text)
        self.assertIn("account_id", text)
        self.assertIn("v6", text, "必须点出缺失产物属于哪一档迁移")

    def test_batch_write_failure_reports_dropped_row_count(self):
        self._db_missing_v6_column()
        rows = [{"ts": "2026-10-06 07:00:00", "phone": PHONE, "status": "success",
                 "stage": "sign"}] * 3
        with self.assertLogs("yiban.store.events", level="WARNING") as logs:
            db.add_sign_events_batch(rows)
        text = "\n".join(logs.output)
        self.assertIn("sign_events", text)
        self.assertIn("v6", text)
        self.assertIn("3 条", text, "批量路径必须报出丢了几个事件")

    def test_write_failures_never_escape_to_the_signing_flow(self):
        self._db_missing_v6_column()
        self.assertIsNone(db.add_sign_event("2026-10-06 07:00:00", PHONE, "success"))
        self.assertIsNone(db.add_sign_events_batch(
            [{"ts": "2026-10-06 07:00:00", "phone": PHONE, "status": "success"}]))

    def test_phones_are_not_written_into_the_warning(self):
        self._db_missing_v6_column()
        with self.assertLogs("yiban.store.events", level="WARNING") as logs:
            db.add_sign_event("2026-10-06 07:00:00", PHONE, "success", stage="sign")
        self.assertNotIn(PHONE, "\n".join(logs.output), "告警里不得出现手机号明文")


if __name__ == "__main__":
    unittest.main()
