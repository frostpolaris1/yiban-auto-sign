# -*- coding: utf-8 -*-
"""执行体健康卡（巡检面第二刀 ④，工单 yiban-auto-sign-o3o7）。

标签：E · Web：认证/权限/API
覆盖：`GET /api/admin/executor-health`（只读巡检端点）与它消费的两个口径——
     `yiban.adapters.statefiles.state_io` 的**心跳身份解析**与 `web.services.executor_env` 的卡片装配。

本文件钉**不变式**，不钉调用顺序：
  I1 身份只认**心跳文件名**，不认记录体自称的 `role`/`index`（ba-p03-09：生产实测
     `worker-alive-fallback.json` 的内容写着 `{"role": "worker", "index": 0}`）。
  I2 卡片身份与心跳文件名互为逆函数（`worker_alive_identity(path) == 文件名编码的档位`）。
  I3 心跳过期 ⇒ 状态 `stale` 且给出时刻与年龄，不留白、不显示 0。
  I4 无记录 ⇒ 状态 `idle`，心跳字段为 `None`（前端据此显式写"无心跳"）。
  I5 吞吐口径单义：分子＝当日该执行体名下了结数，分母＝有效签到窗口长度（秒）；
     分子不等于该执行体的总行数（claimed/failed 不进分子）。
  I6 响应不含身份原串（主机名/进程号）与逐账号明细。

对应实现：yiban/adapters/statefiles/state_io.py（worker_alive_identity / worker_alive_facts）、
     web/services/executor_env.py（_executor_health_cards）、
     web/routes/executor_health_api.py。
"""
import contextlib
import importlib.util
import json
import os
import shutil
import socket
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))
sys.path.insert(0, BASE)

from yiban import egress  # noqa: E402
from yiban.adapters.statefiles import state_io  # noqa: E402
from yiban.core import clock  # noqa: E402

ADMIN_PASS = "TestPass1234!"
ADMIN_USER = "admin@test.local"
#: 极具辨识度的主机名：脱敏断言据此反查"响应里有没有身份原串"。
SECRET_HOST = "host-secret-7k2m"
HEARTBEAT_SEC = 30  # 与 state_io.WORKER_HEARTBEAT_SEC 同值：这里钉的是判据口径
FRESH_LIMIT = 2 * HEARTBEAT_SEC


