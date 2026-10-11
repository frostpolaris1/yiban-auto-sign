# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""SSE 实时进度流契约回归：读取面 + 两个端点（`/api/stream/progress` 与
`/api/stream/my-signin`）。

覆盖（逐条对应工单验收项）
1. 端点契约：建连收 `snapshot`（六节点 totals + by_executor[]，finalize 单独一列）；
   有新增行时 1 拍内收到 `progress`；空闲收 `heartbeat`；快照不调 `summarize()`；
2. `Last-Event-ID` 续传：缺口可补则只补 `progress`（不发快照）；
3. 缺口语义：缺口 > GAP 或游标被 purge ⇒ `resync` + `snapshot`（**绝不静默重放**）；
4. 隐私守卫：流 payload 无手机号（含 message 里内嵌的裸号也必须遮）、executor 只回
   公开标签（不含主机名）；`my-signin` 只推本人号、不带全局聚合；
5. 鉴权：匿名 401；普通用户访问 progress 403；普通用户访问 my-signin 200；
   跨站 Origin ⇒ 403；
6. 连接治理：开关关闭 ⇒ `end(disabled)`；每会话上限 ⇒ `end(too_many_connections)`；
7. 审计：只读面留痕只在建连时一次，长连接生命周期内审计行数不随拍增长；
8. 配置名册：9 枚新键在册且缺省与代码读取口径一致。

