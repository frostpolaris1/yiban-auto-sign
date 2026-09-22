# -*- coding: utf-8 -*-
"""校验任务的生命周期防线（v16）。

对应三条缺陷的回归测试（均为在线校验异步化引入）：
- **超龄收口**：进程在任务执行期间消失（重启/重部署/OOM）后任务永久停在
  running——既不去终态、又不可取消，还一直占待办名额，累计到上限后所有新增
  账号的在线校验永久 503 且不自愈。启动期与每次入队前都要收口。
- **人工决定优先**：异步结果回写账号时按 `prev_status` CAS，管理员在任务执行
  期间点的"审核通过"不得被迟到的校验结果静默回滚。
- **级联清理**：账号物理删除的 8 条路径都必须连带清除 verify_jobs，且
  "以 phone 为键的表"清单由 schema 枚举测试兜底（新增表漏加会直接红）。
"""
import contextlib
import datetime
import importlib.util
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TEST_KEY = "a" * 64
ADMIN_PASS = "TestPass1234!"
USER_PASS = "secret1"
EMAIL = "u1@test.local"
EMAIL2 = "u2@test.local"
PHONE = "13800000001"
PHONE2 = "13800000002"

# 以 phone 为键的业务表（accounts 自身除外）。**新增此类表必须同时加进
# _cascade_phone_owned**——下面的枚举测试会失败提醒，这是刻意的。
PHONE_KEYED_TABLES = {"time_prefs", "session_cache", "sign_events", "verify_jobs",
                     "sign_claims", "sign_tasks"}


def _ago(seconds):
    return (datetime.datetime.now() - datetime.timedelta(seconds=seconds)).strftime(
        "%Y-%m-%d %H:%M:%S")


