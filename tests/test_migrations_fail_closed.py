# -*- coding: utf-8 -*-
"""迁移 fail-closed（MF-40）：分级拒绝启动、迁移记录表、断点重跑、tmp 权限、守恒断言。

验收不变量逐条（task-5 brief）：
- ① 迁移链跑完后 `epoch` 仍缺失 ⇒ 非零退出并点名缺哪条迁移（`ArtifactIntegrityTest`
  + `StartupExitCodeTest` 的 rc4/rc1 反例对）；
- ② 每条迁移跑完断言"行数守恒 + 版本已提升"（`ConservationTest`，守卫的失败分支用
  must-fail 桩直接证）；
- ③ 迁移中途 kill 的残留（users_new 半程表）必须能重跑收敛（`CrashResumeTest`）；
- ④ tmp 文件权限先于内容、失败无明文残留（`TmpPermissionTest`，含"写内容时已是 0600"
  的过程断言与"replace 失败不留 tmp"的反例）；
- ⑤ v20 状态目录不可读 ⇒ 版本不提升 + 告警（`V20UnreadableDirTest`；目录缺失仍按
  "无状态"放行的既有语义同步钉住）；
- ⑥ v17 → 全链成功路径保持绿（`StartupExitCodeTest` 的健康库对照组 +
  `ConservationTest` 的全链版本推进）。

另覆盖：登记档位改判（v17/v18/v19 核心、v20 可选）、`schema_migrations` 记录表
（提升与记录同事务、blocked 不记、存量库继承回填、记录缺失拒启）、
`BEGIN IMMEDIATE` 提前 commit 修复（框架内不提前落盘 / 直调仍提交）、
v18/v20 惰性标记行不再永久挤占当日真实计划（`write_plan` 显式接管同键标记行）、
SQL 标识符转义（v5 含单引号的索引名、v13 含双引号的表名）。

标签：C · 存储：迁移与库完整性
对应实现：`yiban/store/migrations.py`（分级/记录/校验/原子性/守恒/权限）、
`yiban/engine/runner.py` + `workers.py`（拒启退出码 4）、`yiban/engine/planner.py`
（计划接管）、`tests/test_cli_contract.py` 同族子进程口径。
依赖：临时库/临时目录/子进程，无网络；chmod 用例在无 euid==0 前提跑（root 无视权限位）。
"""
import contextlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import timedelta
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import db  # noqa: E402  (tests/conftest 注入 yiban/store 到 sys.path)

from yiban import clock  # noqa: E402
from yiban.engine import planner  # noqa: E402
from yiban.infra import account_crypto  # noqa: E402
from yiban.store import migrations, queue_store  # noqa: E402

TEST_KEY = "a" * 64
PHONE_A = "13800000001"
PHONE_B = "13800000002"
DAY = "2026-09-22"


def _day(offset=0):
    return (clock.now() - timedelta(days=offset)).strftime("%Y-%m-%d")


def _table_names(conn):
    return {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def _cols(conn, table):
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def _user_version(conn):
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def _close_global_conn():
    if db._conn is not None:
        with contextlib.suppress(Exception):
            db._conn.close()
        db._conn = None


class _DbTemp(unittest.TestCase):
    """每用例一套临时 库/.env/状态目录；进程内不共享单例连接（setUp 均断开重开）。"""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="yiban-mig-fc-")
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
        """跑框架建库；upto 非空时把登记表缩窄到该版本（模拟"停在 vN"的存量库）。"""
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


# ---------------------------------------------------------------------------
# 登记档位（修法 1）
# ---------------------------------------------------------------------------
class RegistryClassificationTest(_DbTemp):
    def test_claim_path_migrations_are_core(self):
        """v17/v18/v19 是 v3 领取路径硬前置 ⇒ 核心档；v20 补账保持可选（延后可重试）。"""
        flags = {m[0]: m[3] for m in migrations._MIGRATIONS}
        self.assertTrue(flags[17], "v17 sign_claims 领取池必须为核心")
        self.assertTrue(flags[18], "v18 sign_tasks 队列必须为核心")
        self.assertTrue(flags[19], "v19 epoch 是 try_claim 硬编列名，必须为核心")
        self.assertFalse(flags[20], "v20 只补台账数据，失败延后即可，不得拒启")

    def test_core_failure_blocks_and_cleans(self):
        def failing(conn):
            raise RuntimeError("boom")

        db._MIGRATIONS = [(19, "v19_failing", failing, True)]
        with self.assertRaises(RuntimeError):
            db.init_db(self.db_file, env_file=self.env_file, cleanup=False)
        self.assertIsNone(db._conn, "核心迁移失败必须关连接复位（阻断启动）")

    def test_optional_failure_still_passes(self):
        def failing(conn):
            raise RuntimeError("boom")

        db._MIGRATIONS = [(19, "v19_failing", failing, False)]
        conn = db.init_db(self.db_file, env_file=self.env_file, cleanup=False)
        self.assertIsNotNone(conn)
        self.assertEqual(_user_version(conn), 0)


