# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""签到日志族与签到状态族拆分契约：`web/services/{logs,signstatus}.py` 是真源。

日志读写/解析、账号状态与熔断暂停、`.env` 开关解析、签到窗口与运行时段判定、系统信息
（状态文案与连通性检测）从 `web/app.py` 迁入 `web/services/`，app.py 只保留名字面与少量
注入转发。本文件钉住五件事，任一件破了都会**静默**改变行为：

1. **名字面完整**：迁移名在 `web.app` 与所属服务模块上都可达（routes 经
   `sys.modules[current_app.import_name].<名字>` 晚查找取用）。
2. **转发注入 app 模块级状态**：日志目录 `LOG_FILE`、状态目录 `STATE_DIR`、倒读实现
   `_tail_lines`、窗口解析器 `_sign_window`、钟点判定 `_in_sign_window` 都留在 web.app 且
   会被测试改写（直接赋值 / `mock.patch.object`），服务层另存一份绑定会让改写静默失效——
   故转发必须在调用时刻现取后传入。
3. **行为逐字不变**：可见性口径、日期串口径、日期前缀过滤、倒读截断与状态文件回退链、
   `.env` 开关真值字面量、窗口钟点判定、状态文案与连通性判据都保持原样。
4. **别名加载安全**：服务层不导入 `web.app`（普通 import 会在别名加载的测试进程里再执行
   一份 app.py 副本）；别名加载的 app 副本与 `web.services.*` 共享同一实现。
5. **服务层不持有 app 状态**：`ENV_FILE` / `LOG_FILE` / `STATE_DIR` / `_REPO_ROOT` /
   `_doc_cache` / `_atomic_write` / `_file_lock` / `_rate_lock` / `send_notification` 不得出现
   在服务模块上，否则第 2 条的"另存绑定"会以更隐蔽的形态回归。
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
import unittest
from datetime import datetime
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TEST_KEY = "a" * 64

#: 迁入 `web/services/logs.py` 的名字（实现唯一在那里；web.app 上必须是可达的兼容面）
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

#: 迁入 `web/services/signstatus.py` 的名字
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

#: 纯再导出（不读 app 模块级状态）：两边必须是同一对象
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

#: 必须由转发包装注入 app 状态的名字（不能是纯再导出）
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

#: 服务层不得持有的 web.app 模块级状态
APP_HELD_STATE = (
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

SERVICE_SOURCES = ("web/services/logs.py", "web/services/signstatus.py")


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
            for name in APP_HELD_STATE:
                self.assertFalse(hasattr(mod, name),
                                 f"{mod.__name__} 不得持有 {name}（应由 web.app 调用时刻注入）")

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
            "import web.services.logs, web.services.signstatus;"
            "print('APP_LOADED' if 'web.app' in sys.modules else 'SERVICES_ONLY_OK')"
        )
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        r = subprocess.run([sys.executable, "-c", code], cwd=BASE, env=env,
                           capture_output=True, text=True, timeout=60)
        self.assertIn("SERVICES_ONLY_OK", r.stdout, r.stderr[-600:])
        self.assertEqual(r.returncode, 0, r.stderr[-600:])


if __name__ == "__main__":
    unittest.main(verbosity=2)
