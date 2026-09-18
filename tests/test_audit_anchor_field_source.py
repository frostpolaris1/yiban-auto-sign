# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""锚点行的字段同源与"篡改/重链"诊断的可达性。

两条都是取证路径上的确定性缺陷，与并发/时序无关：

1. 锚点行的 `head` 原先用"当前最后一行"另取一次。两次读之间落进一条审计写入时，
   锚点行的 `max_id` 与 `head` 指向不同行——此后每次校验都报"链尾内容被篡改"。
   修法是把 head **按 max_id 取值**，与 max_id 天然同源。
2. 链尾哈希与锚点不符时的诊断摘要函数被用两个实参调用、定义只收一个，这条分支
   必然抛 TypeError，被外层兜成"锚点校验异常"——检测结论还在，但"内容篡改 vs
   全表重链"的区分整条丢掉。
"""

import contextlib
import os
import shutil
import sqlite3
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TEST_KEY = "a" * 64
AUDIT_KEY = "b" * 64


class _Fixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-anchor-source-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\nYIBAN_AUDIT_KEY={AUDIT_KEY}\n")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls._old = {}
        for k, v in (("YIBAN_ACCOUNTS_KEY", TEST_KEY), ("YIBAN_AUDIT_KEY", AUDIT_KEY),
                     ("YIBAN_ENV_FILE", cls.env_file), ("YIBAN_DB_FILE", cls.db_file),
                     ("YIBAN_ACCOUNTS_FILE", os.path.join(cls.tmp, "accounts.json")),
                     ("YIBAN_STATE_DIR", cls.tmp)):
            cls._old[k] = os.environ.get(k)
            os.environ[k] = v
        global db
        import db

    @classmethod
    def tearDownClass(cls):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        for k, v in cls._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        for suffix in ("", "-wal", "-shm"):
            with contextlib.suppress(OSError):
                os.remove(self.db_file + suffix)
        self.anchor = db.audit_anchor_path()
        with contextlib.suppress(OSError):
            os.remove(self.anchor)
        db.init_db(cleanup=False)

    def _anchor_row(self):
        """锚点文件的最后一行 → dict。字段**从行尾取**（时间戳自带空格，行首分段会错位）。"""
        with open(self.anchor, encoding="utf-8") as f:
            lines = [ln for ln in f.read().splitlines() if ln.strip()]
        parts = lines[-1].split()
        head = parts[-2]
        max_id = int(parts[-5])
        count = int(parts[-4])
        return {"max_id": max_id, "head": head, "count": count}


class AnchorFieldCoherenceTest(_Fixture):
    """锚点行的 head 必须与 max_id 指向同一行，即使期间有并发写入。"""

    def test_head_matches_the_row_recorded_as_max_id(self):
        for i in range(3):
            db.audit("tester", "seed", f"t{i}", "d")
        conn = db.get_conn()
        real_total = db._audit_purge_total

        def _inject(conn_arg):
            """模拟"两次读之间落进一条审计写入"（用另一条连接，绕开进程内锁）。"""
            out = real_total(conn_arg)
            other = sqlite3.connect(self.db_file)
            try:
                row = other.execute(
                    "SELECT hash FROM audit_logs ORDER BY id DESC LIMIT 1").fetchone()
                prev = row[0] if row else ""
                ts = "2026-09-18 20:00:00"
                # 真实并发写入是**已签名**的：空 hash 的行会被"拒绝写锚点行"拦下，
                # 那就测不到本用例要测的字段同源问题了。
                h = db._audit_hash(prev, ts, "racer", "concurrent", "t", "d")
                other.execute(
                    "INSERT INTO audit_logs (ts, username, action, target, detail, prev_hash, hash) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (ts, "racer", "concurrent", "t", "d", prev, h))
                other.commit()
            finally:
                other.close()
            return out

        db._audit_purge_total = _inject
        try:
            db.record_audit_anchor(self.anchor)
        finally:
            db._audit_purge_total = real_total

        rec = self._anchor_row()
        got = conn.execute("SELECT hash FROM audit_logs WHERE id=?",
                           (rec["max_id"],)).fetchone()
        self.assertIsNotNone(got, "锚点记的 max_id 必须真实存在")
        self.assertEqual(
            rec["head"], (got["hash"] or ""),
            "锚点行的 head 与它记的 max_id 不是同一行——并发写入落在两次读之间时，"
            "此后每次校验都会误报「链尾内容被篡改」",
        )


class RechainDiagnosisReachableTest(_Fixture):
    """链尾哈希与锚点不符时，必须给出"篡改 / 全表重链"的诊断，而不是异常文本。"""

    def test_hash_mismatch_yields_diagnosis_not_exception(self):
        for i in range(3):
            db.audit("tester", "seed", f"t{i}", "d")
        db.record_audit_anchor(self.anchor)
        # 只改链尾行的 hash：行还在（定点判据能命中），但内容与锚点不符
        conn = db.get_conn()
        conn.execute("UPDATE audit_logs SET hash=? WHERE id=(SELECT MAX(id) FROM audit_logs)",
                     ("f" * 64,))
        conn.commit()
        ok, msg = db.verify_audit_anchor(self.anchor)
        self.assertFalse(ok, "链尾内容被改过必须判失败")
        self.assertNotIn("锚点校验异常", msg, f"诊断不该被异常吞掉：{msg}")
        self.assertTrue(
            ("全表重链" in msg) or ("篡改" in msg),
            f"应给出「内容篡改 / 全表重链」的区分：{msg}",
        )

    def test_rechain_hint_is_callable_with_anchor_only(self):
        """签名固定为单参：多传一个实参会抛 TypeError，被外层兜成通用异常。"""
        import inspect
        self.assertEqual(len(inspect.signature(db._rechain_hint).parameters), 1)
        self.assertIsInstance(db._rechain_hint({"ts": "2026-01-01 00:00:00"}), str)


if __name__ == "__main__":
    unittest.main()
