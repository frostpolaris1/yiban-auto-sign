# -*- coding: utf-8 -*-
"""出口分配（`yiban/egress.py`）与执行体接口（`/api/scheduler/executors`）的断言。

多执行体上线后，部署者要能给**每个执行体（含兜底常驻执行体）**单独配出口，
也可以留空走本机出口。这里钉住三件事：

1. **分配规则唯一**：列表按序取、不足循环、空位=直连、未配列表退回单出口——
   拉起执行体的一方与展示接口必须得到同一答案；
2. **不泄漏凭据**：代理串可能带 `user:pass@`，日志与接口只允许出现
   `scheme://host[:port]`（`describe()`）；接口另外只许主管理员访问；
3. **建议值不编数字**：没实测就没有建议（`recommended` 为 null），实测了才按
   实测 × 2/3 给建议，且文案说明"是建议不是上限"；
4. **执行体身份可判定且不回原串**：身份串的构造与解析同源（`worker_owner` /
   `fallback_owner` / `single_owner` / `parse_owner` / `role_label`），接口只回角色与
   1-based 槽位号——身份串含主机名，属部署信息，任何响应里都不许出现原串
   （本文件的脱敏断言反查它）。名字**跨重启稳定**（不含进程号/启动时刻），
   解析同时认得**旧格式**（库里有 14 天保留期的存量记录）；
5. **单段出口写接口**（`PUT …/executors/workers/<index>` 与 `…/executors/fallback`）：
   只替换目标段、其余段**逐字保留**（前端整条回写会把别人段的凭据清成空，这是本接口
   存在的理由），读接口只回描述串（不含 userinfo）；
6. **每个并行执行体的存活四态**（`workers.assignments[].state`）：`running` / `finished`
   / `idle` / `stale`，由后端按心跳文件算好。四态而非 alive 布尔，是因为执行体是
   **一轮就退出的短命进程**——"没在跑"多数时候正常，只有"有开始、无收尾且心跳过期"
   才值得报警；
7. **账号列表的 `last_executor`**：口径是**最近一次有记录的业务日**是谁签的（用户
   2026-09-21 定，「上次」的字面意即最近一次；跨周末停签仍显示上一轮），
   无记录为 `null`，且只回角色/槽位/标签（身份原串含主机名）；
8. **限频实测端点**（`POST …/executors/measure`）：仅主管理员、全局冷却（429 + 剩余
   秒数）、窗口内拒绝（409）；它**真的会用真实账号访问易班一次**（只读路径，不写
   签到状态、不动领取池），故这里的 `verify_account` 一律打桩，绝不联网。
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
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

from yiban import (  # noqa: E402
    clock,
    egress,
)

ADMIN_PASS = "TestPass1234!"


def _load_webapp():
    """**独立名字**加载 web/app.py：它把 ENV_FILE 等路径在导入期读成模块级常量，
    共用同一个模块对象时，别的测试文件先导入就会让本类读到**另一个 .env**
    （单个文件跑绿、全量并发跑红，正是这个原因）。"""
    spec = importlib.util.spec_from_file_location(
        "webapp_egress", os.path.join(BASE, "web", "app.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["webapp_egress"] = mod
    with contextlib.suppress(Exception):
        spec.loader.exec_module(mod)
    return mod
SECRET_PROXY = "http://svcuser:svcp@proxy1.example:8080"


class EgressRulesTest(unittest.TestCase):
    """纯函数：分配规则与脱敏（不依赖 web/DB）。"""

    def test_list_takes_by_index_and_wraps(self):
        env = {egress.ENV_WORKER_LIST: "http://a:1,http://b:2,http://c:3"}
        self.assertEqual(egress.resolve(egress.ROLE_WORKER, 0, env), "http://a:1")
        self.assertEqual(egress.resolve(egress.ROLE_WORKER, 2, env), "http://c:3")
        self.assertEqual(egress.resolve(egress.ROLE_WORKER, 4, env), "http://b:2",
                         "执行体数超过出口数应按序循环取用")

    def test_empty_slot_means_direct(self):
        env = {egress.ENV_WORKER_LIST: f"{SECRET_PROXY},,http://c:3"}
        self.assertEqual(egress.resolve(egress.ROLE_WORKER, 1, env), egress.DIRECT)
        self.assertEqual(egress.describe(egress.DIRECT), "直连（本机出口）")
        self.assertEqual(egress.resolve(egress.ROLE_WORKER, 0, env), SECRET_PROXY,
                         "空位只影响它自己那一格，其余照旧")

    def test_missing_list_falls_back_to_single_proxy(self):
        """只配了单出口的老配置必须继续可用（升级不改变行为）。"""
        env = {egress.ENV_SINGLE: "http://solo:9"}
        self.assertEqual(egress.resolve(egress.ROLE_WORKER, 3, env), "http://solo:9")
        self.assertEqual(egress.resolve(egress.ROLE_SINGLE, 0, env), "http://solo:9")
        # 列表键存在但只有空白 → 视为未配置
        self.assertEqual(egress.resolve(egress.ROLE_WORKER, 0,
                                       {egress.ENV_WORKER_LIST: "   ",
                                        egress.ENV_SINGLE: "http://solo:9"}),
                         "http://solo:9")

    def test_fallback_has_its_own_key_then_falls_back(self):
        self.assertEqual(egress.resolve(egress.ROLE_FALLBACK, env={
            egress.ENV_FALLBACK: "http://fb:5", egress.ENV_SINGLE: "http://solo:9"}),
            "http://fb:5")
        self.assertEqual(egress.resolve(egress.ROLE_FALLBACK, env={
            egress.ENV_FALLBACK: "", egress.ENV_SINGLE: "http://solo:9"}), "http://solo:9")
        self.assertEqual(egress.resolve(egress.ROLE_FALLBACK, env={}), egress.DIRECT)

    def test_worker_list_does_not_leak_into_fallback(self):
        """兜底执行体的出口与并行执行体的表互不干扰（各自可独立配置）。"""
        env = {egress.ENV_WORKER_LIST: "http://w0:1,http://w1:2"}
        self.assertEqual(egress.resolve(egress.ROLE_FALLBACK, env=env), egress.DIRECT)
        self.assertEqual(egress.resolve(egress.ROLE_WORKER, 0, env), "http://w0:1")

    def test_describe_drops_credentials(self):
        desc = egress.describe(SECRET_PROXY)
        self.assertEqual(desc, "http://proxy1.example:8080")
        self.assertNotIn("svcuser", desc)
        self.assertNotIn("svcp", desc)

    def test_assignments_report_every_worker(self):
        env = {egress.ENV_WORKER_LIST: f"{SECRET_PROXY},,http://c:3"}
        got = egress.assignments(4, env)
        self.assertEqual([i for i, _p, _d in got], [0, 1, 2, 3])
        self.assertEqual([d for _i, _p, d in got],
                         ["http://proxy1.example:8080", "直连（本机出口）",
                          "http://c:3", "http://proxy1.example:8080"])
        self.assertNotIn("svcp", json.dumps([d for _i, _p, d in got]))


class OwnerIdentityTest(unittest.TestCase):
    """执行体身份串：**写入与解析必须同源**（各写一份字符串迟早漂移）。

    身份串写进 `sign_claims.owner`，是"这个执行体算什么类型"的唯一事实来源。
    三种形态：并行执行体（可解析出序号）、兜底常驻、单执行体；历史遗留串判不出
    角色，**照实回 `unknown`** 而不是猜——老数据里兜底与单执行体同前缀，本就无法追溯。
    """

    def test_worker_owner_round_trip(self):
        owner = egress.worker_owner(3, "myhost")
        self.assertEqual(owner, "worker-3@myhost")
        parsed = egress.parse_owner(owner)
        self.assertEqual((parsed["role"], parsed["index"]), (egress.ROLE_WORKER, 3))
        self.assertEqual(parsed["label"], "并行执行体 #4")

    def test_stable_names_round_trip(self):
        """三种角色的**稳定槽位名**都要解析得回来（写入与解析同源）。"""
        cases = ((egress.worker_owner(2, "myhost"), egress.ROLE_WORKER, 2),
                 (egress.fallback_owner("myhost"), egress.ROLE_FALLBACK, None),
                 (egress.single_owner("myhost"), egress.ROLE_SINGLE, None))
        for owner, role, index in cases:
            with self.subTest(owner=owner):
                parsed = egress.parse_owner(owner)
                self.assertEqual((parsed["role"], parsed["index"]), (role, index))

    def test_owner_name_is_stable_and_host_scoped(self):
        """名字**跨重启稳定**（同参两次相等：不含进程号/启动时刻），且**跨主机唯一**。

        稳定性是本次改动的目的（前端把执行体当界面对象、重启后立即认领自己的在飞账号）；
        跨主机唯一是它的安全代价（同名 = 两台机器互相认领 → 同时登录同一账号）。
        """
        self.assertEqual(egress.worker_owner(3, "h1"), egress.worker_owner(3, "h1"))
        self.assertNotEqual(egress.worker_owner(3, "h1"), egress.worker_owner(3, "h2"))
        self.assertNotEqual(egress.worker_owner(2, "h1"), egress.worker_owner(3, "h1"))
        for fn in (egress.fallback_owner, egress.single_owner):
            with self.subTest(fn=fn.__name__):
                self.assertEqual(fn("h1"), fn("h1"))
                self.assertNotEqual(fn("h1"), fn("h2"))
        self.assertNotEqual(egress.fallback_owner("h1"), egress.single_owner("h1"))

    def test_owner_defaults_to_this_host(self):
        """省略主机名时取本机名（拉起执行体的一方无需自己拼串），且名字里**没有进程号**。"""
        host = socket.gethostname()
        self.assertEqual(egress.worker_owner(0), f"worker-0@{host}")
        self.assertEqual(egress.fallback_owner(), f"fallback@{host}")
        self.assertEqual(egress.single_owner(), f"single@{host}")
        for owner in (egress.worker_owner(0), egress.fallback_owner(), egress.single_owner()):
            self.assertNotIn(str(os.getpid()), owner, "稳定槽位名不得含进程号")

    def test_worker_index_parse_failure_is_not_fatal(self):
        """序号解析不出来（历史串/被改写）→ `index=None`，角色仍是 worker。"""
        for owner in ("host:workers:1:wx", "host:workers:1:7", "host:workers:1"):
            with self.subTest(owner=owner):
                parsed = egress.parse_owner(owner)
                self.assertEqual(parsed["role"], egress.ROLE_WORKER)
                self.assertIsNone(parsed["index"])
                self.assertEqual(parsed["label"], "并行执行体")
        # 稳定槽位名同理：`worker-@h` / `worker-xx@h` 判得出角色、判不出序号
        for owner in ("worker-@h", "worker-xx@h"):
            with self.subTest(owner=owner):
                parsed = egress.parse_owner(owner)
                self.assertEqual((parsed["role"], parsed["index"]),
                                 (egress.ROLE_WORKER, None))

    def test_legacy_owner_formats_still_parse(self):
        """**旧格式必须继续认得**：库里还有 14 天保留期的存量记录，写入格式改了不等于
        读不懂老数据（`:workers:` 中缀、`fallback-`/`exec-` 前缀）。"""
        cases = (("hostA:workers:4242:w3", egress.ROLE_WORKER, 3),
                 ("fallback-hostA:4242:090000", egress.ROLE_FALLBACK, None),
                 ("exec-hostA:4242:090001", egress.ROLE_SINGLE, None))
        for owner, role, index in cases:
            with self.subTest(owner=owner):
                parsed = egress.parse_owner(owner)
                self.assertEqual((parsed["role"], parsed["index"]), (role, index))
        # 新格式与旧格式的判定**不互相干扰**：含 `:workers:` 的串仍走旧分支（不会被
        # 稳定名分支截胡），稳定名也不会被旧前缀规则误判
        self.assertEqual(egress.parse_owner("worker-3@myhost")["index"], 3)
        self.assertEqual(egress.parse_owner("fallback@myhost")["role"],
                         egress.ROLE_FALLBACK)

    def test_fallback_and_single_prefixes(self):
        fb = egress.parse_owner(egress.IDENT_FALLBACK_PREFIX + "h:1:090000")
        self.assertEqual((fb["role"], fb["index"], fb["label"]),
                         (egress.ROLE_FALLBACK, None, "故障转移"))
        sg = egress.parse_owner(egress.IDENT_SINGLE_PREFIX + "h:1:090000")
        self.assertEqual((sg["role"], sg["index"], sg["label"]),
                         (egress.ROLE_SINGLE, None, "单执行体"))

    def test_unknown_for_legacy_and_empty(self):
        """历史遗留串（`{主机}:{进程}:{时刻}`，无前缀）判不出角色——照实回 unknown。"""
        for owner in ("", "   ", None, "hostA:100:090000", "随便写的串"):
            with self.subTest(owner=owner):
                parsed = egress.parse_owner(owner)
                self.assertEqual(parsed["role"], egress.ROLE_UNKNOWN)
                self.assertIsNone(parsed["index"])
                self.assertIn("未标注", parsed["label"])

    def test_role_label_fallbacks(self):
        self.assertEqual(egress.role_label(egress.ROLE_WORKER), "并行执行体")
        self.assertEqual(egress.role_label("别的角色"), "未标注（旧数据）")


class _WebBase(unittest.TestCase):
    """临时库/环境 + 主管理员登录（与既有 web 测试同一套骨架）。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-egress-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        # 明文写 .env（与既有 web 测试同做法）：首启迁移会转成哈希并删掉明文
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write("YIBAN_ACCOUNTS_KEY=" + "a" * 64 + "\n")
            f.write("YIBAN_ADMIN_USER=admin@test.local\n")
            f.write(f"YIBAN_ADMIN_PASSWORD={ADMIN_PASS}\n")
            f.write(f"{egress.ENV_WORKER_LIST}={SECRET_PROXY},,http://c.example:3128\n")
            f.write(f"{egress.ENV_FALLBACK}=http://fbuser:fbpw@fb.example:8080\n")
            f.write("YIBAN_WORKERS=3\n")
            f.write("YIBAN_CAPACITY_MEASURED=354\n")
            # 执行体写操作的门禁用例钉的是"真变更当次要口令、口令错零落盘"这套机制，
            # 固定在 full（默认档 risk 下这些动作不再当次要口令）
            f.write("YIBAN_PW_GATE=full\n")
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

    def _read_env(self):
        """共享 `.env` 解析成 dict（各用例契约断言共用）。"""
        with open(self.env_file, encoding="utf-8") as f:
            return dict(ln.split("=", 1) for ln in f.read().splitlines()
                        if "=" in ln and not ln.startswith("#"))

    def _login(self):
        """登录并带回 CSRF 令牌（写接口必须带 `X-CSRF-Token`，与既有测试同做法）。"""
        c = self.webapp.create_app().test_client()
        r = c.post("/api/login", json={"username": "admin@test.local", "password": ADMIN_PASS})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        c.csrf = c.get("/api/me").get_json()["csrf_token"]
        return c


