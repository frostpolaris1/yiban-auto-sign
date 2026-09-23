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
import datetime as _datetime_TPREF
import importlib.util
import json
import os
import random
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta  # 弹性冷却测试构造审计时间戳/窗口用

import db
import signin

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

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
                "YIBAN_TIME_PREF_COOLDOWN_SEC=0\n"  # 默认关闭冷却，冷却专项测试单独开启
                "YIBAN_PAUSE_COOLDOWN_SEC=0\n"      # 默认关闭暂停冷却，专项测试单独开启
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
        os.environ["YIBAN_STATE_DIR"] = cls.tmp  # 快照标记/状态文件隔离到临时目录
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
        self._add_stat_account("13900139002")
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

    def _add_stat_account(self, phone):
        """为“只用于统计/拥挤度”的手机号创建未删除账号，避免被 time_pref_stats 新语义排除。"""
        db.add_account({"name": "stat", "phone": phone, "password": "p",
                        "phone_model": "", "phone_code": "", "status": "active",
                        "owner": "admin"})

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
        # 5 人选同一片 + 块容量 15 → 5/15=33.3% → 粗粒度 10% 档 = 30
        for i in range(5):
            phone = f"139{i:08d}"
            self._add_stat_account(phone)
            db.set_time_pref(phone, 0, f"2026-08-15 0{i+1}:00:00")
        c = self.webapp.create_app().test_client()
        self._login(c, "user1@test.local", USER_PASS)
        data = c.get("/api/my-time-pref").get_json()
        slot0 = next(s for s in data["slots"] if s["slot_min"] == 0)
        self.assertEqual(slot0["pct"], 30)
        self.assertNotIn("count", slot0)  # 不暴露真实人数
        self.assertNotIn("cap", slot0)    # 不暴露块容量（防反推人数）

    def test_api_pref_pct_coarse_anti_inference(self):
        """对抗（2026-08-15 深度审查）：10% 粗粒度防反推——1 人与 2 人同显 10%，跳变点不唯一。"""
        c = self.webapp.create_app().test_client()
        self._login(c, "user1@test.local", USER_PASS)
        data = c.get("/api/my-time-pref").get_json()
        slot5 = next(s for s in data["slots"] if s["slot_min"] == 5)
        # 0 人 → 0%
        self.assertEqual(slot5["pct"], 0)
        # 1 人 → 6.67% → 10% 档
        self._add_stat_account("13900000009")
        db.set_time_pref("13900000009", 5, "2026-08-15 10:00:00")
        data = c.get("/api/my-time-pref").get_json()
        slot5 = next(s for s in data["slots"] if s["slot_min"] == 5)
        self.assertEqual(slot5["pct"], 10)
        # 2 人 → 13.3% → 仍 10% 档（无法区分 1 人/2 人 → 反推失效）
        self._add_stat_account("13900000008")
        db.set_time_pref("13900000008", 5, "2026-08-15 10:01:00")
        data = c.get("/api/my-time-pref").get_json()
        slot5 = next(s for s in data["slots"] if s["slot_min"] == 5)
        self.assertEqual(slot5["pct"], 10)
        # 3 人 → 20% 档
        self._add_stat_account("13900000007")
        db.set_time_pref("13900000007", 5, "2026-08-15 10:02:00")
        data = c.get("/api/my-time-pref").get_json()
        slot5 = next(s for s in data["slots"] if s["slot_min"] == 5)
        self.assertEqual(slot5["pct"], 20)

    def test_api_pref_full_exact_100(self):
        """对抗（2026-08-15 深度审查）：未满封顶 90、满员恰好 100——前端判满精确不误报。"""
        # 14/15 = 93.3% → 未满封顶 90（不再四舍五入成 100 误报"已选满"）
        for i in range(14):
            phone = f"137{i:08d}"
            self._add_stat_account(phone)
            db.set_time_pref(phone, 5, f"2026-08-15 0{i+1}:00:00")
        c = self.webapp.create_app().test_client()
        self._login(c, "user1@test.local", USER_PASS)
        data = c.get("/api/my-time-pref").get_json()
        slot5 = next(s for s in data["slots"] if s["slot_min"] == 5)
        self.assertEqual(slot5["pct"], 90)
        # 15/15 → 恰好 100（满员，前端显示"已选满"与后端 count>=cap 一致）
        self._add_stat_account("13700000014")
        db.set_time_pref("13700000014", 5, "2026-08-15 10:00:00")
        data = c.get("/api/my-time-pref").get_json()
        slot5 = next(s for s in data["slots"] if s["slot_min"] == 5)
        self.assertEqual(slot5["pct"], 100)

    def test_api_pref_full_slot_notice(self):
        """对抗（2026-08-15 用户决策）：满员片仍可保存（先到先得+顺延语义），提示"已选满"且不带人数。"""
        # 填满 slot 0（cap 默认 15）
        for i in range(15):
            phone = f"138{i:08d}"
            self._add_stat_account(phone)
            db.set_time_pref(phone, 0, f"2026-08-15 0{i+1}:00:00")
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

    def test_api_pref_cooldown_elastic(self):
        """对抗（2026-08-15 用户反馈→弹性冷却）：60s 窗口内自由次数内放行（浏览式全点一遍正常）；
        超出后递增冷却拦截；清除豁免；清除后重选仍受限。"""
        from datetime import timedelta as _td

        # 开启弹性冷却（基础 30s，自由 20 次；追加覆盖默认 0）
        with open(self.env_file, "a", encoding="utf-8") as f:
            f.write("YIBAN_TIME_PREF_COOLDOWN_SEC=30\n")
        try:
            c = self.webapp.create_app().test_client()
            token = self._login(c, "user1@test.local", USER_PASS)
            h = self._csrf(token)
            # 自由窗口：伪造 5 条近期审计 → 保存仍放行（5 < 20）
            now = datetime.now()
            for i in range(5):
                db.audit("user1@test.local", "time_pref_set", db.hash_phone("13800138001"),
                         (now - _td(seconds=5 * i)).strftime("%Y-%m-%d %H:%M:%S"))
            r = c.put("/api/my-time-pref", json={"slot_min": 0}, headers=h)
            self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
            # 超限：凑满 20 条（审计 target 已是匿名哈希，手工插入时同样使用 db.hash_phone）
            for i in range(15):
                db.audit("user1@test.local", "time_pref_set", db.hash_phone("13800138001"),
                         (now - _td(seconds=5 * i)).strftime("%Y-%m-%d %H:%M:%S"))
            r2 = c.put("/api/my-time-pref", json={"slot_min": 5}, headers=h)
            self.assertEqual(r2.status_code, 429, r2.get_data(as_text=True))
            self.assertIn("频繁", r2.get_json()["error"])
            # 清除不受冷却限制（time_pref_clear 不计数）
            r3 = c.put("/api/my-time-pref", json={"slot_min": None}, headers=h)
            self.assertEqual(r3.status_code, 200, r3.get_data(as_text=True))
            # 清除后立即重选仍受限（防绕过：time_pref_set 计数不因清除清零）
            r4 = c.put("/api/my-time-pref", json={"slot_min": 10}, headers=h)
            self.assertEqual(r4.status_code, 429, r4.get_data(as_text=True))
        finally:
            s = open(self.env_file, encoding="utf-8").read()
            open(self.env_file, "w", encoding="utf-8").write(
                s.replace("YIBAN_TIME_PREF_COOLDOWN_SEC=30\n", ""))

    def test_api_pref_save_audits_hash_for_cooldown_count(self):
        """I1 回归：真实保存自选后冷却计数可通过匿名哈希 target 查到（不再依赖原始手机号）。"""
        c = self.webapp.create_app().test_client()
        token = self._login(c, "user1@test.local", USER_PASS)
        h = self._csrf(token)
        r = c.put("/api/my-time-pref", json={"slot_min": 0}, headers=h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        since = (datetime.now() - timedelta(seconds=60)).strftime("%Y-%m-%d %H:%M:%S")
        self.assertEqual(db.time_pref_set_count_since("13800138001", since), 1)
        conn = db.get_conn()
        row = conn.execute(
            "SELECT target FROM audit_logs WHERE action='time_pref_set' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(row["target"], db.hash_phone("13800138001"))

    def test_api_pref_snapshot_boundary(self):
        """对抗（2026-08-15 用户反馈：卡点缓冲）：生效分界优先取当日调度快照标记——
        标记存在（cron 已快照）→ 之后改选提示"明日生效"；标记不存在 → 回退窗口起点+1 分钟。"""
        import unittest.mock as mock
        from datetime import datetime as _dt

        class FakeDT:  # 替换 webapp.datetime：now 固定 06:30（cron 前），strptime 复用真实实现
            @staticmethod
            def now():
                return _dt(2026, 8, 15, 6, 30, 0)

            strptime = staticmethod(_dt.strptime)

        # 场景 1：无标记 → 兜底 boundary=06:31:00 → 今日生效
        snap = os.path.join(self.tmp, "sched-snapshot-2026-08-15.json")
        if os.path.exists(snap):
            os.remove(snap)
        c = self.webapp.create_app().test_client()
        token = self._login(c, "user1@test.local", USER_PASS)
        h = self._csrf(token)
        with mock.patch.object(self.webapp.clock, "now", FakeDT.now):
            r = c.put("/api/my-time-pref", json={"slot_min": 0}, headers=h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertIn("今日生效", r.get_json()["msg"])
        # 场景 2：标记存在（snapshot_at=06:00:00，cron 已快照）→ 改选在快照后 → 明日生效
        with open(snap, "w", encoding="utf-8") as f:
            json.dump({"snapshot_at": "06:00:00"}, f)
        with mock.patch.object(self.webapp.clock, "now", FakeDT.now):
            r2 = c.put("/api/my-time-pref", json={"slot_min": 5}, headers=h)
        self.assertEqual(r2.status_code, 200, r2.get_data(as_text=True))
        self.assertIn("明日生效", r2.get_json()["msg"])
        # 场景 3：标记时间在未来（时钟偏移/写坏，H1 对抗性审查）→ 视为无效回退兜底（06:31）
        with open(snap, "w", encoding="utf-8") as f:
            json.dump({"snapshot_at": "07:00:00"}, f)
        with mock.patch.object(self.webapp.clock, "now", FakeDT.now):
            r3 = c.put("/api/my-time-pref", json={"slot_min": 10}, headers=h)
        self.assertEqual(r3.status_code, 200, r3.get_data(as_text=True))
        self.assertIn("今日生效", r3.get_json()["msg"])  # 回退兜底 06:31 → now(06:30) 之前

    def test_api_pref_boundary_fallback_uses_effective_start(self):
        """快照标记缺失时的兜底分界取**有效**窗口起点（已扣前裁），不是原始起点+1 分钟。

        原始窗口 07:00~08:00、前裁 300s ⇒ 有效起点 07:05。07:03 改选仍早于该起点，
        引擎此刻尚未读自选表 ⇒ 应提示"今日生效"；旧实现按原始起点+1 分钟算成 07:01，
        会把同一时刻误报成"明日生效"（用户以为今天不生效，实际今天会生效）。
        """
        import unittest.mock as mock
        from datetime import datetime as _dt

        class FakeDT:  # 固定业务钟在 07:03（有效起点 07:05 之前）
            @staticmethod
            def now():
                return _dt(2026, 8, 15, 7, 3, 0)

            strptime = staticmethod(_dt.strptime)

        original = open(self.env_file, encoding="utf-8").read()
        snap = os.path.join(self.tmp, "sched-snapshot-2026-08-15.json")
        if os.path.exists(snap):
            os.remove(snap)
        try:
            with open(self.env_file, "a", encoding="utf-8") as f:
                f.write("YIBAN_SIGN_START=07:00\nYIBAN_SIGN_END=08:00\n"
                        "YIBAN_WINDOW_EDGE_FRONT_SEC=300\nYIBAN_WINDOW_EDGE_BACK_SEC=0\n")
            c = self.webapp.create_app().test_client()
            token = self._login(c, "user1@test.local", USER_PASS)
            h = self._csrf(token)
            with mock.patch.object(self.webapp.clock, "now", FakeDT.now):
                r = c.put("/api/my-time-pref", json={"slot_min": 5}, headers=h)
            self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
            self.assertIn("今日生效", r.get_json()["msg"])
        finally:
            with open(self.env_file, "w", encoding="utf-8") as f:
                f.write(original)

    def test_api_me_sign_window_uses_effective_window(self):
        """`/api/me` 的 `sign_window` 取**生效**窗口端点，与同页自选片卡片同一份几何。

        非退化（生产默认 06:30~07:50）逐值等价；缓冲过大时窗口被保留（只收缩缓冲），
        页面显示的仍是管理员设的 07:00~07:10，与片卡同一份几何（片号基点就是它）。
        """
        base = (f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\nYIBAN_ADMIN_USER=admin\n"
                f"YIBAN_ADMIN_PASSWORD={ADMIN_PASS}\nYIBAN_ALLOW_TIME_PREF=1\n"
                "YIBAN_TIME_PREF_COOLDOWN_SEC=0\nYIBAN_PAUSE_COOLDOWN_SEC=0\n")
        original = open(self.env_file, encoding="utf-8").read()

        def _write(extra):
            with open(self.env_file, "w", encoding="utf-8") as f:
                f.write(base + extra)

        c = self.webapp.create_app().test_client()
        self._login(c, "user1@test.local", USER_PASS)
        try:
            _write("")
            self.assertEqual(c.get("/api/me").get_json()["sign_window"], "06:30 ~ 07:50")
            _write("YIBAN_SIGN_START=07:00\nYIBAN_SIGN_END=07:10\n"
                   "YIBAN_WINDOW_EDGE_FRONT_SEC=300\nYIBAN_WINDOW_EDGE_BACK_SEC=300\n")
            self.assertEqual(c.get("/api/me").get_json()["sign_window"], "07:00 ~ 07:10",
                             "窗口被保留，展示应与片卡同一窗口（而非默认 06:30 ~ 07:50）")
        finally:
            with open(self.env_file, "w", encoding="utf-8") as f:
                f.write(original)

    def test_api_pref_slot_type_strict(self):
        """对抗（M1）：bool（False→0）与小数（5.9→5）截断不得误入合法槽位。"""
        c = self.webapp.create_app().test_client()
        token = self._login(c, "user1@test.local", USER_PASS)
        h = self._csrf(token)
        self.assertEqual(c.put("/api/my-time-pref", json={"slot_min": 5.9}, headers=h).status_code, 400)
        self.assertEqual(c.put("/api/my-time-pref", json={"slot_min": False}, headers=h).status_code, 400)
        self.assertEqual(c.put("/api/my-time-pref", json={"slot_min": True}, headers=h).status_code, 400)
        self.assertEqual(c.put("/api/my-time-pref", json={"slot_min": "5.9"}, headers=h).status_code, 400)
        # 合法整数与整数字符串仍可用
        self.assertEqual(c.put("/api/my-time-pref", json={"slot_min": 0}, headers=h).status_code, 200)
        self.assertEqual(c.put("/api/my-time-pref", json={"slot_min": "5"}, headers=h).status_code, 200)

    def test_db_delete_user_cleans_pref(self):
        """对抗（H2）：删除用户连带清 pref（delete_user_with_accounts）。"""
        db.set_time_pref("13800138001", 0, "2026-08-15 10:00:00")
        db.delete_user_with_accounts("user1@test.local")
        self.assertIsNone(db.get_time_pref("13800138001"))

    def test_db_replace_accounts_cleans_orphan_pref(self):
        """对抗（H2）：整表替换后，被移除账号的 pref 一并清理（防孤儿虚高拥挤度）。"""
        # 13900139099 先作为正式账号入表，再被 replace_accounts 移除
        db.add_account({"name": "B", "phone": "13900139099", "password": "p2",
                        "status": "active", "owner": "admin"})
        db.set_time_pref("13800138001", 0, "2026-08-15 10:00:00")
        db.set_time_pref("13900139099", 5, "2026-08-15 10:01:00")
        db.replace_accounts([{"name": "A", "phone": "13800138001", "password": "p1",
                             "status": "active", "owner": "user1@test.local"}])
        # 13800138001 保留在表内 → pref 保留；13900139099 被移除 → pref 清理
        self.assertIsNotNone(db.get_time_pref("13800138001"))
        self.assertIsNone(db.get_time_pref("13900139099"))

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
        self.assertEqual(data["edge_front_sec"], 60)  # 0.22.0 前后独立（默认各 60s）
        self.assertEqual(data["edge_back_sec"], 60)
        self.assertEqual(data["allow_time_pref"], 1)
        self.assertIn("06:30", data["sign_window"])
        # 保存新参数 → .env 生效（掐头去尾 0.22.0 起写前后两键，旧键删除）
        r = c.post("/api/settings", json={
            "sign_order": "random", "sign_dist": "normal",
            "window_edge_sec": 0, "allow_time_pref": 0,
            "confirm_password": ADMIN_PASS,  # 含 A 档边缘键 → 必须当次口令
        }, headers=self._csrf(token))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        env = open(self.env_file, encoding="utf-8").read()
        self.assertIn("YIBAN_SIGN_ORDER=random", env)
        self.assertIn("YIBAN_SIGN_DIST=normal", env)
        self.assertIn("YIBAN_WINDOW_EDGE_FRONT_SEC=0", env)
        self.assertIn("YIBAN_WINDOW_EDGE_BACK_SEC=0", env)
        self.assertNotIn("YIBAN_WINDOW_EDGE_SEC=", env)
        self.assertIn("YIBAN_ALLOW_TIME_PREF=0", env)

    def test_api_settings_front_back_asymmetric(self):
        """0.22.0：掐头去尾前后独立——POST 不同值 → env 写两键，GET 回读一致。"""
        c = self.webapp.create_app().test_client()
        token = self._login(c, "admin", ADMIN_PASS)
        h = self._csrf(token)
        r = c.post("/api/settings", json={
            "edge_front_sec": 30, "edge_back_sec": 300,
            "confirm_password": ADMIN_PASS,  # A 档：主管理员亦须当次口令
        }, headers=h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        env = open(self.env_file, encoding="utf-8").read()
        self.assertIn("YIBAN_WINDOW_EDGE_FRONT_SEC=30", env)
        self.assertIn("YIBAN_WINDOW_EDGE_BACK_SEC=300", env)
        data = c.get("/api/settings").get_json()
        self.assertEqual(data["edge_front_sec"], 30)
        self.assertEqual(data["edge_back_sec"], 300)

    def test_api_settings_edge_validation(self):
        """0.22.0：裁剪值必须 0~300 且 30 的倍数（0.5 分钟粒度）；非法拒绝且不落盘。"""
        c = self.webapp.create_app().test_client()
        token = self._login(c, "admin", ADMIN_PASS)
        h = self._csrf(token)
        for bad in (15, 301, -30, 130):
            r = c.post("/api/settings", json={"edge_front_sec": bad}, headers=h)
            self.assertEqual(r.status_code, 400, f"edge_front_sec={bad} 应被拒")
        env = open(self.env_file, encoding="utf-8").read()
        self.assertNotIn("YIBAN_WINDOW_EDGE_FRONT_SEC=15", env)

    # ---- 缓冲预防：保存时按窗口宽度夹取（夹取而非拒绝）----
    # 逐用例整文件快照/还原：本类共用一份 .env，而 pytest 按方法名字母序执行，
    # 就地追加的窗口/缓冲键会漏给后面的用例（既有用例同样是这个口径）。
    @contextlib.contextmanager
    def _env_guard(self):
        original = open(self.env_file, encoding="utf-8").read()
        try:
            yield
        finally:
            with open(self.env_file, "w", encoding="utf-8") as f:
                f.write(original)

    def _with_window(self, start, end):
        with open(self.env_file, "a", encoding="utf-8") as f:
            f.write(f"YIBAN_SIGN_START={start}\nYIBAN_SIGN_END={end}\n")

    def test_api_settings_clamps_edge_to_window_share(self):
        """10 分钟窗口 + 各 300s：保存被夹到单边 120s（窗口的 20%）并写明原因。"""
        with self._env_guard():
            self._with_window("06:30", "06:40")
            c = self.webapp.create_app().test_client()
            h = self._csrf(self._login(c, "admin", ADMIN_PASS))
            r = c.post("/api/settings", json={
                "edge_front_sec": 300, "edge_back_sec": 300,
                "confirm_password": ADMIN_PASS,
            }, headers=h)
            self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
            body = r.get_json()
            self.assertIn("120", body["msg"], "响应必须说明被夹到多少")
            self.assertIn("20%", body["msg"], "响应必须说明为什么")
            env = open(self.env_file, encoding="utf-8").read()
            self.assertIn("YIBAN_WINDOW_EDGE_FRONT_SEC=120", env)
            self.assertIn("YIBAN_WINDOW_EDGE_BACK_SEC=120", env)
            data = c.get("/api/settings").get_json()
            self.assertEqual((data["edge_front_sec"], data["edge_back_sec"]), (120, 120))

    def test_api_settings_window_change_uses_new_window_cap(self):
        """同一次请求里改窗口：上限按**新**窗口算（10 分钟 → 单边 120s）。"""
        with self._env_guard():
            c = self.webapp.create_app().test_client()
            h = self._csrf(self._login(c, "admin", ADMIN_PASS))
            r = c.post("/api/settings", json={
                "sign_window": "06:30 ~ 06:40",
                "edge_front_sec": 300, "edge_back_sec": 300,
                "confirm_password": ADMIN_PASS,
            }, headers=h)
            self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
            env = open(self.env_file, encoding="utf-8").read()
            self.assertIn("YIBAN_WINDOW_EDGE_FRONT_SEC=120", env)
            self.assertIn("YIBAN_WINDOW_EDGE_BACK_SEC=120", env)

    def test_api_settings_legacy_edge_key_clamped_symmetrically(self):
        """旧键 `window_edge_sec` 的对称映射保留，且同样被夹（两边都到 120s）。"""
        with self._env_guard():
            self._with_window("06:30", "06:40")
            c = self.webapp.create_app().test_client()
            h = self._csrf(self._login(c, "admin", ADMIN_PASS))
            r = c.post("/api/settings", json={
                "window_edge_sec": 300, "confirm_password": ADMIN_PASS,
            }, headers=h)
            self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
            env = open(self.env_file, encoding="utf-8").read()
            self.assertIn("YIBAN_WINDOW_EDGE_FRONT_SEC=120", env)
            self.assertIn("YIBAN_WINDOW_EDGE_BACK_SEC=120", env)

    def test_api_settings_edge_untouched_when_within_cap(self):
        """默认 80 分钟窗口：单边 20% = 960s > 既有量程，故逐值不变且响应无夹取说明。"""
        with self._env_guard():
            c = self.webapp.create_app().test_client()
            h = self._csrf(self._login(c, "admin", ADMIN_PASS))
            r = c.post("/api/settings", json={
                "edge_front_sec": 30, "edge_back_sec": 300,
                "confirm_password": ADMIN_PASS,
            }, headers=h)
            self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
            self.assertEqual(r.get_json()["msg"], "设置已保存（cron 下次触发自动生效）")
            env = open(self.env_file, encoding="utf-8").read()
            self.assertIn("YIBAN_WINDOW_EDGE_FRONT_SEC=30", env)
            self.assertIn("YIBAN_WINDOW_EDGE_BACK_SEC=300", env)

    def test_api_settings_window_fallback_flag_default_off(self):
        """窗口可用（常态）：`window_fallback` 为假、提示为空串（响应逐字不变）。"""
        c = self.webapp.create_app().test_client()
        self._login(c, "admin", ADMIN_PASS)
        data = c.get("/api/settings").get_json()
        self.assertFalse(data["window_fallback"])
        self.assertEqual(data["window_fallback_text"], "")

    def test_api_settings_window_fallback_flag_visible(self):
        """窗口不可用（已回退默认）：设置页拿到可见提示（"已按 X~Y 运行"）。"""
        from unittest import mock

        from yiban import window as yb_window
        c = self.webapp.create_app().test_client()
        self._login(c, "admin", ADMIN_PASS)
        dead = yb_window.bounds({"sign_start": (7, 0), "sign_end": (6, 0),
                                 "edge_front_sec": 60, "edge_back_sec": 60})
        self.assertTrue(dead.fell_back)
        with mock.patch.object(self.webapp, "sign_window_bounds", return_value=dead):
            data = c.get("/api/settings").get_json()
        self.assertTrue(data["window_fallback"])
        self.assertIn("配置异常", data["window_fallback_text"])
        self.assertIn("06:31~07:49", data["window_fallback_text"])

    def test_api_pref_slots_disabled_partial(self):
        """0.22.0：前 2 分钟 + 后 5 分钟 → 首片 partial（可点+提示）、末片 disabled（灰）。"""
        with open(self.env_file, "a", encoding="utf-8") as f:
            f.write("YIBAN_WINDOW_EDGE_FRONT_SEC=120\nYIBAN_WINDOW_EDGE_BACK_SEC=300\n")
        try:
            c = self.webapp.create_app().test_client()
            self._login(c, "user1@test.local", USER_PASS)
            data = c.get("/api/my-time-pref").get_json()
            self.assertEqual(data["edge_front_sec"], 120)
            self.assertEqual(data["edge_back_sec"], 300)
            first = data["slots"][0]
            last = data["slots"][-1]
            self.assertFalse(first["disabled"], "首片部分保留应可选")
            self.assertTrue(first["edge_note"], "首片应有裁剪提示")
            self.assertTrue(last["disabled"], "末片完全在后裁区内应禁用")
            self.assertFalse(last["edge_note"])
        finally:
            s = open(self.env_file, encoding="utf-8").read()
            open(self.env_file, "w", encoding="utf-8").write(
                s.replace("YIBAN_WINDOW_EDGE_FRONT_SEC=120\n", "")
                 .replace("YIBAN_WINDOW_EDGE_BACK_SEC=300\n", ""))

    def test_api_pref_save_partial_ok_full_clip_rejected(self):
        """0.22.0：部分裁剪片可保存（调度在可用部分安排）；完全裁剪片保存被拒。"""
        with open(self.env_file, "a", encoding="utf-8") as f:
            f.write("YIBAN_WINDOW_EDGE_FRONT_SEC=120\nYIBAN_WINDOW_EDGE_BACK_SEC=300\n")
        try:
            c = self.webapp.create_app().test_client()
            token = self._login(c, "user1@test.local", USER_PASS)
            h = self._csrf(token)
            # 首片（0-5 分，前 2 分钟被裁）→ 部分可用，允许保存
            r = c.put("/api/my-time-pref", json={"slot_min": 0}, headers=h)
            self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
            # 末片（75-80 分，完全在后裁 5 分钟内）→ 拒绝
            r2 = c.put("/api/my-time-pref", json={"slot_min": 75}, headers=h)
            self.assertEqual(r2.status_code, 400, r2.get_data(as_text=True))
        finally:
            s = open(self.env_file, encoding="utf-8").read()
            open(self.env_file, "w", encoding="utf-8").write(
                s.replace("YIBAN_WINDOW_EDGE_FRONT_SEC=120\n", "")
                 .replace("YIBAN_WINDOW_EDGE_BACK_SEC=300\n", ""))

    def test_api_pref_save_ok_when_edges_clamped(self):
        """缓冲过大被收缩：可用性判定与展示同准绳，收缩窗口下的可选片仍可保存。

        窗口 06:30~06:40 前后各 300s（合计 >= 窗口宽度）⇒ `window.bounds` 保留窗口、
        把缓冲等比收缩为各 60s（有效窗口 06:31~06:39）。展示侧 `_pref_slots` 按该窗口
        给出 2 片且不全为灰；保存闸门若仍按原始裁剪判定（前后各 5 分钟），每一片都会被
        判"不在可选范围内"——用户点得到、存不下。
        """
        added = ("YIBAN_SIGN_START=06:30\nYIBAN_SIGN_END=06:40\n"
                 "YIBAN_WINDOW_EDGE_FRONT_SEC=300\nYIBAN_WINDOW_EDGE_BACK_SEC=300\n")
        with open(self.env_file, "a", encoding="utf-8") as f:
            f.write(added)
        try:
            c = self.webapp.create_app().test_client()
            token = self._login(c, "user1@test.local", USER_PASS)
            h = self._csrf(token)
            slots = c.get("/api/my-time-pref").get_json()["slots"]
            self.assertFalse(all(s["disabled"] for s in slots), "收缩窗口下不应全部置灰")
            for slot in (0, 5):
                r = c.put("/api/my-time-pref", json={"slot_min": slot}, headers=h)
                self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        finally:
            s = open(self.env_file, encoding="utf-8").read()
            open(self.env_file, "w", encoding="utf-8").write(s.replace(added, ""))

    def test_clamped_window_clock_labels_agree_across_the_page(self):
        """缓冲过大被收缩：选片卡 / 已存偏好 / 保存提示 / 管理员列表四处钟点必须一致。

        配置原始窗口 07:00~07:10 + 前后各 300s ⇒ `window.bounds` 保留窗口、缓冲收缩为
        各 60s。展示侧若仍按原始裁剪折算，片卡会把两片全置灰；管理员列表的首尾标记若按
        有效窗口宽度（8 分钟）判，也会与片号基准分叉。四处都必须以**窗口起止**为基准。
        """
        added = ("YIBAN_SIGN_START=07:00\nYIBAN_SIGN_END=07:10\n"
                 "YIBAN_WINDOW_EDGE_FRONT_SEC=300\nYIBAN_WINDOW_EDGE_BACK_SEC=300\n")
        with open(self.env_file, "a", encoding="utf-8") as f:
            f.write(added)
        try:
            c = self.webapp.create_app().test_client()
            token = self._login(c, "user1@test.local", USER_PASS)
            h = self._csrf(token)
            data = c.get("/api/my-time-pref").get_json()
            # 窗口被保留：窗口串与片卡标签都以它为基准
            self.assertEqual(data["window"], "07:00 ~ 07:10")
            self.assertEqual(data["slots"][0]["label"], "07:00")
            self.assertEqual(data["slots"][-1]["label"], "07:05")
            # 保存提示与已存偏好标签同基准
            r = c.put("/api/my-time-pref", json={"slot_min": 5}, headers=h)
            self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
            self.assertIn("已保存自选 07:05", r.get_json()["msg"])
            data2 = c.get("/api/my-time-pref").get_json()
            self.assertEqual(data2["pref"], "07:05")
            self.assertEqual(data2["pref_slot"], 5)
            # 管理员列表：片标签同基准，首尾标记按窗口宽度（span=10）判
            adm = self.webapp.create_app().test_client()
            self._login(adm, "admin", ADMIN_PASS)
            acc = next(a for a in adm.get("/api/accounts").get_json()["accounts"]
                       if a["phone"] == "138****8001")
            self.assertEqual(acc["time_pref"], "07:05")
            self.assertEqual(acc["time_pref_edge"], "last")
        finally:
            s = open(self.env_file, encoding="utf-8").read()
            open(self.env_file, "w", encoding="utf-8").write(s.replace(added, ""))

    def test_api_accounts_time_pref_edge_marks_without_fallback(self):
        """非回退（生产默认窗口 06:30~07:50）：片标签与首尾标记逐值不变。"""
        c = self.webapp.create_app().test_client()
        self._login(c, "admin", ADMIN_PASS)
        db.set_time_pref("13800138001", 75, "2026-08-15 10:00:00")
        acc = next(a for a in c.get("/api/accounts").get_json()["accounts"]
                   if a["phone"] == "138****8001")
        self.assertEqual(acc["time_pref"], "07:45")
        self.assertEqual(acc["time_pref_edge"], "last")
        db.set_time_pref("13800138001", 30, "2026-08-15 10:00:00")
        acc = next(a for a in c.get("/api/accounts").get_json()["accounts"]
                   if a["phone"] == "138****8001")
        self.assertEqual(acc["time_pref"], "07:00")
        self.assertIsNone(acc["time_pref_edge"], "中段片不得带首尾标记")

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

    def test_api_pref_non_active_user_hidden(self):
        """对抗（2026-08-15 用户反馈）：仅注册未正式进入签到列表（pending/rejected）的用户
        不可查看/选择时间片——GET has_account=False（前端整卡隐藏）、PUT 400 且不写库。"""
        cases = [("pending", "13600138001"), ("rejected", "13600138002")]
        for status, phone in cases:
            user = f"{status}@test.local"
            db.create_user(user, self.webapp.generate_password_hash(USER_PASS))
            db.add_account({"name": status, "phone": phone, "password": "p",
                            "status": status, "owner": user})
            c = self.webapp.create_app().test_client()
            token = self._login(c, user, USER_PASS)
            data = c.get("/api/my-time-pref").get_json()
            self.assertFalse(data["has_account"], f"{status} 不应有自选资格")
            self.assertIsNone(data["pref"])
            r = c.put("/api/my-time-pref", json={"slot_min": 0}, headers=self._csrf(token))
            self.assertEqual(r.status_code, 400, f"{status} 保存应被拒绝")
            self.assertIn("审核", r.get_json()["error"], f"{status} 提示应区分未生效")
            self.assertIsNone(db.get_time_pref(phone), f"{status} 不应写库")

    def test_api_pause_admin_own_forbidden(self):
        """对抗（2026-08-15 用户确认）：管理员不能暂停自己账号——owner=admin 账号暂停 403
        + pause_forbidden 下发（前端隐藏按钮）；恢复放行（幂等）；注册管理员自己提交的账号
        （owner=本人邮箱）仍可暂停（非系统账号）。"""
        db.add_account({"name": "管理员账号", "phone": "13900139099", "password": "p2",
                        "status": "active", "owner": "admin"})
        c = self.webapp.create_app().test_client()
        token = self._login(c, "admin", ADMIN_PASS)
        h = self._csrf(token)
        data = c.get("/api/my-accounts").get_json()
        acc = next(a for a in data["accounts"] if a["phone"] == "13900139099")
        self.assertTrue(acc["pause_forbidden"], "管理员账号应标记不可暂停")
        r = c.put(f"/api/my-accounts/{acc['index']}/pause", json={"paused": True}, headers=h)
        self.assertEqual(r.status_code, 403, r.get_data(as_text=True))
        self.assertIn("管理员", r.get_json()["error"])
        self.assertFalse(
            next(a for a in db.load_accounts_raw() if a["phone"] == "13900139099").get("user_paused"),
            "管理员账号不应被暂停写入")
        # 恢复放行（幂等无危害）
        r2 = c.put(f"/api/my-accounts/{acc['index']}/pause", json={"paused": False}, headers=h)
        self.assertEqual(r2.status_code, 200, r2.get_data(as_text=True))
        # 注册管理员自己提交的账号（owner=本人邮箱）仍可暂停——只有系统管理员账号受保护
        db.create_user("admin2@test.local", self.webapp.generate_password_hash(USER_PASS), role="admin")
        db.add_account({"name": "A2", "phone": "13600999001", "password": "p3",
                        "status": "active", "owner": "admin2@test.local"})
        c2 = self.webapp.create_app().test_client()
        token2 = self._login(c2, "admin2@test.local", USER_PASS)
        r3 = c2.put("/api/my-accounts/0/pause", json={"paused": True}, headers=self._csrf(token2))
        self.assertEqual(r3.status_code, 200, r3.get_data(as_text=True))
        self.assertTrue(
            next(a for a in db.load_accounts_raw() if a["phone"] == "13600999001").get("user_paused"))

    def test_api_pref_own_account_per_admin(self):
        """对抗（2026-08-15 用户报告严重问题）：注册管理员的选片必须绑定自己的账号。
        此前 _my_phone() 的 admin 分支硬编码 owner='admin'，导致所有管理员（含注册管理员）
        都看到并覆盖内置管理员的选片。修复：与"我的账号"视图同口径
        （内置管理员=owner admin/本人邮箱；注册管理员=owner 本人邮箱）。"""
        # 内置管理员账号（owner=admin）选 slot 5；注册管理员 admin2 有自己的账号
        db.add_account({"name": "内置管理员", "phone": "13900139099", "password": "p2",
                        "status": "active", "owner": "admin"})
        db.set_time_pref("13900139099", 5, "2026-08-15 10:00:00")
        db.create_user("admin2@test.local", self.webapp.generate_password_hash(USER_PASS), role="admin")
        db.add_account({"name": "A2", "phone": "13600999001", "password": "p3",
                        "status": "active", "owner": "admin2@test.local"})
        # admin2 保存选片 → 应写入自己的账号（13600999001），不得覆盖内置管理员账号
        c2 = self.webapp.create_app().test_client()
        token2 = self._login(c2, "admin2@test.local", USER_PASS)
        r = c2.put("/api/my-time-pref", json={"slot_min": 10}, headers=self._csrf(token2))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(db.get_time_pref("13600999001")["slot_min"], 10, "应写入 admin2 自己账号")
        self.assertEqual(db.get_time_pref("13900139099")["slot_min"], 5, "不得覆盖内置管理员账号的选片")
        # admin2 GET：pref 显示自己的选片（10），而不是内置管理员的（5）
        data2 = c2.get("/api/my-time-pref").get_json()
        self.assertTrue(data2["has_account"])
        self.assertEqual(data2["pref_slot"], 10, "admin2 应看到自己的选片")
        # 内置管理员 GET：仍看到自己的选片（5）
        c = self.webapp.create_app().test_client()
        self._login(c, "admin", ADMIN_PASS)
        data = c.get("/api/my-time-pref").get_json()
        self.assertEqual(data["pref_slot"], 5, "内置管理员应看到自己的选片")

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

    def test_api_pause_cooldown(self):
        """对抗（2026-08-16 调整）：暂停采用弹性冷却——60s 窗口内前 3 次自由，
        恢复不限（紧迫正向操作）；第 4 次暂停才触发冷却，防连点/防刷屏噪音。"""
        with open(self.env_file, "a", encoding="utf-8") as f:
            f.write("YIBAN_PAUSE_COOLDOWN_SEC=30\n")
        try:
            c = self.webapp.create_app().test_client()
            token = self._login(c, "user1@test.local", USER_PASS)
            h = self._csrf(token)
            # 前 3 次暂停完全自由（好奇地暂停/恢复/再暂停不会被误杀）
            for _ in range(3):
                r = c.put("/api/my-accounts/0/pause", json={"paused": True}, headers=h)
                self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
                r = c.put("/api/my-accounts/0/pause", json={"paused": False}, headers=h)
                self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
            # 第 4 次暂停触发弹性冷却 → 429（不暴露时长，信息分层）
            r4 = c.put("/api/my-accounts/0/pause", json={"paused": True}, headers=h)
            self.assertEqual(r4.status_code, 429, r4.get_data(as_text=True))
            self.assertIn("频繁", r4.get_json()["error"])
            self.assertNotIn("30", r4.get_json()["error"])
            # 冷却期内恢复不受限（紧迫正向操作）
            r5 = c.put("/api/my-accounts/0/pause", json={"paused": False}, headers=h)
            self.assertEqual(r5.status_code, 200, r5.get_data(as_text=True))
            acc = next(a for a in db.load_accounts() if a["phone"] == "13800138001")
            self.assertFalse(acc["user_paused"])
        finally:
            s = open(self.env_file, encoding="utf-8").read()
            open(self.env_file, "w", encoding="utf-8").write(
                s.replace("YIBAN_PAUSE_COOLDOWN_SEC=30\n", ""))

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
        with mock.patch.object(signin.clock, "now", FakeNow.now), \
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
            r = c.post("/api/register", json={"email": "cap1@test.local", "password": "StrongPass1!", "agree": True})
            self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
            # 第二个被拒（已达上限）
            r2 = c.post("/api/register", json={"email": "cap2@test.local", "password": "StrongPass1!", "agree": True})
            self.assertEqual(r2.status_code, 403, r2.get_data(as_text=True))
            self.assertIn("上限", r2.get_json()["error"])
        finally:
            s = open(self.env_file, encoding="utf-8").read()
            open(self.env_file, "w", encoding="utf-8").write(
                s.replace(f"YIBAN_MAX_USERS={limit}\n", ""))

    def test_api_capacity_accounts_limit(self):
        """对抗性审查补：账号配额（YIBAN_MAX_ACCOUNTS，2026-09-08 口径后 = 活跃
        账号数，含 admin 直属裸账号——裸账号同样参与签到占负载）——超限拒绝新增
        （调小上限不删存量，只限制新增）。"""
        with open(self.env_file, "a", encoding="utf-8") as f:
            f.write("YIBAN_MAX_ACCOUNTS=1\n")
        try:
            app = self.webapp.create_app()
            c = app.test_client()
            self._login(c, "admin", ADMIN_PASS)
            token = c.get("/api/me").get_json()["csrf_token"]
            h = {"X-CSRF-Token": token}
            # setUp 已有 user1@test.local 持 1 个 active 账号 → 活跃账号已达 1/1
            # 1) 管理员带新邮箱添加 → 403 且不自动注册
            r = c.post("/api/accounts", json={
                "name": "C1", "phone": "13700137001", "password": "p1",
                "email": "capnew@test.local", "initial_password": "UserPass123!",
            }, headers=h)
            self.assertEqual(r.status_code, 403, r.get_data(as_text=True))
            self.assertIsNone(db.find_user("capnew@test.local"), "配额拒绝不应自动注册")
            # 2) admin 直属裸账号（无 email，owner='admin'）同样占配额 → 403
            r2 = c.post("/api/accounts", json={
                "name": "裸账号", "phone": "13700137002", "password": "p2",
            }, headers=h)
            self.assertEqual(r2.status_code, 403, r2.get_data(as_text=True))
            # 3) user1 再提交：已有账号 → 400（单账号限制，非容量拒绝）
            c2 = app.test_client()
            token2 = self._login(c2, "user1@test.local", USER_PASS)
            r3 = c2.post("/api/my-accounts", json={
                "name": "U2", "phone": "13700137003", "password": "p3",
            }, headers={"X-CSRF-Token": token2})
            self.assertEqual(r3.status_code, 400, r3.get_data(as_text=True))
            self.assertIn("只能提交一个账号", r3.get_json()["error"])
            # 4) 新注册用户提交 → 403
            db.create_user("capuser@test.local", self.webapp.generate_password_hash(USER_PASS))
            c4 = app.test_client()
            token4 = self._login(c4, "capuser@test.local", USER_PASS)
            r4 = c4.post("/api/my-accounts", json={
                "name": "U4", "phone": "13700137004", "password": "p4",
            }, headers={"X-CSRF-Token": token4})
            self.assertEqual(r4.status_code, 403, r4.get_data(as_text=True))
            # 上限调大后裸账号放行（不删人，只限制新增）
            self.webapp.write_env_key(self.env_file, "YIBAN_MAX_ACCOUNTS", "2")
            r5 = c.post("/api/accounts", json={
                "name": "裸账号", "phone": "13700137002", "password": "p2",
            }, headers=h)
            self.assertEqual(r5.status_code, 200, r5.get_data(as_text=True))
            # settings 容量状态（2026-09-08 口径：账号 = 活跃账号数，含裸账号）
            c3 = app.test_client()
            self._login(c3, "admin", ADMIN_PASS)
            data = c3.get("/api/settings").get_json()
            self.assertEqual(data["capacity"]["accounts_max"], 2)
            self.assertEqual(data["capacity"]["accounts"], 2)
            self.assertEqual(data["capacity"]["users"], len(db.load_users()))
        finally:
            s = open(self.env_file, encoding="utf-8").read()
            # 兼清两档写入值（=1 原始追加 / =2 调大后的改写行），防泄漏到后续用例
            s = s.replace("YIBAN_MAX_ACCOUNTS=1\n", "").replace("YIBAN_MAX_ACCOUNTS=2\n", "")
            open(self.env_file, "w", encoding="utf-8").write(s)

    def test_capacity_stats_semantics(self):
        """2026-09-08 口径修订：用户 = 全部未删除注册用户（含空用户，上限 500）；
        账号 = 全部非删除活跃账号（上限 200，含 admin 直属裸账号——同样参与签到
        占负载），空用户不计入账号。"""
        self.assertEqual(self.webapp.DEFAULT_MAX_USERS, 500)
        self.assertEqual(self.webapp.DEFAULT_MAX_ACCOUNTS, 200)
        app = self.webapp.create_app()
        c = app.test_client()
        self._login(c, "admin", ADMIN_PASS)
        cap = c.get("/api/settings").get_json()["capacity"]
        # setUp：admin@test.local + user1@test.local 两个未删除用户；
        # user1 持有 1 个 active 账号（owner=user1@test.local）
        self.assertEqual(cap["users"], 2)
        self.assertEqual(cap["accounts"], 1)
        self.assertEqual(cap["users_max"], 500)
        self.assertEqual(cap["accounts_max"], 200)
        # 空用户计入 users、不计入 accounts
        c2 = app.test_client()
        r = c2.post("/api/register", json={
            "email": "empty@test.local", "password": "StrongPass1!", "agree": True})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        c3 = app.test_client()
        self._login(c3, "admin", ADMIN_PASS)
        cap2 = c3.get("/api/settings").get_json()["capacity"]
        self.assertEqual(cap2["users"], 3)
        self.assertEqual(cap2["accounts"], 1)
        # admin 直属裸账号计入 accounts（2026-09-08：容量约束请求负载，裸账号占配额）
        token = c3.get("/api/me").get_json()["csrf_token"]
        r3 = c3.post("/api/accounts", json={
            "name": "裸账号", "phone": "13700137005", "password": "p5",
        }, headers={"X-CSRF-Token": token})
        self.assertEqual(r3.status_code, 200, r3.get_data(as_text=True))
        cap3 = c3.get("/api/settings").get_json()["capacity"]
        self.assertEqual(cap3["users"], 3)
        self.assertEqual(cap3["accounts"], 2)

    def test_users_at_capacity_semantics_unified(self):
        """容量阈值语义统一：_users_at_capacity 与 _accounts_at_capacity
        同构（"再注册 1 人后 > 上限才拒"，达到上限恰好填满、超过才拒）；
        并与旧内联判定 len(users) >= max 逐值等价（统一语义不改变行为）。"""
        cur = len(db.load_users())
        # 0 = 不限：永不触发
        self.webapp.write_env_key(self.env_file, "YIBAN_MAX_USERS", "0")
        self.assertFalse(self.webapp._users_at_capacity())
        # 上限 = 当前用户数：已满，再注册将超限 → 拒绝
        self.webapp.write_env_key(self.env_file, "YIBAN_MAX_USERS", str(cur))
        self.assertTrue(self.webapp._users_at_capacity())
        # 上限 = 当前用户数 + 1：再注册 1 人恰好到顶（<= 上限）→ 放行
        self.webapp.write_env_key(self.env_file, "YIBAN_MAX_USERS", str(cur + 1))
        self.assertFalse(self.webapp._users_at_capacity())
        # 上限 = 当前用户数 + 2：余位更足 → 放行
        self.webapp.write_env_key(self.env_file, "YIBAN_MAX_USERS", str(cur + 2))
        self.assertFalse(self.webapp._users_at_capacity())
        # 与旧内联判定逐值等价（0 为不限需单独处理）
        for maxv in (cur, cur + 1, cur + 2, 0):
            self.webapp.write_env_key(self.env_file, "YIBAN_MAX_USERS", str(maxv))
            expect = (maxv > 0 and len(db.load_users()) >= maxv)
            self.assertEqual(self.webapp._users_at_capacity(), expect,
                             f"max={maxv} 时语义必须与旧判定一致")

    def test_api_settings_sched_tier_split(self):
        """档位重排后：排序/分布/模式/自选归 B 档（口令可用豁免），周末/窗口/边缘归 A 档。

        旧用例钉的是"sign_order 仅主管理员 + 周日开关人人可改"，两个方向都被
        「影响半径 × 能否造成静默漏签」的判据改判：这里同时钉住改判后的两侧。
        """
        app = self.webapp.create_app()
        db.create_user("admin2@test.local", self.webapp.generate_password_hash(USER_PASS), role="admin")
        c = app.test_client()
        token = self._login(c, "admin2@test.local", USER_PASS)
        h = self._csrf(token)
        cur = c.get("/api/settings", headers=h).get_json()["sign_order"]
        other = "sequence" if cur == "random" else "random"
        # B 档真变更：无口令 403、带当次口令 200（普通管理员即可）
        self.assertEqual(c.post("/api/settings", json={"sign_order": other},
                                headers=h).status_code, 403)
        r = c.post("/api/settings", json={"sign_order": other, "confirm_password": USER_PASS},
                   headers=h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(c.get("/api/settings", headers=h).get_json()["sign_order"], other)
        # A 档（周日开关）：普通管理员即使带口令也 403
        r = c.post("/api/settings", json={"sunday_sign": 1, "confirm_password": USER_PASS},
                   headers=h)
        self.assertEqual(r.status_code, 403, r.get_data(as_text=True))
        # 主管理员可改 A 档，但同样要当次口令
        c2 = app.test_client()
        token2 = self._login(c2, "admin", ADMIN_PASS)
        self.assertEqual(c2.post("/api/settings", json={"sunday_sign": 1},
                                 headers=self._csrf(token2)).status_code, 403)
        r = c2.post("/api/settings", json={"sunday_sign": 1, "confirm_password": ADMIN_PASS},
                    headers=self._csrf(token2))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

    # ---- 安全审查 2026-08：sign_mode 权限 / settings 原子性 / 公告 .env 注入 ----
    def test_announcement_rejects_newline(self):
        """公告含换行必须 400（防 .env 注入新配置行提权），.env 不得出现注入行。"""
        c = self.webapp.create_app().test_client()
        token = self._login(c, "admin", ADMIN_PASS)
        h = self._csrf(token)
        payload = "正常公告\nYIBAN_ADMIN_PASSWORD_HASH=scrypt:fake"
        r = c.put("/api/announcement", json={"text": payload}, headers=h)
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        env = open(self.env_file, encoding="utf-8").read()
        # .env 中原本就有启动时迁移生成的合法 YIBAN_ADMIN_PASSWORD_HASH 行，
        # 注入行会再追加一行 → 校验注入内容不出现且该键仍只有 1 行
        self.assertNotIn("scrypt:fake", env, "注入的哈希值不应落盘")
        self.assertEqual(env.count("YIBAN_ADMIN_PASSWORD_HASH="), 1,
                         "注入不应产生第二个 YIBAN_ADMIN_PASSWORD_HASH 行")
        # 单行公告正常保存（双人发布后落在草稿键，正式键只能由发布动作写）
        r = c.put("/api/announcement", json={"text": "服务器维护中"}, headers=h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        env = open(self.env_file, encoding="utf-8").read()
        self.assertIn("YIBAN_ANNOUNCEMENT_DRAFT=服务器维护中", env)
        self.assertNotIn("YIBAN_ANNOUNCEMENT=服务器维护中", env,
                         "PUT 只写草稿：直接落正式键 = 双人发布被绕过")

    def test_api_settings_atomic_no_partial_write(self):
        """任一字段校验失败时全部不落盘（此前 start/gap 先写、后续字段非法时部分生效）。"""
        c = self.webapp.create_app().test_client()
        token = self._login(c, "admin", ADMIN_PASS)
        h = self._csrf(token)
        r = c.post("/api/settings", json={
            "start_delay_max": 300, "gap_max": 30, "sign_mode": "bogus",
            "confirm_password": ADMIN_PASS,
        }, headers=h)
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        env = open(self.env_file, encoding="utf-8").read()
        for k in ("YIBAN_START_DELAY_MAX", "YIBAN_ACCOUNT_GAP_MAX", "YIBAN_SIGN_MODE"):
            self.assertNotIn(k + "=", env, f"校验失败时 {k} 不应落盘")
        # 合法请求照常写入（sign_mode 用 sequence：该键会持久化进共享 .env，
        # 用默认等价值避免污染后续测试对默认状态的断言）
        r = c.post("/api/settings", json={
            "start_delay_max": 300, "gap_max": 30, "sign_mode": "sequence",
            "confirm_password": ADMIN_PASS,
        }, headers=h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        env = open(self.env_file, encoding="utf-8").read()
        self.assertIn("YIBAN_START_DELAY_MAX=300", env)
        self.assertIn("YIBAN_SIGN_MODE=sequence", env)

    def test_api_settings_sign_mode_gated(self):
        """sign_mode 归 B 档（普通管理员带当次口令可写）；A 档仍一律 403。

        旧用例钉的是"sign_mode 仅主管理员"，理由是"普通管理员可借它间接改排序"——
        排序本身现已下放 B 档，那条理由不再成立；改判后仍要钉住的是：B 档也要当次口令，
        而 A 档（周末/窗口/边缘）连口令带过去也不给普通管理员写。
        """
        app = self.webapp.create_app()
        db.create_user("admin2@test.local", self.webapp.generate_password_hash(USER_PASS), role="admin")
        c = app.test_client()
        token = self._login(c, "admin2@test.local", USER_PASS)
        h = self._csrf(token)
        cur = c.get("/api/settings", headers=h).get_json()["sign_mode"]
        other = "sequence" if cur == "random" else "random"
        self.assertEqual(c.post("/api/settings", json={"sign_mode": other},
                                headers=h).status_code, 403, "B 档真变更无口令即拒")
        r = c.post("/api/settings", json={"sign_mode": other, "confirm_password": USER_PASS},
                   headers=h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        env = open(self.env_file, encoding="utf-8").read()
        self.assertIn(f"YIBAN_SIGN_MODE={other}", env)
        # A 档：普通管理员即使带口令也 403（周日开关）
        r = c.post("/api/settings", json={"sunday_sign": 1, "confirm_password": USER_PASS},
                   headers=h)
        self.assertEqual(r.status_code, 403, r.get_data(as_text=True))

    def test_write_env_key_guards_newline(self):
        """write_env_key 兜底：含换行/回车的键或值直接抛 ValueError，不落盘。"""
        with self.assertRaises(ValueError):
            self.webapp.write_env_key(self.env_file, "K", "a\nb")
        with self.assertRaises(ValueError):
            self.webapp.write_env_key(self.env_file, "K", "a\rb")
        env = open(self.env_file, encoding="utf-8").read()
        self.assertNotIn("K=", env, "被拒的键不应写入 .env")
        self.webapp.write_env_key(self.env_file, "K", "ok")
        env = open(self.env_file, encoding="utf-8").read()
        self.assertIn("K=ok", env)


def hm_EDGE(dt):
    """datetime → 当天分钟浮点（0:00 = 0，含秒）。"""
    return dt.hour * 60 + dt.minute + dt.second / 60.0


def make_accounts(n):
    return [signin.Account(phone=str(13800000000 + i), password="p") for i in range(n)]


def pop_env(keys):
    for k in keys:
        os.environ.pop(k, None)


class EdgeScheduleTest(unittest.TestCase):
    def setUp(self):
        keys = (
            "YIBAN_SIGN_ORDER", "YIBAN_SIGN_DIST", "YIBAN_SIGN_MODE",
            "YIBAN_WINDOW_EDGE_SEC", "YIBAN_WINDOW_EDGE_FRONT_SEC",
            "YIBAN_WINDOW_EDGE_BACK_SEC", "YIBAN_SIGN_START", "YIBAN_SIGN_END",
        )
        pop_env(keys)
        # 用例各自再写入的键随测试结束一并清除：新键（FRONT/BACK）优先级高于
        # 旧键 YIBAN_WINDOW_EDGE_SEC，泄漏会静默改写后续调度测试（schedule_v2
        # 的旧键用例）的有效窗口——2026-08-22 全量跑查明的跨文件污染源
        self.addCleanup(pop_env, keys)

    def test_asymmetric_front_back(self):
        """前 30s + 后 300s：所有计划时刻 ∈ [06:30.5, 07:45]（默认窗口 06:30~07:50）。"""
        os.environ["YIBAN_WINDOW_EDGE_FRONT_SEC"] = "30"
        os.environ["YIBAN_WINDOW_EDGE_BACK_SEC"] = "300"
        sched = signin.build_schedule(
            make_accounts(10), order="sequence", dist="uniform", rng=random.Random(1))
        self.assertEqual(len(sched), 10)
        for t in sched.values():
            m = hm_EDGE(t)
            self.assertGreaterEqual(m, 390.5, "不得早于窗口起点+前裁 0.5 分钟")
            self.assertLessEqual(m, 470 - 5, "不得晚于窗口终点-后裁 5 分钟")

    def test_half_minute_front_edge_precision(self):
        """0.5 分钟（30s）粒度：前裁 0.5 分钟 → 首块 lo=390.5（浮点分钟精确）。"""
        os.environ["YIBAN_WINDOW_EDGE_FRONT_SEC"] = "30"
        os.environ["YIBAN_WINDOW_EDGE_BACK_SEC"] = "0"
        cfg = signin._schedule_config()
        blocks, eff_lo, eff_hi = signin._schedule_blocks(cfg)
        self.assertEqual(eff_lo, 390.5)
        self.assertEqual(eff_hi, 470.0)
        # 首块被部分裁剪：lo=390.5，仍有 4.5 分钟可用（块存在）
        self.assertEqual(blocks[0], (390.5, 395.0))

    def test_legacy_env_symmetric_mapping(self):
        """旧键 YIBAN_WINDOW_EDGE_SEC=120 → front=back=120（升级兼容，行为不变）。"""
        os.environ["YIBAN_WINDOW_EDGE_SEC"] = "120"
        cfg = signin._schedule_config()
        self.assertEqual(cfg["edge_front_sec"], 120)
        self.assertEqual(cfg["edge_back_sec"], 120)

    def test_new_keys_override_legacy(self):
        """新键存在时优先于旧键（前后可不同）。"""
        os.environ["YIBAN_WINDOW_EDGE_SEC"] = "120"
        os.environ["YIBAN_WINDOW_EDGE_FRONT_SEC"] = "30"
        os.environ["YIBAN_WINDOW_EDGE_BACK_SEC"] = "300"
        cfg = signin._schedule_config()
        self.assertEqual(cfg["edge_front_sec"], 30)
        self.assertEqual(cfg["edge_back_sec"], 300)

    def test_front_back_sum_overflows_window_fallback(self):
        """前 5 分 + 后 5 分 超过窗口（06:30~06:40 共 10 分钟）→ 回退默认窗口，不崩溃。"""
        os.environ["YIBAN_SIGN_START"] = "06:30"
        os.environ["YIBAN_SIGN_END"] = "06:40"
        os.environ["YIBAN_WINDOW_EDGE_FRONT_SEC"] = "300"
        os.environ["YIBAN_WINDOW_EDGE_BACK_SEC"] = "300"
        sched = signin.build_schedule(
            make_accounts(3), order="sequence", dist="uniform", rng=random.Random(1))
        self.assertEqual(len(sched), 3)
        for t in sched.values():
            self.assertTrue(391 <= hm_EDGE(t) <= 469, t)

    def test_pref_slot_partially_clipped_block_maps(self):
        """前 2 分钟裁剪：自选 slot 0（首片）仍可映射到块（块 0 lo=392.0），不因裁剪丢块。"""
        os.environ["YIBAN_WINDOW_EDGE_FRONT_SEC"] = "120"
        os.environ["YIBAN_WINDOW_EDGE_BACK_SEC"] = "0"
        cfg = signin._schedule_config()
        slot_to_bi = signin._slot_to_bi(cfg)
        self.assertIn(0, slot_to_bi, "部分裁剪的首片仍应可被自选选中")
        _blocks, eff_lo, _eff_hi = signin._schedule_blocks(cfg)
        self.assertEqual(eff_lo, 392.0)


USER = "pref-user@test.local"


PHONE = "13900001234"


class TimePrefRestoreConsistencyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-timepref-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.accounts_file = os.path.join(cls.tmp, "accounts.json")
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_ACCOUNTS_FILE"] = cls.accounts_file
        os.environ["YIBAN_DB_FILE"] = cls.db_file
        # setdefault（非硬赋值）：该键由 conftest 全局管理，本类只做兜底，绝不覆盖/清除
        os.environ.setdefault("YIBAN_DISABLE_PURGE_LOOP", "1")

    @classmethod
    def tearDownClass(cls):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        # 只清本类自己设置的键。**绝不清 YIBAN_DISABLE_PURGE_LOOP**——它由
        # tests/conftest.py 在进程启动时 setdefault 设成 "1"，是"全量 pytest 反复
        # create_app 时禁止启动 daily-purge 后台线程"的全局前提；一旦在此 pop 掉，
        # 后续任何 create_app 都会真的起线程并把 web.app._purge_loop_started 置 True，
        # 使 test_web_auth_security 的"该开关为 1 时不应启动 daily-purge"用例失败
        # （2026-09-10 实际踩过：全量 1 failed，该用例在单跑时却通过）。
        for k in ("YIBAN_ACCOUNTS_KEY", "YIBAN_ENV_FILE", "YIBAN_ACCOUNTS_FILE",
                  "YIBAN_DB_FILE"):
            os.environ.pop(k, None)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        for suffix in ("", "-wal", "-shm"):
            p = self.db_file + suffix
            if os.path.exists(p):
                os.remove(p)
        db.init_db(self.db_file, env_file=self.env_file, cleanup=False)

    def _seed(self, phone=PHONE, slot=390):
        db.create_user(USER, "hash", created_at="2026-09-10")
        db.add_account({
            "name": "测试账号", "phone": phone, "password": "p1",
            "phone_model": "", "phone_code": "", "owner": USER,
            "status": "active", "reject_reason": "",
        })
        db.set_time_pref(phone, slot, "2026-09-10 10:00:00")
        row = next(a for a in db.load_accounts_raw() if a["phone"] == phone)
        return row["id"]

    def _age_deleted_at(self, days):
        """把用户与账号的 deleted_at 回拨到保留期之外（模拟宽限期已过）。"""
        stale = (_datetime_TPREF.datetime.now() - _datetime_TPREF.timedelta(days=days)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        conn = db.get_conn()
        with db._conn_lock, conn:
            conn.execute("UPDATE accounts SET deleted_at=? WHERE deleted=1", (stale,))
            conn.execute("UPDATE users SET deleted_at=? WHERE deleted=1", (stale,))
            conn.commit()

    # ---- 1. 账号级软删 → 恢复：自选保留（既有行为，防回退） ----
    def test_account_level_soft_delete_keeps_pref(self):
        acc_id = self._seed()
        db.set_account_deleted(acc_id, 1, "2026-09-10 11:00:00", deleted_by=USER)
        db.set_account_deleted(acc_id, 0)
        self.assertIsNotNone(
            db.get_time_pref(PHONE), "账号级软删恢复后自选应保留"
        )

    # ---- 2. 用户级注销 → 恢复：自选保留（本次修复点，原先会丢） ----
    def test_user_cancel_keeps_pref_across_restore(self):
        self._seed()
        self.assertTrue(db.soft_delete_user_with_accounts(USER))
        self.assertIsNotNone(
            db.get_time_pref(PHONE),
            "注销（软删）阶段不应清除自选时间片——与账号级软删口径统一",
        )
        self.assertTrue(db.restore_user(USER))
        pref = db.get_time_pref(PHONE)
        self.assertIsNotNone(pref, "注销后恢复应带回自选时间片（可逆操作完整可逆）")
        self.assertEqual(pref["slot_min"], 390)

    # ---- 3. 过期物理清除仍连带清 prefs（不留孤儿） ----
    def test_expired_purge_still_clears_pref(self):
        self._seed()
        db.soft_delete_user_with_accounts(USER)
        self._age_deleted_at(db.SOFT_DELETE_RETENTION_DAYS + 1)
        db._purge_expired_deleted(db.get_conn())
        self.assertIsNone(
            db.get_time_pref(PHONE),
            "物理清除后必须连带清理自选，否则会留下无主 pref",
        )

    # ---- 4. 管理员硬清除已注销用户仍连带清 prefs ----
    def test_hard_purge_still_clears_pref(self):
        self._seed()
        db.soft_delete_user_with_accounts(USER)
        purged = db.purge_deleted_users_hard([USER])
        self.assertEqual(purged, [USER])
        self.assertIsNone(
            db.get_time_pref(PHONE), "管理员硬清除后自选应连带清理"
        )

    # ---- 5. 拥挤度统计不计入软删账号的自选（既有语义不得回退） ----
    def test_time_pref_stats_ignores_soft_deleted(self):
        self._seed()
        before = [s for s in db.time_pref_stats() if s["slot_min"] == 390]
        self.assertEqual(before, [{"slot_min": 390, "count": 1}])
        db.soft_delete_user_with_accounts(USER)
        after = [s for s in db.time_pref_stats() if s["slot_min"] == 390]
        self.assertEqual(
            after, [],
            "软删账号的残留 pref 不得计入拥挤度（否则占位会虚高）",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
