# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""web 应用工厂与各域服务层的**行为**回归。

标签：F · 前端与界面守卫
对应实现：`web/routes/*`、`web/services/*`、`web/render.py`、`web/security.py`。
依赖：纯本地——importlib 装载 `web/app.py` 与各真源模块；不起常驻服务器、无需 node、不联网。

各域类保留的是**行为**用例，例如：账号域的口径（脱敏、口令策略文案、席位/配额闸门、
`_reject_account` 的 CAS 写回、席位句柄取用）；env 域的 `.env` 往返（注释与无关行保留、
整数读写、密钥一次生成、清单迁移写回）；logs 域的解析（日期校验、倒读、按日过滤、
旧版符号回查、`sign_status` 三段端点切换与文案）；notify 域的容量公式与手动签到收尾
语义、子进程等待；security 域的信任代理取 IP、口令策略阈值、会话绝对过期、计数器语义；
以及 `StaleIdxGuardE2ETest` 的 fail-closed 409。

2026-10-16 饮食批：原四大块**拆分机制**断言整族删除——
① `*_stub_reaches_*` / `*_assignment_reaches_*`：把 `web.app` 的模块级属性打桩或赋值后
断言服务层"看到新值"。这钉的是**转发写法**（晚查找 `m.<名字>` 而非直接导入），不是行为：
服务层改成直接导入则行为一字不变而它们集体转红。
② `AppNameSurfaceContractTest` 的名字面清单、`web/routes/*` 不得 `import web.app` 的正则、
别名加载后 `sys.modules` 的检查。
③ `BoundaryMechanismLedgerTest` 的"复制头通用正确性"总账（`is` 同对象、转发器 arity）。
④ `WebRenderSplitContractTest` 的同类面。
理由见 AGENTS §一.6a（测不变式，不测编舞）。本批删除的断言原本钉住的性质，
凡属行为者已有其它判据：窗口端点切换、脱敏、口令阈值等由本文件保留用例钉住
（已用突变抽验：把 `sign_status` 的窗口端点判据改坏 ⇒ 保留用例转红）。
"""
import ast
import contextlib
import functools
import importlib.util
import io
import json
import os
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
from werkzeug.security import generate_password_hash

from yiban.core import clock
from yiban.core import status as yiban_status
from yiban.core.masking import mask_email_local

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


TEST_KEY = "a" * 64


PHONE = "13800138000"


OTHER = "13900139000"


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

    def test_estimate_slot_reads_ledger_via_app(self):
        """`_estimate_slot` 的取值来源是台账 `sign_tasks.run_at`（与执行同源）。

        stub 台账读口 `db.task_run_at_by_phone` 必须换掉结果——网页侧不再自算窗口/块容/
        分布（那正是与 v3 执行计划分叉的旧算法，工单 m9bi）。
        """
        day = clock.now().strftime("%Y-%m-%d")
        with mock.patch.object(self.webapp.db, "task_run_at_by_phone",
                               return_value={PHONE: f"{day} 07:20:00"}) as spy:
            got = self.webapp._estimate_slot(PHONE)
        self.assertEqual(spy.call_count, 1, "转发必须现取台账（服务层不自持一份快照）")
        self.assertEqual(got[0], "07:20", "estimated 必须等于台账 run_at 的时分")
        # 打桩换成"台账没有这个号" → （当日无计划行）返回「待生成」，不回退任何钟点
        with mock.patch.object(self.webapp.db, "task_run_at_by_phone", return_value={}):
            self.assertEqual(self.webapp._estimate_slot(PHONE), (None, "待生成"))


    def test_estimate_slot_no_plan_returns_pending_generation(self):
        """fail-closed：台账读不通（`None` 哨兵）时也返回「待生成」，绝不回退旧算法。

        `None` 与空映射同一失败方向——页面标"未知"，不得编造钟点，也不得回退 v2 派生。
        """
        with mock.patch.object(self.webapp.db, "task_run_at_by_phone", return_value=None):
            self.assertEqual(self.webapp._estimate_slot(PHONE), (None, "待生成"))


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


    # ------------------------------------------------------------------
    # 3. 行为往返（口径逐字不变）
    # ------------------------------------------------------------------
    def test_mask_account_masking_and_fields(self):
        acc = {"phone": PHONE, "password": "pw", "phone_code": "code",
               "name": "小明", "owner": "user@example.com"}
        view = self.webapp.mask_account(acc, 0)
        self.assertEqual(view["phone"], "138****8000")
        self.assertEqual(view["owner"], "use***@example.com")
        self.assertEqual(view["owner_display"], mask_email_local("user"),
                         "MF-49：归属展示名只出遮罩后的本地部")
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
        self.assertEqual(od("someone@x.io"), mask_email_local("someone"),
                         "MF-49：本地部遮罩后再下发（前端不得自拆）")
        self.assertEqual(od("plain"), mask_email_local("plain"))

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
        """`_reject_account` 的两向报级与 CAS 写回口径。

        "缺任务上下文"分两档，**不得合并**：`account_id is None` 是设计内路径（编辑
        探针刻意不绑账号 id，见 `start_edit_probe`），按 INFO 报；`account_id` 有值而
        `prev_status` 空才是真缺陷（任务上下文丢失），按 ERROR 报。两档都零写回。
        """
        with mock.patch.object(self.webapp.logger, "error") as err_log, \
                mock.patch.object(self.webapp.logger, "info") as info_log, \
                mock.patch.object(self.webapp.db, "update_account_status_if") as upd:
            # ① 设计内（编辑探针不绑账号 id）→ INFO 留痕、零写回、不得报 ERROR
            self.webapp._reject_account(PHONE, "原因", None, "pending")
            self.assertEqual(upd.call_count, 0, "缺任务上下文时不得改账号状态")
            self.assertEqual(info_log.call_count, 1, "设计内探针路径必须按 INFO 留痕")
            self.assertEqual(err_log.call_count, 0, "设计内路径不得按 ERROR 报")
            # ② 真缺陷（有账号 id 而无 prev_status）→ ERROR 留痕、零写回
            self.webapp._reject_account(PHONE, "原因", 5, "")
            self.assertEqual(upd.call_count, 0, "缺 prev_status 时不得改账号状态")
            self.assertEqual(err_log.call_count, 1, "真缺上下文必须按 ERROR 留痕")
            self.assertEqual(info_log.call_count, 1, "真缺陷不得被压成 INFO")
            # ③ 上下文完整：按 expect_status 做 CAS，空原因落默认文案
            self.webapp._reject_account(PHONE, "", 5, "pending")
            self.assertEqual(upd.call_args.args,
                             (5, "rejected", "pending", "在线校验未通过"),
                             "空原因落默认文案，且按 expect_status 做 CAS")
            # ④ CAS 失配（账号已被人工改动）→ INFO 留痕，不覆盖人工决定
            upd.return_value = False
            self.webapp._reject_account(PHONE, "原因", 5, "pending")
            self.assertEqual(info_log.call_count, 2, "未写回（已人工变更）必须留痕")

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
    def test_estimate_slot_dist_and_order_do_not_change_the_source(self):
        """分布/排序键不再影响预计时段：取值只来自台账，与 v2 自算几何解耦。

        旧实现按 `YIBAN_SIGN_DIST` / `YIBAN_SIGN_ORDER` 分支自算钟点——正是与 v3 执行
        计划分叉的那条路（工单 m9bi）。置位这两个键不得改变 `_estimate_slot` 的结果。
        """
        day = clock.now().strftime("%Y-%m-%d")
        envs = ({"YIBAN_SIGN_DIST": "normal"}, {"YIBAN_SIGN_DIST": "uniform"},
                {"YIBAN_SIGN_ORDER": "random"})
        with mock.patch.object(self.webapp.db, "task_run_at_by_phone",
                               return_value={PHONE: f"{day} 06:41:00"}):
            want = self.webapp._estimate_slot(PHONE)
            self.assertEqual(want[0], "06:41")
            for env in envs:
                with mock.patch.object(self.webapp, "read_env", return_value=env):
                    self.assertEqual(self.webapp._estimate_slot(PHONE), want,
                                     f"{env} 不得改变预计时段来源")


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


    def test_executors_window_defaults_match_engine_window(self):
        """`.env` 未写裁剪键时执行体页与引擎同窗：缺省 60s（原实现缺省 0 → 宽 1 分钟）。

        示例 `.env` 把两个裁剪键注释掉，故这条缺省路径就是生产默认形态：缺省 0 会让
        页面上显示的窗口、容量换算的分母与"窗口内不做实测"的拦截都比引擎宽 1 分钟。
        """
        import yiban.core.window as yb_window
        self._write_raw("YIBAN_SIGN_START=06:30\nYIBAN_SIGN_END=07:50\n")
        with mock.patch.object(self.webapp, "ENV_FILE", self.env_file):
            win = self.webapp._executors_window()
        engine = yb_window.bounds({"sign_start": (6, 30), "sign_end": (7, 50),
                                   "edge_front_sec": 60, "edge_back_sec": 60})
        self.assertEqual((win.front_sec, win.back_sec), (60, 60))
        self.assertEqual((win.lo_min, win.hi_min), (engine.lo_min, engine.hi_min))
        self.assertEqual(win.full_sec(), 78 * 60)


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


    # ------------------------------------------------------------------
    # 3. 行为往返（口径逐字不变）
    # ------------------------------------------------------------------
    def test_log_line_visibility_rules(self):
        vis = self.webapp._log_line_visible
        for logger_name in ("yiban", "yiban.client", "yiban.platform",
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
        today = clock.today()
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

    def test_legacy_daily_symbols_all_decode_to_a_real_status(self):
        """`SYMBOL` 全表的**每个**符号，回退反查都必须解出同符的状态码（不得落 `pending`）。

        反查表原先是手抄的三项（✅/❌/➖），写入侧（`runner._write_sign_daily`）却写六项。
        手抄子集随写入侧扩表静默陈旧时，多出的符号（🚫 no_position、🕓 supplementing）
        解不出，一律落 `pending`——日历把"已跳过/补签中"显示成"待签"。本门按**行为**钉住
        覆盖面：把这些符号逐个写进 sign-daily，核每个解出的状态码属于该符号的状态码集。

        覆盖面**取自 `yiban.status.SYMBOL` 全表**，不手抄一份子集：手抄的那份会在写入侧
        扩表时静默漏格——正是本门要防的形状，写成手抄就成了同病。
        """
        date = "2026-09-20"
        # 同一符号可能属多个状态码（✅ = success / already）：按符号分组，解出的码落组内即可。
        # 本表**不含** ⏳（`STATUS_PENDING` 不在 `SYMBOL` 里），故"落组内"已等价于
        # "不等于 `pending`"——漏格时该符号解成 `pending`，而没有任何组含 `pending`。
        # 若日后把 `pending` 并入 `SYMBOL`，这条等价即失效，本门须补一条"不等于 pending"。
        by_symbol = {}
        for code, sym in yiban_status.SYMBOL.items():
            by_symbol.setdefault(sym, set()).add(code)
        rows = {"1380013%04d" % i: sym for i, sym in enumerate(sorted(by_symbol))}
        with io.open(os.path.join(self.tmp, f"sign-daily-{date}.json"), "w",
                     encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False)
        got = self.webapp.load_sign_state(date)
        self.assertEqual(len(got), len(rows), "每个符号都要解出一条记录")
        for phone, sym in rows.items():
            # 漏格时该符号解成 `pending`，而 `pending` 只同符 ⏳ ⇒ 除 ⏳ 外一律当场翻红
            self.assertIn(got[phone]["status"], by_symbol[sym],
                          "符号 %s（%s）解出的状态码不属于它：%r"
                          % (sym, phone, got[phone]["status"]))

    def test_mask_log_phones_masks_all_bare_11_digits(self):
        mask = self.webapp._mask_log_phones
        self.assertEqual(mask("[13800138000] ✅ 签到成功"), "[138****8000] ✅ 签到成功")
        self.assertEqual(mask("[13800138000][13900139000] 两个"), "[138****8000][139****9000] 两个")
        self.assertEqual(mask("[1380013800] 少一位"), "[1380013800] 少一位")
        # 中文逗号分隔的裸号此前漏过（展示层只认方括号形态），必须一并遮住
        self.assertEqual(mask("账号: 13800138000"), "账号: 138****8000")
        # 坐标/普通数字不得误伤
        self.assertEqual(mask("定位 118.88459277562808"), "定位 118.88459277562808")
        self.assertEqual(mask("无号码"), "无号码")

    def test_cred_paused_phones_and_clear_fuse_pause(self):
        import json

        from yiban.adapters.statefiles import cred_state
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
        import yiban.core.window as yb_window
        bounds = yb_window.bounds({"sign_start": (6, 30), "sign_end": (7, 50),
                                   "edge_front_sec": 0, "edge_back_sec": 0})
        inside = self.webapp._in_sign_window
        self.assertFalse(inside(bounds, datetime(2026, 9, 16, 6, 29, 59)))
        self.assertTrue(inside(bounds, datetime(2026, 9, 16, 6, 30, 0)))
        self.assertTrue(inside(bounds, datetime(2026, 9, 16, 7, 50, 0)))
        self.assertFalse(inside(bounds, datetime(2026, 9, 16, 7, 50, 1)))
        self.assertFalse(inside(bounds, datetime(2026, 9, 16, 23, 0, 0)))

    def test_in_run_period_combines_window_and_gates(self):
        import yiban.core.window as yb_window
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
        """三段文案与配色（含周末短路）按**有效**窗口端点出字。

        默认窗口 06:30~07:50、前后各裁 60s ⇒ 有效端点 06:31 / 07:49；文案里的钟点
        必须与引擎实际开关点同源（旧实现按原始配置说"06:30 开始 / ~07:50 结束"，
        与引擎 06:31 开、07:49 停手分叉 1 分钟）。
        """
        self._write_raw("YIBAN_SIGN_START=06:30\nYIBAN_SIGN_END=07:50\n")
        status = self.webapp.sign_status
        self.assertEqual(status(datetime(2026, 9, 16, 6, 0)),
                         ("未到签到时间（06:31 开始）", "#7aa2f7"))
        self.assertEqual(status(datetime(2026, 9, 16, 7, 0)),
                         ("签到窗口进行中（~07:49 结束）", "#9ece6a"))
        self.assertEqual(status(datetime(2026, 9, 16, 8, 0)),
                         ("今日签到已结束", "#e0af68"))
        self.assertEqual(status(datetime(2026, 9, 19, 7, 0)),
                         ("今日无需打卡（周六）", "#a1a1aa"))
        self.assertEqual(status(datetime(2026, 9, 20, 7, 0)),
                         ("今日无需打卡（周日）", "#a1a1aa"))
        self._write_raw("YIBAN_SATURDAY_SIGN=1\nYIBAN_SIGN_START=06:30\nYIBAN_SIGN_END=07:50\n")
        self.assertEqual(status(datetime(2026, 9, 19, 7, 0)),
                         ("签到窗口进行中（~07:49 结束）", "#9ece6a"))

    def test_sign_status_switches_exactly_at_effective_endpoints(self):
        """三段切换点逐分钟钉住：06:30 未到、06:31 进行中、07:49 进行中、07:50 已结束。

        边界值显式回归：判定与文案同取有效端点，任一漂回原始窗口（06:30 / 07:50）即红。
        """
        self._write_raw("YIBAN_SIGN_START=06:30\nYIBAN_SIGN_END=07:50\n"
                        "YIBAN_WINDOW_EDGE_FRONT_SEC=60\nYIBAN_WINDOW_EDGE_BACK_SEC=60\n")
        status = self.webapp.sign_status
        self.assertEqual(status(datetime(2026, 9, 16, 6, 30))[0], "未到签到时间（06:31 开始）")
        self.assertEqual(status(datetime(2026, 9, 16, 6, 31))[0], "签到窗口进行中（~07:49 结束）")
        self.assertEqual(status(datetime(2026, 9, 16, 7, 49))[0], "签到窗口进行中（~07:49 结束）")
        self.assertEqual(status(datetime(2026, 9, 16, 7, 50))[0], "今日签到已结束")

    def test_sign_status_clamped_window_uses_window_endpoints(self):
        """缓冲过大被收缩：文案取收缩后的有效端点（07:01 / 07:09），窗口本身仍是 07:00~07:10。

        配置 07:00~07:10、前后各 300s ⇒ 缓冲收缩为各 60s。按原始裁剪直算会得到空
        区间；文案必须取实际生效的开关点。
        """
        self._write_raw("YIBAN_SIGN_START=07:00\nYIBAN_SIGN_END=07:10\n"
                        "YIBAN_WINDOW_EDGE_FRONT_SEC=300\nYIBAN_WINDOW_EDGE_BACK_SEC=300\n")
        status = self.webapp.sign_status
        self.assertEqual(status(datetime(2026, 9, 16, 6, 30)),
                         ("未到签到时间（07:01 开始）", "#7aa2f7"))
        self.assertEqual(status(datetime(2026, 9, 16, 7, 5)),
                         ("签到窗口进行中（~07:09 结束）", "#9ece6a"))

    def test_sign_status_reports_window_unavailable(self):
        """窗口本身不可用（已回退默认）：状态文案整句改为"配置异常、已按 X~Y 运行"。

        此时管理员设的窗口根本没被采用，继续报三段状态等于谎报；这句话也是页面上唯一
        能看见该异常的出口（只写日志+邮件不够）。
        """
        import yiban.core.window as yb_window
        dead = yb_window.bounds({"sign_start": (7, 0), "sign_end": (6, 0),
                                 "edge_front_sec": 60, "edge_back_sec": 60})
        self.assertTrue(dead.fell_back)
        with mock.patch.object(self.webapp, "sign_window_bounds", return_value=dead):
            text, color = self.webapp.sign_status(datetime(2026, 9, 16, 7, 0))
        self.assertEqual(text, "配置异常：签到窗口不可用，已按 06:31~07:49 运行")
        self.assertEqual(color, "#e0af68")

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


    # ------------------------------------------------------------------
    # 2. 打桩往返（注入的是调用时刻的 app 侧名字）
    # ------------------------------------------------------------------


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
        """回环首跳采信 XFF；非回环首跳丢弃；无 XFF 时回落 remote_addr。"""
        app = self.flask_app
        with app.test_request_context("/", environ_base={"REMOTE_ADDR": "127.0.0.1"},
                                      headers={"X-Forwarded-For": "203.0.113.9, 10.0.0.1"}):
            self.assertEqual(self.webapp._client_ip(), "203.0.113.9")
        with app.test_request_context("/", environ_base={"REMOTE_ADDR": "::1"},
                                      headers={"X-Forwarded-For": "203.0.113.9"}):
            self.assertEqual(self.webapp._client_ip(), "203.0.113.9",
                             "IPv6 回环首跳同样是可信第一跳")
        with app.test_request_context("/", environ_base={"REMOTE_ADDR": "203.0.113.9"},
                                      headers={"X-Forwarded-For": "198.51.100.7"}):
            self.assertEqual(self.webapp._client_ip(), "203.0.113.9",
                             "非可信首跳的 XFF 必须被忽略（不可伪造）")
        with app.test_request_context("/", environ_base={"REMOTE_ADDR": "::1"}):
            self.assertEqual(self.webapp._client_ip(), "::1")

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


# （名字面计数钉已随流程门禁整族裁撤，缩减批 6a：名字面漂移的可见性由
# test_every_m_route_name_reachable_on_app_module 的可达性断言承接。）


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


    def test_site_image_rejects_non_https_round_trip(self):
        self._write_env("YIBAN_SITE_IMAGE=http://img.example.com/a.png\n")
        with mock.patch.object(self.webapp, "ENV_FILE", self.env_file):
            self.assertEqual(self.webapp.site_image(), "")


    # ------------------------------------------------------------------
    # 3/4. 别名加载安全与状态归属
    # ------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 6c3-B 机制总账
# ---------------------------------------------------------------------------
# 原六个域契约类逐域复制的 38 条机制头（名字面 / 纯再导出 / 转发不退化 / 调用
# 签名 / 别名共享 / 状态归属 / 导入形状 / 执行副作用）收敛为下面 8 条参数化用例。
# 钉的是**通用**正确性："转发落到真状态"（app 模块级名字在调用时刻现取）、别名
# 加载的 app 副本与各真源同一、拆分模块不得把第二份 app 拉进进程。各域**行为**
# 用例（*_stub_reaches_* 等）仍留在原域类一字未动；security/render 的别名安全
# 变体（*_security_module / *_render_module 四条）亦原样保留。
# 新增域只需往各表登记一行 + 一张名字表。


class StaleIdxGuardE2ETest(unittest.TestCase):
    """M91/F60：位置寻址的管理面写口在请求不带 phone 时 fail-closed（409）。

    旧行为（`_stale_idx_guard` 默认 fail_open）会在视图漂移后静默作用到漂移后的
    另一行。这里从 HTTP 层钉住 review/delete/purge 三条出口必须 409，且目标行未变；
    另钉批量口 phones 与 ids 长度不等时同样 409（不得静默跳过比对）。
    """

    ADMIN_PASS = "TestPass1234!"

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-staleidx-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        cls.accounts_file = os.path.join(cls.tmp, "accounts.json")
        cls._old_env = {k: os.environ.get(k) for k in (
            "YIBAN_ENV_FILE", "YIBAN_DB_FILE", "YIBAN_STATE_DIR", "YIBAN_ACCOUNTS_FILE",
            "YIBAN_ACCOUNTS_KEY", "YIBAN_USERS_FILE", "YIBAN_LOG_FILE")}
        with io.open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
                    "YIBAN_ADMIN_USER=admin\n"
                    f"YIBAN_ADMIN_PASSWORD={cls.ADMIN_PASS}\n")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_DB_FILE"] = cls.db_file
        os.environ["YIBAN_STATE_DIR"] = cls.tmp
        os.environ["YIBAN_ACCOUNTS_FILE"] = cls.accounts_file
        os.environ["YIBAN_USERS_FILE"] = os.path.join(cls.tmp, "users.json")
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
        os.environ["YIBAN_LOG_FILE"] = os.path.join(cls.tmp, "sign.log")
        import db as _db
        cls.db = _db
        spec = importlib.util.spec_from_file_location(
            "webapp_staleidx", os.path.join(BASE, "web", "app.py"))
        cls.webapp = importlib.util.module_from_spec(spec)
        sys.modules["webapp_staleidx"] = cls.webapp
        spec.loader.exec_module(cls.webapp)

    @classmethod
    def tearDownClass(cls):
        if cls.db._conn is not None:
            with contextlib.suppress(Exception):
                cls.db._conn.close()
            cls.db._conn = None
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k, v in cls._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def setUp(self):
        if self.db._conn is not None:
            with contextlib.suppress(Exception):
                self.db._conn.close()
            self.db._conn = None
        for suffix in ("", "-wal", "-shm"):
            p = self.db_file + suffix
            if os.path.exists(p):
                os.remove(p)
        with io.open(self.accounts_file, "w", encoding="utf-8") as f:
            f.write("[]")
        self.db.init_db(self.db_file, migrate_from=self.accounts_file, env_file=self.env_file)
        for name, phone, owner in (("待审核", "13800138101", "u1@test.local"),
                                   ("在效", "13800138102", "u2@test.local"),
                                   ("已删", "13800138103", "u3@test.local")):
            self.db.add_account({"name": name, "phone": phone, "password": "pw",
                                 "status": "pending", "owner": owner})
        rows = self.db.load_accounts()
        self.idx = {a.get("name"): i for i, a in enumerate(rows)}
        self.db.set_account_deleted(
            rows[self.idx["已删"]]["id"], 1, "2026-09-01 00:00:00", deleted_by="admin")

    def _admin(self):
        c = self.webapp.create_app().test_client()
        r = c.post("/api/login", json={"username": "admin", "password": self.ADMIN_PASS})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        t = c.get("/api/me").get_json()["csrf_token"]
        return c, {"X-CSRF-Token": t}

    def _snapshot(self):
        return {a.get("name"): (a.get("status"), bool(a.get("deleted")))
                for a in self.db.load_accounts()}

    def test_review_without_phone_is_409_and_row_unchanged(self):
        before = self._snapshot()
        c, h = self._admin()
        r = c.post(f"/api/accounts/{self.idx['待审核']}/review",
                   json={"action": "approve"}, headers=h)
        self.assertEqual(r.status_code, 409, r.get_data(as_text=True))
        self.assertIn("账号列表已变化", r.get_json()["error"])
        self.assertEqual(self._snapshot(), before)

    def test_delete_without_phone_is_409_and_row_unchanged(self):
        before = self._snapshot()
        c, h = self._admin()
        r = c.delete(f"/api/accounts/{self.idx['在效']}", headers=h)
        self.assertEqual(r.status_code, 409, r.get_data(as_text=True))
        self.assertEqual(self._snapshot(), before)

    def test_purge_without_phone_is_409_and_row_unchanged(self):
        before = self._snapshot()
        c, h = self._admin()
        r = c.post(f"/api/accounts/{self.idx['已删']}/purge",
                   json={"confirm_password": self.ADMIN_PASS, "confirm_delay_ack": True},
                   headers=h)
        self.assertEqual(r.status_code, 409, r.get_data(as_text=True))
        self.assertEqual(self._snapshot(), before)

    def test_batch_phones_ids_length_mismatch_is_409(self):
        before = self._snapshot()
        c, h = self._admin()
        r = c.post("/api/accounts/batch", json={
            "action": "approve",
            "ids": [self.idx["待审核"]],
            "phones": ["138****8101", "139****9999"],
        }, headers=h)
        self.assertEqual(r.status_code, 409, r.get_data(as_text=True))
        self.assertIn("账号列表已变化", r.get_json()["error"])
        self.assertEqual(self._snapshot(), before)

    def test_batch_without_phones_is_fail_closed(self):
        """M91：完全不带 phones（None）也 409，不得静默按 idx 作用。

        此前只判 `len(phones) != len(ids)`；phones 缺失（None）时两个内联分支都不进，
        请求按 idx 直接落到不可逆的 purge → 静默清除。fail-closed 后缺令牌即 409。

        purge 的二次鉴权门在 phones 校验**之前**（刻意留在 _file_lock 之外），故本条
        必须带 confirm_password/confirm_delay_ack 才能把请求送到 phones 校验；不带
        口令的 purge 仍是 400 password_required（另一条既有用例）。
        """
        before = self._snapshot()
        c, h = self._admin()
        # 1) 不可逆 purge：带口令但不带 phones → 409，已删行仍在
        r = c.post("/api/accounts/batch", json={
            "action": "purge",
            "ids": [self.idx["已删"]],
            "confirm_password": self.ADMIN_PASS,
            "confirm_delay_ack": True,
        }, headers=h)
        self.assertEqual(r.status_code, 409, r.get_data(as_text=True))
        self.assertIn("账号列表已变化", r.get_json()["error"])
        self.assertEqual(self._snapshot(), before)
        # 2) 非门禁动作 delete：不带 phones 同样 409，无行被软删
        r2 = c.post("/api/accounts/batch", json={
            "action": "delete",
            "ids": [self.idx["在效"]],
        }, headers=h)
        self.assertEqual(r2.status_code, 409, r2.get_data(as_text=True))
        self.assertIn("账号列表已变化", r2.get_json()["error"])
        self.assertEqual(self._snapshot(), before)

    def test_ip_store_trim_runs_inside_rate_lock(self):
        """N3：my_api.py / pages.py 两处 _ip_store_trim 必须在 _rate_lock 内调用。

        `_ip_store_trim` 在超限时会 `store.items()` 遍历；锁外调用时并发插入会让
        迭代中的 dict 变尺寸 → RuntimeError → 500（locks.py:37 的不变量是"调用方持锁、
        trim 自身不取锁"）。这里用确定性替身 `_TrimProbeStore` 复现：未持锁即视为
        "并发写者正在插入"（边迭代边插入，必抛 RuntimeError）；持锁则正常迭代并记
        saw_locked。把 `_IP_STORE_LIMIT` 临时钳到 0 强制进入清理分支（不依赖 len>10000）。
        """
        import web.security as _sec
        app = self.webapp.create_app()
        c = app.test_client()
        self.db.create_user("trim@test.local",
                            generate_password_hash("secret1"))
        r = c.post("/api/login", json={"username": "trim@test.local", "password": "secret1"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        token = c.get("/api/me").get_json()["csrf_token"]
        with mock.patch.object(_sec, "_IP_STORE_LIMIT", 0):
            # 1) my_api.py 的判重预检 trim：提交一个在册手机号即命中该分支
            dup_store = _TrimProbeStore({"seed": (0, time.time())})
            app.extensions["yiban_dupcheck_limits"] = dup_store
            r = c.post("/api/my-accounts", json={
                "name": "dup", "phone": "13800138102", "password": "pw"},
                headers={"X-CSRF-Token": token})
            self.assertNotEqual(r.status_code, 500, r.get_data(as_text=True))
            self.assertTrue(dup_store.saw_locked, "my_api.py 的 trim 未在 _rate_lock 内调用")
            self.assertFalse(dup_store.saw_unlocked)
            # 2) pages.py 的登录页循环检测 trim：已登录 GET /login 触发
            loop_store = _TrimProbeStore({"1.2.3.4": (1, time.time())})
            app.extensions["yiban_login_loop"] = loop_store
            r2 = c.get("/login")
            self.assertNotEqual(r2.status_code, 500, r2.get_data(as_text=True))
            self.assertTrue(loop_store.saw_locked, "pages.py 的 trim 未在 _rate_lock 内调用")
            self.assertFalse(loop_store.saw_unlocked)


class _TrimProbeStore(dict):
    """`_ip_store_trim` 计数表替身：确定性暴露"trim 是否在 _rate_lock 内调用"。

    真实竞态 = trim 锁外遍历 `items()` 时另一线程插入键。这里把"未持锁"等价为
    "写者正在插入"：未持锁时边迭代边插入（dict 尺寸变化 → RuntimeError）；持锁时
    写者已被锁排除，正常迭代。saw_locked/saw_unlocked 供断言进入过临界区。
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.saw_locked = False
        self.saw_unlocked = False

    def items(self):
        from web.services import locks as _locks
        if _locks._rate_lock.locked():
            self.saw_locked = True
            return dict.items(self)
        self.saw_unlocked = True
        return self._racing()

    def _racing(self):
        for k, v in dict.items(self):
            dict.__setitem__(self, "__racing__", (1, 0.0))
            yield k, v


