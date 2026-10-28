# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""签到事件表 `sign_events`：写入、按天聚合与前端统计口径。

`sign_events` 是 WebUI 统计与可视化表格的共同数据源；本文件并两处断言：表的写入与聚合
口径，以及前端可视化表格取数时对同一批事件的解读（列集合与计数），避免两边各写一套。

功能：`sign_events` 表与聚合统计的行为回归。
归属：`yiban/store` 数据层 + `web/` 统计展示的交叉测试。
复用：`BASE` / `TEST_KEY` 与临时库装配助手。
通信：写临时 SQLite 后经聚合函数读回；由 pytest 收集 `unittest.TestCase`。

标签：C · 存储：迁移与库完整性
覆盖：`sign_events` 的建表迁移（含删掉旧统计表）、写入函数在表缺失时降级不抛、
过期清理、按手机/按时间的读回形状、v6 字段与批量写入、WebUI 统计的计数口径、
`hash_ip` 加盐稳定性、可视化表格对同一批事件的解读，以及
`DashboardTerminalStatsTest`（看板 `accounts_stats` 按终态载体取数，工单
yiban-auto-sign-v5ty）。
对应实现：`yiban/adapters/persistence/` 的 sign_events 域（写入/聚合）与 `web/` 侧统计与可视化取数、
`hash_ip` 的盐取自追踪盐。
关键断言：**计数按 distinct 手机而非行数**是这里最容易改错的一格——同一账号一天内多次
事件会把统计抬高，故 `test_stats_count_distinct_phone_not_rows` 与
`test_stats_same_phone_in_both_status_buckets` 必须成对（后者钉"同机跨两桶各计一次"，
防止改成全局去重把成功/失败混成一个数）。`test_write_functions_degrade_when_table_missing`
钉的是**降级不抛**：埋点失败不该掀掉签到主流程，别把它误读成"表可以没有"。
`test_upgrade_from_v13_drops_legacy_stats_keeps_sign_events` 只覆盖新代码打开 v13 旧库；
反方向见 `tests/test_migration_compat.py`。
依赖：临时库 + 临时 `.env`（盐），无网络、无 skip；`hash_ip` 用例对盐值敏感，
换盐须同步改期望值。
"""
import contextlib
import datetime
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest

from yiban.core import clock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


TEST_KEY = "a" * 64


AUDIT_KEY = "b" * 64


TRACK_SALT = "c" * 64


class VisualTablesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-visual-tables-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(
                f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
                f"YIBAN_AUDIT_KEY={AUDIT_KEY}\n"
                f"YIBAN_TRACK_SALT={TRACK_SALT}\n"
            )
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.accounts_file = os.path.join(cls.tmp, "accounts.json")
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
        os.environ["YIBAN_AUDIT_KEY"] = AUDIT_KEY
        os.environ["YIBAN_TRACK_SALT"] = TRACK_SALT
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_ACCOUNTS_FILE"] = cls.accounts_file
        os.environ["YIBAN_DB_FILE"] = cls.db_file
        global db
        import db

    @classmethod
    def tearDownClass(cls):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        os.environ.pop("YIBAN_ACCOUNTS_KEY", None)
        os.environ.pop("YIBAN_AUDIT_KEY", None)
        os.environ.pop("YIBAN_TRACK_SALT", None)
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
        db.init_db(self.db_file, env_file=self.env_file)

    def _reset_conn(self):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None

    def test_migration_creates_sign_events_and_drops_legacy_stats(self):
        conn = db.init_db(self.db_file)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0],
                         db._MIGRATIONS[-1][0])
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='sign_events'"
        ).fetchone()
        self.assertIsNotNone(row, "表 sign_events 应存在")
        for index in ("idx_sign_events_ts", "idx_sign_events_phone"):
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name=?", (index,)
            ).fetchone()
            self.assertIsNotNone(row, f"索引 {index} 应存在")
        # v14：从未接线的统计表必须已被删除。全新库的执行序是
        # migrate_v4 建表 → migrate_v6 补列 → migrate_v14 删表（已发布迁移不可改）。
        for table in ("page_visits", "server_metrics"):
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            self.assertIsNone(row, f"表 {table} 应已被 v14 删除")

    def test_write_functions_succeed(self):
        db.add_sign_event("2026-08-16 06:30:00", "13800138000", "success", "ok", "signin", 1)
        conn = db.get_conn()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM sign_events").fetchone()[0], 1)

    def test_write_functions_degrade_when_table_missing(self):
        conn = db.get_conn()
        conn.execute("DROP TABLE sign_events")
        conn.commit()
        # 不应抛异常
        db.add_sign_event("2026-08-16 06:30:00", "13800138000", "success")

    def test_cleanup_removes_expired(self):
        old = (clock.now() - datetime.timedelta(days=400)).strftime("%Y-%m-%d %H:%M:%S")
        recent = clock.now().strftime("%Y-%m-%d %H:%M:%S")
        db.add_sign_event(old, "13800138000", "success")
        db.add_sign_event(recent, "13900139000", "success")
        db._event_cleanup(db.get_conn())
        conn = db.get_conn()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM sign_events").fetchone()[0], 1)

    def test_read_functions_return_expected_shape(self):
        # 时间戳用相对时间（此前硬编码 2026-08-16 会随真实时钟过期：
        # sign_event_stats 按 now 过滤 days 窗口，隔天跑即空——2026-08-17 发现）
        now = clock.now().strftime("%Y-%m-%d %H:%M:%S")
        earlier = (clock.now() - datetime.timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
        db.add_sign_event(now, "13800138000", "success", "ok", "signin", 1)
        db.add_sign_event(earlier, "13900139000", "failed", "bad", "signin", 1)
        sign_stats = db.sign_event_stats(days=30)
        self.assertTrue(sign_stats)
        self.assertIn("day", sign_stats[0])
        self.assertIn("status", sign_stats[0])

    def test_upgrade_from_v13_drops_legacy_stats_keeps_sign_events(self):
        """存量库路径：v13 库（两张遗留表在位）升到 v14 后表被删、签到事件保留。

        与"全新库建了又删"不同，这是真实存量库的升级路径——迁移只增不改，
        靠 v14 的 DROP 收口。
        """
        conn = db.get_conn()
        conn.execute("CREATE TABLE page_visits (id INTEGER PRIMARY KEY, ts TEXT)")
        conn.execute("CREATE TABLE server_metrics (ts TEXT)")
        conn.execute("INSERT INTO page_visits (ts) VALUES ('2026-01-01 00:00:00')")
        conn.execute("PRAGMA user_version = 13")
        conn.commit()
        db.add_sign_event("2026-08-16 06:30:00", "13800138000", "success")

        self._reset_conn()
        conn = db.init_db(self.db_file, env_file=self.env_file)

        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0],
                         db._MIGRATIONS[-1][0])
        names = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn("page_visits", names, "v14 应删除 page_visits")
        self.assertNotIn("server_metrics", names, "v14 应删除 server_metrics")
        self.assertIn("sign_events", names, "v14 不得误删 sign_events")
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM sign_events").fetchone()[0], 1,
            "升级不得丢签到事件",
        )

    def test_hash_ip_uses_salt_and_is_stable(self):
        h1 = db.hash_ip("1.2.3.4")
        h2 = db.hash_ip("1.2.3.4")
        h3 = db.hash_ip("5.6.7.8")
        self.assertEqual(h1, h2)
        self.assertNotEqual(h1, h3)
        self.assertEqual(len(h1), 64)


def _recent(days=0, hours=0, minutes=0):
    """距今给定偏移的 ts 字面量。

    查询函数（sign_events_by_phone / sign_events_since）按 days 窗口裁剪，写死日期
    会在窗口滑过该日期后假红。
    """
    delta = datetime.timedelta(days=days, hours=hours, minutes=minutes)
    return (clock.now() - delta).strftime("%Y-%m-%d %H:%M:%S")


class WebuiStatsDbTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-webui-stats-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.accounts_file = os.path.join(cls.tmp, "accounts.json")
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_ACCOUNTS_FILE"] = cls.accounts_file
        os.environ["YIBAN_DB_FILE"] = cls.db_file
        global db
        import db

    @classmethod
    def tearDownClass(cls):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        os.environ.pop("YIBAN_ACCOUNTS_KEY", None)
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
        db.init_db(self.db_file, env_file=self.env_file)

    def test_migration_v6_schema(self):
        conn = db.init_db(self.db_file)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0],
                         db._MIGRATIONS[-1][0])
        sign_cols = {r["name"] for r in conn.execute("PRAGMA table_info(sign_events)").fetchall()}
        self.assertIn("account_id", sign_cols)
        self.assertIn("dur_sec", sign_cols)
        self.assertIn("finished_at", sign_cols)
        for index in ("idx_sign_events_phone_ts", "idx_sign_events_account_ts"):
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name=?", (index,)
            ).fetchone()
            self.assertIsNotNone(row, f"索引 {index} 应存在")

    def test_add_sign_event_with_new_fields(self):
        db.add_sign_event("2026-08-16 06:30:00", "13800138000", "success",
                          account_id=1, dur_sec=1.5, finished_at="2026-08-16 06:30:02")
        conn = db.get_conn()
        row = conn.execute("SELECT * FROM sign_events").fetchone()
        self.assertEqual(row["account_id"], 1)
        self.assertEqual(row["dur_sec"], 1.5)
        self.assertEqual(row["finished_at"], "2026-08-16 06:30:02")

    def test_batch_write_functions(self):
        db.add_sign_events_batch([
            {"ts": "2026-08-16 06:30:00", "phone": "13800138000", "status": "success",
             "account_id": 1, "dur_sec": 1.0, "finished_at": "2026-08-16 06:30:01"},
            {"ts": "2026-08-16 06:31:00", "phone": "13900139000", "status": "failed"},
        ])
        conn = db.get_conn()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM sign_events").fetchone()[0], 2)

    def test_sign_events_by_phone(self):
        db.add_sign_event(_recent(hours=2), "13800138000", "success")
        db.add_sign_event(_recent(hours=1), "13900139000", "failed")
        rows = db.sign_events_by_phone("13800138000", days=30)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["phone"], "13800138000")

    def test_sign_events_since(self):
        db.add_sign_event("2026-08-16 06:30:00", "13800138000", "success")
        rows = db.sign_events_since("2026-08-16 06:00:00", limit=10)
        self.assertEqual(len(rows), 1)
        rows2 = db.sign_events_since("2026-08-16 06:30:01", limit=10)
        self.assertEqual(len(rows2), 0)

    # ---- 聚合口径：按账号去重，同时保留原始行数 ----
    def _stat_for(self, status):
        """取今日该状态的那条聚合行（找不到返回 None）。"""
        today = clock.today()
        for row in db.sign_event_stats(days=30):
            if row["day"] == today and row["status"] == status:
                return row
        return None

    def test_stats_count_distinct_phone_not_rows(self):
        # 同一账号当日可有多行，但那是 **attempt 递进的真重试**（写入端对同一
        # (账号, 日, status, attempt) 只留一行，工单 f7or）。cnt 按账号计一次，
        # row_cnt 保留真实行数（尝试次数）。
        db.add_sign_event(_recent(minutes=2), "13800138000", "failed", attempt=1)
        db.add_sign_event(_recent(minutes=1), "13800138000", "failed", attempt=2)
        row = self._stat_for("failed")
        self.assertIsNotNone(row)
        self.assertEqual(row["cnt"], 1, "同一账号同一状态当日只计一次")
        self.assertEqual(row["row_cnt"], 2, "原始行数（尝试次数）不得丢失")

    def test_stats_count_distinct_across_phones(self):
        db.add_sign_event(_recent(minutes=2), "13800138000", "failed")
        db.add_sign_event(_recent(minutes=1), "13900139000", "failed")
        row = self._stat_for("failed")
        self.assertEqual(row["cnt"], 2)
        self.assertEqual(row["row_cnt"], 2)

    def test_stats_same_phone_in_both_status_buckets(self):
        # 语义边界：各状态桶分别去重 ⇒ 先失败后成功的账号同时出现在两个桶，
        # 各桶相加当"账号总数"仍会偏大（账号终值口径**不**由事件表出，见
        # DashboardTerminalStatsTest——消费方禁止对 cnt 跨维直加）。
        db.add_sign_event(_recent(minutes=3), "13800138000", "failed")
        db.add_sign_event(_recent(minutes=2), "13800138000", "success")
        failed = self._stat_for("failed")
        success = self._stat_for("success")
        self.assertEqual(failed["cnt"], 1)
        self.assertEqual(success["cnt"], 1)
        self.assertEqual(failed["cnt"] + success["cnt"], 2,
                         "同一账号在两个桶各计一次（分桶去重的必然结果）")

    def test_accountless_sign_rows_cannot_pollute_account_stats(self):
        """无账号的 sign 行在写入口就被拒——负向不变式从"读者守"变成"写不进"。

        旧形状（工单 81xt／MF-55）：兜底收尾把「已由他人负责」收成一条 `phone=''`
        的摘要行写进 `sign_events`，每个读者都得记得过滤，否则日志页凭空多出
        `pending` 桶。摘要行现在住 `run_events`（观测表），本表在写入口拒绝空账号
        （`_accountless_sign_row`）；`sign_event_stats` 保留 `phone <> ''` 只作
        历史行防御（旧库的摘要行在 180 天保留期内仍可能被读到）。
        """
        db.add_sign_event(_recent(minutes=2), "13800138000", "success", stage="sign")
        with self.assertLogs("yiban.store.events", level="WARNING"):
            db.add_sign_event(_recent(minutes=1), "", "pending",
                              "本执行体未领取：任务已由其他执行体领取，共 6 个账号",
                              stage="sign")
        # ① 写入口拒绝：表里只剩账号级事实
        raw = db.get_conn().execute(
            "SELECT phone, status FROM sign_events ORDER BY id").fetchall()
        self.assertEqual([(r["phone"], r["status"]) for r in raw],
                         [("13800138000", "success")],
                         "无账号的 sign 行不得落库（哨兵不得复活）")
        # ② 聚合不需要哨兵过滤也看不到 pending 桶
        stats = {(r["day"], r["status"]): r
                 for r in db.sign_event_stats(days=30, stage="sign")}
        self.assertEqual(set(s for (_d, s) in stats), {"success"})


class DashboardTerminalStatsTest(unittest.TestCase):
    """看板账号统计**按终态取数**（工单 yiban-auto-sign-v5ty，用户 2026-10-18 裁定）。

    覆盖：`GET /api/admin/sign-events` 的 `accounts_stats`。三个字段（`total` /
        `by_status` / `by_day`）**全部**从终态载体（按日 `sign-state-<day>.json`）
        推出，不再对 `sign_events` 取「每账号当日最后一条事件」模拟终态。
    对应实现：`web/services/logs.py::accounts_terminal_summary`（读取复用日历页/账号表
        同一份 `load_sign_state_ex`）+ `web/routes/data.py::api_admin_sign_events`。
    关键断言：
        ① 先失败后成功 ⇒ 当日终态是成功（终态载体说话，事件表不得覆盖）；
        ② 成功之后又写一条 `pending` 观测（2026-10-16 现网污染形状：收尾洪水每账号写
           多条 `pending`，把「最后一条事件」刷成待签）⇒ 终态**仍是成功**；
        两条在「来源换回最后一条事件」的突变下都必须红：②由 `by_day` 桶判定
        （`pending` 顶掉了 `success`），①由 `by_status` 判定（事件表口径下 `failed`
        桶留档，终态口径下不留）。
    依赖：Flask test client + 临时库/临时状态目录；不联网、无需 node。
    """

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-dash-terminal-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(
                f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
                "YIBAN_ADMIN_USER=admin\nYIBAN_ADMIN_PASSWORD=TestPass1234!\n"
            )
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.accounts_file = os.path.join(cls.tmp, "accounts.json")
        cls._old_env = {k: os.environ.get(k) for k in
                        ("YIBAN_ACCOUNTS_KEY", "YIBAN_ENV_FILE", "YIBAN_DB_FILE",
                         "YIBAN_STATE_DIR", "YIBAN_ACCOUNTS_FILE", "YIBAN_LOG_FILE")}
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_DB_FILE"] = cls.db_file
        os.environ["YIBAN_STATE_DIR"] = cls.tmp
        os.environ["YIBAN_ACCOUNTS_FILE"] = cls.accounts_file
        os.environ["YIBAN_LOG_FILE"] = os.path.join(cls.tmp, "sign.log")
        global db
        import db
        spec = importlib.util.spec_from_file_location(
            "webapp_dash_terminal", os.path.join(BASE, "web", "app.py"))
        cls.webapp = importlib.util.module_from_spec(spec)
        sys.modules["webapp_dash_terminal"] = cls.webapp
        with contextlib.suppress(Exception):
            spec.loader.exec_module(cls.webapp)

    @classmethod
    def tearDownClass(cls):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        for k, v in cls._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
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
        with open(self.accounts_file, "w", encoding="utf-8") as f:
            f.write("[]")
        db.init_db(self.db_file, migrate_from=self.accounts_file, env_file=self.env_file)
        self.client = self.webapp.create_app().test_client()
        r = self.client.post("/api/login", json={"username": "admin",
                                                 "password": "TestPass1234!"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

    # ---- 夹具 ----
    def _day(self, days_ago):
        """窗口内的日期串（本地正午口径：跨时区双跑时不会漂到相邻日）。"""
        return ((clock.now() - datetime.timedelta(days=days_ago))
                .replace(hour=12, minute=0, second=0, microsecond=0)
                .strftime("%Y-%m-%d"))

    def _write_state(self, day, entries):
        """写该日终态载体（`sign-state-<day>.json`）。"""
        path = os.path.join(self.tmp, f"sign-state-{day}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(entries, f)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))

    def _entry(self, status, time_str="12:30:00"):
        return {"status": status, "message": "", "time": time_str, "task": "default"}

    def _fetch(self):
        r = self.client.get("/api/admin/sign-events?days=30&stage=sign")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return r.get_json()

    @staticmethod
    def _buckets(accounts_stats):
        return {(row["day"], row["status"]): row["accounts"]
                for row in accounts_stats["by_day"]}

    # ---- ① 先失败后成功 ⇒ 终态是成功 ----
    def test_failed_then_success_terminal_is_success(self):
        day = self._day(1)
        phone = "13800138000"
        db.add_sign_event(f"{day} 12:00:00", phone, "failed", stage="sign", attempt=1)
        db.add_sign_event(f"{day} 12:30:00", phone, "success", stage="sign")
        self._write_state(day, {phone: self._entry("success")})
        stats = self._fetch()["accounts_stats"]
        buckets = self._buckets(stats)
        self.assertEqual(buckets.get((day, "success")), 1, "先败后成 ⇒ 当日终态是成功")
        self.assertEqual(buckets.get((day, "failed"), 0), 0, "失败不是终态，不进 by_day")
        self.assertEqual(stats["total"], 1, "窗口内出现过的账号并集")
        self.assertEqual(stats["by_status"], {"success": 1})
        self.assertIn("days_missing", stats,
                      "缺终态载体的日期必须显式可见（AGENTS §30 跳过不得静默算 0）")
        self.assertNotIn(day, stats["days_missing"], "有终态载体的日期不得报缺")

    # ---- ② 成功之后又写一条观测 ⇒ 终态仍是成功（10-16 污染形状） ----
    def test_observation_after_success_keeps_success(self):
        day = self._day(1)
        phone = "13800138000"
        db.add_sign_event(f"{day} 12:00:00", phone, "success", stage="sign")
        # 10-16 现网污染：收尾洪水每账号写多条 pending 观测，attempt 不变（0）
        db.add_sign_event(f"{day} 12:05:00", phone, "pending", stage="sign", attempt=0)
        self._write_state(day, {phone: self._entry("success")})
        data = self._fetch()
        stats = data["accounts_stats"]
        buckets = self._buckets(stats)
        self.assertEqual(buckets.get((day, "success")), 1,
                         "成功之后的 pending 观测不得改写终态（10-16 把 311 个账号刷成待签）")
        self.assertEqual(buckets.get((day, "pending"), 0), 0, "观测不是终态，不进 by_day")
        self.assertEqual(stats["total"], 1)
        self.assertEqual(stats["by_status"], {"success": 1})
        # 过程数据仍留在日志页（事件流与按天事件统计不得因本次改动被过滤）
        daily = {(row["day"], row["status"]) for row in data["daily_stats"]}
        self.assertIn((day, "pending"), daily, "过程事件仍须在 daily_stats 可见")
        self.assertIn((day, "success"), daily, "过程事件仍须在 daily_stats 可见")


# ---------------------------------------------------------------------------
# f7or：sign_events 不得记录「观测」
# ---------------------------------------------------------------------------
#: 门的历史豁免登记：业务日 → 理由。只登记**已判明**的日子，不做"整段历史放行"。
#: 2026-08-31：`failed`/`retrying` 在 attempt 1..4 上各 3 行，是多执行体各记一次
#: **同一次真实尝试**（变迁重复）。删行会伪造尝试史，故登记豁免而不是清洗。
GATE_EXEMPT_DAYS = {
    "2026-08-31": "多执行体各记一次同一次尝试（attempt>0）；删行会伪造尝试史",
}


def sign_events_observation_gate(conn):
    """门：同 (账号, 业务日, status, attempt) 的 sign 事件行必须 ≤1。

    两类历史数据必须分开（工单 f7or 验收 2）：
    - **观测重复**：`attempt == 0`——同一个事实被每轮重扫重发（pending 洪水、
      user_cancelled 每轮一条），**没有尝试号递进**。一律红。
    - **变迁重复**：`attempt > 0`——行带着真实尝试号，是多执行体各记一次同一次
      尝试。旧数据里确实存在（2026-08-31），删除会伪造尝试史，故按**日期显式
      登记**豁免；登记日之外再出现就是回归（写入端已做幂等），同样红。

    返回 `(status, groups)`：status ∈ {"ok", "violations", "cannot-determine"}；
    groups 每项是 dict（含 `class` 与 `reason`）。`cannot-determine` 对应
    "一行可判定的数据都没有"——扫空不得当绿（AGENTS §30）。
    """
    rows = conn.execute(
        "SELECT phone, substr(ts, 1, 10) AS day, status, attempt, "
        "COUNT(*) AS n, MIN(ts) AS lo, MAX(ts) AS hi "
        "FROM sign_events WHERE stage <> 'probe' AND phone <> '' "
        "GROUP BY phone, day, status, attempt HAVING COUNT(*) > 1 "
        "ORDER BY day, phone, status, attempt").fetchall()
    scanned = conn.execute(
        "SELECT 1 FROM sign_events WHERE stage <> 'probe' AND phone <> '' LIMIT 1"
    ).fetchone()
    if scanned is None:
        return "cannot-determine", []
    violations, exemptions = [], []
    for r in rows:
        group = dict(r)
        if int(group["attempt"] or 0) > 0:
            group["class"] = "transition"
            reason = GATE_EXEMPT_DAYS.get(group["day"])
            if reason:
                group["reason"] = reason
                exemptions.append(group)
            else:
                group["reason"] = "未登记的变迁重复（attempt>0）"
                violations.append(group)
        else:
            group["class"] = "observation"
            group["reason"] = "attempt=0：同一事实被重扫重发"
            violations.append(group)
    return ("violations" if violations else "ok"), violations + exemptions


class ObservationWriteGateTest(unittest.TestCase):
    """f7or：写入端幂等（重复在结构上不可能）＋ 门的两类判别与突变。

    每条断言钉的是**不变式**：任何时刻同一 (账号, 业务日, status, attempt) 的
    sign 事件行 ≤1；观测行没有尝试号（attempt=0）时不得因重扫而增行；探针行
    （stage='probe'）是另一条观测通道，不参与本不变量。
    """

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-observation-gate-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_DB_FILE"] = cls.db_file
        global db
        import db

    @classmethod
    def tearDownClass(cls):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        os.environ.pop("YIBAN_ACCOUNTS_KEY", None)
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
        db.init_db(self.db_file, env_file=self.env_file)
        self.conn = db.get_conn()

    def _count(self):
        return self.conn.execute("SELECT COUNT(*) FROM sign_events").fetchone()[0]

    # ---- 写入端：重复在结构上不可能 ----
    def test_same_key_written_twice_keeps_one_row(self):
        db.add_sign_event("2026-09-01 06:30:00", "13800138000", "failed",
                          stage="sign", attempt=2)
        db.add_sign_event("2026-09-01 06:30:01", "13800138000", "failed",
                          stage="sign", attempt=2)
        self.assertEqual(self._count(), 1)

    def test_same_key_inside_one_batch_keeps_one_row(self):
        db.add_sign_events_batch([
            {"ts": "2026-09-01 06:30:00", "phone": "13800138000",
             "status": "user_cancelled", "stage": "sign", "attempt": 0},
            {"ts": "2026-09-01 06:30:05", "phone": "13800138000",
             "status": "user_cancelled", "stage": "sign", "attempt": 0},
        ])
        self.assertEqual(self._count(), 1)

    def test_same_status_on_different_attempt_is_kept(self):
        """真实重试不是重复：attempt 递进必须各留一行（尝试史不得被吞）。"""
        for attempt in (1, 2, 3):
            db.add_sign_event(f"2026-09-01 06:3{attempt}:00", "13800138000", "failed",
                              stage="sign", attempt=attempt)
        self.assertEqual(self._count(), 3)

    def test_different_day_is_not_a_duplicate(self):
        db.add_sign_event("2026-09-01 06:30:00", "13800138000", "failed",
                          stage="sign", attempt=1)
        db.add_sign_event("2026-09-02 06:30:00", "13800138000", "failed",
                          stage="sign", attempt=1)
        self.assertEqual(self._count(), 2)

    def test_probe_rows_keep_their_cadence(self):
        """探针是另一条观测通道：同号同日同状态的多拍探针不得被合并。"""
        for hour in (1, 2, 3):
            db.add_sign_event(f"2026-09-01 0{hour}:00:00", "13800138000", "success",
                              stage="probe")
        self.assertEqual(self._count(), 3,
                         "stage='probe' 的多拍观测不得被 sign 口径的去重吞掉")

    def test_accountless_sign_row_is_refused(self):
        """`phone=''` 带内哨兵已撤：sign_events 只装账号级事实（结构上拒绝）。"""
        with self.assertLogs("yiban.store.events", level="WARNING"):
            db.add_sign_event("2026-09-01 06:30:00", "", "pending",
                              "本执行体未领取：任务已由其他执行体领取，共 6 个账号",
                              stage="sign")
        self.assertEqual(self._count(), 0,
                         "无账号的 sign 事件必须被写入口拒绝（哨兵不得复活）")

    # ---- 门：两类判别 ----
    def _insert_raw(self, ts, phone, status, stage, attempt):
        """绕过写入口直插——模拟**历史行**（门要能审旧库）。"""
        self.conn.execute(
            "INSERT INTO sign_events (ts, phone, status, message, stage, attempt) "
            "VALUES (?,?,?,?,?,?)", (ts, phone, status, "", stage, attempt))
        self.conn.commit()

    def test_gate_passes_written_data(self):
        db.add_sign_event("2026-09-01 06:30:00", "13800138000", "failed",
                          stage="sign", attempt=1)
        db.add_sign_event("2026-09-01 06:31:00", "13800138000", "retrying",
                          stage="sign", attempt=1)
        status, groups = sign_events_observation_gate(self.conn)
        self.assertEqual(status, "ok")
        self.assertEqual(groups, [])

    def test_gate_separates_observation_from_transition_duplicates(self):
        # 观测重复（attempt=0，重扫重发）⇒ 红
        for minute in (1, 2):
            self._insert_raw(f"2026-09-01 06:3{minute}:00", "13800138000",
                             "pending", "sign", 0)
        # 变迁重复（attempt>0，多执行体各记一次）且在登记豁免日 ⇒ 登记，不算违规
        for minute in (1, 2, 3):
            self._insert_raw(f"2026-08-31 06:3{minute}:00", "13800138001",
                             "failed", "sign", 2)
        status, groups = sign_events_observation_gate(self.conn)
        self.assertEqual(status, "violations", "观测重复必须判红")
        classes = {(g["class"], g["day"]) for g in groups}
        self.assertIn(("observation", "2026-09-01"), classes)
        self.assertIn(("transition", "2026-08-31"), classes,
                      "08-31 的变迁重复必须作为豁免登记出现在结果里（不许静默吞掉）")
        observation = [g for g in groups if g["class"] == "observation"]
        self.assertEqual(len(observation), 1, "只应报那一条观测重复组")
        transition = [g for g in groups if g["class"] == "transition"]
        self.assertIn("伪造尝试史", transition[0]["reason"])

    def test_gate_transition_duplicate_outside_registry_is_red(self):
        """写入端幂等之后，登记日之外的变迁重复就是回归。"""
        for minute in (1, 2):
            self._insert_raw(f"2026-09-01 06:3{minute}:00", "13800138000",
                             "failed", "sign", 2)
        status, groups = sign_events_observation_gate(self.conn)
        self.assertEqual(status, "violations")
        self.assertEqual(groups[0]["class"], "transition")
        self.assertIn("未登记", groups[0]["reason"])

    def test_gate_empty_scan_is_not_green(self):
        status, groups = sign_events_observation_gate(self.conn)
        self.assertEqual(status, "cannot-determine",
                         "一行可判定的数据都没有时不得报绿（扫空 ≠ 通过）")
        self.assertEqual(groups, [])

    # ---- 突变验证：往干净夹具写两条同键观测 ⇒ 门必红 ----
    def test_mutation_two_observation_rows_turn_the_gate_red(self):
        db.add_sign_event("2026-09-01 06:30:00", "13800138000", "failed",
                          stage="sign", attempt=1)
        status, _ = sign_events_observation_gate(self.conn)
        self.assertEqual(status, "ok", "干净夹具必须先绿（突变前的基准）")
        self._insert_raw("2026-09-01 07:00:00", "13800138002", "pending", "sign", 0)
        self._insert_raw("2026-09-01 07:00:05", "13800138002", "pending", "sign", 0)
        status, groups = sign_events_observation_gate(self.conn)
        self.assertEqual(status, "violations", "突变后门必须变红")
        self.assertIn("observation", {g["class"] for g in groups})
