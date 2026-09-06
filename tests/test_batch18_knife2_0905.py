# -*- coding: utf-8 -*-
"""批次18 刀2（通知/调度/内存卫生）回归（2026-09-05）。

六项修复：
1. M4 手动签到冷却单源化：单条与批量共用 YIBAN_BATCH_SIGN_COOLDOWN_SEC 计数——
   spawn 成功前检查、成功后刷新基准；_run_batch finally 不再无条件重置（失败不刷基准）；
2. M5 批量超时缩放+上限：选中账号数复用 BATCH_OP_LIMIT(10) 上限（超出 400）；
   队列等待超时 max(300, 120*n+300)（_wait_signin_proc 默认参数保持 300）；
3. M7 零成功/窗口外告警兜底：_flush_admin_mail_summary 收件人为空 warning 留痕；
   邮件发送失败（False/异常）降级 send_notification(urgent=True, force=True) 走 webhook；
4. M8 登录失败告警独立账本：notify.send 新增 ledger=；web 登录失败告警走
   ledger="login_fail"，独立日额度 YIBAN_LOGINFAIL_DAILY_MAX（默认 3，0=不限）；
5. P3-1 限速内存表卫生：_restore_fail_rate / _verify_limits / _admin_delete_limits
   写入路径补 _ip_store_trim；登录 fail_key 的 username 与 restore 的 email 统一 [:128]；
6. P3-6 _atomic_write 创建即 0600（os.open + fdopen，与 account_crypto 口径一致）。

全程 mock（Popen / SMTP / webhook），纯本地 Flask test client，无任何网络请求。
用法（项目根目录）：python -m pytest tests/test_batch18_knife2_0905.py -v
"""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import stat
import sys
import tempfile
import time
import unittest
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "scripts"))
sys.path.insert(0, os.path.join(BASE, "web"))

TEST_KEY = "a" * 64
ADMIN_PASS = "MasterPass#2026"
SCT_KEY = "SCT406257TESTTESTTESTTEST"


class _RecordingProc:
    """fake Popen 返回值：记录 wait(timeout=...) 收到的超时值（M5 断言用）。"""

    def __init__(self, recorded):
        self._recorded = recorded
        self.returncode = 0

    def wait(self, timeout=None):
        self._recorded.append(timeout)
        return 0

    def terminate(self):
        pass

    def kill(self):
        pass


