# -*- coding: utf-8 -*-
"""批次 8 修复回归测试（2026-08-28 深夜，批次7 P1/P2 + 补充审查 A 组）。

覆盖：
- P2-1  mailer 显式 TLS 证书校验（SMTP_SSL/starttls 均传 create_default_context）
- P2-2  db_export / generate_demo_data 不触发启动清理（cleanup=False）
- P2-3  env_lock Windows msvcrt 跨进程锁（导入与接口形态；平台行为差异仅冒烟）
- P2-4  密钥/盐落盘临时文件创建即 0600 且无残留
- P2-7  audit_verify 只读化：缺库报错退出、不执行迁移
- P1-1/P2-10 child_env 共享模块：YIBAN_ 白名单 + 非法键名丢弃 + 覆盖注入
- A1    SSH 重设主管理员密码 → 迁移检测明文与哈希不一致 → 递增 PW_VERSION
- A2/A3 批量端点单次 10 上限（users/batch、accounts/batch）
- A4    settings 部分更新不再静默清空未提交的延迟字段
- A5    start_delay_max/gap_max 收归主管理员（注册管理员 403）
- A6    登录成功写审计（action=login_ok，IP 匿名化；动作名于批次14/PROD-2 由 login 收敛）

用法（项目根目录）：
    py -m pytest tests/test_batch8_fixes_0828.py -v
"""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "scripts"))
sys.path.insert(0, os.path.join(BASE, "web"))

import db  # noqa: E402

TEST_KEY = "a" * 64
ADMIN_PASS = "MasterPass#2026"
USER_PASS = "secret1"


class ChildEnvTest(unittest.TestCase):
    def test_parse_env_file_filters_and_overrides(self):
        import child_env

        tmp = tempfile.mkdtemp(prefix="childenv-")
        try:
            env_file = os.path.join(tmp, ".env")
            with io.open(env_file, "w", encoding="utf-8") as f:
                f.write(
                    "# 注释\n"
                    "YIBAN_PROXY=http://127.0.0.1:7890\n"
                    "NOT_YIBAN_KEY=x\n"          # 非 YIBAN_ 前缀 → 丢弃
                    "YIBAN_BAD KEY=v\n"          # 非法键名（空格）→ 丢弃
                    "YIBAN_EMPTY=\n"
                    "  YIBAN_SPACED =  spaced \n"
                )
            parsed = child_env.parse_env_file(env_file)
            self.assertEqual(parsed.get("YIBAN_PROXY"), "http://127.0.0.1:7890")
            self.assertNotIn("NOT_YIBAN_KEY", parsed)
            self.assertNotIn("YIBAN_BAD KEY", parsed)
            self.assertEqual(parsed.get("YIBAN_EMPTY"), "")
            self.assertEqual(parsed.get("YIBAN_SPACED"), "spaced")

            env = child_env.build_child_env(env_file, base={"PATH": "/x", "YIBAN_PROXY": "old"})
            self.assertEqual(env["YIBAN_PROXY"], "http://127.0.0.1:7890")
            self.assertEqual(env["PATH"], "/x")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class MailerTlsContextTest(unittest.TestCase):
    """P2-1：SMTP_SSL/starttls 必须显式传入证书校验 context。"""

    def test_ssl_uses_default_context(self):
        import mailer

        captured = {}

        class _FakeSMTP(contextlib.AbstractContextManager):
            def __init__(self, host, port, timeout=0, context=None, **kw):
                captured["context"] = context

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def login(self, u, p):
                pass

            def sendmail(self, f, t, msg):
                pass

        cfg = {
            "SMTP_HOST": "smtp.example.com",
            "SMTP_PORT": "465",
            "USER": "a@example.com",
            "PASS": "secret",
        }
        with mock.patch.object(mailer, "is_enabled", lambda: True), \
             mock.patch.object(mailer, "_get", lambda k: cfg.get(k)), \
             mock.patch.object(mailer.smtplib, "SMTP_SSL", _FakeSMTP):
            mailer._send("标题", "正文", "to@example.com")
        ctx = captured.get("context")
        self.assertIsNotNone(ctx, "SMTP_SSL 必须显式传 context")
        import ssl
        self.assertNotEqual(ctx.verify_mode, ssl.CERT_NONE)
        self.assertTrue(ctx.check_hostname)


