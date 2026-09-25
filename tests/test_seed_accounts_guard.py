# -*- coding: utf-8 -*-
"""压测造数的清库防线：目标指纹 + 确认 + 留痕 + dry-run 真不删 + 反误指。

标签：J · 运维：部署/备份/发布
覆盖：`scripts/loadtest/seed_accounts.py`——dry-run 打印将清表与行数且零删除；
    缺确认/指纹不匹配拒绝；目标库已有非压测账号（误指生产库）拒绝；指纹齐全才清空
    重建并在审计链落一条；留痕写入失败必须放弃清空（fail-closed、零删除）。
对应实现：`scripts/loadtest/seed_accounts.py`（`main` / `seed` 的守卫分支）与
    `yiban/store/purge_guard.py`。
关键断言：`--yes` 单独不构成放行；"误指生产库"由**库内账号归属**判据拦下（不是看路径
    字符串）；审计失败 ⇒ 清空失败——用前后行数对照与审计桩 False 两条活体反例钉住。
依赖：临时目录 + 数据层真 schema 库；无网络、无子进程、无 bash/docker。
"""
import contextlib
import importlib
import io
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_LOADTEST = os.path.join(BASE, "scripts", "loadtest")
if os.path.dirname(_LOADTEST) not in sys.path:
    sys.path.insert(0, os.path.dirname(_LOADTEST))

seed_accounts = importlib.import_module("loadtest.seed_accounts")

from yiban.store import connection, purge_guard  # noqa: E402

_ENV_KEYS = ("YIBAN_ENV_FILE", "YIBAN_DB_FILE")


def _counts(db_path):
    conn = sqlite3.connect(db_path)
    try:
        out = {}
        for table in ("accounts", "users", "session_cache", "sign_events", "time_prefs"):
            try:
                out[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except sqlite3.Error:
                out[table] = 0
        return out
    finally:
        conn.close()


class SeedAccountsGuardTest(unittest.TestCase):
    KEY = "a" * 64
    AUDIT_KEY = "b" * 64

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-seed-guard-")
        self.db_path = os.path.join(self.tmp, "yiban.db")
        self.env_file = os.path.join(self.tmp, "test.env")
        with open(self.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={self.KEY}\nYIBAN_AUDIT_KEY={self.AUDIT_KEY}\n")
        self._saved = {k: os.environ.get(k) for k in _ENV_KEYS}

    def _fp(self):
        return purge_guard.db_content_fingerprint(self.db_path)[0]

    def tearDown(self):
        self._reset_conn()
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def _reset_conn():
        conn = connection.current()
        if conn is not None:
            conn.close()
        connection.reset_conn()

    def _seed_existing(self, owner):
        self._reset_conn()
        from yiban.store import db as store_db
        store_db.init_db(db_file=self.db_path, env_file=self.env_file, cleanup=False)
        store_db.add_account({
            "name": "seed", "phone": "13100000000", "password": "p",
            "phone_model": "", "phone_code": "", "owner": owner,
            "status": "active", "reject_reason": "",
        })
        store_db.get_conn().commit()
        store_db.get_conn().close()
        connection.reset_conn()

    def _run(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = seed_accounts.main(["--n", "2", "--db", self.db_path,
                                     "--env", self.env_file, *argv])
        return rc, out.getvalue()

    def test_dry_run_reports_and_does_not_delete(self):
        self._seed_existing("loadtest00000@mock.invalid")
        before = _counts(self.db_path)
        rc, text = self._run(["--dry-run"])
        self.assertEqual(rc, 0, text)
        self.assertEqual(_counts(self.db_path), before, "dry-run 必须零删除")
        self.assertIn("PURGE-", text)
        self.assertIn("accounts=1", text)

    def test_yes_alone_is_not_enough(self):
        self._seed_existing("loadtest00000@mock.invalid")
        before = _counts(self.db_path)
        rc, _ = self._run(["--yes"])
        self.assertEqual(rc, 2)
        self.assertEqual(_counts(self.db_path), before, "缺确认必须零删除")

    def test_wrong_fingerprint_is_refused(self):
        self._seed_existing("loadtest00000@mock.invalid")
        before = _counts(self.db_path)
        rc, _ = self._run(["--yes", "--fingerprint", "PURGE-0000000000000000"])
        self.assertEqual(rc, 2)
        self.assertEqual(_counts(self.db_path), before)

    def test_refuses_db_with_non_loadtest_accounts(self):
        """误指生产库：库内账号 owner 不是压测域 ⇒ 即便指纹匹配也拒绝且零删除。"""
        self._seed_existing("real-user@corp.example")
        fp, _ = purge_guard.db_content_fingerprint(self.db_path)
        before = _counts(self.db_path)
        rc, _ = self._run(["--yes", "--fingerprint", fp])
        self.assertEqual(rc, 2)
        self.assertEqual(_counts(self.db_path), before, "拒绝路径必须零删除")

    def test_correct_fingerprint_wipes_and_audits(self):
        self._seed_existing("loadtest00000@mock.invalid")
        rc, _ = self._run(["--yes", "--fingerprint", self._fp()])
        self.assertEqual(rc, 0)
        after = _counts(self.db_path)
        self.assertEqual(after["accounts"], 2, "应重建 2 个压测账号")
        conn = sqlite3.connect(self.db_path)
        try:
            n = conn.execute(
                "SELECT COUNT(*) FROM audit_logs WHERE action='loadtest_seed'").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(n, 1, "应落一条清库留痕")

    def test_audit_failure_aborts_wipe(self):
        self._seed_existing("loadtest00000@mock.invalid")
        before = _counts(self.db_path)
        with mock.patch.object(seed_accounts.db, "audit", return_value=False):
            rc, _ = self._run(["--yes", "--fingerprint", self._fp()])
        self.assertNotEqual(rc, 0, "留痕失败必须放弃清空")
        self.assertEqual(_counts(self.db_path), before, "审计失败必须零删除（fail-closed）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
