# -*- coding: utf-8 -*-
"""调度 v2（S2/S3）自选时间片全链路测试：db 层 + 调度层 + API 层。

全程 mock / 纯计算，不访问易班服务器（无任何网络请求）。
用法（项目根目录）：
    py -m pytest tests/test_time_prefs.py -v
    py tests/test_time_prefs.py

覆盖（docs/design/plan-scheduler-v2.md 2.2/2.3/3.3/6 章）：
- db：set/get/clear/stats；账号 purge 连带清理自选
- 调度：自选固定所选片；同片超 K 先到先得（updated_at 早者留）；溢出双向就近顺延；
  总开关关时忽略自选
- API：my-time-pref GET/PUT 校验（5 对齐/窗口内/null 清除）；stats 仅管理员；
  accounts 返回 time_pref 字段；settings 读写新参数
"""
import contextlib
import importlib.util
import json
import os
import random
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
USER_PASS = "secret1"


def hm(dt):
    """datetime → 当天分钟数（0:00 = 0）。"""
    return dt.hour * 60 + dt.minute


class TimePrefsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-pref-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(
                f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
                f"YIBAN_ADMIN_USER=admin\nYIBAN_ADMIN_PASSWORD={ADMIN_PASS}\n"
                "YIBAN_ALLOW_TIME_PREF=1\n"
            )
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.accounts_file = os.path.join(cls.tmp, "accounts.json")
        cls.users_file = os.path.join(cls.tmp, "users.json")
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_ACCOUNTS_FILE"] = cls.accounts_file
        os.environ["YIBAN_USERS_FILE"] = cls.users_file
        os.environ["YIBAN_DB_FILE"] = cls.db_file
        os.environ["YIBAN_ALLOW_TIME_PREF"] = "1"
        global db, signin
        import db
        import signin
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
        for k in ("YIBAN_ACCOUNTS_KEY", "YIBAN_ALLOW_TIME_PREF"):
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

    # ================= db 层 =================
    def test_db_crud_and_stats(self):
        db.set_time_pref("13800138001", 0, "2026-08-15 10:00:00")
        db.set_time_pref("13900139002", 5, "2026-08-15 10:01:00")
        prefs = db.get_time_prefs()
        self.assertEqual(prefs["13800138001"]["slot_min"], 0)
        self.assertEqual(db.get_time_pref("13800138001")["slot_min"], 0)
        self.assertIsNone(db.get_time_pref("13700000000"))
        stats = {s["slot_min"]: s["count"] for s in db.time_pref_stats()}
        self.assertEqual(stats, {0: 1, 5: 1})
        db.clear_time_pref("13800138001")
        self.assertIsNone(db.get_time_pref("13800138001"))

    def test_db_purge_cleans_pref(self):
        db.set_time_pref("13800138001", 0, "t")
        acc_id = next(
            r["id"] for r in db.load_accounts_raw() if r["phone"] == "13800138001"
        )
        db.purge_account(acc_id)
        self.assertIsNone(db.get_time_pref("13800138001"))

    # ================= 调度层（纯计算，无网络） =================
    def _accs(self, n, base=13810000000):
        return [signin.Account(phone=str(base + i), password="p") for i in range(n)]

    def test_schedule_pref_fixed_slot(self):
        """自选账号固定落在所选片（06:30 片 → 首块 [06:31, 06:35)）。"""
        accs = self._accs(20)
        prefs = {"13810000000": {"slot_min": 0, "updated_at": "2026-08-15 10:00:00"}}
        sched = signin.build_schedule(
            accs, order="random", dist="uniform", rng=random.Random(1), prefs=prefs)
        t = sched["13810000000"]
        self.assertTrue(391 <= hm(t) < 395, t)

    def test_schedule_pref_fifo_overflow_nearby(self):
        """同片 16 人（K=15）：updated_at 最早 15 人留下，最晚 1 人就近顺延到块 1。"""
        accs = self._accs(18)
        prefs = {}
        for i in range(16):
            phone = str(13810000000 + i)
            prefs[phone] = {"slot_min": 0, "updated_at": f"2026-08-15 {10 + i // 60:02d}:{i % 60:02d}:00"}
        sched = signin.build_schedule(
            accs, order="random", dist="uniform", rng=random.Random(2), prefs=prefs)
        for i in range(15):
            m = hm(sched[str(13810000000 + i)])
            self.assertTrue(391 <= m < 395, (i, m))
        overflow = hm(sched[str(13810000000 + 15)])
        self.assertTrue(395 <= overflow < 400, overflow)  # 就近顺延块 1 [06:35,06:40)

    def test_schedule_pref_ignored_when_switch_off(self):
        """总开关关：prefs=None 且不读 db，全部走算法（不激活）。"""
        accs = self._accs(10)
        os.environ["YIBAN_ALLOW_TIME_PREF"] = "0"
        try:
            sched = signin.build_schedule(
                accs, order="sequence", dist="uniform", rng=random.Random(3), prefs=None)
            self.assertEqual(len(sched), 10)
            self.assertTrue(all(391 <= hm(t) <= 469 for t in sched.values()))
        finally:
            os.environ["YIBAN_ALLOW_TIME_PREF"] = "1"

    def test_schedule_pref_tail_slot_within_window(self):
        """自选尾片（07:45 片，slot 75）：时间 ∈ [07:45, 07:49]（首尾缓冲内不越界）。"""
        accs = self._accs(20)
        prefs = {"13810000000": {"slot_min": 75, "updated_at": "2026-08-15 10:00:00"}}
        sched = signin.build_schedule(
            accs, order="sequence", dist="normal", rng=random.Random(4), prefs=prefs)
        t = sched["13810000000"]
        self.assertTrue(465 <= hm(t) <= 469, t)

    # ================= API 层（Flask test client，无网络） =================
    def _login(self, c, username, password):
        r = c.post("/api/login", json={"username": username, "password": password})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return c.get("/api/me").get_json()["csrf_token"]

    def _csrf(self, token):
        return {"X-CSRF-Token": token}

    def test_api_pref_get_put_clear(self):
        c = self.webapp.create_app().test_client()
        token = self._login(c, "user1@test.local", USER_PASS)
        r = c.get("/api/my-time-pref")
        data = r.get_json()
        self.assertTrue(data["ok"])
        self.assertTrue(data["has_account"])
        self.assertTrue(data["allowed"])
        self.assertEqual(len(data["slots"]), 16)
        self.assertIsNone(data["pref"])
        # 预计签到时段：顺序排序（默认）→ 非空可预期
        self.assertIsNotNone(data["estimated"], "顺序排序应返回预计时段")
        self.assertRegex(data["estimated"], r"\d{2}:\d{2}")
        # 保存 slot 0（06:30 片）
        r = c.put("/api/my-time-pref", json={"slot_min": 0}, headers=self._csrf(token))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        data = c.get("/api/my-time-pref").get_json()
        self.assertEqual(data["pref"], "06:30")
        # 清除
        r = c.put("/api/my-time-pref", json={"slot_min": None}, headers=self._csrf(token))
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(c.get("/api/my-time-pref").get_json()["pref"])

    def test_api_pref_estimate_random_order_null(self):
        """随机排序：预计时段为 None + 提示文案（随机才不提醒）。"""
        c = self.webapp.create_app().test_client()
        self._login(c, "user1@test.local", USER_PASS)
        env = open(self.env_file, "a", encoding="utf-8")
        env.write("YIBAN_SIGN_ORDER=random\n")
        env.close()
        try:
            data = c.get("/api/my-time-pref").get_json()
            self.assertIsNone(data["estimated"])
            self.assertIn("当天 06:31 后可见", data["estimate_note"])
        finally:
            s = open(self.env_file, encoding="utf-8").read()
            open(self.env_file, "w", encoding="utf-8").write(
                s.replace("YIBAN_SIGN_ORDER=random\n", ""))

    def test_api_pref_validation(self):
        c = self.webapp.create_app().test_client()
        token = self._login(c, "user1@test.local", USER_PASS)
        h = self._csrf(token)
        self.assertEqual(c.put("/api/my-time-pref", json={"slot_min": 7}, headers=h).status_code, 400)
        self.assertEqual(c.put("/api/my-time-pref", json={"slot_min": 999}, headers=h).status_code, 400)
        self.assertEqual(c.put("/api/my-time-pref", json={"slot_min": "abc"}, headers=h).status_code, 400)

    def test_api_pref_save_window_start_59_no_crash(self):
        """对抗：窗口起点分钟=59（06:59）→ 保存自选不得 500（boundary 计算需进位）。"""
        with open(self.env_file, "a", encoding="utf-8") as f:
            f.write("YIBAN_SIGN_START=06:59\nYIBAN_SIGN_END=07:50\n")
        try:
            c = self.webapp.create_app().test_client()
            token = self._login(c, "user1@test.local", USER_PASS)
            r = c.put("/api/my-time-pref", json={"slot_min": 0}, headers=self._csrf(token))
            self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
            self.assertIn("已保存", r.get_json()["msg"])
        finally:
            s = open(self.env_file, encoding="utf-8").read()
            open(self.env_file, "w", encoding="utf-8").write(
                s.replace("YIBAN_SIGN_START=06:59\nYIBAN_SIGN_END=07:50\n", ""))

    def test_api_pref_crowding_pct_no_pii(self):
        """对抗（2026-08-15 用户决策）：用户端拥挤度只返回已选百分比，不暴露人数/容量（防调研）。"""
        # 5 人选同一片 + 块容量 15 → pct=33
        for i in range(5):
            db.set_time_pref(f"139{i:08d}", 0, f"2026-08-15 0{i+1}:00:00")
        c = self.webapp.create_app().test_client()
        self._login(c, "user1@test.local", USER_PASS)
        data = c.get("/api/my-time-pref").get_json()
        slot0 = next(s for s in data["slots"] if s["slot_min"] == 0)
        self.assertEqual(slot0["pct"], 33)
        self.assertNotIn("count", slot0)  # 不暴露真实人数
        self.assertNotIn("cap", slot0)    # 不暴露块容量（防反推人数）

    def test_api_pref_full_slot_notice(self):
        """对抗（2026-08-15 用户决策）：满员片仍可保存（先到先得+顺延语义），提示"已选满"且不带人数。"""
        # 填满 slot 0（cap 默认 15）
        for i in range(15):
            db.set_time_pref(f"138{i:08d}", 0, f"2026-08-15 0{i+1}:00:00")
        c = self.webapp.create_app().test_client()
        token = self._login(c, "user1@test.local", USER_PASS)
        r = c.put("/api/my-time-pref", json={"slot_min": 0}, headers=self._csrf(token))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        msg = r.get_json()["msg"]
        self.assertIn("已选满", msg)
        self.assertNotIn("15", msg)  # 提示不泄露真实人数/容量
        # 自己的位不算满（换片不误报）：清除后重新选回同片不再提示满
        c.put("/api/my-time-pref", json={"slot_min": None}, headers=self._csrf(token))
        # 现在自己已清除 → 再选同片仍满（count=15 不含自己）
        r2 = c.put("/api/my-time-pref", json={"slot_min": 0}, headers=self._csrf(token))
        self.assertIn("已选满", r2.get_json()["msg"])

    def test_api_pref_stats_admin_only(self):
        app = self.webapp.create_app()
        c = app.test_client()
        self._login(c, "user1@test.local", USER_PASS)
        self.assertEqual(c.get("/api/time-prefs/stats").status_code, 403)
        c2 = app.test_client()
        self._login(c2, "admin", ADMIN_PASS)
        r = c2.get("/api/time-prefs/stats")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.get_json()["slots"]), 16)

    def test_api_accounts_time_pref_field(self):
        c = self.webapp.create_app().test_client()
        self._login(c, "admin", ADMIN_PASS)
        db.set_time_pref("13800138001", 0, "2026-08-15 10:00:00")
        data = c.get("/api/accounts").get_json()
        acc = next(a for a in data["accounts"] if a["phone"] == "138****8001")
        self.assertEqual(acc["time_pref"], "06:30")
        self.assertEqual(acc["time_pref_edge"], "first")

    def test_api_settings_extended(self):
        c = self.webapp.create_app().test_client()
        token = self._login(c, "admin", ADMIN_PASS)
        data = c.get("/api/settings").get_json()
        self.assertEqual(data["sign_order"], "sequence")
        self.assertEqual(data["sign_dist"], "uniform")
        self.assertEqual(data["window_edge_sec"], 60)
        self.assertEqual(data["allow_time_pref"], 1)
        self.assertIn("06:30", data["sign_window"])
        # 保存新参数 → .env 生效
        r = c.post("/api/settings", json={
            "sign_order": "random", "sign_dist": "normal",
            "window_edge_sec": 0, "allow_time_pref": 0,
        }, headers=self._csrf(token))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        env = open(self.env_file, encoding="utf-8").read()
        self.assertIn("YIBAN_SIGN_ORDER=random", env)
        self.assertIn("YIBAN_SIGN_DIST=normal", env)
        self.assertIn("YIBAN_WINDOW_EDGE_SEC=0", env)
        self.assertIn("YIBAN_ALLOW_TIME_PREF=0", env)

    def test_api_settings_window_validation(self):
        c = self.webapp.create_app().test_client()
        token = self._login(c, "admin", ADMIN_PASS)
        h = self._csrf(token)
        r = c.post("/api/settings", json={"sign_window": "07:50 ~ 06:30"}, headers=h)
        self.assertEqual(r.status_code, 400)
        r = c.post("/api/settings", json={"sign_window": "06:30 ~ 07:50"}, headers=h)
        self.assertEqual(r.status_code, 200)
        env = open(self.env_file, encoding="utf-8").read()
        self.assertIn("YIBAN_SIGN_START=06:30", env)
        self.assertIn("YIBAN_SIGN_END=07:50", env)

    def test_api_pref_no_account_400(self):
        c = self.webapp.create_app().test_client()
        self._login(c, "admin", ADMIN_PASS)
        # 管理员无 my-phone（owner=admin 的账号不属于普通用户自选）→ has_account 为 False
        # 管理员本身走内置认证，不查账号；直接用无账号用户验证
        db.create_user("user2@test.local", self.webapp.generate_password_hash(USER_PASS))
        c2 = self.webapp.create_app().test_client()
        token = self._login(c2, "user2@test.local", USER_PASS)
        data = c2.get("/api/my-time-pref").get_json()
        self.assertFalse(data["has_account"])
        self.assertEqual(
            c2.put("/api/my-time-pref", json={"slot_min": 0}, headers=self._csrf(token)).status_code,
            400)

    # ================= 用户自暂停签到（调度 v2） =================
    def test_api_pause_resume(self):
        c = self.webapp.create_app().test_client()
        token = self._login(c, "user1@test.local", USER_PASS)
        h = self._csrf(token)
        r = c.put("/api/my-accounts/0/pause", json={"paused": True}, headers=h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        acc = next(a for a in db.load_accounts() if a["phone"] == "13800138001")
        self.assertTrue(acc["user_paused"])
        r = c.put("/api/my-accounts/0/pause", json={"paused": False}, headers=h)
        self.assertEqual(r.status_code, 200)
        acc = next(a for a in db.load_accounts() if a["phone"] == "13800138001")
        self.assertFalse(acc["user_paused"])

    def test_api_accounts_shows_paused_immediately(self):
        """管理端立即体现：用户暂停后 /api/accounts 状态直接为 user_cancelled（无需等状态文件）。"""
        db.set_user_paused(next(a["id"] for a in db.load_accounts_raw()
                                if a["phone"] == "13800138001"), True)
        c = self.webapp.create_app().test_client()
        self._login(c, "admin", ADMIN_PASS)
        data = c.get("/api/accounts").get_json()
        self.assertEqual(data["states"].get("138****8001"), "user_cancelled")
        self.assertIn("已取消", data["state_msgs"].get("138****8001", ""))

    def test_schedule_skips_paused_account(self):
        """build_schedule 过滤 user_paused 账号（零占位）。"""
        accs = self._accs(5)
        accs[2].user_paused = True
        sched = signin.build_schedule(
            accs, order="sequence", dist="uniform", rng=random.Random(1))
        self.assertEqual(len(sched), 4)
        self.assertNotIn(accs[2].phone, sched)

    def test_run_queue_retry_skips_paused(self):
        """端到端确认：user_paused 账号在 run_queue_retry 中零请求、状态写 user_cancelled。"""
        import unittest.mock as mock

        accs = [signin.Account(phone="13800138001", password="p")]
        accs[0].user_paused = True
        state_dir = tempfile.mkdtemp(prefix="yiban-pause-")
        self.addCleanup(shutil.rmtree, state_dir, ignore_errors=True)
        with mock.patch.object(signin, "_write_sign_state") as w, \
             mock.patch.object(signin, "attempt_signin") as attempt:
            results = signin.run_queue_retry(accs, "", 0, 0)
        attempt.assert_not_called()  # 零请求
        self.assertEqual(results["13800138001"][3], signin.STATUS_USER_CANCELLED)
        w.assert_called_once()
        self.assertEqual(w.call_args[0][1], signin.STATUS_USER_CANCELLED)

    def test_run_queue_retry_window_over_zero_request(self):
        """对抗（2026-08-15）：窗口已过（08:30 > 07:50）→ 全部零请求跳过，不登录不发通知。"""
        import unittest.mock as mock
        from datetime import datetime as _dt

        accs = [signin.Account(phone="13800138001", password="p")]

        class FakeNow:
            @staticmethod
            def now():
                return _dt(2026, 8, 15, 8, 30, 0)

        sched = {"13800138001": _dt(2026, 8, 15, 6, 40)}
        with mock.patch.object(signin, "datetime", FakeNow), \
             mock.patch.object(signin, "attempt_signin") as attempt, \
             mock.patch.object(signin, "_write_sign_state") as w, \
             mock.patch.object(signin, "_update_cred_state"):
            results = signin.run_queue_retry(accs, "http://notify.invalid", 0, 0, schedule=sched)
        attempt.assert_not_called()
        self.assertEqual(results["13800138001"][3], signin.STATUS_SKIPPED_WINDOW)
        self.assertEqual(w.call_args[0][1], signin.STATUS_SKIPPED_WINDOW)

    def test_api_capacity_user_registration_limit(self):
        """对抗性审查补：注册总人数上限（YIBAN_MAX_USERS）——超限拒绝。"""
        # 临时设上限 = 当前用户数 + 1（追加 .env，用完移除）
        cur = len(db.load_users())
        limit = cur + 1
        with open(self.env_file, "a", encoding="utf-8") as f:
            f.write(f"YIBAN_MAX_USERS={limit}\n")
        try:
            app = self.webapp.create_app()
            c = app.test_client()
            # 第一个注册成功
            r = c.post("/api/register", json={"email": "cap1@test.local", "password": "StrongPass1!"})
            self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
            # 第二个被拒（已达上限）
            r2 = c.post("/api/register", json={"email": "cap2@test.local", "password": "StrongPass1!"})
            self.assertEqual(r2.status_code, 403, r2.get_data(as_text=True))
            self.assertIn("上限", r2.get_json()["error"])
        finally:
            s = open(self.env_file, encoding="utf-8").read()
            open(self.env_file, "w", encoding="utf-8").write(
                s.replace(f"YIBAN_MAX_USERS={limit}\n", ""))

    def test_api_capacity_accounts_limit(self):
        """对抗性审查补：账号总数上限（YIBAN_MAX_ACCOUNTS）——管理员添加与用户提交均受限。"""
        with open(self.env_file, "a", encoding="utf-8") as f:
            f.write("YIBAN_MAX_ACCOUNTS=1\n")
        try:
            app = self.webapp.create_app()
            c = app.test_client()
            self._login(c, "admin", ADMIN_PASS)
            token = c.get("/api/me").get_json()["csrf_token"]
            h = {"X-CSRF-Token": token}
            # 第一个账号添加成功（setUp 已有一个 → 已达 1 → 被拒）
            r = c.post("/api/accounts", json={
                "name": "C1", "phone": "13700137001", "password": "p1",
            }, headers=h)
            self.assertEqual(r.status_code, 403, r.get_data(as_text=True))
            self.assertIn("上限", r.get_json()["error"])
            # 用户提交同样受限
            c2 = app.test_client()
            token2 = self._login(c2, "user1@test.local", USER_PASS)
            r2 = c2.post("/api/my-accounts", json={
                "name": "U2", "phone": "13700137002", "password": "p2",
            }, headers={"X-CSRF-Token": token2})
            self.assertEqual(r2.status_code, 403, r2.get_data(as_text=True))
            # settings 容量状态返回
            c3 = app.test_client()
            self._login(c3, "admin", ADMIN_PASS)
            data = c3.get("/api/settings").get_json()
            self.assertEqual(data["capacity"]["accounts_max"], 1)
            self.assertGreaterEqual(data["capacity"]["accounts"], 1)
        finally:
            s = open(self.env_file, encoding="utf-8").read()
            open(self.env_file, "w", encoding="utf-8").write(
                s.replace("YIBAN_MAX_ACCOUNTS=1\n", ""))

    def test_api_settings_sched_master_only(self):
        """调度字段仅主管理员可改；普通管理员 403，其他字段仍可改。"""
        app = self.webapp.create_app()
        # 普通管理员（非主）
        db.create_user("admin2@test.local", self.webapp.generate_password_hash(USER_PASS), role="admin")
        c = app.test_client()
        token = self._login(c, "admin2@test.local", USER_PASS)
        h = self._csrf(token)
        r = c.post("/api/settings", json={"sign_order": "random"}, headers=h)
        self.assertEqual(r.status_code, 403, r.get_data(as_text=True))
        # 低风险字段（周日）仍可改
        r = c.post("/api/settings", json={"sunday_sign": 1}, headers=h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        # 主管理员可改调度
        c2 = app.test_client()
        token2 = self._login(c2, "admin", ADMIN_PASS)
        r = c2.post("/api/settings", json={"sign_order": "random"}, headers=self._csrf(token2))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main(verbosity=2)
