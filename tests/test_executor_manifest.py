# -*- coding: utf-8 -*-
"""**执行体清单**（`YIBAN_EXECUTORS`）的模型、迁移等价与行接口断言。

标签：B · 调度：领取/队列/执行体
覆盖：执行体清单的模型层（解析/序列化/槽位只增不复用/上限
   63/兜底唯一/停用行语义）、旧三键各形态的迁移等价与只读回退、接口层的首次读迁移写回与以清单为准、删当前最大槽位后「真被用过」不复用、claims.owners_since
   的保留期口径、行 CRUD
   与权限脱敏、单行写入不影响他行、拉起列表与槽位号贯穿身份/锁/心跳、派发监督进程前的周末/暂停/补签轮闸门。
对应实现：yiban/egress.py（manifest
   读写、legacy_rows、apply_legacy_config、manifest_state、SLOT_MAX）、web/app.py
   的执行体行接口、yiban/store/claims.py（owners_since）、yiban/engine/runner.py
   与 workers.py（拉起列表、槽位透传、派发前的门）。
关键断言：迁移必须逐字等价（顺序、空位=直连、兜底位置、worker
   数量），旧键在写回后再留一个版本周期——不一致的表现是「升级后出口串了」。槽位只增不复用，且删掉的号若真出现在领取历史里就不得再发出去（否则新执行体顶用死执行体的身份，归属统计串人）；从没用过的号照旧复用。disabled
   行保留出口、不进拉起列表、不计入建议值分母、不报存活。清单与旧键并存时以清单为准，但旧接口在清单模式下也必须同步维护清单。周末/暂停/补签判定必须排在派发监督进程之前。
依赖：临时目录 + 独立模块名加载的 Flask test client + SimpleNamespace
   替身；worker_presence / fallback_alive
   读的是临时状态目录里的本地文件。全部离线，无 skip。

旧口径是"一个数量（`YIBAN_WORKERS`）+ 一整条逗号列表（`YIBAN_PROXY_LIST`）+
单独兜底出口（`YIBAN_PROXY_FALLBACK`）"，表达不了"停用某一行"与"删中间行不重排"。
新清单是**单键 JSON 数组**，每个执行体一行：`{"slot", "type", "proxy"}`。

"""
import contextlib
import importlib.util
import json
import logging
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

from yiban import clock, egress  # noqa: E402

logging.getLogger("yiban").addHandler(logging.NullHandler())

ADMIN_PASS = "TestPass1234!"
SECRET_PROXY = "http://svcuser:svcp@proxy1.example:8080"

#: 固定业务时刻（周三 06:40）；派发路径用例把时钟钉在非周末，否则跑测当天是周六/周日
#: 时「周末签到未开启」的提前门会先拦下（那是另一组用例的主题，不该让这组周末变红）
WEEKDAY_06_40 = datetime(2026, 9, 2, 6, 40)


def _manifest(*rows):
    """紧凑 JSON 串（与 `egress.dump_manifest` 同一写法，便于手写 .env 用例）。"""
    return json.dumps(list(rows), ensure_ascii=False, separators=(",", ":")) # 紧凑写法与 dump_manifest 一致，手写的 .env 才能逐字比对


# ---------------------------------------------------------------------------
# 纯函数：迁移等价
# ---------------------------------------------------------------------------
#: 旧三键的各种形态（覆盖空位、userinfo、循环取用、只配单出口、空白列表、占满槽位）
LEGACY_CASES = ( # 这批形态就是升级路径的全部已知输入，少一种等于没测那条路
    {},
    {"YIBAN_WORKERS": "1"},
    {"YIBAN_WORKERS": "3", "YIBAN_PROXY_LIST": f"{SECRET_PROXY},,http://c.example:3128"},
    {"YIBAN_WORKERS": "5", "YIBAN_PROXY_LIST": "http://a:1,http://b:2"},
    {"YIBAN_WORKERS": "2", "YIBAN_PROXY": "http://solo:9"},
    {"YIBAN_WORKERS": "2", "YIBAN_PROXY_LIST": "http://a:1",
     "YIBAN_PROXY_FALLBACK": "http://fb:9"},
    {"YIBAN_WORKERS": "4", "YIBAN_PROXY_LIST": "   ",
     "YIBAN_PROXY": "http://solo:9"},
    {"YIBAN_WORKERS": "64", "YIBAN_PROXY_LIST": "http://a:1",
     "YIBAN_PROXY_FALLBACK": "http://fb:9"},
    {"YIBAN_WORKERS": "0"},                       # 非法/0 → 旧口径下限 1
    {"YIBAN_WORKERS": "many"},                    # 非整数 → 旧口径默认 1
)


