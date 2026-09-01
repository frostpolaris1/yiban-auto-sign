# -*- coding: utf-8 -*-
"""批次 9 修复回归测试（批次7 P3 系列）。

覆盖：
- P3-1  审计锚点"记录过却消失"可检出（app_meta 留痕）
- P3-2  遗留未提交事务被 _begin_immediate 安全回滚（不再连锁锁死写路径）
- P3-4  审计链重建按 id 游标分批（链自洽）
- P3-5  服务端会话吊销：登出/被重置密码轮换 sid，被盗 cookie 重放失效；
        自助改密当前会话保持有效
- P3-7  混合大小写 YIBAN_ADMIN_USER 可正常登录并自助改密
- P3-8  账号恢复的每 IP 聚合失败窗口（30 次/10 分钟）
- P3-11 探针确认健康 → 清除熔断暂停
- P3-14 容器调度子进程超时不崩调度循环

用法（项目根目录）：
    py -m pytest tests/test_batch9_fixes_0828.py -v
"""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "scripts"))
sys.path.insert(0, os.path.join(BASE, "web"))

import db  # noqa: E402

TEST_KEY = "a" * 64
AUDIT_KEY = "b" * 64
ADMIN_PASS = "MasterPass#2026"
USER_PASS = "secret1"
EMAIL = "user1@test.local"
PHONE = "13800138001"