class ExecutorsEndpointTest(_WebBase):
    def test_requires_master_admin(self):
        c = self.webapp.create_app().test_client()
        self.assertEqual(c.get("/api/scheduler/executors").status_code, 401)

    def test_returns_assignments_and_masks_credentials(self):
        c = self._login()
        r = c.get("/api/scheduler/executors")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        body = r.get_json()
        self.assertEqual(body["workers"]["configured"], 3)
        self.assertEqual([a["egress"] for a in body["workers"]["assignments"]],
                         ["http://proxy1.example:8080", "直连（本机出口）",
                          "http://c.example:3128"])
        self.assertEqual(body["fallback"]["egress"], "http://fb.example:8080")
        raw = json.dumps(body)
        for secret in ("svcuser", "svcp", "fbuser", "fbpw"):
            self.assertNotIn(secret, raw, "接口不得回显代理凭据")
        # 环境键名一并给出：前端不必硬编码字符串
        self.assertEqual(body["workers"]["env_keys"]["list"], egress.ENV_WORKER_LIST)

    def test_assignments_carry_role_and_label(self):
        """每个并行执行体都带角色与中文标签：前端不必自己拼文案。"""
        body = self._login().get("/api/scheduler/executors").get_json()
        got = body["workers"]["assignments"]
        self.assertEqual([a["role"] for a in got], [egress.ROLE_WORKER] * 3)
        self.assertEqual([a["label"] for a in got],
                         ["并行执行体 #1", "并行执行体 #2", "并行执行体 #3"])
        self.assertEqual([a["index"] for a in got], [0, 1, 2])

    def test_recommendation_comes_from_measured_value_only(self):
        c = self._login()
        body = c.get("/api/scheduler/executors").get_json()
        self.assertEqual(body["measured"]["per_executor_capacity"], 354)
        rec = body["recommendation"]
        self.assertEqual(rec["per_executor_accounts"], 236)   # 354 × 2/3
        self.assertIn("建议", rec["note"])
        self.assertNotIn("上限", rec["note"].replace("不是程序上限", ""))

    def test_no_measured_value_means_no_recommendation(self):
        """没实测就不给建议——不编数字（临时移除 .env 里的实测值，读完还原）。

        ⚠ 必须还原：unittest 按方法名字母序执行，本类的 .env 是共享的；改坏它会让
        后面检查"有实测值"的用例看到空值（我在此处踩过一次）。
        """
        with open(self.env_file, encoding="utf-8") as f:
            original = f.read()
        self.addCleanup(
            lambda: open(self.env_file, "w", encoding="utf-8").write(original)
        )
        with open(self.env_file, "w", encoding="utf-8") as f:
            f.write("\n".join(ln for ln in original.splitlines()
                              if not ln.startswith("YIBAN_CAPACITY_MEASURED")) + "\n")
        body = self._login().get("/api/scheduler/executors").get_json()
        self.assertIsNone(body["measured"])
        self.assertIsNone(body["recommendation"])