class _LifecycleBase(unittest.TestCase):
    """临时 .env/DB + 每用例全新 app（与 test_a4_verify_jobs 同一套骨架）。"""

    verify_on = True
    async_on = True

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-vj-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        os.environ.update({
            "YIBAN_ACCOUNTS_KEY": TEST_KEY,
            "YIBAN_ENV_FILE": cls.env_file,
            "YIBAN_DB_FILE": cls.db_file,
            "YIBAN_STATE_DIR": cls.tmp,
            "YIBAN_LOG_FILE": os.path.join(cls.tmp, "sign.log"),
        })
        global db
        import db
        spec = importlib.util.spec_from_file_location(
            "webapp", os.path.join(BASE, "web", "app.py"))
        cls.webapp = importlib.util.module_from_spec(spec)
        sys.modules["webapp"] = cls.webapp
        with contextlib.suppress(Exception):
            spec.loader.exec_module(cls.webapp)

    @classmethod
    def tearDownClass(cls):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k in ("YIBAN_ACCOUNTS_KEY", "YIBAN_ENV_FILE", "YIBAN_DB_FILE",
                  "YIBAN_STATE_DIR", "YIBAN_LOG_FILE", "YIBAN_VERIFY_ASYNC"):
            os.environ.pop(k, None)

    def setUp(self):
        os.environ["YIBAN_VERIFY_ASYNC"] = "1" if self.async_on else "0"
        with open(self.env_file, "w", encoding="utf-8") as f:
            f.write(
                f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
                f"YIBAN_ADMIN_USER=admin\nYIBAN_ADMIN_PASSWORD={ADMIN_PASS}\n"
                + ("YIBAN_ACCOUNT_VERIFY=1\n" if self.verify_on else "")
            )
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        for suffix in ("", "-wal", "-shm"):
            p = self.db_file + suffix
            if os.path.exists(p):
                os.remove(p)
        db.init_db(self.db_file, env_file=self.env_file, cleanup=False)
        db.create_user(EMAIL, self.webapp.generate_password_hash(USER_PASS))
        db.create_user(EMAIL2, self.webapp.generate_password_hash(USER_PASS))
        self.app = self.webapp.create_app()
        self.c = self.app.test_client()

    def tearDown(self):
        mock.patch.stopall()

    # ---- 工具 ----
    def _login(self, email=EMAIL, password=None):
        r = self.c.post("/api/login", json={"username": email,
                                           "password": password or USER_PASS})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return self.c.get("/api/me").get_json()["csrf_token"]

    def _account(self, phone=PHONE, owner=EMAIL, status="pending"):
        return db.add_account({
            "name": "测试", "phone": phone, "password": "pw", "owner": owner,
            "status": status, "phone_model": "", "phone_code": "",
        })

    def _job(self, account_id, phone=PHONE, owner=EMAIL, prev_status="pending",
             age=None, running=False):
        """建一条任务并按需回拨时间戳（模拟"超龄"），返回 job_id。"""
        job_id, _ = db.create_verify_job(account_id, phone, owner,
                                        prev_status=prev_status)
        conn = db.get_conn()
        if age is not None:
            conn.execute("UPDATE verify_jobs SET created_at=? WHERE id=?",
                         (_ago(age), job_id))
        if running:
            conn.execute(
                "UPDATE verify_jobs SET status='running', started_at=? WHERE id=?",
                (_ago(age if age is not None else 0), job_id))
        conn.commit()
        return job_id

    def _raw(self, table, phone=PHONE):
        return db.get_conn().execute(
            f"SELECT COUNT(*) FROM {table} WHERE phone=?", (phone,)).fetchone()[0]

    def _acct(self, account_id):
        """按 id 取账号行（已解密明文），不存在则失败。"""
        return next((a for a in db.load_accounts() if a["id"] == account_id), None)

    def _acct_by_phone(self, phone=PHONE):
        return next((a for a in db.load_accounts() if a["phone"] == phone), None)

    def _wait_terminal(self, job_id, timeout=10.0):
        """轮询到任务落终态。**必须在 patch 存活期内调用**——否则后台线程
        拿到的是真实实现，会真的外呼易班（测试里既慢又留下活跃线程）。"""
        end = time.time() + timeout
        while time.time() < end:
            job = db.get_verify_job(job_id)
            if job and job["status"] in self.webapp.VERIFY_JOB_TERMINAL:
                return job
            time.sleep(0.05)
        self.fail(f"任务未在 {timeout}s 内落终态: {db.get_verify_job(job_id)}")

    def _seed_phone_rows(self, phone=PHONE):
        """在每张以 phone 为键的表里插一行（级联清理的验证素材）。"""
        conn = db.get_conn()
        conn.execute("INSERT OR REPLACE INTO time_prefs (phone, slot_min, updated_at) "
                     "VALUES (?,?,?)", (phone, 390, _ago(0)))
        conn.execute("INSERT OR REPLACE INTO session_cache "
                     "(phone, cookies_ct, csrf, created_at, updated_at) "
                     "VALUES (?,?,?,?,?)", (phone, "ct", "csrf", _ago(0), _ago(0)))
        conn.execute("INSERT INTO sign_events (ts, phone, status, message, stage, attempt) "
                     "VALUES (?,?,?,?,?,?)", (_ago(0), phone, "success", "", "main", 1))
        conn.execute("INSERT INTO verify_jobs (account_id, phone, owner_email, status, "
                     "created_at) VALUES (?,?,?,?,?)", (None, phone, EMAIL, "pending", _ago(0)))
        conn.execute("INSERT OR REPLACE INTO sign_claims "
                     "(phone, day, owner, claimed_at, heartbeat_at, state) "
                     "VALUES (?,?,?,?,?,?)",
                     (phone, _ago(0)[:10], "seed-proc", _ago(0), _ago(0), "claimed"))
        conn.execute("INSERT OR REPLACE INTO sign_tasks "
                     "(phone, day, vshard, owner, run_at, state, created_at) "
                     "VALUES (?,?,?,?,?,?,?)",
                     (phone, _ago(0)[:10], 0, "seed-proc", _ago(0), "claimed", _ago(0)))
        conn.commit()


