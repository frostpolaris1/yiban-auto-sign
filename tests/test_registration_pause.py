# -*- coding: utf-8 -*-
"""暂停注册（v0.26.3）+ web 日志落盘（_DailyFlockFileHandler）测试。

覆盖：
- 注册开关：未配置=允许；YIBAN_REGISTRATION_PAUSE=1 → /api/register 403；
  已注册用户登录不受影响（用户裁决：暂停仅关注册入口）；
- 设置接口：GET 返回 registration_pause；主管理员可写（写入 .env）；
  普通管理员 403（与 global_pause 同权限口径）；
- 公开端点：/api/registration_paused 仅暴露布尔；
- ensure_secret_key：全新部署（.env 不存在）默认写入暂停键；既有部署不写；
- 日志落盘：create_app 后 root 挂 _DailyFlockFileHandler 且写入 sign-*.log。

全程 mock / 纯本地（Flask test client），无任何网络请求。
用法（项目根目录）：
    python -m pytest tests/test_registration_pause.py -v
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
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "scripts"))
sys.path.insert(0, os.path.join(BASE, "web"))

TEST_KEY = "a" * 64
ADMIN_PASS = "TestPass1234!"
USER_PASS = "UserPass123!"


class RegistrationPauseWebTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-reg-pause-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        cls.log_dir = os.path.join(cls.tmp, "logs")
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
        os.environ["YIBAN_LOG_FILE"] = os.path.join(cls.log_dir, "sign.log")
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
        self._set_pause_env(None)  # 每用例回到"未配置"基线

    def _set_pause_env(self, value):
        """直接改 .env 的注册暂停键（None=删键，"1"/""=写值）。"""
        with open(self.env_file, encoding="utf-8-sig") as f:
            lines = f.read().splitlines()
        lines = [ln for ln in lines
                 if not ln.strip().startswith("YIBAN_REGISTRATION_PAUSE=")]
        if value is not None:
            lines.append(f"YIBAN_REGISTRATION_PAUSE={value}".rstrip())
        with open(self.env_file, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

    def _login(self, c, username, password):
        r = c.post("/api/login", json={"username": username, "password": password})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return c.get("/api/me").get_json()["csrf_token"]

    def _csrf(self, token):
        return {"X-CSRF-Token": token}

    def _mk_admin(self, email="admin@test.local"):
        if db.find_user(email) is None:
            db.create_user(email, self.webapp.generate_password_hash(USER_PASS),
                           role="admin")

    # ---- 注册开关 ----
    def test_register_allowed_by_default(self):
        """未配置（默认）→ 开放注册。"""
        c = self.webapp.create_app().test_client()
        r = c.post("/api/register", json={
            "email": "new1@test.local", "password": "RegPass1234!", "agree": True})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertIsNotNone(db.find_user("new1@test.local"))

    def test_register_rejected_when_paused(self):
        """YIBAN_REGISTRATION_PAUSE=1 → 403，用户不落库。"""
        self._set_pause_env("1")
        c = self.webapp.create_app().test_client()
        r = c.post("/api/register", json={
            "email": "new2@test.local", "password": "RegPass1234!", "agree": True})
        self.assertEqual(r.status_code, 403, r.get_data(as_text=True))
        self.assertIn("注册已暂停", r.get_json()["error"])
        self.assertIsNone(db.find_user("new2@test.local"))

    def test_register_reenabled_after_pause_cleared(self):
        """暂停值清除（删键/空）→ 恢复开放（读侧默认 0）。"""
        self._set_pause_env("1")
        self._set_pause_env("")
        c = self.webapp.create_app().test_client()
        r = c.post("/api/register", json={
            "email": "new3@test.local", "password": "RegPass1234!", "agree": True})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

    def test_paused_registration_does_not_block_login(self):
        """暂停注册期间已注册用户照常登录（用户裁决：仅关注册入口）。"""
        db.create_user("u1@test.local",
                       self.webapp.generate_password_hash(USER_PASS))
        self._set_pause_env("1")
        c = self.webapp.create_app().test_client()
        r = c.post("/api/login", json={
            "username": "u1@test.local", "password": USER_PASS})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json().get("role"), "user")

    def test_paused_registration_keeps_admin_add_account(self):
        """暂停注册不影响管理员添加账号路径的自动建用户（admin 手动添加通道）。"""
        self._set_pause_env("1")
        c = self.webapp.create_app().test_client()
        token = self._login(c, "admin", ADMIN_PASS)
        r = c.post("/api/accounts", json={
            "phone": "13800138000", "password": "accpass", "owner": "manual@test.local",
            "initial_password": "ManualPass123!",
        }, headers=self._csrf(token))
        # 具体字段以现有添加账号 API 为准：仅断言未因注册暂停被拦（非 403 注册文案）
        body = r.get_data(as_text=True)
        self.assertNotIn("注册已暂停", body, "管理员添加账号不应受注册暂停影响")

    # ---- 公开端点 ----
    def test_public_endpoint_reflects_state(self):
        c = self.webapp.create_app().test_client()
        self.assertFalse(c.get("/api/registration_paused").get_json()["paused"])
        self._set_pause_env("1")
        self.assertTrue(c.get("/api/registration_paused").get_json()["paused"])

    # ---- 设置接口 ----
    def test_settings_get_includes_registration_pause(self):
        c = self.webapp.create_app().test_client()
        token = self._login(c, "admin", ADMIN_PASS)
        r = c.get("/api/settings", headers=self._csrf(token))
        self.assertEqual(r.status_code, 200)
        self.assertIn("registration_pause", r.get_json())

    def test_master_admin_can_toggle_pause(self):
        c = self.webapp.create_app().test_client()
        token = self._login(c, "admin", ADMIN_PASS)
        r = c.post("/api/settings", json={"registration_pause": 1},
                   headers=self._csrf(token))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        with open(self.env_file, encoding="utf-8") as f:
            self.assertIn("YIBAN_REGISTRATION_PAUSE=1", f.read())
        r2 = c.post("/api/settings", json={"registration_pause": 0},
                    headers=self._csrf(token))
        self.assertEqual(r2.status_code, 200)
        with open(self.env_file, encoding="utf-8") as f:
            self.assertNotIn("YIBAN_REGISTRATION_PAUSE=1", f.read())

    def test_regular_admin_cannot_toggle_pause(self):
        """普通管理员改注册暂停 → 403（与 global_pause 同权限口径）。"""
        self._mk_admin("regadmin@test.local")
        c = self.webapp.create_app().test_client()
        token = self._login(c, "regadmin@test.local", USER_PASS)
        r = c.post("/api/settings", json={"registration_pause": 1},
                   headers=self._csrf(token))
        self.assertEqual(r.status_code, 403, r.get_data(as_text=True))
        self.assertIn("仅主管理员", r.get_json()["error"])

    # ---- ensure_secret_key 新部署默认值 ----
    def test_fresh_deployment_writes_pause(self):
        """.env 不存在（全新部署）→ ensure_secret_key 创建时默认写入暂停键。"""
        fresh_env = os.path.join(self.tmp, "fresh.env")
        self.assertFalse(os.path.exists(fresh_env))
        self.webapp.ensure_secret_key(fresh_env)
        with open(fresh_env, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("YIBAN_SECRET_KEY=", content)
        self.assertIn("YIBAN_REGISTRATION_PAUSE=1", content)
        os.remove(fresh_env)

    def test_existing_deployment_not_touched(self):
        """.env 已存在（既有部署，缺该键）→ ensure_secret_key 不写暂停键。"""
        exist_env = os.path.join(self.tmp, "exist.env")
        with open(exist_env, "w", encoding="utf-8") as f:
            f.write("YIBAN_ADMIN_USER=admin\n")
        self.webapp.ensure_secret_key(exist_env)
        with open(exist_env, encoding="utf-8") as f:
            self.assertNotIn("YIBAN_REGISTRATION_PAUSE", f.read())
        os.remove(exist_env)

    # ---- 日志落盘（v0.26.3 缺口修复）----
    def test_root_logger_writes_daily_file(self):
        """create_app 后 root logger 挂 _DailyFlockFileHandler 且 INFO 落入 sign-*.log。"""
        import logging
        from datetime import datetime
        c = self.webapp.create_app().test_client()
        c.get("/api/registration_paused")  # 触发一条请求级日志路径
        root = logging.getLogger()
        fh = [h for h in root.handlers
              if type(h).__name__ == "_DailyFlockFileHandler"]
        self.assertTrue(fh, "create_app 应为 root 挂载按天文件 handler")
        # 批次16 P3：root 保持 WARNING（防 requests/urllib3/werkzeug 等第三方 INFO
        # 全量落盘且无轮转上限），仅自有组件单独放开 INFO
        self.assertEqual(root.level, logging.WARNING, "root 应保持 WARNING")
        self.assertEqual(logging.getLogger("web").level, logging.INFO,
                         "自有组件 web 应放开 INFO（否则 INFO 全被丢弃）")
        today = datetime.now().strftime("%Y-%m-%d")
        log_path = os.path.join(self.log_dir, f"sign-{today}.log")
        self.assertTrue(os.path.exists(log_path), "按天日志文件应已创建")
        with open(log_path, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("web:", content, "web logger 的 INFO 应落盘（此前被丢弃）")

    def test_log_page_shows_component_warnings(self):
        """后台日志页解析：非 yiban 组件仅 WARNING+ 入列（INFO 仍不展示）。"""
        parse = self.webapp.parse_sign_log
        yiban_info = "[2026-09-01 10:00:00] [INFO] yiban: 签到正常"
        comp_warn = "[2026-09-01 10:00:01] [WARNING] mailer: 邮件通知发送失败（SMTPAuthenticationError）"
        comp_info = "[2026-09-01 10:00:02] [INFO] notify: 消息推送已发送（serverchan）: t"
        comp_debug = "[2026-09-01 10:00:03] [DEBUG] yiban: 探针跳过"
        tmp_log = os.path.join(self.tmp, "parse-probe.log")
        with open(tmp_log, "w", encoding="utf-8") as f:
            f.write("\n".join([yiban_info, comp_warn, comp_info, comp_debug]) + "\n")
        with unittest.mock.patch.object(self.webapp, "_tail_lines",
                                        return_value=[yiban_info, comp_warn,
                                                      comp_info, comp_debug]), \
             unittest.mock.patch.object(self.webapp, "log_path_for",
                                        return_value=tmp_log):
            lines = parse(tmp_log)
        self.assertIn(yiban_info, lines)
        self.assertIn(comp_warn, lines, "组件 WARNING 应展示（故障留痕）")
        self.assertNotIn(comp_info, lines, "组件 INFO 不展示（维持日志页洁净）")
        self.assertNotIn(comp_debug, lines)


import unittest.mock  # noqa: E402  （置于文件尾部：仅测试方法内使用）

if __name__ == "__main__":
    unittest.main()
