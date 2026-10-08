# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""巡检面：`run_events` 只读读取层 + `/api/admin/run-events` 契约回归。

本批之前 `run_events`（内核进度事件层）**只有写者、没有读者**：六个节点由
`executor_v3.py` 与 `runner.py` 打点，`cleanup.py` 每日按保留期清理，web 面 0 个文件
读它。排障时"谁签的、走到哪一步"只能靠文件日志猜。本文件钉住新开的读取面。

覆盖的三件交付（逐件一组）：
1. 读取层 + 轮级摘要：`yiban/store/run_events.py::summarize`（每轮一行——领取数 /
   成功数 / 失败数 / 未执行数 / 耗时）；
2. 事件时间线：`run_events.py::timeline`（按轮展开 claim → start → success/fail →
   finalize）与 `GET /api/admin/run-events` 的 `events` 字段；
3. 页面接线：`/data/logs` 仍是巡检页（no-store、挂载点），新端点在它的客户端拉取面内。

契约（本文件钉住的硬性质）：
- 只读：不写表、不自行写审计、**不调 `verify_audit_chain`**（读接口不触发全表哈希）；
- 鉴权：`/api/*` 默认要登录（管理员面），普通用户 403、匿名 401；
- 隐私：账号一律掩码（复用 `yiban.masking.mask_phone` 单源），执行体只回角色与槽位
  （`yiban.egress.owner_tag`），**响应体里既无 11 位明文号、也无主机名**；
- 保留期：响应回显 `retention_days` 与窗口起止；窗口外的日期给 `in_window=false`，
  页面据此给"跨月回溯请走审计日志页"的指引，不静默出空表。

标签：A · 内核：队列/领取/执行体
覆盖：`yiban/store/run_events.py`（读取层）、`web/routes/run_events_api.py`（新端点）、
    `web/app.py` 的登录守卫（默认拒绝）。
对应实现：同覆盖清单；节点值域唯一出处仍是 `run_events.NODES`。
关键断言：轮级计数与"领取未开始"的口径、耗时来自时刻差、时间线按写入序、掩码与
    主机名不落地、只读不触发全表哈希、窗口外给指引标志。
