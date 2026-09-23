# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""web 层拆分边界：名字面完整 + 转发落到 app 模块级真状态。

`web/app.py` 把实现按域拆入 `web/routes/`、`web/services/`、`web/render.py`、`web/security.py`
后只剩「应用工厂 + 跨域中间件 + 名字面（再导出与转发包装）」。本文件把各域拆分契约并到一处，
逐域钉住同一组边界：

1. **名字面完整**：routes 经 `m.<名字>` 或 `sys.modules[current_app.import_name].<名字>`
   晚查找取用的名字，在 app 模块与真源模块上都可达；`web/app.py` 收尾契约另钉名字面总账
   （`web/routes/*.py` 命中总数与兼容名面计数）。
2. **转发注入 app 模块级状态**：`.env` 路径与读取器、窗口解析器、告警出口、原子落盘、
   状态目录等留在 app 且会被测试打桩或直接赋值改写；服务层若另存一份绑定，改写会**静默**
   失效——故转发必须在调用时刻现取后传入。
3. **别名加载安全**：拆分模块不得导入 `web.app`（普通 import 会在别名加载的测试进程里
   造出第二份 app），发现即红。

功能：web 应用拆分后的边界回归（名字面 / 转发注入 / 别名加载）。
归属：`web/` 应用层测试；被测真源为 `web/services/*`、`web/render.py`、`web/security.py`。
复用：各域 `MOVED_*` 名字面清单、`_load_webapp()` 装载助手、`BASE` / `TEST_KEY` 公共常量。
通信：导入 `web.app` 与各真源模块，并另起 subprocess 验证别名加载；由 pytest 收集
`unittest.TestCase`，不启动常驻服务器。
"""
import contextlib
import importlib.util
import io
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from unittest import mock

import flask

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


TEST_KEY = "a" * 64


PHONE = "13800138000"


OTHER = "13900139000"


MOVED_ACCOUNTS = (
    "load_accounts",
    "load_accounts_raw",
    "load_users",
    "mask_account",
    "find_account_index",
    "_duplicate_phone_error",
    "_owner_has_other_live",
    "validate_account",
    "_as_signin_account",
    "_verify_account_clean",
    "_stale_idx_guard",
    "_delete_grace_remaining",
    "_slot_to_label",
    "_estimate_slot",
    "_owner_display_of",
    "_password_policy_error",
    "_admin_password_policy_error",
    "_mask_email",
    # 只被迁出族使用的常量，随所属域搬
    "ACCOUNT_STATUS_PENDING",
    "ACCOUNT_STATUS_ACTIVE",
    "ACCOUNT_STATUS_REJECTED",
    "PHONE_RE",
    "DELETE_GRACE_DAYS",
    "PASSWORD_MIN_LEN",
    "_PASSWORD_CLASS_PATTERNS",
    "_PASSWORD_CLASS_LABELS",
    "_PASSWORD_MIN_CLASSES",
    "_PASSWORD_CLASS_HINT",
    "_PASSWORD_POLICY_HINT",
    "ADMIN_PASSWORD_MIN_LEN",
    "ADMIN_PASSWORD_MIN_CLASSES",
)


MOVED_VERIFY = (
    "VerifyGateBusy",
    "VerifyQuotaExceeded",
    "run_verify_with_gate",
    "_reject_account",
    "_reclaim_stale_verify_jobs",
    "_verify_queue_full",
    "_start_verify_job",
    "verify_async_enabled",
    "_account_verify_enabled",
    # 只被迁出族使用，随所属域搬
    "VERIFY_JOBS_MAX_PENDING",
)


MOVED_MEASURE = (
    "_measure_state_path",
    "_read_measure_state",
    "_write_measure_state",
    "_measure_cooldown_remaining",
    "_pick_measure_account",
    # 只被迁出族使用，随所属域搬
    "MEASURE_STATE_FILE",
)


PURE_REEXPORTS_ACCOUNTS = (
    "load_accounts",
    "load_accounts_raw",
    "load_users",
    "mask_account",
    "find_account_index",
    "_duplicate_phone_error",
    "_owner_has_other_live",
    "validate_account",
    "_as_signin_account",
    "_verify_account_clean",
    "_stale_idx_guard",
    "_delete_grace_remaining",
    "_owner_display_of",
    "_password_policy_error",
    "_admin_password_policy_error",
    "_mask_email",
)


PURE_REEXPORTS_VERIFY = (
    "VerifyGateBusy",
    "VerifyQuotaExceeded",
    "_reject_account",
    "_reclaim_stale_verify_jobs",
    "_verify_queue_full",
    "_start_verify_job",
)


PURE_REEXPORTS_MEASURE = (
    "_read_measure_state",
    "_measure_cooldown_remaining",
    "_pick_measure_account",
)


FORWARDED_ACCOUNTS = ("_slot_to_label", "_estimate_slot")


FORWARDED_VERIFY = ("run_verify_with_gate", "verify_async_enabled", "_account_verify_enabled")


FORWARDED_MEASURE = ("_measure_state_path", "_write_measure_state")


APP_HELD_STATE_ACC = {
    "web/services/accounts_data.py": (
        "ENV_FILE", "LOG_FILE", "STATE_DIR", "_atomic_write", "_verify_sem",
        "_verify_attempt_allowed", "_rate_lock", "send_notification", "verify_jobs",
    ),
    "web/services/verify_queue.py": (
        "ENV_FILE", "STATE_DIR", "_atomic_write", "_verify_sem",
        "_verify_attempt_allowed", "_file_lock", "verify_jobs", "_sign_window",
    ),
    "web/services/measure.py": (
        "ENV_FILE", "LOG_FILE", "STATE_DIR", "_atomic_write", "_file_lock", "verify_jobs",
    ),
}


SERVICE_SOURCES_ACC = (
    "web/services/accounts_data.py",
    "web/services/verify_queue.py",
    "web/services/measure.py",
)


class WebServicesAccountsSplitContractTest(unittest.TestCase):
    """以别名加载的 app 副本为靶子（routes 与测试实际看到的那一份）。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-accounts-split-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        cls._old_env = {k: os.environ.get(k) for k in
                        ("YIBAN_ENV_FILE", "YIBAN_DB_FILE", "YIBAN_STATE_DIR",
                         "YIBAN_ACCOUNTS_FILE", "YIBAN_ACCOUNTS_KEY", "YIBAN_LOG_FILE",
                         "YIBAN_VERIFY_ASYNC")}
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_DB_FILE"] = os.path.join(cls.tmp, "yiban.db")
        os.environ["YIBAN_STATE_DIR"] = cls.tmp
        os.environ["YIBAN_ACCOUNTS_FILE"] = os.path.join(cls.tmp, "accounts.json")
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
        os.environ["YIBAN_LOG_FILE"] = os.path.join(cls.tmp, "sign.log")
        os.environ.pop("YIBAN_VERIFY_ASYNC", None)
        cls._write_raw("")
        spec = importlib.util.spec_from_file_location(
            "webapp_accountssplit", os.path.join(BASE, "web", "app.py"))
        cls.webapp = importlib.util.module_from_spec(spec)
        sys.modules["webapp_accountssplit"] = cls.webapp
        with contextlib.suppress(Exception):
            spec.loader.exec_module(cls.webapp)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k, v in cls._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    @classmethod
    def _write_raw(cls, text):
        with io.open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n" + text)

    def setUp(self):
        self._write_raw("")
        os.environ.pop("YIBAN_VERIFY_ASYNC", None)
        self.webapp.ENV_FILE = self.env_file
        self.webapp.STATE_DIR = self.tmp

    # ------------------------------------------------------------------
    # 1. 名字面
    # ------------------------------------------------------------------
    def test_every_moved_name_reachable_on_app_and_home_module(self):
        from web.services import accounts_data as ad
        from web.services import measure as ms
        from web.services import verify_queue as vq
        for name in MOVED_ACCOUNTS:
            self.assertTrue(hasattr(self.webapp, name), f"web.app 兼容面缺失 {name}")
            self.assertTrue(hasattr(ad, name), f"web/services/accounts_data.py 缺 {name}")
        for name in MOVED_VERIFY:
            self.assertTrue(hasattr(self.webapp, name), f"web.app 兼容面缺失 {name}")
            self.assertTrue(hasattr(vq, name), f"web/services/verify_queue.py 缺 {name}")
        for name in MOVED_MEASURE:
            self.assertTrue(hasattr(self.webapp, name), f"web.app 兼容面缺失 {name}")
            self.assertTrue(hasattr(ms, name), f"web/services/measure.py 缺 {name}")

    def test_verify_jobs_name_not_taken_by_new_module(self):
        """`web.app.verify_jobs` 仍是 yiban 真源；新模块不得占用这个名字（否则
        `from web.services.verify_queue import *` 之类会遮蔽宿主名字面）。"""
        from web.services import verify_queue as vq
        from yiban.attempt import jobs as yb_jobs
        self.assertIs(self.webapp.verify_jobs, yb_jobs,
                      "m.verify_jobs 必须仍是 yiban.attempt.jobs 真源")
        self.assertFalse(hasattr(vq, "verify_jobs"),
                         "web/services/verify_queue.py 不得定义/别名 verify_jobs")
        self.assertIs(vq.attempt_jobs, yb_jobs, "新模块须用真源（别名 attempt_jobs）")

    def test_pure_reexports_are_same_object(self):
        from web.services import accounts_data as ad
        from web.services import measure as ms
        from web.services import verify_queue as vq
        for name in PURE_REEXPORTS_ACCOUNTS:
            self.assertIs(getattr(self.webapp, name), getattr(ad, name),
                          f"{name} 应为同一个对象（web.app 只是再导出）")
        for name in PURE_REEXPORTS_VERIFY:
            self.assertIs(getattr(self.webapp, name), getattr(vq, name),
                          f"{name} 应为同一个对象（web.app 只是再导出）")
        for name in PURE_REEXPORTS_MEASURE:
            self.assertIs(getattr(self.webapp, name), getattr(ms, name),
                          f"{name} 应为同一个对象（web.app 只是再导出）")
        # 随域搬迁的常量：值与来源口径保持不变
        self.assertEqual(
            (self.webapp.ACCOUNT_STATUS_PENDING, self.webapp.ACCOUNT_STATUS_ACTIVE,
             self.webapp.ACCOUNT_STATUS_REJECTED), ("pending", "active", "rejected"))
        self.assertEqual(self.webapp.PHONE_RE.pattern, r"^1\d{10}$")
        self.assertEqual(self.webapp.PASSWORD_MIN_LEN, 10)
        self.assertEqual(self.webapp._PASSWORD_MIN_CLASSES, 2)
        self.assertEqual(self.webapp.ADMIN_PASSWORD_MIN_LEN, 12)
        self.assertEqual(self.webapp.ADMIN_PASSWORD_MIN_CLASSES, 3)
        self.assertEqual(self.webapp.MEASURE_STATE_FILE, "capacity-measure.json")
        self.assertEqual(self.webapp.VERIFY_JOBS_MAX_PENDING, self.webapp.verify_jobs.MAX_PENDING)

    def test_forwarders_actually_forward(self):
        """注入型名字不得退化成纯再导出（否则 app 侧打桩面静默失效）。"""
        from web.services import accounts_data as ad
        from web.services import measure as ms
        from web.services import verify_queue as vq
        for name, mod in (*((n, ad) for n in FORWARDED_ACCOUNTS),
                          *((n, vq) for n in FORWARDED_VERIFY),
                          *((n, ms) for n in FORWARDED_MEASURE)):
            self.assertIsNot(getattr(self.webapp, name), getattr(mod, name),
                             f"{name} 必须是转发包装（调用时刻现取 app 侧名字）")

    def test_forwarders_keep_original_call_arity(self):
        """routes / 既有测试按原实参个数调用，转发不得改签名。"""
        self.assertIsNone(self.webapp._slot_to_label(None))
        self.assertIsInstance(self.webapp._estimate_slot(PHONE), tuple)
        self.assertTrue(self.webapp._measure_state_path().endswith("capacity-measure.json"))
        self.webapp._write_measure_state(os.path.join(self.tmp, "m.json"), {"at": ""})
        # 带闸校验三参、两个开关零参（与迁出前一致）
        with mock.patch.object(self.webapp, "_verify_attempt_allowed", return_value=True), \
                mock.patch.object(self.webapp, "_verify_account_clean", return_value=None):
            self.assertIsNone(self.webapp.run_verify_with_gate(
                {"phone": PHONE}, "admin", {"count": 0}))
        self.assertIsInstance(self.webapp.verify_async_enabled(), bool)
        self.assertIsInstance(self.webapp._account_verify_enabled(), bool)

    # ------------------------------------------------------------------
    # 2. 转发注入 app 模块级状态（打桩往返）
    # ------------------------------------------------------------------
    def test_load_accounts_stub_reaches_estimate_slot(self):
        """`web.app.load_accounts` 是全仓最高频的打桩名：替换后 `_estimate_slot` 必须跟着变。"""
        accounts = [
            {"phone": OTHER, "status": "active", "deleted": False},
            {"phone": PHONE, "status": "active", "deleted": False},
        ]
        with mock.patch.object(self.webapp, "read_env",
                               return_value={}), \
                mock.patch.object(self.webapp, "_sign_window",
                                  return_value=((6, 30), (7, 50))), \
                mock.patch.object(self.webapp, "edge_config", return_value=(0, 0)), \
                mock.patch.object(self.webapp, "load_accounts", return_value=accounts) as spy:
            got = self.webapp._estimate_slot(PHONE)
        self.assertEqual(spy.call_count, 1, "转发必须现取 load_accounts（而非服务层自持绑定）")
        self.assertEqual(got, ("06:30~06:35", "（每日固定时段，块内时刻每天略有抖动）"))
        # 打桩换成"没有这个号"的列表 → 结果随之变化（证明确实用了桩）
        with mock.patch.object(self.webapp, "read_env", return_value={}), \
                mock.patch.object(self.webapp, "load_accounts", return_value=[]):
            self.assertEqual(self.webapp._estimate_slot(PHONE), (None, ""))

    def test_sign_window_and_edge_stubs_reach_slot_labels(self):
        """`web.app._sign_window` / `edge_config` 的既有打桩点对迁出后的两处消费方生效。"""
        with mock.patch.object(self.webapp, "_sign_window",
                               return_value=((8, 0), (9, 0))):
            self.assertEqual(self.webapp._slot_to_label(0), "08:00")
            self.assertEqual(self.webapp._slot_to_label(5), "08:05")
        accounts = [{"phone": PHONE, "status": "active", "deleted": False}]
        with mock.patch.object(self.webapp, "read_env", return_value={}), \
                mock.patch.object(self.webapp, "_sign_window",
                                  return_value=((6, 30), (7, 50))), \
                mock.patch.object(self.webapp, "edge_config", return_value=(0, 0)), \
                mock.patch.object(self.webapp, "load_accounts", return_value=accounts):
            full = self.webapp._estimate_slot(PHONE)
        with mock.patch.object(self.webapp, "read_env", return_value={}), \
                mock.patch.object(self.webapp, "_sign_window",
                                  return_value=((7, 0), (8, 0))), \
                mock.patch.object(self.webapp, "edge_config", return_value=(0, 0)), \
                mock.patch.object(self.webapp, "load_accounts", return_value=accounts):
            shifted = self.webapp._estimate_slot(PHONE)
        self.assertEqual(full[0], "06:30~06:35")
        self.assertEqual(shifted[0], "07:00~07:05", "窗口打桩必须换掉预计时段")

    def test_estimate_slot_block_cap_zero_means_unlimited(self):
        """`YIBAN_BLOCK_CAP=0`（不限容量，与 my.py 拥挤度同口径）时预计时段照常返回。

        不能拿块容量当除数——否则用户端自选片接口对全员 500。分块线下全员落首块：
        第 20 人（idx=19）默认块容量 15 时应落第 2 块，不限容量时必须回到第 1 块。
        """
        accounts = [{"phone": f"1380013{i:04d}", "status": "active", "deleted": False}
                    for i in range(20)]
        target = accounts[-1]["phone"]
        self._write_raw("YIBAN_BLOCK_CAP=0\n")
        with mock.patch.object(self.webapp, "_sign_window",
                               return_value=((6, 30), (7, 50))), \
                mock.patch.object(self.webapp, "edge_config", return_value=(0, 0)), \
                mock.patch.object(self.webapp, "load_accounts", return_value=accounts):
            got = self.webapp._estimate_slot(target)
        self.assertEqual(got, ("06:30~06:35", "（每日固定时段，块内时刻每天略有抖动）"),
                         "不限容量时全员落首块，且不得除零")
        # 反证：默认块容量 15 下同一目标落第 2 块，说明上面的断言真的钉住了口径
        self._write_raw("YIBAN_BLOCK_CAP=15\n")
        with mock.patch.object(self.webapp, "_sign_window",
                               return_value=((6, 30), (7, 50))), \
                mock.patch.object(self.webapp, "edge_config", return_value=(0, 0)), \
                mock.patch.object(self.webapp, "load_accounts", return_value=accounts):
            capped = self.webapp._estimate_slot(target)
        self.assertEqual(capped[0], "06:35~06:40")

    def test_estimate_slot_nonempty_on_fallback_window(self):
        """裁剪吃空回退：预计签到时段按有效窗口算，不得静默变空。

        原实现自拼 `eff_lo/eff_hi`（原始窗口 + 原始裁剪）：原始 07:00~07:10 各裁 300s
        时 span=0、无有效块 ⇒ 返回 (None, "")，用户端"预计签到时段"整块空白。改消费
        有效窗口（回退默认 06:30~07:50、前后各 60s）后首块 06:31~06:35，与引擎同源。
        """
        self._write_raw("YIBAN_SIGN_START=07:00\nYIBAN_SIGN_END=07:10\n"
                        "YIBAN_WINDOW_EDGE_FRONT_SEC=300\nYIBAN_WINDOW_EDGE_BACK_SEC=300\n")
        accounts = [{"phone": PHONE, "status": "active", "deleted": False}]
        with mock.patch.object(self.webapp, "load_accounts", return_value=accounts):
            got = self.webapp._estimate_slot(PHONE)
        self.assertEqual(got, ("06:31~06:35", "（每日固定时段，块内时刻每天略有抖动）"),
                         "回退窗口下不得返回空（原实现 span=0 → (None, \"\")）")

    def test_estimate_slot_still_fails_closed_when_no_usable_block(self):
        """fail-closed 语义保留：确实没有可用片时仍返回 (None, "")，不回退成默认片。"""
        import yiban.window as yb_window
        degenerate = yb_window.Window(390, 470, 400.0, 400.0, 60, 60)
        accounts = [{"phone": PHONE, "status": "active", "deleted": False}]
        with mock.patch.object(self.webapp, "sign_window_bounds", return_value=degenerate), \
                mock.patch.object(self.webapp, "load_accounts", return_value=accounts):
            got = self.webapp._estimate_slot(PHONE)
        self.assertEqual(got, (None, ""))

    def test_read_env_and_env_file_stubs_reach_verify_switches(self):
        """`web.app.read_env` / `ENV_FILE` 是既有开关打桩点（test_probe 的写法）。"""
        with mock.patch.object(self.webapp, "read_env",
                               return_value={"YIBAN_ACCOUNT_VERIFY": "1"}):
            self.assertTrue(self.webapp._account_verify_enabled())
        with mock.patch.object(self.webapp, "read_env", return_value={}):
            self.assertFalse(self.webapp._account_verify_enabled())
        self._write_raw("YIBAN_ACCOUNT_VERIFY=on\nYIBAN_VERIFY_ASYNC=yes\n")
        self.assertTrue(self.webapp._account_verify_enabled(),
                        "赋值 ENV_FILE 后必须读到新文件")
        self.assertTrue(self.webapp.verify_async_enabled())

    def test_os_environ_wins_over_env_file_for_async_switch(self):
        self._write_raw("YIBAN_VERIFY_ASYNC=1\n")
        os.environ["YIBAN_VERIFY_ASYNC"] = "0"
        try:
            self.assertFalse(self.webapp.verify_async_enabled(),
                             "os.environ 优先（便于临时覆盖）")
        finally:
            os.environ.pop("YIBAN_VERIFY_ASYNC", None)
        self.assertTrue(self.webapp.verify_async_enabled(), "未设环境变量时读 .env")
        self._write_raw("")
        self.assertFalse(self.webapp.verify_async_enabled(), "默认关")

    def test_verify_account_clean_stub_reaches_gate(self):
        """`web.app._verify_account_clean` 是既有打桩点：带闸校验必须取到桩。"""
        with mock.patch.object(self.webapp, "_verify_attempt_allowed", return_value=True), \
                mock.patch.object(self.webapp, "_verify_account_clean",
                                  return_value="stub-err") as spy:
            got = self.webapp.run_verify_with_gate({"phone": PHONE}, "admin", {})
        self.assertEqual(got, "stub-err")
        self.assertEqual(spy.call_count, 1)

    def test_gate_stub_points_are_injected(self):
        """席位与配额判定也是现取：打桩必须改变闸门结论。"""
        with mock.patch.object(self.webapp, "_verify_sem") as sem:
            sem.acquire.return_value = False
            with self.assertRaises(self.webapp.VerifyGateBusy):
                self.webapp.run_verify_with_gate({"phone": PHONE}, "admin", {})
            self.assertEqual(sem.release.call_count, 0, "抢不到席位不得释放别人的席位")
        with mock.patch.object(self.webapp, "_verify_attempt_allowed", return_value=False), \
                mock.patch.object(self.webapp, "_verify_account_clean",
                                  return_value=None) as spy:
            with self.assertRaises(self.webapp.VerifyQuotaExceeded):
                self.webapp.run_verify_with_gate({"phone": PHONE}, "admin", {})
            self.assertEqual(spy.call_count, 0, "配额用尽时不得发起外呼")

    def test_state_dir_assignment_reaches_measure_path(self):
        other = os.path.join(self.tmp, "other-state")
        os.makedirs(other, exist_ok=True)
        with mock.patch.object(self.webapp, "STATE_DIR", other):
            got = self.webapp._measure_state_path()
        self.assertTrue(got.startswith(other), "转发必须现取 STATE_DIR")

    def test_atomic_write_stub_reaches_measure_write(self):
        payload = {"at": "2026-09-19 06:00:00", "seconds": None, "sample": "138****8000"}
        with mock.patch.object(self.webapp, "_atomic_write") as spy:
            self.webapp._write_measure_state(os.path.join(self.tmp, "sub", "m.json"), payload)
        self.assertEqual(spy.call_count, 1, "落盘必须经 web.app 的 _atomic_write（打桩点）")
        path, text = spy.call_args.args
        self.assertEqual(path, os.path.join(self.tmp, "sub", "m.json"))
        self.assertIn("138****8000", text)
        self.assertIs(spy.call_args.kwargs["chmod_priv"], True)

    # ------------------------------------------------------------------
    # 3. 行为往返（口径逐字不变）
    # ------------------------------------------------------------------
    def test_mask_account_masking_and_fields(self):
        acc = {"phone": PHONE, "password": "pw", "phone_code": "code",
               "name": "小明", "owner": "user@example.com"}
        view = self.webapp.mask_account(acc, 0)
        self.assertEqual(view["phone"], "138****8000")
        self.assertEqual(view["owner"], "use***@example.com")
        self.assertEqual(view["owner_display"], "user")
        self.assertEqual(view["display_name"], "小明")
        self.assertEqual(view["status"], "active", "缺 status 时按已生效展示")
        self.assertTrue(view["has_password"])
        self.assertTrue(view["has_phone_code"])
        self.assertNotIn("password", view, "口令绝不下发")
        self.assertNotIn("phone_code", view, "识别码绝不下发")
        full = self.webapp.mask_account(acc, 7, masked=False)
        self.assertEqual(full["phone"], PHONE)
        self.assertEqual(full["owner"], "user@example.com")
        self.assertEqual(full["index"], 7)
        unnamed = self.webapp.mask_account({}, 2)
        self.assertEqual(unnamed["display_name"], "账号3")
        self.assertEqual(unnamed["owner_display"], "管理员")
        self.assertEqual(unnamed["status"], "active")

    def test_mask_email_and_owner_display(self):
        m = self.webapp._mask_email
        self.assertEqual(m("user@example.com"), "use***@example.com")
        self.assertEqual(m("use***@example.com"), "use***@example.com", "幂等")
        self.assertEqual(m("not-an-email"), "not-an-email")
        self.assertEqual(m("@example.com"), "@example.com")
        self.assertEqual(m(""), "")
        od = self.webapp._owner_display_of
        self.assertEqual(od("admin"), "管理员")
        self.assertEqual(od(""), "管理员")
        self.assertEqual(od("someone@x.io"), "someone")
        self.assertEqual(od("plain"), "plain")

    def test_stale_idx_guard_matches_and_fails_closed(self):
        g = self.webapp._stale_idx_guard
        acc = {"phone": PHONE}
        self.assertFalse(g(acc, {"phone": PHONE}), "全号一致放行")
        self.assertFalse(g(acc, {"phone": "138****8000"}), "掩码形态归一后一致放行")
        self.assertTrue(g(acc, {"phone": OTHER}), "号码不同即错位")
        self.assertFalse(g(acc, {}), "未带 phone 的旧客户端默认不校验")
        self.assertTrue(g(acc, {}, fail_closed=True), "凭据写路径拿不出标识按错位处理")
        self.assertFalse(g(acc, None), "非 dict 且不 fail_closed 时不校验")
        self.assertTrue(g(acc, None, fail_closed=True))

    def test_delete_grace_remaining_formats(self):
        now = datetime(2026, 9, 19, 12, 0, 0)
        with mock.patch.object(self.webapp.clock, "now", return_value=now):
            self.assertEqual(self.webapp._delete_grace_remaining(""), 0)
            self.assertEqual(self.webapp._delete_grace_remaining(None), 0)
            self.assertEqual(self.webapp._delete_grace_remaining("不是时间"), 0)
            fresh = (now - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
            remain = self.webapp._delete_grace_remaining(fresh)
            self.assertAlmostEqual(remain, 6 * 86400, delta=60, msg="7 天宽限期 − 1 天")
            expired = (now - timedelta(days=8)).strftime("%Y-%m-%d %H:%M:%S")
            self.assertEqual(self.webapp._delete_grace_remaining(expired), 0)
            iso = (now - timedelta(days=2)).isoformat()
            self.assertAlmostEqual(self.webapp._delete_grace_remaining(iso), 5 * 86400,
                                   delta=60, msg="存量 ISO 格式自动回退")

    def test_validate_account_messages_verbatim(self):
        v = self.webapp.validate_account
        self.assertEqual(v({"name": "x" * 51}, False), ("名称过长（最多 50 字）", None))
        self.assertEqual(v({}, False), ("手机号为必填项", None))
        self.assertEqual(v({"phone": "12345"}, False),
                         ("手机号格式不正确（应为 1 开头的 11 位数字）", None))
        self.assertEqual(v({"phone": PHONE}, True), ("密码为必填项", None))
        self.assertEqual(v({"phone": PHONE, "phone_model": "m" * 51}, False),
                         ("设备型号过长（最多 50 字）", None))
        self.assertEqual(v({"phone": PHONE, "phone_code": "c" * 129}, False),
                         ("设备识别码过长", None))
        err, clean = v({"name": "  n  ", "phone": f" {PHONE} ", "password": " pw ",
                        "phone_model": " m ", "phone_code": " c "}, True)
        self.assertIsNone(err)
        self.assertEqual(clean, {"name": "n", "phone": PHONE, "password": "pw",
                                 "phone_model": "m", "phone_code": "c"},
                         "清洗仍逐字段 strip（旧客户端兼容面）")

    def test_find_index_duplicate_error_and_owner_live(self):
        accounts = [
            {"phone": OTHER, "owner": "a@x.io", "deleted": False},
            {"phone": PHONE, "owner": "me@x.io", "deleted": True, "deleted_by": "me@x.io"},
        ]
        self.assertEqual(self.webapp.find_account_index(accounts, PHONE), 1)
        self.assertIsNone(self.webapp.find_account_index(accounts, "13700137000"))
        self.assertEqual(
            self.webapp._duplicate_phone_error(accounts, PHONE, "me@x.io"),
            "该手机号对应你刚删除的账号，可先撤销删除；或等 7 天自动清除后再提交")
        self.assertIsNone(self.webapp._duplicate_phone_error(accounts, PHONE, "other@x.io"),
                          "他人号码冲突不得泄露归属")
        self.assertIsNone(self.webapp._duplicate_phone_error(accounts, "13700137000", "me@x.io"))
        self.assertTrue(self.webapp._owner_has_other_live(
            [{"owner": "me@x.io", "deleted": False}, {"owner": "me@x.io", "deleted": False}],
            {"owner": "me@x.io"}))
        self.assertFalse(self.webapp._owner_has_other_live(
            [{"owner": "admin", "deleted": False}], {"owner": "admin"}),
            "admin 不受一人一号限制")

    def test_password_policy_errors_verbatim(self):
        pp = self.webapp._password_policy_error
        self.assertEqual(pp("Abcdefg1"), "密码至少 10 位，且包含大小写字母、数字、符号中的至少两类")
        self.assertEqual(pp("abcdefghijkl"), "密码需包含大小写字母、数字、符号中的至少两类")
        self.assertEqual(pp(""), "密码至少 10 位，且包含大小写字母、数字、符号中的至少两类")
        for ok in ("Abcdefghij", "abcdefg123", "!@#$%^&*()12", "aaaaaaaaa中", "Abcdefgh12"):
            with self.subTest(pw=ok):
                self.assertIsNone(pp(ok), "任意两类即过、符号算一类")
        ap = self.webapp._admin_password_policy_error
        self.assertEqual(
            ap("Ab1"), "至少 12 位，且包含大写字母、小写字母、数字、符号中的至少三类")
        self.assertEqual(ap("Abcdefghijkl"), "需包含大写字母、小写字母、数字、符号中的至少三类")
        self.assertIsNone(ap("Abcdefghij12"))
        self.assertIsNotNone(ap("Abcdefghijkl"), "12 位单类不满足管理员档")

    def test_as_signin_account_carries_id(self):
        acc = self.webapp._as_signin_account(
            {"phone": PHONE, "password": "pw", "phone_model": "m",
             "phone_code": "c", "id": 7})
        self.assertEqual(acc.account_id, 7)
        self.assertEqual(acc.phone, PHONE)
        self.assertEqual(acc.password, "pw")
        self.assertEqual(self.webapp._as_signin_account({}).account_id, 0,
                         "注册路径的待入库账号取 0")

    def test_verify_account_clean_uses_real_signin_and_flattens_newlines(self):
        from web.services import accounts_data as ad
        clean = {"phone": PHONE, "password": "pw"}
        with mock.patch.object(ad.signin, "verify_account", return_value=(True, "")):
            self.assertIsNone(self.webapp._verify_account_clean(clean))
        with mock.patch.object(ad.signin, "verify_account",
                               return_value=(False, "登录失败\n账号或密码错误")):
            err = self.webapp._verify_account_clean(clean)
        self.assertIn("账号验证未通过：", err)
        self.assertNotIn("\n", err, "换行必须折平（日志/页面注入面）")
        with mock.patch.object(ad.signin, "verify_account", side_effect=OSError("boom")):
            err = self.webapp._verify_account_clean(clean)
        self.assertIn("账号验证异常：boom", err)

    def test_reclaim_and_queue_full_use_rejected_status_and_pending_cap(self):
        from web.services import accounts_data as ad
        # 告警通道是同一个 logger 对象（logging.getLogger("web")），故按方法打桩即可观测
        with mock.patch.object(self.webapp.logger, "warning") as warn, \
                mock.patch.object(self.webapp.db, "reclaim_stale_verify_jobs",
                                  return_value=[{"id": 1}, {"id": 2}]) as reclaim:
            self.assertEqual(self.webapp._reclaim_stale_verify_jobs(), 2)
        self.assertEqual(reclaim.call_args.kwargs["reject_status"],
                         ad.ACCOUNT_STATUS_REJECTED, "收口与账号侧 CAS 拒绝同一状态值")
        self.assertEqual(warn.call_count, 1, "有收口才留痕")
        with mock.patch.object(self.webapp.db, "reclaim_stale_verify_jobs", return_value=[]), \
                mock.patch.object(self.webapp.db, "count_active_verify_jobs",
                                  return_value=self.webapp.VERIFY_JOBS_MAX_PENDING):
            self.assertTrue(self.webapp._verify_queue_full(),
                            "达到待办上限即视为满（判定前先收口）")
        with mock.patch.object(self.webapp.db, "reclaim_stale_verify_jobs", return_value=[]), \
                mock.patch.object(self.webapp.db, "count_active_verify_jobs",
                                  return_value=self.webapp.VERIFY_JOBS_MAX_PENDING - 1):
            self.assertFalse(self.webapp._verify_queue_full())

    def test_reject_account_cas_semantics(self):
        with mock.patch.object(self.webapp.logger, "error") as err_log, \
                mock.patch.object(self.webapp.logger, "info") as info_log, \
                mock.patch.object(self.webapp.db, "update_account_status_if") as upd:
            self.webapp._reject_account(PHONE, "原因", None, "pending")
            self.assertEqual(upd.call_count, 0, "缺任务上下文时不得改账号状态")
            self.assertEqual(err_log.call_count, 1, "缺上下文必须留痕")
            self.webapp._reject_account(PHONE, "", 5, "pending")
            self.assertEqual(upd.call_args.args,
                             (5, "rejected", "pending", "在线校验未通过"),
                             "空原因落默认文案，且按 expect_status 做 CAS")
            upd.return_value = False
            self.webapp._reject_account(PHONE, "原因", 5, "pending")
            self.assertEqual(info_log.call_count, 1, "未写回（已人工变更）必须留痕")

    def test_start_verify_job_raises_busy_when_queue_full(self):
        from web.services import verify_queue as vq
        with mock.patch.object(vq.attempt_jobs, "start", return_value=None), \
                self.assertRaises(self.webapp.VerifyGateBusy):
            self.webapp._start_verify_job({}, "u", 1, {}, {})
        with mock.patch.object(vq.attempt_jobs, "start", return_value=("jid", None)):
            self.assertEqual(self.webapp._start_verify_job({}, "u", 1, {}, {}),
                             ("jid", None))

    def test_measure_state_read_write_and_cooldown(self):
        import json
        path = os.path.join(self.tmp, "state", "capacity-measure.json")
        self.assertEqual(self.webapp._read_measure_state(path), {}, "缺失按从未实测")
        # 先让写入路径建目录，再逐形态覆盖内容
        self.webapp._write_measure_state(path, {"at": ""})
        with io.open(path, "w", encoding="utf-8") as f:
            f.write("不是 JSON")
        self.assertEqual(self.webapp._read_measure_state(path), {})
        with io.open(path, "w", encoding="utf-8") as f:
            f.write("[1, 2]")
        self.assertEqual(self.webapp._read_measure_state(path), {}, "非 dict 按从未实测")
        self.webapp._write_measure_state(path, {"at": "2026-09-19 06:00:00", "seconds": 1.5,
                                                "sample": "138****8000"})
        self.assertEqual(self.webapp._read_measure_state(path),
                         {"at": "2026-09-19 06:00:00", "seconds": 1.5,
                          "sample": "138****8000"})
        with io.open(path, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["sample"], "138****8000")
        rem = self.webapp._measure_cooldown_remaining
        self.assertEqual(rem({"at": "2026-09-19 06:00:00"}, 0), 0, "冷却为 0 = 不限制")
        self.assertEqual(rem({}, 600), 0, "时刻读不出按可实测")
        self.assertEqual(rem({"at": "坏"}, 600), 0)
        now = datetime(2026, 9, 19, 6, 5, 0)
        with mock.patch.object(self.webapp.clock, "now", return_value=now):
            self.assertEqual(rem({"at": "2026-09-19 05:00:00"}, 600), 0, "已过冷却")
            self.assertEqual(rem({"at": "2026-09-19 06:00:00"}, 600), 300, "剩余整秒")
            self.assertEqual(rem({"at": "2026-09-19 05:55:00"}, 600), 0, "冷却恰好走完")
            self.assertEqual(rem({"at": "2026-09-19 06:04:30.0"}, 600), 0,
                             "旧格式解析失败按可实测")

    def test_pick_measure_account_filters(self):
        pick = self.webapp._pick_measure_account
        live = {"phone": PHONE, "status": "active", "deleted": False}
        pending = {"phone": OTHER, "status": "pending", "deleted": False}
        deleted = {"phone": "13700137000", "status": "active", "deleted": True}
        paused = {"phone": "13600136000", "status": "active", "deleted": False,
                  "user_paused": True}
        accounts = [pending, deleted, paused, live]
        self.assertIs(pick(accounts), live, "跳过未过审/已删/用户暂停，取第一个可用")
        self.assertIs(pick(accounts, PHONE), live)
        self.assertIsNone(pick(accounts, OTHER), "指定不可用号码返回 None")
        self.assertIsNone(pick([], None))

    # ------------------------------------------------------------------
    # 4/5. 别名加载安全与状态归属
    # ------------------------------------------------------------------
    def test_alias_loaded_app_shares_service_modules(self):
        from web.services import accounts_data as ad
        from web.services import measure as ms
        from web.services import verify_queue as vq
        self.assertIs(self.webapp._accounts_data, ad,
                      "别名加载的 app 副本必须复用同一个 web.services.accounts_data")
        self.assertIs(self.webapp._measure, ms)
        self.assertIs(self.webapp._verify_queue, vq)

    def test_service_modules_hold_no_app_state(self):
        import importlib
        for rel, names in APP_HELD_STATE_ACC.items():
            mod = importlib.import_module(
                rel.replace("web/", "web.").replace("/", ".")[:-3])
            for name in names:
                self.assertFalse(hasattr(mod, name),
                                 f"{rel} 不得持有 {name}（应由 web.app 调用时刻注入）")

    def test_service_sources_do_not_import_app(self):
        for rel in SERVICE_SOURCES_ACC:
            with io.open(os.path.join(BASE, rel), encoding="utf-8") as f:
                src = f.read()
            self.assertIsNone(re.search(r"^\s*(?:import|from)\s+web\.app\b", src, re.M),
                              f"{rel} 禁止 import web.app（别名加载会执行副本模块）")

    def test_importing_services_does_not_execute_web_app(self):
        """全新解释器里只 import 服务层：不得把 web.app 拉进 sys.modules。"""
        code = (
            "import sys;"
            f"sys.path[:0]=[{os.path.join(BASE, 'scripts')!r}, {BASE!r}];"
            "import web.services.accounts_data, web.services.verify_queue,"
            " web.services.measure;"
            "print('APP_LOADED' if 'web.app' in sys.modules else 'SERVICES_ONLY_OK')"
        )
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        r = subprocess.run([sys.executable, "-c", code], cwd=BASE, env=env,
                           capture_output=True, text=True, timeout=120)
        self.assertIn("SERVICES_ONLY_OK", r.stdout, r.stderr[-600:])
        self.assertEqual(r.returncode, 0, r.stderr[-600:])

    def test_estimate_slot_normal_branch_is_deterministic(self):
        """顺序 × 正态：锚点 z 由手机号固定 → 同一账号每天同一中心（与文档公式一致）。"""
        accounts = [{"phone": PHONE, "status": "active", "deleted": False}]
        with mock.patch.object(self.webapp, "read_env",
                               return_value={"YIBAN_SIGN_DIST": "normal"}), \
                mock.patch.object(self.webapp, "_sign_window",
                                  return_value=((6, 30), (7, 50))), \
                mock.patch.object(self.webapp, "edge_config", return_value=(0, 0)), \
                mock.patch.object(self.webapp, "load_accounts", return_value=accounts):
            got = self.webapp._estimate_slot(PHONE)
        z = random.Random(str(PHONE)).gauss(0, 1)
        center = max(390.0, min(470.0, 390 + 80 * 0.5 + 80 * 0.20 * z))
        self.assertEqual(got, (f"约 {int(center) // 60:02d}:{int(center) % 60:02d}",
                               "（每日波动约 ±10 分钟）"))
        with mock.patch.object(self.webapp, "read_env",
                               return_value={"YIBAN_SIGN_ORDER": "random"}), \
                mock.patch.object(self.webapp, "load_accounts", return_value=accounts):
            self.assertEqual(self.webapp._estimate_slot(PHONE),
                             (None, "随机模式每日重排，签到时间当天 06:31 后可见"))


MOVED_ENV_IO = (
    "read_env",
    "load_env_int",
    "write_env_int",
    "write_env_key",
    "write_env_batch",
    "ensure_secret_key",
    "_env_write_lock",
    "_settings_label",
    "_settings_value_text",
    "_settings_effective_values",
    "_is_http_proxy_url",
    "_report_env_key_collisions",
    "_parse_announcement_meta",
    # 随解析器搬迁的格式常量（公告元数据形态的唯一真源）
    "ANNOUNCEMENT_DRAFT_META_SEP",
    "ANNOUNCEMENT_DRAFT_META_FMT",
    # 只被迁出的两个展示函数使用，随所属域搬
    "_SETTINGS_KEY_LABELS",
    "_BOOL_SETTINGS_KEYS",
)


MOVED_EXECUTOR_ENV = (
    "_executor_rows",
    "_mutate_executor_rows",
    "_next_executor_slot",
    "_save_row_egress",
    "_save_fallback_egress",
    "_save_slot_egress",
    "_validated_proxy_value",
    "_validated_name",
    "_executors_window",
    "_last_executors",
    "_executor_row_payload",
    "_executor_activity",
)


PURE_REEXPORTS_ENV_IO = (
    "read_env",
    "load_env_int",
    "_env_write_lock",
    "_settings_label",
    "_settings_value_text",
    "_is_http_proxy_url",
    "_parse_announcement_meta",
    "_SETTINGS_KEY_LABELS",
    "_BOOL_SETTINGS_KEYS",
    "ANNOUNCEMENT_DRAFT_META_SEP",
    "ANNOUNCEMENT_DRAFT_META_FMT",
)


PURE_REEXPORTS_EXECUTOR_ENV = (
    "_next_executor_slot",
    "_validated_proxy_value",
    "_validated_name",
    "_last_executors",
    "_executor_row_payload",
    "_executor_activity",
)


FORWARDED_ENV_IO = (
    "write_env_batch",
    "write_env_key",
    "write_env_int",
    "ensure_secret_key",
    "_settings_effective_values",
    "_report_env_key_collisions",
)


FORWARDED_EXECUTOR_ENV = (
    "_executor_rows",
    "_mutate_executor_rows",
    "_save_row_egress",
    "_save_fallback_egress",
    "_save_slot_egress",
    "_executors_window",
)


APP_HELD_STATE_ENV = (
    "ENV_FILE",
    "_REPO_ROOT",
    "_doc_cache",
    "_atomic_write",
    "_env_flag",
    "_sign_window",
    "send_notification",
    "_file_lock",
    "_rate_lock",
)


SERVICE_SOURCES_ENV = ("web/services/env_io.py", "web/services/executor_env.py",
                   "web/services/locks.py")


class WebServicesEnvSplitContractTest(unittest.TestCase):
    """以别名加载的 app 副本为靶子（routes 与测试实际看到的那一份）。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-env-split-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        cls._old_env = {k: os.environ.get(k) for k in
                        ("YIBAN_ENV_FILE", "YIBAN_DB_FILE", "YIBAN_STATE_DIR",
                         "YIBAN_ACCOUNTS_FILE", "YIBAN_ACCOUNTS_KEY")}
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_DB_FILE"] = os.path.join(cls.tmp, "yiban.db")
        os.environ["YIBAN_STATE_DIR"] = cls.tmp
        os.environ["YIBAN_ACCOUNTS_FILE"] = os.path.join(cls.tmp, "accounts.json")
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
        cls._write_raw("")
        spec = importlib.util.spec_from_file_location(
            "webapp_envsplit", os.path.join(BASE, "web", "app.py"))
        cls.webapp = importlib.util.module_from_spec(spec)
        sys.modules["webapp_envsplit"] = cls.webapp
        with contextlib.suppress(Exception):
            spec.loader.exec_module(cls.webapp)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k, v in cls._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    @classmethod
    def _write_raw(cls, text):
        with io.open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n" + text)

    def setUp(self):
        self._write_raw("")
        # 歧义键闩是模块级：逐例复位，避免上一例的报告把本例的调用吞掉
        self.webapp._env_collision_reported = False

    # ------------------------------------------------------------------
    # 1. 名字面
    # ------------------------------------------------------------------
    def test_every_moved_name_reachable_on_app_and_home_module(self):
        from web.services import env_io as env_mod
        from web.services import executor_env as exec_mod
        for name in MOVED_ENV_IO:
            self.assertTrue(hasattr(self.webapp, name), f"web.app 兼容面缺失 {name}")
            self.assertTrue(hasattr(env_mod, name), f"web/services/env_io.py 缺 {name}")
        for name in MOVED_EXECUTOR_ENV:
            self.assertTrue(hasattr(self.webapp, name), f"web.app 兼容面缺失 {name}")
            self.assertTrue(hasattr(exec_mod, name), f"web/services/executor_env.py 缺 {name}")
        self.assertTrue(hasattr(self.webapp, "_file_lock"), "web.app._file_lock 必须可达")

    def test_pure_reexports_are_same_object(self):
        from web.services import env_io as env_mod
        from web.services import executor_env as exec_mod
        for name in PURE_REEXPORTS_ENV_IO:
            self.assertIs(getattr(self.webapp, name), getattr(env_mod, name),
                          f"{name} 应为同一个对象（web.app 只是再导出）")
        for name in PURE_REEXPORTS_EXECUTOR_ENV:
            self.assertIs(getattr(self.webapp, name), getattr(exec_mod, name),
                          f"{name} 应为同一个对象（web.app 只是再导出）")
        self.assertIs(self.webapp._next_executor_slot, exec_mod._next_executor_slot)
        self.assertEqual(self.webapp.ANNOUNCEMENT_DRAFT_META_SEP, "|")
        self.assertEqual(self.webapp.ANNOUNCEMENT_DRAFT_META_FMT, "%Y-%m-%d %H:%M:%S")

    def test_forwarders_actually_forward(self):
        """注入型名字不得退化成纯再导出（否则 app 侧打桩面静默失效）。"""
        from web.services import env_io as env_mod
        from web.services import executor_env as exec_mod
        for name in FORWARDED_ENV_IO:
            self.assertIsNot(getattr(self.webapp, name), getattr(env_mod, name),
                             f"{name} 必须是转发包装（注入 app 侧现取值）")
        for name in FORWARDED_EXECUTOR_ENV:
            self.assertIsNot(getattr(self.webapp, name), getattr(exec_mod, name),
                             f"{name} 必须是转发包装（注入 write_env_batch / ENV_FILE）")

    def test_forwarders_keep_original_call_arity(self):
        """routes / 既有测试按原实参个数调用，转发不得改签名。"""
        self.webapp.write_env_batch(self.env_file, {})
        self.webapp.write_env_key(self.env_file, "YIBAN_X", "")
        self.webapp.write_env_int(self.env_file, "YIBAN_Y", 0)
        self.assertIsInstance(self.webapp.ensure_secret_key(self.env_file), str)
        self.assertIsInstance(self.webapp._executor_rows(), list)
        self.assertIsInstance(self.webapp._mutate_executor_rows(lambda rows: (rows, "ok")), str)
        self.assertEqual(self.webapp._save_slot_egress(self.env_file, "YIBAN_PROXY", None, ""),
                         (None, None))
        self.assertIsInstance(self.webapp._settings_effective_values(self.env_file), dict)
        self.assertIsNone(self.webapp._report_env_key_collisions(self.env_file))
        self.assertIsInstance(self.webapp._executors_window().front_sec, int)
        self.assertIsInstance(self.webapp._next_executor_slot([]), int)

    # ------------------------------------------------------------------
    # 2. 锁身份单一
    # ------------------------------------------------------------------
    def test_file_lock_single_identity_and_reentrant(self):
        from web.services import env_io as env_mod
        from web.services import executor_env as exec_mod
        from web.services import locks as locks_mod
        self.assertIs(self.webapp._file_lock, locks_mod._file_lock,
                      "web.app._file_lock 必须就是真源那把锁")
        self.assertFalse(hasattr(env_mod, "_file_lock"), "env_io 不得再建一把进程内锁")
        self.assertFalse(hasattr(exec_mod, "_file_lock"), "executor_env 不得再建一把进程内锁")
        self.assertIsInstance(self.webapp._file_lock, type(threading.RLock()))
        with self.webapp._file_lock:  # noqa: SIM117 - RLock 同线程可重入，嵌套正是重入场景
            with self.webapp._file_lock:
                pass
        self.assertIsNot(self.webapp._rate_lock, self.webapp._file_lock,
                         "_rate_lock 是限速表专用锁，不得与 _file_lock 合并")

    # ------------------------------------------------------------------
    # 2'. 转发注入 app 模块级状态（代表性打桩往返）
    # ------------------------------------------------------------------
    def test_atomic_write_stub_sees_batch_and_single_key_paths(self):
        """`web.app._atomic_write` 是"每一次 .env 落盘"的观测点：两条写路径都要经过它。"""
        real = self.webapp._atomic_write
        seen = []

        def spy(path, text, **kw):
            seen.append(text)
            return real(path, text, **kw)

        with mock.patch.object(self.webapp, "_atomic_write", side_effect=spy):
            self.webapp.write_env_batch(self.env_file, {"YIBAN_B": "2"})
            self.webapp.write_env_key(self.env_file, "YIBAN_C", "3")
        self.assertEqual(len(seen), 2, f"两次写入都应落在 _atomic_write 上，实际 {seen}")
        self.assertIn("YIBAN_B=2", seen[0])
        self.assertIn("YIBAN_C=3", seen[1])

    def test_write_env_batch_stub_sees_single_key_and_executor_paths(self):
        """`web.app.write_env_batch` 打桩点对单键写与执行体写回同样生效。"""
        from yiban import egress
        rows = egress.add_row([], egress.TYPE_WORKER, "http://a:1")
        self._write_raw(f"{egress.ENV_MANIFEST}={egress.dump_manifest(rows)}\n")
        real = self.webapp.write_env_batch
        calls = []

        def spy(env_path, updates):
            calls.append(dict(updates))
            return real(env_path, updates)

        with mock.patch.object(self.webapp, "write_env_batch", side_effect=spy):
            self.webapp.write_env_key(self.env_file, "YIBAN_D", "4")
            err = self.webapp._save_row_egress(self.env_file, rows[0]["slot"], "http://b:2")
        self.assertEqual(err, (None, None))
        self.assertEqual([sorted(c) for c in calls],
                         [["YIBAN_D"], [egress.ENV_MANIFEST]])
        self.assertEqual(self.webapp.read_env(self.env_file)["YIBAN_D"], "4")

    def test_executors_window_reads_app_env_file_and_sign_window(self):
        """`_executors_window` 现取 web.app 的 ENV_FILE 与 `_sign_window`（--config 同款）。"""
        self._write_raw("YIBAN_WINDOW_EDGE_FRONT_SEC=90\nYIBAN_WINDOW_EDGE_BACK_SEC=30\n")
        with mock.patch.object(self.webapp, "ENV_FILE", self.env_file):
            win = self.webapp._executors_window()
        self.assertEqual((win.front_sec, win.back_sec), (90, 30))
        with mock.patch.object(self.webapp, "ENV_FILE", self.env_file), \
                mock.patch.object(self.webapp, "_sign_window", return_value=((8, 0), (9, 0))):
            win = self.webapp._executors_window()
        self.assertEqual((win.start_min, win.end_min), (480, 540))

    def test_executors_window_defaults_match_engine_window(self):
        """`.env` 未写裁剪键时执行体页与引擎同窗：缺省 60s（原实现缺省 0 → 宽 1 分钟）。

        示例 `.env` 把两个裁剪键注释掉，故这条缺省路径就是生产默认形态：缺省 0 会让
        页面上显示的窗口、容量换算的分母与"窗口内不做实测"的拦截都比引擎宽 1 分钟。
        """
        import yiban.window as yb_window
        self._write_raw("YIBAN_SIGN_START=06:30\nYIBAN_SIGN_END=07:50\n")
        with mock.patch.object(self.webapp, "ENV_FILE", self.env_file):
            win = self.webapp._executors_window()
        engine = yb_window.bounds({"sign_start": (6, 30), "sign_end": (7, 50),
                                   "edge_front_sec": 60, "edge_back_sec": 60})
        self.assertEqual((win.front_sec, win.back_sec), (60, 60))
        self.assertEqual((win.lo_min, win.hi_min), (engine.lo_min, engine.hi_min))
        self.assertEqual(win.full_sec(), 78 * 60)

    def test_settings_effective_values_uses_app_side_flag_and_defaults(self):
        """`_env_flag` 与三个容量缺省值都在调用时刻从 web.app 现取。"""
        self._write_raw("")
        cur = self.webapp._settings_effective_values(self.env_file)
        self.assertEqual(cur["max_users"], str(self.webapp.DEFAULT_MAX_USERS))
        self.assertEqual(cur["gap_max"], str(self.webapp.DEFAULT_ACCOUNT_GAP_MAX))
        self.assertEqual(cur["sunday_sign"], "0")
        with mock.patch.object(self.webapp, "DEFAULT_MAX_USERS", 777), \
                mock.patch.object(self.webapp, "_env_flag",
                                  side_effect=lambda v: str(v).strip().lower() == "yes"):
            self._write_raw("YIBAN_SUNDAY_SIGN=yes\n")
            cur = self.webapp._settings_effective_values(self.env_file)
        self.assertEqual(cur["max_users"], "777", "容量缺省值必须现取注入")
        self.assertEqual(cur["sunday_sign"], "1", "开关解析器必须现取注入")

    def test_collision_report_latch_and_alert_through_app_face(self):
        """`_env_collision_reported` 可直接改写、`send_notification` 可打桩（启动路径两块面）。"""
        self._write_raw("YIBAN_AUDIT_KEY=abc\nYIBAN_AUDIT_KEY = def\n")
        self.webapp._env_collision_reported = False
        with self.assertLogs("web", level="ERROR") as logs, \
                mock.patch.object(self.webapp, "send_notification") as sn:
            self.webapp._report_env_key_collisions(self.env_file)
        self.assertTrue(any("YIBAN_AUDIT_KEY" in m for m in logs.output),
                        f"ERROR 日志应点名歧义键: {logs.output}")
        self.assertTrue(sn.called, "歧义键必须发告警")
        self.assertTrue(sn.call_args.kwargs.get("urgent"), f"告警须为紧急: {sn.call_args}")
        # 闩置位后同一进程不再重复告警
        sn.reset_mock()
        self.webapp._report_env_key_collisions(self.env_file)
        sn.assert_not_called()
        # 干净配置不发告警
        self._write_raw("YIBAN_A=1\n")
        self.webapp._env_collision_reported = False
        with mock.patch.object(self.webapp, "send_notification") as sn2:
            self.webapp._report_env_key_collisions(self.env_file)
        sn2.assert_not_called()

    # ------------------------------------------------------------------
    # 3. 行为往返（写入语义逐字不变）
    # ------------------------------------------------------------------
    def test_env_write_round_trip_keeps_comments_and_other_lines(self):
        self._write_raw("# 手工注释\nYIBAN_A = 1\nYIBAN_B=2\n")
        self.webapp.write_env_batch(self.env_file, {"YIBAN_A": "9", "YIBAN_C": "3"})
        with io.open(self.env_file, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("# 手工注释", text, "注释行必须保留")
        self.assertIn("YIBAN_A=9", text, "带空格的旧行折叠后落为新值")
        self.assertIn("YIBAN_B=2", text, "未更新的键逐字保留")
        self.assertIn("YIBAN_C=3", text)
        self.assertEqual(self.webapp.read_env(self.env_file)["YIBAN_A"], "9")
        # 空值 = 删键
        self.webapp.write_env_batch(self.env_file, {"YIBAN_B": ""})
        self.assertNotIn("YIBAN_B", self.webapp.read_env(self.env_file))
        # 注入校验：值与键名两处都不放行行分隔符
        with self.assertRaises(ValueError):
            self.webapp.write_env_batch(self.env_file, {"YIBAN_D": "x\ny"})
        with self.assertRaises(ValueError):
            self.webapp.write_env_batch(self.env_file, {"yiban_d": "x"})

    def test_write_env_int_and_load_env_int_semantics(self):
        self.webapp.write_env_int(self.env_file, "YIBAN_NUM", 5)
        self.assertEqual(self.webapp.load_env_int(self.env_file, "YIBAN_NUM", 0), 5)
        self.webapp.write_env_int(self.env_file, "YIBAN_NUM", 0)
        self.assertNotIn("YIBAN_NUM", self.webapp.read_env(self.env_file),
                         "value<=0 语义是删除该行")
        self.assertEqual(self.webapp.load_env_int(self.env_file, "YIBAN_NUM", 42), 42)
        self.webapp.write_env_key(self.env_file, "YIBAN_BAD", "not-a-number")
        self.assertEqual(self.webapp.load_env_int(self.env_file, "YIBAN_BAD", 7), 7)

    def test_ensure_secret_key_generates_once_and_keeps_existing(self):
        os.remove(self.env_file)      # 全新部署（文件不存在）才写「默认暂停注册」
        key = self.webapp.ensure_secret_key(self.env_file)
        self.assertEqual(len(key), 64)
        self.assertEqual(self.webapp.read_env(self.env_file)["YIBAN_SECRET_KEY"], key)
        self.assertEqual(self.webapp.read_env(self.env_file)["YIBAN_REGISTRATION_PAUSE"], "1",
                         "全新部署默认暂停注册")
        self.assertEqual(self.webapp.ensure_secret_key(self.env_file), key, "已有密钥不得换发")

    def test_ensure_secret_key_degrades_when_env_unreadable(self):
        """.env 存在但不可读时降级返回进程内密钥并告警，不得把启动炸掉。

        用一个目录当 env 路径制造 OSError（跨平台）。修复前该 open 在 try 之外，
        read_env 吞掉 OSError 后紧接着的 open 直接把启动打挂。"""
        bad = os.path.join(self.tmp, "unreadable.env")
        os.makedirs(bad, exist_ok=True)
        with self.assertLogs("web", level="WARNING") as logs:
            key = self.webapp.ensure_secret_key(bad)
        self.assertEqual(len(key), 64)
        self.assertTrue(any("YIBAN_SECRET_KEY" in ln for ln in logs.output), logs.output)

    def test_executor_rows_migrates_legacy_keys_once_and_keeps_them(self):
        from yiban import egress
        self._write_raw("YIBAN_WORKERS=2\n"
                        "YIBAN_PROXY_LIST=http://a:1,http://b:2\n"
                        "YIBAN_PROXY_FALLBACK=http://f:1\n")
        rows = self.webapp._executor_rows()
        self.assertEqual([r["proxy"] for r in egress.worker_rows(rows)],
                         ["http://a:1", "http://b:2"])
        env = self.webapp.read_env(self.env_file)
        self.assertIn(egress.ENV_MANIFEST, env, "旧三键必须迁移写回清单键")
        self.assertEqual(env["YIBAN_WORKERS"], "2", "旧键保留，供回退读取")
        # 迁移只发生一次：清单已在，第二次读不再落盘
        real = self.webapp._atomic_write
        with mock.patch.object(self.webapp, "_atomic_write", side_effect=real) as spy:
            self.webapp._executor_rows()
        spy.assert_not_called()

    def test_executor_manifest_write_round_trip_preserves_other_lines(self):
        from yiban import egress
        rows = egress.add_row([], egress.TYPE_WORKER, "http://a:1", name="机一")
        self._write_raw(f"# 保留我\n{egress.ENV_MANIFEST}={egress.dump_manifest(rows)}\n")
        # 追加兜底行（清单里没有则新增）
        self.assertEqual(self.webapp._save_fallback_egress(self.env_file, "http://fb:9"),
                         (None, None))
        # 单行出口改写：其余行逐字保留
        self.assertEqual(
            self.webapp._save_row_egress(self.env_file, rows[0]["slot"], "http://b:2"),
            (None, None))
        with io.open(self.env_file, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("# 保留我", text)
        back = egress.manifest_state(self.webapp.read_env(self.env_file))[0]
        self.assertEqual(egress.fallback_row(back)["proxy"], "http://fb:9")
        worker = egress.row_by_slot(back, rows[0]["slot"])
        self.assertEqual((worker["proxy"], worker["name"]), ("http://b:2", "机一"),
                         "只改出口，自定义名与其余字段逐字保留")

    def test_save_slot_egress_replaces_one_segment_only(self):
        self._write_raw("YIBAN_PROXY_LIST=http://a:1,http://b:2,http://c:3\n")
        self.assertEqual(
            self.webapp._save_slot_egress(self.env_file, "YIBAN_PROXY_LIST", 1, "http://d:4"),
            (None, None))
        self.assertEqual(
            self.webapp.read_env(self.env_file)["YIBAN_PROXY_LIST"],
            "http://a:1,http://d:4,http://c:3", "其余段必须逐字保留")
        err, code = self.webapp._save_slot_egress(
            self.env_file, "YIBAN_PROXY_LIST", 1, "不是代理\n")
        self.assertEqual((err, code), ("代理配置不能包含换行", 400))
        err, code = self.webapp._save_slot_egress(
            self.env_file, "YIBAN_PROXY_LIST", 1, "http://u:p@h:1/x 备注")
        self.assertEqual(code, 400)
        self.assertIn("代理地址格式不正确", err)

    def test_validated_proxy_and_name_rules(self):
        for fn in (self.webapp._validated_proxy_value,):
            self.assertEqual(fn(None), ("", None))
            self.assertEqual(fn(""), ("", None))
            self.assertEqual(fn("  http://a:1  "), ("http://a:1", None))
            self.assertEqual(fn("http://u:p@h:1"), ("http://u:p@h:1", None))
            self.assertEqual(fn("http://a:1\n"), (None, "代理配置不能包含换行"))
            err = fn("备注文字")[1]
            self.assertIn("代理地址格式不正确", err)
        name = self.webapp._validated_name("  我的出口  ")
        self.assertEqual(name, ("我的出口", None))
        self.assertEqual(self.webapp._validated_name(None), ("", None))
        self.assertEqual(self.webapp._validated_name("a\nb")[1], "名称不能包含换行")
        self.assertIn("名称最长", self.webapp._validated_name("x" * 999)[1])
        from yiban import egress
        kept = self.webapp._validated_name("ok\x00name")[0]
        self.assertTrue(all(ch.isprintable() for ch in kept), "不可打印字符必须被滤掉")
        self.assertLessEqual(len(kept), egress.NAME_MAX_LEN)

    def test_settings_label_and_value_text(self):
        self.assertEqual(self.webapp._settings_label("sign_window"), "签到窗口")
        self.assertEqual(self.webapp._settings_label("no_such_key"), "no_such_key",
                         "未列入标签表的键按键名原样回")
        self.assertEqual(self.webapp._settings_value_text("sunday_sign", "1"), "开")
        self.assertEqual(self.webapp._settings_value_text("sunday_sign", "0"), "关")
        self.assertEqual(self.webapp._settings_value_text("max_users", 500), "500")

    def test_parse_announcement_meta_both_faces(self):
        from web.services import env_io as env_mod
        good = "someone@example.invalid|2001-02-03 04:05:06"
        self.assertEqual(self.webapp._parse_announcement_meta(good),
                         ("someone@example.invalid", "2001-02-03 04:05:06"))
        self.assertEqual(env_mod._parse_announcement_meta(good),
                         self.webapp._parse_announcement_meta(good))
        for bad in (None, "", "x", "a|b|c", "|2001-02-03 04:05:06", "a|2001-02-03"):
            self.assertEqual(self.webapp._parse_announcement_meta(bad), ("", ""),
                             f"元数据不可用时必须回空而非抛错: {bad!r}")

    # ------------------------------------------------------------------
    # 4/5. 别名加载安全与状态归属
    # ------------------------------------------------------------------
    def test_alias_loaded_app_shares_service_modules(self):
        from web.services import env_io as env_mod
        from web.services import executor_env as exec_mod
        from web.services import locks as locks_mod
        self.assertIs(self.webapp._env_io_svc, env_mod,
                      "别名加载的 app 副本必须复用同一个 web.services.env_io")
        self.assertIs(self.webapp._executor_env, exec_mod,
                      "别名加载的 app 副本必须复用同一个 web.services.executor_env")
        self.assertIs(self.webapp._file_lock, locks_mod._file_lock)
        self.assertIs(self.webapp._rate_lock, locks_mod._rate_lock,
                      "限速/失败计数表与 web.app（routes 经 m.*）必须共用同一把锁")

    def test_service_modules_hold_no_app_state(self):
        from web.services import env_io as env_mod
        from web.services import executor_env as exec_mod
        from web.services import locks as locks_mod
        # locks 本来就是进程内锁的真源（_file_lock / _rate_lock，唯一例外），
        # 其余 app 状态一概不得持有
        for mod, exempt in ((env_mod, frozenset()), (exec_mod, frozenset()),
                            (locks_mod, frozenset({"_file_lock", "_rate_lock"}))):
            for name in APP_HELD_STATE_ENV:
                if name in exempt:
                    continue
                self.assertFalse(hasattr(mod, name),
                                 f"{mod.__name__} 不得持有 {name}（应由 web.app 调用时刻注入）")

    def test_service_sources_do_not_import_app(self):
        for rel in SERVICE_SOURCES_ENV:
            with io.open(os.path.join(BASE, rel), encoding="utf-8") as f:
                src = f.read()
            self.assertIsNone(re.search(r"^\s*(?:import|from)\s+web\.app\b", src, re.M),
                              f"{rel} 禁止 import web.app（别名加载会执行副本模块）")

    def test_importing_services_does_not_execute_web_app(self):
        """全新解释器里只 import 服务层：不得把 web.app 拉进 sys.modules。"""
        code = (
            "import sys;"
            f"sys.path[:0]=[{os.path.join(BASE, 'scripts')!r}, {BASE!r}];"
            "import web.services.env_io, web.services.executor_env, web.services.locks;"
            "print('APP_LOADED' if 'web.app' in sys.modules else 'SERVICES_ONLY_OK')"
        )
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        r = subprocess.run([sys.executable, "-c", code], cwd=BASE, env=env,
                           capture_output=True, text=True, timeout=60)
        self.assertIn("SERVICES_ONLY_OK", r.stdout, r.stderr[-600:])
        self.assertEqual(r.returncode, 0, r.stderr[-600:])


MOVED_LOGS = (
    "_log_line_visible",
    "_cred_paused_phones",
    "clear_fuse_pause",
    "clear_fuse_on_cred_change",
    "load_sign_state",
    "_tail_lines",
    "parse_sign_log",
    "_is_valid_date_str",
    "log_path_for",
    "_log_lines_for",
    "_today_has_logs",
    "_most_recent_log_date",
    "_mask_log_phones",
    "_LOG_TAIL_BYTES",
    # 只被迁出族使用，随所属域搬（行正则与最近日期缓存）
    "SIGN_LOG_RE",
    "_most_recent_log_cache",
)


MOVED_SIGNSTATUS = (
    "_sign_window",
    "_env_flag",
    "_in_sign_window",
    "_in_run_period",
    "_day_off_reason",
    "sign_status",
    "check_connectivity",
    # 只被迁出的开关解析器使用，随所属域搬
    "_TRUTHY_LITERALS",
)


PURE_REEXPORTS_LOGS = (
    "_log_line_visible",
    "_cred_paused_phones",
    "clear_fuse_pause",
    "clear_fuse_on_cred_change",
    "_tail_lines",
    "_is_valid_date_str",
    "_mask_log_phones",
    "_LOG_TAIL_BYTES",
    "SIGN_LOG_RE",
    "_most_recent_log_cache",
)


PURE_REEXPORTS_SIGNSTATUS = (
    "_env_flag",
    "_in_sign_window",
    "_day_off_reason",
    "check_connectivity",
    "_TRUTHY_LITERALS",
)


FORWARDED_LOGS = (
    "parse_sign_log",
    "log_path_for",
    "_log_lines_for",
    "_today_has_logs",
    "_most_recent_log_date",
    "load_sign_state",
)


FORWARDED_SIGNSTATUS = (
    "_sign_window",
    "_in_run_period",
    "sign_status",
)


APP_HELD_STATE_LOGS = (
    "ENV_FILE",
    "LOG_FILE",
    "STATE_DIR",
    "_REPO_ROOT",
    "_doc_cache",
    "_atomic_write",
    "_file_lock",
    "_rate_lock",
    "send_notification",
)


SERVICE_SOURCES_LOGS = ("web/services/logs.py", "web/services/signstatus.py")


class WebServicesLogsSplitContractTest(unittest.TestCase):
    """以别名加载的 app 副本为靶子（routes 与测试实际看到的那一份）。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-logs-split-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        cls.log_dir = os.path.join(cls.tmp, "logs")
        os.makedirs(cls.log_dir, exist_ok=True)
        cls._old_env = {k: os.environ.get(k) for k in
                        ("YIBAN_ENV_FILE", "YIBAN_DB_FILE", "YIBAN_STATE_DIR",
                         "YIBAN_ACCOUNTS_FILE", "YIBAN_ACCOUNTS_KEY", "YIBAN_LOG_FILE")}
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_DB_FILE"] = os.path.join(cls.tmp, "yiban.db")
        os.environ["YIBAN_STATE_DIR"] = cls.tmp
        os.environ["YIBAN_ACCOUNTS_FILE"] = os.path.join(cls.tmp, "accounts.json")
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
        os.environ["YIBAN_LOG_FILE"] = os.path.join(cls.log_dir, "sign.log")
        cls._write_raw("")
        spec = importlib.util.spec_from_file_location(
            "webapp_logssplit", os.path.join(BASE, "web", "app.py"))
        cls.webapp = importlib.util.module_from_spec(spec)
        sys.modules["webapp_logssplit"] = cls.webapp
        with contextlib.suppress(Exception):
            spec.loader.exec_module(cls.webapp)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k, v in cls._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    @classmethod
    def _write_raw(cls, text):
        with io.open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n" + text)

    def setUp(self):
        self._write_raw("")
        self.webapp.ENV_FILE = self.env_file
        self.webapp.LOG_FILE = os.path.join(self.log_dir, "sign.log")
        self.webapp.STATE_DIR = self.tmp
        self.webapp._most_recent_log_cache.update({"history_date": None, "checked_day": ""})
        for name in os.listdir(self.log_dir):
            os.remove(os.path.join(self.log_dir, name))

    # ------------------------------------------------------------------
    # 1. 名字面
    # ------------------------------------------------------------------
    def test_every_moved_name_reachable_on_app_and_home_module(self):
        from web.services import logs as logs_mod
        from web.services import signstatus as ss_mod
        for name in MOVED_LOGS:
            self.assertTrue(hasattr(self.webapp, name), f"web.app 兼容面缺失 {name}")
            self.assertTrue(hasattr(logs_mod, name), f"web/services/logs.py 缺 {name}")
        for name in MOVED_SIGNSTATUS:
            self.assertTrue(hasattr(self.webapp, name), f"web.app 兼容面缺失 {name}")
            self.assertTrue(hasattr(ss_mod, name), f"web/services/signstatus.py 缺 {name}")

    def test_pure_reexports_are_same_object(self):
        from web.services import logs as logs_mod
        from web.services import signstatus as ss_mod
        for name in PURE_REEXPORTS_LOGS:
            self.assertIs(getattr(self.webapp, name), getattr(logs_mod, name),
                          f"{name} 应为同一个对象（web.app 只是再导出）")
        for name in PURE_REEXPORTS_SIGNSTATUS:
            self.assertIs(getattr(self.webapp, name), getattr(ss_mod, name),
                          f"{name} 应为同一个对象（web.app 只是再导出）")
        self.assertEqual(tuple(self.webapp._TRUTHY_LITERALS), ("1", "true", "on", "yes"))

    def test_forwarders_actually_forward(self):
        """注入型名字不得退化成纯再导出（否则 app 侧打桩面静默失效）。"""
        from web.services import logs as logs_mod
        from web.services import signstatus as ss_mod
        for name in FORWARDED_LOGS:
            self.assertIsNot(getattr(self.webapp, name), getattr(logs_mod, name),
                             f"{name} 必须是转发包装（注入 LOG_FILE / STATE_DIR / _tail_lines）")
        for name in FORWARDED_SIGNSTATUS:
            self.assertIsNot(getattr(self.webapp, name), getattr(ss_mod, name),
                             f"{name} 必须是转发包装（注入 ENV_FILE / _sign_window）")

    def test_forwarders_keep_original_call_arity(self):
        """routes / 既有测试按原实参个数调用，转发不得改签名。"""
        import yiban.window as yb_window
        bounds = yb_window.bounds({"sign_start": (6, 30), "sign_end": (7, 50),
                                   "edge_front_sec": 0, "edge_back_sec": 0})
        self.assertEqual(self.webapp.parse_sign_log("nope.log"), [])
        self.assertTrue(self.webapp.log_path_for().endswith(".log"))
        self.assertEqual(self.webapp._log_lines_for("2020-01-01"), [])
        self.assertIsInstance(self.webapp._today_has_logs(), bool)
        self.assertIsInstance(self.webapp._most_recent_log_date(), str)
        self.assertIsInstance(self.webapp._most_recent_log_date(3), str)
        self.assertIsInstance(self.webapp.load_sign_state(), dict)
        self.assertEqual(len(self.webapp._sign_window()), 2)
        self.assertIsInstance(self.webapp._in_run_period(bounds), bool)
        self.assertEqual(len(self.webapp.sign_status()), 2)

    # ------------------------------------------------------------------
    # 2'. 转发注入 app 模块级状态（代表性打桩往返）
    # ------------------------------------------------------------------
    def test_log_file_assignment_reaches_every_log_reader(self):
        """`web.app.LOG_FILE` 直接赋值（既有测试写法）必须换掉整条日志读路径的目录。"""
        today = datetime.now().strftime("%Y-%m-%d")
        fixture = (f"[{today} 06:40:04] [INFO] yiban: 签到开始\n"
                   f"[{today} 06:40:05] [INFO] yiban.client: [13800138000] 生成定位\n")
        with io.open(os.path.join(self.log_dir, f"sign-{today}.log"), "w",
                     encoding="utf-8") as f:
            f.write(fixture)
        lines = self.webapp.parse_sign_log(self.webapp.log_path_for())
        self.assertEqual(len(lines), 2, "转发必须现取 LOG_FILE，而不是服务层自持的绑定")
        self.assertEqual(len(self.webapp._log_lines_for(today)), 2)
        self.assertTrue(self.webapp._today_has_logs())
        self.assertEqual(self.webapp._most_recent_log_date(3), today)

    def test_tail_lines_stub_reaches_parse_and_lines_for(self):
        """`web.app._tail_lines` 是既有打桩点：替换倒读输入后解析结果随之变化。"""
        today = datetime.now().strftime("%Y-%m-%d")
        injected = [f"[{today} 06:00:00] [INFO] yiban: 注入行",
                    "[2026-01-02 06:00:00] [WARNING] mailer: 注入告警",
                    "[2026-01-02 06:00:00] [INFO] notify: 不该出现"]
        with mock.patch.object(self.webapp, "_tail_lines", return_value=injected):
            got = self.webapp.parse_sign_log("whatever")
            self.assertEqual(got, [injected[0], injected[1]])
            self.assertEqual(self.webapp._log_lines_for(today), [injected[0]],
                             "当日读取仍按行首日期过滤（跨天行不混入）")

    def test_state_dir_assignment_reaches_load_sign_state(self):
        other = os.path.join(self.tmp, "other-state")
        os.makedirs(other, exist_ok=True)
        with io.open(os.path.join(other, "sign-state-2026-09-19.json"), "w",
                     encoding="utf-8") as f:
            f.write('{"13800138000": {"status": "success", "message": "", "task": "default"}}')
        with mock.patch.object(self.webapp, "STATE_DIR", other):
            got = self.webapp.load_sign_state("2026-09-19")
        self.assertEqual(got, {"13800138000": {"status": "success", "message": "",
                                               "task": "default"}},
                         "转发必须现取 STATE_DIR，而不是服务层自持的绑定")

    def test_sign_window_stub_reaches_status_executors_and_capacity(self):
        """`web.app._sign_window` 的既有打桩点对三处消费方都生效。"""
        with mock.patch.object(self.webapp, "_sign_window",
                               return_value=((8, 0), (9, 0))):
            win = self.webapp._executors_window()
            self.assertEqual((win.start_min, win.end_min), (480, 540))
            self.assertEqual(self.webapp._slot_to_label(0), "08:00")
            now = datetime(2026, 9, 16, 8, 30)
            self.assertEqual(self.webapp.sign_status(now),
                             ("签到窗口进行中（~09:00 结束）", "#9ece6a"))
        self.assertIsInstance(self.webapp._capacity_estimate(10), int)

    def test_in_sign_window_stub_reaches_in_run_period(self):
        """`web.app._in_sign_window` / `_day_off_reason` 的既有打桩点必须穿透到 `_in_run_period`。"""
        import yiban.window as yb_window
        bounds = yb_window.bounds({"sign_start": (6, 30), "sign_end": (7, 50),
                                   "edge_front_sec": 0, "edge_back_sec": 0})
        now = datetime(2026, 9, 16, 9, 0)     # 钟点上在窗口外
        with mock.patch.object(self.webapp, "_in_sign_window", return_value=True):
            self.assertTrue(self.webapp._in_run_period(bounds, now),
                            "打桩后的钟点判定必须被 `_in_run_period` 取用")
        with mock.patch.object(self.webapp, "_in_sign_window", return_value=False):
            self.assertFalse(self.webapp._in_run_period(bounds, now))
        # 门判定同样是现取注入：窗口内但被判为"今天不签到"时结果为 False
        with mock.patch.object(self.webapp, "_in_sign_window", return_value=True), \
                mock.patch.object(self.webapp, "_day_off_reason",
                                  return_value="今日无需打卡（周六）"):
            self.assertFalse(self.webapp._in_run_period(bounds, now),
                             "打桩后的门判定必须被 `_in_run_period` 取用")

    def test_env_flag_stub_reaches_settings_effective_values(self):
        """`web.app._env_flag` 是设置页生效值的注入点：打桩必须生效。"""
        self._write_raw("YIBAN_SUNDAY_SIGN=enable\n")
        values = self.webapp._settings_effective_values(self.env_file)
        self.assertEqual(values["sunday_sign"], "0", "默认解析器只认 1/true/on/yes")
        with mock.patch.object(self.webapp, "_env_flag",
                               side_effect=lambda v: str(v).strip().lower() == "enable"):
            values = self.webapp._settings_effective_values(self.env_file)
        self.assertEqual(values["sunday_sign"], "1", "开关解析器必须现取注入")

    # ------------------------------------------------------------------
    # 3. 行为往返（口径逐字不变）
    # ------------------------------------------------------------------
    def test_log_line_visibility_rules(self):
        vis = self.webapp._log_line_visible
        for logger_name in ("yiban", "yiban.client", "yiban.fyiban.protocol",
                            "yiban.engine.queue"):
            for level in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
                self.assertTrue(vis(level, logger_name), f"{logger_name}/{level} 应可见")
        self.assertFalse(vis("INFO", "notify"), "非 yiban 组件的 INFO 不展示")
        self.assertFalse(vis("INFO", "werkzeug"), "请求日志不展示")
        self.assertFalse(vis("DEBUG", "mailer"))
        self.assertTrue(vis("WARNING", "mailer"), "组件 WARNING 应展示（故障留痕）")
        self.assertTrue(vis("ERROR", "werkzeug"))
        self.assertTrue(vis("CRITICAL", "notify"))
        self.assertFalse(vis("INFO", "yibanx"), "前缀相近的 logger 不享受全级别")

    def test_parse_and_lines_for_filter_by_date_and_format(self):
        today = datetime.now().strftime("%Y-%m-%d")
        lines = [
            f"[{today} 06:40:04] [INFO] yiban: 签到开始",
            f"[{today} 06:40:05] [INFO] yiban.client: [13800138000] 生成定位: (1,2)",
            f"[{today} 06:40:06] [WARNING] mailer: 邮件通知发送失败",
            f"[{today} 06:40:07] [INFO] notify: 推送已发送",
            f"[{today} 06:40:08] [DEBUG] yiban: 探针跳过",
            "不是日志行",
        ]
        path = self.webapp.log_path_for(today)
        with io.open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        got = self.webapp.parse_sign_log(path)
        self.assertEqual(got, [lines[0], lines[1], lines[2], lines[4]])
        self.assertEqual(self.webapp._log_lines_for(today), got,
                         "两条路径必须共用同一条可见性规则")

    def test_is_valid_date_str_is_calendar_strict(self):
        good = self.webapp._is_valid_date_str
        self.assertTrue(good("2026-09-19"))
        self.assertTrue(good("2024-02-29"))
        # strptime 接受非零填充的月/日（既有口径，未收紧）
        self.assertTrue(good("2026-9-1"))
        for bad in ("2026-02-30", "2026-13-01", "", None, 20260919):
            self.assertFalse(good(bad), f"{bad!r} 应被拒绝")

    def test_tail_lines_truncates_and_tolerates_missing(self):
        path = os.path.join(self.log_dir, "tail.log")
        rows = [f"row-{i:04d}-{'x' * 20}" for i in range(200)]
        with io.open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(rows) + "\n")
        full = self.webapp._tail_lines(path)
        self.assertEqual(full, rows)
        tail = self.webapp._tail_lines(path, 40)
        self.assertTrue(tail, "截断后仍应读到尾部若干完整行")
        self.assertEqual(tail[-1], rows[-1], "尾部必须完整")
        self.assertLess(len(tail), len(rows))
        self.assertEqual(self.webapp._tail_lines(os.path.join(self.log_dir, "nope")), [])

    def test_load_sign_state_falls_back_to_legacy_daily_file(self):
        date = "2026-09-19"
        with io.open(os.path.join(self.tmp, f"sign-daily-{date}.json"), "w",
                     encoding="utf-8") as f:
            f.write('{"13800138000": "✅", "13900139000": "❌", '
                    '"13700137000": "➖", "13600136000": "?"}')
        got = self.webapp.load_sign_state(date)
        self.assertEqual(got["13800138000"]["status"], self.webapp.STATUS_SUCCESS)
        self.assertEqual(got["13900139000"]["status"], self.webapp.STATUS_FAILED)
        self.assertEqual(got["13700137000"]["status"], self.webapp.STATUS_NO_TASK)
        self.assertEqual(got["13600136000"]["status"], self.webapp.STATUS_PENDING)
        # 结构化文件优先；为空 dict 时继续回退
        structured = os.path.join(self.tmp, f"sign-state-{date}.json")
        with io.open(structured, "w", encoding="utf-8") as f:
            f.write('{"13800138000": {"status": "success"}}')
        self.assertEqual(self.webapp.load_sign_state(date),
                         {"13800138000": {"status": "success"}})
        with io.open(structured, "w", encoding="utf-8") as f:
            f.write("{}")
        self.assertEqual(len(self.webapp.load_sign_state(date)), 4, "空结构化文件回退旧格式")
        for name in (structured, os.path.join(self.tmp, f"sign-daily-{date}.json")):
            os.remove(name)
        self.assertEqual(self.webapp.load_sign_state(date), {})

    def test_mask_log_phones_only_masks_bracketed_11_digits(self):
        mask = self.webapp._mask_log_phones
        self.assertEqual(mask("[13800138000] ✅ 签到成功"), "[138****8000] ✅ 签到成功")
        self.assertEqual(mask("[13800138000][13900139000] 两个"), "[138****8000][139****9000] 两个")
        self.assertEqual(mask("[1380013800] 少一位"), "[1380013800] 少一位")
        self.assertEqual(mask("账号: 13800138000"), "账号: 13800138000")
        self.assertEqual(mask("无号码"), "无号码")

    def test_cred_paused_phones_and_clear_fuse_pause(self):
        import json

        from yiban import cred_state
        p1, p2 = "13800138000", "13900139000"
        with io.open(cred_state.path(), "w", encoding="utf-8") as f:
            json.dump({p1: {"fail_days": 3, "paused_since": "2026-09-01 00:00:00"},
                       p2: {"fail_days": 0, "paused_since": ""}}, f)
        self.assertEqual(self.webapp._cred_paused_phones(), {p1})
        self.webapp.clear_fuse_pause(p1)
        with io.open(cred_state.path(), encoding="utf-8") as f:
            self.assertNotIn(p1, json.load(f), "凭据变更后必须清除暂停记录")
        # 文件不存在 = 常态（从未熔断），静默返回且不抛
        os.remove(cred_state.path())
        self.assertEqual(self.webapp._cred_paused_phones(), set())
        self.assertIsNone(self.webapp.clear_fuse_pause(p1))
        # 只改备注/状态不清：手机号与密码都没变
        with io.open(cred_state.path(), "w", encoding="utf-8") as f:
            json.dump({p1: {"fail_days": 2, "paused_since": "2026-09-01 00:00:00"}}, f)
        self.webapp.clear_fuse_on_cred_change(p1, "pw", {"phone": p1, "password": "pw"})
        self.assertEqual(self.webapp._cred_paused_phones(), {p1}, "非凭据变更不得清熔断")
        # 改密清当前号、改绑清旧号
        self.webapp.clear_fuse_on_cred_change(p1, "old", {"phone": p1, "password": "new"})
        self.assertEqual(self.webapp._cred_paused_phones(), set())
        with io.open(cred_state.path(), "w", encoding="utf-8") as f:
            json.dump({p1: {"fail_days": 2, "paused_since": "2026-09-01 00:00:00"}}, f)
        self.webapp.clear_fuse_on_cred_change(p1, "pw", {"phone": p2, "password": "pw"})
        self.assertEqual(self.webapp._cred_paused_phones(), set(), "改绑必须清旧号")

    def test_env_flag_literals(self):
        flag = self.webapp._env_flag
        for truthy in ("1", "true", "TRUE", "On", "yes", "  YES  "):
            self.assertTrue(flag(truthy), f"{truthy!r} 应为开")
        for falsy in ("0", "false", "off", "no", "", None, "enable", "2"):
            self.assertFalse(flag(falsy), f"{falsy!r} 应为关")

    def test_sign_window_reads_env_and_falls_back(self):
        self._write_raw("YIBAN_SIGN_START=07:00\nYIBAN_SIGN_END=07:30\n")
        self.assertEqual(self.webapp._sign_window(), ((7, 0), (7, 30)))
        self._write_raw("YIBAN_SIGN_START=bad\nYIBAN_SIGN_END=07:30\n")
        self.assertEqual(self.webapp._sign_window(), ((6, 30), (7, 30)),
                         "非法起点回退默认、终点照取 .env（与引擎同一份逐字段解析）")
        self._write_raw("")
        self.assertEqual(self.webapp._sign_window(), ((6, 30), (7, 50)))

    def test_in_sign_window_minute_bounds(self):
        import yiban.window as yb_window
        bounds = yb_window.bounds({"sign_start": (6, 30), "sign_end": (7, 50),
                                   "edge_front_sec": 0, "edge_back_sec": 0})
        inside = self.webapp._in_sign_window
        self.assertFalse(inside(bounds, datetime(2026, 9, 16, 6, 29, 59)))
        self.assertTrue(inside(bounds, datetime(2026, 9, 16, 6, 30, 0)))
        self.assertTrue(inside(bounds, datetime(2026, 9, 16, 7, 50, 0)))
        self.assertFalse(inside(bounds, datetime(2026, 9, 16, 7, 50, 1)))
        self.assertFalse(inside(bounds, datetime(2026, 9, 16, 23, 0, 0)))

    def test_in_run_period_combines_window_and_gates(self):
        import yiban.window as yb_window
        bounds = yb_window.bounds({"sign_start": (6, 30), "sign_end": (7, 50),
                                   "edge_front_sec": 0, "edge_back_sec": 0})
        self._write_raw("YIBAN_SIGN_START=06:30\nYIBAN_SIGN_END=07:50\n")
        # 周六/周日默认关闭 → 钟点在窗口内也不算"本应运行"
        self.assertFalse(self.webapp._in_run_period(bounds, datetime(2026, 9, 19, 6, 40)))
        self.assertFalse(self.webapp._in_run_period(bounds, datetime(2026, 9, 20, 6, 40)))
        self.assertTrue(self.webapp._in_run_period(bounds, datetime(2026, 9, 16, 6, 40)))
        self.assertFalse(self.webapp._in_run_period(bounds, datetime(2026, 9, 16, 9, 0)),
                         "窗口外一律不算本应运行")

    def test_sign_status_texts_and_weekend_short_circuit(self):
        self._write_raw("YIBAN_SIGN_START=06:30\nYIBAN_SIGN_END=07:50\n")
        status = self.webapp.sign_status
        self.assertEqual(status(datetime(2026, 9, 16, 6, 0)),
                         ("未到签到时间（06:30 开始）", "#7aa2f7"))
        self.assertEqual(status(datetime(2026, 9, 16, 7, 0)),
                         ("签到窗口进行中（~07:50 结束）", "#9ece6a"))
        self.assertEqual(status(datetime(2026, 9, 16, 8, 0)),
                         ("今日签到已结束", "#e0af68"))
        self.assertEqual(status(datetime(2026, 9, 19, 7, 0)),
                         ("今日无需打卡（周六）", "#a1a1aa"))
        self.assertEqual(status(datetime(2026, 9, 20, 7, 0)),
                         ("今日无需打卡（周日）", "#a1a1aa"))
        self._write_raw("YIBAN_SATURDAY_SIGN=1\nYIBAN_SIGN_START=06:30\nYIBAN_SIGN_END=07:50\n")
        self.assertEqual(status(datetime(2026, 9, 19, 7, 0)),
                         ("签到窗口进行中（~07:50 结束）", "#9ece6a"))

    def test_check_connectivity_ok_and_error_detail(self):
        from web.services import signstatus as ss_mod
        resp = mock.Mock(status_code=200)
        with mock.patch.object(ss_mod.requests, "get", return_value=resp) as get:
            self.assertEqual(self.webapp.check_connectivity(), (True, "HTTP 200"))
        self.assertEqual(get.call_args.kwargs["timeout"], 6)
        self.assertIn("Mobile", get.call_args.kwargs["headers"]["User-Agent"])
        resp503 = mock.Mock(status_code=503)
        with mock.patch.object(ss_mod.requests, "get", return_value=resp503):
            self.assertEqual(self.webapp.check_connectivity(), (False, "HTTP 503"))
        with mock.patch.object(ss_mod.requests, "get", side_effect=OSError("x" * 100)):
            ok, detail = self.webapp.check_connectivity()
        self.assertFalse(ok)
        self.assertEqual(len(detail), 60, "错误详情必须截到 60 字")

    def test_most_recent_log_date_scans_history_and_caches(self):
        self.webapp.LOG_FILE = os.path.join(self.log_dir, "sign.log")
        with io.open(os.path.join(self.log_dir, "sign-2020-01-02.log"), "w",
                     encoding="utf-8") as f:
            f.write("[2020-01-02 06:40:00] [INFO] yiban: 签到\n")
        with mock.patch.object(self.webapp.clock, "now",
                               return_value=datetime(2020, 1, 5, 9, 0)):
            self.assertEqual(self.webapp._most_recent_log_date(30), "2020-01-02")
            self.assertEqual(self.webapp._most_recent_log_date(1), "2020-01-02",
                             "同日命中缓存：不因本次的扫描范围更窄而改写答案")
            self.assertEqual(self.webapp._most_recent_log_cache["checked_day"], "2020-01-05")
        # 今天有日志时立即返回今天（并刷新缓存）
        self.webapp._most_recent_log_cache.update({"history_date": None, "checked_day": ""})
        with io.open(os.path.join(self.log_dir, "sign-2020-01-05.log"), "w",
                     encoding="utf-8") as f:
            f.write("[2020-01-05 06:40:00] [INFO] yiban: 签到\n")
        with mock.patch.object(self.webapp.clock, "now",
                               return_value=datetime(2020, 1, 5, 9, 0)):
            self.assertEqual(self.webapp._most_recent_log_date(30), "2020-01-05")

    # ------------------------------------------------------------------
    # 4/5. 别名加载安全与状态归属
    # ------------------------------------------------------------------
    def test_alias_loaded_app_shares_service_modules(self):
        from web.services import logs as logs_mod
        from web.services import signstatus as ss_mod
        self.assertIs(self.webapp._logs_svc, logs_mod,
                      "别名加载的 app 副本必须复用同一个 web.services.logs")
        self.assertIs(self.webapp._signstatus, ss_mod,
                      "别名加载的 app 副本必须复用同一个 web.services.signstatus")

    def test_service_modules_hold_no_app_state(self):
        from web.services import logs as logs_mod
        from web.services import signstatus as ss_mod
        for mod in (logs_mod, ss_mod):
            for name in APP_HELD_STATE_LOGS:
                self.assertFalse(hasattr(mod, name),
                                 f"{mod.__name__} 不得持有 {name}（应由 web.app 调用时刻注入）")

    def test_service_sources_do_not_import_app(self):
        for rel in SERVICE_SOURCES_LOGS:
            with io.open(os.path.join(BASE, rel), encoding="utf-8") as f:
                src = f.read()
            self.assertIsNone(re.search(r"^\s*(?:import|from)\s+web\.app\b", src, re.M),
                              f"{rel} 禁止 import web.app（别名加载会执行副本模块）")

    def test_importing_services_does_not_execute_web_app(self):
        """全新解释器里只 import 服务层：不得把 web.app 拉进 sys.modules。"""
        code = (
            "import sys;"
            f"sys.path[:0]=[{os.path.join(BASE, 'scripts')!r}, {BASE!r}];"
            "import web.services.logs, web.services.signstatus;"
            "print('APP_LOADED' if 'web.app' in sys.modules else 'SERVICES_ONLY_OK')"
        )
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        r = subprocess.run([sys.executable, "-c", code], cwd=BASE, env=env,
                           capture_output=True, text=True, timeout=60)
        self.assertIn("SERVICES_ONLY_OK", r.stdout, r.stderr[-600:])
        self.assertEqual(r.returncode, 0, r.stderr[-600:])