契约来源：`yiban/store/run_events.py`（读取面，节点值域唯一出处）、
`web/services/run_stream.py`（广播器）、`web/routes/stream_api.py`（端点）、
`config/registry.json`（配置键唯一定义点）。
依赖：临时 SQLite（真实迁移链）+ Flask test client（流式响应 `buffered=False`）；
不联网、不访问真实易班接口。
"""
import contextlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

from web.services import run_stream
from yiban import clock
from yiban.store import run_events

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TEST_KEY = "a" * 64
AUDIT_KEY = "b" * 64
ADMIN_PASS = "TestPass1234!"
USER_PASS = "UserPass123!"
USER_EMAIL = "stream-user@example.com"
RAW_PHONE = "13800138001"
OTHER_PHONE = "13900139002"
OWNER_SINGLE = "single@stream-host"
OWNER_WORKER = "worker-2@stream-host"

#: 基线 `.env`：SSE 节拍压到 1 秒（心跳/快照），让"1 拍内到达"可在秒级断言。
_BASE_ENV = (
    "YIBAN_ACCOUNTS_KEY={acct}\n"
    "YIBAN_AUDIT_KEY={audit}\n"
    "YIBAN_ADMIN_USER=admin\n"
    "YIBAN_ADMIN_PASSWORD={pw}\n"
    "YIBAN_DISABLE_PURGE_LOOP=1\n"
    "YIBAN_SSE_POLL_SEC=1\n"
    "YIBAN_SSE_HEARTBEAT_SEC=1\n"
).format(acct=TEST_KEY, audit=AUDIT_KEY, pw=ADMIN_PASS)


def _reset_db_singleton(db):
    if db._conn is not None:
        with contextlib.suppress(Exception):
            db._conn.close()
        db._conn = None


def _parse(frame):
    """SSE 帧文本 → (event, id, data)。"""
    ev, eid, data = None, None, None
    for line in frame.splitlines():
        if line.startswith("event: "):
            ev = line[len("event: "):]
        elif line.startswith("id: "):
            eid = int(line[len("id: "):].strip())
        elif line.startswith("data: "):
            data = json.loads(line[len("data: "):])
    return ev, eid, data


class StreamCase(unittest.TestCase):
    """共享夹具：临时 `.env`/SQLite（真实迁移链）+ 管理员会话 + 普通用户会话。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-stream-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.accounts_file = os.path.join(cls.tmp, "accounts.json")
        cls.users_file = os.path.join(cls.tmp, "users.json")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(_BASE_ENV)
        with open(cls.accounts_file, "w", encoding="utf-8") as f:
            json.dump([], f)
        cls._old_env = {k: os.environ.get(k) for k in (
            "YIBAN_ACCOUNTS_KEY", "YIBAN_AUDIT_KEY", "YIBAN_ENV_FILE",
            "YIBAN_ACCOUNTS_FILE", "YIBAN_USERS_FILE", "YIBAN_DB_FILE",
            "YIBAN_STATE_DIR", "YIBAN_LOG_FILE", "YIBAN_DISABLE_PURGE_LOOP")}
        os.environ.update({
            "YIBAN_ACCOUNTS_KEY": TEST_KEY,
            "YIBAN_AUDIT_KEY": AUDIT_KEY,
            "YIBAN_ENV_FILE": cls.env_file,
            "YIBAN_ACCOUNTS_FILE": cls.accounts_file,
            "YIBAN_USERS_FILE": cls.users_file,
            "YIBAN_DB_FILE": cls.db_file,
            "YIBAN_STATE_DIR": cls.tmp,
            "YIBAN_LOG_FILE": os.path.join(cls.tmp, "sign.log"),
            "YIBAN_DISABLE_PURGE_LOOP": "1",
        })
        global db
        import db
        _reset_db_singleton(db)
        spec = importlib.util.spec_from_file_location(
            "webapp_stream", os.path.join(BASE, "web", "app.py"))
        cls.webapp = importlib.util.module_from_spec(spec)
        sys.modules["webapp_stream"] = cls.webapp
        with contextlib.suppress(Exception):
            spec.loader.exec_module(cls.webapp)
        db.init_db(cls.db_file, migrate_from=cls.accounts_file, env_file=cls.env_file)
        db.create_user(USER_EMAIL, cls.webapp.generate_password_hash(USER_PASS))
        db.add_account({"name": "u-acct", "phone": RAW_PHONE, "password": "p1",
                        "status": "active", "owner": USER_EMAIL})
        db.add_account({"name": "admin-acct", "phone": OTHER_PHONE, "password": "p2",
                        "status": "active", "owner": "admin"})
        cls.admin_client = cls.webapp.create_app().test_client()
        r = cls.admin_client.post("/api/login",
                                  json={"username": "admin", "password": ADMIN_PASS})
        assert r.status_code == 200, r.get_data(as_text=True)[:400]
        # create_app/登录已把 SECRET_KEY / ADMIN_PASSWORD_HASH / PW_VERSION / sid 写进 `.env`；
        # 逐用例重写 `.env` 必须从**这份基线**出发，否则会把它们抹掉、令管理员会话失效
        # （表现为流回 401 JSON，读两帧即 StopIteration）。
        with open(cls.env_file, encoding="utf-8") as f:
            cls._env_base_text = f.read()

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
        with open(self.env_file, "w", encoding="utf-8") as f:
            f.write(self._env_base_text)
        conn = db.get_conn()
        conn.execute("DELETE FROM run_events")
        conn.execute("DELETE FROM audit_logs WHERE action='run_events_stream'")
        conn.commit()
        run_stream.reset_hub()

    # ---- 夹具 ----
    def _env(self, **kv):
        # 从 post-create_app 基线出发（保留 SECRET_KEY / HASH / PW_VERSION / sid），
        # 再叠加本用例的 SSE 覆盖键。
        text = self._env_base_text + "".join("%s=%s\n" % (k, v) for k, v in kv.items())
        with open(self.env_file, "w", encoding="utf-8") as f:
            f.write(text)

    def _insert(self, node, executor=OWNER_SINGLE, phone="", message="", ts=None, day=None):
        day = day or self.day
        conn = db.get_conn()
        conn.execute(
            "INSERT INTO run_events (ts, day, node, executor, phone, message) "
            "VALUES (?,?,?,?,?,?)",
            (ts or f"{day} 06:40:00", day, node, executor, phone, message))
        conn.commit()

    def _seed_round(self):
        a, b, c = "13800138001", "13800138002", "13800138003"
        for p in (a, b, c):
            self._insert("claim", OWNER_SINGLE, p, "", f"{self.day} 06:40:00")
        for p in (a, b):
            self._insert("start", OWNER_SINGLE, p, "", f"{self.day} 06:40:05")
        self._insert("success", OWNER_SINGLE, a, "签到成功", f"{self.day} 06:40:12")
        self._insert("fail", OWNER_SINGLE, b, "密码错误", f"{self.day} 06:40:20")
        self._insert("finalize", OWNER_SINGLE, "",
                     "执行体会话收尾：本轮完成 2 个账号", f"{self.day} 06:40:21")

    def _user_client(self):
        u = db.find_user(USER_EMAIL)
        self.assertIsNotNone(u)
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

    # ---- 读流 ----
    def _close(self, resp):
        with contextlib.suppress(Exception):
            resp.close()

    def _open(self, client, path="/api/stream/progress", headers=None):
        return client.get(path, buffered=False, headers=headers or {})

    def _next_frame(self, resp, timeout=6.0):
        """取下一帧（后台线程读，超时即断言失败）。"""
        box = {}

        def run():
            try:
                box["chunk"] = next(resp.response)
            except StopIteration:
                box["chunk"] = None
            except Exception as e:      # 连接被关等
                box["err"] = e
        t = threading.Thread(target=run, daemon=True)
        t.start()
        t.join(timeout)
        self.assertFalse(t.is_alive(), "等待 SSE 帧超时（%.1fs）" % timeout)
        self.assertNotIn("err", box, "读帧异常: %r" % box.get("err"))
        chunk = box.get("chunk")
        if chunk is None:
            return None
        return chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk

    def _next_event(self, resp, timeout=6.0):
        frame = self._next_frame(resp, timeout)
        if frame is None:
            return None, None, None, ""
        ev, eid, data = _parse(frame)
        return ev, eid, data, frame

    def _read_until(self, resp, predicate, limit=12, timeout=6.0):
        """读到满足 predicate(event, id, data) 的帧；→ (frame, event, id, data)。"""
        deadline = time.time() + timeout
        last = ""
        for _ in range(limit):
            remaining = max(0.2, deadline - time.time())
            frame = self._next_frame(resp, remaining)
            if frame is None:
                break
            last = frame
            ev, eid, data = _parse(frame)
            if predicate(ev, eid, data):
                return frame, ev, eid, data
            if time.time() > deadline:
                break
        self.fail("未在 %ss/%d 帧内等到目标 SSE 事件；末帧=%r" % (timeout, limit, last))


