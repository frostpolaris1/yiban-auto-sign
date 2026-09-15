# -*- coding: utf-8 -*-
"""账号验证防锁定三联修复回归（2026-09-04 生产复盘）。

生产事件：用户在「我的账号」页添加账号时密码输错，连续 6 次提交 = 6 次真实
易班登录，第 6 次后易班返回「错误尝试过多」把账号锁定；同期用户集中编辑账号，
每次编辑触发 clear_fuse_pause 读取不存在的 cred-state.json，被笼统 except
捕获后刷出约 20 条「清除账密熔断暂停状态失败」误导性 WARNING；且验证失败的
提交不留任何审计痕迹，事后无法溯源。

覆盖：
1. signin._retry_budget：确定性认证失败（密码错误/易班锁定）终态 1 次不重试，
   其余分级（风控/会话陈旧/无点位/网络）不变；探针路径本就单次调用无重试
2. web.clear_fuse_pause：cred-state.json 不存在（从未有账号熔断）静默返回，
   真实 I/O 错误仍 WARNING 留痕
3. web 验证失败冷却：同一手机号 15 分钟窗口内第 2 次认证失败 → 约 10 分钟冷却
   （429 拒绝且不再发起真实易班登录），网络类失败不计数，按手机号隔离；
   用户提交与管理员添加两条路径同口径
4. 验证失败写审计：my_account_add_verify_fail / account_add_verify_fail，
   detail 只记失败原因类别（不含密码等敏感信息）

用法（项目根目录）：py -m pytest tests/test_verify_fail_cooldown.py -v
"""
import contextlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TEST_KEY = "a" * 64
ADMIN_PASS = "TestPass1234!"
USER_PASS = "secret1"
EMAIL = "user1@test.local"
PHONE1 = "13800000001"
PHONE2 = "13800000002"
PHONE3 = "13800000003"
AUTH_FAIL_MSG = "账号验证未通过：登录失败（账号或密码错误）: 138****0001"


class SigninRetryBudgetTest(unittest.TestCase):
    """signin 层：确定性认证失败终态不重试（其余分级保持原状）。"""

    def setUp(self):
        import signin
        self.s = signin

    def test_wrong_password_is_terminal(self):
        # 真实文案：login() 在 usersure reUrl 含 error 时 raise 的完整消息
        budget, clear_cache = self.s._retry_budget(
            "登录失败（账号或密码错误）: 138****8000")
        self.assertEqual(budget, self.s.AUTH_FAIL_MAX_ATTEMPTS)
        self.assertEqual(budget, 1, "密码错误重试只会加速易班侧锁定，必须终态")
        self.assertTrue(clear_cache)

    def test_yiban_lockout_message_is_terminal(self):
        # 真实文案：账号被锁定后 msgCN 原文（生产 2026-09-04 事件第 6 次返回）
        budget, _ = self.s._retry_budget(
            "登录失败: 错误尝试过多，请至易班APP【忘记密码】功能重置密码")
        self.assertEqual(budget, 1)

    def test_other_failure_classes_unchanged(self):
        cases = [
            ("请求被 WAF 风控拦截，请配置 YIBAN_PROXY 代理后重试",
             self.s.RISK_MAX_ATTEMPTS, True),
            ("获取签到任务失败: 未登录或登录已经超时",
             self.s.SESSION_STALE_MAX_ATTEMPTS, True),
            ("未找到签到位置数据（易班未返回该账号的签到点位）",
             self.s.NO_POSITION_MAX_ATTEMPTS, False),
            ("HTTPSConnectionPool 读超时", self.s.MAX_ATTEMPTS, False),
        ]
        for msg, attempts, clear in cases:
            self.assertEqual(self.s._retry_budget(msg), (attempts, clear), msg)


