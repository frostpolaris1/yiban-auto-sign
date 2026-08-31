# -*- coding: utf-8 -*-
"""对抗性审查修复批次回归测试（v0.24.3，2026-08-27）。

覆盖本批次八组修复：
- 容器调度器：.env 白名单注入子进程环境（P1-1）；分钟级到点闩锁语义常量齐备；
- 探针：状态文件 BOM 容错 + M12 锁写（P2-9）；main() 中 --probe 先于零账号守卫
  （空账号部署静默 rc=0，签到模式仍 rc=1，防顺序回退）；
- 注册/添加账号即时验证：资格预筛前置——注定失败的提交不发起网络验证（P1-2）；
  每用户验证尝试配额（P1-2）；
- 邮件：用户失败提醒每账号每日上限三入口统一（P2-1）；管理员汇总条数封顶截断
  （P2-2）；
- Web 会话绝对过期：超限失效 / 旧会话就地补记 / YIBAN_SESSION_ABS_DAYS 越界钳制
  （P2-5）。

全程本地（临时 sqlite + Flask test client + mock），无真实网络请求。
用法（项目根目录）：py -m pytest tests/test_audit_fixes_0827.py -v
"""
import contextlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "scripts"))
sys.path.insert(0, os.path.join(BASE, "web"))

TEST_KEY = "c" * 64
ADMIN_USER = "root@test.local"
ADMIN_PASS = "TestPass1234!"
USER_PASS = "secret1"


def _load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    with contextlib.suppress(Exception):
        spec.loader.exec_module(mod)
    return mod


class SchedulerEnvMergeTest(unittest.TestCase):
    """P1-1：容器调度器把 .env 的 YIBAN_* 注入子进程环境。"""

    @classmethod
    def setUpClass(cls):
        cls.sched = _load_module(
            os.path.join(BASE, "docker", "scheduler.py"), "yiban_sched_audit")

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-sched-env-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_env(self, text):
        p = os.path.join(self.tmp, ".env")
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        return p

    def test_yiban_keys_override_ambient_and_skip_others(self):
        env_file = self._write_env(
            "# 注释行\n"
            "\n"
            "YIBAN_PROBE_ENABLE=1\n"
            "YIBAN_PROBE_TIME=03:30\n"
            "YIBAN_MAIL_ENABLE = 0 \n"      # 等号两侧空白容忍
            "NON_YIBAN_KEY=evil\n"           # 非 YIBAN_ 前缀丢弃
            "broken line no equals\n"        # 无等号丢弃
            "export YIBAN_PROXY=http://x\n"  # export 前缀键名不合 → 丢弃（对齐 run.sh）
        )
        base = {"PATH": "/usr/bin", "TZ": "UTC",
                "YIBAN_PROBE_TIME": "20:00", "HOME": "/root"}
        merged = self.sched.build_child_env(env_file=env_file, base=base)
        self.assertEqual(merged["YIBAN_PROBE_ENABLE"], "1")
        self.assertEqual(merged["YIBAN_PROBE_TIME"], "03:30", "文件值应覆盖外部环境")
        self.assertEqual(merged["YIBAN_MAIL_ENABLE"], "0")
        self.assertNotIn("NON_YIBAN_KEY", merged)
        self.assertEqual(merged["PATH"], "/usr/bin", "非白名单键不受影响")
        self.assertEqual(merged["HOME"], "/root")

    def test_missing_env_file_degrades_to_inherit(self):
        merged = self.sched.build_child_env(
            env_file=os.path.join(self.tmp, "nope.env"),
            base={"PATH": "/bin", "YIBAN_SIGN_MODE": "random"},
        )
        self.assertEqual(merged["YIBAN_SIGN_MODE"], "random")

    def test_latch_constants_present(self):
        # P2-10 分钟级闩锁重写的时间点常量保持既有语义
        # 批次15 P3-1：探针由固定时刻（PROBE_AT=(23,55)）改为周期尝试
        # （PROBE_TRY_SECONDS，64a273e），原断言随之更新，避免 CI 门禁恢复后红
        self.assertEqual(self.sched.FIRST, (6, 31))
        self.assertEqual(self.sched.SECOND, (7, 10))
        self.assertEqual(self.sched.PROBE_TRY_SECONDS, 600)


