# -*- coding: utf-8 -*-
"""安全核心回归（2026-09-05）。

七项修复：
1. H-1 XSS：_doc_page 对 base_path（request.script_root）/icp_text/police_text
   统一 html.escape(quote=True) 后再拼模板（转义收敛在函数内）；
2. H-2 告警致盲（裁决 A 全量收口）：notify-config 的 cooldown/urgent_only/
   daily_max/urgent_daily_max 纳入二次鉴权；mail-config / notify-config 的
   "配置变更告警"改为**先告警后落盘**并 force=True（绕过节流与当日额度）；
   （2026-09-08 修订：notify-config 告警时序与 mail-config 对齐为落盘后发，
   见 test_notify_config_alert_sent_after_write_with_force）；
3. M1 编辑回审（裁决 C）：用户/管理员改绑手机号一律回 pending 重审；
   仅密码/识别码变更（phone 不变）状态不变；
4. M2 历史数据隔离：my-calendar / my-logs 仅回显 active 且未删除账号的历史
   （/api/my-accounts 列表展示口径不变）；
5. M3 accounts_only 门禁（裁决 A）：清空用户账号接入 _high_risk_gate（与 full 对齐）；
6. M9 枚举文案（裁决 A）：注册注销冷却期文案与「该邮箱已注册」逐字一致；
7. M10 cookie path：.env 设 YIBAN_BASE_PATH 时会话 Cookie 收窄到挂载前缀。

全程 mock / 纯本地（Flask test client），无任何网络请求。
用法（项目根目录）：py -m pytest tests/test_batch18_fixes_0905.py -v
"""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TEST_KEY = "a" * 64
ADMIN_PASS = "MasterPass#2026"   # 15 位四类，满足主管理员 12/3 策略
USER_PASS = "UserPass123!"       # 注册用户口径：10 位两类
PHONE = "13800138001"
REBIND_PHONE = "13900139000"
CAL_MONTH = "2026-09"
CAL_DATE = "2026-09-15"
LOG_LINE = f"[{CAL_DATE} 10:00:00] [INFO] yiban: [{PHONE}] 签到成功"


