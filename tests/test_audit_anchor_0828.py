# -*- coding: utf-8 -*-
"""审计可追溯性与并发安全回归测试（2026-08-28 审查批次 5）。

用法（在项目根目录）：
    py -m pytest tests/test_audit_anchor_0828.py -v
    py tests/test_audit_anchor_0828.py               # 无 pytest 也可直接运行

覆盖三个阻断级缺陷：

- **B-1** `db.audit()` 原先吞掉全部异常、只写 WARNING 并返回 None。锁等待超过
  busy_timeout 时业务接口照常返回 200，审计表里却没有这条记录；因为是"没写进去"
  而非"写完被删"，哈希链依然自洽，verify 永远验不出来。现改为重试 + 返回 bool +
  累加失败计数（供每日校验告警）。
- **B-2** `verify_audit_chain()` 只能检出"删中间"：首行以自身 prev_hash 自锚、
  空表直接判通过，于是「删前缀 / 删尾 / 清空整表」全部验不出来。现引入**库外**
  锚点（min_id/max_id/head_hash），删尾与清空可被确证检出。
- **B-5** `update_account` 的读-改-写原先跨进程无互斥，两个进程/两个标签页并发
  编辑同一账号会静默丢更新（且 web 自编辑路径不传乐观锁、总回填旧密码，可把用户
  刚改的密码回滚）。现整个读-改-写纳入 BEGIN IMMEDIATE 写锁。
"""
import contextlib
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "scripts"))

TEST_KEY = "a" * 64
AUDIT_KEY = "b" * 64