class ProbeStateBomLockTest(unittest.TestCase):
    """P2-9：探针状态文件 utf-8-sig 读容错 + 锁内读改写。"""

    @classmethod
    def setUpClass(cls):
        global signin
        import signin

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-probe-state-")
        self._old_state_dir = os.environ.get("YIBAN_STATE_DIR")
        os.environ["YIBAN_STATE_DIR"] = self.tmp
        self.state_path = os.path.join(self.tmp, "probe-state.json")

    def tearDown(self):
        if self._old_state_dir is None:
            os.environ.pop("YIBAN_STATE_DIR", None)
        else:
            os.environ["YIBAN_STATE_DIR"] = self._old_state_dir
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_bom_file_still_parsed(self):
        with open(self.state_path, "wb") as f:
            f.write(b"\xef\xbb\xbf" + json.dumps({"last_run": "2026-08-27"}).encode())
        data = signin._read_probe_state()
        self.assertEqual(data.get("last_run"), "2026-08-27",
                         "带 BOM 的手工编辑文件不应被判为空而丢失当日去重")

    def test_update_preserves_existing_keys_under_lock(self):
        with open(self.state_path, "w", encoding="utf-8") as f:
            json.dump({"keep": 1}, f)
        signin._update_probe_state_run("2026-09-01")
        with open(self.state_path, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data, {"keep": 1, "last_run": "2026-09-01"})


class ProbeMainZeroAccountsTest(unittest.TestCase):
    """main() 顺序修复：--probe 在零账号部署上静默成功；签到模式维持 rc=1 报缺账号。"""

    @classmethod
    def setUpClass(cls):
        cls.base = {
            k: v for k, v in os.environ.items()
            if not k.startswith("YIBAN_")
        }
        cls.base.update({"SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
                         "COMSPEC": os.environ.get("COMSPEC", ""),
                         "PYTHONIOENCODING": "utf-8"})

    def _run_signin(self, extra_args, probe_enabled=False):
        tmp = tempfile.mkdtemp(prefix="yiban-zeroacc-")
        try:
            for sub in ("state", "logs"):
                os.makedirs(os.path.join(tmp, sub), exist_ok=True)
            with open(os.path.join(tmp, ".env"), "w", encoding="utf-8") as f:
                f.write("")  # 空 .env（零账号）
            env = dict(self.base)
            env.update({
                "YIBAN_ENV_FILE": os.path.join(tmp, ".env"),
                "YIBAN_DB_FILE": os.path.join(tmp, "yiban.db"),
                "YIBAN_STATE_DIR": os.path.join(tmp, "state"),
                "YIBAN_LOG_FILE": os.path.join(tmp, "logs", "sign.log"),
            })
            if probe_enabled:
                env["YIBAN_PROBE_ENABLE"] = "1"
            proc = subprocess.run(
                [sys.executable, os.path.join(BASE, "scripts", "signin.py")] + extra_args,
                capture_output=True, text=True, timeout=120,
                cwd=tmp, env=env)
            logs = ""
            logdir = os.path.join(tmp, "logs")
            for name in os.listdir(logdir) if os.path.isdir(logdir) else []:
                with open(os.path.join(logdir, name), encoding="utf-8",
                          errors="replace") as f:
                    logs += f.read()
            return proc.returncode, logs
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_probe_with_zero_accounts_silent_success(self):
        rc, logs = self._run_signin(["--probe"], probe_enabled=True)
        self.assertEqual(rc, 0, "零账号+探针开启应为静默成功退出")
        self.assertNotIn("未配置任何账号", logs, "探针分支不应落到零账号守卫的 ERROR 文案")

    def test_sign_mode_zero_accounts_keeps_rc1_hint(self):
        rc, logs = self._run_signin([])
        self.assertEqual(rc, 1, "正常签到模式零账号应维持报错退出")
        self.assertIn("未配置任何账号", logs)