class ClearFusePauseTest(unittest.TestCase):
    """web 层：cred-state.json 不存在 = 从未熔断，必须静默（不刷误导性 WARNING）。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-verify-cool-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n")
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_DB_FILE"] = os.path.join(cls.tmp, "yiban.db")
        os.environ["YIBAN_STATE_DIR"] = cls.tmp
        os.environ["YIBAN_LOG_FILE"] = os.path.join(cls.tmp, "sign.log")
        spec = importlib.util.spec_from_file_location(
            "webapp", os.path.join(BASE, "web", "app.py"))
        cls.webapp = importlib.util.module_from_spec(spec)
        sys.modules["webapp"] = cls.webapp
        with contextlib.suppress(Exception):
            spec.loader.exec_module(cls.webapp)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k in ("YIBAN_ACCOUNTS_KEY", "YIBAN_ENV_FILE", "YIBAN_DB_FILE",
                  "YIBAN_STATE_DIR", "YIBAN_LOG_FILE"):
            os.environ.pop(k, None)

    def setUp(self):
        # unittest 方法按名排序执行：前序用例（如 test_corrupt_*）可能留下状态
        # 文件，每个用例前重建“文件不存在”基线，不依赖类内执行顺序
        with contextlib.suppress(OSError):
            os.remove(self._cred_path())

    def _cred_path(self):
        return os.path.join(self.tmp, "cred-state.json")

    def test_missing_file_is_silent(self):
        # 生产常态：从未有账号触发熔断，cred-state.json 不存在（只有 .lock）
        self.assertFalse(os.path.exists(self._cred_path()))
        with self.assertNoLogs("web", "WARNING"):
            self.webapp.clear_fuse_pause(PHONE1)

    def test_existing_entry_removed(self):
        with open(self._cred_path(), "w", encoding="utf-8") as f:
            json.dump({PHONE1: {"fail_days": 3, "paused_since": "2026-09-01"},
                       PHONE2: {"fail_days": 1}}, f)
        self.webapp.clear_fuse_pause(PHONE1)
        with open(self._cred_path(), encoding="utf-8") as f:
            data = json.load(f)
        self.assertNotIn(PHONE1, data)
        self.assertIn(PHONE2, data, "只清目标账号，不得误伤他人")

    def test_missing_entry_is_silent(self):
        with open(self._cred_path(), "w", encoding="utf-8") as f:
            json.dump({PHONE2: {"fail_days": 1}}, f)
        with self.assertNoLogs("web", "WARNING"):
            self.webapp.clear_fuse_pause(PHONE1)

    def test_corrupt_json_still_warns(self):
        # 真实 I/O/数据错误保留 WARNING 留痕（2026-08-27 审查背景仍成立）。
        # 留痕方为唯一入口 yiban.cred_state（web 侧不再自己读写该文件）。
        with open(self._cred_path(), "w", encoding="utf-8") as f:
            f.write("{not-json")
        with self.assertLogs("yiban.cred_state", "WARNING") as cm:
            self.webapp.clear_fuse_pause(PHONE1)
        self.assertIn("账密状态文件损坏", cm.output[0])

    def test_write_failure_still_warns(self):
        # 保留另一条记录：清空到"无记录"时走的是删文件（不是写盘），
        # 必须让本次清除真正落到写盘路径上才能验证"写失败留痕"。
        with open(self._cred_path(), "w", encoding="utf-8") as f:
            json.dump({PHONE1: {"fail_days": 3},
                       PHONE2: {"fail_days": 1, "paused_since": "2026-09-01"}}, f)
        with mock.patch.object(self.webapp.os, "replace",
                               side_effect=OSError("disk full")), \
             self.assertLogs("yiban.cred_state", "WARNING") as cm:
            self.webapp.clear_fuse_pause(PHONE1)
        self.assertIn("写入账密状态文件失败", cm.output[0])


class _WebAppBase(unittest.TestCase):
    """e2e 公共底座：临时 .env/DB（YIBAN_ACCOUNT_VERIFY=1）+ 每用例全新 app。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-verify-cool-web-")
        # 本文件断言的是**同步**外呼契约（提交即返回 400/429）。A4 异步化后，
        # `YIBAN_ACCOUNT_VERIFY=1` 时默认走异步（提交即 200，结果由后台任务落库），
        # 故这里显式退回同步带闸路径——它仍是受支持的运维路径（`YIBAN_VERIFY_ASYNC=0`），
        # 异步契约由 tests/test_a4_verify_jobs.py 覆盖。
        os.environ["YIBAN_VERIFY_ASYNC"] = "0"
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(
                f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
                f"YIBAN_ADMIN_USER=admin\nYIBAN_ADMIN_PASSWORD={ADMIN_PASS}\n"
                "YIBAN_ACCOUNT_VERIFY=1\n"
            )
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.accounts_file = os.path.join(cls.tmp, "accounts.json")
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_ACCOUNTS_FILE"] = cls.accounts_file
        os.environ["YIBAN_DB_FILE"] = cls.db_file
        os.environ["YIBAN_STATE_DIR"] = cls.tmp
        os.environ["YIBAN_LOG_FILE"] = os.path.join(cls.tmp, "sign.log")
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
        import db
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k in ("YIBAN_ACCOUNTS_KEY", "YIBAN_ENV_FILE", "YIBAN_ACCOUNTS_FILE",
                  "YIBAN_DB_FILE", "YIBAN_STATE_DIR", "YIBAN_LOG_FILE",
                  "YIBAN_VERIFY_ASYNC"):
            os.environ.pop(k, None)

    def setUp(self):
        import db
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        for suffix in ("", "-wal", "-shm"):
            p = self.db_file + suffix
            if os.path.exists(p):
                os.remove(p)
        with open(self.accounts_file, "w", encoding="utf-8") as f:
            json.dump([], f)
        db.init_db(self.db_file, migrate_from=self.accounts_file,
                   env_file=self.env_file)
        db.create_user(EMAIL, self.webapp.generate_password_hash(USER_PASS))
        # 每用例全新 app：验证配额/失败冷却/登录限速均为 create_app 内内存态
        self.app = self.webapp.create_app()
        self.c = self.app.test_client()

    def tearDown(self):
        mock.patch.stopall()

    # ---- 工具 ----
    def _login(self, username, password):
        r = self.c.post("/api/login", json={"username": username,
                                            "password": password})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return self.c.get("/api/me").get_json()["csrf_token"]

    def _csrf(self, token):
        return {"X-CSRF-Token": token}

    def _submit(self, phone, token, password="pw1"):
        return self.c.post("/api/my-accounts", json={
            "name": "测试账号", "phone": phone, "password": password,
        }, headers=self._csrf(token))

    def _verify_fail_rows(self):
        import db
        return db.get_conn().execute(
            "SELECT username, action, target, detail FROM audit_logs "
            "WHERE action LIKE '%verify_fail' ORDER BY id"
        ).fetchall()


