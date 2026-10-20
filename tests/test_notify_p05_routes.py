# -*- coding: utf-8 -*-
"""通知配置 / 系统设置路由的 P05 加固回归（ba-p05-04/05/07/09/10）。

标签：H · 通知：邮件与推送
覆盖：`PUT /api/notify-config` 的「仅 secret 为空即清钥」、数值键区间校验；
    `POST /api/settings` 的原始请求值不进日志/审计（换行伪造面）、探针频率上界、
    延迟/间隔越界改 400、变更判定与落盘同持 `.env` 写锁。
对应实现：`web/routes/notify.py`（api_notify_config_save）、
    `web/routes/settings_api.py`（api_settings_save）。
关键断言：清钥请求真的删掉密文键（不是 200 空操作）；越界数值以 400 拒绝且零落盘；
    外部可控原值不得以含换行的形态进入日志或审计；`_settings_effective_values`
    执行期间另一线程拿不到同一把 `.env` 写锁（判据与写入同版）。
依赖：Flask test client + 临时 `.env`/SQLite（复用 `test_rekey_key_source` 的
    `_B14AlertGateBase`）；不连 SMTP、不触网。
"""
import threading
import unittest
from unittest import mock

from test_rekey_key_source import ADMIN_PASS, _B14AlertGateBase

from web.services import env_io as _env_io_svc
from yiban.adapters.config import env_io

SCT_KEY = "SCT406257TESTTESTTESTTESTTEST"


class _AdminBase(_B14AlertGateBase):
    def _admin(self):
        c = self._client()
        token = self._login(c, "admin", ADMIN_PASS)
        return c, self._csrf(token)


# ---------------------------------------------------------------------------
# ba-p05-04：只提交 {"secret": ""} 必须真的清钥
# ---------------------------------------------------------------------------