class Batch18Knife2WebTest(unittest.TestCase):
    """web/app.py 侧：M4 冷却单源 / M5 上限与超时 / M8 接线 / P3-1 / P3-6。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-knife2-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        cls._env_content = (
            f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
            f"YIBAN_ADMIN_USER=admin\nYIBAN_ADMIN_PASSWORD={ADMIN_PASS}\n"
        )
        with io.open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(cls._env_content)
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.accounts_file = os.path.join(cls.tmp, "accounts.json")
        cls.users_file = os.path.join(cls.tmp, "users.json")
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_ACCOUNTS_FILE"] = cls.accounts_file
        os.environ["YIBAN_USERS_FILE"] = cls.users_file
        os.environ["YIBAN_DB_FILE"] = cls.db_file
        os.environ["YIBAN_STATE_DIR"] = cls.tmp
        global db
        import db
        spec = importlib.util.spec_from_file_location(
            "webapp", os.path.join(BASE, "web", "app.py"))
        cls.webapp = importlib.util.module_from_spec(spec)
        sys.modules["webapp"] = cls.webapp
        with contextlib.suppress(Exception):
            spec.loader.exec_module(cls.webapp)
        # Popen 类级 patch（沿用 test_batch_sign_cooldown 的批次16 修复口径）：
        # 覆盖后台队列线程的任意调度时刻，防真实 spawn signin 子进程。
        cls.wait_timeouts = []  # _RecordingProc 记录的 wait(timeout=...) 值
        cls._popen_patch = mock.patch.object(
            cls.webapp.subprocess, "Popen",
            side_effect=lambda *a, **k: _RecordingProc(cls.wait_timeouts))
        cls._popen_patch.start()

    @classmethod
    def tearDownClass(cls):
        cls._popen_patch.stop()
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k in ("YIBAN_ACCOUNTS_KEY", "YIBAN_ENV_FILE", "YIBAN_ACCOUNTS_FILE",
                  "YIBAN_USERS_FILE", "YIBAN_DB_FILE", "YIBAN_STATE_DIR"):
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
        with open(self.accounts_file, "w", encoding="utf-8") as f:
            json.dump([], f)
        db.init_db(self.db_file, migrate_from=self.accounts_file, env_file=self.env_file)
        self._set_cooldown(None)
        self.wait_timeouts.clear()

    def tearDown(self):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None

    # ---- 辅助 ----
    def _set_cooldown(self, value):
        """写/删 .env 的冷却键（None=删键回默认 1800s）。"""
        with open(self.env_file, encoding="utf-8-sig") as f:
            lines = f.read().splitlines()
        lines = [ln for ln in lines
                 if not ln.strip().startswith("YIBAN_BATCH_SIGN_COOLDOWN_SEC=")]
        if value is not None:
            lines.append(f"YIBAN_BATCH_SIGN_COOLDOWN_SEC={value}")
        with open(self.env_file, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

    def _add_accounts(self, n, start=0):
        for i, phone in enumerate(self._phones(n, start)):
            db.add_account({
                "phone": phone, "password": f"pw{i}",
                "owner": "admin", "name": f"t{i}", "status": "active",
                "phone_code": "",
            })

    @staticmethod
    def _phones(n, start=0):
        """生成 n 个互异的 11 位测试手机号（与账号添加顺序一致，供 ids 对位）。"""
        return [f"138{start + i:08d}" for i in range(n)]

    def _login(self, c):
        r = c.post("/api/login", json={"username": "admin", "password": ADMIN_PASS})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return c.get("/api/me").get_json()["csrf_token"]

    def _trigger_batch(self, c, csrf, phones):
        # ids 按 _phones 编码的账号下标还原（138{start+i:08d} → i），任意子集与
        # load_accounts() 顺序对位，避免 409「账号列表已变化」（防错位校验拦截）
        ids = [int(p[3:]) for p in phones]
        return c.post("/api/signin/batch",
                      json={"ids": ids, "phones": list(phones)},
                      headers={"X-CSRF-Token": csrf})

    def _trigger_until_batch_done(self, c, csrf, phones, deadline_s=15):
        """触发批量签到；若后台队列未完成（"正在执行"）则轮询重试至完成。"""
        deadline = time.time() + deadline_s
        while True:
            r = self._trigger_batch(c, csrf, phones)
            if r.status_code != 429:
                return r
            err = r.get_json().get("error", "")
            if "正在执行" in err and time.time() < deadline:
                time.sleep(0.2)
                continue
            return r

    # =====================================================================
    # 1. M4 手动签到冷却单源化
    # =====================================================================
    def test_m4_single_trigger_blocks_batch_within_cooldown(self):
        """验收：单条手动签到成功后立刻触发批量 → 429 冷却提示（共用同一计数）。"""
        self._add_accounts(3)
        phones = self._phones(3)
        c = self.webapp.create_app().test_client()
        csrf = self._login(c)
        r1 = c.post("/api/signin", json={"phone": phones[0]},
                    headers={"X-CSRF-Token": csrf})
        self.assertEqual(r1.status_code, 200, r1.get_data(as_text=True))
        r2 = self._trigger_batch(c, csrf, phones[1:])
        self.assertEqual(r2.status_code, 429, r2.get_data(as_text=True))
        self.assertIn("冷却中", r2.get_json()["error"])

    def test_m4_single_trigger_blocks_another_single(self):
        """验收：单条触发后立刻再触发任意号 → 429 冷却提示（非 per-phone 语义）。"""
        self._add_accounts(3)
        phones = self._phones(3)
        c = self.webapp.create_app().test_client()
        csrf = self._login(c)
        r1 = c.post("/api/signin", json={"phone": phones[0]},
                    headers={"X-CSRF-Token": csrf})
        self.assertEqual(r1.status_code, 200, r1.get_data(as_text=True))
        r2 = c.post("/api/signin", json={"phone": phones[1]},
                    headers={"X-CSRF-Token": csrf})
        self.assertEqual(r2.status_code, 429, r2.get_data(as_text=True))
        self.assertIn("冷却中", r2.get_json()["error"])

    def test_m4_cooldown_zero_disables_for_single_and_batch(self):
        """反面：YIBAN_BATCH_SIGN_COOLDOWN_SEC=0 时单条连发不受拦（共用开关）。"""
        self._add_accounts(3)
        phones = self._phones(3)
        self._set_cooldown(0)
        c = self.webapp.create_app().test_client()
        csrf = self._login(c)
        for _ in range(2):
            r = c.post("/api/signin", json={"phone": phones[0]},
                       headers={"X-CSRF-Token": csrf})
            self.assertIn(r.status_code, (200, 429))
            if r.status_code == 429:  # 60s per-phone 防抖仍在（语义保持）
                self.assertIn("正在签到", r.get_json()["error"])
        r = self._trigger_batch(c, csrf, phones[1:])
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

    def test_m4_failed_batch_spawn_does_not_refresh_baseline(self):
        """失败不刷基准：spawn 失败（Popen 抛 FileNotFoundError）后，单条签到不得
        撞上冷却（新行为：launch 失败 → 500 启动失败；旧行为：finally 无条件刷基准
        → 单条会被 429 冷却拦下）。"""
        self._add_accounts(3)
        phones = self._phones(3)
        c = self.webapp.create_app().test_client()
        csrf = self._login(c)
        with mock.patch.object(self.webapp.subprocess, "Popen",
                               side_effect=FileNotFoundError("no script")):
            r = self._trigger_until_batch_done(c, csrf, phones[:2])
            self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
            r2 = c.post("/api/signin", json={"phone": phones[2]},
                        headers={"X-CSRF-Token": csrf})
        self.assertEqual(r2.status_code, 500, r2.get_data(as_text=True))
        self.assertIn("启动失败", r2.get_json()["error"],
                      "单条应越过冷却检查后在启动环节失败（基准未被失败批量刷新）")

    # =====================================================================
    # 2. M5 批量超时缩放 + 上限
    # =====================================================================
    def test_m5_batch_over_limit_400(self):
        """验收：选中 11 个号 → 400（与 /api/accounts/batch 的 BATCH_OP_LIMIT 口径一致）。"""
        self._add_accounts(11)
        c = self.webapp.create_app().test_client()
        csrf = self._login(c)
        r = self._trigger_batch(c, csrf, self._phones(11))
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        self.assertIn("最多 10 个账号", r.get_json()["error"])

    def test_m5_batch_at_limit_accepted(self):
        """反面：10 个号（恰在上限内）→ 200 正常入队。"""
        self._add_accounts(10)
        c = self.webapp.create_app().test_client()
        csrf = self._login(c)
        r = self._trigger_batch(c, csrf, self._phones(10))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

    def test_m5_batch_wait_timeout_formula(self):
        """验收：超时计算公式 max(300, 120*n+300) 单测（默认参数 300 不动）。"""
        f = self.webapp._batch_wait_timeout
        self.assertEqual(f(0), 300)
        self.assertEqual(f(1), 420)
        self.assertEqual(f(5), 900)
        self.assertEqual(f(10), 1500)  # 120*10+300
        self.assertEqual(self.webapp._wait_signin_proc.__defaults__[0], 300,
                         "_wait_signin_proc 默认参数必须保持 300")

    def test_m5_batch_wait_timeout_scaled_at_call_site(self):
        """调用点传参缩放：2 个号的后台队列 wait(timeout=540)。完成后基准已刷新
        （下次批量 429 冷却中），以此确认 wait 已真实发生再断言。"""
        self._add_accounts(2)
        phones = self._phones(2)
        c = self.webapp.create_app().test_client()
        csrf = self._login(c)
        r = self._trigger_batch(c, csrf, phones)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        deadline = time.time() + 15
        while time.time() < deadline:
            probe = self._trigger_batch(c, csrf, phones)
            if probe.status_code == 429 and "冷却中" in probe.get_json().get("error", ""):
                break  # 冷却基准已挂 = spawn 成功 = wait(timeout=...) 已执行
            time.sleep(0.1)
        self.assertEqual(self.wait_timeouts, [540],
                         f"2 号队列超时应为 120*2+300=540，实际 {self.wait_timeouts}")

    # =====================================================================
    # 4. M8 web 侧接线：登录失败告警走独立账本
    # =====================================================================
    def test_m8_login_alert_routed_to_loginfail_ledger(self):
        """验收：3 次登录失败 → notify.send 收到 ledger="login_fail"（走真实
        send_notification 验证透传链路）；urgent 仍按喷洒判据为 False。"""
        c = self.webapp.create_app().test_client()
        with mock.patch.object(self.webapp, "verify_admin", return_value=False), \
             mock.patch.object(self.webapp, "check_password_hash", return_value=False), \
             mock.patch.object(self.webapp, "_constant_time_dummy", lambda pwd: None), \
             mock.patch.object(self.webapp.notify, "send") as nsend:
            for _ in range(self.webapp.LOGIN_FAIL_NOTIFY):
                r = c.post("/api/login", json={"username": "admin", "password": "WrongPass#111"})
                self.assertEqual(r.status_code, 401)
        self.assertEqual(nsend.call_count, 1)
        kwargs = nsend.call_args.kwargs
        self.assertEqual(kwargs.get("ledger"), "login_fail",
                         "登录失败告警必须走 login_fail 独立账本")
        self.assertIs(kwargs.get("urgent"), False)

    # =====================================================================
    # 5. P3-1 限速内存表卫生
    # =====================================================================
    def test_p31_login_fail_key_truncated_to_128(self):
        """验收：200 长用户名截断——同 128 前缀的不同超长用户名共享失败计数
        （3+2=5 次即锁定）；若未截断则第二键独立计数、第 5 次仍是 401。"""
        c = self.webapp.create_app().test_client()
        long_a, long_b = "a" * 200, "a" * 128 + "b" * 72
        with mock.patch.object(self.webapp, "verify_admin", return_value=False), \
             mock.patch.object(self.webapp, "check_password_hash", return_value=False), \
             mock.patch.object(self.webapp, "_constant_time_dummy", lambda pwd: None), \
             mock.patch.object(self.webapp, "send_notification"):
            for _ in range(3):
                c.post("/api/login", json={"username": long_a, "password": "x"})
            r = c.post("/api/login", json={"username": long_b, "password": "x"})
            self.assertEqual(r.status_code, 401)
            r = c.post("/api/login", json={"username": long_b, "password": "x"})
        self.assertEqual(r.status_code, 429, r.get_data(as_text=True))
        self.assertIn("已锁定", r.get_json()["error"], "同 128 前缀应合并计数并锁定")

    def test_p31_restore_fail_key_truncated_to_128(self):
        """验收：restore 的 200 长邮箱同样截断 [:128]（3+2=5 次锁定）。"""
        c = self.webapp.create_app().test_client()
        long_a, long_b = "r" * 200, "r" * 128 + "s" * 72
        with mock.patch.object(self.webapp, "_constant_time_dummy", lambda pwd: None), \
             mock.patch.object(self.webapp, "send_notification"):
            for _ in range(3):
                r = c.post("/api/me/restore", json={"email": long_a, "password": "x"})
                self.assertEqual(r.status_code, 400)
            r = c.post("/api/me/restore", json={"email": long_b, "password": "x"})
            self.assertEqual(r.status_code, 400)
            r = c.post("/api/me/restore", json={"email": long_b, "password": "x"})
        self.assertEqual(r.status_code, 429, r.get_data(as_text=True))
        self.assertIn("密码错误次数过多", r.get_json()["error"])

    def test_p31_verify_limits_store_trim_evicts_stale(self):
        """_verify_limits 写入路径 trim：>10000 条且全过期的 store 在配额判定后
        应被清到只剩新鲜条目。"""
        store = {}
        stale = time.time() - (self.webapp.VERIFY_WINDOW + self.webapp._IP_STORE_MAX_AGE + 10)
        for i in range(self.webapp._IP_STORE_LIMIT + 1):
            store[f"u{i}"] = (1, stale)
        allowed = self.webapp._verify_attempt_allowed(store, "fresh-user")
        self.assertTrue(allowed)
        self.assertLess(len(store), 5, "过期条目应被 trim 回收")
        self.assertIn("fresh-user", store)

    def test_p31_restore_fail_rate_trim_called_on_write_path(self):
        """_restore_fail_rate（唯一无 trim 的表）写入路径补 trim：restore 失败路径
        必须以 RESTORE_FAIL_WINDOW + _IP_STORE_MAX_AGE 调用 _ip_store_trim。"""
        c = self.webapp.create_app().test_client()
        real_trim = self.webapp._ip_store_trim
        seen = []

        def spy(store, max_age):
            seen.append(max_age)
            return real_trim(store, max_age)

        with mock.patch.object(self.webapp, "_ip_store_trim", side_effect=spy), \
             mock.patch.object(self.webapp, "_constant_time_dummy", lambda pwd: None), \
             mock.patch.object(self.webapp, "send_notification"):
            c.post("/api/me/restore", json={"email": "gone@qq.com", "password": "x"})
        want = self.webapp.RESTORE_FAIL_WINDOW + self.webapp._IP_STORE_MAX_AGE
        self.assertIn(want, seen,
                      f"restore 失败路径应以 max_age={want} trim _restore_fail_rate，实际 {seen}")

    # =====================================================================
    # 6. P3-6 _atomic_write 创建即 0600
    # =====================================================================
    def test_p36_atomic_write_creates_with_0600(self):
        """验收：_atomic_write 的 tmp 文件经 os.open(..., 0o600) 创建（跨平台断言
        mode 实参；与 account_crypto._write_key_to_env_file 口径一致）。"""
        real_open = os.open
        recorded = []

        def spy(path, flags, mode=0o666):
            recorded.append(mode)
            return real_open(path, flags, mode)

        target = os.path.join(self.tmp, "aw-mode.txt")
        with mock.patch.object(self.webapp.os, "open", side_effect=spy):
            self.webapp._atomic_write(target, "hello")
        self.assertEqual(recorded, [0o600], "创建即 0600，不得先 0644 再补 chmod")
        self.assertTrue(os.path.exists(target))

    def test_p36_atomic_write_roundtrip_and_no_tmp_left(self):
        """功能不回归：内容正确写盘、无 .tmp 残留、chmod_priv 路径不炸；
        POSIX 上落盘文件权限 0600。"""
        target = os.path.join(self.tmp, "aw-roundtrip.txt")
        self.webapp._atomic_write(target, "内容content", chmod_priv=True)
        with open(target, encoding="utf-8") as f:
            self.assertEqual(f.read(), "内容content")
        leftovers = [p for p in os.listdir(self.tmp)
                     if p.startswith("aw-roundtrip") and ".tmp" in p]
        self.assertEqual(leftovers, [], "os.replace 后不得残留 tmp 文件")
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o600)


class Batch18Knife2SummaryMailTest(unittest.TestCase):
    """scripts/signin.py 侧 M7：_flush_admin_mail_summary 兜底。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-knife2-signin-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n")
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_STATE_DIR"] = cls.tmp
        os.environ["YIBAN_DB_FILE"] = os.path.join(cls.tmp, "yiban.db")
        global db, signin
        import db
        import signin

    @classmethod
    def tearDownClass(cls):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        for k in ("YIBAN_ENV_FILE", "YIBAN_STATE_DIR", "YIBAN_DB_FILE"):
            os.environ.pop(k, None)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        signin._mail_summary.clear()

    def tearDown(self):
        signin._mail_summary.clear()

    def test_m7_mail_failure_falls_back_to_webhook_urgent_force(self):
        """验收：mock SMTP 不可用（send_admin_alert 返回 False）+ webhook 启用 →
        汇总告警降级 notify.send(urgent=True, force=True)，零成功类告警仍达 webhook。"""
        signin._collect_admin_mail("当日签到异常告警", "本次全量签到 0 个账号成功")
        with mock.patch.object(signin.db, "admin_mail_recipients",
                               return_value=["a@x.com"]), \
             mock.patch.object(signin.mailer, "send_admin_alert", return_value=False) as m_mail, \
             mock.patch.object(signin.notify, "send", return_value=True) as m_notify:
            signin._flush_admin_mail_summary()
        m_mail.assert_called_once()
        m_notify.assert_called_once()
        self.assertEqual(m_notify.call_args.args[0], "易班签到汇总")
        self.assertTrue(m_notify.call_args.kwargs.get("urgent"),
                        "webhook 兜底必须 urgent=True")
        self.assertTrue(m_notify.call_args.kwargs.get("force"),
                        "webhook 兜底必须 force=True（绕过节流与当日额度）")
        self.assertEqual(signin._mail_summary, [])

    def test_m7_mail_exception_falls_back_to_webhook(self):
        """mailer 抛异常同样降级（不外泄、不中断收尾）。"""
        signin._collect_admin_mail("易班签到耗时告警", "账号: 138****0001")
        with mock.patch.object(signin.db, "admin_mail_recipients",
                               return_value=["a@x.com"]), \
             mock.patch.object(signin.mailer, "send_admin_alert",
                               side_effect=RuntimeError("smtp boom")), \
             mock.patch.object(signin.notify, "send", return_value=True) as m_notify:
            signin._flush_admin_mail_summary()
        m_notify.assert_called_once()
        self.assertTrue(m_notify.call_args.kwargs.get("force"))
        self.assertEqual(signin._mail_summary, [])

    def test_m7_mail_success_skips_webhook_fallback(self):
        """反面：邮件发送成功 → 不走 webhook 兜底（不双发）。"""
        signin._collect_admin_mail("易班签到失败", "账号: 138****0001\n原因: A")
        with mock.patch.object(signin.db, "admin_mail_recipients",
                               return_value=["a@x.com"]), \
             mock.patch.object(signin.mailer, "send_admin_alert", return_value=True), \
             mock.patch.object(signin.notify, "send") as m_notify:
            signin._flush_admin_mail_summary()
        m_notify.assert_not_called()
        self.assertEqual(signin._mail_summary, [])

    def test_m7_empty_recipients_logs_warning_and_skips_mail(self):
        """收件人集为空：显式 warning 留痕、不调 mailer、不走 webhook、清空收集器。"""
        signin._collect_admin_mail("当日签到异常告警", "零成功且窗口外")
        with mock.patch.object(signin.db, "admin_mail_recipients", return_value=[]), \
             mock.patch.object(signin.mailer, "send_admin_alert") as m_mail, \
             mock.patch.object(signin.notify, "send") as m_notify, \
             self.assertLogs("yiban", level="WARNING") as logs:
            signin._flush_admin_mail_summary()
        m_mail.assert_not_called()
        m_notify.assert_not_called()
        self.assertEqual(signin._mail_summary, [])
        self.assertTrue(any("无可用收件人" in msg for msg in logs.output),
                        f"收件人为空必须 warning 留痕，实际 {logs.output}")


