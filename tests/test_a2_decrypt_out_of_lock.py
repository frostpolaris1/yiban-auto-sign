# -*- coding: utf-8 -*-
"""A2：账号解密移出 `_conn_lock`（2026-09-15）。

背景：`db.load_accounts()` 原先全程持 `_conn_lock` 逐行做 AES-GCM 解密，而全项目有
数十个调用点，导致**全站 DB 访问被串行化**（实测 `/api/accounts` 恒定 28 rps 而
CPU 仅 0.66 核 → 锁瓶颈而非 CPU 瓶颈）。

改后契约（本文件逐条断言）：
1. 解密期间 `_conn_lock` 可被其他线程立即取得（即解密确实不在锁内）；
2. 并发读与改绑手机号（会换 AAD）交错时读路径不崩、不返回半成品；
3. AAD 失配时重取快照重试一次；重试仍失败则原样抛出（不掩盖真实损坏）；
4. `accounts_snapshot()`（不解密）与 `load_accounts()`（解密）口径一致；
5. 明文驻留的幂等加密自愈在拆出锁外后仍然生效。
"""
import contextlib
import os
import shutil
import tempfile
import threading
import unittest
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TEST_KEY = "a" * 64


class A2DecryptOutOfLockTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-a2-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
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
        os.environ.pop("YIBAN_ACCOUNTS_KEY", None)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        for suffix in ("", "-wal", "-shm"):
            p = self.db_file + suffix
            if os.path.exists(p):
                os.remove(p)
        db.init_db(self.db_file, env_file=self.env_file, cleanup=False)

    def _add(self, phone, password="pw"):
        return db.add_account({"name": phone[-4:], "phone": phone, "password": password})

    # ---- 1. 核心性质：解密不在 _conn_lock 内 ----
    def test_decrypt_runs_outside_conn_lock(self):
        self._add("13800138001")
        real = db._decrypt_row
        probed = []

        def _spy(row):
            # 解密进行中，另一个线程应能立刻拿到 _conn_lock；
            # 若解密仍在锁内，acquire 会超时失败（False）
            def _probe():
                got = db._conn_lock.acquire(timeout=2.0)
                probed.append(got)
                if got:
                    db._conn_lock.release()

            t = threading.Thread(target=_probe)
            t.start()
            t.join(timeout=3.0)
            return real(row)

        with mock.patch.object(db, "_decrypt_row", side_effect=_spy):
            accts = db.load_accounts()
        self.assertTrue(accts, "应至少解出一行")
        self.assertTrue(probed, "_decrypt_row 应被调用")
        self.assertTrue(
            all(probed),
            "解密期间 _conn_lock 必须能被其他线程取得（即解密不在锁内）",
        )

    # ---- 2. 并发读 + 改绑手机号（换 AAD）不得崩 ----
    def test_concurrent_load_and_phone_rebind_no_crash(self):
        ids = [self._add(f"1380013810{i}", f"pw{i}") for i in range(6)]
        stop = threading.Event()
        errors = []

        def _reader():
            while not stop.is_set():
                try:
                    accts = db.load_accounts()
                    for a in accts:
                        if not a.get("phone"):
                            errors.append(AssertionError("返回了半成品行（phone 为空）"))
                except Exception as e:
                    errors.append(e)

        threads = [threading.Thread(target=_reader) for _ in range(4)]
        for t in threads:
            t.start()
        try:
            for _ in range(15):
                for i, aid in enumerate(ids[:3]):
                    db.update_account(aid, {"phone": f"139001382{i:02d}", "password": "npw"})
        finally:
            stop.set()
            for t in threads:
                t.join(timeout=15)

        self.assertEqual(errors, [], f"并发读不得抛错：{errors[:3]}")

    # ---- 3. AAD 失配重试一次；重试仍失败则抛出 ----
    def test_read_accounts_retries_once_on_runtime_error(self):
        calls = []

        def _flaky(rows):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("AAD 失配（模拟并发改绑手机号）")
            return ["ok"]

        with mock.patch.object(db, "decrypt_account_rows", side_effect=_flaky):
            self.assertEqual(db.read_accounts(db.accounts_snapshot), ["ok"])
        self.assertEqual(len(calls), 2, "首次失败后应重取快照重试一次")

    def test_read_accounts_raises_after_retry_fails(self):
        with mock.patch.object(
            db, "decrypt_account_rows", side_effect=RuntimeError("密文真实损坏")
        ), self.assertRaises(RuntimeError):
            db.read_accounts(db.accounts_snapshot)

    # ---- 4. 快照（不解密）与解密读口径一致 ----
    def test_snapshot_and_load_accounts_agree_on_order_and_identity(self):
        for i in range(4):
            self._add(f"1380013830{i}", f"pw{i}")
        snap = db.accounts_snapshot()
        accts = db.load_accounts()
        self.assertEqual([r["id"] for r in snap], [a["id"] for a in accts])
        self.assertEqual([r["phone"] for r in snap], [a["phone"] for a in accts])
        self.assertTrue(all(a["password"].startswith("pw") for a in accts), "应已解密为明文")
        self.assertTrue(all(isinstance(r["deleted"], bool) for r in snap))

    # ---- 5. 明文自愈在锁外解密后仍然生效 ----
    def test_plaintext_self_heal_still_persists(self):
        conn = db.get_conn()
        conn.execute(
            "INSERT INTO accounts (sort_order, name, phone, password) "
            "VALUES (1, '甲', '13800138099', 'PlainPW')"
        )
        conn.commit()
        with self.assertLogs("yiban.store.accounts", level="WARNING") as cm:
            accts = db.load_accounts()
        self.assertEqual(accts[0]["password"], "PlainPW", "明文值照常可用（不阻断业务）")
        self.assertTrue(any("已自动加密回写" in m for m in cm.output), cm.output)
        row = db.accounts_snapshot()[0]
        self.assertNotIn("PlainPW", str(row["password"]), "自愈必须落盘（不得只在内存）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
