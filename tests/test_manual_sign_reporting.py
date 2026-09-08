# -*- coding: utf-8 -*-
"""手动签到与账号编辑守卫回归（2026-09-08）。

逐项活体复现 + 修复钉版：
1. 仅凭据（密码/手机号）实际变更才清熔断计数，只改备注不再重置 fail_days
2. 手动签到子进程非 0 退出码透传到签到日志（用户可见真实失败原因）
"""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest import mock

import db

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TEST_KEY = "a" * 64
ADMIN_PASS = "MasterPass#2026"
USER_PASS = "secret1"


class _WebAppMixin:
    """web/app.py 隔离加载（独立 .env / db / 状态目录）。"""

    @classmethod
    def _isolate_env(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-web-sign-admin-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with io.open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(
                f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
                f"YIBAN_ADMIN_USER=admin\nYIBAN_ADMIN_PASSWORD={ADMIN_PASS}\n"
            )
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.accounts_file = os.path.join(cls.tmp, "accounts.json")
        cls.users_file = os.path.join(cls.tmp, "users.json")
        cls.log_file = os.path.join(cls.tmp, "sign.log")
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_ACCOUNTS_FILE"] = cls.accounts_file
        os.environ["YIBAN_USERS_FILE"] = cls.users_file
        os.environ["YIBAN_DB_FILE"] = cls.db_file
        os.environ["YIBAN_STATE_DIR"] = cls.tmp
        os.environ["YIBAN_LOG_FILE"] = cls.log_file
        spec = importlib.util.spec_from_file_location(
            "webapp_sign_admin", os.path.join(BASE, "web", "app.py"))
        cls.webapp = importlib.util.module_from_spec(spec)
        sys.modules["webapp_sign_admin"] = cls.webapp
        with contextlib.suppress(Exception):
            spec.loader.exec_module(cls.webapp)

    @classmethod
    def _teardown_env(cls):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        for k in ("YIBAN_ACCOUNTS_KEY", "YIBAN_ENV_FILE", "YIBAN_ACCOUNTS_FILE",
                  "YIBAN_USERS_FILE", "YIBAN_DB_FILE", "YIBAN_STATE_DIR",
                  "YIBAN_LOG_FILE"):
            os.environ.pop(k, None)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _reset_db(self):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        for suffix in ("", "-wal", "-shm"):
            p = self.db_file + suffix
            if os.path.exists(p):
                os.remove(p)
        with io.open(self.accounts_file, "w", encoding="utf-8") as f:
            json.dump([], f)
        db.init_db(self.db_file, migrate_from=self.accounts_file,
                   env_file=self.env_file)

    def _seed_fuse_pause(self, phone, fail_days=3):
        """写入熔断暂停条目（fail_days=3 表示已连续失败 3 天）。"""
        path = os.path.join(self.tmp, "cred-state.json")
        with io.open(path, "w", encoding="utf-8") as f:
            json.dump({phone: {
                "fail_days": fail_days,
                "last_fail": "2026-09-07",
                "paused_since": "2026-09-07 07:00:00",
            }}, f)

    def _read_fuse_pause(self):
        path = os.path.join(self.tmp, "cred-state.json")
        if not os.path.exists(path):
            return {}
        with io.open(path, encoding="utf-8") as f:
            return json.load(f)

    def _login_admin(self, c):
        r = c.post("/api/login", json={"username": "admin", "password": ADMIN_PASS})
        assert r.status_code == 200, r.get_data(as_text=True)
        return c.get("/api/me").get_json()["csrf_token"]

    def _csrf(self, token):
        return {"X-CSRF-Token": token}


# ---------------------------------------------------------------------------
# 熔断计数不可被任意编辑重置
# ---------------------------------------------------------------------------
class FuseResetGateTest(_WebAppMixin, unittest.TestCase):
    """管理员/用户编辑账号：仅凭据变更清熔断，只改备注保持 fail_days。"""

    PHONE = "13800138000"

    @classmethod
    def setUpClass(cls):
        cls._isolate_env()

    @classmethod
    def tearDownClass(cls):
        cls._teardown_env()

    def setUp(self):
        self._reset_db()
        self._seed_fuse_pause(self.PHONE)
        # 被编辑账号（不经真实 db 写路径：本用例聚焦 clear_fuse_pause 的触发条件）
        self._account = {
            "id": 1, "phone": self.PHONE, "password": "OldPass123",
            "name": "张三", "phone_code": "", "owner": "admin",
            "status": "active", "deleted": False,
        }
        patcher = mock.patch.object(
            self.webapp, "load_accounts",
            side_effect=lambda: [dict(self._account)])
        patcher.start()
        self.addCleanup(patcher.stop)
        db_patch = mock.patch.object(self.webapp.db, "update_account", return_value=True)
        db_patch.start()
        self.addCleanup(db_patch.stop)

    def _admin_client(self):
        c = self.webapp.create_app().test_client()
        token = self._login_admin(c)
        return c, token

    def test_admin_note_only_edit_keeps_fail_days(self):
        """只改备注：熔断条目原样保留（fail_days 不清零，熔断仍会跳闸）。"""
        c, token = self._admin_client()
        r = c.put("/api/accounts/0",
                  json={"phone": self.PHONE, "name": "只改备注", "password": ""},
                  headers=self._csrf(token))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        entry = self._read_fuse_pause().get(self.PHONE)
        self.assertIsNotNone(entry, "只改备注不得清除熔断暂停条目")
        self.assertEqual(entry.get("fail_days"), 3)

    def test_admin_password_change_clears_fuse_pause(self):
        """改密码（凭据变更）：熔断条目清除，账号立即恢复签到资格。"""
        c, token = self._admin_client()
        r = c.put("/api/accounts/0",
                  json={"phone": self.PHONE, "name": "x", "password": "NewPass456"},
                  headers=self._csrf(token))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertNotIn(self.PHONE, self._read_fuse_pause())

    def test_user_note_only_edit_keeps_fail_days(self):
        """用户侧只改备注：同样不得重置熔断计数。"""
        db.create_user("user1@test.local", self.webapp.generate_password_hash(USER_PASS))
        self._account["owner"] = "user1@test.local"
        c = self.webapp.create_app().test_client()
        r = c.post("/api/login", json={"username": "user1@test.local",
                                       "password": USER_PASS})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        token = c.get("/api/me").get_json()["csrf_token"]
        r = c.put("/api/my-accounts/0",
                  json={"phone": self.PHONE, "name": "只改备注", "password": ""},
                  headers=self._csrf(token))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        entry = self._read_fuse_pause().get(self.PHONE)
        self.assertIsNotNone(entry, "用户只改备注不得清除熔断暂停条目")
        self.assertEqual(entry.get("fail_days"), 3)


# ---------------------------------------------------------------------------
# 手动签到非 0 退出码透传
# ---------------------------------------------------------------------------
class ManualSignExitTest(_WebAppMixin, unittest.TestCase):
    """手动签到子进程 exit 3（运行锁占用）等不得表现为无声成功。"""

    PHONE = "13800138000"

    @classmethod
    def setUpClass(cls):
        cls._isolate_env()

    @classmethod
    def tearDownClass(cls):
        cls._teardown_env()

    def setUp(self):
        self._reset_db()

    def _today_log(self):
        path = os.path.join(
            self.tmp, f"sign-{datetime.now().strftime('%Y-%m-%d')}.log")
        return path

    def _wait_reap(self):
        for _ in range(50):
            time.sleep(0.05)
            if os.path.exists(self._today_log()):
                with io.open(self._today_log(), encoding="utf-8") as f:
                    if "手动签到未完成" in f.read():
                        return True
        return False

    def test_exit_reason_mapping(self):
        """退出码 → 用户可见原因：3=队列忙，2=跳过，其他=异常退出，0=None。"""
        f = self.webapp._manual_sign_failure_reason
        self.assertIsNone(f(0))
        self.assertIn("队列忙", f(3))
        self.assertIn("跳过", f(2))
        self.assertIn("退出码 1", f(1))

    def test_single_manual_sign_exit3_logged_as_failure(self):
        """单号手动签到子进程 exit 3：签到日志出现失败留痕（前端日志页可见）。"""
        c = self.webapp.create_app().test_client()
        token = self._login_admin(c)
        accounts = [{"phone": self.PHONE, "status": "active", "deleted": False}]
        proc = SimpleNamespace(returncode=3, pid=4321)
        proc.wait = lambda timeout=None: 3
        proc.poll = lambda: 3
        proc.terminate = lambda: None
        proc.kill = lambda: None
        with mock.patch.object(self.webapp, "load_accounts",
                               return_value=accounts), \
             mock.patch.object(self.webapp.subprocess, "Popen",
                               return_value=proc):
            r = c.post("/api/signin", json={"phone": self.PHONE},
                       headers=self._csrf(token))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(
            self._wait_reap(),
            "子进程 exit 3 必须在签到日志留下失败留痕（用户侧唯一结果通道）",
        )

    def test_batch_manual_sign_exit3_logged_as_failure(self):
        """批量手动签到子进程 exit 3：同样在签到日志留痕。"""
        c = self.webapp.create_app().test_client()
        token = self._login_admin(c)
        accounts = [{"phone": self.PHONE, "status": "active", "deleted": False}]
        proc = SimpleNamespace(returncode=3, pid=4322)
        proc.wait = lambda timeout=None: 3
        proc.poll = lambda: 3
        proc.terminate = lambda: None
        proc.kill = lambda: None
        with mock.patch.object(self.webapp, "load_accounts",
                               return_value=accounts), \
             mock.patch.object(self.webapp.subprocess, "Popen",
                               return_value=proc):
            r = c.post("/api/signin/batch", json={"ids": [0], "phones": [self.PHONE]},
                       headers=self._csrf(token))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(self._wait_reap())


if __name__ == "__main__":
    unittest.main(verbosity=2)
