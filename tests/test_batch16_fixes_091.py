# -*- coding: utf-8 -*-
"""批次16（2026-09-01）对抗性审查修复验证。

覆盖：
- P1-2 批量/单条重置密码二次鉴权：普通管理员无 confirm_password 被门禁拦；
  带正确 confirm_password 成功；普通用户自改密码（/api/me/password）不受门禁影响；
- P2-1 _DailyFlockFileHandler：跨天滚动 + 目录故障（_open 抛 OSError）不传播到
  调用方；下一条日志仍可重试；create_app 挂载构造失败降级不崩启动；
- P2-7 ensure_secret_key：空 .env（无有效键）视为新部署写暂停键；有有效键不写；
- 版本号同步：APP_VERSION == "0.26.3"。

全程 mock / 纯本地（Flask test client），无任何网络请求。
用法（项目根目录）：
    python -m pytest tests/test_batch16_fixes_091.py -v
"""
import contextlib
import importlib.util
import json
import logging
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "scripts"))
sys.path.insert(0, os.path.join(BASE, "web"))

TEST_KEY = "a" * 64
ADMIN_PASS = "TestPass1234!"
USER_PASS = "UserPass123!"
NEW_PASS = "NewPass123!"


class Batch16FixesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-batch16-")
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
        self._set_pause_env(None)

    def _set_pause_env(self, value):
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

    def _mk_user(self, email, role="user", password=USER_PASS):
        if db.find_user(email) is None:
            db.create_user(email, self.webapp.generate_password_hash(password),
                           role=role)

    def _pw_version(self, email):
        u = db.find_user(email)
        return u.get("pw_version", 1) if u else None

    # ---- P1-2 批量重置密码二次鉴权 ----
    def test_batch_reset_requires_confirm(self):
        """普通管理员批量重置无 confirm_password → 400 被门禁拦，密码不变。"""
        self._mk_user("regadmin@test.local", role="admin", password=ADMIN_PASS)
        self._mk_user("user1@test.local")
        c = self.webapp.create_app().test_client()
        token = self._login(c, "regadmin@test.local", ADMIN_PASS)
        r = c.post("/api/users/batch",
                   json={"action": "reset_password", "emails": ["user1@test.local"],
                         "password": NEW_PASS},
                   headers=self._csrf(token))
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        self.assertIn("当前密码不正确", r.get_json()["error"])
        self.assertEqual(self._pw_version("user1@test.local"), 1, "密码不应被重置")
        # 旧密码仍可登录（未被动）
        c1 = self.webapp.create_app().test_client()
        r1 = c1.post("/api/login", json={"username": "user1@test.local",
                                         "password": USER_PASS})
        self.assertEqual(r1.status_code, 200, "旧密码应仍有效")

    def test_batch_reset_ok_with_confirm(self):
        """普通管理员批量重置带正确 confirm_password → 成功。"""
        self._mk_user("regadmin@test.local", role="admin", password=ADMIN_PASS)
        self._mk_user("user1@test.local")
        c = self.webapp.create_app().test_client()
        token = self._login(c, "regadmin@test.local", ADMIN_PASS)
        r = c.post("/api/users/batch",
                   json={"action": "reset_password", "emails": ["user1@test.local"],
                         "password": NEW_PASS, "confirm_password": ADMIN_PASS},
                   headers=self._csrf(token))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(self._pw_version("user1@test.local"), 2)
        c1 = self.webapp.create_app().test_client()
        r1 = c1.post("/api/login", json={"username": "user1@test.local",
                                         "password": NEW_PASS})
        self.assertEqual(r1.status_code, 200, "新密码应可登录")

    def test_single_reset_requires_confirm(self):
        """普通管理员单条重置无 confirm_password → 400；带正确 → 200。"""
        self._mk_user("regadmin@test.local", role="admin", password=ADMIN_PASS)
        self._mk_user("user1@test.local")
        c = self.webapp.create_app().test_client()
        token = self._login(c, "regadmin@test.local", ADMIN_PASS)
        r = c.post("/api/users/user1@test.local/password",
                   json={"password": NEW_PASS}, headers=self._csrf(token))
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        self.assertEqual(self._pw_version("user1@test.local"), 1)
        r2 = c.post("/api/users/user1@test.local/password",
                    json={"password": NEW_PASS, "confirm_password": ADMIN_PASS},
                    headers=self._csrf(token))
        self.assertEqual(r2.status_code, 200, r2.get_data(as_text=True))
        self.assertEqual(self._pw_version("user1@test.local"), 2)

    def test_self_change_password_unaffected(self):
        """普通用户自改密码（/api/me/password，需旧密码）不受门禁影响。"""
        self._mk_user("user1@test.local")
        c = self.webapp.create_app().test_client()
        token = self._login(c, "user1@test.local", USER_PASS)
        r = c.post("/api/me/password",
                   json={"old_password": USER_PASS, "new_password": NEW_PASS,
                         "confirm_password": NEW_PASS},
                   headers=self._csrf(token))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        c1 = self.webapp.create_app().test_client()
        r1 = c1.post("/api/login", json={"username": "user1@test.local",
                                         "password": NEW_PASS})
        self.assertEqual(r1.status_code, 200, "新密码应可登录")
        # 旧密码校验路径仍生效：错误旧密码 400
        c2 = self.webapp.create_app().test_client()
        token2 = self._login(c2, "user1@test.local", NEW_PASS)
        r2 = c2.post("/api/me/password",
                     json={"old_password": "wrong", "new_password": "Another123!",
                           "confirm_password": "Another123!"},
                     headers=self._csrf(token2))
        self.assertEqual(r2.status_code, 400, r2.get_data(as_text=True))

    # ---- P2-1 日志 handler 异常不传播 ----
    def _mk_handler(self):
        os.makedirs(self.log_dir, exist_ok=True)
        return self.webapp._DailyFlockFileHandler(self.log_dir)

    def test_emit_rollover_oserror_not_propagated(self):
        """跨天滚动时 _open 抛 OSError → emit 不向调用方抛异常。"""
        h = self._mk_handler()
        h._day = "2000-01-01"  # 强制触发跨天滚动分支
        record = logging.LogRecord("web", logging.INFO, __file__, 1,
                                   "batch16 test msg", None, None)
        with mock.patch.object(h, "_open", side_effect=OSError("dir gone")), \
             mock.patch.object(h, "handleError") as mh:
            h.emit(record)  # 不应抛异常
        mh.assert_called_once_with(record)

    def test_emit_retries_after_failure(self):
        """异常后下一条日志仍重试 _open：目录恢复后写入成功。"""
        h = self._mk_handler()
        h._day = "2000-01-01"
        record = logging.LogRecord("web", logging.INFO, __file__, 1,
                                   "batch16 retry msg", None, None)
        with mock.patch.object(h, "_open", side_effect=OSError("dir gone")), \
             mock.patch.object(h, "handleError"):
            h.emit(record)  # 失败降级
        h.emit(record)  # 目录已"恢复"，重试应成功
        from datetime import datetime
        today = datetime.now().strftime("%Y-%m-%d")
        path = os.path.join(self.log_dir, f"sign-{today}.log")
        self.assertTrue(os.path.exists(path), "重试后日志文件应已创建")
        with open(path, encoding="utf-8") as f:
            self.assertIn("batch16 retry msg", f.read())
        h.close()

    def test_create_app_degrades_on_handler_construct_failure(self):
        """_DailyFlockFileHandler 构造失败 → create_app 仅告警不崩启动。"""
        root = logging.getLogger()
        # 清掉 root 上残留的 flock 文件 handler（模块导入时 signin 会挂基类
        # _FlockFileHandler，既有 create_app 可能挂 _DailyFlockFileHandler 子类）
        for _h in list(root.handlers):
            if type(_h).__name__ in ("_FlockFileHandler", "_DailyFlockFileHandler"):
                root.removeHandler(_h)
                with contextlib.suppress(Exception):
                    _h.close()

        # 用"构造即抛 OSError 的真实子类"替换模块类名：既让 create_app 的
        # isinstance 判定保持合法（mock 的 MagicMock 不是类型会崩），又能触发
        # 构造降级路径
        class _ExplodingDailyFh(self.webapp._DailyFlockFileHandler):
            def __init__(self, log_dir):
                raise OSError("no such dir")

        with mock.patch.object(self.webapp, "_DailyFlockFileHandler",
                               _ExplodingDailyFh), \
             mock.patch.object(self.webapp.logger, "warning") as mw:
            app = self.webapp.create_app()  # 不应抛异常
            self.assertIsNotNone(app)
            self.assertTrue(mw.called, "应记录降级告警")

    # ---- P2-7 ensure_secret_key 新部署判定 ----
    def test_empty_env_file_treated_as_fresh(self):
        """空 .env（touch 后无有效键）→ 视为新部署，写 YIBAN_REGISTRATION_PAUSE=1。"""
        empty_env = os.path.join(self.tmp, "empty.env")
        with open(empty_env, "w", encoding="utf-8"):
            pass  # 空文件（模拟部署者 touch 或复制 .env.example 后未配置）
        self.webapp.ensure_secret_key(empty_env)
        with open(empty_env, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("YIBAN_SECRET_KEY=", content)
        self.assertIn("YIBAN_REGISTRATION_PAUSE=1", content,
                      "空 .env 应视为新部署写暂停键")
        os.remove(empty_env)

    def test_env_with_valid_key_not_treated_as_fresh(self):
        """有有效键的 .env → 既有部署，不写暂停键。"""
        exist_env = os.path.join(self.tmp, "exist.env")
        with open(exist_env, "w", encoding="utf-8") as f:
            f.write("YIBAN_ADMIN_USER=admin\nYIBAN_ADMIN_PASSWORD=x12345678!\n")
        self.webapp.ensure_secret_key(exist_env)
        with open(exist_env, encoding="utf-8") as f:
            self.assertNotIn("YIBAN_REGISTRATION_PAUSE", f.read())
        os.remove(exist_env)

    # ---- 版本号 ----
    def test_version_synced(self):
        """APP_VERSION 与 web/__init__.py __version__ 同步为 0.26.3。"""
        self.assertEqual(self.webapp.APP_VERSION, "0.26.3")
        with open(os.path.join(BASE, "web", "__init__.py"), encoding="utf-8") as f:
            self.assertIn('__version__ = "0.26.3"', f.read())


import unittest.mock  # noqa: E402

if __name__ == "__main__":
    unittest.main()