MOVED_NOTIFY = (
    "_nl_safe",
    "_audit_actor",
    "_audit_alert_facts",
    "_last_cleanup_text",
    "_change_mail",
    "_review_reject_mail",
    "_alert_mail_recipients",
    "send_notification",
    "_exhaustion_notice_mail",
    "_mail_flags_desc",
    "_notify_change_desc",
    "_push_ever_configured",
    # 只被迁出族使用、随所属域搬的常量
    "_NOTIFY_LEDGER_LABELS",
    "_MAIL_FLAG_NAMES",
    "_PUSH_CONFIG_ENV_KEYS",
)


MOVED_CHANNEL = (
    "_alert_channel_status",
    "_channel_health_degraded",
    "_channel_status_lines",
    "_daily_budget_desc",
    "_health_report_sent_today",
    "_channel_health_facts",
    "_audit_channel_health_degraded",
    "_send_channel_health_report",
    "_HEALTH_REPORT_META_KEY",
)


MOVED_CAPACITY = (
    "_capacity_account_count",
    "_capacity_audit_count",
    "_capacity_estimate",
    "_accounts_at_capacity",
    "_registration_paused",
    "_users_at_capacity",
    "_mail_alert_due",
    "_notify_capacity_once",
    # 随域搬的节流状态与缺省窗口
    "DEFAULT_MAIL_ALERT_COOLDOWN",
    "_mail_alert_ts",
    "_mail_alert_lock",
    "_capacity_alerts",
)


