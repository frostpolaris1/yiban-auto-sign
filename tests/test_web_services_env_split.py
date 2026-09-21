# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""env 读写与执行体清单拆分契约：`web/services/{env_io,executor_env,locks}.py` 是真源。

`.env` 读写、设置项展示、公告元数据解析与执行体清单族从 `web/app.py` 迁入
`web/services/`，app.py 只保留名字面与少量注入转发。本文件钉住五件事，任一件破了都会
**静默**改变行为：

1. **名字面完整**：迁移名在 `web.app` 与所属服务模块上都可达（routes 经
   `sys.modules[current_app.import_name].<名字>` 晚查找取用）。
2. **转发注入 app 模块级状态**：`.env` 落盘用的 `_atomic_write`、批量写入口
   `write_env_batch`、设置项缺省值与开关解析器 `_env_flag`、告警出口 `send_notification`、
   执行体窗口用的 `ENV_FILE` / `_sign_window` 都留在 web.app 且会被测试改写
   （`mock.patch.object` / 直接赋值），服务层另存一份绑定会让改写静默失效——故转发必须
   在调用时刻现取后传入。
3. **锁身份单一**：`_file_lock` 只有一把（`web.services.locks`），`web.app._file_lock`
   与 `m._file_lock` 是同一对象；服务模块不得自建第二把。
4. **别名加载安全**：服务层不导入 `web.app`（普通 import 会在别名加载的测试进程里再执行
   一份 app.py 副本）；别名加载的 app 副本与 `web.services.*` 共享同一实现。
5. **服务层不持有 app 状态**：`ENV_FILE` / `_REPO_ROOT` / `_atomic_write` / `_env_flag` /
   `_sign_window` / `send_notification` 不得出现在服务模块上，否则第 2 条的"另存绑定"会
   以更隐蔽的形态回归。
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
import threading
import unittest
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TEST_KEY = "a" * 64

#: 迁入 `web/services/env_io.py` 的名字（实现唯一在那里；web.app 上必须是可达的兼容面）
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

#: 迁入 `web/services/executor_env.py` 的名字
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

#: 纯再导出（不读 app 模块级状态）：两边必须是同一对象
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

#: 必须由转发包装注入 app 状态的名字（不能是纯再导出）
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

#: 服务层不得持有的 web.app 模块级状态
APP_HELD_STATE = (
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

SERVICE_SOURCES = ("web/services/env_io.py", "web/services/executor_env.py",
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
            for name in APP_HELD_STATE:
                if name in exempt:
                    continue
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
            "import web.services.env_io, web.services.executor_env, web.services.locks;"
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