# ---------------------------------------------------------------------------
# 迁移记录表（修法 2）：提升与记录同事务；缺记录 ⇒ 拒启
# ---------------------------------------------------------------------------
class MigrationRecordTest(_DbTemp):
    def test_applied_migrations_get_records(self):
        conn = self._init()
        rows = {r["version"]: r["name"] for r in
                conn.execute("SELECT version, name FROM schema_migrations").fetchall()}
        self.assertEqual(_user_version(conn), max(m[0] for m in migrations._MIGRATIONS))
        for target, name, _fn, _core in migrations._MIGRATIONS:
            self.assertIn(target, rows, f"已提升过 v{target} 必须有完成记录")
            self.assertEqual(rows[target], name)

    def test_blocked_migration_is_not_recorded(self):
        """可选迁移失败：改动落地但版本不提升 ⇒ 也不得有记录（下次启动整段重跑）。"""
        def failing(conn):
            raise RuntimeError("boom")

        self._init(upto=17)
        _close_global_conn()  # 强制重开：否则 init_db 直接返回既有单例、不再跑迁移
        db._MIGRATIONS = [(t, n, f, c) for t, n, f, c in self._migrations_backup
                          if t <= 17] + [(18, "v18_failing", failing, False)]
        conn2 = db.init_db(self.db_file, env_file=self.env_file, cleanup=False)
        self.assertEqual(_user_version(conn2), 17, "blocked 期间不得提升版本")
        versions = {r[0] for r in conn2.execute(
            "SELECT version FROM schema_migrations").fetchall()}
        self.assertNotIn(18, versions, "失败未提升的迁移不得有完成记录")
        self.assertTrue(conn2.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version<=17").fetchone()[0])

    def test_inherited_db_without_table_boots(self):
        """红线：存量库（旧链升上来的，无记录表）首次撞上新代码 ⇒ 继承回填、照常启动。"""
        conn = self._init()
        conn.execute("DROP TABLE schema_migrations")
        conn.commit()
        _close_global_conn()
        conn2 = db.init_db(self.db_file, env_file=self.env_file, cleanup=False)
        versions = {r[0] for r in conn2.execute(
            "SELECT version FROM schema_migrations").fetchall()}
        self.assertLessEqual({17, 18, 19, 20}, versions)

    def test_missing_record_refuses_startup_naming_migration(self):
        """记录被抹 ⇒ 版本推进与迁移成功脱钩的反例 ⇒ 拒启并点名。"""
        conn = self._init()
        conn.execute("DELETE FROM schema_migrations WHERE version=18")
        conn.commit()
        with self.assertRaises(migrations.MigrationIntegrityError) as cm:
            migrations._run_migrations(conn)
        self.assertIn("v18", str(cm.exception))


# ---------------------------------------------------------------------------
# ① 产物校验：链跑完仍缺 epoch ⇒ 拒启点名
# ---------------------------------------------------------------------------
class ArtifactIntegrityTest(_DbTemp):
    def _drifted_v19_db(self):
        """真实"停在 v19"的库：链跑到 v18 后手工把版本拨到 19（绕过 v19）⇒
        sign_claims 无 epoch，而 user_version 声称已过 v19。"""
        conn = self._init(upto=18)
        conn.execute("PRAGMA user_version = 19")
        conn.commit()
        _close_global_conn()
        return conn

    def test_missing_epoch_refuses_and_names_v19(self):
        self._drifted_v19_db()
        conn = sqlite3.connect(self.db_file)
        conn.row_factory = sqlite3.Row
        self.assertNotIn("epoch", _cols(conn, "sign_claims"))
        with self.assertRaises(migrations.MigrationIntegrityError) as cm:
            migrations._run_migrations(conn)
        msg = str(cm.exception)
        self.assertIn("v19", msg, "必须点名缺失的迁移版本")
        self.assertIn("sign_claims", msg)
        self.assertIn("epoch", msg)
        conn.close()

    def test_epoch_dropped_after_full_chain_refuses(self):
        """MF-91 反例：全链跑完后有人把 epoch 列抹掉 ⇒ 下次启动拒启（点名 v19）。"""
        conn = self._init()
        self.assertIn("epoch", _cols(conn, "sign_claims"))
        conn.execute("ALTER TABLE sign_claims DROP COLUMN epoch")
        conn.commit()
        with self.assertRaises(migrations.MigrationIntegrityError) as cm:
            migrations._run_migrations(conn)
        self.assertIn("v19", str(cm.exception))

    def test_stopped_v17_db_passes_verification(self):
        """红线：库仍停在 v17（v18+ 未应用）⇒ 产物校验不得拿未应用版本的产物要人。"""
        conn = self._init(upto=17)
        migrations._run_migrations(conn)  # 不抛即通过；v18+ 仍会被正常应用
        self.assertGreaterEqual(_user_version(conn), 18)


# ---------------------------------------------------------------------------
# ② 守恒 + 版本推进
# ---------------------------------------------------------------------------
class ConservationTest(_DbTemp):
    def test_full_chain_preserves_rows_and_bumps_version(self):
        conn = self._init(upto=17)
        conn.execute(
            "INSERT INTO accounts (sort_order, name, phone, password, owner, status) "
            "VALUES (1, 'A', ?, 'enc', 'admin', 'active')", (PHONE_A,))
        conn.execute(
            "INSERT INTO users (email, password_hash, role, created_at, pw_version) "
            "VALUES ('u@test.local', 'h', 'user', '2026-09-01 00:00:00', 1)")
        conn.execute(
            "INSERT INTO sign_claims (phone, day, owner, claimed_at, heartbeat_at, "
            "state, result, attempts) VALUES (?,?,?,?,?,?,?,?)",
            (PHONE_A, DAY, "hostA:1:090000", f"{DAY} 07:00:01", f"{DAY} 07:00:11",
             "done", "ok", 1))
        conn.commit()
        before = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in ("accounts", "users", "audit_logs", "sign_claims")}
        migrations._run_migrations(conn)
        after = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                 for t in before}
        self.assertEqual(before, after, "全链迁移不得增删既有表的行")
        self.assertEqual(_user_version(conn),
                         max(m[0] for m in migrations._MIGRATIONS))
        self.assertIn("epoch", _cols(conn, "sign_claims"))
        self.assertIn("epoch", _cols(conn, "sign_tasks"))

    def test_rebuild_guard_fires_on_row_loss(self):
        """must-fail 桩：重建后行数不符 ⇒ 守卫抛错（迁移失败），绝不静默吞。"""
        class _Stub:
            def execute(self, sql):
                class _R:
                    def fetchone(self):
                        return (41,)  # 期望 40，重建丢了 1 行

                    def fetchall(self):
                        return [{"name": "id"}, {"name": "email"}]
                return _R()

        with self.assertRaises(RuntimeError) as cm:
            migrations._assert_rebuild_preserved(
                _Stub(), "users", expected_count=40, expected_cols={"id", "email"})
        self.assertIn("users", str(cm.exception))

    def test_rebuild_guard_fires_on_column_loss(self):
        """must-fail 桩：重建后列集不覆盖重建前列集 ⇒ 抛错点名缺失列。"""
        class _Stub:
            def execute(self, sql):
                class _R:
                    def fetchone(self):
                        return (40,)

                    def fetchall(self):
                        return [{"name": "id"}]  # 丢了 email
                return _R()

        with self.assertRaises(RuntimeError) as cm:
            migrations._assert_rebuild_preserved(
                _Stub(), "users", expected_count=40, expected_cols={"id", "email"})
        self.assertIn("email", str(cm.exception))