MOVED_MANUAL = (
    "_wait_signin_proc",
    "_batch_wait_timeout",
    "_manual_sign_failure_reason",
    "_log_manual_sign_exit",
    "_SIGNIN_EXIT_REASONS",
)


PURE_REEXPORTS_NOTIFY = {
    "notify_mail": (
        "_nl_safe", "_audit_actor", "_audit_alert_facts", "_last_cleanup_text",
        "_change_mail", "_review_reject_mail", "_alert_mail_recipients",
        "_exhaustion_notice_mail", "_mail_flags_desc", "_notify_change_desc",
        "_NOTIFY_LEDGER_LABELS", "_MAIL_FLAG_NAMES", "_PUSH_CONFIG_ENV_KEYS",
    ),
    "channel_health": (
        "_channel_health_degraded", "_daily_budget_desc", "_health_report_sent_today",
        "_channel_health_facts", "_audit_channel_health_degraded", "_HEALTH_REPORT_META_KEY",
    ),
    "capacity": (
        "_capacity_account_count", "_capacity_audit_count",
        "DEFAULT_MAIL_ALERT_COOLDOWN", "_mail_alert_ts", "_mail_alert_lock", "_capacity_alerts",
    ),
    "manual_sign": (
        "_wait_signin_proc", "_batch_wait_timeout", "_manual_sign_failure_reason",
        "_SIGNIN_EXIT_REASONS",
    ),
}