# ---------------------------------------------------------------------------
# 拆墙 W0：`web/app.py` 转发墙名集与真实读者对账
# ---------------------------------------------------------------------------
# 来历（工单 `128 P2`，施工方案 §1.2/§1.3/§3.1）：
# `web/app.py` 曾把各域实现的名字再导出，供路由经 `web.routes.appmod()` 按属性取用、
# 供测试按属性打桩。批 A1–A5 把实现迁走后，其中一批名字已**零读者**（纯转发壳），
# 本批（W0）删除。本门钉两条不变式，防"删了还在用的名"与"零读者名回流"。
#
# ① **读者可解析**：路由与测试经 app 别名读到的每个名字，都必须在 `web/app.py` 有绑定。
#    删掉一个还有读者的名字 ⇒ 红，点名 `文件:行`。
# ② **无零读者转发名**：带 `noqa: F401` 的每个 import 绑定名，至少要有一个读者。
#    豁免登记在 `_WALL_DEAD_F401_EXEMPT`（逐名附理由），不许空豁免。
#    批 3-a 的过渡集 `_WALL_DEAD_F401_BATCH3A`（21 名）已在批 3-b 与墙名同批删除，
#    故本门不再有那份过渡登记，也没有对应的腐烂断言（删名与删登记同批做）。
# 扫描面非空：绑定名、读者、F401 名集各设下限；扫空即红（AGENTS §30）。
#
# 边界：① 的别名取**规范别名**（`self.webapp` 一族），不把 `w`/`mod` 等易与其他模块
# 撞名的局部变量计入，避免误红；② 的读者面取**宽松别名**（多算读者），故它判出的
# "零读者"更可靠——漏判只让本门更松，不会误红。测试自身仍是最终兜底：删了在用的名字，
# 用例会以 `AttributeError` 响亮红。
_WALL_APP = os.path.join(BASE, "web", "app.py")
_WALL_ROUTES_DIR = os.path.join(BASE, "web", "routes")
_WALL_TESTS_DIR = os.path.join(BASE, "tests")

