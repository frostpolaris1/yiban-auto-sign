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
from datetime import datetime, timedelta  # 弹性冷却测试构造审计时间戳/窗口用

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
        with mock.patch.object(self.webapp, "datetime", FakeDT):
            r = c.put("/api/my-time-pref", json={"slot_min": 0}, headers=h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertIn("今日生效", r.get_json()["msg"])
        # 场景 2：标记存在（snapshot_at=06:00:00，cron 已快照）→ 改选在快照后 → 明日生效
        with open(snap, "w", encoding="utf-8") as f:
            json.dump({"snapshot_at": "06:00:00"}, f)
        with mock.patch.object(self.webapp, "datetime", FakeDT):
            r2 = c.put("/api/my-time-pref", json={"slot_min": 5}, headers=h)
        self.assertEqual(r2.status_code, 200, r2.get_data(as_text=True))
        self.assertIn("明日生效", r2.get_json()["msg"])
        # 场景 3：标记时间在未来（时钟偏移/写坏，H1 对抗性审查）→ 视为无效回退兜底（06:31）
        with open(snap, "w", encoding="utf-8") as f:
            json.dump({"snapshot_at": "07:00:00"}, f)
        with mock.patch.object(self.webapp, "datetime", FakeDT):
            r3 = c.put("/api/my-time-pref", json={"slot_min": 10}, headers=h)
        self.assertEqual(r3.status_code, 200, r3.get_data(as_text=True))
        self.assertIn("今日生效", r3.get_json()["msg"])  # 回退兜底 06:31 → now(06:30) 之前

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
        """对抗（H2）：整表替换（TUI）后，被移除账号的 pref 一并清理（防孤儿虚高拥挤度）。"""
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
        """对抗性审查补：账号配额（YIBAN_MAX_ACCOUNTS，2026-08-31 口径修订后 = 活跃
        注册用户持有者数）——新增持有者（管理员带 email 添加 / 新用户提交）受限；
        admin 直属裸账号（owner='admin'）按口径不占配额。"""
        with open(self.env_file, "a", encoding="utf-8") as f:
            f.write("YIBAN_MAX_ACCOUNTS=1\n")
        try:
            app = self.webapp.create_app()
            c = app.test_client()
            self._login(c, "admin", ADMIN_PASS)
            token = c.get("/api/me").get_json()["csrf_token"]
            h = {"X-CSRF-Token": token}
            # setUp 已有 user1@test.local 持 1 个 active 账号 → 活跃持有者已达 1/1
            # 1) 管理员带新邮箱添加（新增持有者）→ 403 且不自动注册
            r = c.post("/api/accounts", json={
                "name": "C1", "phone": "13700137001", "password": "p1",
                "email": "capnew@test.local", "initial_password": "UserPass123!",
            }, headers=h)
            self.assertEqual(r.status_code, 403, r.get_data(as_text=True))
            self.assertIsNone(db.find_user("capnew@test.local"), "配额拒绝不应自动注册")
            # 2) admin 直属裸账号（无 email，owner='admin'）按口径不占配额 → 可添加
            r2 = c.post("/api/accounts", json={
                "name": "裸账号", "phone": "13700137002", "password": "p2",
            }, headers=h)
            self.assertEqual(r2.status_code, 200, r2.get_data(as_text=True))
            # 3) user1 再提交：已有账号 → 400（单账号限制，非容量拒绝）
            c2 = app.test_client()
            token2 = self._login(c2, "user1@test.local", USER_PASS)
            r3 = c2.post("/api/my-accounts", json={
                "name": "U2", "phone": "13700137003", "password": "p3",
            }, headers={"X-CSRF-Token": token2})
            self.assertEqual(r3.status_code, 400, r3.get_data(as_text=True))
            self.assertIn("只能提交一个账号", r3.get_json()["error"])
            # 4) 新注册用户提交（新增持有者）→ 403
            db.create_user("capuser@test.local", self.webapp.generate_password_hash(USER_PASS))
            c4 = app.test_client()
            token4 = self._login(c4, "capuser@test.local", USER_PASS)
            r4 = c4.post("/api/my-accounts", json={
                "name": "U4", "phone": "13700137004", "password": "p4",
            }, headers={"X-CSRF-Token": token4})
            self.assertEqual(r4.status_code, 403, r4.get_data(as_text=True))
            # settings 容量状态（新口径：账号 = 活跃持有者，admin 裸账号不计入）
            c3 = app.test_client()
            self._login(c3, "admin", ADMIN_PASS)
            data = c3.get("/api/settings").get_json()
            self.assertEqual(data["capacity"]["accounts_max"], 1)
            self.assertEqual(data["capacity"]["accounts"], 1)
            self.assertEqual(data["capacity"]["users"], len(db.load_users()))
        finally:
            s = open(self.env_file, encoding="utf-8").read()
            open(self.env_file, "w", encoding="utf-8").write(
                s.replace("YIBAN_MAX_ACCOUNTS=1\n", ""))

    def test_capacity_stats_semantics(self):
        """2026-08-31 口径修订：用户 = 全部未删除注册用户（含空用户，上限 500）；
        账号 = 至少持有 1 个非删除账号的活跃注册用户（上限 200，排除空用户与
        admin 直属裸账号）。"""
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
        # admin 直属裸账号不计入 accounts（owner='admin' 非注册用户）
        token = c3.get("/api/me").get_json()["csrf_token"]
        r3 = c3.post("/api/accounts", json={
            "name": "裸账号", "phone": "13700137005", "password": "p5",
        }, headers={"X-CSRF-Token": token})
        self.assertEqual(r3.status_code, 200, r3.get_data(as_text=True))
        cap3 = c3.get("/api/settings").get_json()["capacity"]
        self.assertEqual(cap3["users"], 3)
        self.assertEqual(cap3["accounts"], 1)

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
        # 单行公告正常保存
        r = c.put("/api/announcement", json={"text": "服务器维护中"}, headers=h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        env = open(self.env_file, encoding="utf-8").read()
        self.assertIn("YIBAN_ANNOUNCEMENT=服务器维护中", env)

    def test_api_settings_atomic_no_partial_write(self):
        """任一字段校验失败时全部不落盘（此前 start/gap 先写、后续字段非法时部分生效）。"""
        c = self.webapp.create_app().test_client()
        token = self._login(c, "admin", ADMIN_PASS)
        h = self._csrf(token)
        r = c.post("/api/settings", json={
            "start_delay_max": 300, "gap_max": 30, "sign_mode": "bogus",
        }, headers=h)
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        env = open(self.env_file, encoding="utf-8").read()
        for k in ("YIBAN_START_DELAY_MAX", "YIBAN_ACCOUNT_GAP_MAX", "YIBAN_SIGN_MODE"):
            self.assertNotIn(k + "=", env, f"校验失败时 {k} 不应落盘")
        # 合法请求照常写入（sign_mode 用 sequence：该键会持久化进共享 .env，
        # 用默认等价值避免污染后续测试对默认状态的断言）
        r = c.post("/api/settings", json={
            "start_delay_max": 300, "gap_max": 30, "sign_mode": "sequence",
        }, headers=h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        env = open(self.env_file, encoding="utf-8").read()
        self.assertIn("YIBAN_START_DELAY_MAX=300", env)
        self.assertIn("YIBAN_SIGN_MODE=sequence", env)

    def test_api_settings_sign_mode_master_only(self):
        """遗留 sign_mode 字段同样仅主管理员可写（防普通管理员借其改调度排序）。"""
        app = self.webapp.create_app()
        db.create_user("admin2@test.local", self.webapp.generate_password_hash(USER_PASS), role="admin")
        c = app.test_client()
        token = self._login(c, "admin2@test.local", USER_PASS)
        h = self._csrf(token)
        r = c.post("/api/settings", json={"sign_mode": "random"}, headers=h)
        self.assertEqual(r.status_code, 403, r.get_data(as_text=True))
        # 低风险字段（周日）仍可改
        r = c.post("/api/settings", json={"sunday_sign": 1}, headers=h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        # 主管理员可写 sign_mode
        c2 = app.test_client()
        token2 = self._login(c2, "admin", ADMIN_PASS)
        r = c2.post("/api/settings", json={"sign_mode": "random"}, headers=self._csrf(token2))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        env = open(self.env_file, encoding="utf-8").read()
        self.assertIn("YIBAN_SIGN_MODE=random", env)

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