FORWARDED_NOTIFY = {
    "notify_mail": ("send_notification", "_push_ever_configured"),
    "channel_health": ("_alert_channel_status", "_channel_status_lines",
                       "_send_channel_health_report"),
    "capacity": ("_capacity_estimate", "_accounts_at_capacity", "_registration_paused",
                 "_users_at_capacity", "_mail_alert_due", "_notify_capacity_once"),
    "manual_sign": ("_log_manual_sign_exit",),
}


COMMON_APP_STATE = (
    "ENV_FILE", "LOG_FILE", "STATE_DIR", "read_env", "load_env_int",
    "_sign_window", "edge_config", "log_path_for", "_atomic_write",
    "_file_lock", "_rate_lock",
)


APP_HELD_STATE_NOTIFY = {
    "web/services/notify_mail.py": (*COMMON_APP_STATE, "_mail_alert_due"),
    "web/services/channel_health.py": (
        *COMMON_APP_STATE, "send_notification", "_push_ever_configured", "_mail_alert_due"),
    "web/services/capacity.py": (
        *COMMON_APP_STATE, "send_notification", "DEFAULT_MAX_ACCOUNTS", "DEFAULT_MAX_USERS"),
    "web/services/manual_sign.py": COMMON_APP_STATE,
}


SERVICE_SOURCES_NOTIFY = tuple(APP_HELD_STATE_NOTIFY)


