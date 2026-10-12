# -*- coding: utf-8 -*-
"""MF-49 出口面·Task 2-9b：存储与展示面（后端侧）回归钉。

覆盖（与任务书 8 条逐项对应；前端出口由 `tests/test_users_exit_surface_frontend.py`
node 真跑另钉）：
  ① `owner_display`/`/api/users.display`：邮箱本地部的遮罩展示口径唯一住在服务端
     （号形态本地部 → `138****0000`，其余 → 前 3 字符 + `***`）；
  ③ users 单条操作按**不透明 id** 定位：`/api/users/<int:id>/…` 生效、
     旧"邮箱编进 path"形态在路由层直接 404（不再被当邮箱解析）；
  ④ 审计 `actor` 列写入口即遮罩（`actor_tag`=`mask_email`，幂等，非邮箱标识原样），
     暂停冷却查询双形态兼容升级边界，purge 的 target 逐条遮罩；
  ⑤ MF-111：引号 dict 键与 `Authorization: Basic …==` 尾巴收口到值域判据族，
     且**不误伤 JSON 正文**（遮罩后仍合法可解析、非凭据键与数字叶子不动）；
  ⑥ `_bounded_audit_json` 对极端 `admin_to` 的顶破收口（字符串字段限量 + 视图让位，
     产出永远是可解析 JSON）；
  ⑦ 撇号名 `Account(...)` 的 repr 吞除加固（两种引号各自配对）。

假值口径：手机号一律 13800000000（遮罩后 138****0000）；邮箱遮罩后 a***@/xxx***@ 形态。

标签：G · 安全：脱敏/审计/配置注入
覆盖：上述 ①③④⑤⑥⑦ 各出口至少一条"遮罩形态"断言 + 一条"明文不得出现"反例断言
对应实现：`yiban/masking.py`、`yiban/store/audit_chain.py::actor_tag`、
`yiban/store/events.py::_actor_forms`、`yiban/store/users.py::find_user_by_id/purge_deleted_users_hard`、
`web/routes/users_api.py`、`web/routes/notify.py::_bounded_audit_json`、
`web/services/accounts_data.py::_owner_display_of`
关键断言：所有断言打的是**真函数/真路由**的输出（test client + 真 SQLite 审计行），
不是源码文本扫描；actor/target 断言同时钉"遮罩值等于口径输出"与"明文整行不得出现"。
依赖：无网络、无 node；临时 `.env`/SQLite 由 `setUpClass` 搭建。
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

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TEST_KEY = "a" * 64
AUDIT_KEY = "b" * 64
ADMIN_PASS = "MasterPass#2026"

PHONE_LOCAL_EMAIL = "13800000000@qq.com"     # 本地部即手机号（现网 9/96 的真实形态）
PLAIN_EMAIL = "trail-user@test.local"        # 普通本地部
MASKED_PHONE_LOCAL = "138****0000"
MASKED_PLAIN = "tra***@test.local"


def _load_webapp(tag):
    spec = importlib.util.spec_from_file_location(
        f"webapp_{tag}", os.path.join(BASE, "web", "app.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"webapp_{tag}"] = mod
    with contextlib.suppress(Exception):
        spec.loader.exec_module(mod)
    return mod


class _Base(unittest.TestCase):
    """共享脚手架：临时 .env + SQLite + webapp + 主管理员登录（照批 2 各文件先例）。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-2-9b-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with io.open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(
                f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\nYIBAN_AUDIT_KEY={AUDIT_KEY}\n"
                "YIBAN_ADMIN_USER=admin@test.local\n"
                f"YIBAN_ADMIN_PASSWORD={ADMIN_PASS}\n"
                # 高危门禁固定 full 档（与 test_admin_privilege_web 同法）：默认 risk 档
                # 下不可逆动作的倒计时确认会抢在"用户不存在 404"之前，把定位面断言
                # 变成档位断言。本文件钉的是 id 定位与遮罩出口，不是档位。
                "YIBAN_PW_GATE=full\n"
            )
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.accounts_file = os.path.join(cls.tmp, "accounts.json")
        os.environ.update({
            "YIBAN_ACCOUNTS_KEY": TEST_KEY,
            "YIBAN_AUDIT_KEY": AUDIT_KEY,
            "YIBAN_ENV_FILE": cls.env_file,
            "YIBAN_ACCOUNTS_FILE": cls.accounts_file,
            "YIBAN_DB_FILE": cls.db_file,
            "YIBAN_USERS_FILE": os.path.join(cls.tmp, "users.json"),
            "YIBAN_STATE_DIR": cls.tmp,
            "YIBAN_LOG_FILE": os.path.join(cls.tmp, "sign.log"),
        })
        global db
        import db
        cls.db = db
        cls.webapp = _load_webapp(cls.__name__)

    @classmethod
    def tearDownClass(cls):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        for k in ("YIBAN_ACCOUNTS_KEY", "YIBAN_AUDIT_KEY", "YIBAN_ENV_FILE",
                  "YIBAN_ACCOUNTS_FILE", "YIBAN_DB_FILE", "YIBAN_USERS_FILE",
                  "YIBAN_STATE_DIR", "YIBAN_LOG_FILE"):
            os.environ.pop(k, None)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        for suffix in ("", "-wal", "-shm"):
            p = self.db_file + suffix
            if os.path.exists(p):
                os.remove(p)
        db.init_db(self.db_file, migrate_from=self.accounts_file, env_file=self.env_file)

    def _audit_rows(self, action):
        with db._conn_lock:
            return [dict(r) for r in db.get_conn().execute(
                "SELECT username, action, target, detail FROM audit_logs "
                "WHERE action=? ORDER BY id", (action,)).fetchall()]

    def _master_client(self):
        c = self.webapp.create_app().test_client()
        r = c.post("/api/login", json={"username": "admin@test.local",
                                       "password": ADMIN_PASS})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        t = c.get("/api/me").get_json()["csrf_token"]
        return c, {"X-CSRF-Token": t}


