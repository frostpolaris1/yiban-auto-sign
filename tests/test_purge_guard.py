# -*- coding: utf-8 -*-
"""清库/删除入口的公共防线：目标指纹、确认回显、清库留痕 fail-closed。

标签：G · 安全：脱敏/审计/配置注入
覆盖：`yiban/store/purge_guard.py` 的四件事——目标指纹由库**内容**派生（行数不同则
    指纹不同、同一内容稳定、库缺失不建文件）；确认回显按逐字比对；清库留痕复用审计
    链且失败返回 False（库缺失/审计失败都 False，绝不静默放行）。
对应实现：`yiban/store/purge_guard.py`（`db_content_fingerprint` /
    `content_fingerprint` / `confirmation_ok` / `write_purge_audit`）。
关键断言：指纹必须能区分"demo/压测库"与"误指的生产库"（行数/规模差异天然可辨），
    故断言的是"内容变了指纹就变"，而不是"路径不同指纹就不同"；审计失败必须让调用方
    拿到 False（fail-closed），否则清库会在无留痕的情况下照删。
依赖：临时目录建真实 schema 库（走数据层）+ 直接 SQL 造行；无网络、无子进程、
    无 bash/docker。手机号一律 138****0000 形态。
"""
import os
import shutil
import sqlite3
import tempfile
import unittest
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from yiban.store import purge_guard  # noqa: E402


def _touch_sqlite(path, rows):
    """在 path 建一个最小 SQLite 库并写入给定表行数（不要求真 schema）。"""
    conn = sqlite3.connect(path)
    try:
        for table, n in rows.items():
            conn.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY, v TEXT)")
            for i in range(n):
                conn.execute(f"INSERT INTO {table} (v) VALUES (?)", (f"r{i}",))
        conn.commit()
    finally:
        conn.close()


class DbContentFingerprintTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-purge-guard-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_same_target_is_stable_but_content_sensitive(self):
        path = os.path.join(self.tmp, "a.db")
        _touch_sqlite(path, {"accounts": 3, "users": 2})
        f1, _ = purge_guard.db_content_fingerprint(path)
        f2, _ = purge_guard.db_content_fingerprint(path)
        self.assertEqual(f1, f2, "同一目标重复计算必须稳定")
        # 内容一变（这里是同一路径上多了一行）指纹必须跟着变——指纹是内容派生的，
        # 不是"把路径字符串拿来比"。
        with sqlite3.connect(path) as c:
            c.execute("INSERT INTO accounts (v) VALUES ('r3')")
        f3, _ = purge_guard.db_content_fingerprint(path)
        self.assertNotEqual(f1, f3, "行数变化必须改变指纹（路径相同也不算同一个目标）")

    def test_different_scale_changes_fingerprint(self):
        """行数/规模差异必须改变指纹——这正是它区分 demo 库与生产库的地方。"""
        small = os.path.join(self.tmp, "small.db")
        big = os.path.join(self.tmp, "big.db")
        _touch_sqlite(small, {"accounts": 3})
        _touch_sqlite(big, {"accounts": 5000})
        fs, _ = purge_guard.db_content_fingerprint(small)
        fb, _ = purge_guard.db_content_fingerprint(big)
        self.assertNotEqual(fs, fb, "账号规模不同 ⇒ 指纹必须不同")

    def test_missing_db_does_not_create_file(self):
        path = os.path.join(self.tmp, "absent.db")
        fp, lines = purge_guard.db_content_fingerprint(path)
        self.assertTrue(fp.startswith("PURGE-"))
        self.assertFalse(os.path.exists(path), "指纹计算不得建库")
        self.assertTrue(lines, "应给出人类可读的目标摘要")

    def test_summary_mentions_target_and_counts(self):
        path = os.path.join(self.tmp, "demo.db")
        _touch_sqlite(path, {"accounts": 2, "audit_logs": 1})
        _fp, lines = purge_guard.db_content_fingerprint(path)
        joined = "\n".join(lines)
        self.assertIn("accounts=2", joined)
        self.assertIn("audit_logs=1", joined)

    def test_special_char_path_is_still_readable(self):
        """含空格的路径也要能只读到行数（只读 URI 经 pathlib 转义，不拼裸路径）。"""
        d = os.path.join(self.tmp, "dir with space")
        os.makedirs(d)
        path = os.path.join(d, "demo db.db")
        _touch_sqlite(path, {"accounts": 2})
        self.assertEqual(purge_guard.table_counts(path)["accounts"], 2)

    def test_unreadable_target_is_marked_not_readable(self):
        """只读连接打不开时摘要须标「不可读」，不得退化成读写连接读出数字。"""
        d = os.path.join(self.tmp, "adir")
        os.makedirs(d)  # 目录不是库：只读连接必然打不开
        fp, lines = purge_guard.db_content_fingerprint(d)
        self.assertTrue(fp.startswith("PURGE-"))
        self.assertTrue(any("不可读" in ln for ln in lines), lines)
        self.assertNotIn("accounts=1", "\n".join(lines))