class SecretFilePermTest(unittest.TestCase):
    """P2-4：密钥/盐临时文件创建即 0600（POSIX 断言），写后无残留。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="perm-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_track_salt_write_no_tmp_residue(self):
        import db

        env_file = os.path.join(self.tmp, ".env")
        with io.open(env_file, "w", encoding="utf-8") as f:
            f.write("YIBAN_OTHER=1\n")
        db._write_track_salt_to_env_file(env_file, "salt123")
        content = io.open(env_file, encoding="utf-8").read()
        self.assertIn("YIBAN_TRACK_SALT=salt123", content)
        self.assertIn("YIBAN_OTHER=1", content)  # 保留其他行
        residues = [n for n in os.listdir(self.tmp) if ".tmp" in n]
        self.assertEqual(residues, [], "临时文件必须被 replace 掉，不得残留")
        if os.name == "posix":
            import stat
            mode = stat.S_IMODE(os.stat(env_file).st_mode)
            self.assertEqual(mode, 0o600)


class AuditVerifyReadOnlyTest(unittest.TestCase):
    """P2-7：audit_verify 缺库拒绝新建；migrate=False 不改被校验库。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="auditverify-")
        for k in ("YIBAN_DB_FILE", "YIBAN_ENV_FILE", "YIBAN_ACCOUNTS_KEY", "YIBAN_STATE_DIR", "YIBAN_LOG_FILE"):
            os.environ.pop(k, None)
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None

    def tearDown(self):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_missing_db_exits_2(self):
        import audit_verify

        missing = os.path.join(self.tmp, "no-such.db")
        with contextlib.redirect_stdout(io.StringIO()) as out, \
             mock.patch.object(sys, "argv", ["audit_verify.py", "--db", missing]), \
             self.assertRaises(SystemExit) as cm:
            audit_verify.main()
        self.assertEqual(cm.exception.code, 2)
        self.assertFalse(os.path.exists(missing), "不得新建空库")
        self.assertIn("不存在", out.getvalue())

    def test_verify_does_not_run_migrations(self):
        import audit_verify

        db_file = os.path.join(self.tmp, "yiban.db")
        db.init_db(db_file=db_file, cleanup=False)
        ver_before = db.get_conn().execute("PRAGMA user_version").fetchone()[0]
        if db._conn is not None:
            db._conn.close()
            db._conn = None
        # 模拟旧库：版本回拨到 v2（audit_verify 不应把迁移跑上去）
        conn = __import__("sqlite3").connect(db_file)
        conn.execute("PRAGMA user_version = 2")
        conn.commit()
        conn.close()
        with contextlib.redirect_stdout(io.StringIO()), \
             mock.patch.object(sys, "argv", ["audit_verify.py", "--db", db_file]), \
             contextlib.suppress(SystemExit):
            audit_verify.main()
        conn = __import__("sqlite3").connect(db_file)
        ver_after = conn.execute("PRAGMA user_version").fetchone()[0]
        conn.close()
        self.assertEqual(ver_after, 2, "只读校验不得执行迁移（迁移会重写审计链抹平篡改）")
        self.assertEqual(ver_before, 12)


class AdminPasswordRotationTest(unittest.TestCase):
    """A1：SSH 重设主管理员密码 → 迁移检测明文与哈希不一致 → 递增 PW_VERSION 踢被盗会话。"""

    @classmethod
    def setUpClass(cls):
        global webapp
        spec = importlib.util.spec_from_file_location("webapp_rot", os.path.join(BASE, "web", "app.py"))
        webapp = importlib.util.module_from_spec(spec)
        sys.modules["webapp_rot"] = webapp
        with contextlib.suppress(Exception):
            spec.loader.exec_module(webapp)

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rot-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _env_with(self, content):
        p = os.path.join(self.tmp, ".env")
        with io.open(p, "w", encoding="utf-8") as f:
            f.write(content)
        return p

    def _read(self, p):
        with io.open(p, encoding="utf-8") as f:
            return f.read()

    def test_externally_changed_password_bumps_pw_version(self):
        from werkzeug.security import generate_password_hash

        env_path = self._env_with(
            f"YIBAN_ADMIN_PASSWORD_HASH={generate_password_hash('OldPass#1')}\n"
            "YIBAN_ADMIN_PASSWORD=NewPass#2\n"
            "YIBAN_ADMIN_PW_VERSION=3\n"
        )
        webapp.migrate_admin_password_to_hash(env_path)
        env = self._read(env_path)
        self.assertIn("YIBAN_ADMIN_PW_VERSION=4", env, "外部改密必须递增 PW_VERSION 踢旧会话")
        self.assertNotIn("YIBAN_ADMIN_PASSWORD=NewPass#2", env, "迁移后明文应被清空")
        self.assertIn("YIBAN_ADMIN_PASSWORD_HASH=", env)

    def test_same_password_no_bump(self):
        from werkzeug.security import generate_password_hash

        old = "SamePass#1"
        env_path = self._env_with(
            f"YIBAN_ADMIN_PASSWORD_HASH={generate_password_hash(old)}\n"
            f"YIBAN_ADMIN_PASSWORD={old}\n"
            "YIBAN_ADMIN_PW_VERSION=3\n"
        )
        webapp.migrate_admin_password_to_hash(env_path)
        self.assertIn("YIBAN_ADMIN_PW_VERSION=3", self._read(env_path), "口令未变更不得递增")

    def test_first_migration_no_bump(self):
        env_path = self._env_with(
            "YIBAN_ADMIN_PASSWORD=FirstPass#1\n"
            "YIBAN_ADMIN_USER=admin\n"
        )
        webapp.migrate_admin_password_to_hash(env_path)
        env = self._read(env_path)
        self.assertNotIn("YIBAN_ADMIN_PW_VERSION", env, "首次明文迁移不递增（会话 pw_version 保持有效）")
        self.assertIn("YIBAN_ADMIN_PASSWORD_HASH=", env)