依赖：临时 SQLite（真实迁移链）+ Flask test client；不联网、不访问真实易班接口。
"""
import contextlib
import datetime
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

from yiban import clock
from yiban.store import run_events

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TEST_KEY = "a" * 64
AUDIT_KEY = "b" * 64
ADMIN_PASS = "TestPass1234!"
USER_PASS = "UserPass123!"
USER_EMAIL = "inspect-user@example.com"
RAW_PHONE = "13800138001"
MASKED_PHONE = "138****8001"
#: 执行体身份串两例：单执行体与第 3 个并行执行体。主机名是**部署信息**，绝不出接口。
OWNER_SINGLE = "single@inspect-host"
OWNER_WORKER = "worker-2@inspect-host"

db = None  # setUpClass 装载（裸模块名，pyproject pythonpath 已含 scripts）


def _ts(day, hhmmss):
    return f"{day} {hhmmss}"


class InspectionCase(unittest.TestCase):
    """共享夹具：临时 `.env`/SQLite（真实迁移链）+ 一个管理员会话。

    三个子类共用同一套夹具（同一库文件、同一次登录）：`db` 是进程级单例，
    每个类各建一个库会让先跑的类的连接被后跑的类换掉。登录只在 setUpClass 做
    一次——`/api/login` 有 60 秒 10 次/IP 的限速，逐用例登录会把后面的用例顶到 429。
    """

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-runevents-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.accounts_file = os.path.join(cls.tmp, "accounts.json")
        cls.users_file = os.path.join(cls.tmp, "users.json")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(
                f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
                f"YIBAN_AUDIT_KEY={AUDIT_KEY}\n"
                f"YIBAN_ADMIN_USER=admin\nYIBAN_ADMIN_PASSWORD={ADMIN_PASS}\n"
            )
        with open(cls.accounts_file, "w", encoding="utf-8") as f:
            json.dump([], f)
        # 逐键记旧值再覆盖（收尾按旧值还原，不做"删掉当清理"）。
        cls._old_env = {k: os.environ.get(k) for k in (
            "YIBAN_ACCOUNTS_KEY", "YIBAN_AUDIT_KEY", "YIBAN_ENV_FILE",
            "YIBAN_ACCOUNTS_FILE", "YIBAN_USERS_FILE", "YIBAN_DB_FILE",
            "YIBAN_STATE_DIR", "YIBAN_LOG_FILE")}
        os.environ.update({
            "YIBAN_ACCOUNTS_KEY": TEST_KEY,
            "YIBAN_AUDIT_KEY": AUDIT_KEY,
            "YIBAN_ENV_FILE": cls.env_file,
            "YIBAN_ACCOUNTS_FILE": cls.accounts_file,
            "YIBAN_USERS_FILE": cls.users_file,
            "YIBAN_DB_FILE": cls.db_file,
            "YIBAN_STATE_DIR": cls.tmp,
            "YIBAN_LOG_FILE": os.path.join(cls.tmp, "sign.log"),
        })

        global db
        import db
        # 库单例是**进程级**的：先清掉上一个测试模块可能留下的连接。不清就会踩
        # `init_db` 的既有语义——已存在连接时它复用旧连接、只刷新"声明的路径"，
        # 于是本夹具的库根本没被打开（全量并发下 `/api/accounts` 空表那类伪红即由此而来）。
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        spec = importlib.util.spec_from_file_location(
            "webapp_runevents", os.path.join(BASE, "web", "app.py"))
        cls.webapp = importlib.util.module_from_spec(spec)
        sys.modules["webapp_runevents"] = cls.webapp
        with contextlib.suppress(Exception):
            spec.loader.exec_module(cls.webapp)
        db.init_db(cls.db_file, migrate_from=cls.accounts_file, env_file=cls.env_file)
        db.create_user(USER_EMAIL, cls.webapp.generate_password_hash(USER_PASS))
        cls.admin_client = cls.webapp.create_app().test_client()
        r = cls.admin_client.post("/api/login",
                                  json={"username": "admin", "password": ADMIN_PASS})
        assert r.status_code == 200, r.get_data(as_text=True)[:400]

    @classmethod
    def tearDownClass(cls):
        if db is not None and db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k, old in cls._old_env.items():
            if old is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = old

    def setUp(self):
        self.day = clock.now().strftime("%Y-%m-%d")
        conn = db.get_conn()
        conn.execute("DELETE FROM run_events")
        conn.commit()

    # ---- 夹具：直接按表契约插入行（表就是契约面） ----
    def _insert(self, day, node, executor, phone="", message="", ts=None):
        conn = db.get_conn()
        conn.execute(
            "INSERT INTO run_events (ts, day, node, executor, phone, message) "
            "VALUES (?,?,?,?,?,?)",
            (ts or _ts(day, "06:40:00"), day, node, executor, phone, message))
        conn.commit()

    def _seed_round(self, day=None, executor=OWNER_SINGLE):
        """一轮：A 成功、B 失败、C 领取后未开始；另有执行体会话收尾一行。"""
        day = day or self.day
        a, b, c = "13800138001", "13800138002", "13800138003"
        for p in (a, b, c):
            self._insert(day, run_events.NODE_CLAIM, executor, p, "", _ts(day, "06:40:00"))
        for p, t in ((a, "06:40:05"), (b, "06:40:06")):
            self._insert(day, run_events.NODE_START, executor, p, "", _ts(day, t))
        self._insert(day, run_events.NODE_SUCCESS, executor, a, "签到成功",
                     _ts(day, "06:40:12"))
        self._insert(day, run_events.NODE_FAIL, executor, b, "密码错误",
                     _ts(day, "06:40:20"))
        self._insert(day, run_events.NODE_FINALIZE, executor, "",
                     "执行体会话收尾：本轮完成 2 个账号", _ts(day, "06:40:21"))

    def _user_client(self):
        u = db.find_user(USER_EMAIL)
        self.assertIsNotNone(u, "setUpClass 应已创建普通用户")
        c = self.webapp.create_app().test_client()
        with c.session_transaction() as s:
            s["auth"] = True
            s["role"] = "user"
            s["username"] = USER_EMAIL.lower()
            s["auth_source"] = "user"
            s["pw_version"] = u.get("pw_version", 1)
            s["login_ts"] = int(time.time())
            s["sid"] = "0" * 32
        return c


# ---------------------------------------------------------------------------
# 交付 1/2（读取层）：计数、掩码、执行体收敛、时间线、只读
# ---------------------------------------------------------------------------
class StoreReadLayerTest(InspectionCase):
    def test_summary_counts_and_unexecuted_and_duration(self):
        self._seed_round()
        rounds = run_events.summarize()
        self.assertEqual(len(rounds), 1, f"一个 (业务日, 执行体) 应聚成一行: {rounds}")
        r = rounds[0]
        self.assertEqual(r["day"], self.day)
        self.assertEqual(r["claim"], 3)
        self.assertEqual(r["start"], 2)
        self.assertEqual(r["success"], 1)
        self.assertEqual(r["fail"], 1)
        self.assertEqual(r["pause"], 0)
        self.assertEqual(r["unexecuted"], 1,
                         "领取后无 start 的账号（C）必须计成未执行")
        self.assertEqual(r["duration_sec"], 21, "耗时 = 该轮最晚与最早时刻之差（秒）")
        self.assertEqual(r["first_ts"], _ts(self.day, "06:40:00"))
        self.assertEqual(r["last_ts"], _ts(self.day, "06:40:21"))

    def test_summary_groups_by_day_and_executor(self):
        self._seed_round()
        other = (clock.now() - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
        self._insert(other, run_events.NODE_SUCCESS, OWNER_WORKER, RAW_PHONE, "签到成功")
        rounds = run_events.summarize()
        keys = {(r["day"], r["executor"]) for r in rounds}
        self.assertEqual(keys, {(self.day, "single"), (other, "worker-2")},
                         f"分组键含业务日与执行体公开标签: {rounds}")
        self.assertEqual(rounds[0]["day"], self.day, "最新的轮次排在最前")

    def test_phone_is_masked_at_the_read_boundary(self):
        self._seed_round()
        rounds = run_events.summarize()
        events, _truncated = run_events.timeline(self.day, "single")
        blob = json.dumps([rounds, events], ensure_ascii=False)
        self.assertNotIn(RAW_PHONE, blob, "读取层响应里不得出现 11 位明文手机号")
        self.assertIn(MASKED_PHONE, blob, "读取层必须按单源口径下发已遮值")
        self.assertIn("138****8002", json.dumps(events, ensure_ascii=False))

    def test_executor_is_reduced_to_role_and_slot(self):
        self._seed_round(executor=OWNER_WORKER)
        rounds = run_events.summarize()
        self.assertEqual(rounds[0]["executor"], "worker-2", "执行体只回槽位名，不带主机名")
        self.assertEqual(rounds[0]["executor_label"], "并行执行体 #3")
        self.assertNotIn("inspect-host", json.dumps(rounds, ensure_ascii=False),
                         "主机名属部署信息，任何接口都不得回原串")

    def test_timeline_is_ordered_and_truncates(self):
        self._seed_round()
        events, truncated = run_events.timeline(self.day, "single")
        self.assertFalse(truncated)
        self.assertEqual([e["node"] for e in events],
                         ["claim", "claim", "claim", "start", "start",
                          "success", "fail", "finalize"],
                         f"时间线必须按写入序展开: {events}")
        self.assertEqual(events[0]["node_label"], "领取")
        self.assertEqual(events[-1]["node_label"], "收尾")
        self.assertEqual(events[5]["message"], "签到成功")
        limited, limited_trunc = run_events.timeline(self.day, "single", limit=3)
        self.assertEqual(len(limited), 3)
        self.assertTrue(limited_trunc, "行数超过上限时必须报截断")

    def test_day_bounds_report_actual_min_and_max(self):
        older = (clock.now() - datetime.timedelta(days=2)).strftime("%Y-%m-%d")
        self._seed_round()
        self._seed_round(day=older)
        self.assertEqual(run_events.day_bounds(), (older, self.day))

    def test_day_bounds_empty_table(self):
        self.assertEqual(run_events.day_bounds(), (None, None))

    def test_read_layer_writes_nothing(self):
        """只读：读前后 run_events 行数与审计行数都不变。"""
        self._seed_round()
        conn = db.get_conn()
        before = conn.execute("SELECT COUNT(*) FROM run_events").fetchone()[0]
        audits = conn.execute("SELECT COUNT(*) FROM audit_logs").fetchone()[0]
        run_events.summarize()
        run_events.timeline(self.day, "single")
        run_events.day_bounds()
        self.assertEqual(before,
                         conn.execute("SELECT COUNT(*) FROM run_events").fetchone()[0],
                         "读取层不得增删 run_events 行")
        self.assertEqual(
            audits, conn.execute("SELECT COUNT(*) FROM audit_logs").fetchone()[0],
            "读取层不得自行写审计（留痕由路由层聚合，见 _read_audit_trace）")


# ---------------------------------------------------------------------------
# 交付 1/2（端点）：信封、鉴权、参数白名单、隐私、保留期、只读、留痕
# ---------------------------------------------------------------------------
class RunEventsApiTest(InspectionCase):
    def test_envelope_fields(self):
        self._seed_round()
        r = self.admin_client.get("/api/admin/run-events")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True)[:400])
        body = r.get_json()
        for key in ("ok", "retention_days", "window", "rounds", "events",
                    "events_truncated", "events_limit"):
            self.assertIn(key, body, f"信封缺键：{key}")
        for key in ("start_day", "end_day", "day", "in_window", "min_day",
                    "max_day", "has_data"):
            self.assertIn(key, body["window"], f"window 缺键：{key}")
        self.assertEqual(body["retention_days"], run_events.RETENTION_DAYS)
        self.assertEqual(body["window"]["day"], self.day)
        self.assertTrue(body["window"]["in_window"])
        self.assertTrue(body["window"]["has_data"])

    def test_default_round_and_timeline_selected(self):
        """不带参数：默认日 = 数据里的最新日，默认轮 = 该日最新一轮（含时间线）。"""
        self._seed_round()
        body = self.admin_client.get("/api/admin/run-events").get_json()
        self.assertEqual(len(body["rounds"]), 1)
        self.assertEqual(body["rounds"][0]["executor"], "single")
        self.assertEqual(body["rounds"][0]["claim"], 3)
        self.assertEqual(body["rounds"][0]["unexecuted"], 1)
        self.assertTrue(body["events"], "默认必须带上该轮的时间线")
        self.assertEqual([e["node"] for e in body["events"]].count("claim"), 3)

    def test_events_row_fields_and_node_label(self):
        self._seed_round()
        body = self.admin_client.get(
            "/api/admin/run-events?executor=single").get_json()
        row = body["events"][0]
        for key in ("ts", "node", "node_label", "phone", "message"):
            self.assertIn(key, row, f"事件行缺键：{key}")
        self.assertEqual(row["node"], "claim")
        self.assertEqual(row["node_label"], "领取")

    def test_auth_anonymous_401_and_user_403(self):
        anon = self.webapp.create_app().test_client()
        self.assertEqual(anon.get("/api/admin/run-events").status_code, 401)
        self.assertEqual(self._user_client().get("/api/admin/run-events").status_code, 403)

    def test_endpoint_is_not_in_any_allowlist(self):
        """必须挂在默认拒绝的守卫下：不得被登记进 web/app.py 的放行清单。"""
        src = open(os.path.join(BASE, "web", "app.py"), encoding="utf-8").read()
        self.assertNotIn("/api/admin/run-events", src,
                         "端点不得被登记进 web/app.py 的放行清单（管理员面按默认拒绝处理）")

    def test_unknown_query_key_400(self):
        r = self.admin_client.get("/api/admin/run-events?evil=1")
        self.assertEqual(r.status_code, 400)
        self.assertIn("不支持的过滤字段", r.get_json()["error"])

    def test_bad_params_400(self):
        c = self.admin_client
        self.assertEqual(c.get("/api/admin/run-events?day=2026-13-99").status_code, 400)
        self.assertEqual(c.get("/api/admin/run-events?day=abc").status_code, 400)
        self.assertEqual(c.get("/api/admin/run-events?limit=0").status_code, 400)
        self.assertEqual(c.get("/api/admin/run-events?limit=99999").status_code, 400)
        self.assertEqual(c.get("/api/admin/run-events?limit=abc").status_code, 400)
        self.assertEqual(c.get("/api/admin/run-events?executor=%3Cscript%3E").status_code, 400)

    def test_no_plaintext_phone_nor_hostname_in_body(self):
        self._seed_round()
        text = self.admin_client.get("/api/admin/run-events").get_data(as_text=True)
        self.assertNotIn(RAW_PHONE, text, "响应体不得含 11 位明文手机号")
        self.assertIn(MASKED_PHONE, text)
        self.assertNotIn("inspect-host", text, "响应体不得含执行体身份原串（带主机名）")

    def test_out_of_window_day_flags_guidance(self):
        old = (clock.now() - datetime.timedelta(days=30)).strftime("%Y-%m-%d")
        body = self.admin_client.get(
            f"/api/admin/run-events?day={old}").get_json()
        self.assertFalse(body["window"]["in_window"],
                         "保留期外的日期必须回显 in_window=false（页面据此给指引）")
        self.assertFalse(body["window"]["has_data"])
        self.assertEqual(body["rounds"], [])
        self.assertEqual(body["window"]["start_day"],
                         (clock.now() - datetime.timedelta(
                             days=run_events.RETENTION_DAYS - 1)).strftime("%Y-%m-%d"))

    def test_executor_filter_limits_timeline(self):
        self._seed_round(executor=OWNER_WORKER)
        self._insert(self.day, run_events.NODE_SUCCESS, OWNER_SINGLE, RAW_PHONE,
                     "签到成功", _ts(self.day, "07:00:00"))
        body = self.admin_client.get(
            "/api/admin/run-events?executor=worker-2").get_json()
        self.assertEqual({e["node"] for e in body["events"]}, {"claim", "start",
                                                              "success", "fail",
                                                              "finalize"})
        self.assertNotIn("07:00:00", json.dumps(body["events"]),
                         "执行体过滤必须排除其它执行体的行")

    def test_timeline_limit_and_truncated_flag(self):
        self._seed_round()
        r = self.admin_client.get(
            "/api/admin/run-events?executor=single&limit=2")
        body = r.get_json()
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True)[:300])
        self.assertEqual(len(body["events"]), 2)
        self.assertTrue(body["events_truncated"])
        self.assertEqual(body["events_limit"], 2)

    def test_read_does_not_verify_the_audit_chain(self):
        """只读端点不得触发全表哈希校验（照 audit_api 的先例）。"""
        self._seed_round()
        from yiban.store import audit_chain
        with mock.patch.object(audit_chain, "verify_audit_chain",
                               side_effect=AssertionError("读接口触发了全表哈希校验")):
            r = self.admin_client.get("/api/admin/run-events")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True)[:300])

    def test_successful_read_is_traced(self):
        """只读面留痕：审计读取本身也是一次访问（与 users/logs 同口径）。

        留痕是**按窗口聚合**的（`_read_audit_trace`：窗口内首行必落，此后按档位补行），
        故"多一行"只在窗口首读成立。本用例改为直接钉**调用事实**：路由必须以
        `run_events_read` 调一次留痕入口。聚合行为本身由 test_audit_chain 覆盖。
        """
        self._seed_round()
        app = self.admin_client.application
        calls = []
        orig = app.extensions["yiban_read_audit_trace"]
        app.extensions["yiban_read_audit_trace"] = (
            lambda action, target="": calls.append((action, target)))
        try:
            self.assertEqual(
                self.admin_client.get("/api/admin/run-events").status_code, 200)
        finally:
            app.extensions["yiban_read_audit_trace"] = orig
        self.assertEqual([c[0] for c in calls], ["run_events_read"],
                         "成功读取必须以 run_events_read 调一次留痕入口")

    def test_empty_table_still_returns_envelope(self):
        body = self.admin_client.get("/api/admin/run-events").get_json()
        self.assertEqual(body["rounds"], [])
        self.assertFalse(body["window"]["has_data"])
        self.assertEqual(body["window"]["day"],
                         clock.now().strftime("%Y-%m-%d"),
                         "无数据时默认日回今天")


# ---------------------------------------------------------------------------
# 交付 3（页面接线）：巡检页仍是 /data/logs，新端点在它的客户端拉取面内
# ---------------------------------------------------------------------------
class InspectionPageWiringTest(InspectionCase):
    def test_logs_page_still_renders_and_is_no_store(self):
        r = self.admin_client.get("/data/logs")
        self.assertEqual(r.status_code, 200)
        self.assertIn('id="vue-logs-app"', r.get_data(as_text=True))
        self.assertEqual(r.headers.get("Cache-Control"), "no-store")

    def test_frontend_consumes_the_run_events_contract(self):
        """跨边界契约：前端 `run-events.ts` 消费的字段与后端信封逐一对应。

        两侧单测都覆盖不到这一段：Python 侧只测自己返回什么，Vitest 只测纯函数；
        任一侧给字段改名而另一侧没跟上，就是巡检首屏白屏。
        """
        self._seed_round()
        body = self.admin_client.get("/api/admin/run-events").get_json()
        for key in body["rounds"][0]:
            self.assertIn(key, ("day", "executor", "executor_label", "claim", "start",
                                "success", "fail", "pause", "unexecuted",
                                "duration_sec", "first_ts", "last_ts"),
                          f"轮级摘要多了前端不认的键：{key}")
        for key in body["events"][0]:
            self.assertIn(key, ("ts", "node", "node_label", "phone", "message"),
                          f"事件行多了前端不认的键：{key}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