class FallbackStatusTest(_WebBase):
    """兜底常驻执行体的开关四态：`enabled`（.env 声明的开关）× `alive`（心跳）。

    为什么要后端算好 status：**声明开关**（网页写 .env）与**真在跑**（心跳新鲜度）
    是两件事，"开了却没跑起来"要靠宿主 cron，"没开却在跑"是人工起的进程——两者
    的运维动作完全不同，不能糊成一个布尔让前端猜。
    """

    def _set_enabled(self, value):
        """改写共享 `.env` 的开关行（`value=None` = 删掉该键）。"""
        with open(self.env_file, encoding="utf-8") as f:
            lines = [ln for ln in f.read().splitlines()
                     if not ln.startswith("YIBAN_FALLBACK_ENABLE")]
        if value is not None:
            lines.append(f"YIBAN_FALLBACK_ENABLE={value}")
        with open(self.env_file, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

    def _set_alive(self, alive):
        """造/删兜底心跳文件（与 `state_io._write_fallback_alive` 同格式）。"""
        path = os.path.join(self.tmp, "fallback-alive.json")
        if not alive:
            if os.path.exists(path):
                os.remove(path)
            return
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"at": clock.now().strftime("%Y-%m-%d %H:%M:%S"), "pid": 1}, f)

    def _fallback(self):
        return self._login().get("/api/scheduler/executors").get_json()["fallback"]

    def test_status_four_states(self):
        cases = (
            (None, False, False, "off"),
            ("0", False, False, "off"),
            ("1", True, True, "running"),
            ("1", False, True, "declared_not_running"),
            ("0", True, False, "running_not_declared"),
        )
        for raw, alive, enabled, status in cases:
            with self.subTest(YIBAN_FALLBACK_ENABLE=raw, alive=alive):
                self._set_enabled(raw)
                self._set_alive(alive)
                fb = self._fallback()
                self.assertEqual(fb["enabled"], enabled)
                self.assertEqual(fb["alive"], alive)
                self.assertEqual(fb["status"], status)

    def test_fallback_carries_role_label_and_key_name(self):
        self._set_enabled(None)
        self._set_alive(False)
        fb = self._fallback()
        self.assertEqual(fb["role"], egress.ROLE_FALLBACK)
        self.assertEqual(fb["label"], "故障转移")
        self.assertEqual(fb["env_key_enable"], "YIBAN_FALLBACK_ENABLE")
        self.assertIsInstance(fb["in_window"], bool)
        # 窗口外 alive=false 属正常：前端要据此只对窗口内报警，故 in_window 必须回
        self.assertIn("in_window", fb)

    def test_in_window_also_covers_the_day_gate(self):
        """`in_window` = 有效窗口内 **且** 今天没被周末门/暂停门挡下（2026-09-17 口径）。

        为什么必须含门：兜底常驻在"周末签到关闭 / 一键暂停"时会直接退出，那两天的
        钟点却落在窗口内——只按钟点算，页面会在每个周末、每次暂停期间误报"开了却没
        跑起来"。前端规矩不变（只对 in_window=true 的 declared_not_running 报警）。
        """
        wed = datetime(2026, 9, 2, 6, 35, 0)    # 周三 06:35（窗口内）
        sat = datetime(2026, 9, 5, 6, 35, 0)    # 周六 06:35（同样在窗口钟点内）
        self._set_enabled("1")
        self._set_alive(False)
        with mock.patch.dict(os.environ,
                             {"YIBAN_SATURDAY_SIGN": "0", "YIBAN_GLOBAL_PAUSE": "0"},
                             clear=False):
            with mock.patch.object(clock, "now", lambda: wed):
                self.assertTrue(self._fallback()["in_window"], "工作日在窗口内应为 True")
            with mock.patch.object(clock, "now", lambda: sat):
                self.assertFalse(self._fallback()["in_window"],
                                 "周六未开签到：钟点在窗口内，但今天本不该有兜底在跑")
            with mock.patch.object(
                    clock, "now", lambda: datetime(2026, 9, 2, 8, 30, 0)):
                self.assertFalse(self._fallback()["in_window"], "窗口外照样是 False")
            with mock.patch.dict(os.environ, {"YIBAN_GLOBAL_PAUSE": "1"}), \
                    mock.patch.object(clock, "now", lambda: wed):
                self.assertFalse(self._fallback()["in_window"], "一键暂停期间为 False")


    def test_truthy_env_value_reads_as_enabled(self):
        """`.env` 里手写 `true`/`on` 也算开（与 run.sh 的真值字面量同一套）。"""
        for value in ("true", "TRUE", "on", "yes"):
            with self.subTest(value=value):
                self._set_enabled(value)
                self._set_alive(False)
                fb = self._fallback()
                self.assertTrue(fb["enabled"])
                self.assertEqual(fb["status"], "declared_not_running")


#: 极具辨识度的主机名与进程号：脱敏断言据此反查"响应里有没有身份原串"。
#: 并行执行体用**当前的稳定槽位名**（不含进程号——进程号只出现在旧格式那两条里）
SECRET_HOST = "host-secret-9z8y"
OWNER_WORKER = egress.worker_owner(0, SECRET_HOST)
OWNER_FALLBACK = egress.IDENT_FALLBACK_PREFIX + f"{SECRET_HOST}:4243:090000"
OWNER_SINGLE = egress.IDENT_SINGLE_PREFIX + f"{SECRET_HOST}:4244:090001"
OWNER_LEGACY = f"{SECRET_HOST}:4245:090002"


class ActivityEndpointTest(_WebBase):
    """`activity`：当日"谁做了多少"，**已脱敏**（用槽位号替代 owner 原串）。

    身份串含主机名（旧格式还含进程号），对攻击者就是资产清单；但"第 1 个并行执行体
    做了 10 个"这类信息前端确实需要——槽位号正好给出同样的信息量而不泄漏部署细节。
    """

    def _seed(self):
        """造三类身份 + 一条历史遗留串的领取记录。"""
        from yiban.store import db as store_db
        day = clock.today()
        store_db.init_db(os.environ["YIBAN_DB_FILE"], env_file=self.env_file,
                         cleanup=False)
        rows = (
            ("13900000001", OWNER_WORKER, "done"),
            ("13900000002", OWNER_WORKER, "done"),
            ("13900000003", OWNER_WORKER, "failed"),
            ("13900000004", OWNER_FALLBACK, "claimed"),
            ("13900000005", OWNER_SINGLE, "done"),
            ("13900000006", OWNER_LEGACY, "done"),
        )
        for phone, owner, state in rows:
            self.assertTrue(store_db.claim_sign_account(phone, day, owner)[0], phone)
            if state == "done":
                store_db.claim_settle(phone, day, owner, store_db.CLAIM_STATE_DONE, "ok")
            elif state == "failed":
                store_db.claim_give_up(phone, day, owner, "重试耗尽")
        return day

    def test_activity_groups_and_masks_owner(self):
        day = self._seed()
        body = self._login().get("/api/scheduler/executors").get_json()
        act = body["activity"]
        self.assertEqual(act["day"], day)
        self.assertIsInstance(act["in_window"], bool)
        by_slot = {e["slot"]: e for e in act["by_executor"]}
        self.assertEqual(sorted(by_slot), list(range(1, len(by_slot) + 1)),
                         "槽位必须是 1-based 连续编号")
        roles = {e["role"] for e in act["by_executor"]}
        self.assertEqual(roles, {egress.ROLE_WORKER, egress.ROLE_FALLBACK,
                                 egress.ROLE_SINGLE, egress.ROLE_UNKNOWN})
        worker = next(e for e in act["by_executor"] if e["role"] == egress.ROLE_WORKER)
        self.assertEqual((worker["index"], worker["label"]), (0, "并行执行体 #1"))
        self.assertEqual((worker["done"], worker["failed"], worker["claimed"],
                          worker["total"]), (2, 1, 0, 3))
        unknown = next(e for e in act["by_executor"]
                       if e["role"] == egress.ROLE_UNKNOWN)
        self.assertEqual(unknown["label"], "未标注（旧数据）")
        self.assertEqual(act["totals"],
                         {"claimed": 1, "failed": 1, "done": 4, "total": 6})

    def test_activity_never_returns_raw_owner(self):
        """**脱敏硬要求**：响应 JSON 里反查不到任何身份原串（含主机名与进程号）。

        进程号只出现在旧格式的存量身份里（稳定槽位名不含进程号），故只对旧格式那三条
        的进程号做反查；主机名对四种身份都要反查。
        """
        self._seed()
        body = self._login().get("/api/scheduler/executors").get_json()
        raw = json.dumps(body, ensure_ascii=False)
        for owner in (OWNER_WORKER, OWNER_FALLBACK, OWNER_SINGLE, OWNER_LEGACY):
            self.assertNotIn(owner, raw, "接口不得回显执行体身份原串")
        self.assertNotIn(SECRET_HOST, raw, "主机名属部署信息，不得进响应")
        for pid in ("4243", "4244", "4245"):
            self.assertNotIn(pid, raw, "进程号属部署信息，不得进响应")

    def test_empty_database_is_not_an_error(self):
        """新部署没有库很正常：`activity` 回空结构，接口照常 200。"""
        body = self._login().get("/api/scheduler/executors").get_json()
        self.assertEqual(body["activity"]["by_executor"], [])
        self.assertEqual(body["activity"]["totals"],
                         {"claimed": 0, "failed": 0, "done": 0, "total": 0})


