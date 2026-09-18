# -*- coding: utf-8 -*-
"""A4 第二段：在线校验异步化（v15 verify_jobs，2026-09-15）。

契约（`45` §8.2）：
- 开关关闭时行为**与现在完全一致**（同步返回、不建任务）；
- 开关开启时两处提交端点**内部**建任务，账号照常落库，响应增补 `job_id` / `status:"verifying"`；
- `GET /api/verify-jobs/<id>` 与 `DELETE /api/verify-jobs/<id>`（仅本人或管理员；仅 pending 可取消）；
- 失败语义（D-2）：账号**留在库中**被置 `rejected`，`reject_reason` 承载技术原因；
- 异步任务**计入** `VERIFY_MAX` 与 `VERIFY_FAIL_COOLDOWN`；
- 保留期 7 天，挂入每日清理。
"""
import contextlib
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
NET_FAIL = "账号验证异常：HTTPSConnectionPool 读超时"  # 网络类失败：不计冷却


class _A4Base(unittest.TestCase):
    """临时 .env/DB + 每用例全新 app。`verify_on` 控制 YIBAN_ACCOUNT_VERIFY。"""

    verify_on = True
    async_on = True

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-a4-")
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
    def _login(self, email=EMAIL, client=None, password=None):
        c = client or self.c
        r = c.post("/api/login", json={"username": email,
                                       "password": password or USER_PASS})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return c.get("/api/me").get_json()["csrf_token"]

    def _submit(self, token, phone=PHONE, client=None):
        c = client or self.c
        return c.post("/api/my-accounts", json={
            "name": "测试账号", "phone": phone, "password": "pw1",
        }, headers={"X-CSRF-Token": token})

    def _jobs(self):
        return db.get_conn().execute(
            "SELECT * FROM verify_jobs ORDER BY id").fetchall()

    def _wait_job(self, job_id, timeout=10.0):
        """轮询直到任务落终态，返回最终 dict。"""
        end = time.time() + timeout
        while time.time() < end:
            job = db.get_verify_job(job_id)
            if job and job["status"] in self.webapp.VERIFY_JOB_TERMINAL:
                return job
            time.sleep(0.05)
        return db.get_verify_job(job_id)

    def _account(self, phone=PHONE):
        for a in db.load_accounts():
            if a["phone"] == phone:
                return a
        return None


class VerifyAsyncSwitchOffTest(_A4Base):
    """开关关闭时行为与现在完全一致（D-4 的"零行为变更"在默认配置下成立）。"""

    verify_on = False

    def test_no_job_and_no_job_fields_when_switch_off(self):
        token = self._login()
        with mock.patch.object(self.webapp.signin, "verify_account") as va:
            r = self._submit(token)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertNotIn("job_id", r.get_json(), "开关关闭不得增补 job_id")
        self.assertEqual(self._jobs(), [], "开关关闭不得建任务")
        va.assert_not_called()


