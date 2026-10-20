# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""账号物理清除必须与其余四类清理同口径写留痕。

工单 `ba-p02-10`：`_purge_expired_deleted` 删除超期软删账号后直接
`_cascade_phone_owned` + commit，不调 `_record_purge_event`。审计、sign_events、
verify_jobs、users、user_delete_requests 五类清理都写了留痕，accounts 又在
`_ALLOWED_TABLES` 白名单内——结果"这一轮到底物理删掉了几个账号、删除前后 max_id
是多少"事后无从核对，也让"账号清除不需要留痕"成为同模块内的错误先例。

修法：删除前取 `_table_min_max(conn, "accounts")`，删除后按 rowcount 调
`_record_purge_event(conn, "accounts", "account_cleanup", cutoff, deleted, before, after)`
（与 `_audit_cleanup` 同一形状，删除与留痕同事务）。

标签：G · 安全：脱敏/审计/配置注入
覆盖：`purge_expired_deleted_accounts` 的清理留痕（deleted 计数与 before/after 边界）。
对应实现：`yiban/adapters/persistence/cleanup.py` 的 `_purge_expired_deleted`、
`yiban/adapters/persistence/db.py` 的 `_record_purge_event` / `_table_min_max`。
关键断言：物理删除一个账号后，`audit_purge_events` 必有一条
`table=accounts, kind=account_cleanup, deleted=1`，且 before/after 指向被删行；
没有删除时不得凭空写事件。
依赖：临时库 + 临时 `.env`；无网络、无 skip。
"""
import contextlib
import datetime
import os
import shutil
import tempfile
import unittest

from yiban.core import clock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TEST_KEY = "a" * 64
PHONE = "13800138031"


class _Fixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-cleanup-trace-")
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

    def _expired_soft_deleted_account(self):
        aid = db.add_account({
            "name": "测试", "phone": PHONE, "password": "pw", "owner": "u@test.local",
            "status": "active", "phone_model": "", "phone_code": "",
        })
        old = (clock.now() - datetime.timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
        db.set_account_deleted(aid, True, old, "admin")
        return aid

    def test_account_physical_purge_writes_trace(self):
        aid = self._expired_soft_deleted_account()
        db.set_session_cache(PHONE, '{"sid":"a"}', "c")

        db.purge_expired_deleted_accounts()

        n = db.get_conn().execute(
            "SELECT COUNT(*) FROM accounts WHERE id=?", (aid,)).fetchone()[0]
        self.assertEqual(n, 0, "前置：超期软删账号应已被物理清除")
        events = [e for e in db.audit_purge_events()
                  if e.get("table") == "accounts" and e.get("kind") == "account_cleanup"]
        self.assertEqual(len(events), 1,
                         "账号物理清除必须写清理留痕（与审计/事件/用户清理同口径）")
        ev = events[0]
        self.assertEqual(ev["deleted"], 1)
        self.assertEqual((ev["before_min"], ev["before_max"]), (aid, aid),
                         "before 边界必须是删除前的真实行号")
        self.assertEqual((ev["after_min"], ev["after_max"]), (None, None),
                         "删空后 after 边界为 None")
        self.assertIsNone(db.get_session_cache(PHONE), "凭据缓存仍须连带清除")

    def test_no_trace_when_nothing_is_purged(self):
        self._expired_soft_deleted_account()
        db.set_account_deleted(
            db.get_conn().execute("SELECT id FROM accounts").fetchone()[0], False)
        db.purge_expired_deleted_accounts()
        self.assertEqual(
            [e for e in db.audit_purge_events() if e.get("table") == "accounts"], [],
            "没有删除不得写留痕（留痕是取证数据，不能凭空产生）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
