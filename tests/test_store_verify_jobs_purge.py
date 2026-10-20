# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""verify_jobs 的保留期清理必须接时钟跳变守卫。

工单 `ba-p02-09`：`purge()` 是五处"按保留期物理删除"里唯一没接 `_clock_jump_guard`
的（claims.purge / cleanup._purge_expired_deleted / cleanup._audit_cleanup /
events._event_cleanup / users 两处都接了）。系统时钟被拨快时 cutoff 前移到未来，
校验记录（含明文手机号与错误文本）会被一次性删空且无告警。今天保留期实际由
`events._event_cleanup` 代跑、`db.purge_verify_jobs` 无生产调用点——属潜伏面：
门面名一旦被复用即绕过守卫。

修法：DELETE 前调用 `db._clock_jump_guard(conn, "purge_verify_jobs_clock")`，
越界路径跳过本轮并按既有口径 rollback 解除写锁。

标签：C · 存储：迁移与库完整性
覆盖：时钟异常跳变时 purge 不删行；正常路径照旧删除并提交。
对应实现：`yiban/adapters/persistence/verify_jobs.py` 的 `purge`。
关键断言：守卫参照点显示当前时间比上次前进超过 72h 时，超期行必须原样保留。
依赖：临时库 + 临时 `.env`；守卫参照点直接写 app_meta，不 sleep、不 mock 时钟。
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
PHONE = "13800138021"
GUARD_KEY = "purge_verify_jobs_clock"


class _Fixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-verify-purge-")
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

    def _old_job(self, days=30):
        job_id, _created = db.create_verify_job(None, PHONE, "u@test.local")
        old = (clock.now() - datetime.timedelta(days=days)).strftime(
            "%Y-%m-%d %H:%M:%S")
        conn = db.get_conn()
        conn.execute("UPDATE verify_jobs SET created_at=? WHERE id=?", (old, job_id))
        conn.commit()
        return job_id

    def test_clock_jump_skips_purge(self):
        job_id = self._old_job()
        seen = (clock.now() - datetime.timedelta(days=5)).strftime("%Y-%m-%d %H:%M:%S")
        conn = db.get_conn()
        conn.execute("INSERT OR REPLACE INTO app_meta (key, value) VALUES (?,?)",
                     (GUARD_KEY, seen))
        conn.commit()

        self.assertEqual(db.purge_verify_jobs(days=7), 0,
                         "时钟前进超过 72h 时不得执行保留期物理删除")
        self.assertIsNotNone(db.get_verify_job(job_id),
                             "校验记录（含明文手机号与错误文本）必须原样保留")

    def test_normal_purge_still_deletes_expired_rows(self):
        job_id = self._old_job()
        self.assertEqual(db.purge_verify_jobs(days=7), 1)
        self.assertIsNone(db.get_verify_job(job_id))


if __name__ == "__main__":
    unittest.main(verbosity=2)
