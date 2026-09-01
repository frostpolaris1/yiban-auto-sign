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

    def test_replace_accounts_clears_sign_events(self):
        """批次15 P2-1：TUI 整表保存（replace_accounts）移除的账号必须连带清 sign_events。"""
        self._add("13900000005", owner="keep@test.local")
        self._add("13900000006", owner="drop@test.local")
        db.add_sign_event("2026-08-28 06:36:00", "13900000005", "success")
        db.add_sign_event("2026-08-28 06:36:01", "13900000006", "success")
        # 整表替换：只保留 13900000005（模拟 TUI 删除 13900000006 后保存）
        from scripts import db as _db  # noqa: F401  (db 已在 setUpClass 导入)
        rows = [r for r in db.load_accounts_raw() if r["phone"] == "13900000005"]
        kept = {
            "name": rows[0]["name"], "phone": rows[0]["phone"],
            "password": "pw-1", "phone_model": "", "phone_code": "",
            "owner": "keep@test.local", "status": "active",
        }
        db.replace_accounts([kept])
        self.assertEqual(self._event_count("13900000005"), 1, "保留账号的事件不清理")
        self.assertEqual(self._event_count("13900000006"), 0,
                         "replace_accounts 移除账号后 sign_events 不应残留明文手机号（批次15 P2-1）")

    # ---------------- M3：时钟跳变保护 ----------------
    def test_clock_jump_forward_blocked(self):
        """系统时间比上次记录前进超过 72h → 必须跳过清理并告警。"""
        conn = db.get_conn()
        with db._conn_lock:
            yesterday = (datetime.now() - timedelta(days=4)).strftime("%Y-%m-%d %H:%M:%S")
            conn.execute("INSERT OR REPLACE INTO app_meta (key, value) VALUES (?,?)",
                         ("test_clock_fwd", yesterday))
            # 2026-09-01 性能修复：INSERT 后立即提交释放写锁——否则守卫跳变分支
            # 开第二连接写告警（_record_clock_guard_alert）与本连接未提交事务
            # 争锁超时 5s（全量串行 3 用例各 +5s，生产调用点 guard 前无前置写）。
            conn.commit()
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
            conn.commit()  # 同上：释放写锁，避免守卫告警连接争锁超时
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
            conn.commit()  # 同上：释放写锁，避免 purge 内守卫告警连接争锁超时
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
            # 批次8 P1-2：遗留未提交事务现在会被安全回滚（不再被盲提交）——
            # 夹具自提交，不依赖任何后续函数的隐式提交
            conn.commit()
        purged = db.purge_deleted_users_hard(["victim3@test.local"])
        self.assertEqual(purged, ["victim3@test.local"])
        self.assertEqual(self._req_count("victim3@test.local"), 0,
                         "管理员手动清除后冷却计数也应连带清除（M4a）")


class LastAdminAndUpdateUserTest(unittest.TestCase):
    """C-M1 update_user 行数 + C-M3 最后管理员事务内复核。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-lastadmin-")
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

    # ---------------- C-M1 ----------------
    def test_update_user_nonexistent_returns_zero(self):
        affected = db.update_user("nobody@test.local", {"mail_notify": 0})
        self.assertEqual(affected, 0, "不存在的邮箱 update_user 必须返回 0（原返回 None 静默 no-op）")

    def test_update_user_existing_returns_one(self):
        db.create_user("someone@test.local", "hash", role="user",
                       created_at="2026-08-28 00:00:00", pw_version=1)
        affected = db.update_user("someone@test.local", {"mail_notify": 0})
        self.assertEqual(affected, 1)

    def test_update_user_soft_deleted_returns_zero(self):
        """已注销用户行也不可更新（deleted=0 过滤），返回 0。"""
        db.create_user("gone@test.local", "hash", role="user",
                       created_at="2026-08-28 00:00:00", pw_version=1)
        db.soft_delete_user_with_accounts("gone@test.local")
        affected = db.update_user("gone@test.local", {"mail_notify": 0})
        self.assertEqual(affected, 0)

    # ---------------- C-M3 ----------------
    def test_last_registered_admin_cannot_soft_delete(self):
        db.create_user("admin1@test.local", "hash", role="admin",
                       created_at="2026-08-28 00:00:00", pw_version=1)
        with self.assertRaises(db.LastAdminError):
            db.soft_delete_user_with_accounts("admin1@test.local")
        # 注销未发生
        rows = [u for u in db.load_users(include_deleted=True)
                if u["email"] == "admin1@test.local"]
        self.assertTrue(rows and not rows[0].get("deleted"), "最后管理员不得被软注销")

    def test_second_admin_can_soft_delete(self):
        db.create_user("admin1@test.local", "hash", role="admin",
                       created_at="2026-08-28 00:00:00", pw_version=1)
        db.create_user("admin2@test.local", "hash", role="admin",
                       created_at="2026-08-28 00:00:00", pw_version=1)
        self.assertTrue(db.soft_delete_user_with_accounts("admin1@test.local"),
                        "存在第二个管理员时允许注销")
        rows = [u for u in db.load_users(include_deleted=True)
                if u["email"] == "admin1@test.local"]
        self.assertTrue(rows[0].get("deleted"))

    def test_regular_user_soft_delete_unaffected(self):
        db.create_user("admin1@test.local", "hash", role="admin",
                       created_at="2026-08-28 00:00:00", pw_version=1)
        db.create_user("user1@test.local", "hash", role="user",
                       created_at="2026-08-28 00:00:00", pw_version=1)
        self.assertTrue(db.soft_delete_user_with_accounts("user1@test.local"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
