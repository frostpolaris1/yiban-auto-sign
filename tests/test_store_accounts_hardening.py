# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""账号域的两条安全判据：软删即停用凭据缓存、有效性复核失败必须 fail-closed。

对应 2026-10-20 复核：

- `ba-p02-04`：账号软删（用户自删与管理员删）只置 deleted/deleted_at/deleted_by，
  `session_cache` 里的易班登录态（cookie + csrf 密文）留到物理清除（最长 7 天）。
  同威胁模型下 `users.soft_delete_user_with_accounts` 明确"注销即刻停用凭据缓存"，
  账号级软删漏了同一半。修法：软删分支在同一事务内按 phone 清 session_cache。
- `ba-p02-06`：运行期复核 `account_still_signable` 在查询异常时按"仍有效"返回 True，
  且只记 DEBUG。库抖动期间被删除/停用的账号照旧被真实登录，现场无人知情。
  修法：与 `claims.notify_pool_down` 同口径 fail-closed（拒绝发起真实登录），
  ERROR 留痕并并入当日管理员汇总，进程内只报一次。

标签：I · 容量、熔断与账号有效性
覆盖：`set_account_deleted` 的会话缓存停用与事务原子性；`account_still_signable`
查询异常时的 fail-closed、日志级别与告警入账。
对应实现：`yiban/adapters/persistence/accounts.py`（`set_account_deleted` /
`account_still_signable`）、`yiban/adapters/persistence/db.py`
（`_clear_session_cache_by_phones`）。
关键断言：软删 = 凭据即刻不可用；审计写失败与软删同回滚（不留"删了却无痕"也不留
"无痕却已删"）；复核失败时**不得**返回"仍有效"。
依赖：临时库 + 临时 `.env`；无网络、无 skip。
"""
import contextlib
import os
import shutil
import tempfile
import types
import unittest
from unittest import mock

from yiban.adapters.persistence import accounts as accounts_mod

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TEST_KEY = "a" * 64
PHONE = "13800138123"


class _Fixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-accounts-hardening-")
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
        # 每条用例重置"已报过"的进程内标记（生产语义是每进程一次）
        accounts_mod._validity_check_failure_reported = False

    def _add_account(self, phone=PHONE):
        return db.add_account({
            "name": "测试", "phone": phone, "password": "pw", "owner": "u@test.local",
            "status": "active", "phone_model": "", "phone_code": "",
        })

    def _deleted(self, account_id):
        row = db.get_conn().execute(
            "SELECT deleted FROM accounts WHERE id=?", (account_id,)).fetchone()
        return None if row is None else row[0]


class SoftDeleteClearsSessionCacheTest(_Fixture):
    """`ba-p02-04`：软删必须停用登录态凭据缓存。"""

    def test_soft_delete_clears_session_cache(self):
        aid = self._add_account()
        db.set_session_cache(PHONE, '{"sid":"abc"}', "csrf-token")
        self.assertIsNotNone(db.get_session_cache(PHONE), "前置：缓存已写入")

        db.set_account_deleted(aid, True, "2099-01-01 00:00:00", "admin")

        self.assertIsNone(db.get_session_cache(PHONE),
                          "软删后凭据缓存必须即刻不可用（库文件/备份泄露仍可解密）"
                          )
        self.assertEqual(self._deleted(aid), 1)

    def test_batch_soft_delete_also_clears_session_cache(self):
        """同形状调用点：管理员批量删除走 `batch_account_ops`，同样必须停用缓存。"""
        aid = self._add_account()
        db.set_session_cache(PHONE, '{"sid":"abc"}', "csrf-token")
        db.batch_account_ops([("set_deleted", aid, 1, "2099-01-01 00:00:00")])
        self.assertIsNone(db.get_session_cache(PHONE),
                          "批量软删不得成为绕过凭据停用的第二入口")

    def test_audit_failure_rolls_back_delete_and_cache_clear(self):
        """审计与软删同事务：审计写失败 ⇒ 账号未删、缓存仍在（没有半程状态）。"""
        aid = self._add_account()
        db.set_session_cache(PHONE, '{"sid":"abc"}', "csrf-token")
        spec = {"username": "admin", "action": "delete_account", "target": "x",
                "detail": "d"}
        with mock.patch.object(db, "record_in_txn",
                               side_effect=RuntimeError("audit down")), \
                self.assertRaises(RuntimeError):
            db.set_account_deleted(aid, True, "2099-01-01 00:00:00", "admin",
                                   audit_spec=spec)
        self.assertEqual(self._deleted(aid), 0, "审计失败必须整体回滚")
        self.assertIsNotNone(db.get_session_cache(PHONE), "缓存清理必须随删号一起回滚")


class ValidityCheckFailureIsFailClosedTest(_Fixture):
    """`ba-p02-06`：查不出账号状态时不得放行真实登录。"""

    def _account(self, account_id=1):
        return types.SimpleNamespace(phone=PHONE, account_id=account_id)

    def test_query_failure_blocks_signing(self):
        with mock.patch.object(db, "account_is_signable",
                               side_effect=RuntimeError("db down")), \
                self.assertLogs("yiban.store.accounts", level="ERROR") as logs:
            allowed = accounts_mod.account_still_signable(self._account())
        self.assertFalse(allowed,
                         "有效性查不出来时不得按『仍有效』放行——那是静默多签")
        self.assertTrue(any("有效性复核" in line for line in logs.output), logs.output)

    def test_query_failure_is_reported_once_per_process(self):
        from yiban.adapters.notifier import report as alerts
        with mock.patch.object(db, "account_is_signable",
                               side_effect=RuntimeError("db down")), \
             mock.patch.object(alerts, "_collect_admin_mail") as collect, \
             self.assertLogs("yiban.store.accounts", level="ERROR"):
            self.assertFalse(accounts_mod.account_still_signable(self._account()))
            self.assertFalse(accounts_mod.account_still_signable(self._account()))
        self.assertEqual(collect.call_count, 1,
                         "同一场库故障只进一次当日汇总（防逐账号刷屏）")

    def test_account_without_db_row_is_still_allowed(self):
        """JSON / 环境变量账号模式（account_id=0）没有库内行可查，语义不变。"""
        self.assertTrue(accounts_mod.account_still_signable(self._account(0)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