# ---------------------------------------------------------------------------
# ① owner_display：本地部遮罩口径（服务端唯一真源）
# ---------------------------------------------------------------------------
class OwnerDisplayTest(_Base):
    def test_mask_email_local_phone_form(self):
        """本地部即手机号 → 号遮罩形态；**不是**整段本地部直出（旧行为）。"""
        from yiban.masking import mask_email_local
        self.assertEqual(mask_email_local("13800000000"), MASKED_PHONE_LOCAL)
        # RED 可能性：把号形态也走"前3+***"（138***）会让归属列失去"这是个号"的可辨性，
        # 也钉住"改回整段直出"（泄漏）——两个退化方向都红。

    def test_mask_email_local_general_and_idempotent(self):
        from yiban.masking import mask_email_local
        self.assertEqual(mask_email_local("alice"), "ali***")
        self.assertEqual(mask_email_local("ab"), "ab***")
        self.assertEqual(mask_email_local("ali***"), "ali***")   # 幂等
        self.assertEqual(mask_email_local(""), "")

    def test_owner_display_of_endpoints(self):
        from web.services.accounts_data import _owner_display_of
        self.assertEqual(_owner_display_of(PHONE_LOCAL_EMAIL), MASKED_PHONE_LOCAL)
        # display 是**本地部**的遮罩形态（旧展示名也只给本地部）：域名不进归属列。
        self.assertEqual(_owner_display_of(PLAIN_EMAIL), "tra***")
        self.assertEqual(_owner_display_of("admin"), "管理员")
        # 展示名不得含完整本地部（号形态时即完整手机号）
        self.assertNotIn("13800000000", _owner_display_of(PHONE_LOCAL_EMAIL))

    def test_mask_account_owner_display_masked(self):
        from web.services.accounts_data import mask_account
        acc = {"name": "N", "phone": "13800000000", "password": "p",
               "model": "M", "status": "active", "owner": PHONE_LOCAL_EMAIL}
        out = mask_account(acc, 0)
        self.assertEqual(out["owner_display"], MASKED_PHONE_LOCAL)
        self.assertNotIn("13800000000", out["owner_display"])

    def test_api_users_row_has_masked_display(self):
        """/api/users 每行带不透明 id 与遮罩 display（前端禁止再算第二套）。"""
        db.create_user(PHONE_LOCAL_EMAIL,
                       self.webapp.generate_password_hash("UserPass#123"))
        c, hdr = self._master_client()
        users = c.get("/api/users", headers=hdr).get_json()["users"]
        row = [u for u in users if u["email"] == PHONE_LOCAL_EMAIL]
        self.assertEqual(len(row), 1)
        self.assertEqual(row[0]["display"], MASKED_PHONE_LOCAL)
        self.assertIsInstance(row[0]["id"], int)


