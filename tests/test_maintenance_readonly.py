# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""维护脚本的只读承诺：读账号/库的维护脚本不得经 `init_db` 建库/建表/切 WAL。

标签：J · 运维：部署/备份/发布
覆盖：`scripts/list_duplicate_owners.py` / `scripts/ledger_check.py` /
    `scripts/audit_verify.py` 在空 cwd 跑完不产生任何库文件；对既有库跑完内容
    （mtime/大小/表清单/user_version）不变，新增文件只限 SQLite 读簿记 sidecar；
    `db.init_db(create=False)` 的只读语义（库缺失抛错不建库、连接写不进）。
对应实现：`yiban/store/connection.open_readonly`（含 `immutable=False`）、
    `yiban/store/db.init_db(create=False)` 与三个脚本的只读打开点。
关键断言：只读脚本零新库文件且不改动目标库内容；`create=False` 只打开既有库，任何写以
    `sqlite3.OperationalError` 被 SQLite 拒绝。
依赖：起 `sys.executable` 子进程跑脚本（纯 Python）；临时 STATE/LOG/DB/ENV，不碰本机
    真实 .env 与真实备份；无网络/node/docker。
"""
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 只读维护脚本（读账号/库、不写）：脚本名 → 额外 argv
READONLY_SCRIPTS = {
    "list_duplicate_owners.py": [],
    "ledger_check.py": [],
    "audit_verify.py": [],
}


def _env(root):
    """隔离环境：临时路径四件套 + 剥掉继承的 YIBAN_*；审计/账号密钥给固定测试值。"""
    env = {k: v for k, v in os.environ.items() if not k.startswith("YIBAN_")}
    env.update({
        "YIBAN_STATE_DIR": os.path.join(root, "state"),
        "YIBAN_LOG_FILE": os.path.join(root, "logs", "sign.log"),
        "YIBAN_DB_FILE": os.path.join(root, "yiban.db"),
        "YIBAN_ENV_FILE": os.path.join(root, ".env"),
        "YIBAN_ACCOUNTS_KEY": "a" * 64,
        "PYTHONPATH": BASE,
        "PYTHONIOENCODING": "utf-8",
    })
    return env


def _run_script(name, argv, env, cwd):
    return subprocess.run(
        [sys.executable, os.path.join(BASE, "scripts", name), *argv],
        cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8",
        errors="replace", stdin=subprocess.DEVNULL, timeout=120)


def _make_db(db_file, root, env):
    """造一个真实的空库（init_db 建表 + 迁移后关闭，WAL 已收敛为静默态）。"""
    code = ("import sys; from yiban.store import db;"
            "db.init_db(db_file=sys.argv[1], env_file=sys.argv[2], cleanup=False);"
            "db.get_conn().close()")
    r = subprocess.run([sys.executable, "-c", code, db_file,
                        os.path.join(root, ".env")], cwd=BASE, env=env,
                       capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert r.returncode == 0, r.stderr[-400:]


class MaintenanceScriptsReadOnlyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="yiban-maint-ro-")
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.env = _env(self.root)

    def test_scripts_do_not_create_db_in_empty_cwd(self):
        """空 cwd（库不存在）：脚本不得当场建库；缺库按非 0 明确报告。"""
        for name, argv in READONLY_SCRIPTS.items():
            with self.subTest(script=name):
                work = os.path.join(self.root, name.replace(".py", ""))
                os.makedirs(os.path.join(work, "state"))
                env = dict(self.env, YIBAN_DB_FILE=os.path.join(work, "yiban.db"))
                before = sorted(os.listdir(work))
                r = _run_script(name, argv, env, work)
                self.assertNotEqual(r.returncode, 0, r.stdout[-300:])
                self.assertNotIn("yiban.db", os.listdir(work),
                                 f"{name} 在空 cwd 建出了库")
                self.assertEqual(sorted(os.listdir(work)), before,
                                 f"{name} 在空 cwd 留下了新文件")

    def test_scripts_leave_existing_db_untouched(self):
        """既有库：库文件 mtime/大小/表清单/user_version 全不变；新增文件只限 SQLite sidecar。

        `list_duplicate_owners` / `ledger_check` 走 `open_readonly` 的自动档（静默库
        `immutable=1`），连 `-shm`/`-wal` 都不新建；`audit_verify` 走 `immutable=False`
        的 `mode=ro`（必须看得见并发写者与锁，见 `test_audit_transaction` 的锁库用例），
        WAL 库上可能新建 `-shm`/`-wal`——那是 SQLite 的读簿记，不是数据改动，故只断言
        "新增文件 ∈ {<db>.db-shm, <db>.db-wal}"，绝不新增任何数据库文件。
        """
        work = os.path.join(self.root, "hasdb")
        os.makedirs(os.path.join(work, "state"))
        db_file = os.path.join(work, "yiban.db")
        env = dict(self.env, YIBAN_DB_FILE=db_file)
        _make_db(db_file, work, env)
        before_entries = sorted(os.listdir(work))
        before_mtime = os.path.getmtime(db_file)
        before_size = os.path.getsize(db_file)
        before_tables = self._tables(db_file)
        before_uv = self._user_version(db_file)
        sidecars = {os.path.basename(db_file) + "-shm", os.path.basename(db_file) + "-wal"}

        for name, argv in READONLY_SCRIPTS.items():
            with self.subTest(script=name):
                _run_script(name, argv, env, work)
                allowed = set(before_entries) | sidecars
                new = set(os.listdir(work)) - allowed
                self.assertFalse(new, f"{name} 留下了非 sidecar 文件：{sorted(new)}")
                self.assertEqual(os.path.getmtime(db_file), before_mtime,
                                 f"{name} 改动了库 mtime")
                self.assertEqual(os.path.getsize(db_file), before_size,
                                 f"{name} 改动了库大小")
                self.assertEqual(self._tables(db_file), before_tables,
                                 f"{name} 改动了表清单")
                self.assertEqual(self._user_version(db_file), before_uv,
                                 f"{name} 改动了 user_version")

    def test_audit_verify_does_not_create_missing_tables(self):
        """库缺表时 audit_verify 只报 rc=2，不得顺手 `CREATE TABLE`（此前经 init_db 会）。"""
        work = os.path.join(self.root, "sparse")
        os.makedirs(os.path.join(work, "state"))
        db_file = os.path.join(work, "sparse.db")
        conn = sqlite3.connect(db_file)
        try:
            conn.execute("CREATE TABLE only_this(x)")
            conn.commit()
        finally:
            conn.close()
        env = dict(self.env, YIBAN_DB_FILE=db_file)
        r = _run_script("audit_verify.py", [], env, work)
        self.assertEqual(r.returncode, 2, r.stdout[-300:] + r.stderr[-300:])
        self.assertEqual(self._tables(db_file), ["only_this"],
                         "audit_verify 在只读取证里建了表")

    @staticmethod
    def _tables(db_file):
        # immutable=1：静默库上读 schema 不留 -shm/-wal（mode=ro 会为 WAL 库建 sidecar，
        # 那会让"文件集合不变"这条断言被测试自己的读操作污染）。
        conn = sqlite3.connect(f"file:{db_file}?mode=ro&immutable=1", uri=True)
        try:
            return sorted(r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"))
        finally:
            conn.close()

    @staticmethod
    def _user_version(db_file):
        conn = sqlite3.connect(f"file:{db_file}?mode=ro&immutable=1", uri=True)
        try:
            return int(conn.execute("PRAGMA user_version").fetchone()[0])
        finally:
            conn.close()


class InitDbReadonlyTest(unittest.TestCase):
    """`db.init_db(create=False)`：只打开既有库，绝不建库/建表/切 WAL。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="yiban-initdb-ro-")
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self._reset_db_singleton()

    def tearDown(self):
        self._reset_db_singleton()

    @staticmethod
    def _reset_db_singleton():
        from yiban.store import connection
        conn = connection.current()
        if conn is not None:
            conn.close()
        connection.reset_conn()

    def test_create_false_missing_db_raises_without_creating(self):
        missing = os.path.join(self.root, "nope.db")
        from yiban.store import db
        with self.assertRaises(FileNotFoundError):
            db.init_db(db_file=missing, create=False)
        self.assertFalse(os.path.exists(missing), "只读初始化建出了空库")

    def test_create_false_connection_rejects_writes(self):
        db_file = os.path.join(self.root, "yiban.db")
        env = _env(self.root)
        _make_db(db_file, self.root, env)
        before_uv = sqlite3.connect(db_file).execute("PRAGMA user_version").fetchone()[0]
        before_entries = sorted(os.listdir(self.root))

        from yiban.store import db
        conn = db.init_db(db_file=db_file, create=False)
        self.assertIsNotNone(conn)
        with self.assertRaises(sqlite3.OperationalError):
            conn.execute("CREATE TABLE should_not_exist(x)")
        with self.assertRaises(sqlite3.OperationalError):
            conn.execute("INSERT INTO app_meta(key, value) VALUES('x','y')")
        self._reset_db_singleton()

        # `mode=ro` 在 WAL 库上可能新建 -shm/-wal（SQLite 读簿记）；只断言没有新增
        # 任何**数据库文件**，且内容未变。
        allowed = set(before_entries) | {"yiban.db-shm", "yiban.db-wal"}
        self.assertFalse(set(os.listdir(self.root)) - allowed,
                         "只读初始化留下了非 sidecar 文件")
        after_uv = sqlite3.connect(db_file).execute("PRAGMA user_version").fetchone()[0]
        self.assertEqual(after_uv, before_uv)


if __name__ == "__main__":
    unittest.main()
