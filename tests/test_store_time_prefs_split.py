# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""自选时间片域拆分契约：`yiban/store/time_prefs.py` 是 time_prefs 表族的唯一定义点。

自选时间片域从 `yiban/store/db.py` 迁入新模块 `time_prefs.py`；其中按**表归属**落在别处
的两名（`last_pause_at` / `pause_count_since`，查 audit_logs 表的 action='my_account_pause'）
并入既有 `yiban/store/events.py`。db 门面把全部迁出名纳入**模块级读写转发**
（`_FORWARDED_STATE`）而不是快照式再导出。本文件钉住四件事，任何一件破了都会**静默**
改变全仓行为：

1. **同一对象**：`db.get_time_prefs is time_prefs.get_time_prefs`、
   `db.last_pause_at is events.last_pause_at`，且这些名字不再出现在 db 自己的
   `__dict__` 里。
2. **写转发落真定义点**：`db.set_time_pref = 替身` 必须改到 `time_prefs.set_time_pref`、
   `db.last_pause_at = 替身` 必须改到 `events.last_pause_at`——只换掉门面那一份就是
   打桩静默失效。
3. **门面内的晚解析**：冷却查询里的 `hash_phone`（追踪盐哈希，定义点在
   `yiban/store/tracking.py`）经 `_facade()` 按属性取——`db.hash_phone = 替身` 必须被
   `last_time_pref_set_at` / `time_pref_set_count_since` 看见。
4. **delattr 隐藏名语义**：`del db.<名字>` 只把名字从门面摘下（真定义与模块内部调用不动），
   随后的 `setattr` 恢复让它重新可读——`mock.patch.object` / `monkeypatch.delattr` 的撤销
   依赖这条，删不掉的后果是原值永不恢复、打桩残留。