class Batch18FixesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-batch18-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        cls._env_content = (
            f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
            f"YIBAN_ADMIN_USER=admin\nYIBAN_ADMIN_PASSWORD={ADMIN_PASS}\n"
        )
        with io.open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(cls._env_content)
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.accounts_file = os.path.join(cls.tmp, "accounts.json")
        cls.users_file = os.path.join(cls.tmp, "users.json")
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_ACCOUNTS_FILE"] = cls.accounts_file
        os.environ["YIBAN_USERS_FILE"] = cls.users_file
        os.environ["YIBAN_DB_FILE"] = cls.db_file
        os.environ["YIBAN_STATE_DIR"] = cls.tmp
        global db
        import db
        spec = importlib.util.spec_from_file_location("webapp", os.path.join(BASE, "web", "app.py"))
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
                  "YIBAN_USERS_FILE", "YIBAN_DB_FILE", "YIBAN_STATE_DIR"):
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

    # ---- 辅助 ----
    def _login(self, c, username, password):
        r = c.post("/api/login", json={"username": username, "password": password})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return c.get("/api/me").get_json()["csrf_token"]

    def _admin_client(self):
        c = self.webapp.create_app().test_client()
        return c, self._login(c, "admin", ADMIN_PASS)

    def _make_formal_user(self, email, phone):
        """构造「正式用户」：注册 + 提交账号 + 管理员审核通过（active）。"""
        db.create_user(email, self.webapp.generate_password_hash(USER_PASS))
        c = self.webapp.create_app().test_client()
        t = self._login(c, email, USER_PASS)
        r = c.post("/api/my-accounts", json={"name": "n", "phone": phone, "password": "p"},
                   headers={"X-CSRF-Token": t})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        ac, at = self._admin_client()
        accounts = db.load_accounts()
        idx = next(i for i, a in enumerate(accounts) if a["phone"] == phone)
        r = ac.post(f"/api/accounts/{idx}/review", json={"action": "approve"},
                    headers={"X-CSRF-Token": at})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

    def _write_daily_state(self):
        """构造当日签到状态文件（日历数据源）：STATE_DIR/sign-daily-YYYY-MM-DD.json。"""
        path = os.path.join(self.tmp, f"sign-daily-{CAL_DATE}.json")
        with io.open(path, "w", encoding="utf-8") as f:
            json.dump({PHONE: "✅"}, f, ensure_ascii=False)
        return path

    def _write_date_log(self):
        """构造当日按天日志文件（my-logs 数据源），测试后清理。"""
        path = self.webapp.log_path_for(CAL_DATE)
        with io.open(path, "w", encoding="utf-8") as f:
            f.write(LOG_LINE + "\n")
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        return path

    def _last_audit_detail(self, action):
        with db._conn_lock:
            conn = db.get_conn()
            row = conn.execute(
                "SELECT detail FROM audit_logs WHERE action=? ORDER BY id DESC LIMIT 1",
                (action,),
            ).fetchone()
        return row["detail"] if row else None

    # =====================================================================
    # 1. H-1 XSS：_doc_page 转义收敛
    # =====================================================================
    def test_privacy_page_reflected_xss_escaped(self):
        """验收用例：构造任意前缀路径 /x"><script>…/privacy → 响应不含未转义脚本。"""
        c = self.webapp.create_app().test_client()
        r = c.get('/x"><script>alert(1)</script>/privacy')
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        body = r.get_data(as_text=True)
        self.assertNotIn("<script>alert(1)", body, "注入的脚本不得以未转义形态出现在响应体")
        self.assertIn("&lt;script&gt;alert(1)", body, "base_path 应被 HTML 转义后输出")

    def test_doc_page_icp_police_escaped(self):
        """备案文本含引号/尖括号时同样转义（quote=True 覆盖属性上下文）。"""
        env2 = os.path.join(self.tmp, "env-icp.env")
        with io.open(env2, "w", encoding="utf-8") as f:
            f.write(self._env_content
                    + 'YIBAN_ICP_INFO="><svg onload=alert(2)>\n'
                    + "YIBAN_POLICE_INFO=<img src=x onerror=alert(3)>\n")
        with mock.patch.object(self.webapp, "ENV_FILE", env2):
            c = self.webapp.create_app().test_client()
            # icp_info/police_info 在请求时读 ENV_FILE → GET 也必须在 patch 内
            r = c.get("/privacy")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        body = r.get_data(as_text=True)
        # 模板自身含合法的 <img src="/gongan-beian.png"> 徽标，只断言注入载荷不出现
        self.assertNotIn("<svg", body)
        self.assertNotIn("<img src=x", body)
        self.assertIn("&lt;svg", body)
        self.assertIn("&lt;img src=x", body)

    # =====================================================================
    # 2. H-2 告警致盲：额度/节流参数收口 + 先告警后落盘
    # =====================================================================
    def test_notify_cooldown_without_password_400_no_write_no_alert(self):
        """验收用例：无 confirm_password 调 PUT notify-config {"cooldown":90000} → 400，
        零写入、零告警。"""
        ac, at = self._admin_client()
        before = self.webapp.read_env(self.env_file)
        with mock.patch.object(self.webapp, "send_notification") as sn:
            r = ac.put("/api/notify-config", json={"cooldown": 90000},
                       headers={"X-CSRF-Token": at})
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        self.assertIn("当前密码不正确", r.get_json()["error"])
        self.assertEqual(self.webapp.read_env(self.env_file), before, "鉴权失败必须零写入")
        sn.assert_not_called()

    def test_notify_cooldown_with_password_200_and_audited(self):
        """带正确口令 → 200、落盘、留痕，并按新参数记审计。"""
        ac, at = self._admin_client()
        r = ac.put("/api/notify-config", json={"cooldown": 90000, "confirm_password": ADMIN_PASS},
                   headers={"X-CSRF-Token": at})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        env = self.webapp.read_env(self.env_file)
        self.assertEqual(env.get("YIBAN_NOTIFY_COOLDOWN"), "90000")
        detail = self._last_audit_detail("notify_config")
        self.assertIsNotNone(detail)
        self.assertEqual(json.loads(detail)["cooldown"], 90000)

    def test_notify_config_alert_sent_after_write_with_force(self):
        """notify-config 变更告警在落盘成功之后发出 + force=True。

        原契约"先告警后落盘"（防新写入的额度/节流参数吞掉告警）不成立：
        force=True 本就绕过两侧节流；先发反而让写入失败（500）时运营者收到
        一条描述从未生效变更的通知。落盘成功后必须仍发告警、urgent=True。
        """
        ac, at = self._admin_client()
        order = []
        real_write = self.webapp.write_env_batch

        def _write_spy(env_path, updates):
            order.append("write")
            return real_write(env_path, updates)

        sn = mock.Mock(side_effect=lambda t, c, **kw: order.append(("alert", kw.get("force"))))
        with mock.patch.object(self.webapp, "send_notification", sn), \
             mock.patch.object(self.webapp, "write_env_batch", _write_spy):
            r = ac.put("/api/notify-config", json={"cooldown": 60, "confirm_password": ADMIN_PASS},
                       headers={"X-CSRF-Token": at})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(len(order), 2, f"应恰好一次落盘 + 一次告警，实际 {order}")
        self.assertEqual(order[0], "write", "告警只能描述已落盘的事实：先写入后告警")
        self.assertEqual(order[1][0], "alert", "落盘成功后必须发出变更告警")
        self.assertTrue(order[1][1], "变更告警必须 force=True")
        self.assertEqual(sn.call_args.args[0], "消息推送配置变更告警")
        self.assertTrue(sn.call_args.kwargs.get("urgent"))

    def test_notify_config_write_failure_500_without_alert(self):
        """落盘失败（磁盘错）→ 500、零告警、零审计：告警与留痕只能描述已生效的变更。"""
        ac, at = self._admin_client()
        before = self.webapp.read_env(self.env_file)
        with mock.patch.object(self.webapp, "send_notification") as sn, \
             mock.patch.object(self.webapp, "write_env_batch",
                               side_effect=RuntimeError("disk full")):
            r = ac.put("/api/notify-config", json={"cooldown": 60, "confirm_password": ADMIN_PASS},
                       headers={"X-CSRF-Token": at})
        self.assertEqual(r.status_code, 500, r.get_data(as_text=True))
        sn.assert_not_called()
        self.assertIsNone(self._last_audit_detail("notify_config"),
                          "写入失败不得留下描述未生效变更的审计行")
        self.assertEqual(self.webapp.read_env(self.env_file), before,
                         "写入失败不得改动 .env")

    def test_mail_config_alert_sent_after_write_with_force(self):
        """mail-config 变更告警在落盘成功之后发出 + force=True（安全审查 2026-09-08）。

        原契约"先告警后落盘"的理由（防新写入的节流参数吞掉告警）不成立：
        force=True 本就绕过两侧节流；先发反而会在加密/写盘失败（500）时外发一条
        描述从未生效变更的"配置已变更"通知。落盘成功后必须仍发告警。
        """
        ac, at = self._admin_client()
        order = []
        real_write = self.webapp.write_env_batch

        def _write_spy(env_path, updates):
            order.append("write")
            return real_write(env_path, updates)

        sn = mock.Mock(side_effect=lambda t, c, **kw: order.append(("alert", kw.get("force"))))
        with mock.patch.object(self.webapp, "send_notification", sn), \
             mock.patch.object(self.webapp, "write_env_batch", _write_spy):
            r = ac.put("/api/mail-config", json={"enabled": False, "confirm_password": ADMIN_PASS},
                       headers={"X-CSRF-Token": at})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(len(order), 2, f"应恰好一次落盘 + 一次告警，实际 {order}")
        self.assertEqual(order[0], "write", "告警只能描述已落盘的事实：先写入后告警")
        self.assertEqual(order[1][0], "alert", "落盘成功后必须发出变更告警")
        self.assertTrue(order[1][1], "变更告警必须 force=True")
        self.assertEqual(sn.call_args.args[0], "邮件配置变更告警")

    def test_mail_config_close_without_password_400_no_alert(self):
        """mail-config 关闭动作未带口令 → 400、零写入、零告警（先验口令才发告警）。"""
        ac, at = self._admin_client()
        with mock.patch.object(self.webapp, "send_notification") as sn:
            r = ac.put("/api/mail-config", json={"enabled": False},
                       headers={"X-CSRF-Token": at})
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        sn.assert_not_called()
        self.assertNotIn("YIBAN_MAIL_ENABLE=0", self.webapp.read_env(self.env_file))

    def test_notify_alert_after_urgent_daily_max_1_still_sent(self):
        """验收用例：urgent_daily_max=1 落盘后，后续变更告警仍能发出（force 绕过新额度）。"""
        ac, at = self._admin_client()
        h = {"X-CSRF-Token": at}
        r = ac.put("/api/notify-config", json={
            "type": "serverchan", "secret": "SCT406257TESTTESTTESTTEST",
            "confirm_password": ADMIN_PASS}, headers=h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        r = ac.put("/api/notify-config",
                   json={"urgent_daily_max": 1, "confirm_password": ADMIN_PASS}, headers=h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        # 走真实 send_notification → 断言 notify.send 收到 force=True（不被刚写入的额度吞掉）
        with mock.patch.object(self.webapp.notify, "send") as nsend, \
             mock.patch.object(self.webapp.mailer, "send_admin_alert"):
            r = ac.put("/api/notify-config",
                       json={"cooldown": 60, "confirm_password": ADMIN_PASS}, headers=h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        nsend.assert_called_once()
        self.assertTrue(nsend.call_args.kwargs.get("force"), "force 必须透传到 notify.send")
        self.assertTrue(nsend.call_args.kwargs.get("urgent"))

    # =====================================================================
    # 3. M1 编辑回审：改绑一律回 pending 重审
    # =====================================================================
    def test_user_rebind_resets_pending_and_audited(self):
        """用户改绑手机号：ACTIVE 号也回 pending，返回"重新提交"提示，审计带改绑回审。"""
        self._make_formal_user("u1@test.local", PHONE)
        c = self.webapp.create_app().test_client()
        t = self._login(c, "u1@test.local", USER_PASS)
        mine = c.get("/api/my-accounts").get_json()["accounts"]
        self.assertEqual(mine[0]["status"], "active", "前置：审核通过为 active")
        r = c.put(f"/api/my-accounts/{mine[0]['index']}",
                  json={"name": "n", "phone": REBIND_PHONE, "password": ""},
                  headers={"X-CSRF-Token": t})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json()["msg"], "已重新提交，等待管理员审核")
        acc = next(a for a in db.load_accounts() if a["owner"] == "u1@test.local")
        self.assertEqual(acc["phone"], REBIND_PHONE)
        self.assertEqual(acc["status"], "pending", "改绑后必须回待审核")
        detail = self._last_audit_detail("my_account_update")
        self.assertIn("改绑回审", detail or "")

    def test_user_password_only_edit_keeps_active(self):
        """仅改密码（phone 不变）：状态保持 active，返回"已保存"。"""
        self._make_formal_user("u2@test.local", PHONE)
        c = self.webapp.create_app().test_client()
        t = self._login(c, "u2@test.local", USER_PASS)
        mine = c.get("/api/my-accounts").get_json()["accounts"]
        r = c.put(f"/api/my-accounts/{mine[0]['index']}",
                  json={"name": "n", "phone": PHONE, "password": "newpass123"},
                  headers={"X-CSRF-Token": t})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json()["msg"], "已保存")
        acc = next(a for a in db.load_accounts() if a["owner"] == "u2@test.local")
        self.assertEqual(acc["status"], "active", "非改绑编辑不得回审")

    def test_admin_rebind_resets_pending_and_clears_reason(self):
        """管理员改绑用户的号：同样回 pending（由管理员再批），并清除旧拒绝理由。"""
        db.add_account({"name": "A", "phone": PHONE, "password": "pw",
                        "status": "active", "owner": "u3@test.local"})
        ac, at = self._admin_client()
        row = db.load_accounts()[0]
        snap = json.dumps({
            "name": row["name"], "phone": row["phone"],
            "phone_model": row.get("phone_model", ""),
            "status": row["status"], "deleted": bool(row.get("deleted")),
        }, ensure_ascii=False)
        r = ac.put("/api/accounts/0",
                   json={"name": "A", "phone": REBIND_PHONE, "password": "", "_snapshot": snap},
                   headers={"X-CSRF-Token": at})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        acc = db.load_accounts()[0]
        self.assertEqual(acc["phone"], REBIND_PHONE)
        self.assertEqual(acc["status"], "pending", "管理员改绑同样回待审核")
        self.assertEqual(acc.get("reject_reason", ""), "", "回审应清除旧拒绝理由")
        detail = self._last_audit_detail("account_update")
        self.assertIn("改绑回审", detail or "")

    def test_admin_edit_same_phone_keeps_status(self):
        """管理员编辑不改绑（仅名称/密码）：状态保持不变。"""
        db.add_account({"name": "A", "phone": PHONE, "password": "pw",
                        "status": "active", "owner": "admin"})
        ac, at = self._admin_client()
        r = ac.put("/api/accounts/0",
                   json={"name": "A2", "phone": PHONE, "password": "newpass123"},
                   headers={"X-CSRF-Token": at})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        acc = db.load_accounts()[0]
        self.assertEqual(acc["name"], "A2")
        self.assertEqual(acc["status"], "active", "非改绑编辑不得回审")

    # =====================================================================
    # 4. M2 历史数据隔离：pending 行的历史不回显
    # =====================================================================
    def test_my_calendar_hides_pending_history(self):
        """pending 行提交后 my-calendar 不返回该号历史（验收用例）。"""
        self._write_daily_state()
        db.create_user("m2a@test.local", self.webapp.generate_password_hash(USER_PASS))
        c = self.webapp.create_app().test_client()
        t = self._login(c, "m2a@test.local", USER_PASS)
        r = c.post("/api/my-accounts", json={"name": "n", "phone": PHONE, "password": "p"},
                   headers={"X-CSRF-Token": t})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        data = c.get(f"/api/my-calendar?month={CAL_MONTH}").get_json()
        self.assertIn(CAL_DATE, data["days"])
        self.assertEqual(data["days"][CAL_DATE], {}, "pending 号的历史状态不得回显")
        # 列表展示口径不变：my-accounts 仍含该行（展示 pending 状态用）
        mine = c.get("/api/my-accounts").get_json()["accounts"]
        self.assertEqual(mine[0]["phone"], PHONE)
        self.assertEqual(mine[0]["status"], "pending")

    def test_my_calendar_shows_active_history_after_approve(self):
        """对照：审核通过（active）后日历正常回显该号历史。"""
        self._write_daily_state()
        self._make_formal_user("m2b@test.local", PHONE)
        c = self.webapp.create_app().test_client()
        self._login(c, "m2b@test.local", USER_PASS)
        data = c.get(f"/api/my-calendar?month={CAL_MONTH}").get_json()
        self.assertEqual(data["days"][CAL_DATE], {PHONE: "✅"})

    def test_my_logs_hide_pending_and_show_active(self):
        """my-logs 同口径：pending 不回显，active 回显（脱敏形态）。"""
        self._write_date_log()
        db.create_user("m2c@test.local", self.webapp.generate_password_hash(USER_PASS))
        c = self.webapp.create_app().test_client()
        t = self._login(c, "m2c@test.local", USER_PASS)
        r = c.post("/api/my-accounts", json={"name": "n", "phone": PHONE, "password": "p"},
                   headers={"X-CSRF-Token": t})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        data = c.get(f"/api/my-logs?date={CAL_DATE}").get_json()
        self.assertEqual(data["logs"], [], "pending 号的日志不得回显")
        # 管理员审核通过 → 历史日志恢复可见
        ac, at = self._admin_client()
        idx = next(i for i, a in enumerate(db.load_accounts()) if a["phone"] == PHONE)
        r = ac.post(f"/api/accounts/{idx}/review", json={"action": "approve"},
                    headers={"X-CSRF-Token": at})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        data = c.get(f"/api/my-logs?date={CAL_DATE}").get_json()
        self.assertEqual(len(data["logs"]), 1)
        self.assertIn("138****8001", data["logs"][0], "回显行保持出站脱敏")

    # =====================================================================
    # 5. M3 accounts_only 门禁
    # =====================================================================
    def test_accounts_only_without_password_400_accounts_intact(self):
        """无口令调 accounts_only → 400，账号原封不动。"""
        db.create_user("m3@test.local", self.webapp.generate_password_hash(USER_PASS))
        db.add_account({"name": "A", "phone": PHONE, "password": "pw",
                        "status": "active", "owner": "m3@test.local"})
        ac, at = self._admin_client()
        r = ac.post("/api/users/m3@test.local/delete",
                    json={"mode": "accounts_only"}, headers={"X-CSRF-Token": at})
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        self.assertIn("当前密码不正确", r.get_json()["error"])
        self.assertEqual(len(db.load_accounts()), 1, "鉴权失败不得清空账号")

    def test_accounts_only_with_password_clears_accounts_keeps_user(self):
        """带正确口令 → 200：账号全部清空、用户保留可重新提交（与 full 语义分界）。"""
        db.create_user("m3b@test.local", self.webapp.generate_password_hash(USER_PASS))
        db.add_account({"name": "A", "phone": PHONE, "password": "pw",
                        "status": "active", "owner": "m3b@test.local"})
        ac, at = self._admin_client()
        r = ac.post("/api/users/m3b@test.local/delete",
                    json={"mode": "accounts_only", "confirm_password": ADMIN_PASS},
                    headers={"X-CSRF-Token": at})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(db.load_accounts(), [], "账号应被清空")
        self.assertIsNotNone(db.find_user("m3b@test.local"), "用户本体保留")

    # =====================================================================
    # 6. M9 枚举文案：注销冷却期与「该邮箱已注册」同文案
    # =====================================================================
    def test_register_cooldown_message_matches_already_registered(self):
        """冷却期分支文案必须与已注册分支逐字一致（不得泄露"近期注销过"信号）。"""
        db.create_user("live@qq.com", self.webapp.generate_password_hash(USER_PASS))
        db.create_user("gone@qq.com", self.webapp.generate_password_hash(USER_PASS))
        db.soft_delete_user_with_accounts("gone@qq.com")  # 刚注销 → 冷却期内
        c = self.webapp.create_app().test_client()
        r_live = c.post("/api/register",
                        json={"email": "live@qq.com", "password": USER_PASS, "agree": True})
        r_gone = c.post("/api/register",
                        json={"email": "gone@qq.com", "password": USER_PASS, "agree": True})
        self.assertEqual(r_live.status_code, 400)
        self.assertEqual(r_gone.status_code, 400)
        self.assertEqual(r_live.get_json()["error"], "该邮箱已注册")
        self.assertEqual(r_gone.get_json()["error"], "该邮箱已注册",
                         "冷却期文案与「该邮箱已注册」必须逐字一致")

    def test_register_after_cooldown_expiry_succeeds(self):
        """对照：仅文案收敛，行为不变——冷却期结束后邮箱正常释放可再注册。"""
        # 注意顺序：先建 client 再回填过期时间（create_app 启动清理会物理清除过期注销用户）
        c = self.webapp.create_app().test_client()
        db.create_user("old@qq.com", self.webapp.generate_password_hash(USER_PASS))
        db.soft_delete_user_with_accounts("old@qq.com")
        old = (datetime.now() - timedelta(days=8)).strftime("%Y-%m-%d %H:%M:%S")
        conn = db.get_conn()
        conn.execute("UPDATE users SET deleted_at=? WHERE email=?", (old, "old@qq.com"))
        conn.commit()
        r = c.post("/api/register",
                   json={"email": "old@qq.com", "password": USER_PASS, "agree": True})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertIsNotNone(db.find_user("old@qq.com"))

    # =====================================================================
    # 7. M10 cookie path：YIBAN_BASE_PATH 收窄会话 Cookie
    # =====================================================================
    def test_cookie_path_narrowed_when_base_path_env_set(self):
        """验收用例：YIBAN_BASE_PATH=/tool/yiban-auto-sign/demo → 登录 Set-Cookie
        含 Path=/tool/yiban-auto-sign/demo/。"""
        env2 = os.path.join(self.tmp, "env-basepath.env")
        with io.open(env2, "w", encoding="utf-8") as f:
            f.write(self._env_content + "YIBAN_BASE_PATH=/tool/yiban-auto-sign/demo\n")
        with mock.patch.object(self.webapp, "ENV_FILE", env2):
            c = self.webapp.create_app().test_client()
        r = c.post("/api/login", json={"username": "admin", "password": ADMIN_PASS})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        cookie = r.headers.get("Set-Cookie", "")
        self.assertIn("Path=/tool/yiban-auto-sign/demo/", cookie,
                      f"会话 Cookie 应收窄到挂载前缀，实际 Set-Cookie: {cookie}")

    def test_cookie_path_default_root_without_base_path(self):
        """对照：未设 YIBAN_BASE_PATH（自动探测形态）不强行收窄，行为不变。"""
        c = self.webapp.create_app().test_client()
        r = c.post("/api/login", json={"username": "admin", "password": ADMIN_PASS})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        cookie = r.headers.get("Set-Cookie", "")
        self.assertNotIn("Path=/tool", cookie, "未显式配置时不得收窄 Cookie 路径")
        self.assertIn("yiban_admin=", cookie)


if __name__ == "__main__":
    unittest.main(verbosity=2)