class ExecutorsSaveEndpointTest(_WebBase):
    """写路径：只写 .env、非法值不落盘、审计不含凭据。"""

    def test_put_requires_master_admin(self):
        c = self.webapp.create_app().test_client()
        r = c.put("/api/scheduler/executors", json={"workers": 2, "confirm_password": ADMIN_PASS},
                  headers={"X-CSRF-Token": "x"})
        self.assertIn(r.status_code, (401, 403))

    def test_put_writes_workers_and_proxies(self):
        c = self._login()
        r = c.put("/api/scheduler/executors", headers={"X-CSRF-Token": c.csrf}, json={
            "workers": 5,
            "proxy_list": "http://p1:1,http://p2:2",
            "proxy_fallback": "http://fb:8080",
            "capacity_measured": 400,
            "confirm_password": ADMIN_PASS,
        })
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        env = self._read_env()
        self.assertEqual(env["YIBAN_WORKERS"], "5")
        self.assertEqual(env["YIBAN_PROXY_LIST"], "http://p1:1,http://p2:2")
        self.assertEqual(env["YIBAN_PROXY_FALLBACK"], "http://fb:8080")
        self.assertEqual(env["YIBAN_CAPACITY_MEASURED"], "400")
        # 读回是描述串（有凭据也只回 host）
        body = c.get("/api/scheduler/executors").get_json()
        self.assertEqual([a["egress"] for a in body["workers"]["assignments"]][:2],
                         ["http://p1:1", "http://p2:2"])

    def test_put_rejects_bad_values_without_touching_env(self):
        c = self._login()
        before = self._read_env()
        for payload in ({"workers": 0}, {"workers": 999}, {"workers": "many"},
                        {"proxy_list": "not a url"}, {"proxy_list": "http://a:1" + chr(10) + "YIBAN_X=1"},
                        {"capacity_measured": -5}):
            with self.subTest(payload=payload):
                r = c.put("/api/scheduler/executors",
                          json={**payload, "confirm_password": ADMIN_PASS},
                          headers={"X-CSRF-Token": c.csrf})
                self.assertEqual(r.status_code, 400, payload)
        self.assertEqual(self._read_env(), before, "校验失败不得落盘")

    def test_put_empty_clears_measured(self):
        c = self._login()
        c.put("/api/scheduler/executors", headers={"X-CSRF-Token": c.csrf},
              json={"capacity_measured": 0, "confirm_password": ADMIN_PASS})
        self.assertEqual(self._read_env().get("YIBAN_CAPACITY_MEASURED", ""), "")
        self.assertIsNone(c.get("/api/scheduler/executors").get_json()["measured"])

    def test_put_fallback_enable_accepts_literals(self):
        """开关只接受 0/1/true/false，落盘统一归一成 0/1（.env 里只有一种写法）。"""
        c = self._login()
        for payload, expect in (({"fallback_enable": 1}, "1"),
                                ({"fallback_enable": "true"}, "1"),
                                ({"fallback_enable": True}, "1"),
                                ({"fallback_enable": 0}, "0"),
                                ({"fallback_enable": "FALSE"}, "0"),
                                ({"fallback_enable": False}, "0")):
            with self.subTest(payload=payload):
                r = c.put("/api/scheduler/executors",
                          json={**payload, "confirm_password": ADMIN_PASS},
                          headers={"X-CSRF-Token": c.csrf})
                self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
                self.assertEqual(self._read_env()["YIBAN_FALLBACK_ENABLE"], expect)
        # 读回与开关一致（enabled/status 由后端算好）
        body = c.get("/api/scheduler/executors").get_json()
        self.assertFalse(body["fallback"]["enabled"])
        self.assertEqual(body["fallback"]["status"], "off")

    def test_put_rejects_bad_fallback_enable_without_touching_env(self):
        c = self._login()
        before = self._read_env()
        for value in ("on", "yes", "也许", "1" + chr(10) + "YIBAN_X=1", "", 2):
            with self.subTest(value=value):
                r = c.put("/api/scheduler/executors",
                          json={"fallback_enable": value, "confirm_password": ADMIN_PASS},
                          headers={"X-CSRF-Token": c.csrf})
                self.assertEqual(r.status_code, 400, value)
        self.assertEqual(self._read_env(), before, "校验失败不得落盘")

    def test_audit_records_keys_not_credentials(self):
        """审计链不得出现代理凭据（只记键名）。"""
        c = self._login()
        from unittest import mock as _mock
        with _mock.patch.object(self.webapp.db, "audit", return_value=True) as m:
            c.put("/api/scheduler/executors", headers={"X-CSRF-Token": c.csrf},
                  json={"proxy_list": f"{SECRET_PROXY},,http://c:3",
                        "confirm_password": ADMIN_PASS})
        detail = " ".join(str(a) for a in m.call_args[0])
        for secret in ("svcuser", "svcp", SECRET_PROXY):
            self.assertNotIn(secret, detail)
        self.assertIn("YIBAN_PROXY_LIST", detail)


class FallbackSwitchOnlySaveTest(_WebBase):
    """「只拨故障转移开关」的保存链路（真实 Flask test client，不打桩）。

    开关不归行接口管：`PUT …/executors/rows/<slot>` 缺 type/proxy/name 一律 400
    （「没有可更新的字段」）。前端只拨开关时必须**只**发整条接口那一个请求——否则行接口
    先 400、`Promise.all` 直接 reject：用户被告知失败，而 `.env` 里开关已经落盘，且重试
    永远走同一条路。本类固定后端两侧的真实契约；前端"只拨开关时不带空请求体"由
    `test_executors_kpi_scope.py` 的源级守卫钉住。
    """

    def test_rows_endpoint_rejects_a_body_without_type_proxy_name(self):
        """先固定陷阱本身：缺三键的行接口请求确实是 400（前端不能带这个空请求体）。"""
        c = self._login()
        r = c.put("/api/scheduler/executors/rows/0",
                  json={"confirm_password": ADMIN_PASS},
                  headers={"X-CSRF-Token": c.csrf})
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        self.assertIn("没有可更新的字段", r.get_json()["error"])

    def test_switch_only_save_persists_and_returns_200(self):
        """只拨开关 → 整条接口 200 且 `.env` 落盘，这条路径上不应出现任何 400。"""
        c = self._login()
        r = c.put("/api/scheduler/executors",
                  json={"fallback_enable": 1, "confirm_password": ADMIN_PASS},
                  headers={"X-CSRF-Token": c.csrf})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(self._read_env()["YIBAN_FALLBACK_ENABLE"], "1")
        self.assertTrue(c.get("/api/scheduler/executors").get_json()["fallback"]["enabled"])

    def test_switch_only_save_with_wrong_password_stays_unpersisted(self):
        """口令错 → 403 + 报口令错，`.env` 不动（不会出现"报失败但已生效"）。"""
        c = self._login()
        before = self._read_env()
        cur = before.get("YIBAN_FALLBACK_ENABLE", "0")
        want = 0 if cur == "1" else 1            # 必与现值不同，才会触发口令门
        r = c.put("/api/scheduler/executors",
                  json={"fallback_enable": want, "confirm_password": "DefinitelyWrong!9"},
                  headers={"X-CSRF-Token": c.csrf})
        self.assertEqual(r.status_code, 403, r.get_data(as_text=True))
        self.assertIn("口令校验未通过", r.get_json()["error"])
        self.assertEqual(self._read_env().get("YIBAN_FALLBACK_ENABLE"),
                         before.get("YIBAN_FALLBACK_ENABLE"), "口令错不得落盘")


