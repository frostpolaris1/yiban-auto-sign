# -*- coding: utf-8 -*-
"""敏感口令门禁的统一入口（`_sensitive_password_gate`）行为契约。

被修的现实缺陷（活体复现）：以主管理员会话对 `POST /api/settings` 反复提交**错误**
`confirm_password`，200 次只拿到 58×403 + 142×429，**没有冷却、没有锁定、会话照旧
有效、随后正确口令照常通过**；唯一上限是通用 API 限速（60 次/10 秒 ≈ 6 次/秒）。而
每次 403 背后都是一整次 scrypt 口令哈希（实测 157ms/次），6 次/秒就能打满一核——
口令复核门比项目自己的登录口（10 次/60 秒 + 第 5 次锁 300 秒）**快约 38 倍且永不锁**，
等于绕开登录限速专门留了一个算力口子。

本文件钉住修复后的口径：

1. **一个入口**：系统开关门、执行体写门、高危二次鉴权（含其 20+ 调用点）全部走
   `_sensitive_password_gate`，三处不再各写各的判定。
2. **冷却**：独立计数达到阈值（`LOGIN_FAIL_NOTIFY`）后进入门禁级冷却，冷却期内**拒绝
   一切需要复核的写操作**（含正确口令——否则冷却可被"改用对口令"绕过），返回 429
   并留审计；冷却**不影响**登录、只读 GET、以及不需要复核的普通写操作。
3. **豁免**：近期（`YIBAN_PW_CONFIRM_TTL`，默认 300s、钳 0~900、0=关闭）在本会话内
   复核过口令、且出口 IP 与授权时一致 → 免再输口令。**只给配置类动作**；不可逆清除、
   关闭告警通道、角色变更、重置他人口令这类必须当次输口令。
4. **P18 语义原样保留**：门禁失败**绝不写** `_login_fails`——被窃会话不得用错口令把
   管理员锁在"登录"之外。

全程 mock / 纯本地（Flask test client），无任何网络请求。
用法（项目根目录）：
    python -m pytest tests/test_sensitive_gate_94.py -v
"""
import contextlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TEST_KEY = "a" * 64
ADMIN_PASS = "TestPass1234!"
WRONG_PASS = "WrongPass999!"