# ---------------------------------------------------------------------------
# ③ 不透明 id：路由收紧后的行为钉
# ---------------------------------------------------------------------------
class OpaqueIdRoutingTest(_Base):
    def test_email_in_path_is_dead_route(self):
        """旧"邮箱编进 path"的形态在 `<int:...>` 转换器处直接 404——
        从路由层杜绝明文邮箱回潮（响应不再是本端点的 JSON，Werkzeug 兜底）。"""
        c, hdr = self._master_client()
        r = c.post("/api/users/someone@test.local/role",
                   json={"role": "user", "confirm_password": ADMIN_PASS}, headers=hdr)
        self.assertEqual(r.status_code, 404)
        body = r.get_data(as_text=True)
        self.assertNotIn("someone@test.local/role", body,
                         "404 兜底也不得把请求 path 原样回显")

    def test_ghost_numeric_id_returns_json_404(self):
        """不存在的 id 走业务 404（`用户不存在`）：证明服务端按 id 解析回邮箱再操作。"""
        c, hdr = self._master_client()
        for tail, body in (("/role", {"role": "user", "confirm_password": ADMIN_PASS}),
                           ("/password", {"password": "FreshPass#123",
                                          "confirm_password": ADMIN_PASS}),
                           ("/delete", {"mode": "full", "confirm_password": ADMIN_PASS})):
            with self.subTest(tail=tail):
                r = c.post(f"/api/users/987654321{tail}", json=body, headers=hdr)
                self.assertEqual(r.status_code, 404, r.get_data(as_text=True))
                self.assertIn("用户不存在", r.get_json()["error"])

    def test_role_change_by_id_works_and_url_has_no_email(self):
        """真 id 定位：设为/取消管理员生效，且审计/响应面不含操作目标邮箱明文。"""
        db.create_user(PLAIN_EMAIL, self.webapp.generate_password_hash("UserPass#123"))
        db.add_account({"name": "A", "phone": "13900139001", "password": "pw",
                        "status": "active", "owner": PLAIN_EMAIL})
        uid = db.find_user(PLAIN_EMAIL)["id"]
        c, hdr = self._master_client()
        r = c.post(f"/api/users/{uid}/role",
                   json={"role": "admin", "confirm_password": ADMIN_PASS}, headers=hdr)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(db.find_user(PLAIN_EMAIL).get("role"), "admin")
        msg = r.get_json().get("msg", "")
        self.assertNotIn(PLAIN_EMAIL, msg, "成功文案必须用遮罩邮箱（_mask_email）")
        self.assertIn(MASKED_PLAIN, msg)


# ---------------------------------------------------------------------------
# ④ 审计 actor 遮罩 + 冷却双形态 + purge target 遮罩
# ---------------------------------------------------------------------------
class ActorSurfaceTest(_Base):
    def test_audit_actor_masked_at_write_entry(self):
        db.audit(PLAIN_EMAIL, "unit_exit_probe", "target-x", "detail-x")
        rows = self._audit_rows("unit_exit_probe")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["username"], MASKED_PLAIN)
        ok, broken, first = db.verify_audit_chain()
        self.assertTrue(ok, f"遮罩在哈希**之前**落位，链必须自洽：broken={broken} first={first}")
        # 幂等：调用方传遮罩形态进 audit，结果不变（不存在"库里明文、出口才遮"的第二份）
        db.audit(MASKED_PLAIN, "unit_exit_probe2", "", "")
        self.assertEqual(self._audit_rows("unit_exit_probe2")[0]["username"], MASKED_PLAIN)

    def test_non_email_actor_passthrough(self):
        for who in ("admin", "system", "?"):
            db.audit(who, "unit_exit_probe3", "", "")
        self.assertEqual({r["username"] for r in self._audit_rows("unit_exit_probe3")},
                         {"admin", "system", "?"})

    def test_pause_cooldown_dual_form_across_upgrade(self):
        """冷却查询双形态：新行 actor 是遮罩态，但按**明文邮箱**查仍命中
        （升级边界历史行为明文形态，两套同查，暂停冷却在升级前后都成立）。"""
        from yiban.store import events
        db.audit(PLAIN_EMAIL, "my_account_pause", "", "")   # 写入即遮罩
        ts = events.last_pause_at(PLAIN_EMAIL)             # 传入明文
        self.assertIsNotNone(ts, "遮罩收口后冷却不得哑火")
        self.assertEqual(events.pause_count_since(PLAIN_EMAIL, "2000-01-01 00:00:00"), 1)
        # 别人的冷却不得被串用
        self.assertIsNone(events.last_pause_at("other-user@test.local"))

    def test_purge_target_masked_per_address(self):
        """purge 的 target 逐条遮罩（旧形态整表明文），计数与 detail 语义不变。"""
        db.create_user(PLAIN_EMAIL, self.webapp.generate_password_hash("UserPass#123"))
        db.soft_delete_user_with_accounts(PLAIN_EMAIL)
        purged = db.purge_deleted_users_hard(
            [PLAIN_EMAIL], audit_spec={"username": "admin", "action": "user_deleted_purge"})
        self.assertEqual(purged, [PLAIN_EMAIL])   # 返回值是**明文**列表（供上层回显），审计面才遮罩
        rows = self._audit_rows("user_deleted_purge")
        self.assertTrue(rows)
        self.assertIn(MASKED_PLAIN, rows[0]["target"])
        self.assertNotIn(PLAIN_EMAIL, rows[0]["target"], "target 不得含完整邮箱")
        self.assertNotIn(PLAIN_EMAIL, json.dumps(rows, ensure_ascii=False))


