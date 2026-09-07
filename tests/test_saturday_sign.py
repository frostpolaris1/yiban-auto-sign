# -*- coding: utf-8 -*-
"""周六签到开关测试（2026-08-29；2026-09-07 v0.29.0 默认语义反转：默认关闭）。

覆盖：
- signin.main()：周六 + 缺省（默认关闭）→ exit 2（SKIPPED）；周六 + 显式开启 → 放行；
  --only 手动签到不受限；周日语义回归（不变）
- web sign_status：周六缺省 → 「今日无需打卡（周六）」；显式开启 → 走正常窗口逻辑
- /api/settings：GET 返回 saturday_sign 默认 0；POST 显式写入 0/1；
  部分更新不清空其他设置；普通管理员可改（非主管理员专属）

用法（项目根目录）：
    py -m pytest tests/test_saturday_sign.py -v
"""
import contextlib
import datetime as _dt
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

import signin  # noqa: E402

TEST_KEY = "a" * 64
ADMIN_PASS = "MasterPass#2026"
USER_PASS = "secret1"


def _weekday_dt(weekday, hour=10):
    """返回 2026-08 中首个 weekday 的 10:00 datetime（weekday: Mon=0 ... Sun=6）。"""
    d = _dt.date(2026, 8, 1)
    while d.weekday() != weekday:
        d += _dt.timedelta(days=1)
    return _dt.datetime(d.year, d.month, d.day, hour, 0, 0)


class _FakeDT:
    """signin.datetime 替身：now() 返回注入时刻；其余委托真实 datetime。"""
    _now = None
    max = _dt.datetime.max

    @classmethod
    def now(cls, tz=None):
        return cls._now


def _run_main(now_dt, const_override=None, argv=None):
    """隔离环境执行 signin.main()，返回退出码或捕获的 SystemExit。

    const_override：{常量名: 值}，覆盖 signin.SATURDAY_SIGN / SUNDAY_SIGN 等。
    """
    accounts_json = json.dumps([{"phone": "13800000000", "password": "test-pass", "name": "测试"}])
    tmp = tempfile.mkdtemp(prefix="yiban-sat-test-")
    old_env = {k: os.environ.get(k) for k in (
        "YIBAN_ACCOUNTS_JSON", "YIBAN_DB_FILE", "YIBAN_STATE_DIR", "YIBAN_LOG_FILE")}
    old_argv = sys.argv[:]
    try:
        os.environ["YIBAN_ACCOUNTS_JSON"] = accounts_json
        os.environ["YIBAN_DB_FILE"] = os.path.join(tmp, "empty.db")
        os.environ["YIBAN_STATE_DIR"] = tmp
        os.environ["YIBAN_LOG_FILE"] = os.path.join(tmp, "sign.log")
        sys.argv = ["signin.py"] + (argv or [])
        _FakeDT._now = now_dt
        patchers = [
            mock.patch.object(signin, "datetime", _FakeDT),
            mock.patch.object(signin, "load_accounts",
                              return_value=[mock.Mock(phone="13800000000", user_paused=False)]),
            mock.patch.object(signin, "run_queue_retry",
                              return_value={"13800000000": (True, "ok", False, "success")}),
            mock.patch.object(signin, "_save_cred_state"),
        ]
        for k, v in (const_override or {}).items():
            patchers.append(mock.patch.object(signin, k, v))
        with contextlib.ExitStack() as stack:
            for p in patchers:
                stack.enter_context(p)
            try:
                signin.main()
                return 0
            except SystemExit as e:
                return e.code
    finally:
        for k in ("YIBAN_ACCOUNTS_JSON", "YIBAN_DB_FILE", "YIBAN_STATE_DIR", "YIBAN_LOG_FILE"):
            if old_env.get(k) is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = old_env[k]
        sys.argv = old_argv
        shutil.rmtree(tmp, ignore_errors=True)


class SaturdaySigninGateTest(unittest.TestCase):
    """signin.main() 周六早退门（v0.29.0 起默认关闭，与周日同语义）。"""

    def test_saturday_default_off_exits_2(self):
        """周六 + SATURDAY_SIGN 缺省（False）→ exit 2（SKIPPED 语义，v0.29.0 反转）。"""
        code = _run_main(_weekday_dt(5))
        self.assertEqual(code, 2)

    def test_saturday_off_exits_2(self):
        """周六 + SATURDAY_SIGN=False（显式关闭）→ exit 2。"""
        code = _run_main(_weekday_dt(5), {"SATURDAY_SIGN": False})
        self.assertEqual(code, 2)

    def test_saturday_on_proceeds(self):
        """周六 + SATURDAY_SIGN=True（显式开启）→ 放行（不 exit 2，走到签到流程）。"""
        code = _run_main(_weekday_dt(5), {"SATURDAY_SIGN": True})
        self.assertEqual(code, 0)

    def test_sunday_default_still_skips(self):
        """周日 + SUNDAY_SIGN 缺省（False）→ 仍 exit 2（回归：不破坏既有行为）。"""
        code = _run_main(_weekday_dt(6), {"SUNDAY_SIGN": False})
        self.assertEqual(code, 2)

    def test_saturday_manual_not_blocked(self):
        """周六 + 开关关闭 + --only → 放行（用户主动触发不受限，不 exit 2）。"""
        code = _run_main(_weekday_dt(5), {"SATURDAY_SIGN": False}, argv=["--only", "13800000000"])
        self.assertNotEqual(code, 2)