# ---------------------------------------------------------------------------
# ③ 半程崩溃残留可重跑
# ---------------------------------------------------------------------------
class CrashResumeTest(_DbTemp):
    def test_users_new_leftover_resumes(self):
        """v5 中途 kill 的残留（users_new 建了没改名）：重跑必须先 DROP 再重建。"""
        conn = self._init(upto=4)
        conn.execute("CREATE UNIQUE INDEX uniq_email ON users(email)")
        conn.execute(
            "CREATE TABLE users_new ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT NOT NULL, "
            "password_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'user', "
            "created_at TEXT NOT NULL DEFAULT '', pw_version INTEGER NOT NULL DEFAULT 1, "
            "deleted INTEGER NOT NULL DEFAULT 0, deleted_at TEXT NOT NULL DEFAULT '')")
        conn.execute(
            "INSERT INTO users_new (id, email, password_hash, role, created_at, "
            "pw_version) SELECT id, email, password_hash, role, created_at, "
            "pw_version FROM users")
        conn.commit()
        _close_global_conn()

        conn2 = db.init_db(self.db_file, env_file=self.env_file, cleanup=False)
        self.assertEqual(_user_version(conn2),
                         max(m[0] for m in migrations._MIGRATIONS),
                         "半程残留必须可重跑，不得永久阻断启动")
        self.assertNotIn("users_new", _table_names(conn2))
        self.assertIn("deleted", _cols(conn2, "users"))
        self.assertEqual(conn2.execute("SELECT COUNT(*) FROM users").fetchone()[0], 0)