#: 测试里承载 `web.app` 模块对象的规范别名（现量：`self.webapp` 占绝大多数）。
_WALL_APP_ALIASES = frozenset(("self.webapp", "cls.webapp", "webapp"))

#: 宽松别名：再加少数把 app 模块装进局部变量的写法。只用于"零读者"判定。
_WALL_APP_ALIASES_WIDE = _WALL_APP_ALIASES | frozenset(("w", "web", "mod", "impl"))

#: F401 里"确实零读者但不删"的名字（显式豁免，逐条附理由）。W0 收口时为空
#: （现存 F401 名都有读者）。留零读者名的唯一合法路径是在这里逐条登记并写明理由。
#:
#: 现有两条来自 145-B `T1` 片：该片把口令哈希两名的读者迁到真实实现坐标
#: `werkzeug.security`（路由与测试各自直导入），于是这两个墙名在本片后**零读者**。
#: **本片不删墙名**（删名归 `145-E`），故按本门的豁免机制登记。这是**过渡态**：
#: `145-E` 删名时须同批删掉这两行——`test_exempt_names_are_still_f401` 钉住这一点。
_WALL_DEAD_F401_EXEMPT = {
    "generate_password_hash": (
        "145-B T1 已迁走**自有面**（tests/web/scripts）全部读者；墙名待 145-E 删除。"
        "**面外读者 0 处**：原 1 处（`frontend/e2e/server.py` 的 Playwright e2e 夹具）"
        "已于 2026-10-18 经用户授权由主会话处置——该夹具改直连 `werkzeug.security`，"
        "`_PWHASH_OUT_OF_FACE_REGISTRY` 同批清空。"),
    "check_password_hash": "145-B T1 已迁走自有面全部读者；墙名待 145-E 删除",
}