class WebServicesNotifySplitContractTest(unittest.TestCase):
    """以别名加载的 app 副本为靶子（routes 与测试实际看到的那一份）。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-notify-split-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        cls._old_env = {k: os.environ.get(k) for k in
                        ("YIBAN_ENV_FILE", "YIBAN_DB_FILE", "YIBAN_STATE_DIR",
                         "YIBAN_ACCOUNTS_FILE", "YIBAN_ACCOUNTS_KEY", "YIBAN_LOG_FILE")}
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_DB_FILE"] = os.path.join(cls.tmp, "yiban.db")
        os.environ["YIBAN_STATE_DIR"] = cls.tmp
        os.environ["YIBAN_ACCOUNTS_FILE"] = os.path.join(cls.tmp, "accounts.json")
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
        os.environ["YIBAN_LOG_FILE"] = os.path.join(cls.tmp, "sign.log")
        cls._write_env([])
        spec = importlib.util.spec_from_file_location(
            "webapp_notifysplit", os.path.join(BASE, "web", "app.py"))
        cls.webapp = importlib.util.module_from_spec(spec)
        sys.modules["webapp_notifysplit"] = cls.webapp
        with contextlib.suppress(Exception):
            spec.loader.exec_module(cls.webapp)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k, v in cls._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    @classmethod
    def _write_env(cls, lines):
        with io.open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n" + "\n".join(lines) + "\n")

    def setUp(self):
        self._write_env([])
        self.webapp.ENV_FILE = self.env_file
        self.webapp._mail_alert_ts.clear()
        # 去重表定义在 web/services/capacity.py 上，是**全进程唯一**的一份：其他
        # 测试文件（如 test_capacity_limits）置位后不会复位，会让本类的触顶用例
        # 从"已告警"起步而静默少一次调用。每用例复位到实现默认值。
        self.webapp._capacity_alerts.update({"users": False, "accounts": False})

    # ------------------------------------------------------------------
    # 1. 名字面
    # ------------------------------------------------------------------
    def test_every_moved_name_reachable_on_app_and_home_module(self):
        from web.services import capacity as cap
        from web.services import channel_health as ch
        from web.services import manual_sign as ms
        from web.services import notify_mail as nm
        for mod, names in ((nm, MOVED_NOTIFY), (ch, MOVED_CHANNEL),
                           (cap, MOVED_CAPACITY), (ms, MOVED_MANUAL)):
            for name in names:
                self.assertTrue(hasattr(self.webapp, name), f"web.app 兼容面缺失 {name}")
                self.assertTrue(hasattr(mod, name), f"{mod.__name__} 缺 {name}")

    def test_pure_reexports_are_same_object(self):
        import web.services.capacity as cap
        import web.services.channel_health as ch
        import web.services.manual_sign as ms
        import web.services.notify_mail as nm
        for mod, names in ((nm, PURE_REEXPORTS_NOTIFY["notify_mail"]),
                           (ch, PURE_REEXPORTS_NOTIFY["channel_health"]),
                           (cap, PURE_REEXPORTS_NOTIFY["capacity"]),
                           (ms, PURE_REEXPORTS_NOTIFY["manual_sign"])):
            for name in names:
                self.assertIs(getattr(self.webapp, name), getattr(mod, name),
                              f"{name} 应为同一个对象（web.app 只是再导出）")

    def test_forwarders_actually_forward(self):
        """注入型名字不得退化成纯再导出（否则 app 侧打桩面静默失效）。"""
        import web.services.capacity as cap
        import web.services.channel_health as ch
        import web.services.manual_sign as ms
        import web.services.notify_mail as nm
        for mod, names in ((nm, FORWARDED_NOTIFY["notify_mail"]),
                           (ch, FORWARDED_NOTIFY["channel_health"]),
                           (cap, FORWARDED_NOTIFY["capacity"]),
                           (ms, FORWARDED_NOTIFY["manual_sign"])):
            for name in names:
                self.assertIsNot(getattr(self.webapp, name), getattr(mod, name),
                                 f"{name} 必须是转发包装（调用时刻现取 app 侧名字）")

    def test_forwarders_keep_original_call_arity(self):
        """routes / 既有测试按原实参个数调用，转发不得改签名。"""
        with self.flask_request_context():
            self.assertEqual(self.webapp._nl_safe("x"), "x")
            self.assertIsInstance(self.webapp._audit_actor(), str)
            self.assertEqual(self.webapp._change_mail("摘要").summary, "摘要")
            self.assertEqual(self.webapp._review_reject_mail([], "").summary,
                             "您提交的易班账号未通过管理员审核。")
        self.assertIsInstance(self.webapp._push_ever_configured(), bool)
        self.assertIsInstance(self.webapp._push_ever_configured({}), bool)
        self.assertIsInstance(self.webapp._alert_channel_status(), dict)
        self.assertIsInstance(self.webapp._channel_status_lines(), list)
        self.assertIsInstance(self.webapp._capacity_estimate(0), int)
        self.assertIsInstance(self.webapp._accounts_at_capacity(0), bool)
        self.assertIsInstance(self.webapp._registration_paused(), bool)
        self.assertIsInstance(self.webapp._users_at_capacity(), bool)
        self.assertIsInstance(self.webapp._mail_alert_due("t"), bool)
        self.assertIsInstance(self.webapp._channel_health_degraded(self._status_base()), bool)
        self.assertIsInstance(self.webapp._daily_budget_desc({}), list)

    @staticmethod
    def _status_base(**overrides):
        """通道健康判据的最小完整状态（字段口径见 `_alert_channel_status`）。"""
        status = {
            "mail_flag_on": True, "mail_usable": True, "mail_state": "ok",
            "mail_state_detail": "", "mail_self_notify": True, "mail_recipients": 1,
            "mail_user": "-", "mail_admin_to": "-", "mail_error": "",
            "push_usable": True, "push_configured": True, "push_ever_configured": False,
            "push_type": "serverchan", "push_secret_masked": "SCT****", "push_urgent_only": False,
            "push_error": "", "daily_max": None, "daily_remaining": None,
            "urgent_daily_max": None, "urgent_daily_remaining": None,
        }
        status.update(overrides)
        return status

    def flask_request_context(self):
        import flask
        app = flask.Flask("notify-split-ctx")
        app.secret_key = "notify-split-secret"
        return app.test_request_context()

    # ------------------------------------------------------------------
    # 2. 转发注入 app 模块级状态（打桩往返）
    # ------------------------------------------------------------------
    def test_mail_alert_due_stub_reaches_send_notification(self):
        """`web.app._mail_alert_due` 是既有打桩点：必须穿透到发送路径。"""
        with mock.patch.object(self.webapp.mailer, "admin_recipients",
                               return_value=["alert@test.local"]), \
                mock.patch.object(self.webapp.mailer, "admin_notify_enabled",
                                  return_value=True), \
                mock.patch.object(self.webapp.mailer, "send_admin_alert") as snd, \
                mock.patch.object(self.webapp.notify, "send"), \
                mock.patch.object(self.webapp.notify, "pop_exhaustion_notice",
                                  return_value=None), \
                mock.patch.object(self.webapp, "_mail_alert_due",
                                  return_value=False) as due:
            self.webapp.send_notification("节流标题", "正文")
        self.assertEqual(due.call_count, 1, "转发必须现取 _mail_alert_due")
        snd.assert_not_called()
        # 反面对照：节流放行时邮件照发（证明打桩值真的驱动了分支）
        with mock.patch.object(self.webapp.mailer, "admin_recipients",
                               return_value=["alert@test.local"]), \
                mock.patch.object(self.webapp.mailer, "admin_notify_enabled",
                                  return_value=True), \
                mock.patch.object(self.webapp.mailer, "send_admin_alert") as snd, \
                mock.patch.object(self.webapp.notify, "send"), \
                mock.patch.object(self.webapp.notify, "pop_exhaustion_notice",
                                  return_value=None), \
                mock.patch.object(self.webapp, "_mail_alert_due", return_value=True):
            self.webapp.send_notification("节流标题", "正文")
        self.assertEqual(snd.call_count, 1)
        self.assertEqual(snd.call_args.kwargs["to"], "alert@test.local")

    def test_read_env_and_env_file_stubs_reach_push_ever_configured(self):
        with mock.patch.object(self.webapp, "read_env",
                               return_value={"YIBAN_NOTIFY_TYPE": "serverchan"}) as spy:
            self.assertTrue(self.webapp._push_ever_configured())
        self.assertEqual(spy.call_args.args[0], self.env_file,
                         "转发必须现取本模块的 ENV_FILE")
        with mock.patch.object(self.webapp, "read_env", return_value={}):
            self.assertFalse(self.webapp._push_ever_configured())
        with mock.patch.object(self.webapp, "read_env", return_value={}):
            self.assertTrue(self.webapp._push_ever_configured(
                {"YIBAN_NOTIFY_SECRET_ENC": "x"}), "显式传入的 envs 优先")

    def test_sign_window_and_edge_stubs_reach_capacity_estimate(self):
        """`web.app._sign_window` / `edge_config` 的既有打桩点对容量预估生效。"""
        with mock.patch.object(self.webapp, "_sign_window",
                               return_value=((6, 30), (7, 50))), \
                mock.patch.object(self.webapp, "edge_config", return_value=(0, 0)), \
                mock.patch.dict(os.environ, {"YIBAN_AVG_ATTEMPT_SEC": "8"}):
            self.assertEqual(self.webapp._capacity_estimate(0), 600)
        with mock.patch.object(self.webapp, "_sign_window",
                               return_value=((6, 30), (7, 50))), \
                mock.patch.object(self.webapp, "edge_config", return_value=(60, 60)), \
                mock.patch.dict(os.environ, {"YIBAN_AVG_ATTEMPT_SEC": "8"}):
            self.assertEqual(self.webapp._capacity_estimate(0), (4680 - 8) // 8 + 1,
                             "窗口打桩与裁剪打桩都必须被现取")

    def test_load_env_int_and_env_file_stubs_reach_capacity_gates(self):
        with mock.patch.object(self.webapp, "load_env_int", return_value=1) as spy:
            self.assertFalse(self.webapp._accounts_at_capacity(0))
            self.assertTrue(self.webapp._accounts_at_capacity(2))
        self.assertEqual(spy.call_args.args[0], self.env_file)
        self.assertEqual(spy.call_args.args[1], "YIBAN_MAX_ACCOUNTS")
        self.assertEqual(spy.call_args.args[2], self.webapp.DEFAULT_MAX_ACCOUNTS,
                         "缺省上限必须现取本模块的常量")
        with mock.patch.object(self.webapp, "load_env_int", return_value=0):
            self.assertFalse(self.webapp._accounts_at_capacity(999), "0 = 不限")
        with mock.patch.object(self.webapp, "load_env_int",
                               side_effect=lambda f, k, d: 1 if k == "YIBAN_REGISTRATION_PAUSE"
                               else d):
            self.assertTrue(self.webapp._registration_paused())
        with mock.patch.object(self.webapp, "load_env_int", return_value=1) as spy:
            self.webapp._users_at_capacity()
        self.assertEqual(spy.call_args.args[1], "YIBAN_MAX_USERS")
        self.assertEqual(spy.call_args.args[2], self.webapp.DEFAULT_MAX_USERS)

    def test_mail_alert_due_uses_app_env_reader_and_shared_table(self):
        """节流表必须与 `web.app._mail_alert_ts` 是同一份（测试按属性 .clear() 复位）。"""
        import web.services.capacity as cap
        self.assertIs(self.webapp._mail_alert_ts, cap._mail_alert_ts)
        with mock.patch.object(self.webapp, "load_env_int", return_value=60) as spy:
            self.assertTrue(self.webapp._mail_alert_due("标题"))
            self.assertFalse(self.webapp._mail_alert_due("标题"))
            self.assertTrue(self.webapp._mail_alert_due("另一标题"))
        self.assertEqual(spy.call_args.args[0], self.env_file)
        self.webapp._mail_alert_ts.clear()
        with mock.patch.object(self.webapp, "load_env_int", return_value=0):
            self.assertTrue(self.webapp._mail_alert_due("标题"))
            self.assertTrue(self.webapp._mail_alert_due("标题"), "0=关闭节流")
        # 直接写在 app 侧节流表上的时刻必须被真源看见（同一对象，非副本）
        self.webapp._mail_alert_ts["注入标题"] = time.time()
        with mock.patch.object(self.webapp, "load_env_int", return_value=60):
            self.assertFalse(self.webapp._mail_alert_due("注入标题"))

    def test_status_lines_and_send_stubs_reach_health_report(self):
        """日报的状态行与发信出口都必须是 app 侧现取（既有打桩面）。"""
        with mock.patch.object(self.webapp.db, "audit", return_value=True), \
                mock.patch.object(self.webapp.db, "get_meta", return_value=""), \
                mock.patch.object(self.webapp.db, "set_meta") as set_meta, \
                mock.patch.object(self.webapp.notify, "pop_exhaustion_notice",
                                  return_value=[]), \
                mock.patch.object(self.webapp, "_channel_status_lines",
                                  return_value=["改版后的状态行"]) as lines, \
                mock.patch.object(self.webapp, "send_notification") as snd:
            self.assertTrue(self.webapp._send_channel_health_report())
        self.assertEqual(lines.call_count, 1, "转发必须现取 _channel_status_lines")
        self.assertEqual(snd.call_count, 1)
        self.assertEqual(snd.call_args.args[0], "告警通道健康日报")
        report = snd.call_args.args[1]
        self.assertIn("改版后的状态行", report.items, "正文必须用打桩后的状态行")
        self.assertEqual(set_meta.call_count, 1, "成功后落当日去重标记")

    def test_log_path_for_stub_reaches_manual_sign_exit_log(self):
        """`web.app.log_path_for` 必须被现取：换日志目录后留痕跟着换。"""
        target = os.path.join(self.tmp, "manual-exit.log")
        with mock.patch.object(self.webapp, "log_path_for", return_value=target) as spy:
            self.webapp._log_manual_sign_exit("138****0000", 3)
        self.assertEqual(spy.call_count, 1)
        with io.open(target, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("手动签到未完成", text)
        self.assertIn("队列忙", text)
        # 退出码 0（正常）不写任何行
        os.remove(target)
        with mock.patch.object(self.webapp, "log_path_for", return_value=target):
            self.assertIsNone(self.webapp._log_manual_sign_exit("138****0000", 0))
        self.assertFalse(os.path.exists(target), "0 退出码不得留痕")

    def test_notify_capacity_once_uses_app_send_notification(self):
        with mock.patch.object(self.webapp, "send_notification") as snd:
            self.webapp._notify_capacity_once("users", 500, "注册人数")
            self.webapp._notify_capacity_once("users", 500, "注册人数")  # 每进程只一次
        self.assertEqual(snd.call_count, 1)
        self.assertEqual(snd.call_args.args[0], "注册人数已达上限")
        self.assertTrue(snd.call_args.kwargs.get("urgent"))
        self.webapp._capacity_alerts["users"] = False  # 复位进程内去重表

    # ------------------------------------------------------------------
    # 3. 行为逐字不变
    # ------------------------------------------------------------------
    def test_capacity_formula_verbatim(self):
        with mock.patch.object(self.webapp, "_sign_window",
                               return_value=((6, 30), (7, 50))), \
                mock.patch.object(self.webapp, "edge_config", return_value=(0, 0)), \
                mock.patch.dict(os.environ, {"YIBAN_AVG_ATTEMPT_SEC": "8"}):
            self.assertEqual(self.webapp._capacity_estimate(10), (4800 - 8) // 18 + 1)
            self.assertEqual(self.webapp._capacity_estimate(3600), 2)
        # 退化窗口（起止同点）回退默认窗口 06:30~07:50（与引擎同源，故不为 0）
        with mock.patch.object(self.webapp, "_sign_window",
                               return_value=((7, 50), (7, 50))), \
                mock.patch.object(self.webapp, "edge_config", return_value=(0, 0)), \
                mock.patch.dict(os.environ, {"YIBAN_AVG_ATTEMPT_SEC": "8"}):
            self.assertEqual(self.webapp._capacity_estimate(0), (4680 - 8) // 8 + 1)

    def test_manual_sign_exit_semantics_verbatim(self):
        f = self.webapp._manual_sign_failure_reason
        self.assertIsNone(f(0))
        self.assertIsNone(f(None))
        self.assertIn("队列忙", f(3))
        self.assertIn("跳过", f(2))
        self.assertIn("退出码 1", f(1))
        w = self.webapp._batch_wait_timeout
        self.assertEqual((w(0), w(1), w(5), w(10)), (300, 420, 900, 1500))
        self.assertEqual(self.webapp._wait_signin_proc.__defaults__[0], 300,
                         "_wait_signin_proc 默认参数必须保持 300")

    def test_wait_signin_proc_terminates_then_kills(self):
        class FakeProc:
            def __init__(self):
                self.calls, self.terminated, self.killed = 0, False, False

            def wait(self, timeout=None):
                self.calls += 1
                if self.calls == 1:
                    raise subprocess.TimeoutExpired("fake", timeout)
                return 0

            def terminate(self):
                self.terminated = True

            def kill(self):
                self.killed = True

        proc = FakeProc()
        self.webapp._wait_signin_proc(proc)
        self.assertEqual((proc.calls, proc.terminated, proc.killed), (2, True, False))

    def test_wait_signin_proc_tolerates_already_dead_child(self):
        """子进程在 terminate/kill 前已退出：ProcessLookupError 不得穿出等待函数，
        否则调用方的退出码留痕被跳过；kill 后回收也给超时兜底。"""
        class DeadProc:
            def __init__(self):
                self.terminated, self.killed = 0, 0

            def wait(self, timeout=None):
                raise subprocess.TimeoutExpired("dead", timeout)

            def terminate(self):
                self.terminated += 1
                raise ProcessLookupError("no such process")

            def kill(self):
                self.killed += 1
                raise ProcessLookupError("no such process")

        proc = DeadProc()
        self.webapp._wait_signin_proc(proc)      # 不得抛
        self.assertEqual((proc.terminated, proc.killed), (1, 1))

    def test_channel_health_degraded_three_conditions_verbatim(self):
        base = self._status_base()
        degraded = self.webapp._channel_health_degraded
        self.assertFalse(degraded(dict(base)), "两路都活着 → 不降级")
        self.assertFalse(degraded(dict(base, push_usable=False)),
                         "推送从未配置过（ever=False）而不可用 → 合法终态，不降级")
        # (a) 邮件侧不可用
        self.assertTrue(degraded(dict(base, mail_usable=False)))
        # (b) 邮件侧可用却无收件人
        self.assertTrue(degraded(dict(base, mail_recipients=0)))
        # (c) 推送曾配置过而现在不可用
        self.assertTrue(degraded(dict(base, push_ever_configured=True, push_usable=False)))
        # 看不清状态 / 当日额度耗尽
        self.assertTrue(degraded(dict(base, mail_error="OSError")))
        self.assertTrue(degraded(dict(base, push_error="ValueError")))
        self.assertTrue(degraded(dict(base), ["general"]), "当日有账本耗尽即降级")
        self.assertTrue(degraded(dict(base, push_ever_configured=True), ["urgent"]))

    def test_channel_health_facts_records_both_sides_unconditionally(self):
        status = self.webapp._alert_channel_status()
        facts = self.webapp._channel_health_facts(status, ["general"])
        self.assertIn("邮件通道=", facts)
        self.assertIn("推送通道=", facts)
        self.assertIn("推送额度已用尽=非紧急", facts)
        self.assertLessEqual(len(facts), 200, "摘要须能被 db.audit 的 200 字符上限容纳")

    def test_capacity_alerts_and_meta_key_same_object(self):
        import web.services.capacity as cap
        import web.services.channel_health as ch
        self.assertIs(self.webapp._capacity_alerts, cap._capacity_alerts)
        self.assertEqual(self.webapp._HEALTH_REPORT_META_KEY, ch._HEALTH_REPORT_META_KEY)
        self.assertEqual(self.webapp._HEALTH_REPORT_META_KEY, "channel_health_last")
        self.assertEqual(self.webapp._NOTIFY_LEDGER_LABELS["login_fail"], "登录失败告警")

    def test_nl_safe_line_model_verbatim(self):
        for ch in ("\r", "\n", "\v", "\f", "\x1c", "\x1d", "\x1e", "\u0085", "\u2028", "\u2029"):
            self.assertNotIn(ch, self.webapp._nl_safe(f"甲{ch}乙"))
        once = self.webapp._nl_safe("a\u2028b")
        self.assertEqual(self.webapp._nl_safe(once), once, "净化必须幂等")

    def test_review_reject_mail_sanitizes_fields(self):
        """拒信正文的被拒账号与理由都要过净化：纯文本拒信不得被拆出伪造行。

        理由来自管理员表单（外部输入）；此前直接塞进 `fields`，`\\n`/`U+2028`
        都能在用户收到的纯文本邮件里伪造一行（如假造一条管理员说明）。
        """
        mail_obj = self.webapp._review_reject_mail(
            ["138****8000\u2028伪造行"], "理由\n伪造第二行")
        fields = dict(mail_obj.fields)
        for label in ("被拒账号", "审核理由"):
            value = fields[label]
            for ch in ("\n", "\r", "\u2028"):
                self.assertNotIn(ch, value, f"{label} 仍含裸分隔符: {value!r}")
        self.assertIn("\\n", fields["审核理由"], "换行应转成字面量而非直接丢弃")
        # 空值分支的兜底文案不受净化影响
        empty = dict(self.webapp._review_reject_mail([], "").fields)
        self.assertEqual(empty["被拒账号"], "（见「我的账号」页）")
        self.assertEqual(empty["审核理由"], "管理员未填写，可联系管理员了解详情")

    # ------------------------------------------------------------------
    # 4/5. 别名加载安全与状态归属
    # ------------------------------------------------------------------
    def test_alias_loaded_app_shares_service_modules(self):
        import web.services.capacity as cap
        import web.services.channel_health as ch
        import web.services.manual_sign as ms
        import web.services.notify_mail as nm
        self.assertIs(self.webapp._capacity, cap)
        self.assertIs(self.webapp._channel_health, ch)
        self.assertIs(self.webapp._manual_sign, ms)
        self.assertIs(self.webapp._notify_mail, nm)

    def test_service_modules_hold_no_app_state(self):
        import web.services.capacity as cap
        import web.services.channel_health as ch
        import web.services.manual_sign as ms
        import web.services.notify_mail as nm
        mods = {"web/services/notify_mail.py": nm, "web/services/channel_health.py": ch,
                "web/services/capacity.py": cap, "web/services/manual_sign.py": ms}
        for rel, mod in mods.items():
            for name in APP_HELD_STATE_NOTIFY[rel]:
                self.assertFalse(hasattr(mod, name),
                                 f"{rel} 不得持有 {name}（应由 web.app 调用时刻注入）")

    def test_service_sources_do_not_import_app(self):
        for rel in SERVICE_SOURCES_NOTIFY:
            with io.open(os.path.join(BASE, rel), encoding="utf-8") as f:
                src = f.read()
            self.assertIsNone(re.search(r"^\s*(?:import|from)\s+web\.app\b", src, re.M),
                              f"{rel} 禁止 import web.app（别名加载会执行副本模块）")

    def test_importing_services_does_not_execute_web_app(self):
        """全新解释器里只 import 四个服务模块：不得把 web.app 拉进 sys.modules。"""
        code = (
            "import sys;"
            f"sys.path[:0]=[{os.path.join(BASE, 'scripts')!r}, {BASE!r}];"
            "import web.services.notify_mail, web.services.channel_health,"
            " web.services.capacity, web.services.manual_sign;"
            "print('APP_LOADED' if 'web.app' in sys.modules else 'SERVICES_ONLY_OK')"
        )
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        r = subprocess.run([sys.executable, "-c", code], cwd=BASE, env=env,
                           capture_output=True, text=True, timeout=120)
        self.assertIn("SERVICES_ONLY_OK", r.stdout, r.stderr[-600:])
        self.assertEqual(r.returncode, 0, r.stderr[-600:])


MOVED_SECURITY = (
    "_new_admin_sid",
    "_issue_admin_sid",
    "_admin_session_facts",
    "_builtin_admin_email",
    "_is_builtin_admin_session",
    "_effective_role",
    "_current_role",
    "migrate_admin_password_to_hash",
    "_constant_time_dummy",
    "reject_default_admin_password",
    "_client_ip",
    "_ip_store_trim",
    "_bump_window_count",
    "_bump_login_failure",
    "_sensitive_gate_params",
    "_verify_attempt_allowed",
    "_verify_fail_cooldown_remaining",
    "_record_verify_failure",
    "check_admin_configured",
    "_builtin_admin_loginable",
    "verify_admin",
    "_atomic_write",
    "_replace_with_retry",
    # 只被迁出族使用、随所属域搬的常量；另加同域的策略/口径常量
    "SCRYPT_METHOD",
    "ADMIN_SID_ENV_KEY",
    "LOGIN_LOCK_SECONDS",
    "TRUSTED_PROXIES",
    "_IP_STORE_LIMIT",
    "_IP_STORE_MAX_AGE",
    "VERIFY_MAX",
    "VERIFY_WINDOW",
    "VERIFY_FAIL_MAX",
    "VERIFY_FAIL_WINDOW",
    "VERIFY_FAIL_COOLDOWN",
    "VERIFY_FAIL_AUTH_KEYWORDS",
    "PW_CONFIRM_TTL_DEFAULT",
    "PW_CONFIRM_TTL_MAX",
    "PW_CONFIRM_COOLDOWN_DEFAULT",
    "_DEFAULT_ADMIN_LITERALS",
    "_REPLACE_RETRY_ATTEMPTS",
    "_REPLACE_RETRY_BASE_SEC",
)


PURE_REEXPORTS_SEC = (
    "_new_admin_sid",
    "_constant_time_dummy",
    "_client_ip",
    "_ip_store_trim",
    "_bump_window_count",
    "_bump_login_failure",
    "_verify_attempt_allowed",
    "_verify_fail_cooldown_remaining",
    "_record_verify_failure",
    "_atomic_write",
    "_replace_with_retry",
)


FORWARDED_SEC = (
    "_issue_admin_sid",
    "_admin_session_facts",
    "_builtin_admin_email",
    "_is_builtin_admin_session",
    "_effective_role",
    "_current_role",
    "migrate_admin_password_to_hash",
    "reject_default_admin_password",
    "_sensitive_gate_params",
    "check_admin_configured",
    "_builtin_admin_loginable",
    "verify_admin",
)


APP_HELD_STATE_SEC = (
    "ENV_FILE", "LOG_FILE", "STATE_DIR", "read_env", "write_env_key",
    "write_env_batch", "load_env_int", "_count_env_key_lines", "send_notification",
    "SESSION_ABS_TTL_SECONDS", "verify_jobs", "_verify_sem", "_file_lock",
)


class WebSecuritySplitContractTest(unittest.TestCase):
    """以别名加载的 app 副本为靶子（routes 与测试实际看到的那一份）。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-security-split-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        cls._old_env = {k: os.environ.get(k) for k in
                        ("YIBAN_ENV_FILE", "YIBAN_DB_FILE", "YIBAN_STATE_DIR",
                         "YIBAN_ACCOUNTS_FILE", "YIBAN_ACCOUNTS_KEY", "YIBAN_LOG_FILE")}
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_DB_FILE"] = os.path.join(cls.tmp, "yiban.db")
        os.environ["YIBAN_STATE_DIR"] = cls.tmp
        os.environ["YIBAN_ACCOUNTS_FILE"] = os.path.join(cls.tmp, "accounts.json")
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
        os.environ["YIBAN_LOG_FILE"] = os.path.join(cls.tmp, "sign.log")
        cls._write_env([])
        spec = importlib.util.spec_from_file_location(
            "webapp_securitysplit", os.path.join(BASE, "web", "app.py"))
        cls.webapp = importlib.util.module_from_spec(spec)
        sys.modules["webapp_securitysplit"] = cls.webapp
        with contextlib.suppress(Exception):
            spec.loader.exec_module(cls.webapp)
        cls.flask_app = flask.Flask("securitysplit-tests")
        cls.flask_app.secret_key = "securitysplit-test-secret"

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k, v in cls._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    @classmethod
    def _write_env(cls, lines):
        with io.open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n" + "\n".join(lines) + "\n")

    def setUp(self):
        self._write_env([])
        self.webapp.ENV_FILE = self.env_file

    # ------------------------------------------------------------------
    # 1. 名字面
    # ------------------------------------------------------------------
    def test_every_moved_name_reachable_on_app_and_home_module(self):
        import web.security as sec
        for name in MOVED_SECURITY:
            self.assertTrue(hasattr(self.webapp, name), f"web.app 兼容面缺失 {name}")
            self.assertTrue(hasattr(sec, name), f"web/security.py 缺 {name}")

    def test_pure_reexports_are_same_object(self):
        import web.security as sec
        for name in PURE_REEXPORTS_SEC:
            self.assertIs(getattr(self.webapp, name), getattr(sec, name),
                          f"{name} 应为同一个对象（web.app 只是再导出）")

    def test_forwarders_actually_forward(self):
        """注入型名字不得退化成纯再导出（否则 app 侧打桩面静默失效）。"""
        import web.security as sec
        for name in FORWARDED_SEC:
            self.assertIsNot(getattr(self.webapp, name), getattr(sec, name),
                             f"{name} 必须是转发包装（调用时刻现取 app 侧名字）")

    def test_forwarders_keep_original_call_arity(self):
        """routes / 既有测试按原实参个数调用，转发不得改签名。"""
        self.assertIsInstance(self.webapp._builtin_admin_email(), str)
        with self.flask_app.test_request_context():
            self.assertIsInstance(self.webapp._is_builtin_admin_session(), bool)
        self.assertIsInstance(self.webapp.check_admin_configured(), bool)
        self.assertIsInstance(self.webapp._builtin_admin_loginable(), bool)
        self.assertIsNone(self.webapp._effective_role(None))
        self.assertIsInstance(self.webapp._admin_session_facts(self.env_file), tuple)
        self.assertIsInstance(self.webapp._issue_admin_sid(self.env_file), str)
        self.assertIsInstance(self.webapp.verify_admin("x", "y"), bool)
        self.assertIsInstance(self.webapp._sensitive_gate_params(self.env_file), tuple)

    def test_rate_lock_single_definition_point(self):
        """`_rate_lock` 收口在 locks.py：三处必须是同一把锁（禁止另建一把）。"""
        import web.security as sec
        from web.services import locks
        self.assertIs(self.webapp._rate_lock, locks._rate_lock)
        self.assertIs(sec._rate_lock, locks._rate_lock)
        self.assertIsNot(self.webapp._rate_lock, self.webapp._file_lock,
                         "限速锁与文件锁是两把不同用途的锁")

    def test_verify_queue_and_measure_names_untouched(self):
        """安全域不得占用他域名字面（`verify_jobs` 仍指 yiban 真源）。"""
        import web.security as sec
        from yiban.attempt import jobs as yb_jobs
        self.assertIs(self.webapp.verify_jobs, yb_jobs)
        self.assertFalse(hasattr(sec, "verify_jobs"))

    # ------------------------------------------------------------------
    # 2. 打桩往返（注入的是调用时刻的 app 侧名字）
    # ------------------------------------------------------------------
    def test_read_env_stub_reaches_check_admin_configured(self):
        with mock.patch.object(self.webapp, "read_env",
                               return_value={"YIBAN_ADMIN_USER": "a",
                                             "YIBAN_ADMIN_PASSWORD_HASH": "h"}) as spy:
            self.assertTrue(self.webapp.check_admin_configured())
        self.assertEqual(spy.call_count, 1, "转发必须现取 read_env")
        with mock.patch.object(self.webapp, "read_env", return_value={}):
            self.assertFalse(self.webapp.check_admin_configured())

    def test_env_file_assignment_reaches_builtin_admin_email(self):
        self._write_env(["YIBAN_ADMIN_USER=Root@Example.com"])
        self.assertEqual(self.webapp._builtin_admin_email(), "root@example.com",
                         "转发必须现取本模块的 ENV_FILE（小写化口径不变）")

    def test_send_notification_and_read_env_stubs_reach_verify_admin(self):
        """verify_admin 的两条打桩面：.env 读取器与告警出口。"""
        # 只有明文、没有哈希 = M1 明文回退 fail-closed（迁移失败降级态）
        with mock.patch.object(self.webapp, "read_env",
                               return_value={"YIBAN_ADMIN_USER": "a",
                                             "YIBAN_ADMIN_PASSWORD": "Plain#1234"}) as spy:
            self.assertFalse(self.webapp.verify_admin("a", "Plain#1234"))
        self.assertEqual(spy.call_count, 1)
        # 哈希歧义（统计得 2 行）→ 拒绝 + 经 app 侧 send_notification 告警
        with mock.patch.object(self.webapp, "read_env",
                               return_value={"YIBAN_ADMIN_USER": "a",
                                             "YIBAN_ADMIN_PASSWORD_HASH": "h"}), \
                mock.patch.object(self.webapp, "_count_env_key_lines", return_value=2), \
                mock.patch.object(self.webapp, "send_notification") as sn, \
                mock.patch.object(self.webapp, "_constant_time_dummy") as dummy:
            self.assertFalse(self.webapp.verify_admin("a", "x"))
        self.assertTrue(sn.called, "歧义态必须经 app 侧告警出口告警")
        self.assertTrue(sn.call_args.kwargs.get("urgent"))
        self.assertEqual(sn.call_args.kwargs.get("ledger"), "login_fail")
        self.assertEqual(dummy.call_count, 1, "拒绝分支必须走 app 侧的时延拉平")

    def test_constant_time_dummy_stub_reaches_verify_admin(self):
        with mock.patch.object(self.webapp, "read_env", return_value={}), \
                mock.patch.object(self.webapp, "_constant_time_dummy") as dummy:
            self.assertFalse(self.webapp.verify_admin("a", "x"))
        self.assertEqual(dummy.call_count, 1, "凭据未配齐分支须现取 _constant_time_dummy")

    def test_session_abs_ttl_assignment_reaches_current_role(self):
        """`SESSION_ABS_TTL_SECONDS` 在 create_app 里会按 .env 回写模块全局：转发必须现取。"""
        now = int(time.time())
        with mock.patch.object(self.webapp, "read_env", return_value={}), \
                self.flask_app.test_request_context():
            flask.session["auth"] = True
            flask.session["username"] = ""
            flask.session["login_ts"] = now - 100
            with mock.patch.object(self.webapp, "SESSION_ABS_TTL_SECONDS", 1000):
                self.assertIsNone(self.webapp._current_role(), "未登录（无角色）仍是 None")
            with mock.patch.object(self.webapp, "SESSION_ABS_TTL_SECONDS", 10):
                self.assertIsNone(self.webapp._current_role(),
                                  "绝对期被打桩成 10 秒后超龄会话必须失效")

    def test_load_env_int_stub_reaches_sensitive_gate_params(self):
        with mock.patch.object(self.webapp, "load_env_int",
                               return_value=99999) as spy:
            ttl, cooldown = self.webapp._sensitive_gate_params(self.env_file)
        self.assertEqual(ttl, self.webapp.PW_CONFIRM_TTL_MAX,
                         "TTL 上界硬钳在真源里生效（打桩值 99999 只按 900）")
        self.assertEqual(cooldown, 99999, "冷却不钳制（0=关闭）")
        self.assertEqual(spy.call_count, 2, "两个旋钮各读一次，且都经 app 侧读取器")

    def test_write_env_key_and_read_env_stubs_reach_issue_admin_sid(self):
        with mock.patch.object(self.webapp, "write_env_key") as w, \
                mock.patch.object(self.webapp, "read_env",
                                  return_value={self.webapp.ADMIN_SID_ENV_KEY: "old-sid"}):
            sid = self.webapp._issue_admin_sid(self.env_file)
        self.assertEqual(w.call_args.args[0], self.env_file)
        self.assertEqual(w.call_args.args[1], self.webapp.ADMIN_SID_ENV_KEY)
        self.assertNotEqual(sid, "old-sid")
        # 落盘失败（OSError）→ 返回 .env 里的旧值，不把会话锁在门外
        with mock.patch.object(self.webapp, "write_env_key", side_effect=OSError("ro")), \
                mock.patch.object(self.webapp, "read_env",
                                  return_value={self.webapp.ADMIN_SID_ENV_KEY: "old-sid"}):
            self.assertEqual(self.webapp._issue_admin_sid(self.env_file), "old-sid")
        with mock.patch.object(self.webapp, "read_env", return_value={}), \
                mock.patch.object(self.webapp, "write_env_batch") as wb, \
                mock.patch.object(self.webapp, "load_env_int", return_value=1):
            self.webapp.migrate_admin_password_to_hash(self.env_file)
        self.assertEqual(wb.call_count, 0, "无明文时不写 .env（幂等）")

    def test_atomic_write_stub_reaches_env_and_measure_paths(self):
        """`_atomic_write` 必须是同一函数对象，且 env_io / measure 的转发按调用时刻现取。"""
        import web.security as sec
        self.assertIs(self.webapp._atomic_write, sec._atomic_write,
                      "web.app._atomic_write 必须是 web/security.py 的同一函数对象")
        with mock.patch.object(self.webapp, "_atomic_write") as spy:
            self.webapp.write_env_batch(self.env_file, {"YIBAN_X": "1"})
        self.assertEqual(spy.call_count, 1, "env_io 落盘必须经 app 侧 _atomic_write（打桩点）")
        self.assertIs(spy.call_args.kwargs["chmod_priv"], True)
        with mock.patch.object(self.webapp, "_atomic_write") as spy:
            self.webapp._write_measure_state(os.path.join(self.tmp, "m.json"), {"at": ""})
        self.assertEqual(spy.call_count, 1, "实测落盘同样经 app 侧 _atomic_write")

    def test_ip_store_trim_stub_is_reexport(self):
        """计数助手是纯再导出：routes 经 m.* 打桩时命中真实现（同一对象）。"""
        import web.security as sec
        self.assertIs(self.webapp._ip_store_trim, sec._ip_store_trim)
        store = {f"k{i}": (1, 0.0) for i in range(self.webapp._IP_STORE_LIMIT + 1)}
        self.webapp._ip_store_trim(store, self.webapp._IP_STORE_MAX_AGE)
        self.assertLess(len(store), 10, "超限且全过期必须被回收")

    # ------------------------------------------------------------------
    # 3. 行为逐字不变
    # ------------------------------------------------------------------
    def test_migrate_admin_password_to_hash_roundtrip(self):
        real_write = self.webapp.write_env_batch
        with mock.patch.object(self.webapp, "read_env",
                               return_value={"YIBAN_ADMIN_PASSWORD": "Strong#1234"}):
            self.webapp.migrate_admin_password_to_hash(self.env_file)
        text = io.open(self.env_file, encoding="utf-8").read()
        self.assertIn("YIBAN_ADMIN_PASSWORD_HASH=", text)
        self.assertNotIn("Strong#1234", text, "明文必须被清空（值空 → 删行）")
        self.assertNotIn("YIBAN_ADMIN_PASSWORD=", text)
        self.assertIs(real_write, self.webapp.write_env_batch)

    def test_reject_default_admin_password_verbatim_thresholds(self):
        weak = os.path.join(self.tmp, "weak.env")
        strong = os.path.join(self.tmp, "strong.env")
        with io.open(weak, "w", encoding="utf-8") as f:
            f.write("YIBAN_ADMIN_PASSWORD=admin123\n")
        with io.open(strong, "w", encoding="utf-8") as f:
            f.write("YIBAN_ADMIN_PASSWORD=Abcdefghij12\n")
        with self.assertRaises(SystemExit) as cm:
            self.webapp.reject_default_admin_password(weak)
        self.assertEqual(cm.exception.code, 2, "公开模板默认字面量必须 fail-closed")
        self.assertIsNone(self.webapp.reject_default_admin_password(strong),
                          "12 位三类应放行")
        with mock.patch.object(self.webapp, "read_env", side_effect=OSError("boom")):
            self.assertIsNone(self.webapp.reject_default_admin_password(weak),
                              "读取失败不得阻断启动")

    def test_window_and_failure_counters_semantics(self):
        store = {}
        for i in range(10):
            _c, _s, allowed = self.webapp._bump_window_count(store, "ip", 1000.0, 60, limit=10)
            self.assertTrue(allowed, f"第 {i + 1} 次应放行")
        _c, _s, allowed = self.webapp._bump_window_count(store, "ip", 1000.0, 60, limit=10)
        self.assertFalse(allowed, "第 11 次应拒绝")
        self.assertEqual(store["ip"][0], 10, "拒绝时不得递增")
        # 翻窗：窗口起点后移，计数从 1 重新开始
        _c, start, allowed = self.webapp._bump_window_count(store, "ip", 2000.0, 60, limit=10)
        self.assertTrue(allowed)
        self.assertEqual((_c, start), (1, 2000.0))
        fails = {}
        self.assertEqual(self.webapp._bump_login_failure(fails, "k", 5.0), 1)
        self.assertEqual(self.webapp._bump_login_failure(fails, "k", 6.0), 2)
        self.assertEqual(fails["k"], (2, 0, 6.0), "失败表口径 (次数, 0, 时刻) 不变")

    def test_verify_attempt_quota_and_cooldown(self):
        store = {}
        for _ in range(self.webapp.VERIFY_MAX):
            self.assertTrue(self.webapp._verify_attempt_allowed(store, "Admin@x.io"))
        self.assertFalse(self.webapp._verify_attempt_allowed(store, "admin@x.io"),
                         "配额按用户名小写归一且先判后增")
        cooldown = {}
        now = 5000.0
        self.assertEqual(self.webapp._verify_fail_cooldown_remaining(cooldown, "p", now), 0)
        self.assertEqual(self.webapp._record_verify_failure(cooldown, "p", "网络超时", now),
                         "其他失败", "网络类失败不计冷却")
        self.assertEqual(cooldown, {})
        for _ in range(self.webapp.VERIFY_FAIL_MAX):
            kind = self.webapp._record_verify_failure(cooldown, "p", "账号或密码错误", now)
        self.assertEqual(kind, "认证失败")
        self.assertGreater(self.webapp._verify_fail_cooldown_remaining(cooldown, "p", now), 0)
        self.assertEqual(cooldown["p"][0], 0, "触发冷却后窗口计数清零")

    def test_effective_role_builtin_credential_matrix(self):
        env = {"YIBAN_ADMIN_USER": "admin", "YIBAN_ADMIN_PW_VERSION": "3",
               self.webapp.ADMIN_SID_ENV_KEY: "sid-1"}
        cases = [
            ({"auth_source": "builtin", "username": "admin", "sid": "sid-1"}, 3, "admin"),
            ({"auth_source": "builtin", "username": "ADMIN", "sid": "sid-1"}, 3, "admin"),
            ({"auth_source": "builtin", "username": "admin", "sid": "sid-1"}, 2, None),
            ({"auth_source": "builtin", "username": "admin", "sid": "stale"}, 3, None),
            ({"auth_source": "builtin", "username": "admin"}, 3, None),  # 未携带 sid 即不匹配
            ({"auth_source": "user", "username": "admin", "sid": "sid-1"}, 3, None),
            ({"username": "admin", "sid": "sid-1"}, 3, None),  # 缺 auth_source 的旧会话
        ]
        for data, pwv, want in cases:
            with self.subTest(data=data, pwv=pwv):
                with mock.patch.object(self.webapp, "read_env", return_value=env), \
                        mock.patch.object(self.webapp.db, "find_user", return_value=None), \
                        self.flask_app.test_request_context():
                    flask.session.update(data)
                    got = self.webapp._effective_role(data.get("username"), pwv)
                self.assertEqual(got, want)
        # 升级日存量部署兼容：.env 尚未签发 sid（空）时不强制重登
        with mock.patch.object(self.webapp, "read_env",
                               return_value={"YIBAN_ADMIN_USER": "admin"}), \
                self.flask_app.test_request_context():
            flask.session.update({"auth_source": "builtin", "username": "admin"})
            self.assertEqual(self.webapp._effective_role("admin", 1), "admin")
        with mock.patch.object(self.webapp, "read_env", return_value=env), \
                mock.patch.object(self.webapp.db, "find_user",
                                  return_value={"role": "admin", "sid": "s", "pw_version": 2}), \
                self.flask_app.test_request_context():
            flask.session.update({"auth_source": "user", "username": "u@x.io", "sid": "s"})
            self.assertEqual(self.webapp._effective_role("u@x.io", 2), "admin")
            self.assertEqual(self.webapp._effective_role("u@x.io", 1), None,
                             "pw_version 不匹配即失效")

    def test_current_role_absolute_expiry_clears_session(self):
        env = {"YIBAN_ADMIN_USER": "admin"}
        with mock.patch.object(self.webapp, "read_env", return_value=env), \
                self.flask_app.test_request_context():
            self.assertIsNone(self.webapp._current_role(), "无 auth 即未登录")
            flask.session["auth"] = True
            flask.session["username"] = "u@x.io"
            flask.session["login_ts"] = int(time.time()) - self.webapp.SESSION_ABS_TTL_SECONDS - 5
            with mock.patch.object(self.webapp.db, "find_user", return_value=None):
                self.assertIsNone(self.webapp._current_role())
            self.assertEqual(len(flask.session), 0, "超限必须清空会话（视为未登录）")

    def test_client_ip_trusted_proxy_and_fallback(self):
        proxies = self.webapp.TRUSTED_PROXIES
        app = self.flask_app
        with app.test_request_context("/", environ_base={"REMOTE_ADDR": proxies[0]},
                                      headers={"X-Forwarded-For": "203.0.113.9, 10.0.0.1"}):
            self.assertEqual(self.webapp._client_ip(), "203.0.113.9")
        with app.test_request_context("/", environ_base={"REMOTE_ADDR": "203.0.113.9"},
                                      headers={"X-Forwarded-For": "198.51.100.7"}):
            self.assertEqual(self.webapp._client_ip(), "203.0.113.9",
                             "非可信首跳的 XFF 必须被忽略（不可伪造）")
        with app.test_request_context("/", environ_base={"REMOTE_ADDR": proxies[1]}):
            self.assertEqual(self.webapp._client_ip(), proxies[1])

    def test_atomic_write_roundtrip_and_reparse(self):
        target = os.path.join(self.tmp, "aw-security.txt")
        self.webapp._atomic_write(target, "内容content", chmod_priv=True)
        with io.open(target, encoding="utf-8") as f:
            self.assertEqual(f.read(), "内容content")
        leftovers = [p for p in os.listdir(self.tmp)
                     if p.startswith("aw-security") and ".tmp" in p]
        self.assertEqual(leftovers, [], "替换后不得残留 tmp")

    def test_migrate_admin_password_clears_plain_and_hash_priority(self):
        env_path = os.path.join(self.tmp, "migrate.env")
        with io.open(env_path, "w", encoding="utf-8") as f:
            f.write("YIBAN_ADMIN_USER=admin\nYIBAN_ADMIN_PASSWORD=Strong#1234\n")
        self.webapp.migrate_admin_password_to_hash(env_path)
        text = io.open(env_path, encoding="utf-8").read()
        self.assertIn("YIBAN_ADMIN_PASSWORD_HASH=", text)
        self.assertNotIn("YIBAN_ADMIN_PASSWORD=Strong#1234", text)
        before = text
        self.webapp.migrate_admin_password_to_hash(env_path)
        self.assertEqual(io.open(env_path, encoding="utf-8").read(), before,
                         "第二次迁移必须幂等（无明文可迁）")
        with mock.patch.object(self.webapp, "read_env") as re_:
            re_.return_value = {"YIBAN_ADMIN_USER": "admin",
                                "YIBAN_ADMIN_PASSWORD_HASH": "not-a-hash"}
            self.assertFalse(self.webapp.verify_admin("admin", "pw"))

    def test_builtin_admin_loginable_three_states(self):
        cases = [
            ({"YIBAN_ADMIN_USER": "", "YIBAN_ADMIN_PASSWORD_HASH": "h"}, 1, False),
            ({"YIBAN_ADMIN_USER": "a", "YIBAN_ADMIN_PASSWORD": "p"}, 0, False),
            ({"YIBAN_ADMIN_USER": "a", "YIBAN_ADMIN_PASSWORD_HASH": "h"}, 2, False),
            ({"YIBAN_ADMIN_USER": "a", "YIBAN_ADMIN_PASSWORD_HASH": "h"}, 1, True),
        ]
        for env, lines, want in cases:
            with self.subTest(env=env, lines=lines), \
                    mock.patch.object(self.webapp, "read_env", return_value=env), \
                    mock.patch.object(self.webapp, "_count_env_key_lines",
                                      return_value=lines):
                self.assertEqual(self.webapp._builtin_admin_loginable(), want)

    # ------------------------------------------------------------------
    # 4/5. 别名加载安全与状态归属
    # ------------------------------------------------------------------
    def test_alias_loaded_app_shares_security_module(self):
        import web.security as sec
        self.assertIs(self.webapp._security, sec,
                      "别名加载的 app 副本必须复用同一个 web.security")

    def test_security_module_holds_no_app_state(self):
        import web.security as sec
        for name in APP_HELD_STATE_SEC:
            self.assertFalse(hasattr(sec, name),
                             f"web/security.py 不得持有 {name}（应由 web.app 调用时刻注入）")

    def test_security_source_does_not_import_app(self):
        with io.open(os.path.join(BASE, "web", "security.py"), encoding="utf-8") as f:
            src = f.read()
        import re as _re
        self.assertIsNone(_re.search(r"^\s*(?:import|from)\s+web\.app\b", src, _re.M),
                          "web/security.py 禁止 import web.app")

    def test_importing_security_does_not_execute_web_app(self):
        """全新解释器里只 import web.security：不得把 web.app 拉进 sys.modules。"""
        code = (
            "import sys;"
            f"sys.path[:0]=[{os.path.join(BASE, 'scripts')!r}, {BASE!r}];"
            "import web.security;"
            "print('APP_LOADED' if 'web.app' in sys.modules else 'SECURITY_ONLY_OK')"
        )
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        r = subprocess.run([sys.executable, "-c", code], cwd=BASE, env=env,
                           capture_output=True, text=True, timeout=120)
        self.assertIn("SECURITY_ONLY_OK", r.stdout, r.stderr[-600:])
        self.assertEqual(r.returncode, 0, r.stderr[-600:])