# ---------------------------------------------------------------------------
# 修法 7：BEGIN IMMEDIATE 不得被内部 commit 提前结束
# ---------------------------------------------------------------------------
class AtomicityTest(_DbTemp):
    def test_framework_migration_rolls_back_wholly(self):
        """框架内迁移：中途 _ensure_column 已落列后抛错 ⇒ 整体回滚，半途列不得持久化。"""
        self._init(upto=5)
        _close_global_conn()  # 重开才会在既有库上跑打桩迁移

        def add_then_boom(c):
            migrations._ensure_column(c, "accounts", "atom_probe", "INTEGER NOT NULL DEFAULT 0")
            raise RuntimeError("boom")

        db._MIGRATIONS = [(6, "atom_probe", add_then_boom, True)]
        with self.assertRaises(RuntimeError):
            db.init_db(self.db_file, env_file=self.env_file, cleanup=False)
        fresh = self._reopen()
        try:
            self.assertNotIn("atom_probe", _cols(fresh, "accounts"),
                             "提前 conn.commit() 会把半途 ALTER 落盘——原子性承诺要求回滚")
        finally:
            fresh.close()

    def test_direct_helper_still_commits(self):
        """登记表之外直调（旧语义保持）：_ensure_column 仍然自带提交，另开连接可见。"""
        conn = self._init(upto=5)
        migrations._ensure_column(conn, "accounts", "direct_probe", "INTEGER")
        conn.commit()
        fresh = self._reopen()
        try:
            self.assertIn("direct_probe", _cols(fresh, "accounts"))
        finally:
            fresh.close()


