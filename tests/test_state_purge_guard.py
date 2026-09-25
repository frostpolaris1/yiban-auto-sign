# -*- coding: utf-8 -*-
"""清库/删除入口的状态侧防线：备份副本保护、state 清理 argv/dry-run、CLI --yes 门。

标签：D · 状态词汇与账号生命周期
覆盖：`yiban/state_gc.py` 的清理迭代器跳过 SQLite 备份副本（按内容识别，非文件名）；
    `scripts/state_cleanup.py` 的 `--dry-run` 真不删与未知参数报错；
    `yiban.cli` 的 `state --yes` 需要指纹回显、留痕写不进去即拒绝（fail-closed）。
对应实现：`yiban/state_gc.py`（`_iter_expired` 的 SQLite 跳过）与 `yiban/cli.py`
    的 `_cmd_state`（`--fingerprint` + `write_purge_audit` 门）。
关键断言：`db --backup` 落在状态目录的副本**不得按文件名被 state 清理掉**——副本是
    SQLite 库（内容判据），文件名叫得像按日状态文件也不行；`state --yes` 单独不构成
    放行；留痕失败必须零删除。三条都用前后对照与活体反例钉住。
依赖：临时目录写真/伪文件；importlib 加载 `scripts/state_cleanup.py`；CLI 门在进程内
    调 `yiban.cli.main`（退出码与零删除是可进程内断言的），无子进程、无 bash。
"""
import contextlib
import io
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import state_cleanup  # noqa: E402  （scripts/state_cleanup.py）

from yiban import cli, state_gc  # noqa: E402

EXPIRED = "sched-run-2020-01-01.json"
#: 一个"名字像按日状态文件、内容却是 SQLite 库"的备份副本（`db --backup` 的产物形态）
BACKUP_LIKE = "sign-state-2020-01-02.json"


def _write_sqlite(path):
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        conn.execute("INSERT INTO t (v) VALUES ('backup')")
        conn.commit()
    finally:
        conn.close()


class BackupCopyProtectionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-state-guard-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_sqlite_backup_copy_is_not_deleted_by_state_cleanup(self):
        """文件名叫得像过期按日文件，但内容是 SQLite 库 ⇒ 必须保留（备份机制自管理）。"""
        plain = os.path.join(self.tmp, EXPIRED)
        with open(plain, "w", encoding="utf-8") as f:
            f.write("{}")
        backup = os.path.join(self.tmp, BACKUP_LIKE)
        _write_sqlite(backup)
        removed, detail = state_gc.sweep(self.tmp)
        self.assertEqual(removed, 1, detail)
        self.assertFalse(os.path.exists(plain), "普通过期状态文件仍应清理")
        self.assertTrue(os.path.exists(backup),
                        "SQLite 备份副本不得被按文件名清掉（db --backup 的副本）")

    def test_plan_excludes_backup_copy(self):
        backup = os.path.join(self.tmp, BACKUP_LIKE)
        _write_sqlite(backup)
        candidates, detail = state_gc.plan(self.tmp)
        self.assertEqual(candidates, 0, detail)
        self.assertNotIn(BACKUP_LIKE, " ".join(detail))


class StateCleanupArgvTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-state-argv-")
        self.expired = os.path.join(self.tmp, EXPIRED)
        with open(self.expired, "w", encoding="utf-8") as f:
            f.write("{}")
        self._env = mock.patch.dict(os.environ, {
            "YIBAN_STATE_DIR": self.tmp, "YIBAN_LOG_FILE": "",
        })
        self._env.start()

    def tearDown(self):
        self._env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_dry_run_reports_and_does_not_delete(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = state_cleanup.main(["--dry-run"])
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.exists(self.expired), "--dry-run 必须真不删")
        self.assertIn(EXPIRED, out.getvalue())

    def test_unknown_argument_errors_and_deletes_nothing(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = state_cleanup.main(["--bogus"])
        self.assertEqual(rc, 2, "被丢弃/未知参数必须报错退出")
        self.assertTrue(os.path.exists(self.expired), "参数错误不得真删")

    def test_default_still_cleans_for_cron(self):
        self.assertEqual(state_cleanup.main([]), 0)
        self.assertFalse(os.path.exists(self.expired), "cron 默认路径仍要正常清理")


class CliStateGuardTest(unittest.TestCase):
    KEY = "a" * 64
    AUDIT_KEY = "b" * 64

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-cli-state-")
        self.state_dir = os.path.join(self.tmp, "state")
        os.makedirs(self.state_dir)
        self.db_file = os.path.join(self.tmp, "yiban.db")
        self.env_file = os.path.join(self.tmp, ".env")
        with open(self.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={self.KEY}\nYIBAN_AUDIT_KEY={self.AUDIT_KEY}\n")
        self.expired = os.path.join(self.state_dir, EXPIRED)
        with open(self.expired, "w", encoding="utf-8") as f:
            f.write("{}")
        self._env = mock.patch.dict(os.environ, {
            "YIBAN_STATE_DIR": self.state_dir,
            "YIBAN_LOG_FILE": os.path.join(self.tmp, "logs", "sign.log"),
            "YIBAN_DB_FILE": self.db_file,
            "YIBAN_ENV_FILE": self.env_file,
        })
        self._env.start()
        self._make_db()

    def tearDown(self):
        self._env.stop()
        from yiban.store import connection
        conn = connection.current()
        if conn is not None:
            conn.close()
        connection.reset_conn()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_db(self):
        from yiban.store import db as store_db
        store_db.init_db(db_file=self.db_file, env_file=self.env_file, cleanup=False)
        store_db.get_conn().close()
        from yiban.store import connection
        connection.reset_conn()

    def _run(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli.main(argv)
        return rc, out.getvalue()

    def _fingerprint(self):
        rc, text = self._run(["state", "--json"])
        self.assertEqual(rc, 0, text)
        return json.loads(text)["fingerprint"]

    def test_yes_without_fingerprint_is_refused(self):
        rc, _ = self._run(["state", "--yes", "--json"])
        self.assertEqual(rc, 2)
        self.assertTrue(os.path.exists(self.expired), "缺指纹必须零删除")

    def test_wrong_fingerprint_is_refused(self):
        rc, _ = self._run(["state", "--yes", "--fingerprint", "PURGE-0000000000000000",
                           "--json"])
        self.assertEqual(rc, 2)
        self.assertTrue(os.path.exists(self.expired), "指纹不匹配必须零删除")

    def test_audit_failure_aborts_state_yes(self):
        fp = self._fingerprint()
        with mock.patch.object(cli.store_db, "audit", return_value=False):
            rc, _ = self._run(["state", "--yes", "--fingerprint", fp, "--json"])
        self.assertNotEqual(rc, 0, "留痕失败必须拒绝清理")
        self.assertTrue(os.path.exists(self.expired), "审计失败必须零删除（fail-closed）")

    def test_correct_fingerprint_cleans_and_audits(self):
        fp = self._fingerprint()
        rc, text = self._run(["state", "--yes", "--fingerprint", fp, "--json"])
        self.assertEqual(rc, 0, text)
        self.assertFalse(os.path.exists(self.expired))
        conn = sqlite3.connect(self.db_file)
        try:
            n = conn.execute(
                "SELECT COUNT(*) FROM audit_logs WHERE action='state_purge'").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(n, 1, "应落一条 state_purge 留痕")


if __name__ == "__main__":
    unittest.main(verbosity=2)