# ---------------------------------------------------------------------------
# 1) 端点契约：快照 / 进度 / 心跳
# ---------------------------------------------------------------------------
class EndpointContractTest(StreamCase):
    def test_snapshot_delivered_on_connect(self):
        self._seed_round()
        resp = self._open(self.admin_client)
        try:
            self.assertEqual(resp.headers.get("Content-Type").split(";")[0],
                             "text/event-stream")
            self.assertEqual(resp.headers.get("X-Accel-Buffering"), "no")
            self.assertEqual(resp.headers.get("Cache-Control"), "no-cache")
            frame = self._next_frame(resp)
            ev, eid, data = _parse(frame)
            self.assertEqual(ev, "snapshot")
            self.assertGreater(eid, 0, "快照必须带 cursor（断线续传基准）")
            self.assertEqual(data["totals"],
                             {"claim": 3, "start": 2, "success": 1, "fail": 1,
                              "pause": 0, "finalize": 1},
                             "六节点各计数；finalize 单独一列，不并入任何完成数")
            rows = data["by_executor"]
            self.assertEqual(len(rows), 1)
            e = rows[0]
            self.assertEqual(e["executor"], "single")
            self.assertEqual(e["executor_label"], "单执行体")
            self.assertEqual(e["claim"], 3)
            self.assertEqual(e["unexecuted"], 1, "有 claim 无 start 的账号计未执行")
            self.assertEqual(e["duration_sec"], 21)
            self.assertEqual(data["cursor"], eid)
            self.assertTrue(data["window"]["in_window"])
            self.assertEqual(data["window"]["retention_days"], run_events.RETENTION_DAYS)
        finally:
            self._close(resp)

    def test_progress_delivered_within_one_tick(self):
        self._seed_round()
        resp = self._open(self.admin_client)
        try:
            _ev, snap_id, _snap, _f = self._next_event(resp)
            self._insert("success", OWNER_SINGLE, RAW_PHONE, "签到成功",
                         f"{self.day} 06:41:00")
            _frame, ev, eid, data = self._read_until(
                resp, lambda e, i, d: e == "progress" and d.get("node") == "success")
            self.assertEqual(ev, "progress")
            self.assertGreater(eid, snap_id)
            self.assertEqual(data["node_label"], "成功")
            self.assertEqual(data["executor"], "single")
            self.assertNotIn("phone", data, "progress payload 不得含手机号字段")
        finally:
            self._close(resp)

    def test_heartbeat_when_idle(self):
        resp = self._open(self.admin_client)
        try:
            self._next_event(resp)      # snapshot
            _frame, ev, _eid, data = self._read_until(
                resp, lambda e, i, d: e == "heartbeat")
            self.assertEqual(ev, "heartbeat")
            self.assertIn("ts", data)
            self.assertIn("cursor", data)
        finally:
            self._close(resp)

    def test_snapshot_does_not_call_summarize(self):
        """快照走定向聚合，绝不调 `summarize()`（125 §3.2：12 万行 407ms 会卡死服务）。"""
        self._seed_round()

        def boom(*a, **k):
            raise AssertionError("snapshot 不得调 summarize")

        real = run_events.summarize
        run_events.summarize = boom
        try:
            snap = run_events.snapshot()
        finally:
            run_events.summarize = real
        self.assertEqual(snap["totals"]["claim"], 3)