另外钉住本任务唯一可观察的行为面变化：日志通道由 `yiban.db` 细分为
`yiban.store.time_prefs`（文案未改），旧通道不再收到本域日志。
"""
import contextlib
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
PHONE = "13800001234"
OTHER_PHONE = "13800005678"
ADMIN = "admin@test.local"
PAUSER = "user1@test.local"

# db 门面上的 time_prefs 域迁出名（与 db.py `_FORWARDED_STATE` 里的登记一致）
MOVED_TO_TIME_PREFS = (
    "last_time_pref_set_at",
    "time_pref_set_count_since",
    "get_time_prefs",
    "get_time_pref",
    "set_time_pref",
    "clear_time_pref",
    "time_pref_stats",
)
# 按表归属并入 events 域的暂停冷却两名（查 audit_logs 表）
MOVED_TO_EVENTS = (
    "last_pause_at",
    "pause_count_since",
)
MOVED_NAMES = MOVED_TO_TIME_PREFS + MOVED_TO_EVENTS


def _import_shell():
    """`scripts/db.py` 兼容壳（旧 `import db` 路径）。"""
    sys.path.insert(0, os.path.join(BASE, "scripts"))
    return importlib.import_module("db")


from yiban.store import connection as conn_mod  # noqa: E402
from yiban.store import db as impl  # noqa: E402
from yiban.store import events as events_mod  # noqa: E402
from yiban.store import time_prefs as prefs_mod  # noqa: E402


class _TimePrefsSplitBase(unittest.TestCase):
    """连接状态与密钥路径的公共收发。

    连接收尾走门面（顺带覆盖写入转发）；`_connection._env_file` / `_db_file` 与
    `YIBAN_DB_FILE` 是**模块级状态**，必须保存/还原——只置空连接不还原路径，就会给后续
    用例留下本文件的临时库路径（顺序依赖隐患，与 `test_store_connection_split.py` 同款）。
    """

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-prefs-split-")
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
        path = os.path.join(tempfile.mkdtemp(prefix="yiban-prefs-split-db-"),
                            "yiban.db")
        self.addCleanup(shutil.rmtree, os.path.dirname(path), ignore_errors=True)
        impl.init_db(path, env_file=self.env_file, cleanup=False)
        return path

    def _add_account(self, phone, owner=ADMIN):
        """建未删除账号：time_pref_stats 按 accounts.deleted = 0 过滤，缺行统计为 0。"""
        impl.add_account({"name": "stat", "phone": phone, "password": "p",
                          "phone_model": "", "phone_code": "", "status": "active",
                          "owner": owner})


class SameObjectTest(_TimePrefsSplitBase):
    """① 门面读取回落到定义点，且 db 不再持有自己的绑定。"""

    def test_functions_are_the_same_objects(self):
        for name in MOVED_TO_TIME_PREFS:
            self.assertIs(getattr(impl, name), getattr(prefs_mod, name),
                          f"db.{name} 与 time_prefs.{name} 不是同一对象")
        for name in MOVED_TO_EVENTS:
            self.assertIs(getattr(impl, name), getattr(events_mod, name),
                          f"db.{name} 与 events.{name} 不是同一对象")

    def test_definitions_live_only_in_owning_modules(self):
        """唯一的定义点：按表归属各自持有，db 一个都不留。"""
        for name in MOVED_TO_TIME_PREFS:
            self.assertIn(name, vars(prefs_mod), f"time_prefs 应定义 {name}")
        for name in MOVED_TO_EVENTS:
            self.assertIn(name, vars(events_mod), f"events 应定义 {name}")
        for name in MOVED_NAMES:
            self.assertNotIn(name, vars(impl), f"db 不该再持有自己的 {name} 绑定")

    def test_lookup_is_by_table_ownership(self):
        """落点按表判据：time_pref 冷却在 time_prefs，pause 冷却在 events。"""
        self.assertNotIn("last_pause_at", vars(prefs_mod))
        self.assertNotIn("pause_count_since", vars(prefs_mod))
        self.assertNotIn("get_time_prefs", vars(events_mod))

    def test_names_importable_from_db(self):
        from yiban.store.db import (  # noqa: F401  # 导入成功本身就是断言
            clear_time_pref,
            get_time_pref,
            get_time_prefs,
            last_pause_at,
            last_time_pref_set_at,
            pause_count_since,
            set_time_pref,
            time_pref_set_count_since,
            time_pref_stats,
        )
        self.assertIs(get_time_prefs, prefs_mod.get_time_prefs)
        self.assertIs(last_time_pref_set_at, prefs_mod.last_time_pref_set_at)
        self.assertIs(last_pause_at, events_mod.last_pause_at)


class WriteForwardingTest(_TimePrefsSplitBase):
    """② 写入落到真定义点；③ 门面内的晚解析（hash_phone）。"""

    SENTINEL = object()

    def test_plain_assignment_lands_in_time_prefs(self):
        original = prefs_mod.set_time_pref
        try:
            impl.set_time_pref = self.SENTINEL
            self.assertIs(prefs_mod.set_time_pref, self.SENTINEL,
                          "门面上的赋值没有落到真定义点")
            self.assertIs(impl.set_time_pref, self.SENTINEL)
        finally:
            impl.set_time_pref = original
        self.assertIs(prefs_mod.set_time_pref, original)

    def test_plain_assignment_lands_in_events(self):
        original = events_mod.pause_count_since
        try:
            impl.pause_count_since = self.SENTINEL
            self.assertIs(events_mod.pause_count_since, self.SENTINEL,
                          "pause 冷却的赋值没有落到 events 真定义点")
        finally:
            impl.pause_count_since = original
        self.assertIs(events_mod.pause_count_since, original)

    def test_hash_phone_patch_seen_inside_cooldown_query(self):
        """`db.hash_phone = 替身` 必须被 time_prefs 模块内的冷却查询看见（晚解析）。"""
        self._init()
        stubbed = "hash-of-" + PHONE
        with mock.patch.object(impl, "hash_phone", return_value=stubbed):
            impl.audit(ADMIN, "time_pref_set", stubbed, "")
            self.assertIsNotNone(impl.last_time_pref_set_at(PHONE),
                                 "替身哈希没被内部查询用上")
            self.assertEqual(impl.time_pref_set_count_since(PHONE, "1970-01-01 00:00:00"), 1)
        # 换回真 hash_phone：同一个 phone 查不到替身写入的那条审计
        self.assertIsNone(impl.last_time_pref_set_at(PHONE),
                          "真 hash_phone 不该命中替身哈希写下的审计行")

    def test_patch_object_round_trips(self):
        real = prefs_mod.get_time_prefs
        with mock.patch.object(impl, "get_time_prefs", self.SENTINEL):
            self.assertIs(prefs_mod.get_time_prefs, self.SENTINEL)
        self.assertIs(prefs_mod.get_time_prefs, real,
                      "退出打桩必须把真函数恢复回来（丢成 None 即静默残留）")
        self.assertIs(impl.get_time_prefs, real)

    def test_patch_object_round_trips_on_events_names(self):
        real = events_mod.last_pause_at
        with mock.patch.object(impl, "last_pause_at", self.SENTINEL):
            self.assertIs(events_mod.last_pause_at, self.SENTINEL)
        self.assertIs(events_mod.last_pause_at, real)
        self.assertIs(impl.last_pause_at, real)

    def test_patch_string_target_round_trips(self):
        real = prefs_mod.clear_time_pref
        with mock.patch("yiban.store.db.clear_time_pref", self.SENTINEL):
            self.assertIs(prefs_mod.clear_time_pref, self.SENTINEL)
        self.assertIs(prefs_mod.clear_time_pref, real)
        self.assertIs(impl.clear_time_pref, real)

    def test_shell_write_forwarding_reaches_time_prefs(self):
        """`scripts/db.py` 壳（旧 `import db`）的写入同样落到 time_prefs。"""
        shell = _import_shell()
        original = prefs_mod.time_pref_stats
        try:
            shell.time_pref_stats = self.SENTINEL
            self.assertIs(prefs_mod.time_pref_stats, self.SENTINEL,
                          "壳上的赋值经 db 门面后没有落到真定义点")
        finally:
            shell.time_pref_stats = original
        self.assertIs(prefs_mod.time_pref_stats, original)

    def test_shell_patch_object_round_trips(self):
        shell = _import_shell()
        real_pref, real_pause = prefs_mod.get_time_pref, events_mod.last_pause_at
        with mock.patch.object(shell, "get_time_pref", self.SENTINEL):
            self.assertIs(prefs_mod.get_time_pref, self.SENTINEL)
            self.assertIs(events_mod.last_pause_at, real_pause)
        self.assertIs(prefs_mod.get_time_pref, real_pref)
        self.assertIs(events_mod.last_pause_at, real_pause)


class DeleteHidingTest(_TimePrefsSplitBase):
    """④ delattr 只摘门面上的名字，真定义与模块内部调用不受影响。"""

    def test_delattr_hides_name_on_facade(self):
        real = prefs_mod.clear_time_pref
        del impl.clear_time_pref
        self.assertFalse(hasattr(impl, "clear_time_pref"),
                         "删不掉的话 patch 撤销不会 setattr 回原值")
        with self.assertRaises(AttributeError):
            _ = impl.clear_time_pref
        self.assertIs(prefs_mod.clear_time_pref, real, "摘名不该动真定义")
        impl.clear_time_pref = real
        self.assertIs(impl.clear_time_pref, real)

    def test_delattr_hides_events_name_on_facade(self):
        real = events_mod.pause_count_since
        del impl.pause_count_since
        self.assertFalse(hasattr(impl, "pause_count_since"))
        self.assertIs(events_mod.pause_count_since, real)
        impl.pause_count_since = real
        self.assertIs(impl.pause_count_since, real)

    def test_delattr_of_unknown_name_still_raises(self):
        with self.assertRaises(AttributeError):
            del impl.definitely_not_a_name

    def test_monkeypatch_delattr_undo_restores(self):
        real = prefs_mod.last_time_pref_set_at
        mp = pytest.MonkeyPatch()
        try:
            mp.delattr(impl, "last_time_pref_set_at")
            self.assertFalse(hasattr(impl, "last_time_pref_set_at"))
            self.assertIs(prefs_mod.last_time_pref_set_at, real)
        finally:
            mp.undo()
        self.assertIs(impl.last_time_pref_set_at, real)
        self.assertIs(prefs_mod.last_time_pref_set_at, real)


class FacadeBehaviourTest(_TimePrefsSplitBase):
    """真实可用：经门面走一遍写/读/统计/冷却，并钉住日志通道细分。"""

    def setUp(self):
        super().setUp()
        self._init()

    def test_round_trip_through_facade(self):
        self.assertIsNone(impl.get_time_pref(PHONE), "未写入时返回 None")
        impl.set_time_pref(PHONE, 390, "2026-09-01 10:00:00")
        impl.set_time_pref(OTHER_PHONE, 395, "2026-09-01 10:01:00")
        self.assertEqual(impl.get_time_pref(PHONE)["slot_min"], 390)
        self.assertEqual(impl.get_time_prefs()[OTHER_PHONE]["slot_min"], 395)
        # UPSERT：同 phone 再写只更新
        impl.set_time_pref(PHONE, 400, "2026-09-01 11:00:00")
        self.assertEqual(impl.get_time_pref(PHONE)["slot_min"], 400)
        self.assertEqual(len(impl.get_time_prefs()), 2)

        # 拥挤度只算未删除账号
        self.assertEqual(impl.time_pref_stats(), [], "无账号行时拥挤度为空")
        self._add_account(PHONE)
        self._add_account(OTHER_PHONE, owner=PAUSER)
        self.assertEqual(
            {s["slot_min"]: s["count"] for s in impl.time_pref_stats()},
            {400: 1, 395: 1},
        )

        impl.clear_time_pref(PHONE)
        self.assertIsNone(impl.get_time_pref(PHONE))
        impl.clear_time_pref(PHONE)  # 幂等：行不存在时不报错

    def test_time_pref_cooldown_through_facade(self):
        impl.audit(ADMIN, "time_pref_set", impl.hash_phone(PHONE), "")
        impl.audit(ADMIN, "time_pref_set", impl.hash_phone(OTHER_PHONE), "")
        self.assertIsNotNone(impl.last_time_pref_set_at(PHONE))
        self.assertEqual(
            impl.time_pref_set_count_since(PHONE, "1970-01-01 00:00:00"), 1,
            "冷却按被选账号计价：别的账号的保存不该计入本账号",
        )
        self.assertEqual(
            impl.time_pref_set_count_since(PHONE, "2999-01-01 00:00:00"), 0,
        )
        self.assertIsNone(impl.last_time_pref_set_at("13700000000"))

    def test_pause_cooldown_through_facade(self):
        self.assertIsNone(impl.last_pause_at(PAUSER), "无审计时返回 None")
        impl.audit(PAUSER, "my_account_pause", "138****0001", "")
        impl.audit(PAUSER, "my_account_resume", "138****0001", "")
        impl.audit(ADMIN, "my_account_pause", "138****0002", "")
        ts = impl.last_pause_at(PAUSER)
        self.assertIsNotNone(ts)
        self.assertEqual(
            impl.pause_count_since(PAUSER, "1970-01-01 00:00:00"), 1,
            "恢复不计入暂停次数；别的用户名不计入",
        )
        self.assertEqual(
            impl.pause_count_since(ADMIN, "1970-01-01 00:00:00"), 1,
            "冷却按 username 计价：别的用户名的暂停不计入",
        )
        self.assertIsNone(impl.last_pause_at("nobody@test.local"))

    def test_failure_log_uses_new_channel_only(self):
        """日志通道细分为 `yiban.store.time_prefs`；旧通道 `yiban.db` 不再收到本域日志。"""
        with mock.patch.object(impl, "get_conn", side_effect=RuntimeError("boom")), \
                self.assertLogs("yiban.store.time_prefs", level="WARNING") as captured, \
                self.assertNoLogs("yiban.db", level="WARNING"):
            self.assertEqual(impl.get_time_prefs(), {}, "查询失败回退空字典")
        self.assertIn("读取 time_prefs 失败", "\n".join(captured.output))


if __name__ == "__main__":
    unittest.main(verbosity=2)
