# -*- coding: utf-8 -*-
"""口令门收窄（缩减批 6a）与管理员摩擦专项的管理员视角端到端钉。

功能：把"哪些操作免门免额度、哪些仍受门与额度"钉成一条管理员视角的端到端链，
    每个 A4 项一条；A4-1/A4-2 各带"不可逆操作仍受门与额度"的反例。
归属：web 高危门禁族（`web.app._high_risk_gate` 与各域调用点）的收窄验收。
复用：`_e2e_admin_client`（内置主管理员登录 + CSRF）、`_e2e_audit_rows`
    （按 action 读审计行）；本文件自带隔离夹具，不依赖其他测试文件的模块状态。
通信：Flask test client 走真 HTTP 语义（json + CSRF 头）；.env/SQLite 全部落
    本文件临时目录；零真实外联（mailer/notify 的告警由测试内打桩记录）。

覆盖：A4-1 门收窄面——可逆操作（改角色 / 开启邮件通道 / 推送数值参数）full 档
    主管理员无口令直达成功且各自落审计行；反例——删除类（accounts/batch purge、
    users/<id>/delete）、换钥类（notify 换钥）与**关闭邮件通道**（拆掉告警最后一条
    送达路径）无口令仍 400 password_required，带口令成功后仍消耗对应额度
    （MAX=1 时第二次 429）。
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
import io
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
        """主管理员一次会话内：改角色 / 开启邮件通道 / 调推送数值，全程零口令、
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
        # ② 邮件通道**开启**（关闭方向受门——它是告警最后一条送达路径，见下方专门用例；
        #    把告警装回去不是"拆报警器"，故开启免门免额度）
        r2 = c.put("/api/mail-config", json={"enabled": True}, headers=hdr)
        self.assertEqual(r2.status_code, 200, r2.get_data(as_text=True))
        self.assertTrue(self._audit_rows("mail_config"), "邮件通道开启必须落审计行")
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


class MailChannelCloseGateE2ETest(_GateNarrowBase):
    """关闭邮件通道 = 拆掉告警最后一条送达路径：必须过口令门，且占**凭据**额度。

    `mail_config._get` 环境变量优先于 `.env`，而本机 conftest 把 YIBAN_MAIL_ENABLE
    钉成 0；关闭方向的"值真变化"判据读的就是这个口径，故这里显式改环境变量并登记还原。
    """

    def _set_mail_enable(self, value):
        old = os.environ.get("YIBAN_MAIL_ENABLE")
        os.environ["YIBAN_MAIL_ENABLE"] = value

        def _restore():
            if old is None:
                os.environ.pop("YIBAN_MAIL_ENABLE", None)
            else:
                os.environ["YIBAN_MAIL_ENABLE"] = old

        self.addCleanup(_restore)

    def test_closing_channel_needs_password_then_bills_creds_quota(self):
        """通道当前是开的：无口令关闭 → 400 password_required；带口令 → 200 且落审计；
        关闭占凭据额度（MAX=1）→ 第二次关闭 429 且不落盘。"""
        self._env(("YIBAN_ADMIN_CREDS_MAX=1\n",))
        self._set_mail_enable("1")
        c, t = self._admin_client()
        hdr = {"X-CSRF-Token": t}
        r1 = c.put("/api/mail-config", json={"enabled": False}, headers=hdr)
        self.assertEqual(r1.status_code, 400, r1.get_data(as_text=True))
        self.assertEqual(r1.get_json().get("reason"), "password_required")
        r2 = c.put("/api/mail-config",
                   json={"enabled": False, "confirm_password": ADMIN_PASS}, headers=hdr)
        self.assertEqual(r2.status_code, 200, r2.get_data(as_text=True))
        self.assertEqual(len(self._audit_rows("mail_config")), 1,
                         "关闭邮件通道必须恰好落一行审计")
        # 凭据额度那一格已被关闭动作占用：再关一次 → 429，且被拒动作不落盘
        r3 = c.put("/api/mail-config",
                   json={"enabled": False, "confirm_password": ADMIN_PASS}, headers=hdr)
        self.assertEqual(r3.status_code, 429, r3.get_data(as_text=True))
        self.assertEqual(len(self._audit_rows("mail_config")), 1, "429 被拒的动作不得落审计")

    def test_opening_and_already_off_close_are_gate_free(self):
        """通道当前是关的：关闭方向"值真变化"不成立 → 不设门；开启方向从不设门。"""
        self._set_mail_enable("0")
        c, t = self._admin_client()
        hdr = {"X-CSRF-Token": t}
        r1 = c.put("/api/mail-config", json={"enabled": False}, headers=hdr)
        self.assertEqual(r1.status_code, 200, r1.get_data(as_text=True))
        r2 = c.put("/api/mail-config", json={"enabled": True}, headers=hdr)
        self.assertEqual(r2.status_code, 200, r2.get_data(as_text=True))
        self.assertTrue(self._audit_rows("mail_config"))


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


