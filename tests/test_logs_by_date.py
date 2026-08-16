# -*- coding: utf-8 -*-
"""按天日志（sign-YYYY-MM-DD.log）读取与按日期查看功能测试。

背景（2026-08-16 需求）：日志改为按天分文件后，
- 管理员 /api/logs?date=YYYY-MM-DD 可查任意日期日志（缺省=今天，行为不变）
- 用户 /api/my-logs?date= 读对应日期文件（日历点历史日期可见自己的日志）
- 行首日期过滤防跨天残留；历史日期文件缺失返回空（不报错）

用法（项目根目录）：
    py -m pytest tests/test_logs_by_date.py -v
"""
import contextlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "scripts"))
sys.path.insert(0, os.path.join(BASE, "web"))

TEST_KEY = "a" * 64
ADMIN_PASS = "TestPass1234!"
USER_PASS = "secret1"
HIST_DATE = "2026-08-15"  # 固定历史日期（不与今天冲突）


def _log_line(date, level, name, msg):
    return f"[{date} 06:31:01] [{level}] {name}: {msg}"


class LogsByDateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-logdate-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(
                f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
                f"YIBAN_ADMIN_USER=admin\nYIBAN_ADMIN_PASSWORD={ADMIN_PASS}\n"
            )
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.accounts_file = os.path.join(cls.tmp, "accounts.json")
        cls.users_file = os.path.join(cls.tmp, "users.json")
        # 日志目录隔离到临时目录：LOG_FILE 指向 tmp/sign.log → 按天文件 = tmp/sign-*.log
        cls.log_file = os.path.join(cls.tmp, "sign.log")
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_ACCOUNTS_FILE"] = cls.accounts_file
        os.environ["YIBAN_USERS_FILE"] = cls.users_file
        os.environ["YIBAN_DB_FILE"] = cls.db_file
        os.environ["YIBAN_STATE_DIR"] = cls.tmp
        os.environ["YIBAN_LOG_FILE"] = cls.log_file
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
        with open(self.accounts_file, "w", encoding="utf-8") as f:
            json.dump([], f)
        db.init_db(self.db_file, migrate_from=self.accounts_file, env_file=self.env_file)
        db.create_user("admin@test.local", self.webapp.generate_password_hash(ADMIN_PASS), role="admin")
        db.create_user("user1@test.local", self.webapp.generate_password_hash(USER_PASS))
        db.add_account({"name": "U1", "phone": "13800138001", "password": "p1",
                        "status": "active", "owner": "user1@test.local"})
        db.add_account({"name": "A2", "phone": "13900139002", "password": "p2",
                        "status": "active", "owner": "admin"})
        # 清理临时目录中的按天日志文件（跨测试隔离）
        for n in os.listdir(self.tmp):
            if n.startswith("sign-") and n.endswith(".log"):
                os.remove(os.path.join(self.tmp, n))

    # ---- 工具：构造按天日志文件 ----
    def _write_date_log(self, date, lines):
        path = os.path.join(self.tmp, f"sign-{date}.log")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        return path

    # ---- 1. log_path_for：按天路径 ----
    def test_log_path_for_today_and_hist(self):
        today = datetime.now().strftime("%Y-%m-%d")
        self.assertEqual(
            self.webapp.log_path_for(),
            os.path.join(self.tmp, f"sign-{today}.log"),
        )
        self.assertEqual(
            self.webapp.log_path_for(HIST_DATE),
            os.path.join(self.tmp, f"sign-{HIST_DATE}.log"),
        )

    # ---- 2. _log_lines_for：只返回该日 yiban 非 DEBUG 行 ----
    def test_log_lines_for_filters(self):
        self._write_date_log(HIST_DATE, [
            _log_line(HIST_DATE, "INFO", "yiban", "[13800138001] ✅ 签到成功"),
            _log_line(HIST_DATE, "INFO", "yiban", "==== 开始执行签到，共 1 个账号，队列重试模式 ===="),
            _log_line(HIST_DATE, "DEBUG", "yiban", "[13800138001] 登录方式: KillYiBan 同款"),  # DEBUG 应滤掉
            _log_line(HIST_DATE, "INFO", "werkzeug", '127.0.0.1 - - "GET /api/logs HTTP/1.1" 200 -'),  # 非 yiban 应滤掉
            "无格式行（run.sh 直接 echo）",  # 不匹配正则应滤掉
        ])
        out = self.webapp._log_lines_for(HIST_DATE)
        self.assertEqual(len(out), 2)
        self.assertIn("签到成功", out[0])
        self.assertIn("开始执行签到", out[1])

    def test_log_lines_for_blocks_crossday_leftover(self):
        """跨天残留行（文件日期 ≠ 行首日期）不得混入。"""
        self._write_date_log(HIST_DATE, [
            _log_line(HIST_DATE, "INFO", "yiban", "[13800138001] ✅ 签到成功"),
            _log_line("2026-08-16", "INFO", "yiban", "[13800138001] ✅ 签到成功"),  # 残留
        ])
        out = self.webapp._log_lines_for(HIST_DATE)
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0].startswith(f"[{HIST_DATE} "))

    def test_log_lines_for_missing_file(self):
        self.assertEqual(self.webapp._log_lines_for("2026-08-01"), [])

    # ---- 3. parse_sign_log 兼容按天文件（0.19.6 起仅返回 recent 行，states 语义已移除）----
    def test_parse_sign_log_returns_recent_only(self):
        today = datetime.now().strftime("%Y-%m-%d")
        self._write_date_log(today, [
            _log_line(today, "INFO", "yiban", "[13800138001] ✅ 签到成功"),
            _log_line(today, "DEBUG", "yiban", "[13800138001] 内部细节"),  # DEBUG 应滤掉
            _log_line(today, "INFO", "werkzeug", 'GET /api/logs'),  # 非 yiban 应滤掉
        ])
        recent = self.webapp.parse_sign_log(self.webapp.log_path_for())
        self.assertEqual(len(recent), 1)
        self.assertIn("✅ 签到成功", recent[0])

    # ---- 4. API：/api/logs 日期参数 ----
    def _admin_client(self):
        c = self.webapp.create_app().test_client()
        r = c.post("/api/login", json={"username": "admin", "password": ADMIN_PASS})
        self.assertEqual(r.status_code, 200)
        return c

    def test_api_logs_default_is_today(self):
        today = datetime.now().strftime("%Y-%m-%d")
        self._write_date_log(today, [
            _log_line(today, "INFO", "yiban", "[13800138001] ✅ 签到成功"),
        ])
        c = self._admin_client()
        data = c.get("/api/logs").get_json()
        self.assertEqual(data["date"], today)
        self.assertEqual(data["log_file"], f"sign-{today}.log")
        self.assertEqual(len(data["logs"]), 1)
        self.assertIn("✅ 签到成功", data["logs"][0])
        # 0.19.6 起 /api/logs 不再返回 states（账号图标事实源为 /api/accounts），
        # 防止日志符号污染前端状态映射
        self.assertNotIn("states", data)

    def test_api_logs_bad_date_400(self):
        c = self._admin_client()
        self.assertEqual(c.get("/api/logs?date=2026-13-99").status_code, 400)
        self.assertEqual(c.get("/api/logs?date=abc").status_code, 400)

    def test_api_logs_hist_date(self):
        self._write_date_log(HIST_DATE, [
            _log_line(HIST_DATE, "INFO", "yiban", "[13900139002] ✅ 签到成功"),
        ])
        c = self._admin_client()
        data = c.get(f"/api/logs?date={HIST_DATE}").get_json()
        self.assertEqual(data["date"], HIST_DATE)
        self.assertEqual(data["log_file"], f"sign-{HIST_DATE}.log")
        self.assertEqual(len(data["logs"]), 1)

    def test_api_logs_missing_date_empty(self):
        c = self._admin_client()
        data = c.get("/api/logs?date=2026-08-01").get_json()
        self.assertEqual(data["logs"], [])

    # ---- 5. API：/api/my-logs 读按天文件（用户日历）----
    def _user_client(self):
        c = self.webapp.create_app().test_client()
        r = c.post("/api/login", json={"username": "user1@test.local", "password": USER_PASS})
        self.assertEqual(r.status_code, 200)
        return c

    def test_my_logs_hist_date_filters_own_phone(self):
        self._write_date_log(HIST_DATE, [
            _log_line(HIST_DATE, "INFO", "yiban", "[13800138001] ✅ 签到成功"),  # 自己的
            _log_line(HIST_DATE, "INFO", "yiban", "[13900139002] ✅ 签到成功"),  # 管理员的
            _log_line(HIST_DATE, "INFO", "yiban", "==== 签到汇总 ===="),
        ])
        c = self._user_client()
        data = c.get(f"/api/my-logs?date={HIST_DATE}").get_json()
        self.assertEqual(data["date"], HIST_DATE)
        self.assertEqual(len(data["logs"]), 1)
        # 日志行已脱敏：完整号 → 138****8001
        self.assertIn("138****8001", data["logs"][0])
        self.assertNotIn("13900139002", data["logs"][0])

    def test_my_logs_missing_date_empty(self):
        c = self._user_client()
        data = c.get("/api/my-logs?date=2026-08-01").get_json()
        self.assertEqual(data["logs"], [])

    def test_my_logs_bad_date_400(self):
        c = self._user_client()
        self.assertEqual(c.get("/api/my-logs?date=bad").status_code, 400)


if __name__ == "__main__":
    unittest.main()
