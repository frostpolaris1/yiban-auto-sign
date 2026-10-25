# -*- coding: utf-8 -*-
"""二段资料暴露建模（工单 qmz4）：profile 内容与逐字段暴露开关。

标签：E · Web：认证/权限/API
覆盖：`PUT /api/accounts/<idx>/profile`（管理员维护面）与
    `PUT /api/my-accounts/<idx>/profile`（本人自助面），以及三视图的暴露装配。
关键断言：
    ① 默认全关：列表/详情/我的三视图回包**逐字节不变**——列表与详情 golden 复用
      `test_account_detail_narrow_read` 的实拍常量（同形夹具），我的视图为本文件
      夹具在改动前实拍；资料内容已落库但开关全关时，三视图仍逐字节不变；
    ② 开单字段：只有被开启的字段出现在我的视图与管理详情里；管理列表**永不**
      出现 profile（列表是逐行广播面，暴露出口收在限速+审计的单行面）——即使
      开关全开，列表回包仍与 golden 逐字节相等；关回后三视图逐字节还原；
    ③ 权限：普通用户访问管理面端点 403（全局守卫，跟既有账号编辑权限门同口径）；
      本人面按 `_my_account_indices_of` 收窄，非本人 idx 一律 404（存在性隐藏，
      与 /api/my-accounts 既有写口同口径）；未登录 401；
    ④ 落库口径：profile 内容是 AES-GCM 密文 JSON 串（AAD=手机号，与
      password/phone_code 同通道），原文不落库；profile_expose 开关明文 JSON
      （布尔集非 PII）；明文驻留自愈；改绑手机号重加密（AAD 陷阱守卫）；
    ⑤ 审计不含字段值：维护动作的审计 detail 只记字段名（审计面是 PII 出站面）；
    ⑥ 请求校验：空请求/未知字段/非布尔开关/非字符串值/超长一律 400；管理面
      写口缺对齐令牌（phone）409（fail-closed，与 delete/restore/review 同闸）。
依赖：纯本地 Flask test client + 临时 `.env`/SQLite；不联网、不访问真实易班接口；
    无需 node。

**为什么需要**：二段资料（姓名/学号/学院/班级/邮箱）全部默认不暴露是用户拍板的
最小暴露面。本文件钉住"回包零变化"这条硬判据与逐字段开关的唯一出口，下一个改
账号序列化的人越界把资料塞进列表时，这里的 golden 会红。

用法（项目根目录）：
    py -m pytest tests/test_account_profile_exposure.py -v
"""
import contextlib
import importlib.util
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

from werkzeug.security import generate_password_hash

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))
sys.path.insert(0, BASE)

import db  # noqa: E402

# 列表/详情的 golden 是**同形夹具**在改动前的实拍；跨测试文件引用常量在本仓有先例
# （test_alert_channel_dispatch 引 test_creds_write_accounting）。
from test_account_detail_narrow_read import (  # noqa: E402
    DETAIL_GOLDEN,
    LIST_GOLDEN,
)

TEST_KEY = "3f9a2c7e5b1d8046a7c3e9f2b6d10485c2a7f4b9e1d63058fa4c7b2e9d15803a"
ADMIN_PASS = "TestPass1234!"
USER_PASS = "UserPass123!"
OWNER = "user1@test.local"

#: 未改树实拍的 user1「我的账号」回包（夹具：4 账号，乙归 user1，pending）。
MY_VIEW_GOLDEN_USER1 = (
    b'{"accounts":[{"admin_index":1,"deleted":false,"deleted_at":"",'
    b'"deleted_by_me":false,"display_name":"\\u4e59","index":0,"logs":[],'
    b'"name":"\\u4e59","pause_forbidden":false,"phone":"13800138002",'
    b'"phone_code_masked":"","phone_model":"","queue_ahead":0,"reject_reason":"",'
    b'"state_icon":"\\u23f3","state_message":"","state_status":"pending",'
    b'"status":"pending","user_paused":false}],"ok":true}\n'
)