class EnvWriteRefusedOperationalE2ETest(_GateNarrowBase):
    """A4-4：脏 `.env` 上保存 → 409 附 problems（行号 + 脱敏片段，值已隐去）；
    一键清理端点只吃"确含行分隔符的行"，清理后保存恢复。"""

    SECRET_MARKER = "s3cret-mail-pass-value"

    def _inject_poison(self):
        """注入一行潜伏分隔符行：注释尾带 U+0085，其后是攻击载荷与口令形状文本。"""
        with io.open(self.env_file, "a", encoding="utf-8") as f:
            f.write("# 例行备注\u0085YIBAN_GLOBAL_PAUSE=1 MAIL_PASS="
                         + self.SECRET_MARKER + "\n")
        with io.open(self.env_file, encoding="utf-8") as f:
            return f.read()

    def _read_env(self):
        with io.open(self.env_file, encoding="utf-8") as f:
            return f.read()

    def test_409_problems_and_cleanup_restore_saving(self):
        c, t = self._admin_client()
        hdr = {"X-CSRF-Token": t}
        before = self._inject_poison()
        # 保存触发 fail-closed：409 + problems 定位载荷（full 档的设置保存带当次口令）
        r = c.post("/api/settings", json={"sign_order": "random",
                                          "confirm_password": ADMIN_PASS}, headers=hdr)
        self.assertEqual(r.status_code, 409, r.get_data(as_text=True))
        body = r.get_json()
        self.assertEqual(body.get("reason"), "env_write_refused")
        problems = body.get("problems") or []
        line_pbs = [p for p in problems if p.get("kind") == "line"]
        self.assertTrue(line_pbs, f"409 必须带行定位：{body}")
        snippet = line_pbs[0].get("snippet") or ""
        self.assertTrue(line_pbs[0]["line"] >= 1)
        self.assertNotIn(self.SECRET_MARKER, snippet, "片段绝不回显值原文")
        self.assertIn("***", snippet, "值必须以隐去形态出现")
        self.assertIn("YIBAN_GLOBAL_PAUSE", snippet, "键名保留（定位信息）")
        self.assertEqual(self._read_env(), before, "被拒的保存不得改动 .env")
        # 一键清理：行号来自 409 载荷；该行（含口令形状文本）被整行移除
        r2 = c.post("/api/settings/env-cleanup",
                    json={"line": line_pbs[0]["line"]}, headers=hdr)
        self.assertEqual(r2.status_code, 200, r2.get_data(as_text=True))
        after = self._read_env()
        self.assertNotIn(self.SECRET_MARKER, after, "含载荷的行必须整行移除")
        self.assertNotIn("YIBAN_GLOBAL_PAUSE=1", after, "载荷实体化前就被清掉")
        # 清理入口不是编辑器：干净行拒绝清理
        r3 = c.post("/api/settings/env-cleanup", json={"line": 1}, headers=hdr)
        self.assertEqual(r3.status_code, 400, r3.get_data(as_text=True))
        # 清理后保存恢复
        r4 = c.post("/api/settings", json={"sign_order": "random",
                                           "confirm_password": ADMIN_PASS}, headers=hdr)
        self.assertEqual(r4.status_code, 200, r4.get_data(as_text=True))

    def test_cleanup_is_master_admin_only(self):
        c, t = self._admin_client()
        hdr = {"X-CSRF-Token": t}
        self._inject_poison()
        db.create_user("ra@test.local", self.webapp.generate_password_hash("Radmin#1234"),
                       role="admin")
        rc = self.webapp.create_app().test_client()
        r = rc.post("/api/login", json={"username": "ra@test.local", "password": "Radmin#1234"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        rh = {"X-CSRF-Token": rc.get("/api/me").get_json()["csrf_token"]}
        r2 = rc.post("/api/settings/env-cleanup", json={"line": 2}, headers=rh)
        self.assertEqual(r2.status_code, 403, r2.get_data(as_text=True))
        # 主管理员同样被"行干净"判据拦下（line 1 是配置行）
        r3 = c.post("/api/settings/env-cleanup", json={"line": 1}, headers=hdr)
        self.assertEqual(r3.status_code, 400, r3.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