class MigrationEquivalenceTest(unittest.TestCase):
    """迁移前后行为**逐字不变**：顺序、空位=直连、兜底位置、worker 数量。"""

    def test_worker_proxies_match_legacy_resolve(self):
        for env in LEGACY_CASES:
            with self.subTest(env=env):
                n = egress.legacy_worker_count(env) # 先拿旧口径的人数当基准，再逐个比迁移结果，而不是反过来
                rows = egress.legacy_rows(env)
                migrated = egress.worker_rows(rows) # 只取 worker 行：兜底行不占 worker 序号，混进来会把槽位比歪
                self.assertEqual([r["slot"] for r in migrated], list(range(n)),
                                 "槽位必须就是旧的 0..N-1（顺序不变）")
                for r in migrated:
                    self.assertEqual(r["proxy"],
                                     egress.resolve(egress.ROLE_WORKER, r["slot"], env),
                                     "第 %d 个执行体的出口必须与旧 resolve 逐字一致"
                                     % r["slot"])

    def test_fallback_row_matches_legacy_resolve(self):
        for env in LEGACY_CASES:
            with self.subTest(env=env):
                rows = egress.legacy_rows(env)
                fb = egress.fallback_row(rows)
                legacy = egress.resolve(egress.ROLE_FALLBACK, env=env)
                if len(egress.worker_rows(rows)) > egress.SLOT_MAX:
                    # 64 个并行执行体占满 0..63：旧口径的兜底不占槽位，清单里也不产出
                    self.assertIsNone(fb)
                    continue
                self.assertIsNotNone(fb, "兜底行必须紧随 worker 行之后（位置不变）")
                self.assertEqual(fb["slot"], egress.legacy_worker_count(env))
                self.assertEqual(fb["proxy"], legacy)

    def test_assignments_and_describe_match(self):
        """`assignments(count)` 的三元组（下标/原串/描述串）在迁移后逐字重现。"""
        for env in LEGACY_CASES:
            with self.subTest(env=env):
                old = egress.assignments(egress.legacy_worker_count(env), env)
                rows = egress.legacy_rows(env)
                new = [(r["slot"], r["proxy"], egress.describe(r["proxy"]))
                       for r in egress.worker_rows(rows)]
                self.assertEqual(new, old)

    def test_manifest_state_reports_write_once_then_stops(self):
        env = dict(LEGACY_CASES[2])
        rows, needs_write = egress.manifest_state(env)
        self.assertTrue(needs_write, "旧键存在且清单缺失 → 需要一次性迁移写回")
        # 写回之后的 .env（清单键在场）：不再需要写回，读到的行完全一致
        env[egress.ENV_MANIFEST] = egress.dump_manifest(rows)
        again, needs_write = egress.manifest_state(env)
        self.assertFalse(needs_write)
        self.assertEqual(again, rows)

    def test_missing_manifest_and_no_legacy_keys_does_not_write(self):
        """新部署不该因为读一次就长出配置键（默认单执行体形态在内存里给）。"""
        rows, needs_write = egress.manifest_state({})
        self.assertFalse(needs_write)
        self.assertEqual(egress.worker_rows(rows), [{"slot": 0, "type": "worker", "proxy": ""}])
        self.assertEqual(egress.fallback_row(rows)["slot"], 1)

    def test_broken_manifest_falls_back_read_only(self):
        """清单键在但 JSON 坏 → 按旧键读，但**不写回**（别把能手工修的配置抹掉）。"""
        env = {"YIBAN_WORKERS": "2", "YIBAN_PROXY_LIST": "http://a:1,http://b:2",
               egress.ENV_MANIFEST: "{不是 JSON}"}
        rows, needs_write = egress.manifest_state(env)
        self.assertFalse(needs_write)
        self.assertEqual([r["proxy"] for r in egress.worker_rows(rows)],
                         ["http://a:1", "http://b:2"])


class ManifestModelTest(unittest.TestCase):
    """清单解析/序列化/行操作（纯函数）。"""

    def test_roundtrip_and_compactness(self):
        rows = egress.legacy_rows(LEGACY_CASES[2])
        text = egress.dump_manifest(rows)
        self.assertNotIn("\n", text)
        self.assertNotIn(" ", text, "紧凑写法：整条进 .env 的一行，不引入可被 strip 的空白")
        self.assertEqual(egress.parse_manifest(text), rows)

    def test_parse_skips_invalid_items_and_later_wins_on_duplicate(self):
        raw = _manifest(
            {"slot": 0, "type": "worker", "proxy": "http://a:1"},
            {"slot": 99, "type": "worker", "proxy": "http://bad:1"},     # 超范围 → 跳过
            {"slot": 1, "type": "nonsense", "proxy": "x"},                # 类型不认识 → 跳过
            "不是对象",                                                    # 非对象 → 跳过
            {"slot": 0, "type": "disabled", "proxy": "http://a:1"},       # 同 slot 后写者胜
            {"slot": 2, "type": "fallback", "proxy": None},               # None = 直连
        )
        rows = egress.parse_manifest(raw)
        self.assertEqual([(r["slot"], r["type"]) for r in rows],
                         [(0, "disabled"), (2, "fallback")])
        self.assertEqual(rows[-1]["proxy"], "")

    def test_parse_none_for_missing_or_broken(self):
        for raw in (None, "", "   ", "not json", "{}", '"str"', "[1,2]x"):
            with self.subTest(raw=raw):
                self.assertIsNone(egress.parse_manifest(raw))
        self.assertEqual(egress.parse_manifest("[]"), [])   # 合法空数组 = 清单在场但无行

    def test_slot_only_increases_and_no_renumber(self):
        rows = []
        for _ in range(3):
            rows = egress.add_row(rows, egress.TYPE_WORKER, "http://p:1")
        self.assertEqual([r["slot"] for r in rows], [0, 1, 2])
        # 删中间行：其余行不重排，新行拿到 max + 1
        rows = egress.delete_row(rows, 1)
        self.assertEqual([r["slot"] for r in rows], [0, 2])
        rows = egress.add_row(rows, egress.TYPE_WORKER, "http://q:1")
        self.assertEqual([r["slot"] for r in rows], [0, 2, 3])

    def test_slot_max_is_63(self):
        rows = [{"slot": egress.SLOT_MAX, "type": egress.TYPE_WORKER, "proxy": ""}]
        with self.assertRaises(ValueError) as ctx:
            egress.add_row(rows, egress.TYPE_WORKER, "")
        self.assertIn("63", str(ctx.exception))

    def test_fallback_at_most_one_row(self):
        rows = egress.add_row([], egress.TYPE_FALLBACK, "http://fb:1")
        with self.assertRaises(ValueError):
            egress.add_row(rows, egress.TYPE_FALLBACK, "http://fb:2")
        # 改行成 fallback 同样受约束；把**原来那行**改掉则允许
        rows = egress.add_row(rows, egress.TYPE_WORKER, "")
        with self.assertRaises(ValueError):
            egress.update_row(rows, 1, rtype=egress.TYPE_FALLBACK)
        rows = egress.update_row(rows, 0, rtype=egress.TYPE_DISABLED)
        rows = egress.update_row(rows, 1, rtype=egress.TYPE_FALLBACK)
        self.assertEqual(egress.fallback_row(rows)["slot"], 1)

    def test_disabled_row_keeps_egress_and_leaves_launch_list(self):
        rows = [{"slot": 0, "type": "worker", "proxy": "http://w0:1"},
                {"slot": 1, "type": "worker", "proxy": "http://w1:2"}]
        self.assertEqual(egress.launch_slots({egress.ENV_MANIFEST: egress.dump_manifest(rows)}),
                         [0, 1])
        disabled = egress.update_row(rows, 1, rtype=egress.TYPE_DISABLED)
        # 出口仍保留（重新启用后出口还在）
        self.assertEqual(egress.row_by_slot(disabled, 1)["proxy"], "http://w1:2")
        self.assertEqual(egress.executor_label(egress.TYPE_DISABLED, 1), "已停用")
        self.assertEqual(egress.launch_slots(
            {egress.ENV_MANIFEST: egress.dump_manifest(disabled)}), [0],
            "停用行不参与分配、不拉起")
        # 槽位仍占着：新增行不会复用 1
        grown = egress.add_row(disabled, egress.TYPE_WORKER, "")
        self.assertEqual([r["slot"] for r in grown], [0, 1, 2])
        # 重新启用后出口照旧
        revived = egress.update_row(disabled, 1, rtype=egress.TYPE_WORKER)
        self.assertEqual(revived[1]["proxy"], "http://w1:2")

    def test_apply_legacy_config_keeps_slots_and_appends_with_max_plus_one(self):
        rows = [{"slot": 0, "type": "worker", "proxy": "old0"},
                {"slot": 2, "type": "worker", "proxy": "old2"},
                {"slot": 5, "type": "disabled", "proxy": "kept"},
                {"slot": 6, "type": "fallback", "proxy": "oldfb"}]
        env = {"YIBAN_WORKERS": "5", "YIBAN_PROXY_LIST": "p1,p2,p3,p4,p5",
               "YIBAN_PROXY_FALLBACK": "newfb"}
        out = egress.apply_legacy_config(rows, env)
        by_slot = {r["slot"]: r for r in out}
        self.assertEqual((by_slot[0]["proxy"], by_slot[2]["proxy"]), ("p1", "p2"),
                         "已存在的槽位保留、按顺序吃新出口")
        self.assertEqual([s for s in by_slot if by_slot[s]["type"] == "worker"],
                         [0, 2, 7, 8, 9], "新增的执行体拿 max + 1（只增不复用）")
        self.assertEqual(by_slot[5]["type"], "disabled", "停用行不因旧接口写入而丢失")
        self.assertEqual(by_slot[6]["proxy"], "newfb")
        # 执行体数变少：多出来的 worker 行删除，槽位不重排
        out2 = egress.apply_legacy_config(rows, {"YIBAN_WORKERS": "1",
                                                 "YIBAN_PROXY_LIST": "only"})
        self.assertEqual([(r["slot"], r["type"]) for r in out2],
                         [(0, "worker"), (5, "disabled"), (6, "fallback")])


