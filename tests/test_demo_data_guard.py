# -*- coding: utf-8 -*-
"""demo 数据生成的清库防线：目标指纹 + 确认 + 清库留痕 + dry-run 真不删。

标签：J · 运维：部署/备份/发布
覆盖：`scripts/generate_demo_data.py` 的四条防线——dry-run 打印将清对象与计数且
    文件系统零删除；缺确认/指纹不匹配一律拒绝且零删除；指纹齐全才真清并在
    `audit_logs` 落一条 `demo_purge`；审计写入失败必须让清库失败（fail-closed、
    零删除）；未知参数报错退出。
对应实现：`scripts/generate_demo_data.py`（`main` 的 `--dry-run` / `--yes` /
    `--fingerprint` 分支）与 `yiban/store/purge_guard.py`。
关键断言：`--yes` 单独不构成放行（指纹回显是前置）；"dry-run" 必须真的不删；
    审计失败 ⇒ 清库一并失败——这三条分别用"行数前后对照"与"审计桩返回 False"活体反例钉住。
依赖：临时目录 + 数据层真 schema 库；无网络、无 bash/docker；手机号用演示号段。
"""
import contextlib
import io
import os
import shutil
import sqlite3
import tempfile
import unittest
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import generate_demo_data  # noqa: E402  （scripts/generate_demo_data.py）

from yiban.store import connection, purge_guard  # noqa: E402


def _counts(db_path):
    conn = sqlite3.connect(db_path)
    try:
        out = {}
        for table in ("accounts", "users", "audit_logs", "sign_events", "time_prefs"):
            try:
                out[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except sqlite3.Error:
                out[table] = 0
        return out
    finally:
        conn.close()


class DemoDataGuardTest(unittest.TestCase):
    KEY = "a" * 64
    AUDIT_KEY = "b" * 64

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-demo-guard-")
        self.db_path = os.path.join(self.tmp, "demo.db")
        self.env_file = os.path.join(self.tmp, ".env")
        with open(self.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={self.KEY}\nYIBAN_AUDIT_KEY={self.AUDIT_KEY}\n")
        self._seed()
        self.fp, _ = purge_guard.db_content_fingerprint(self.db_path)

    def tearDown(self):
        self._reset_conn()
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def _reset_conn():
        conn = connection.current()
        if conn is not None:
            conn.close()
        connection.reset_conn()

    def _seed(self):
        """建真 schema 库并写少量"旧 demo 数据"（供指纹与前后对照）。"""
        self._reset_conn()
        from yiban.store import db as store_db
        store_db.init_db(db_file=self.db_path, env_file=self.env_file, cleanup=False)
        store_db.create_user("user@demo.local", "demo-hash", role="user",
                             created_at="2026-01-01 08:00:00", pw_version=1)
        store_db.add_account({
            "name": "demo", "phone": "13100000000", "password": "p",
            "phone_model": "", "phone_code": "", "owner": "user@demo.local",
            "status": "active", "reject_reason": "",
        })
        store_db.audit("user@demo.local", "account_add", "", "keep")
        store_db.get_conn().commit()
        store_db.get_conn().close()
        connection.reset_conn()

    def _run(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = generate_demo_data.main(["--db", self.db_path, "--env", self.env_file, *argv])
        return rc, out.getvalue()

    # ---- dry-run ----
    def test_dry_run_reports_and_does_not_delete(self):
        before = _counts(self.db_path)
        rc, text = self._run(["--dry-run", "--users", "2"])
        self.assertEqual(rc, 0, text)
        self.assertEqual(_counts(self.db_path), before, "dry-run 必须文件系统零删除")
        self.assertIn("PURGE-", text, "应打印目标指纹供回显")
        self.assertIn("将", text, "应打印将清对象/计数")
        self.assertIn("accounts=1", text)

    # ---- 拒绝形态 ----
    def test_yes_alone_is_not_enough(self):
        before = _counts(self.db_path)
        rc, _ = self._run(["--yes"])
        self.assertEqual(rc, 2)
        self.assertEqual(_counts(self.db_path), before, "缺确认必须零删除")

    def test_wrong_fingerprint_is_refused(self):
        before = _counts(self.db_path)
        rc, _ = self._run(["--yes", "--fingerprint", "PURGE-0000000000000000"])
        self.assertEqual(rc, 2)
        self.assertEqual(_counts(self.db_path), before, "指纹不匹配必须零删除")

    def test_unknown_argument_errors(self):
        rc, _ = self._run(["--bogus"])
        self.assertEqual(rc, 2)

    # ---- 正常路径 ----
    def test_correct_fingerprint_clears_and_leaves_one_audit_row(self):
        rc, text = self._run(["--yes", "--fingerprint", self.fp,
                              "--users", "2", "--admin-accounts", "1",
                              "--events-per-day", "1", "--days", "1"])
        self.assertEqual(rc, 0, text)
        after = _counts(self.db_path)
        self.assertGreaterEqual(after["accounts"], 2, "应重建 demo 账号")
        conn = sqlite3.connect(self.db_path)
        try:
            purges = conn.execute(
                "SELECT COUNT(*) FROM audit_logs WHERE action='demo_purge'").fetchone()[0]
            kept = conn.execute(
                "SELECT COUNT(*) FROM audit_logs WHERE detail='keep'").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(purges, 1, "清空后应恰好落一条清库留痕")
        self.assertEqual(kept, 0, "旧 demo 审计行必须随 audit_logs 一并清空")
        self.assertIn("audit_logs", text, "应显式说明审计表被清空")

    # ---- 审计 fail-closed ----
    def test_audit_failure_aborts_purge(self):
        before = _counts(self.db_path)
        with mock.patch.object(generate_demo_data.db, "audit", return_value=False):
            rc, _ = self._run(["--yes", "--fingerprint", self.fp, "--users", "2"])
        self.assertNotEqual(rc, 0, "审计写入失败必须让清库失败")
        self.assertEqual(_counts(self.db_path), before, "审计失败必须零删除（fail-closed）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
