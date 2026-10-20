# -*- coding: utf-8 -*-
"""运维脚本的破坏性动作守卫：覆盖前留痕、既有库不迁移、日志失败响亮。

标签：J · 运维：部署/备份/发布
覆盖（工单 ba-p11-03 / ba-p11-06 / ba-p11-09 / ba-p11-10）：
  · `scripts/generate_demo_data.py`：既有库只校验 schema、绝不在清空前迁移
    （迁移会用当前密钥回填审计链，抹平"链被动过"的证据）；新库照常迁移到当前
    schema（否则 `db.audit` 需要的 `prev_hash`/`hash` 列不存在）。
  · `scripts/db_export.py`：默认拒绝覆盖既有 `accounts.json` / `users.json`；
    `--force` 才替换；`--dry-run` 零写盘；明文警告出现在写盘之前。
  · `scripts/state_cleanup.py`：留痕写不进去时退出码抬到 1，stderr 点名日志路径。
  · `scripts/list_duplicate_owners.py`：输出里的手机号与 owner 邮箱走
    `yiban.core.masking` 的同一份脱敏实现。
对应实现：四个脚本的入口函数（各自 `main`）。
关键断言：钉"破坏前可预知、既有证据不被改写、留痕失败必须响亮"三条不变式；
    不钉任何输出排版。
依赖：起 `sys.executable` 子进程跑脚本；临时 DB/.env/状态目录，不碰真实部署。
"""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEST_KEY = "a" * 64


def _run(script, argv, env, cwd):
    return subprocess.run(
        [sys.executable, os.path.join(BASE, "scripts", script), *argv],
        cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8",
        errors="replace", stdin=subprocess.DEVNULL, timeout=180,
    )


def _env(root, **extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith("YIBAN_")}
    env.update({
        "YIBAN_ENV_FILE": os.path.join(root, ".env"),
        "YIBAN_DB_FILE": os.path.join(root, "yiban.db"),
        "YIBAN_STATE_DIR": os.path.join(root, "state"),
        "YIBAN_LOG_FILE": os.path.join(root, "state", "sign.log"),
        "YIBAN_ACCOUNTS_KEY": TEST_KEY,
        "PYTHONPATH": BASE,
        "PYTHONIOENCODING": "utf-8",
    })
    env.update(extra)
    return env


def _write_env(root):
    path = os.path.join(root, ".env")
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n")
    return path


def _init_db(root, env, migrate=True):
    code = (
        "import sys\n"
        "from yiban.adapters.persistence import db\n"
        "db.init_db(db_file=sys.argv[1], env_file=sys.argv[2], cleanup=False, migrate=%r)\n"
        "db.get_conn().close()\n" % migrate
    )
    r = subprocess.run(
        [sys.executable, "-c", code, os.path.join(root, "yiban.db"), os.path.join(root, ".env")],
        cwd=BASE, env=env, capture_output=True, text=True, encoding="utf-8", timeout=120,
    )
    assert r.returncode == 0, r.stderr[-600:]
    return os.path.join(root, "yiban.db")


def _insert_accounts(db_file, rows):
    conn = sqlite3.connect(db_file)
    try:
        for i, (phone, owner, name) in enumerate(rows):
            conn.execute(
                "INSERT INTO accounts (sort_order, name, phone, password, owner, status) "
                "VALUES (?,?,?,?,?,?)",
                (i, name, phone, "x", owner, "active"),
            )
        conn.commit()
    finally:
        conn.close()