@functools.lru_cache(maxsize=None)
def _wall_app_bindings():
    """`web/app.py` 的模块级绑定名集合与带 `F401` 标注的 import 绑定名集合。"""
    with open(_WALL_APP, encoding="utf-8") as f:
        src = f.read()
    lines = src.splitlines()
    tree = ast.parse(src)
    bound, f401 = set(), set()
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                nm = a.asname or (a.name.split(".")[0] if isinstance(node, ast.Import) else a.name)
                bound.add(nm)
                if "F401" in lines[a.lineno - 1]:
                    f401.add(nm)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, ast.Assign):
            bound.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            bound.add(node.target.id)
    return bound, f401


@functools.lru_cache(maxsize=None)
def _wall_route_reads():
    """路由 `m.<名字>` 的读者：`(名字, 文件, 行号)` 列表。"""
    out = []
    for dirpath, dirs, files in os.walk(_WALL_ROUTES_DIR):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for fname in sorted(files):
            if not fname.endswith(".py"):
                continue
            path = os.path.join(dirpath, fname)
            with open(path, encoding="utf-8") as f:
                tree = ast.parse(f.read())
            aliases = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                    fn = getattr(node.value.func, "id", None) or getattr(node.value.func, "attr", None)
                    if fn in ("appmod", "_appmod"):
                        aliases.update(t.id for t in node.targets if isinstance(t, ast.Name))
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) \
                        and node.value.id in aliases and not node.attr.startswith("__"):
                    out.append((node.attr, os.path.relpath(path, BASE).replace("\\", "/"),
                                node.lineno))
    return out