class StaleJobReclaimTest(_LifecycleBase):
    """超龄 pending/running 必须被收口，否则功能永久不可用。"""

    verify_on = False

    def test_running_job_reclaimed_and_seat_freed(self):
        acc = self._account()
        job_id = self._job(acc, running=True, age=3600)
        self.assertEqual(db.count_active_verify_jobs(), 1)
        reclaimed = db.reclaim_stale_verify_jobs()
        self.assertEqual([r["id"] for r in reclaimed], [job_id])
        job = db.get_verify_job(job_id)
        self.assertEqual(job["status"], "rejected")
        self.assertIn("超时", job["error"])
        self.assertTrue(job["finished_at"], "收口须落终态时间")
        self.assertEqual(db.count_active_verify_jobs(), 0, "名额必须释放")

    def test_stale_pending_job_reclaimed(self):
        """pending 超龄 = 线程从未启动（进程在建任务与开线程之间消失）。"""
        acc = self._account()
        job_id = self._job(acc, age=3600)
        self.assertEqual(len(db.reclaim_stale_verify_jobs()), 1)
        self.assertEqual(db.get_verify_job(job_id)["status"], "rejected")

    def test_fresh_jobs_untouched(self):
        """合法寿命内的任务（含正在等席位的 running）不得被误收口。"""
        acc = self._account()
        j_pending = self._job(acc, age=1)
        j_running = self._job(acc, running=True, age=30)
        self.assertEqual(db.reclaim_stale_verify_jobs(), [])
        self.assertEqual(db.get_verify_job(j_pending)["status"], "pending")
        self.assertEqual(db.get_verify_job(j_running)["status"], "running")

    def test_reclaim_only_rejects_account_still_in_prev_status(self):
        """账号侧联动同样走 CAS：已被人工审批的不动。"""
        acc_a = self._account(PHONE, EMAIL, status="pending")
        acc_b = self._account(PHONE2, EMAIL2, status="active")
        self._job(acc_a, PHONE, prev_status="pending", running=True, age=3600)
        self._job(acc_b, PHONE2, prev_status="pending", running=True, age=3600)
        self.webapp._reclaim_stale_verify_jobs()
        by_phone = {a["phone"]: a for a in db.load_accounts()}
        self.assertEqual(by_phone[PHONE]["status"], "rejected")
        self.assertEqual(by_phone[PHONE2]["status"], "active",
                         "管理员已审批的账号不得被超龄收口改回 rejected")

    def test_reclaim_tolerates_missing_table(self):
        """v15 可选迁移被延后时表不存在：收口只告警，不得让入队主流程崩。"""
        db.get_conn().execute("DROP TABLE verify_jobs")
        db.get_conn().commit()
        self.assertEqual(db.reclaim_stale_verify_jobs(), [])
        self.assertEqual(self.webapp._reclaim_stale_verify_jobs(), 0)

    def test_reclaim_cutoff_uses_business_clock_not_host_tz(self):
        """H2：UTC 主机（宿主时间比北京慢 8 小时）上，超龄判定必须按业务钟。

        任务按业务钟（clock.ts）写入 created_at；宿主 `datetime.now()` 晚 8 小时，
        若用宿主时间算 cutoff，会把"已超龄"误判成"仍新鲜"——任务多滞留 8 小时。
        """
        acc = self._account()
        biz_now = datetime.datetime(2026, 9, 17, 6, 0, 0)       # 北京 06:00
        host_now = datetime.datetime(2026, 9, 16, 22, 0, 0)     # 宿主 UTC 22:00
        created = datetime.datetime(2026, 9, 16, 23, 0, 0).strftime(
            "%Y-%m-%d %H:%M:%S")                                 # 北京 23:00 建（已超龄 7h）
        job_id, _ = db.create_verify_job(acc, PHONE, EMAIL, prev_status="pending")
        conn = db.get_conn()
        conn.execute(
            "UPDATE verify_jobs SET created_at=?, status='running', started_at=? WHERE id=?",
            (created, created, job_id))
        conn.commit()
        with mock.patch("yiban.store.verify_jobs.datetime.datetime") as dt, \
             mock.patch("yiban.clock.now", return_value=biz_now):
            dt.now.return_value = host_now
            dt.timedelta = datetime.timedelta  # patch 整个类，保留真实 timedelta
            reclaimed = db.reclaim_stale_verify_jobs()
        self.assertEqual([r["id"] for r in reclaimed], [job_id],
                         "UTC 主机上超龄任务也必须按业务钟收口")

    def test_purge_cutoff_uses_business_clock_not_host_tz(self):
        """H2：purge 的保留期判定同样只认业务钟。

        任务建于业务 09-10 00:00、保留 7 天：按业务钟（cutoff 09-10 06:00）已过期；
        宿主时间（cutoff 09-09 22:00）会漏删。
        """
        acc = self._account()
        biz_now = datetime.datetime(2026, 9, 17, 6, 0, 0)
        host_now = datetime.datetime(2026, 9, 16, 22, 0, 0)
        created = datetime.datetime(2026, 9, 10, 0, 0).strftime("%Y-%m-%d %H:%M:%S")
        job_id, _ = db.create_verify_job(acc, PHONE, EMAIL, prev_status="pending")
        db.get_conn().execute("UPDATE verify_jobs SET created_at=? WHERE id=?",
                              (created, job_id))
        db.get_conn().commit()
        with mock.patch("yiban.store.verify_jobs.datetime.datetime") as dt, \
             mock.patch("yiban.clock.now", return_value=biz_now):
            dt.now.return_value = host_now
            dt.timedelta = datetime.timedelta  # patch 整个类，保留真实 timedelta
            purged = db.purge_verify_jobs(days=7)
        self.assertEqual(purged, 1, "UTC 主机上保留期判定同样按业务钟")