class VerifyAsyncSubmitTest(_A4Base):
    """开关开启：提交即返回，后台任务承担外呼。"""

    def test_submit_returns_job_id_and_verifying(self):
        token = self._login()
        # patch 必须覆盖到后台任务跑完为止：提交是异步的，出 with 就还原了，
        # worker 会拿到真实的 verify_account（真发外呼）
        with mock.patch.object(self.webapp.signin, "verify_account",
                               return_value=(True, "ok")):
            r = self._submit(token)
            self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
            body = r.get_json()
            self.assertIn("job_id", body)
            self.assertEqual(body["status"], "verifying")
            job = self._wait_job(body["job_id"])
        self.assertEqual(job["status"], "done", job)
        acct = self._account()
        self.assertIsNotNone(acct, "账号应照常落库")

    def test_account_kept_and_rejected_on_verify_failure(self):
        """D-2：校验失败时账号**留在库中**并置 rejected，理由承载技术原因。"""
        token = self._login()
        with mock.patch.object(self.webapp.signin, "verify_account",
                               side_effect=RuntimeError("boom")):
            r = self._submit(token)
            self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
            job = self._wait_job(r.get_json()["job_id"])
        self.assertEqual(job["status"], "rejected", job)
        self.assertIn("账号验证异常", job["error"])
        acct = self._account()
        self.assertIsNotNone(acct, "账号必须留在库中（不得因校验失败被删）")
        self.assertEqual(acct["status"], "rejected")
        self.assertIn("账号验证异常", acct["reject_reason"])

    def test_job_visible_to_owner_and_denied_to_others(self):
        token = self._login()
        with mock.patch.object(self.webapp.signin, "verify_account",
                               return_value=(True, "ok")):
            job_id = self._submit(token).get_json()["job_id"]
        self._wait_job(job_id)
        r = self.c.get(f"/api/verify-jobs/{job_id}")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json()["job"]["job_id"], job_id)
        self.assertNotIn("13800000001", r.get_data(as_text=True), "不得回显完整手机号")

        other = self.app.test_client()
        t2 = self._login(EMAIL2, client=other)
        r2 = other.get(f"/api/verify-jobs/{job_id}")
        self.assertEqual(r2.status_code, 403, "非本人不得读他人任务")
        r3 = other.delete(f"/api/verify-jobs/{job_id}",
                          headers={"X-CSRF-Token": t2})
        self.assertEqual(r3.status_code, 403, "非本人不得取消他人任务")

    def test_registered_admin_has_no_cross_owner_access(self):
        """"同为管理员"不等于能看/能取消别人的任务（批 3 §4.8）。

        改前 `_verify_job_visible` 对 `role == "admin"` 一路放行；改后管理面只放行
        内置主管理员。取消别人 pending 的任务，会让对方的新账号一直停在「校验中」，
        而这条动作此前不需要任何归属关系、也不二次鉴权。
        """
        job_id, _ = db.create_verify_job(1, PHONE, EMAIL)   # pending：不起 worker
        db.set_user_role(EMAIL2, "admin")
        other = self.app.test_client()
        t2 = self._login(EMAIL2, client=other)
        self.assertEqual(other.get(f"/api/verify-jobs/{job_id}").status_code, 403,
                         "普通管理员不得读他人任务")
        self.assertEqual(other.delete(f"/api/verify-jobs/{job_id}",
                                      headers={"X-CSRF-Token": t2}).status_code, 403,
                         "普通管理员不得取消他人任务")
        self.assertEqual(db.get_verify_job(job_id)["status"], "pending", "被拒不得改状态")
        # 内置主管理员保留排障入口
        mc = self.app.test_client()
        mt = self._login("admin", client=mc, password=ADMIN_PASS)
        self.assertEqual(mc.get(f"/api/verify-jobs/{job_id}").status_code, 200)
        r = mc.delete(f"/api/verify-jobs/{job_id}", headers={"X-CSRF-Token": mt})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(db.get_verify_job(job_id)["status"], "cancelled")

    def test_cancel_only_pending(self):
        token = self._login()
        # pending：直接建行不起 worker，模拟排队中的任务
        job_id, _ = db.create_verify_job(1, PHONE, EMAIL)
        r = self.c.delete(f"/api/verify-jobs/{job_id}", headers={"X-CSRF-Token": token})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json()["job"]["status"], "cancelled")
        # 已终态 → 409
        r2 = self.c.delete(f"/api/verify-jobs/{job_id}", headers={"X-CSRF-Token": token})
        self.assertEqual(r2.status_code, 409)

    def test_cancel_missing_job_404(self):
        token = self._login()
        r = self.c.delete("/api/verify-jobs/999999", headers={"X-CSRF-Token": token})
        self.assertEqual(r.status_code, 404)

    def test_async_jobs_count_toward_quota(self):
        """异步任务计入 VERIFY_MAX：第 VERIFY_MAX+1 次提交应 429。

        走**管理员**路径：普通用户受"每人只能提交一个账号"限制，而异步校验失败会留下
        `rejected` 账号（D-2），第二次提交即被该规则挡下——测不到配额。管理员添加
        不占该名额（`idx_accounts_owner_live` 对 owner='admin' 豁免），且配额按用户名计，
        正好用来验证配额消耗。
        """
        token = self._login("admin", password=ADMIN_PASS)
        with mock.patch.object(self.webapp.db, "count_active_verify_jobs",
                               return_value=0, ), \
                mock.patch.object(self.webapp.signin, "verify_account",
                                  return_value=(False, NET_FAIL)):
            codes = []
            for i in range(self.webapp.VERIFY_MAX + 1):
                r = self.c.post("/api/accounts", json={
                    "name": "测试", "phone": f"1380000020{i}", "password": "pw",
                }, headers={"X-CSRF-Token": token})
                codes.append(r.status_code)
                if r.status_code == 200:
                    self._wait_job(r.get_json()["job_id"])
        self.assertEqual(codes.count(200), self.webapp.VERIFY_MAX, codes)
        self.assertEqual(codes[-1], 429, f"超出配额应 429，实际 {codes}")

    def test_full_job_queue_returns_503(self):
        token = self._login()
        with mock.patch.object(self.webapp.db, "count_active_verify_jobs",
                               return_value=self.webapp.VERIFY_JOBS_MAX_PENDING):
            r = self._submit(token)
        self.assertEqual(r.status_code, 503, r.get_data(as_text=True))
        self.assertEqual(r.get_json()["error"], self.webapp.VERIFY_BUSY_MSG)
        self.assertEqual(self._jobs(), [], "队列满时不得建任务（账号也不得落库）")

    def test_sync_kill_switch_keeps_gate_semantics(self):
        """YIBAN_VERIFY_ASYNC=0：退回同步带闸路径（503 繁忙 / 429 配额）。"""
        os.environ["YIBAN_VERIFY_ASYNC"] = "0"
        token = self._login()
        with mock.patch.object(self.webapp.signin, "verify_account",
                               return_value=(True, "ok")):
            r = self._submit(token)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertNotIn("job_id", r.get_json(), "同步路径不得增补 job_id")
        self.assertEqual(self._jobs(), [], "同步路径不得建任务")