# ---------------------------------------------------------------------------
# 2/3) 断线续传与缺口
# ---------------------------------------------------------------------------
class ResumeTest(StreamCase):
    def test_last_event_id_replays_gap_without_snapshot(self):
        self._seed_round()
        resp = self._open(self.admin_client)
        try:
            _ev, cursor, _d, _f = self._next_event(resp)
        finally:
            self._close(resp)
        # 断开后写入 3 行
        for i in range(3):
            self._insert("start", OWNER_SINGLE, RAW_PHONE, "", f"{self.day} 06:42:0{i}")
        resp2 = self._open(self.admin_client, headers={"Last-Event-ID": str(cursor)})
        try:
            ids = []
            for _ in range(3):
                ev, eid, _data, _f = self._next_event(resp2)
                self.assertEqual(ev, "progress", "可补缺口只补 progress，不发快照")
                ids.append(eid)
            self.assertEqual(ids, [cursor + 1, cursor + 2, cursor + 3])
        finally:
            self._close(resp2)

    def test_gap_over_max_triggers_resync(self):
        self._env(YIBAN_SSE_GAP_MAX=2)
        self._seed_round()
        resp = self._open(self.admin_client)
        try:
            _ev, cursor, _d, _f = self._next_event(resp)
        finally:
            self._close(resp)
        for i in range(5):
            self._insert("start", OWNER_SINGLE, RAW_PHONE, "", f"{self.day} 06:43:0{i}")
        resp2 = self._open(self.admin_client, headers={"Last-Event-ID": str(cursor)})
        try:
            ev, _eid, data, _f = self._next_event(resp2)
            self.assertEqual(ev, "resync")
            self.assertEqual(data["reason"], "gap_too_large")
            self.assertEqual(data["cursor"], cursor)
            ev2, _eid2, data2, _f2 = self._next_event(resp2)
            self.assertEqual(ev2, "snapshot", "resync 之后必须紧跟 snapshot")
            self.assertGreaterEqual(data2["cursor"], cursor + 5,
                                    "重同步快照必须取新（游标不得因 1 秒缓存回退）")
        finally:
            self._close(resp2)

    def test_purged_cursor_triggers_resync(self):
        for i in range(5):
            self._insert("claim", OWNER_SINGLE, RAW_PHONE, "", f"{self.day} 06:44:0{i}")
        conn = db.get_conn()
        max_before = conn.execute("SELECT MAX(id) AS m FROM run_events").fetchone()["m"]
        # 模拟 purge：删掉游标以下的旧行，使 min_id 抬到 max 附近
        conn.execute("DELETE FROM run_events WHERE id <= ?", (max_before - 1,))
        conn.commit()
        resp = self._open(self.admin_client, headers={"Last-Event-ID": "1"})
        try:
            ev, _eid, data, _f = self._next_event(resp)
            self.assertEqual(ev, "resync")
            self.assertEqual(data["reason"], "cursor_invalid")
            ev2, _eid2, _d2, _f2 = self._next_event(resp)
            self.assertEqual(ev2, "snapshot")
        finally:
            self._close(resp)


