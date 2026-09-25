# -*- coding: utf-8 -*-
"""按天日志（sign-YYYY-MM-DD.log）读取与按日期查看功能测试。

标签：F · 前端与界面守卫
覆盖：按天日志文件的路径推导与行过滤（跨天残留剔除）、`/api/logs` 与 `/api/my-logs` 的日期参数与权限、`recent_*` 字段指向、检索/全量与导出，以及日志页日期校验的 JS 行为
对应实现：`web/app.py` 的 `log_path_for` / `_log_lines_for` / `parse_sign_log` 与 `/api/logs*`；前端 `web/static/js/pages/data_logs.js` 里的 `isValidDate`
关键断言：文件日期≠行首日期的残留行不得混入；非法日期 400、历史日期文件缺失返回空而非报错；`yiban.*` 全级别入列而其它组件仅 WARNING+；`recent_log_date` 在当前日期就是最新天时为空串
依赖：⚠ **需要 node**——`LogsDateValidationTest` 把 `isValidDate` 从前端源码按花括号配对抽出后在 node 里真跑，`shutil.which("node")` 取不到时整类 `skipUnless`；宿主 ICU 不认 `Etc/GMT±N` 时区名时该用例还会 `skipTest`。其余用例纯本地 Flask test client + 临时 `.env`/SQLite，不联网、不访问真实易班接口；无需 node

背景（2026-08-16 需求）：日志改为按天分文件后，
- 管理员 /api/logs?date=YYYY-MM-DD 可查任意日期日志（缺省=今天，行为不变）
- 用户 /api/my-logs?date= 读对应日期文件（日历点历史日期可见自己的日志）
- 行首日期过滤防跨天残留；历史日期文件缺失返回空（不报错）

用法（项目根目录）：
    py -m pytest tests/test_logs_by_date.py -v
"""
import contextlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import timedelta

from yiban import clock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TEST_KEY = "a" * 64
ADMIN_PASS = "TestPass1234!"
USER_PASS = "secret1"
HIST_DATE = "2026-08-15"  # 固定历史日期（不与今天冲突）


def _log_line(date, level, name, msg):
    return f"[{date} 06:31:01] [{level}] {name}: {msg}"


class LogsByDateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-logdate-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(
                f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
                f"YIBAN_ADMIN_USER=admin\nYIBAN_ADMIN_PASSWORD={ADMIN_PASS}\n"
            )
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.accounts_file = os.path.join(cls.tmp, "accounts.json")
        cls.users_file = os.path.join(cls.tmp, "users.json")
        # 日志目录隔离到临时目录：LOG_FILE 指向 tmp/sign.log → 按天文件 = tmp/sign-*.log
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
        spec = importlib.util.spec_from_file_location("webapp", os.path.join(BASE, "web", "app.py"))
        cls.webapp = importlib.util.module_from_spec(spec)
        sys.modules["webapp"] = cls.webapp
        with contextlib.suppress(Exception):
            spec.loader.exec_module(cls.webapp)
        # 口令明文→哈希的启动迁移在此显式做掉：留在首个 create_app() 里做的话，那条
        # WARNING 会写进「当时的当天日志文件」——若正好是某个用例先写好文件再建 app，
        # 该用例就会多出一行（本文件 test_api_logs_default_is_today 断言行数，随
        # pytest-randomly 的执行顺序偶发失败，CI 上已复现）。setUp 每个用例都会清掉
        # 当天日志文件，故这里产生的告警行不会污染任何用例。
        cls.webapp.migrate_admin_password_to_hash(cls.env_file)

    @classmethod
    def tearDownClass(cls):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k in ("YIBAN_ACCOUNTS_KEY", "YIBAN_LOG_FILE", "YIBAN_STATE_DIR"):
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
        db.add_account({"name": "A2", "phone": "13900139002", "password": "p2",
                        "status": "active", "owner": "admin"})
        # 清理临时目录中的按天日志文件（跨测试隔离）
        for n in os.listdir(self.tmp):
            if n.startswith("sign-") and n.endswith(".log"):
                os.remove(os.path.join(self.tmp, n))

    # ---- 工具：构造按天日志文件 ----
    def _write_date_log(self, date, lines):
        path = os.path.join(self.tmp, f"sign-{date}.log")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        return path

    # ---- 1. log_path_for：按天路径 ----
    def test_log_path_for_today_and_hist(self):
        today = clock.today()
        self.assertEqual(
            self.webapp.log_path_for(),
            os.path.join(self.tmp, f"sign-{today}.log"),
        )
        self.assertEqual(
            self.webapp.log_path_for(HIST_DATE),
            os.path.join(self.tmp, f"sign-{HIST_DATE}.log"),
        )

    # ---- 2. _log_lines_for：该日 `yiban.*` 全部级别 + 其它组件仅告警级（2026-09-19 改口径）----
    def test_log_lines_for_filters(self):
        """签到链路的子 logger 与 DEBUG 必须入列——细节行原先被正则漏掉，页面只剩结果。"""
        self._write_date_log(HIST_DATE, [
            _log_line(HIST_DATE, "INFO", "yiban", "[13800138001] ✅ 签到成功"),
            _log_line(HIST_DATE, "INFO", "yiban", "==== 开始执行签到，共 1 个账号，队列重试模式 ===="),
            _log_line(HIST_DATE, "INFO", "yiban.client", "[13800138001] 生成定位: (118.8, 31.9)"),
            _log_line(HIST_DATE, "INFO", "yiban.fyiban.protocol", "[13800138001] 登录成功"),
            _log_line(HIST_DATE, "DEBUG", "yiban", "[13800138001] 登录方式: KillYiBan 同款"),
            _log_line(HIST_DATE, "INFO", "werkzeug", '127.0.0.1 - - "GET /api/logs HTTP/1.1" 200 -'),
            "无格式行（run.sh 直接 echo）",
        ])
        out = self.webapp._log_lines_for(HIST_DATE)
        joined = "\n".join(out)
        self.assertIn("签到成功", joined)
        self.assertIn("开始执行签到", joined)
        self.assertIn("生成定位", joined, "yiban.client 的细节行必须入列（旧正则漏掉带点的 logger）")
        self.assertIn("登录成功", joined, "yiban.fyiban.protocol 同上")
        self.assertIn("登录方式", joined, "yiban.* 的 DEBUG 也入列（部署自己开的级别）")
        self.assertNotIn("werkzeug", joined, "非 yiban 组件的 INFO 仍不入列")
        self.assertNotIn("无格式行", joined)
        self.assertEqual(len(out), 5)

    def test_log_lines_for_blocks_crossday_leftover(self):
        """跨天残留行（文件日期 ≠ 行首日期）不得混入。"""
        self._write_date_log(HIST_DATE, [
            _log_line(HIST_DATE, "INFO", "yiban", "[13800138001] ✅ 签到成功"),
            _log_line("2026-08-16", "INFO", "yiban", "[13800138001] ✅ 签到成功"),  # 残留
        ])
        out = self.webapp._log_lines_for(HIST_DATE)
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0].startswith(f"[{HIST_DATE} "))

    def test_log_lines_for_missing_file(self):
        self.assertEqual(self.webapp._log_lines_for("2026-08-01"), [])

    # ---- 3. parse_sign_log 兼容按天文件（0.19.6 起仅返回 recent 行，states 语义已移除）----
    def test_parse_sign_log_returns_recent_only(self):
        """口径与 `_log_lines_for` 同源（2026-09-19）：`yiban.*` 全级别入列，其它组件仅告警级。"""
        today = clock.today()
        self._write_date_log(today, [
            _log_line(today, "INFO", "yiban", "[13800138001] ✅ 签到成功"),
            _log_line(today, "DEBUG", "yiban", "[13800138001] 内部细节"),
            _log_line(today, "INFO", "yiban.client", "[13800138001] 生成定位: (118.8, 31.9)"),
            _log_line(today, "INFO", "werkzeug", "GET /api/logs"),  # 非 yiban INFO 仍应滤掉
        ])
        recent = self.webapp.parse_sign_log(self.webapp.log_path_for())
        self.assertEqual(len(recent), 3, "yiban 家族全收；非 yiban 的 INFO 不收")
        self.assertIn("✅ 签到成功", recent[0])
        self.assertIn("内部细节", recent[1])
        self.assertIn("生成定位", recent[2])

    # ---- 4. API：/api/logs 日期参数 ----
    def _admin_client(self):
        c = self.webapp.create_app().test_client()
        r = c.post("/api/login", json={"username": "admin", "password": ADMIN_PASS})
        self.assertEqual(r.status_code, 200)
        return c

    def test_api_logs_default_is_today(self):
        today = clock.today()
        self._write_date_log(today, [
            _log_line(today, "INFO", "yiban", "[13800138001] ✅ 签到成功"),
        ])
        c = self._admin_client()
        data = c.get("/api/logs").get_json()
        self.assertEqual(data["date"], today)
        self.assertEqual(data["log_file"], f"sign-{today}.log")
        self.assertEqual(len(data["logs"]), 1, data["logs"])
        self.assertIn("✅ 签到成功", data["logs"][0])
        # 0.19.6 起 /api/logs 不再返回 states（账号图标事实源为 /api/accounts），
        # 防止日志符号污染前端状态映射
        self.assertNotIn("states", data)

    def test_api_logs_bad_date_400(self):
        c = self._admin_client()
        self.assertEqual(c.get("/api/logs?date=2026-13-99").status_code, 400)
        self.assertEqual(c.get("/api/logs?date=abc").status_code, 400)

    def test_api_logs_hist_date(self):
        self._write_date_log(HIST_DATE, [
            _log_line(HIST_DATE, "INFO", "yiban", "[13900139002] ✅ 签到成功"),
        ])
        c = self._admin_client()
        data = c.get(f"/api/logs?date={HIST_DATE}").get_json()
        self.assertEqual(data["date"], HIST_DATE)
        self.assertEqual(data["log_file"], f"sign-{HIST_DATE}.log")
        self.assertEqual(len(data["logs"]), 1)

    def test_api_logs_missing_date_empty(self):
        c = self._admin_client()
        data = c.get("/api/logs?date=2026-08-01").get_json()
        self.assertEqual(data["logs"], [])

    # ---- 5. API：/api/my-logs 读按天文件（用户日历）----
    def _user_client(self):
        c = self.webapp.create_app().test_client()
        r = c.post("/api/login", json={"username": "user1@test.local", "password": USER_PASS})
        self.assertEqual(r.status_code, 200)
        return c

    def test_my_logs_hist_date_filters_own_phone(self):
        self._write_date_log(HIST_DATE, [
            _log_line(HIST_DATE, "INFO", "yiban", "[13800138001] ✅ 签到成功"),  # 自己的
            _log_line(HIST_DATE, "INFO", "yiban", "[13900139002] ✅ 签到成功"),  # 管理员的
            _log_line(HIST_DATE, "INFO", "yiban", "==== 签到汇总 ===="),
        ])
        c = self._user_client()
        data = c.get(f"/api/my-logs?date={HIST_DATE}").get_json()
        self.assertEqual(data["date"], HIST_DATE)
        self.assertEqual(len(data["logs"]), 1)
        # 日志行已脱敏：完整号 → 138****8001
        self.assertIn("138****8001", data["logs"][0])
        self.assertNotIn("13900139002", data["logs"][0])

    def test_my_logs_missing_date_empty(self):
        c = self._user_client()
        data = c.get("/api/my-logs?date=2026-08-01").get_json()
        self.assertEqual(data["logs"], [])

    def test_my_logs_bad_date_400(self):
        c = self._user_client()
        self.assertEqual(c.get("/api/my-logs?date=bad").status_code, 400)


