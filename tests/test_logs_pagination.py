# -*- coding: utf-8 -*-
"""`GET /api/logs` 分页参数（`limit`/`offset`）守卫（工单 yiban-auto-sign-p37f 后端面）。

标签：E · Web：认证/权限/API
覆盖：`web/routes/data.py::api_logs` 新增的可选分页参数。
对应实现：`web/routes/__init__.py::page_args`（三域共用的唯一解析点）、
    `web/routes/data.py::api_logs`。
关键断言（测不变式，不测编舞）：
    ① 不传 `limit`/`offset` 时响应与分页前**逐字相同**——无 `limit`/`offset` 键，
       `logs`/`total_lines`/`returned`/`truncated` 与今天同轴；
    ② `limit`/`offset` 生效：`logs` 等于过滤后结果集的 `[offset, offset+limit)` 切片，
       顺序与 `all=1` 全量视图逐行一致；
    ③ `limit` 越界被夹取到 [1, 200]（与账号/用户列表同一口径），非法整数回落缺省 100；
    ④ 任一 `offset` 都不许返回越界行：超总数 → 空页、`returned=0`、`total_lines` 仍为总数；
    ⑤ 分页切分不重不漏：逐页取完覆盖全量集合，页间无重复。
依赖：纯本地 Flask test client + 临时 `.env`/SQLite/按天日志文件；不联网、无需 node。

**为什么需要**：单日日志实测可达 6.3MB/40k 行（巡检记录）。前端今天是"接口按日一次性
返回当日结果集、客户端翻页"，5k 行下首屏 DOM 不受控。服务端分页参数是"翻页不整页
重拉"的地基。本文件钉住向后兼容边界：旧调用方（不传参数）的响应一个字节都不能变。
"""
import contextlib
import importlib.util
import os
import shutil
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TEST_KEY = "a" * 64
ADMIN_PASS = "TestPass1234!"
#: 固定历史日期：不与今天冲突。测试建 app 时会在**今天**的日志文件写启动告警，
#: 用历史日期可让被测日只有本文件种下的行（否则 returned 会多出启动行，伪红）。
LOG_DATE = "2026-08-15"

#: 与实现里的 page_args 口径一致（沿用日志列表 1–200）。
PAGE_LIMIT_MAX = 200
PAGE_LIMIT_DEFAULT = 100


def _log_line(date, level, name, msg):
    return f"[{date} 06:31:01] [{level}] {name}: {msg}"


class LogsPaginationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-logpage-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(
                f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
                f"YIBAN_ADMIN_USER=admin\nYIBAN_ADMIN_PASSWORD={ADMIN_PASS}\n"
            )
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.accounts_file = os.path.join(cls.tmp, "accounts.json")
        cls.users_file = os.path.join(cls.tmp, "users.json")
        cls.log_file = os.path.join(cls.tmp, "sign.log")
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_ACCOUNTS_FILE"] = cls.accounts_file
        os.environ["YIBAN_USERS_FILE"] = cls.users_file
        os.environ["YIBAN_DB_FILE"] = cls.db_file
        os.environ["YIBAN_STATE_DIR"] = cls.tmp
        os.environ["YIBAN_LOG_FILE"] = cls.log_file
        global db
        import db
        spec = importlib.util.spec_from_file_location(
            "webapp_logpage", os.path.join(BASE, "web", "app.py"))
        cls.webapp = importlib.util.module_from_spec(spec)
        sys.modules["webapp_logpage"] = cls.webapp
        with contextlib.suppress(Exception):
            spec.loader.exec_module(cls.webapp)
        cls.webapp.migrate_admin_password_to_hash(cls.env_file)

    @classmethod
    def tearDownClass(cls):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k in ("YIBAN_ACCOUNTS_KEY", "YIBAN_LOG_FILE", "YIBAN_STATE_DIR",
                  "YIBAN_ENV_FILE", "YIBAN_ACCOUNTS_FILE", "YIBAN_USERS_FILE",
                  "YIBAN_DB_FILE"):
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
            f.write("[]")
        with open(self.users_file, "w", encoding="utf-8") as f:
            f.write("[]")
        db.init_db(self.db_file, migrate_from=self.accounts_file, env_file=self.env_file)
        for n in os.listdir(self.tmp):
            if n.startswith("sign-") and n.endswith(".log"):
                os.remove(os.path.join(self.tmp, n))

    # ---- 夹具 ----
    def _admin_client(self):
        c = self.webapp.create_app().test_client()
        r = c.post("/api/login", json={"username": "admin", "password": ADMIN_PASS})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return c

    def _write_log(self, date, lines):
        path = os.path.join(self.tmp, f"sign-{date}.log")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        return path

    def _seed_lines(self, date, n):
        """造 n 行可见（yiban logger，全级别放行）日志。"""
        self._write_log(date, [
            _log_line(date, "ERROR", "yiban", f"[1380013800{i % 10}] 行{i:04d}")
            for i in range(n)
        ])

    def _full(self, c, date, q=""):
        url = f"/api/logs?date={date}&all=1&level=all"
        if q:
            url += f"&q={q}"
        return c.get(url).get_json()

    # ---- ① 默认无参数 = 旧行为 ----
    def test_default_no_params_unchanged(self):
        """不传 limit/offset：无分页键，logs/returned/truncated 与今天同轴。"""
        date = LOG_DATE
        self._seed_lines(date, 5)
        c = self._admin_client()
        data = c.get(f"/api/logs?date={date}&level=all").get_json()
        self.assertEqual(data["total_lines"], 5)
        self.assertEqual(data["returned"], 5, "5 行 < 80：缺省全量返回")
        self.assertFalse(data["truncated"])
        self.assertNotIn("limit", data, "不传参数不得出现 limit 键")
        self.assertNotIn("offset", data, "不传参数不得出现 offset 键")

    def test_default_all_flag_unchanged(self):
        """`all=1` 模式不受新参数影响：仍是当日全量（封顶）。"""
        date = LOG_DATE
        self._seed_lines(date, 5)
        c = self._admin_client()
        data = c.get(f"/api/logs?date={date}&all=1&level=all").get_json()
        self.assertEqual(data["returned"], 5)
        self.assertNotIn("limit", data)
        self.assertNotIn("offset", data)

    def test_default_key_set_frozen(self):
        """不传参数时顶层键名集合与分页前逐字一致——新参数不得以任何键名泄漏。"""
        date = LOG_DATE
        self._seed_lines(date, 3)
        c = self._admin_client()
        data = c.get(f"/api/logs?date={date}&level=all").get_json()
        self.assertEqual(set(data), {
            "ok", "logs", "total_lines", "returned", "truncated", "dropped_lines",
            "q", "level", "collapsed_lines", "log_file", "date", "is_today",
            "probe_events", "sign_events",
            "recent_log_date", "recent_probe_date", "recent_sign_date",
        }, "不传参数时顶层键集合被改动（新参数泄漏进旧调用方）")

    def test_paged_key_set_adds_only_limit_offset(self):
        """分页时只允许新增 `limit`/`offset` 两键（不多不少）。"""
        date = LOG_DATE
        self._seed_lines(date, 3)
        c = self._admin_client()
        base = c.get(f"/api/logs?date={date}&level=all").get_json()
        paged = c.get(f"/api/logs?date={date}&level=all&limit=2&offset=0").get_json()
        self.assertEqual(set(paged) - set(base), {"limit", "offset"})
        self.assertEqual(set(base) - set(paged), set())

    # ---- ② limit/offset 生效：切片 == 全量视图的 [offset, offset+limit) ----
    def test_page_slice_matches_full_view(self):
        date = LOG_DATE
        self._seed_lines(date, 7)
        c = self._admin_client()
        full = self._full(c, date)["logs"]
        self.assertEqual(len(full), 7)
        data = c.get(f"/api/logs?date={date}&level=all&limit=3&offset=2").get_json()
        self.assertEqual(data["logs"], full[2:5], "分页必须是全量视图的连续切片")
        self.assertEqual(data["total_lines"], 7)
        self.assertEqual(data["returned"], 3)
        self.assertEqual(data["limit"], 3)
        self.assertEqual(data["offset"], 2)
        self.assertTrue(data["truncated"], "还有更多行 ⇒ truncated")

    def test_page_without_all_flag_still_slices(self):
        """只给 limit 不配合 all=1：必须切片，而不是回落"最后 80 行"。"""
        date = LOG_DATE
        self._seed_lines(date, 5)
        c = self._admin_client()
        full = self._full(c, date)["logs"]
        data = c.get(f"/api/logs?date={date}&level=all&limit=2&offset=0").get_json()
        self.assertEqual(data["logs"], full[0:2])
        self.assertEqual(data["returned"], 2)

    def test_page_respects_q_filter(self):
        """分页作用在**过滤后**的结果集上（q 先过滤，再切片）。"""
        date = LOG_DATE
        self._write_log(date, [
            _log_line(date, "ERROR", "yiban", "KEEP-a"),
            _log_line(date, "ERROR", "yiban", "DROP-x"),
            _log_line(date, "ERROR", "yiban", "KEEP-b"),
            _log_line(date, "ERROR", "yiban", "KEEP-c"),
        ])
        c = self._admin_client()
        hits = self._full(c, date, q="KEEP")["logs"]
        self.assertEqual(len(hits), 3)
        data = c.get(
            f"/api/logs?date={date}&level=all&q=KEEP&limit=1&offset=1").get_json()
        self.assertEqual(data["logs"], hits[1:2])
        self.assertEqual(data["total_lines"], 3, "total_lines 仍是过滤后命中数")

    # ---- ③ 越界夹取 ----
    def test_limit_clamped_upper(self):
        date = LOG_DATE
        self._seed_lines(date, PAGE_LIMIT_MAX + 5)  # 205 > 200
        c = self._admin_client()
        data = c.get(f"/api/logs?date={date}&level=all&limit=100000").get_json()
        self.assertEqual(data["limit"], PAGE_LIMIT_MAX, "limit 上限必须夹到 200")
        self.assertEqual(data["returned"], PAGE_LIMIT_MAX, "一次最多返回 200 行")

    def test_limit_clamped_lower(self):
        date = LOG_DATE
        self._seed_lines(date, 3)
        c = self._admin_client()
        for bad in ("0", "-7"):
            data = c.get(f"/api/logs?date={date}&level=all&limit={bad}").get_json()
            self.assertEqual(data["limit"], 1, f"limit={bad} 必须夹到下限 1")
            self.assertEqual(data["returned"], 1, f"limit={bad} 只返回 1 行")

    def test_invalid_params_fall_back(self):
        date = LOG_DATE
        self._seed_lines(date, 3)
        c = self._admin_client()
        data = c.get(f"/api/logs?date={date}&level=all&limit=abc&offset=xyz").get_json()
        self.assertEqual(data["limit"], PAGE_LIMIT_DEFAULT, "非法 limit 回落缺省 100")
        self.assertEqual(data["offset"], 0, "非法 offset 回落 0")
        self.assertEqual(data["returned"], 3)

    # ---- ④ 越界 offset：空页、不越界 ----
    def test_offset_beyond_end_returns_empty(self):
        date = LOG_DATE
        self._seed_lines(date, 3)
        c = self._admin_client()
        data = c.get(f"/api/logs?date={date}&level=all&limit=10&offset=999").get_json()
        self.assertEqual(data["logs"], [])
        self.assertEqual(data["returned"], 0)
        self.assertEqual(data["total_lines"], 3, "总数与页无关")

    def test_negative_offset_clamped_to_zero(self):
        date = LOG_DATE
        self._seed_lines(date, 3)
        c = self._admin_client()
        data = c.get(f"/api/logs?date={date}&level=all&limit=2&offset=-5").get_json()
        self.assertEqual(data["offset"], 0, "负 offset 夹到 0")
        full = self._full(c, date)["logs"]
        self.assertEqual(data["logs"], full[0:2])

    # ---- ⑤ 分页不重不漏 ----
    def test_pages_cover_full_set_without_gap_or_dup(self):
        date = LOG_DATE
        n = 25
        self._seed_lines(date, n)
        c = self._admin_client()
        full = self._full(c, date)["logs"]
        self.assertEqual(len(full), n)
        page = 4
        got = []
        for off in range(0, n + page, page):
            data = c.get(
                f"/api/logs?date={date}&level=all&limit={page}&offset={off}").get_json()
            got.extend(data["logs"])
        self.assertEqual(got, full, "逐页取完必须逐行等于全量视图（不重不漏、次序一致）")


if __name__ == "__main__":
    unittest.main()