def _load_webapp():
    """**独立名字**加载 web/app.py：它在导入期把 ENV_FILE 等读成模块级常量，与别的
    测试文件共用同一模块对象会读到另一个 `.env`（单跑绿、全量红的老坑）。"""
    spec = importlib.util.spec_from_file_location(
        "webapp_sensitive_gate", os.path.join(BASE, "web", "app.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["webapp_sensitive_gate"] = mod
    with contextlib.suppress(Exception):
        spec.loader.exec_module(mod)
    return mod


class _GateBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-sensitive-gate-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(
                f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
                f"YIBAN_ADMIN_USER=admin\nYIBAN_ADMIN_PASSWORD={ADMIN_PASS}\n"
            )
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.accounts_file = os.path.join(cls.tmp, "accounts.json")
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_ACCOUNTS_FILE"] = cls.accounts_file
        os.environ["YIBAN_USERS_FILE"] = os.path.join(cls.tmp, "users.json")
        os.environ["YIBAN_DB_FILE"] = cls.db_file
        os.environ["YIBAN_STATE_DIR"] = cls.tmp
        os.environ["YIBAN_LOG_FILE"] = os.path.join(cls.tmp, "sign.log")
        os.environ["YIBAN_DISABLE_PURGE_LOOP"] = "1"
        global db
        import db
        cls.webapp = _load_webapp()

    @classmethod
    def tearDownClass(cls):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k in ("YIBAN_ACCOUNTS_KEY", "YIBAN_ENV_FILE", "YIBAN_ACCOUNTS_FILE",
                  "YIBAN_USERS_FILE", "YIBAN_DB_FILE", "YIBAN_STATE_DIR",
                  "YIBAN_LOG_FILE", "YIBAN_DISABLE_PURGE_LOOP"):
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
        db.init_db(self.db_file, migrate_from=self.accounts_file,
                   env_file=self.env_file)
        # 每用例回到"两开关均未配置（=开放）+ 门禁旋钮走默认值"基线；B 档三键同清——
        # 它们是豁免用例的"真变更"载体，上一条用例留下的值会让下一条根本不进门禁
        self.webapp.write_env_batch(self.env_file, {
            "YIBAN_GLOBAL_PAUSE": "", "YIBAN_REGISTRATION_PAUSE": "",
            "YIBAN_PW_CONFIRM_TTL": "", "YIBAN_PW_CONFIRM_COOLDOWN_SEC": "",
            "YIBAN_SIGN_ORDER": "", "YIBAN_SIGN_DIST": "", "YIBAN_SIGN_MODE": "",
        })
        self.alerts = []
        patcher = mock.patch.object(
            self.webapp, "send_notification",
            side_effect=lambda t, c, urgent=False, force=False, ledger=None:
            self.alerts.append((t, c, urgent)))
        patcher.start()
        self.addCleanup(patcher.stop)

    # ---- 工具 ----
    def _login(self, username="admin", password=None, app=None):
        c = (app or self.webapp.create_app()).test_client()
        r = c.post("/api/login", json={
            "username": username, "password": password or ADMIN_PASS})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        c.csrf = c.get("/api/me").get_json()["csrf_token"]
        return c

    def _hdr(self, c):
        return {"X-CSRF-Token": c.csrf}

    def _env_has(self, needle):
        with open(self.env_file, encoding="utf-8-sig") as f:
            return needle in f.read()

    def _rows(self, action):
        return db.get_conn().execute(
            "SELECT detail FROM audit_logs WHERE action=?", (action,)
        ).fetchall()

    def _fail_gate_times(self, c, n, *, endpoint="switch"):
        """连续 n 次用错误口令撞门禁，返回状态码列表。"""
        codes = []
        for _ in range(n):
            codes.append(self._attempt(c, endpoint, WRONG_PASS).status_code)
        return codes

    # 三种门禁落点（系统开关 / 执行体写 / 高危二次鉴权）在测试里等价可驱动
    def _attempt(self, c, endpoint, password):
        if endpoint == "switch":
            return c.post("/api/settings",
                          json={"global_pause": 1, "confirm_password": password},
                          headers=self._hdr(c))
        if endpoint == "executors":
            return c.post("/api/scheduler/executors/rows",
                          json={"proxy": "http://n:1", "confirm_password": password},
                          headers=self._hdr(c))
        if endpoint == "batch":  # 高危二次鉴权（always_required）
            return c.post("/api/users/batch",
                          json={"action": "delete", "emails": ["ghost@test.local"],
                                "confirm_password": password},
                          headers=self._hdr(c))
        raise AssertionError(endpoint)

    def _switch_with(self, c, password=None):
        body = {"global_pause": 1}
        if password is not None:
            body["confirm_password"] = password
        return c.post("/api/settings", json=body, headers=self._hdr(c))


class CooldownTest(_GateBase):
    """独立计数 + 首达阈值告警 + 门禁级冷却（回归护栏 ①②）。"""

    def test_threshold_failure_alerts_once_and_next_try_429(self):
        """①错口令 3 次 → 每次 403、告警恰 1 条；第 4 次进冷却返回 429。"""
        n = self.webapp.LOGIN_FAIL_NOTIFY
        c = self._login()
        codes = self._fail_gate_times(c, n)
        self.assertEqual(codes, [403] * n, f"阈值前每次都是口令不符 403，实际 {codes}")
        fails = [a for a in self.alerts if "二次鉴权失败" in a[0]]
        self.assertEqual(len(fails), 1, f"首达阈值只告警一次，实际 {self.alerts}")
        self.assertTrue(fails[0][2], "复核失败告警必须 urgent")
        self.assertIn("系统开关", fails[0][1], "告警须写明是哪个门禁")
        self.assertNotIn(WRONG_PASS, fails[0][1], "告警不得回显口令")
        r = self._attempt(c, "switch", WRONG_PASS)
        self.assertEqual(r.status_code, 429, r.get_data(as_text=True))

    def test_missing_password_denied_but_not_counted(self):
        """「没带口令」不占冷却预算：它一次散列都不做，计入阈值等于让攻击者用空请求
        把合法管理员的敏感操作预算刷光（与 _admin_delete_limited 修掉的运维 DoS 同类）。
        """
        c = self._login()
        codes = [self._switch_with(c, None).status_code for _ in range(6)]
        self.assertEqual(codes, [403] * 6, f"缺口令的拒绝仍是 403，实际 {codes}")
        self.assertEqual(self.alerts, [], "缺口令不得触发复核失败告警")
        # 预算完整：错口令仍是从第 4 次起才进冷却
        self.assertEqual(self._fail_gate_times(c, self.webapp.LOGIN_FAIL_NOTIFY),
                         [403] * self.webapp.LOGIN_FAIL_NOTIFY)
        self.assertEqual(self._attempt(c, "switch", WRONG_PASS).status_code, 429)

    def test_cooldown_rejects_correct_password_too(self):
        """②冷却期内正确口令也不放行——否则"改用对口令"就绕过了冷却。"""
        c = self._login()
        self._fail_gate_times(c, self.webapp.LOGIN_FAIL_NOTIFY)
        r = self._attempt(c, "switch", ADMIN_PASS)
        self.assertEqual(r.status_code, 429, "冷却期内对口令同样拒绝")
        self.assertFalse(self._env_has("YIBAN_GLOBAL_PAUSE=1"), "冷却期不得落盘")
        self.assertTrue(self._rows("sensitive_pw_cooldown"), "进入冷却必须留审计")

    def test_cooldown_spans_all_reconfirm_endpoints(self):
        """一处撞满阈值，另两处落点同样进冷却（同一入口、同一计数）。"""
        c = self._login()
        self._fail_gate_times(c, self.webapp.LOGIN_FAIL_NOTIFY, endpoint="switch")
        for endpoint in ("executors", "batch"):
            r = self._attempt(c, endpoint, ADMIN_PASS)
            self.assertEqual(r.status_code, 429,
                             f"{endpoint} 应与系统开关共用冷却（同一门禁入口）")

    def test_cooldown_leaves_login_reads_and_ungated_writes_alone(self):
        """冷却只封"需要复核的写"：登录、只读 GET、普通页与普通写操作一律照常。"""
        c = self._login()
        self._fail_gate_times(c, self.webapp.LOGIN_FAIL_NOTIFY)
        self.assertEqual(c.get("/api/me").status_code, 200, "只读 GET 不受冷却影响")
        self.assertEqual(c.get("/api/users").status_code, 200, "管理页数据不受冷却影响")
        self.assertEqual(c.get("/api/clock").status_code, 200, "公开只读接口不受冷却影响")
        # 值未变更 → 不在门禁范围内（不要求复核），冷却不该顺手把它也停了
        # （用 A 档的 sunday_sign 提交它的现值 0：档位高低都不该让"没改"的保存多一道口令）
        r = c.post("/api/settings", json={"sunday_sign": 0}, headers=self._hdr(c))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        # 登录完全可用：新会话用正确口令照常登录，用错误口令仍是"口令错"而不是"锁定"
        c2 = self.webapp.create_app().test_client()
        r = c2.post("/api/login", json={"username": "admin", "password": ADMIN_PASS})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        r = c2.post("/api/login", json={"username": "admin", "password": WRONG_PASS})
        self.assertEqual(r.status_code, 401, "门禁冷却不得占用登录锁定，错口令仍 401")

    def test_cooldown_expires_and_gate_reopens(self):
        """冷却窗口过后门禁重新可用（可配的短窗口，免睡长秒）。"""
        self.webapp.write_env_batch(self.env_file, {"YIBAN_PW_CONFIRM_COOLDOWN_SEC": "1"})
        c = self._login()
        self._fail_gate_times(c, self.webapp.LOGIN_FAIL_NOTIFY)
        self.assertEqual(self._attempt(c, "switch", ADMIN_PASS).status_code, 429)
        time.sleep(1.2)
        r = self._attempt(c, "switch", ADMIN_PASS)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(self._env_has("YIBAN_GLOBAL_PAUSE=1"))

    def test_cooldown_rearms_on_next_failure_after_expiry(self):
        """冷却到期后再错一次必须立刻重新布防。

        计数窗口（900 秒）比冷却长得多：若只在"恰好第 3 次"布防，冷却到期后的第
        4、5… 次失败既不再告警也不再被挡，攻击者就等于拿到了每 300 秒一次的免费
        口令散列批次——正是本次要堵的那个洞。
        """
        self.webapp.write_env_batch(self.env_file, {"YIBAN_PW_CONFIRM_COOLDOWN_SEC": "1"})
        c = self._login()
        self._fail_gate_times(c, self.webapp.LOGIN_FAIL_NOTIFY)
        time.sleep(1.2)
        self.assertEqual(self._attempt(c, "switch", WRONG_PASS).status_code, 403,
                         "冷却到期后这一次是「第 4 次失败」，仍要散列比对一次")
        r = self._attempt(c, "switch", ADMIN_PASS)
        self.assertEqual(r.status_code, 429, "第 4 次失败应立刻重新布防冷却")

    def test_zero_cooldown_keeps_alert_but_no_lockout(self):
        """冷却窗口 0 = 关闭冷却（告警与独立计数照旧），用于运维按 .env 降级。"""
        self.webapp.write_env_batch(self.env_file, {"YIBAN_PW_CONFIRM_COOLDOWN_SEC": "0"})
        c = self._login()
        self._fail_gate_times(c, self.webapp.LOGIN_FAIL_NOTIFY)
        self.assertEqual(self._attempt(c, "switch", ADMIN_PASS).status_code, 200)


class P18IsolationTest(_GateBase):
    """门禁失败绝不写 `_login_fails`（P18：被窃会话不得把管理员锁出登录）。"""

    def test_gate_failures_never_touch_login_counter(self):
        """门禁失败绝不写 `_login_fails`（P18）——且必须**在同一个 app 实例内**验证：
        两张计数表都是 create_app 的闭包字典，换 app 探测等于换了个内存桶，测不出串味。
        """
        app = self.webapp.create_app()
        c = self._login(app=app)
        # 远超登录锁定阈值（LOGIN_MAX_FAILS）次数的门禁失败
        codes = self._fail_gate_times(c, self.webapp.LOGIN_MAX_FAILS + 2)
        self.assertIn(429, codes, "阈值后应进冷却")
        self.assertNotIn(401, codes)
        probe = app.test_client()
        for _ in range(2):
            r = probe.post("/api/login", json={"username": "admin", "password": WRONG_PASS})
            self.assertEqual(r.status_code, 401,
                             f"登录侧的账没被门禁污染过，错口令仍应是 401：{r.get_data(as_text=True)}")
        r = probe.post("/api/login", json={"username": "admin", "password": ADMIN_PASS})
        self.assertEqual(r.status_code, 200, "管理员始终没被锁出登录")

    def test_reconfirm_path_no_longer_shares_login_bucket(self):
        """登录侧锁定不再牵连高危运维：旧实现读共享 `_login_fails` 的 lock_until，
        于是同出口的登录失败会把主管理员一并锁在**所有高危操作**之外（运维 DoS）；
        现在门禁只看自己的冷却表，登录锁着也照样能用正确口令做高危操作。"""
        c = self._login()
        for _ in range(self.webapp.LOGIN_MAX_FAILS):
            r = c.post("/api/login", json={"username": "admin", "password": WRONG_PASS})
        self.assertEqual(r.status_code, 429, "前置：登录侧此刻已锁定")
        r = c.post("/api/users/ghost@test.local/delete",
                   json={"mode": "full", "confirm_password": ADMIN_PASS},
                   headers=self._hdr(c))
        self.assertEqual(r.status_code, 404,
                         f"门禁应放行（登录侧锁定不得牵连高危运维），实际 {r.status_code} "
                         f"{r.get_data(as_text=True)}")
        self.assertIn("用户不存在", r.get_json()["error"])


class ExemptionTest(_GateBase):
    """配置类动作的"刚复核过就免再输"豁免，及其边界（IP / TTL / always_required）。"""

    def test_config_gate_exempt_within_ttl(self):
        """③先正确口令过一次，TTL 内改另一个**可豁免档位**的值不必再输口令。"""
        c = self._login()
        self.assertEqual(self._switch_with(c, None).status_code, 403,
                         "前置：未复核过的真变更必须要口令")
        self.assertEqual(self._switch_with(c, ADMIN_PASS).status_code, 200)
        # B 档真变更，但本会话刚复核过 → 免口令
        r = c.post("/api/settings", json={"sign_order": "random"}, headers=self._hdr(c))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(self._env_has("YIBAN_SIGN_ORDER=random"))
        # 同一份豁免对 A 档不生效（A 档必须当次输口令）
        r = c.post("/api/settings", json={"registration_pause": 1}, headers=self._hdr(c))
        self.assertEqual(r.status_code, 403, "A 档不得被豁免放行")
        self.assertFalse(self._env_has("YIBAN_REGISTRATION_PAUSE=1"))

    def test_executor_gate_shares_the_exemption(self):
        """豁免跨落点生效（同一入口的同一份会话凭据）。"""
        c = self._login()
        self.assertEqual(self._switch_with(c, ADMIN_PASS).status_code, 200)
        r = c.post("/api/scheduler/executors/rows", json={"proxy": "http://n:1"},
                   headers=self._hdr(c))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

    def test_exemption_not_granted_from_a_different_ip(self):
        """出口 IP 变了必须重新输口令——被窃 Cookie 换个出口就免检是不可接受的。

        载体用 B 档 sign_order（可豁免动作）：拿 A 档键测等于在测 always_required，
        豁免这条边界就没人管了。
        """
        c = self._login()
        self.assertEqual(self._switch_with(c, ADMIN_PASS).status_code, 200)
        with mock.patch.object(self.webapp, "_client_ip", return_value="203.0.113.9"):
            r = c.post("/api/settings", json={"sign_order": "random"},
                       headers=self._hdr(c))
        self.assertEqual(r.status_code, 403, "跨 IP 不得沿用豁免")
        self.assertFalse(self._env_has("YIBAN_SIGN_ORDER=random"))

    def test_exemption_disabled_by_ttl_zero(self):
        """`YIBAN_PW_CONFIRM_TTL=0` = 关闭豁免（每次都要口令）。"""
        self.webapp.write_env_batch(self.env_file, {"YIBAN_PW_CONFIRM_TTL": "0"})
        c = self._login()
        self.assertEqual(self._switch_with(c, ADMIN_PASS).status_code, 200)
        r = c.post("/api/settings", json={"sign_order": "random"}, headers=self._hdr(c))
        self.assertEqual(r.status_code, 403, "TTL=0 时不得豁免")

    def test_exemption_expires_after_ttl(self):
        """超过 TTL 后回到"每次都要口令"。"""
        self.webapp.write_env_batch(self.env_file, {"YIBAN_PW_CONFIRM_TTL": "1"})
        c = self._login()
        self.assertEqual(self._switch_with(c, ADMIN_PASS).status_code, 200)
        time.sleep(1.2)
        r = c.post("/api/settings", json={"sign_order": "random"}, headers=self._hdr(c))
        self.assertEqual(r.status_code, 403, "豁免到期后须重新复核")

    def _grant_exemption(self, c, i):
        """建立"本会话刚复核过口令"的豁免态：交替写 global_pause 的真变更 + 正确口令。
        （同值提交不进门禁，故必须交替，否则第二次起就没在建立豁免了。）"""
        r = c.post("/api/settings",
                   json={"global_pause": 1 if i % 2 == 0 else 0,
                         "confirm_password": ADMIN_PASS}, headers=self._hdr(c))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

    def test_exemption_never_granted_for_irreversible_or_alerting_actions(self):
        """③不可逆清除 / 角色变更 / 重置他人口令 / 关闭告警通道 / 改主管理员口令
        ——即便本会话刚复核过，也必须当次输口令。

        每例各建新 app：门禁冷却表是 per-app 的，共用一份会让第 4 例撞进冷却、
        把"豁免不该放行"这件事掩盖成 429。
        """
        cases = (
            ("POST", "/api/users/batch",
             {"action": "delete", "emails": ["ghost@test.local"]}),
            ("POST", "/api/users/ghost@test.local/role", {"role": "admin"}),
            ("POST", "/api/users/ghost@test.local/password", {"password": ADMIN_PASS}),
            ("POST", "/api/users/ghost@test.local/delete", {"mode": "full"}),
            ("POST", "/api/users/ghost@test.local/delete", {"mode": "accounts_only"}),
            ("POST", "/api/users/deleted/purge", {"emails": ["ghost@test.local"]}),
            ("POST", "/api/accounts/batch",
             {"action": "purge", "ids": [1], "h_idx": 0, "h_phone": "138****0000"}),
            ("PUT", "/api/mail-config", {"enabled": False}),
            ("PUT", "/api/notify-config", {"type": ""}),
        )
        for i, (method, path, body) in enumerate(cases):
            with self.subTest(path=path, body=body):
                c = self._login()
                self._grant_exemption(c, i)
                self.alerts.clear()
                r = c.open(path, method=method, json=body, headers=self._hdr(c))
                self.assertEqual(
                    r.status_code, 400,
                    f"{path} 属必须当次复核的动作，豁免不得放行：{r.get_data(as_text=True)}")
                self.assertIn("当前密码不正确", r.get_json()["error"])
                self.assertEqual(self.alerts, [], "被拒的高危动作不得发出任何变更告警")

    def test_self_password_change_never_uses_exemption(self):
        """改主管理员口令不是"配置写入"：豁免存在时仍须给出正确的当前口令。"""
        c = self._login()
        self.assertEqual(self._switch_with(c, ADMIN_PASS).status_code, 200)
        r = c.post("/api/me/password", json={
            "old_password": WRONG_PASS, "new_password": "FreshPass123!",
            "confirm_password": "FreshPass123!"}, headers=self._hdr(c))
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))

    def test_gate_params_clamped(self):
        """旋钮钳制：TTL 0~900（越界夹回），冷却非负；避免误配出一个永久豁免。"""
        for raw, want in (("99999", 900), ("-5", 0), ("120", 120), ("", 300)):
            with self.subTest(raw=raw):
                self.webapp.write_env_batch(self.env_file,
                                            {"YIBAN_PW_CONFIRM_TTL": raw})
                ttl, _cd = self.webapp._sensitive_gate_params(self.env_file)
                self.assertEqual(ttl, want)