class ReplaceSlotTest(unittest.TestCase):
    """`egress.replace_slot`：**只换目标段、其余段逐字保留**（纯函数，不依赖 web/DB）。

    排在整条写入之外的第二个写面的地基：段位必须与 `parse_list` 同号（否则"改第 2 个
    执行体的出口"会改到别人头上），其余段必须逐字写回（否则一次改动会把别人段的写法
    与"空位=直连"的刻意留白一起抹掉）。
    """

    def test_replaces_only_target_and_keeps_the_rest_verbatim(self):
        raw = "http://u1:p1@a:1,http://u2:p2@b:2,http://u3:p3@c:3"
        out = egress.replace_slot(raw, 1, "http://new:9")
        self.assertEqual(out, "http://u1:p1@a:1,http://new:9,http://u3:p3@c:3")
        self.assertEqual(out.split(",")[0], raw.split(",")[0], "第 1 段必须逐字未变")
        self.assertEqual(out.split(",")[2], raw.split(",")[2], "第 3 段必须逐字未变")

    def test_slot_numbering_matches_parse_list(self):
        """段号口径与 `parse_list` 一致（含空位），否则会改到别人段上。"""
        raw = "http://a:1,,http://c:3"
        self.assertEqual(egress.parse_list(raw)[1], egress.DIRECT)
        self.assertEqual(egress.replace_slot(raw, 1, "http://b:2"),
                         "http://a:1,http://b:2,http://c:3")
        # 末尾逗号也是"多一个空段"（parse_list 的字面语义），补位时同样按逗号连接
        self.assertEqual(len(egress.parse_list("http://a:1,")), 2)
        self.assertEqual(egress.replace_slot("http://a:1,", 1, "http://b:2"),
                         "http://a:1,http://b:2")

    def test_pads_missing_segments(self):
        """段数不足时用空段补齐到 index（中间段为空 = 该执行体直连）。"""
        self.assertEqual(egress.replace_slot("http://a:1", 2, "http://c:3"),
                         "http://a:1,,http://c:3")
        self.assertEqual(egress.replace_slot("", 0, "http://a:1"), "http://a:1")
        self.assertEqual(egress.replace_slot(None, 1, "http://b:2"), ",http://b:2")
        # 补齐出来的空段在解析口径下确实是"直连"
        self.assertEqual(egress.parse_list(egress.replace_slot("http://a:1", 2, "x:1"))[1],
                         egress.DIRECT)

    def test_untouched_input_round_trips(self):
        """不替换任何段（写回同样的值）必须**逐字回到原串**——其余段一字不差。"""
        for raw in ("http://a:1,http://b:2", "http://a:1,,http://c:3", "http://only:1",
                    "http://u:p@a:1, http://b:2"):
            with self.subTest(raw=raw):
                fields = raw.split(",")
                index = len(fields) - 1
                self.assertEqual(egress.replace_slot(raw, index, fields[index]), raw)