#: 未改树实拍的内置管理员「我的账号」回包（甲/丙，均 active；丙排队位 1）。
MY_VIEW_GOLDEN_ADMIN = (
    b'{"accounts":[{"admin_index":0,"deleted":false,"deleted_at":"",'
    b'"deleted_by_me":false,"display_name":"\\u7532","index":0,"logs":[],'
    b'"name":"\\u7532","pause_forbidden":true,"phone":"13800138001",'
    b'"phone_code_masked":"devi\\u20260001","phone_model":"MI 9","queue_ahead":0,'
    b'"reject_reason":"","state_icon":"\\u23f3","state_message":"\\u5f85\\u751f\\u6210",'
    b'"state_status":"pending","status":"active","user_paused":false},'
    b'{"admin_index":2,"deleted":false,"deleted_at":"","deleted_by_me":false,'
    b'"display_name":"\\u4e19","index":1,"logs":[],"name":"\\u4e19",'
    b'"pause_forbidden":true,"phone":"13800138003","phone_code_masked":'
    b'"devi\\u20260003","phone_model":"iPhone15","queue_ahead":1,"reject_reason":"",'
    b'"state_icon":"\\u23f3","state_message":"\\u5f85\\u751f\\u6210",'
    b'"state_status":"pending","status":"active","user_paused":false}],"ok":true}\n'
)

#: 夹具写入的二段资料值（断言这些明文不出现在不该出现的地方）。
PROFILE_CONTENT = {
    "real_name": "张三",
    "student_id": "20250001",
    "college": "计算机学院",
    "class_name": "软件2401",
    "email": "zhangsan@example.edu",
}