class Batch18Knife2NotifyLedgerTest(unittest.TestCase):
    """scripts/notify.py 侧 M8：login_fail 独立账本。"""

    @classmethod
    def setUpClass(cls):
        global notify
        import notify

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-knife2-notify-")
        self.env_file = os.path.join(self.tmp, "nope.env")
        os.environ["YIBAN_STATE_DIR"] = self.tmp
        os.environ["YIBAN_ENV_FILE"] = self.env_file
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
        os.environ["YIBAN_NOTIFY_TYPE"] = "serverchan"
        os.environ["YIBAN_NOTIFY_URL"] = SCT_KEY
        os.environ["YIBAN_NOTIFY_COOLDOWN"] = "0"  # 关节流，单测只看账本
        for k in ("YIBAN_LOGINFAIL_DAILY_MAX", "YIBAN_NOTIFY_DAILY_MAX",
                  "YIBAN_NOTIFY_URGENT_DAILY_MAX"):
            os.environ.pop(k, None)
        notify._throttle_ts.clear()
        notify._skip_logged.clear()
        self._reset_ledgers()

    def tearDown(self):
        for k in ("YIBAN_STATE_DIR", "YIBAN_ENV_FILE", "YIBAN_ACCOUNTS_KEY",
                  "YIBAN_NOTIFY_TYPE", "YIBAN_NOTIFY_URL", "YIBAN_NOTIFY_COOLDOWN",
                  "YIBAN_LOGINFAIL_DAILY_MAX", "YIBAN_NOTIFY_DAILY_MAX",
                  "YIBAN_NOTIFY_URGENT_DAILY_MAX"):
            os.environ.pop(k, None)
        self._reset_ledgers()
        notify._throttle_ts.clear()
        notify._skip_logged.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _reset_ledgers(self):
        for led in (notify._general_daily, notify._urgent_daily,
                    notify._loginfail_daily):
            led["state"].update({"date": "", "count": 0})
            led["notice"].update({"pending": False, "notified": False, "warned": False})
        # 磁盘是唯一事实源（_with_ledger_locked 每次读盘合并）：只重置内存不删盘，
        # 上一子步骤的计数会跨步骤串账——重置必须连磁盘账本一起清
        with contextlib.suppress(OSError):
            os.remove(self._ledger_file())

    def _ledger_file(self):
        return os.path.join(self.tmp, "notify-ledger.json")

    def _read_disk(self):
        with open(self._ledger_file(), encoding="utf-8") as f:
            return json.load(f)

    def test_m8_loginfail_exhausts_independently(self):
        """验收：YIBAN_LOGINFAIL_DAILY_MAX=1 → login_fail 账第 2 条被拒后，
        主 urgent 账与 general 账仍可发（互不挤占）。"""
        os.environ["YIBAN_LOGINFAIL_DAILY_MAX"] = "1"
        with mock.patch.object(notify, "_send_serverchan", return_value=True) as send:
            self.assertTrue(notify.send("登录失败告警", "1", ledger="login_fail"))
            self.assertFalse(notify.send("登录失败告警B", "2", ledger="login_fail"),
                             "login_fail 账打满后应停手")
            self.assertTrue(notify.send("审计链异常", "3", urgent=True),
                            "login_fail 打满后主 urgent 账仍可发")
            self.assertTrue(notify.send("用户日常改密", "4"),
                            "login_fail 打满后 general 账仍可发")
        self.assertEqual(send.call_count, 3)

    def test_m8_loginfail_limit_reading_env_var_and_dotenv(self):
        """读取口径：环境变量优先于 .env；0=不限；非法值回退默认 3。"""
        with mock.patch.object(notify, "_send_serverchan", return_value=True):
            # 1) .env 键（无环境变量）：上限 2
            with open(self.env_file, "w", encoding="utf-8") as f:
                f.write("YIBAN_LOGINFAIL_DAILY_MAX=2\n")
            for i in range(2):
                self.assertTrue(notify.send(f"lf-a{i}", "x", ledger="login_fail"))
            self.assertFalse(notify.send("lf-a2", "x", ledger="login_fail"))
            self._reset_ledgers()
            # 2) 环境变量优先：env=3 覆盖 .env=1
            os.environ["YIBAN_LOGINFAIL_DAILY_MAX"] = "3"
            with open(self.env_file, "w", encoding="utf-8") as f:
                f.write("YIBAN_LOGINFAIL_DAILY_MAX=1\n")
            for i in range(3):
                self.assertTrue(notify.send(f"lf-b{i}", "x", ledger="login_fail"))
            self.assertFalse(notify.send("lf-b3", "x", ledger="login_fail"))
            self._reset_ledgers()
            # 3) 0 = 不限
            os.environ["YIBAN_LOGINFAIL_DAILY_MAX"] = "0"
            for i in range(5):
                self.assertTrue(notify.send(f"lf-c{i}", "x", ledger="login_fail"))
            self._reset_ledgers()
            # 4) 非法值回退默认 3
            os.environ["YIBAN_LOGINFAIL_DAILY_MAX"] = "abc"
            for i in range(3):
                self.assertTrue(notify.send(f"lf-d{i}", "x", ledger="login_fail"))
            self.assertFalse(notify.send("lf-d3", "x", ledger="login_fail"))

    def test_m8_loginfail_ledger_persists_to_disk(self):
        """login_fail 账随 P2-3 机制落盘（跨进程共享额度），结构完整。"""
        with mock.patch.object(notify, "_send_serverchan", return_value=True):
            self.assertTrue(notify.send("登录失败告警", "x", ledger="login_fail"))
        self.assertTrue(os.path.exists(self._ledger_file()))
        disk = self._read_disk()
        self.assertEqual(disk["login_fail"]["count"], 1)
        self.assertEqual(disk["login_fail"]["date"], notify._daily_today())
        # 既有两本账不受影响
        self.assertIn("general", disk)
        self.assertIn("urgent", disk)

    def test_m8_default_behavior_unchanged_without_ledger_arg(self):
        """反面（向后兼容）：不传 ledger → 现行行为（urgent→urgent 账 / 其余→general 账）。"""
        os.environ["YIBAN_NOTIFY_DAILY_MAX"] = "1"
        os.environ["YIBAN_NOTIFY_URGENT_DAILY_MAX"] = "1"
        with mock.patch.object(notify, "_send_serverchan", return_value=True):
            self.assertTrue(notify.send("g1", "x"))
            self.assertFalse(notify.send("g2", "x"), "general 账上限 1 → 第二条拒")
            self.assertTrue(notify.send("u1", "x", urgent=True))
            self.assertFalse(notify.send("u2", "x", urgent=True), "urgent 账上限 1 → 第二条拒")
        disk = self._read_disk()
        self.assertEqual(disk["general"]["count"], 1)
        self.assertEqual(disk["urgent"]["count"], 1)
        self.assertEqual(disk["login_fail"]["count"], 0,
                         "不传 ledger 不得在 login_fail 账上记账")


if __name__ == "__main__":
    unittest.main(verbosity=2)
