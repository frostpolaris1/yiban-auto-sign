# -*- coding: utf-8 -*-
"""面板假信号回归（MF-55 ①）。

标签：E · Web：账号面板
覆盖：① `GET /api/accounts` 的用户自暂停合成——当日**已有结论**（success）时不得被
    涂成 user_cancelled（面板与 sign_events 台账相反即假信号）；当日**无结论**时照常
    合成"已取消"（原便利语义保留）。
对应实现：`web/routes/accounts_api.py::api_accounts`（is_concluded_status 门槛）。
关键断言：结论判据取 `yiban.status.is_concluded_status`（排除法，看不懂不覆盖）。
依赖：纯本地 Flask test client + 临时 `.env`/SQLite + 临时状态目录，不联网。

注：登记 55③（`_my_account_indices_of` owner 哨兵撞车）命中升级子句未修——
    `tests/test_time_prefs.py::test_api_pause_admin_own_forbidden` 与
    `test_api_pref_own_account_per_admin` 把"内置管理员 /mine 含 owner='admin' 账号"
    当作 2026-08-15 用户确认的设计钉死（pause_forbidden 标记与选片绑定都依赖它），
    方案与证据见 `.superpowers/sdd/m3-batch4-plan/task-4a-report.md`。
"""
import contextlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TEST_KEY = "c" * 64
ADMIN_USER = "admin"
ADMIN_PASS = "MasterPass#2026"
PAUSED_PHONE = "13800000002"   # 假号（遮罩形态 138****0002）
NOCONC_PHONE = "13800000005"   # 假号（遮罩形态 138****0005）


class PanelFalseSignalTest(unittest.TestCase):
    """① 已了结当日结论不被"已取消"覆写（登记原 ③ 的隔离项经裁定按默认配置取舍，未实现）。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-panel4a-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(
                f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
                f"YIBAN_ADMIN_USER={ADMIN_USER}\nYIBAN_ADMIN_PASSWORD={ADMIN_PASS}\n"
            )
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_DB_FILE"] = cls.db_file
        os.environ["YIBAN_STATE_DIR"] = cls.tmp
        os.environ["YIBAN_LOG_FILE"] = os.path.join(cls.tmp, "sign.log")
        os.environ["YIBAN_DISABLE_PURGE_LOOP"] = "1"
        global db
        import db
        spec = importlib.util.spec_from_file_location(
            "webapp_panel4a", os.path.join(BASE, "web", "app.py"))
        cls.webapp = importlib.util.module_from_spec(spec)
        sys.modules["webapp_panel4a"] = cls.webapp
        spec.loader.exec_module(cls.webapp)

    @classmethod
    def tearDownClass(cls):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        sys.modules.pop("webapp_panel4a", None)
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
        for fn in os.listdir(self.tmp):
            if fn.startswith("sign-state-") or fn.startswith("sign-daily-"):
                os.remove(os.path.join(self.tmp, fn))
        db.init_db(self.db_file, env_file=self.env_file)

    # ---- 助手 ----
    def _admin_client(self):
        c = self.webapp.create_app().test_client()
        r = c.post("/api/login", json={"username": ADMIN_USER, "password": ADMIN_PASS})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return c, c.get("/api/me").get_json()["csrf_token"]

    def _add_bare(self, c, token, phone):
        r = c.post("/api/accounts",
                   json={"name": "裸号", "phone": phone, "password": "pw12345678"},
                   headers={"X-CSRF-Token": token})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return next(a for a in db.load_accounts() if a["phone"] == phone)

    def _set_paused(self, account_id):
        with db._conn_lock:
            conn = db.get_conn()
            with conn:
                conn.execute("UPDATE accounts SET user_paused=1 WHERE id=?",
                             (account_id,))

    def _write_today_state(self, phone, status):
        today = self.webapp.clock.now().strftime("%Y-%m-%d")
        with open(os.path.join(self.tmp, f"sign-state-{today}.json"),
                  "w", encoding="utf-8") as f:
            json.dump({phone: {"status": status, "message": "签到成功"}}, f)

    # ---- ① 已签成功后自暂停：面板不得涂成"已取消"；无结论时仍即时合成 ----
    def test_paused_overwrite_respects_today_conclusion(self):
        c, token = self._admin_client()
        done_acc = self._add_bare(c, token, PAUSED_PHONE)      # 今日 success
        raw_acc = self._add_bare(c, token, NOCONC_PHONE)       # 今日无记录
        self._write_today_state(PAUSED_PHONE, "success")
        self._set_paused(done_acc["id"])
        self._set_paused(raw_acc["id"])
        body = c.get("/api/accounts").get_json()
        self.assertEqual(body["states"].get(self.webapp._mask_phone(PAUSED_PHONE)),
                         "success",
                         "当日已了结（success）不得被 user_paused 覆写成 user_cancelled")
        self.assertEqual(body["states"].get(self.webapp._mask_phone(NOCONC_PHONE)),
                         "user_cancelled",
                         "当日无结论时仍即时合成'已取消'（原便利语义不变）")


if __name__ == "__main__":
    unittest.main()
