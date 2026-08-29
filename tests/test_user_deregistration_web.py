# -*- coding: utf-8 -*-
"""用户自助注销 Web/API 层测试（数据库 v5 软删除对接）。

覆盖 docs/design/plan-frontend-user-deregistration.md 第 3 章 API 契约：
- 登录要求 401 / CSRF 缺失 403
- 密码确认：错误 400、连续失败达阈值锁定 429
- 防批量冷却：用户维度 60s 1 次、IP 维度 60s 5 次 → 429（不暴露秒数）
- 管理员保护：内置管理员 400、最后一个注册管理员 400
- 成功路径：软删除标记 + 账号清除 + 会话失效 + 审计留痕 + 邮箱可重新注册

全程 mock / 纯本地（Flask test client），无任何网络请求。
用法（项目根目录）：
    py -m pytest tests/test_user_deregistration_web.py -v
"""
import contextlib
import hashlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "scripts"))
sys.path.insert(0, os.path.join(BASE, "web"))

TEST_KEY = "a" * 64
ADMIN_PASS = "TestPass1234!"
USER_PASS = "secret1"


class UserDeregistrationWebTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-del-")
        cls.env_file = os.path.join(cls.tmp, ".env")
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
        db.create_user("admin@test.local", self.webapp.generate_password_hash(ADMIN_PASS), role="admin")
        db.create_user("user1@test.local", self.webapp.generate_password_hash(USER_PASS))
        db.add_account({"name": "U1", "phone": "13800138001", "password": "p1",
                        "status": "active", "owner": "user1@test.local"})

    def _login(self, c, username, password):
        r = c.post("/api/login", json={"username": username, "password": password})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return c.get("/api/me").get_json()["csrf_token"]

    def _delete(self, c, token, password="x"):
        return c.post("/api/me/delete", json={"password": password},
                      headers={"X-CSRF-Token": token})

    def _audit_actions(self):
        import sqlite3
        conn = sqlite3.connect(self.db_file)
        try:
            return [r[0] for r in conn.execute(
                "SELECT action FROM audit_logs ORDER BY id")]
        finally:
            conn.close()

    # ---- 基础防护 ----
    def test_requires_login(self):
        c = self.webapp.create_app().test_client()
        r = self._delete(c, "t")
        self.assertEqual(r.status_code, 401)

    def test_csrf_required(self):
        c = self.webapp.create_app().test_client()
        self._login(c, "user1@test.local", USER_PASS)
        r = c.post("/api/me/delete", json={"password": USER_PASS})
        self.assertEqual(r.status_code, 403, "缺少 X-CSRF-Token 应被全局 CSRF 校验拦截")

    def test_empty_password(self):
        c = self.webapp.create_app().test_client()
        token = self._login(c, "user1@test.local", USER_PASS)
        r = self._delete(c, token, password="")
        self.assertEqual(r.status_code, 400)
        self.assertIn("密码", r.get_json()["error"])

    def test_wrong_password(self):
        c = self.webapp.create_app().test_client()
        token = self._login(c, "user1@test.local", USER_PASS)
        r = self._delete(c, token, password="wrong-pass")
        self.assertEqual(r.status_code, 400)
        self.assertIn("密码不正确", r.get_json()["error"])
        u = db.find_user("user1@test.local")
        self.assertIsNotNone(u, "密码错误不应注销用户")
        self.assertEqual(u["deleted"], 0)

    def test_failed_password_lockout(self):
        c = self.webapp.create_app().test_client()
        token = self._login(c, "user1@test.local", USER_PASS)
        max_fails = self.webapp.LOGIN_MAX_FAILS
        for i in range(max_fails - 1):
            r = self._delete(c, token, password="bad")
            self.assertEqual(r.status_code, 400, f"第 {i + 1} 次错误应为 400")
        r = self._delete(c, token, password="bad")  # 达阈值 → 锁定 429
        self.assertEqual(r.status_code, 429)
        self.assertNotIn(str(self.webapp.LOGIN_LOCK_SECONDS), r.get_json()["error"],
                         "不应暴露锁定时长")

    # ---- 成功路径 ----
    def test_delete_success(self):
        c = self.webapp.create_app().test_client()
        token = self._login(c, "user1@test.local", USER_PASS)
        r = self._delete(c, token, password=USER_PASS)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertIn("7 天内可撤销", r.get_json()["msg"])
        # 软删除标记 + 账号软删除（7 天保留，安全审查 2026-08-16）
        u = db.find_user_any("user1@test.local")
        self.assertEqual(u["deleted"], 1)
        self.assertTrue(u["deleted_at"])
        accs = db.load_accounts_raw()
        self.assertFalse(
            [a for a in accs if a["owner"] == "user1@test.local" and not a["deleted"]],
            "注销后该用户不应有未删除的易班账号")
        # 会话已清（注销即登出：再访问 /api/me 未登录 → 401）
        me = c.get("/api/me")
        self.assertEqual(me.status_code, 401)
        # 审计留痕
        actions = self._audit_actions()
        self.assertIn("user_self_delete_request", actions)
        self.assertIn("user_self_delete_confirm", actions)
        # 冷却期邮箱保护（安全审查 2026-08-16）：注销后 7 天内同邮箱注册被拒（恢复权不被抢占）
        r = c.post("/api/register", json={"email": "user1@test.local", "password": "newpass1234", "agree": True})
        self.assertEqual(r.status_code, 400, "冷却期内同邮箱注册应被拒")

    # ---- 管理员视图（v0.20.1：注销不发通知，改主动查看）----
    def test_admin_sees_deleted_users(self):
        c = self.webapp.create_app().test_client()
        token = self._login(c, "user1@test.local", USER_PASS)
        self._delete(c, token, password=USER_PASS)
        # 管理员查看已注销列表
        c2 = self.webapp.create_app().test_client()
        self._login(c2, "admin", ADMIN_PASS)
        r = c2.get("/api/users/deleted")
        self.assertEqual(r.status_code, 200)
        items = r.get_json()["items"]
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["email"], "user1@test.local")
        self.assertEqual(item["status"], "cooling")
        self.assertGreaterEqual(item["remaining_days"], 6, "刚注销应剩约 7 天（整天向下取整 ≥6）")
        self.assertTrue(item["deleted_at"])

    def test_deleted_users_expired_shows_purge_pending(self):
        # 先 create_app() 拿 client 并登录，再构造过期注销用户；
        # 否则 init_db 启动清理会在 create_app() 时把 8 天前用户直接清除，
        # 测试意图是验证 API 对“待 purge 用户”的展示语义。
        c = self.webapp.create_app().test_client()
        self._login(c, "admin", ADMIN_PASS)
        db.create_user("expired@test.local", "hash")
        db.soft_delete_user_with_accounts("expired@test.local")
        conn = db.get_conn()
        old = (datetime.now() - timedelta(days=8)).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute("UPDATE users SET deleted_at=? WHERE email=?", (old, "expired@test.local"))
        conn.commit()
        r = c.get("/api/users/deleted")
        items = r.get_json()["items"]
        item = next(i for i in items if i["email"] == "expired@test.local")
        self.assertEqual(item["status"], "purge_pending")
        self.assertEqual(item["remaining_days"], 0)

    def test_deleted_users_expired_before_app_start_are_purged_by_cleanup(self):
        # 反向回归：若在 create_app() 之前就构造 8 天前注销用户，
        # 默认 init_db(cleanup=True) 的启动清理会把它物理清除——这正是
        # test_deleted_users_expired_shows_purge_pending 必须调整顺序的原因。
        db.create_user("expired-boot@test.local", "hash")
        db.soft_delete_user_with_accounts("expired-boot@test.local")
        conn = db.get_conn()
        old = (datetime.now() - timedelta(days=8)).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute("UPDATE users SET deleted_at=? WHERE email=?", (old, "expired-boot@test.local"))
        conn.commit()
        # 模拟进程重启：关闭当前连接，让 create_app() 重新执行 init_db 启动清理
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        c = self.webapp.create_app().test_client()
        self._login(c, "admin", ADMIN_PASS)
        r = c.get("/api/users/deleted")
        emails = [i["email"] for i in r.get_json()["items"]]
        self.assertNotIn("expired-boot@test.local", emails)

    def test_deleted_users_requires_admin(self):
        c = self.webapp.create_app().test_client()
        self._login(c, "user1@test.local", USER_PASS)
        r = c.get("/api/users/deleted")
        self.assertEqual(r.status_code, 403, "普通用户无权查看已注销列表")

    def test_deleted_users_empty(self):
        c = self.webapp.create_app().test_client()
        self._login(c, "admin", ADMIN_PASS)
        r = c.get("/api/users/deleted")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["items"], [])

    # ---- 管理员手动清除已注销用户（2026-08-17）----
    def _purge(self, c, token, emails):
        # 2026-08-29 二次鉴权：彻底清除不可逆，须输入当前管理员密码
        return c.post("/api/users/deleted/purge",
                      json={"emails": emails, "confirm_password": ADMIN_PASS},
                      headers={"X-CSRF-Token": token})

    def test_purge_deleted_users_success(self):
        # user1 注销（带账号与自选时间）→ 管理员立即清除 → 用户/账号/自选时间全没了
        db.set_time_pref("13800138001", 0, "2026-08-17 10:00:00")
        c = self.webapp.create_app().test_client()
        token = self._login(c, "user1@test.local", USER_PASS)
        self._delete(c, token, password=USER_PASS)
        c2 = self.webapp.create_app().test_client()
        admin_token = self._login(c2, "admin", ADMIN_PASS)
        r = self._purge(c2, admin_token, ["user1@test.local"])
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json()["purged"], ["user1@test.local"])
        self.assertIsNone(db.find_user_any("user1@test.local"), "用户行应被物理删除")
        accs = [a for a in db.load_accounts_raw() if a["owner"] == "user1@test.local"]
        self.assertEqual(accs, [], "其易班账号行应被物理删除")
        self.assertIsNone(db.get_time_pref("13800138001"), "其自选时间应被物理删除")
        actions = self._audit_actions()
        self.assertIn("user_deleted_purge", actions)

    def test_purge_skips_active_user(self):
        # 传入活跃用户邮箱 → 跳过不误删（安全边界：仅清 deleted=1 行）
        c = self.webapp.create_app().test_client()
        admin_token = self._login(c, "admin", ADMIN_PASS)
        r = self._purge(c, admin_token, ["user1@test.local"])
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["purged"], [])
        self.assertEqual(r.get_json()["skipped"], ["user1@test.local"])
        self.assertIsNotNone(db.find_user("user1@test.local"), "活跃用户不应被删除")

    def test_purge_requires_admin(self):
        c = self.webapp.create_app().test_client()
        token = self._login(c, "user1@test.local", USER_PASS)
        r = self._purge(c, token, ["user1@test.local"])
        self.assertEqual(r.status_code, 403, "普通用户无权清除已注销用户")

    def test_purge_empty_emails(self):
        c = self.webapp.create_app().test_client()
        admin_token = self._login(c, "admin", ADMIN_PASS)
        r = self._purge(c, admin_token, [])
        self.assertEqual(r.status_code, 400)

    # ---- 管理员保护 ----
    def test_builtin_admin_cannot_delete(self):
        c = self.webapp.create_app().test_client()
        token = self._login(c, "admin", ADMIN_PASS)
        r = self._delete(c, token, password=ADMIN_PASS)
        self.assertEqual(r.status_code, 400)
        self.assertIn("不可注销", r.get_json()["error"])

    def test_last_registered_admin_cannot_delete(self):
        # user1 提升为管理员，且原测试管理员软删 → user1 是最后一个活跃注册管理员
        db.update_user("user1@test.local", {"role": "admin"})
        db.soft_delete_user_with_accounts("admin@test.local")
        c = self.webapp.create_app().test_client()
        token = self._login(c, "user1@test.local", USER_PASS)
        r = self._delete(c, token, password=USER_PASS)
        self.assertEqual(r.status_code, 400)
        self.assertIn("最后一个管理员", r.get_json()["error"])
        self.assertEqual(db.find_user("user1@test.local")["deleted"], 0)

    # ---- 防批量冷却 ----
    def test_cooldown_per_user(self):
        db.record_user_delete_request("user1@test.local", ip_hash="x")
        c = self.webapp.create_app().test_client()
        token = self._login(c, "user1@test.local", USER_PASS)
        r = self._delete(c, token, password=USER_PASS)
        self.assertEqual(r.status_code, 429)
        self.assertNotIn("60", r.get_json()["error"], "不应暴露冷却秒数")

    def test_cooldown_per_ip(self):
        ip_hash = hashlib.sha256(b"127.0.0.1").hexdigest()
        for i in range(5):
            db.record_user_delete_request(f"other{i}@test.local", ip_hash=ip_hash)
        c = self.webapp.create_app().test_client()
        token = self._login(c, "user1@test.local", USER_PASS)
        r = self._delete(c, token, password=USER_PASS)
        self.assertEqual(r.status_code, 429)

    # ---- 冷静期登录即恢复（v0.20.3，2026-08-16 用户裁决）----
    def test_login_recoverable_flag(self):
        c = self.webapp.create_app().test_client()
        token = self._login(c, "user1@test.local", USER_PASS)
        self._delete(c, token, password=USER_PASS)
        # 冷静期内密码正确 → 200 recoverable（不放行登录）
        r = c.post("/api/login", json={"username": "user1@test.local", "password": USER_PASS})
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertTrue(data.get("recoverable"))
        self.assertIn("7 天内可恢复", data.get("msg", ""))
        # 不建立会话
        me = c.get("/api/me")
        self.assertEqual(me.status_code, 401)

    def test_login_recoverable_wrong_password_still_fails(self):
        c = self.webapp.create_app().test_client()
        token = self._login(c, "user1@test.local", USER_PASS)
        self._delete(c, token, password=USER_PASS)
        r = c.post("/api/login", json={"username": "user1@test.local", "password": "bad-pass"})
        self.assertEqual(r.status_code, 401, "密码错误不返回 recoverable 标记")

    def test_restore_success(self):
        # 直接 DB 构造冷却中账号（不经 /api/me/delete，避免注销冷却记录挡住恢复的 60s 窗口）
        db.soft_delete_user_with_accounts("user1@test.local")
        c = self.webapp.create_app().test_client()
        # 恢复（未登录调用；CSRF 走同源校验）
        r = c.post("/api/me/restore", json={"email": "user1@test.local", "password": USER_PASS})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json()["role"], "user")
        # 用户 + 易班账号联动恢复
        u = db.find_user("user1@test.local")
        self.assertIsNotNone(u)
        self.assertEqual(u["deleted"], 0)
        accs = db.load_accounts_raw()
        row = next((a for a in accs if a["owner"] == "user1@test.local"), None)
        self.assertIsNotNone(row, "恢复应联动恢复易班账号")
        self.assertFalse(row["deleted"])
        # 恢复即登录（会话已建立）
        me = c.get("/api/me")
        self.assertEqual(me.status_code, 200)
        # 审计留痕
        actions = self._audit_actions()
        self.assertIn("user_self_delete_restore", actions)

    def test_restore_wrong_password(self):
        db.soft_delete_user_with_accounts("user1@test.local")
        c = self.webapp.create_app().test_client()
        r = c.post("/api/me/restore", json={"email": "user1@test.local", "password": "bad-pass"})
        self.assertEqual(r.status_code, 400)
        # 统一文案（2026-08-17 安全审查）：不区分密码错误与账号状态，防注销账号枚举
        self.assertIn("已过恢复期", r.get_json()["error"])
        self.assertIsNone(db.find_user("user1@test.local"), "密码错误不应恢复")

    def test_restore_not_in_grace_period(self):
        # 已过 7 天宽限期（8 天前注销）→ 统一 400 文案（2026-08-17：原 404 可被无凭探测）
        db.create_user("oldone@test.local", "hash")
        db.soft_delete_user_with_accounts("oldone@test.local")
        conn = db.get_conn()
        old = (datetime.now() - timedelta(days=8)).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute("UPDATE users SET deleted_at=? WHERE email=?", (old, "oldone@test.local"))
        conn.commit()
        c = self.webapp.create_app().test_client()
        r = c.post("/api/me/restore", json={"email": "oldone@test.local", "password": "hash"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("已过恢复期", r.get_json()["error"])
        self.assertIsNone(db.find_user("oldone@test.local"), "过恢复期不应恢复")

    def test_restore_lockout(self):
        """2026-08-17 安全审查补齐：恢复接口密码试错必须计入锁定（原可无限爆破冷却期账号）。"""
        db.soft_delete_user_with_accounts("user1@test.local")
        c = self.webapp.create_app().test_client()
        for i in range(self.webapp.LOGIN_MAX_FAILS - 1):
            r = c.post("/api/me/restore", json={"email": "user1@test.local", "password": "bad"})
            self.assertEqual(r.status_code, 400, f"第 {i + 1} 次错误应为 400")
        r = c.post("/api/me/restore", json={"email": "user1@test.local", "password": "bad"})
        self.assertEqual(r.status_code, 429, "达阈值应锁定")
        # 锁定期间正确密码也被拒（不泄露锁定状态与账号存在性）
        r = c.post("/api/me/restore", json={"email": "user1@test.local", "password": USER_PASS})
        self.assertEqual(r.status_code, 429)
        self.assertNotIn(str(self.webapp.LOGIN_LOCK_SECONDS), r.get_json()["error"],
                         "不应暴露锁定时长")
        self.assertIsNone(db.find_user("user1@test.local"), "锁定期间不应恢复")

    def test_restore_no_email_enum(self):
        """2026-08-17 安全审查：不存在/过期的邮箱与密码错误返回完全一致（防注销状态枚举）。"""
        db.soft_delete_user_with_accounts("user1@test.local")  # 冷却中账号
        c = self.webapp.create_app().test_client()
        r1 = c.post("/api/me/restore", json={"email": "user1@test.local", "password": "bad"})
        r2 = c.post("/api/me/restore", json={"email": "ghost@test.local", "password": "bad"})
        self.assertEqual(r1.status_code, r2.status_code, "状态码应一致")
        self.assertEqual(r1.get_json()["error"], r2.get_json()["error"], "错误文案应一致")

    def test_restore_cooldown(self):
        # v7 分流：仅 restore 记录占用恢复冷却；注销记录不再阻断恢复
        db.record_user_delete_request("user1@test.local", ip_hash="x", kind="restore")
        c = self.webapp.create_app().test_client()
        r = c.post("/api/me/restore", json={"email": "user1@test.local", "password": USER_PASS})
        self.assertEqual(r.status_code, 429)
        self.assertNotIn("60", r.get_json()["error"], "不应暴露冷却秒数")

    def test_restore_not_blocked_by_delete_record(self):
        """v7 回归（2026-08-17 修复）：注销动作自身的记录不阻断 60s 内恢复。

        此前注销与恢复共用计数，真实路径"注销 → 立即反悔 → 恢复"必现 429，
        用户表现为"注销后不能登录"。现有 test_restore_success 曾靠绕过
        /api/me/delete 规避该缺陷，本测试走真实路径锁死回归。
        """
        c = self.webapp.create_app().test_client()
        token = self._login(c, "user1@test.local", USER_PASS)
        r = self._delete(c, token, password=USER_PASS)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        # 登录 → recoverable 引导
        r = c.post("/api/login", json={"username": "user1@test.local", "password": USER_PASS})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json().get("recoverable"))
        # 立即恢复（注销后 0 秒）：应成功，不再 429
        r = c.post("/api/me/restore", json={"email": "user1@test.local", "password": USER_PASS})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertIsNotNone(db.find_user("user1@test.local"), "恢复后用户应回到活跃状态")

    # ---- 冷却期邮箱保护（安全审查 2026-08-16：恢复权不被新注册抢占）----
    def test_register_blocked_during_grace(self):
        c = self.webapp.create_app().test_client()
        token = self._login(c, "user1@test.local", USER_PASS)
        self._delete(c, token, password=USER_PASS)
        r = c.post("/api/register", json={"email": "user1@test.local", "password": "newpass1234", "agree": True})
        self.assertEqual(r.status_code, 400, "冷却期内同邮箱注册应被拒")
        self.assertIn("冷却期", r.get_json()["error"])
        self.assertIsNone(db.find_user("user1@test.local"), "注册不应产生新活跃用户")

    def test_register_allowed_after_grace(self):
        # 8 天前注销（已过 7 天宽限期）→ 同邮箱可重新注册
        db.create_user("oldone@test.local", "hash")
        db.soft_delete_user_with_accounts("oldone@test.local")
        conn = db.get_conn()
        old = (datetime.now() - timedelta(days=8)).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute("UPDATE users SET deleted_at=? WHERE email=?", (old, "oldone@test.local"))
        conn.commit()
        c = self.webapp.create_app().test_client()
        r = c.post("/api/register", json={"email": "oldone@test.local", "password": "newpass1234", "agree": True})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertIsNotNone(db.find_user("oldone@test.local"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
