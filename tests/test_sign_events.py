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
`hash_ip` 加盐稳定性，以及可视化表格对同一批事件的解读。
对应实现：`yiban/store/` 的 sign_events 域（写入/聚合）与 `web/` 侧统计与可视化取数、
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
import os
import shutil
import tempfile
import unittest

from yiban import clock

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
        # 跳过发生在领取之前：两个执行体各走一遍跳过路径、各写一行 ⇒ 同一账号两行。
        # cnt 必须按账号计一次，否则报表虚增；row_cnt 保留真实行数（尝试次数）。
        db.add_sign_event(_recent(minutes=2), "13800138000", "user_cancelled")
        db.add_sign_event(_recent(minutes=1), "13800138000", "user_cancelled")
        row = self._stat_for("user_cancelled")
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
        # 各桶相加当"账号总数"仍会偏大（账号终值口径由 sign_event_accounts_summary 出，
        # 见 test_accounts_summary_emits_final_values——消费方禁止对 cnt 跨维直加）。
        db.add_sign_event(_recent(minutes=3), "13800138000", "failed")
        db.add_sign_event(_recent(minutes=2), "13800138000", "success")
        failed = self._stat_for("failed")
        success = self._stat_for("success")
        self.assertEqual(failed["cnt"], 1)
        self.assertEqual(success["cnt"], 1)
        self.assertEqual(failed["cnt"] + success["cnt"], 2,
                         "同一账号在两个桶各计一次（分桶去重的必然结果）")

    # ======== 新增聚焦钉（MF-55 主体：账号数终值由后端出） ========
    def test_accounts_summary_emits_final_values(self):
        """`sign_event_accounts_summary` 出账号数终值：窗口去重总数 / 按状态去重 / 按日最终态。

        回退即红的三格：① total 必须是 COUNT(DISTINCT phone)——旧前端把
        daily_stats.cnt 跨天跨状态直加，现网 953 对真值 94（10.1 倍）；② 探针行
        （stage=probe）不得混入（防混算的另一半在后端过滤，URL 那 10 个字符已另行钉死）；
        ③ by_day 是日终态分桶——先败后成的账号只落最终桶，日历染色不再 fail 优先。
        """
        def _noon(days_ago, minute=0):
            # 本地正午时刻：跨 UTC/本地时区双跑时，"日"的切分仍由被测代码自己一致地算，
            # 避开用数在午夜边缘漂到相邻日
            return ((clock.now() - datetime.timedelta(days=days_ago))
                    .replace(hour=12, minute=minute, second=0, microsecond=0)
                    .strftime("%Y-%m-%d %H:%M:%S"))
        d2, d1 = _noon(2)[:10], _noon(1)[:10]
        a, b, c = "13800138000", "13800138001", "13800138002"
        db.add_sign_event(_noon(2, 0), a, "failed", stage="sign")     # A 先败
        db.add_sign_event(_noon(2, 30), a, "success", stage="sign")   # 后成 ⇒ 当日终态 success
        db.add_sign_event(_noon(2, 10), b, "success", stage="sign")   # B 当日成功
        db.add_sign_event(_noon(1, 0), a, "success", stage="sign")    # A 次日又成功（跨天）
        db.add_sign_event(_noon(1, 20), c, "success", stage="probe")  # 探针行：不得混入
        s = db.sign_event_accounts_summary(days=30, stage="sign")
        # ① 总数 = 窗口去重（A、B 两个账号；A 跨两天、跨两个状态桶都只算一次）
        self.assertEqual(s["total"], 2, "「签到账号总数」= 窗口 COUNT(DISTINCT phone)")
        naive = sum(r["cnt"] for r in db.sign_event_stats(days=30, stage="sign"))
        self.assertGreater(naive, s["total"],
                           "cnt 跨天跨状态直加必虚增（登记现网 953 vs 94 的机制）")
        # ② stage 过滤在查询端也成立：不带 stage 时探针账号混进来
        self.assertEqual(s["by_status"], {"failed": 1, "success": 2},
                         "success 桶含 A、B（窗口去重），failed 桶含 A——桶间有交集")
        self.assertGreater(sum(s["by_status"].values()), s["total"],
                           "桶有交集：桶合计不得当总数（占比只按桶自身计）")
        no_stage = db.sign_event_accounts_summary(days=30)
        self.assertEqual(no_stage["total"], 3, "不过滤时探针账号混入——stage 过滤是真防线")
        # ③ 日终态分桶：每账号当日恰落一桶（桶互斥 ⇒ 日内相加合法）
        by_day = {(r["day"], r["status"]): r["accounts"] for r in s["by_day"]}
        self.assertEqual(by_day.get((d2, "failed"), 0), 0,
                         "A 先败后成 ⇒ 当日终态不再落 failed 桶（日历不再被涂红）")
        self.assertEqual(by_day[(d2, "success")], 2, "D2：A、B 终态都成功")
        self.assertEqual(by_day[(d1, "success")], 1, "D1：只有 A（探针 C 已排除）")
        day_totals = {}
        for r in s["by_day"]:
            day_totals[r["day"]] = day_totals.get(r["day"], 0) + r["accounts"]
        self.assertEqual(day_totals, {d2: 2, d1: 1}, "日终态桶合计 = 当下去重账号数")

    def test_summary_rows_do_not_pollute_account_stats(self):
        """收尾扫描的**批量摘要行**（`phone=''`）不进账号统计——工单 81xt 的读者面。

        兜底收尾扫描把「已由他人负责」收成一条批量事件（O(1)）。该行没有单一账号
        （`phone=''`），若照常参与聚合：`sign_event_stats` 会凭空造出一个 `pending`
        桶、`sign_event_accounts_summary.total` 会把摘要当成第 N+1 个账号——正是
        2026-10-10 生产「pending 桶 = 全部账号数」的成因形状（MF-55）。
        摘要行仍留在表里、仍可被事件流读出（它是那条 O(1) 留痕本身）。
        """
        db.add_sign_event(_recent(minutes=2), "13800138000", "success", stage="sign")
        # 摘要行：一条批量结论，不带单一账号
        db.add_sign_event(_recent(minutes=1), "", "pending",
                          "本执行体未领取：任务已由其他执行体领取，共 6 个账号",
                          stage="sign")
        # ① 聚合：摘要行不得造出 pending 桶、不得计入账号总数
        stats = {(r["day"], r["status"]): r
                 for r in db.sign_event_stats(days=30, stage="sign")}
        self.assertEqual(set(s for (_d, s) in stats), {"success"},
                         f"摘要行凭空造出了状态桶（生产 pending 桶污染的形状）：{stats}")
        summary = db.sign_event_accounts_summary(days=30, stage="sign")
        self.assertEqual(summary["total"], 1,
                         "摘要行被算成了第 2 个账号——账号总数被污染")
        self.assertEqual(summary["by_status"], {"success": 1},
                         "摘要行混进了按状态去重的账号数")
        # ② 留痕：摘要行仍在表内、仍可被事件流读出
        raw = db.get_conn().execute(
            "SELECT phone, status FROM sign_events ORDER BY id").fetchall()
        self.assertEqual([(r["phone"], r["status"]) for r in raw],
                         [("13800138000", "success"), ("", "pending")],
                         "摘要行必须仍在表里（它就是那条 O(1) 留痕）")
        stream = db.sign_events_since("1970-01-01 00:00:00", limit=10)
        self.assertIn("", [str(r["phone"]) for r in stream],
                      "摘要行必须仍能被事件流读出，不得被静默丢弃")