def _d(offset):
    """相对今天的日期字符串（避免硬编码日期随运行日漂移）。"""
    return (clock.now() + timedelta(days=offset)).strftime("%Y-%m-%d")


class LogsRecentDateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-logs-recent-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        # 直接写 scrypt 哈希而非明文：明文会触发 create_app 的「口令明文」WARNING，
        # 该告警会被 _log_lines_for 收进当天日志，破坏「今天无日志」的场景。
        from werkzeug.security import generate_password_hash
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(
                f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
                f"YIBAN_ADMIN_USER=admin\n"
                f"YIBAN_ADMIN_PASSWORD_HASH={generate_password_hash(ADMIN_PASS, method='scrypt')}\n"
            )
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.accounts_file = os.path.join(cls.tmp, "accounts.json")
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_ACCOUNTS_FILE"] = cls.accounts_file
        os.environ["YIBAN_DB_FILE"] = cls.db_file
        os.environ["YIBAN_STATE_DIR"] = cls.tmp
        cls.log_file = os.path.join(cls.tmp, "sign.log")
        os.environ["YIBAN_LOG_FILE"] = cls.log_file
        os.environ["YIBAN_DISABLE_PURGE_LOOP"] = "1"
        global db
        import db
        with open(cls.accounts_file, "w", encoding="utf-8") as f:
            json.dump([], f)
        db.init_db(cls.db_file, migrate_from=cls.accounts_file, env_file=cls.env_file)
        spec = importlib.util.spec_from_file_location(
            "webapp_logs_recent", os.path.join(BASE, "web", "app.py"))
        cls.webapp = importlib.util.module_from_spec(spec)
        sys.modules["webapp_logs_recent"] = cls.webapp
        with contextlib.suppress(Exception):
            spec.loader.exec_module(cls.webapp)

        # 数据布局（相对今天）：
        #   log_day   = 今天-3：有日志行 + 有签到事件
        #   probe_day = 今天-1：只有探针事件
        #   today            ：三块皆空（构造用户实拍场景）
        cls.log_day = _d(-3)
        cls.probe_day = _d(-1)
        cls.today = _d(0)
        with open(os.path.join(cls.tmp, f"sign-{cls.log_day}.log"), "w", encoding="utf-8") as f:
            f.write(f"[{cls.log_day} 06:31:01] [INFO] yiban: ==== 今日签到开始 ====\n")
        db.add_sign_event(f"{cls.log_day} 06:31:02", "13800138000",
                          "success", "登录成功", stage="sign", attempt=1)
        db.add_sign_event(f"{cls.probe_day} 03:00:00", "", "ok",
                          "接口探测正常", stage="probe")

    @classmethod
    def tearDownClass(cls):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k in ("YIBAN_ACCOUNTS_KEY", "YIBAN_ENV_FILE", "YIBAN_ACCOUNTS_FILE",
                  "YIBAN_DB_FILE", "YIBAN_STATE_DIR", "YIBAN_LOG_FILE",
                  "YIBAN_DISABLE_PURGE_LOOP"):
            os.environ.pop(k, None)

    def _client(self):
        c = self.webapp.create_app().test_client()
        # create_app 会把「管理员口令明文」告警写进当天日志文件（WARNING 级也会被
        # _log_lines_for 收录），会破坏「今天三块皆空」的场景——删掉当天文件还原空态；
        # 同时清掉「最近日志日期」模块缓存，避免跨用例的当日缓存干扰本用例场景。
        with contextlib.suppress(OSError):
            os.remove(os.path.join(self.tmp, f"sign-{self.today}.log"))
        with contextlib.suppress(AttributeError, KeyError, TypeError):
            self.webapp._most_recent_log_cache["history_date"] = None
            self.webapp._most_recent_log_cache["checked_day"] = ""
        r = c.post("/api/login", json={"username": "admin", "password": ADMIN_PASS})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return c

    def test_recent_fields_point_to_each_source(self):
        c = self._client()
        r = c.get(f"/api/logs?date={self.today}")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        d = r.get_json()
        self.assertEqual(d["date"], self.today)
        self.assertEqual(d["logs"], [])
        self.assertEqual(d["recent_log_date"], self.log_day)
        self.assertEqual(d["recent_sign_date"], self.log_day)
        self.assertEqual(d["recent_probe_date"], self.probe_day)

    def test_recent_field_empty_when_current_date_is_latest(self):
        c = self._client()
        d = c.get(f"/api/logs?date={self.log_day}").get_json()
        # 当前日期自身有日志/签到事件 → 不再给「最近有数据日期」出口
        self.assertEqual(d["recent_log_date"], "")
        self.assertEqual(d["recent_sign_date"], "")
        # 该日期无探针事件，探针出口仍指向 probe_day
        self.assertEqual(d["recent_probe_date"], self.probe_day)

    def test_default_date_falls_back_to_recent_log_day(self):
        c = self._client()
        d = c.get("/api/logs").get_json()
        self.assertEqual(d["date"], self.log_day)
        self.assertTrue(d["logs"])


