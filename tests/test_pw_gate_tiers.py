# -*- coding: utf-8 -*-
"""口令复核门的三档（`YIBAN_PW_GATE`）与软性摩擦：档位矩阵 + 风控 + 倒计时确认。

标签：E · Web：认证/权限/API
覆盖：`YIBAN_PW_GATE` 三档（`full` / `risk` / `off`）的档位矩阵——十类受门禁操作逐格验证「要不要当次口令、要不要倒计时确认」，加上风控判据、档位解析与事后告警
对应实现：`web/app.py` 的 `_pw_gate_tier`、统一口令门、`confirm_delay_ack` 校验与事后告警出口（凭据改写 / 执行体写 / 删用户）
关键断言：`full` 档逐格无口令必拒、且带了口令就**不再**要求倒计时字段（旧前端不认识它）；`risk` 档可逆操作首击免口令、不可逆操作缺 `confirm_delay_ack` 即拒且操作未发生；`off` 档连换出口 IP 也不要口令，但绝不凭空发出门禁失败告警；风控唯一判据是「换环境」——密度不是判据，两级 IP 都无记录的历史会话不触发；档位缺省与非法值都落 `risk`（非法值另告警一次）
依赖：纯本地 Flask test client + 临时 `.env`/SQLite，不联网、不访问真实易班接口；无需 node。每格都新建 app + 新登录：高危额度与风控计数是 `create_app` 的工厂局部状态，换 app 才是干净的一份。无需 node

背景：危险操作此前**一律**要求当次输入管理员口令，摩擦成本超过威胁收益。现按
`.env` 的 `YIBAN_PW_GATE` 分三档：

- `full`：每个受保护操作都要当次口令（改造前的行为，本文件逐格回归它不变）；
- `risk`（**默认**，缺省与非法值都落这里）：只有风控命中才要口令；
- `off`：永不要求口令。

`risk`/`off` 档用两件软摩擦替代事中口令：不可逆操作要请求体带 `confirm_delay_ack`
（前端倒计时后置 true），以及最高危两类操作**成功之后**补一封管理员告警。

风控唯一判据（`risk` 档）：**换环境**——本次出口 IP 与本会话"已验证 IP"（最近一次
口令验证通过的出口）不一致，会话还没验证过则退回登录出口。**两级都无记录的历史会话
视为未知，不触发**——否则升级后存量会话人人被判异常。

全程 mock / 纯本地（Flask test client），无任何网络请求。
用法（项目根目录）：
    python -m pytest tests/test_pw_gate_tiers.py -v
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

from _mail_body import render_body

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TEST_KEY = "a" * 64
ADMIN_PASS = "TestPass1234!"
USER_PASS = "UserPass123!"
# 掩码形态即文档口径 138****0000（脱敏后的手机号是本文件里唯一会出现的号码形态）
PHONE = "13800000000"
MASKED_PHONE = "138****0000"

# 受门禁保护的操作族：键 = 本文件的 `_op_<键>`，值是"是否不可逆"与"被拒时的状态码"。
# 覆盖简报点名的八类：A 档设置、B 档设置、改他人凭据、purge、删用户、急停、执行体写、发公告。
OPS = {
    "a_setting": {"irreversible": False, "deny": 403},
    "b_setting": {"irreversible": False, "deny": 403},
    "creds": {"irreversible": False, "deny": 400},
    "purge": {"irreversible": True, "deny": 400},
    "user_delete": {"irreversible": True, "deny": 400},
    "estop": {"irreversible": True, "deny": 403},
    "exec_write": {"irreversible": False, "deny": 403},
    "announce": {"irreversible": False, "deny": 403},
}


def _load_webapp():
    """**独立名字**加载 web/app.py：它在导入期把 ENV_FILE 等读成模块级常量，
    与别的测试文件共用同一模块对象会读到另一个 `.env`。"""
    spec = importlib.util.spec_from_file_location(
        "webapp_pwgate", os.path.join(BASE, "web", "app.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["webapp_pwgate"] = mod
    with contextlib.suppress(Exception):
        spec.loader.exec_module(mod)
    return mod


class _TierBase(unittest.TestCase):
    """临时 .env/DB + 主管理员会话；每格操作都从"新 app + 新登录"起。

    高危额度是 create_app 的工厂局部状态，换一个 app 就是干净的一份——所以矩阵里每格
    都重开 app，格与格之间不会互相把对方顶进 429；换环境判据只读会话，随新登录重置。
    """

    TIER = None  # None = 不写键（钉缺省档）

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-pw-gate-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.accounts_file = os.path.join(cls.tmp, "accounts.json")
        cls.users_file = os.path.join(cls.tmp, "users.json")
        os.environ.update({
            "YIBAN_ACCOUNTS_KEY": TEST_KEY,
            "YIBAN_ENV_FILE": cls.env_file,
            "YIBAN_ACCOUNTS_FILE": cls.accounts_file,
            "YIBAN_USERS_FILE": cls.users_file,
            "YIBAN_DB_FILE": cls.db_file,
            "YIBAN_STATE_DIR": cls.tmp,
            "YIBAN_LOG_FILE": os.path.join(cls.tmp, "sign.log"),
            "YIBAN_DISABLE_PURGE_LOOP": "1",
        })
        cls.webapp = _load_webapp()

    @classmethod
    def tearDownClass(cls):
        import db
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
        self._write_env()
        self._reset_db()
        self.alerts = []
        p = mock.patch.object(
            self.webapp, "send_notification",
            side_effect=lambda t, c, urgent=False, force=False, ledger=None:
                self.alerts.append((t, render_body(c), urgent)),
        )
        p.start()
        self.addCleanup(p.stop)

    # ---- 夹具 ----
    def _write_env(self):
        lines = [
            f"YIBAN_ACCOUNTS_KEY={TEST_KEY}",
            "YIBAN_ADMIN_USER=admin",
            f"YIBAN_ADMIN_PASSWORD={ADMIN_PASS}",
            "YIBAN_MAIL_ADMIN_TO=admin@test.local",
            # 发公告那一路要有草稿，否则它先报"没有待发布草稿"
            "YIBAN_ANNOUNCEMENT_DRAFT=测试公告草稿",
        ]
        # 不设档位时干脆不写这一行，让 `_pw_gate_tier` 走缺省解析路径
        if self.TIER:
            lines.append(f"YIBAN_PW_GATE={self.TIER}")
        with open(self.env_file, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        # 口令门比对的是哈希；重写 .env 会抹掉首启迁移的产物，补跑一次让夹具
        # 回到真实部署的样子（不是为了让测试变绿而放宽断言）
        self.webapp.migrate_admin_password_to_hash(self.env_file)

    def _set_tier(self, value):
        """把档位写进 `.env`（空值 = 删键，用于钉缺省档）。"""
        self.webapp.write_env_batch(self.env_file, {"YIBAN_PW_GATE": value or ""})

    def _reset_db(self):
        import db
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

    def _fresh(self):
        """新 app + 新登录（风控计数与高危额度随之归零），返回 (client, csrf 头)。"""
        self._reset_db()
        c = self.webapp.create_app().test_client()
        r = c.post("/api/login", json={"username": "admin", "password": ADMIN_PASS})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        token = c.get("/api/me").get_json()["csrf_token"]
        return c, {"X-CSRF-Token": token}

    # ---- 操作族（每格自己备好最小夹具，可反复调用）----
    def _ensure_user(self, email):
        import db
        if db.find_user(email) is None:
            db.create_user(email, self.webapp.generate_password_hash(USER_PASS))
        return email

    def _ensure_account(self, deleted=False):
        import db
        rows = db.load_accounts()
        if not rows:
            acc_id = db.add_account({"name": "A", "phone": PHONE, "password": "pw-1",
                                     "status": "active", "owner": "u1@test.local"})
            rows = db.load_accounts()
            acc_id = rows[0]["id"]
        else:
            acc_id = rows[0]["id"]
        db.set_account_deleted(acc_id, 1 if deleted else 0)
        return db.load_accounts()[0]

    def _op_a_setting(self, c, hdr, **extra):
        return c.post("/api/settings", json={"sunday_sign": 1, **extra}, headers=hdr)

    def _op_b_setting(self, c, hdr, **extra):
        return c.post("/api/settings", json={"sign_order": "random", **extra}, headers=hdr)

    def _op_estop(self, c, hdr, **extra):
        return c.post("/api/settings", json={"global_pause": 1, **extra}, headers=hdr)

    def _op_announce(self, c, hdr, **extra):
        return c.post("/api/announcement/publish", json={**extra}, headers=hdr)

    def _op_exec_write(self, c, hdr, **extra):
        return c.post("/api/scheduler/executors/rows",
                      json={"proxy": "http://203.0.113.9:8080", **extra}, headers=hdr)

    def _op_creds(self, c, hdr, **extra):
        acc = self._ensure_account()
        body = {"name": "A", "phone": acc["phone"], "password": "new-pw-1", **extra}
        return c.put("/api/accounts/0", json=body, headers=hdr)

    def _op_purge(self, c, hdr, **extra):
        acc = self._ensure_account(deleted=True)
        return c.post("/api/accounts/0/purge",
                      json={"phone": acc["phone"], **extra}, headers=hdr)

    def _op_user_delete(self, c, hdr, **extra):
        self._ensure_user("u1@test.local")
        return c.post("/api/users/u1@test.local/delete",
                      json={"mode": "full", **extra}, headers=hdr)

    def _call(self, op, c, hdr, **extra):
        """按操作名分发；`_xff=` 走 X-Forwarded-For（换出口 IP 用，其余进请求体）。"""
        xff = extra.pop("_xff", None)
        headers = dict(hdr)
        if xff:
            headers["X-Forwarded-For"] = xff
        return getattr(self, f"_op_{op}")(c, hdr=headers, **extra)


class FullTierTest(_TierBase):
    """`full` 档 = 改造前的行为，逐格不变：无口令必拒、带口令放行。"""

    TIER = "full"

    def test_每个操作无口令都被拒(self):
        for op, meta in OPS.items():
            with self.subTest(op=op):
                c, hdr = self._fresh()
                r = self._call(op, c, hdr)
                self.assertEqual(r.status_code, meta["deny"], r.get_data(as_text=True))
                self.assertEqual(r.get_json()["reason"], "password_required")

    def test_每个操作带口令都放行(self):
        for op in OPS:
            with self.subTest(op=op):
                c, hdr = self._fresh()
                r = self._call(op, c, hdr, confirm_password=ADMIN_PASS)
                self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

    def test_full_档不要求倒计时确认凭据(self):
        """带了口令就不该再被倒计时框拦（向后兼容：旧前端不知道这个字段）。"""
        c, hdr = self._fresh()
        r = self._call("purge", c, hdr, confirm_password=ADMIN_PASS)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(self.webapp.load_accounts(), [], "口令对了就该真的清除")


class RiskTierTest(_TierBase):
    """`risk`（默认）档：首击不要口令；不可逆操作改要倒计时确认。"""

    TIER = "risk"

    def test_可逆操作首击不要口令且真的生效(self):
        for op in ("a_setting", "b_setting", "exec_write", "announce"):
            with self.subTest(op=op):
                c, hdr = self._fresh()
                r = self._call(op, c, hdr)
                self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

    def test_不可逆操作缺倒计时确认被拒且操作未发生(self):
        for op in ("purge", "user_delete", "estop"):
            with self.subTest(op=op):
                c, hdr = self._fresh()
                r = self._call(op, c, hdr)
                self.assertEqual(r.status_code, OPS[op]["deny"],
                                 r.get_data(as_text=True))
                self.assertEqual(r.get_json()["reason"], "delay_ack_required")
        # 被拒的那次不得留下任何效果
        c, hdr = self._fresh()
        self._call("purge", c, hdr)
        self.assertEqual(len(self.webapp.load_accounts()), 1, "被拒不得物理清除")

    def test_不可逆操作带倒计时确认即放行(self):
        for op in ("purge", "user_delete", "estop"):
            with self.subTest(op=op):
                c, hdr = self._fresh()
                r = self._call(op, c, hdr, confirm_delay_ack=True)
                self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

    def test_改他人凭据首击不要口令(self):
        c, hdr = self._fresh()
        r = self._call("creds", c, hdr)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(self.webapp.load_accounts()[0]["password"], "new-pw-1")


class OffTierTest(_TierBase):
    """`off` 档：永不要求口令，只留倒计时确认与事后告警。"""

    TIER = "off"

    def test_可逆操作不要口令(self):
        for op in ("a_setting", "b_setting", "creds", "exec_write", "announce"):
            with self.subTest(op=op):
                c, hdr = self._fresh()
                r = self._call(op, c, hdr)
                self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

    def test_不可逆操作仍要倒计时确认(self):
        for op in ("purge", "user_delete", "estop"):
            with self.subTest(op=op):
                c, hdr = self._fresh()
                r = self._call(op, c, hdr)
                self.assertEqual(r.status_code, OPS[op]["deny"],
                                 r.get_data(as_text=True))
                self.assertEqual(r.get_json()["reason"], "delay_ack_required")
                r2 = self._call(op, c, hdr, confirm_delay_ack=True)
                self.assertEqual(r2.status_code, 200, r2.get_data(as_text=True))

    def test_换环境也不要口令(self):
        """off 档连风控也不升级：连续换出口 IP，仍一路放行。"""
        c, hdr = self._fresh()
        for i in range(3):
            r = self._call("creds", c, hdr, _xff=f"203.0.113.{i + 1}")
            self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

    def test_永不发门禁失败告警(self):
        """off 档从不要求口令 ⇒ 门禁失败计数告警也不该被凭空发出（信号不得伪造）。"""
        c, hdr = self._fresh()
        for i in range(3):
            r = self._call("creds", c, hdr, confirm_password="WrongPass999!",
                           _xff=f"203.0.113.{i + 1}")
            self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertNotIn("高危操作二次鉴权失败告警", [t for t, _b, _u in self.alerts])


class RiskTriggerTest(_TierBase):
    """`risk` 档的唯一判据"换环境"，以及"无已验证/登录 IP 记录不触发"。"""

    TIER = "risk"

    def test_换IP后首次危险操作要口令(self):
        c, hdr = self._fresh()
        r = self._call("creds", c, hdr, _xff="203.0.113.7")
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        self.assertEqual(r.get_json()["reason"], "password_required")
        # 命中后的路径与 full 档同路：带对口令即放行
        r2 = self._call("creds", c, hdr, confirm_password=ADMIN_PASS,
                        _xff="203.0.113.7")
        self.assertEqual(r2.status_code, 200, r2.get_data(as_text=True))

    def test_同IP不要求口令(self):
        """同一出口的危险操作不因"次数多"而被要求口令：密度不是判据。"""
        c, hdr = self._fresh()
        for i in range(3):
            r = self._call("creds", c, hdr)
            self.assertEqual(r.status_code, 200,
                             f"第 {i + 1} 次同出口操作不该要口令：{r.get_data(as_text=True)}")

    def test_换IP后口令正确则该IP被记住且后续不再要口令(self):
        c, hdr = self._fresh()
        r = self._call("creds", c, hdr, _xff="203.0.113.7")
        self.assertEqual(r.get_json()["reason"], "password_required")
        r2 = self._call("creds", c, hdr, confirm_password=ADMIN_PASS, _xff="203.0.113.7")
        self.assertEqual(r2.status_code, 200, r2.get_data(as_text=True))
        # 同一出口后续危险操作不再重复要求（验证通过即记住该 IP）
        r3 = self._call("creds", c, hdr, _xff="203.0.113.7")
        self.assertEqual(r3.status_code, 200, r3.get_data(as_text=True))
        # 记住的是**最近一次验证过的出口**，不是把登录出口永久放行：换回登录出口
        # 仍要一次口令（否则"记住"就退化成"见过即信任"的白名单）
        r4 = self._call("creds", c, hdr)
        self.assertEqual(r4.status_code, 400, r4.get_data(as_text=True))
        self.assertEqual(r4.get_json()["reason"], "password_required")

    def test_无登录IP记录不触发(self):
        """历史会话没有 login_ip 键、也没验证过口令 = 未知，不得因此判成换环境。"""
        c, hdr = self._fresh()
        with c.session_transaction() as sess:
            sess.pop("login_ip", None)
        r = self._call("creds", c, hdr, _xff="203.0.113.7")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

    def test_恢复即登录的会话换IP后要口令(self):
        """恢复即登录建立的会话必须与登录路径记同一份登录出口。

        两级基准（已验证 IP / 登录 IP）都空 = 未知 ⇒ 不触发，这是给**存量**会话的
        宽容；但"注销后恢复"建立的是完整会话，若这里不记登录出口，那条判据对它永久
        失效——同一条风控在恢复入口上被静默豁免。
        """
        import db
        email = "restored-admin@test.local"
        self._reset_db()
        # 两个注册管理员：db 的"最后一个注册管理员不可注销"保护会拦住唯一那一个
        db.create_user(email, self.webapp.generate_password_hash(USER_PASS), role="admin")
        db.create_user("keeper@test.local", self.webapp.generate_password_hash(USER_PASS),
                       role="admin")
        db.soft_delete_user_with_accounts(email)
        c = self.webapp.create_app().test_client()
        r = c.post("/api/me/restore", json={"email": email, "password": USER_PASS},
                   headers={"X-Forwarded-For": "203.0.113.7"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        token = c.get("/api/me").get_json()["csrf_token"]
        # 两次都必须是**真变更**：无变更的保存根本不进门禁，那样断言会假绿
        cur = self.webapp._settings_effective_values(self.env_file).get("sign_order") or "sequential"
        other = "sequential" if cur == "random" else "random"

        def save(order, xff):
            return c.post("/api/settings", json={"sign_order": order},
                          headers={"X-CSRF-Token": token, "X-Forwarded-For": xff})

        # 与恢复时同一个出口：判据未命中，不要求口令
        r1 = save(other, "203.0.113.7")
        self.assertEqual(r1.status_code, 200, r1.get_data(as_text=True))
        # 换出口：命中"换环境"，首次危险操作必须补口令
        r2 = save(cur, "203.0.113.9")
        self.assertEqual(r2.status_code, 403, r2.get_data(as_text=True))
        self.assertEqual(r2.get_json()["reason"], "password_required")

    def test_换环境命中后的口令失败仍发门禁失败告警(self):
        """换环境才要求口令，但失败计数与告警这条信号不得因此静音。"""
        c, hdr = self._fresh()
        self.alerts.clear()
        # 阈值取门禁侧常量：登录失败告警阈值是另一件事（登录侧调它是为了少发误报），
        # 门禁侧这个数同时是"每窗口可做的 scrypt 尝试次数"上界，两者不得互相牵连。
        th = self.webapp.SENSITIVE_PW_FAIL_NOTIFY
        for _ in range(th):
            r = self._call("creds", c, hdr, confirm_password="WrongPass999!",
                           _xff="203.0.113.7")
            self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
            self.assertEqual(r.get_json()["reason"], "password_incorrect")
        self.assertIn("高危操作二次鉴权失败告警", [t for t, _b, _u in self.alerts])


class TierResolutionTest(_TierBase):
    """档位解析：缺省与非法值都落 `risk`（安全件不得被笔误静默降档到 off）。"""

    TIER = None

    def test_缺省档位是risk(self):
        self.assertEqual(self.webapp._pw_gate_tier(self.env_file), "risk")

    def test_非法值回退risk并告警(self):
        self._set_tier("bogus")
        with self.assertLogs("web", level="WARNING") as cm:
            tier = self.webapp._pw_gate_tier(self.env_file)
        self.assertEqual(tier, "risk")
        self.assertTrue(any("YIBAN_PW_GATE" in line for line in cm.output),
                        f"非法档位必须留一条可 grep 的告警，实际 {cm.output}")

    def test_三个合法值原样解析(self):
        for tier in ("off", "risk", "full"):
            with self.subTest(tier=tier):
                self._set_tier(tier)
                self.assertEqual(self.webapp._pw_gate_tier(self.env_file), tier)

    def test_大小写与空白归一(self):
        self._set_tier("  FULL  ")
        self.assertEqual(self.webapp._pw_gate_tier(self.env_file), "full")


class DelayAckTest(_TierBase):
    """倒计时确认凭据：只认布尔真值，只在不可逆操作上要求，full 档不要求。"""

    TIER = "risk"

    def test_非布尔真值一律算缺(self):
        c, hdr = self._fresh()
        for bad in ("true", "1", 1, 0, None, False):
            with self.subTest(value=bad):
                r = self._call("purge", c, hdr, confirm_delay_ack=bad)
                self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
                self.assertEqual(r.get_json()["reason"], "delay_ack_required")

    def test_full_档不要求该字段(self):
        self._set_tier("full")
        c, hdr = self._fresh()
        r = self._call("purge", c, hdr, confirm_password=ADMIN_PASS)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

    def test_可逆操作不要该字段(self):
        c, hdr = self._fresh()
        r = self._call("creds", c, hdr)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))


class PostHocAlertTest(_TierBase):
    """非 full 档的软摩擦兜底：改他人凭据成功后补一封管理员告警（脱敏）。"""

    TIER = "risk"

    def test_risk_档改写凭据成功后有告警(self):
        c, hdr = self._fresh()
        r = self._call("creds", c, hdr)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        titles = [t for t, _b, _u in self.alerts]
        self.assertIn("高危管理操作告警", titles, f"实际告警 {titles}")
        body = "\n".join(b for _t, b, _u in self.alerts)
        self.assertIn("改写他人易班凭据", body)
        self.assertIn(MASKED_PHONE, body, "目标手机号必须脱敏")
        self.assertNotIn(PHONE, body, "告警不得含完整手机号")
        self.assertIn("admin", body, "告警要能看出是谁做的")
        self.assertTrue(all(u for _t, _b, u in self.alerts), "必须走紧急通道")

    def test_off_档改写凭据成功后有告警(self):
        self._set_tier("off")
        c, hdr = self._fresh()
        r = self._call("creds", c, hdr)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertIn("高危管理操作告警", [t for t, _b, _u in self.alerts])

    def test_full_档不新增该告警(self):
        self._set_tier("full")
        c, hdr = self._fresh()
        r = self._call("creds", c, hdr, confirm_password=ADMIN_PASS)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(self.alerts, [], "full 档本就有当次口令，不重复发事后告警")

    def test_purge_在非full档不再发高危告警(self):
        """物理清除不再外发即时告警：非 full 档的补偿信号只剩审计行。"""
        c, hdr = self._fresh()
        r = self._call("purge", c, hdr, confirm_delay_ack=True)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(self.alerts, [], f"物理清除告警已下线，实际 {self.alerts}")
        import db
        rows = [dict(x) for x in db.get_conn().execute(
            "SELECT action FROM audit_logs WHERE action='account_purge'").fetchall()]
        self.assertTrue(rows, "清除动作必须留在审计链上")


class ExecutorChangeAlertTest(_TierBase):
    """执行体写的事后告警：非 full 档下改出口/增删行必须留一条管理员侧信号。

    这一族是本批十类受门禁操作里唯一既无当次口令、也无补偿信号的（改出口能把全站
    签到流量交给任意代理，还能顺手关掉兜底），故按"真的改了配置才发"补一封脱敏告警。
    """

    TIER = "risk"

    def _put(self, c, hdr, path, body):
        return c.put(path, json=body, headers=hdr)

    def test_每个写端点改配置后都有告警(self):
        """五个写端点（整条保存 / 单段出口 / 追加 / 改行 / 删行）逐条都得有信号。"""
        for name, call in (
            ("整条保存", lambda c, h: self._put(c, h, "/api/scheduler/executors",
                                                {"workers": 3})),
            ("并行段出口", lambda c, h: self._put(c, h,
                                                  "/api/scheduler/executors/workers/0",
                                                  {"egress": "http://203.0.113.9:8080"})),
            ("兜底段出口", lambda c, h: self._put(c, h, "/api/scheduler/executors/fallback",
                                                  {"egress": "http://203.0.113.9:8080"})),
            ("追加行", lambda c, h: c.post("/api/scheduler/executors/rows",
                                           json={"proxy": "http://203.0.113.9:8080"},
                                           headers=h)),
        ):
            with self.subTest(endpoint=name):
                c, hdr = self._fresh()
                self.alerts.clear()
                r = call(c, hdr)
                self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
                self.assertIn("系统设置变更告警", [t for t, _b, _u in self.alerts],
                              f"{name} 改配置后必须发一条事后告警")

    def test_改行与删行也有告警(self):
        c, hdr = self._fresh()
        slot = c.post("/api/scheduler/executors/rows",
                      json={"proxy": "http://203.0.113.9:8080"}, headers=hdr).get_json()["slot"]
        self.alerts.clear()
        r = self._put(c, hdr, f"/api/scheduler/executors/rows/{slot}",
                      {"proxy": "http://203.0.113.10:8080"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertIn("系统设置变更告警", [t for t, _b, _u in self.alerts], "改行改出口必须留痕")
        self.alerts.clear()
        r = c.delete(f"/api/scheduler/executors/rows/{slot}", json={}, headers=hdr)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertIn("系统设置变更告警", [t for t, _b, _u in self.alerts], "删行必须留痕")

    def test_无变更的保存不发告警(self):
        """同值提交/只改名不是"变更"：发了只会稀释同类告警（与口令门同一判据）。"""
        c, hdr = self._fresh()
        slot = c.post("/api/scheduler/executors/rows",
                      json={"proxy": "http://203.0.113.9:8080", "name": "机房A"},
                      headers=hdr).get_json()["slot"]
        self.alerts.clear()
        self.assertEqual(self._put(c, hdr, f"/api/scheduler/executors/rows/{slot}",
                                   {"proxy": "http://203.0.113.9:8080"}).status_code, 200)
        self.assertEqual(self._put(c, hdr, f"/api/scheduler/executors/rows/{slot}",
                                   {"name": "机房B"}).status_code, 200)
        self.assertEqual(self.alerts, [], "无实质变更的保存不该发变更告警")

    def test_告警正文不含出口凭据(self):
        """出口串可能带 user:pass，告警只落动作名与槽位——凭据不进邮件。"""
        c, hdr = self._fresh()
        r = c.post("/api/scheduler/executors/rows",
                   json={"proxy": "http://svc:SecretPw123@203.0.113.9:8080"}, headers=hdr)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        body = "\n".join(b for _t, b, _u in self.alerts)
        self.assertNotIn("SecretPw123", body, "告警正文不得含出口凭据")
        self.assertIn("追加执行体行", body, "至少要能看出动了哪个动作")

    def test_full_档执行体写不新增该告警(self):
        """full 档当次已要求口令，事后告警不重复发——该档行为逐字不变。"""
        self._set_tier("full")
        c, hdr = self._fresh()
        r = c.post("/api/scheduler/executors/rows",
                   json={"proxy": "http://203.0.113.9:8080", "confirm_password": ADMIN_PASS},
                   headers=hdr)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(self.alerts, [], "full 档本就有当次口令，不重复发事后告警")


class UserDeleteAlertTest(_TierBase):
    """删用户的两种模式：只有"清空账号"保留事后告警，full 分支只剩审计。"""

    TIER = "risk"

    def test_清空账号模式也有告警(self):
        c, hdr = self._fresh()
        self._ensure_user("u1@test.local")
        r = c.post("/api/users/u1@test.local/delete",
                   json={"mode": "accounts_only", "confirm_delay_ack": True}, headers=hdr)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        titles = [t for t, _b, _u in self.alerts]
        self.assertIn("高危管理操作告警", titles, f"实际告警 {titles}")
        body = "\n".join(b for _t, b, _u in self.alerts)
        self.assertIn("清空用户", body)
        self.assertNotIn("u1@test.local", body, "告警里的目标邮箱必须脱敏")
        self.assertIn("u1***@test.local", body)

    def test_完全删除模式不再发告警(self):
        """full 分支的即时告警已下线：清空账号那一路的补偿信号不受此影响（见上一例）。"""
        c, hdr = self._fresh()
        self._ensure_user("u1@test.local")
        r = c.post("/api/users/u1@test.local/delete",
                   json={"mode": "full", "confirm_delay_ack": True}, headers=hdr)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(self.alerts, [], f"完全删除告警已下线，实际 {self.alerts}")
        import db
        rows = [dict(x) for x in db.get_conn().execute(
            "SELECT detail FROM audit_logs WHERE action='user_delete'").fetchall()]
        self.assertTrue(rows, "完全删除必须留在审计链上")
        self.assertIn("mode=full", rows[-1]["detail"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