class NotifyConfigClearSecretTest(_AdminBase):
    def test_secret_only_empty_clears_key(self):
        c, h = self._admin()
        r = c.put("/api/notify-config",
                  json={"type": "serverchan", "secret": SCT_KEY,
                        "confirm_password": ADMIN_PASS}, headers=h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(env_io.parse_env_file(self.env_file).get("YIBAN_NOTIFY_SECRET_ENC"),
                        "前置：密钥已落盘")

        r = c.put("/api/notify-config",
                  json={"secret": "", "confirm_password": ADMIN_PASS}, headers=h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        env = env_io.parse_env_file(self.env_file)
        self.assertNotIn("YIBAN_NOTIFY_SECRET_ENC", env,
                         "secret 为空 = 清除密钥：键必须真被删掉，不得 200 空操作")
        self.assertFalse(r.get_json()["enabled"], "清钥后的回显必须是未启用")
        rows = self._audit_rows("notify_config")
        self.assertTrue(rows, "清钥必须留审计行")
        self.assertIn("cleared", rows[-1]["detail"], "审计必须记下清钥动作")

    def test_empty_body_is_rejected_without_consuming_creds_quota(self):
        c, h = self._admin()
        r = c.put("/api/notify-config", json={}, headers=h)
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        self.assertEqual(self._audit_rows("notify_config"), [],
                         "空请求不是配置变更，不得留 notify_config 审计")


# ---------------------------------------------------------------------------
# ba-p05-10：cooldown / daily_max / urgent_daily_max 的区间校验
# ---------------------------------------------------------------------------

class NotifyConfigNumericBoundsTest(_AdminBase):
    def test_out_of_range_values_rejected_and_not_persisted(self):
        c, h = self._admin()
        cases = (
            {"cooldown": 86401},
            {"cooldown": -1},
            {"daily_max": 100001},
            {"daily_max": -1},
            {"urgent_daily_max": 100001},
            {"urgent_daily_max": -1},
        )
        for body in cases:
            with self.subTest(body=body):
                r = c.put("/api/notify-config", json=body, headers=h)
                self.assertEqual(r.status_code, 400, f"{body} 应被拒绝: {r.get_data(as_text=True)}")
        env = env_io.parse_env_file(self.env_file)
        for key in ("YIBAN_NOTIFY_COOLDOWN", "YIBAN_NOTIFY_DAILY_MAX",
                    "YIBAN_NOTIFY_URGENT_DAILY_MAX"):
            self.assertNotIn(key, env, f"越界值不得落盘：{key}")

    def test_in_range_values_persisted(self):
        c, h = self._admin()
        r = c.put("/api/notify-config",
                  json={"cooldown": 86400, "daily_max": 100000,
                        "urgent_daily_max": 0}, headers=h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        env = env_io.parse_env_file(self.env_file)
        self.assertEqual(env.get("YIBAN_NOTIFY_COOLDOWN"), "86400")
        self.assertEqual(env.get("YIBAN_NOTIFY_DAILY_MAX"), "100000")
        self.assertEqual(env.get("YIBAN_NOTIFY_URGENT_DAILY_MAX"), "0")


# ---------------------------------------------------------------------------
# ba-p05-05：原始请求值不得以含换行形态进日志/审计
# ---------------------------------------------------------------------------

class SettingsRawValueNlSafeTest(_AdminBase):
    def test_raw_pref_and_window_never_reach_log_or_audit(self):
        c, h = self._admin()
        forged_window = "06:00 \n~ 07:00"
        forged_pref = "1\nFAKE-LOG-LINE"
        with self.assertLogs("web", level="INFO") as logs:
            r = c.post("/api/settings",
                       json={"sign_window": forged_window,
                             "allow_time_pref": forged_pref,
                             "confirm_password": ADMIN_PASS}, headers=h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        messages = [rec.getMessage() for rec in logs.records]
        update_lines = [m for m in messages if "更新设置" in m]
        self.assertTrue(update_lines, "设置保存应有 INFO 日志")
        self.assertNotIn("\n", update_lines[0], "日志行不得被原始请求值折行伪造")
        self.assertNotIn("FAKE-LOG-LINE", update_lines[0])

        rows = self._audit_rows("settings_save")
        self.assertTrue(rows, "设置保存必须留审计行")
        detail = rows[-1]["detail"]
        self.assertNotIn("\n", detail, "审计正文不得被原始请求值折行伪造")
        self.assertNotIn("FAKE-LOG-LINE", detail)
        env = env_io.parse_env_file(self.env_file)
        self.assertEqual(env.get("YIBAN_SIGN_START"), "06:00", "落盘用规范化后的窗口")
        self.assertEqual(env.get("YIBAN_SIGN_END"), "07:00")
        self.assertEqual(env.get("YIBAN_ALLOW_TIME_PREF"), "0",
                         "非法真值串按假处理，且只落规范化结果")


# ---------------------------------------------------------------------------
# ba-p05-07：probe_interval 上界、start/gap 越界不得静默夹取
# ---------------------------------------------------------------------------

class SettingsNumericBoundsTest(_AdminBase):
    def test_probe_interval_upper_bound(self):
        c, h = self._admin()
        for bad in ("0", "100000"):
            with self.subTest(probe_interval=bad):
                r = c.post("/api/settings", json={"probe_interval": bad}, headers=h)
                self.assertEqual(r.status_code, 400, f"probe_interval={bad} 应拒绝")
        r = c.post("/api/settings",
                   json={"probe_interval": "3650", "confirm_password": ADMIN_PASS}, headers=h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(env_io.parse_env_file(self.env_file)
                         .get("YIBAN_PROBE_INTERVAL_DAYS"), "3650")

    def test_start_and_gap_out_of_range_rejected_not_clamped(self):
        c, h = self._admin()
        for body in ({"start_delay_max": 99999}, {"gap_max": -1}):
            with self.subTest(body=body):
                r = c.post("/api/settings", json=body, headers=h)
                self.assertEqual(r.status_code, 400, f"{body} 应拒绝而不是静默夹取")
        env = env_io.parse_env_file(self.env_file)
        self.assertNotIn("YIBAN_START_DELAY_MAX", env)
        self.assertNotIn("YIBAN_ACCOUNT_GAP_MAX", env)
        r = c.post("/api/settings",
                   json={"start_delay_max": 3600, "gap_max": 10,
                         "confirm_password": ADMIN_PASS}, headers=h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        env = env_io.parse_env_file(self.env_file)
        self.assertEqual(env.get("YIBAN_START_DELAY_MAX"), "3600")
        self.assertEqual(env.get("YIBAN_ACCOUNT_GAP_MAX"), "10")


# ---------------------------------------------------------------------------
# ba-p05-09：变更判定与落盘同持 .env 写锁
# ---------------------------------------------------------------------------

class SettingsCompareUnderWriteLockTest(_AdminBase):
    def test_comparison_runs_while_holding_env_write_lock(self):
        c, h = self._admin()
        real = _env_io_svc._settings_effective_values
        acquired = threading.Event()
        state = {}

        def _probe(env_file):
            def _take():
                with _env_io_svc._env_write_lock(env_file):
                    acquired.set()

            t = threading.Thread(target=_take, daemon=True)
            state["thread"] = t
            t.start()
            # 本函数仍在请求线程内执行；若判定确实在写锁内，探测线程拿不到锁。
            state["blocked"] = not acquired.wait(0.5)
            return real(env_file)

        with mock.patch.object(_env_io_svc, "_settings_effective_values", _probe):
            r = c.post("/api/settings",
                       json={"sign_window": "06:00 ~ 07:00",
                             "confirm_password": ADMIN_PASS}, headers=h)
        state["thread"].join(timeout=5)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(state.get("blocked"),
                        "变更判定必须与落盘同持 _env_write_lock（比对时的那版 = 写入时的那版）")


if __name__ == "__main__":
    unittest.main()