# ---------------------------------------------------------------------------
# 接口层
# ---------------------------------------------------------------------------
def _load_webapp():
    """**独立名字**加载 web/app.py（它在导入期把路径读成模块级常量，与别的测试文件
    共用同一模块对象会读到另一个 .env——单跑绿、全量红）。"""
    spec = importlib.util.spec_from_file_location(
        "webapp_manifest", os.path.join(BASE, "web", "app.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["webapp_manifest"] = mod
    with contextlib.suppress(Exception):
        spec.loader.exec_module(mod)
    return mod


class _WebBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-manifest-")
        cls.env_file = os.path.join(cls.tmp, ".env")
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
        for k in ("YIBAN_ENV_FILE", "YIBAN_ACCOUNTS_KEY", "YIBAN_DB_FILE",
                  "YIBAN_STATE_DIR", "YIBAN_LOG_FILE", "YIBAN_DISABLE_PURGE_LOOP"):
            os.environ.pop(k, None)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        import db
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        for suffix in ("", "-wal", "-shm"):
            path = os.environ["YIBAN_DB_FILE"] + suffix
            if os.path.exists(path):
                os.remove(path)
        self._write_env()

    def _write_env(self, *extra_lines):
        """重写共享 `.env`（每个用例自带前置配置，避免"单跑绿、全量红"）。"""
        lines = ["YIBAN_ACCOUNTS_KEY=" + "a" * 64,
                 "YIBAN_ADMIN_USER=admin@test.local",
                 f"YIBAN_ADMIN_PASSWORD={ADMIN_PASS}",
                 *extra_lines]
        with open(self.env_file, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

    def _read_env(self):
        with open(self.env_file, encoding="utf-8") as f:
            return dict(ln.split("=", 1) for ln in f.read().splitlines()
                        if "=" in ln and not ln.startswith("#"))

    def _login(self, user="admin@test.local", password=ADMIN_PASS):
        c = self.webapp.create_app().test_client()
        r = c.post("/api/login", json={"username": user, "password": password})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        c.csrf = c.get("/api/me").get_json()["csrf_token"]
        return c

    def _get(self, c):
        r = c.get("/api/scheduler/executors")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return r.get_json()


class MigrationWritebackTest(_WebBase):
    """接口层：首次读到旧键 → 迁移写回清单；旧字段与旧键都还在。"""

    LEGACY = ("YIBAN_WORKERS=3",
              f"YIBAN_PROXY_LIST={SECRET_PROXY},,http://c.example:3128",
              "YIBAN_PROXY_FALLBACK=http://fbuser:fbpw@fb.example:8080")

    def test_get_migrates_and_keeps_legacy_keys(self):
        self._write_env(*self.LEGACY)
        self._get(self._login())
        env = self._read_env()
        # 新键写回、旧三键逐字保留（一个版本周期）
        self.assertIn(egress.ENV_MANIFEST, env)
        self.assertEqual(env["YIBAN_WORKERS"], "3")
        self.assertEqual(env["YIBAN_PROXY_LIST"], f"{SECRET_PROXY},,http://c.example:3128")
        self.assertEqual(env["YIBAN_PROXY_FALLBACK"], "http://fbuser:fbpw@fb.example:8080")
        rows = egress.parse_manifest(env[egress.ENV_MANIFEST])
        self.assertEqual([(r["slot"], r["type"]) for r in rows],
                         [(0, "worker"), (1, "worker"), (2, "worker"), (3, "fallback")])
        self.assertEqual(rows[1]["proxy"], "", "空位迁移成空串=直连")

    def test_get_fields_match_old_semantics(self):
        self._write_env(*self.LEGACY)
        body = self._get(self._login())
        self.assertEqual(body["workers"]["configured"], 3)
        self.assertEqual([a["index"] for a in body["workers"]["assignments"]], [0, 1, 2])
        self.assertEqual([a["egress"] for a in body["workers"]["assignments"]],
                         ["http://proxy1.example:8080", "直连（本机出口）",
                          "http://c.example:3128"])
        self.assertEqual(body["fallback"]["egress"], "http://fb.example:8080")
        raw = json.dumps(body, ensure_ascii=False)
        for secret in ("svcuser", "svcp", "fbuser", "fbpw"):
            self.assertNotIn(secret, raw, "响应不得出现任何凭据原文")

    def test_executors_contains_every_row(self):
        self._write_env(*self.LEGACY)
        body = self._get(self._login())
        got = body["executors"]
        self.assertEqual([(e["slot"], e["type"]) for e in got],
                         [(0, "worker"), (1, "worker"), (2, "worker"), (3, "fallback")])
        self.assertEqual(got[0]["egress"], "http://proxy1.example:8080")
        self.assertEqual(got[0]["label"], "并行执行体 #1")
        self.assertEqual(got[3]["label"], "故障转移")
        # worker 行带存活四态；fallback 行的存活在 fallback.* 里（字段在、值为 null）
        for e in got[:3]:
            self.assertIn(e["state"], ("running", "finished", "idle", "stale"))
            self.assertIn("last_seen_at", e)
        self.assertIsNone(got[3]["state"])
        self.assertIsNone(got[3]["last_seen_at"])
        self.assertEqual(body["workers"]["env_keys"]["manifest"], egress.ENV_MANIFEST)

    def test_second_get_does_not_rewrite_manifest(self):
        self._write_env(*self.LEGACY)
        c = self._login()
        self._get(c)
        first = self._read_env()[egress.ENV_MANIFEST]
        self._get(c)
        self.assertEqual(self._read_env()[egress.ENV_MANIFEST], first,
                         "迁移只发生一次；清单在场后读接口不再改写它")

    def test_all_rows_disabled_falls_back_to_single_executor_shape(self):
        """清单里一个 worker 行都没有（全停用/删除）→ 回退单执行体形态，出口读 YIBAN_PROXY。

        `workers.configured` 按契约仍 ≥1；停用行的出口不参与分配，故显示的出口必须是
        **实际运行**的单执行体用的 `YIBAN_PROXY`，不是某行停用行留下的出口。
        """
        rows = [{"slot": 0, "type": "disabled", "proxy": "http://disabled:1"},
                {"slot": 1, "type": "fallback", "proxy": "http://fb:2"}]
        self._write_env("YIBAN_PROXY=http://solo:9",
                        f"{egress.ENV_MANIFEST}={_manifest(*rows)}")
        body = self._get(self._login())
        self.assertEqual(body["workers"]["configured"], 1)
        self.assertEqual([(a["index"], a["egress"]) for a in body["workers"]["assignments"]],
                         [(0, "http://solo:9")])
        self.assertEqual([e["type"] for e in body["executors"]], ["disabled", "fallback"])

    def test_manifest_wins_over_stale_legacy_keys(self):
        """清单与旧键并存 → **以清单为准**（旧键只作回退读取）。"""
        rows = [{"slot": 0, "type": "worker", "proxy": "http://manifest:1"},
                {"slot": 4, "type": "fallback", "proxy": "http://mfb:2"}]
        self._write_env("YIBAN_WORKERS=3",
                        "YIBAN_PROXY_LIST=http://stale:9,http://stale2:9",
                        "YIBAN_PROXY_FALLBACK=http://stalefb:9",
                        f"{egress.ENV_MANIFEST}={_manifest(*rows)}")
        body = self._get(self._login())
        self.assertEqual(body["workers"]["configured"], 1)
        self.assertEqual([(a["index"], a["egress"]) for a in body["workers"]["assignments"]],
                         [(0, "http://manifest:1")])
        self.assertEqual(body["fallback"]["egress"], "http://mfb:2")


class SlotNeverReusedAfterDeleteTest(_WebBase):
    """删掉**当前最大**那一行之后，那个号若**真被用过**（领取历史里有）就不得再发出去。

    为什么单靠 `next_slot`（最大值 + 1）不够：删掉最大行后它会立刻把刚空出来的号再发一次，
    而那个号在 `sign_claims.owner` 里已有历史——新建的执行体会在页面上显示成前任的归属
    （账号列表的"上次实领"按槽位解析身份串）。故追加接口再按领取历史抬一次下限。
    """

    def _env_with_two_rows(self):
        self._write_env(
            f"{egress.ENV_MANIFEST}=" + _manifest(
                {"slot": 0, "type": "worker", "proxy": "http://w0:1"},
                {"slot": 1, "type": "worker", "proxy": "http://w1:1"}))

    def _claim_history(self, slot):
        """造一条领取历史（业务日=今天，落在保留期内），身份串用真实格式。"""
        import db
        conn = db.get_conn()
        with db._conn_lock:
            conn.execute(
                "INSERT INTO sign_claims (phone, day, owner, claimed_at, heartbeat_at, "
                "state, result, attempts) VALUES (?, ?, ?, ?, ?, 'done', '', 0)",
                ("13800138000", clock.today(), egress.worker_owner(slot, "testhost"),
                 clock.ts(), clock.ts()))
            conn.commit()

    def _post(self, c, payload, password=ADMIN_PASS):
        body = dict(payload)
        if password is not None:
            body.setdefault("confirm_password", password)
        return c.post("/api/scheduler/executors/rows", json=body,
                      headers={"X-CSRF-Token": c.csrf})

    def _delete(self, c, slot, password=ADMIN_PASS):
        body = {} if password is None else {"confirm_password": password}
        return c.delete(f"/api/scheduler/executors/rows/{slot}", json=body,
                        headers={"X-CSRF-Token": c.csrf})

    def test_deleted_slot_with_history_is_not_handed_out_again(self):
        self._env_with_two_rows()
        c = self._login()
        self._claim_history(1)          # 1 号槽位真跑过
        self.assertEqual(self._delete(c, 1).status_code, 200)
        got = self._post(c, {"proxy": "http://new:1"}).get_json()
        self.assertGreater(got["slot"], 1,
                           "1 号在领取历史里出现过，不得再发给新行（会被显示成前任的归属）")

    def test_deleted_slot_without_history_may_be_reused(self):
        """反向控制：从没用过的号照旧复用（不白白烧号）——这是**刻意**的行为。"""
        self._env_with_two_rows()
        c = self._login()
        self.assertEqual(self._delete(c, 1).status_code, 200)
        got = self._post(c, {"proxy": "http://new:1"}).get_json()
        self.assertEqual(got["slot"], 1)


class OwnersSinceTest(_WebBase):
    """`claims.owners_since`：保留期内的身份串（去重、升序），保留期外的不算。"""

    def _insert(self, phone, day, owner):
        import db
        conn = db.get_conn()
        with db._conn_lock:
            conn.execute(
                "INSERT INTO sign_claims (phone, day, owner, claimed_at, heartbeat_at, "
                "state, result, attempts) VALUES (?, ?, ?, ?, ?, 'done', '', 0)",
                (phone, day, owner, clock.ts(), clock.ts()))
            conn.commit()

    def test_reads_distinct_owners_in_window(self):
        import db
        today = clock.today()
        for phone, owner in (("13800138000", "worker-0@h"),
                            ("13800138001", "worker-0@h"),
                            ("13800138002", "fallback@h")):
            self._insert(phone, today, owner)
        self.assertEqual(db.claim_owners_since(), ["fallback@h", "worker-0@h"])

    def test_rows_outside_retention_are_ignored(self):
        import datetime as _dt

        import db
        old_day = (clock.now()
                   - _dt.timedelta(days=db.CLAIM_RETENTION_DAYS + 1)).strftime("%Y-%m-%d")
        self._insert("13800138000", old_day, "worker-7@h")
        self.assertEqual(db.claim_owners_since(), [],
                         "保留期外的记录不参与槽位下限（展示口径同样读不到它）")


class RowsCrudTest(_WebBase):
    """行的增/改/删：槽位只增不复用、上限 63、兜底唯一。"""

    def _base_env(self):
        self._write_env("YIBAN_WORKERS=1",
                        "YIBAN_PROXY_LIST=http://only:1",
                        f"{egress.ENV_MANIFEST}={_manifest({'slot': 0, 'type': 'worker', 'proxy': 'http://w0:1'})}")

    def _post(self, c, payload, password=ADMIN_PASS):
        """追加行：默认带 `confirm_password`（2026-09-17 起写操作要口令门）。"""
        body = dict(payload)
        if password is not None:
            body.setdefault("confirm_password", password)
        return c.post("/api/scheduler/executors/rows", json=body,
                      headers={"X-CSRF-Token": c.csrf})

    def _put(self, c, slot, payload, password=ADMIN_PASS):
        body = dict(payload)
        if password is not None:
            body.setdefault("confirm_password", password)
        return c.put(f"/api/scheduler/executors/rows/{slot}", json=body,
                     headers={"X-CSRF-Token": c.csrf})

    def _delete(self, c, slot, password=ADMIN_PASS):
        body = {} if password is None else {"confirm_password": password}
        return c.delete(f"/api/scheduler/executors/rows/{slot}", json=body,
                        headers={"X-CSRF-Token": c.csrf})

    def test_append_uses_max_plus_one_and_survives_middle_delete(self):
        self._base_env()
        c = self._login()
        self.assertEqual(self._post(c, {"proxy": "http://w1:1"}).get_json()["slot"], 1)
        self.assertEqual(self._post(c, {"proxy": "http://w2:1"}).get_json()["slot"], 2)
        # 删中间行：不重排，新行 = max + 1
        self.assertEqual(self._delete(c, 1).status_code, 200)
        r = self._post(c, {"proxy": "http://w3:1"})
        self.assertEqual(r.get_json()["slot"], 3)
        rows = egress.parse_manifest(self._read_env()[egress.ENV_MANIFEST])
        self.assertEqual([x["slot"] for x in rows], [0, 2, 3])

    def test_append_rejected_at_slot_cap(self):
        rows = [{"slot": i, "type": "worker", "proxy": ""} for i in range(egress.SLOT_MAX + 1)]
        self._write_env(f"{egress.ENV_MANIFEST}={_manifest(*rows)}")
        r = self._post(self._login(), {})
        self.assertEqual(r.status_code, 400)
        self.assertIn("63", r.get_json()["error"])

    def test_fallback_second_row_rejected(self):
        self._write_env(f"{egress.ENV_MANIFEST}={_manifest({'slot': 0, 'type': 'fallback', 'proxy': ''})}")
        r = self._post(self._login(), {"type": "fallback"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("兜底", r.get_json()["error"])

    def test_disabled_row_not_launched_not_counted_not_alive(self):
        self._base_env()
        c = self._login()
        self._post(c, {"proxy": "http://w1:1"})            # slot 1
        r = self._put(c, 1, {"type": "disabled"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json()["type"], "disabled")
        body = self._get(c)
        # 不计入 configured（建议值分母只数 worker）、不在拉起列表
        self.assertEqual(body["workers"]["configured"], 1)
        self.assertEqual([a["index"] for a in body["workers"]["assignments"]], [0])
        # 接口回 disabled，且不报存活
        disabled = next(e for e in body["executors"] if e["slot"] == 1)
        self.assertEqual(disabled["type"], "disabled")
        self.assertEqual(disabled["egress"], "http://w1:1", "停用保留出口")
        self.assertEqual(disabled["label"], "已停用")
        self.assertIsNone(disabled["state"], "停用行不报存活（字段照给、值为 null）")
        self.assertIsNone(disabled["last_seen_at"])
        # 拉起源（清单）里该行确实不在 worker 列表里
        rows = egress.parse_manifest(self._read_env()[egress.ENV_MANIFEST])
        self.assertEqual([x["slot"] for x in egress.worker_rows(rows)], [0])
        # 重新启用后出口还在
        self._put(c, 1, {"type": "worker"})
        body = self._get(c)
        self.assertEqual([a["index"] for a in body["workers"]["assignments"]], [0, 1])
        self.assertEqual(body["executors"][1]["egress"], "http://w1:1")

    def test_delete_and_missing_slot_errors(self):
        self._base_env()
        c = self._login()
        r = self._delete(c, 0)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["type"], "worker")
        self.assertEqual(egress.parse_manifest(self._read_env()[egress.ENV_MANIFEST]), [])
        for call in (lambda: self._delete(c, 0),
                     lambda: self._put(c, 0, {"type": "worker"})):
            with self.subTest(call=call):
                r = call()
                self.assertEqual(r.status_code, 400)
                self.assertIn("不在执行体清单", r.get_json()["error"])

    def test_update_requires_a_field(self):
        self._base_env()
        r = self._put(self._login(), 0, {})
        self.assertEqual(r.status_code, 400)


class RowIsolationTest(_WebBase):
    """单行写入不影响其他行的出口（与既有"按序号写单段"同一纪律）。"""

    def test_put_row_keeps_other_rows_verbatim(self):
        rows = [{"slot": 0, "type": "worker", "proxy": "http://u1:p1@a.example:1"},
                {"slot": 3, "type": "worker", "proxy": "http://u2:p2@b.example:2"},
                {"slot": 9, "type": "fallback", "proxy": "http://u3:p3@c.example:3"}]
        self._write_env(f"{egress.ENV_MANIFEST}={_manifest(*rows)}")
        c = self._login()
        r = c.put("/api/scheduler/executors/rows/3",
                  json={"proxy": "http://u4:p4@d.example:4",
                        "confirm_password": ADMIN_PASS},
                  headers={"X-CSRF-Token": c.csrf})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json()["egress"], "http://d.example:4")
        stored = egress.parse_manifest(self._read_env()[egress.ENV_MANIFEST])
        self.assertEqual(stored[0]["proxy"], "http://u1:p1@a.example:1", "第 1 行逐字未变")
        self.assertEqual(stored[2]["proxy"], "http://u3:p3@c.example:3", "兜底行逐字未变")
        self.assertEqual(stored[1]["proxy"], "http://u4:p4@d.example:4")
        body = self._get(c)
        raw = json.dumps(body, ensure_ascii=False)
        for secret in ("u1:p1", "u2:p2", "u3:p3", "u4:p4"):
            self.assertNotIn(secret, raw, "接口只回脱敏描述串")

    def test_legacy_slot_endpoint_maintains_manifest(self):
        """旧单段接口在清单模式下也必须生效（不能写旧键后被"以清单为准"盖过）。"""
        rows = [{"slot": 0, "type": "worker", "proxy": "http://old:1"},
                {"slot": 1, "type": "fallback", "proxy": "http://oldfb:1"}]
        self._write_env("YIBAN_WORKERS=1",
                        f"{egress.ENV_MANIFEST}={_manifest(*rows)}")
        c = self._login()
        r = c.put("/api/scheduler/executors/workers/0",
                  json={"egress": "http://u:p@new.example:2",
                        "confirm_password": ADMIN_PASS},
                  headers={"X-CSRF-Token": c.csrf})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json()["egress"], "http://new.example:2")
        self.assertEqual(self._get(c)["workers"]["assignments"][0]["egress"],
                         "http://new.example:2")
        r = c.put("/api/scheduler/executors/fallback",
                  json={"egress": "", "confirm_password": ADMIN_PASS},
                  headers={"X-CSRF-Token": c.csrf})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(self._get(c)["fallback"]["egress"], "直连（本机出口）")
        stored = egress.parse_manifest(self._read_env()[egress.ENV_MANIFEST])
        self.assertEqual(stored[0]["proxy"], "http://u:p@new.example:2")
        self.assertEqual(stored[1]["proxy"], "")

    def test_legacy_whole_write_maintains_manifest(self):
        """旧整条写入在清单模式下同步维护清单（否则 GET 以清单为准、保存看着没生效）。"""
        rows = [{"slot": 0, "type": "worker", "proxy": "http://old:1"},
                {"slot": 1, "type": "fallback", "proxy": "http://oldfb:1"}]
        self._write_env(f"{egress.ENV_MANIFEST}={_manifest(*rows)}")
        c = self._login()
        r = c.put("/api/scheduler/executors", headers={"X-CSRF-Token": c.csrf}, json={
            "workers": 3,
            "proxy_list": "http://p1:1,http://p2:2,http://p3:3",
            "proxy_fallback": "http://fb:9",
            "confirm_password": ADMIN_PASS,
        })
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        env = self._read_env()
        self.assertEqual(env["YIBAN_WORKERS"], "3", "旧键照旧写入（兼容）")
        body = self._get(c)
        self.assertEqual(body["workers"]["configured"], 3, "清单同步维护")
        self.assertEqual([a["egress"] for a in body["workers"]["assignments"]],
                         ["http://p1:1", "http://p2:2", "http://p3:3"])
        self.assertEqual(body["fallback"]["egress"], "http://fb:9")


class RowPermissionTest(_WebBase):
    """权限沿用现有口径（仅主管理员）与响应脱敏。"""

    def _env(self):
        self._write_env(f"{egress.ENV_MANIFEST}={_manifest({'slot': 0, 'type': 'worker', 'proxy': ''})}")

    def test_anonymous_is_rejected(self):
        self._env()
        anon = self.webapp.create_app().test_client()
        for method, path, payload in (("post", "/api/scheduler/executors/rows", {}),
                                      ("put", "/api/scheduler/executors/rows/0", {}),
                                      ("delete", "/api/scheduler/executors/rows/0", None)):
            with self.subTest(method=method):
                r = getattr(anon, method)(path, json=payload,
                                          headers={"X-CSRF-Token": "x"})
                self.assertIn(r.status_code, (401, 403))

    def test_plain_admin_is_rejected(self):
        self._env()
        from yiban.store import db as store_db
        store_db.init_db(os.environ["YIBAN_DB_FILE"], env_file=self.env_file, cleanup=False)
        store_db.create_user("admin2@test.local",
                             self.webapp.generate_password_hash("UserPass1234!"),
                             role="admin")
        c = self.webapp.create_app().test_client()
        self.assertEqual(c.post("/api/login", json={
            "username": "admin2@test.local", "password": "UserPass1234!"}).status_code, 200)
        csrf = c.get("/api/me").get_json()["csrf_token"]
        before = self._read_env()
        self.assertEqual(c.get("/api/scheduler/executors").status_code, 403)
        for method, path, payload in (("post", "/api/scheduler/executors/rows", {}),
                                      ("put", "/api/scheduler/executors/rows/0",
                                       {"type": "worker"}),
                                      ("delete", "/api/scheduler/executors/rows/0", None)):
            with self.subTest(method=method):
                r = getattr(c, method)(path, json=payload,
                                       headers={"X-CSRF-Token": csrf})
                self.assertEqual(r.status_code, 403, r.get_data(as_text=True))
        self.assertEqual(self._read_env(), before, "无权限不得落盘")


class LaunchWiringTest(unittest.TestCase):
    """拉起路径：清单的拉起列表排除停用/兜底行；槽位号贯穿身份/锁/心跳（不拉起停用槽位）。"""

    ROWS = ({"slot": 0, "type": "worker", "proxy": "http://w0:1"},
            {"slot": 1, "type": "disabled", "proxy": "http://w1:2"},
            {"slot": 2, "type": "fallback", "proxy": "http://fb:3"},
            {"slot": 3, "type": "worker", "proxy": "http://w3:4"})

    def test_launch_slots_exclude_disabled_and_fallback(self):
        env = {egress.ENV_MANIFEST: egress.dump_manifest(self.ROWS)}
        self.assertEqual(egress.launch_slots(env), [0, 3])
        # 清单缺失 → None（调用方回退旧口径 `--workers N`，行为不变）
        self.assertIsNone(egress.launch_slots({"YIBAN_WORKERS": "3"}))

    def test_runner_passes_manifest_slots_to_supervisor(self):
        from yiban.engine import runner, workers
        env = {egress.ENV_MANIFEST: egress.dump_manifest(self.ROWS),
               "YIBAN_GLOBAL_PAUSE": "0"}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(runner.clock, "now", lambda: WEEKDAY_06_40), \
                mock.patch.object(workers, "run_worker_supervisor",
                                  return_value=7) as m:
            rc = runner.main(["--workers", "5"])
        self.assertEqual(rc, 7)
        args, kwargs = m.call_args
        self.assertEqual(args[0], 2, "只拉起 2 个（停用行与兜底行不在拉起列表里）")
        self.assertEqual(kwargs.get("slots"), [0, 3])

    def test_runner_falls_back_to_workers_flag_without_manifest(self):
        from yiban.engine import runner, workers
        env = {"YIBAN_EXECUTORS": "", "YIBAN_GLOBAL_PAUSE": "0"}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(runner.clock, "now", lambda: WEEKDAY_06_40), \
                mock.patch.object(workers, "run_worker_supervisor",
                                  return_value=0) as m:
            runner.main(["--workers", "3"])
        args, kwargs = m.call_args
        self.assertEqual(args[0], 3)
        self.assertIsNone(kwargs.get("slots"), "旧口径：槽位就是 0..N-1")

    def test_supervisor_binds_identity_lock_and_heartbeat_to_slots(self):
        from yiban.engine import workers
        tmp = tempfile.mkdtemp(prefix="yiban-manifest-launch-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        spawned = []
        started = []

        class _FakeProc:
            def __init__(self, cmd, env=None, cwd=None):
                spawned.append(dict(env or {}))

            def poll(self):
                return 0

        with mock.patch.dict(os.environ, {"YIBAN_STATE_DIR": tmp}), \
                mock.patch.object(workers.cli_support, "_acquire_run_lock",
                                  return_value=None), \
                mock.patch.object(workers.accounts_mod, "load_accounts",
                                  return_value=[SimpleNamespace(phone="13800000000")]), \
                mock.patch.object(workers.subprocess, "Popen", _FakeProc), \
                mock.patch.object(workers.time, "sleep"), \
                mock.patch.object(workers.state_io, "mark_worker_started",
                                  side_effect=lambda slot, **kw: started.append(slot)):
            rc = workers.run_worker_supervisor(2, ["--workers", "2"], slots=[0, 3])
        self.assertEqual(rc, 0)
        self.assertEqual(started, [0, 3], "心跳按槽位号写（不按子进程序号）")
        self.assertEqual([e["YIBAN_EXECUTOR_ID"] for e in spawned],
                         [egress.worker_owner(0), egress.worker_owner(3)],
                         "执行体身份按槽位号构造（跨重启稳定）")
        self.assertEqual([e["YIBAN_RUN_LOCK_NAME"] for e in spawned],
                         ["signin-run.lock.w0", "signin-run.lock.w3"])


class DispatchGateTest(unittest.TestCase):
    """派发监督进程前的两道闸：周末/暂停门与补签轮判定，都必须在 spawn 之前。

    门若排在派发之后，清单形态下会先按清单拉起一批执行体子进程、再由每个子进程各自
    撞门退出——读配置、连库、载账号都白做一遍。补签轮判定本应是"只读本地状态"，
    被派发挡在后面就成了重活，退出码也传不回来。
    """

    ROWS = ({"slot": 0, "type": "worker", "proxy": ""},
            {"slot": 1, "type": "worker", "proxy": ""})

    #: 命中提前门的样例时刻：周日代表周末门，周三代表工作日（暂停门由开关单独注入）
    SUNDAY_06_31 = datetime(2026, 9, 6, 6, 31)

    def _manifest_env(self, **extra):
        env = {egress.ENV_MANIFEST: egress.dump_manifest(self.ROWS),
               "YIBAN_EXECUTOR_ID": "",
               "YIBAN_GLOBAL_PAUSE": "0"}
        env.update(extra)
        return env

    def test_day_off_returns_2_before_dispatch(self):
        """周日签到未开启 + 清单 2 槽位 → 返回 2，不派发监督进程、也不加载账号。"""
        from yiban.engine import runner, workers
        with mock.patch.dict(os.environ, self._manifest_env()), \
                mock.patch.object(runner.clock, "now", lambda: self.SUNDAY_06_31), \
                mock.patch.object(runner, "SUNDAY_SIGN", False), \
                mock.patch.object(workers, "run_worker_supervisor") as m_sup, \
                mock.patch.object(runner.accounts_mod, "load_accounts") as m_load:
            rc = runner.main([])
        self.assertEqual(rc, 2, "应走 SKIPPED 语义（run.sh 据此写 SKIPPED）")
        m_sup.assert_not_called()
        m_load.assert_not_called()

    def test_paused_returns_2_before_dispatch(self):
        """一键暂停命中 → 同样在派发之前返回 2。"""
        from yiban.engine import runner, workers
        with mock.patch.dict(os.environ, self._manifest_env(YIBAN_GLOBAL_PAUSE="1")), \
                mock.patch.object(runner.clock, "now", lambda: WEEKDAY_06_40), \
                mock.patch.object(workers, "run_worker_supervisor") as m_sup, \
                mock.patch.object(runner.accounts_mod, "load_accounts") as m_load:
            rc = runner.main([])
        self.assertEqual(rc, 2)
        m_sup.assert_not_called()
        m_load.assert_not_called()

    def test_second_run_check_returns_10_before_dispatch(self):
        """补签轮判定 + 清单多槽位 → 返回 10（需要补跑），不派发、不起子进程、不载账号。"""
        from yiban.engine import runner, workers
        with mock.patch.dict(os.environ, self._manifest_env()), \
                mock.patch.object(runner.clock, "now", lambda: self.SUNDAY_06_31), \
                mock.patch.object(runner.state_io, "need_second_run", return_value=True), \
                mock.patch.object(workers, "run_worker_supervisor") as m_sup, \
                mock.patch.object(workers.subprocess, "Popen") as m_popen, \
                mock.patch.object(runner.accounts_mod, "load_accounts") as m_load:
            rc = runner.main(["--second-run-check"])
        self.assertEqual(rc, 10, "补签判定不得被监督分支劫持")
        m_sup.assert_not_called()
        m_popen.assert_not_called()
        m_load.assert_not_called()

    def test_manual_only_still_dispatches_on_day_off(self):
        """`--only` 是用户主动触发（既有语义：不受周末/暂停门限制），多执行体下照旧派发。

        被提前门拦下等于"用户点了手动签到却被静默跳过"——门只写在单执行体路径时
        根本到不了这里（子进程各自按 `if not args.only` 放行），故豁免必须与之一致。
        """
        from yiban.engine import runner, workers
        for label, now, extra in (("周日", self.SUNDAY_06_31, {}),
                                  ("暂停", WEEKDAY_06_40, {"YIBAN_GLOBAL_PAUSE": "1"})):
            with self.subTest(label=label):
                with mock.patch.dict(os.environ, self._manifest_env(**extra)), \
                        mock.patch.object(runner.clock, "now", lambda now=now: now), \
                        mock.patch.object(runner, "SUNDAY_SIGN", False), \
                        mock.patch.object(workers, "run_worker_supervisor",
                                          return_value=7) as m_sup:
                    rc = runner.main(["--only", "13800000000"])
                self.assertEqual(rc, 7, "用户主动触发被门拦下 = 手动签到被静默跳过")
                m_sup.assert_called_once()

    def test_check_config_and_probe_still_dispatch_on_day_off(self):
        """`--check-config`（部署验证，哪天都要能验）与 `--probe`（自带门、跳过语义是 0）同样不被拦。"""
        from yiban.engine import runner, workers
        for argv in (["--check-config"], ["--probe"]):
            with self.subTest(argv=argv):
                with mock.patch.dict(os.environ, self._manifest_env()), \
                        mock.patch.object(runner.clock, "now", lambda: self.SUNDAY_06_31), \
                        mock.patch.object(runner, "SUNDAY_SIGN", False), \
                        mock.patch.object(workers, "run_worker_supervisor",
                                          return_value=0) as m_sup:
                    rc = runner.main(list(argv))
                self.assertEqual(rc, 0)
                m_sup.assert_called_once()

    def test_check_config_dispatches_supervisor_without_migrate(self):
        """F3：`--check-config` 派发监督进程时，监督进程的配置预检也不跑迁移。

        监督进程会在 spawn 前先 `load_accounts()` 校验配置（否则 N 个子进程同秒抢
        init_db、配置错误报 N 份）。若它用 migrate=True，宣称只读的校验仍会把目标库
        改一遍——子进程各自的 migrate=False 就被父进程抢先作废了。
        """
        from yiban.engine import runner, workers
        with mock.patch.dict(os.environ, self._manifest_env()), \
                mock.patch.object(runner.clock, "now", lambda: self.SUNDAY_06_31), \
                mock.patch.object(runner, "SUNDAY_SIGN", False), \
                mock.patch.object(workers, "run_worker_supervisor",
                                  return_value=0) as m_sup:
            rc = runner.main(["--check-config"])
        self.assertEqual(rc, 0)
        self.assertFalse(m_sup.call_args.kwargs.get("migrate", True),
                         "只读校验不得让监督进程先跑迁移")

        # 对照组：普通全量轮照旧在监督进程迁移（缺省 True）
        with mock.patch.dict(os.environ, self._manifest_env()), \
                mock.patch.object(runner.clock, "now", lambda: WEEKDAY_06_40), \
                mock.patch.object(workers, "run_worker_supervisor",
                                  return_value=0) as m_sup:
            rc = runner.main([])
        self.assertEqual(rc, 0)
        self.assertTrue(m_sup.call_args.kwargs.get("migrate", True),
                        "普通全量轮必须照旧迁移")


if __name__ == "__main__":
    unittest.main(verbosity=2)
