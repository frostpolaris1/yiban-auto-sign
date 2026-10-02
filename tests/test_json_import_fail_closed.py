# -*- coding: utf-8 -*-
"""M22：JSON→SQLite 自动导入**丢行即 fail-closed**（不把源文件改成 .bak）。

标签：A · 存储：迁移/导入
覆盖：`_maybe_migrate` 的逐文件计数口径、丢行时保留源文件原样 + 审计留痕、
   不丢行时照常改名 .bak，以及审计行与已导入行同事务。

对应实现：yiban/store/migrations.py（`_maybe_migrate`、`_dropped_source_keys`、
   `_audit_json_import_loss`、`_mask_key_sample`、`_in_chunks`）。

关键断言：`INSERT OR IGNORE` 撞唯一约束时**不报错、不留痕**，行被静默丢掉。若随后仍把
   源文件改名 `.bak`，管理员看到的是"迁移完成"而源文件已不在原路径——丢掉的账号再也找不
   回来，即"丢数据不留痕"。故验收要两面都成立：

   - 冲突 → **不**改名（源文件仍在原路径），且审计链里有这一条；
   - 无冲突 → 正常改名 `.bak`。

   冲突面：`accounts.phone` UNIQUE、v2 的 `idx_accounts_owner_live`（owner 部分唯一，
   `deleted=0 AND owner NOT IN ('', 'admin')`）、`idx_users_email_live`（email 部分唯一）。
   库此时必为空（`has_db_rows` 早退），所以冲突只可能来自**源文件内部**的重复。

依赖：临时 sqlite（db.init_db 迁移建表）+ 临时 JSON；不触网。整文件在本机执行，无 skip。
"""
import contextlib
import glob
import json
import os
import shutil
import tempfile
import unittest

import db

from yiban.store import migrations as migrations_mod

