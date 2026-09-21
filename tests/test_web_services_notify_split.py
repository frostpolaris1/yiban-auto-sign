# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""通知告警 / 通道健康 / 容量 / 手动签到四族拆分契约。

实现唯一真源在 `web/services/{notify_mail,channel_health,capacity,manual_sign}.py`，
`web/app.py` 只保留名字面与注入转发。本文件钉住五件事，任一件破了都会**静默**改变行为：

1. **名字面完整**：迁移名与随域常量在 `web.app` 与所属服务模块上都可达（routes 经
   `web.routes.appmod()` 按属性取用）。
2. **转发注入 app 模块级状态**：`.env` 路径与读取器、整数配置读取器、窗口解析器
   `_sign_window`、掐头去尾口径 `edge_config`、同类型告警邮件节流 `_mail_alert_due`、
   日志路径 `log_path_for`、状态生产者 / 状态行 / 告警出口 `send_notification` 都留在
   `web.app` 且会被测试打桩（`mock.patch.object`）或直接赋值改写，服务层另存一份绑定
   会让改写静默失效——故转发必须在调用时刻现取后传入。
3. **同一对象**：节流表 `_mail_alert_ts` / `_capacity_alerts`、日报标记键
   `_HEALTH_REPORT_META_KEY`、账本中文名表 `_NOTIFY_LEDGER_LABELS` 在两边是同一份
   （测试按属性读取或 `.clear()` 复位进程内状态）。
4. **行为逐字不变**：容量算式（有效窗口扣除掐头去尾、与引擎同源）、手动签到退出码
   文案与批量超时公式、通道降级判据的三条成立条件都保持原样。
5. **别名加载安全**：服务层不导入 `web.app`（普通 import 会在别名加载的测试进程里再
   执行一份 app.py 副本），且不持有应由 web.app 调用时刻注入的那些名字。
"""
import contextlib
import importlib.util
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TEST_KEY = "a" * 64

#: 迁入 `web/services/notify_mail.py` 的名字
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

#: 迁入 `web/services/channel_health.py` 的名字
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

#: 迁入 `web/services/capacity.py` 的名字
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

#: 迁入 `web/services/manual_sign.py` 的名字
MOVED_MANUAL = (
    "_wait_signin_proc",
    "_batch_wait_timeout",
    "_manual_sign_failure_reason",
    "_log_manual_sign_exit",
    "_SIGNIN_EXIT_REASONS",
)

#: 纯再导出（不读 app 模块级状态）：两边必须是同一对象
PURE_REEXPORTS = {
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

#: 必须由转发包装注入 app 状态的名字（不能是纯再导出）
FORWARDED = {
    "notify_mail": ("send_notification", "_push_ever_configured"),
    "channel_health": ("_alert_channel_status", "_channel_status_lines",
                       "_send_channel_health_report"),
    "capacity": ("_capacity_estimate", "_accounts_at_capacity", "_registration_paused",
                 "_users_at_capacity", "_mail_alert_due", "_notify_capacity_once"),
    "manual_sign": ("_log_manual_sign_exit",),
}

#: 各服务模块不得持有的 web.app 模块级名字（应调用时刻注入）。
#: 排除该模块自己定义/持有的名字（如 notify_mail 的 `send_notification`、
#: capacity 的 `_mail_alert_due` 与节流表、channel_health 的状态行）。
COMMON_APP_STATE = (
    "ENV_FILE", "LOG_FILE", "STATE_DIR", "read_env", "load_env_int",
    "_sign_window", "edge_config", "log_path_for", "_atomic_write",
    "_file_lock", "_rate_lock",
)
APP_HELD_STATE = {
    "web/services/notify_mail.py": (*COMMON_APP_STATE, "_mail_alert_due"),
    "web/services/channel_health.py": (
        *COMMON_APP_STATE, "send_notification", "_push_ever_configured", "_mail_alert_due"),
    "web/services/capacity.py": (
        *COMMON_APP_STATE, "send_notification", "DEFAULT_MAX_ACCOUNTS", "DEFAULT_MAX_USERS"),
    "web/services/manual_sign.py": COMMON_APP_STATE,
}
SERVICE_SOURCES = tuple(APP_HELD_STATE)


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
        for mod, names in ((nm, PURE_REEXPORTS["notify_mail"]),
                           (ch, PURE_REEXPORTS["channel_health"]),
                           (cap, PURE_REEXPORTS["capacity"]),
                           (ms, PURE_REEXPORTS["manual_sign"])):
            for name in names:
                self.assertIs(getattr(self.webapp, name), getattr(mod, name),
                              f"{name} 应为同一个对象（web.app 只是再导出）")

    def test_forwarders_actually_forward(self):
        """注入型名字不得退化成纯再导出（否则 app 侧打桩面静默失效）。"""
        import web.services.capacity as cap
        import web.services.channel_health as ch
        import web.services.manual_sign as ms
        import web.services.notify_mail as nm
        for mod, names in ((nm, FORWARDED["notify_mail"]),
                           (ch, FORWARDED["channel_health"]),
                           (cap, FORWARDED["capacity"]),
                           (ms, FORWARDED["manual_sign"])):
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
            for name in APP_HELD_STATE[rel]:
                self.assertFalse(hasattr(mod, name),
                                 f"{rel} 不得持有 {name}（应由 web.app 调用时刻注入）")

    def test_service_sources_do_not_import_app(self):
        for rel in SERVICE_SOURCES:
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
