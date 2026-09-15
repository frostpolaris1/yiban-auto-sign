# -*- coding: utf-8 -*-
"""运行期账号有效性复核（DAT-1）。

**缺陷（docs/refactor/52 §2.3 DAT-1）**：签到进程用**启动时的全量快照**跑完整轮
（窗口最长 80 分钟），期间 web 端可能删除/停用账号。原实现只在启动时筛一次，于是：

- 运行期被删的账号**仍会被完整登录并签退**（对已注销用户做了真实请求）；
- 登录成功后会把加密的 `session_cache` 写回该 phone，而 session_cache 的清理全按
  "现存账号行的 phone" 驱动 —— 那种行成为**永久孤儿凭据缓存**；
- `sign_events` / 按日状态文件同样继续写入已删账号。

**修法**：`db.account_is_signable()` 运行期复核（attempt_signin / verify_account /
会话缓存落库三处），外加孤儿会话缓存的每日清理兜底。
"""
import contextlib
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

import db  # noqa: E402
import signin  # noqa: E402

TEST_KEY = "a" * 64
PHONE = "13800138000"
PHONE2 = "13800138001"


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-live-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n")
        os.environ.update({
            "YIBAN_ACCOUNTS_KEY": TEST_KEY,
            "YIBAN_ENV_FILE": cls.env_file,
            "YIBAN_DB_FILE": cls.db_file,
            "YIBAN_STATE_DIR": cls.tmp,
            "YIBAN_LOG_FILE": os.path.join(cls.tmp, "sign.log"),
        })

    @classmethod
    def tearDownClass(cls):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k in ("YIBAN_ACCOUNTS_KEY", "YIBAN_ENV_FILE", "YIBAN_DB_FILE",
                  "YIBAN_STATE_DIR", "YIBAN_LOG_FILE", "YIBAN_ACCOUNTS_JSON"):
            os.environ.pop(k, None)

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

    def _add(self, phone=PHONE, status="active", owner="u@test.local"):
        return db.add_account({
            "name": "测试", "phone": phone, "password": "pw", "owner": owner,
            "status": status, "phone_model": "", "phone_code": "",
        })

    def _acc(self, account_id, phone=PHONE):
        return signin.Account(phone=phone, password="pw", account_id=account_id)


class AccountIsSignableTest(_Base):
    def test_missing_row_not_signable(self):
        self.assertFalse(db.account_is_signable(999999))

    def test_soft_deleted_not_signable(self):
        acc = self._add()
        db.set_account_deleted(acc, True, "2026-09-15 06:00:00", "admin")
        self.assertFalse(db.account_is_signable(acc))

    def test_pending_and_rejected_not_signable(self):
        # 注意：index_accounts_owner_live 对非 admin 归属只允许一个未删除账号，
        # 故两个状态各用一个归属。
        self.assertFalse(db.account_is_signable(self._add(PHONE, "pending", "a@test.local")))
        self.assertFalse(db.account_is_signable(self._add(PHONE2, "rejected", "b@test.local")))

    def test_active_and_unknown_id_signable(self):
        self.assertTrue(db.account_is_signable(self._add()))
        # 0/None：JSON / 环境变量账号模式（库内没有对应行）不受此门约束
        self.assertTrue(db.account_is_signable(0))
        self.assertTrue(db.account_is_signable(None))


class AttemptSkipsRemovedAccountTest(_Base):
    """运行期复核：快照里的账号在轮到它之前被删/停用 → 不发起任何请求。"""

    def _run_attempt(self, account):
        client = mock.MagicMock()
        with mock.patch.object(signin, "YibanClient", return_value=client):
            result = signin.attempt_signin(account)
        return result, client

    def test_live_account_is_attempted(self):
        _, client = self._run_attempt(self._acc(self._add()))
        self.assertTrue(client.login_killyiban.called, "有效账号应正常登录")

    def test_deleted_before_turn_is_skipped(self):
        acc_id = self._add()
        account = self._acc(acc_id)
        db.delete_accounts_by_owner("u@test.local")  # 轮到它之前被删除
        result, client = self._run_attempt(account)
        self.assertFalse(client.login_killyiban.called, "已删账号不得发起登录")
        self.assertFalse(client.login.called)
        self.assertTrue(result[2], "应作为跳过处理（不重试）")
        self.assertIn("删除或停用", result[1])

    def test_deactivated_before_turn_is_skipped(self):
        acc_id = self._add()
        account = self._acc(acc_id)
        db.update_account_status(acc_id, "rejected", "管理员打回")
        _, client = self._run_attempt(account)
        self.assertFalse(client.login_killyiban.called)

    def test_account_without_id_is_not_gated(self):
        """JSON/环境变量账号（account_id=0）不受库内状态门限制。"""
        _, client = self._run_attempt(signin.Account(phone=PHONE, password="pw"))
        self.assertTrue(client.login_killyiban.called)

    def test_verify_account_also_gated(self):
        acc_id = self._add()
        account = self._acc(acc_id)
        db.set_account_deleted(acc_id, True, "2026-09-15 06:00:00", "admin")
        with mock.patch.object(signin, "YibanClient") as client:
            ok, msg = signin.verify_account(account)
        self.assertFalse(ok)
        self.assertIn("删除或停用", msg)
        self.assertFalse(client.called, "探针同样不得为已删账号发起登录")


class SessionCacheNotWrittenForDeadAccountTest(_Base):
    """登录期间账号被删 → 不落会话缓存（否则成为永久孤儿凭据行）。"""

    def _client(self, account):
        client = signin.YibanClient(account)
        # 免掉真实网络：直接构造"登录成功"后的状态
        client.logged_in = True
        client.session.cookies.set("sid", "abc")
        client.csrf = "csrf-token"
        return client

    def test_cache_written_for_live_account(self):
        acc_id = self._add()
        client = self._client(self._acc(acc_id))
        client._save_session_cache()
        self.assertIsNotNone(db.get_session_cache(PHONE))

    def test_cache_skipped_when_account_purged(self):
        acc_id = self._add()
        client = self._client(self._acc(acc_id))
        db.purge_account(acc_id)  # 登录完成前账号行已被物理清除
        client._save_session_cache()
        self.assertIsNone(db.get_session_cache(PHONE), "已消失的账号不得写入会话缓存")


class OrphanSessionCacheSweepTest(_Base):
    """每日清理兜底：账号行不存在的会话缓存一律清除，现存账号的不受影响。"""

    def test_sweep_removes_only_orphans(self):
        live = self._add()
        db.set_session_cache(PHONE, '{"a":"1"}', "c")
        db.set_session_cache(PHONE2, '{"a":"1"}', "c")  # 无账号行 → 孤儿
        self.assertEqual(db.purge_orphan_session_cache(db.get_conn()), 1)
        db.get_conn().commit()
        self.assertIsNotNone(db.get_session_cache(PHONE), "现存账号的缓存不得被误清")
        self.assertIsNone(db.get_session_cache(PHONE2))
        self.assertTrue(live)

    def test_daily_cleanup_includes_sweep(self):
        self._add()
        db.set_session_cache(PHONE2, '{"a":"1"}', "c")
        db.run_daily_cleanup()
        self.assertIsNone(db.get_session_cache(PHONE2), "每日清理应顺带扫掉孤儿")


if __name__ == "__main__":
    unittest.main(verbosity=2)