#: 服务层里可以**经参数**收下 app 模块的形参名。批 3-b 实测：`web/services/device_refresh.py`
#: 的 `refresh_account_device(m, …)` 在 except 子句里读 `m.VerifyQuotaExceeded`——那种读法
#: 既不是 `appmod()` 别名（路由面）也不是测试别名，只看 routes/tests 会把它判成"零读者"
#: 并删掉**还有读者的名字**（删了 429 分支变 500，实测抓到）。故本门把服务层面的读法
#: 并入读者面：§29「门看的东西 ＝ 实际改的东西」。
_WALL_HOST_PARAMS = frozenset(("m",))
#: 服务层面（相对 BASE）。
_WALL_HOST_DIR = os.path.join(BASE, "web", "services")


def host_module_reads_in(src, rel):
    """一段服务层源码里"经宿主模块参数读 app 属性"的读者：`(名字, rel, 行号)`。

    别名＝`appmod()` 赋值名 ∪ 形参名 ∈ `_WALL_HOST_PARAMS`。纯函数，便于做突变验证。
    """
    tree = ast.parse(src)
    aliases = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            fn = getattr(node.value.func, "id", None) or getattr(node.value.func, "attr", None)
            if fn in ("appmod", "_appmod"):
                aliases.update(t.id for t in node.targets if isinstance(t, ast.Name))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = (list(node.args.posonlyargs) + list(node.args.args)
                    + list(node.args.kwonlyargs))
            aliases.update(a.arg for a in args if a.arg in _WALL_HOST_PARAMS)
    if not aliases:
        return []
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) \
                and node.value.id in aliases and not node.attr.startswith("__"):
            out.append((node.attr, rel, node.lineno))
    return out


