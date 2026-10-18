# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""设备字段按需补全（后端）：端点 e2e 与下发口径。

标签：E · Web：认证/权限/API ＋ G · 安全：脱敏/审计/配置注入

覆盖：`POST /api/accounts/<int:idx>/device-refresh`（管理员）与
    `POST /api/my-accounts/<int:idx>/device-refresh`（归属人本人）。两条端点各触发
    一次"读服务端已登记设备并落库"（登录 + `getState` + 写账号行）。另钉设备识别码的
    **下发口径**：原文永不下发，只发 `phone_code_masked`（`前4…后4`）。

预期行为（先于实现写下，按通用工程语义）：
1. 补全成功 ⇒ 账号行的 `phone_code`/`phone_model` 写入服务端登记值，响应带
   `phone_code_masked`；响应正文**不含**识别码原文；
2. 读不到（网络失败 / 上游未登记 / WAF 拦截）⇒ 明确回"未取到"，且**不得**把库里既有
   的设备值冲成空值（`update_account` 的"键不存在则不动该列"行为保住）；
3. 防错位：请求体必须带 `phone`（管理面 fail-closed）；缺失/不匹配 ⇒ 409，且**不发起登录**；
4. 只有"会签到的账号"（`signs_in`：未软删、审核态生效）可补全；pending/rejected/已删 ⇒ 400，
   且不发起登录；
5. 补全走与"账号验证"同一个外呼闸门（全局席位 + 每用户配额）：席位满 ⇒ 503、配额尽 ⇒ 429，
   两者都**不发起登录**；
6. 权限档：管理员可补全任一账号；普通用户只能补全**本人**账号（他人下标 ⇒ 404）；
   普通用户打管理面端点 ⇒ 403，且不发起登录；
7. 每次补全留一条审计（管理面 `account_device_refresh` / 本人 `my_account_device_refresh`）。

打桩目标：设备读取走真实调用链（`YibanClient._read_registered_device` → 协议层 →
`parse_device_state` → `update_account`），只把**登录**与**上游 `getState`** 两个网络出口
换成替身——比打桩 `_read_registered_device` 更强：连"落库那一步真跑了"一起验了。
依赖：纯本地 Flask test client + 临时 `.env`/SQLite；登录与 `getState` 按需打桩，不触网。
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

import requests
from werkzeug.security import generate_password_hash

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TEST_KEY = "c" * 64
ADMIN_USER = "admin"
ADMIN_PASS = "MasterPass#2026"
USER_PASS = "secret1"

PHONE = "13800138000"
OWNER = "owner@test.local"
OTHER_OWNER = "other@test.local"

#: 服务端登记的合成设备（首尾各 4 位可辨，便于断言掩码形态；真值是指纹，不入仓库）
CODE = "abcd" + "0" * 56 + "wxyz"
MODEL = "Example-EX1000"


def _device_envelope(code=CODE, model=MODEL, authorized=True):
    """`getState` 的成功信封（平台形状：`data` 承载登记设备）。"""
    return {"code": 0, "msg": "", "data": {
        "authorize": authorized, "Code": code, "PhoneModel": model, "UpdateTime": 1761517844,
    }}