class VerifyCooldownWebTest(_WebAppBase):
    """web 层：按手机号的验证失败冷却 + 失败审计（用户提交路径）。"""

    def test_two_auth_fails_then_cooldown_rejects_without_real_verify(self):
        token = self._login(EMAIL, USER_PASS)
        with mock.patch.object(
                self.webapp.signin, "verify_account",
                return_value=(False, AUTH_FAIL_MSG)) as va:
            r1 = self._submit(PHONE1, token)
            self.assertEqual(r1.status_code, 400)
            self.assertIn("账号或密码错误", r1.get_json()["error"])
            r2 = self._submit(PHONE1, token)
            self.assertEqual(r2.status_code, 400)
            r3 = self._submit(PHONE1, token)
            self.assertEqual(r3.status_code, 429, "第 3 次必须被冷却拒绝")
            self.assertEqual(va.call_count, 2,
                             "冷却拒绝不得再发起真实易班登录")
        msg = r3.get_json()["error"]
        self.assertIn("易班账号被锁定", msg, "必须向用户解释失败过多的后果")

    def test_failures_are_audited_without_secrets(self):
        token = self._login(EMAIL, USER_PASS)
        with mock.patch.object(self.webapp.signin, "verify_account",
                               return_value=(False, AUTH_FAIL_MSG)):
            self._submit(PHONE1, token, password="secret-pw-1")
            self._submit(PHONE1, token, password="secret-pw-2")
        rows = self._verify_fail_rows()
        self.assertEqual(len(rows), 2)
        row = rows[0]
        self.assertEqual(row["action"], "my_account_add_verify_fail")
        self.assertEqual(row["username"], EMAIL)
        self.assertEqual(row["target"], "138****0001")
        self.assertEqual(row["detail"], "验证未通过（认证失败）")
        self.assertNotIn("secret-pw", row["detail"], "审计不得含密码")

    def test_network_failure_not_counted(self):
        token = self._login(EMAIL, USER_PASS)
        with mock.patch.object(
                self.webapp.signin, "verify_account",
                return_value=(False,
                              "账号验证异常：HTTPSConnectionPool 读超时")) as va:
            for _ in range(3):
                r = self._submit(PHONE1, token)
                self.assertEqual(r.status_code, 400)
            self.assertEqual(va.call_count, 3, "网络类失败不触发冷却")
        rows = self._verify_fail_rows()
        self.assertEqual([r["detail"] for r in rows],
                         ["验证未通过（其他失败）"] * 3)

    def test_cooldown_is_per_phone(self):
        token = self._login(EMAIL, USER_PASS)
        with mock.patch.object(
                self.webapp.signin, "verify_account",
                return_value=(False, AUTH_FAIL_MSG)) as va:
            self._submit(PHONE1, token)
            self._submit(PHONE1, token)
            r = self._submit(PHONE1, token)
            self.assertEqual(r.status_code, 429)
            other = self._submit(PHONE2, token)
            self.assertEqual(other.status_code, 400,
                             "冷却按手机号隔离，其他号码不受影响")
            self.assertEqual(va.call_count, 3)

    def test_success_path_unaffected(self):
        token = self._login(EMAIL, USER_PASS)
        with mock.patch.object(
                self.webapp.signin, "verify_account",
                return_value=(True, "账号健康，可正常签到")) as va:
            r = self._submit(PHONE1, token)
            self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
            self.assertEqual(va.call_count, 1)
        self.assertEqual(self._verify_fail_rows(), [])