# ---------------------------------------------------------------------------
# 4) 隐私守卫
# ---------------------------------------------------------------------------
class PrivacyTest(StreamCase):
    def test_progress_stream_has_no_raw_phone(self):
        resp = self._open(self.admin_client)
        seen = []
        try:
            seen.append(self._next_frame(resp))     # snapshot
            # message 里内嵌裸号：写入面已遮；读取面复遮是纵深防御
            self._insert("success", OWNER_SINGLE, RAW_PHONE,
                         f"登录 {RAW_PHONE} 完成", f"{self.day} 06:40:12")
            frame, _e, _i, _d = self._read_until(resp, lambda e, i, d: e == "progress")
            seen.append(frame)
        finally:
            self._close(resp)
        blob = "\n".join(seen)
        self.assertNotIn(RAW_PHONE, blob, "流 payload 不得出现原始手机号")
        self.assertNotIn("stream-host", blob, "executor 只回公开标签，不回主机名")

    def test_my_signin_only_pushes_self(self):
        resp = self._open(self._user_client(), path="/api/stream/my-signin")
        blobs = []
        try:
            blobs.append(self._next_frame(resp))    # 最小快照
            # 本人号一行 + 他人号一行（同一拍内）
            self._insert("success", OWNER_SINGLE, RAW_PHONE, "签到成功",
                         f"{self.day} 06:40:12")
            self._insert("success", OWNER_WORKER, OTHER_PHONE, "签到成功",
                         f"{self.day} 06:40:13")
            frame, _ev, _eid, data = self._read_until(
                resp, lambda e, i, d: e == "progress")
            blobs.append(frame)
            self.assertEqual(data.get("executor"), "single",
                             "本人流不得出现他人号的事件")
        finally:
            self._close(resp)
        blob = "\n".join(blobs)
        self.assertNotIn(RAW_PHONE, blob, "过滤用的手机号不进 payload")
        self.assertNotIn(OTHER_PHONE, blob, "他人号的原始号码不得出现")

    def test_my_signin_never_leaks_other_events(self):
        """只写他人号事件时，本人流的 progress 事件计数为 0（只有快照/心跳）。"""
        resp = self._open(self._user_client(), path="/api/stream/my-signin")
        try:
            for _ in range(4):
                frame = self._next_frame(resp)
                if frame is None:
                    break
                ev, _eid, _d = _parse(frame)
                self.assertNotEqual(ev, "progress",
                                    "本人流不得推他人号的事件: %s" % frame)
                if ev == "heartbeat":
                    break
        finally:
            self._close(resp)

    def test_my_signin_snapshot_has_no_global_aggregate(self):
        resp = self._open(self._user_client(), path="/api/stream/my-signin")
        try:
            frame = self._next_frame(resp)
            ev, _eid, data = _parse(frame)
            self.assertEqual(ev, "snapshot")
            self.assertNotIn("totals", data, "用户流快照不得含全局聚合")
            self.assertNotIn("by_executor", data, "用户流快照不得含全局聚合")
        finally:
            self._close(resp)