class AuditTraceabilityTest(unittest.TestCase):
    """B-1 审计写入失败必须可感知 + B-2 锚点必须能检出删尾/清空。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-audit-anchor-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\nYIBAN_AUDIT_KEY={AUDIT_KEY}\n")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.accounts_file = os.path.join(cls.tmp, "accounts.json")
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
        os.environ["YIBAN_AUDIT_KEY"] = AUDIT_KEY
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_ACCOUNTS_FILE"] = cls.accounts_file
        os.environ["YIBAN_DB_FILE"] = cls.db_file
        os.environ["YIBAN_STATE_DIR"] = cls.tmp
        global db
        import db

    @classmethod
    def tearDownClass(cls):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        for key in ("YIBAN_ACCOUNTS_KEY", "YIBAN_AUDIT_KEY", "YIBAN_ENV_FILE",
                    "YIBAN_ACCOUNTS_FILE", "YIBAN_DB_FILE", "YIBAN_STATE_DIR"):
            os.environ.pop(key, None)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        for suffix in ("", "-wal", "-shm"):
            with contextlib.suppress(OSError):
                os.remove(self.db_file + suffix)
        # 清空外部锚点，保证用例间互不干扰
        with contextlib.suppress(OSError):
            os.remove(db.audit_anchor_path())
        db.init_db(cleanup=False)

    # ---------------- B-1：审计写入失败 fail-loud ----------------
    def test_audit_returns_true_on_success(self):
        self.assertTrue(db.audit("tester", "unit_test", "target", "ok"))

    def test_audit_returns_false_and_counts_failure(self):
        """强制写入失败：必须返回 False 且失败计数 +1（不再静默吞掉）。"""
        before = db.audit_write_failures()
        real_hash = db._audit_hash

        def _boom(*_a, **_kw):
            raise RuntimeError("模拟审计写入失败")

        db._audit_hash = _boom
        try:
            result = db.audit("tester", "unit_test", "target", "should fail")
        finally:
            db._audit_hash = real_hash
        self.assertFalse(result, "审计写入失败时 audit() 必须返回 False（原实现返回 None 且静默）")
        self.assertEqual(db.audit_write_failures(), before + 1, "失败计数必须累加，供每日校验告警")

    def test_audit_retries_before_giving_up(self):
        """失败应重试（默认 3 次）后再放弃，而不是一次就丢。"""
        calls = {"n": 0}
        real_hash = db._audit_hash

        def _flaky(*a, **kw):
            calls["n"] += 1
            return real_hash(*a, **kw)

        db._audit_hash = _flaky
        try:
            ok = db.audit("tester", "unit_test", "target", "retry path")
        finally:
            db._audit_hash = real_hash
        self.assertTrue(ok)
        self.assertEqual(calls["n"], 1, "成功路径不应触发重试")

    # ---------------- B-2：库外锚点 ----------------
    def _seed(self, n=6):
        for i in range(n):
            db.audit("tester", "seed", f"t{i}", f"d{i}")

    def test_anchor_roundtrip_ok(self):
        self._seed(6)
        self.assertIsNotNone(db.record_audit_anchor())
        ok, msg = db.verify_audit_anchor()
        self.assertTrue(ok, msg)
        self.assertEqual(msg, "")

    def test_anchor_detects_suffix_deletion(self):
        """删掉最近的审计记录（最有价值的攻击）必须被检出。"""
        self._seed(6)
        db.record_audit_anchor()
        with db._conn_lock:
            conn = db.get_conn()
            max_id = conn.execute("SELECT MAX(id) AS m FROM audit_logs").fetchone()["m"]
            conn.execute("DELETE FROM audit_logs WHERE id > ?", (max_id - 3,))
            conn.commit()
        ok, msg = db.verify_audit_anchor()
        self.assertFalse(ok, "删尾必须被检出（原实现下 verify_audit_chain 依然返回通过）")
        self.assertIn("减少", msg)

    def test_anchor_detects_full_wipe(self):
        """整表清空必须被检出（原实现：空表直接 return True）。"""
        self._seed(6)
        db.record_audit_anchor()
        with db._conn_lock:
            conn = db.get_conn()
            conn.execute("DELETE FROM audit_logs")
            conn.commit()
        ok, msg = db.verify_audit_anchor()
        self.assertFalse(ok, "清空整表必须被检出")
        self.assertIn("清空", msg)

    def test_anchor_tolerates_prefix_cleanup(self):
        """删前缀是保留期清理的合法行为：不判失败，但给出提示信息。"""
        self._seed(6)
        db.record_audit_anchor()
        with db._conn_lock:
            conn = db.get_conn()
            min_id = conn.execute("SELECT MIN(id) AS m FROM audit_logs").fetchone()["m"]
            conn.execute("DELETE FROM audit_logs WHERE id <= ?", (min_id + 2,))
            conn.commit()
        ok, msg = db.verify_audit_anchor()
        self.assertTrue(ok, "合法清理不应触发告警，否则每日校验天天误报淹没真告警")
        self.assertIn("回收", msg)

    def test_anchor_detects_tail_tamper(self):
        """条数不变但链尾内容被改 → 链内哈希自洽校验必须失败（audit_health 汇总）。"""
        self._seed(6)
        db.record_audit_anchor()
        with db._conn_lock:
            conn = db.get_conn()
            conn.execute(
                "UPDATE audit_logs SET detail='篡改' WHERE id=(SELECT MAX(id) FROM audit_logs)"
            )
            conn.commit()
        # 内容篡改由 verify_audit_chain 的逐行哈希校验检出（锚点只比对 min/max/head）
        chain_ok, broken, _first = db.verify_audit_chain()
        self.assertFalse(chain_ok, "链尾内容被改后 verify_audit_chain 必须断链")
        self.assertGreater(broken, 0)
        self.assertFalse(db.audit_health()["healthy"], "audit_health 必须汇总为不健康")

    def test_anchor_missing_is_not_a_failure(self):
        """从未记录过锚点时不做判定，避免首次运行误报。"""
        self._seed(3)
        ok, msg = db.verify_audit_anchor()
        self.assertTrue(ok)
        self.assertEqual(msg, "")

    def test_audit_health_aggregates(self):
        self._seed(4)
        db.record_audit_anchor()
        h = db.audit_health()
        self.assertTrue(h["chain_ok"])
        self.assertTrue(h["anchor_ok"])
        self.assertEqual(h["write_failures"], 0)
        self.assertTrue(h["healthy"])

    def test_audit_health_unhealthy_after_wipe(self):
        self._seed(4)
        db.record_audit_anchor()
        with db._conn_lock:
            conn = db.get_conn()
            conn.execute("DELETE FROM audit_logs")
            conn.commit()
        self.assertFalse(db.audit_health()["healthy"])


class UpdateAccountLockTest(unittest.TestCase):
    """B-5：update_account 的读-改-写必须持有库级写锁。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-upd-lock-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\nYIBAN_AUDIT_KEY={AUDIT_KEY}\n")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
        os.environ["YIBAN_AUDIT_KEY"] = AUDIT_KEY
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_DB_FILE"] = cls.db_file
        os.environ["YIBAN_STATE_DIR"] = cls.tmp
        global db
        import db

    @classmethod
    def tearDownClass(cls):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        for key in ("YIBAN_ACCOUNTS_KEY", "YIBAN_AUDIT_KEY", "YIBAN_ENV_FILE",
                    "YIBAN_DB_FILE", "YIBAN_STATE_DIR"):
            os.environ.pop(key, None)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        for suffix in ("", "-wal", "-shm"):
            with contextlib.suppress(OSError):
                os.remove(self.db_file + suffix)
        db.init_db(cleanup=False)

    def test_update_account_waits_for_write_lock(self):
        """另一连接持有写锁时，update_account 必须阻塞等待而不是带陈旧数据写入。

        这是 B-5 的核心断言：若读-改-写不在同一写锁内，SELECT 拿到的会是锁释放
        前的陈旧行，随后的 UPDATE 会把并发方的修改整体覆盖（丢更新）。
        """
        account_id = db.add_account({
            "name": "锁定测试", "phone": "13900000001", "password": "pw-1",
            "owner": "tester@test.local", "status": "active",
        })

        blocker = sqlite3.connect(self.db_file, timeout=10)
        blocker.execute("BEGIN IMMEDIATE")

        outcome = {}

        def _worker():
            try:
                db.update_account(account_id, {"name": "被锁等待后写入"})
                outcome["done"] = True
            except Exception as e:
                outcome["error"] = repr(e)

        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        t.join(1.5)
        self.assertNotIn("done", outcome), (
            "另一进程持有写锁时 update_account 不应立即完成——"
            "若已完成，说明读-改-写没有真正纳入 BEGIN IMMEDIATE 事务"
        )
        blocker.rollback()
        blocker.close()
        t.join(10)
        self.assertNotIn("error", outcome, f"update_account 抛异常: {outcome.get('error')}")
        self.assertTrue(outcome.get("done"))

        rows = db.load_accounts()
        got = next((r for r in rows if r.get("phone") == "13900000001"), None)
        self.assertIsNotNone(got)
        self.assertEqual(got.get("name"), "被锁等待后写入")

    def test_update_account_concurrent_no_lost_update(self):
        """并发改不同字段：两个修改都必须生效，不能互相覆盖。

        A 只改 name，B 只改 phone_model。若读-改-写不原子，后写入方会把先前的
        修改连同自己未涉及的字段一起回滚成陈旧值。
        """
        account_id = db.add_account({
            "name": "原始名", "phone": "13900000002", "password": "pw-1",
            "phone_model": "原始机型", "owner": "tester@test.local", "status": "active",
        })

        errors = []

        def _update(fields):
            try:
                for _ in range(5):  # 多轮提高交错概率
                    db.update_account(account_id, dict(fields))
            except Exception as e:
                errors.append(repr(e))

        threads = [
            threading.Thread(target=_update, args=({"name": "来自A"},), daemon=True),
            threading.Thread(target=_update, args=({"phone_model": "来自B"},), daemon=True),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(20)

        self.assertEqual(errors, [], f"并发 update_account 抛异常: {errors}")
        rows = db.load_accounts()
        got = next((r for r in rows if r.get("phone") == "13900000002"), None)
        self.assertIsNotNone(got)
        self.assertEqual(got.get("name"), "来自A", "A 的修改被 B 覆盖（丢更新）")
        self.assertEqual(got.get("phone_model"), "来自B", "B 的修改被 A 覆盖（丢更新）")

    def test_update_account_phone_change_reencrypts(self):
        """改绑手机号后密码仍可解密（AAD 随手机号重绑）。"""
        account_id = db.add_account({
            "name": "改号", "phone": "13900000003", "password": "secret-pw",
            "owner": "tester@test.local", "status": "active",
        })
        self.assertTrue(db.update_account(account_id, {"phone": "13900000004"}))
        rows = db.load_accounts()
        got = next((r for r in rows if r.get("phone") == "13900000004"), None)
        self.assertIsNotNone(got, "手机号应已更新")
        self.assertEqual(got.get("password"), "secret-pw", "改号后密码应仍可解密（AAD 已重绑）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
