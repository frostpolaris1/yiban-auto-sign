# -*- coding: utf-8 -*-
"""容量口径与容量上限设置（2026-09-08）。

标签：I · 容量、熔断与账号有效性
覆盖：保存延迟时的容量单门、`_accounts_at_capacity` 口径（裸账号计数与 `extra_accounts` 语义）、max_users / max_accounts 设置项（权限、钳位、携带才写、热读）、`capacity_estimate` 字段收敛、三分类拆解与设置页统计的解密读取
对应实现：`webapp._capacity_estimate`、`_accounts_at_capacity`、`/api/settings` 读写路由、`signin.capacity_accounts`
关键断言：注册用户多但活跃账号少应放行（旧 users 分支已删）；只带一个延迟字段的部分更新必须按 `.env` 现存的账号间隔算容量——缺省取 0 会把预估顶到窗口上限、把硬门整个绕过；0=不限、钳位 0~100000、字段未携带不得把已有值清零；引擎预检与 web 预估同一条公式（含「缓冲过大只收缩缓冲、窗口 1 分钟保留」的退化分支）；`cred-state.json` 损坏或缺失时接口仍 200 且 `cred_paused=0`（不得 500）
依赖：纯本地 Flask test client + 临时 `.env`/SQLite，不联网、不访问真实易班接口；无需 node；多处 importlib 以独立模块名装载 `web/app.py`（共用模块对象会读到别人的 `.env`）。各基类与多数用例都显式写 `YIBAN_PW_GATE=full`——默认档 `risk` 下这些写操作不再当次要口令

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
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

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
                # 容量/上限用例钉的是"真变更当次要口令、超容量拒绝"这套机制，
                # 固定在 full（默认档 risk 下这些动作不再当次要口令）
                "YIBAN_PW_GATE=full\n"
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
                "YIBAN_PW_GATE=full\n"  # 与 setUpClass 同档：这些用例钉的是当次口令机制
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

    def test_partial_update_uses_current_gap_for_gate(self):
        """只带 start_delay_max 的部分更新必须按 `.env` 里现存的账号间隔算容量。

        缺省取 0 会把预估容量顶到窗口上限，容量硬门被"乐观上限"整个绕过。
        """
        for i in range(3):
            self.db.add_account({"name": "N", "phone": f"1380013802{i}", "password": "pw",
                                 "status": "active", "owner": "admin"})
        self.webapp.write_env_key(self.env_file, "YIBAN_ACCOUNT_GAP_MAX", "3600")
        try:
            c, h = self._master()
            r = c.post("/api/settings",
                       json={"start_delay_max": 30, "confirm_password": ADMIN_PASS}, headers=h)
            self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
            self.assertIn("活跃账号", r.get_json()["error"])
        finally:
            self.webapp.write_env_key(self.env_file, "YIBAN_ACCOUNT_GAP_MAX", "")

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
        # 携带 max_* 但缺 confirm_password → 口令二次确认拒绝（A 档走设置路由统一的
        # 403「口令校验未通过，设置未生效」），.env 不落盘
        c, h = self._master()
        r = c.post("/api/settings", json={"max_users": 10}, headers=h)
        self.assertEqual(r.status_code, 403, r.get_data(as_text=True))
        self.assertNotIn("YIBAN_MAX_USERS", self._read_env())
        r = c.post("/api/settings", json={"max_accounts": 10}, headers=h)
        self.assertEqual(r.status_code, 403, r.get_data(as_text=True))
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


class CapacityAccountsUnitTest(unittest.TestCase):
    """signin.capacity_accounts：容量口径唯一源的边界（引擎预检与 web 预估共用）。"""

    def _fn(self):
        import signin
        return signin.capacity_accounts

    def test_window_smaller_than_one_account(self):
        cap = self._fn()
        self.assertEqual(cap(2, 10, avg=3), 0)   # 窗口 2s 容不下单账号 3s
        self.assertEqual(cap(3, 10, avg=3), 1)   # 恰好一个（slack=0 → 1 个）

    def test_gap_zero_means_pure_serial(self):
        cap = self._fn()
        # (4800-3)/3+1 = 1600：无间隔时限只由单账号耗时决定
        self.assertEqual(cap(4800, 0, avg=3), 1600)

    def test_gap_dominates_and_is_additive(self):
        cap = self._fn()
        # 实测口径：单账号周期 = avg + gap（gap 是「上一次完成 → 下一次开始」的下限）
        self.assertEqual(cap(4800, 10, avg=3), (4800 - 3) // 13 + 1)
        self.assertEqual(cap(4800, 10, avg=8), (4800 - 8) // 18 + 1)
        self.assertGreater(cap(4800, 10, avg=3), cap(4800, 10, avg=8))

    def test_default_avg_comes_from_env(self):
        cap = self._fn()
        with mock.patch.dict(os.environ, {"YIBAN_AVG_ATTEMPT_SEC": "9"}):
            self.assertEqual(cap(4800, 10), (4800 - 9) // 19 + 1)
        # 非法值回退发行缺省档（3s），不抛异常
        with mock.patch.dict(os.environ, {"YIBAN_AVG_ATTEMPT_SEC": "abc"}):
            self.assertEqual(cap(4800, 10), (4800 - 3) // 13 + 1)


def _load_webapp_B19(tag):
    import db as _db
    spec = importlib.util.spec_from_file_location(f"webapp_{tag}", os.path.join(BASE, "web", "app.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"webapp_{tag}"] = mod
    with contextlib.suppress(Exception):
        spec.loader.exec_module(mod)
    return _db, mod


class _Base_B19(unittest.TestCase):
    """共享脚手架：临时 .env/DB + webapp 加载 + 主管理员登录。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-b19-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with io.open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(
                f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
                "YIBAN_ADMIN_USER=admin@test.local\n"
                f"YIBAN_ADMIN_PASSWORD={ADMIN_PASS}\n"
                # 容量/上限用例钉的是"真变更当次要口令、超容量拒绝"这套机制，
                # 固定在 full（默认档 risk 下这些动作不再当次要口令）
                "YIBAN_PW_GATE=full\n"
            )
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.accounts_file = os.path.join(cls.tmp, "accounts.json")
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_ACCOUNTS_FILE"] = cls.accounts_file
        os.environ["YIBAN_DB_FILE"] = cls.db_file
        os.environ["YIBAN_STATE_DIR"] = cls.tmp
        os.environ["YIBAN_LOG_FILE"] = os.path.join(cls.tmp, "sign.log")
        cls.db, cls.webapp = _load_webapp_B19(cls.__name__)

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
        self.db.init_db(self.db_file, migrate_from=self.accounts_file, env_file=self.env_file)

    def _master(self):
        c = self.webapp.create_app().test_client()
        r = c.post("/api/login", json={"username": "admin@test.local", "password": ADMIN_PASS})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        t = c.get("/api/me").get_json()["csrf_token"]
        return c, {"X-CSRF-Token": t}

    def _seed_user_with_account(self, email, phone, status="pending"):
        """注册用户 + 建一条指定状态的账号（owner=email）。"""
        self.db.create_user(email, "x", role="user")
        acc = {"name": "N", "phone": phone, "password": "pw", "status": status, "owner": email}
        self.db.add_account(acc)


