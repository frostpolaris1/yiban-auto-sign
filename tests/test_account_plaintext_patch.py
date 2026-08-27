# -*- coding: utf-8 -*-
"""2026-08-27 对抗性审查补丁测试：账号凭据明文驻留三缺口。

- 缺口 1：_row_to_account 对明文行告警 + 幂等加密回写（对照 session_cache 的 M14 抛错清除）
- 缺口 2：迁移 .bak 逃生门含明文时重写加密版，.bak 一律 0600
- 缺口 3：legacy 环境变量明文凭据加载告警（告警内容不含明文）
"""
import contextlib
import glob
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "scripts"))

import db  # noqa: E402
import signin  # noqa: E402


class PlaintextPatchTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-plaintext-")
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
        # 清理上次用例残留的迁移 JSON / .bak（_maybe_migrate 要求文件名恰为 accounts.json）
        for p in glob.glob(os.path.join(self.tmp, "accounts*.json*")):
            with contextlib.suppress(OSError):
                os.remove(p)

    # ---- 缺口 1：读路径明文自愈 ----
    def test_load_accounts_heals_plaintext_row(self):
        conn = db.init_db(self.db_file, env_file=self.env_file, cleanup=False)
        conn.execute(
            "INSERT INTO accounts (sort_order, name, phone, password, phone_code) "
            "VALUES (1, '张三', '13800138000', 'MyPass123', 'code456')"
        )
        conn.commit()
        with self.assertLogs("yiban.db", level="WARNING") as cm:
            accounts = db.load_accounts()
        self.assertEqual(accounts[0]["password"], "MyPass123", "明文值本次照常可用")
        self.assertEqual(accounts[0]["phone_code"], "code456")
        self.assertTrue(any("明文存储" in m for m in cm.output), cm.output)
        # 库内已加密（幂等回写，持久化）
        raw = db.load_accounts_raw()[0]
        self.assertTrue(db._is_encrypted_value(raw["password"]))
        self.assertTrue(db._is_encrypted_value(raw["phone_code"]))
        # 二次读取不再告警（已自愈）
        with self.assertNoLogs("yiban.db", level="WARNING"):
            db.load_accounts()

    def test_load_accounts_decrypt_error_rolls_back_dangling_txn(self):
        """P1：某行解密抛错时不得在共享连接留下悬挂事务（2026-08-27）。

        行1 明文（触发自愈 UPDATE，开启隐式事务）+ 行2 损坏密文（解密必失败）。
        load_accounts 抛出后：后续 BEGIN IMMEDIATE 写路径必须仍可用，
        且行1 的半自愈回写随整批回滚（原子性：要么全愈要么全不动）。
        """
        conn = db.init_db(self.db_file, env_file=self.env_file, cleanup=False)
        conn.execute(
            "INSERT INTO accounts (sort_order, name, phone, password) "
            "VALUES (1, '甲', '13800138000', 'PlainPass1')"
        )
        bad = json.dumps({
            "v": 1, "nonce": "00" * 12, "ct": "ab" * 3, "tag": "00" * 16,
        })
        conn.execute(
            "INSERT INTO accounts (sort_order, name, phone, password) "
            "VALUES (2, '乙', '13800138001', ?)", (bad,)
        )
        conn.commit()
        with self.assertRaises(RuntimeError):
            db.load_accounts()
        # 关键断言：悬挂事务已回滚——BEGIN IMMEDIATE 写路径不再连锁报错
        c = db.get_conn()
        c.execute("BEGIN IMMEDIATE")
        c.execute("UPDATE accounts SET name='ok' WHERE phone='13800138000'")
        c.commit()
        # 行1 的自愈回写随整批回滚，库内仍是明文（未残留半自愈状态）
        raw = {r["phone"]: r for r in db.load_accounts_raw()}
        self.assertFalse(
            db._is_encrypted_value(raw["13800138000"]["password"]),
            "整批解密失败时应整体回滚，不残留半自愈状态",
        )

    # ---- 缺口 2：迁移 .bak ----
    def test_migrate_bak_reencrypted_and_0600(self):
        accounts_json = os.path.join(self.tmp, "accounts.json")
        with open(accounts_json, "w", encoding="utf-8") as f:
            json.dump([
                {"name": "旧账号", "phone": "13800138000",
                 "password": "plain123", "phone_code": "code7"},
            ], f, ensure_ascii=False)
        db.init_db(self.db_file, migrate_from=accounts_json,
                   env_file=self.env_file, cleanup=False)
        baks = glob.glob(accounts_json + ".bak-*")
        self.assertEqual(len(baks), 1, baks)
        bak = baks[0]
        if os.name != "nt":
            self.assertEqual(oct(os.stat(bak).st_mode & 0o777), oct(0o600), ".bak 应 0600")
        with open(bak, encoding="utf-8") as f:
            data = json.load(f)
        self.assertTrue(db._is_encrypted_value(data[0]["password"]), ".bak 密码应已加密")
        self.assertTrue(db._is_encrypted_value(data[0]["phone_code"]), ".bak phone_code 应已加密")
        with open(bak, encoding="utf-8") as f:
            bak_text = f.read()
        self.assertNotIn("plain123", bak_text, ".bak 不得含明文")
        # 库内同步加密
        rows = db.load_accounts_raw()
        self.assertTrue(db._is_encrypted_value(rows[0]["password"]))

    def test_migrate_bak_plaintext_only_when_source_has_plain(self):
        """已密文源（0.16+ 密文对象）→ .bak 原样保留（不重写），仍 0600。"""
        accounts_json = os.path.join(self.tmp, "accounts.json")
        key = db.account_crypto.load_key(self.env_file)
        enc = json.dumps(
            db.account_crypto.encrypt_password("secret", key, "13800138000")
        )
        with open(accounts_json, "w", encoding="utf-8") as f:
            json.dump([
                {"name": "新账号", "phone": "13800138000", "password": enc},
            ], f, ensure_ascii=False)
        db.init_db(self.db_file, migrate_from=accounts_json,
                   env_file=self.env_file, cleanup=False)
        baks = glob.glob(accounts_json + ".bak-*")
        self.assertEqual(len(baks), 1, baks)
        if os.name != "nt":
            self.assertEqual(oct(os.stat(baks[0]).st_mode & 0o777), oct(0o600))
        # 保持密文，未被重复加密（is_encrypted 仍成立）
        with open(baks[0], encoding="utf-8") as f:
            data = json.load(f)
        self.assertTrue(db._is_encrypted_value(data[0]["password"]))

    # ---- 缺口 3：legacy env 告警 ----
    def test_legacy_env_warns_without_leaking(self):
        with mock.patch.dict(os.environ, {
            "YIBAN_ACCOUNTS": "13800138000:secret1#13800138001:secret2",
        }, clear=False):
            with self.assertLogs("yiban", level="WARNING") as cm:
                accs = signin._load_accounts_from_legacy_env()
            self.assertEqual(len(accs), 2)
            self.assertTrue(any("旧格式明文账号配置" in m for m in cm.output), cm.output)
            joined = "\n".join(cm.output)
            self.assertNotIn("secret1", joined, "告警不得泄露明文")
            self.assertNotIn("secret2", joined, "告警不得泄露明文")

    def test_legacy_env_no_warning_when_unset(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("YIBAN_ACCOUNTS", None)
            os.environ.pop("YIBAN_PASSWORD", None)
            with self.assertNoLogs("yiban", level="WARNING"):
                accs = signin._load_accounts_from_legacy_env()
            self.assertEqual(accs, [])


if __name__ == "__main__":
    unittest.main()
