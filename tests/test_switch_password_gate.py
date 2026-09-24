# -*- coding: utf-8 -*-
"""系统开关口令门禁测试（global_pause / registration_pause 变更需 confirm_password）。

标签：E · Web：认证/权限/API
覆盖：`global_pause` / `registration_pause` 变更的口令复核门——缺口令、错口令、值未变三态，以及失败计数的隔离
对应实现：`web/app.py` 的统一门禁 `_sensitive_password_gate` 与 `/api/settings` 写路径
关键断言：只有「请求值≠服务端现值」才要口令（两个方向都算变更）；缺口令与错口令同为 403 但 `reason` 与文案必须分开（前端据此决定弹口令框还是提示输错）；被拒不落 `.env`、响应不回显口令、写一条 `settings_switch_pw_fail` 审计；错口令走独立计数并首达阈值告警一次，绝不写与登录共用的 `_login_fails`
依赖：纯本地 Flask test client + 临时 `.env`/SQLite，不联网、不访问真实易班接口；无需 node，每例新登录会话（不带短时豁免态）。无需 node

背景：此前前端口令框收集的 confirm_password 后端并不校验（假门），持主管理员
Cookie 的会话可无口令直接翻转 global_pause / registration_pause。

口径（2026-09）：
- 本门禁是统一入口 _sensitive_password_gate 的一个落点；仅当请求值与当前值**不同**时
  才要求 confirm_password，值未变（或未携带）不要求，其它字段的保存流程零影响；
- 缺口令 → 403 {"error":"需要输入当前口令，设置未生效","reason":"password_required"}；
  错口令 → 403 {"error":"口令校验未通过，设置未生效","reason":"password_incorrect"}。
  两档状态码相同、文案必须分开（前端据此决定弹口令框还是提示输错），响应都不回显口令，
  并写一条 settings_switch_pw_fail 审计；
- 错口令走**独立计数**：首达阈值告警一次，其后进入门禁级冷却（429）。仍**绝不写**与登录
  共用的 _login_fails（P18：持 Cookie 者不得借门禁把管理员锁出登录）。
  计数/冷却/豁免的完整口径见 tests/test_web_auth_security.py；本文件每例都用新登录的
  会话（无豁免态），钉的是"单次请求要不要口令"这层语义。

全程 mock / 纯本地（Flask test client），无任何网络请求。
用法（项目根目录）：
    python -m pytest tests/test_switch_password_gate.py -v
"""
import contextlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 告警/邮件正文入参已放宽为 layout.Mail | str，捕获点统一渲染成文本
from _mail_body import render_body  # noqa: E402

TEST_KEY = "a" * 64
ADMIN_PASS = "TestPass1234!"


class SwitchPasswordGateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-switch-pw-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(
                f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
                f"YIBAN_ADMIN_USER=admin\nYIBAN_ADMIN_PASSWORD={ADMIN_PASS}\n"
                # 本文件钉的是口令门的**机制**（当次要口令、失败文案与独立计数、
                # 冷却 429、豁免），故把档位固定在 full——默认档 risk 下这些动作
                # 不再当次要口令。默认档与 off 档由 tests/test_pw_gate_tiers.py 钉。
                "YIBAN_PW_GATE=full\n"
            )
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.accounts_file = os.path.join(cls.tmp, "accounts.json")
        cls.users_file = os.path.join(cls.tmp, "users.json")
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_ACCOUNTS_FILE"] = cls.accounts_file
        os.environ["YIBAN_USERS_FILE"] = cls.users_file
        os.environ["YIBAN_DB_FILE"] = cls.db_file
        os.environ["YIBAN_STATE_DIR"] = cls.tmp
        os.environ["YIBAN_LOG_FILE"] = os.path.join(cls.tmp, "logs", "sign.log")
        global db
        import db
        spec = importlib.util.spec_from_file_location(
            "webapp", os.path.join(BASE, "web", "app.py"))
        cls.webapp = importlib.util.module_from_spec(spec)
        sys.modules["webapp"] = cls.webapp
        with contextlib.suppress(Exception):
            spec.loader.exec_module(cls.webapp)

    @classmethod
    def tearDownClass(cls):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k in ("YIBAN_ACCOUNTS_KEY", "YIBAN_ENV_FILE", "YIBAN_ACCOUNTS_FILE",
                  "YIBAN_USERS_FILE", "YIBAN_DB_FILE", "YIBAN_STATE_DIR",
                  "YIBAN_LOG_FILE"):
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
        # 每用例回到"两开关均未配置（=开放）"基线
        self.webapp.write_env_batch(self.env_file, {
            "YIBAN_GLOBAL_PAUSE": "", "YIBAN_REGISTRATION_PAUSE": ""})

    @property
    def _fail_threshold(self):
        """门禁失败告警/冷却的起点：取门禁侧常量，不另抄一份字面量。

        抄一份就会与实现漂移——阈值改了而测试还在按旧值数次数，测的就不是实现。
        取的是 `SENSITIVE_PW_FAIL_NOTIFY` 而不是登录侧的 `LOGIN_FAIL_NOTIFY`：后者是
        "登录失败告警阈值"，与门禁的失败预算各调各的（见 web/app.py 的常量注释）。
        """
        return self.webapp.SENSITIVE_PW_FAIL_NOTIFY

    def _login(self):
        c = self.webapp.create_app().test_client()
        r = c.post("/api/login", json={"username": "admin", "password": ADMIN_PASS})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        token = c.get("/api/me").get_json()["csrf_token"]
        return c, {"X-CSRF-Token": token}

    def _env_has(self, needle):
        with open(self.env_file, encoding="utf-8-sig") as f:
            return needle in f.read()

    def _audit_fail_rows(self):
        return db.get_conn().execute(
            "SELECT detail FROM audit_logs WHERE action='settings_switch_pw_fail'"
        ).fetchall()

    # ---- registration_pause：任务口径四态 ----
    def test_reg_change_without_password_403(self):
        """改值无口令 → 403，.env 不变，失败审计留痕。"""
        c, hdr = self._login()
        r = c.post("/api/settings", json={"registration_pause": 1}, headers=hdr)
        self.assertEqual(r.status_code, 403, r.get_data(as_text=True))
        self.assertIn("需要输入当前口令", r.get_json()["error"])
        self.assertEqual(r.get_json()["reason"], "password_required")
        self.assertFalse(self._env_has("YIBAN_REGISTRATION_PAUSE=1"))
        self.assertEqual(len(self._audit_fail_rows()), 1)

    def test_reg_change_wrong_password_403(self):
        """错口令 → 403，.env 不变，响应不回显口令。"""
        c, hdr = self._login()
        r = c.post("/api/settings", json={
            "registration_pause": 1, "confirm_password": "WrongPass999!"},
            headers=hdr)
        self.assertEqual(r.status_code, 403, r.get_data(as_text=True))
        body = r.get_data(as_text=True)
        self.assertNotIn("WrongPass999!", body, "错误响应不得回显口令")
        self.assertIn("口令校验未通过", r.get_json()["error"])
        self.assertEqual(r.get_json()["reason"], "password_incorrect")
        self.assertFalse(self._env_has("YIBAN_REGISTRATION_PAUSE=1"))

    def test_reg_change_with_correct_password_200(self):
        """对口令 → 200 且生效（写入 .env）。"""
        c, hdr = self._login()
        r = c.post("/api/settings", json={
            "registration_pause": 1, "confirm_password": ADMIN_PASS}, headers=hdr)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(self._env_has("YIBAN_REGISTRATION_PAUSE=1"))

    def test_reg_same_value_without_password_200(self):
        """值未变（已是暂停）不带口令 → 200（未变值不要求复核）。"""
        self.webapp.write_env_batch(self.env_file, {"YIBAN_REGISTRATION_PAUSE": "1"})
        c, hdr = self._login()
        r = c.post("/api/settings", json={"registration_pause": 1}, headers=hdr)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(self._env_has("YIBAN_REGISTRATION_PAUSE=1"))

    # ---- global_pause：同口径抽查 ----
    def test_global_change_without_password_403(self):
        c, hdr = self._login()
        r = c.post("/api/settings", json={"global_pause": 1}, headers=hdr)
        self.assertEqual(r.status_code, 403, r.get_data(as_text=True))
        self.assertFalse(self._env_has("YIBAN_GLOBAL_PAUSE=1"))

    def test_global_change_with_correct_password_200(self):
        c, hdr = self._login()
        r = c.post("/api/settings", json={
            "global_pause": 1, "confirm_password": ADMIN_PASS}, headers=hdr)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(self._env_has("YIBAN_GLOBAL_PAUSE=1"))

    def test_global_same_value_without_password_200(self):
        self.webapp.write_env_batch(self.env_file, {"YIBAN_GLOBAL_PAUSE": "1"})
        c, hdr = self._login()
        r = c.post("/api/settings", json={"global_pause": 1}, headers=hdr)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

    def test_global_unpause_also_requires_password(self):
        """恢复（1→0）同为变更，同样要求口令——两个方向都不得无口令翻转。"""
        self.webapp.write_env_batch(self.env_file, {"YIBAN_GLOBAL_PAUSE": "1"})
        c, hdr = self._login()
        r = c.post("/api/settings", json={"global_pause": 0}, headers=hdr)
        self.assertEqual(r.status_code, 403, r.get_data(as_text=True))
        r2 = c.post("/api/settings", json={
            "global_pause": 0, "confirm_password": ADMIN_PASS}, headers=hdr)
        self.assertEqual(r2.status_code, 200, r2.get_data(as_text=True))
        self.assertFalse(self._env_has("YIBAN_GLOBAL_PAUSE=1"))

    # ---- 外溢面守卫 ----
    def test_gate_only_fires_on_real_changes(self):
        """门禁只咬"真变更"：同值保存零口令，真变更才按档位要口令。

        旧用例钉的是"非开关字段随便写、不要口令"——那是档位重排前的口径。现在
        A/B 档真变更一律过门禁（A 档还不得豁免），所以这里钉三件事：值未变不进门禁
        （整表回传/误点保存不该多一道口令）、B 档真变更无口令即拒、A 档真变更无口令即拒。
        """
        c, hdr = self._login()
        # B 档同值（未配过 sign_order → 生效值就是派生的 sequence）→ 不进门禁
        r = c.post("/api/settings", json={"sign_order": "sequence"}, headers=hdr)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        # B 档真变更且无口令 → 403，且不得落盘
        r = c.post("/api/settings", json={"sign_order": "random"}, headers=hdr)
        self.assertEqual(r.status_code, 403, r.get_data(as_text=True))
        self.assertFalse(self._env_has("YIBAN_SIGN_ORDER=random"))
        # A 档真变更且无口令 → 403（周末开关已上收 A 档）
        r = c.post("/api/settings", json={"sunday_sign": 1}, headers=hdr)
        self.assertEqual(r.status_code, 403, r.get_data(as_text=True))
        self.assertFalse(self._env_has("YIBAN_SUNDAY_SIGN=1"))
        # A 档同值 → 仍不进门禁
        r = c.post("/api/settings", json={"sunday_sign": 0}, headers=hdr)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

    def test_wrong_password_does_not_touch_login_fail_counter(self):
        """P18 回归守卫：门禁错口令绝不写与登录共用的 _login_fails。

        新语义下第 4 次起是**门禁级冷却**（429），不再是永远 403；要钉的不变量没变——
        被窃会话不得借门禁把管理员锁出登录：登录侧既不被锁（正确口令照登），
        也不被误判成锁定（错口令仍是 401 而不是 429）。
        """
        c, hdr = self._login()
        th = self._fail_threshold
        codes = []
        for _ in range(th):
            r = c.post("/api/settings", json={
                "registration_pause": 1, "confirm_password": "WrongPass999!"},
                headers=hdr)
            codes.append(r.status_code)
        self.assertEqual(codes, [403] * th, "阈值内每次都是口令错（未进冷却）")
        self.assertFalse(self._env_has("YIBAN_REGISTRATION_PAUSE=1"), "被拒不得落盘")
        # 登录侧完好无损（同一个 app：_login_fails 是 create_app 的闭包字典，
        # 换 app 探测等于换了个内存桶，测不出门禁有没有污染登录的账）
        c2 = c.application.test_client()
        r = c2.post("/api/login", json={"username": "admin", "password": "WrongPass999!"})
        self.assertEqual(r.status_code, 401, "错口令仍是口令错，不是被门禁连带锁定")
        r = c2.post("/api/login", json={"username": "admin", "password": ADMIN_PASS})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

    def test_sensitive_pw_fail_alerts_once_at_threshold(self):
        """统一门禁：口令复核失败走独立计数，首达阈值发一次紧急告警。

        用户裁决（P18 延续）：不写 _login_fails（不锁管理员），另开独立计数 +
        连续失败告警。连错阈值次：每次仍 403，仅达阈值那一次触发
        send_notification 一次（每窗口一次），且有审计留痕。
        """
        th = self._fail_threshold
        c, hdr = self._login()
        with mock.patch.object(self.webapp, "send_notification") as m:
            for _ in range(th):
                r = c.post("/api/settings", json={
                    "registration_pause": 1, "confirm_password": "WrongPass999!"},
                    headers=hdr)
                self.assertEqual(r.status_code, 403, r.get_data(as_text=True))
        self.assertEqual(m.call_count, 1, "首达阈值只告警一次")
        title, body, kw = m.call_args[0][0], render_body(m.call_args[0][1]), m.call_args[1]
        self.assertIn("二次鉴权失败", title, "三处落点共用同一条告警标题")
        self.assertTrue(kw.get("urgent"), "敏感操作复核失败应走紧急告警")
        self.assertIn("破坏性设置", body, "A 档门禁须写明档位")
        self.assertIn("暂停注册", body, "A 档门禁还须点出被尝试的具体键")
        self.assertNotIn("WrongPass999!", body, "告警不得回显口令")
        self.assertTrue(self._audit_fail_rows(), "失败必须留审计")

    def test_sensitive_pw_fail_no_repeat_alert_in_same_window(self):
        """同一窗口内超阈值后再多失败也不重复告警（避免刷屏），改为进冷却拒绝。"""
        th = self._fail_threshold
        c, hdr = self._login()
        codes = []
        with mock.patch.object(self.webapp, "send_notification") as m:
            for _ in range(th + 2):
                r = c.post("/api/settings", json={
                    "registration_pause": 1, "confirm_password": "WrongPass999!"},
                    headers=hdr)
                codes.append(r.status_code)
        self.assertEqual(m.call_count, 1,
                         "窗口未滚动时超阈值再多失败也只告警一次")
        self.assertEqual(codes[:th], [403] * th)
        self.assertEqual(codes[th:], [429] * 2,
                         f"超阈值后应被冷却挡住，实际 {codes}")
        self.assertFalse(self._env_has("YIBAN_REGISTRATION_PAUSE=1"))


if __name__ == "__main__":
    unittest.main()