M_ATTR_RE = re.compile(r"\bm\.([A-Za-z_]\w*)")


M_ROUTE_NAMES_TOTAL = 199


M_ROUTE_COMPAT_NAMES = 198


M_ROUTE_NAME_EXCLUDED = frozenset({"__file__"})


SPLIT_MODULES = (
    "web.services.accounts_data",
    "web.services.capacity",
    "web.services.channel_health",
    "web.services.env_io",
    "web.services.executor_env",
    "web.services.locks",
    "web.services.logs",
    "web.services.manual_sign",
    "web.services.measure",
    "web.services.notify_mail",
    "web.services.signstatus",
    "web.services.verify_queue",
    "web.render",
    "web.security",
)


SPLIT_SOURCES = tuple(m.replace(".", "/") + ".py" for m in SPLIT_MODULES)


def scan_m_route_names():
    """扫 `web/routes/*.py` 的 `m.<名字>` 命中集（排序列表，供断言与报错用）。"""
    names = set()
    routes_dir = os.path.join(BASE, "web", "routes")
    for name in sorted(os.listdir(routes_dir)):
        if not name.endswith(".py"):
            continue
        with io.open(os.path.join(routes_dir, name), encoding="utf-8") as f:
            names.update(M_ATTR_RE.findall(f.read()))
    return sorted(names)