# ---------------------------------------------------------------------------
# ④ tmp 权限先于内容、失败无残留
# ---------------------------------------------------------------------------
class TmpPermissionTest(_DbTemp):
    def _plaintext_json(self):
        path = os.path.join(self.root, "accounts.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump([{"name": "A", "phone": PHONE_A, "password": "hunter2-pwd"}], f)
        return path

    def _key(self):
        return account_crypto.load_key(self.env_file)

    def test_reencrypt_bak_is_0600_and_no_tmp_leftover(self):
        path = self._plaintext_json()
        key = self._key()
        migrations._rename_backup(path, reencrypt=True, key=key)
        bak = [p for p in os.listdir(self.root) if p.startswith("accounts.json.bak")]
        self.assertEqual(len(bak), 1)
        bak_path = os.path.join(self.root, bak[0])
        mode = os.stat(bak_path).st_mode & 0o777
        self.assertEqual(mode & 0o077, 0, f".bak 必须仅属主可读写，实际 {oct(mode)}")
        self.assertEqual([p for p in os.listdir(self.root) if ".tmp" in p], [],
                         "重写完成后不得残留 tmp")
        with open(bak_path, encoding="utf-8") as f:
            data = json.load(f)
        self.assertNotIn("hunter2-pwd", json.dumps(data), "明文凭据不得驻留 .bak")

    def test_tmp_created_0600_before_content(self):
        """过程反例：往 tmp 写第一笔内容时权限就必须已是 0600（先权限后内容）。"""
        path = self._plaintext_json()
        key = self._key()
        seen = {}
        real_dump = json.dump

        def spy_dump(obj, fh, *a, **kw):
            seen["mode"] = os.fstat(fh.fileno()).st_mode & 0o077
            return real_dump(obj, fh, *a, **kw)

        with mock.patch.object(migrations.json, "dump", side_effect=spy_dump):
            migrations._rename_backup(path, reencrypt=True, key=key)
        self.assertEqual(seen.get("mode"), 0,
                         "json.dump 开写时 tmp 仍组/其他可读 ⇒ umask 竞态未修")

    def test_failed_rewrite_leaves_no_plaintext_tmp(self):
        """replace 失败 ⇒ tmp（含重写中的明文/密文）必须被清理，不得永久残留。"""
        path = self._plaintext_json()
        key = self._key()
        with mock.patch.object(migrations.os, "replace", side_effect=OSError("disk")):
            migrations._rename_backup(path, reencrypt=True, key=key)
        leftovers = [p for p in os.listdir(self.root) if ".tmp" in p]
        self.assertEqual(leftovers, [], f"失败路径残留了 tmp: {leftovers}")

    def test_plain_rename_bak_is_0600(self):
        path = self._plaintext_json()
        os.chmod(path, 0o644)
        migrations._rename_backup(path)
        bak = [p for p in os.listdir(self.root) if p.startswith("accounts.json.bak")]
        self.assertEqual(len(bak), 1)
        mode = os.stat(os.path.join(self.root, bak[0])).st_mode & 0o077
        self.assertEqual(mode, 0, f"改名后的 .bak 应收到 0600，实际组/其他位 {oct(mode)}")


# ---------------------------------------------------------------------------
# ⑤ v20 状态目录不可读 ⇒ 版本不提升 + 告警
# ---------------------------------------------------------------------------
class V20UnreadableDirTest(_DbTemp):
    def _v19_db(self):
        conn = self._init(upto=19)
        return conn

    def test_unreadable_dir_defers_without_version_bump(self):
        """注入"目录在而读不动"（EACCES）——root 环境权限位不设防，用 must-fail 桩。"""
        conn = self._v19_db()
        self.assertEqual(_user_version(conn), 19)
        with mock.patch.object(migrations.os, "listdir",
                               side_effect=PermissionError(13, "denied")):
            with self.assertRaises(migrations.MigrationDeferred):
                migrations.migrate_v20(conn)
            with self.assertLogs("yiban.store.migrations", level="WARNING") as logs:
                migrations._run_migrations(conn)
        self.assertEqual(_user_version(conn), 19,
                         "状态目录不可读时 v20 不得被记为成功（版本不提升）")
        out = "\n".join(logs.output)
        self.assertIn("v20", out)

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0,
                     "root 无视权限位，chmod 反例不成立")
    def test_unreadable_dir_real_chmod_defers(self):
        """真反例（非 root CI）：chmod 000 的目录 ⇒ v20 延后、版本停在 19。"""
        conn = self._v19_db()
        os.chmod(self.state_dir, 0o000)
        try:
            with self.assertLogs("yiban.store.migrations", level="WARNING"):
                migrations._run_migrations(conn)
            self.assertEqual(_user_version(conn), 19)
        finally:
            os.chmod(self.state_dir, 0o755)

    def test_absent_dir_still_passes_as_no_state(self):
        """既有语义保持：目录不存在 = 无状态可补（全新部署常态），放行并提升版本。"""
        conn = self._v19_db()
        os.environ["YIBAN_STATE_DIR"] = os.path.join(self.root, "absent")
        self.assertEqual(migrations.migrate_v20(conn), 0)
        migrations._run_migrations(conn)
        self.assertEqual(_user_version(conn),
                         max(m[0] for m in migrations._MIGRATIONS))