@functools.lru_cache(maxsize=None)
def _wall_host_module_reads():
    """服务层里经宿主模块参数读 app 属性的读者：`(名字, 文件, 行号)` 列表。"""
    out = []
    for dirpath, dirs, files in os.walk(_WALL_HOST_DIR):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for fname in sorted(files):
            if not fname.endswith(".py"):
                continue
            path = os.path.join(dirpath, fname)
            with open(path, encoding="utf-8") as f:
                src = f.read()
            rel = os.path.relpath(path, BASE).replace("\\", "/")
            out.extend(host_module_reads_in(src, rel))
    return out


def _wall_declared_app_aliases(tree):
    """文件里显式声明的 app 模块别名：`import web.app as X` / `from web import app as X`。"""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name == "web.app":
                    out.add(a.asname or "web")
        elif isinstance(node, ast.ImportFrom) and node.module == "web":
            for a in node.names:
                if a.name == "app":
                    out.add(a.asname or "app")
    return out


@functools.lru_cache(maxsize=None)
def _wall_test_reads(aliases):
    """测试里经 app 别名读属性或打桩的读者：`(名字, 文件, 行号)` 列表。

    除入参的固定别名面外，逐文件并上**显式声明**的别名（`import web.app as w` 之类）——
    这类别名是精确的（绑定对象就是 app 模块），并上不引入误红。
    """
    out = []
    for dirpath, dirs, files in os.walk(_WALL_TESTS_DIR):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for fname in sorted(files):
            if not fname.endswith(".py"):
                continue
            path = os.path.join(dirpath, fname)
            with open(path, encoding="utf-8") as f:
                text = f.read()
            # 廉价预筛：没有任何 app 别名字样的测试文件不可能有 app 读者，跳过 AST 解析。
            # 三种别名来源（`webapp*` / `import web.app as X` / `appmod()`）都含这三个
            # 子串之一，故预筛不会漏掉任何受支持的别名形态（本门不支持的形态本就不算）。
            if not any(tok in text for tok in ("webapp", "web.app", "appmod")):
                continue
            tree = ast.parse(text)
            rel = os.path.relpath(path, BASE).replace("\\", "/")
            file_aliases = aliases | _wall_declared_app_aliases(tree)
            for node in ast.walk(tree):
                # 用 unparse 取基表达式文本：`self.webapp.<名字>` 的基是 Attribute 而非
                # Name，只认 Name 会漏掉绝大多数读者（现量口径）。
                if isinstance(node, ast.Attribute) and ast.unparse(node.value) in file_aliases:
                    out.append((node.attr, rel, node.lineno))
                if isinstance(node, ast.Call):
                    fn = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
                    if fn in ("patch", "object", "getattr", "setattr", "hasattr") and node.args:
                        if ast.unparse(node.args[0]) not in file_aliases:
                            continue
                        last = node.args[-1]
                        if isinstance(last, ast.Constant) and isinstance(last.value, str):
                            out.append((last.value.split(".")[-1], rel, node.lineno))
    return out


