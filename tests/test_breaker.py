# -*- coding: utf-8 -*-
"""账密熔断器（circuit breaker）测试：v0.18.4 核心行为防回归。

用法（在项目根目录）：
    py -m pytest tests/test_breaker.py -v        # 需要 pytest
    py tests/test_breaker.py                     # 无 pytest 也可直接运行

覆盖：
- 凭据失败计数：连续 3 天 → 暂停 + 试探日；同一天多次失败只计 1 天
- 成功清除计数；网络类失败不计数
- run_queue_retry：暂停中零请求；--only 手动签到绕过；半开试探日执行并恢复
- web：编辑账号（改密码）清除 cred-state 暂停记录
"""
import contextlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock as mock
from datetime import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class BreakerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-brk-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write("YIBAN_ADMIN_USER=admin\nYIBAN_ADMIN_PASSWORD=TestPass1234!\n")
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_ACCOUNTS_KEY"] = "a" * 64
        os.environ["YIBAN_ACCOUNTS_FILE"] = os.path.join(cls.tmp, "accounts.json")
        os.environ["YIBAN_USERS_FILE"] = os.path.join(cls.tmp, "users.json")
        os.environ["YIBAN_DB_FILE"] = os.path.join(cls.tmp, "yiban.db")
        os.environ["YIBAN_STATE_DIR"] = cls.tmp
        os.environ["YIBAN_LOG_FILE"] = os.path.join(cls.tmp, "sign.log")
        sys.path.insert(0, BASE)
        sys.path.insert(0, os.path.join(BASE, "scripts"))
        sys.path.insert(0, os.path.join(BASE, "web"))
        global db, signin
        import db
        import signin
        with open(os.environ["YIBAN_ACCOUNTS_FILE"], "w", encoding="utf-8") as f:
            json.dump([], f)
        db.init_db(os.environ["YIBAN_DB_FILE"],
                   migrate_from=os.environ["YIBAN_ACCOUNTS_FILE"], env_file=cls.env_file)
        db.add_account({"name": "A", "phone": "13800138000", "password": "p1",
                        "status": "active", "owner": "admin"})

    @classmethod
    def tearDownClass(cls):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k in ("YIBAN_ENV_FILE", "YIBAN_ACCOUNTS_KEY", "YIBAN_ACCOUNTS_FILE",
                  "YIBAN_USERS_FILE", "YIBAN_DB_FILE", "YIBAN_STATE_DIR", "YIBAN_LOG_FILE"):
            os.environ.pop(k, None)

    # ---- 1. _update_cred_state 计数逻辑 ----
    # 周一~周三（2026-08-17/18/19），避开周日（周日签到开关默认关闭会提前退出）
    D1, D2, D3, D4 = "2026-08-17", "2026-08-18", "2026-08-19", "2026-08-20"

    def test_three_days_fail_pauses_with_probe(self):
        cs = {}
        signin._update_cred_state(cs, "13800138000", False, "登录失败: 账号或密码错误", self.D1)
        self.assertEqual(cs["13800138000"]["fail_days"], 1)
        signin._update_cred_state(cs, "13800138000", False, "登录失败: 账号或密码错误", self.D1)
        self.assertEqual(cs["13800138000"]["fail_days"], 1, "同一天重复失败只计 1 天")
        signin._update_cred_state(cs, "13800138000", False, "登录失败: 账号或密码错误", self.D2)
        self.assertEqual(cs["13800138000"]["fail_days"], 2)
        signin._update_cred_state(cs, "13800138000", False, "登录失败: 账号或密码错误", self.D3)
        cred = cs["13800138000"]
        self.assertEqual(cred["fail_days"], 3)
        self.assertEqual(cred["paused_since"], self.D3)
        self.assertEqual(cred["probe_date"], "2026-08-26", "暂停日 + 7 天 = 试探日")

    def test_success_clears_record(self):
        cs = {"13800138000": {"fail_days": 3, "paused_since": self.D3}}
        signin._update_cred_state(cs, "13800138000", True, "签到成功", self.D4)
        self.assertNotIn("13800138000", cs)

    def test_network_failure_not_counted(self):
        cs = {}
        signin._update_cred_state(cs, "13800138000", False, "Connection timed out", self.D4)
        self.assertNotIn("13800138000", cs, "网络类失败不应计数")

    # ---- 2. run_queue_retry / main 行为 ----
    def _run_main(self, fixed_dt, cred_state, only=False):
        class FakeDT(datetime):
            _f = fixed_dt

            @classmethod
            def now(cls, tz=None):
                return cls._f

        calls = self._calls

        def fake_login(self):
            self.logged_in = True

        def fake_signin(self):
            calls.append(self.account.phone)
            return (True, "签到成功", False, signin.STATUS_SUCCESS)

        argv = ["signin.py"] + (["--only", "13800138000"] if only else [])
        with mock.patch.object(signin, "datetime", FakeDT), \
             mock.patch.object(signin.YibanClient, "login_killyiban", fake_login), \
             mock.patch.object(signin.YibanClient, "signin", fake_signin), \
             mock.patch.object(signin.time, "sleep"), \
             mock.patch.object(signin, "_load_cred_state", return_value=cred_state), \
             mock.patch.object(signin, "_save_cred_state"), \
             mock.patch.object(sys, "argv", argv), \
             contextlib.suppress(SystemExit):
            signin.main()

    def setUp(self):
        self._calls = []

    def test_paused_account_zero_requests(self):
        cred = {"13800138000": {"fail_days": 3, "last_fail": self.D3,
                                 "paused_since": self.D3, "probe_date": "2026-08-26"}}
        self._run_main(datetime(2026, 8, 19, 6, 40), dict(cred))
        self.assertEqual(self._calls, [], "暂停中不应发起任何请求")

    def test_only_bypasses_pause(self):
        cred = {"13800138000": {"fail_days": 3, "last_fail": self.D3,
                                 "paused_since": self.D3, "probe_date": "2026-08-26"}}
        self._run_main(datetime(2026, 8, 19, 6, 40), dict(cred), only=True)
        self.assertEqual(self._calls, ["13800138000"], "--only 手动签到应绕过暂停")

    def test_probe_day_executes_and_recovers(self):
        cred = {"13800138000": {"fail_days": 3, "last_fail": self.D3,
                                 "paused_since": self.D3, "probe_date": "2026-08-26"}}
        self._run_main(datetime(2026, 8, 26, 6, 40), dict(cred))
        self.assertEqual(self._calls, ["13800138000"], "试探日应执行一次")

    # ---- 3. web 编辑账号清除 cred-state ----
    def test_account_edit_clears_cred_state(self):
        spec = importlib.util.spec_from_file_location("webapp", os.path.join(BASE, "web", "app.py"))
        webapp = importlib.util.module_from_spec(spec)
        sys.modules["webapp"] = webapp
        spec.loader.exec_module(webapp)
        app = webapp.create_app()
        client = app.test_client()
        r = client.post("/api/login", json={"username": "admin", "password": "TestPass1234!"})
        self.assertEqual(r.status_code, 200)
        with client.session_transaction() as sess:
            sess["csrf_token"] = "t"
        state_path = os.path.join(self.tmp, "cred-state.json")
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump({"13800138000": {"fail_days": 3, "paused_since": self.D3}}, f)
        r = client.put("/api/accounts/0",
                       json={"name": "A", "phone": "13800138000", "password": "newpass1234"},
                       headers={"X-CSRF-Token": "t"})
        self.assertEqual(r.status_code, 200, r.get_json())
        with open(state_path, encoding="utf-8") as f:
            cs = json.load(f)
        self.assertNotIn("13800138000", cs, "编辑账号（改密码）应清除暂停记录")

    # ---- 4. BOM 容错：带 BOM 的状态文件应正常读取 ----
    def test_load_cred_state_tolerates_bom(self):
        state_path = os.path.join(self.tmp, "cred-state.json")
        with open(state_path, "w", encoding="utf-8-sig") as f:
            json.dump({"13800138000": {"fail_days": 3}}, f)
        self.assertEqual(signin._load_cred_state()["13800138000"]["fail_days"], 3)

    # ---- 5. 空状态删除 + dur 耗时字段（2026-08-16，P5b/P6）----
    def test_save_cred_state_empty_removes_file(self):
        state_path = signin._cred_state_path()
        signin._save_cred_state({"13800138000": {"fail_days": 3}})
        self.assertTrue(os.path.exists(state_path), "有暂停记录时应存在")
        signin._save_cred_state({})
        self.assertFalse(os.path.exists(state_path), "无暂停记录时应删除文件（无暂停=不存在语义）")

    def test_write_sign_state_records_dur(self):
        signin._write_sign_state("13800138000", "success", "签到成功", dur=3.45)
        with open(os.path.join(self.tmp, "sign-state-" + datetime.now().strftime("%Y-%m-%d") + ".json"),
                  encoding="utf-8") as f:
            data = json.load(f)
        entry = data["13800138000"]
        self.assertEqual(entry["dur"], 3.45, "应记录单次尝试耗时（P6）")
        self.assertEqual(entry["status"], "success")


if __name__ == "__main__":
    unittest.main(verbosity=2)