class AccountProfileExposureTest(unittest.TestCase):
    """三视图 golden + 逐字段开关 + 权限 + 落库口径的守卫。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-profile-expose-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(
                f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
                f"YIBAN_ADMIN_USER=admin\nYIBAN_ADMIN_PASSWORD={ADMIN_PASS}\n"
            )
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_ACCOUNTS_FILE"] = os.path.join(cls.tmp, "accounts.json")
        os.environ["YIBAN_USERS_FILE"] = os.path.join(cls.tmp, "users.json")
        os.environ["YIBAN_DB_FILE"] = cls.db_file
        os.environ["YIBAN_STATE_DIR"] = cls.tmp
        os.environ["YIBAN_LOG_FILE"] = os.path.join(cls.tmp, "sign.log")
        spec = importlib.util.spec_from_file_location(
            "webapp_profile_expose", os.path.join(BASE, "web", "app.py"))
        cls.webapp = importlib.util.module_from_spec(spec)
        sys.modules["webapp_profile_expose"] = cls.webapp
        with contextlib.suppress(Exception):
            spec.loader.exec_module(cls.webapp)

    @classmethod
    def tearDownClass(cls):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k in ("YIBAN_ACCOUNTS_KEY", "YIBAN_LOG_FILE", "YIBAN_STATE_DIR",
                  "YIBAN_ENV_FILE", "YIBAN_ACCOUNTS_FILE", "YIBAN_USERS_FILE",
                  "YIBAN_DB_FILE"):
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
        db.init_db(self.db_file, env_file=self.env_file)
        self._seed()

    # ---- 夹具 ----
    def _seed(self):
        """与 test_account_detail_narrow_read 同形的 4 账号（乙归 user1，丁已软删）。

        golden 逐字节成立的前提是夹具逐字段同形；改动夹具必须同步重拍三份 golden。
        """
        db.create_user(OWNER, generate_password_hash(USER_PASS), role="user")
        db.create_user("user2@test.local", generate_password_hash(USER_PASS), role="user")
        rows = (
            ("甲", "13800138001", "AlphaPW1!", "MI 9", "devicecode-alpha-0001",
             "admin", "active"),
            ("乙", "13800138002", "BetaPW2!", "", "", OWNER, "pending"),
            ("丙", "13800138003", "", "iPhone15", "devicecode-gamma-0003",
             "admin", "active"),
            ("丁", "13800138004", "DeltaPW4!", "", "", "admin", "active"),
        )
        for name, phone, pw, model, code, owner, status in rows:
            db.add_account({
                "name": name, "phone": phone, "password": pw,
                "phone_model": model, "phone_code": code,
                "owner": owner, "status": status, "reject_reason": "",
            })
        db.set_account_deleted(db.load_accounts()[3]["id"], True, "2026-10-01T00:00:00")

    def _client(self, username, password):
        c = self.webapp.create_app().test_client()
        r = c.post("/api/login", json={"username": username, "password": password})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return c

    def _admin_client(self):
        return self._client("admin", ADMIN_PASS)

    def _csrf(self, c):
        return c.get("/api/me").get_json()["csrf_token"]

    def _put_admin(self, idx, body, client=None):
        c = client or self._admin_client()
        token = self._csrf(c)
        return c.put(f"/api/accounts/{idx}/profile", json=body,
                     headers={"X-CSRF-Token": token})

    def _put_my(self, idx, body, username=OWNER, password=USER_PASS):
        c = self._client(username, password)
        token = self._csrf(c)
        return c.put(f"/api/my-accounts/{idx}/profile", json=body,
                     headers={"X-CSRF-Token": token})

    def _fill_profile(self):
        """给乙（全局 idx 1）写入完整资料但不开启任何开关；返回维护回包。"""
        r = self._put_admin(1, {
            "profile": dict(PROFILE_CONTENT),
            "phone": "138****8002",
        })
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return r

    # ---- ① 默认全关：三视图逐字节不变 ----
    def test_list_golden_unchanged_when_all_off(self):
        c = self._admin_client()
        r = c.get("/api/accounts")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_data(), LIST_GOLDEN, "默认全关：列表回包必须逐字节不变")

    def test_detail_golden_unchanged_when_all_off(self):
        c = self._admin_client()
        for idx, want in DETAIL_GOLDEN.items():
            r = c.get(f"/api/accounts/{idx}/detail")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.get_data(), want, f"默认全关：详情 idx={idx} 逐字节不变")

    def test_my_view_golden_unchanged_when_all_off(self):
        r = self._client(OWNER, USER_PASS).get("/api/my-accounts")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_data(), MY_VIEW_GOLDEN_USER1, "默认全关：我的视图逐字节不变")
        r = self._admin_client().get("/api/my-accounts")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_data(), MY_VIEW_GOLDEN_ADMIN,
                         "默认全关：管理员我的视图逐字节不变")

    def test_content_without_exposure_stays_byte_identical(self):
        """ sharper 形态：资料已在库里、开关全关——出站面仍必须一字不变。"""
        self._fill_profile()
        c = self._admin_client()
        self.assertEqual(c.get("/api/accounts").get_data(), LIST_GOLDEN)
        self.assertEqual(c.get("/api/accounts/1/detail").get_data(), DETAIL_GOLDEN[1])
        r = self._client(OWNER, USER_PASS).get("/api/my-accounts")
        self.assertEqual(r.get_data(), MY_VIEW_GOLDEN_USER1)

    # ---- ② 开单字段：只有目标视图出现、只有被开启的字段 ----
    def test_exposed_field_appears_only_in_my_view_and_detail(self):
        self._fill_profile()
        r = self._put_admin(1, {"expose": {"student_id": True},
                                "phone": "138****8002"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        # 本人视图：出现且只出现 student_id
        my = self._client(OWNER, USER_PASS).get("/api/my-accounts").get_json()
        self.assertEqual(my["accounts"][0].get("profile"), {"student_id": "20250001"})
        # 管理详情：同上
        detail = self._admin_client().get("/api/accounts/1/detail").get_json()["account"]
        self.assertEqual(detail.get("profile"), {"student_id": "20250001"})
        # 管理列表：即使开关已开，回包仍逐字节等于 golden（profile 永不进列表）
        listing = self._admin_client().get("/api/accounts")
        self.assertEqual(listing.get_data(), LIST_GOLDEN,
                         "管理列表是逐行广播面：开关开启后也不得出现 profile")
        self.assertNotIn(b'"profile"', listing.get_data())

    def test_turning_switch_off_restores_golden_bytes(self):
        self._fill_profile()
        self._put_admin(1, {"expose": {"student_id": True}, "phone": "138****8002"})
        r = self._put_admin(1, {"expose": {"student_id": False}, "phone": "138****8002"})
        self.assertEqual(r.status_code, 200)
        c = self._admin_client()
        self.assertEqual(c.get("/api/accounts").get_data(), LIST_GOLDEN)
        self.assertEqual(c.get("/api/accounts/1/detail").get_data(), DETAIL_GOLDEN[1])
        r = self._client(OWNER, USER_PASS).get("/api/my-accounts")
        self.assertEqual(r.get_data(), MY_VIEW_GOLDEN_USER1, "关回后我的视图必须逐字节还原")

    def test_list_golden_unchanged_with_every_switch_on(self):
        """观察项实例：**五个字段全开**时列表回包仍逐字节等于 golden。

        与单字段用例走同一代码路径（mask_account 的 masked 分支），但取极值——
        "全开"是最容易被误塞进列表的形态，单字段用例未必覆盖。
        """
        self._fill_profile()
        r = self._put_admin(1, {"expose": {k: True for k in PROFILE_CONTENT},
                                "phone": "138****8002"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        c = self._admin_client()
        listing = c.get("/api/accounts")
        self.assertEqual(listing.get_data(), LIST_GOLDEN,
                         "全开时管理列表仍不得出现 profile")
        self.assertNotIn(b'"profile"', listing.get_data())
        # 对照：同一时刻详情确实出现了全部五项（证明开关真的生效，列表不是"没开"才不变）
        detail = c.get("/api/accounts/1/detail").get_json()["account"]
        self.assertEqual(detail.get("profile"), PROFILE_CONTENT)

    # ---- 中-1：视图漂移后按旧 idx 写必须被拦 ----
    def test_stale_index_after_view_drift_is_rejected(self):
        """列表变动后旧 idx 指向另一行：带旧 phone 的写必须 409，且库里资料不变。

        成因是真实的：`sort_order` 决定我的视图顺序（`_my_account_indices_of` 保序），
        管理员整表重排/并发移动后，客户端手里的 idx 就指向了别人那一行。没有守卫
        就是"A 的资料写给 B"且回 200。
        """
        c = self._admin_client()  # admin 我的视图：[甲(idx0), 丙(idx1)]
        token = self._csrf(c)
        before = c.get("/api/my-accounts").get_json()["accounts"]
        self.assertEqual([a["phone"] for a in before], ["13800138001", "13800138003"])
        # 并发排序变动：把丙排到最前（等价于管理员重排后的顺序）
        conn = sqlite3.connect(self.db_file)
        try:
            conn.execute("UPDATE accounts SET sort_order=0 WHERE phone='13800138003'")
            conn.commit()
        finally:
            conn.close()
        after = c.get("/api/my-accounts").get_json()["accounts"]
        self.assertEqual([a["phone"] for a in after], ["13800138003", "13800138001"],
                         "前置条件：视图顺序已漂移")
        # 旧客户端仍按 idx=1 写，并带它记着的 phone（丙）
        r = c.put("/api/my-accounts/1/profile",
                  json={"expose": {"student_id": True}, "phone": "13800138003"},
                  headers={"X-CSRF-Token": token})
        self.assertEqual(r.status_code, 409, r.get_data(as_text=True))
        # 库里两行的资料都没被动过（错位写即使回了错码也可能已经落库）
        for phone in ("13800138001", "13800138003"):
            acc = next(a for a in db.load_accounts() if a["phone"] == phone)
            self.assertEqual(acc.get("profile", ""), "", f"{phone} 的资料不得被错位写改到")

    # ---- 中-2：整表替换回环保住资料与开关 ----
    def test_replace_accounts_round_trip_keeps_profile_and_expose(self):
        """`replace_accounts` 是导出→导入的整表重建路径：密文透传，不许静默丢列。"""
        self._fill_profile()
        self._put_admin(1, {"expose": {"student_id": True}, "phone": "138****8002"})
        raw = db.load_accounts_raw()  # 导出形态（profile 密文串、expose 明文串）
        db.replace_accounts(raw)
        acc = next(a for a in db.load_accounts() if a["phone"] == "13800138002")
        self.assertEqual(json.loads(acc["profile"]), PROFILE_CONTENT)
        self.assertEqual(json.loads(acc["profile_expose"]), {"student_id": True})
        # 明文形态（load_accounts 的 profile 是明文 JSON 串）同样可回环加密
        plain = [dict(a) for a in db.load_accounts()]
        db.replace_accounts(plain)
        acc = next(a for a in db.load_accounts() if a["phone"] == "13800138002")
        self.assertEqual(json.loads(acc["profile"]), PROFILE_CONTENT)
        self.assertIn("ct", json.loads(self._raw_row("13800138002")["profile"]),
                      "明文回环后列内必须仍是密文")

    def test_owner_toggle_via_my_endpoint_is_visible_to_self(self):
        """本人面写开关（真实用户动作）：本人视图立即可见，详情同样出现。"""
        r = self._put_my(0, {"profile": {"student_id": "20250001"},
                             "expose": {"real_name": True}})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        body = r.get_json()
        self.assertEqual(body["profile"], {"student_id": "20250001"},
                         "回显合并后的资料全文")
        self.assertEqual(body["expose"], {"real_name": True}, "回显开着的开关")
        my = self._client(OWNER, USER_PASS).get("/api/my-accounts").get_json()
        self.assertEqual(my["accounts"][0].get("profile"), {"real_name": ""},
                         "开了开关但未填值的字段按空串暴露")

    # ---- ③ 权限 ----
    def test_normal_user_gets_403_on_admin_endpoint(self):
        r = self._put_admin(0, {"expose": {"student_id": True}},
                            client=self._client("user2@test.local", USER_PASS))
        self.assertEqual(r.status_code, 403, r.get_data(as_text=True))

    def test_unauthenticated_gets_401(self):
        c = self.webapp.create_app().test_client()
        r = c.put("/api/accounts/0/profile", json={"expose": {}})
        self.assertEqual(r.status_code, 401, r.get_data(as_text=True))

    def test_non_owner_gets_404_on_my_endpoint(self):
        """非本人：本人面按自己的下标空间收窄，他人的号不在空间内 → 404（存在性隐藏，
        与 /api/my-accounts 既有写口同口径）。"""
        r = self._put_my(0, {"expose": {"student_id": True}},
                         username="user2@test.local")
        self.assertEqual(r.status_code, 404, r.get_data(as_text=True))

    # ---- ④ 落库口径 ----
    def _raw_row(self, phone):
        conn = sqlite3.connect(self.db_file)
        conn.row_factory = sqlite3.Row
        try:
            return dict(conn.execute("SELECT * FROM accounts WHERE phone=?",
                                     (phone,)).fetchone())
        finally:
            conn.close()

    def test_profile_is_ciphertext_at_rest_and_expose_is_plaintext(self):
        self._fill_profile()
        raw = self._raw_row("13800138002")["profile"]
        self.assertNotEqual(raw, "", "写入了资料的行，profile 列不得为空")
        envelope = json.loads(raw)
        self.assertIn("ct", envelope, "profile 列必须是 AES-GCM 密文 JSON 串")
        for value in PROFILE_CONTENT.values():
            self.assertNotIn(value, raw, "资料明文不得落库")
        expose_raw = self._raw_row("13800138002")["profile_expose"]
        self.assertEqual(expose_raw, "", "未开启任何开关时开关列为空串（空 = 全关）")
        # 走读路径能解回明文（加密与解密同通道闭环）
        acc = next(a for a in db.load_accounts() if a["phone"] == "13800138002")
        self.assertEqual(json.loads(acc["profile"]), PROFILE_CONTENT)

    def test_phone_rebind_reencrypts_profile(self):
        """AAD=手机号：改绑后必须用新号重加密，否则下次读取整行 RuntimeError。"""
        self._fill_profile()
        acc = next(a for a in db.load_accounts() if a["phone"] == "13800138002")
        result = db.update_account(acc["id"], {"phone": "13900139000"})
        self.assertTrue(result)
        rows = db.load_accounts()
        moved = next(a for a in rows if a["phone"] == "13900139000")
        self.assertEqual(json.loads(moved["profile"]), PROFILE_CONTENT,
                         "改绑后资料内容必须原样保留（经新 AAD 重加密）")

    def test_plaintext_residency_is_healed(self):
        """明文驻留自愈：手工把明文 JSON 塞进 profile 列，读取必须幂等加密回写。"""
        plain = json.dumps({"student_id": "20250001"}, ensure_ascii=False)
        conn = sqlite3.connect(self.db_file)
        try:
            conn.execute("UPDATE accounts SET profile=? WHERE phone=?",
                         (plain, "13800138002"))
            conn.commit()
        finally:
            conn.close()
        rows = db.load_accounts()
        healed = next(a for a in rows if a["phone"] == "13800138002")
        self.assertEqual(json.loads(healed["profile"]), {"student_id": "20250001"})
        raw = self._raw_row("13800138002")["profile"]
        self.assertIn("ct", json.loads(raw), "自愈后列内必须是密文")
        self.assertNotIn("20250001", raw)

    # ---- ⑤ 审计不含字段值 ----
    def test_audit_records_field_names_not_values(self):
        self._put_admin(1, {"profile": {"student_id": "20250001"},
                            "expose": {"student_id": True},
                            "phone": "138****8002"})
        rows = db.get_conn().execute(
            "SELECT action, target, detail FROM audit_logs WHERE "
            "action='account_profile_update'").fetchall()
        self.assertEqual(len(rows), 1, "管理员维护必须留痕")
        row = dict(rows[0])
        self.assertEqual(row["target"], "138****8002", "审计 target 只能是掩码号")
        self.assertIn("student_id", row["detail"], "detail 记字段名")
        for value in ("20250001", "张三"):
            self.assertNotIn(value, row["detail"], "审计不得出现字段值")
        for r in db.get_conn().execute(
                "SELECT detail FROM audit_logs").fetchall():
            self.assertNotIn("20250001", r["detail"], "任何审计行都不得出现字段值")

    def test_my_endpoint_audits_with_owner_identity(self):
        self._put_my(0, {"expose": {"real_name": True}})
        rows = db.get_conn().execute(
            "SELECT username, action FROM audit_logs WHERE "
            "action='my_account_profile_update'").fetchall()
        self.assertEqual(len(rows), 1)
        # actor 列沿用审计域的统一脱敏口径（mask_email；与 my_account_update 同族）
        self.assertEqual(dict(rows[0])["username"], "use***@test.local")

    # ---- ⑥ 请求校验 ----
    def test_empty_body_is_400(self):
        r = self._put_admin(1, {"phone": "138****8002"})
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))

    def test_unknown_field_is_400(self):
        r = self._put_admin(1, {"profile": {"nickname": "x"}, "phone": "138****8002"})
        self.assertEqual(r.status_code, 400)
        r = self._put_admin(1, {"expose": {"nickname": True}, "phone": "138****8002"})
        self.assertEqual(r.status_code, 400)

    def test_non_bool_expose_is_400(self):
        r = self._put_admin(1, {"expose": {"student_id": "yes"},
                                "phone": "138****8002"})
        self.assertEqual(r.status_code, 400)

    def test_non_string_profile_value_is_400(self):
        r = self._put_admin(1, {"profile": {"student_id": 20250001},
                                "phone": "138****8002"})
        self.assertEqual(r.status_code, 400)

    def test_overlong_value_is_400(self):
        r = self._put_admin(1, {"profile": {"real_name": "张" * 51},
                                "phone": "138****8002"})
        self.assertEqual(r.status_code, 400)
        r = self._put_admin(1, {"profile": {"email": "a" * 250 + "@x.test"},
                                "phone": "138****8002"})
        self.assertEqual(r.status_code, 400)

    def test_missing_alignment_phone_is_409_on_admin_endpoint(self):
        """管理面写口 fail-closed：不带对齐令牌不得写入（与 delete/restore 同闸）。"""
        c = self._admin_client()
        r = c.put("/api/accounts/1/profile", json={"expose": {"student_id": True}},
                  headers={"X-CSRF-Token": self._csrf(c)})
        self.assertEqual(r.status_code, 409, r.get_data(as_text=True))
        acc = next(a for a in db.load_accounts() if a["phone"] == "13800138002")
        self.assertEqual(acc.get("profile", ""), "", "409 之外库里那行不得被动过")

    def test_deleted_account_is_400(self):
        r = self._put_admin(3, {"expose": {"student_id": True},
                                "phone": "138****8004"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("已删除", r.get_json()["error"])


class ProfileFieldVocabularyTest(unittest.TestCase):
    """词表单一真源：展示层 `_PROFILE_FIELDS` 必须与存储层 `PROFILE_FIELDS` 逐项相等。

    两处各写一份是必然的（分层不互相导入私名）；手抄的副本迟早分叉——一边加字段、
    另一边漏加时，新字段写不进库或被写口拒收，两种都是静默失败。故用等值断言钉住，
    不做逐项抽查：顺序也要一致（展示层按此顺序装配出站字段）。
    """

    def test_front_fields_equal_storage_fields(self):
        from web.services import accounts_data
        from yiban.adapters.persistence import accounts as store
        self.assertEqual(tuple(accounts_data._PROFILE_FIELDS),
                         tuple(store.PROFILE_FIELDS))


if __name__ == "__main__":
    unittest.main()