class QueueRecoveryTest(_LifecycleBase):
    """致命后果：卡死任务占满名额后，新提交必须能自愈。"""

    def _submit(self, token, phone=PHONE):
        return self.c.post("/api/my-accounts", json={
            "name": "测试账号", "phone": phone, "password": "pw1",
        }, headers={"X-CSRF-Token": token})

    def test_full_queue_of_stale_jobs_recovers(self):
        token = self._login()
        acc = self._account(PHONE2, EMAIL2, status="active")
        for _ in range(self.webapp.VERIFY_JOBS_MAX_PENDING):
            self._job(acc, PHONE2, EMAIL2, running=True, age=7200)
        self.assertGreaterEqual(db.count_active_verify_jobs(),
                                self.webapp.VERIFY_JOBS_MAX_PENDING)
        with mock.patch.object(self.webapp, "_verify_account_clean", return_value=None):
            r = self._submit(token)
            self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
            self.assertIn("job_id", r.get_json(),
                          "超龄任务收口后必须能继续入队，不得永久 503")
            self._wait_terminal(r.get_json()["job_id"])  # patch 存活期内等完，防真外呼

    def test_fresh_queue_still_returns_503(self):
        """收口不得把"确实繁忙"也放行——满队列仍是 503。"""
        token = self._login()
        acc = self._account(PHONE2, EMAIL2, status="active")
        for _ in range(self.webapp.VERIFY_JOBS_MAX_PENDING):
            self._job(acc, PHONE2, EMAIL2, age=0)
        r = self._submit(token)
        self.assertEqual(r.status_code, 503, r.get_data(as_text=True))

    def test_cancel_stale_running_job_is_terminal(self):
        """卡在 running 的超龄任务不再"无法取消"：先收口为终态并可读回结果。"""
        token = self._login()
        acc = self._account()
        job_id = self._job(acc, running=True, age=7200)
        r = self.c.delete(f"/api/verify-jobs/{job_id}", headers={"X-CSRF-Token": token})
        self.assertEqual(r.status_code, 409, r.get_data(as_text=True))
        job = db.get_verify_job(job_id)
        self.assertEqual(job["status"], "rejected")
        self.assertEqual(db.count_active_verify_jobs(), 0)


