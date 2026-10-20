# -*- coding: utf-8 -*-
"""web 面加固批的**不变式**用例（工单 ba-p06-* / ba-p07-* / cgx6）。

标签：E · Web：认证/权限/API
覆盖（每条对应一个工单，断言的是"任何时刻都不该被违反"的性质）：
  - ba-p06-02：个人域/注册审计的 `target` 不得是明文邮箱（遮罩后才入链）；
  - ba-p06-03：日志导出必须与展示层同口径回显丢弃行数（缺行可见）；
  - ba-p06-04：登录喷洒分级只数**窗口内**的失败键（历史噪声不得触发 urgent）；
  - ba-p07-03：`_ip_store_trim` 在新键喷洒下必须**收口到上限内**（数量淘汰）；
  - ba-p07-05：`_atomic_write` 任何失败路径都不得残留 `.env.tmp*`；
  - ba-p07-06/07：整数配置与两个验证开关共用 overlay 口径（`.env` 优先、进程环境补缺）；
  - ba-p07-09：`_today_has_logs` 整读判定（2MB 尾部无 yiban 行时仍须判真）；
  - ba-p07-11：`_effective_role` 每个请求只读一次 `.env`；
  - ba-p07-04：清理歧义行的原子写失败不得截断原文件；
  - cgx6：同一手机号不得被并发持有（两道外呼路径共用一把权）。
依赖：纯本地 Flask test client / 临时文件；不联网、不访问真实易班接口。
"""
import contextlib
import importlib.util
import io
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "scripts"))

TEST_KEY = "a" * 64
ADMIN_EMAIL = "admin@test.local"
ADMIN_PASS = "TestPass1234!"
USER_EMAIL = "user@example.com"
USER_PASS = "UserPass123!"
PHONE = "13800138000"


