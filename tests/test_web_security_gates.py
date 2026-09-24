# -*- coding: utf-8 -*-
"""对抗性审查修复验证（2026-09-01）。

覆盖：
- P1-2 批量/单条重置密码二次鉴权：普通管理员无 confirm_password 被门禁拦；
  带正确 confirm_password 成功；普通用户自改密码（/api/me/password）不受门禁影响；
- P2-1 DailyFlockFileHandler：跨天滚动 + 目录故障（_open 抛 OSError）不传播到
  调用方；下一条日志仍可重试；create_app 挂载构造失败降级不崩启动；
- P2-7 ensure_secret_key：空 .env（无有效键）视为新部署写暂停键；有有效键不写；
- 版本号同步：APP_VERSION 与 web/__init__.py 的 __version__ 一致且等于当前版本
  （断言值随发版更新，当前 0.4.5）。

全程 mock / 纯本地（Flask test client），无任何网络请求。
用法（项目根目录）：
    python -m pytest tests/test_web_security_gates.py -v
"""
import contextlib
import importlib.util
import io
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

from _mail_body import render_body

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TEST_KEY = "a" * 64
ADMIN_PASS = "TestPass1234!"
USER_PASS = "UserPass123!"
NEW_PASS = "NewPass123!"
# 门禁档位：本文件多数用例钉的是"当次要口令"这一层的机制（档位门、冷却、豁免、
# 变更告警），必须显式固定在 full——默认档是 risk，不固定则这些动作不再当次要口令。
# 默认档与 off 档的行为由 tests/test_pw_gate_tiers.py 钉。
GATE_FULL = "full"


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
                # 本文件钉的是口令门的**机制**（当次要口令、豁免、冷却、变更告警），
                # 故把档位固定在 full（默认档 risk 下这些动作不再当次要口令）
                f"YIBAN_PW_GATE={GATE_FULL}\n"
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
        self.assertEqual(r.get_json()["reason"], "password_required")
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
        return self.webapp.DailyFlockFileHandler(self.log_dir)

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
        """DailyFlockFileHandler 构造失败 → create_app 仅告警不崩启动。"""
        root = logging.getLogger()
        # 清掉 root 上残留的 flock 文件 handler（模块导入时 signin 会挂基类
        # _FlockFileHandler，既有 create_app 可能挂 DailyFlockFileHandler 子类）
        for _h in list(root.handlers):
            if type(_h).__name__ in ("FlockFileHandler", "DailyFlockFileHandler"):
                root.removeHandler(_h)
                with contextlib.suppress(Exception):
                    _h.close()

        # 用"构造即抛 OSError 的真实子类"替换模块类名：既让 create_app 的
        # isinstance 判定保持合法（mock 的 MagicMock 不是类型会崩），又能触发
        # 构造降级路径
        class _ExplodingDailyFh(self.webapp.DailyFlockFileHandler):
            def __init__(self, log_dir):
                raise OSError("no such dir")

        with mock.patch.object(self.webapp, "DailyFlockFileHandler",
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
        """版本号只有一个来源：`yiban/__init__.py`；web 侧引用它，不得各写一份字面量。

        发布门槛要用"引擎轮次横幅里的版本号"把生产跑过的轮次与提交对齐
        （见 docs/dev/release-gate.md），版本一旦分叉，台账就不再可信。
        """
        from yiban import __version__ as engine_version
        self.assertRegex(engine_version, r"^\d+\.\d+\.\d+$")
        self.assertEqual(self.webapp.APP_VERSION, engine_version)
        # web 侧两个文件都不得再出现版本字面量（否则就是第二个来源）
        for rel in ("web/__init__.py", "web/app.py"):
            with open(os.path.join(BASE, rel), encoding="utf-8") as f:
                self.assertNotIn(
                    f'"{engine_version}"', f.read(),
                    f"{rel} 里又写了一份版本字面量；版本只能定义在 yiban/__init__.py",
                )


import unittest.mock  # noqa: E402

REG_ADMIN = "reg-admin@test.local"


REG_PASS = "RegPass5678!"


USER_EMAIL = "stud@test.local"


USER_PASS_ANN = "StudPass123!"


DRAFT_RE = re.compile(r"^[^|\n]{1,64}\|\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


class _AnnBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-ann-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(
                f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
                f"YIBAN_ADMIN_USER=admin\nYIBAN_ADMIN_PASSWORD={ADMIN_PASS}\n"
                # 本文件钉的是口令门的**机制**（当次要口令、豁免、冷却、变更告警），
                # 故把档位固定在 full（默认档 risk 下这些动作不再当次要口令）
                f"YIBAN_PW_GATE={GATE_FULL}\n"
            )
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.accounts_file = os.path.join(cls.tmp, "accounts.json")
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_ACCOUNTS_FILE"] = cls.accounts_file
        os.environ["YIBAN_USERS_FILE"] = os.path.join(cls.tmp, "users.json")
        os.environ["YIBAN_DB_FILE"] = cls.db_file
        os.environ["YIBAN_STATE_DIR"] = cls.tmp
        os.environ["YIBAN_LOG_FILE"] = os.path.join(cls.tmp, "sign.log")
        os.environ["YIBAN_DISABLE_PURGE_LOOP"] = "1"
        global db
        import db
        # 独立名字加载 web/app.py：它在导入期把 ENV_FILE 等读成模块级常量，与别的测试
        # 文件共用同一模块对象会读到另一个 .env（单跑绿、全量红的老坑）
        spec = importlib.util.spec_from_file_location(
            "webapp_ann_publish", os.path.join(BASE, "web", "app.py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules["webapp_ann_publish"] = mod
        with contextlib.suppress(Exception):
            spec.loader.exec_module(mod)
        cls.webapp = mod

    @classmethod
    def tearDownClass(cls):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k in ("YIBAN_ACCOUNTS_KEY", "YIBAN_ENV_FILE", "YIBAN_ACCOUNTS_FILE",
                  "YIBAN_USERS_FILE", "YIBAN_DB_FILE", "YIBAN_STATE_DIR",
                  "YIBAN_LOG_FILE", "YIBAN_DISABLE_PURGE_LOOP"):
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
        with open(self.env_file, "w", encoding="utf-8") as f:
            f.write(
                f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
                f"YIBAN_ADMIN_USER=admin\nYIBAN_ADMIN_PASSWORD={ADMIN_PASS}\n"
                f"YIBAN_PW_GATE={GATE_FULL}\n"
            )
        self.webapp.ENV_FILE = self.env_file
        # 公告缓存是模块级全局：不清零会把上一例的已发布文本带进本例
        self.webapp._announcement_cache[0] = None
        db.init_db(self.db_file, migrate_from=self.accounts_file, env_file=self.env_file)
        self.alerts = []
        patcher = mock.patch.object(
            self.webapp, "send_notification",
            side_effect=lambda t, c, urgent=False, force=False, ledger=None:
            self.alerts.append((t, render_body(c), urgent, force)))
        patcher.start()
        self.addCleanup(patcher.stop)
        # .env 每一次原子落盘的全文快照（判定"发布是否一次写完"用）
        self.writes = []
        real_atomic = self.webapp._atomic_write
        wp = mock.patch.object(
            self.webapp, "_atomic_write",
            side_effect=lambda path, text, **kw: (
                self.writes.append(text), real_atomic(path, text, **kw))[0])
        wp.start()
        self.addCleanup(wp.stop)

    # ---- 会话与探针 ----
    def _login(self, username, password):
        c = self.webapp.create_app().test_client()
        r = c.post("/api/login", json={"username": username, "password": password})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        c.csrf = c.get("/api/me").get_json()["csrf_token"]
        return c

    def _master(self):
        return self._login("admin", ADMIN_PASS)

    def _reg_admin(self):
        db.create_user(REG_ADMIN, self.webapp.generate_password_hash(REG_PASS), role="admin")
        return self._login(REG_ADMIN, REG_PASS)

    def _user(self):
        db.create_user(USER_EMAIL, self.webapp.generate_password_hash(USER_PASS_ANN))
        return self._login(USER_EMAIL, USER_PASS_ANN)

    def _anon(self):
        return self.webapp.create_app().test_client()

    def _hdr(self, c):
        return {"X-CSRF-Token": c.csrf}

    def _put_draft(self, c, text):
        return c.put("/api/announcement", json={"text": text}, headers=self._hdr(c))

    def _publish(self, c, password=ADMIN_PASS):
        body = {"confirm_password": password} if password is not None else {}
        return c.post("/api/announcement/publish", json=body, headers=self._hdr(c))

    def _env(self):
        return self.webapp.read_env(self.env_file)

    def _raw_env(self):
        with open(self.env_file, encoding="utf-8-sig") as f:
            return f.read()

    def _audit(self, action):
        return [dict(r) for r in db.get_conn().execute(
            "SELECT username, action, target, detail FROM audit_logs WHERE action=?",
            (action,)).fetchall()]


class DraftWriteTest(_AnnBase):
    """PUT = 写草稿：管理员权限与既有校验不变，已发布面一分不动。"""

    def test_put_writes_draft_not_published(self):
        c = self._reg_admin()
        r = self._put_draft(c, "服务器今晚 23:00 维护")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(r.get_json()["ok"])
        env = self._env()
        self.assertEqual(env.get("YIBAN_ANNOUNCEMENT_DRAFT"), "服务器今晚 23:00 维护")
        self.assertNotIn("YIBAN_ANNOUNCEMENT", env)
        self.assertRegex(env.get("YIBAN_ANNOUNCEMENT_DRAFT_META", ""), DRAFT_RE)

    def test_put_keeps_published_text_and_cache_untouched(self):
        m = self._master()
        self._put_draft(m, "旧公告")
        self.assertEqual(self._publish(m).status_code, 200)
        published_cache_before = self.webapp._announcement_cache[0]
        sub = self._reg_admin()
        self.assertEqual(self._put_draft(sub, "新公告").status_code, 200)
        # 已发布键、公告缓存、匿名可见文本三者都必须仍是旧公告
        self.assertEqual(self._env().get("YIBAN_ANNOUNCEMENT"), "旧公告")
        self.assertEqual(self.webapp._announcement_cache[0], published_cache_before)
        self.assertEqual(self.webapp._announcement_cache[0], "旧公告")
        self.assertEqual(self._anon().get("/api/announcement").get_json()["text"], "旧公告")

    def test_put_validation_still_rejects_too_long_and_breaks(self):
        c = self._reg_admin()
        r = self._put_draft(c, "长" * 201)
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        self.assertIn("过长", r.get_json()["error"])
        for ch in ("\n", "\r", "\v", "\x1c", "\x85", "\u2028", "\u2029"):
            with self.subTest(ch=hex(ord(ch))):
                r = self._put_draft(c, f"正常{ch}YIBAN_ADMIN_PASSWORD_HASH=scrypt:fake")
                self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
                err = r.get_json()["error"]
                self.assertIn("行分隔符", err, f"400 须来自行分隔符守卫：{err}")
                self.assertNotIn("过长", err)
        self.assertNotIn("YIBAN_ANNOUNCEMENT_DRAFT", self._raw_env())
        self.assertNotIn("scrypt:fake", self._raw_env())
        self.assertEqual(self._raw_env().count("YIBAN_ADMIN_PASSWORD_HASH="), 1)

    def test_put_clears_draft_and_meta_together(self):
        c = self._reg_admin()
        self._put_draft(c, "待发布内容")
        self.assertEqual(self._put_draft(c, "").status_code, 200)
        self.assertNotIn("YIBAN_ANNOUNCEMENT_DRAFT", self._raw_env())
        self.assertNotIn("YIBAN_ANNOUNCEMENT_DRAFT_META", self._raw_env())

    def test_put_alert_says_pending_and_is_not_urgent(self):
        c = self._reg_admin()
        self._put_draft(c, "维护通知")
        self.assertEqual(len(self.alerts), 1, f"草稿写入应恰好一条告警，实际 {self.alerts}")
        title, body, urgent, force = self.alerts[0]
        self.assertEqual(title, "公告变更告警")
        self.assertFalse(urgent, "草稿不是对外可见状态，不得占用紧急账")
        self.assertFalse(force)
        self.assertIn("公告草稿已更新", body)
        self.assertIn("待主管理员发布", body)
        self.assertIn(REG_ADMIN, body)

    def test_draft_audit_marks_pending(self):
        c = self._reg_admin()
        self._put_draft(c, "维护通知")
        rows = self._audit("announcement_draft_save")
        self.assertEqual(len(rows), 1, f"草稿写入须留一条审计，实际 {self._audit('announcement')}")
        self.assertEqual(rows[0]["username"], REG_ADMIN)
        self.assertIn("待发布", rows[0]["detail"])
        self.assertIn("维护通知", rows[0]["detail"])
        self.assertEqual(self._audit("announcement_publish"), [], "写草稿不得记成发布")


class GetShapeTest(_AnnBase):
    """GET 的公开形态是契约；草稿只对管理员会话可见。"""

    def test_anonymous_response_has_only_ok_and_text(self):
        self._put_draft(self._reg_admin(), "草稿内容")   # 最坏情形：确有草稿待发布
        cases = {"匿名": self._anon(), "普通用户": self._user()}
        for label, c in cases.items():
            with self.subTest(label):
                body = c.get("/api/announcement").get_json()
                self.assertEqual(set(body), {"ok", "text"},
                                 f"{label}侧响应键集合必须不变（草稿键不得外泄）")
                self.assertIs(body["ok"], True)
                self.assertEqual(body["text"], "")

    def test_admin_get_sees_draft_keys(self):
        sub = self._reg_admin()
        self._put_draft(sub, "草稿内容")
        m = self._master()
        body = m.get("/api/announcement", headers=self._hdr(m)).get_json()
        self.assertEqual(set(body), {"ok", "text", "draft", "draft_by", "draft_at",
                                     "published_by", "published_at"})
        self.assertEqual(body["text"], "")
        self.assertEqual(body["draft"], "草稿内容")
        self.assertEqual(body["draft_by"], REG_ADMIN)
        self.assertRegex(body["draft_at"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
        # 从未发布过：两键存在但为空串（前端据此显示"线上暂无公告"而不是"未知"）
        self.assertEqual(body["published_by"], "")
        self.assertEqual(body["published_at"], "")

    def test_draft_never_leaks_into_public_text_even_when_published_is_empty(self):
        self._put_draft(self._reg_admin(), "只有草稿")
        self.assertEqual(self._anon().get("/api/announcement").get_json()["text"], "")

    def test_broken_meta_degrades_to_unavailable(self):
        c = self._reg_admin()
        self._put_draft(c, "草稿内容")
        for bad in ("", "no-pipe", "a|b|c", "x@y.local|2026-13-45 99:99:99",
                    "x@y.local|昨天", "\x00|x"):
            with self.subTest(bad=bad):
                self.webapp.write_env_batch(
                    self.env_file, {"YIBAN_ANNOUNCEMENT_DRAFT_META": bad})
                r = c.get("/api/announcement", headers=self._hdr(c))
                self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
                body = r.get_json()
                self.assertEqual(body["draft"], "草稿内容", "草稿正文与元数据解析互不牵连")
                self.assertEqual(body["draft_by"], "")
                self.assertEqual(body["draft_at"], "")


class PublishAuthorizationTest(_AnnBase):
    """发布面：仅主管理员 + 当次口令。"""

    def test_anonymous_and_user_and_regular_admin_denied(self):
        self._put_draft(self._reg_admin(), "草稿")
        anon = self._anon()
        self.assertEqual(anon.post("/api/announcement/publish", json={}).status_code, 401)
        u = self._user()
        self.assertEqual(self._publish(u, None).status_code, 403)
        sub = self._reg_admin()
        r = self._publish(sub, REG_PASS)   # 普通管理员带自己的正确口令
        self.assertEqual(r.status_code, 403, r.get_data(as_text=True))
        self.assertNotIn("YIBAN_ANNOUNCEMENT", self._env(), "越界发布不得落盘")
        self.assertEqual([a for a in self.alerts if a[0] == "公告发布告警"], [],
                         "被拒的越界发布不得发出发布告警（草稿写入那条变更告警是应有之义）")
        self.assertEqual(self._audit("announcement_publish"), [])

    def test_master_without_password_denied_even_right_after_exempt_save(self):
        m = self._master()
        self._put_draft(m, "草稿")
        # 先走一次"可豁免"的配置类复核，令会话进入短时豁免窗口
        r = m.post("/api/settings", json={"sign_order": "random",
                                          "confirm_password": ADMIN_PASS},
                   headers=self._hdr(m))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(m.get("/api/announcement", headers=self._hdr(m)))
        r = self._publish(m, None)
        self.assertEqual(r.status_code, 403,
                         f"发布必须吃当次口令，短时豁免不得覆盖：{r.get_data(as_text=True)}")
        self.assertNotIn("YIBAN_ANNOUNCEMENT", self._env())
        r = self._publish(m, "wrong-password")
        self.assertEqual(r.status_code, 403, r.get_data(as_text=True))
        self.assertNotIn("YIBAN_ANNOUNCEMENT", self._env())


class PublishEffectTest(_AnnBase):
    """发布 = 一次原子写：正式=草稿、草稿清空。"""

    def test_publish_promotes_draft_in_a_single_atomic_write(self):
        self._put_draft(self._reg_admin(), "今晚 23:00 维护")
        m = self._master()
        # 口令复核会读 .env，但不写；发布这一次是本轮唯一的落盘
        self.assertEqual(m.get("/api/announcement", headers=self._hdr(m)).status_code, 200)
        before = len(self.writes)
        r = self._publish(m)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(r.get_json()["ok"])
        mine = self.writes[before:]
        self.assertEqual(len(mine), 1,
                         f"发布必须一次 write_env_batch 完成，实际 {len(mine)} 次落盘")
        self.assertIn("YIBAN_ANNOUNCEMENT=今晚 23:00 维护", mine[0])
        self.assertNotIn("YIBAN_ANNOUNCEMENT_DRAFT", mine[0],
                         "落盘内容里仍留草稿键 = 存在「正式已清、草稿未落」的中间态")
        env = self._env()
        self.assertEqual(env.get("YIBAN_ANNOUNCEMENT"), "今晚 23:00 维护")
        self.assertNotIn("YIBAN_ANNOUNCEMENT_DRAFT", env)
        self.assertNotIn("YIBAN_ANNOUNCEMENT_DRAFT_META", env)
        self.assertEqual(self._anon().get("/api/announcement").get_json()["text"],
                         "今晚 23:00 维护")
        self.assertEqual(self.webapp._announcement_cache[0], "今晚 23:00 维护")

    def test_publish_overwrites_previous_and_draft_gone_afterwards(self):
        m = self._master()
        self._put_draft(m, "第一条")
        self.assertEqual(self._publish(m).status_code, 200)
        self.assertEqual(self._raw_env().count("YIBAN_ANNOUNCEMENT="), 1,
                         "覆盖不得留下第二行已发布键")
        self._put_draft(m, "第二条")
        self.assertEqual(self._publish(m).status_code, 200)
        self.assertEqual(self._env().get("YIBAN_ANNOUNCEMENT"), "第二条")
        self.assertEqual(self._anon().get("/api/announcement").get_json()["text"], "第二条")
        # 发布后草稿已被清空，再点一次 = 下线（空草稿 + 线上有内容的裁决语义）。
        # 防误按的不是报错，而是这条通道每次都要求当次口令、不吃短时豁免。
        r_again = self._publish(m)
        self.assertEqual(r_again.status_code, 200, r_again.get_data(as_text=True))
        self.assertIn("下线", r_again.get_json()["msg"])
        self.assertNotEqual(self._env().get("YIBAN_ANNOUNCEMENT"), "第二条")
        self.assertEqual(self._anon().get("/api/announcement").get_json()["text"], "")
        self.assertEqual(self._env().get("YIBAN_ANNOUNCEMENT_PUBLISHED_META"), None)

    def test_publish_without_draft_is_400_not_silent_success(self):
        m = self._master()
        r = self._publish(m)
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        self.assertIn("草稿", r.get_json()["error"])
        self.assertNotIn("YIBAN_ANNOUNCEMENT", self._env())
        self.assertEqual(self.alerts, [])
        self.assertEqual(self._audit("announcement_publish"), [])
        # 草稿被普通管理员清空后同样算"无草稿"
        self._put_draft(self._reg_admin(), "内容")
        self._put_draft(self._reg_admin(), "")
        self.assertEqual(self._publish(m).status_code, 400)

    def test_publish_alert_is_urgent_and_describes_the_change(self):
        m = self._master()
        self._put_draft(self._reg_admin(), "全体注意：今晚维护")
        self._publish(m)
        rows = [a for a in self.alerts if a[0] == "公告发布告警"]
        self.assertEqual(len(rows), 1, f"发布应恰好一条发布告警，实际 {self.alerts}")
        title, body, urgent, force = rows[0]
        self.assertEqual(title, "公告发布告警",
                         "发布与草稿变更必须不同标题，否则邮件同类节流会互相吞")
        self.assertTrue(urgent, "对外可见内容变更必须走紧急账")
        self.assertTrue(force, "发布告警不得被同类节流吞掉")
        self.assertIn("admin", body)
        self.assertIn("全体注意：今晚维护", body)
        self.assertIn("新发布", body)
        # 覆盖场景：正文要说清是被替换掉一条旧公告，且只截前 80 字
        self._put_draft(m, "长" * 200)
        self._publish(m)
        body2 = [a for a in self.alerts if a[0] == "公告发布告警"][-1][1]
        self.assertIn("覆盖", body2)
        # 正文会按显示宽度主动折行并悬挂缩进，故"80 字连续出现"不再是可断言的形状；
        # 真正要钉住的是截断口径：恰好 80 个"长"，一个字都不能多。
        self.assertEqual(body2.replace("\n", "").replace(" ", "").count("长"), 80,
                         "正文只截发布后前 80 字，不得外泄全文")
        self.assertTrue(any(ln.startswith("  长") for ln in body2.splitlines()),
                        "超宽值应悬挂缩进续行（主动断行），而不是留给客户端被动折行")

    def test_publish_audit_records_publisher_and_diff(self):
        m = self._master()
        self._put_draft(self._reg_admin(), "维护通知")
        self._publish(m)
        rows = self._audit("announcement_publish")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["username"], "admin", "发布人必须是点发布的主管理员")
        self.assertIn("维护通知", rows[0]["detail"])
        self.assertIn("新发布", rows[0]["detail"])


class TakedownTest(_AnnBase):
    """空草稿 + 线上有内容 = 下线（用户裁决：不另设 clear 端点，一收一发同档）。"""

    def _published(self):
        m = self._master()
        self._put_draft(m, "今晚维护")
        self.assertEqual(self._publish(m).status_code, 200)
        return m

    def test_takedown_needs_master_and_password_of_this_request(self):
        m = self._published()
        sub = self._reg_admin()
        r = self._publish(sub, password=REG_PASS)
        self.assertEqual(r.status_code, 403, r.get_data(as_text=True))
        self.assertEqual(self._env().get("YIBAN_ANNOUNCEMENT"), "今晚维护", "被拒不得改动线上")
        r2 = self._publish(m, password=None)
        self.assertEqual(r2.status_code, 403, r2.get_data(as_text=True))
        self.assertEqual(self._env().get("YIBAN_ANNOUNCEMENT"), "今晚维护")

    def test_takedown_clears_every_announcement_key_in_one_write(self):
        m = self._published()
        before = len(self.writes)
        r = self._publish(m)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertIn("下线", r.get_json()["msg"])
        raw = self._raw_env()
        for key in ("YIBAN_ANNOUNCEMENT=", "YIBAN_ANNOUNCEMENT_PUBLISHED_META=",
                    "YIBAN_ANNOUNCEMENT_DRAFT="):
            self.assertNotIn(key, raw, f"下线后 {key} 应被清掉")
        self.assertEqual(len(self.writes), before + 1, "下线必须是一次原子落盘")
        self.assertEqual(self._anon().get("/api/announcement").get_json()["text"], "")
        body = m.get("/api/announcement", headers=self._hdr(m)).get_json()
        self.assertEqual((body["published_by"], body["published_at"]), ("", ""))

    def test_takedown_alert_is_urgent_forced_and_names_the_action(self):
        m = self._published()
        self.alerts.clear()
        self._publish(m)
        offline = [a for a in self.alerts if "下线" in a[1]]
        self.assertEqual(len(offline), 1, f"应恰有一条写明下线的告警：{self.alerts}")
        self.assertTrue(offline[0][2] and offline[0][3], "下线告警必须紧急且突破额度送达")
        rows = self._audit("announcement_publish")
        self.assertTrue(any("下线" in (r["detail"] or "") for r in rows),
                        "审计要能区分这是下线而非发布")

    def test_put_empty_text_no_longer_claims_the_live_one_is_gone(self):
        m = self._published()
        r = self._put_draft(m, "")
        self.assertEqual(r.status_code, 200)
        msg = r.get_json()["msg"]
        self.assertIn("线上公告未变", msg, f"清空草稿不得被读成下线：{msg}")
        self.assertEqual(self._env().get("YIBAN_ANNOUNCEMENT"), "今晚维护")

    def test_published_meta_names_the_publisher(self):
        m = self._published()
        body = m.get("/api/announcement", headers=self._hdr(m)).get_json()
        self.assertEqual(body["published_by"], "admin")
        self.assertRegex(body["published_at"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
        self.assertEqual(set(self._anon().get("/api/announcement").get_json()),
                         {"ok", "text"}, "线上元数据不得随公开响应外泄")


class DraftMetaHelperTest(_AnnBase):
    """元数据解析单源且 fail-safe：坏值只降级为「元数据不可用」，绝不抛。"""

    def test_parse_accepts_the_documented_shape(self):
        by, at = self.webapp._parse_announcement_meta(
            "someone@test.local|2026-09-19 10:20:30")
        self.assertEqual((by, at), ("someone@test.local", "2026-09-19 10:20:30"))

    def test_parse_rejects_anything_else(self):
        for bad in ("", "   ", "no-pipe", "a@test.local|", "|2026-09-19 10:20:30",
                    "a@test.local|x|y", "a@test.local|2026-09-19",
                    "a@test.local|2026-13-45 99:99:99",
                    "a@test.local|2026-09-19T10:20:30"):
            with self.subTest(bad=bad):
                self.assertEqual(
                    self.webapp._parse_announcement_meta(bad), ("", ""),
                    f"非法元数据必须解析为不可用：{bad!r}")


ADMIN_PASS_B18F = "MasterPass#2026"   # 15 位四类，满足主管理员 12/3 策略


USER_PASS_B18F = "UserPass123!"       # 注册用户口径：10 位两类


PHONE = "13800138001"


REBIND_PHONE = "13900139000"


CAL_MONTH = "2026-09"


CAL_DATE = "2026-09-15"


LOG_LINE = f"[{CAL_DATE} 10:00:00] [INFO] yiban: [{PHONE}] 签到成功"


class Batch18FixesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-batch18-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        cls._env_content = (
            f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
            f"YIBAN_ADMIN_USER=admin\nYIBAN_ADMIN_PASSWORD={ADMIN_PASS_B18F}\n"
            # 本类里的口令门用例钉的是"当次要口令 + 失败零写入"，固定在 full
            f"YIBAN_PW_GATE={GATE_FULL}\n"
        )
        with io.open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(cls._env_content)
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.accounts_file = os.path.join(cls.tmp, "accounts.json")
        cls.users_file = os.path.join(cls.tmp, "users.json")
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_ACCOUNTS_FILE"] = cls.accounts_file
        os.environ["YIBAN_USERS_FILE"] = cls.users_file
        os.environ["YIBAN_DB_FILE"] = cls.db_file
        os.environ["YIBAN_STATE_DIR"] = cls.tmp
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
        for k in ("YIBAN_ACCOUNTS_KEY", "YIBAN_ENV_FILE", "YIBAN_ACCOUNTS_FILE",
                  "YIBAN_USERS_FILE", "YIBAN_DB_FILE", "YIBAN_STATE_DIR"):
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

    # ---- 辅助 ----
    def _login(self, c, username, password):
        r = c.post("/api/login", json={"username": username, "password": password})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return c.get("/api/me").get_json()["csrf_token"]

    def _admin_client(self):
        c = self.webapp.create_app().test_client()
        return c, self._login(c, "admin", ADMIN_PASS_B18F)

    def _make_formal_user(self, email, phone):
        """构造「正式用户」：注册 + 提交账号 + 管理员审核通过（active）。"""
        db.create_user(email, self.webapp.generate_password_hash(USER_PASS_B18F))
        c = self.webapp.create_app().test_client()
        t = self._login(c, email, USER_PASS_B18F)
        r = c.post("/api/my-accounts", json={"name": "n", "phone": phone, "password": "p"},
                   headers={"X-CSRF-Token": t})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        ac, at = self._admin_client()
        accounts = db.load_accounts()
        idx = next(i for i, a in enumerate(accounts) if a["phone"] == phone)
        r = ac.post(f"/api/accounts/{idx}/review", json={"action": "approve"},
                    headers={"X-CSRF-Token": at})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

    def _write_daily_state(self):
        """构造当日签到状态文件（日历数据源）：STATE_DIR/sign-daily-YYYY-MM-DD.json。"""
        path = os.path.join(self.tmp, f"sign-daily-{CAL_DATE}.json")
        with io.open(path, "w", encoding="utf-8") as f:
            json.dump({PHONE: "✅"}, f, ensure_ascii=False)
        return path

    def _write_date_log(self):
        """构造当日按天日志文件（my-logs 数据源），测试后清理。"""
        path = self.webapp.log_path_for(CAL_DATE)
        with io.open(path, "w", encoding="utf-8") as f:
            f.write(LOG_LINE + "\n")
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        return path

    def _last_audit_detail(self, action):
        with db._conn_lock:
            conn = db.get_conn()
            row = conn.execute(
                "SELECT detail FROM audit_logs WHERE action=? ORDER BY id DESC LIMIT 1",
                (action,),
            ).fetchone()
        return row["detail"] if row else None

    # =====================================================================
    # 1. H-1 XSS：_doc_page 转义收敛
    # =====================================================================
    def test_privacy_page_reflected_xss_escaped(self):
        """验收用例：构造任意前缀路径 /x"><script>…/privacy → 响应不含未转义脚本。"""
        c = self.webapp.create_app().test_client()
        r = c.get('/x"><script>alert(1)</script>/privacy')
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        body = r.get_data(as_text=True)
        self.assertNotIn("<script>alert(1)", body, "注入的脚本不得以未转义形态出现在响应体")
        self.assertIn("&lt;script&gt;alert(1)", body, "base_path 应被 HTML 转义后输出")

    def test_doc_page_icp_police_escaped(self):
        """备案文本含引号/尖括号时同样转义（quote=True 覆盖属性上下文）。"""
        env2 = os.path.join(self.tmp, "env-icp.env")
        with io.open(env2, "w", encoding="utf-8") as f:
            f.write(self._env_content
                    + 'YIBAN_ICP_INFO="><svg onload=alert(2)>\n'
                    + "YIBAN_POLICE_INFO=<img src=x onerror=alert(3)>\n")
        with mock.patch.object(self.webapp, "ENV_FILE", env2):
            c = self.webapp.create_app().test_client()
            # icp_info/police_info 在请求时读 ENV_FILE → GET 也必须在 patch 内
            r = c.get("/privacy")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        body = r.get_data(as_text=True)
        # 模板自身含合法的 <img src="/gongan-beian.png"> 徽标，只断言注入载荷不出现
        self.assertNotIn("<svg", body)
        self.assertNotIn("<img src=x", body)
        self.assertIn("&lt;svg", body)
        self.assertIn("&lt;img src=x", body)

    # =====================================================================
    # 2. H-2 告警致盲：额度/节流参数收口 + 先告警后落盘
    # =====================================================================
    def test_notify_cooldown_without_password_400_no_write_no_alert(self):
        """验收用例：无 confirm_password 调 PUT notify-config {"cooldown":90000} → 400，
        零写入、零告警。"""
        ac, at = self._admin_client()
        before = self.webapp.read_env(self.env_file)
        with mock.patch.object(self.webapp, "send_notification") as sn:
            r = ac.put("/api/notify-config", json={"cooldown": 90000},
                       headers={"X-CSRF-Token": at})
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        self.assertEqual(r.get_json()["reason"], "password_required")
        self.assertEqual(self.webapp.read_env(self.env_file), before, "鉴权失败必须零写入")
        sn.assert_not_called()

    def test_notify_cooldown_with_password_200_and_audited(self):
        """带正确口令 → 200、落盘、留痕，并按新参数记审计。"""
        ac, at = self._admin_client()
        r = ac.put("/api/notify-config", json={"cooldown": 90000, "confirm_password": ADMIN_PASS_B18F},
                   headers={"X-CSRF-Token": at})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        env = self.webapp.read_env(self.env_file)
        self.assertEqual(env.get("YIBAN_NOTIFY_COOLDOWN"), "90000")
        detail = self._last_audit_detail("notify_config")
        self.assertIsNotNone(detail)
        self.assertEqual(json.loads(detail)["cooldown"], 90000)

    def test_notify_config_alert_sent_after_write_with_force(self):
        """notify-config 变更告警在落盘成功之后发出 + force=True。

        原契约"先告警后落盘"（防新写入的额度/节流参数吞掉告警）不成立：
        force=True 本就绕过两侧节流；先发反而让写入失败（500）时运营者收到
        一条描述从未生效变更的通知。落盘成功后必须仍发告警、urgent=True。
        """
        ac, at = self._admin_client()
        order = []
        real_write = self.webapp.write_env_batch

        def _write_spy(env_path, updates):
            order.append("write")
            return real_write(env_path, updates)

        sn = mock.Mock(side_effect=lambda t, c, **kw: order.append(("alert", kw.get("force"))))
        with mock.patch.object(self.webapp, "send_notification", sn), \
             mock.patch.object(self.webapp, "write_env_batch", _write_spy):
            r = ac.put("/api/notify-config", json={"cooldown": 60, "confirm_password": ADMIN_PASS_B18F},
                       headers={"X-CSRF-Token": at})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(len(order), 2, f"应恰好一次落盘 + 一次告警，实际 {order}")
        self.assertEqual(order[0], "write", "告警只能描述已落盘的事实：先写入后告警")
        self.assertEqual(order[1][0], "alert", "落盘成功后必须发出变更告警")
        self.assertTrue(order[1][1], "变更告警必须 force=True")
        self.assertEqual(sn.call_args.args[0], "消息推送配置变更告警")
        self.assertTrue(sn.call_args.kwargs.get("urgent"))

    def test_notify_config_write_failure_500_without_alert(self):
        """落盘失败（磁盘错）→ 500、零告警、零审计：告警与留痕只能描述已生效的变更。"""
        ac, at = self._admin_client()
        before = self.webapp.read_env(self.env_file)
        with mock.patch.object(self.webapp, "send_notification") as sn, \
             mock.patch.object(self.webapp, "write_env_batch",
                               side_effect=RuntimeError("disk full")):
            r = ac.put("/api/notify-config", json={"cooldown": 60, "confirm_password": ADMIN_PASS_B18F},
                       headers={"X-CSRF-Token": at})
        self.assertEqual(r.status_code, 500, r.get_data(as_text=True))
        sn.assert_not_called()
        self.assertIsNone(self._last_audit_detail("notify_config"),
                          "写入失败不得留下描述未生效变更的审计行")
        self.assertEqual(self.webapp.read_env(self.env_file), before,
                         "写入失败不得改动 .env")

    def test_mail_config_alert_sent_after_write_with_force(self):
        """mail-config 变更告警在落盘成功之后发出 + force=True（安全审查 2026-09-08）。

        原契约"先告警后落盘"的理由（防新写入的节流参数吞掉告警）不成立：
        force=True 本就绕过两侧节流；先发反而会在加密/写盘失败（500）时外发一条
        描述从未生效变更的"配置已变更"通知。落盘成功后必须仍发告警。
        """
        ac, at = self._admin_client()
        order = []
        real_write = self.webapp.write_env_batch

        def _write_spy(env_path, updates):
            order.append("write")
            return real_write(env_path, updates)

        sn = mock.Mock(side_effect=lambda t, c, **kw: order.append(("alert", kw.get("force"))))
        with mock.patch.object(self.webapp, "send_notification", sn), \
             mock.patch.object(self.webapp, "write_env_batch", _write_spy):
            r = ac.put("/api/mail-config", json={"enabled": False, "confirm_password": ADMIN_PASS_B18F},
                       headers={"X-CSRF-Token": at})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(len(order), 2, f"应恰好一次落盘 + 一次告警，实际 {order}")
        self.assertEqual(order[0], "write", "告警只能描述已落盘的事实：先写入后告警")
        self.assertEqual(order[1][0], "alert", "落盘成功后必须发出变更告警")
        self.assertTrue(order[1][1], "变更告警必须 force=True")
        self.assertEqual(sn.call_args.args[0], "邮件配置变更告警")

    def test_mail_config_close_without_password_400_no_alert(self):
        """mail-config 关闭动作未带口令 → 400、零写入、零告警（先验口令才发告警）。"""
        ac, at = self._admin_client()
        with mock.patch.object(self.webapp, "send_notification") as sn:
            r = ac.put("/api/mail-config", json={"enabled": False},
                       headers={"X-CSRF-Token": at})
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        sn.assert_not_called()
        self.assertNotIn("YIBAN_MAIL_ENABLE=0", self.webapp.read_env(self.env_file))

    def test_notify_alert_after_urgent_daily_max_1_still_sent(self):
        """验收用例：urgent_daily_max=1 落盘后，后续变更告警仍能发出（force 绕过新额度）。"""
        ac, at = self._admin_client()
        h = {"X-CSRF-Token": at}
        r = ac.put("/api/notify-config", json={
            "type": "serverchan", "secret": "SCT406257TESTTESTTESTTEST",
            "confirm_password": ADMIN_PASS_B18F}, headers=h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        r = ac.put("/api/notify-config",
                   json={"urgent_daily_max": 1, "confirm_password": ADMIN_PASS_B18F}, headers=h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        # 走真实 send_notification → 断言 notify.send 收到 force=True（不被刚写入的额度吞掉）
        with mock.patch.object(self.webapp.notify, "send") as nsend, \
             mock.patch.object(self.webapp.mailer, "send_admin_alert"):
            r = ac.put("/api/notify-config",
                       json={"cooldown": 60, "confirm_password": ADMIN_PASS_B18F}, headers=h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        nsend.assert_called_once()
        self.assertTrue(nsend.call_args.kwargs.get("force"), "force 必须透传到 notify.send")
        self.assertTrue(nsend.call_args.kwargs.get("urgent"))

    # =====================================================================
    # 3. M1 编辑回审：改绑一律回 pending 重审
    # =====================================================================
    def test_user_rebind_resets_pending_and_audited(self):
        """用户改绑手机号：ACTIVE 号也回 pending，返回"重新提交"提示，审计带改绑回审。"""
        self._make_formal_user("u1@test.local", PHONE)
        c = self.webapp.create_app().test_client()
        t = self._login(c, "u1@test.local", USER_PASS_B18F)
        mine = c.get("/api/my-accounts").get_json()["accounts"]
        self.assertEqual(mine[0]["status"], "active", "前置：审核通过为 active")
        # 编辑表单会带上"打开表单那一刻"的快照（乐观锁 + 防错位比对基准）——
        # 改绑本来就要求带快照：否则服务端无法区分"改绑"与"列表漂移"，
        # 只能按 fail-safe 拒绝（见 test_rebind_without_snapshot_is_rejected）
        import json as _json
        snapshot = _json.dumps({"phone": PHONE, "name": mine[0].get("name", "")},
                               ensure_ascii=False)
        r = c.put(f"/api/my-accounts/{mine[0]['index']}",
                  json={"name": "n", "phone": REBIND_PHONE, "password": "",
                        "_snapshot": snapshot},
                  headers={"X-CSRF-Token": t})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json()["msg"], "已重新提交，等待管理员审核")
        acc = next(a for a in db.load_accounts() if a["owner"] == "u1@test.local")
        self.assertEqual(acc["phone"], REBIND_PHONE)
        self.assertEqual(acc["status"], "pending", "改绑后必须回待审核")
        detail = self._last_audit_detail("my_account_update")
        self.assertIn("改绑回审", detail or "")

    def test_rebind_without_snapshot_is_rejected(self):
        """不带快照的改绑请求按 fail-safe 拒绝（409），防"列表漂移静默改到他人行"。

        代价：直连 API 的调用方改绑必须带 `_snapshot`（前端表单本来就会带）。
        """
        self._make_formal_user("u3@test.local", PHONE)
        c = self.webapp.create_app().test_client()
        t = self._login(c, "u3@test.local", USER_PASS_B18F)
        mine = c.get("/api/my-accounts").get_json()["accounts"]
        r = c.put(f"/api/my-accounts/{mine[0]['index']}",
                  json={"name": "n", "phone": REBIND_PHONE, "password": ""},
                  headers={"X-CSRF-Token": t})
        self.assertEqual(r.status_code, 409, r.get_data(as_text=True))
        acc = next(a for a in db.load_accounts() if a["owner"] == "u3@test.local")
        self.assertEqual(acc["phone"], PHONE, "被拒后手机号不得变化")

    def test_user_password_only_edit_keeps_active(self):
        """仅改密码（phone 不变）：状态保持 active，返回"已保存"。"""
        self._make_formal_user("u2@test.local", PHONE)
        c = self.webapp.create_app().test_client()
        t = self._login(c, "u2@test.local", USER_PASS_B18F)
        mine = c.get("/api/my-accounts").get_json()["accounts"]
        r = c.put(f"/api/my-accounts/{mine[0]['index']}",
                  json={"name": "n", "phone": PHONE, "password": "newpass123"},
                  headers={"X-CSRF-Token": t})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json()["msg"], "已保存")
        acc = next(a for a in db.load_accounts() if a["owner"] == "u2@test.local")
        self.assertEqual(acc["status"], "active", "非改绑编辑不得回审")

    def test_admin_rebind_resets_pending_and_clears_reason(self):
        """管理员改绑用户的号：同样回 pending（由管理员再批），并清除旧拒绝理由。"""
        db.add_account({"name": "A", "phone": PHONE, "password": "pw",
                        "status": "active", "owner": "u3@test.local"})
        ac, at = self._admin_client()
        row = db.load_accounts()[0]
        snap = json.dumps({
            "name": row["name"], "phone": row["phone"],
            "phone_model": row.get("phone_model", ""),
            "status": row["status"], "deleted": bool(row.get("deleted")),
        }, ensure_ascii=False)
        r = ac.put("/api/accounts/0",
                   json={"name": "A", "phone": REBIND_PHONE, "password": "",
                         "_snapshot": snap},
                   headers={"X-CSRF-Token": at})
        self.assertIn(r.status_code, (400, 403),
                      "改绑手机号即改写他人凭据，必须当次口令: %s" % r.get_data(as_text=True))
        self.assertEqual(db.load_accounts()[0]["phone"], PHONE, "鉴权未通过不得改绑")
        r = ac.put("/api/accounts/0",
                   json={"name": "A", "phone": REBIND_PHONE, "password": "",
                         "_snapshot": snap, "confirm_password": ADMIN_PASS_B18F},
                   headers={"X-CSRF-Token": at})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        acc = db.load_accounts()[0]
        self.assertEqual(acc["phone"], REBIND_PHONE)
        self.assertEqual(acc["status"], "pending", "管理员改绑同样回待审核")
        self.assertEqual(acc.get("reject_reason", ""), "", "回审应清除旧拒绝理由")
        detail = self._last_audit_detail("account_update")
        self.assertIn("改绑回审", detail or "")

    def test_admin_edit_same_phone_keeps_status(self):
        """管理员编辑不改绑（仅名称/密码）：状态保持不变。"""
        db.add_account({"name": "A", "phone": PHONE, "password": "pw",
                        "status": "active", "owner": "admin"})
        ac, at = self._admin_client()
        # 改写他人易班凭据（这里换了密码）必须当次口令——先确认没口令时不改库
        denied = ac.put("/api/accounts/0",
                        json={"name": "A2", "phone": PHONE, "password": "newpass123"},
                        headers={"X-CSRF-Token": at})
        self.assertIn(denied.status_code, (400, 403), denied.get_data(as_text=True))
        self.assertEqual(db.load_accounts()[0]["password"], "pw", "鉴权未通过不得改写凭据")
        r = ac.put("/api/accounts/0",
                   json={"name": "A2", "phone": PHONE, "password": "newpass123",
                         "confirm_password": ADMIN_PASS_B18F},
                   headers={"X-CSRF-Token": at})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        acc = db.load_accounts()[0]
        self.assertEqual(acc["name"], "A2")
        self.assertEqual(acc["status"], "active", "非改绑编辑不得回审")

    # =====================================================================
    # 4. M2 历史数据隔离：pending 行的历史不回显
    # =====================================================================
    def test_my_calendar_hides_pending_history(self):
        """pending 行提交后 my-calendar 不返回该号历史（验收用例）。"""
        self._write_daily_state()
        db.create_user("m2a@test.local", self.webapp.generate_password_hash(USER_PASS_B18F))
        c = self.webapp.create_app().test_client()
        t = self._login(c, "m2a@test.local", USER_PASS_B18F)
        r = c.post("/api/my-accounts", json={"name": "n", "phone": PHONE, "password": "p"},
                   headers={"X-CSRF-Token": t})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        data = c.get(f"/api/my-calendar?month={CAL_MONTH}").get_json()
        self.assertIn(CAL_DATE, data["days"])
        self.assertEqual(data["days"][CAL_DATE], {}, "pending 号的历史状态不得回显")
        # 列表展示口径不变：my-accounts 仍含该行（展示 pending 状态用）
        mine = c.get("/api/my-accounts").get_json()["accounts"]
        self.assertEqual(mine[0]["phone"], PHONE)
        self.assertEqual(mine[0]["status"], "pending")

    def test_my_calendar_shows_active_history_after_approve(self):
        """对照：审核通过（active）后日历正常回显该号历史。"""
        self._write_daily_state()
        self._make_formal_user("m2b@test.local", PHONE)
        c = self.webapp.create_app().test_client()
        self._login(c, "m2b@test.local", USER_PASS_B18F)
        data = c.get(f"/api/my-calendar?month={CAL_MONTH}").get_json()
        self.assertEqual(data["days"][CAL_DATE], {PHONE: "✅"})

    def test_my_logs_hide_pending_and_show_active(self):
        """my-logs 同口径：pending 不回显，active 回显（脱敏形态）。"""
        self._write_date_log()
        db.create_user("m2c@test.local", self.webapp.generate_password_hash(USER_PASS_B18F))
        c = self.webapp.create_app().test_client()
        t = self._login(c, "m2c@test.local", USER_PASS_B18F)
        r = c.post("/api/my-accounts", json={"name": "n", "phone": PHONE, "password": "p"},
                   headers={"X-CSRF-Token": t})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        data = c.get(f"/api/my-logs?date={CAL_DATE}").get_json()
        self.assertEqual(data["logs"], [], "pending 号的日志不得回显")
        # 管理员审核通过 → 历史日志恢复可见
        ac, at = self._admin_client()
        idx = next(i for i, a in enumerate(db.load_accounts()) if a["phone"] == PHONE)
        r = ac.post(f"/api/accounts/{idx}/review", json={"action": "approve"},
                    headers={"X-CSRF-Token": at})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        data = c.get(f"/api/my-logs?date={CAL_DATE}").get_json()
        self.assertEqual(len(data["logs"]), 1)
        self.assertIn("138****8001", data["logs"][0], "回显行保持出站脱敏")

    # =====================================================================
    # 5. M3 accounts_only 门禁
    # =====================================================================
    def test_accounts_only_without_password_400_accounts_intact(self):
        """无口令调 accounts_only → 400，账号原封不动。"""
        db.create_user("m3@test.local", self.webapp.generate_password_hash(USER_PASS_B18F))
        db.add_account({"name": "A", "phone": PHONE, "password": "pw",
                        "status": "active", "owner": "m3@test.local"})
        ac, at = self._admin_client()
        r = ac.post("/api/users/m3@test.local/delete",
                    json={"mode": "accounts_only"}, headers={"X-CSRF-Token": at})
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        self.assertEqual(r.get_json()["reason"], "password_required")
        self.assertEqual(len(db.load_accounts()), 1, "鉴权失败不得清空账号")

    def test_accounts_only_with_password_clears_accounts_keeps_user(self):
        """带正确口令 → 200：账号全部清空、用户保留可重新提交（与 full 语义分界）。"""
        db.create_user("m3b@test.local", self.webapp.generate_password_hash(USER_PASS_B18F))
        db.add_account({"name": "A", "phone": PHONE, "password": "pw",
                        "status": "active", "owner": "m3b@test.local"})
        ac, at = self._admin_client()
        r = ac.post("/api/users/m3b@test.local/delete",
                    json={"mode": "accounts_only", "confirm_password": ADMIN_PASS_B18F},
                    headers={"X-CSRF-Token": at})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(db.load_accounts(), [], "账号应被清空")
        self.assertIsNotNone(db.find_user("m3b@test.local"), "用户本体保留")

    # =====================================================================
    # 6. M9 枚举文案：注销冷却期与「该邮箱已注册」同文案
    # =====================================================================
    def test_register_cooldown_message_matches_already_registered(self):
        """冷却期分支文案必须与已注册分支逐字一致（不得泄露"近期注销过"信号）。"""
        db.create_user("live@qq.com", self.webapp.generate_password_hash(USER_PASS_B18F))
        db.create_user("gone@qq.com", self.webapp.generate_password_hash(USER_PASS_B18F))
        db.soft_delete_user_with_accounts("gone@qq.com")  # 刚注销 → 冷却期内
        c = self.webapp.create_app().test_client()
        r_live = c.post("/api/register",
                        json={"email": "live@qq.com", "password": USER_PASS_B18F, "agree": True})
        r_gone = c.post("/api/register",
                        json={"email": "gone@qq.com", "password": USER_PASS_B18F, "agree": True})
        self.assertEqual(r_live.status_code, 400)
        self.assertEqual(r_gone.status_code, 400)
        self.assertEqual(r_live.get_json()["error"], "该邮箱已注册")
        self.assertEqual(r_gone.get_json()["error"], "该邮箱已注册",
                         "冷却期文案与「该邮箱已注册」必须逐字一致")

    def test_register_after_cooldown_expiry_succeeds(self):
        """对照：仅文案收敛，行为不变——冷却期结束后邮箱正常释放可再注册。"""
        # 注意顺序：先建 client 再回填过期时间（create_app 启动清理会物理清除过期注销用户）
        c = self.webapp.create_app().test_client()
        db.create_user("old@qq.com", self.webapp.generate_password_hash(USER_PASS_B18F))
        db.soft_delete_user_with_accounts("old@qq.com")
        old = (datetime.now() - timedelta(days=8)).strftime("%Y-%m-%d %H:%M:%S")
        conn = db.get_conn()
        conn.execute("UPDATE users SET deleted_at=? WHERE email=?", (old, "old@qq.com"))
        conn.commit()
        r = c.post("/api/register",
                   json={"email": "old@qq.com", "password": USER_PASS_B18F, "agree": True})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertIsNotNone(db.find_user("old@qq.com"))

    # =====================================================================
    # 7. M10 cookie path：YIBAN_BASE_PATH 收窄会话 Cookie
    # =====================================================================
    def test_cookie_path_narrowed_when_base_path_env_set(self):
        """验收用例：YIBAN_BASE_PATH=/tool/yiban-auto-sign/demo → 登录 Set-Cookie
        含 Path=/tool/yiban-auto-sign/demo/。"""
        env2 = os.path.join(self.tmp, "env-basepath.env")
        with io.open(env2, "w", encoding="utf-8") as f:
            f.write(self._env_content + "YIBAN_BASE_PATH=/tool/yiban-auto-sign/demo\n")
        with mock.patch.object(self.webapp, "ENV_FILE", env2):
            c = self.webapp.create_app().test_client()
        r = c.post("/api/login", json={"username": "admin", "password": ADMIN_PASS_B18F})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        cookie = r.headers.get("Set-Cookie", "")
        self.assertIn("Path=/tool/yiban-auto-sign/demo/", cookie,
                      f"会话 Cookie 应收窄到挂载前缀，实际 Set-Cookie: {cookie}")

    def test_cookie_path_default_root_without_base_path(self):
        """对照：未设 YIBAN_BASE_PATH（自动探测形态）不强行收窄，行为不变。"""
        c = self.webapp.create_app().test_client()
        r = c.post("/api/login", json={"username": "admin", "password": ADMIN_PASS_B18F})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        cookie = r.headers.get("Set-Cookie", "")
        self.assertNotIn("Path=/tool", cookie, "未显式配置时不得收窄 Cookie 路径")
        self.assertIn("yiban_admin=", cookie)


SPEC_MASTER_ONLY = {
    "sign_window", "window_edge_sec", "edge_front_sec", "edge_back_sec",
    "sunday_sign", "saturday_sign", "registration_pause",
    "start_delay_max", "gap_max", "max_users", "max_accounts",
    "account_verify", "probe_enable", "probe_time", "probe_interval",
}


SPEC_GATED = {"sign_order", "sign_dist", "sign_mode", "allow_time_pref"}


A_SAMPLES = {
    "sign_window": "03:00 ~ 04:00",
    "window_edge_sec": 90,
    "edge_front_sec": 90,
    "edge_back_sec": 120,
    "sunday_sign": 1,
    "saturday_sign": 1,
    "registration_pause": 1,
    "start_delay_max": 120,
    "gap_max": 30,
    "max_users": 123,
    "max_accounts": 45,
    "account_verify": 1,
    "probe_enable": 1,
    "probe_time": "06:00",
    "probe_interval": "3",
}


B_SAMPLES = {
    "sign_order": "random",
    "sign_dist": "normal",
    "sign_mode": "random",
    "allow_time_pref": 1,
}


class _TierBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-tiers-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(
                f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
                f"YIBAN_ADMIN_USER=admin\nYIBAN_ADMIN_PASSWORD={ADMIN_PASS}\n"
                # 本文件钉的是口令门的**机制**（当次要口令、豁免、冷却、变更告警），
                # 故把档位固定在 full（默认档 risk 下这些动作不再当次要口令）
                f"YIBAN_PW_GATE={GATE_FULL}\n"
            )
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.accounts_file = os.path.join(cls.tmp, "accounts.json")
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_ACCOUNTS_FILE"] = cls.accounts_file
        os.environ["YIBAN_USERS_FILE"] = os.path.join(cls.tmp, "users.json")
        os.environ["YIBAN_DB_FILE"] = cls.db_file
        os.environ["YIBAN_STATE_DIR"] = cls.tmp
        os.environ["YIBAN_LOG_FILE"] = os.path.join(cls.tmp, "sign.log")
        os.environ["YIBAN_DISABLE_PURGE_LOOP"] = "1"
        global db
        import db
        # 独立名字加载 web/app.py：它在导入期把 ENV_FILE 等读成模块级常量，与别的测试
        # 文件共用同一模块对象会读到另一个 .env（单跑绿、全量红的老坑）
        spec = importlib.util.spec_from_file_location(
            "webapp_settings_tiers", os.path.join(BASE, "web", "app.py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules["webapp_settings_tiers"] = mod
        with contextlib.suppress(Exception):
            spec.loader.exec_module(mod)
        cls.webapp = mod

    @classmethod
    def tearDownClass(cls):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k in ("YIBAN_ACCOUNTS_KEY", "YIBAN_ENV_FILE", "YIBAN_ACCOUNTS_FILE",
                  "YIBAN_USERS_FILE", "YIBAN_DB_FILE", "YIBAN_STATE_DIR",
                  "YIBAN_LOG_FILE", "YIBAN_DISABLE_PURGE_LOOP"):
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
        # 每例回到「全部档位键未配置 = 生效值即默认」的基线
        self.webapp.write_env_batch(self.env_file, {
            "YIBAN_GLOBAL_PAUSE": "", "YIBAN_REGISTRATION_PAUSE": "",
            "YIBAN_SUNDAY_SIGN": "", "YIBAN_SATURDAY_SIGN": "",
            "YIBAN_SIGN_ORDER": "", "YIBAN_SIGN_DIST": "", "YIBAN_SIGN_MODE": "",
            "YIBAN_ALLOW_TIME_PREF": "", "YIBAN_START_DELAY_MAX": "",
            "YIBAN_ACCOUNT_GAP_MAX": "", "YIBAN_WINDOW_EDGE_FRONT_SEC": "",
            "YIBAN_WINDOW_EDGE_BACK_SEC": "", "YIBAN_WINDOW_EDGE_SEC": "",
            "YIBAN_MAX_USERS": "", "YIBAN_MAX_ACCOUNTS": "",
            "YIBAN_ACCOUNT_VERIFY": "", "YIBAN_PROBE_ENABLE": "",
            "YIBAN_PROBE_TIME": "", "YIBAN_PROBE_INTERVAL_DAYS": "",
            "YIBAN_SIGN_START": "", "YIBAN_SIGN_END": "",
            "YIBAN_PW_CONFIRM_TTL": "", "YIBAN_PW_CONFIRM_COOLDOWN_SEC": "",
            "YIBAN_ADMIN_DELETE_COOLDOWN_SEC": "", "YIBAN_ADMIN_DELETE_MAX": "",
        })
        self.alerts = []
        patcher = mock.patch.object(
            self.webapp, "send_notification",
            side_effect=lambda t, c, urgent=False, force=False, ledger=None:
            self.alerts.append((t, render_body(c), urgent, force)))
        patcher.start()
        self.addCleanup(patcher.stop)

    # ---- 工具 ----
    def _client(self, username, password):
        c = self.webapp.create_app().test_client()
        r = c.post("/api/login", json={"username": username, "password": password})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        c.csrf = c.get("/api/me").get_json()["csrf_token"]
        return c

    def _master(self):
        return self._client("admin", ADMIN_PASS)

    def _reg_admin(self):
        db.create_user(REG_ADMIN, self.webapp.generate_password_hash(REG_PASS),
                       role="admin")
        return self._client(REG_ADMIN, REG_PASS)

    def _hdr(self, c):
        return {"X-CSRF-Token": c.csrf}

    def _save(self, c, payload):
        return c.post("/api/settings", json=payload, headers=self._hdr(c))

    def _env_has(self, needle):
        with open(self.env_file, encoding="utf-8-sig") as f:
            return needle in f.read()

    def _audit_rows(self, action):
        return [dict(r) for r in db.get_conn().execute(
            "SELECT detail FROM audit_logs WHERE action=?", (action,)).fetchall()]


class TierTableMetaTest(_TierBase):
    """档位表单源：常量对 == 规格表，且每个档位键的现值都读得回来。"""

    def test_tier_sets_match_spec(self):
        self.assertEqual(set(self.webapp.MASTER_ONLY_KEYS), SPEC_MASTER_ONLY,
                         "A 档表被单方面改动：规格与常量必须同时改")
        self.assertEqual(set(self.webapp.GATED_KEYS), SPEC_GATED,
                         "B 档表被单方面改动：规格与常量必须同时改")
        self.assertFalse(set(self.webapp.MASTER_ONLY_KEYS) & set(self.webapp.GATED_KEYS),
                         "一个键不可能既是 A 档又是 B 档")
        # 急停靠「按变更方向分流」实现，混进任一张表都会让方向语义失效
        self.assertNotIn("global_pause", self.webapp.MASTER_ONLY_KEYS)
        self.assertNotIn("global_pause", self.webapp.GATED_KEYS)
        self.assertEqual(self.webapp.GLOBAL_PAUSE_KEY, "global_pause")

    def test_every_tier_key_is_readable_back(self):
        """档位键必须能读回现值：读不回就没法判「这次到底变没变」，门禁会被静默跳过。

        两个容量上限挂在 capacity.users_max / accounts_max 下（非顶层键），单独放行。
        """
        c = self._master()
        payload = c.get("/api/settings", headers=self._hdr(c)).get_json()
        nested = {"max_users": "users_max", "max_accounts": "accounts_max"}
        for key in set(self.webapp.MASTER_ONLY_KEYS) | set(self.webapp.GATED_KEYS):
            if key in nested:
                self.assertIn(nested[key], payload["capacity"], f"{key} 现值读不回")
            else:
                self.assertIn(key, payload, f"{key} 现值读不回")

    def test_effective_values_cover_both_tiers(self):
        cur = self.webapp._settings_effective_values(self.env_file)
        keys = (set(self.webapp.MASTER_ONLY_KEYS) | set(self.webapp.GATED_KEYS)
                | {self.webapp.GLOBAL_PAUSE_KEY})
        self.assertEqual(keys - set(cur), set(), "有档位键没有现值口径")
        # 现值必须是「生效值」而不是「键在不在」：未配置的延迟要报默认值
        self.assertEqual(cur["gap_max"], str(self.webapp.DEFAULT_ACCOUNT_GAP_MAX))
        self.assertEqual(cur["sign_order"], "sequence")

    def test_every_effective_value_key_has_a_tier(self):
        """反方向对拍：读侧枚举的每一个键都必须归属某一档，"没档位"= 对全体管理员开放。

        档位键 ⊆ 读侧由上一条用例负责；缺的是这个反方向。新加设置项时若只补了
        `_settings_effective_values` 而忘了补 `MASTER_ONLY_KEYS`/`GATED_KEYS`，
        `set(data).intersection(MASTER|GATED|gp)` 会把它判成"无档位"——不要求口令、
        不进 403 名单、不发变更告警，静默向所有管理员开放。常量定义处那句
        "新增设置键时必须改档位表"要防的正是这件事，这里把它变成会报红的检查。
        """
        cur = self.webapp._settings_effective_values(self.env_file)
        unclassified = set(cur) - (set(self.webapp.MASTER_ONLY_KEYS)
                                   | set(self.webapp.GATED_KEYS)
                                   | {self.webapp.GLOBAL_PAUSE_KEY})
        self.assertEqual(unclassified, set(),
                         f"这些设置键没有档位归属（等于任意管理员免口令可写）："
                         f"{sorted(unclassified)}")


class MasterOnlyTierTest(_TierBase):
    """A 档：普通管理员连口令都不给过；主管理员无口令也不给过。"""

    def test_regular_admin_denied_on_every_master_key(self):
        c = self._reg_admin()
        for key, value in sorted(A_SAMPLES.items()):
            with self.subTest(key=key):
                r = self._save(c, {key: value, "confirm_password": REG_PASS})
                self.assertEqual(r.status_code, 403, r.get_data(as_text=True))
        self.assertFalse(self._env_has("YIBAN_SUNDAY_SIGN"), "越界请求不得落盘")
        self.assertEqual(self.alerts, [], "被拒的越界请求不得发出变更告警")

    def test_promoted_keys_are_named_individually(self):
        """本次上收的键逐一点名（周末×2 / 窗口 / 边缘）——防「改了表漏了路由」。"""
        c = self._reg_admin()
        for key in ("sunday_sign", "saturday_sign", "sign_window",
                    "edge_front_sec", "edge_back_sec"):
            with self.subTest(key=key):
                r = self._save(c, {key: A_SAMPLES[key], "confirm_password": REG_PASS})
                self.assertEqual(r.status_code, 403, r.get_data(as_text=True))

    def test_demoted_keys_are_no_longer_denied(self):
        """本次下放的四个键（排序/分布/模式/自选）不得再吃「仅主管理员」403。"""
        c = self._reg_admin()
        for key in sorted(B_SAMPLES):
            with self.subTest(key=key):
                r = self._save(c, {key: B_SAMPLES[key], "confirm_password": REG_PASS})
                self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

    def test_master_needs_password_of_this_request(self):
        """主管理员改 A 档值：无口令 403、当次正确口令 200。"""
        c = self._master()
        self.assertEqual(self._save(c, {"sunday_sign": 1}).status_code, 403)
        self.assertFalse(self._env_has("YIBAN_SUNDAY_SIGN=1"), "被拒不得落盘")
        r = self._save(c, {"sunday_sign": 1, "confirm_password": ADMIN_PASS})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(self._env_has("YIBAN_SUNDAY_SIGN=1"))

    def test_master_same_value_needs_no_password(self):
        """值未变不要求口令（沿用系统开关门既有语义），且现值是服务端现读的。"""
        c = self._master()
        self.assertEqual(self._save(c, {"sunday_sign": 0}).status_code, 200)
        # 请求自称「从 1 改成 1」也没用：现读值是 0，这就是真变更 → 仍要口令
        self.assertEqual(self._save(c, {"sunday_sign": 1, "old_sunday_sign": 1}).status_code,
                         403)

    def test_exemption_does_not_cover_master_only_keys(self):
        """A 档不吃短时豁免：刚复核过的同一出口，改另一档 A 档仍要当次口令。"""
        c = self._master()
        self.assertEqual(self._save(c, {"sunday_sign": 1,
                                        "confirm_password": ADMIN_PASS}).status_code, 200)
        self.assertEqual(self._save(c, {"saturday_sign": 1}).status_code, 403,
                         "豁免只给配置类（B 档 / 执行体写）动作")


class GatedTierTest(_TierBase):
    """B 档：下放到任意管理员，真变更过统一门禁（可豁免）。"""

    def test_regular_admin_can_write_every_gated_key_with_password(self):
        c = self._reg_admin()
        for key, value in sorted(B_SAMPLES.items()):
            with self.subTest(key=key):
                r = self._save(c, {key: value, "confirm_password": REG_PASS})
                self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(self._env_has("YIBAN_SIGN_ORDER=random"))
        self.assertTrue(self._env_has("YIBAN_ALLOW_TIME_PREF=1"))

    def test_regular_admin_denied_without_password(self):
        c = self._reg_admin()
        for key, value in sorted(B_SAMPLES.items()):
            with self.subTest(key=key):
                self.assertEqual(self._save(c, {key: value}).status_code, 403)
        self.assertFalse(self._env_has("YIBAN_SIGN_DIST=normal"), "被拒不得落盘")

    def test_gated_key_honours_recent_reconfirm(self):
        """B 档真变更可被「刚复核过 + 同出口 IP」的豁免覆盖。"""
        c = self._reg_admin()
        self.assertEqual(self._save(c, {"sign_order": "random",
                                        "confirm_password": REG_PASS}).status_code, 200)
        r = self._save(c, {"sign_dist": "normal"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(self._env_has("YIBAN_SIGN_DIST=normal"))

    def test_legacy_derived_value_counts_as_unchanged(self):
        """只存旧 sign_mode 的存量配置：提交其派生排序值属「没改」，不该多一道口令。"""
        self.webapp.write_env_key(self.env_file, "YIBAN_SIGN_MODE", "random")
        c = self._reg_admin()
        r = self._save(c, {"sign_order": "random"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))


class EmergencyStopTest(_TierBase):
    """急停例外：0→1 人人可做（当次口令 + 高危额度 + 紧急告警），1→0 仅主管理员。"""

    def test_any_admin_can_stop_but_not_resume(self):
        c = self._reg_admin()
        self.assertEqual(self._save(c, {"global_pause": 1}).status_code, 403,
                         "急停同样要当次口令，不是带个会话就能按")
        r = self._save(c, {"global_pause": 1, "confirm_password": REG_PASS})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(self._env_has("YIBAN_GLOBAL_PAUSE=1"))
        # 恢复方向仍收归主管理员（普通管理员即使带口令也是 403）
        self.assertEqual(self._save(c, {"global_pause": 0,
                                        "confirm_password": REG_PASS}).status_code, 403)
        self.assertTrue(self._env_has("YIBAN_GLOBAL_PAUSE=1"), "恢复不得由普通管理员完成")

    def test_any_admin_can_stop_a_different_admins_switch(self):
        """「急停」不是「任何管理员可改任何暂停」：注册暂停仍不在急停例外里。"""
        c = self._reg_admin()
        r = self._save(c, {"registration_pause": 1, "confirm_password": REG_PASS})
        self.assertEqual(r.status_code, 403, r.get_data(as_text=True))
        self.assertFalse(self._env_has("YIBAN_REGISTRATION_PAUSE=1"))

    def test_same_value_pause_needs_no_password(self):
        """已处于急停态时再交一次 1：值未变 → 不进门禁、不占额度。"""
        self.webapp.write_env_batch(self.env_file, {"YIBAN_GLOBAL_PAUSE": "1"})
        c = self._reg_admin()
        self.assertEqual(self._save(c, {"global_pause": 1}).status_code, 200)

    def test_stop_shares_high_risk_quota(self):
        """急停与「删数据 / 拆报警器」共用同一套高危额度（默认 5 次/60 秒）。"""
        c = self._master()
        codes = []
        for _ in range(self.webapp.ADMIN_DELETE_MAX + 1):
            # 1→0 是恢复（不占额度），0→1 才是急停
            self._save(c, {"global_pause": 0, "confirm_password": ADMIN_PASS})
            codes.append(self._save(
                c, {"global_pause": 1, "confirm_password": ADMIN_PASS}).status_code)
        self.assertEqual(codes[:-1], [200] * self.webapp.ADMIN_DELETE_MAX,
                         f"额度内应放行，实际 {codes}")
        self.assertEqual(codes[-1], 429, "超出高危额度后应 429")
        denied = self._save(c, {"global_pause": 1, "confirm_password": ADMIN_PASS})
        self.assertEqual(denied.status_code, 429)
        self.assertNotIn(str(self.webapp.ADMIN_DELETE_MAX), denied.get_json()["error"],
                         "429 文案不得泄露内部阈值参数")

    def test_wrong_password_does_not_consume_quota(self):
        """先鉴权后占额度：不知口令的被盗会话刷不光合法管理员的急停预算。"""
        # 冷却窗口关成 0（运维降级旋钮）：本例要钉的是「额度不被错口令吃掉」，
        # 留着冷却的话第 4 次起就是 429 而不是 403，测不到额度这一层
        self.webapp.write_env_batch(self.env_file, {"YIBAN_PW_CONFIRM_COOLDOWN_SEC": "0"})
        c = self._master()
        for _ in range(self.webapp.ADMIN_DELETE_MAX + 3):
            self._save(c, {"global_pause": 1, "confirm_password": "WrongPass999!"})
        r = self._save(c, {"global_pause": 1, "confirm_password": ADMIN_PASS})
        self.assertEqual(r.status_code, 200, f"错口令不得吃掉急停额度：{r.get_data(as_text=True)}")


class ChangeAlertTest(_TierBase):
    """变更告警：一次请求合并成一条，档位决定紧急度。"""

    def test_multi_key_request_emits_single_alert(self):
        c = self._master()
        r = self._save(c, {"sunday_sign": 1, "saturday_sign": 1, "gap_max": 30,
                           "sign_order": "random", "confirm_password": ADMIN_PASS})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(len(self.alerts), 1, f"一次请求只应有一条告警：{self.alerts}")
        title, body, urgent, force = self.alerts[0]
        self.assertIn("系统设置变更", title)
        self.assertTrue(urgent, "含 A 档变更必须 urgent")
        self.assertFalse(force, "非「拆报警器 / 不可逆清除 / 全停急停」不得 force")
        for frag in ("周日签到：关 → 开", "周六签到：关 → 开",
                     "签到排序：sequence → random", "操作者：admin"):
            self.assertIn(frag, body, f"告警正文缺少 {frag}")
        rows = self._audit_rows("settings_save")
        self.assertEqual(len(rows), 1, "审计仍是一行 settings_save")
        self.assertIn("周日签到=关→开", rows[0]["detail"], "审计须带真变化键的旧→新")

    def test_gated_only_change_is_non_urgent(self):
        c = self._master()
        r = self._save(c, {"sign_dist": "normal", "confirm_password": ADMIN_PASS})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(len(self.alerts), 1)
        self.assertFalse(self.alerts[0][2], "纯 B 档变更属非紧急")
        self.assertFalse(self.alerts[0][3])

    def test_emergency_stop_alert_is_forced(self):
        c = self._reg_admin()
        r = self._save(c, {"global_pause": 1, "confirm_password": REG_PASS})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(len(self.alerts), 1)
        _title, body, urgent, force = self.alerts[0]
        self.assertTrue(urgent and force, "全停急停要立刻叫醒：urgent + force")
        self.assertIn("全局暂停签到：关 → 开", body)

    def test_noop_save_sends_no_alert(self):
        """误点保存（值一个没变）不该发出任何告警，也不该被口令挡住。"""
        c = self._master()
        r = self._save(c, {"sunday_sign": 0,
                           "gap_max": self.webapp.DEFAULT_ACCOUNT_GAP_MAX})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(self.alerts, [], "无实质变更不得发告警（否则告警成了免费打字机）")

    def test_alert_body_has_no_raw_newline_from_values(self):
        """值里的换行/控制字符不得把告警正文撑成多行（伪造第二行文案）。"""
        c = self._master()
        r = self._save(c, {"probe_time": "06:00", "confirm_password": ADMIN_PASS})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(len(self.alerts), 1)
        body = self.alerts[0][1]
        lines = body.splitlines()
        # 行数不是判据（摘要/明细/时间之间本来就有空行）；真正要钉的是"每个部分各占
        # 一行"——值若夹带裸换行，明细行就会裂成两行。
        self.assertEqual(sum(ln.startswith("· 探针时刻") for ln in lines), 1,
                         f"值不得把明细项撑成多行：{body!r}")
        self.assertEqual(sum(ln.startswith("· 操作者") for ln in lines), 1)
        self.assertEqual(sum(ln.startswith("时间：") for ln in lines), 1)


if __name__ == "__main__":
    unittest.main()