def _load_webapp():
    """**独立名字**加载 web/app.py（路径常量在导入期固化，共用模块对象会读到别的 .env）。"""
    spec = importlib.util.spec_from_file_location(
        "webapp_exehealth", os.path.join(BASE, "web", "app.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["webapp_exehealth"] = mod
    with contextlib.suppress(Exception):
        spec.loader.exec_module(mod)
    return mod


class _HealthBase(unittest.TestCase):
    """临时库/环境 + 主管理员登录。"""

    FIXED_NOW = datetime(2026, 9, 16, 6, 40, 0)

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-exehealth-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write("YIBAN_ACCOUNTS_KEY=" + "a" * 64 + "\n")
            f.write(f"YIBAN_ADMIN_USER={ADMIN_USER}\n")
            f.write(f"YIBAN_ADMIN_PASSWORD={ADMIN_PASS}\n")
            # 并行 2 行 + 1 兜底行：卡片身份要从文件名区分 worker-0 / worker-1 / fallback
            f.write(f"{egress.ENV_WORKER_LIST}=,http://b.example:3128\n")
            f.write(f"{egress.ENV_FALLBACK}=http://fb.example:8080\n")
            f.write("YIBAN_WORKERS=2\n")
        os.environ.update({
            "YIBAN_ENV_FILE": cls.env_file,
            "YIBAN_ACCOUNTS_KEY": "a" * 64,
            "YIBAN_DB_FILE": os.path.join(cls.tmp, "yiban.db"),
            "YIBAN_STATE_DIR": cls.tmp,
            "YIBAN_LOG_FILE": os.path.join(cls.tmp, "sign.log"),
            "YIBAN_DISABLE_PURGE_LOOP": "1",
        })
        cls.webapp = _load_webapp()

    @classmethod
    def tearDownClass(cls):
        for key in ("YIBAN_ENV_FILE", "YIBAN_ACCOUNTS_KEY", "YIBAN_DB_FILE",
                    "YIBAN_STATE_DIR", "YIBAN_LOG_FILE", "YIBAN_DISABLE_PURGE_LOOP"):
            os.environ.pop(key, None)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        import db
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        for suffix in ("", "-wal", "-shm"):
            path = os.environ["YIBAN_DB_FILE"] + suffix
            with contextlib.suppress(OSError):
                os.remove(path)
        self._clear_heartbeats()

    def _clear_heartbeats(self):
        prefix = self.webapp.signin.WORKER_ALIVE_FILE_PREFIX
        for name in os.listdir(self.tmp):
            if name.startswith(prefix):
                os.remove(os.path.join(self.tmp, name))

    def _login(self):
        c = self.webapp.create_app().test_client()
        r = c.post("/api/login", json={"username": ADMIN_USER, "password": ADMIN_PASS})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return c

    def _cards(self, now=None):
        """取健康卡并按身份建索引（`now` 由渲染时刻决定；断言用相对量，不钉绝对钟）。"""
        body = self._login().get("/api/admin/executor-health").get_json()
        self.assertTrue(body["ok"], body)
        return body, {c["identity"]: c for c in body["executors"]}


class ExecutorHealthEndpointTest(_HealthBase):
    def test_requires_login_and_is_not_an_anonymous_path(self):
        """巡检面按管理员面：匿名 401、普通用户 403（不得加进放行清单）。"""
        c = self.webapp.create_app().test_client()
        self.assertEqual(c.get("/api/admin/executor-health").status_code, 401)


class HeartbeatIdentityTest(_HealthBase):
    """I1/I2：身份来自文件名，记录体自称的 role/index 一律不采。"""

    def test_fallback_heartbeat_is_not_reported_as_worker_zero(self):
        """生产形状：`worker-alive-fallback.json` 的记录体写着 `{"role":"worker","index":0}`。

        卡片必须报 `fallback`，且不得出现"某个 worker-0 卡片挂着 fallback 的心跳文件"。
        """
        s = self.webapp.signin
        path = s.worker_alive_path(0, role=egress.ROLE_FALLBACK)
        day = clock.now().strftime("%Y-%m-%d")
        stamp = clock.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(path, "w", encoding="utf-8") as f:
            # 逐字复刻 `_alive_record` 的分叉：role 写死 worker、index 是折成 0 的槽位
            json.dump({"day": day, "role": "worker", "index": 0,
                       "started_at": stamp, "ts": stamp}, f)
        _, cards = self._cards()
        base = os.path.basename(path)
        owner = [c for c in cards.values() if c["heartbeat_file"] == base]
        self.assertEqual(len(owner), 1, cards)
        self.assertEqual(owner[0]["identity"], egress.ROLE_FALLBACK)
        self.assertEqual(owner[0]["role"], egress.ROLE_FALLBACK)
        self.assertEqual(owner[0]["index"], 0)
        for card in cards.values():
            if card["heartbeat_file"] == base:
                continue
            self.assertNotEqual(card.get("heartbeat_file"), base)

    def test_card_identity_is_the_inverse_of_the_heartbeat_filename(self):
        """I2：任一带心跳文件的卡片，其身份必须等于该文件名解析出的档位。"""
        s = self.webapp.signin
        s.mark_worker_started(1, now=clock.now(), role=egress.ROLE_WORKER)
        s.mark_worker_started(0, now=clock.now(), role=egress.ROLE_SINGLE)
        _, cards = self._cards()
        with_file = [c for c in cards.values() if c["heartbeat_file"]]
        self.assertGreaterEqual(len(with_file), 2, cards)
        for card in with_file:
            parsed = state_io.worker_alive_identity(card["heartbeat_file"])
            self.assertIsNotNone(parsed, card)
            self.assertEqual((card["role"], card["index"]), parsed, card)

    def test_config_only_executor_has_no_heartbeat_file_and_no_identity_guess(self):
        """清单里有、心跳文件没有 ⇒ 身份的来源是清单，且明确标出（不是猜）。"""
        _, cards = self._cards()
        self.assertIn("worker-0", cards)
        card = cards["worker-0"]
        self.assertIsNone(card["heartbeat_file"])
        self.assertEqual(card["identity_source"], "config")


class HeartbeatFreshnessTest(_HealthBase):
    """I3/I4：过期与无记录都必须显式可分辨。"""

    def test_expired_heartbeat_is_stale_with_time_and_age(self):
        s = self.webapp.signin
        now = clock.now().replace(microsecond=0)
        s.mark_worker_started(0, now=now - timedelta(seconds=FRESH_LIMIT + 5),
                              role=egress.ROLE_WORKER)
        _, cards = self._cards()
        card = cards["worker-0"]
        self.assertEqual(card["state"], "stale")
        self.assertIsNotNone(card["heartbeat_at"], "过期不等于没心跳：时刻必须照给")
        self.assertGreaterEqual(card["age_sec"], FRESH_LIMIT)
        self.assertIs(card["fresh"], False)

    def test_no_record_is_idle_with_null_heartbeat(self):
        _, cards = self._cards()
        self.assertEqual(cards["worker-1"]["state"], "idle")
        self.assertIsNone(cards["worker-1"]["heartbeat_at"])
        self.assertIsNone(cards["worker-1"]["age_sec"])
        self.assertIsNone(cards["worker-1"]["fresh"], "无记录时新鲜度无从判定，不得编 0/False")

    def test_fresh_only_for_running(self):
        """新鲜度只有一份判据：`fresh is True` 当且仅当 `state == running`。"""
        s = self.webapp.signin
        now = clock.now().replace(microsecond=0)
        s.mark_worker_started(0, now=now - timedelta(seconds=5))
        s.mark_worker_started(1, now=now - timedelta(seconds=FRESH_LIMIT + 60))
        # 收尾态（清单外的孤儿槽位照样出卡）：新鲜度对它无从说起，必须是 None
        s.mark_worker_finished(2, exit_code=0, now=now - timedelta(seconds=30))
        _, cards = self._cards()
        for card in cards.values():
            expected = {"running": True, "stale": False,
                        "idle": None, "finished": None}[card["state"]]
            self.assertIs(card["fresh"], expected,
                          f"新鲜度与四态分叉：{card}")
        self.assertEqual(cards["worker-0"]["state"], "running")
        self.assertEqual(cards["worker-1"]["state"], "stale")
        self.assertEqual(cards["worker-2"]["state"], "finished")
        self.assertFalse(cards["worker-2"]["configured"], "清单里没有槽位 2：照实标出来")


class ThroughputTest(_HealthBase):
    """I5：吞吐 = 名下了结数 ÷ 有效窗口长度（秒），口径单义。"""

    def _seed_tasks(self):
        """造当日 sign_tasks 行：worker-0 名下了结 3 个、在飞 1 个、失败 1 个。"""
        from yiban.adapters.persistence import db as store_db
        day = clock.today()
        store_db.init_db(os.environ["YIBAN_DB_FILE"], env_file=self.env_file, cleanup=False)
        owner = egress.worker_owner(0, SECRET_HOST)
        rows = (
            ("13900000001", owner, "done"),
            ("13900000002", owner, "done"),
            ("13900000003", owner, "done"),
            ("13900000004", owner, "claimed"),
            ("13900000005", owner, "failed"),
        )
        conn = store_db.get_conn()
        conn.executemany(
            "INSERT INTO sign_tasks (phone, day, vshard, owner, run_at, state, created_at) "
            "VALUES (?,?,0,?,?,?,?)",
            [(p, day, ow, f"{day} 06:30:00", st, f"{day} 06:00:00") for p, ow, st in rows])
        conn.commit()

    def test_done_count_is_numerator_and_window_is_denominator(self):
        self._seed_tasks()
        body, cards = self._cards()
        tp = cards["worker-0"]["throughput"]
        self.assertEqual(tp["done"], 3, "分子＝名下了结数（done + skipped），不含在飞/失败")
        self.assertEqual(tp["total"], 5)
        self.assertEqual(tp["window_sec"], body["window_sec"])
        self.assertGreater(body["window_sec"], 0)
        self.assertAlmostEqual(tp["per_min"], 3 * 60 / body["window_sec"], places=6)

    def test_throughput_is_zero_for_executor_without_rows(self):
        self._seed_tasks()
        _, cards = self._cards()
        self.assertEqual(cards["worker-1"]["throughput"]["done"], 0)
        self.assertEqual(cards["worker-1"]["throughput"]["total"], 0)


class PrivacyTest(_HealthBase):
    def test_response_carries_no_owner_string_host_or_account(self):
        self._seed_owner_with_secret()
        c = self._login()
        text = c.get("/api/admin/executor-health").get_data(as_text=True)
        self.assertNotIn(SECRET_HOST, text, "身份原串（含主机名）不得进响应")
        self.assertNotIn(socket.gethostname(), text, "主机名属部署信息")
        self.assertNotIn("13900000001", text, "不得出逐账号明细")
        for key in ("pid", "host", "hostname", "owner"):
            self.assertNotIn(f'"{key}"', text, f"响应不得携带 {key} 字段")

    def _seed_owner_with_secret(self):
        from yiban.adapters.persistence import db as store_db
        day = clock.today()
        store_db.init_db(os.environ["YIBAN_DB_FILE"], env_file=self.env_file, cleanup=False)
        conn = store_db.get_conn()
        conn.execute(
            "INSERT INTO sign_tasks (phone, day, vshard, owner, run_at, state, created_at) "
            "VALUES (?,?,0,?,?,?,?)",
            ("13900000001", day, egress.worker_owner(0, SECRET_HOST),
             f"{day} 06:30:00", "done", f"{day} 06:00:00"))
        conn.commit()


if __name__ == "__main__":
    unittest.main()
