# -*- coding: utf-8 -*-
"""批次17 P3 修复验证（2026-09-01）。

覆盖三个 P3 级小修（均为纯本地，无网络请求）：

- P3-1 scripts/db.py：`conn.executescript()` 会在执行脚本前**隐式 COMMIT**，
  把 `_run_migrations` 为每个迁移开启的 `BEGIN IMMEDIATE` 提前提交——迁移中途
  失败时，同事务先前的 DDL 已落盘无法回滚，迁移原子性被击穿。修复：改为逐条
  `conn.execute`（_create_tables / migrate_v4 / migrate_v5 / migrate_v8 /
  migrate_v12），DDL 全部落在事务内，失败可整体回滚。

- P3-2 scripts/rekey_accounts.py：`--new-key <密钥>` 把新密钥写进进程 argv，
  同机其他用户可通过 ps / /proc/<pid>/cmdline / shell 历史读到。修复：读钥时
  醒目告警并推荐 `--new-key-file`（0600 文件，首行为密钥），读后尽力覆写
  C argv 内存（Linux/glibc 经 `__libc_argv`，其它平台静默跳过）。

- P3-3 scripts/backup.sh：`BACKUP_PLAINTEXT=1` 只豁免**本地归档**的默认加密，
  异机副本契约不变（异机副本绝不传明文）。原 `[ "$BACKUP_PLAINTEXT" != "1" ]`
  拦截让"显式明文 + 配置了异机"时异机副本被整体静默丢弃，且告警文案误导
  （有 gpg 却报"未提供可用加密方式"）。修复：移除该拦截，异机副本仍尝试加密后
  出站；明文导致异机缺位时给出准确告警。

用法（项目根目录，勿设 PYTHONIOENCODING）：
    env -u ACC_PRODUCT_CONFIG_V3 py -3.14 -m pytest tests/test_p3_fixes_091.py -v
backup.sh 另以 `bash -n scripts/backup.sh` 静态核验。
"""
import contextlib
import io
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

TEST_KEY = "b" * 64


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
        """转换后的逐条 DDL 语义不变：新库仍建出全部基线 + 迁移表。"""
        db.init_db(self.db_file, env_file=self.env_file, cleanup=False)
        names = {r["name"] for r in db.get_conn().execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for t in ("accounts", "users", "audit_logs", "time_prefs",
                  "user_delete_requests", "sign_events", "page_visits",
                  "server_metrics", "session_cache", "app_meta"):
            self.assertIn(t, names, f"缺表 {t}——逐条 DDL 转换丢失了建表语句")

    def test_db_source_has_no_executescript_call(self):
        """源码级回归绊线：db.py 不得再出现 executescript 调用。"""
        with open(os.path.join(BASE, "scripts", "db.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertNotIn(".executescript(", src,
                         "db.py 重新引入了 executescript（隐式 COMMIT 隐患）")


def _close_db():
    if db._conn is not None:
        with contextlib.suppress(Exception):
            db._conn.close()
        db._conn = None


class RekeyArgvLeakP3Test(unittest.TestCase):
    """P3-2：--new-key argv 泄露的告警与尽力擦除。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="p3-rekey-")
        cls.key_file = os.path.join(cls.tmp, "newkey.txt")
        with open(cls.key_file, "w", encoding="utf-8") as f:
            f.write(TEST_KEY + "\n")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _args(self, **kw):
        import argparse
        defaults = {"new_key": "", "new_key_file": "", "generate": False}
        defaults.update(kw)
        return argparse.Namespace(**defaults)

    def _read_with_stderr(self, args):
        import rekey_accounts
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            key = rekey_accounts._read_new_key(args)
        return key, stderr.getvalue()

    def test_new_key_argv_emits_warning(self):
        """--new-key 读钥必须向 stderr 告警并推荐 --new-key-file。"""
        key, msg = self._read_with_stderr(self._args(new_key=TEST_KEY))
        self.assertEqual(key.hex(), TEST_KEY)
        self.assertIn("警告", msg)
        self.assertIn("--new-key-file", msg)
        self.assertTrue(
            any(w in msg for w in ("进程列表", "/proc", "ps ", "shell 历史")),
            f"告警未提及 argv 暴露途径: {msg!r}")

    def test_new_key_file_no_argv_warning(self):
        """--new-key-file 读钥不应出现 argv 暴露告警。"""
        key, msg = self._read_with_stderr(
            self._args(new_key_file=self.key_file))
        self.assertEqual(key.hex(), TEST_KEY)
        self.assertNotIn("警告", msg, f"--new-key-file 不应有 argv 暴露告警: {msg!r}")

    def test_generate_no_argv_warning(self):
        """--generate 读钥同样不应出现 argv 暴露告警。"""
        key, msg = self._read_with_stderr(self._args(generate=True))
        self.assertEqual(len(key), 32)
        self.assertNotIn("警告", msg)

    def test_wipe_argv_never_raises(self):
        """_wipe_argv 尽力而为：任何平台都不抛异常，返回 bool。"""
        import rekey_accounts
        result = rekey_accounts._wipe_argv()
        self.assertIsInstance(result, bool)


class BackupPlaintextP3Test(unittest.TestCase):
    """P3-3：backup.sh 明文模式的异机加密契约与告警（静态源码核验）。"""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(BASE, "scripts", "backup.sh"), encoding="utf-8") as f:
            cls.src = f.read()

    def test_plaintext_local_warning_present(self):
        """BACKUP_PLAINTEXT=1 时本地明文归档仍有大字告警。"""
        self.assertIn("BACKUP_PLAINTEXT=1", self.src)
        self.assertIn("明文", self.src)

    def test_remote_no_longer_blocked_by_plaintext(self):
        """异机路径不得再被 BACKUP_PLAINTEXT=1 拦截（本地豁免不取消异机加密副本）。"""
        # 精确匹配可执行代码行（注释里引用旧代码属文档说明，不算回归）
        self.assertNotIn('&& [ "${BACKUP_PLAINTEXT}" != "1" ]; then', self.src,
                         "原拦截仍存在：BACKUP_PLAINTEXT=1 时异机副本会被整体丢弃")

    def test_remote_plaintext_warning_distinguishes_cases(self):
        """明文导致异机缺位时的告警须明确"豁免只作用于本地、异机副本绝不传明文"。"""
        self.assertIn("BACKUP_PLAINTEXT=1 只豁免本地归档的默认加密", self.src)
        self.assertIn("异机副本绝不传明文", self.src)

    def test_remote_still_attempts_encryption(self):
        """本地为明文（含显式 BACKUP_PLAINTEXT=1）时仍尝试加密后再出站。"""
        self.assertIn("try_encrypt", self.src)
        self.assertIn("REMOTE_FILE", self.src)


if __name__ == "__main__":
    unittest.main()