# ---------------------------------------------------------------------------
# 修法 4：惰性标记行不得永久挤占当日真实计划
# ---------------------------------------------------------------------------
class MarkerRecoveryTest(_DbTemp):
    def test_write_plan_replaces_same_day_marker_and_row_becomes_claimable(self):
        conn = self._init()
        today = _day()
        conn.execute(
            "INSERT INTO sign_tasks (phone, day, vshard, owner, run_at, priority, "
            "state, attempts, lease_until, result, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (PHONE_A, today, -1, "backfill", f"{today} 00:00:00", 5, "failed",
             0, "", "", f"{today} 00:00:00"))
        conn.commit()
        plan_row = {"phone": PHONE_A, "day": today, "vshard": 7, "owner": "w1@host",
                    "run_at": f"{today} 07:00:00", "priority": 5, "state": "pending"}
        written = planner.write_plan([plan_row], today)
        self.assertEqual(written, 1, "真实计划必须接管同键标记行（显式恢复路径）")
        row = conn.execute("SELECT * FROM sign_tasks WHERE phone=? AND day=?",
                           (PHONE_A, today)).fetchone()
        self.assertEqual(row["vshard"], 7, "标记行必须被真实计划覆盖")
        self.assertEqual(row["state"], "pending")
        claimed = queue_store.claim_batch(
            "w1@host", today, tuple(range(256)), now=f"{today} 23:59:59")
        self.assertEqual([c["phone"] for c in claimed], [PHONE_A],
                         "被标记行挤占的账号必须重新可领取")
        self.assertEqual(queue_store.pending_count(today, ()), 0)
        self.assertEqual(
            queue_store.pending_count(today, tuple(range(256))), 0,
            "领取后 pending 归零（领之前是 1：见 claim_batch 已消费）")

    def test_write_plan_never_overwrites_real_rows(self):
        conn = self._init()
        today = _day()
        conn.execute(
            "INSERT INTO sign_tasks (phone, day, vshard, owner, run_at, priority, "
            "state, attempts, lease_until, result, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (PHONE_A, today, 3, "w0@host", f"{today} 07:00:00", 5, "done",
             1, "", "ok", f"{today} 07:00:00"))
        conn.commit()
        planner.write_plan([{"phone": PHONE_A, "day": today, "vshard": 3,
                             "owner": "w0@host", "run_at": f"{today} 06:00:00",
                             "priority": 5, "state": "pending"}], today)
        row = conn.execute("SELECT * FROM sign_tasks WHERE phone=? AND day=?",
                           (PHONE_A, today)).fetchone()
        self.assertEqual(row["state"], "done", "已有真实结论的行绝不被计划重建改写")

    def test_history_marker_outside_plan_day_is_kept(self):
        conn = self._init()
        past = _day(3)
        conn.execute(
            "INSERT INTO sign_tasks (phone, day, vshard, owner, run_at, priority, "
            "state, attempts, lease_until, result, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (PHONE_A, past, -1, "backfill", f"{past} 00:00:00", 5, "skipped",
             0, "", "", f"{past} 00:00:00"))
        conn.commit()
        planner.write_plan([{"phone": PHONE_A, "day": _day(), "vshard": 1,
                             "owner": "w@h", "run_at": f"{_day()} 07:00:00",
                             "priority": 5, "state": "pending"}], _day())
        hist = conn.execute("SELECT vshard FROM sign_tasks WHERE day=?",
                            (past,)).fetchone()
        self.assertEqual(hist["vshard"], -1, "历史日标记行保持原样（台账证据）")


# ---------------------------------------------------------------------------
# 修法 6：SQL 标识符转义
# ---------------------------------------------------------------------------
class QuotingTest(_DbTemp):
    def test_v5_handles_single_quote_index_name(self):
        conn = self._init(upto=4)
        conn.execute("CREATE UNIQUE INDEX \"o'k_unique\" ON users(email)")
        conn.commit()
        _close_global_conn()
        conn2 = db.init_db(self.db_file, env_file=self.env_file, cleanup=False)
        self.assertEqual(_user_version(conn2),
                         max(m[0] for m in migrations._MIGRATIONS),
                         "含单引号的索引名不得打断 v5（最坏 OperationalError 拒启）")

    def test_v13_handles_double_quote_table_name(self):
        conn = self._init(upto=12)
        conn.execute('CREATE TABLE "we""ird" (id INTEGER PRIMARY KEY, flag flag '
                     'INTEGER NOT NULL DEFAULT 0)')
        conn.execute('INSERT INTO "we""ird" (id) VALUES (1)')
        conn.execute("PRAGMA user_version = 12")
        conn.commit()
        _close_global_conn()
        conn2 = db.init_db(self.db_file, env_file=self.env_file, cleanup=False)
        self.assertIn('we"ird', _table_names(conn2), "含双引号的表名必须原样保留")
        decls = {r["name"]: r["type"] for r in conn2.execute(
            'PRAGMA table_info("we""ird")').fetchall()}
        self.assertEqual(decls.get("flag"), "INTEGER", "畸形声明应被修复")


