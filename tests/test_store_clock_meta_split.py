# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""时钟守卫告警与 app_meta 域拆分契约：`yiban/store/clock_meta.py` 是唯一定义点。

`_record_clock_guard_alert` / `clock_guard_alert` / `get_meta` / `set_meta` 从
`yiban/store/db.py` 迁入新模块 `clock_meta.py`（守卫本体 `_clock_jump_guard` 作为登记
承诺的跨域粘合留在门面）。db 门面把这四名纳入**模块级读写转发**（`_FORWARDED_STATE`）
而不是快照式再导出。本文件钉住五件事，任何一件破了都会**静默**改变全仓行为：

1. **同一对象**：`db.get_meta is clock_meta.get_meta` 等四名，且这些名字不再出现在 db
   自己的 `__dict__` 里。
2. **写转发落真定义点**：`db.clock_guard_alert = 替身` 必须改到 `clock_meta.clock_guard_alert`
   ——只换掉门面那一份就是打桩静默失效。
3. **门面内的晚解析**：留守的 `_clock_jump_guard` 在拦截分支调用告警落库时按**属性**取
   `clock_meta._record_clock_guard_alert`——`db._record_clock_guard_alert = 替身` 必须被
   它看见（否则替换品被跳过、真函数照旧开第二连接写库）。
4. **delattr 隐藏名语义**：`del db.<名字>` 只把名字从门面摘下（真定义与模块内部调用不动），
   随后的 `setattr` 恢复让它重新可读——`mock.patch.object` / `monkeypatch.delattr` 的撤销
   依赖这条，删不掉的后果是原值永不恢复、打桩残留。
5. **常量定义点**：告警留痕键 `_CLOCK_GUARD_ALERT_KEY` 随域迁出、门面按常量再导出
   （读取不变）；守卫阈值 `_CLOCK_ALLOW_FWD_HOURS` / `_CLOCK_ALLOW_BACK_SECONDS` 只被
   留守的守卫本体使用，不随迁。