class AdminAddVerifyAuditTest(_WebAppBase):
    """管理员添加路径：同一冷却口径 + account_add_verify_fail 审计。"""

    def test_admin_add_fail_audits_and_cooldowns(self):
        token = self._login("admin", ADMIN_PASS)
        with mock.patch.object(
                self.webapp.signin, "verify_account",
                return_value=(False, AUTH_FAIL_MSG)) as va:
            for _ in range(2):
                r = self.c.post("/api/accounts", json={
                    "name": "测试", "phone": PHONE3, "password": "pw3",
                    "email": EMAIL,
                }, headers=self._csrf(token))
                self.assertEqual(r.status_code, 400)
            r = self.c.post("/api/accounts", json={
                "name": "测试", "phone": PHONE3, "password": "pw3",
                "email": EMAIL,
            }, headers=self._csrf(token))
            self.assertEqual(r.status_code, 429, "管理员路径同受冷却约束")
            self.assertEqual(va.call_count, 2)
        rows = self._verify_fail_rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["action"], "account_add_verify_fail")
        self.assertEqual(rows[0]["username"], "admin")
        self.assertEqual(rows[0]["target"], "138****0003")
        self.assertEqual(rows[0]["detail"], "验证未通过（认证失败）")


class VerifyConcurrencyGateTest(_WebAppBase):
    """A4：外呼校验的**全局**并发闸（2026-09-15）。

    上面两条配额分别是「按会话用户」（VERIFY_MAX）与「按手机号」（失败冷却），都覆盖
    不到"多个账号同时校验"这一维度。实测 8 个并发校验即占满 gunicorn 的 8 个线程 →
    整站约 15 秒完全无响应（/api/clock 探针在饱和期无响应）。
    """

    NET_FAIL = "账号验证异常：HTTPSConnectionPool 读超时"  # 网络类失败：不计冷却

    def _saturate(self):
        """占满全部席位，返回已占数量（调用方负责用 _release 归还）。"""
        n = 0
        while self.webapp._verify_sem.acquire(blocking=False):
            n += 1
        return n

    def _release(self, n):
        for _ in range(n):
            self.webapp._verify_sem.release()

    def test_busy_returns_503_without_consuming_quota(self):
        """席位满 → 503 + 明确文案 + Retry-After，且**不扣**用户配额。"""
        token = self._login(EMAIL, USER_PASS)
        held = self._saturate()
        try:
            self.assertGreater(held, 0)
            r = self._submit(PHONE1, token)
            self.assertEqual(r.status_code, 503, r.get_data(as_text=True))
            self.assertEqual(r.get_json()["error"], self.webapp.VERIFY_BUSY_MSG)
            self.assertEqual(r.headers.get("Retry-After"), "2")
        finally:
            self._release(held)
        # 席位恢复后同一用户应能正常走到验证环节 —— 证明上面的 503 没扣配额
        with mock.patch.object(self.webapp.signin, "verify_account",
                               return_value=(True, "ok")):
            r2 = self._submit(PHONE2, token)
        self.assertEqual(r2.status_code, 200, r2.get_data(as_text=True))

    def test_concurrent_outbound_never_exceeds_max(self):
        """并发驱动闸门时，同时在跑的外呼条数不得超过 VERIFY_CONCURRENCY_MAX。

        直接驱动 `run_verify_with_gate`（闸门本体）：HTTP 层要多套会话并发，
        而 `users.sid` 是**每用户一份**的会话标识——同一用户并发登录会互相吊销
        会话（"新登录踢旧会话"是既定语义），会把断言搅进与闸门无关的噪声里。
        HTTP 层的 503 / 配额 / 异常三条路径由本类的其余用例覆盖。
        """
        import threading as _threading
        import time as _time

        cur = {"n": 0, "max": 0}
        lock = _threading.Lock()
        n_workers = 6
        gate = _threading.Barrier(n_workers)
        results = []
        limits = {}  # 每用户配额表：本用例不触发配额

        def _fake(_clean):
            with lock:
                cur["n"] += 1
                cur["max"] = max(cur["max"], cur["n"])
            _time.sleep(0.2)
            with lock:
                cur["n"] -= 1
            return self.NET_FAIL

        def _worker():
            gate.wait(timeout=20)
            try:
                self.webapp.run_verify_with_gate({"phone": PHONE1}, EMAIL, limits)
                results.append("done")
            except self.webapp.VerifyGateBusy:
                results.append("busy")
            except Exception as e:  # 意外异常收集后统一断言
                results.append(f"err:{type(e).__name__}")

        with mock.patch.object(self.webapp, "_verify_account_clean", side_effect=_fake):
            ts = [_threading.Thread(target=_worker) for _ in range(n_workers)]
            for t in ts:
                t.start()
            for t in ts:
                t.join(timeout=30)

        self.assertEqual(len(results), n_workers, f"应有 {n_workers} 个结果，实际 {results}")
        self.assertLessEqual(
            cur["max"], self.webapp.VERIFY_CONCURRENCY_MAX,
            f"同时外呼数不得超过 {self.webapp.VERIFY_CONCURRENCY_MAX}",
        )
        self.assertIn("busy", results, "席位满时应立即失败（VerifyGateBusy）而非排队")
        self.assertNotIn("err:RuntimeError", results)
        # 席位必须全部归还：再串行跑一轮应全部成功
        with mock.patch.object(self.webapp, "_verify_account_clean", return_value=None):
            for _ in range(self.webapp.VERIFY_CONCURRENCY_MAX):
                self.webapp.run_verify_with_gate({"phone": PHONE1}, EMAIL, limits)

    def test_semaphore_not_leaked_when_quota_exceeded(self):
        """配额拒绝路径不得漏掉席位。"""
        token = self._login(EMAIL, USER_PASS)
        with mock.patch.object(self.webapp.signin, "verify_account",
                               return_value=(False, self.NET_FAIL)):
            for i in range(self.webapp.VERIFY_MAX):
                self.assertEqual(self._submit(f"1380000020{i}", token).status_code, 400)
            r = self._submit("13800000209", token)
        self.assertEqual(r.status_code, 429, r.get_data(as_text=True))
        held = self._saturate()
        self.assertEqual(held, self.webapp.VERIFY_CONCURRENCY_MAX,
                         "拒绝路径泄漏了席位")
        self._release(held)

    def test_semaphore_not_leaked_when_verify_raises(self):
        """校验异常路径不得漏掉席位。"""
        token = self._login(EMAIL, USER_PASS)
        with mock.patch.object(self.webapp.signin, "verify_account",
                               side_effect=RuntimeError("boom")):
            r = self._submit(PHONE1, token)
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        self.assertIn("账号验证异常", r.get_json()["error"])
        held = self._saturate()
        self.assertEqual(held, self.webapp.VERIFY_CONCURRENCY_MAX,
                         "异常路径泄漏了席位")
        self._release(held)