class ContentFingerprintTest(unittest.TestCase):
    def test_parts_drive_digest(self):
        a = purge_guard.content_fingerprint("state", ["/var/log/yiban", "3", "x"])
        b = purge_guard.content_fingerprint("state", ["/var/log/yiban", "4", "x"])
        self.assertNotEqual(a, b)
        self.assertTrue(a.startswith("PURGE-"))


class ConfirmationTest(unittest.TestCase):
    def test_requires_exact_echo(self):
        fp = "PURGE-abc123"
        self.assertTrue(purge_guard.confirmation_ok(fp, fp))
        self.assertTrue(purge_guard.confirmation_ok(fp, f"  {fp}  "))
        self.assertFalse(purge_guard.confirmation_ok(fp, ""))
        self.assertFalse(purge_guard.confirmation_ok(fp, "PURGE-abc124"))


class WritePurgeAuditTest(unittest.TestCase):
    KEY = "a" * 64
    AUDIT_KEY = "b" * 64

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-purge-audit-")
        self.env_file = os.path.join(self.tmp, ".env")
        with open(self.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={self.KEY}\nYIBAN_AUDIT_KEY={self.AUDIT_KEY}\n")
        self.db_file = os.path.join(self.tmp, "yiban.db")
        self._close()

    def tearDown(self):
        self._close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def _close():
        from yiban.store import connection
        conn = connection.current()
        if conn is not None:
            conn.close()
        connection.reset_conn()

    def _make_db(self):
        from yiban.store import db as store_db
        store_db.init_db(db_file=self.db_file, env_file=self.env_file, cleanup=False)
        store_db.get_conn().close()
        from yiban.store import connection
        connection.reset_conn()

    def test_missing_db_returns_false(self):
        path = os.path.join(self.tmp, "absent.db")
        self.assertFalse(purge_guard.write_purge_audit(
            "state_purge", "fp", "detail", db_file=path, env_file=self.env_file))
        self.assertFalse(os.path.exists(path), "审计不得顺手建库")

    def test_success_writes_one_row(self):
        self._make_db()
        ok = purge_guard.write_purge_audit(
            "state_purge", "PURGE-x", "清理 3 个文件",
            db_file=self.db_file, env_file=self.env_file)
        self.assertTrue(ok)
        conn = sqlite3.connect(self.db_file)
        try:
            n = conn.execute(
                "SELECT COUNT(*) FROM audit_logs WHERE action='state_purge'").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(n, 1)

    def test_audit_failure_returns_false(self):
        self._make_db()
        from yiban.store import db as store_db
        with mock.patch.object(store_db, "audit", return_value=False):
            ok = purge_guard.write_purge_audit(
                "state_purge", "PURGE-x", "d",
                db_file=self.db_file, env_file=self.env_file)
        self.assertFalse(ok, "审计写入失败必须让调用方拿到 False（fail-closed）")

    def test_connection_pointing_elsewhere_is_refused(self):
        """单例连接指向别的库时不得把留痕写过去：按实际连接判定，返回 False。"""
        self._make_db()
        other = os.path.join(self.tmp, "other.db")
        from yiban.store import connection
        from yiban.store import db as store_db
        conn = connection.current()
        if conn is not None:
            conn.close()
        connection.reset_conn()
        store_db.init_db(db_file=other, env_file=self.env_file, cleanup=False)
        try:
            ok = purge_guard.write_purge_audit(
                "state_purge", "PURGE-x", "d",
                db_file=self.db_file, env_file=self.env_file)
            self.assertFalse(ok, "连接指向别的库时必须拒绝留痕（否则审计落到别处）")
            n = connection.current().execute(
                "SELECT COUNT(*) FROM audit_logs WHERE action='state_purge'").fetchone()[0]
            self.assertEqual(n, 0, "不得把留痕落到别的库")
        finally:
            c = connection.current()
            if c is not None:
                c.close()
            connection.reset_conn()


if __name__ == "__main__":
    unittest.main(verbosity=2)