TEST_KEY = "a" * 64


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-jsonimp-")

    @classmethod
    def tearDownClass(cls):
        cls._close_conn()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @staticmethod
    def _close_conn():
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None

    def setUp(self):
        self.work = tempfile.mkdtemp(dir=self.tmp, prefix="case-")
        self.addCleanup(shutil.rmtree, self.work, ignore_errors=True)
        self.db_file = os.path.join(self.work, "yiban.db")
        self.env_file = os.path.join(self.work, ".env")
        with open(self.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n")
        os.environ.update({
            "YIBAN_ACCOUNTS_KEY": TEST_KEY,
            "YIBAN_ENV_FILE": self.env_file,
            "YIBAN_DB_FILE": self.db_file,
            "YIBAN_STATE_DIR": self.work,
        })
        self.addCleanup(self._pop_env)
        # 必须先关旧连接再 init_db（否则会写进上一个用例的库文件）
        self._close_conn()
        db.init_db(self.db_file, env_file=self.env_file, cleanup=False, migrate=True)
        self.addCleanup(self._close_conn)
        self.accounts_json = os.path.join(self.work, "accounts.json")
        self.users_json = os.path.join(self.work, "users.json")

    def _pop_env(self):
        for k in ("YIBAN_ACCOUNTS_KEY", "YIBAN_ENV_FILE", "YIBAN_DB_FILE",
                  "YIBAN_STATE_DIR"):
            os.environ.pop(k, None)

    def _write_accounts(self, rows):
        with open(self.accounts_json, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False)

    def _write_users(self, rows):
        with open(self.users_json, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False)

    def _run(self):
        migrations_mod._maybe_migrate(db.get_conn(), self.accounts_json)

    def _account_phones(self):
        return [r[0] for r in db.get_conn().execute(
            "SELECT phone FROM accounts ORDER BY id").fetchall()]

    def _user_emails(self):
        return [r[0] for r in db.get_conn().execute(
            "SELECT email FROM users ORDER BY id").fetchall()]

    def _audit_rows(self):
        return [dict(r) for r in db.get_conn().execute(
            "SELECT action, target, detail FROM audit_logs ORDER BY id").fetchall()]

    def _bak_files(self):
        return sorted(glob.glob(self.accounts_json + ".bak*")
                      + glob.glob(self.users_json + ".bak*"))


class PhoneConflictTest(_Base):
    """`accounts.phone` UNIQUE 冲突（源文件内部重复手机号）。"""

    def _conflicting(self):
        self._write_accounts([
            {"phone": "13800000001", "name": "甲", "password": "p1"},
            {"phone": "13800000002", "name": "乙", "password": "p2"},
            {"phone": "13800000001", "name": "甲重复", "password": "p3"},
        ])

    def test_conflict_keeps_source_file_in_place(self):
        self._conflicting()
        self._run()
        self.assertTrue(os.path.exists(self.accounts_json),
                        "丢行时源 JSON 必须留在原路径：它仍是『数据还在』的唯一证据")
        self.assertEqual(self._bak_files(), [],
                         "丢行时不得改名 .bak（否则丢掉的行再也找不回来）")

    def test_conflict_writes_audit_row_naming_the_keys(self):
        self._conflicting()
        self._run()
        rows = [r for r in self._audit_rows() if r["action"] == "json_import_row_loss"]
        self.assertEqual(len(rows), 1, "丢行必须留一条审计（否则仍是丢数据不留痕）")
        detail = rows[0]["detail"]
        self.assertIn("accounts", detail)
        self.assertIn("138****0001", detail,
                      "审计 detail 必须点名冲突键（掩码形态），否则管理员无从下手")
        self.assertNotIn("13800000001", detail, "审计里不得落手机号明文")

    def test_conflict_still_commits_the_rows_that_did_land(self):
        """唯一性冲突的那一行丢，其余行照常入库并提交（否则一次冲突丢掉整个账号表）。"""
        self._conflicting()
        self._run()
        self.assertEqual(self._account_phones(), ["13800000001", "13800000002"])

    def test_no_conflict_renames_source_file(self):
        """反面：无冲突时行为逐字不变 —— 照常改名 .bak。"""
        self._write_accounts([
            {"phone": "13800000001", "name": "甲", "password": "p1"},
            {"phone": "13800000002", "name": "乙", "password": "p2"},
        ])
        self._run()
        self.assertFalse(os.path.exists(self.accounts_json),
                         "无冲突时源文件必须离开原路径（.bak 逃生门）")
        self.assertEqual(len(self._bak_files()), 1)
        self.assertEqual(self._account_phones(), ["13800000001", "13800000002"])
        self.assertEqual([r for r in self._audit_rows()
                          if r["action"] == "json_import_row_loss"], [],
                         "无冲突不得写丢行审计（否则告警成了噪声）")


class OwnerPartialUniqueTest(_Base):
    """v2 的 `idx_accounts_owner_live`：同 owner 的两条未删除账号撞部分唯一索引。

    这是评审点名的第二个冲突面：`phone` 不同但 owner 相同，`OR IGNORE` 照样丢行。
    """

    def test_owner_conflict_keeps_source_file(self):
        self._write_accounts([
            {"phone": "13800000001", "owner": "u@x.com", "password": "p1"},
            {"phone": "13800000002", "owner": "u@x.com", "password": "p2"},
        ])
        self._run()
        self.assertTrue(os.path.exists(self.accounts_json),
                        "owner 部分唯一冲突同样不得改名 .bak")
        self.assertEqual(self._bak_files(), [])
        rows = [r for r in self._audit_rows() if r["action"] == "json_import_row_loss"]
        self.assertEqual(len(rows), 1, "owner 冲突也必须留痕")
        self.assertIn("138****0002", rows[0]["detail"])

    def test_same_owner_allowed_when_rows_are_deleted_or_admin(self):
        """索引的 `WHERE deleted=0 AND owner NOT IN ('','admin')` 边界不得被误伤。

        这三条都不该丢行：软删的、admin 的、空 owner 的。若判据写错成"owner 相同即冲突"，
        常规部署（全部 owner=admin）会全表丢行——比原缺陷更严重。
        """
        self._write_accounts([
            {"phone": "13800000001", "owner": "admin", "password": "p1"},
            {"phone": "13800000002", "owner": "", "password": "p2"},
            {"phone": "13800000003", "owner": "u@x.com", "password": "p3", "deleted": 1},
        ])
        self._run()
        self.assertEqual(self._account_phones(),
                         ["13800000001", "13800000002", "13800000003"],
                         "索引 WHERE 之外的组合不得丢行")
        self.assertFalse(os.path.exists(self.accounts_json),
                         "无冲突时仍应正常改名 .bak")


class UserEmailConflictTest(_Base):
    """`idx_users_email_live`（email 部分唯一）：用户侧同样要 fail-closed。"""

    def test_email_conflict_keeps_users_source_file(self):
        self._write_users([
            {"email": "a@x.com", "password_hash": "h1"},
            {"email": "a@x.com", "password_hash": "h2"},
        ])
        self._run()
        self.assertTrue(os.path.exists(self.users_json),
                        "用户侧丢行同样不得改名 .bak")
        self.assertEqual(self._bak_files(), [])
        rows = [r for r in self._audit_rows() if r["action"] == "json_import_row_loss"]
        self.assertEqual(len(rows), 1)
        self.assertIn("users", rows[0]["detail"])

    def test_users_no_conflict_renames(self):
        self._write_users([
            {"email": "a@x.com", "password_hash": "h1"},
            {"email": "b@x.com", "password_hash": "h2"},
        ])
        self._run()
        self.assertFalse(os.path.exists(self.users_json))
        self.assertEqual(self._user_emails(), ["a@x.com", "b@x.com"])


class PerFileScopeTest(_Base):
    """逐文件口径：accounts 丢行不得牵连 users 的改名（也不得反过来）。"""

    def test_loss_in_accounts_does_not_block_users_rename(self):
        self._write_accounts([
            {"phone": "13800000001", "owner": "admin", "password": "p1"},
            {"phone": "13800000001", "owner": "admin", "password": "p2"},
        ])
        self._write_users([{"email": "a@x.com", "password_hash": "h1"}])
        self._run()
        self.assertTrue(os.path.exists(self.accounts_json), "accounts 丢行 ⇒ 保留")
        self.assertFalse(os.path.exists(self.users_json),
                         "users 自己没丢行 ⇒ 照常改名（不该被兄弟文件的失败连坐）")
        self.assertEqual(self._user_emails(), ["a@x.com"])


if __name__ == "__main__":
    unittest.main()