class VerifyPrescreenWebTest(unittest.TestCase):
    """P1-2（web）：预筛前置让注定失败的提交不再发起网络验证；配额节流生效。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-prescreen-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.accounts_file = os.path.join(cls.tmp, "accounts.json")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(
                f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
                f"YIBAN_ADMIN_USER={ADMIN_USER}\nYIBAN_ADMIN_PASSWORD={ADMIN_PASS}\n"
                f"YIBAN_ACCOUNT_VERIFY=1\n"
            )
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_DB_FILE"] = cls.db_file
        os.environ["YIBAN_STATE_DIR"] = cls.tmp
        os.environ["YIBAN_DISABLE_PURGE_LOOP"] = "1"
        global db, signin
        import db
        import signin
        cls.db = db
        cls.signin = signin
        cls.webapp = _load_module(os.path.join(BASE, "web", "app.py"), "webapp_audit")

    @classmethod
    def tearDownClass(cls):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        for suffix in ("", "-wal", "-shm"):
            p = self.db_file + suffix
            if os.path.exists(p):
                os.remove(p)
        db.init_db(self.db_file, env_file=self.env_file)
        # 普通注册用户 + 关闭 mail 告警噪音
        db.create_user("u1@test.local",
                       self.webapp.generate_password_hash(USER_PASS))

    def _login_user(self, c):
        r = c.post("/api/login", json={"username": "u1@test.local", "password": USER_PASS})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return c.get("/api/me").get_json()["csrf_token"]

    def _post_account(self, c, csrf, idx, phone):
        return c.post("/api/my-accounts",
                      json={"name": f"n{idx}", "phone": phone,
                            "password": "AbcdEfghij", "phone_model": "",
                            "phone_code": ""},
                      headers={"X-CSRF-Token": csrf})

    def test_perscreen_reject_skips_network_verify(self):
        app = self.webapp.create_app()
        c = app.test_client()
        token = self._login_user(c)
        seeded = {"name": "occupied", "phone": "13800138001", "password": "p",
                  "phone_model": "", "phone_code": "", "status": "active",
                  "owner": "someone@test.local"}
        self.db.add_account(seeded)
        with mock.patch.object(signin, "verify_account",
                               side_effect=AssertionError("网络验证不应发生")) as m:
            r = self._post_account(c, token, 0, "13800138001")
            self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
            self.assertIn("已被使用", r.get_json().get("error", ""))
            m.assert_not_called()

    def test_verify_throttle_per_user(self):
        app = self.webapp.create_app()
        c = app.test_client()
        token = self._login_user(c)
        codes = []
        with mock.patch.object(signin, "verify_account",
                               return_value=(False, "模拟验证失败")) as m:
            for i in range(self.webapp.VERIFY_MAX + 2):
                r = self._post_account(c, token, i, f"139000000{i:02d}")
                codes.append(r.status_code)
        self.assertEqual(codes[:self.webapp.VERIFY_MAX],
                         [400] * self.webapp.VERIFY_MAX,
                         "前 VERIFY_MAX 次应走到网络验证并失败打回")
        self.assertEqual(codes[self.webapp.VERIFY_MAX], 429, "超出配额应 429")
        self.assertEqual(codes[-1], 429)
        self.assertLessEqual(m.call_count, self.webapp.VERIFY_MAX,
                             "429 次不得再发起真实网络验证")


class UserFailMailDailyCapTest(unittest.TestCase):
    """P2-1：B 线用户失败提醒每账号每日上限跨入口统一。"""

    @classmethod
    def setUpClass(cls):
        global signin, db
        import signin, db

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-mailcap-")
        self._old = os.environ.get("YIBAN_STATE_DIR")
        os.environ["YIBAN_STATE_DIR"] = self.tmp
        self._old_cap = signin.USER_FAIL_MAIL_DAILY_CAP
        signin.USER_FAIL_MAIL_DAILY_CAP = 1

    def tearDown(self):
        if self._old is None:
            os.environ.pop("YIBAN_STATE_DIR", None)
        else:
            os.environ["YIBAN_STATE_DIR"] = self._old
        signin.USER_FAIL_MAIL_DAILY_CAP = self._old_cap
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _seed_user(self):
        tmpdb = os.path.join(self.tmp, "yiban.db")
        os.environ["YIBAN_DB_FILE"] = tmpdb
        os.environ["YIBAN_ENV_FILE"] = os.path.join(self.tmp, ".env")
        with open(os.environ["YIBAN_ENV_FILE"], "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n")
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        for suffix in ("", "-wal", "-shm"):
            p = tmpdb + suffix
            if os.path.exists(p):
                os.remove(p)
        db.init_db(tmpdb, env_file=os.environ["YIBAN_ENV_FILE"])
        db.create_user("owner@test.local", "x" * 20, role="user")

    def test_same_phone_second_mail_suppressed(self):
        self._seed_user()
        sent = []
        with mock.patch.object(sys.modules["mailer"], "send_user",
                               side_effect=lambda to, s, t: sent.append(to)):
            signin.send_user_fail_mail("owner@test.local", "13800138001", "失败A")
            signin.send_user_fail_mail("owner@test.local", "13800138001", "失败B（手动重跑）")
        self.assertEqual(len(sent), 1, "同账号当日第二封应被每日限频抑制")

    def test_different_phone_not_affected_and_zero_means_unlimited(self):
        self._seed_user()
        sent = []
        with mock.patch.object(sys.modules["mailer"], "send_user",
                               side_effect=lambda to, s, t: sent.append(to)):
            signin.send_user_fail_mail("owner@test.local", "13800138001", "a")
            signin.send_user_fail_mail("owner@test.local", "13900139002", "b")
            self.assertEqual(len(sent), 2, "不同账号互不挤占额度")
            signin.USER_FAIL_MAIL_DAILY_CAP = 0
            signin.send_user_fail_mail("owner@test.local", "13800138001", "c")
            self.assertEqual(len(sent), 3, "cap<=0 恢复不限频旧行为")


class MailSummaryTruncationTest(unittest.TestCase):
    """P2-2：管理员汇总邮件条数封顶与尾部截断说明。"""

    @classmethod
    def setUpClass(cls):
        global signin
        import signin

    def setUp(self):
        signin._mail_summary.clear()
        self._old_max = signin.MAIL_SUMMARY_MAX_ENTRIES
        self._old_chars = signin.MAIL_SUMMARY_MAX_CHARS
        self.sent = []

    def tearDown(self):
        signin.MAIL_SUMMARY_MAX_ENTRIES = self._old_max
        signin.MAIL_SUMMARY_MAX_CHARS = self._old_chars
        signin._mail_summary.clear()

    def test_entries_capped_with_notice(self):
        signin.MAIL_SUMMARY_MAX_ENTRIES = 3
        for i in range(5):
            signin._collect_admin_mail("易班签到失败", f"条目{i}")
        with mock.patch.object(sys.modules["mailer"], "send_admin_alert",
                               return_value=True,
                               side_effect=lambda s, t, to=None: self.sent.append(t)), \
             mock.patch.object(sys.modules["db"], "admin_mail_recipients",
                               return_value=["a@test.local"]):
            signin._flush_admin_mail_summary()
        self.assertEqual(len(self.sent), 1)
        body = self.sent[0]
        self.assertIn("共 5 条异常/预警", body, "头部计数应反映收集总量")
        self.assertIn("其余 2 条已截断", body)
        self.assertNotIn("条目3\n", body.replace("条目3\"", "\""))
        self.assertIn("条目2", body, "保留前 N 条明细")
        self.assertEqual(len(signin._mail_summary), 0, "发送后清空收集器")


class ProbeWordingTest(unittest.TestCase):
    """P3：探针邮件措辞解耦——汇总带阶段标签、用户侧不再误报「今日签到失败」。"""

    @classmethod
    def setUpClass(cls):
        global signin
        import signin

    def setUp(self):
        signin._mail_summary.clear()
        self.sent = []

    def tearDown(self):
        signin._mail_summary.clear()

    def test_flush_phase_label(self):
        with mock.patch.object(sys.modules["mailer"], "send_admin_alert",
                               return_value=True,
                               side_effect=lambda s, t, to=None: self.sent.append(t)), \
             mock.patch.object(sys.modules["db"], "admin_mail_recipients",
                               return_value=["a@test.local"]):
            signin._collect_admin_mail("健康探测预警", "账号: 138****0001\n原因: 密码错误")
            signin._flush_admin_mail_summary(phase="健康探测")
            self.assertTrue(self.sent[0].startswith("易班健康探测已完成"),
                            self.sent[0][:40])
            self.assertNotIn("签到任务已结束", self.sent[0])
            # 缺省沿用原签到文案（定时批次行为不变）
            signin._collect_admin_mail("易班签到失败", "条目")
            signin._flush_admin_mail_summary()
            self.assertIn("易班签到任务已结束", self.sent[1])

    def test_probe_scenario_mail_wording(self):
        sent = []
        with mock.patch.object(signin.mailer, "send_user",
                               side_effect=lambda to, s, t: sent.append((s, t))):
            signin.send_user_fail_mail("owner@test.local", "13800000000",
                                       "图形验证墙", scenario="probe")
        subject, text = sent[0]
        self.assertEqual(subject, "易班账号健康预警")
        self.assertNotIn("今日签到失败", text)
        self.assertIn("138****0000", text)
        self.assertNotIn("13800000000", text)


class ProbeEventsOnTest(unittest.TestCase):
    """P3：探针结构化事件可按日查询（此前 write-only）。"""

    @classmethod
    def setUpClass(cls):
        global db
        import db

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-pev-")
        self.db_file = os.path.join(self.tmp, "yiban.db")
        self.env_file = os.path.join(self.tmp, ".env")
        with open(self.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n")
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        for suffix in ("", "-wal", "-shm"):
            p = self.db_file + suffix
            if os.path.exists(p):
                os.remove(p)
        db.init_db(self.db_file, env_file=self.env_file)

    def tearDown(self):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_only_probe_stage_returned_for_date(self):
        db.add_sign_event("2026-08-27 09:00:00", "13800138001",
                          "success", "", stage="")
        db.add_sign_event("2026-08-27 23:55:30", "13900139002",
                          "failed", "密码错误", stage="probe")
        rows = db.probe_events_on("2026-08-27")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["phone"], "13900139002")
        self.assertEqual(rows[0]["status"], "failed")
        self.assertEqual(db.probe_events_on("2026-08-26"), [])


class MailerPortFallbackTest(unittest.TestCase):
    """P3：SMTP_PORT 非法/缺省时 get_config 显式暴露 port_fallback。"""

    @classmethod
    def setUpClass(cls):
        global mailer_mod
        import mailer as mailer_mod

    def _cfg_with_port(self, raw):
        env = dict(os.environ)
        for k in list(os.environ):
            if k.startswith("YIBAN_MAIL_"):
                os.environ.pop(k, None)
        os.environ["YIBAN_MAIL_ENABLE"] = "1"
        os.environ["YIBAN_MAIL_USER"] = "sender@qq.com"
        os.environ["YIBAN_MAIL_PASS"] = "secret"
        if raw is not None:
            os.environ["YIBAN_MAIL_SMTP_PORT"] = raw
        try:
            return mailer_mod.get_config()
        finally:
            os.environ.clear()
            os.environ.update(env)

    def test_invalid_and_missing_marked_fallback(self):
        self.assertEqual(self._cfg_with_port("abc")["port"], 465)
        self.assertTrue(self._cfg_with_port("abc")["port_fallback"])
        self.assertTrue(self._cfg_with_port("")["port_fallback"])
        cfg = self._cfg_with_port("587")
        self.assertEqual(cfg["port"], 587)
        self.assertFalse(cfg["port_fallback"])


class SessionAbsoluteTTLTest(unittest.TestCase):
    """P2-5：会话绝对过期；旧会话就地补记；配置越界钳制。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-absttl-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(
                f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
                f"YIBAN_ADMIN_USER={ADMIN_USER}\nYIBAN_ADMIN_PASSWORD={ADMIN_PASS}\n"
                f"YIBAN_SESSION_ABS_DAYS=99\n"   # 故意越界 → 应回退默认 7
            )
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_DB_FILE"] = cls.db_file
        os.environ["YIBAN_STATE_DIR"] = cls.tmp
        os.environ["YIBAN_DISABLE_PURGE_LOOP"] = "1"
        global db
        import db
        cls.db = db
        cls.webapp = _load_module(os.path.join(BASE, "web", "app.py"), "webapp_abs_ttl")

    @classmethod
    def tearDownClass(cls):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        for suffix in ("", "-wal", "-shm"):
            p = self.db_file + suffix
            if os.path.exists(p):
                os.remove(p)
        db.init_db(self.db_file, env_file=self.env_file)

    def _login_admin(self, c):
        r = c.post("/api/login", json={"username": ADMIN_USER, "password": ADMIN_PASS})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

    def test_config_clamped_and_default(self):
        c = self.webapp.create_app().test_client()
        self.assertEqual(self.webapp.SESSION_ABS_TTL_SECONDS, 7 * 86400,
                         "越界 99 天应回退默认 7 天")
        self._login_admin(c)
        r = c.get("/api/me")
        self.assertEqual(r.status_code, 200)

    def test_expired_session_forced_logout(self):
        c = self.webapp.create_app().test_client()
        self._login_admin(c)
        old = int(__import__("time").time()) - 8 * 86400  # > 默认 7 天
        with c.session_transaction() as sess:
            sess["login_ts"] = old
        r = c.get("/api/me")
        self.assertEqual(r.status_code, 401, "绝对过期会话应强制登出")

    def test_legacy_session_without_ts_is_grandfathered(self):
        c = self.webapp.create_app().test_client()
        self._login_admin(c)
        with c.session_transaction() as sess:
            sess.pop("login_ts", None)
        r = c.get("/api/me")
        self.assertEqual(r.status_code, 200, "存量无时间戳会话升级日不应被踢")
        with c.session_transaction() as sess:
            self.assertTrue(sess.get("login_ts"), "补记 login_ts 后续期基准生成")


if __name__ == "__main__":
    unittest.main()
