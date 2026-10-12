# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""正态 μ/σ 区间四键的设置读写（缺陷 B2 回归）。

标签：E · Web：认证/权限/API
覆盖：`/api/settings` 对 `mu_min_pct` / `mu_max_pct` / `sigma_min_pct` /
      `sigma_max_pct` 的 GET 回显（缺省 40/60/15/25）、主管理员 POST 落盘回环、
      非主管理员 403、越界 400、以及 `lo >= hi` 不拒绝（回退语义归属引擎）
对应实现：`web/routes/settings_api.py` 的 GET 回显与 POST 校验/A 档判定；
      `web/services/env_io.py` 的标签表、`SCHEDULE_DIST_ENV_KEYS` 与生效值
关键断言：四键是 **A 档**（仅主管理员）——普通管理员提交一律 403 且不落盘，与
      `sunday_sign` 等同档；真实变更必须当次口令，提交回显值（无变更）不进门禁；
      值域 0~100，`lo >= hi` 沿用引擎"告警 + 回退默认"语义（后端不得拒绝写入，
      否则存量非法配置连别的字段都存不了）。
依赖：纯本地 Flask test client + 临时 `.env`/SQLite，不联网、不需要 node；
      `YIBAN_PW_GATE=full` 由 `setUpClass` 写死（默认档 risk 下 A 档写入不再当次要口令）。
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

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TEST_KEY = "a" * 64
ADMIN_PASS = "MasterPass#2026"
USER_PASS = "secret1"

#: 四键（字段名 → .env 键名 → 缺省值）——与 `env_io.SCHEDULE_DIST_ENV_KEYS` /
#: `SCHEDULE_DIST_DEFAULTS` 同值；本测试刻意手写一份，若后端缺省被改会被 GET 用例报红。
FIELDS = {
    "mu_min_pct": ("YIBAN_SCHEDULE_MU_MIN_PCT", 40),
    "mu_max_pct": ("YIBAN_SCHEDULE_MU_MAX_PCT", 60),
    "sigma_min_pct": ("YIBAN_SCHEDULE_SIGMA_MIN_PCT", 15),
    "sigma_max_pct": ("YIBAN_SCHEDULE_SIGMA_MAX_PCT", 25),
}