另外钉住本次迁出唯一可观察的行为面变化：日志通道由 `yiban.db` 细分为
`yiban.store.clock_meta`（文案未改），旧通道不再收到本域日志。
"""
import contextlib
import datetime
import importlib
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

import pytest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TEST_KEY = "a" * 64

# db 门面上的 clock_meta 域迁出名（与 db.py `_FORWARDED_STATE` 里的登记一致）
MOVED_NAMES = (
    "_record_clock_guard_alert",
    "clock_guard_alert",
    "get_meta",
    "set_meta",
)


def _import_shell():
    """`scripts/db.py` 兼容壳（旧 `import db` 路径）。"""
    sys.path.insert(0, os.path.join(BASE, "scripts"))
    return importlib.import_module("db")


from yiban.store import clock_meta as meta_mod  # noqa: E402
from yiban.store import connection as conn_mod  # noqa: E402
from yiban.store import db as impl  # noqa: E402


class _ClockMetaSplitBase(unittest.TestCase):
    """连接状态与密钥路径的公共收发。

    连接收尾走门面（顺带覆盖写入转发）；`_connection._env_file` / `_db_file` 与
    `YIBAN_DB_FILE` 是**模块级状态**，必须保存/还原——只置空连接不还原路径，就会给后续
    用例留下本文件的临时库路径（顺序依赖隐患，与 `test_store_connection_split.py` 同款）。
    """

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-clockmeta-split-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n")
        cls._prev_key = os.environ.get("YIBAN_ACCOUNTS_KEY")
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY

    @classmethod
    def tearDownClass(cls):
        if cls._prev_key is None:
            os.environ.pop("YIBAN_ACCOUNTS_KEY", None)
        else:
            os.environ["YIBAN_ACCOUNTS_KEY"] = cls._prev_key
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self._prev_env_file = conn_mod._env_file
        self._prev_db_file = conn_mod._db_file
        self._prev_env = os.environ.get("YIBAN_DB_FILE")
        self._close_conn()

    def tearDown(self):
        self._close_conn()
        if self._prev_env is None:
            os.environ.pop("YIBAN_DB_FILE", None)
        else:
            os.environ["YIBAN_DB_FILE"] = self._prev_env
        conn_mod._env_file = self._prev_env_file
        conn_mod._db_file = self._prev_db_file

    def _close_conn(self):
        conn = conn_mod.current()
        if conn is not None:
            with contextlib.suppress(Exception):
                conn.close()
        impl._conn = None

    def _init(self):
        """临时库 + 本文件 .env 初始化，返回库路径。"""
        path = os.path.join(tempfile.mkdtemp(prefix="yiban-clockmeta-split-db-"),
                            "yiban.db")
        self.addCleanup(shutil.rmtree, os.path.dirname(path), ignore_errors=True)
        impl.init_db(path, env_file=self.env_file, cleanup=False)
        return path

    def _set_reference(self, key, value):
        """写守卫参照点并**立即提交**：未提交事务会让守卫的告警短连接争锁超时。"""
        conn = impl.get_conn()
        with impl._conn_lock:
            conn.execute("INSERT OR REPLACE INTO app_meta (key, value) VALUES (?,?)", (key, value))
            conn.commit()
        return conn

    def _trip_guard(self, key="test_clock_meta_fwd"):
        """把参照点拨到 100h 前再调守卫 → 必然判定跳变（>72h）。"""
        old = (datetime.datetime.now() - datetime.timedelta(hours=100)).strftime(
            "%Y-%m-%d %H:%M:%S")
        conn = self._set_reference(key, old)
        return impl._clock_jump_guard(conn, key)


class SameObjectTest(_ClockMetaSplitBase):
    """① 门面读取回落到定义点，且 db 不再持有自己的绑定。"""

    def test_functions_are_the_same_objects(self):
        for name in MOVED_NAMES:
            self.assertIs(getattr(impl, name), getattr(meta_mod, name),
                          f"db.{name} 与 clock_meta.{name} 不是同一对象")

    def test_definitions_live_only_in_owning_module(self):
        for name in MOVED_NAMES:
            self.assertIn(name, vars(meta_mod), f"clock_meta 应定义 {name}")
            self.assertNotIn(name, vars(impl), f"db 不该再持有自己的 {name} 绑定")

    def test_names_importable_from_db(self):
        # 导入成功本身就是断言的一部分
        from yiban.store.db import (
            _record_clock_guard_alert,
            clock_guard_alert,
            get_meta,
            set_meta,
        )
        self.assertIs(get_meta, meta_mod.get_meta)
        self.assertIs(set_meta, meta_mod.set_meta)
        self.assertIs(clock_guard_alert, meta_mod.clock_guard_alert)
        self.assertIs(_record_clock_guard_alert, meta_mod._record_clock_guard_alert)

    def test_alert_key_reexport_and_staying_thresholds(self):
        """告警键随域迁出（门面按常量再导出）；守卫阈值只被留守本体使用，不随迁。"""
        self.assertIsNot(impl._CLOCK_GUARD_ALERT_KEY, None)
        self.assertEqual(impl._CLOCK_GUARD_ALERT_KEY, meta_mod._CLOCK_GUARD_ALERT_KEY)
        self.assertEqual(meta_mod._CLOCK_GUARD_ALERT_KEY, "clock_guard_alert")
        self.assertIn("_CLOCK_GUARD_ALERT_KEY", vars(meta_mod))
        self.assertIn("_CLOCK_ALLOW_FWD_HOURS", vars(impl), "守卫阈值跟留守本体走")
        self.assertIn("_CLOCK_ALLOW_BACK_SECONDS", vars(impl))
        self.assertNotIn("_CLOCK_ALLOW_FWD_HOURS", vars(meta_mod))

    def test_staying_guard_stays_in_db(self):
        """守卫本体是登记承诺的粘合，定义点仍在 db（cleanup/users/events 经门面调用它）。"""
        self.assertIn("_clock_jump_guard", vars(impl))
        self.assertNotIn("_clock_jump_guard", vars(meta_mod))


class WriteForwardingTest(_ClockMetaSplitBase):
    """② 写入落到真定义点；③ 门面内的晚解析（`_record_clock_guard_alert`）。"""

    SENTINEL = object()

    def test_plain_assignment_lands_in_clock_meta(self):
        for name in MOVED_NAMES:
            original = getattr(meta_mod, name)
            try:
                setattr(impl, name, self.SENTINEL)
                self.assertIs(getattr(meta_mod, name), self.SENTINEL,
                              f"门面上的 {name} 赋值没有落到真定义点")
                self.assertIs(getattr(impl, name), self.SENTINEL)
            finally:
                setattr(impl, name, original)
            self.assertIs(getattr(meta_mod, name), original)

    def test_patch_object_round_trips(self):
        for name in MOVED_NAMES:
            real = getattr(meta_mod, name)
            with mock.patch.object(impl, name, self.SENTINEL):
                self.assertIs(getattr(meta_mod, name), self.SENTINEL)
            self.assertIs(getattr(meta_mod, name), real,
                          f"退出 {name} 的打桩必须把真函数恢复回来（丢成 None 即静默残留）")
            self.assertIs(getattr(impl, name), real)

    def test_patch_string_target_round_trips(self):
        real = meta_mod.get_meta
        with mock.patch("yiban.store.db.get_meta", self.SENTINEL):
            self.assertIs(meta_mod.get_meta, self.SENTINEL)
        self.assertIs(meta_mod.get_meta, real)
        self.assertIs(impl.get_meta, real)

    def test_shell_write_forwarding_reaches_clock_meta(self):
        """`scripts/db.py` 壳（旧 `import db`）的写入同样落到 clock_meta。"""
        shell = _import_shell()
        original = meta_mod.clock_guard_alert
        try:
            shell.clock_guard_alert = self.SENTINEL
            self.assertIs(meta_mod.clock_guard_alert, self.SENTINEL,
                          "壳上的赋值经 db 门面后没有落到真定义点")
        finally:
            shell.clock_guard_alert = original
        self.assertIs(meta_mod.clock_guard_alert, original)

    def test_shell_patch_object_round_trips(self):
        shell = _import_shell()
        real = meta_mod.set_meta
        with mock.patch.object(shell, "set_meta", self.SENTINEL):
            self.assertIs(meta_mod.set_meta, self.SENTINEL)
        self.assertIs(meta_mod.set_meta, real)
        self.assertIs(impl.set_meta, real)

    def test_record_alert_stub_seen_by_staying_guard(self):
        """`db._record_clock_guard_alert = 替身` 必须被留守的守卫本体看见（晚解析）。"""
        self._init()
        real = meta_mod._record_clock_guard_alert
        recorder = mock.MagicMock()
        with mock.patch.object(impl, "_record_clock_guard_alert", recorder):
            ok, note = self._trip_guard("test_clock_meta_stub")
        self.assertFalse(ok, "前进 100h（>72h）必须判定为跳变")
        recorder.assert_called_once_with(note)
        self.assertIs(meta_mod._record_clock_guard_alert, real)
        # 替身在场时真函数一次也没跑 → 库里没有告警留痕
        self.assertIsNone(impl.clock_guard_alert(),
                          "真告警落库被跳过才说明替身确实被内部调用点用上")

    def test_record_alert_real_path_still_writes_alert(self):
        """不替换任何名字时，守卫拦截照旧把告警落进 app_meta 且可读回。"""
        self._init()
        ok, note = self._trip_guard("test_clock_meta_real")
        self.assertFalse(ok)
        alert = impl.clock_guard_alert()
        self.assertIsNotNone(alert, "拦截必须留下可读的告警（web 每日线程据此发邮件）")
        self.assertIn("系统时间异常跳变", alert["note"])
        self.assertEqual(alert["note"], note)


class DeleteHidingTest(_ClockMetaSplitBase):
    """④ delattr 只摘门面上的名字，真定义与模块内部调用不受影响。"""

    def test_delattr_hides_name_on_facade(self):
        real = meta_mod.get_meta
        del impl.get_meta
        self.assertFalse(hasattr(impl, "get_meta"),
                         "删不掉的话 patch 撤销不会 setattr 回原值")
        with self.assertRaises(AttributeError):
            _ = impl.get_meta
        self.assertIs(meta_mod.get_meta, real, "摘名不该动真定义")
        impl.get_meta = real
        self.assertIs(impl.get_meta, real)

    def test_delattr_of_unknown_name_still_raises(self):
        with self.assertRaises(AttributeError):
            del impl.definitely_not_a_name

    def test_monkeypatch_delattr_undo_restores(self):
        real = meta_mod.set_meta
        mp = pytest.MonkeyPatch()
        try:
            mp.delattr(impl, "set_meta")
            self.assertFalse(hasattr(impl, "set_meta"))
            self.assertIs(meta_mod.set_meta, real)
        finally:
            mp.undo()
        self.assertIs(impl.set_meta, real)
        self.assertIs(meta_mod.set_meta, real)


class FacadeBehaviourTest(_ClockMetaSplitBase):
    """真实可用：经门面走一遍单键读写与告警读取，并钉住日志通道细分。"""

    def setUp(self):
        super().setUp()
        self._init()

    def test_meta_round_trip_through_facade(self):
        self.assertEqual(impl.get_meta("missing", "fallback"), "fallback",
                         "键不存在返回 default")
        self.assertTrue(impl.set_meta("channel_health_last", "2026-09-19"),
                        "写入成功返回 True")
        self.assertEqual(impl.get_meta("channel_health_last"), "2026-09-19")
        # UPSERT：同键再写覆盖（日报去重靠这条）
        impl.set_meta("channel_health_last", "2026-09-20")
        self.assertEqual(impl.get_meta("channel_health_last"), "2026-09-20")
        # 值统一按 TEXT 存，读回一律 str
        self.assertTrue(impl.set_meta("counter", 7))
        self.assertEqual(impl.get_meta("counter"), "7")
        # 缺省 default 只对"没有记录"生效，不覆盖空串
        self.assertTrue(impl.set_meta("empty", ""))
        self.assertEqual(impl.get_meta("empty", "fallback"), "")
        self.assertEqual(impl.get_meta("other", None), None)

    def test_clock_guard_alert_round_trip(self):
        self.assertIsNone(impl.clock_guard_alert(), "初始无告警")
        ok, note = self._trip_guard("test_clock_meta_roundtrip")
        self.assertFalse(ok, note)
        alert = impl.clock_guard_alert()
        self.assertIsNotNone(alert)
        self.assertTrue(alert["note"].startswith("系统时间异常跳变"), alert)
        self.assertTrue(alert["ts"], "告警带写入时刻（web 邮件展示它）")
        # 人工重置工具清告警的口径：同键写空串 → 读回 None
        impl.set_meta(meta_mod._CLOCK_GUARD_ALERT_KEY, "")
        self.assertIsNone(impl.clock_guard_alert(), "清空留痕后应视为无告警")

    def test_clock_guard_alert_tolerates_non_json_value(self):
        """留痕是非 JSON 串时不抛：按 {ts:'', note:<原文>} 返回（体检不发邮件也得能读）。"""
        impl.set_meta(meta_mod._CLOCK_GUARD_ALERT_KEY, "not-json")
        self.assertEqual(impl.clock_guard_alert(), {"ts": "", "note": "not-json"})
        impl.set_meta(meta_mod._CLOCK_GUARD_ALERT_KEY, '{"ts": "x"}')
        self.assertEqual(impl.clock_guard_alert(), {"ts": "", "note": '{"ts": "x"}'},
                         "缺 note 的 JSON 同样回落成原文")

    def test_guard_reference_updated_only_on_pass(self):
        """放行路径 upsert 参照点，拦截路径不更新（防"拨快一次、下轮洗白"）。"""
        conn = impl.get_conn()
        old = (datetime.datetime.now() - datetime.timedelta(hours=1)).strftime(
            "%Y-%m-%d %H:%M:%S")
        self._set_reference("test_clock_meta_ok", old)
        ok, note = impl._clock_jump_guard(conn, "test_clock_meta_ok")
        self.assertTrue(ok, note)
        row = conn.execute(
            "SELECT value FROM app_meta WHERE key='test_clock_meta_ok'").fetchone()
        self.assertNotEqual(row["value"], old, "放行时应把参照点推进到当前时间")

    def test_failure_log_uses_new_channel_only(self):
        """日志通道细分为 `yiban.store.clock_meta`；旧通道 `yiban.db` 不再收到本域日志。"""
        with mock.patch.object(impl, "get_conn", side_effect=RuntimeError("boom")), \
                self.assertLogs("yiban.store.clock_meta", level="WARNING") as captured, \
                self.assertNoLogs("yiban.db", level="WARNING"):
            self.assertEqual(impl.get_meta("boom-key", "fallback"), "fallback",
                             "读失败回退 default（兜底路径不得变成新故障点）")
        self.assertIn("读取 app_meta[boom-key] 失败", "\n".join(captured.output))

    def test_alert_log_failure_uses_new_channel(self):
        """告警落库失败只告警不抛，且走新通道。"""
        with mock.patch.object(impl, "_db_file", "/nonexistent-dir/yiban.db"), \
                self.assertLogs("yiban.store.clock_meta", level="WARNING") as captured, \
                self.assertNoLogs("yiban.db", level="WARNING"):
            meta_mod._record_clock_guard_alert("note")  # 不得抛出
        self.assertIn("时钟守卫告警留痕失败", "\n".join(captured.output))


if __name__ == "__main__":
    unittest.main(verbosity=2)
