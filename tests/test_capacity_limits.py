# -*- coding: utf-8 -*-
"""容量口径与容量上限设置（2026-09-08）。

- 保存延迟容量门单门化：活跃账号数 > _capacity_estimate(gap) 才拒；
  注册用户多但活跃账号少放行（旧 users 分支删除）
- _accounts_at_capacity 新口径：占用 = 全部非删除活跃账号数（含 owner='admin'
  裸账号），extra_accounts = 本次将新增账号数
- max_users / max_accounts 设置项：仅主管理员可写（非主 403）、0=不限、
  钳位 0~100000、字段携带才写（部分更新不清零）、热读即时生效
  （注册到上限被拒 → 设置调大 → 再注册放行）
- /api/settings GET：capacity_estimate 字段收敛
  （accounts_cap / current_accounts / potential_load）

用法（项目根目录）：
    py -m pytest tests/test_capacity_limits.py -v
"""
import contextlib
import importlib.util
import io
import os
import shutil
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TEST_KEY = "a" * 64
ADMIN_PASS = "MasterPass#2026"
USER_PASS = "secret1"


def _load_webapp():
    spec = importlib.util.spec_from_file_location("webapp_caplim", os.path.join(BASE, "web", "app.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["webapp_caplim"] = mod
    with contextlib.suppress(Exception):
        spec.loader.exec_module(mod)
    return mod


class _Base(unittest.TestCase):
    """共享脚手架：临时 .env/DB + webapp 加载 + 主管理员登录。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-caplim-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with io.open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(
                f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
                "YIBAN_ADMIN_USER=admin@test.local\n"
                f"YIBAN_ADMIN_PASSWORD={ADMIN_PASS}\n"
            )
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.accounts_file = os.path.join(cls.tmp, "accounts.json")
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_ACCOUNTS_FILE"] = cls.accounts_file
        os.environ["YIBAN_DB_FILE"] = cls.db_file
        os.environ["YIBAN_STATE_DIR"] = cls.tmp
        os.environ["YIBAN_LOG_FILE"] = os.path.join(cls.tmp, "sign.log")
        cls.db = __import__("db")
        cls.webapp = _load_webapp()

    @classmethod
    def tearDownClass(cls):
        if cls.db._conn is not None:
            with contextlib.suppress(Exception):
                cls.db._conn.close()
            cls.db._conn = None
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k in ("YIBAN_ACCOUNTS_KEY", "YIBAN_LOG_FILE", "YIBAN_STATE_DIR"):
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
        with io.open(self.env_file, "w", encoding="utf-8") as f:
            f.write(
                f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
                "YIBAN_ADMIN_USER=admin@test.local\n"
                f"YIBAN_ADMIN_PASSWORD={ADMIN_PASS}\n"
            )
        self.db.init_db(self.db_file, migrate_from=self.accounts_file, env_file=self.env_file)

    def _master(self):
        c = self.webapp.create_app().test_client()
        r = c.post("/api/login", json={"username": "admin@test.local", "password": ADMIN_PASS})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        t = c.get("/api/me").get_json()["csrf_token"]
        return c, {"X-CSRF-Token": t}


class SaveGateSingleTierTest(_Base):
    """保存延迟容量门（单门：活跃账号数 vs _capacity_estimate(gap)）。"""

    def test_over_capacity_rejected(self):
        # gap=3600 → 预估容量 = (4680-8)/3608+1 = 2（默认掐头去尾前后各 60s）；
        # 3 个活跃账号（含裸账号）→ 拒
        for i in range(3):
            self.db.add_account({"name": "N", "phone": f"1380013800{i}", "password": "pw",
                                 "status": "active", "owner": "admin"})
        c, h = self._master()
        r = c.post("/api/settings",
                   json={"gap_max": 3600, "confirm_password": ADMIN_PASS}, headers=h)
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        err = r.get_json()["error"]
        self.assertIn("活跃账号", err)
        for kw in ("账号间隔", "签到窗口", "清理"):
            self.assertIn(kw, err, "报错必须给出去路")

    def test_many_users_few_accounts_allowed(self):
        # 注册用户多但活跃账号少：旧 users 分支已删，不构成负载 → 放行
        for i in range(6):
            self.db.create_user(f"u{i}@test.local", "x", role="user")
        c, h = self._master()
        r = c.post("/api/settings",
                   json={"gap_max": 3600, "confirm_password": ADMIN_PASS}, headers=h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

    def test_deleted_accounts_not_counted(self):
        # 软删除账号不占负载：3 条中 2 条 deleted → 占用 1 ≤ 2 → 放行
        for i in range(3):
            acc = {"name": "N", "phone": f"1380013801{i}", "password": "pw",
                   "status": "active", "owner": "admin"}
            self.db.add_account(acc)
        accs = self.db.load_accounts()
        for a in accs[:2]:
            self.db.set_account_deleted(a["id"], True, "2026-09-08 06:00:00")
        c, h = self._master()
        r = c.post("/api/settings",
                   json={"gap_max": 3600, "confirm_password": ADMIN_PASS}, headers=h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))


class AccountsAtCapacityTest(_Base):
    """_accounts_at_capacity 新口径：裸账号计数、extra_accounts 语义。"""

    def test_bare_account_counts_and_extra_semantics(self):
        self.db.add_account({"name": "N", "phone": "13800138000", "password": "pw",
                             "status": "active", "owner": "admin"})
        self.webapp.write_env_key(self.env_file, "YIBAN_MAX_ACCOUNTS", "1")
        try:
            # 占用 1（裸账号计入）+ 本次新增 1 = 2 > 1 → True
            self.assertTrue(self.webapp._accounts_at_capacity(1))
            # 已满但本次不新增（extra=0）：1 > 1 不成立 → False
            self.assertFalse(self.webapp._accounts_at_capacity(0))
        finally:
            self.webapp.write_env_key(self.env_file, "YIBAN_MAX_ACCOUNTS", "")

    def test_zero_limit_means_unlimited(self):
        self.db.add_account({"name": "N", "phone": "13800138000", "password": "pw",
                             "status": "active", "owner": "admin"})
        self.webapp.write_env_key(self.env_file, "YIBAN_MAX_ACCOUNTS", "0")
        try:
            self.assertFalse(self.webapp._accounts_at_capacity(1))
        finally:
            self.webapp.write_env_key(self.env_file, "YIBAN_MAX_ACCOUNTS", "")


class MaxLimitsSettingsTest(_Base):
    """max_users / max_accounts：权限、钳位、携带才写、热读。"""

    def test_non_master_admin_forbidden(self):
        self.db.create_user("admin2@test.local",
                            self.webapp.generate_password_hash(USER_PASS), role="admin")
        c = self.webapp.create_app().test_client()
        self.assertEqual(c.post("/api/login", json={
            "username": "admin2@test.local", "password": USER_PASS}).status_code, 200)
        t = c.get("/api/me").get_json()["csrf_token"]
        r = c.post("/api/settings", json={"max_users": 5},
                   headers={"X-CSRF-Token": t})
        self.assertEqual(r.status_code, 403, r.get_data(as_text=True))
        r = c.post("/api/settings", json={"max_accounts": 5},
                   headers={"X-CSRF-Token": t})
        self.assertEqual(r.status_code, 403, r.get_data(as_text=True))

    def _save(self, payload):
        c, h = self._master()
        payload = dict(payload, confirm_password=ADMIN_PASS)
        return c.post("/api/settings", json=payload, headers=h)

    def _read_env(self):
        return self.webapp.read_env(self.env_file)

    def test_save_and_partial_update_keeps_other_key(self):
        r = self._save({"max_users": 10, "max_accounts": 20})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        env = self._read_env()
        self.assertEqual(env["YIBAN_MAX_USERS"], "10")
        self.assertEqual(env["YIBAN_MAX_ACCOUNTS"], "20")
        # 部分更新：只携带 max_users，不得把未携带的 max_accounts 清零/删键
        r = self._save({"max_users": 15})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        env = self._read_env()
        self.assertEqual(env["YIBAN_MAX_USERS"], "15")
        self.assertEqual(env["YIBAN_MAX_ACCOUNTS"], "20")
        # 只改周末开关之类的无关键请求，不触碰 max_*（字段携带才写）
        r = self._save({"sunday_sign": 1})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        env = self._read_env()
        self.assertEqual(env["YIBAN_MAX_USERS"], "15")
        self.assertEqual(env["YIBAN_MAX_ACCOUNTS"], "20")

    def test_validation_clamps_and_rejects(self):
        for bad in (-1, 100001, "abc"):
            r = self._save({"max_users": bad})
            self.assertEqual(r.status_code, 400, f"max_users={bad}")
        for bad in (-5, 200000):
            r = self._save({"max_accounts": bad})
            self.assertEqual(r.status_code, 400, f"max_accounts={bad}")
        env = self._read_env()
        self.assertNotIn("YIBAN_MAX_USERS", env)
        self.assertNotIn("YIBAN_MAX_ACCOUNTS", env)
        # 边界合法：0=不限、100000 恰好可存
        r = self._save({"max_users": 0, "max_accounts": 100000})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        env = self._read_env()
        self.assertEqual(env["YIBAN_MAX_USERS"], "0")
        self.assertEqual(env["YIBAN_MAX_ACCOUNTS"], "100000")

    def test_max_limits_require_password_confirm(self):
        # 携带 max_* 但缺 confirm_password → 口令二次确认拒绝（400），.env 不落盘
        c, h = self._master()
        r = c.post("/api/settings", json={"max_users": 10}, headers=h)
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        self.assertNotIn("YIBAN_MAX_USERS", self._read_env())
        r = c.post("/api/settings", json={"max_accounts": 10}, headers=h)
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        self.assertNotIn("YIBAN_MAX_ACCOUNTS", self._read_env())

    def test_hot_read_roundtrip(self):
        # 热读链路：注册到上限被拒 → 设置调大（走 POST /api/settings）→ 再注册放行
        self.db.create_user("u1@test.local", "x", role="user")
        self.db.create_user("u2@test.local", "x", role="user")
        self.webapp.write_env_key(self.env_file, "YIBAN_MAX_USERS", "2")
        app = self.webapp.create_app()
        c = app.test_client()
        r = c.post("/api/register", json={
            "email": "new@test.local", "password": "StrongPass1!", "agree": True})
        self.assertEqual(r.status_code, 403, r.get_data(as_text=True))
        # 设置页调大上限（热读：load_env_int 每次现读 .env，无需重启/清缓存）
        r = self._save({"max_users": 5})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        r = c.post("/api/register", json={
            "email": "new@test.local", "password": "StrongPass1!", "agree": True})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))


class PotentialLoadTest(_Base):
    """GET /api/settings capacity_estimate 字段正确性。"""

    def test_fields_single_tier(self):
        # 账号：user A 持 1 个 + admin 裸账号 1 个 → 活跃 2；
        # user B 空用户、user C 仅持有已删除账号 → 潜在负载 = 2（A/admin 已持有不计）
        self.db.create_user("a@test.local", "x", role="user")
        self.db.create_user("b@test.local", "x", role="user")
        self.db.create_user("c@test.local", "x", role="user")
        self.db.add_account({"name": "NA", "phone": "13800138001", "password": "pw",
                             "status": "active", "owner": "a@test.local"})
        self.db.add_account({"name": "NC", "phone": "13800138002", "password": "pw",
                             "status": "active", "owner": "c@test.local"})
        self.db.add_account({"name": "NAdmin", "phone": "13800138003", "password": "pw",
                             "status": "active", "owner": "admin"})
        accs = self.db.load_accounts()
        for a in accs:
            if a["phone"] == "13800138002":
                self.db.set_account_deleted(a["id"], True, "2026-09-08 06:00:00")
        c, h = self._master()
        data = c.get("/api/settings", headers=h).get_json()
        est = data["capacity_estimate"]
        self.assertNotIn("users_cap", est)
        self.assertNotIn("current_holders", est)
        self.assertNotIn("current_users", est)
        self.assertEqual(est["current_accounts"], 2)
        self.assertEqual(est["potential_load"], 2)
        # capacity 区块：users = 全部未删除注册用户；accounts = 活跃账号数
        cap = data["capacity"]
        self.assertEqual(cap["users"], len(self.db.load_users()))
        self.assertEqual(cap["accounts"], 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