LOGS_JS = os.path.join(BASE, "web", "static", "js", "pages", "data_logs.js")


NODE = shutil.which("node")


TIMEZONES = [("Etc/GMT-8", -480), ("Etc/GMT+5", 300)]  # POSIX 反号记法：`Etc/GMT-8` 实为 UTC+8


VALID_DATES = ("2026-09-11", "2026-02-28", "2024-02-29", "2026-01-01")


INVALID_DATES = ("2026-02-29", "2026-13-01", "2026-00-10", "2026-09-31",
                 "2026-9-1", "", "2026-09-11 ", "20260911")


def _extract_function(src, name):
    """从源码里按花括号配对抽出 `function <name>(...) { ... }` 整段。

    正则字面量里的 `{4}`/`{2}` 是成对出现，配对计数不受影响。
    """
    start = src.index("function " + name + "(")
    i = src.index("{", start)
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[start:j + 1]
    raise AssertionError("函数 %s 未找到匹配的右花括号" % name)


def _run_in_tz(fn_src, tz):
    """在指定时区里执行抽出函数，返回 (实际偏移, 各日期结果)。"""
    script = (
        # TZ 必须先于脚本里任何 new Date() 设好，否则进程已按宿主时区初始化
        "process.env.TZ = %s;\n" % json.dumps(tz)
        + fn_src
        + "\nconst dates = %s;\n" % json.dumps(list(VALID_DATES + INVALID_DATES))
        + "console.log(JSON.stringify({"
        + "off: new Date('2026-09-11T00:00:00').getTimezoneOffset(),"
        + "r: dates.map(function (d) { return isValidDate(d); })}));\n"
    )
    proc = subprocess.run([NODE, "-e", script], capture_output=True, text=True,
                          timeout=30)
    if proc.returncode != 0:
        raise AssertionError("node 执行失败：%s" % (proc.stderr or proc.stdout))
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _strip_line_comments(src):
    """去掉 `//` 行注释（本函数体内的说明注释正以 toISOString 作反例，不能误伤）。"""
    return "\n".join(re.sub(r"//.*$", "", ln) for ln in src.split("\n"))