class ForwardingWallReaderGateTest(unittest.TestCase):
    """W0：`web/app.py` 转发墙的名集与真实读者对账（两条不变式，见本段模块注释）。"""

    def test_route_and_test_readers_are_all_bound(self):
        bound, _f401 = _wall_app_bindings()
        reads = (_wall_route_reads() + _wall_test_reads(_WALL_APP_ALIASES)
                 + _wall_host_module_reads())
        # 绑定面下限：用途只是"解析器或路径失效 ⇒ 红"。批 3-a 末（`e5aceb07`）实测 360，
        # 批 3-b 删 21 个零读者转发名、搬走 47 个真定义（其中 4 名改由新家导入）、
        # 去掉 2 个已无用的 stdlib 导入 ⇒ 本批实测 294，故下限由 300 下调为 250。
        self.assertGreaterEqual(len(bound), 250,
                                "web/app.py 绑定名过少——解析器或路径失效（扫空不得当绿）")
        self.assertGreaterEqual(len(reads), 1000,
                                "读者扫描面过小——别名探测失效（扫空不得当绿）")
        bad = [f"{rel}:{line}: {name}" for name, rel, line in reads if name not in bound]
        self.assertEqual(
            bad, [],
            "以下读者引用的名字在 web/app.py 已不存在（删了还在用的名字）：\n" + "\n".join(bad))

    def test_no_dead_f401_name_beyond_registered_exemptions(self):
        _bound, f401 = _wall_app_bindings()
        reads = (_wall_route_reads() + _wall_test_reads(_WALL_APP_ALIASES_WIDE)
                 + _wall_host_module_reads())
        self.assertGreaterEqual(len(f401), 50,
                                "F401 名集过小——注解解析失效（扫空不得当绿）")
        self.assertGreaterEqual(len(reads), 1000,
                                "读者扫描面过小——别名探测失效（扫空不得当绿）")
        read_names = {name for name, _, _ in reads}
        exempt = set(_WALL_DEAD_F401_EXEMPT)
        dead = sorted(n for n in f401 if n not in read_names and n not in exempt)
        self.assertEqual(dead, [],
                         "以下带 noqa: F401 的名字没有任何读者（零读者转发名）：" + ", ".join(dead))

    def test_host_module_reader_face_has_teeth(self):
        """服务层读者面有牙：经形参 `m` 的读法必须命中；无宿主参数的文件不得误命中。

        来历见 `_WALL_HOST_PARAMS` 的注释：批 3-b 删零读者名时，只看 routes/tests 面
        会把 `m.VerifyQuotaExceeded` 判成零读者并删掉——删了 429 分支变 500（实测抓到）。
        """
        n = chr(10)
        src = n.join(["def go(m, acc):",
                      "    try:",
                      "        return m.run_gated_call(acc)",
                      "    except m.VerifyQuotaExceeded:",
                      "        return None", ""])
        got = sorted(host_module_reads_in(src, "web/services/x.py"))
        self.assertIn(("VerifyQuotaExceeded", "web/services/x.py", 4), got)
        self.assertIn(("run_gated_call", "web/services/x.py", 3), got)
        self.assertEqual(
            host_module_reads_in("def go(acc):" + n + "    return acc.run_gated_call()" + n,
                                 "web/services/y.py"),
            [], "没有宿主模块参数却报出读者：本面会误红")
        self.assertGreaterEqual(
            len(_wall_host_module_reads()), 10,
            "服务层读者面过小（实测 14 处，全在 device_refresh）——形参别名探测失效")

    def test_exempt_names_are_still_f401(self):
        """豁免清单不得腐烂：登记的名字必须仍在 F401 名集里。

        豁免是**过渡态**（名字待删）。名字真被删掉后，这两条登记必须同批删掉——
        否则清单会悄悄变成"永久豁免"，让后来者以为这两个名字本来就该留着。
        """
        _bound, f401 = _wall_app_bindings()
        stale = sorted(n for n in _WALL_DEAD_F401_EXEMPT if n not in f401)
        self.assertEqual(
            stale, [],
            f"豁免清单登记的名字已不在 F401 名集（名字已删/已不再是转发名）：{stale}；"
            "请把这些登记从 _WALL_DEAD_F401_EXEMPT 删掉")