class AdminDecisionWinsTest(_LifecycleBase):
    """迟到的异步结果不得覆盖人工决定。"""

    verify_on = False

    def test_cas_writes_when_status_unchanged(self):
        acc = self._account(status="pending")
        self.assertTrue(db.update_account_status_if(
            acc, "rejected", "pending", "校验未通过"))
        row = self._acct(acc)
        self.assertEqual(row["status"], "rejected")
        self.assertEqual(row["reject_reason"], "校验未通过")

    def test_cas_skips_when_status_changed(self):
        acc = self._account(status="active")
        self.assertFalse(db.update_account_status_if(
            acc, "rejected", "pending", "校验未通过"))
        self.assertEqual(self._acct(acc)["status"],
                         "active")

    def test_reject_account_respects_approval(self):
        acc = self._account(status="active")  # 管理员已审批
        self.webapp._reject_account(PHONE, "迟到的校验失败",
                                    account_id=acc, expect_status="pending")
        self.assertEqual(self._acct(acc)["status"],
                         "active")

    def test_reject_account_still_rejects_bare_admin_account(self):
        """管理员的裸账号建库即 active，校验失败必须能打回（不能一概跳过 active）。"""
        acc = self._account(status="active")
        self.webapp._reject_account(PHONE, "校验未通过",
                                    account_id=acc, expect_status="active")
        row = self._acct(acc)
        self.assertEqual(row["status"], "rejected")
        self.assertEqual(row["reject_reason"], "校验未通过")

    def test_worker_late_failure_does_not_override_approval(self):
        """端到端：任务在跑期间管理员审批 → 校验失败落终态但账号保持 active。"""
        acc = self._account(status="pending")
        job_id = self._job(acc, prev_status="pending")
        self.assertTrue(db.update_account_status_if(
            acc, "active", "pending", ""), "模拟管理员审批")
        with mock.patch.object(self.webapp, "_verify_account_clean",
                               return_value="账号验证异常：读超时"):
            self.webapp.verify_jobs.run(job_id, {"phone": PHONE}, EMAIL, acc,
                                        "pending", {}, {})
        self.assertEqual(db.get_verify_job(job_id)["status"], "rejected",
                         "任务本身照常落终态")
        row = self._acct(acc)
        self.assertEqual(row["status"], "active", "管理员的审批不得被回滚")

    def test_worker_failure_rejects_untouched_account(self):
        """对照组：无人干预时校验失败照旧把账号打回 rejected。"""
        acc = self._account(status="pending")
        job_id = self._job(acc, prev_status="pending")
        with mock.patch.object(self.webapp, "_verify_account_clean",
                               return_value="账号验证异常：读超时"):
            self.webapp.verify_jobs.run(job_id, {"phone": PHONE}, EMAIL, acc,
                                        "pending", {}, {})
        row = self._acct(acc)
        self.assertEqual(row["status"], "rejected")
        self.assertIn("读超时", row["reject_reason"])


