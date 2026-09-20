# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""账号数据 / 校验队列 / 现场实测三族拆分契约：`web/services/{accounts_data,verify_queue,measure}.py` 是真源。

账号读取与展示、字段校验与口令策略、在线校验的执行闸门与异步任务、现场实测的状态文件与
冷却判定从 `web/app.py` 迁入 `web/services/`，app.py 只保留名字面与少量注入转发。本文件
钉住五件事，任一件破了都会**静默**改变行为：

1. **名字面完整**：迁移名在 `web.app` 与所属服务模块上都可达（routes 经
   `sys.modules[current_app.import_name].<名字>` 晚查找取用）；`m.verify_jobs` 仍是
   `yiban.attempt.jobs` 真源，而新模块**不占用**这个名字。
2. **转发注入 app 模块级状态**：账号读入口 `load_accounts`、签到窗口 `_sign_window`、
   掐头去尾 `edge_config`、`.env` 路径与读取器、整数配置读取器、状态目录 `STATE_DIR`、
   原子落盘 `_atomic_write`、外呼席位 `_verify_sem`、配额判定 `_verify_attempt_allowed`、
   只读验证 `_verify_account_clean` 都留在 web.app 且会被测试改写（直接赋值 /
   `mock.patch.object`），服务层另存一份绑定会让改写静默失效——故转发必须在调用时刻
   现取后传入。
3. **行为逐字不变**：掩码口径、校验错误文案、idx 错位守卫（含 fail_closed）、注销冷却
   的两种时间格式、口令策略判定与文案、实测冷却判据与状态文件容错、校验闸门的
   "先抢席位后扣配额"、超龄收口的 reject_status、队列上限阈值都保持原样。
4. **别名加载安全**：服务层不导入 `web.app`（普通 import 会在别名加载的测试进程里再执行
   一份 app.py 副本）；别名加载的 app 副本与 `web.services.*` 共享同一实现。
5. **服务层不持有 app 状态**：各模块不得出现它应由 web.app 调用时刻注入的那些名字，
   否则第 2 条的"另存绑定"会以更隐蔽的形态回归。
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
import unittest
from datetime import datetime, timedelta
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TEST_KEY = "a" * 64
PHONE = "13800138000"
OTHER = "13900139000"

#: 迁入 `web/services/accounts_data.py` 的名字（实现唯一在那里；web.app 上必须是可达的兼容面）
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

#: 迁入 `web/services/verify_queue.py` 的名字
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

#: 迁入 `web/services/measure.py` 的名字
MOVED_MEASURE = (
    "_measure_state_path",
    "_read_measure_state",
    "_write_measure_state",
    "_measure_cooldown_remaining",
    "_pick_measure_account",
    # 只被迁出族使用，随所属域搬
    "MEASURE_STATE_FILE",
)

#: 纯再导出（不读 app 模块级状态）：两边必须是同一对象
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

#: 必须由转发包装注入 app 状态的名字（不能是纯再导出）
FORWARDED_ACCOUNTS = ("_slot_to_label", "_estimate_slot")
FORWARDED_VERIFY = ("run_verify_with_gate", "verify_async_enabled", "_account_verify_enabled")
FORWARDED_MEASURE = ("_measure_state_path", "_write_measure_state")

#: 服务层不得持有的 web.app 模块级名字（各模块按自身依赖列举；`_file_lock` 是
#: `web/services/locks.py` 的真源，故不在名单里）
APP_HELD_STATE = {
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

SERVICE_SOURCES = (
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
        for rel, names in APP_HELD_STATE.items():
            mod = importlib.import_module(
                rel.replace("web/", "web.").replace("/", ".")[:-3])
            for name in names:
                self.assertFalse(hasattr(mod, name),
                                 f"{rel} 不得持有 {name}（应由 web.app 调用时刻注入）")

    def test_service_sources_do_not_import_app(self):
        for rel in SERVICE_SOURCES:
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