class SaturdaySignStatusTest(unittest.TestCase):
    """web.sign_status 周六文案（v0.29.0 起缺省=无需打卡，显式开启走窗口逻辑）。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-sat-web-")
        cls.sat_env = os.path.join(cls.tmp, ".env")
        import db as _db
        cls.db = _db
        spec = importlib.util.spec_from_file_location("webapp_sat", os.path.join(BASE, "web", "app.py"))
        cls.webapp = importlib.util.module_from_spec(spec)
        sys.modules["webapp_sat"] = cls.webapp
        with contextlib.suppress(Exception):
            spec.loader.exec_module(cls.webapp)

    @classmethod
    def tearDownClass(cls):
        if cls.db._conn is not None:
            with contextlib.suppress(Exception):
                cls.db._conn.close()
            cls.db._conn = None
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _env(self, content):
        with io.open(self.sat_env, "w", encoding="utf-8") as f:
            f.write(content)

    def test_sign_status_saturday_default_off(self):
        """周六 + 缺省（默认关闭）→ 「今日无需打卡（周六）」（v0.29.0 语义反转）。"""
        self._env("")
        with mock.patch.object(self.webapp, "ENV_FILE", self.sat_env):
            text, color = self.webapp.sign_status(now=_weekday_dt(5))
        self.assertEqual(text, "今日无需打卡（周六）")
        self.assertEqual(color, "#a1a1aa")

    def test_sign_status_saturday_on_proceeds(self):
        """周六 + 显式开启 → 走正常窗口逻辑（此时 10:00 已过 07:50 → 已结束）。"""
        self._env("YIBAN_SATURDAY_SIGN=1\n")
        with mock.patch.object(self.webapp, "ENV_FILE", self.sat_env):
            text, _ = self.webapp.sign_status(now=_weekday_dt(5))
        self.assertNotEqual(text, "今日无需打卡（周六）")
        self.assertIn("已结束", text)


class SaturdaySettingsWebTest(unittest.TestCase):
    """/api/settings 周六开关读写（默认 0、POST 显式写 0/1、部分更新不串改、普通管理员可改）。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-sat-set-")
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
        import db as _db
        cls.db = _db
        spec = importlib.util.spec_from_file_location("webapp_set", os.path.join(BASE, "web", "app.py"))
        cls.webapp = importlib.util.module_from_spec(spec)
        sys.modules["webapp_set"] = cls.webapp
        with contextlib.suppress(Exception):
            spec.loader.exec_module(cls.webapp)

    @classmethod
    def tearDownClass(cls):
        if cls.db._conn is not None:
            with contextlib.suppress(Exception):
                cls.db._conn.close()
            cls.db._conn = None
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k in ("YIBAN_ACCOUNTS_KEY", "YIBAN_LOG_FILE", "YIBAN_STATE_DIR"):
            os.environ.pop(k, None)

    def setUp(self):
        if self.db._conn is not None:
            with contextlib.suppress(Exception):
                self.db._conn.close()
            self.db._conn = None
        for suffix in ("", "-wal", "-shm"):
            p = self.db_file + suffix
            if os.path.exists(p):
                os.remove(p)
        with io.open(self.accounts_file, "w", encoding="utf-8") as f:
            json.dump([], f)
        self.db.init_db(self.db_file, migrate_from=self.accounts_file, env_file=self.env_file)

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
        self.db.create_user("reg-admin@test.local", h(USER_PASS), role="admin")
        c = self.webapp.create_app().test_client()
        t = self._login(c, "reg-admin@test.local", USER_PASS)
        return c, t

    def test_settings_get_default_saturday_off(self):
        """GET /api/settings：saturday_sign 默认 0（v0.29.0 起默认关闭），sunday_sign 默认 0。"""
        # 先重置 env：同类内字母序更前的 toggle 用例会向共享 .env 写入显式开关值
        with io.open(self.env_file, "w", encoding="utf-8") as f:
            f.write(
                f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
                "YIBAN_ADMIN_USER=admin@test.local\n"
                f"YIBAN_ADMIN_PASSWORD={ADMIN_PASS}\n"
            )
        c, t = self._master()
        r = c.get("/api/settings", headers=self._csrf(t))
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertEqual(data["saturday_sign"], 0)
        self.assertEqual(data["sunday_sign"], 0)

    def test_saturday_toggle_saves_env(self):
        """POST saturday_sign=0/1 → 显式写 0/1（写值口径不变，关闭也显式落盘自文档化）。"""
        c, t = self._master()
        r = c.post("/api/settings", json={"saturday_sign": 0}, headers=self._csrf(t))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        env = io.open(self.env_file, encoding="utf-8").read()
        self.assertIn("YIBAN_SATURDAY_SIGN=0", env)
        r = c.post("/api/settings", json={"saturday_sign": 1}, headers=self._csrf(t))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        env = io.open(self.env_file, encoding="utf-8").read()
        self.assertIn("YIBAN_SATURDAY_SIGN=1", env)

    def test_saturday_toggle_regular_admin_ok(self):
        """普通管理员可改周六开关（非主管理员专属，与周日同级）。"""
        c, t = self._reg_admin()
        r = c.post("/api/settings", json={"saturday_sign": 0}, headers=self._csrf(t))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

    def test_saturday_partial_update_preserves_delays(self):
        """只改 saturday_sign 不得清空已配置的延迟（沿用字段携带才写语义）。"""
        c, t = self._master()
        r = c.post("/api/settings", json={"gap_max": 60, "confirm_password": ADMIN_PASS}, headers=self._csrf(t))
        self.assertEqual(r.status_code, 200)
        r = c.post("/api/settings", json={"saturday_sign": 0}, headers=self._csrf(t))
        self.assertEqual(r.status_code, 200)
        env = io.open(self.env_file, encoding="utf-8").read()
        self.assertIn("YIBAN_ACCOUNT_GAP_MAX=60", env)
        self.assertNotIn("YIBAN_START_DELAY_MAX", env, "未携带的启动延迟键不得被写入")


if __name__ == "__main__":
    unittest.main(verbosity=2)