class CascadeCleanupTest(_LifecycleBase):
    """账号物理删除的每条路径都要连带清除 verify_jobs。"""

    verify_on = False

    def test_cascade_clears_every_phone_keyed_table(self):
        self._seed_phone_rows()
        for table in PHONE_KEYED_TABLES:
            self.assertEqual(self._raw(table), 1, table)
        conn = db.get_conn()
        db._cascade_phone_owned(conn, [PHONE])
        conn.commit()
        for table in PHONE_KEYED_TABLES:
            self.assertEqual(self._raw(table), 0, f"{table} 未被连带清理")

    def test_all_phone_keyed_tables_are_declared(self):
        """schema 枚举兜底：新增以 phone 为键的表会在此失败，提示补 _cascade_phone_owned。"""
        conn = db.get_conn()
        tables = [r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        keyed = set()
        for t in tables:
            if t.startswith("sqlite_"):
                continue
            cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({t})")}
            if "phone" in cols and t != "accounts":
                keyed.add(t)
        self.assertEqual(
            keyed, PHONE_KEYED_TABLES,
            "phone 键表清单变化：请把新表加入 _cascade_phone_owned 并更新本清单",
        )

    def _assert_jobs_gone(self, phone=PHONE):
        self.assertEqual(self._raw("verify_jobs", phone), 0, "校验任务未连带清除")

    def test_purge_account_cascades(self):
        acc = self._account()
        self._seed_phone_rows()
        db.purge_account(acc)
        self._assert_jobs_gone()

    def test_delete_accounts_by_owner_cascades(self):
        self._account()
        self._seed_phone_rows()
        db.delete_accounts_by_owner(EMAIL)
        self._assert_jobs_gone()

    def test_delete_user_with_accounts_cascades(self):
        self._account()
        self._seed_phone_rows()
        db.delete_user_with_accounts(EMAIL)
        self._assert_jobs_gone()

    def test_replace_accounts_cascades_removed(self):
        self._account()
        self._seed_phone_rows()
        db.replace_accounts([])
        self._assert_jobs_gone()

    def test_batch_account_purge_cascades(self):
        acc = self._account()
        self._seed_phone_rows()
        db.batch_account_ops([("purge", acc)])
        self._assert_jobs_gone()

    def test_batch_user_delete_cascades(self):
        self._account()
        self._seed_phone_rows()
        db.batch_user_ops([("delete_user_with_accounts", EMAIL)])
        self._assert_jobs_gone()

    def test_purge_deleted_users_hard_cascades(self):
        self._account()
        self._seed_phone_rows()
        db.soft_delete_user_with_accounts(EMAIL)  # 注销：用户与账号一并软删
        db.purge_deleted_users_hard([EMAIL])
        self._assert_jobs_gone()

    def test_expired_soft_delete_purge_cascades(self):
        """7 天保留期到期后的物理清除（每日线程路径）。"""
        import db as _db
        acc = self._account()
        db.set_account_deleted(acc, True, _ago(_db.SOFT_DELETE_RETENTION_SECONDS + 3600), "u")
        self._seed_phone_rows()
        db.purge_expired_deleted_accounts()
        self.assertIsNone(
            db.get_conn().execute("SELECT 1 FROM accounts WHERE id=?", (acc,)).fetchone(),
            "前置条件：账号应已物理清除", )
        self._assert_jobs_gone()


class JobTimestampTest(_LifecycleBase):
    """收口判定读的是 started_at（running）/ created_at（pending）。"""

    verify_on = False

    def test_running_uses_started_at_not_created_at(self):
        """建任务很久但刚开工的任务不超龄——判定不得用 created_at。"""
        acc = self._account()
        job_id = self._job(acc, age=7200)
        db.get_conn().execute(
            "UPDATE verify_jobs SET status='running', started_at=? WHERE id=?",
            (_ago(5), job_id))
        db.get_conn().commit()
        self.assertEqual(db.reclaim_stale_verify_jobs(), [])
        self.assertEqual(db.get_verify_job(job_id)["status"], "running")

    def test_prev_status_defaults_to_pending_for_legacy_rows(self):
        """旧行（v16 之前建立，无 prev_status）取默认 pending，方向偏保守。"""
        acc = self._account(status="active")
        job_id = self._job(acc, running=True, age=7200)
        db.get_conn().execute("UPDATE verify_jobs SET prev_status='' WHERE id=?", (job_id,))
        db.get_conn().commit()
        self.webapp._reclaim_stale_verify_jobs()
        self.assertEqual(self._acct(acc)["status"],
                         "active")


class VerifyJobIntegrationTest(_LifecycleBase):
    """正常路径不回归：开关开启时提交仍建任务、成功不落 rejected。"""

    def test_submit_creates_job_with_prev_status(self):
        token = self._login()
        with mock.patch.object(self.webapp, "_verify_account_clean", return_value=None):
            r = self.c.post("/api/my-accounts", json={
                "name": "测试账号", "phone": PHONE, "password": "pw1",
            }, headers={"X-CSRF-Token": token})
            self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
            job_id = r.get_json()["job_id"]
            self._wait_terminal(job_id)
        job = db.get_verify_job(job_id)
        self.assertEqual(job["prev_status"], "pending")
        self.assertEqual(job["status"], "done")
        row = self._acct_by_phone()
        self.assertEqual(row["status"], "pending", "校验通过不改账号状态，仍待人工审核")

    def test_admin_submit_creates_active_account_rejected_on_failure(self):
        """管理员的裸账号（建库即 active）校验失败必须打回——prev_status=active。"""
        token = self._login("admin", password=ADMIN_PASS)
        with mock.patch.object(self.webapp, "_verify_account_clean",
                               return_value="账号验证异常：读超时"):
            r = self.c.post("/api/accounts", json={
                "name": "裸账号", "phone": PHONE, "password": "pw1",
            }, headers={"X-CSRF-Token": token})
            self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
            job_id = r.get_json()["job_id"]
            self._wait_terminal(job_id)
        self.assertEqual(db.get_verify_job(job_id)["prev_status"], "active")
        self.assertEqual(self._acct_by_phone()["status"], "rejected",
                         "裸账号建库即 active，校验失败必须能打回")


if __name__ == "__main__":
    unittest.main(verbosity=2)
