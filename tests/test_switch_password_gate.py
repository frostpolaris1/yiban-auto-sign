# -*- coding: utf-8 -*-
"""系统开关口令门禁测试（global_pause / registration_pause 变更需 confirm_password）。

背景：此前前端口令框收集的 confirm_password 后端并不校验（假门），持主管理员
Cookie 的会话可无口令直接翻转 global_pause / registration_pause。

口径（2026-09）：
- 仅当请求值与当前值**不同**时才要求 confirm_password；值未变（或未携带）不要求，
  其它字段的保存流程零影响；
- 缺口令 / 错口令 → 403 {"error":"口令校验未通过，设置未生效"}，响应不回显口令；
- 失败只比对不计数：不写与登录共用的 _login_fails（持 Cookie 者不得借门禁把
  管理员锁出登录），失败写一条 settings_switch_pw_fail 审计。

全程 mock / 纯本地（Flask test client），无任何网络请求。
用法（项目根目录）：
    python -m pytest tests/test_switch_password_gate.py -v
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
LOGIN_FAIL_NOTIFY = 3


class SwitchPasswordGateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-switch-pw-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(
                f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
                f"YIBAN_ADMIN_USER=admin\nYIBAN_ADMIN_PASSWORD={ADMIN_PASS}\n"
            )
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.accounts_file = os.path.join(cls.tmp, "accounts.json")
        cls.users_file = os.path.join(cls.tmp, "users.json")
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_ACCOUNTS_FILE"] = cls.accounts_file
        os.environ["YIBAN_USERS_FILE"] = cls.users_file
        os.environ["YIBAN_DB_FILE"] = cls.db_file
        os.environ["YIBAN_STATE_DIR"] = cls.tmp
        os.environ["YIBAN_LOG_FILE"] = os.path.join(cls.tmp, "logs", "sign.log")
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
        for k in ("YIBAN_ACCOUNTS_KEY", "YIBAN_ENV_FILE", "YIBAN_ACCOUNTS_FILE",
                  "YIBAN_USERS_FILE", "YIBAN_DB_FILE", "YIBAN_STATE_DIR",
                  "YIBAN_LOG_FILE"):
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
        db.init_db(self.db_file, migrate_from=self.accounts_file,
                   env_file=self.env_file)
        # 每用例回到"两开关均未配置（=开放）"基线
        self.webapp.write_env_batch(self.env_file, {
            "YIBAN_GLOBAL_PAUSE": "", "YIBAN_REGISTRATION_PAUSE": ""})

    def _login(self):
        c = self.webapp.create_app().test_client()
        r = c.post("/api/login", json={"username": "admin", "password": ADMIN_PASS})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        token = c.get("/api/me").get_json()["csrf_token"]
        return c, {"X-CSRF-Token": token}

    def _env_has(self, needle):
        with open(self.env_file, encoding="utf-8-sig") as f:
            return needle in f.read()

    def _audit_fail_rows(self):
        return db.get_conn().execute(
            "SELECT detail FROM audit_logs WHERE action='settings_switch_pw_fail'"
        ).fetchall()

    # ---- registration_pause：任务口径四态 ----
    def test_reg_change_without_password_403(self):
        """改值无口令 → 403，.env 不变，失败审计留痕。"""
        c, hdr = self._login()
        r = c.post("/api/settings", json={"registration_pause": 1}, headers=hdr)
        self.assertEqual(r.status_code, 403, r.get_data(as_text=True))
        self.assertIn("口令校验未通过", r.get_json()["error"])
        self.assertFalse(self._env_has("YIBAN_REGISTRATION_PAUSE=1"))
        self.assertEqual(len(self._audit_fail_rows()), 1)

    def test_reg_change_wrong_password_403(self):
        """错口令 → 403，.env 不变，响应不回显口令。"""
        c, hdr = self._login()
        r = c.post("/api/settings", json={
            "registration_pause": 1, "confirm_password": "WrongPass999!"},
            headers=hdr)
        self.assertEqual(r.status_code, 403, r.get_data(as_text=True))
        body = r.get_data(as_text=True)
        self.assertNotIn("WrongPass999!", body, "错误响应不得回显口令")
        self.assertFalse(self._env_has("YIBAN_REGISTRATION_PAUSE=1"))

    def test_reg_change_with_correct_password_200(self):
        """对口令 → 200 且生效（写入 .env）。"""
        c, hdr = self._login()
        r = c.post("/api/settings", json={
            "registration_pause": 1, "confirm_password": ADMIN_PASS}, headers=hdr)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(self._env_has("YIBAN_REGISTRATION_PAUSE=1"))

    def test_reg_same_value_without_password_200(self):
        """值未变（已是暂停）不带口令 → 200（未变值不要求复核）。"""
        self.webapp.write_env_batch(self.env_file, {"YIBAN_REGISTRATION_PAUSE": "1"})
        c, hdr = self._login()
        r = c.post("/api/settings", json={"registration_pause": 1}, headers=hdr)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(self._env_has("YIBAN_REGISTRATION_PAUSE=1"))

    # ---- global_pause：同口径抽查 ----
    def test_global_change_without_password_403(self):
        c, hdr = self._login()
        r = c.post("/api/settings", json={"global_pause": 1}, headers=hdr)
        self.assertEqual(r.status_code, 403, r.get_data(as_text=True))
        self.assertFalse(self._env_has("YIBAN_GLOBAL_PAUSE=1"))

    def test_global_change_with_correct_password_200(self):
        c, hdr = self._login()
        r = c.post("/api/settings", json={
            "global_pause": 1, "confirm_password": ADMIN_PASS}, headers=hdr)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(self._env_has("YIBAN_GLOBAL_PAUSE=1"))

    def test_global_same_value_without_password_200(self):
        self.webapp.write_env_batch(self.env_file, {"YIBAN_GLOBAL_PAUSE": "1"})
        c, hdr = self._login()
        r = c.post("/api/settings", json={"global_pause": 1}, headers=hdr)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

    def test_global_unpause_also_requires_password(self):
        """恢复（1→0）同为变更，同样要求口令——两个方向都不得无口令翻转。"""
        self.webapp.write_env_batch(self.env_file, {"YIBAN_GLOBAL_PAUSE": "1"})
        c, hdr = self._login()
        r = c.post("/api/settings", json={"global_pause": 0}, headers=hdr)
        self.assertEqual(r.status_code, 403, r.get_data(as_text=True))
        r2 = c.post("/api/settings", json={
            "global_pause": 0, "confirm_password": ADMIN_PASS}, headers=hdr)
        self.assertEqual(r2.status_code, 200, r2.get_data(as_text=True))
        self.assertFalse(self._env_has("YIBAN_GLOBAL_PAUSE=1"))

    # ---- 外溢面守卫 ----
    def test_other_fields_save_without_password(self):
        """不带开关字段的普通保存不受门禁影响（零口令仍可保存其它字段）。"""
        c, hdr = self._login()
        r = c.post("/api/settings", json={"sunday_sign": 1}, headers=hdr)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

    def test_wrong_password_does_not_touch_login_fail_counter(self):
        """P18 回归守卫：门禁错口令只比对不计数——连错 6 次（>LOGIN_MAX_FAILS=5）
        每次都是 403 而非 429，且此后正确口令登录不受影响（管理员未被锁出）。"""
        c, hdr = self._login()
        for _ in range(6):
            r = c.post("/api/settings", json={
                "registration_pause": 1, "confirm_password": "WrongPass999!"},
                headers=hdr)
            self.assertEqual(r.status_code, 403, r.get_data(as_text=True))
        r2 = c.post("/api/settings", json={
            "registration_pause": 1, "confirm_password": ADMIN_PASS}, headers=hdr)
        self.assertEqual(r2.status_code, 200, r2.get_data(as_text=True))
        # 同会话仍有效；再开一个新客户端用正确口令登录也应成功（计数未被污染）
        c2 = self.webapp.create_app().test_client()
        r3 = c2.post("/api/login", json={"username": "admin", "password": ADMIN_PASS})
        self.assertEqual(r3.status_code, 200, r3.get_data(as_text=True))

    def test_sensitive_pw_fail_alerts_once_at_threshold(self):
        """M5：口令复核失败走独立计数，首达阈值发一次紧急告警。

        用户裁决（P18 延续）：不写 _login_fails（不锁管理员），另开独立计数 +
        连续失败告警。连错 LOGIN_FAIL_NOTIFY 次：每次仍 403，仅第 3 次触发
        send_notification 一次（每窗口一次），且有审计留痕。
        """
        c, hdr = self._login()
        with mock.patch.object(self.webapp, "send_notification") as m:
            for _ in range(LOGIN_FAIL_NOTIFY):
                r = c.post("/api/settings", json={
                    "registration_pause": 1, "confirm_password": "WrongPass999!"},
                    headers=hdr)
                self.assertEqual(r.status_code, 403, r.get_data(as_text=True))
        self.assertEqual(m.call_count, 1, "首达阈值只告警一次")
        title, body, kw = m.call_args[0][0], m.call_args[0][1], m.call_args[1]
        self.assertIn("口令复核失败", title)
        self.assertTrue(kw.get("urgent"), "敏感操作复核失败应走紧急告警")
        self.assertIn("系统开关", body)
        self.assertNotIn("WrongPass999!", body, "告警不得回显口令")
        self.assertTrue(self._audit_fail_rows(), "失败必须留审计")

    def test_sensitive_pw_fail_no_repeat_alert_in_same_window(self):
        """M5：同一窗口内超阈值后再多失败也不重复告警（避免刷屏）。"""
        c, hdr = self._login()
        with mock.patch.object(self.webapp, "send_notification") as m:
            for _ in range(LOGIN_FAIL_NOTIFY + 2):
                r = c.post("/api/settings", json={
                    "registration_pause": 1, "confirm_password": "WrongPass999!"},
                    headers=hdr)
                self.assertEqual(r.status_code, 403, r.get_data(as_text=True))
        self.assertEqual(m.call_count, 1,
                         "窗口未滚动时超阈值再多失败也只告警一次")


if __name__ == "__main__":
    unittest.main()
