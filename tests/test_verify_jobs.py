# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""在线校验任务：生命周期、失败冷却与并发闸。

标签：E · Web：认证/权限/API
覆盖：在线校验任务的全生命周期（创建/领取/结算/取消、超龄收口、保留期清理）、失败冷却与熔断、外呼并发闸、按手机号的级联清理，以及「迟到的异步结果不得覆盖人工决定」的状态 CAS
对应实现：`yiban/store/verify_jobs.py` 与 `web/app.py` 的校验入口（`run_verify_with_gate`、`reclaim_stale_verify_jobs`、`update_account_status_if`、`_record_verify_failure`、`_verify_fail_cooldown_remaining`）
关键断言：席位满→503 + Retry-After 且**不扣**用户配额；并发外呼同时在跑数不超 `VERIFY_CONCURRENCY_MAX`，配额拒绝与异常路径都不漏席位；超龄与保留期判定只认业务钟（UTC 主机上按宿主 `datetime.now()` 会误判）；每张以 phone 为键的表都必须在级联清单内（schema 枚举兜底，新增表即报红）；管理员审批后校验失败仍保持 active
依赖：纯本地 Flask test client + 临时 `.env`/SQLite，不联网、不访问真实易班接口；无需 node；`verify_account` 一律打桩，绝不联网。**异步用例必须在 patch 存活期内轮询到终态**，否则后台线程拿到真实实现会真的外呼易班

`yiban/store/verify_jobs.py` + Web 侧校验入口共同构成在线校验：任务创建/领取/结算/取消、
超龄回收、管理员判定优先、失败后的冷却与熔断。本文件并两处断言：生命周期与并发闸门，
以及失败冷却、重试预算与冷却助手的行为。