class CapacitySettingsTest(_Base_B19):
    """/api/settings 容量预估 + 延迟修改的密码确认与超容量拒绝。"""

    def test_get_returns_capacity_estimate(self):
        # 先重置 env：同类更前的保存用例会写入延迟值，估算按当前设置实时计算
        with io.open(self.env_file, "w", encoding="utf-8") as f:
            f.write(
                f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
                "YIBAN_ADMIN_USER=admin@test.local\n"
                f"YIBAN_ADMIN_PASSWORD={ADMIN_PASS}\n"
                "YIBAN_PW_GATE=full\n"  # 与 setUpClass 同档：这些用例钉的是当次口令机制
            )
        # 清掉可能被其他用例写进进程环境的 avg（本用例断言发行缺省档）
        with mock.patch.dict(os.environ):
            os.environ.pop("YIBAN_AVG_ATTEMPT_SEC", None)
            c, h = self._master()
            data = c.get("/api/settings", headers=h).get_json()
        est = data["capacity_estimate"]
        for k in ("accounts_cap", "current_accounts", "potential_load"):
            self.assertIn(k, est)
        # gap 缺省取 DEFAULT_ACCOUNT_GAP_MAX=10，掐头去尾缺省前后各 60s
        # → 有效窗口 4800-120=4680；avg 缺省 3：(4680-3)//13+1 = 360
        self.assertEqual(est["accounts_cap"], 360)
        self.assertEqual(est["current_accounts"], 0)
        self.assertEqual(est["potential_load"], len(self.db.load_users()))

    def test_delay_requires_confirm_password(self):
        """随机延迟属 A 档：缺口令/错口令都由设置路由统一的 403 拒绝（不落盘）。"""
        c, h = self._master()
        r = c.post("/api/settings", json={"start_delay_max": 60}, headers=h)
        self.assertEqual(r.status_code, 403)
        r = c.post("/api/settings", json={"start_delay_max": 60, "confirm_password": "wrong!"},
                   headers=h)
        self.assertEqual(r.status_code, 403)
        self.assertNotIn("YIBAN_START_DELAY_MAX=60",
                         io.open(self.env_file, encoding="utf-8").read())

    def test_delay_save_with_confirm(self):
        c, h = self._master()
        r = c.post("/api/settings",
                   json={"start_delay_max": 60, "gap_max": 10, "confirm_password": ADMIN_PASS},
                   headers=h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        env = io.open(self.env_file, encoding="utf-8").read()
        self.assertIn("YIBAN_START_DELAY_MAX=60", env)
        self.assertIn("YIBAN_ACCOUNT_GAP_MAX=10", env)

    def test_delay_save_rejected_when_over_capacity(self):
        # 重置 env：字母序下 allows_many_users（写 3600）先于本用例执行，防残留误判
        with io.open(self.env_file, "w", encoding="utf-8") as f:
            f.write(
                f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
                "YIBAN_ADMIN_USER=admin@test.local\n"
                f"YIBAN_ADMIN_PASSWORD={ADMIN_PASS}\n"
                "YIBAN_PW_GATE=full\n"  # 与 setUpClass 同档：这些用例钉的是当次口令机制
            )
        # 恶性间隔 gap=3600 → 预估账号容量 = (4680-8)/3608+1 = 2（默认掐头去尾前后各 60s）；
        # 灌 3 个活跃账号（含裸账号，均占配额）→ 必超
        for i in range(3):
            self.db.add_account({"name": "N", "phone": f"1390013900{i}", "password": "pw",
                                 "status": "active", "owner": "admin"})
        c, h = self._master()
        r = c.post("/api/settings",
                   json={"start_delay_max": 3600, "gap_max": 3600, "confirm_password": ADMIN_PASS},
                   headers=h)
        self.assertEqual(r.status_code, 400)
        err = r.get_json()["error"]
        self.assertIn("容量", err)
        # 报错必须给出去路：缩短间隔 / 延长窗口 / 清理账号
        for kw in ("账号间隔", "签到窗口", "清理"):
            self.assertIn(kw, err)
        env = io.open(self.env_file, encoding="utf-8").read()
        self.assertNotIn("YIBAN_START_DELAY_MAX=3600", env, "拒绝保存时不得落盘")

    def test_delay_save_allows_many_users_few_accounts(self):
        # 单门口径（2026-09-08）：注册用户多但活跃账号少不构成负载 → 放行
        # （旧口径 users 分支已删除，不再因注册人数拒绝保存）
        for i in range(4):
            self.db.create_user(f"u{i}@test.local", "x", role="user")
        c, h = self._master()
        r = c.post("/api/settings",
                   json={"start_delay_max": 3600, "gap_max": 3600, "confirm_password": ADMIN_PASS},
                   headers=h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))


