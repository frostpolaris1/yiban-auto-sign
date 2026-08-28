# -*- coding: utf-8 -*-
"""清理残留与时钟保护回归测试（2026-08-28 审查批次 6）。

用法（在项目根目录）：
    py -m pytest tests/test_cleanup_residue_0828.py -v
    py tests/test_cleanup_residue_0828.py

覆盖：
- **M2** 账号物理删除后 sign_events 明文手机号残留（原保留至 180 天期满）；
- **M3** 系统时钟异常跳变时物理清理必须被跳过（防"拨快后刚软删的数据被立即清除"）；
- **M4a** 用户被物理删除后 user_delete_requests 明文邮箱残留（原保留 30 天）。
"""
import contextlib
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "scripts"))

TEST_KEY = "a" * 64
AUDIT_KEY = "b" * 64


class CleanupResidueTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-residue-")
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

    def _add(self, phone, owner="t@test.local"):
        return db.add_account({
            "name": phone, "phone": phone, "password": "pw-1",
            "owner": owner, "status": "active",
        })

    def _event_count(self, phone):
        with db._conn_lock:
            conn = db.get_conn()
            return conn.execute(
                "SELECT COUNT(*) FROM sign_events WHERE phone=?", (phone,)
            ).fetchone()[0]

    def _req_count(self, email):
        with db._conn_lock:
            conn = db.get_conn()
            return conn.execute(
                "SELECT COUNT(*) FROM user_delete_requests WHERE username=?", (email,)
            ).fetchone()[0]

    # ---------------- M2：删号连带清 sign_events ----------------
    def test_purge_account_clears_sign_events(self):
        aid = self._add("13900000001")
        db.add_sign_event("2026-08-28 06:31:00", "13900000001", "success")
        self.assertEqual(self._event_count("13900000001"), 1)
        db.purge_account(aid)
        self.assertEqual(self._event_count("13900000001"), 0,
                         "purge_account 后 sign_events 不应残留明文手机号（M2）")

    def test_delete_user_with_accounts_clears_sign_events(self):
        self._add("13900000002", owner="victim@test.local")
        db.add_sign_event("2026-08-28 06:32:00", "13900000002", "failed")
        db.delete_user_with_accounts("victim@test.local")
        self.assertEqual(self._event_count("13900000002"), 0)

    def test_batch_purge_clears_sign_events(self):
        aid = self._add("13900000003")
        db.add_sign_event("2026-08-28 06:33:00", "13900000003", "success")
        db.batch_account_ops([("purge", aid)])
        self.assertEqual(self._event_count("13900000003"), 0)

    def test_soft_delete_keeps_events(self):
        """软删除（宽限期内可恢复）不清理事件——恢复后历史仍需保留。"""
        aid = self._add("13900000004")
        db.add_sign_event("2026-08-28 06:34:00", "13900000004", "success")
        db.set_account_deleted(aid, True, "2026-08-28 06:35:00")
        self.assertEqual(self._event_count("13900000004"), 1, "软删除不应清理事件")

    # ---------------- M3：时钟跳变保护 ----------------
    def test_clock_jump_forward_blocked(self):
        """系统时间比上次记录前进超过 72h → 必须跳过清理并告警。"""
        conn = db.get_conn()
        with db._conn_lock:
            yesterday = (datetime.now() - timedelta(days=4)).strftime("%Y-%m-%d %H:%M:%S")
            conn.execute("INSERT OR REPLACE INTO app_meta (key, value) VALUES (?,?)",
                         ("test_clock_fwd", yesterday))
            ok, note = db._clock_jump_guard(conn, "test_clock_fwd")
        self.assertFalse(ok, "前进 4 天（>72h）必须被判定为跳变")
        self.assertIn("跳变", note)

    def test_clock_jump_backward_blocked(self):
        """系统时间比上次记录回拨超过 1h → 跳过清理。"""
        conn = db.get_conn()
        with db._conn_lock:
            later = (datetime.now() + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
            conn.execute("INSERT OR REPLACE INTO app_meta (key, value) VALUES (?,?)",
                         ("test_clock_back", later))
            ok, _note = db._clock_jump_guard(conn, "test_clock_back")
        self.assertFalse(ok, "回拨 2h（>1h）必须被判定为跳变")

    def test_clock_normal_passes(self):
        """正常间隔（1h）→ 放行并更新参照。"""
        conn = db.get_conn()
        with db._conn_lock:
            hour_ago = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
            conn.execute("INSERT OR REPLACE INTO app_meta (key, value) VALUES (?,?)",
                         ("test_clock_ok", hour_ago))
            ok, note = db._clock_jump_guard(conn, "test_clock_ok")
        self.assertTrue(ok, note)
        self.assertEqual(note, "")

    def test_purge_accounts_skips_on_clock_jump(self):
        """拨快后 purge_expired_deleted_accounts 必须整体跳过（不物理清除）。"""
        # 记录一次"上次运行时刻" = 现在 - 10 天 → 本次调用视为跳变
        aid = self._add("13900000005")
        old = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S")
        db.set_account_deleted(aid, True, old)  # 已软删且超 7 天保留期
        conn = db.get_conn()
        with db._conn_lock:
            conn.execute("INSERT OR REPLACE INTO app_meta (key, value) VALUES (?,?)",
                         ("purge_accounts_clock", old))
        db.purge_expired_deleted_accounts()  # 应因跳变跳过，账号保留
        rows = db.load_accounts_raw()
        self.assertTrue(
            any(r["phone"] == "13900000005" for r in rows),
            "时钟跳变时 purge 必须跳过——否则刚软删的数据被拨快后立即物理清除",
        )

    # ---------------- M4a：删用户连带清冷却计数 ----------------
    def test_purge_deleted_users_clears_delete_requests(self):
        db.create_user("victim2@test.local", "hash", role="user",
                       created_at="2026-08-28 00:00:00", pw_version=1)
        db.record_user_delete_request("victim2@test.local", ip_hash="x", kind="delete")
        self.assertEqual(self._req_count("victim2@test.local"), 1)
        # 直接模拟"宽限期已过"：把 deleted_at 拨到 10 天前
        conn = db.get_conn()
        with db._conn_lock:
            old = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S")
            conn.execute("UPDATE users SET deleted=1, deleted_at=? WHERE email=?",
                         (old, "victim2@test.local"))
        # 时钟参照设为正常（1 小时前），避免 M3 误拦
        hour_ago = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        with db._conn_lock:
            conn.execute("INSERT OR REPLACE INTO app_meta (key, value) VALUES (?,?)",
                         ("purge_users_clock", hour_ago))
        db.purge_deleted_users()
        self.assertEqual(self._req_count("victim2@test.local"), 0,
                         "purge_deleted_users 后 user_delete_requests 不应残留明文邮箱（M4a）")

    def test_purge_deleted_users_hard_clears_delete_requests(self):
        db.create_user("victim3@test.local", "hash", role="user",
                       created_at="2026-08-28 00:00:00", pw_version=1)
        db.record_user_delete_request("victim3@test.local", ip_hash="x", kind="restore")
        conn = db.get_conn()
        with db._conn_lock:
            conn.execute("UPDATE users SET deleted=1, deleted_at=? WHERE email=?",
                         ("2026-08-28 00:00:00", "victim3@test.local"))
        purged = db.purge_deleted_users_hard(["victim3@test.local"])
        self.assertEqual(purged, ["victim3@test.local"])
        self.assertEqual(self._req_count("victim3@test.local"), 0,
                         "管理员手动清除后冷却计数也应连带清除（M4a）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