# ---------------------------------------------------------------------------
# 5) 鉴权与同源
# ---------------------------------------------------------------------------
class AuthTest(StreamCase):
    def test_anonymous_gets_401(self):
        c = self.webapp.create_app().test_client()
        r = c.get("/api/stream/progress")
        self.assertEqual(r.status_code, 401)

    def test_normal_user_progress_403(self):
        r = self._user_client().get("/api/stream/progress")
        self.assertEqual(r.status_code, 403)

    def test_normal_user_my_signin_ok(self):
        resp = self._open(self._user_client(), path="/api/stream/my-signin")
        try:
            self.assertEqual(resp.status_code, 200)
            frame = self._next_frame(resp)
            ev, _eid, _d = _parse(frame)
            self.assertIn(ev, ("snapshot", "heartbeat"))
        finally:
            self._close(resp)

    def test_cross_origin_rejected(self):
        r = self.admin_client.get("/api/stream/progress",
                                  headers={"Origin": "https://evil.example"})
        self.assertEqual(r.status_code, 403)

    def test_same_origin_allowed(self):
        r = self.admin_client.get("/api/stream/progress",
                                  headers={"Origin": "http://localhost"})
        self.assertEqual(r.status_code, 200)
        self._close(r)


# ---------------------------------------------------------------------------
# 6) 连接治理
# ---------------------------------------------------------------------------
class LimitsTest(StreamCase):
    def test_disabled_switch(self):
        self._env(YIBAN_SSE_ENABLED=0)
        resp = self._open(self.admin_client)
        try:
            ev, _eid, data, _f = self._next_event(resp)
            self.assertEqual(ev, "end")
            self.assertEqual(data["reason"], "disabled")
        finally:
            self._close(resp)

    def test_per_session_cap(self):
        self._env(YIBAN_SSE_SESSION_MAX=1)
        resp1 = self._open(self.admin_client)
        try:
            self._next_event(resp1)     # snapshot：登记订阅者
            resp2 = self._open(self.admin_client)
            try:
                ev, _eid, data, _f = self._next_event(resp2)
                self.assertEqual(ev, "end")
                self.assertEqual(data["reason"], "too_many_connections")
            finally:
                self._close(resp2)
        finally:
            self._close(resp1)


# ---------------------------------------------------------------------------
# 7) 审计：建连一次，不随拍增长
# ---------------------------------------------------------------------------
class AuditTest(StreamCase):
    def _audit_count(self):
        conn = db.get_conn()
        return conn.execute(
            "SELECT COUNT(*) AS n FROM audit_logs WHERE action='run_events_stream'"
        ).fetchone()["n"]

    def test_one_audit_row_per_connection(self):
        resp = self._open(self.admin_client)
        try:
            self._next_event(resp)      # snapshot
            # 跨过至少两拍（心跳），审计不得随拍增长
            self._read_until(resp, lambda e, i, d: e == "heartbeat", limit=6)
        finally:
            self._close(resp)
        self.assertLessEqual(self._audit_count(), 1,
                             "只读面留痕只在建连时一次，绝不每拍写")


# ---------------------------------------------------------------------------
# 8) 配置名册
# ---------------------------------------------------------------------------
class RegistryKeysTest(unittest.TestCase):
    EXPECTED = (
        ("YIBAN_SSE_ENABLED", True),
        ("YIBAN_SSE_POLL_SEC", 1),
        ("YIBAN_SSE_HEARTBEAT_SEC", 15),
        ("YIBAN_SSE_GAP_MAX", 500),
        ("YIBAN_SSE_SESSION_MAX", 3),
        ("YIBAN_SSE_CONNECTIONS_MAX", 10),
        ("YIBAN_SSE_IDLE_SEC", 300),
        ("YIBAN_SSE_SESSION_RECHECK_SEC", 60),
        ("YIBAN_SSE_CONNECTION_MAX_SEC", 7200),
    )

    def test_keys_registered_with_expected_defaults(self):
        from yiban import config_loader
        for key, default in self.EXPECTED:
            self.assertEqual(config_loader.default_of(key), default, key)

    def test_heartbeat_below_gunicorn_timeout(self):
        """125 §3.4：心跳必须 < gunicorn `--timeout 60`（丢一拍不触发读超时）。"""
        from yiban import config_loader
        self.assertLess(config_loader.default_of("YIBAN_SSE_HEARTBEAT_SEC"), 60)


if __name__ == "__main__":
    unittest.main()