class ScheduleParamsSettingsTest(unittest.TestCase):
    """/api/settings 的正态 μ/σ 四键读写与 A 档权限。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-sched-params-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        cls._write_env("")
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
        spec = importlib.util.spec_from_file_location(
            "webapp_sched_params", os.path.join(BASE, "web", "app.py"))
        cls.webapp = importlib.util.module_from_spec(spec)
        sys.modules["webapp_sched_params"] = cls.webapp
        with contextlib.suppress(Exception):
            spec.loader.exec_module(cls.webapp)

    @classmethod
    def tearDownClass(cls):
        if cls.db._conn is not None:
            with contextlib.suppress(Exception):
                cls.db._conn.close()
            cls.db._conn = None
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k in ("YIBAN_ACCOUNTS_KEY", "YIBAN_ENV_FILE", "YIBAN_ACCOUNTS_FILE",
                  "YIBAN_DB_FILE", "YIBAN_STATE_DIR", "YIBAN_LOG_FILE"):
            os.environ.pop(k, None)

    # ---- 夹具 ----
    @classmethod
    def _write_env(cls, extra):
        with io.open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(
                f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
                "YIBAN_ADMIN_USER=admin@test.local\n"
                f"YIBAN_ADMIN_PASSWORD={ADMIN_PASS}\n"
                # A 档：本类钉的是"真变更当次要口令"，固定在 full
                "YIBAN_PW_GATE=full\n"
                + extra
            )

    def setUp(self):
        if self.db._conn is not None:
            with contextlib.suppress(Exception):
                self.db._conn.close()
            self.db._conn = None
        for suffix in ("", "-wal", "-shm"):
            p = self.db_file + suffix
            if os.path.exists(p):
                os.remove(p)
        self._write_env("")
        with io.open(self.accounts_file, "w", encoding="utf-8") as f:
            json.dump([], f)
        self.db.init_db(self.db_file, migrate_from=self.accounts_file,
                        env_file=self.env_file)

    def _login(self, c, username, password):
        r = c.post("/api/login", json={"username": username, "password": password})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return c.get("/api/me").get_json()["csrf_token"]

    def _csrf(self, token):
        return {"X-CSRF-Token": token}

    def _master(self):
        c = self.webapp.create_app().test_client()
        return c, self._login(c, "admin@test.local", ADMIN_PASS)

    def _reg_admin(self):
        h = self.webapp.generate_password_hash
        self.db.create_user("reg-admin@test.local", h(USER_PASS), role="admin")
        c = self.webapp.create_app().test_client()
        return c, self._login(c, "reg-admin@test.local", USER_PASS)

    def _env_text(self):
        return io.open(self.env_file, encoding="utf-8").read()

    # ---- GET ----
    def test_get_defaults(self):
        """未配置时 GET 回显引擎缺省 40 / 60 / 15 / 25。"""
        c, t = self._master()
        r = c.get("/api/settings", headers=self._csrf(t))
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        for key, (_env_key, default) in FIELDS.items():
            with self.subTest(key=key):
                self.assertEqual(data[key], default)

    # ---- POST（主管理员）----
    def test_master_write_requires_password_then_roundtrips(self):
        """真变更当次要口令；写后 GET 回显新值、`.env` 落新键。"""
        c, t = self._master()
        r = c.post("/api/settings", json={"mu_min_pct": 35, "mu_max_pct": 55},
                   headers=self._csrf(t))
        self.assertEqual(r.status_code, 403, r.get_data(as_text=True))
        r = c.post("/api/settings",
                   json={"mu_min_pct": 35, "mu_max_pct": 55,
                         "confirm_password": ADMIN_PASS},
                   headers=self._csrf(t))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        env = self._env_text()
        self.assertIn("YIBAN_SCHEDULE_MU_MIN_PCT=35", env)
        self.assertIn("YIBAN_SCHEDULE_MU_MAX_PCT=55", env)
        data = c.get("/api/settings", headers=self._csrf(t)).get_json()
        self.assertEqual(data["mu_min_pct"], 35)
        self.assertEqual(data["mu_max_pct"], 55)
        # 未携带的两把 σ 键不得被顺带改动
        self.assertEqual(data["sigma_min_pct"], 15)
        self.assertEqual(data["sigma_max_pct"], 25)

    def test_same_value_is_no_change(self):
        """提交等于现值的值 = 无变更：不进门禁、不写盘。"""
        self._write_env("YIBAN_SCHEDULE_MU_MIN_PCT=40\n")
        c, t = self._master()
        r = c.post("/api/settings", json={"mu_min_pct": 40}, headers=self._csrf(t))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

    def test_out_of_range_rejected(self):
        """值域 0~100：越界给 400，不落盘。"""
        c, t = self._master()
        for bad in (101, -1):
            with self.subTest(bad=bad):
                r = c.post("/api/settings", json={"mu_min_pct": bad},
                           headers=self._csrf(t))
                self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        self.assertNotIn("YIBAN_SCHEDULE_MU_MIN_PCT", self._env_text())

    def test_invalid_interval_is_written_not_rejected(self):
        """lo >= hi 不拒绝（引擎负责告警 + 回退默认），两侧值逐字落盘。"""
        c, t = self._master()
        r = c.post("/api/settings",
                   json={"mu_min_pct": 70, "mu_max_pct": 60,
                         "confirm_password": ADMIN_PASS},
                   headers=self._csrf(t))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        env = self._env_text()
        self.assertIn("YIBAN_SCHEDULE_MU_MIN_PCT=70", env)
        self.assertIn("YIBAN_SCHEDULE_MU_MAX_PCT=60", env)

    # ---- 权限（A 档）----
    def test_regular_admin_denied_on_every_key(self):
        """普通管理员提交任一 μ/σ 键 → 403，且不给口令也拒、不落盘。"""
        c, t = self._reg_admin()
        for key, (_env_key, default) in FIELDS.items():
            with self.subTest(key=key):
                r = c.post("/api/settings", json={key: default + 1,
                                                  "confirm_password": USER_PASS},
                           headers=self._csrf(t))
                self.assertEqual(r.status_code, 403, r.get_data(as_text=True))
        env = self._env_text()
        for key, (env_key, _default) in FIELDS.items():
            self.assertNotIn(env_key, env, f"{key} 被普通管理员越界写入")


if __name__ == "__main__":
    unittest.main(verbosity=2)