# ---------------------------------------------------------------------------
# ⑤ MF-111：引号键与 Basic 尾巴收口；不误伤 JSON 正文
# ---------------------------------------------------------------------------
class MF111SanitizeTest(_Base):
    def test_quoted_key_credential_full_table(self):
        """整张凭据名表的**引号键形态**（vars()/json.dumps 调试输出）都要被拦。"""
        from yiban.masking import sanitize_text
        for key in ("access_token", "refresh_token", "api_key", "session_id",
                    "csrf_token", "password", "phone_code", "authorization"):
            samples = (
                '{"%s": "secret value"}' % key,     # dict/JSON 双引号键
                "{'%s': 'secret value'}" % key,     # vars() 单引号键
                '{"%s": "secret value", "user": "bob"}' % key,   # 复合 JSON
            )
            for s in samples:
                out = sanitize_text(s)
                with self.subTest(key=key, sample=s):
                    self.assertNotIn("secret", out, "凭据值必须被遮")
                    self.assertIn("***", out)

    def test_authorization_basic_tail_swallowed(self):
        from yiban.masking import sanitize_text
        out = sanitize_text("req header Authorization: Basic ZGVmOg== sent")
        self.assertNotIn("ZGVmOg", out, "base64 尾巴（含 == ）必须整体收口，不得留尾")
        self.assertIn("authorization=***", out)
        out2 = sanitize_text("token=a b==")
        self.assertNotIn("b==", out2, "值续段 `b==` 不得被当成新键值对放走")

    def test_benign_json_not_harmed(self):
        """"不误伤 JSON 正文"判据：非凭据键的值、数字/布尔/null 叶子原样保留；
        凭据键遮罩后仍是合法 JSON（解析/还原能力不丢）。"""
        from yiban.masking import sanitize_text
        benign = ('{"user": "bob", "note": "the token expires soon", '
                  '"count": 42, "ratio": -1.5, "ok": true, "nil": null}')
        self.assertEqual(sanitize_text(benign), benign,
                         "普通 JSON 正文一字不动（键名不在凭据名表、值是句子）")
        cred = '{"access_token": "a b", "user": "bob"}'
        out = sanitize_text(cred)
        parsed = json.loads(out)          # 遮罩后必须仍可解析
        self.assertEqual(parsed["access_token"], "***")
        self.assertEqual(parsed["user"], "bob")

    def test_cookie_pairs_still_masked_individually(self):
        from yiban.masking import sanitize_text
        out = sanitize_text("Set-Cookie: session=abc123; path=/; csrf=xy")
        self.assertNotIn("abc123", out)
        self.assertNotIn("xy", out.split("csrf=")[1].split(";")[0])
        self.assertIn("path=/", out, "非凭据属性不得被连带吞掉（cookie 逐对口径保留）")

    def test_numeric_leaf_invariant(self):
        """数字叶子不变量（2-9a 入册）：凭据键后的**数字**不被改写——
        遮罩数字会破坏 JSON 类型契约；裸号只允许以字符串形态进 payload。"""
        from yiban.masking import sanitize_text
        s = '{"password": 123456, "n": true}'
        self.assertEqual(json.loads(sanitize_text(s)), {"password": 123456, "n": True})


