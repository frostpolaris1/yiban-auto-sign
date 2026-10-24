# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""`/api/clock` 的时区契约守卫：`tz_offset_min` 必须与 `now` 同一事实源（业务时区 CST）。

背景（2026-10-25 P0）：旧实现用 `clock.now().astimezone()` 取偏移。`clock.now()` 返回
**naive 北京墙上时间**，`astimezone()` 会把 naive 值按**宿主**时区解释——UTC 主机得到 0。
前端 `serverNow()` 按 `(tz_offset_min − 浏览器时区)` 平移，于是 UTC 主机在北京 00:00–08:00
期间把「今天」整体退到前一日：日历默认选中错误、看板今日 KPI 归零。缺陷在 UTC 主干
（CI、多数海外 VPS）必现，在 CST 开发机不可见。

本门钉两条不变式：
① `tz_offset_min` 恒为业务时区偏移 +480，**不随宿主 TZ 变化**；
② `now`（北京墙上时间）与 `server_ts`（epoch）之间的差值等于 `tz_offset_min`。

宿主 TZ 用 `TZ=UTC` + `time.tzset()` 模拟（CI 本身就是 UTC；POSIX 平台）。
突变验证：把实现改回 `.astimezone()` 写法，本文件在 CI/UTC 主机必红。

标签：E · Web：认证/权限/API
覆盖：`GET /api/clock` 的 `tz_offset_min` / `now` / `server_ts` 三者一致性。
对应实现：`web/routes/data.py::api_clock`。
依赖：纯本地 Flask test client + 临时 `.env`/SQLite，不联网、不访问真实易班接口。
"""

import datetime
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

from yiban.core import clock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEST_KEY = "c" * 64
#: 业务时区（CST）相对 UTC 的分钟偏移；与 `yiban.core.clock.TZ` 同值。
BUSINESS_TZ_OFFSET_MIN = 8 * 60


class ApiClockBusinessTzTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-clock-tz-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with io.open(cls.env_file, "w", encoding="utf-8", newline="\n") as f:
            f.write("YIBAN_ACCOUNTS_KEY=%s\n" % TEST_KEY)
        cls.accounts_file = os.path.join(cls.tmp, "accounts.json")
        with io.open(cls.accounts_file, "w", encoding="utf-8") as f:
            json.dump([], f)
        cls._old_env = {k: os.environ.get(k) for k in
                        ("YIBAN_ACCOUNTS_KEY", "YIBAN_ENV_FILE", "YIBAN_ACCOUNTS_FILE",
                         "YIBAN_DB_FILE", "YIBAN_STATE_DIR", "YIBAN_LOG_FILE")}
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_ACCOUNTS_FILE"] = cls.accounts_file
        os.environ["YIBAN_DB_FILE"] = os.path.join(cls.tmp, "yiban.db")
        os.environ["YIBAN_STATE_DIR"] = cls.tmp
        os.environ["YIBAN_LOG_FILE"] = os.path.join(cls.tmp, "sign.log")
        spec = importlib.util.spec_from_file_location(
            "webapp_clocktz", os.path.join(BASE, "web", "app.py"))
        cls.webapp = importlib.util.module_from_spec(spec)
        sys.modules["webapp_clocktz"] = cls.webapp
        spec.loader.exec_module(cls.webapp)
        # /api/* 需登录（普通用户即可读 /api/clock）；建一个普通用户并复用其会话。
        import db as _db
        from werkzeug.security import generate_password_hash
        cls.db = _db
        cls.user = "clock-tz@example.com"
        cls.password = "ClockPass123!"
        cls.db.init_db(os.environ["YIBAN_DB_FILE"],
                       migrate_from=cls.accounts_file, env_file=cls.env_file)
        cls.db.create_user(cls.user, generate_password_hash(cls.password))
        cls.client = cls.webapp.create_app().test_client()

    @classmethod
    def tearDownClass(cls):
        import contextlib
        if cls.db._conn is not None:
            with contextlib.suppress(Exception):
                cls.db._conn.close()
            cls.db._conn = None
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k, v in cls._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _login(self):
        r = self.client.post("/api/login",
                             json={"username": self.user, "password": self.password})
        self.assertEqual(r.status_code, 200, r.data[:200])

    def _clock(self):
        self._login()
        r = self.client.get("/api/clock")
        self.assertEqual(r.status_code, 200, r.data[:200])
        return r.get_json()

    @unittest.skipUnless(hasattr(time, "tzset"), "需要 POSIX time.tzset（CI/WSL）")
    def test_tz_offset_does_not_follow_host_timezone(self):
        """宿主 TZ=UTC 时仍报业务时区 +480（旧实现此时报 0）。"""
        old = os.environ.get("TZ")
        try:
            os.environ["TZ"] = "UTC"
            time.tzset()
            payload = self._clock()
        finally:
            if old is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old
            time.tzset()
        self.assertEqual(payload["tz_offset_min"], BUSINESS_TZ_OFFSET_MIN,
                         "宿主 TZ=UTC 时 tz_offset_min 必须仍是业务时区 +480")

    def test_now_and_offset_describe_the_same_clock(self):
        """`now` 是北京墙上时间；它与 UTC 的差必须恰好等于 `tz_offset_min`。"""
        payload = self._clock()
        self.assertEqual(payload["tz_offset_min"], BUSINESS_TZ_OFFSET_MIN)
        now = datetime.datetime.strptime(payload["now"], "%Y-%m-%d %H:%M:%S")
        utc_now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        drift = (now - utc_now) - datetime.timedelta(minutes=payload["tz_offset_min"])
        self.assertLessEqual(abs(drift.total_seconds()), 5,
                             "now 与 tz_offset_min 描述的时区不一致（差 %s 秒）"
                             % drift.total_seconds())
        # 逐字口径：now 就是业务钟的当下，不是宿主 date（秒级容差防跨秒）
        self.assertLessEqual(
            abs((now - clock.now()).total_seconds()), 5,
            "now 必须取业务钟 clock.now()")


if __name__ == "__main__":
    unittest.main()