class CapacityFormulaTest(_Base_B19):
    """_capacity_estimate 公式（有效窗口扣除掐头去尾，avg 与 gap 共同决定容量）。

    avg 经 YIBAN_AVG_ATTEMPT_SEC 固定为 8s，与缺省档解耦（缺省档的断言在
    CapacitySettingsTest 里按发行缺省值另行核对）。
    """

    def test_formula_single_tier(self):
        with mock.patch.object(self.webapp, "_sign_window",
                               return_value=((6, 30), (7, 50))), \
             mock.patch.object(self.webapp, "edge_config", return_value=(0, 0)), \
             mock.patch.dict(os.environ, {"YIBAN_AVG_ATTEMPT_SEC": "8"}):
            # 4800s 窗口、无间隔：(4800-8)/8+1 = 600（单档返回单值 int）
            self.assertEqual(self.webapp._capacity_estimate(0), 600)
            # gap=10：(4800-8)/18+1 = 267
            self.assertEqual(self.webapp._capacity_estimate(10), (4800 - 8) // 18 + 1)
            # gap=3600：4792/3608+1 = 2
            self.assertEqual(self.webapp._capacity_estimate(3600), 2)

    def test_edge_shrinks_capacity(self):
        # 掐头去尾计入有效窗口：前后各裁 60s → 有效 4680s，容量较 4800s 变小
        with mock.patch.object(self.webapp, "_sign_window",
                               return_value=((6, 30), (7, 50))), \
             mock.patch.dict(os.environ, {"YIBAN_AVG_ATTEMPT_SEC": "8"}):
            with mock.patch.object(self.webapp, "edge_config", return_value=(0, 0)):
                full = self.webapp._capacity_estimate(10)
            with mock.patch.object(self.webapp, "edge_config", return_value=(60, 60)):
                trimmed = self.webapp._capacity_estimate(10)
            self.assertEqual(full, (4800 - 8) // 18 + 1)
            self.assertEqual(trimmed, (4680 - 8) // 18 + 1)
            self.assertLess(trimmed, full)

    def test_degenerate_window_falls_back_like_engine(self):
        # 退化窗口（起止同点/裁剪吃空）时，网页与引擎
        # 一致地回退默认窗口（06:30~07:50、默认裁剪各 60s），故容量**不是** 0。
        # 原实现网页内联算"原始窗口 − 裁剪"、引擎按回退窗口排计划 → 出现
        # "引擎有完整计划、网页容量显示 0"的自相矛盾。
        with mock.patch.object(self.webapp, "_sign_window",
                               return_value=((7, 50), (7, 50))), \
             mock.patch.object(self.webapp, "edge_config", return_value=(0, 0)), \
             mock.patch.dict(os.environ, {"YIBAN_AVG_ATTEMPT_SEC": "8"}):
            self.assertEqual(self.webapp._capacity_estimate(0), (4680 - 8) // 8 + 1)

    def test_trimmed_empty_window_matches_engine_plan(self):
        """缓冲过大 → 只收缩缓冲（窗口 1 分钟保留）；网页容量与引擎计划同源。

        窗口 07:00~07:01 前后各 300s（合计 >= 窗口宽度）⇒ 缓冲等比收缩为各 6s，
        有效窗口 ≈48s；容量 = (47 - 8) / 8 + 1 = 5（**不是** 0，也不是回退默认窗口；
        47 是 `capacity_accounts` 对窗口秒数取整的结果）。
        """
        with mock.patch.object(self.webapp, "_sign_window",
                               return_value=((7, 0), (7, 1))), \
             mock.patch.object(self.webapp, "edge_config", return_value=(300, 300)), \
             mock.patch.dict(os.environ, {"YIBAN_AVG_ATTEMPT_SEC": "8"}):
            self.assertEqual(self.webapp._capacity_estimate(0), 5)

    def test_engine_and_web_share_one_formula(self):
        """引擎容量预检与 web 容量预估必须同口径（同概念不得两套阈值）。"""
        import signin
        with mock.patch.object(self.webapp, "_sign_window",
                               return_value=((6, 30), (7, 50))), \
             mock.patch.object(self.webapp, "edge_config", return_value=(60, 60)):
            for gap in (0, 10, 60):
                self.assertEqual(
                    self.webapp._capacity_estimate(gap),
                    signin.capacity_accounts(4680, gap),
                )


def _load_webapp_CAP():
    spec = importlib.util.spec_from_file_location("webapp_capbd", os.path.join(BASE, "web", "app.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["webapp_capbd"] = mod
    with contextlib.suppress(Exception):
        spec.loader.exec_module(mod)
    return mod


class _Base_CAP(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-capbd-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.accounts_file = os.path.join(cls.tmp, "accounts.json")
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_ACCOUNTS_FILE"] = cls.accounts_file
        os.environ["YIBAN_DB_FILE"] = cls.db_file
        os.environ["YIBAN_STATE_DIR"] = cls.tmp  # cred-state.json 落在此
        os.environ["YIBAN_LOG_FILE"] = os.path.join(cls.tmp, "sign.log")
        os.environ["YIBAN_MAIL_ENABLE"] = "0"
        cls.db = __import__("db")
        cls.webapp = _load_webapp_CAP()

    @classmethod
    def tearDownClass(cls):
        if cls.db._conn is not None:
            with contextlib.suppress(Exception):
                cls.db._conn.close()
            cls.db._conn = None
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @property
    def cred_state_file(self):
        return os.path.join(self.tmp, "cred-state.json")

    def setUp(self):
        if self.db._conn is not None:
            with contextlib.suppress(Exception):
                self.db._conn.close()
            self.db._conn = None
        for suffix in ("", "-wal", "-shm"):
            p = self.db_file + suffix
            if os.path.exists(p):
                os.remove(p)
        if os.path.exists(self.cred_state_file):
            os.remove(self.cred_state_file)
        with io.open(self.env_file, "w", encoding="utf-8") as f:
            f.write(
                f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
                "YIBAN_ADMIN_USER=admin@test.local\n"
                f"YIBAN_ADMIN_PASSWORD={ADMIN_PASS}\n"
                "YIBAN_PW_GATE=full\n"  # 与 setUpClass 同档：这些用例钉的是当次口令机制
            )
        self.db.init_db(self.db_file, migrate_from=self.accounts_file, env_file=self.env_file)

    # ---- 脚手架 ----
    def _add(self, phone, owner="admin", paused=False):
        self.db.add_account({
            "name": "N", "phone": phone, "password": "pw",
            "phone_model": "", "phone_code": "", "owner": owner,
            "status": "active", "reject_reason": "",
        })
        if paused:
            acc = next(a for a in self.db.load_accounts_raw() if a["phone"] == phone)
            self.db.set_user_paused(acc["id"], 1)

    def _write_cred_state(self, phones, raw=None):
        with io.open(self.cred_state_file, "w", encoding="utf-8") as f:
            f.write(raw if raw is not None else json.dumps(
                {p: {"fail_days": 3, "paused_since": "2026-09-10 06:00:00"} for p in phones}
            ))

    def _capacity(self):
        c = self.webapp.create_app().test_client()
        r = c.post("/api/login", json={"username": "admin@test.local", "password": ADMIN_PASS})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        rs = c.get("/api/settings")
        self.assertEqual(rs.status_code, 200, rs.get_data(as_text=True))
        return rs.get_json()["capacity"]


class CapacityBreakdownTest(_Base_CAP):
    def test_three_buckets_mutually_exclusive_and_sum(self):
        """2 正常 + 1 自暂停 + 1 账密故障暂停 + 1 两者都命中 → 优先级归自暂停。"""
        self._add("13900000001")                      # 正常
        self._add("13900000002")                      # 正常
        self._add("13900000003", paused=True)         # 用户自暂停
        self._add("13900000004")                      # 账密故障暂停（cred-state）
        self._add("13900000005", paused=True)         # 两者都命中 → 归自暂停
        self._write_cred_state(["13900000004", "13900000005"])

        cap = self._capacity()
        bd = cap["accounts_breakdown"]
        self.assertEqual(bd["normal"], 2, bd)
        self.assertEqual(bd["user_paused"], 2, bd)
        self.assertEqual(bd["cred_paused"], 1, bd)
        self.assertEqual(
            bd["normal"] + bd["user_paused"] + bd["cred_paused"], cap["accounts"],
            "三桶必须互斥且求和等于账号总数",
        )

    def test_soft_deleted_excluded(self):
        """软删账号不占名额、也不计入任何桶。"""
        self._add("13900000001")
        self._add("13900000002", paused=True)
        self._write_cred_state(["13900000003"])
        self._add("13900000003")
        cap0 = self._capacity()
        self.assertEqual(cap0["accounts"], 3)
        acc = next(a for a in self.db.load_accounts_raw() if a["phone"] == "13900000003")
        self.db.set_account_deleted(acc["id"], 1, "2026-09-10 06:00:00")
        cap1 = self._capacity()
        self.assertEqual(cap1["accounts"], 2)
        self.assertEqual(cap1["accounts_breakdown"]["cred_paused"], 0)
        self.assertEqual(
            sum(cap1["accounts_breakdown"].values()), cap1["accounts"],
            "软删后三桶求和仍须等于总数",
        )

    def test_broken_cred_state_does_not_break_settings(self):
        """cred-state.json 损坏/缺失时接口仍 200，cred_paused=0（不得 500）。"""
        self._add("13900000001")
        with io.open(self.cred_state_file, "w", encoding="utf-8") as f:
            f.write("{ 这不是合法 JSON")
        cap = self._capacity()
        self.assertEqual(cap["accounts_breakdown"]["cred_paused"], 0)
        self.assertIn("normal", cap["accounts_breakdown"])
        # 文件不存在同样安全（"无暂停 = 文件不存在"语义）
        os.remove(self.cred_state_file)
        cap2 = self._capacity()
        self.assertEqual(cap2["accounts_breakdown"]["cred_paused"], 0)

    def test_quota_judgement_unchanged(self):
        """配额判定不受显示层影响：暂停账号仍占额（拆解只是展示）。"""
        self._add("13900000001")
        self._add("13900000002", paused=True)
        self._write_cred_state(["13900000002"])
        self.webapp.write_env_key(self.env_file, "YIBAN_MAX_ACCOUNTS", "2")
        try:
            # 2 个非删除账号（含 1 个自暂停）已到上限 → 再新增 1 个应被拒
            self.assertTrue(
                self.webapp._accounts_at_capacity(1),
                "暂停账号必须仍占名额（配额判定口径未变）",
            )
            cap = self._capacity()
            self.assertEqual(cap["accounts"], 2)
            self.assertEqual(cap["accounts_max"], 2)
        finally:
            self.webapp.write_env_key(self.env_file, "YIBAN_MAX_ACCOUNTS", "")

    def test_settings_stats_from_raw_snapshot_no_decrypt(self):
        """2026-09-14 性能回归：设置页统计改用不解密读取并去重。

        钉死两点：
        (a) 判定路径不触达解密（打桩为抛错，命中即 500）；A2 后 web 侧的解密入口是
            `db.decrypt_account_rows`，原始读入口是 `db.accounts_snapshot`；
        (b) accounts 原始读 / users 读各恰一次（去重），且三分类、owners、
            潜在负载、活跃计数都能用不解密原始行独立复算，口径不变。
        """
        self._add("13900000011")                         # 正常
        self._add("13900000012", paused=True)            # 用户自暂停
        self._add("13900000013")                         # 账密故障暂停
        self._add("13900000014", owner="u1@test.local")  # 有主 + 账密故障暂停
        self._write_cred_state(["13900000013", "13900000014"])
        self.db.create_user(email="u1@test.local", password_hash="x")
        self.db.create_user(email="u2@test.local", password_hash="x")  # 空用户 → 潜在负载

        app = self.webapp.create_app()
        c = app.test_client()
        r = c.post("/api/login", json={"username": "admin@test.local", "password": ADMIN_PASS})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

        with mock.patch.object(
            self.db, "decrypt_account_rows",
            side_effect=AssertionError("设置页不应触发解密"),
        ) as m_dec, mock.patch.object(
            self.db, "accounts_snapshot", wraps=self.db.accounts_snapshot
        ) as m_raw, mock.patch.object(
            self.db, "load_users", wraps=self.db.load_users
        ) as m_users:
            rs = c.get("/api/settings")
        self.assertEqual(rs.status_code, 200, rs.get_data(as_text=True))
        m_dec.assert_not_called()
        self.assertEqual(m_raw.call_count, 1, "账号原始读须去重为单次")
        self.assertEqual(m_users.call_count, 1, "用户读取须去重为单次")

        data = rs.get_json()
        cap, bd = data["capacity"], data["capacity"]["accounts_breakdown"]
        live = [a for a in self.db.load_accounts_raw() if not a["deleted"]]
        cred = self.webapp._cred_paused_phones()
        exp_user = sum(1 for a in live if a.get("user_paused"))
        exp_cred = sum(
            1 for a in live
            if not a.get("user_paused") and str(a.get("phone", "")) in cred
        )
        self.assertEqual(
            bd,
            {"normal": len(live) - exp_user - exp_cred,
             "user_paused": exp_user, "cred_paused": exp_cred},
            "三分类口径：自暂停优先于账密故障",
        )
        self.assertEqual(cap["accounts"], len(live))
        self.assertEqual(sum(bd.values()), cap["accounts"], "三桶求和 = 计容量的账号数")
        owners = {a.get("owner") for a in live if a.get("owner")}
        self.assertEqual(
            data["capacity_estimate"]["potential_load"],
            sum(1 for u in self.db.load_users() if u["email"] not in owners),
        )
        # 计数/配额入口同样不得解密
        with mock.patch.object(
            self.db, "decrypt_account_rows",
            side_effect=AssertionError("计数不应触发解密"),
        ):
            self.assertEqual(self.webapp._capacity_account_count(), len(live))
            self.assertFalse(self.webapp._accounts_at_capacity(0))


if __name__ == "__main__":
    unittest.main(verbosity=2)