def _load_webapp(name):
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(BASE, "web", "app.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class _WebFixture(unittest.TestCase):
    """临时 .env / DB / 日志目录 + 以别名加载的 web.app。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-webhard-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.log_dir = os.path.join(cls.tmp, "logs")
        os.makedirs(cls.log_dir, exist_ok=True)
        with io.open(cls.env_file, "w", encoding="utf-8", newline="\n") as f:
            f.write(
                f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
                f"YIBAN_ADMIN_USER={ADMIN_EMAIL}\n"
                f"YIBAN_ADMIN_PASSWORD={ADMIN_PASS}\n"
            )
        cls._old_env = {k: os.environ.get(k) for k in (
            "YIBAN_ACCOUNTS_KEY", "YIBAN_ENV_FILE", "YIBAN_DB_FILE",
            "YIBAN_STATE_DIR", "YIBAN_LOG_FILE", "YIBAN_DISABLE_PURGE_LOOP",
            "YIBAN_TRUST_FORWARDED_HEADERS")}
        os.environ.update({
            "YIBAN_ACCOUNTS_KEY": TEST_KEY,
            "YIBAN_ENV_FILE": cls.env_file,
            "YIBAN_DB_FILE": cls.db_file,
            "YIBAN_STATE_DIR": cls.tmp,
            "YIBAN_LOG_FILE": os.path.join(cls.log_dir, "sign.log"),
            "YIBAN_DISABLE_PURGE_LOOP": "1",
        })
        os.environ.pop("YIBAN_TRUST_FORWARDED_HEADERS", None)
        cls.webapp = _load_webapp("webapp_hardening")
        import db
        cls.db = db
        db.init_db(cls.db_file, env_file=cls.env_file)

    @classmethod
    def tearDownClass(cls):
        if cls.db._conn is not None:
            with contextlib.suppress(Exception):
                cls.db._conn.close()
            cls.db._conn = None
        for k, v in cls._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self._reset_db()
        with io.open(self.env_file, "w", encoding="utf-8", newline="\n") as f:
            f.write(
                f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
                f"YIBAN_ADMIN_USER={ADMIN_EMAIL}\n"
                f"YIBAN_ADMIN_PASSWORD={ADMIN_PASS}\n"
            )

    def _reset_db(self):
        if self.db._conn is not None:
            with contextlib.suppress(Exception):
                self.db._conn.close()
            self.db._conn = None
        for suffix in ("", "-wal", "-shm"):
            p = self.db_file + suffix
            if os.path.exists(p):
                os.remove(p)
        self.db.init_db(self.db_file, env_file=self.env_file)

    def _login_admin(self, client):
        r = client.post("/api/login", json={"username": ADMIN_EMAIL,
                                            "password": ADMIN_PASS})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return client.get("/api/me").get_json()["csrf_token"]

    def _register_user(self):
        from werkzeug.security import generate_password_hash
        self.db.create_user(USER_EMAIL, generate_password_hash(USER_PASS))
        return USER_EMAIL


class AuditTargetMaskedTest(_WebFixture):
    """ba-p06-02：审计 target 不得出现明文邮箱。"""

    def _assert_masked_target(self, action, email):
        from yiban.adapters.persistence import audit_chain
        rows, _total = audit_chain.read_audit_rows(action=action, limit=20)
        self.assertTrue(rows, f"{action} 未落审计")
        for row in rows:
            target = str(row["target"] or "")
            self.assertNotEqual(target, email, f"{action} 的 target 是明文邮箱")
            self.assertIn("***", target, f"{action} 的 target 未遮罩：{target!r}")

    def test_mail_notify_target_is_masked(self):
        c = self.webapp.create_app().test_client()
        self._register_user()
        r = c.post("/api/login", json={"username": USER_EMAIL, "password": USER_PASS})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        token = c.get("/api/me").get_json()["csrf_token"]
        r = c.put("/api/my-mail-notify", json={"enabled": False},
                  headers={"X-CSRF-Token": token})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self._assert_masked_target("mail_notify", USER_EMAIL)

    def test_register_target_is_masked(self):
        c = self.webapp.create_app().test_client()
        r = c.post("/api/register", json={
            "email": "newbie@qq.com", "password": "NewPass123!",
            "agree": True,
        })
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self._assert_masked_target("user_register", "newbie@qq.com")


class LogsExportDroppedTest(_WebFixture):
    """ba-p06-03：导出必须回显与展示层同一份丢弃行数。"""

    def test_export_reports_dropped_lines(self):
        from yiban.core import clock
        day = clock.now().strftime("%Y-%m-%d")
        path = os.path.join(self.log_dir, f"sign-{day}.log")
        with io.open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(f"[{day} 06:40:00] [INFO] yiban: 签到开始\n")
            # 裸 NEL 把一条逻辑行劈成两半：后半行解析不出 ⇒ dropped +1
            f.write(f"[{day} 06:40:01] [INFO] yiban: 前半\x85后半\n")
        app = self.webapp.create_app()
        c = app.test_client()
        self._login_admin(c)
        r = c.get(f"/api/logs/export?date={day}")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        view = c.get(f"/api/logs?date={day}&level=all").get_json()
        self.assertGreaterEqual(view["dropped_lines"], 1,
                                "NEL 撑开的残行必须被计数")
        self.assertEqual(r.headers.get("X-Yiban-Dropped-Lines"),
                         str(view["dropped_lines"]),
                         "导出与展示必须同一份丢弃计数")


class LoginSprayWindowTest(_WebFixture):
    """ba-p06-04：历史失败键不得把本次告警顶成 urgent。"""

    def test_old_failures_do_not_count_as_spray(self):
        from web.routes import auth as auth_mod
        from web.services import limits as limits_mod
        app = self.webapp.create_app()
        c = app.test_client()
        store = app.extensions["yiban_login_fails"]
        ip = "127.0.0.1"
        now = time.time()
        # 同一 IP 历史上试过 3 个用户名，但都早于窗口（> LOGIN_LOCK_SECONDS 前）
        old = now - self.webapp._security.LOGIN_LOCK_SECONDS - 120
        for name in ("old1@example.com", "old2@example.com", "old3@example.com"):
            store[(ip, name)] = (1, 0, old)
        alerts = []
        with mock.patch.object(self.webapp._security, "verify_admin", return_value=False), \
                mock.patch.object(auth_mod, "check_password_hash", return_value=False), \
                mock.patch.object(self.webapp._security, "check_password_hash",
                                  return_value=False), \
                mock.patch.object(self.webapp._security, "_constant_time_dummy",
                                  lambda p: None), \
                mock.patch.object(auth_mod._notify_mail, "send_notification",
                                  side_effect=lambda t, body, urgent=False, **kw:
                                  alerts.append((body, urgent))):
            for _ in range(limits_mod.LOGIN_FAIL_NOTIFY):
                r = c.post("/api/login", json={"username": USER_EMAIL,
                                               "password": "x"})
                self.assertIn(r.status_code, (401, 429))
        self.assertTrue(alerts, "到阈值必须发登录失败告警")
        body, urgent = alerts[0]
        self.assertFalse(urgent, "历史键不得把本次告警判成喷洒（urgent）")
        self.assertIn("1 个", str(getattr(body, "fields", body)),
                      "窗口内只有当前用户名，计数应为 1")


class IpStoreTrimBoundTest(unittest.TestCase):
    """ba-p07-03：新键喷洒时长度必须收口到上限内。"""

    def test_trim_evicts_oldest_when_nothing_is_stale(self):
        from web import security as sec
        now = time.time()
        store = {f"k{i}": (1, now + i) for i in range(6)}
        with mock.patch.object(sec, "_IP_STORE_LIMIT", 5):
            sec._ip_store_trim(store, 3600)
        self.assertEqual(len(store), 5, "全未满龄时也必须按数量淘汰到上限内")
        self.assertIn("k5", store, "最老的 k0 应被淘汰")
        self.assertNotIn("k0", store)


class AtomicWriteCleanupTest(unittest.TestCase):
    """ba-p07-05：任何失败路径都不得残留 .env.tmp*。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-atomic-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path = os.path.join(self.tmp, "target.env")

    def _leftovers(self):
        return [p for p in os.listdir(self.tmp) if ".tmp" in p]

    def test_write_stage_failure_removes_tmp(self):
        from web import security as sec
        with mock.patch.object(sec.os, "fsync", side_effect=OSError("ENOSPC")), \
                self.assertRaises(OSError):
            sec._atomic_write(self.path, "A=1\n", chmod_priv=True)
        self.assertEqual(self._leftovers(), [], "写阶段失败必须删临时文件")

    def test_non_permission_replace_failure_removes_tmp(self):
        from web import security as sec
        with mock.patch.object(sec.os, "replace", side_effect=OSError("EXDEV")), \
                self.assertRaises(OSError):
            sec._atomic_write(self.path, "A=1\n", chmod_priv=True)
        self.assertEqual(self._leftovers(), [], "非 PermissionError 的替换失败也必须删临时文件")

    def test_success_leaves_no_tmp(self):
        from web import security as sec
        sec._atomic_write(self.path, "A=1\n", chmod_priv=True)
        self.assertEqual(self._leftovers(), [])
        with io.open(self.path, encoding="utf-8") as f:
            self.assertEqual(f.read(), "A=1\n")


class EffectiveEnvOverlayTest(unittest.TestCase):
    """ba-p07-06/07：overlay 口径（`.env` 优先、进程环境补缺）在读取器与开关上一致。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-overlay-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.env_file = os.path.join(self.tmp, ".env")
        self._old = {k: os.environ.get(k) for k in
                     ("YIBAN_MAX_ACCOUNTS", "YIBAN_VERIFY_ASYNC",
                      "YIBAN_ACCOUNT_VERIFY")}
        for k in self._old:
            os.environ.pop(k, None)

        def _restore():
            for k, v in self._old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

        self.addCleanup(_restore)

    def _write(self, text):
        with io.open(self.env_file, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)

    def test_env_file_wins_then_process_env_fills_gap(self):
        from web.services import env_io
        self._write("YIBAN_MAX_ACCOUNTS=5\n")
        os.environ["YIBAN_MAX_ACCOUNTS"] = "9"
        self.assertEqual(env_io.load_env_int(self.env_file, "YIBAN_MAX_ACCOUNTS", 0), 5)
        self._write("")
        self.assertEqual(env_io.load_env_int(self.env_file, "YIBAN_MAX_ACCOUNTS", 0), 9)

    def test_both_verify_switches_share_precedence(self):
        from web.services import verify_queue
        from yiban.adapters.config.env_io import parse_env_file
        verify_queue.configure(
            env_file=lambda: self.env_file,
            read_env=lambda: (lambda p: parse_env_file(p)))
        self._write("YIBAN_VERIFY_ASYNC=1\n")
        os.environ["YIBAN_VERIFY_ASYNC"] = "0"
        self.assertTrue(verify_queue.verify_async_enabled(), "`.env` 优先")
        self._write("")
        os.environ["YIBAN_VERIFY_ASYNC"] = "1"
        self.assertTrue(verify_queue.verify_async_enabled(), "进程环境补缺")
        # 相邻开关同口径：.env 有值优先，没有则读进程环境
        self._write("YIBAN_ACCOUNT_VERIFY=0\n")
        os.environ["YIBAN_ACCOUNT_VERIFY"] = "1"
        self.assertFalse(verify_queue._account_verify_enabled())
        self._write("")
        self.assertTrue(verify_queue._account_verify_enabled())


class TodayHasLogsFullScanTest(unittest.TestCase):
    """ba-p07-09：当天文件超过 2MB 且尾部无 yiban 行时，判定仍须为真。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-todaylogs-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_yiban_line_before_tail_window_is_seen(self):
        from web.services import logs as logs_svc
        from yiban.core import clock
        day = clock.now().strftime("%Y-%m-%d")
        log_file = os.path.join(self.tmp, "sign.log")
        path = os.path.join(self.tmp, f"sign-{day}.log")
        with io.open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(f"[{day} 06:40:00] [INFO] yiban: 唯一一条签到行\n")
            # 尾部 2MB+ 全是不可见的其他组件 INFO 行（倒读只会看到它们）
            filler = "[" + day + " 07:00:00] [INFO] werkzeug: ping ping ping ping\n"
            for _ in range(45000):
                f.write(filler)
        self.assertGreater(os.path.getsize(path), logs_svc._LOG_TAIL_BYTES)
        logs_svc.configure(log_file=lambda: log_file,
                           tail_lines=lambda p: logs_svc._tail_lines(p),
                           state_dir=lambda: self.tmp)
        self.assertTrue(logs_svc._today_has_logs(),
                        "尾部窗口内没有 yiban 行时，整读判定仍须发现文件前面的日志")


class EffectiveRoleSingleReadTest(unittest.TestCase):
    """ba-p07-11：`_effective_role` 每个请求只读一次 `.env`。"""

    def test_builtin_admin_path_reads_env_once(self):
        import flask

        from web import security as sec
        tmp = tempfile.mkdtemp(prefix="yiban-role-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        env_file = os.path.join(tmp, ".env")
        with io.open(env_file, "w", encoding="utf-8", newline="\n") as f:
            f.write("YIBAN_ADMIN_USER=admin@test.local\n"
                    "YIBAN_ADMIN_PW_VERSION=3\n"
                    "YIBAN_ADMIN_SID=abc\n")
        calls = []

        def _read(path):
            calls.append(path)
            return {"YIBAN_ADMIN_USER": "admin@test.local",
                    "YIBAN_ADMIN_PW_VERSION": "3",
                    "YIBAN_ADMIN_SID": "abc"}

        sec.configure(env_file=lambda: env_file, read_env=lambda: _read)
        app = flask.Flask("role-reads")
        app.secret_key = "x"
        with app.test_request_context("/"):
            flask.session["auth_source"] = "builtin"
            flask.session["sid"] = "abc"
            self.assertEqual(sec._effective_role("admin@test.local", 3), "admin")
        self.assertEqual(len(calls), 1,
                         f"内置管理员分支只许读一次 .env，实际 {len(calls)} 次")


class CleanupEnvAtomicTest(unittest.TestCase):
    """ba-p07-04：原子写失败不得把 .env 截成空/半份。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-cleanup-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.env_file = os.path.join(self.tmp, ".env")
        self.original = "YIBAN_A=1\nYIBAN_B=2\x85injected\n"
        with io.open(self.env_file, "w", encoding="utf-8", newline="\n") as f:
            f.write(self.original)

    def _configure(self, writer):
        from web.services import env_io
        env_io.configure(atomic_write=lambda: writer)

    def test_failed_atomic_write_leaves_file_untouched(self):
        from web.services import env_io

        def _fail(path, text, chmod_priv=False):
            raise OSError("EIO")

        self._configure(_fail)
        ok, _msg, remaining = env_io.cleanup_env_ambiguous_line(self.env_file, 2)
        self.assertFalse(ok)
        self.assertIsNone(remaining)
        with io.open(self.env_file, encoding="utf-8", newline="") as f:
            self.assertEqual(f.read(), self.original, "写失败后原文件必须逐字不变")

    def test_success_removes_the_ambiguous_line(self):
        from web import security as sec
        from web.services import env_io
        self._configure(lambda path, text, chmod_priv=False:
                        sec._atomic_write(path, text, chmod_priv=chmod_priv))
        ok, msg, remaining = env_io.cleanup_env_ambiguous_line(self.env_file, 2)
        self.assertTrue(ok, msg)
        self.assertEqual(remaining, 0)
        with io.open(self.env_file, encoding="utf-8", newline="") as f:
            text = f.read()
        self.assertNotIn("injected", text)
        self.assertIn("YIBAN_A=1", text)


class SameAccountInFlightTest(unittest.TestCase):
    """cgx6：同一手机号不得被并发持有（同步闸门与异步串行器同一把权）。"""

    def setUp(self):
        from web.services import verify_queue
        self.vq = verify_queue
        self.seat = threading.BoundedSemaphore(8)
        verify_queue.configure(
            seat=lambda: self.seat,
            attempt_allowed=lambda: (lambda limits, username: True),
            verify_clean=lambda: (lambda clean: None),
            env_file=lambda: "",
            read_env=lambda: (lambda p: {}))
        # 清掉上一格可能残留的 in-flight 键
        verify_queue.release_account(PHONE)

    def test_sync_gate_rejects_overlapping_same_account(self):
        overlap = []
        active = {"n": 0}
        lock = threading.Lock()
        started = threading.Event()

        def action():
            with lock:
                active["n"] += 1
                overlap.append(active["n"])
            started.set()
            time.sleep(0.25)
            with lock:
                active["n"] -= 1
            return "ok"

        results = []
        errors = []

        def run():
            try:
                results.append(self.vq.run_gated_call(
                    action, "u@example.com", {}, subject=PHONE))
            except self.vq.VerifyGateBusy:
                errors.append("busy")

        t1 = threading.Thread(target=run)
        t1.start()
        started.wait(2)
        t2 = threading.Thread(target=run)
        t2.start()
        t1.join(5)
        t2.join(5)
        self.assertEqual(max(overlap), 1, "同一账号的外呼发生了重叠")
        self.assertEqual(results, ["ok"])
        self.assertEqual(errors, ["busy"], "第二次同账号调用必须被闸门拒绝")

    def test_async_serializer_waits_instead_of_overlapping(self):
        overlap = []
        active = {"n": 0}
        lock = threading.Lock()

        def action():
            with lock:
                active["n"] += 1
                overlap.append(active["n"])
            time.sleep(0.15)
            with lock:
                active["n"] -= 1
            return "ok"

        results = []

        def run():
            results.append(self.vq.run_account_exclusive(PHONE, action))

        threads = [threading.Thread(target=run) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5)
        self.assertEqual(max(overlap), 1, "异步串行器允许了同账号重叠")
        self.assertEqual(results, ["ok", "ok", "ok"])


class LogsPageTierTest(_WebFixture):
    """tklp：日志页与它宿主的强档 API 配对（注册管理员不得读页面本体）。"""

    def test_registered_admin_redirected_from_logs_page(self):
        from werkzeug.security import generate_password_hash
        self.db.create_user("reg-admin@example.com",
                            generate_password_hash(USER_PASS), role="admin")
        c = self.webapp.create_app().test_client()
        r = c.post("/api/login", json={"username": "reg-admin@example.com",
                                       "password": USER_PASS})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        r = c.get("/data/logs", follow_redirects=False)
        self.assertEqual(r.status_code, 302, "注册管理员不得拿到日志页本体")
        self.assertIn("/data/dashboard", r.headers.get("Location", ""))

    def test_builtin_admin_can_open_logs_page(self):
        c = self.webapp.create_app().test_client()
        self._login_admin(c)
        r = c.get("/data/logs", follow_redirects=False)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main(verbosity=2)
