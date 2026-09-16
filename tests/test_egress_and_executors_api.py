# -*- coding: utf-8 -*-
"""出口分配（`yiban/egress.py`）与执行体接口（`/api/scheduler/executors`）的断言。

多执行体上线后，部署者要能给**每个执行体（含兜底常驻执行体）**单独配出口，
也可以留空走本机出口。这里钉住三件事：

1. **分配规则唯一**：列表按序取、不足循环、空位=直连、未配列表退回单出口——
   拉起执行体的一方与展示接口必须得到同一答案；
2. **不泄漏凭据**：代理串可能带 `user:pass@`，日志与接口只允许出现
   `scheme://host[:port]`（`describe()`）；接口另外只许主管理员访问；
3. **建议值不编数字**：没实测就没有建议（`recommended` 为 null），实测了才按
   实测 × 2/3 给建议，且文案说明"是建议不是上限"。
"""
import contextlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

from yiban import egress  # noqa: E402

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


class ExecutorsSaveEndpointTest(_WebBase):
    """写路径：只写 .env、非法值不落盘、审计不含凭据。"""

    def _read_env(self):
        return dict(ln.split("=", 1) for ln in
                    open(self.env_file, encoding="utf-8").read().splitlines()
                    if "=" in ln and not ln.startswith("#"))

    def test_put_requires_master_admin(self):
        c = self.webapp.create_app().test_client()
        r = c.put("/api/scheduler/executors", json={"workers": 2},
                  headers={"X-CSRF-Token": "x"})
        self.assertIn(r.status_code, (401, 403))

    def test_put_writes_workers_and_proxies(self):
        c = self._login()
        r = c.put("/api/scheduler/executors", headers={"X-CSRF-Token": c.csrf}, json={
            "workers": 5,
            "proxy_list": "http://p1:1,http://p2:2",
            "proxy_fallback": "http://fb:8080",
            "capacity_measured": 400,
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
                r = c.put("/api/scheduler/executors", json=payload,
                          headers={"X-CSRF-Token": c.csrf})
                self.assertEqual(r.status_code, 400, payload)
        self.assertEqual(self._read_env(), before, "校验失败不得落盘")

    def test_put_empty_clears_measured(self):
        c = self._login()
        c.put("/api/scheduler/executors", headers={"X-CSRF-Token": c.csrf},
              json={"capacity_measured": 0})
        self.assertEqual(self._read_env().get("YIBAN_CAPACITY_MEASURED", ""), "")
        self.assertIsNone(c.get("/api/scheduler/executors").get_json()["measured"])

    def test_audit_records_keys_not_credentials(self):
        """审计链不得出现代理凭据（只记键名）。"""
        c = self._login()
        from unittest import mock as _mock
        with _mock.patch.object(self.webapp.db, "audit", return_value=True) as m:
            c.put("/api/scheduler/executors", headers={"X-CSRF-Token": c.csrf},
                  json={"proxy_list": f"{SECRET_PROXY},,http://c:3"})
        detail = " ".join(str(a) for a in m.call_args[0])
        for secret in ("svcuser", "svcp", SECRET_PROXY):
            self.assertNotIn(secret, detail)
        self.assertIn("YIBAN_PROXY_LIST", detail)


if __name__ == "__main__":
    unittest.main(verbosity=2)
