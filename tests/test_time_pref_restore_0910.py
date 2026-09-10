# -*- coding: utf-8 -*-
"""批次20 D 项回归：两条软删路径对 time_prefs 的处置必须一致（2026-09-10）。

背景（对抗性审查批次20 H2-F2，已活体复现）：同样是"7 天内可反悔"的可逆软删，
- 账号级软删（set_account_deleted）**保留** time_prefs → 恢复后自选还在；
- 用户级注销（soft_delete_user_with_accounts）原先**物理删除** time_prefs → 恢复后自选丢失。
同一字段在两条可逆入口行为相反，用户无法预期。

修复口径：**软删阶段一律保留，仅在物理清除时连带清理**。
- 保留安全性依据：`time_pref_stats()` 本就按 `accounts.deleted = 0` 过滤，
  软删账号残留 pref 不会虚高拥挤度；物理清除路径（`_purge_expired_deleted` /
  `purge_deleted_users_hard` / `delete_accounts_by_owner`）都会按 phone 清 prefs，
  因此不会留下孤儿。

覆盖：
1. 账号级软删 → 恢复：自选保留；
2. 用户级注销 → 恢复：自选保留（本次修复点）；
3. 过期物理清除仍连带清 prefs（无孤儿）；
4. 管理员硬清除已注销用户仍连带清 prefs；
5. 拥挤度统计不计入软删账号的自选（既有语义不得回退）。
"""
import contextlib
import datetime
import os
import shutil
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import db  # noqa: E402

TEST_KEY = "a" * 64
USER = "pref-user@test.local"
PHONE = "13900001234"


class TimePrefRestoreConsistencyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-timepref-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.accounts_file = os.path.join(cls.tmp, "accounts.json")
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_ACCOUNTS_FILE"] = cls.accounts_file
        os.environ["YIBAN_DB_FILE"] = cls.db_file
        os.environ["YIBAN_DISABLE_PURGE_LOOP"] = "1"

    @classmethod
    def tearDownClass(cls):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        for k in ("YIBAN_ACCOUNTS_KEY", "YIBAN_ENV_FILE", "YIBAN_ACCOUNTS_FILE",
                  "YIBAN_DB_FILE", "YIBAN_DISABLE_PURGE_LOOP"):
            os.environ.pop(k, None)
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

    def _seed(self, phone=PHONE, slot=390):
        db.create_user(USER, "hash", created_at="2026-09-10")
        db.add_account({
            "name": "测试账号", "phone": phone, "password": "p1",
            "phone_model": "", "phone_code": "", "owner": USER,
            "status": "active", "reject_reason": "",
        })
        db.set_time_pref(phone, slot, "2026-09-10 10:00:00")
        row = next(a for a in db.load_accounts_raw() if a["phone"] == phone)
        return row["id"]

    def _age_deleted_at(self, days):
        """把用户与账号的 deleted_at 回拨到保留期之外（模拟宽限期已过）。"""
        stale = (datetime.datetime.now() - datetime.timedelta(days=days)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        conn = db.get_conn()
        with db._conn_lock, conn:
            conn.execute("UPDATE accounts SET deleted_at=? WHERE deleted=1", (stale,))
            conn.execute("UPDATE users SET deleted_at=? WHERE deleted=1", (stale,))
            conn.commit()

    # ---- 1. 账号级软删 → 恢复：自选保留（既有行为，防回退） ----
    def test_account_level_soft_delete_keeps_pref(self):
        acc_id = self._seed()
        db.set_account_deleted(acc_id, 1, "2026-09-10 11:00:00", deleted_by=USER)
        db.set_account_deleted(acc_id, 0)
        self.assertIsNotNone(
            db.get_time_pref(PHONE), "账号级软删恢复后自选应保留"
        )

    # ---- 2. 用户级注销 → 恢复：自选保留（本次修复点，原先会丢） ----
    def test_user_cancel_keeps_pref_across_restore(self):
        self._seed()
        self.assertTrue(db.soft_delete_user_with_accounts(USER))
        self.assertIsNotNone(
            db.get_time_pref(PHONE),
            "注销（软删）阶段不应清除自选时间片——与账号级软删口径统一",
        )
        self.assertTrue(db.restore_user(USER))
        pref = db.get_time_pref(PHONE)
        self.assertIsNotNone(pref, "注销后恢复应带回自选时间片（可逆操作完整可逆）")
        self.assertEqual(pref["slot_min"], 390)

    # ---- 3. 过期物理清除仍连带清 prefs（不留孤儿） ----
    def test_expired_purge_still_clears_pref(self):
        self._seed()
        db.soft_delete_user_with_accounts(USER)
        self._age_deleted_at(db.SOFT_DELETE_RETENTION_DAYS + 1)
        db._purge_expired_deleted(db.get_conn())
        self.assertIsNone(
            db.get_time_pref(PHONE),
            "物理清除后必须连带清理自选，否则会留下无主 pref",
        )

    # ---- 4. 管理员硬清除已注销用户仍连带清 prefs ----
    def test_hard_purge_still_clears_pref(self):
        self._seed()
        db.soft_delete_user_with_accounts(USER)
        purged = db.purge_deleted_users_hard([USER])
        self.assertEqual(purged, [USER])
        self.assertIsNone(
            db.get_time_pref(PHONE), "管理员硬清除后自选应连带清理"
        )

    # ---- 5. 拥挤度统计不计入软删账号的自选（既有语义不得回退） ----
    def test_time_pref_stats_ignores_soft_deleted(self):
        self._seed()
        before = [s for s in db.time_pref_stats() if s["slot_min"] == 390]
        self.assertEqual(before, [{"slot_min": 390, "count": 1}])
        db.soft_delete_user_with_accounts(USER)
        after = [s for s in db.time_pref_stats() if s["slot_min"] == 390]
        self.assertEqual(
            after, [],
            "软删账号的残留 pref 不得计入拥挤度（否则占位会虚高）",
        )


if __name__ == "__main__":
    unittest.main()