class AppNameSurfaceContractTest(unittest.TestCase):
    """以别名加载的 app 副本为靶子（routes 与测试实际看到的那一份）。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-appsplit-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        cls._old_env = {k: os.environ.get(k) for k in
                        ("YIBAN_ENV_FILE", "YIBAN_DB_FILE", "YIBAN_STATE_DIR",
                         "YIBAN_ACCOUNTS_FILE", "YIBAN_ACCOUNTS_KEY", "YIBAN_LOG_FILE")}
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_DB_FILE"] = os.path.join(cls.tmp, "yiban.db")
        os.environ["YIBAN_STATE_DIR"] = cls.tmp
        os.environ["YIBAN_ACCOUNTS_FILE"] = os.path.join(cls.tmp, "accounts.json")
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
        os.environ["YIBAN_LOG_FILE"] = os.path.join(cls.tmp, "sign.log")
        with io.open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n")
        spec = importlib.util.spec_from_file_location(
            "webapp_appsplit", os.path.join(BASE, "web", "app.py"))
        cls.webapp = importlib.util.module_from_spec(spec)
        sys.modules["webapp_appsplit"] = cls.webapp
        with contextlib.suppress(Exception):
            spec.loader.exec_module(cls.webapp)
        cls.flask_app = flask.Flask("webapp_appsplit")
        cls.flask_app.secret_key = "appsplit-test-secret"

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k, v in cls._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # ------------------------------------------------------------------
    # 1. 名字面总账
    # ------------------------------------------------------------------
    def test_scan_counts_are_pinned(self):
        """命中总数与兼容名面数硬钉：名字面增删必须被看见，不能静默漂移。"""
        names = scan_m_route_names()
        self.assertEqual(len(names), M_ROUTE_NAMES_TOTAL,
                         f"routes 的 m.* 命中数变了（现 {len(names)}）；"
                         "若确为新增兼容名，请同步更新本文件的钉死计数并复核 app 名字面")
        compat = [n for n in names if n not in M_ROUTE_NAME_EXCLUDED]
        self.assertEqual(len(compat), M_ROUTE_COMPAT_NAMES,
                         f"兼容名面计数变了（现 {len(compat)}，含 {len(names) - len(compat)} 个排除名）")
        excluded = sorted(set(names) & M_ROUTE_NAME_EXCLUDED)
        self.assertEqual(set(names) & M_ROUTE_NAME_EXCLUDED, set(M_ROUTE_NAME_EXCLUDED),
                         f"排除集与实测不符：命中 {excluded}，排除集 {sorted(M_ROUTE_NAME_EXCLUDED)}")

    def test_every_m_route_name_reachable_on_app_module(self):
        """routes 经 `m.*` 取用的每个名字都必须在 app 模块上可达（别名加载的那一份）。"""
        missing = [n for n in scan_m_route_names() if not hasattr(self.webapp, n)]
        self.assertEqual(missing, [],
                         f"web.app 名字面缺失 routes 取用的名字（路由体在请求期会 AttributeError）：{missing}")

    def test_route_modules_do_not_import_web_app(self):
        """正则钉：拆分模块的源文件不得 `import web.app`（别名加载会执行副本模块）。"""
        bad = []
        for rel in SPLIT_SOURCES:
            with io.open(os.path.join(BASE, rel), encoding="utf-8") as f:
                src = f.read()
            if re.search(r"^\s*(?:import|from)\s+web\.app\b", src, re.M):
                bad.append(rel)
        self.assertEqual(bad, [], f"以下模块禁止 import web.app：{bad}")

    # ------------------------------------------------------------------
    # 2. 全新解释器实测：只 import 拆分模块，不得把 web.app 拉进来
    # ------------------------------------------------------------------
    def test_importing_split_modules_does_not_execute_web_app(self):
        """子进程双钉：导入全部拆分模块后 web.app 不在 sys.modules，且 app 名字面仍齐。"""
        code = (
            "import sys, re, pathlib, importlib;"
            f"sys.path[:0]=[{os.path.join(BASE, 'scripts')!r}, {BASE!r}];"
            f"[importlib.import_module(m) for m in {list(SPLIT_MODULES)!r}];"
            "print('APP_LOADED_BY_SPLIT' if 'web.app' in sys.modules else 'SPLIT_ONLY_OK');"
            "import web.app as m;"
            "names=set();"
            "[names.update(re.findall(r'\\bm\\.([A-Za-z_]\\w*)', p.read_text(encoding='utf-8')))"
            " for p in pathlib.Path('web/routes').glob('*.py')];"
            "print('NAMES', len(names));"
            "print('MISSING', ','.join(sorted(n for n in names if not hasattr(m, n))) or 'NONE')"
        )
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        env["YIBAN_STATE_DIR"] = self.tmp
        env["YIBAN_LOG_FILE"] = os.path.join(self.tmp, "sign.log")
        env["YIBAN_DB_FILE"] = os.path.join(self.tmp, "probe.db")
        env["YIBAN_ENV_FILE"] = self.env_file
        r = subprocess.run([sys.executable, "-c", code], cwd=BASE, env=env,
                           capture_output=True, text=True, timeout=120)
        self.assertIn("SPLIT_ONLY_OK", r.stdout, r.stderr[-800:])
        self.assertNotIn("APP_LOADED_BY_SPLIT", r.stdout, r.stderr[-800:])
        self.assertIn(f"NAMES {M_ROUTE_NAMES_TOTAL}", r.stdout,
                      f"独立进程改扫出的命中数与本文件钉死值不符：{r.stdout!r} {r.stderr[-400:]}")
        self.assertIn("MISSING NONE", r.stdout,
                      f"独立进程里 web.app 名字面缺名：{r.stdout!r} {r.stderr[-400:]}")
        self.assertEqual(r.returncode, 0, r.stderr[-800:])

    # ------------------------------------------------------------------
    # 3. 晚查找机制（routes 取的就是这一份，打桩立即可见）
    # ------------------------------------------------------------------
    def test_appmod_resolves_alias_loaded_module(self):
        """`appmod()` 必须指回别名加载的那一份 app，而不是另执行一份 `web.app`。"""
        from web.routes import appmod
        with self.flask_app.test_request_context():
            self.assertIs(appmod(), self.webapp,
                          "routes 的晚查找没取到在跑的那一份 app 模块")

    def test_patched_app_attribute_is_visible_through_late_lookup(self):
        """代表性往返：patch app 模块属性 → routes 晚查找取到的是同一个被改过的值。"""
        from web.routes import appmod
        with self.flask_app.test_request_context(), \
                mock.patch.object(self.webapp, "ENV_FILE", os.path.join(self.tmp, "x.env")):
            self.assertEqual(appmod().ENV_FILE, os.path.join(self.tmp, "x.env"))
            self.assertEqual(appmod(), self.webapp)


MOVED_NAMES = (
    "_inline_md",
    "_render_md",
    "_read_doc_html",
    "_doc_page",
    "_DOC_FILES",
    "_SAFE_LINK_SCHEMES",
    "_LINK_RE",
    "email_domain_error",
    "icp_info",
    "police_info",
    "police_link",
    "site_description",
    "site_image",
    "edge_config",
    "edge_front_sec",
)


PURE_REEXPORTS_RENDER = ("_inline_md", "_render_md", "_DOC_FILES", "_SAFE_LINK_SCHEMES", "_LINK_RE")


APP_HELD_STATE_RENDER = ("ENV_FILE", "_REPO_ROOT", "_doc_cache")


class WebRenderSplitContractTest(unittest.TestCase):
    """以别名加载的 app 副本为靶子（routes 与测试实际看到的那一份）。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-render-split-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        cls._old_env = {k: os.environ.get(k) for k in
                        ("YIBAN_ENV_FILE", "YIBAN_DB_FILE", "YIBAN_STATE_DIR",
                         "YIBAN_ACCOUNTS_FILE", "YIBAN_ACCOUNTS_KEY")}
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_DB_FILE"] = os.path.join(cls.tmp, "yiban.db")
        os.environ["YIBAN_STATE_DIR"] = cls.tmp
        os.environ["YIBAN_ACCOUNTS_FILE"] = os.path.join(cls.tmp, "accounts.json")
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
        import db as _db
        cls._db = _db
        spec = importlib.util.spec_from_file_location(
            "webapp_render", os.path.join(BASE, "web", "app.py"))
        cls.webapp = importlib.util.module_from_spec(spec)
        sys.modules["webapp_render"] = cls.webapp
        with contextlib.suppress(Exception):
            spec.loader.exec_module(cls.webapp)

    @classmethod
    def tearDownClass(cls):
        if cls._db._conn is not None:
            with contextlib.suppress(Exception):
                cls._db._conn.close()
            cls._db._conn = None
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k, v in cls._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _write_env(self, body):
        with io.open(self.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n" + body)

    # ------------------------------------------------------------------
    # 1. 名字面
    # ------------------------------------------------------------------
    def test_every_moved_name_reachable_on_app_and_render(self):
        from web import render as render_mod
        missing_app = [n for n in MOVED_NAMES if not hasattr(self.webapp, n)]
        missing_render = [n for n in MOVED_NAMES if not hasattr(render_mod, n)]
        self.assertEqual(missing_app, [], "web.app 兼容面缺失迁出名")
        self.assertEqual(missing_render, [], "web/render.py 缺定义")

    def test_pure_reexports_are_same_object(self):
        from web import render as render_mod
        for name in PURE_REEXPORTS_RENDER:
            self.assertIs(getattr(self.webapp, name), getattr(render_mod, name),
                          f"{name} 应为同一个对象（web.app 只是再导出）")

    def test_forwarders_keep_original_call_arity(self):
        """routes / 既有测试按原实参个数调用，转发不得改签名。"""
        self._write_env("")
        self.assertIsInstance(self.webapp._read_doc_html("USER_AGREEMENT.md"), str)
        self.assertIsInstance(self.webapp._doc_page("t", "<p>b</p>"), str)
        self.assertEqual(self.webapp.icp_info(), "")
        self.assertEqual(self.webapp.edge_config(), (60, 60))
        self.assertIsNone(self.webapp.email_domain_error("a@qq.com"))

    # ------------------------------------------------------------------
    # 2. 转发注入 app 模块级状态（代表性打桩往返）
    # ------------------------------------------------------------------
    def test_env_file_stub_round_trip(self):
        """`web.app.ENV_FILE` 改写后，站点展示族必须读到新 .env（现取而非副本绑定）。"""
        self._write_env(
            "YIBAN_ICP_INFO= 京ICP备测试号 \n"
            "YIBAN_POLICE_INFO= 公安备测试号 \n"
            "YIBAN_POLICE_LINK=https://beian.example.gov.cn/p\n"
            "YIBAN_SITE_DESCRIPTION= 自定义摘要 \n"
            "YIBAN_SITE_IMAGE=https://img.example.com/a.png\n"
            "YIBAN_WINDOW_EDGE_FRONT_SEC=90\n"
            "YIBAN_WINDOW_EDGE_BACK_SEC=30\n"
            "YIBAN_EMAIL_DOMAIN_ALLOWLIST=qq.com\n"
        )
        with mock.patch.object(self.webapp, "ENV_FILE", self.env_file):
            self.assertEqual(self.webapp.icp_info(), "京ICP备测试号")
            self.assertEqual(self.webapp.police_info(), "公安备测试号")
            self.assertEqual(self.webapp.police_link(), "https://beian.example.gov.cn/p")
            self.assertEqual(self.webapp.site_description(), "自定义摘要")
            self.assertEqual(self.webapp.site_image(), "https://img.example.com/a.png")
            self.assertEqual(self.webapp.edge_config(), (90, 30))
            self.assertEqual(self.webapp.edge_front_sec(), 90)
            self.assertEqual(self.webapp.email_domain_error("a@qq.com"), None)
            self.assertIsNotNone(self.webapp.email_domain_error("a@163.com"))

    def test_site_image_rejects_non_https_round_trip(self):
        self._write_env("YIBAN_SITE_IMAGE=http://img.example.com/a.png\n")
        with mock.patch.object(self.webapp, "ENV_FILE", self.env_file):
            self.assertEqual(self.webapp.site_image(), "")

    def test_doc_root_and_cache_injected(self):
        """`_REPO_ROOT` 改写要生效、缓存要落在 web.app 的 `_doc_cache` 上。"""
        sub = os.path.join(self.tmp, "docs")
        os.makedirs(sub, exist_ok=True)
        with io.open(os.path.join(sub, "USER_AGREEMENT.md"), "w", encoding="utf-8") as f:
            f.write("# 用户协议\n\n正文。\n")
        with mock.patch.object(self.webapp, "_REPO_ROOT", sub):
            self.webapp._doc_cache.clear()
            html = self.webapp._read_doc_html("USER_AGREEMENT.md")
            self.assertIn("<h1>用户协议</h1>", html)
            self.assertIn("USER_AGREEMENT.md", self.webapp._doc_cache, "缓存应写回 web.app._doc_cache")
            key_first = self.webapp._doc_cache["USER_AGREEMENT.md"][0]
            self.webapp._read_doc_html("USER_AGREEMENT.md")
            self.assertEqual(self.webapp._doc_cache["USER_AGREEMENT.md"][0], key_first,
                             "文件未变更应命中缓存")
        self.webapp._doc_cache.clear()

    def test_doc_page_default_description_uses_app_state(self):
        """`_doc_page` 摘要留空时取 `site_description()`（同样受 ENV_FILE 打桩影响）。"""
        self._write_env("YIBAN_SITE_DESCRIPTION= 独立页摘要 \n")
        with mock.patch.object(self.webapp, "ENV_FILE", self.env_file):
            page = self.webapp._doc_page("用户协议", "<p>正文</p>")
        self.assertIn('content="独立页摘要"', page)

    def test_edge_config_patch_name_face(self):
        """`mock.patch.object(webapp, "edge_config")` 仍是可打桩的名字面。"""
        with mock.patch.object(self.webapp, "edge_config", return_value=(0, 0)):
            self.assertEqual(self.webapp.edge_config(), (0, 0))

    # ------------------------------------------------------------------
    # 3/4. 别名加载安全与状态归属
    # ------------------------------------------------------------------
    def test_alias_loaded_app_shares_render_module(self):
        from web import render as render_mod
        self.assertIs(self.webapp._render, render_mod,
                      "别名加载的 app 副本必须复用同一个 web.render（不得再执行一份）")

    def test_render_module_holds_no_app_state(self):
        from web import render as render_mod
        for name in APP_HELD_STATE_RENDER:
            self.assertFalse(hasattr(render_mod, name),
                             f"render 层不得持有 {name}（应由 web.app 调用时刻注入）")

    def test_render_source_does_not_import_app(self):
        path = os.path.join(BASE, "web", "render.py")
        with io.open(path, encoding="utf-8") as f:
            src = f.read()
        self.assertIsNone(re.search(r"^\s*(?:import|from)\s+web\.app\b", src, re.M),
                          "render 层禁止 import web.app（别名加载会执行副本模块）")

    def test_importing_render_does_not_execute_web_app(self):
        """全新解释器里只 import web.render：不得把 web.app 拉进 sys.modules。"""
        code = (
            "import sys;"
            f"sys.path[:0]=[{os.path.join(BASE, 'scripts')!r}, {BASE!r}];"
            "import web.render;"
            "print('APP_LOADED' if 'web.app' in sys.modules else 'RENDER_ONLY_OK')"
        )
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        r = subprocess.run([sys.executable, "-c", code], cwd=BASE, env=env,
                           capture_output=True, text=True, timeout=60)
        self.assertIn("RENDER_ONLY_OK", r.stdout, r.stderr[-600:])
        self.assertEqual(r.returncode, 0, r.stderr[-600:])
