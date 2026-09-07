# -*- coding: utf-8 -*-
"""功能回归（2026-09-07，v0.29.0 追加）。

覆盖：
- /api/logs：q 关键字检索（当日全量行过滤）、all=1 全量查看（5000 行封顶 + truncated）、
  total_lines/returned 统计
- /api/logs/export：按日期导出原始日志（attachment），日期非法 400、无日志 404
- /api/users：review_count（待审核 + 已拒绝）——修复仅有已拒绝账号的用户
  不出现在用户管理待处理栏的口径差
- /api/settings：延迟字段携带即需 confirm_password（缺失/错误 400）；容量预估
  （v0.29.1 口径：账号=最大间隔、用户=间隔中位数；启动延迟已废弃不参与）
  + 超容量拒绝保存；GET 返回 capacity_estimate
- _capacity_estimate：公式单测

用法（项目根目录）：
    py -m pytest tests/test_batch19_features_0907.py -v
"""
import contextlib
import importlib.util
import io
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


def _load_webapp(tag):
    import db as _db
    spec = importlib.util.spec_from_file_location(f"webapp_{tag}", os.path.join(BASE, "web", "app.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"webapp_{tag}"] = mod
    with contextlib.suppress(Exception):
        spec.loader.exec_module(mod)
    return _db, mod


class _Base(unittest.TestCase):
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
            )
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.accounts_file = os.path.join(cls.tmp, "accounts.json")
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_ACCOUNTS_FILE"] = cls.accounts_file
        os.environ["YIBAN_DB_FILE"] = cls.db_file
        os.environ["YIBAN_STATE_DIR"] = cls.tmp
        os.environ["YIBAN_LOG_FILE"] = os.path.join(cls.tmp, "sign.log")
        cls.db, cls.webapp = _load_webapp(cls.__name__)

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


class LogSearchAllExportTest(_Base):
    """/api/logs 检索/全量 + /api/logs/export 导出。"""

    LOG = (
        "[2026-09-07 06:30:01] [INFO] yiban: [13800138000] 🚀 开始签到\n"
        "[2026-09-07 06:30:05] [INFO] yiban: [13800138000] ✅ 签到成功\n"
        "[2026-09-07 06:31:02] [INFO] yiban: [13800139000] 🚀 开始签到\n"
        "[2026-09-07 06:31:09] [ERROR] yiban: [13800139000] ❌ 签到失败: 密码错误\n"
    )

    def _write_log(self, date):
        path = os.path.join(self.tmp, f"sign-{date}.log")
        with io.open(path, "w", encoding="utf-8") as f:
            f.write(self.LOG)
        return path

    def test_logs_default_last_80_with_totals(self):
        date = "2026-09-07"
        c, h = self._master()  # 先建会话：口令迁移等启动期日志写在覆写之前
        self._write_log(date)
        r = c.get("/api/logs?date=" + date, headers=h)
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertEqual(data["total_lines"], 4)
        self.assertEqual(data["returned"], 4)  # 4 行 < 80：全量返回
        self.assertFalse(data["truncated"])

    def test_logs_search_filters_full_day(self):
        date = "2026-09-07"
        c, h = self._master()
        self._write_log(date)
        # q 作用于打码后的行（与页面展示一致）：原始手机号已变 [139****9000]
        r = c.get(f"/api/logs?date={date}&q=138%2A%2A%2A%2A9000", headers=h)
        data = r.get_json()
        self.assertEqual(data["total_lines"], 4)
        self.assertEqual(data["returned"], 2)
        self.assertTrue(all("138****9000" in ln for ln in data["logs"]))
        # 大小写不敏感子串（对 ERROR 级别关键字）
        r2 = c.get(f"/api/logs?date={date}&q=error", headers=h)
        self.assertEqual(r2.get_json()["returned"], 1)

    def test_logs_all_flag(self):
        date = "2026-09-07"
        self._write_log(date)
        c, h = self._master()
        r = c.get(f"/api/logs?date={date}&all=1", headers=h)
        data = r.get_json()
        self.assertEqual(data["returned"], 4)
        self.assertFalse(data["truncated"])
        # 首行可见（修复「看不到当日靠前的日志」）

        self.assertIn("06:30:01", data["logs"][0])

    def test_export_download(self):
        date = "2026-09-07"
        c, h = self._master()
        self._write_log(date)
        r = c.get(f"/api/logs/export?date={date}", headers=h)
        self.assertEqual(r.status_code, 200)
        self.assertIn("attachment", r.headers.get("Content-Disposition", ""))
        self.assertIn("sign-" + date + ".log", r.headers.get("Content-Disposition", ""))
        self.assertIn("签到成功", r.get_data(as_text=True))

    def test_export_bad_date_and_missing(self):
        c, h = self._master()
        r = c.get("/api/logs/export?date=2026/09/07", headers=h)
        self.assertEqual(r.status_code, 400)
        r = c.get("/api/logs/export?date=2026-09-01", headers=h)  # 未写日志
        self.assertEqual(r.status_code, 404)


class UsersReviewCountTest(_Base):
    """/api/users review_count：待审核 + 已拒绝（修复口径差）。"""

    def test_review_count_covers_rejected(self):
        self._seed_user_with_account("u-pending@test.local", "13900139001", "pending")
        self._seed_user_with_account("u-rejected@test.local", "13900139002", "rejected")
        self._seed_user_with_account("u-active@test.local", "13900139003", "active")
        c, h = self._master()
        data = c.get("/api/users", headers=h).get_json()
        counts = {u["email"]: u for u in data["users"]}
        self.assertEqual(counts["u-pending@test.local"]["review_count"], 1)
        self.assertEqual(counts["u-pending@test.local"]["pending_count"], 1)
        # 修复点：仅有已拒绝账号的用户 review_count=1（旧口径 pending_count=0 → 不显示）
        self.assertEqual(counts["u-rejected@test.local"]["review_count"], 1)
        self.assertEqual(counts["u-rejected@test.local"]["pending_count"], 0)
        self.assertEqual(counts["u-active@test.local"]["review_count"], 0)


class CapacitySettingsTest(_Base):
    """/api/settings 容量预估 + 延迟修改的密码确认与超容量拒绝。"""

    def test_get_returns_capacity_estimate(self):
        # 先重置 env：同类更前的保存用例会写入延迟值，估算按当前设置实时计算
        with io.open(self.env_file, "w", encoding="utf-8") as f:
            f.write(
                f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
                "YIBAN_ADMIN_USER=admin@test.local\n"
                f"YIBAN_ADMIN_PASSWORD={ADMIN_PASS}\n"
            )
        c, h = self._master()
        data = c.get("/api/settings", headers=h).get_json()
        est = data["capacity_estimate"]
        for k in ("accounts_cap", "users_cap", "current_users", "current_holders"):
            self.assertIn(k, est)
        # gap 缺省取 DEFAULT_ACCOUNT_GAP_MAX=10：账号 = (4800-8)/18+1 = 267、
        # 用户（间隔中位数 5）= (4800-8)/13+1 = 369
        self.assertEqual(est["accounts_cap"], 267)
        self.assertEqual(est["users_cap"], 369)

    def test_delay_requires_confirm_password(self):
        c, h = self._master()
        r = c.post("/api/settings", json={"start_delay_max": 60}, headers=h)
        self.assertEqual(r.status_code, 400)
        r = c.post("/api/settings", json={"start_delay_max": 60, "confirm_password": "wrong!"},
                   headers=h)
        self.assertEqual(r.status_code, 400)

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
        # 恶性间隔 gap=3600 → 预估用户容量 = (4800-8)/1808+1 = 3；灌 4 个用户 → 必超
        for i in range(4):
            self.db.create_user(f"u{i}@test.local", "x", role="user")
        c, h = self._master()
        r = c.post("/api/settings",
                   json={"start_delay_max": 3600, "gap_max": 3600, "confirm_password": ADMIN_PASS},
                   headers=h)
        self.assertEqual(r.status_code, 400)
        self.assertIn("容量已满", r.get_json()["error"])
        env = io.open(self.env_file, encoding="utf-8").read()
        self.assertNotIn("YIBAN_START_DELAY_MAX=3600", env, "拒绝保存时不得落盘")


class CapacityFormulaTest(_Base):
    """_capacity_estimate 公式（v0.29.1：启动延迟废弃，仅间隔参与）。"""

    def test_formula_and_median(self):
        with mock.patch.object(self.webapp, "_sign_window",
                               return_value=((6, 30), (7, 50))), \
             mock.patch.object(self.webapp.signin, "_schedule_config",
                               return_value={"avg_attempt_sec": 8}):
            # 4800s 窗口、无间隔：账号 = 用户 = (4800-8)/8+1 = 600
            self.assertEqual(self.webapp._capacity_estimate(0), (600, 600))
            # 账号按最大间隔 gap=10：(4800-8)/18+1 = 267
            # 用户按间隔中位数 gap/2=5：(4800-8)/13+1 = 369
            cap_a, cap_u = self.webapp._capacity_estimate(10)
            self.assertEqual(cap_a, (4800 - 8) // 18 + 1)
            self.assertEqual(cap_u, (4800 - 8) // 13 + 1)
            self.assertGreaterEqual(cap_u, cap_a)
            # gap=3600：账号 = 4792/3608+1 = 2、用户 = 4792/1808+1 = 3
            # （W=4800 恒大于单账号耗时，新口径下不再出现"窗口装不下→0"）
            self.assertEqual(self.webapp._capacity_estimate(3600), (2, 3))


if __name__ == "__main__":
    unittest.main(verbosity=2)
