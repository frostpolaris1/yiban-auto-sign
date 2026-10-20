# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""管理员手动物理清除：同邮箱的多张已注销用户行必须一次删净。

工单 `ba-p02-05`：`purge_deleted_users_hard` 按邮箱 `fetchone()` 取"第一张已注销行"
（无 ORDER/LIMIT），DELETE 又按 `id=?`。而 `users.email` 自 v5 起只有活跃行部分唯一
（`ON users(email) WHERE deleted = 0`），同邮箱可以有一张活跃行 + 多张已注销行。
旧实现只删一张：邮箱与 password_hash 残留、`purged` 却按邮箱记成"已清除"，
管理员"立刻彻底清除这个人"的入口静默做了一半，审计留痕失真。

修法：按 email 一次 DELETE `deleted=1`，按实际 rowcount 判定是否计入 purged。

标签：C · 存储：迁移与库完整性
覆盖：同邮箱多张软删行的物理清除、活跃行不受影响、账号与会话缓存连带清理。
对应实现：`yiban/adapters/persistence/users.py` 的 `purge_deleted_users_hard`。
关键断言：purge 返回后的库状态里，该邮箱**没有任何** deleted=1 行残留；活跃行原样保留；
两台手机号的 session_cache 与 accounts 行都被连带清除。
依赖：临时库 + 临时 `.env`；无网络、无 skip。
"""
import contextlib
import os
import shutil
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TEST_KEY = "a" * 64
EMAIL = "erase@test.local"
PHONE1 = "13800138011"
PHONE2 = "13800138012"


class _Fixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-users-hard-purge-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls._old = {}
        for k, v in (("YIBAN_ACCOUNTS_KEY", TEST_KEY), ("YIBAN_ENV_FILE", cls.env_file),
                     ("YIBAN_DB_FILE", cls.db_file),
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
        db.init_db(self.db_file, env_file=self.env_file, cleanup=False)

    def _count(self, sql, args=()):
        return db.get_conn().execute(sql, args).fetchone()[0]

    def _add_account(self, phone):
        return db.add_account({
            "name": "测试", "phone": phone, "password": "pw", "owner": EMAIL,
            "status": "active", "phone_model": "", "phone_code": "",
        })

    def _register_then_delete(self, phone):
        """活跃用户 + 活跃账号 → 用户注销（账号随软删）。返回软删账号 id。"""
        self.assertTrue(db.create_user(EMAIL, "hash"))
        aid = self._add_account(phone)
        self.assertTrue(db.soft_delete_user_with_accounts(EMAIL))
        return aid


class HardPurgeAllRowsForEmailTest(_Fixture):
    def test_two_soft_deleted_rows_are_both_purged(self):
        self._register_then_delete(PHONE1)
        self._register_then_delete(PHONE2)
        # 模拟登录链在软删之后写回的陈旧凭据缓存（物理清除必须连带清掉）
        db.set_session_cache(PHONE1, '{"sid":"a"}', "c1")
        db.set_session_cache(PHONE2, '{"sid":"b"}', "c2")
        self.assertEqual(
            self._count("SELECT COUNT(*) FROM users WHERE email=? AND deleted=1", (EMAIL,)),
            2, "前置：同邮箱存在两张已注销用户行",
        )

        purged = db.purge_deleted_users_hard(
            [EMAIL], audit_spec={"username": "admin", "action": "admin_purge_user"})

        self.assertEqual(purged, [EMAIL])
        self.assertEqual(
            self._count("SELECT COUNT(*) FROM users WHERE email=? AND deleted=1", (EMAIL,)),
            0, "同邮箱的全部已注销行必须一次删净（旧实现只删第一张）",
        )
        self.assertEqual(self._count("SELECT COUNT(*) FROM accounts WHERE owner=?", (EMAIL,)),
                         0, "软删账号行必须连带清除")
        self.assertIsNone(db.get_session_cache(PHONE1), "凭据缓存必须连带清除")
        self.assertIsNone(db.get_session_cache(PHONE2), "凭据缓存必须连带清除")
        row = db.get_conn().execute(
            "SELECT detail FROM audit_logs WHERE action='admin_purge_user' "
            "ORDER BY id DESC LIMIT 1").fetchone()
        self.assertIsNotNone(row, "清除必须留审计")
        self.assertIn("1 个已注销用户", row["detail"],
                      "按邮箱计一次；同一邮箱的多行不得被记成多个用户")

    def test_active_row_with_same_email_is_untouched(self):
        self._register_then_delete(PHONE1)
        self.assertTrue(db.create_user(EMAIL, "hash2"), "前置：同邮箱重新注册出活跃行")
        self.assertEqual(db.purge_deleted_users_hard([EMAIL]), [EMAIL])
        self.assertEqual(
            self._count("SELECT COUNT(*) FROM users WHERE email=? AND deleted=0", (EMAIL,)),
            1, "活跃行不得被手动物理清除误删",
        )
        self.assertEqual(
            self._count("SELECT COUNT(*) FROM users WHERE email=? AND deleted=1", (EMAIL,)),
            0,
        )

    def test_email_without_deleted_rows_is_not_reported(self):
        self.assertTrue(db.create_user(EMAIL, "hash"))
        self.assertEqual(db.purge_deleted_users_hard([EMAIL]), [],
                         "一行未清不得计入 purged（留痕按实际结果）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