# ---------------------------------------------------------------------------
# ⑥ _bounded_audit_json：admin_to 顶破收口
# ---------------------------------------------------------------------------
class BoundedAuditJsonTest(_Base):
    def test_extreme_admin_to_cuts_with_flag_and_fits_budget(self):
        """极端 admin_to（多条长地址逐项打码后仍超预算）：字符串字段逐段收缩并打
        `_cut` 标记，产出仍是**可解析 JSON** 且**回落到预算内**（旧行为：admin_to
        无人裁剪 → 整条 detail 顶破预算，由 `_scope_detail` 后缀退化把 JSON 截烂）。"""
        from web.routes.notify import _AUDIT_DETAIL_BUDGET, _bounded_audit_json
        detail = {
            "enabled": True, "admin_notify": True,
            "admin_to": ", ".join(f"very-long-admin-name-{i:02d}@remote.example.com"
                                  for i in range(8)),
            "smtps_from": ["smtps://user:pass@a.long.example.com:465"] * 2,
            "smtps_to": [],
        }
        s = _bounded_audit_json(detail)
        out = json.loads(s)
        self.assertTrue(out.get("admin_to_cut"), "收缩必须留显式 _cut 标记")
        self.assertIsInstance(out["admin_to"], str)
        self.assertTrue(out["enabled"])
        self.assertLessEqual(len(s), _AUDIT_DETAIL_BUDGET,
                             "收缩的目标是回到预算内——做不到就仍会被 _scope_detail 截烂")

    def test_hopeless_combo_gives_up_view_keeps_keys_parseable(self):
        """超长 host（不可裁剪字段）+ 超长收件人的病态组合：地址类视图整体让位
        `too_long`，开关键与 JSON 完整性保住（宁可缺一面，不产截烂的伪存证）。"""
        from web.routes.notify import _bounded_audit_json
        detail = {
            "enabled": True, "admin_notify": True,
            "smtps_host": "h" * 300,                    # 裁不动的超长字段：逼出让位分支
            "admin_to": "a" * 300 + "@x.example.com",
            "smtps_from": ["smtps://" + "u" * 200 + ":" + "p" * 200 + "@h.example.com"] * 3,
            "smtps_to": ["smtps://" + "q" * 250 + "@" + "i" * 250 + ".example.com"],
        }
        out = json.loads(_bounded_audit_json(detail))   # 不抛、可解析
        self.assertTrue(out["enabled"])
        self.assertEqual(out.get("smtps_view"), "too_long")
        self.assertIn("smtps_host", out)                # 让位只丢"视图"，其它键保留
        for k in ("smtps_from", "smtps_to", "admin_to", "admin_to_from",
                  "smtps_from_cut", "smtps_to_cut", "admin_to_cut", "admin_to_from_cut"):
            self.assertNotIn(k, out, "让位形态不得残留超限视图与收缩标记")

    def test_small_detail_untouched(self):
        from web.routes.notify import _bounded_audit_json
        detail = {"enabled": False, "admin_to": "a***@qq.com", "smtps_from": [], "smtps_to": []}
        self.assertEqual(json.loads(_bounded_audit_json(detail)), detail)


# ---------------------------------------------------------------------------
# ⑦ Account repr 撇号加固（登记之外的"顺路加固"由此钉）+ 数字叶子不变量登记位
# ---------------------------------------------------------------------------
class AccountReprSwallowTest(_Base):
    def test_apostrophe_name_repr_swallowed(self):
        """撇号名让 dataclass repr 改用**双引号**包裹——旧吞除正则只配单引号，
        在双引号段前失效整段外泄；2-9b 收口后两种引号各自配对。"""
        from yiban.masking import sanitize_text
        s = "failed for Account(name=\"O'Brien\", password='sup3r-secret!', phone='13800000000')"
        out = sanitize_text(s)
        self.assertNotIn("sup3r-secret", out)
        self.assertNotIn("13800000000", out)
        self.assertIn("Account(***)", out)
        # 值内含括号也不截断（跨过 `)`/`(`）
        out2 = sanitize_text('Account(name="a)b", password="x(y") tail')
        self.assertNotIn("x(y", out2)
        self.assertIn("tail", out2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
