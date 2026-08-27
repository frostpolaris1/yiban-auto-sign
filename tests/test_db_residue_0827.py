# -*- coding: utf-8 -*-
"""2026-08-27 批次3 回归测试：自愈收口 + 文件残留三类。

覆盖：
- _row_to_account 无连接上下文时日志如实（不谎称已回写）
- _rename_backup 同日已有 .bak 时源文件仍离开原路径（追加序号）
- 迁移遇损坏 JSON 显式记 ERROR（不误报"无数据"）
- 删除/改绑路径连带清理 session_cache（不再以旧手机号永久驻留）
- update_account IntegrityError 回滚后重放明文自愈（凭据不留明文）
"""
import contextlib
import datetime as _dt
import glob
import json
import os
import shutil
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "scripts"))

import db  # noqa: E402


class DbResidueTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-residue-")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write("YIBAN_ACCOUNTS_KEY=" + "a" * 64 + "\n")
        os.environ["YIBAN_DB_FILE"] = cls.db_file
        os.environ["YIBAN_ENV_FILE"] = cls.env_file

    @classmethod
    def tearDownClass(cls):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        os.environ.pop("YIBAN_DB_FILE", None)
        os.environ.pop("YIBAN_ENV_FILE", None)
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
        for p in glob.glob(os.path.join(self.tmp, "*.json*")):
            with contextlib.suppress(OSError):
                os.remove(p)

    # ---- 1. 删除/改绑路径连带清理 session_cache ----
    def test_purge_account_clears_session_cache(self):
        db.init_db(self.db_file, env_file=self.env_file, cleanup=False)
        aid = db.add_account({"name": "A", "phone": "13800138000", "password": "p1",
                              "status": "active", "owner": "admin"})
        db.set_session_cache("13800138000", '{"cookie": "x"}', "csrf1")
        self.assertIsNotNone(db.get_session_cache("13800138000"), "前置：缓存已写入")
        db.purge_account(aid)
        self.assertIsNone(db.get_session_cache("13800138000"),
                          "物理删除账号应连带清除会话缓存")

    def test_delete_user_with_accounts_clears_session_cache(self):
        db.init_db(self.db_file, env_file=self.env_file, cleanup=False)
        db.create_user("owner@test.local", "hashx")
        db.add_account({"name": "A", "phone": "13800138001", "password": "p1",
                        "status": "active", "owner": "owner@test.local"})
        db.set_session_cache("13800138001", '{"cookie": "x"}', "csrf1")
        self.assertIsNotNone(db.get_session_cache("13800138001"))
        db.delete_user_with_accounts("owner@test.local")
        self.assertIsNone(db.get_session_cache("13800138001"),
                          "删除用户账号应连带清除会话缓存")

    def test_soft_delete_user_clears_session_cache(self):
        db.init_db(self.db_file, env_file=self.env_file, cleanup=False)
        db.create_user("soft@test.local", "hashx")
        db.add_account({"name": "A", "phone": "13800138003", "password": "p1",
                        "status": "active", "owner": "soft@test.local"})
        db.set_session_cache("13800138003", '{"cookie": "x"}', "csrf1")
        self.assertTrue(db.soft_delete_user_with_accounts("soft@test.local"))
        self.assertIsNone(db.get_session_cache("13800138003"),
                          "软注销应停用对应会话缓存")

    def test_update_phone_change_clears_old_cache(self):
        db.init_db(self.db_file, env_file=self.env_file, cleanup=False)
        aid = db.add_account({"name": "A", "phone": "13800138002", "password": "p1",
                              "status": "active", "owner": "admin"})
        db.set_session_cache("13800138002", '{"cookie": "x"}', "csrf1")
        db.update_account(aid, {"phone": "13899998888"})
        self.assertIsNone(db.get_session_cache("13800138002"),
                          "改绑手机号应清除旧号会话缓存")

    # ---- 2. _rename_backup 同日残留 ----
    def test_rename_backup_disambiguates_same_day(self):
        src = os.path.join(self.tmp, "accounts.json")
        with open(src, "w", encoding="utf-8") as f:
            json.dump([{"phone": "13800138000", "password": "plain"}], f)
        bak0 = src + ".bak-" + _dt.datetime.now().strftime("%Y%m%d")
        with open(bak0, "w", encoding="utf-8") as f:
            f.write("[]")  # 预置今日已存在的同名 .bak，制造冲突
        key = db.account_crypto.load_key(self.env_file)
        db._rename_backup(src, reencrypt=True, key=key)
        self.assertFalse(os.path.exists(src), "源文件必须离开原路径（不得明文驻留）")
        self.assertEqual(len(glob.glob(src + ".bak-*")), 2, "应追加序号生成第二个备份")

    # ---- 3. 迁移损坏 JSON 显式告警 ----
    def test_migrate_corrupt_json_logs_error(self):
        accounts_json = os.path.join(self.tmp, "accounts.json")
        with open(accounts_json, "w", encoding="utf-8") as f:
            f.write("{ this is not valid json !!!")
        with self.assertLogs("yiban.db", level="ERROR") as cm:
            db.init_db(self.db_file, migrate_from=accounts_json,
                       env_file=self.env_file, cleanup=False)
        joined = "\n".join(cm.output)
        self.assertIn("读取/解析失败", joined, "损坏 JSON 必须显式记 ERROR")
        self.assertNotIn("无 JSON 数据可迁移", joined, "不得误报为无数据")

    # ---- 4. update_account IntegrityError 回滚后重放明文自愈 ----
    def test_update_integrity_error_reheals_plaintext(self):
        conn = db.init_db(self.db_file, env_file=self.env_file, cleanup=False)
        conn.execute(
            "INSERT INTO accounts (sort_order, name, phone, password, status, owner) "
            "VALUES (1, '甲', '13800138010', 'PlainPW1', 'active', 'admin')"
        )
        conn.commit()  # 先提交明文行，避免与 add_account 的 BEGIN IMMEDIATE 冲突
        db.add_account({"name": "乙", "phone": "13800138011", "password": "p2",
                        "status": "active", "owner": "admin"})
        row = conn.execute("SELECT id FROM accounts WHERE phone='13800138010'").fetchone()
        with self.assertRaises(db.DuplicatePhoneError):
            db.update_account(row["id"], {"phone": "13800138011"})  # 撞账号2
        raw = {r["phone"]: r for r in db.load_accounts_raw()}
        self.assertTrue(db._is_encrypted_value(raw["13800138010"]["password"]),
                        "IntegrityError 回滚后明文自愈应被重放持久化，凭据不留明文")

    # ---- 5. _row_to_account 无连接时日志如实 ----
    def test_row_to_account_no_conn_logs_no_writeback(self):
        conn = db.init_db(self.db_file, env_file=self.env_file, cleanup=False)
        conn.execute(
            "INSERT INTO accounts (sort_order, name, phone, password) "
            "VALUES (1, '甲', '13800138020', 'PlainPW2')"
        )
        conn.commit()
        row = conn.execute("SELECT * FROM accounts WHERE phone='13800138020'").fetchone()
        with self.assertLogs("yiban.db", level="WARNING") as cm:
            db._row_to_account(row)  # conn 缺省 → 不回写
        joined = "\n".join(cm.output)
        self.assertIn("明文存储", joined)
        self.assertIn("未回写", joined, "无连接时应如实说明未回写，不得谎称已回写")


if __name__ == "__main__":
    unittest.main()
