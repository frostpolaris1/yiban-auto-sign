# -*- coding: utf-8 -*-
"""日志导出脱敏与留痕测试（2026-09-08 安全审查）。

背景：/api/logs/export 此前直接 send_file 磁盘原文件——按天日志在盘上按设计
保留完整手机号（signin 状态解析/run.sh 依赖），HTTP 出口却绕开了 /api/logs
展示层的 [11 位号] 脱敏与日志过滤，且成功导出零审计留痕、无独立限速；
date 按天可枚举，一个管理员 GET 即可无痕批量拉取历史未脱敏日志。
本文件钉住：

- 导出与视图同一数据契约：同一 _log_lines_for 过滤管线 + 逐行 _mask_log_phones，
  响应体为内存脱敏副本（不再直接回磁盘原文件）；
- 写入侧两类脱敏旁路收口：signin --only 未命中路径落盘裸号 → 走 _mask_phone；
  web 告警行落盘裸 IP → 与审计同口径 hash_ip；
- 成功导出恰写一条 logs_export 审计（400/404 不写）；每 IP 窗口限速；
  401/403 鉴权边界与 forbidden_path 留痕不回退。

全程 mock / 纯本地（Flask test client + 临时 .env/DB/日志目录），无网络请求。
用法（项目根目录，勿设 PYTHONIOENCODING）：
    py -m pytest tests/test_logs_export_masking.py -v
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

TEST_KEY = "a" * 64
ADMIN_PASS = "TestPass1234!"
USER_PASS = "secret1"
HIST_DATE = "2026-08-15"
PHONE_FULL = "13912345678"
PHONE_MASKED = "139****5678"


def _log_line(date, level, name, msg):
    return f"[{date} 06:31:01] [{level}] {name}: {msg}"


class LogsExportMaskingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-logexp-")
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
        for k, v in (("YIBAN_ACCOUNTS_KEY", TEST_KEY), ("YIBAN_ENV_FILE", cls.env_file),
                     ("YIBAN_ACCOUNTS_FILE", cls.accounts_file),
                     ("YIBAN_USERS_FILE", cls.users_file),
                     ("YIBAN_DB_FILE", cls.db_file), ("YIBAN_STATE_DIR", cls.tmp),
                     ("YIBAN_LOG_FILE", cls.log_file)):
            os.environ[k] = v
        global db
        import db
        cls.db = db
        spec = importlib.util.spec_from_file_location(
            "webapp_logexp", os.path.join(BASE, "web", "app.py"))
        cls.webapp = importlib.util.module_from_spec(spec)
        sys.modules["webapp_logexp"] = cls.webapp
        with contextlib.suppress(Exception):
            spec.loader.exec_module(cls.webapp)
        assert callable(getattr(cls.webapp, "create_app", None)), \
            "web/app.py 加载失败（exec_module 异常被抑制）"

    @classmethod
    def tearDownClass(cls):
        if cls.db._conn is not None:
            with contextlib.suppress(Exception):
                cls.db._conn.close()
            cls.db._conn = None
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k in ("YIBAN_ACCOUNTS_KEY", "YIBAN_ENV_FILE", "YIBAN_ACCOUNTS_FILE",
                  "YIBAN_USERS_FILE", "YIBAN_DB_FILE", "YIBAN_STATE_DIR",
                  "YIBAN_LOG_FILE"):
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
        with open(self.accounts_file, "w", encoding="utf-8") as f:
            json.dump([], f)
        self.db.init_db(self.db_file, migrate_from=self.accounts_file,
                        env_file=self.env_file)
        self.db.create_user("user1@test.local",
                            self.webapp.generate_password_hash(USER_PASS))
        # 清理临时目录中的按天日志文件（跨测试隔离）
        for n in os.listdir(self.tmp):
            if n.startswith("sign-") and n.endswith(".log"):
                os.remove(os.path.join(self.tmp, n))

    # ---- 工具 ----
    def _write_date_log(self, date, lines):
        path = os.path.join(self.tmp, f"sign-{date}.log")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        return path

    def _admin_client(self):
        c = self.webapp.create_app().test_client()
        r = c.post("/api/login", json={"username": "admin", "password": ADMIN_PASS})
        self.assertEqual(r.status_code, 200)
        return c

    def _user_client(self):
        c = self.webapp.create_app().test_client()
        r = c.post("/api/login",
                   json={"username": "user1@test.local", "password": USER_PASS})
        self.assertEqual(r.status_code, 200)
        return c

    def _audit_rows(self, action):
        return self.db.get_conn().execute(
            "SELECT username, target, detail FROM audit_logs WHERE action=?",
            (action,)).fetchall()

    # ---- 1. 导出 = 视图同一数据契约（过滤 + 脱敏，内存副本）----
    def test_export_serves_masked_copy_not_raw_file(self):
        self._write_date_log(HIST_DATE, [
            _log_line(HIST_DATE, "INFO", "yiban", f"[{PHONE_FULL}] ✅ 签到成功"),
        ])
        r = self._admin_client().get(f"/api/logs/export?date={HIST_DATE}")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        body = r.get_data(as_text=True)
        # 脱敏形态在、完整号不在——导出不得再回磁盘原文件
        self.assertIn(PHONE_MASKED, body, f"导出应含脱敏号：{body!r}")
        self.assertNotIn(PHONE_FULL, body, "导出泄漏完整手机号")
        # 附件头：text/plain; charset=utf-8 + attachment; filename=sign-<date>.log
        self.assertTrue(r.headers.get("Content-Type", "").startswith("text/plain"),
                        r.headers.get("Content-Type"))
        self.assertIn("charset=utf-8", r.headers.get("Content-Type", ""))
        cd = r.headers.get("Content-Disposition", "")
        self.assertIn("attachment", cd)
        self.assertIn(f"sign-{HIST_DATE}.log", cd)

    def test_export_lines_equal_view_lines(self):
        """导出行集合与 /api/logs 视图逐行一致（同一 _log_lines_for 过滤管线）。"""
        self._write_date_log(HIST_DATE, [
            _log_line(HIST_DATE, "INFO", "yiban", f"[{PHONE_FULL}] ✅ 签到成功"),
            _log_line(HIST_DATE, "INFO", "mailer", "mailer INFO 行 marker-mailer-info"),
            _log_line(HIST_DATE, "DEBUG", "yiban", "debug 行 marker-yiban-debug"),
            _log_line(HIST_DATE, "WARNING", "web", "web 告警行 marker-web-warn"),
        ])
        c = self._admin_client()
        body = c.get(f"/api/logs/export?date={HIST_DATE}").get_data(as_text=True)
        data = c.get(f"/api/logs?date={HIST_DATE}").get_json()
        self.assertEqual(data["logs"], body.splitlines(),
                         "导出与视图必须同一过滤管线（行集合逐行一致）")
        self.assertNotIn("marker-mailer-info", body, "其他组件 INFO 行不入列（视图同口径）")
        self.assertNotIn("marker-yiban-debug", body, "DEBUG 行不入列")
        self.assertIn("marker-web-warn", body, "非 yiban 组件告警级行视图本就入列——同口径")

    def test_view_shows_masked_only_warning(self):
        """--only 未命中行的落盘形态（写入侧已脱敏）在视图中保持脱敏、无完整号。"""
        self._write_date_log(HIST_DATE, [
            _log_line(HIST_DATE, "WARNING", "yiban",
                      f"--only 指定账号不在配置中: {PHONE_MASKED}"),
        ])
        data = self._admin_client().get(f"/api/logs?date={HIST_DATE}").get_json()
        self.assertEqual(len(data["logs"]), 1)
        self.assertIn(PHONE_MASKED, data["logs"][0])
        self.assertNotIn(PHONE_FULL, data["logs"][0])

    # ---- 2. 写入侧：--only 未命中路径不得落盘裸号 ----
    def test_apply_only_filter_logs_masked_phone(self):
        signin_mod = self.webapp.signin
        with self.assertLogs("yiban", level="WARNING") as logs:
            filtered, missing = signin_mod._apply_only_filter([], PHONE_FULL)
        self.assertEqual(filtered, [])
        self.assertEqual(missing, [PHONE_FULL])
        self.assertTrue(any(PHONE_MASKED in m for m in logs.output),
                        f"未命中告警应落脱敏号: {logs.output}")
        self.assertFalse(any(PHONE_FULL in m for m in logs.output),
                         f"未命中告警泄漏完整号: {logs.output}")

    # ---- 3. 审计留痕：成功恰一条，400/404 零留痕 ----
    def test_export_writes_single_audit_row_failures_write_none(self):
        self._write_date_log(HIST_DATE, [
            _log_line(HIST_DATE, "INFO", "yiban", f"[{PHONE_FULL}] ✅ 签到成功"),
        ])
        c = self._admin_client()
        self.assertEqual(len(self._audit_rows("logs_export")), 0)
        r = c.get(f"/api/logs/export?date={HIST_DATE}")
        self.assertEqual(r.status_code, 200)
        rows = self._audit_rows("logs_export")
        self.assertEqual(len(rows), 1, f"成功导出应恰写一条审计: {len(rows)} 条")
        self.assertEqual(rows[0]["username"], "admin")
        self.assertEqual(rows[0]["target"], HIST_DATE)
        self.assertIn("脱敏", rows[0]["detail"])
        # 失败尝试不写审计
        self.assertEqual(c.get("/api/logs/export?date=2026-08-14").status_code, 404)
        self.assertEqual(c.get("/api/logs/export?date=2026-13-99").status_code, 400)
        self.assertEqual(len(self._audit_rows("logs_export")), 1,
                         "400/404 失败尝试不得写 logs_export 审计")

    # ---- 4. 每 IP 窗口限速：窗口内第 7 次 429 ----
    def test_export_throttles_seventh_request_per_ip(self):
        self._write_date_log(HIST_DATE, [
            _log_line(HIST_DATE, "INFO", "yiban", f"[{PHONE_FULL}] ✅ 签到成功"),
        ])
        c = self._admin_client()
        for i in range(6):
            r = c.get(f"/api/logs/export?date={HIST_DATE}")
            self.assertEqual(r.status_code, 200, f"第 {i + 1} 次导出应放行")
        r = c.get(f"/api/logs/export?date={HIST_DATE}")
        self.assertEqual(r.status_code, 429, "同 IP 窗口内第 7 次导出应 429")
        self.assertIn("频繁", r.get_json()["error"])

    # ---- 5. 鉴权边界：未登录 401、普通用户 403（forbidden_path 留痕不回退）----
    def test_export_requires_login_and_role(self):
        anon = self.webapp.create_app().test_client()
        self.assertEqual(
            anon.get(f"/api/logs/export?date={HIST_DATE}").status_code, 401)
        c = self._user_client()
        self.assertEqual(
            c.get(f"/api/logs/export?date={HIST_DATE}").status_code, 403)
        rows = self._audit_rows("forbidden_path")
        # 既有口径：target=hash_ip、detail=越权路径
        self.assertTrue(any(row["detail"] == "/api/logs/export" for row in rows),
                        f"普通用户越权导出应留 forbidden_path 审计: {[dict(r) for r in rows]}")

    # ---- 6. 写入侧：web 告警行不得落盘裸 IP（与审计 hash_ip 同口径）----
    def test_csrf_warning_logs_hashed_ip_not_raw(self):
        c = self._admin_client()
        expected = self.db.hash_ip("127.0.0.1")
        with self.assertLogs("web", level="WARNING") as logs:
            # 已登录会话缺 X-CSRF-Token 的写请求 → check_csrf 落 WARNING
            r = c.post("/api/settings", json={"sunday_sign": False})
        self.assertEqual(r.status_code, 403)
        hit = [m for m in logs.output if "CSRF 校验失败" in m]
        self.assertTrue(hit, f"应捕获 CSRF 校验失败告警行: {logs.output}")
        self.assertIn(expected, hit[0], f"告警行应含 hash_ip 形态: {hit[0]}")
        self.assertNotIn("127.0.0.1", hit[0], f"告警行泄漏裸 IP: {hit[0]}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