class _DeviceApiBase(unittest.TestCase):
    """隔离环境（临时 .env/SQLite）+ 真实 web app + 网络出口替身。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-devfill-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(
                f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
                f"YIBAN_ADMIN_USER={ADMIN_USER}\nYIBAN_ADMIN_PASSWORD={ADMIN_PASS}\n"
                "YIBAN_MAIL_ADMIN_TO=admin@test.local\n"
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
        os.environ["YIBAN_LOG_FILE"] = os.path.join(cls.tmp, "sign.log")
        global db
        import db
        spec = importlib.util.spec_from_file_location(
            "webapp_devfill", os.path.join(BASE, "web", "app.py"))
        cls.webapp = importlib.util.module_from_spec(spec)
        sys.modules["webapp_devfill"] = cls.webapp
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
        db.init_db(self.db_file, migrate_from=self.accounts_file, env_file=self.env_file)
        db.create_user(OWNER, generate_password_hash(USER_PASS))
        db.create_user(OTHER_OWNER, generate_password_hash(USER_PASS))

    # ---- 工具 ----
    def _login(self, c, username, password):
        r = c.post("/api/login", json={"username": username, "password": password})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return c.get("/api/me").get_json()["csrf_token"]

    @staticmethod
    def _csrf(token):
        return {"X-CSRF-Token": token}

    def _add_account(self, phone=PHONE, owner="admin", status="active",
                     phone_code="", phone_model=""):
        return db.add_account({
            "name": "账号A", "phone": phone, "password": "pw",
            "phone_model": phone_model, "phone_code": phone_code,
            "owner": owner, "status": status,
        })

    def _row(self, phone=PHONE):
        return next(a for a in db.load_accounts() if a.get("phone") == phone)

    def _audit_actions(self):
        return [r["action"] for r in db.get_conn().execute(
            "SELECT action FROM audit_logs").fetchall()]

    # ---- 网络出口替身 ----
    def _login_ok(self):
        """登录成功替身：只置 `logged_in`，不发请求。"""
        p = mock.patch.object(self.webapp.signin.YibanClient, "login_killyiban",
                              lambda self: setattr(self, "logged_in", True))
        p.start()
        self.addCleanup(p.stop)

    def _login_must_not_run(self):
        """断言"这次请求不得发起真实登录"。"""
        p = mock.patch.object(self.webapp.signin.YibanClient, "login_killyiban",
                              side_effect=AssertionError("不得发起登录"))
        p.start()
        self.addCleanup(p.stop)

    def _device_state(self, envelope):
        import yiban.platform as yiban_platform
        p = mock.patch.object(yiban_platform, "fetch_device_state",
                              return_value=yiban_platform.SignResponse(data=envelope))
        p.start()
        self.addCleanup(p.stop)

    def _device_state_raises(self, exc):
        import yiban.platform as yiban_platform
        p = mock.patch.object(yiban_platform, "fetch_device_state", side_effect=exc)
        p.start()
        self.addCleanup(p.stop)


class AdminDeviceRefreshTest(_DeviceApiBase):
    """管理面端点：补全成功、失败不冲库、防错位、闸门与权限。"""

    def test_refresh_reads_registry_persists_and_never_returns_raw_code(self):
        self._add_account()
        self._login_ok()
        self._device_state(_device_envelope())
        c = self.webapp.create_app().test_client()
        token = self._login(c, ADMIN_USER, ADMIN_PASS)
        r = c.post("/api/accounts/0/device-refresh", json={"phone": PHONE},
                   headers=self._csrf(token))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        body = r.get_json()
        self.assertTrue(body.get("ok"), body)
        row = self._row()
        self.assertEqual(row.get("phone_code"), CODE, "登记值必须落库")
        self.assertEqual(row.get("phone_model"), MODEL, "登记机型必须落库")
        self.assertEqual(body["device"]["phone_model"], MODEL)
        self.assertEqual(body["device"]["phone_code_masked"], "abcd…wxyz")
        self.assertNotIn(CODE, r.get_data(as_text=True),
                         "响应正文不得含识别码原文")
        self.assertIn("account_device_refresh", self._audit_actions())

    def test_refresh_failure_preserves_existing_device_values(self):
        """红线：读不到时不得把库里既有的设备值冲成空值。"""
        self._add_account(phone_code="OLD" + "9" * 61, phone_model="Old-Model")
        self._login_ok()
        self._device_state(_device_envelope(code="", model=""))
        c = self.webapp.create_app().test_client()
        token = self._login(c, ADMIN_USER, ADMIN_PASS)
        r = c.post("/api/accounts/0/device-refresh", json={"phone": PHONE},
                   headers=self._csrf(token))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertFalse(r.get_json().get("ok"))
        self.assertIn("未取到", r.get_json().get("error", ""))
        row = self._row()
        self.assertEqual(row.get("phone_code"), "OLD" + "9" * 61, "既有识别码不得被冲掉")
        self.assertEqual(row.get("phone_model"), "Old-Model", "既有不得被冲掉")

    def test_refresh_network_failure_is_reported_and_preserves_values(self):
        self._add_account(phone_code="OLD" + "9" * 61, phone_model="Old-Model")
        self._login_ok()
        self._device_state_raises(requests.exceptions.ConnectionError("网络炸了"))
        c = self.webapp.create_app().test_client()
        token = self._login(c, ADMIN_USER, ADMIN_PASS)
        r = c.post("/api/accounts/0/device-refresh", json={"phone": PHONE},
                   headers=self._csrf(token))
        self.assertEqual(r.status_code, 502, r.get_data(as_text=True))
        self.assertIn("未取到", r.get_json().get("error", ""))
        self.assertEqual(self._row().get("phone_code"), "OLD" + "9" * 61)

    def test_refresh_without_phone_is_rejected_and_never_logs_in(self):
        self._add_account()
        self._login_must_not_run()
        c = self.webapp.create_app().test_client()
        token = self._login(c, ADMIN_USER, ADMIN_PASS)
        r = c.post("/api/accounts/0/device-refresh", json={}, headers=self._csrf(token))
        self.assertEqual(r.status_code, 409, r.get_data(as_text=True))

    def test_refresh_with_wrong_phone_is_rejected_and_never_logs_in(self):
        self._add_account()
        self._login_must_not_run()
        c = self.webapp.create_app().test_client()
        token = self._login(c, ADMIN_USER, ADMIN_PASS)
        r = c.post("/api/accounts/0/device-refresh", json={"phone": "13900139000"},
                   headers=self._csrf(token))
        self.assertEqual(r.status_code, 409, r.get_data(as_text=True))

    def test_refresh_rejects_account_that_does_not_sign_in(self):
        self._add_account(status="pending")
        self._login_must_not_run()
        c = self.webapp.create_app().test_client()
        token = self._login(c, ADMIN_USER, ADMIN_PASS)
        r = c.post("/api/accounts/0/device-refresh", json={"phone": PHONE},
                   headers=self._csrf(token))
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))

    def test_refresh_blocked_when_web_login_seats_are_busy(self):
        """席位满 ⇒ 503，且不发起登录（全局席位是 web 进程真实登录的唯一闸）。"""
        self._add_account()
        self._login_must_not_run()
        m = self.webapp
        held = []
        while m._verify_sem.acquire(blocking=False):
            held.append(1)
        try:
            self.assertTrue(held, "测试前置：应能占满席位")
            c = m.create_app().test_client()
            token = self._login(c, ADMIN_USER, ADMIN_PASS)
            r = c.post("/api/accounts/0/device-refresh", json={"phone": PHONE},
                       headers=self._csrf(token))
            self.assertEqual(r.status_code, 503, r.get_data(as_text=True))
        finally:
            for _ in held:
                m._verify_sem.release()

    def test_refresh_consumes_the_same_quota_as_account_verification(self):
        """配额尽 ⇒ 429，且不发起登录（与账号验证共用同一份账）。"""
        self._add_account()
        self._login_must_not_run()
        from web import security as _security
        with mock.patch.object(_security, "VERIFY_MAX", 0):
            c = self.webapp.create_app().test_client()
            token = self._login(c, ADMIN_USER, ADMIN_PASS)
            r = c.post("/api/accounts/0/device-refresh", json={"phone": PHONE},
                       headers=self._csrf(token))
        self.assertEqual(r.status_code, 429, r.get_data(as_text=True))


class OwnerDeviceRefreshTest(_DeviceApiBase):
    """本人端点：只能补全本人账号；越权与越档都不发起登录。"""

    def test_owner_refreshes_own_account(self):
        self._add_account(owner=OWNER)
        self._login_ok()
        self._device_state(_device_envelope())
        c = self.webapp.create_app().test_client()
        token = self._login(c, OWNER, USER_PASS)
        r = c.post("/api/my-accounts/0/device-refresh", json={"phone": PHONE},
                   headers=self._csrf(token))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(r.get_json().get("ok"))
        self.assertEqual(self._row().get("phone_code"), CODE)
        self.assertIn("my_account_device_refresh", self._audit_actions())

    def test_owner_cannot_refresh_another_users_account(self):
        self._add_account(owner=OTHER_OWNER)
        self._login_must_not_run()
        c = self.webapp.create_app().test_client()
        token = self._login(c, OWNER, USER_PASS)
        r = c.post("/api/my-accounts/0/device-refresh", json={"phone": PHONE},
                   headers=self._csrf(token))
        self.assertEqual(r.status_code, 404, r.get_data(as_text=True))
        # 必须是"归属收窄后的账号不存在"，不是路由缺失的 `未知接口`
        self.assertEqual(r.get_json().get("error"), "账号不存在")

    def test_regular_user_cannot_reach_admin_endpoint(self):
        self._add_account()
        self._login_must_not_run()
        c = self.webapp.create_app().test_client()
        token = self._login(c, OWNER, USER_PASS)
        r = c.post("/api/accounts/0/device-refresh", json={"phone": PHONE},
                   headers=self._csrf(token))
        self.assertEqual(r.status_code, 403, r.get_data(as_text=True))


class DeviceCodeMaskingTest(_DeviceApiBase):
    """下发口径：原文永不下发，只发掩码；掩码幂等且复用仓内唯一口径。"""

    def test_mask_device_code_shape_and_idempotence(self):
        from yiban.core.masking import mask_device_code
        self.assertEqual(mask_device_code(CODE), "abcd…wxyz")
        self.assertEqual(mask_device_code("abcd…wxyz"), "abcd…wxyz", "已掩码串原样返回")
        self.assertEqual(mask_device_code(""), "")
        self.assertNotIn("0" * 56, mask_device_code(CODE))

    def test_account_views_never_expose_raw_code(self):
        self._add_account(phone_code=CODE, phone_model=MODEL)
        c = self.webapp.create_app().test_client()
        self._login(c, ADMIN_USER, ADMIN_PASS)
        detail = c.get("/api/accounts/0/detail")
        self.assertEqual(detail.status_code, 200, detail.get_data(as_text=True))
        body = detail.get_json()["account"]
        self.assertEqual(body.get("phone_code_masked"), "abcd…wxyz")
        self.assertNotIn(CODE, detail.get_data(as_text=True),
                         "详情面（唯一完整值面）也不得下发识别码原文")
        # 本人视图同口径
        c2 = self.webapp.create_app().test_client()
        self._login(c2, OWNER, USER_PASS)
        mine = c2.get("/api/my-accounts")
        self.assertEqual(mine.status_code, 200, mine.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
