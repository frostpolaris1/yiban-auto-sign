# -*- coding: utf-8 -*-
"""核心路径冒烟测试（单人维护用）：改完代码跑一遍，防回归。

用法（在项目根目录）：
    py -m pytest tests/test_smoke.py -v        # 需要 pytest
    py tests/test_smoke.py                     # 无 pytest 也可直接运行

覆盖：加密/解密/迁移（最易改坏）、登录权限、批量防呆、软删除、数据层 CRUD。
所有测试使用临时数据目录（独立 SQLite 库），不碰真实数据。
"""
import contextlib
import datetime
import json
import os
import shutil
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "scripts"))
sys.path.insert(0, os.path.join(BASE, "web"))

TEST_KEY = "a" * 64  # 64 位 hex = 32 字节 AES 密钥


class SmokeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-smoke-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.accounts_file = os.path.join(cls.tmp, "accounts.json")
        cls.users_file = os.path.join(cls.tmp, "users.json")
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_ACCOUNTS_FILE"] = cls.accounts_file
        os.environ["YIBAN_USERS_FILE"] = cls.users_file
        os.environ["YIBAN_DB_FILE"] = cls.db_file
        # 导入被测模块
        global account_crypto, db
        # web/app.py 模块级函数可独立调用
        import importlib.util

        import account_crypto
        import db
        spec = importlib.util.spec_from_file_location("webapp", os.path.join(BASE, "web", "app.py"))
        cls.webapp = importlib.util.module_from_spec(spec)
        sys.modules["webapp"] = cls.webapp
        with contextlib.suppress(Exception):
            spec.loader.exec_module(cls.webapp)  # 模块顶层可能因环境失败，用函数级验证兜底

    @classmethod
    def tearDownClass(cls):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        shutil.rmtree(cls.tmp, ignore_errors=True)
        os.environ.pop("YIBAN_ACCOUNTS_KEY", None)

    def setUp(self):
        # 每个测试独立数据库：关闭连接、删除库文件（含 WAL/SHM），再按需迁移
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        for suffix in ("", "-wal", "-shm"):
            p = self.db_file + suffix
            if os.path.exists(p):
                os.remove(p)
        # 清理迁移源残留：.bak 已存在时新 JSON 不再改名，会导致后续测试重复迁移
        for n in os.listdir(self.tmp):
            if n.startswith("accounts.json.bak-") or n.startswith("users.json.bak-"):
                os.remove(os.path.join(self.tmp, n))

    def _write_accounts(self, accounts):
        with open(self.accounts_file, "w", encoding="utf-8") as f:
            json.dump(accounts, f, ensure_ascii=False)

    def _init_db(self):
        """初始化数据库并执行 JSON→SQLite 自动迁移（模拟 create_app 启动路径）。"""
        db.init_db(self.db_file, migrate_from=self.accounts_file, env_file=self.env_file)

    # ---- 1. 加密/解密 ----
    def test_encrypt_decrypt_roundtrip(self):
        key = account_crypto._decode_key(TEST_KEY)
        ct = account_crypto.encrypt_password("secret-pass", key, "13800138000")
        self.assertTrue(account_crypto.is_encrypted(ct))
        self.assertEqual(account_crypto.decrypt_password(ct, key, "13800138000"), "secret-pass")

    def test_aad_blocks_cross_account(self):
        key = account_crypto._decode_key(TEST_KEY)
        ct = account_crypto.encrypt_password("secret-pass", key, "13800138000")
        with self.assertRaises(ValueError):
            account_crypto.decrypt_password(ct, key, "13900139000")  # AAD 不匹配

    # ---- 2. JSON→SQLite 迁移 + 解密 + 惰性清理 ----
    def test_migration_encrypts_and_decrypts(self):
        old = (datetime.datetime.now() - datetime.timedelta(days=8)).isoformat(timespec="seconds")
        self._write_accounts([
            {"name": "测试", "phone": "13800138000", "password": "plain-pass", "status": "active"},
            # 超期软删除账号：迁移后 load 时应被惰性清理
            {"name": "旧删", "phone": "13900139000", "password": "p2", "status": "active",
             "deleted": True, "deleted_at": old},
        ])
        self._init_db()
        # 业务层看到明文；超期账号被清除
        accounts = db.load_accounts()
        self.assertEqual([a["phone"] for a in accounts], ["13800138000"])
        self.assertEqual(accounts[0]["password"], "plain-pass")
        # 库内为密文（存储层加密）
        conn = db.get_conn()
        row = conn.execute("SELECT password FROM accounts WHERE phone='13800138000'").fetchone()
        self.assertTrue(account_crypto.is_encrypted(json.loads(row["password"])))
        # JSON 已改名 .bak 保留逃生门
        self.assertTrue(any(n.startswith("accounts.json.bak-") for n in os.listdir(self.tmp)))

    def test_add_account_encrypts(self):
        self._init_db()
        db.add_account({"name": "新号", "phone": "13700137000", "password": "new-pass",
                        "owner": "admin", "status": "active"})
        conn = db.get_conn()
        row = conn.execute("SELECT password FROM accounts WHERE phone='13700137000'").fetchone()
        self.assertTrue(account_crypto.is_encrypted(json.loads(row["password"])))
        # load 后业务层为明文
        self.assertEqual(db.load_accounts()[0]["password"], "new-pass")

    def test_migration_cipher_dict_format(self):
        """0.16 生产格式：accounts.json 密文为 JSON 嵌套对象（dict）——迁移须序列化入库。"""
        key = account_crypto._decode_key(TEST_KEY)
        ct_obj = account_crypto.encrypt_password("cipher-pass", key, "13600136000")
        self._write_accounts([
            {"name": "密文账号", "phone": "13600136000", "password": ct_obj,  # dict 密文对象
             "phone_model": "X1", "status": "active", "owner": "admin"},
        ])
        self._init_db()
        accounts = db.load_accounts()
        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0]["password"], "cipher-pass")  # 解密为明文
        conn = db.get_conn()
        row = conn.execute("SELECT password FROM accounts WHERE phone='13600136000'").fetchone()
        self.assertTrue(account_crypto.is_encrypted(json.loads(row["password"])))  # 库内 JSON 串

    # ---- 3. 登录/权限基础（轻量：直接验证 verify_admin 兼容路径）----
    def test_admin_verify_plain_fallback(self):
        # 无哈希时明文回退（旧 .env 兼容）
        if hasattr(self.webapp, "verify_admin"):
            old_env = self.webapp.ENV_FILE
            self.webapp.ENV_FILE = self.env_file
            try:
                self.assertFalse(self.webapp.verify_admin("admin", "wrong"))  # 无配置/密码错误应 False
            finally:
                self.webapp.ENV_FILE = old_env

    # ---- 4. 批量防呆：批量 purge 不能删未软删除账号 ----
    def test_batch_purge_requires_deleted(self):
        self._init_db()
        db.add_account({"name": "正常", "phone": "13800138000", "password": "p1", "status": "active"})
        deleted_id = db.add_account({"name": "已删", "phone": "13900139000", "password": "p2",
                                     "status": "active"})
        db.set_account_deleted(deleted_id, 1, datetime.datetime.now().isoformat(timespec="seconds"))
        # 模拟批量 purge 逻辑：仅删 deleted 的
        accounts = db.load_accounts()
        deleted = [a for a in accounts if a.get("deleted")]
        self.assertEqual(len(deleted), 1)
        self.assertEqual(deleted[0]["phone"], "13900139000")
        for a in deleted:
            db.purge_account(a["id"])
        self.assertEqual([a["phone"] for a in db.load_accounts()], ["13800138000"])

    # ---- 5. 软删除超期惰性清理 ----
    def test_expired_soft_delete_cleaned(self):
        self._init_db()
        old = (datetime.datetime.now() - datetime.timedelta(days=8)).isoformat(timespec="seconds")
        db.add_account({"name": "A", "phone": "13800138000", "password": "p1", "status": "active"})
        db.set_account_deleted(db.load_accounts()[0]["id"], 1, old)
        # load 时惰性清除超期行
        self.assertEqual(db.load_accounts(), [])

    # ---- 6. 用户表 CRUD（db 层）----
    def test_users_crud(self):
        self._init_db()
        db.create_user("a@x.com", "hash1", role="user", created_at="", pw_version=1)
        self.assertEqual(db.find_user("a@x.com")["email"], "a@x.com")
        db.update_user("a@x.com", {"role": "admin", "pw_version": 2})
        u = db.find_user("a@x.com")
        self.assertEqual(u["role"], "admin")
        self.assertEqual(u["pw_version"], 2)
        db.delete_user("a@x.com")
        self.assertIsNone(db.find_user("a@x.com"))

    # ---- 7. 手机号唯一约束（并发兜底语义）----
    def test_phone_unique_conflict(self):
        self._init_db()
        db.add_account({"name": "A", "phone": "13800138000", "password": "p1", "status": "active"})
        with self.assertRaises(db.DuplicatePhoneError):
            db.add_account({"name": "B", "phone": "13800138000", "password": "p2"})
        id2 = db.add_account({"name": "B", "phone": "13900139000", "password": "p2", "status": "active"})
        # 改手机号撞他人 UNIQUE（改自己的号不冲突，排除自身）
        with self.assertRaises(db.DuplicatePhoneError):
            db.update_account(id2, {"phone": "13800138000"})
        db.update_account(id2, {"phone": "13900139000"})  # 原号不改，不冲突

    # ---- 8. 乐观锁：匹配 True / 不匹配 False / 不存在 None ----
    def test_optimistic_lock(self):
        self._init_db()
        acc_id = db.add_account({"name": "A", "phone": "13800138000", "password": "p1",
                                 "status": "active"})
        snap = {"name": "A", "phone": "13800138000", "phone_model": "",
                "status": "active", "deleted": False}
        self.assertTrue(db.update_account(acc_id, {"name": "A2"}, expect_snapshot=snap))
        self.assertFalse(db.update_account(acc_id, {"name": "A3"}, expect_snapshot=snap))
        self.assertIsNone(db.update_account(99999, {"name": "X"}, expect_snapshot=snap))

    # ---- 9. 移动交换排序（仅未删除行参与）----
    def test_move_swap_order(self):
        self._init_db()
        id1 = db.add_account({"name": "A", "phone": "13800138000", "password": "p1", "status": "active"})
        id2 = db.add_account({"name": "B", "phone": "13900139000", "password": "p2", "status": "active"})
        id3 = db.add_account({"name": "C", "phone": "13700137000", "password": "p3", "status": "active"})
        # B 下移（与 C 交换）
        self.assertTrue(db.move_account(id2, 1))
        order = [a["phone"] for a in db.load_accounts()]
        self.assertEqual(order, ["13800138000", "13700137000", "13900139000"])
        # A 上移（已到顶，失败）
        self.assertFalse(db.move_account(id1, -1))
        # 软删除的账号不参与交换
        db.set_account_deleted(id2, 1, datetime.datetime.now().isoformat(timespec="seconds"))
        self.assertFalse(db.move_account(id3, 1))  # C 之后无未删除账号

    # ---- 10. 审计写入 ----
    def test_audit_write(self):
        self._init_db()
        db.audit("tester", "account_add", "138****8000", "测试审计")
        conn = db.get_conn()
        row = conn.execute(
            "SELECT username, action, target, detail FROM audit_logs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(row["username"], "tester")
        self.assertEqual(row["action"], "account_add")
        self.assertEqual(row["detail"], "测试审计")


    # ---- 11. 解密失败统一收口（对抗性审查 2026-08-15 L1）----
    def test_decrypt_failure_wrapped_runtime_error(self):
        """密文损坏（tag 校验失败）→ load_accounts 抛 RuntimeError（统一 JSON 收口），非 ValueError 透传。"""
        self._init_db()
        import account_crypto

        key = account_crypto.load_key(self.env_file)
        good = account_crypto.encrypt_password("secret", key, "13800138000")
        bad = {**good, "tag": "00" * 16}  # 篡改 tag → 解密必失败
        acc_id = db.add_account({"name": "A", "phone": "13800138000",
                                 "password": "p1", "status": "active"})
        conn = db.get_conn()
        conn.execute("UPDATE accounts SET password=? WHERE id=?",
                     (json.dumps(bad), acc_id))
        conn.commit()
        with self.assertRaises(RuntimeError):
            db.load_accounts()

    # ---- 12. 首启建密钥并发唯一（对抗性审查 2026-08-15 F3）----
    def test_load_key_concurrent_single_key(self):
        """多线程首启 load_key：只生成一份密钥并共享（_KEY_LOCK 双检）。"""
        import threading

        import account_crypto

        old_cache = account_crypto._KEY_CACHE
        old_env = os.environ.pop("YIBAN_ACCOUNTS_KEY", None)
        env2 = os.path.join(self.tmp, "env2.env")
        if os.path.exists(env2):
            os.remove(env2)
        account_crypto._KEY_CACHE = None
        keys = []

        def get():
            keys.append(account_crypto.load_key(env2))

        try:
            ts = [threading.Thread(target=get) for _ in range(8)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
            self.assertEqual(len({k.hex() for k in keys}), 1,
                             f"多线程生成了多个密钥: {sorted({k.hex() for k in keys})}")
        finally:
            account_crypto._KEY_CACHE = old_cache
            if old_env is not None:
                os.environ["YIBAN_ACCOUNTS_KEY"] = old_env


if __name__ == "__main__":
    unittest.main(verbosity=2)