class SeatHandleCapturedTest(unittest.TestCase):
    """席位句柄要**取一次**：acquire 与 release 必须落在同一个信号量对象上。

    2026-09-17 全量 `-n 8` 抓到过一次 `ValueError: Semaphore released too many times`
    （`yiban/attempt/jobs.py` 的 `release()`，宿主是 `BoundedSemaphore(2)`，超发直接抛）。
    真实形态：上一个测试文件留下的校验线程还在飞时，下一个测试文件加载了自己的
    web/app.py 实例并 `configure(seat=…)` 重新注册——旧线程若在释放时**再读一次**
    `_hooks["seat"]`，就会释放到一个它从没 acquire 过的信号量上。

    本用例不靠线程时序复现，而是把"校验途中注册被换掉"这件事**直接做出来**：
    `verify_one` 里换注册，然后断言"取到的那个还回去了、别人的没被动过"。
    """

    def test_release_targets_the_acquired_semaphore(self):
        import threading
        from types import SimpleNamespace

        from yiban.attempt import jobs

        seat_a = threading.BoundedSemaphore(2)   # 线程当初取到的
        seat_b = threading.BoundedSemaphore(2)   # 校验途中被换上的（别人的）
        hooks = {
            "seat": seat_a,
            # 校验进行中换注册（真实形态里是"另一个 webapp 实例注册了自己的信号量"）
            "verify_one": lambda clean: (jobs.configure(seat=seat_b), None)[1],
            "record_failure": lambda *a: "fail-kind",
            "mask_phone": lambda phone: phone,
            "reject_account": lambda *a: None,
        }
        fake_store = SimpleNamespace(claim=lambda jid: True, finish=lambda *a, **k: True)
        with mock.patch.object(jobs, "_hooks", hooks), \
                mock.patch.object(jobs, "store", fake_store):
            jobs.run(1, {"phone": PHONE}, EMAIL, 0, "pending", {}, {})
        self.assertEqual(seat_a._value, 2, "取到的席位必须还回它自己")
        self.assertEqual(seat_b._value, 2, "别人的信号量不得被释放（超发会直接抛）")


class VerifyJobRetentionTest(_A4Base):
    """保留期 7 天，挂入每日清理。"""

    verify_on = False

    def test_purge_removes_only_expired(self):
        import datetime as _dt
        stale = (_dt.datetime.now() - _dt.timedelta(days=8)).strftime("%Y-%m-%d %H:%M:%S")
        fresh = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        job_old, _ = db.create_verify_job(1, PHONE, EMAIL)
        job_new, _ = db.create_verify_job(1, PHONE, EMAIL)
        conn = db.get_conn()
        conn.execute("UPDATE verify_jobs SET created_at=? WHERE id=?", (stale, job_old))
        conn.execute("UPDATE verify_jobs SET created_at=? WHERE id=?", (fresh, job_new))
        conn.commit()
        self.assertEqual(db.purge_verify_jobs(), 1)
        self.assertIsNone(db.get_verify_job(job_old))
        self.assertIsNotNone(db.get_verify_job(job_new))

    def test_event_cleanup_also_trims_jobs(self):
        import datetime as _dt
        stale = (_dt.datetime.now() - _dt.timedelta(days=8)).strftime("%Y-%m-%d %H:%M:%S")
        job_id, _ = db.create_verify_job(1, PHONE, EMAIL)
        conn = db.get_conn()
        conn.execute("UPDATE verify_jobs SET created_at=? WHERE id=?", (stale, job_id))
        conn.commit()
        db._event_cleanup(conn)
        self.assertIsNone(db.get_verify_job(job_id), "每日清理应连带清理过期任务")


class VerifyJobsSchemaTest(_A4Base):
    verify_on = False

    def test_v15_creates_table(self):
        conn = db.get_conn()
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0],
                         db._MIGRATIONS[-1][0])
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='verify_jobs'"
        ).fetchone()
        self.assertIsNotNone(row, "v15 应建 verify_jobs")
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(verify_jobs)")}
        self.assertEqual(
            cols,
            {"id", "account_id", "phone", "owner_email", "status", "prev_status",
             "error", "created_at", "started_at", "finished_at"},
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