class StoreHygieneTest(_GateBase):
    """门禁状态不得无界增长（海量不同键打不爆内存）。"""

    def test_gate_stores_trim_and_stay_bounded(self):
        limit = self.webapp._IP_STORE_LIMIT
        captured = []
        real_trim = self.webapp._ip_store_trim

        def spy(store, max_age):
            captured.append((store, max_age))
            return real_trim(store, max_age)

        c = self._login()
        with mock.patch.object(self.webapp, "_ip_store_trim", side_effect=spy):
            self._fail_gate_times(c, self.webapp.LOGIN_FAIL_NOTIFY)
        # 门禁表的键是 (ip, 用户名) 二元组；通用限速表是裸 IP 串，据此筛出门禁自己的表
        gate_stores = {id(s): s for s, _ in captured
                       if any(isinstance(k, tuple) and len(k) == 2 for k in s)}
        self.assertTrue(gate_stores, "门禁写入路径必须对计数表做 trim")
        now = time.time()
        self.assertTrue(
            any(v[-1] > now for s in gate_stores.values() for v in s.values()),
            "冷却状态表必须存在且同样被 trim（末位是未来时刻的解锁时间点）")
        stale = now - (self.webapp._IP_STORE_MAX_AGE + 3600)
        for store in gate_stores.values():
            for i in range(limit + 1):
                store[(f"203.0.113.{i % 251}", f"u{i}")] = (1, stale)
            self.assertGreater(len(store), limit, "前置：先撑出超限")
        # 最后一击换个出口 IP：否则该会话仍在冷却里，门禁只**读**不写计数表，
        # trim 不会被调用，测的就不是"写入路径有界"而是"拒绝路径提前返回"了。
        with mock.patch.object(self.webapp, "_ip_store_trim", side_effect=spy), \
             mock.patch.object(self.webapp, "_client_ip", return_value="198.51.100.7"):
            r = self._attempt(c, "switch", WRONG_PASS)
        self.assertEqual(r.status_code, 403, "前置：新出口 IP 不受旧键冷却影响")
        for store in gate_stores.values():
            self.assertLess(len(store), 10,
                            f"过期条目应被 trim 回收，实际残留 {len(store)} 条")


if __name__ == "__main__":
    unittest.main(verbosity=2)