功能：在线校验任务全生命周期与冷却/并发门禁的回归。
归属：`yiban/store/verify_jobs.py` 与 `web/` 校验入口的交叉测试。
复用：`BASE` / `TEST_KEY` / `PHONE2` 等装配常量与 Web 测试基类。
通信：写临时 SQLite 与临时 `.env`，经 Flask test client 与任务 API 读写。
"""
import contextlib
import datetime
import importlib.util
import json
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


EMAIL_COOL = "user1@test.local"


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
        # 异步契约由本文件的 VerifyAsyncSubmitTest 覆盖。
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
        db.create_user(EMAIL_COOL, self.webapp.generate_password_hash(USER_PASS))
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
        token = self._login(EMAIL_COOL, USER_PASS)
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
        token = self._login(EMAIL_COOL, USER_PASS)
        with mock.patch.object(self.webapp.signin, "verify_account",
                               return_value=(False, AUTH_FAIL_MSG)):
            self._submit(PHONE1, token, password="secret-pw-1")
            self._submit(PHONE1, token, password="secret-pw-2")
        rows = self._verify_fail_rows()
        self.assertEqual(len(rows), 2)
        row = rows[0]
        self.assertEqual(row["action"], "my_account_add_verify_fail")
        self.assertEqual(row["username"], EMAIL_COOL)
        self.assertEqual(row["target"], "138****0001")
        self.assertEqual(row["detail"], "验证未通过（认证失败）")
        self.assertNotIn("secret-pw", row["detail"], "审计不得含密码")

    def test_network_failure_not_counted(self):
        token = self._login(EMAIL_COOL, USER_PASS)
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
        token = self._login(EMAIL_COOL, USER_PASS)
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
        token = self._login(EMAIL_COOL, USER_PASS)
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
                    "email": EMAIL_COOL,
                }, headers=self._csrf(token))
                self.assertEqual(r.status_code, 400)
            r = self.c.post("/api/accounts", json={
                "name": "测试", "phone": PHONE3, "password": "pw3",
                "email": EMAIL_COOL,
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
        token = self._login(EMAIL_COOL, USER_PASS)
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
                self.webapp.run_verify_with_gate({"phone": PHONE1}, EMAIL_COOL, limits)
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
                self.webapp.run_verify_with_gate({"phone": PHONE1}, EMAIL_COOL, limits)

    def test_semaphore_not_leaked_when_quota_exceeded(self):
        """配额拒绝路径不得漏掉席位。"""
        token = self._login(EMAIL_COOL, USER_PASS)
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
        token = self._login(EMAIL_COOL, USER_PASS)
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


EMAIL_LIFE = "u1@test.local"


EMAIL2 = "u2@test.local"


PHONE = "13800000001"


PHONE_KEYED_TABLES = {"time_prefs", "session_cache", "sign_events", "verify_jobs",
                     "sign_claims", "sign_tasks"}


def _ago(seconds):
    return (datetime.datetime.now() - datetime.timedelta(seconds=seconds)).strftime(
        "%Y-%m-%d %H:%M:%S")


class _LifecycleBase(unittest.TestCase):
    """临时 .env/DB + 每用例全新 app（与 VerifyAsyncSubmitTest 同一套骨架）。"""

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
        db.create_user(EMAIL_LIFE, self.webapp.generate_password_hash(USER_PASS))
        db.create_user(EMAIL2, self.webapp.generate_password_hash(USER_PASS))
        self.app = self.webapp.create_app()
        self.c = self.app.test_client()

    def tearDown(self):
        mock.patch.stopall()

    # ---- 工具 ----
    def _login(self, email=EMAIL_LIFE, password=None):
        r = self.c.post("/api/login", json={"username": email,
                                           "password": password or USER_PASS})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return self.c.get("/api/me").get_json()["csrf_token"]

    def _account(self, phone=PHONE, owner=EMAIL_LIFE, status="pending"):
        return db.add_account({
            "name": "测试", "phone": phone, "password": "pw", "owner": owner,
            "status": status, "phone_model": "", "phone_code": "",
        })

    def _job(self, account_id, phone=PHONE, owner=EMAIL_LIFE, prev_status="pending",
             age=None, running=False):
        """建一条任务并按需回拨时间戳（模拟"超龄"），返回 job_id。"""
        job_id, _ = db.create_verify_job(account_id, phone, owner,
                                        prev_status=prev_status)
        conn = db.get_conn()
        if age is not None:
            conn.execute("UPDATE verify_jobs SET created_at=? WHERE id=?",
                         (_ago(age), job_id))
        # running 的超龄判定读的是 started_at，所以这里回拨它而不是 created_at
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
                     "created_at) VALUES (?,?,?,?,?)", (None, phone, EMAIL_LIFE, "pending", _ago(0)))
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
        acc_a = self._account(PHONE, EMAIL_LIFE, status="pending")
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
        job_id, _ = db.create_verify_job(acc, PHONE, EMAIL_LIFE, prev_status="pending")
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
        job_id, _ = db.create_verify_job(acc, PHONE, EMAIL_LIFE, prev_status="pending")
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
            self.webapp.verify_jobs.run(job_id, {"phone": PHONE}, EMAIL_LIFE, acc,
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
            self.webapp.verify_jobs.run(job_id, {"phone": PHONE}, EMAIL_LIFE, acc,
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
        db.delete_accounts_by_owner(EMAIL_LIFE)
        self._assert_jobs_gone()

    def test_delete_user_with_accounts_cascades(self):
        self._account()
        self._seed_phone_rows()
        db.delete_user_with_accounts(EMAIL_LIFE)
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
        db.batch_user_ops([("delete_user_with_accounts", EMAIL_LIFE)])
        self._assert_jobs_gone()

    def test_purge_deleted_users_hard_cascades(self):
        self._account()
        self._seed_phone_rows()
        db.soft_delete_user_with_accounts(EMAIL_LIFE)  # 注销：用户与账号一并软删
        db.purge_deleted_users_hard([EMAIL_LIFE])
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


EMAIL = "u1@test.local"


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
        """标签：schema 版本。v15 = 建 verify_jobs 表的那版迁移；钉的是「新库的 user_version
        必须落在迁移链末档」，停在旧档就意味着任务表压根不存在。"""
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
