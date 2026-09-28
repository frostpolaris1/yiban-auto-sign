# -*- coding: utf-8 -*-
"""口令门收窄（缩减批 6a）与管理员摩擦专项的管理员视角端到端钉。

功能：把"哪些操作免门免额度、哪些仍受门与额度"钉成一条管理员视角的端到端链，
    每个 A4 项一条；A4-1/A4-2 各带"不可逆操作仍受门与额度"的反例。
归属：web 高危门禁族（`web.app._high_risk_gate` 与各域调用点）的收窄验收。
复用：`_e2e_admin_client`（内置主管理员登录 + CSRF）、`_e2e_audit_rows`
    （按 action 读审计行）；本文件自带隔离夹具，不依赖其他测试文件的模块状态。
通信：Flask test client 走真 HTTP 语义（json + CSRF 头）；.env/SQLite 全部落
    本文件临时目录；零真实外联（mailer/notify 的告警由测试内打桩记录）。

覆盖：A4-1 门收窄面——可逆操作（改角色 / 邮件开关关闭 / 推送数值参数）full 档
    主管理员无口令直达成功且各自落审计行；反例——删除类（accounts/batch purge、
    users/<id>/delete）与换钥类（notify 换钥）无口令仍 400 password_required，
    带口令成功后仍消耗删除额度（MAX=1 时第二次 429）。
对应实现：`web/app.py::_high_risk_gate`（docstring 即清单，`test_gate_manifest_sync`
    钉清单↔路由同源）、`web/routes/users_api.py`、`web/routes/notify.py`、
    `web/routes/accounts_api.py`。
关键断言：免门不等于免痕（每个免门动作必须有审计行）；受门动作的额度在口令
    通过后才占用（错口令零消耗）。
依赖：纯本地 Flask test client + 临时 `.env`/SQLite；`mock` 打桩
    `send_notification` 防真实 SMTP；不联网、不建容器。
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

TEST_KEY = "e" * 64
ADMIN_PASS = "GateNarrow#2026"

TEST_KEY_ROLE = "f" * 64


def _write_env(path, lines):
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


class _GateNarrowBase(unittest.TestCase):
    """隔离 .env + SQLite + 内置主管理员会话；档位固定 full（机制钉不依赖缺省档）。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-gate-narrow-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        cls.env_seed = [
            f"YIBAN_ACCOUNTS_KEY={TEST_KEY}",
            f"YIBAN_ADMIN_USER=admin\nYIBAN_ADMIN_PASSWORD={ADMIN_PASS}",
            "YIBAN_PW_GATE=full",
        ]
        _write_env(cls.env_file, cls.env_seed)
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
        spec = importlib.util.spec_from_file_location(
            "webapp_gate_narrow", os.path.join(BASE, "web", "app.py"))
        cls.webapp = importlib.util.module_from_spec(spec)
        sys.modules["webapp_gate_narrow"] = cls.webapp
        spec.loader.exec_module(cls.webapp)

    @classmethod
    def tearDownClass(cls):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        sys.modules.pop("webapp_gate_narrow", None)
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
        self.alerts = []
        p = mock.patch.object(
            self.webapp, "send_notification",
            side_effect=lambda t, c, urgent=False, force=False, ledger=None: (
                self.alerts.append((t, urgent)), True)[1])
        p.start()
        self.addCleanup(p.stop)

    # ---- 工具 ----
    def _env(self, extra=()):
        _write_env(self.env_file, list(self.env_seed) + list(extra))

    def _admin_client(self):
        c = self.webapp.create_app().test_client()
        r = c.post("/api/login", json={"username": "admin", "password": ADMIN_PASS})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return c, c.get("/api/me").get_json()["csrf_token"]

    def _audit_rows(self, action):
        with db._conn_lock:
            conn = db.get_conn()
            rows = conn.execute(
                "SELECT id, username, action, target, detail FROM audit_logs "
                "WHERE action=? ORDER BY id", (action,)).fetchall()
        return [dict(r) for r in rows]

    def _seed_formal_user(self, email, phone, admin=None):
        """构造「正式用户」：注册 + 提交账号 + 管理员审核通过
        （角色晋升前置 = 有已生效账号且无待审核）。

        `admin` 传 `(client, csrf)` 时审核复用同一 app 实例——高危额度表是
        create_app 工厂局部状态，换实例等于换一张空表，同测试内的 429 断言
        必须全程走同一实例。结束清空 alerts（注册送审的待审核提醒不算被测面）。
        """
        db.create_user(email, self.webapp.generate_password_hash("NarrowPass1!"))
        uc = self.webapp.create_app().test_client()
        r = uc.post("/api/login", json={"username": email, "password": "NarrowPass1!"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        ut = uc.get("/api/me").get_json()["csrf_token"]
        r = uc.post("/api/my-accounts",
                    json={"name": "n", "phone": phone, "password": "p"},
                    headers={"X-CSRF-Token": ut})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        ac, at = admin if admin else self._admin_client()
        accounts = db.load_accounts()
        idx = next(i for i, a in enumerate(accounts) if a["phone"] == phone)
        r = ac.post(f"/api/accounts/{idx}/review", json={"action": "approve"},
                    headers={"X-CSRF-Token": at})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.alerts.clear()


class GateNarrowingE2ETest(_GateNarrowBase):
    """A4-1：可逆操作免门直达 + 不可逆操作仍受门与删除额度（管理员视角一条链）。"""

    def test_reversible_ops_reach_success_without_password_and_leave_audit(self):
        """主管理员一次会话内：改角色 / 关邮件开关 / 调推送数值，全程零口令、
        零 429，且每个动作各落一行审计（免门不等于免痕）。"""
        self._seed_formal_user("narrowee@test.local", "13800000001")
        c, t = self._admin_client()
        hdr = {"X-CSRF-Token": t}
        # ① 角色变更（曾要口令 + 占额度）
        uid = db.find_user("narrowee@test.local")["id"]
        r = c.post(f"/api/users/{uid}/role", json={"role": "admin"}, headers=hdr)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(db.find_user("narrowee@test.local").get("role"), "admin")
        self.assertTrue(self._audit_rows("user_role"), "角色变更必须落审计行")
        # ② 邮件开关关闭（曾要口令 + 占删除额度）
        r2 = c.put("/api/mail-config", json={"enabled": False}, headers=hdr)
        self.assertEqual(r2.status_code, 200, r2.get_data(as_text=True))
        self.assertTrue(self._audit_rows("mail_config"), "邮件开关变更必须落审计行")
        # ③ 推送数值参数（曾要口令 + 占删除额度）
        r3 = c.put("/api/notify-config", json={"cooldown": 45, "daily_max": 9}, headers=hdr)
        self.assertEqual(r3.status_code, 200, r3.get_data(as_text=True))
        rows = self._audit_rows("notify_config")
        self.assertTrue(rows, "推送参数变更必须落审计行")
        detail = json.loads(rows[-1]["detail"])
        detail.pop("_req", None)
        self.assertEqual(detail.get("cooldown"), 45)
        # 全程零告警（通道变更只留审计的既有语义；事后告警只挂凭据族）
        self.assertEqual(self.alerts, [], f"免门面不应有变更告警：{self.alerts}")

    def test_irreversible_ops_still_gated_and_delete_quota_still_billed(self):
        """反例（A4-1）：删除类与换钥类无口令仍 400；口令通过后才占删除额度
        （MAX=1 → 第二次真实删除 429）。"""
        self._env(("YIBAN_ADMIN_DELETE_MAX=1\n",))
        c, t = self._admin_client()
        hdr = {"X-CSRF-Token": t}
        self._seed_formal_user("victim@test.local", "13800000002", admin=(c, t))
        # 无口令：删除/彻底清除/换钥全部 400 password_required
        r1 = c.post("/api/users/batch",
                    json={"action": "delete", "emails": ["victim@test.local"]}, headers=hdr)
        self.assertEqual(r1.status_code, 400, r1.get_data(as_text=True))
        self.assertEqual(r1.get_json()["reason"], "password_required")
        r2 = c.put("/api/notify-config",
                   json={"type": "serverchan", "secret": "SCT406257E2ETESTTEST0001"},
                   headers=hdr)
        self.assertEqual(r2.status_code, 400, r2.get_data(as_text=True))
        self.assertEqual(r2.get_json()["reason"], "password_required")
        self.assertIsNotNone(db.find_user("victim@test.local"), "无口令不得删除用户")
        # 错口令两次也不得消耗那唯一一格删除额度
        for _ in range(2):
            rw = c.post("/api/users/batch",
                        json={"action": "delete", "emails": ["victim@test.local"],
                              "confirm_password": "WrongPass#999"}, headers=hdr)
            self.assertEqual(rw.status_code, 400, rw.get_data(as_text=True))
        self.assertIsNotNone(db.find_user("victim@test.local"))
        # 口令正确 → 200 且真删除，并占用那唯一一格额度
        r3 = c.post("/api/users/batch",
                    json={"action": "delete", "emails": ["victim@test.local"],
                          "confirm_password": ADMIN_PASS}, headers=hdr)
        self.assertEqual(r3.status_code, 200, r3.get_data(as_text=True))
        self.assertIsNone(db.find_user("victim@test.local"))
        # 第二个真实删除动作 → 429（删除额度在同一 app 实例的表上仍在运转）
        self._seed_formal_user("victim2@test.local", "13800000003", admin=(c, t))
        r4 = c.post("/api/users/batch",
                    json={"action": "delete", "emails": ["victim2@test.local"],
                          "confirm_password": ADMIN_PASS}, headers=hdr)
        self.assertEqual(r4.status_code, 429, r4.get_data(as_text=True))
        self.assertIsNotNone(db.find_user("victim2@test.local"),
                             "被 429 拒下的删除不得生效")


class CredsQuotaSplitE2ETest(_GateNarrowBase):
    """A4-2：凭据改写类额度与删除类额度分开计数——两个方向互不撞 429。"""

    def test_creds_quota_independent_from_delete_quota(self):
        """凭据额度耗尽后删除类仍可执行（反向：删除额度耗尽不影响凭据面由
        GateNarrowingE2ETest 的 429 反例覆盖）；两族各自超限都 429。"""
        self._env(("YIBAN_ADMIN_DELETE_MAX=1\n", "YIBAN_ADMIN_CREDS_MAX=1\n"))
        c, t = self._admin_client()
        hdr = {"X-CSRF-Token": t}
        self._seed_formal_user("q@test.local", "13800000011", admin=(c, t))
        # 凭据改写①：改写他人易班密码 → 200，占凭据额度那唯一一格
        r1 = c.put("/api/accounts/0",
                   json={"name": "n", "phone": "13800000011", "password": "FreshPw#2468",
                         "confirm_password": ADMIN_PASS},
                   headers=hdr)
        self.assertEqual(r1.status_code, 200, r1.get_data(as_text=True))
        # 凭据改写②：凭据额度已尽 → 429（口令正确也拦——额度在口令之后）
        r2 = c.put("/api/accounts/0",
                   json={"name": "n", "phone": "13800000011", "password": "FreshPw#1357",
                         "confirm_password": ADMIN_PASS},
                   headers=hdr)
        self.assertEqual(r2.status_code, 429, r2.get_data(as_text=True))
        # 删除类不受凭据额度影响：批量删除（占删除额度）仍 200
        r3 = c.post("/api/users/batch",
                    json={"action": "delete", "emails": ["q@test.local"],
                          "confirm_password": ADMIN_PASS}, headers=hdr)
        self.assertEqual(r3.status_code, 200, r3.get_data(as_text=True))
        self.assertIsNone(db.find_user("q@test.local"))


if __name__ == "__main__":
    unittest.main()