class DemoDataMigrationGuardTest(unittest.TestCase):
    """ba-p11-03：demo 脚本不得在清空既有库之前迁移（改写审计链）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="yiban-demo-guard-")
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.env = _env(self.root)
        _write_env(self.root)

    def _fingerprint(self, db_file):
        r = _run("generate_demo_data.py", ["--db", db_file, "--dry-run"],
                 self.env, self.root)
        self.assertEqual(r.returncode, 0, r.stderr[-600:])
        for line in r.stdout.splitlines():
            if line.startswith("目标库指纹:"):
                return line.split(":", 1)[1].strip()
        self.fail("dry-run 没有打印目标库指纹:\n" + r.stdout)

    def test_existing_old_schema_db_is_refused_without_migration(self):
        db_file = _init_db(self.root, self.env, migrate=False)
        # 既有审计行（旧 schema 无 hash 列）：迁移会把它"合法化"，本工具必须拒绝。
        conn = sqlite3.connect(db_file)
        try:
            conn.execute(
                "INSERT INTO audit_logs (ts, username, action, target, detail) "
                "VALUES ('2026-01-01 00:00:00', 'ops', 'before', '', 'old')"
            )
            conn.commit()
            before_version = conn.execute("PRAGMA user_version").fetchone()[0]
            before_columns = {r[1] for r in conn.execute("PRAGMA table_info(audit_logs)")}
        finally:
            conn.close()
        fp = self._fingerprint(db_file)
        r = _run("generate_demo_data.py",
                 ["--db", db_file, "--yes", "--fingerprint", fp,
                  "--users", "1", "--admin-accounts", "0",
                  "--events-per-day", "1", "--days", "1"],
                 self.env, self.root)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("不在清空前执行迁移", r.stderr)
        # 目标库零改动：版本没被推进、审计链列没被补上、原有审计行还在。
        conn = sqlite3.connect(db_file)
        try:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], before_version)
            self.assertEqual({r[1] for r in conn.execute("PRAGMA table_info(audit_logs)")},
                             before_columns)
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM audit_logs").fetchone()[0], 1,
                "拒绝路径不得删除任何既有审计行")
        finally:
            conn.close()

    def test_fresh_db_is_migrated_and_audit_chain_is_writable(self):
        db_file = os.path.join(self.root, "fresh", "demo.db")
        fp = self._fingerprint(db_file)
        r = _run("generate_demo_data.py",
                 ["--db", db_file, "--yes", "--fingerprint", fp,
                  "--users", "2", "--admin-accounts", "1",
                  "--events-per-day", "1", "--days", "1"],
                 self.env, self.root)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("审计链", r.stdout)
        self.assertIn("ok=True", r.stdout)


class DbExportOverwriteGuardTest(unittest.TestCase):
    """ba-p11-06：默认拒绝覆盖既有 JSON 回滚基线。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="yiban-export-guard-")
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.env = _env(self.root)
        _write_env(self.root)
        self.db_file = _init_db(self.root, self.env)
        _insert_accounts(self.db_file, [("13800138000", "admin", "演示")])
        self.out = os.path.join(self.root, "out")
        os.makedirs(self.out)

    def _sentinel(self):
        for name in ("accounts.json", "users.json"):
            with open(os.path.join(self.out, name), "w", encoding="utf-8") as f:
                f.write("SENTINEL")

    def _contents(self):
        out = {}
        for name in ("accounts.json", "users.json"):
            with open(os.path.join(self.out, name), encoding="utf-8") as f:
                out[name] = f.read()
        return out

    def test_existing_files_are_not_overwritten_without_force(self):
        self._sentinel()
        before = self._contents()
        r = _run("db_export.py", ["--out", self.out, "--db", self.db_file], self.env, self.root)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("拒绝执行", r.stderr)
        self.assertEqual(self._contents(), before)

    def test_force_replaces_and_dry_run_writes_nothing(self):
        self._sentinel()
        before = self._contents()
        dry = _run("db_export.py", ["--out", self.out, "--db", self.db_file, "--dry-run"],
                   self.env, self.root)
        self.assertEqual(dry.returncode, 0, dry.stdout + dry.stderr)
        self.assertIn("[dry-run]", dry.stdout)
        self.assertEqual(self._contents(), before)

        forced = _run("db_export.py", ["--out", self.out, "--db", self.db_file, "--force"],
                      self.env, self.root)
        self.assertEqual(forced.returncode, 0, forced.stdout + forced.stderr)
        self.assertEqual(len(json.loads(self._contents()["accounts.json"])), 1)
        self.assertEqual(json.loads(self._contents()["users.json"]), [])

    def test_plaintext_warning_precedes_writing(self):
        r = _run("db_export.py",
                 ["--out", self.out, "--db", self.db_file, "--plaintext", "--force"],
                 self.env, self.root)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("即将导出明文凭据", r.stderr)
        self.assertNotIn("已导出明文凭据", r.stderr,
                         "警告文案必须指向将要发生的动作，不能在写盘后才提示")


class StateCleanupLogFailureTest(unittest.TestCase):
    """ba-p11-09：清理日志写不进去时不许返回 0。"""

    def test_log_write_failure_raises_exit_code(self):
        tmp = tempfile.TemporaryDirectory(prefix="yiban-cleanup-log-")
        self.addCleanup(tmp.cleanup)
        root = tmp.name
        state = os.path.join(root, "state")
        os.makedirs(state)
        expired = os.path.join(state, "sign-state-2020-01-01.json")
        with open(expired, "w", encoding="utf-8") as f:
            f.write("{}")
        # 让 cleanup.log 成为目录：os.open(..., O_APPEND) 必然失败。
        os.makedirs(os.path.join(state, "cleanup.log"))
        env = _env(root, YIBAN_STATE_DIR=state, YIBAN_RETENTION_DAYS="1")
        r = _run("state_cleanup.py", [], env, root)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertFalse(os.path.exists(expired), "过期文件应已清理（删除已发生）")
        self.assertIn("清理日志写入失败", r.stderr)
        self.assertIn("留痕丢失", r.stderr)


class DuplicateOwnerMaskingTest(unittest.TestCase):
    """ba-p11-10：重复 owner 清单输出不得含完整手机号/邮箱。"""

    def test_output_is_masked(self):
        tmp = tempfile.TemporaryDirectory(prefix="yiban-dup-owner-")
        self.addCleanup(tmp.cleanup)
        root = tmp.name
        env = _env(root)
        _write_env(root)
        db_file = _init_db(root, env, migrate=False)
        _insert_accounts(db_file, [
            ("13800138000", "owner@example.cn", "甲"),
            ("13900139000", "owner@example.cn", "乙"),
        ])
        r = _run("list_duplicate_owners.py", ["--db", db_file], env, root)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("138****8000", r.stdout)
        self.assertIn("139****9000", r.stdout)
        self.assertIn("own***@example.cn", r.stdout)
        self.assertNotIn("13800138000", r.stdout)
        self.assertNotIn("13900139000", r.stdout)
        self.assertNotIn("owner@example.cn", r.stdout)


if __name__ == "__main__":
    unittest.main()