@unittest.skipUnless(NODE, "node 不可用：跳过日志页日期校验的 JS 行为测试")
class LogsDateValidationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(LOGS_JS, encoding="utf-8") as fh:
            src = fh.read()
        cls.fn_src = _extract_function(src, "isValidDate")

    def test_extracted_function_is_the_timezone_safe_shape(self):
        """抽出的函数不得再用 toISOString 做回环（回归钉点，失败即缺陷复现）。"""
        code = _strip_line_comments(self.fn_src)
        self.assertNotIn(
            "toISOString", code,
            "isValidDate 又用回 UTC 回环校验——UTC+8 下会把所有日期判非法",
        )
        self.assertIn("new Date(y, m - 1, d)", code,
                      "isValidDate 应按本地年月日构造做日历校验")
        self.assertTrue(re.search(r"getFullYear\(\)\s*===\s*y", code),
                        "isValidDate 应逐项比对年月日，拒绝 2026-02-29 这类不存在的日期")

    def test_valid_and_invalid_dates_in_non_utc_timezones(self):
        """合法/非法日期在多个非 UTC 时区下结果一致且正确。"""
        expected = [True] * len(VALID_DATES) + [False] * len(INVALID_DATES)
        honored = 0
        for tz, want_off in TIMEZONES:
            got = _run_in_tz(self.fn_src, tz)
            if got["off"] != want_off:
                # 宿主不认该时区名（ICU 缺 tzdata）：跳过该档，不误报
                continue
            honored += 1
            self.assertEqual(
                got["r"], expected,
                "时区 %s（偏移 %d）下日期校验结果错误：%r" % (tz, want_off, got["r"]),
            )
        if not honored:
            self.skipTest("宿主 ICU 不认 Etc/GMT±N 时区名，无法在本机重放该时区")

    def test_result_is_timezone_independent(self):
        """同一批日期在 UTC+8 与 UTC-5 下结果必须完全相同。"""
        seen = []
        for tz, want_off in TIMEZONES:
            got = _run_in_tz(self.fn_src, tz)
            if got["off"] == want_off:
                seen.append((tz, got["r"]))
        if len(seen) < 2:
            self.skipTest("宿主未同时认下两个时区，无法比对时区无关性")
        self.assertEqual(seen[0][1], seen[1][1],
                         "日期校验结果随进程时区变化：%r" % (seen,))


ADMIN_PASS_B19 = "MasterPass#2026"


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
                f"YIBAN_ADMIN_PASSWORD={ADMIN_PASS_B19}\n"
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
        r = c.post("/api/login", json={"username": "admin@test.local", "password": ADMIN_PASS_B19})
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


if __name__ == "__main__":
    unittest.main()