class VerifyCooldownHelpersTest(unittest.TestCase):
    """冷却 helper 纯单元：窗口/冷却边界与合成时间。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-verify-cool-unit-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n")
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_DB_FILE"] = os.path.join(cls.tmp, "yiban.db")
        os.environ["YIBAN_STATE_DIR"] = cls.tmp
        os.environ["YIBAN_LOG_FILE"] = os.path.join(cls.tmp, "sign.log")
        spec = importlib.util.spec_from_file_location(
            "webapp", os.path.join(BASE, "web", "app.py"))
        cls.webapp = importlib.util.module_from_spec(spec)
        sys.modules["webapp"] = cls.webapp
        with contextlib.suppress(Exception):
            spec.loader.exec_module(cls.webapp)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k in ("YIBAN_ACCOUNTS_KEY", "YIBAN_ENV_FILE", "YIBAN_DB_FILE",
                  "YIBAN_STATE_DIR", "YIBAN_LOG_FILE"):
            os.environ.pop(k, None)

    def test_second_fail_in_window_triggers_cooldown(self):
        store = {}
        w = self.webapp
        self.assertEqual(
            w._record_verify_failure(store, PHONE1, AUTH_FAIL_MSG, 1000.0),
            "认证失败")
        self.assertEqual(
            w._verify_fail_cooldown_remaining(store, PHONE1, 1001.0), 0,
            "仅 1 次失败不冷却")
        w._record_verify_failure(store, PHONE1, AUTH_FAIL_MSG, 1100.0)
        rem = w._verify_fail_cooldown_remaining(store, PHONE1, 1101.0)
        self.assertTrue(590 <= rem <= w.VERIFY_FAIL_COOLDOWN)

    def test_stale_failures_do_not_accumulate(self):
        store = {}
        w = self.webapp
        w._record_verify_failure(store, PHONE1, AUTH_FAIL_MSG, 1000.0)
        # 窗口（15 分钟）外的零星失败从零计数，不与旧失败累计
        w._record_verify_failure(store, PHONE1, AUTH_FAIL_MSG, 1000.0 + 1900)
        self.assertEqual(
            w._verify_fail_cooldown_remaining(store, PHONE1, 1000.0 + 1901), 0)

    def test_cooldown_expires(self):
        store = {}
        w = self.webapp
        w._record_verify_failure(store, PHONE1, AUTH_FAIL_MSG, 1000.0)
        w._record_verify_failure(store, PHONE1, AUTH_FAIL_MSG, 1001.0)
        self.assertGreater(
            w._verify_fail_cooldown_remaining(store, PHONE1, 1002.0), 0)
        self.assertEqual(
            w._verify_fail_cooldown_remaining(store, PHONE1, 1000.0 + 601), 0,
            "冷却到期后放行")

    def test_non_auth_failures_leave_store_untouched(self):
        store = {}
        w = self.webapp
        for msg in ("账号验证异常：连接超时",
                    "账号验证未通过：获取签到任务失败: 系统繁忙"):
            self.assertEqual(
                w._record_verify_failure(store, PHONE1, msg, 1000.0),
                "其他失败")
        self.assertEqual(store, {}, "非认证失败不得写入任何状态")
        self.assertEqual(
            w._verify_fail_cooldown_remaining(store, PHONE1, 1001.0), 0)


if __name__ == "__main__":
    unittest.main()