# ---------------------------------------------------------------------------
# ⑥+① 启动链路：真实子进程退出码（拒启 rc=4，健康库不误拒）
# ---------------------------------------------------------------------------
class StartupExitCodeTest(unittest.TestCase):
    """进程级：迁移完整性拒启必须是独立非零码 4（0/1/2/3/10 家族只增不改）。"""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="yiban-mig-fc-cli-")
        self.db_file = os.path.join(self.root, "yiban.db")
        self.env_file = os.path.join(self.root, ".env")
        with open(self.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n")
        os.makedirs(os.path.join(self.root, "state"))

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _env(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("YIBAN_")}
        env.update({
            "YIBAN_STATE_DIR": os.path.join(self.root, "state"),
            "YIBAN_LOG_FILE": os.path.join(self.root, "sign.log"),
            "YIBAN_DB_FILE": self.db_file,
            "YIBAN_ENV_FILE": self.env_file,
            "YIBAN_ACCOUNTS_KEY": TEST_KEY,
            "YIBAN_MAIL_ENABLE": "0",
            "PYTHONPATH": BASE,
            "PYTHONIOENCODING": "utf-8",
        })
        return env

    def _run(self, argv):
        return subprocess.run([sys.executable, "-m", "yiban.cli", *argv],
                              cwd=BASE, env=self._env(), capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              timeout=180)

    def _build_db(self, code):
        r = subprocess.run([sys.executable, "-c", code, self.db_file, self.env_file],
                           cwd=BASE, env=self._env(), capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=180)
        self.assertEqual(r.returncode, 0, f"建库子进程失败: {r.stderr[-500:]}")

    def test_healthy_v17_db_boots_and_upgrades(self):
        """不变量⑥：v17 形态库 → sign 正常走完迁移并进入业务判定（零账号 rc=1，
        绝不是迁移拒启），库被提升到链顶。"""
        self._build_db(
            "import sys;"
            "from yiban.store import db;"
            "db._MIGRATIONS=[m for m in db._MIGRATIONS if m[0]<=17];"
            "conn=db.init_db(db_file=sys.argv[1], env_file=sys.argv[2], cleanup=False);"
            "conn.close()")
        r = self._run(["sign"])
        self.assertNotEqual(r.returncode, 4,
                            f"健康库不得被判迁移拒启: {r.stderr[-500:]}")
        self.assertEqual(r.returncode, 1, "零账号的配置判定必须是 1（引擎口径）")
        conn = sqlite3.connect(self.db_file)
        try:
            ver = int(conn.execute("PRAGMA user_version").fetchone()[0])
            self.assertGreaterEqual(ver, 19)
            cols = {row[1] for row in conn.execute("PRAGMA table_info(sign_claims)")}
            self.assertIn("epoch", cols, "升线路径必须把 try_claim 的硬前置补齐")
        finally:
            conn.close()

    def test_drifted_db_refuses_startup_with_rc4_naming_v19(self):
        """不变量①进程级：user_version 声称过 v19 但 epoch 缺失 ⇒ 拒启 rc=4 + 点名。"""
        self._build_db(
            "import sys;"
            "from yiban.store import db;"
            "db._MIGRATIONS=[m for m in db._MIGRATIONS if m[0]<=18];"
            "conn=db.init_db(db_file=sys.argv[1], env_file=sys.argv[2], cleanup=False);"
            "conn.execute('PRAGMA user_version = 19');conn.commit();conn.close()")
        r = self._run(["sign"])
        self.assertEqual(r.returncode, 4,
                         f"迁移完整性失败必须 rc=4（拒启），实际 {r.returncode}: "
                         f"{r.stderr[-800:]}")
        self.assertIn("v19", r.stderr + r.stdout, "必须点名缺哪条迁移")
        # 只读校验模式（migrate=False）不受影响：红线"不得要求人工干预"的另一半——
        # 诊断入口本身还能用
        self.assertNotEqual(self._run(["sign", "--check-config"]).returncode, 4,
                            "--check-config 不跑迁移，不得被完整性门误伤")


if __name__ == "__main__":
    unittest.main()