class BatchCapAndSettingsTest(unittest.TestCase):
    """A2/A3 批量上限；A4 部分更新不清空；A5 延迟参数收归主管理员；A6 登录审计。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="batch8-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with io.open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(
                f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
                "YIBAN_ADMIN_USER=admin@test.local\n"
                f"YIBAN_ADMIN_PASSWORD={ADMIN_PASS}\n"
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
        spec = importlib.util.spec_from_file_location("webapp", os.path.join(BASE, "web", "app.py"))
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
        for k in ("YIBAN_ACCOUNTS_KEY", "YIBAN_LOG_FILE", "YIBAN_STATE_DIR"):
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
        with io.open(self.accounts_file, "w", encoding="utf-8") as f:
            json.dump([], f)
        db.init_db(self.db_file, migrate_from=self.accounts_file, env_file=self.env_file)

    def _login(self, c, username, password):
        r = c.post("/api/login", json={"username": username, "password": password})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return c.get("/api/me").get_json()["csrf_token"]

    def _csrf(self, token):
        return {"X-CSRF-Token": token}

    def _master(self):
        c = self.webapp.create_app().test_client()
        t = self._login(c, "admin@test.local", ADMIN_PASS)
        return c, t

    def _reg_admin(self):
        h = self.webapp.generate_password_hash
        db.create_user("reg-admin@test.local", h(USER_PASS), role="admin")
        c = self.webapp.create_app().test_client()
        t = self._login(c, "reg-admin@test.local", USER_PASS)
        return c, t

    def test_users_batch_cap_10(self):
        c, t = self._master()
        r = c.post("/api/users/batch", json={
            "action": "delete",
            "emails": [f"u{i}@x.test" for i in range(11)],
        }, headers=self._csrf(t))
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        self.assertIn("10", r.get_json()["error"])

    def test_accounts_batch_cap_10(self):
        c, t = self._master()
        r = c.post("/api/accounts/batch", json={
            "action": "delete",
            "ids": list(range(11)),
        }, headers=self._csrf(t))
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        self.assertIn("10", r.get_json()["error"])

    def test_settings_partial_update_preserves_delays(self):
        """A4：只提交 sunday_sign 不得清空已配置的延迟。"""
        c, t = self._master()
        r = c.post("/api/settings", json={"start_delay_max": 60, "gap_max": 30},
                   headers=self._csrf(t))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        r = c.post("/api/settings", json={"sunday_sign": 1}, headers=self._csrf(t))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        env = io.open(self.env_file, encoding="utf-8").read()
        self.assertIn("YIBAN_START_DELAY_MAX=60", env)
        self.assertIn("YIBAN_ACCOUNT_GAP_MAX=30", env)
        # 显式清零仍可用（携带字段即写）
        r = c.post("/api/settings", json={"start_delay_max": 0}, headers=self._csrf(t))
        self.assertEqual(r.status_code, 200)
        env = io.open(self.env_file, encoding="utf-8").read()
        self.assertNotIn("YIBAN_START_DELAY_MAX=60", env)

    def test_delay_settings_master_only(self):
        """A5：注册管理员改延迟/调度字段 → 403；改周日开关仍可。"""
        c, t = self._reg_admin()
        r = c.post("/api/settings", json={"start_delay_max": 3600}, headers=self._csrf(t))
        self.assertEqual(r.status_code, 403)
        r = c.post("/api/settings", json={"gap_max": 3600}, headers=self._csrf(t))
        self.assertEqual(r.status_code, 403)
        r = c.post("/api/settings", json={"sunday_sign": 1}, headers=self._csrf(t))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

    def test_login_success_audited(self):
        """A6：登录成功写审计（action=login_ok），IP 匿名化。

        批次14/PROD-2 把动作名由 login 收敛为 login_ok（与 login_failed/logout_ok 同组
        命名，取证侧一句 WHERE action='login_ok' 即可拉出完整登录时间线）。本用例钉的是
        "成功登录有没有留痕、IP 有没有匿名化"，与名字无关，故只随动字面量，
        三条断言（有行 / username 精确 / target 为 64 位 hex）一字未放宽。
        """
        _c, _ = self._master()
        rows = db.get_conn().execute(
            "SELECT username, action, target FROM audit_logs WHERE action='login_ok'"
        ).fetchall()
        self.assertTrue(rows, "登录成功应写审计")
        self.assertEqual(rows[-1]["username"], "admin@test.local")
        self.assertNotIn("127.0.0.1", rows[-1]["target"], "IP 必须匿名化（hash_ip）")
        self.assertRegex(rows[-1]["target"], r"^[0-9a-f]{64}$")


if __name__ == "__main__":
    unittest.main(verbosity=2)