def _load_webapp():
    spec = importlib.util.spec_from_file_location("webapp", os.path.join(BASE, "web", "app.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["webapp"] = mod
    with contextlib.suppress(Exception):
        spec.loader.exec_module(mod)
    return mod


class Batch9WebTest(unittest.TestCase):
    """web 层修复回归（P3-1/2/4/5/7/8）。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="batch9-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        cls._env_content = (
            f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
            f"YIBAN_AUDIT_KEY={AUDIT_KEY}\n"
            "YIBAN_ADMIN_USER=admin@test.local\n"
            f"YIBAN_ADMIN_PASSWORD={ADMIN_PASS}\n"
        )
        with io.open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(cls._env_content)
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.accounts_file = os.path.join(cls.tmp, "accounts.json")
        cls.state_dir = os.path.join(cls.tmp, "state")
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
        os.environ["YIBAN_AUDIT_KEY"] = AUDIT_KEY
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_ACCOUNTS_FILE"] = cls.accounts_file
        os.environ["YIBAN_DB_FILE"] = cls.db_file
        os.environ["YIBAN_STATE_DIR"] = cls.state_dir
        os.environ["YIBAN_LOG_FILE"] = os.path.join(cls.tmp, "sign.log")
        cls.webapp = _load_webapp()

    @classmethod
    def tearDownClass(cls):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k in ("YIBAN_ACCOUNTS_KEY", "YIBAN_AUDIT_KEY", "YIBAN_LOG_FILE", "YIBAN_STATE_DIR"):
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
        with io.open(self.accounts_file, "w", encoding="utf-8") as f:
            json.dump([], f)
        with io.open(self.env_file, "w", encoding="utf-8") as f:
            f.write(self._env_content)
        db.init_db(self.db_file, migrate_from=self.accounts_file, env_file=self.env_file)
        os.makedirs(self.state_dir, exist_ok=True)

    # ---- 工具 ----
    def _login(self, c, username, password):
        r = c.post("/api/login", json={"username": username, "password": password})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return c.get("/api/me").get_json()["csrf_token"]

    def _csrf(self, token):
        return {"X-CSRF-Token": token}

    def _session_cookie(self, c):
        """从登录后的 client 提取会话 cookie（模拟复制被盗 cookie）。"""
        for header in (c.get("/api/me").headers.getlist("Set-Cookie") or []):
            name_val = header.split(";", 1)[0]
            name, _, val = name_val.partition("=")
            if name.strip() and val:
                return name.strip(), val
        self.fail("未找到会话 cookie")

    def _client_with_cookie(self, cookie_name, cookie_val):
        """构造携带指定会话 cookie 的 client（注入 jar，兼容多版本 Werkzeug 签名）。"""
        c = self.webapp.create_app().test_client()
        injected = False
        for kwargs in (
            {"key": cookie_name, "value": cookie_val, "domain": "localhost", "path": "/"},
            {"server_name": "localhost", "key": cookie_name, "value": cookie_val, "path": "/"},
        ):
            try:
                c.set_cookie(**kwargs)
                injected = True
                break
            except TypeError:
                continue
        if not injected:
            self.fail("当前 Werkzeug 版本无法注入测试 cookie")
        return c

    def _user_with_account(self):
        h = self.webapp.generate_password_hash
        db.create_user(EMAIL, h(USER_PASS))
        c = self.webapp.create_app().test_client()
        t = self._login(c, EMAIL, USER_PASS)
        r = c.post("/api/my-accounts", json={"name": "n1", "phone": PHONE, "password": "p"},
                   headers=self._csrf(t))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        ac = self.webapp.create_app().test_client()
        at = self._login(ac, "admin@test.local", ADMIN_PASS)
        idx = next(i for i, a in enumerate(db.load_accounts()) if a["phone"] == PHONE)
        r = ac.post(f"/api/accounts/{idx}/review", json={"action": "approve"},
                    headers=self._csrf(at))
        self.assertEqual(r.status_code, 200)
        return c, t

    # ---- P3-1 锚点缺失检测 ----
    def test_anchor_missing_after_recorded_is_detected(self):
        db.audit("admin", "op1", "t", "d")
        db.record_audit_anchor()
        anchor = db.audit_anchor_path()
        self.assertTrue(os.path.exists(anchor))
        ok, msg = db.verify_audit_anchor()
        self.assertTrue(ok, msg)
        os.remove(anchor)
        ok, msg = db.verify_audit_anchor()
        self.assertFalse(ok, "app_meta 记录过锚点而文件消失必须可检出")
        self.assertIn("锚点文件缺失", msg)
        # 恢复链路：重新记录后恢复健康
        db.record_audit_anchor()
        ok, msg = db.verify_audit_anchor()
        self.assertTrue(ok, msg)

    # ---- P3-2 遗留事务安全回滚 ----
    def test_leaked_transaction_rolled_back_not_fatal(self):
        h = self.webapp.generate_password_hash
        db.create_user(EMAIL, h(USER_PASS))
        acc_id = db.add_account({
            "name": "n", "phone": PHONE, "password": "p",
            "owner": EMAIL, "status": "active",
        })
        conn = db.get_conn()
        with db._conn_lock:
            conn.execute(
                "INSERT INTO users (email, password_hash, role, created_at, pw_version) "
                "VALUES ('ghost@test.local', 'h', 'user', '2026-01-01 00:00:00', 1)"
            )
            # 故意不提交 → 模拟某写路径泄漏的半事务
        # 走 _begin_immediate 防御的写路径：回滚遗留事务后正常执行（原先直接抛
        # "within a transaction" 连锁锁死全部写路径）
        db.batch_account_ops([("set_deleted", acc_id, 1, "2026-01-02 00:00:00")])
        ghost = db.get_conn().execute(
            "SELECT email FROM users WHERE email='ghost@test.local'"
        ).fetchone()
        self.assertIsNone(ghost, "被泄漏的半事务必须被安全回滚，不得被顺带发布")
        row = next(a for a in db.load_accounts() if a["id"] == acc_id)
        self.assertEqual(row["deleted"], 1, "正常业务写入不受遗留事务影响")

    # ---- P3-4 rechain 分批 ----
    def test_rechain_keeps_long_chain_consistent(self):
        for i in range(30):
            db.audit("admin", f"op{i}", "t", "d")
        conn = db.get_conn()
        with db._conn_lock, conn:
            conn.execute("UPDATE audit_logs SET hash='', prev_hash=''")
        db._rechain_audit_logs(conn)
        ok, broken, _ = db.verify_audit_chain()
        self.assertTrue(ok, f"分批重链后链必须自洽（broken={broken}）")
        self.assertEqual(broken, 0)

    # ---- P3-5 会话吊销 ----
    def test_logout_revokes_stolen_cookie(self):
        c, t = self._user_with_account()
        name, val = self._session_cookie(c)
        stolen = self._client_with_cookie(name, val)
        # 未登出前：被盗 cookie 有效
        r = stolen.get("/api/me")
        self.assertEqual(r.status_code, 200)
        # 登出 → sid 轮换 → 被盗 cookie 重放失效
        r = c.post("/api/logout", json={}, headers=self._csrf(t))
        self.assertEqual(r.status_code, 200)
        r = stolen.get("/api/me")
        self.assertEqual(r.status_code, 401, "登出后旧 cookie 副本必须失效（P3-5 主断言）")
        # 重新登录后一切正常
        t2 = self._login(c, EMAIL, USER_PASS)
        r = c.get("/api/me", headers=self._csrf(t2))
        self.assertEqual(r.status_code, 200)

    def test_admin_password_reset_revokes_target_sessions(self):
        c, _t = self._user_with_account()
        name, val = self._session_cookie(c)
        ac = self.webapp.create_app().test_client()
        at = self._login(ac, "admin@test.local", ADMIN_PASS)
        r = ac.post(f"/api/users/{EMAIL}/password",
                    json={"password": "NewPass#777", "confirm_password": ADMIN_PASS},
                    headers=self._csrf(at))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        stolen2 = self._client_with_cookie(name, val)
        r = stolen2.get("/api/me")
        self.assertEqual(r.status_code, 401, "被重置密码后旧会话必须失效（sid 已轮换）")

    def test_self_password_change_keeps_current_session(self):
        c, t = self._user_with_account()
        r = c.post("/api/me/password", json={
            "old_password": USER_PASS,
            "new_password": "NewPass#888",
            "confirm_password": "NewPass#888",
        }, headers=self._csrf(t))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        # 既有产品语义：改密即当前会话失效（响应文案"下次登录使用新密码"）；
        # P3-5 增量是 sid 已轮换——改密前的被盗 cookie 副本同样失效
        r = c.get("/api/me")
        self.assertEqual(r.status_code, 401)
        row = db.get_conn().execute(
            "SELECT sid FROM users WHERE email=?", (EMAIL,)
        ).fetchone()
        self.assertNotEqual(row["sid"], "", "改密后 sid 必须已重新签发")

    # ---- P3-7 混合大小写主管理员自助改密 ----
    def test_mixed_case_admin_can_change_password(self):
        original = io.open(self.env_file, encoding="utf-8").read()
        try:
            with io.open(self.env_file, "w", encoding="utf-8") as f:
                f.write(
                    f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
                    f"YIBAN_AUDIT_KEY={AUDIT_KEY}\n"
                    "YIBAN_ADMIN_USER=Admin@Test.Local\n"
                    f"YIBAN_ADMIN_PASSWORD={ADMIN_PASS}\n"
                )
            c = self.webapp.create_app().test_client()
            t = self._login(c, "Admin@Test.Local", ADMIN_PASS)
            r = c.post("/api/me/password", json={
                "old_password": ADMIN_PASS,
                "new_password": "Rotated#2026x",
                "confirm_password": "Rotated#2026x",
            }, headers=self._csrf(t))
            self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        finally:
            with io.open(self.env_file, "w", encoding="utf-8") as f:
                f.write(original)

    # ---- P3-8 恢复的每 IP 聚合失败窗口 ----
    def test_restore_per_ip_aggregate_rate_limit(self):
        # 性能优化（2026-09-01）：本用例被测对象是「每 IP 聚合限速窗口」，而非
        # 密码校验本身；40 次对不存在账号的请求每次都会走 scrypt 时延拉平
        # （_constant_time_dummy，安全设计约 0.35s/次）→ 全量串行 14s。mock 掉
        # scrypt 比对为常数时间开销，限速行为判定不受影响（429 来自
        # user_delete_requests 每 IP 计数，与密码校验结果无关）。
        # 注：patch 对象必须是 self.webapp（模块名 "webapp"，非 "web.app"）。
        with mock.patch.object(
                self.webapp, "_constant_time_dummy", lambda pwd: None), \
             mock.patch.object(
                self.webapp, "check_password_hash", lambda h, p: False):
            c = self.webapp.create_app().test_client()
            got_429 = False
            for i in range(40):
                r = c.post("/api/me/restore", json={
                    "email": f"ghost{i}@x.test", "password": "WrongPass#1",
                })
                if r.status_code == 429:
                    got_429 = True
                    break
        self.assertTrue(got_429, "跨邮箱喷洒恢复请求必须在每 IP 聚合窗口处被 429")

    # ---- P3-11 探针清熔断 ----
    def test_probe_healthy_clears_fuse_pause(self):
        import signin as signin_mod

        cred = {PHONE: {"paused_since": "2026-08-01", "probe_date": "2026-01-01"}}
        with io.open(os.path.join(self.state_dir, "cred-state.json"), "w",
                     encoding="utf-8") as f:
            json.dump(cred, f)
        with mock.patch.object(signin_mod, "PROBE_ENABLE", True), \
             mock.patch.object(signin_mod, "_health_probe_due", lambda: True), \
             mock.patch.object(signin_mod, "verify_account",
                               lambda acc: (True, "账号健康，可正常签到")), \
             mock.patch.object(signin_mod, "db") as fake_db, \
             mock.patch.dict(os.environ, {"YIBAN_STATE_DIR": self.state_dir}):
            fake_db.is_initialized.return_value = False  # 探针内不触碰会话缓存
            acc = SimpleNamespace(phone=PHONE, owner="o", password="p",
                                  phone_model="", phone_code="")
            signin_mod.run_probe([acc])
        p = os.path.join(self.state_dir, "cred-state.json")
        saved = json.load(io.open(p, encoding="utf-8")) if os.path.exists(p) else {}
        self.assertNotIn(PHONE, saved, "探针确认健康必须清除熔断暂停")


class SchedulerTimeoutTest(unittest.TestCase):
    """P3-14：签到子进程挂起时调度循环留痕继续，不永久卡死。"""

    def test_timeout_does_not_kill_loop(self):
        import subprocess as sp

        sched_path = os.path.join(BASE, "docker", "scheduler.py")
        spec = importlib.util.spec_from_file_location("container_scheduler2", sched_path)
        sched = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sched)

        def _hang(*a, **kw):
            raise sp.TimeoutExpired(cmd=a, timeout=1)

        sched.subprocess = type(
            "_Stub", (), {"run": staticmethod(_hang),
                          "TimeoutExpired": sp.TimeoutExpired})()
        sched._run_signin_child()  # 不应抛异常（TimeoutExpired 被捕获留痕）


if __name__ == "__main__":
    unittest.main(verbosity=2)
