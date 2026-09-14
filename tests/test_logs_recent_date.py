# -*- coding: utf-8 -*-
"""日志页「最近有数据日期」空态出口（用户实拍：默认日期三块可能都空）。

覆盖：
- 指定日期无日志/无事件时，/api/logs 返回 recent_log_date / recent_probe_date /
  recent_sign_date（各自来源的最近有数据日期），供前端空态一键跳转；
- 当前日期即最近时返回空串（不给无意义的跳转）；
- 缺省日期优先落到最近有日志的一天（回归既有兜底语义）。
"""
import contextlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TEST_KEY = "a" * 64
ADMIN_PASS = "TestPass1234!"


def _d(offset):
    """相对今天的日期字符串（避免硬编码日期随运行日漂移）。"""
    return (datetime.now() + timedelta(days=offset)).strftime("%Y-%m-%d")


class LogsRecentDateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-logs-recent-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        # 直接写 scrypt 哈希而非明文：明文会触发 create_app 的「口令明文」WARNING，
        # 该告警会被 _log_lines_for 收进当天日志，破坏「今天无日志」的场景。
        from werkzeug.security import generate_password_hash
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(
                f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
                f"YIBAN_ADMIN_USER=admin\n"
                f"YIBAN_ADMIN_PASSWORD_HASH={generate_password_hash(ADMIN_PASS, method='scrypt')}\n"
            )
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.accounts_file = os.path.join(cls.tmp, "accounts.json")
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_ACCOUNTS_FILE"] = cls.accounts_file
        os.environ["YIBAN_DB_FILE"] = cls.db_file
        os.environ["YIBAN_STATE_DIR"] = cls.tmp
        cls.log_file = os.path.join(cls.tmp, "sign.log")
        os.environ["YIBAN_LOG_FILE"] = cls.log_file
        os.environ["YIBAN_DISABLE_PURGE_LOOP"] = "1"
        global db
        import db
        with open(cls.accounts_file, "w", encoding="utf-8") as f:
            json.dump([], f)
        db.init_db(cls.db_file, migrate_from=cls.accounts_file, env_file=cls.env_file)
        spec = importlib.util.spec_from_file_location(
            "webapp_logs_recent", os.path.join(BASE, "web", "app.py"))
        cls.webapp = importlib.util.module_from_spec(spec)
        sys.modules["webapp_logs_recent"] = cls.webapp
        with contextlib.suppress(Exception):
            spec.loader.exec_module(cls.webapp)

        # 数据布局（相对今天）：
        #   log_day   = 今天-3：有日志行 + 有签到事件
        #   probe_day = 今天-1：只有探针事件
        #   today            ：三块皆空（构造用户实拍场景）
        cls.log_day = _d(-3)
        cls.probe_day = _d(-1)
        cls.today = _d(0)
        with open(os.path.join(cls.tmp, f"sign-{cls.log_day}.log"), "w", encoding="utf-8") as f:
            f.write(f"[{cls.log_day} 06:31:01] [INFO] yiban: ==== 今日签到开始 ====\n")
        db.add_sign_event(f"{cls.log_day} 06:31:02", "13800138000",
                          "success", "登录成功", stage="sign", attempt=1)
        db.add_sign_event(f"{cls.probe_day} 03:00:00", "", "ok",
                          "接口探测正常", stage="probe")

    @classmethod
    def tearDownClass(cls):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k in ("YIBAN_ACCOUNTS_KEY", "YIBAN_ENV_FILE", "YIBAN_ACCOUNTS_FILE",
                  "YIBAN_DB_FILE", "YIBAN_STATE_DIR", "YIBAN_LOG_FILE",
                  "YIBAN_DISABLE_PURGE_LOOP"):
            os.environ.pop(k, None)

    def _client(self):
        c = self.webapp.create_app().test_client()
        # create_app 会把「管理员口令明文」告警写进当天日志文件（WARNING 级也会被
        # _log_lines_for 收录），会破坏「今天三块皆空」的场景——删掉当天文件还原空态；
        # 同时清掉「最近日志日期」模块缓存，避免跨用例的当日缓存干扰本用例场景。
        with contextlib.suppress(OSError):
            os.remove(os.path.join(self.tmp, f"sign-{self.today}.log"))
        with contextlib.suppress(AttributeError, KeyError, TypeError):
            self.webapp._most_recent_log_cache["history_date"] = None
            self.webapp._most_recent_log_cache["checked_day"] = ""
        r = c.post("/api/login", json={"username": "admin", "password": ADMIN_PASS})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return c

    def test_recent_fields_point_to_each_source(self):
        c = self._client()
        r = c.get(f"/api/logs?date={self.today}")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        d = r.get_json()
        self.assertEqual(d["date"], self.today)
        self.assertEqual(d["logs"], [])
        self.assertEqual(d["recent_log_date"], self.log_day)
        self.assertEqual(d["recent_sign_date"], self.log_day)
        self.assertEqual(d["recent_probe_date"], self.probe_day)

    def test_recent_field_empty_when_current_date_is_latest(self):
        c = self._client()
        d = c.get(f"/api/logs?date={self.log_day}").get_json()
        # 当前日期自身有日志/签到事件 → 不再给「最近有数据日期」出口
        self.assertEqual(d["recent_log_date"], "")
        self.assertEqual(d["recent_sign_date"], "")
        # 该日期无探针事件，探针出口仍指向 probe_day
        self.assertEqual(d["recent_probe_date"], self.probe_day)

    def test_default_date_falls_back_to_recent_log_day(self):
        c = self._client()
        d = c.get("/api/logs").get_json()
        self.assertEqual(d["date"], self.log_day)
        self.assertTrue(d["logs"])


if __name__ == "__main__":
    unittest.main()