class SlotEgressEndpointTest(_WebBase):
    """单段出口写接口：`PUT …/executors/workers/<index>` 与 `PUT …/executors/fallback`。

    这两个接口存在的理由是**防数据破坏**：整条写入收的是整条逗号列表、读接口只回脱敏
    描述串（不含 userinfo），前端拿读回的值整条回写就会把别人段的代理凭据清成空、静默
    退回直连。故本类最要紧的断言是"**只改目标段，其余段逐字未变**"。
    """

    def _setup_env(self, proxy_list=None, workers="3", fallback=None):
        """把共享 `.env` 重写成本次用例要的出口形态（`None` = 不写该键）。

        本类每个用例都自带前置配置：共享 `.env` 会被前面的用例改写，不这样写就会
        出现"单跑绿、全量红"。
        """
        lines = ["YIBAN_ACCOUNTS_KEY=" + "a" * 64,
                 "YIBAN_ADMIN_USER=admin@test.local",
                 f"YIBAN_ADMIN_PASSWORD={ADMIN_PASS}"]
        if proxy_list is not None:
            lines.append(f"{egress.ENV_WORKER_LIST}={proxy_list}")
        if fallback is not None:
            lines.append(f"{egress.ENV_FALLBACK}={fallback}")
        if workers is not None:
            lines.append(f"YIBAN_WORKERS={workers}")
        with open(self.env_file, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        # 重写 .env 会把启动时生成的 `YIBAN_ADMIN_PASSWORD_HASH` 一并抹掉，而
        # `verify_admin` 对"只有明文、没有哈希"是 **fail-closed 拒绝**（M1 口径），
        # 于是写接口的口令门会一律 403。这里补跑一次与启动同源的迁移（明文→哈希），
        # 让夹具回到真实部署的样子——不是为了让测试变绿而放宽断言。
        self.webapp.migrate_admin_password_to_hash(self.env_file)

    def _put(self, c, path, payload, password=ADMIN_PASS):
        """写接口请求：默认带上 `confirm_password`（2026-09-17 起写操作要口令门）。

        `password=None` 用于**专门测那道门**的用例（不带口令 → 期望 403）。
        """
        body = dict(payload)
        if password is not None:
            body.setdefault("confirm_password", password)
        return c.put(path, json=body, headers={"X-CSRF-Token": c.csrf})

    def test_worker_slot_changes_only_target_segment(self):
        """**验收项**：改第 2 段后，第 1、3 段逐字未变；读接口只回描述串（不含 userinfo）。"""
        self._setup_env(proxy_list="http://u1:p1@a.example:1,http://u2:p2@b.example:2,"
                                   "http://u3:p3@c.example:3", workers="3")
        c = self._login()
        r = self._put(c, "/api/scheduler/executors/workers/1",
                      {"egress": "http://newuser:newpw@d.example:4"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        body = r.get_json()
        self.assertEqual(body["index"], 1)
        self.assertEqual(body["egress"], "http://d.example:4", "响应只回描述串")
        for secret in ("newuser", "newpw"):
            self.assertNotIn(secret, json.dumps(body))
        # 落盘：只有第 2 段变了，其余两段**逐字**保留（连 userinfo 都没动）
        self.assertEqual(
            self._read_env()[egress.ENV_WORKER_LIST],
            "http://u1:p1@a.example:1,http://newuser:newpw@d.example:4,"
            "http://u3:p3@c.example:3")
        # 读接口：三段都是描述串，任何凭据都不出现
        got = c.get("/api/scheduler/executors").get_json()
        self.assertEqual([a["egress"] for a in got["workers"]["assignments"]],
                         ["http://a.example:1", "http://d.example:4", "http://c.example:3"])
        raw = json.dumps(got, ensure_ascii=False)
        for secret in ("u1:p1", "u2:p2", "u3:p3", "newuser", "newpw"):
            self.assertNotIn(secret, raw, "读接口不得回显任何段的凭据")

    def test_worker_slot_pads_missing_segments(self):
        """列表只有 1 段、改 index 2 → 第 2 段是新值、第 0 段原样、中间为空段。"""
        self._setup_env(proxy_list="http://only.example:1", workers="3")
        c = self._login()
        r = self._put(c, "/api/scheduler/executors/workers/2",
                      {"egress": "http://late.example:2"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json()["egress"], "http://late.example:2")
        stored = self._read_env()[egress.ENV_WORKER_LIST]
        self.assertEqual(stored, "http://only.example:1,,http://late.example:2")
        self.assertEqual(egress.parse_list(stored)[1], egress.DIRECT, "中间补齐段=直连")

    def test_empty_or_null_means_direct(self):
        """`""` / `null` = 该槽位直连（清空是显式动作，不是"没提交"）。"""
        self._setup_env(proxy_list="http://u1:p1@a.example:1,http://b.example:2,"
                                   "http://c.example:3", workers="3",
                        fallback="http://fb.example:9")
        c = self._login()
        for payload in ({"egress": ""}, {"egress": None}):
            with self.subTest(payload=payload):
                self._setup_env(proxy_list="http://u1:p1@a.example:1,http://b.example:2,"
                                           "http://c.example:3", workers="3")
                r = self._put(c, "/api/scheduler/executors/workers/1", payload)
                self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
                self.assertEqual(r.get_json()["egress"], "直连（本机出口）")
                self.assertEqual(self._read_env()[egress.ENV_WORKER_LIST],
                                 "http://u1:p1@a.example:1,,http://c.example:3",
                                 "只清目标段，别的段一字不动")
        # 兜底段清空 = 删掉该键（既有的"未单独配置就退回 YIBAN_PROXY"语义不变）
        self._setup_env(proxy_list="http://a.example:1", fallback="http://fb.example:9")
        r = self._put(c, "/api/scheduler/executors/fallback", {"egress": None})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json(), {"ok": True, "index": "fallback",
                                        "egress": "直连（本机出口）"})
        self.assertEqual(self._read_env().get(egress.ENV_FALLBACK, ""), "")
        self.assertEqual(c.get("/api/scheduler/executors").get_json()["fallback"]["egress"],
                         "直连（本机出口）")

    def test_fallback_slot_replaces_only_that_key(self):
        """改兜底段不得连带重写并行执行体的出口表（反之亦然）。"""
        self._setup_env(proxy_list="http://u1:p1@a.example:1,http://b.example:2",
                        workers="2")
        c = self._login()
        r = self._put(c, "/api/scheduler/executors/fallback",
                      {"egress": "http://fbuser:fbpw@fb.example:8080"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json(),
                         {"ok": True, "index": "fallback", "egress": "http://fb.example:8080"})
        env = self._read_env()
        self.assertEqual(env[egress.ENV_FALLBACK],
                         "http://fbuser:fbpw@fb.example:8080")
        self.assertEqual(env[egress.ENV_WORKER_LIST], "http://u1:p1@a.example:1,http://b.example:2",
                         "兜底写接口不得动并行执行体的表")

    def test_missing_egress_key_is_400_without_touching_env(self):
        """缺 `egress` 键 → 400（不给"什么都不改"的歧义），且不落盘。"""
        self._setup_env(proxy_list="http://a.example:1", fallback="http://fb.example:9")
        c = self._login()
        before = self._read_env()[egress.ENV_WORKER_LIST]
        for path in ("/api/scheduler/executors/workers/0",
                     "/api/scheduler/executors/fallback"):
            with self.subTest(path=path):
                r = self._put(c, path, {})
                self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
                self.assertIn("egress", r.get_json()["error"])
        self.assertEqual(self._read_env()[egress.ENV_WORKER_LIST], before,
                         "校验失败不得落盘（比整份 .env 只看出口键：启动期口令迁移"
                         "会合法地改写 .env 的别处）")

    def test_rejects_line_break_injection_and_bad_url(self):
        """换行注入与新段的形状校验一律 400，且不落盘（其余段不得被写坏）。"""
        self._setup_env(proxy_list="http://a.example:1,http://b.example:2", workers="2")
        c = self._login()
        before = self._read_env()[egress.ENV_WORKER_LIST]
        bad = ("http://x:1" + chr(10) + "YIBAN_PROXY=", "http://x:1" + chr(13),
               "not a url", "http://user:pa ss@h:1")
        for value in bad:
            with self.subTest(value=value):
                r = self._put(c, "/api/scheduler/executors/workers/0", {"egress": value})
                self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        self.assertEqual(self._read_env()[egress.ENV_WORKER_LIST], before,
                         "校验失败不得落盘")

    def test_index_out_of_range_is_400(self):
        """`index` 必须 `< 当前执行体数` 且 `<= 63`，否则 400 并说明该槽位未被使用。"""
        self._setup_env(proxy_list="http://a.example:1,http://b.example:2", workers="2")
        c = self._login()
        for index in (2, 3, 63):
            with self.subTest(index=index):
                r = self._put(c, f"/api/scheduler/executors/workers/{index}",
                              {"egress": "http://x:1"})
                self.assertEqual(r.status_code, 400)
                self.assertIn("未被使用", r.get_json()["error"])
        # 下标上限 63（`YIBAN_WORKERS` 最大 64）：就算执行体数写满也不收 64
        self._setup_env(proxy_list="http://a.example:1", workers="64")
        r = self._put(c, "/api/scheduler/executors/workers/64", {"egress": "http://x:1"})
        self.assertEqual(r.status_code, 400)
        # 合法槽位在同一份配置下必须能写（反证 400 不是"一律拒绝"）
        self.assertEqual(self._put(c, "/api/scheduler/executors/workers/63",
                                   {"egress": "http://x:1"}).status_code, 200)

    def test_requires_master_admin(self):
        """未登录 401/403；**普通管理员（注册用户）403**；且不落盘。"""
        self._setup_env(proxy_list="http://a.example:1", fallback="http://fb.example:9")
        anon = self.webapp.create_app().test_client()
        for path in ("/api/scheduler/executors/workers/0",
                     "/api/scheduler/executors/fallback"):
            with self.subTest(path=path, who="anon"):
                r = anon.put(path, json={"egress": "http://x:1"},
                             headers={"X-CSRF-Token": "x"})
                self.assertIn(r.status_code, (401, 403))
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
        for path in ("/api/scheduler/executors/workers/0",
                     "/api/scheduler/executors/fallback"):
            with self.subTest(path=path, who="non-master-admin"):
                r = c.put(path, json={"egress": "http://x:1"},
                          headers={"X-CSRF-Token": csrf})
                self.assertEqual(r.status_code, 403, r.get_data(as_text=True))
        self.assertEqual(self._read_env(), before, "无权限不得落盘")

    def test_audit_records_slot_name_not_credentials(self):
        """审计只记**槽位名**（如 `YIBAN_PROXY_LIST[1]`），绝不记代理凭据。"""
        self._setup_env(proxy_list="http://a.example:1,http://b.example:2", workers="2")
        c = self._login()
        from unittest import mock as _mock
        with _mock.patch.object(self.webapp.db, "audit", return_value=True) as m:
            self.assertEqual(self._put(c, "/api/scheduler/executors/workers/1",
                                       {"egress": SECRET_PROXY}).status_code, 200)
        detail = " ".join(str(a) for a in m.call_args[0])
        for secret in ("svcuser", "svcp", SECRET_PROXY):
            self.assertNotIn(secret, detail)
        self.assertIn(f"{egress.ENV_WORKER_LIST}[1]", detail)


class WorkerPresenceTest(_WebBase):
    """存活四态：`running` / `finished` / `idle` / `stale`（心跳文件 → 接口字段）。

    判据的分界是"心跳周期"：新鲜（`<= 2 ×` 周期）算在跑，过期且没收尾算异常。
    测试直接写心跳文件（`mark_worker_*` 是写侧真实现），不真起执行体。
    """

    HEARTBEAT_SEC = 30  # 与 state_io.WORKER_HEARTBEAT_SEC 同值：这里钉的是判据口径
    FIXED_NOW = datetime(2026, 9, 16, 6, 40, 0)

    def setUp(self):
        super().setUp()
        self._clear_heartbeats()

    def _clear_heartbeats(self):
        prefix = self.webapp.signin.WORKER_ALIVE_FILE_PREFIX
        for name in os.listdir(self.tmp):
            if name.startswith(prefix):
                os.remove(os.path.join(self.tmp, name))

    def _presence(self, index, now=None):
        return self.webapp.signin.worker_presence(index, now or self.FIXED_NOW)

    def test_four_states(self):
        s = self.webapp.signin
        now = self.FIXED_NOW
        # idle：本业务日无记录（今天还没跑）
        self.assertEqual(self._presence(0), ("idle", None))
        # running：心跳新鲜
        s.mark_worker_started(1, now=now - timedelta(seconds=5))
        self.assertEqual(self._presence(1), ("running", "2026-09-16 06:39:55"))
        # finished：有收尾标记（正常退出），last_seen 取收尾时刻
        s.mark_worker_started(2, now=now - timedelta(minutes=20))
        s.mark_worker_finished(2, exit_code=0, now=now - timedelta(minutes=18))
        self.assertEqual(self._presence(2), ("finished", "2026-09-16 06:22:00"))
        # stale：有开始、无收尾，心跳已过期——这才是需要用户注意的（疑似被强杀）
        s.mark_worker_started(3, now=now - timedelta(seconds=2 * self.HEARTBEAT_SEC + 1))
        self.assertEqual(self._presence(3), ("stale", "2026-09-16 06:38:59"))
        # 边界：刚好 2 × 周期仍算新鲜（判据是 `<=`，与兜底心跳同口径）
        s.mark_worker_started(4, now=now - timedelta(seconds=2 * self.HEARTBEAT_SEC))
        self.assertEqual(self._presence(4)[0], "running")

    def test_beat_refreshes_ts_but_keeps_started_at(self):
        s = self.webapp.signin
        now = self.FIXED_NOW
        s.mark_worker_started(0, now=now - timedelta(minutes=10))
        self.assertEqual(self._presence(0)[0], "stale", "十分钟前的心跳已过期")
        s.mark_worker_beat(0, now=now - timedelta(seconds=3))
        self.assertEqual(self._presence(0), ("running", "2026-09-16 06:39:57"))
        with open(os.path.join(self.tmp, "worker-alive-0.json"), encoding="utf-8") as f:
            raw = json.load(f)
        # 起始时刻是事实，不能被心跳刷新改写（否则"本轮从何时开始"就查不到了）
        self.assertEqual(raw["started_at"], "2026-09-16 06:30:00")
        self.assertEqual(raw["day"], "2026-09-16")
        self.assertEqual(raw["index"], 0)

    def test_other_day_is_idle_and_unparsable_ts_is_stale(self):
        s = self.webapp.signin
        now = self.FIXED_NOW
        # 昨天"已跑完"不等于今天已跑（否则每天早上页面都显示"已跑完"）
        s.mark_worker_finished(0, exit_code=0, now=now - timedelta(days=1))
        self.assertEqual(self._presence(0), ("idle", None))
        # 有记录却读不出时间：宁可提示异常，也不静默当成在线
        with open(os.path.join(self.tmp, "worker-alive-1.json"), "w", encoding="utf-8") as f:
            json.dump({"day": "2026-09-16", "started_at": "坏值", "ts": ""}, f)
        self.assertEqual(self._presence(1), ("stale", None))

    def test_assignments_carry_state_and_last_seen_without_pid_or_host(self):
        now = clock.now().replace(microsecond=0)
        self.webapp.signin.mark_worker_started(1, now=now)
        body = self._login().get("/api/scheduler/executors").get_json()
        got = {a["index"]: a for a in body["workers"]["assignments"]}
        self.assertEqual(got[1]["state"], "running")
        self.assertEqual(got[1]["last_seen_at"], now.strftime("%Y-%m-%d %H:%M:%S"))
        self.assertEqual(got[0]["state"], "idle")
        self.assertIsNone(got[0]["last_seen_at"], "无记录时时间串必须是 null")
        for key in ("pid", "host", "hostname"):
            self.assertNotIn(key, got[1], "存活字段不得携带 pid/主机名")
        self.assertNotIn(socket.gethostname(), json.dumps(body, ensure_ascii=False),
                         "主机名属部署信息")


class AccountsLastExecutorTest(_WebBase):
    """/api/accounts 的 `last_executor`：**最近一次有记录的业务日**是谁签的。

    口径由用户定（2026-09-21）：「上次」的字面意即最近一次——周末停签后按"昨天"取
    会让整列空白到下一个工作日。无记录必须是 `null`（前端靠它显示"—"）；
    身份串含主机名，故只回角色/槽位/标签。
    """

    PHONES = ("13900000011", "13900000012", "13900000013", "13900000014")

    def _seed_accounts(self, phones):
        from yiban.store import db as store_db
        store_db.init_db(os.environ["YIBAN_DB_FILE"], env_file=self.env_file, cleanup=False)
        for phone in phones:
            store_db.add_account({
                "name": "N", "phone": phone, "password": "pw", "phone_model": "",
                "phone_code": "", "owner": "admin", "status": "active", "reject_reason": "",
            })

    def _prev_day(self):
        return (clock.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    def _last_executor_of(self):
        """`/{脱敏手机号: last_executor}`——列表行里的手机号是打过码的。"""
        body = self._login().get("/api/accounts").get_json()
        return {a["phone"]: a["last_executor"] for a in body["accounts"]}

    def test_latest_day_owner_and_role_resolution(self):
        from yiban.store import db as store_db
        self._seed_accounts(self.PHONES)
        prev, today = self._prev_day(), clock.today()
        # 前天（此处借 _prev_day 模拟更早的业务日）：1 号由并行执行体 #1 签
        self.assertTrue(store_db.claim_sign_account(self.PHONES[0], prev, OWNER_WORKER)[0])
        # 今天是最近一次有记录的业务日：2 号由兜底签、3 号由并行执行体 #1 签
        # ——口径是"最近一次"，故 2/3 号按今天的记录解析，1 号（只在更早日有记录）为 null
        self.assertTrue(store_db.claim_sign_account(self.PHONES[1], today, OWNER_FALLBACK)[0])
        self.assertTrue(store_db.claim_sign_account(self.PHONES[2], today, OWNER_WORKER)[0])
        got = self._last_executor_of()
        m = self.webapp._mask_phone
        self.assertEqual(got[m(self.PHONES[0])], None,
                         "只在更早业务日有记录的账号：最近一次（今日）没有它的记录 → null")
        self.assertEqual(got[m(self.PHONES[1])],
                         {"role": egress.ROLE_FALLBACK, "index": None,
                          "label": "故障转移"})
        self.assertEqual(got[m(self.PHONES[2])],
                         {"role": egress.ROLE_WORKER, "index": 0,
                          "label": "并行执行体 #1"})
        self.assertIsNone(got[m(self.PHONES[3])], "无记录必须是 null，前端据此显示 —")

    def test_weekend_gap_still_shows_last_round(self):
        """跨周末停签后仍显示上一轮：更早业务日的记录不被"昨天"口径清空。"""
        from yiban.store import db as store_db
        self._seed_accounts(self.PHONES[:1])
        # 只有更早业务日有记录（模拟周六/周日无签到轮）
        self.assertTrue(store_db.claim_sign_account(self.PHONES[0], self._prev_day(), OWNER_FALLBACK)[0])
        got = self._last_executor_of()
        self.assertEqual(got[self.webapp._mask_phone(self.PHONES[0])],
                         {"role": egress.ROLE_FALLBACK, "index": None,
                          "label": "故障转移"})

    def test_no_record_and_unavailable_db_are_not_errors(self):
        from yiban.store import db as store_db
        self._seed_accounts(self.PHONES[:1])
        self.assertIsNone(self._last_executor_of()[self.webapp._mask_phone(self.PHONES[0])])
        # 库不存在/未初始化：一次取全的查询必须按空表返回，而不是抛（新部署很正常）
        with mock.patch.object(store_db, "get_conn", side_effect=RuntimeError("库不可用")):
            self.assertEqual(store_db.claim_owners_for_day("2026-09-16"), {})
            self.assertIsNone(store_db.claim_latest_day())

    def test_response_carries_no_owner_raw_string_nor_full_phone(self):
        from yiban.store import db as store_db
        phone = self.PHONES[0]
        self._seed_accounts((phone,))
        self.assertTrue(store_db.claim_sign_account(phone, self._prev_day(), OWNER_WORKER)[0])
        raw = json.dumps(self._login().get("/api/accounts").get_json(), ensure_ascii=False)
        self.assertNotIn(OWNER_WORKER, raw, "不得回显执行体身份原串")
        self.assertNotIn(SECRET_HOST, raw, "主机名属部署信息")
        self.assertNotIn(phone, raw, "列表不回完整手机号")


class MeasureCooldownUnitTest(_WebBase):
    """冷却剩余秒数的纯函数口径（不联网、不起应用）。"""

    def test_remaining_rounds_up_and_expires(self):
        f = self.webapp._measure_cooldown_remaining
        now = datetime(2026, 9, 16, 6, 40, 0)
        self.assertEqual(f({"at": "2026-09-16 06:30:00"}, 600, now=now), 0,
                         "已过冷却期 → 0（可实测）")
        self.assertEqual(f({"at": "2026-09-16 06:39:59"}, 600, now=now), 599)
        self.assertEqual(f({"at": "2026-09-16 06:40:00"}, 600, now=now), 600,
                         "刚实测过 → 整整一个冷却周期")
        self.assertEqual(f({"at": "2026-09-16 06:39:59"}, 600,
                           now=now + timedelta(milliseconds=500)), 599,
                         "剩余的小数部分向上取整（不能提前解锁）")

    def test_disabled_and_broken_state_mean_no_cooldown(self):
        f = self.webapp._measure_cooldown_remaining
        now = datetime(2026, 9, 16, 6, 40, 0)
        self.assertEqual(f({"at": "2026-09-16 06:39:59"}, 0, now=now), 0,
                         "冷却配成 0 = 关闭限频")
        for broken in ({}, {"at": ""}, {"at": "昨天"}, {"at": None}):
            with self.subTest(state=broken):
                self.assertEqual(f(broken, 600, now=now), 0,
                                 "时刻读不出 → 按可实测处理（不因坏文件永久禁用）")


class MeasureEndpointTest(_WebBase):
    """`POST /api/scheduler/executors/measure`：**会用真实账号访问易班一次**。

    三条硬约束：仅主管理员（否则 403）、全局冷却（429 + 剩余秒数）、窗口内拒绝（409）。
    这里 `verify_account` 一律打桩（绝不真联网），并断言它**没有**碰签到状态与领取池。
    """

    PHONE = "13800000021"
    FULL_COOLDOWN = 600        # 默认冷却秒数（YIBAN_MEASURE_COOLDOWN 未配置）
    DOC_GAP = 10               # YIBAN_ACCOUNT_GAP_MAX 默认值（容量公式的间隔项）

    def setUp(self):
        super().setUp()
        self.measure_file = os.path.join(self.tmp, self.webapp.MEASURE_STATE_FILE)
        if os.path.exists(self.measure_file):
            os.remove(self.measure_file)

    def _seed_accounts(self, rows):
        """rows = [(phone, status, user_paused)]"""
        from yiban.store import db as store_db
        store_db.init_db(os.environ["YIBAN_DB_FILE"], env_file=self.env_file, cleanup=False)
        for phone, status, paused in rows:
            store_db.add_account({
                "name": "N", "phone": phone, "password": "pw", "phone_model": "",
                "phone_code": "", "owner": "admin", "status": status, "reject_reason": "",
            })
            if paused:
                acc = next(a for a in store_db.load_accounts_raw() if a["phone"] == phone)
                store_db.set_user_paused(acc["id"], 1)

    def _post(self, client, payload=None, csrf=None):
        return client.post("/api/scheduler/executors/measure", json=payload or {},
                           headers={"X-CSRF-Token": csrf if csrf is not None else "x"})

    @contextlib.contextmanager
    def _no_network_no_sign_writes(self, verify_result=(True, "ok"), seconds=1.83):
        """打桩联网 + 记录所有"会给签到状态/领取池留痕"的函数，返回 (verify 替身, 记录器)。"""
        writers = ("claim_sign_account", "claim_settle", "claim_give_up", "claim_touch",
                   "add_sign_event", "add_sign_events_batch")
        spies = {}
        ticks = iter([1000.0, 1000.0 + seconds])
        with contextlib.ExitStack() as stack:
            verify = stack.enter_context(mock.patch.object(
                self.webapp.signin, "verify_account", return_value=verify_result))
            audit = stack.enter_context(mock.patch.object(self.webapp.db, "audit"))
            stack.enter_context(mock.patch.object(
                self.webapp.time, "monotonic",
                side_effect=lambda: next(ticks, 1000.0 + seconds)))
            for name in writers:
                spies[name] = stack.enter_context(mock.patch.object(self.webapp.db, name))
            spies["_write_sign_state"] = stack.enter_context(
                mock.patch.object(self.webapp.signin, "_write_sign_state"))
            yield verify, audit, spies

    def _assert_no_sign_writes(self, spies):
        called = sorted(n for n, m in spies.items() if m.called)
        self.assertEqual(called, [], "实测只走只读路径，不得写签到状态/领取池")

    def test_requires_master_admin(self):
        self._seed_accounts([(self.PHONE, "active", False)])
        anon = self.webapp.create_app().test_client()
        self.assertIn(self._post(anon).status_code, (401, 403))
        # 普通管理员（注册用户）也不许：它真的会用一个真实账号去登录一次
        from yiban.store import db as store_db
        store_db.create_user("admin2@test.local",
                             self.webapp.generate_password_hash("UserPass1234!"),
                             role="admin")
        c = self.webapp.create_app().test_client()
        self.assertEqual(c.post("/api/login", json={
            "username": "admin2@test.local", "password": "UserPass1234!"}).status_code, 200)
        csrf = c.get("/api/me").get_json()["csrf_token"]
        with mock.patch.object(self.webapp.signin, "verify_account") as verify:
            r = self._post(c, csrf=csrf)
        self.assertEqual(r.status_code, 403, r.get_data(as_text=True))
        verify.assert_not_called()

    def test_window_inside_is_409_and_costs_no_cooldown(self):
        self._seed_accounts([(self.PHONE, "active", False)])
        c = self._login()
        with mock.patch.object(self.webapp, "_in_sign_window", return_value=True), \
                self._no_network_no_sign_writes() as (verify, _audit, spies):
            r = self._post(c, csrf=c.csrf)
            self.assertEqual(r.status_code, 409, r.get_data(as_text=True))
            self.assertIn("窗口内", r.get_json()["error"])
            verify.assert_not_called()
            self._assert_no_sign_writes(spies)
        self.assertFalse(os.path.exists(self.measure_file),
                         "窗口内被拒不得消耗冷却（否则窗口一结束还要再等一次冷却）")

    def test_cooldown_second_call_is_429_with_remaining(self):
        self._seed_accounts([(self.PHONE, "active", False)])
        c = self._login()
        with mock.patch.object(self.webapp, "_in_sign_window", return_value=False), \
                self._no_network_no_sign_writes() as (verify, _audit, spies):
            self.assertEqual(self._post(c, csrf=c.csrf).status_code, 200)
            r2 = self._post(c, csrf=c.csrf)
            self.assertEqual(r2.status_code, 429, r2.get_data(as_text=True))
            body = r2.get_json()
            self.assertEqual(body["error"], "实测冷却中")
            self.assertLessEqual(body["next_allowed_in"], self.FULL_COOLDOWN)
            self.assertGreater(body["next_allowed_in"], 0, "必须给出还要等几秒")
            self.assertEqual(verify.call_count, 1, "冷却期内不得再真登录一次")
            self._assert_no_sign_writes(spies)

    def test_ok_path_structure_masking_and_margined_recommendation(self):
        self._seed_accounts([(self.PHONE, "active", False)])
        c = self._login()
        env_before = self._read_env()
        with mock.patch.object(self.webapp, "_in_sign_window", return_value=False), \
                self._no_network_no_sign_writes() as (verify, audit, spies):
            r = self._post(c, csrf=c.csrf, payload={"phone": self.PHONE})
            self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
            body = r.get_json()
            self._assert_no_sign_writes(spies)

        masked = self.webapp._mask_phone(self.PHONE)
        self.assertTrue(body["ok"])
        self.assertEqual(body["seconds"], 1.83)
        self.assertEqual(body["sample"], masked)
        self.assertEqual(body["cooldown_sec"], self.FULL_COOLDOWN)
        self.assertEqual(body["next_allowed_in"], 0, "本次已实测，不必再等")
        # 容量 = 文档公式（有效窗口 ÷ 单账号周期），不复用别的口径；
        # 建议值含余量：int(容量 × 2/3) 且严格小于容量
        win_sec = self.webapp._executors_window().full_sec()
        expected = (win_sec - 1) // (1 + self.DOC_GAP) + 1
        self.assertEqual(body["per_executor_capacity"], expected)
        self.assertEqual(body["recommended_per_executor"], int(expected * 2 / 3))
        self.assertLess(body["recommended_per_executor"], body["per_executor_capacity"])
        self.assertIn("建议", body["note"])
        self.assertIn("余量", body["note"])
        raw = json.dumps(body, ensure_ascii=False)
        self.assertNotIn(self.PHONE, raw, "响应只回打码号码")
        self.assertNotIn(SECRET_HOST, raw)

        # 拿去做实测的账号对象必须带库内 id（运行期复核"账号还在不在"要用它）
        account = verify.call_args[0][0]
        self.assertEqual(account.phone, self.PHONE)
        self.assertGreater(account.account_id, 0)

        # 审计只记打码号码与耗时
        detail = " ".join(str(a) for a in audit.call_args[0])
        self.assertIn(masked, detail)
        self.assertNotIn(self.PHONE, detail)
        self.assertIn("executors_measure", detail)

        # 冷却状态落盘（跨进程有效），且实测结果**不自动写回 .env**
        with open(self.measure_file, encoding="utf-8") as f:
            state = json.load(f)
        self.assertEqual(state["seconds"], 1.83)
        self.assertEqual(state["sample"], masked)
        self.assertEqual(self._read_env(), env_before,
                         "实测结果不自动落 .env（前端只拿数字填输入框，用户确认后再提交）")

    def test_unusable_or_unknown_account_is_404(self):
        self._seed_accounts([(self.PHONE, "active", False),
                             ("13800000022", "pending", False),
                             ("13800000023", "active", True)])
        c = self._login()
        for phone in ("13900000099",           # 库里没有
                      "13800000022",           # 未过审（按设计不产生任何易班请求）
                      "13800000023"):          # 用户自暂停
            with self.subTest(phone=phone), \
                    mock.patch.object(self.webapp, "_in_sign_window", return_value=False), \
                    self._no_network_no_sign_writes() as (verify, _audit, spies):
                r = self._post(c, csrf=c.csrf, payload={"phone": phone})
                self.assertEqual(r.status_code, 404, r.get_data(as_text=True))
                verify.assert_not_called()
                self._assert_no_sign_writes(spies)
        self.assertFalse(os.path.exists(self.measure_file), "没实测就不该占冷却")

    def test_failure_path_is_reported_without_inventing_numbers(self):
        """登录/拉任务失败 → 502 且回脱敏原因；此时冷却**已消耗**（真实登录已发生）。"""
        self._seed_accounts([(self.PHONE, "active", False)])
        c = self._login()
        with mock.patch.object(self.webapp, "_in_sign_window", return_value=False), \
                self._no_network_no_sign_writes(
                    verify_result=(False, "登录失败（账号或密码错误）" + chr(10) + "第二行")
                ) as (verify, _audit, spies):
            r = self._post(c, csrf=c.csrf)
            self.assertEqual(r.status_code, 502, r.get_data(as_text=True))
            self.assertIn("实测失败", r.get_json()["error"])
            self.assertNotIn(chr(10), r.get_json()["error"], "换行要折平（防注入）")
            verify.assert_called_once()
            self._assert_no_sign_writes(spies)
        self.assertTrue(os.path.exists(self.measure_file), "真实登录已发生，冷却必须生效")
        with open(self.measure_file, encoding="utf-8") as f:
            state = json.load(f)
        self.assertEqual(state["sample"], self.webapp._mask_phone(self.PHONE))


if __name__ == "__main__":
    unittest.main(verbosity=2)


class EgressErrorMustNotLeakCredentialsTest(_WebBase):
    """校验失败时的 `error` 文案不得回显代理凭据（前端复审 2026-09-17 提出的真实泄漏）。

    出口串按契约**允许带 `user:pass@`**，而错误文案会进 HTTP 响应、浏览器 DOM 与日志——
    所以回显前必须抹掉 userinfo。两条路径（整条写 / 按序号单段写）都要守住。
    """

    LEAKY = "http://leakuser:leakpass@" + chr(104) + "ost with space:8080"

    def _assert_no_credentials(self, text):
        self.assertNotIn("leakpass", text, "错误文案里出现了代理口令")
        self.assertNotIn("leakuser", text, "错误文案里出现了代理用户名")
        self.assertIn("***@", text, "应当用 ***@ 替代 userinfo，而不是整段消失")

    def test_whole_list_write_error_is_masked(self):
        c = self._login()
        r = c.put("/api/scheduler/executors",
                  json={"proxy_list": self.LEAKY, "confirm_password": ADMIN_PASS},
                  headers={"X-CSRF-Token": c.csrf})
        self.assertEqual(r.status_code, 400)
        self._assert_no_credentials(r.get_data(as_text=True))

    def test_single_slot_write_error_is_masked(self):
        c = self._login()
        r = c.put("/api/scheduler/executors/workers/1",
                  json={"egress": self.LEAKY, "confirm_password": ADMIN_PASS},
                  headers={"X-CSRF-Token": c.csrf})
        self.assertEqual(r.status_code, 400)
        self._assert_no_credentials(r.get_data(as_text=True))

    def test_fallback_slot_write_error_is_masked(self):
        c = self._login()
        r = c.put("/api/scheduler/executors/fallback",
                  json={"egress": self.LEAKY, "confirm_password": ADMIN_PASS},
                  headers={"X-CSRF-Token": c.csrf})
        self.assertEqual(r.status_code, 400)
        self._assert_no_credentials(r.get_data(as_text=True))
