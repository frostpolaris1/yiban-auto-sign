# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""store 层拆分边界：门面读写转发 + 表级 CRUD 真实可用。

`yiban/store/db.py` 从「唯一实现」退成门面：各域定义点在同包 `accounts` / `clock_meta` /
`connection` / `session_cache` / `time_prefs` / `tracking` 等模块，db 用**模块级读写转发**
（`_FORWARDED_STATE`）暴露它们，而不是快照式再导出。本文件把各域拆分契约并到一处，钉住：

1. **同一对象**：`db.<名字> is <域模块>.<名字>`，且 db 自己的 `__dict__` 里不再持有该绑定。
2. **写转发落真定义点**：`db.<名字> = 替身` 必须改到域模块上，只换门面那一份就是打桩静默失效。
3. **打桩可见于模块内部调用点**：域模块内部互相调用也要看到门面上的替身（否则测试仍绿、
   打桩点却不是它以为的那一个）。
4. **delattr 隐藏名语义**：`del db.<名字>` 只把名字从门面摘下，撤销时 `setattr` 能恢复，
   `mock.patch.object` / `monkeypatch.delattr` 依赖这条。
5. **真实可用**：经门面走一遍该域的读/写/改绑/删除，确认跨域助手仍通。
6. **布局记账**：`yiban/store/db.py` 行数与裸导入（`import db` 一类）不得回潮。

功能：store 层各行级模块与 db 门面之间的读写转发契约与端到端可用性。
归属：`yiban/store/` 数据层的测试。
复用：`BASE` / `TEST_KEY` / 各域 `MOVED_*` 名字清单、`_import_shell()` 兼容壳装载助手。
通信：导入 `yiban.store.db` 与各域模块，读写临时 SQLite 与临时 `.env`；由 pytest 收集
`unittest.TestCase`。

标签：C · 存储：迁移与库完整性
覆盖：`yiban/store/db.py` 退成门面后各域（accounts / clock_meta / connection /
session_cache / time_prefs / tracking 等）的五项契约——同一对象、写转发落真定义点、
打桩对模块内部调用点可见、`delattr` 隐藏名可撤销、经门面的真实 CRUD；
外加"行数与裸导入不得回潮"的布局记账。
对应实现：`yiban/store/db.py` 的 `_FORWARDED_STATE` 读写转发与各域模块的定义点。
关键断言：本文件几乎不测数据，它测的是**打桩打在哪**——`SameObjectTest` 断 `is` 同一性、
`WriteForwardingTest` 断赋值落到域模块，两者缺一就会出现"测试绿但替身根本没生效"
（门面与定义点各持一份绑定）。布局那组是 AST/源码级断言，只保证行数与导入形状，
不保证行为。迁移与库语义本身不在这里，见 `test_db_migrations.py` 与
`test_migration_compat.py`（两个升级方向分工说明在那两处）。
依赖：临时库 + 临时 `.env`，无网络、无 skip；各域用 `_import_shell_*` 另装一份模块实例，
新增用例别复用 `sys.modules` 里的那份，否则打桩会串。
"""
import ast
import contextlib
import datetime
import importlib
import importlib.util
import io
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import unittest
from unittest import mock

import db
import pytest
import signin

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(0, os.path.join(BASE, "scripts"))

from yiban import clock  # noqa: E402
from yiban.store import accounts as accounts_mod  # noqa: E402
from yiban.store import clock_meta as meta_mod  # noqa: E402
from yiban.store import connection as conn_mod  # noqa: E402
from yiban.store import db as impl  # noqa: E402
from yiban.store import events as events_mod  # noqa: E402
from yiban.store import migrations as migrations_mod  # noqa: E402
from yiban.store import session_cache as cache_mod  # noqa: E402
from yiban.store import time_prefs as prefs_mod  # noqa: E402
from yiban.store import tracking as track_mod  # noqa: E402

TEST_KEY = "a" * 64


MOVED_NAMES_ACC = (
    "_mask_phone_display",
    "_decrypt_row",
    "_apply_plaintext_heal",
    "_row_to_account",
    "_is_encrypted_value",
    "_encrypt_field",
    "accounts_snapshot",
    "load_accounts_raw",
    "decrypt_account_rows",
    "read_accounts",
    "load_accounts",
    "_next_sort_order",
    "_convert_integrity_error",
    "add_account",
    "update_account",
    "set_account_deleted",
    "purge_account",
    "update_account_status",
    "set_user_paused",
    "move_account",
    "delete_accounts_by_owner",
    "replace_accounts",
    "batch_account_ops",
    "update_account_status_if",
)


def _import_shell_ACC():
    """`scripts/db.py` 兼容壳（旧 `import db` 路径）。"""
    sys.path.insert(0, os.path.join(BASE, "scripts"))
    return importlib.import_module("db")


class _AccountsSplitBase(unittest.TestCase):
    """加密密钥与连接状态的公共收发（连接收尾走门面，顺带覆盖写入转发）。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-accounts-split-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n")
        cls._prev_env = os.environ.get("YIBAN_ACCOUNTS_KEY")
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY

    @classmethod
    def tearDownClass(cls):
        if cls._prev_env is None:
            os.environ.pop("YIBAN_ACCOUNTS_KEY", None)
        else:
            os.environ["YIBAN_ACCOUNTS_KEY"] = cls._prev_env
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        # 连接层的 .env / 库路径是**模块级全局**，`init_db(env_file=…)` 无条件改写
        # （见 yiban/store/connection.py 的 set_env_file / set_db_file）。子类
        # （如 FacadeBehaviourTest）用 setUpClass 临时目录里的 .env 初始化，而
        # tearDownClass 会 rmtree 掉该目录——不还原就留下悬空的全局路径，污染后续
        # 用例（当前全绿是碰巧）。收尾手法与 test_store_connection_split 一致。
        self._prev_env_file = impl._connection._env_file
        self._prev_db_file = impl._connection._db_file
        self._prev_env = os.environ.get("YIBAN_DB_FILE")
        self._reset_conn()

    def tearDown(self):
        self._reset_conn()
        if self._prev_env is None:
            os.environ.pop("YIBAN_DB_FILE", None)
        else:
            os.environ["YIBAN_DB_FILE"] = self._prev_env
        impl._connection._env_file = self._prev_env_file
        impl._connection._db_file = self._prev_db_file

    def _reset_conn(self):
        conn = impl._conn
        if conn is not None:
            with contextlib.suppress(Exception):
                conn.close()
        impl._conn = None


class SameObjectTest_ACC(_AccountsSplitBase):
    """① 门面读取回落到 accounts，且 db 不再持有自己的绑定。"""

    def test_functions_are_the_same_objects(self):
        for name in MOVED_NAMES_ACC:
            self.assertIs(getattr(impl, name), getattr(accounts_mod, name),
                          f"db.{name} 与 accounts.{name} 不是同一对象")
        self.assertIs(impl.DuplicatePhoneError, accounts_mod.DuplicatePhoneError)

    def test_definitions_live_only_in_accounts(self):
        """唯一的定义点：accounts 才是真正持有这些名字的模块。"""
        for name in MOVED_NAMES_ACC:
            self.assertIn(name, vars(accounts_mod), f"accounts 应定义 {name}")
            self.assertNotIn(name, vars(impl), f"db 不该再持有自己的 {name} 绑定")

    def test_names_importable_from_db(self):
        from yiban.store.db import (  # noqa: F401  # 导入成功本身就是断言
            _decrypt_row,
            accounts_snapshot,
            add_account,
            load_accounts,
            update_account_status_if,
        )
        self.assertIs(load_accounts, accounts_mod.load_accounts)
        self.assertIs(_decrypt_row, accounts_mod._decrypt_row)


class WriteForwardingTest_ACC(_AccountsSplitBase):
    """② 写入落到真定义点；③ 模块内部调用点看到门面上的替身。"""

    SENTINEL = object()

    def test_plain_assignment_lands_in_accounts(self):
        original = accounts_mod.add_account
        try:
            impl.add_account = self.SENTINEL
            self.assertIs(accounts_mod.add_account, self.SENTINEL,
                          "门面上的赋值没有落到真定义点")
            self.assertIs(impl.add_account, self.SENTINEL)
        finally:
            impl.add_account = original
        self.assertIs(accounts_mod.add_account, original)

    def test_internal_caller_sees_facade_patch(self):
        with mock.patch.object(impl, "_decrypt_row", return_value=({"id": 1}, [])):
            out = accounts_mod.decrypt_account_rows([object()])
        self.assertEqual(out, [{"id": 1}], "内部调用点没走门面上的替身")

    def test_read_accounts_uses_patched_decrypt(self):
        with mock.patch.object(impl, "decrypt_account_rows", return_value=["ok"]):
            self.assertEqual(accounts_mod.read_accounts(lambda: []), ["ok"])
        self.assertIs(impl.decrypt_account_rows, accounts_mod.decrypt_account_rows)

    def test_patch_object_round_trips(self):
        real = accounts_mod._decrypt_row
        with mock.patch.object(impl, "_decrypt_row", self.SENTINEL):
            self.assertIs(accounts_mod._decrypt_row, self.SENTINEL)
        self.assertIs(accounts_mod._decrypt_row, real,
                      "退出打桩必须把真函数恢复回来（丢成 None 即静默残留）")
        self.assertIs(impl._decrypt_row, real)

    def test_patch_string_target_round_trips(self):
        real = accounts_mod.load_accounts
        with mock.patch("yiban.store.db.load_accounts", self.SENTINEL):
            self.assertIs(accounts_mod.load_accounts, self.SENTINEL)
        self.assertIs(accounts_mod.load_accounts, real)
        self.assertIs(impl.load_accounts, real)

    def test_shell_write_forwarding_reaches_accounts(self):
        """`scripts/db.py` 壳（旧 `import db`）的写入同样落到 accounts。"""
        shell = _import_shell_ACC()
        original = accounts_mod.add_account
        try:
            shell.add_account = self.SENTINEL
            self.assertIs(accounts_mod.add_account, self.SENTINEL,
                          "壳上的赋值经 db 门面后没有落到真定义点")
        finally:
            shell.add_account = original
        self.assertIs(accounts_mod.add_account, original)

    def test_shell_patch_object_round_trips(self):
        shell = _import_shell_ACC()
        real_decrypt, real_read = accounts_mod._decrypt_row, accounts_mod.decrypt_account_rows
        with mock.patch.object(shell, "_decrypt_row", self.SENTINEL):
            self.assertIs(accounts_mod._decrypt_row, self.SENTINEL)
        self.assertIs(accounts_mod._decrypt_row, real_decrypt)
        self.assertIs(accounts_mod.decrypt_account_rows, real_read)


class DeleteHidingTest_ACC(_AccountsSplitBase):
    """④ delattr 只摘门面上的名字，真定义与模块内部调用不受影响。"""

    def test_delattr_hides_name_on_facade(self):
        real = accounts_mod.add_account
        del impl.add_account
        self.assertFalse(hasattr(impl, "add_account"),
                         "删不掉的话 patch 撤销不会 setattr 回原值")
        with self.assertRaises(AttributeError):
            _ = impl.add_account
        self.assertIs(accounts_mod.add_account, real, "摘名不该动真定义")
        impl.add_account = real
        self.assertIs(impl.add_account, real)

    def test_delattr_of_unknown_name_still_raises(self):
        with self.assertRaises(AttributeError):
            del impl.definitely_not_a_name

    def test_monkeypatch_delattr_undo_restores(self):
        real = accounts_mod.read_accounts
        mp = pytest.MonkeyPatch()
        try:
            mp.delattr(impl, "read_accounts")
            self.assertFalse(hasattr(impl, "read_accounts"))
            self.assertIs(accounts_mod.read_accounts, real)
        finally:
            mp.undo()
        self.assertIs(impl.read_accounts, real)
        self.assertIs(accounts_mod.read_accounts, real)


class FacadeBehaviourTest_ACC(_AccountsSplitBase):
    """真实可用：经门面走一遍读/写/改绑/删除，确认 `_facade()` 取的跨域助手仍通。"""

    def setUp(self):
        super().setUp()
        self.db_file = os.path.join(tempfile.mkdtemp(prefix="yiban-accounts-split-db-"),
                                    "yiban.db")
        self.addCleanup(shutil.rmtree, os.path.dirname(self.db_file), ignore_errors=True)
        impl.init_db(self.db_file, env_file=self.env_file, cleanup=False)

    def test_crud_round_trip_through_facade(self):
        account_id = impl.add_account(
            {"name": "n", "phone": "13800138000", "password": "pw"}
        )
        self.assertTrue(account_id)
        accts = impl.load_accounts()
        self.assertEqual([a["phone"] for a in accts], ["13800138000"])
        self.assertEqual(accts[0]["password"], "pw", "经门面读回的密码应已解密")
        self.assertNotEqual(impl.load_accounts_raw()[0]["password"], "pw",
                            "原始快照应保持密文")

        self.assertTrue(impl.update_account(account_id, {"phone": "13900139000"}))
        self.assertEqual(impl.load_accounts()[0]["phone"], "13900139000")

        impl.purge_account(account_id)
        self.assertEqual(impl.load_accounts(), [])

    def test_duplicate_phone_raises_module_exception(self):
        impl.add_account({"name": "a", "phone": "13800138001", "password": "pw"})
        with self.assertRaises(impl.DuplicatePhoneError):
            impl.add_account({"name": "b", "phone": "13800138001", "password": "pw"})

    def test_duplicate_owner_raises_users_exception(self):
        impl.add_account({"name": "a", "phone": "13800138002", "password": "pw",
                          "owner": "user@test.local"})
        with self.assertRaises(impl.DuplicateOwnerError):
            impl.add_account({"name": "b", "phone": "13800138003", "password": "pw",
                              "owner": "user@test.local"})

    def test_is_encrypted_value_readable_on_facade(self):
        """迁移路径（`_maybe_migrate`）按 `_accounts._is_encrypted_value` 取这个助手。"""
        self.assertFalse(impl._is_encrypted_value("plain"))
        self.assertTrue(impl._is_encrypted_value(
            impl._encrypt_field("pw", "13800138000")
        ))


MOVED_NAMES_CLOCK = (
    "get_meta",
    "set_meta",
)


def _import_shell_CLOCK():
    """`scripts/db.py` 兼容壳（旧 `import db` 路径）。"""
    sys.path.insert(0, os.path.join(BASE, "scripts"))
    return importlib.import_module("db")


class _ClockMetaSplitBase(unittest.TestCase):
    """连接状态与密钥路径的公共收发。

    连接收尾走门面（顺带覆盖写入转发）；`_connection._env_file` / `_db_file` 与
    `YIBAN_DB_FILE` 是**模块级状态**，必须保存/还原——只置空连接不还原路径，就会给后续
    用例留下本文件的临时库路径（顺序依赖隐患，与 `_ConnStateBase` 同款）。
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
        old = (clock.now() - datetime.timedelta(hours=100)).strftime(
            "%Y-%m-%d %H:%M:%S")
        conn = self._set_reference(key, old)
        return impl._clock_jump_guard(conn, key)


class SameObjectTest_CLOCK(_ClockMetaSplitBase):
    """① 门面读取回落到定义点，且 db 不再持有自己的绑定。"""

    def test_functions_are_the_same_objects(self):
        for name in MOVED_NAMES_CLOCK:
            self.assertIs(getattr(impl, name), getattr(meta_mod, name),
                          f"db.{name} 与 clock_meta.{name} 不是同一对象")

    def test_definitions_live_only_in_owning_module(self):
        for name in MOVED_NAMES_CLOCK:
            self.assertIn(name, vars(meta_mod), f"clock_meta 应定义 {name}")
            self.assertNotIn(name, vars(impl), f"db 不该再持有自己的 {name} 绑定")

    def test_names_importable_from_db(self):
        # 导入成功本身就是断言的一部分
        from yiban.store.db import get_meta, set_meta
        self.assertIs(get_meta, meta_mod.get_meta)
        self.assertIs(set_meta, meta_mod.set_meta)

    def test_staying_thresholds_not_moved(self):
        """守卫阈值只被留守本体使用，不随 app_meta 单键读写迁出。"""
        self.assertIn("_CLOCK_ALLOW_FWD_HOURS", vars(impl), "守卫阈值跟留守本体走")
        self.assertIn("_CLOCK_ALLOW_BACK_SECONDS", vars(impl))
        self.assertNotIn("_CLOCK_ALLOW_FWD_HOURS", vars(meta_mod))
        self.assertNotIn("_CLOCK_ALLOW_BACK_SECONDS", vars(meta_mod))

    def test_staying_guard_stays_in_db(self):
        """守卫本体是登记承诺的粘合，定义点仍在 db（cleanup/users/events 经门面调用它）。"""
        self.assertIn("_clock_jump_guard", vars(impl))
        self.assertNotIn("_clock_jump_guard", vars(meta_mod))


class WriteForwardingTest_CLOCK(_ClockMetaSplitBase):
    """② 写入落到真定义点；③ 越界路径的推进与日志（守卫本体仍留守 db）。"""

    SENTINEL = object()

    def test_plain_assignment_lands_in_clock_meta(self):
        for name in MOVED_NAMES_CLOCK:
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
        for name in MOVED_NAMES_CLOCK:
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
        shell = _import_shell_CLOCK()
        original = meta_mod.get_meta
        try:
            shell.get_meta = self.SENTINEL
            self.assertIs(meta_mod.get_meta, self.SENTINEL,
                          "壳上的赋值经 db 门面后没有落到真定义点")
        finally:
            shell.get_meta = original
        self.assertIs(meta_mod.get_meta, original)

    def test_shell_patch_object_round_trips(self):
        shell = _import_shell_CLOCK()
        real = meta_mod.set_meta
        with mock.patch.object(shell, "set_meta", self.SENTINEL):
            self.assertIs(meta_mod.set_meta, self.SENTINEL)
        self.assertIs(meta_mod.set_meta, real)
        self.assertIs(impl.set_meta, real)

    def test_trip_logs_and_advances_reference(self):
        """越界路径：告警只走 logger.error，参照点则被推进到当前时间。"""
        self._init()
        key = "test_clock_meta_real"
        old = (clock.now() - datetime.timedelta(hours=100)).strftime(
            "%Y-%m-%d %H:%M:%S")
        conn = self._set_reference(key, old)
        with self.assertLogs("yiban.db", level="ERROR") as captured:
            ok, note = impl._clock_jump_guard(conn, key)
        self.assertFalse(ok, "前进 100h（>72h）必须判定为跳变")
        self.assertIn("系统时间异常跳变", note)
        self.assertIn(note, "\n".join(captured.output), "跳变事实必须留在日志里")
        row = conn.execute("SELECT value FROM app_meta WHERE key=?", (key,)).fetchone()
        self.assertNotEqual(row["value"], old, "越界路径必须推进参照点")


class DeleteHidingTest_CLOCK(_ClockMetaSplitBase):
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


class FacadeBehaviourTest_CLOCK(_ClockMetaSplitBase):
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

    def test_guard_reference_advanced_on_both_paths(self):
        """放行与越界都推进参照点——"跳过一轮"因此真的只有一轮。"""
        conn = impl.get_conn()
        hour_ago = (clock.now() - datetime.timedelta(hours=1)).strftime(
            "%Y-%m-%d %H:%M:%S")
        self._set_reference("test_clock_meta_ok", hour_ago)
        ok, note = impl._clock_jump_guard(conn, "test_clock_meta_ok")
        self.assertTrue(ok, note)
        row = conn.execute(
            "SELECT value FROM app_meta WHERE key='test_clock_meta_ok'").fetchone()
        self.assertNotEqual(row["value"], hour_ago, "放行时应把参照点推进到当前时间")
        # 参照点刚被推进到 1h 前：再拨回 100h 仍判跳变，而这一次的推进让下一轮放行
        ok2, _note2 = self._trip_guard("test_clock_meta_ok")
        self.assertFalse(ok2, "回拨 100h 仍应判跳变")
        ok3, _note3 = impl._clock_jump_guard(conn, "test_clock_meta_ok")
        self.assertTrue(ok3, "越界已推进参照点 ⇒ 下一轮必须放行")

    def test_failure_log_uses_new_channel_only(self):
        """日志通道细分为 `yiban.store.clock_meta`；旧通道 `yiban.db` 不再收到本域日志。"""
        with mock.patch.object(impl, "get_conn", side_effect=RuntimeError("boom")), \
                self.assertLogs("yiban.store.clock_meta", level="WARNING") as captured, \
                self.assertNoLogs("yiban.db", level="WARNING"):
            self.assertEqual(impl.get_meta("boom-key", "fallback"), "fallback",
                             "读失败回退 default（兜底路径不得变成新故障点）")
        self.assertIn("读取 app_meta[boom-key] 失败", "\n".join(captured.output))



def _import_shell_CONN():
    """`scripts/db.py` 兼容壳（旧 `import db` 路径）。"""
    import importlib
    return importlib.import_module("db")


class _ConnStateBase(unittest.TestCase):
    """连接状态类用例的公共收尾：保状态、关连接、还原路径。"""

    def setUp(self):
        self._prev_env_file = conn_mod._env_file
        self._prev_db_file = conn_mod._db_file
        self._prev_env = os.environ.get("YIBAN_DB_FILE")
        self._close_current()

    def tearDown(self):
        self._close_current()
        if self._prev_env is None:
            os.environ.pop("YIBAN_DB_FILE", None)
        else:
            os.environ["YIBAN_DB_FILE"] = self._prev_env
        conn_mod._env_file = self._prev_env_file
        conn_mod._db_file = self._prev_db_file

    def _close_current(self):
        """关掉当前单例连接并置空——与既有测试类的收尾同一手法（走 db 的名字，
        顺带覆盖写入转发；这正是本文件第 3 条要钉的路径）。"""
        conn = conn_mod.current()
        if conn is not None:
            with contextlib.suppress(Exception):
                conn.close()
        impl._conn = None

    def _temp_db(self):
        tmp = tempfile.mkdtemp(prefix="yiban-conn-split-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        return os.path.join(tmp, "yiban.db")


class ConnectionIdentityTest(_ConnStateBase):
    """① 再导出的名字与 connection 是同一对象（含唯一定义点这一事实）。"""

    def test_lock_is_the_same_object(self):
        self.assertIs(impl._conn_lock, conn_mod._conn_lock)
        # 真 RLock（可重入）——不是被替换成 nullcontext 之类的替身
        self.assertIsInstance(impl._conn_lock, type(threading.RLock()))
        self.assertTrue(impl._conn_lock.acquire(timeout=1.0))
        self.assertTrue(impl._conn_lock.acquire(timeout=1.0), "RLock 必须可重入")
        impl._conn_lock.release()
        impl._conn_lock.release()

    def test_functions_are_the_same_objects(self):
        self.assertIs(impl.get_conn, conn_mod.get_conn)
        self.assertIs(impl.is_initialized, conn_mod.is_initialized)
        self.assertIs(impl.DB_DEFAULT, conn_mod.DB_DEFAULT)

    def test_lock_lives_only_in_connection(self):
        """唯一的定义点：connection 是真正持有这三个名字的模块。"""
        for name in ("_conn", "_conn_lock", "_db_file", "_env_file"):
            self.assertIn(name, vars(conn_mod), f"connection 应定义 {name}")
        self.assertNotIn("_conn", vars(impl), "db 不该再持有自己的 _conn 绑定")


class ReExportNamesTest(_ConnStateBase):
    """③ 旧调用方依赖的下划线名字在 yiban.store.db 上可导入、可读写。"""

    def test_names_importable_from_db(self):
        from yiban.store.db import (  # noqa: F401  # 导入成功本身就是断言
            DB_DEFAULT,
            _conn,
            _conn_lock,
            _db_file,
            _env_file,
            get_conn,
            is_initialized,
        )
        self.assertIs(_conn_lock, conn_mod._conn_lock)
        self.assertIs(get_conn, conn_mod.get_conn)
        self.assertIs(is_initialized, conn_mod.is_initialized)
        self.assertIs(_db_file, conn_mod._db_file)

    def test_reads_fall_through_to_connection(self):
        path = self._temp_db()
        c = impl.init_db(db_file=path, env_file="probe.env", cleanup=False)
        self.assertIs(impl._conn, c)
        self.assertIs(impl._conn, conn_mod.current())
        self.assertEqual(impl._db_file, path)
        self.assertEqual(impl._db_file, conn_mod._db_file)
        self.assertEqual(impl._env_file, "probe.env")
        self.assertEqual(impl._env_file, conn_mod._env_file)

    def test_writes_land_in_connection(self):
        """`db._conn = None` / `db._env_file = path` 必须改到 connection 的真状态。"""
        path = self._temp_db()
        c = impl.init_db(db_file=path, cleanup=False)
        self.assertTrue(impl.is_initialized())

        impl._conn = None                        # 全仓 190+ 处测试收尾的写法
        self.assertIsNone(conn_mod.current(), "置空只写在副本上 → 真连接关不掉")
        self.assertFalse(impl.is_initialized())

        impl._env_file = "/tmp/probe.env"        # test_rekey_key_source 的写法
        self.assertEqual(conn_mod._env_file, "/tmp/probe.env")
        self.assertEqual(impl._resolve_key_env_file()[0], "/tmp/probe.env")

        with self.assertRaises(AttributeError):
            _ = impl.definitely_not_a_name       # __getattr__ 不得吞掉真缺失

        c.close()

    def test_shell_write_forwarding_reaches_connection(self):
        """`scripts/db.py` 壳（旧 `import db`）的读写同样落到 connection。

        这条是 190+ 处 `db._conn = None` 的真正守卫：壳把 setattr 转发到
        `yiban.store.db`，后者必须再转发到 connection，否则照样静默失效。
        """
        shell = _import_shell_CONN()
        self.assertIs(shell.get_conn, conn_mod.get_conn)
        self.assertIs(shell._conn_lock, conn_mod._conn_lock)
        path = self._temp_db()
        shell.init_db(db_file=path, cleanup=False)
        self.assertIs(shell._conn, conn_mod.current())
        shell._conn = None
        self.assertIsNone(conn_mod.current(), "壳上的置空没有落到 connection")
        self.assertIsNone(shell._conn)
        shell._env_file = "via-shell.env"
        self.assertEqual(conn_mod._env_file, "via-shell.env")


class PatchAndDeleteRoundTripTest(_ConnStateBase):
    """外部打桩/撤销路径：`mock.patch.object` / `mock.patch("…_conn")` / `monkeypatch.delattr`。

    `_conn` 等名字不在 db 的 `__dict__` 里（唯一定义点在 connection），而
    `unittest.mock._patch.__exit__` 对"不在 `__dict__` 的名字"走 `delattr` 撤销、随后按
    "删完名字还在不在"决定要不要 `setattr` 回原值。若模块类不实现 `__delattr__`，撤销
    会抛 `AttributeError: _conn`（teardown error）；只实现"删得掉"还不够——必须真的删得掉
    （`hasattr` 变假），否则原值永不被 setattr 回来、打桩静默残留成 None。
    """

    SENTINEL = object()

    def test_patch_object_round_trips_real_state(self):
        path = self._temp_db()
        c = impl.init_db(db_file=path, cleanup=False)
        with mock.patch.object(impl, "_conn", self.SENTINEL):
            self.assertIs(impl._conn, self.SENTINEL, "打桩期间门面读取应看到替身")
            self.assertIs(conn_mod.current(), self.SENTINEL)
        self.assertIs(conn_mod.current(), c,
                      "退出打桩必须把真连接恢复回来（丢成 None 即静默残留）")
        self.assertIs(impl._conn, c)

    def test_patch_string_target_round_trips(self):
        path = self._temp_db()
        c = impl.init_db(db_file=path, cleanup=False)
        with mock.patch("yiban.store.db._conn", self.SENTINEL):
            self.assertIs(impl._conn, self.SENTINEL)
        self.assertIs(conn_mod.current(), c)
        self.assertIs(impl._conn, c)

    def test_monkeypatch_delattr_undo_restores(self):
        """pytest `monkeypatch.delattr` 式撤销：delattr 得掉、undo 后真状态复原。"""
        path = self._temp_db()
        c = impl.init_db(db_file=path, cleanup=False)
        mp = pytest.MonkeyPatch()
        try:
            mp.delattr(impl, "_conn")
            self.assertFalse(hasattr(impl, "_conn"),
                             "删不掉的话 undo 不会 setattr 回原值（mock 同理）")
            with self.assertRaises(AttributeError):
                _ = impl._conn
            self.assertIs(conn_mod.current(), c, "门面摘名不该动 connection 的真状态")
        finally:
            mp.undo()
        self.assertIs(impl._conn, c)
        self.assertIs(conn_mod.current(), c)

    def test_shell_patch_object_round_trips_real_state(self):
        """legacy 壳路径（`import db`）：patch.object 撤销后 connection 真状态必须复原。

        壳的读取回落到实现模块，故 `_patch.__exit__` 的 `delattr` 若只删壳自己的条目，
        `hasattr` 经回落仍为真 → 跳过 setattr 恢复 → `connection._conn` 留下替身。
        """
        shell = _import_shell_CONN()
        path = self._temp_db()
        c = impl.init_db(db_file=path, cleanup=False)
        with mock.patch.object(shell, "_conn", self.SENTINEL):
            self.assertIs(shell._conn, self.SENTINEL, "打桩期间壳的读取应看到替身")
            self.assertIs(conn_mod.current(), self.SENTINEL)
        self.assertIs(conn_mod.current(), c,
                      "壳上的 patch 退出后必须复原真连接，不能留成替身")
        self.assertIs(shell._conn, c)

    def test_shell_monkeypatch_delattr_undo_restores(self):
        """壳上的 `monkeypatch.delattr`：删得掉（不再 AttributeError）、undo 后复原。"""
        shell = _import_shell_CONN()
        path = self._temp_db()
        c = impl.init_db(db_file=path, cleanup=False)
        mp = pytest.MonkeyPatch()
        try:
            mp.delattr(shell, "_conn")
            self.assertFalse(hasattr(shell, "_conn"), "壳上的删除必须让 hasattr 变假")
            self.assertIs(conn_mod.current(), c, "壳摘名不该动 connection 的真状态")
        finally:
            mp.undo()
        self.assertIs(shell._conn, c)
        self.assertIs(conn_mod.current(), c)

    def test_shell_delattr_of_missing_name_still_raises(self):
        shell = _import_shell_CONN()
        with self.assertRaises(AttributeError):
            del shell.definitely_not_a_name

    def test_delattr_of_unknown_name_still_raises(self):
        """非连接状态名字仍走 ModuleType 语义（真缺失必须抛 AttributeError）。"""
        with self.assertRaises(AttributeError):
            del impl.definitely_not_a_name


class InitDbSemanticsTest(_ConnStateBase):
    """② `init_db` 各分支语义（连接、幂等、路径刷新、异常置空）。"""

    def test_init_then_select_one(self):
        path = self._temp_db()
        self.assertFalse(impl.is_initialized())
        c = impl.init_db(db_file=path, cleanup=False)
        self.assertTrue(impl.is_initialized())
        self.assertTrue(os.path.exists(path))
        row = impl.get_conn().execute("SELECT 1").fetchone()
        self.assertEqual(row[0], 1, "get_conn() 必须给出能跑 SQL 的真连接")
        self.assertIs(impl.get_conn(), c)

    def test_second_init_returns_same_conn_but_refreshes_paths(self):
        """连接复用，但 `_db_file`/`_env_file` **无条件刷新**（既有语义，原 db.py:169-170）。"""
        first = self._temp_db()
        c = impl.init_db(db_file=first, env_file="a.env", cleanup=False)
        again = impl.init_db(db_file=os.path.join(os.path.dirname(first), "other.db"),
                             env_file="b.env", cleanup=False)
        self.assertIs(again, c, "第二次 init_db 必须复用同一连接")
        self.assertEqual(conn_mod._db_file, os.path.join(os.path.dirname(first), "other.db"))
        self.assertEqual(conn_mod._env_file, "b.env")

    def test_implicit_init_via_get_conn_uses_env_and_honours_patch(self):
        """裸 `get_conn()` 的隐式初始化走 `db.init_db`（拆到 connection 后仍可打桩）。

        对应 test_scheduler_gate 的 db_export 用例：`mock.patch.object(db, "init_db", …)`
        必须在隐式初始化路径上被看见——故 connection.get_conn 按**属性**取 db.init_db。
        """
        path = self._temp_db()
        os.environ["YIBAN_DB_FILE"] = path
        self._close_current()
        implied = impl.get_conn()
        self.assertIs(impl.get_conn(), implied)
        self.assertEqual(conn_mod._db_file, path)
        implied.close()
        impl._conn = None

        calls = []

        def spy(*a, **kw):
            calls.append(kw)
            return None

        with mock.patch.object(impl, "init_db", side_effect=spy):
            self.assertIsNone(impl.get_conn(), "被打桩的 init_db 建不出连接，应如实返回 None")
        self.assertEqual(len(calls), 1, "隐式初始化必须调用 db.init_db（打桩要打得到）")

    def test_migration_failure_closes_and_resets_conn(self):
        """迁移异常路径：连接关闭 + connection 置空（对应原 `_conn.close(); _conn = None`）。

        唯一一处替身：替换 `_run_migrations` 以触发异常；断言的是**真实**结果
        （连接真的不可再用、状态真的置空），不是替身被调用过。
        """
        path = self._temp_db()

        def boom(_conn):
            raise RuntimeError("迁移失败（用例注入）")

        with mock.patch.object(
            impl, "_run_migrations", side_effect=boom
        ), self.assertRaises(RuntimeError):
            impl.init_db(db_file=path, cleanup=False)
        self.assertIsNone(conn_mod.current(), "异常路径必须置空连接")
        self.assertFalse(impl.is_initialized())

    def test_migrate_false_skips_migrations(self):
        """只读校验类工具的入口（migrate=False）仍被尊重——替身抛错即证明没被调用。"""
        path = self._temp_db()
        with mock.patch.object(impl, "_run_migrations",
                               side_effect=AssertionError("migrate=False 不得跑迁移")):
            c = impl.init_db(db_file=path, cleanup=False, migrate=False)
        self.assertIs(impl.get_conn(), c)
        self.assertEqual(c.execute("SELECT 1").fetchone()[0], 1)
        self.assertTrue(impl.is_initialized())


class DbPathFromEnvFileTest(_ConnStateBase):
    """F1：`YIBAN_DB_FILE` 只写进 .env（未 export）时，库路径必须解析到 .env 指定的库。

    2026-09-21 测试机 47 无上下文 CLI E2E：只写 .env 不导出环境变量时，CLI 的路径
    显示与 `db --status` 认 .env，而 `init_db` 过去只认 `os.environ` → 静默回退默认
    `./yiban.db`（0 账号）→ 引擎"未配置任何账号"、exit 1，错误只进日志文件。
    修法：库路径解析统一走 `env_io.resolve_path`（进程环境 → .env → 默认值），与
    YIBAN_STATE_DIR / YIBAN_LOG_FILE / 审计锚点同口径。
    """

    def setUp(self):
        super().setUp()
        self._prev_env_file_var = os.environ.get("YIBAN_ENV_FILE")
        self.tmp = tempfile.mkdtemp(prefix="yiban-dbpath-envfile-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.db_path = os.path.join(self.tmp, "from-env-file.db")
        self.env_file = os.path.join(self.tmp, ".env")
        with io.open(self.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_DB_FILE={self.db_path}\n")
        # 只写 .env、不导出环境变量（E2E 复现形态）
        os.environ["YIBAN_ENV_FILE"] = self.env_file
        os.environ.pop("YIBAN_DB_FILE", None)

    def tearDown(self):
        if self._prev_env_file_var is None:
            os.environ.pop("YIBAN_ENV_FILE", None)
        else:
            os.environ["YIBAN_ENV_FILE"] = self._prev_env_file_var
        super().tearDown()

    def test_init_db_resolves_db_file_from_env_file(self):
        c = impl.init_db(cleanup=False)
        self.assertEqual(conn_mod._db_file, self.db_path,
                         "库路径应从 .env 解析（而非回退默认 ./yiban.db）")
        self.assertTrue(os.path.isfile(self.db_path), "应在 .env 指定的路径上建库")
        # 连接真正落在 .env 指定的库上（不是默认库）
        rows = c.execute("PRAGMA database_list").fetchall()
        self.assertTrue(any(os.path.realpath(r[2]) == os.path.realpath(self.db_path)
                            for r in rows if r[2]),
                        f"连接指向的库不是 .env 指定的库: {[r[2] for r in rows]}")

    def test_engine_load_accounts_uses_env_file_db(self):
        """引擎取账号（accounts.load_accounts）在只有 .env 配置时解析到 .env 指定的库。"""
        from yiban.engine import accounts as engine_accounts

        os.environ["YIBAN_ACCOUNTS_KEY"] = "b" * 64
        self.addCleanup(os.environ.pop, "YIBAN_ACCOUNTS_KEY", None)
        # 先在 .env 指定的库上建好一个账号（显式路径，模拟 web 侧写入）
        impl.init_db(db_file=self.db_path, env_file=self.env_file, cleanup=False)
        impl.add_account({"name": "A", "phone": "13800138000", "password": "p1",
                          "status": "active", "owner": "admin"})
        self._close_current()
        # 全新进程形态：环境里没有任何 YIBAN_DB_FILE，只有 .env
        loaded = engine_accounts.load_accounts()
        self.assertEqual([a.phone for a in loaded], ["13800138000"],
                         "引擎应解析到 .env 指定的库并取到账号（旧行为：0 账号）")
        self.assertEqual(conn_mod._db_file, self.db_path)


BARE_IMPORT_RE = re.compile(
    r"(?m)^\s*(?:import|from)\s+(?:db|mailer|notify|signin)(?:\s|\.|,|$)"
)


SHELL_MAX_LINES = 300


def _iter_py(root):
    for dirpath, dirnames, filenames in os.walk(os.path.join(BASE, root)):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for name in sorted(filenames):
            if name.endswith(".py"):
                yield os.path.join(dirpath, name)


def _read(path):
    with io.open(path, encoding="utf-8") as f:
        return f.read()


def _count_lines(path):
    return sum(1 for _ in io.open(path, encoding="utf-8"))


class StoreDbLayoutTest(unittest.TestCase):
    def test_yiban_does_not_bare_import_scripts_modules(self):
        """`yiban/**` 不得裸 `import db/mailer/notify/signin`（那依赖 scripts/ 在 sys.path）。"""
        bad = []
        for path in _iter_py("yiban"):
            for m in BARE_IMPORT_RE.finditer(_read(path)):
                rel = os.path.relpath(path, BASE).replace(os.sep, "/")
                bad.append(f"{rel}: {m.group(0).strip()}")
        self.assertEqual(
            bad, [],
            "包内代码必须以 `from yiban.store import db`（或 `from yiban import notify`）"
            "的形式导入，不得裸名依赖 scripts/：" + "; ".join(bad),
        )

    def test_no_other_db_implementation(self):
        """真正的实现在 `yiban/store/db.py`；`scripts/db.py` 只是转发壳。"""
        impl_path = os.path.join(BASE, "yiban", "store", "db.py")
        shell_path = os.path.join(BASE, "scripts", "db.py")
        self.assertTrue(os.path.exists(impl_path), "yiban/store/db.py 缺失")
        self.assertTrue(os.path.exists(shell_path), "scripts/db.py 兼容壳缺失")
        impl_src, shell_src = _read(impl_path), _read(shell_path)
        self.assertRegex(impl_src, r"(?m)^def init_db\(", "实现里应有真正的 init_db 定义")
        self.assertNotRegex(
            shell_src, r"(?m)^def init_db\(",
            "scripts/db.py 又出现了一份实现（应为转发到 yiban.store.db 的兼容壳）",
        )
        self.assertIn("yiban.store import db", shell_src, "壳应转发到 yiban.store.db")
        self.assertIn("_ForwardingModule", shell_src, "壳须把属性写入转发到实现模块")
        impl_lines, shell_lines = _count_lines(impl_path), _count_lines(shell_path)
        self.assertLess(shell_lines, impl_lines,
                        f"壳（{shell_lines} 行）不该与实现（{impl_lines} 行）同量级")
        self.assertLessEqual(shell_lines, SHELL_MAX_LINES,
                             f"壳膨胀到 {shell_lines} 行，已不像转发壳")

    def test_shell_forwards_the_same_objects(self):
        """打桩仍生效的充要条件：壳里的名字与实现是同一对象，且写入会落到实现。

        既有用例大量以 `db.<名字> = 替身` / `mock.patch.object(db, ...)` 打桩；若壳只是
        复制了一份绑定（不转发写入），打桩会落在壳上而实现内部照旧调真名——静默失效。
        """
        import importlib
        import sys

        sys.path.insert(0, os.path.join(BASE, "scripts"))
        shell = importlib.import_module("db")
        impl = importlib.import_module("yiban.store.db")
        self.assertIs(shell.get_conn, impl.get_conn)
        self.assertIs(shell._conn_lock, impl._conn_lock)

        original = impl.audit
        try:
            shell.audit = _sentinel = object()
            self.assertIs(impl.audit, _sentinel, "壳上的打桩没有转发到实现模块")
            impl.audit = original
            self.assertIs(shell.audit, original, "实现上的改动没有回落到壳的读取")
        finally:
            impl.audit = original


DB_PATH = os.path.join(BASE, "yiban", "store", "db.py")


FORWARDED_COUNT = 55


EXPECTED_MODULE_FUNCS = {
    "__getattr__",
    "init_db",
    "resolve_env_file",
    "require_existing_env_file",
    "_begin_immediate",
    "_clock_jump_guard",
    "_record_purge_event",
    "_table_min_max",
    "_cascade_phone_owned",
    "_clear_session_cache_by_phones",
}


EXPECTED_MODULE_CLASSES = {"_StateForwardingModule"}


def _source():
    with open(DB_PATH, encoding="utf-8") as f:
        return f.read()


def _module_defs():
    """db.py 顶层函数名与类名（不含 import 与赋值）。"""
    funcs, classes = set(), set()
    for node in ast.parse(_source()).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            funcs.add(node.name)
        elif isinstance(node, ast.ClassDef):
            classes.add(node.name)
    return funcs, classes


def _reexported_store_modules():
    """`from yiban.store import X as _X`（顶层）收集到的子模块名。"""
    mods = set()
    for node in ast.parse(_source()).body:
        if isinstance(node, ast.ImportFrom) and node.module == "yiban.store":
            for alias in node.names:
                mods.add(alias.name)
    return mods


def _docstring_listed_modules():
    """模块 docstring 中「再导出的同包模块」清单里的反引号模块名。"""
    return set(re.findall(r"(?m)^- `([a-z_]+)`：", impl.__doc__ or ""))


def _docstring_retained_names():
    """「本模块自身仍持有」段里点名的裸标识符（反引号包裹、无点无括号）。"""
    doc = impl.__doc__ or ""
    idx = doc.find("本模块自身仍持有")
    if idx < 0:
        return set()
    return set(re.findall(r"`([A-Za-z_][A-Za-z0-9_]*)`", doc[idx:]))


class ForwardedStateLedgerTest(unittest.TestCase):
    """跨域转发面总账：每一项都必须是"真定义在别处、门面只转发"。"""

    def test_forwarded_count_is_pinned(self):
        self.assertEqual(
            len(impl._FORWARDED_STATE), FORWARDED_COUNT,
            "兼容面转发集项数变化：确认是有意扩缩后同步 FORWARDED_COUNT 与分域契约",
        )

    def test_every_forwarded_name_defined_at_target(self):
        bad = []
        for name, mod in impl._FORWARDED_STATE.items():
            if name in vars(impl):
                bad.append(f"{name}: 同时被 db 自己持有（转发面是假的）")
            elif not hasattr(mod, name):
                bad.append(f"{name}: 在目标模块 {mod.__name__} 未定义")
            elif getattr(impl, name) is not getattr(mod, name):
                bad.append(f"{name}: db 与 {mod.__name__} 上的不是同一对象")
        self.assertEqual(
            bad, [],
            "转发集与真定义点不一致（防往转发集里塞错名字）：\n" + "\n".join(bad),
        )

    def test_no_hidden_forwarded_names_leak(self):
        """`_FORWARDED_STATE_HIDDEN` 是 delattr 的临时摘名表，用例间不得有残留。"""
        self.assertEqual(
            set(impl._FORWARDED_STATE_HIDDEN), set(),
            "有转发名停在隐藏态（delattr 后未恢复）：后续读取会误报缺失，打桩撤销会残留",
        )


class FacadeDocstringLedgerTest(unittest.TestCase):
    """docstring 的"再导出模块"与"留守"两张清单必须与实际一致。"""

    def test_docstring_lists_every_reexported_store_module(self):
        self.assertEqual(
            _docstring_listed_modules(), _reexported_store_modules(),
            "docstring 的再导出模块清单与实际 `from yiban.store import …` 不一致",
        )

    def test_retained_names_are_defined_here(self):
        names = _docstring_retained_names()
        self.assertTrue(names, "docstring 未点名任何留守名字（清单被删或改写了格式）")
        for name in names:
            self.assertIn(name, vars(impl), f"docstring 说 {name} 留守，但 db 未定义它")
            self.assertEqual(
                getattr(vars(impl)[name], "__module__", None), "yiban.store.db",
                f"docstring 说 {name} 留守，但它的定义点不在本模块",
            )

    def test_module_defines_only_facade_and_glue(self):
        funcs, classes = _module_defs()
        self.assertEqual(
            funcs, EXPECTED_MODULE_FUNCS,
            "db.py 顶层函数定义集变化：新迁出/迁入都要有意识（并同步此表与 docstring）",
        )
        self.assertEqual(
            classes, EXPECTED_MODULE_CLASSES,
            "db.py 顶层类定义集变化：转发模块类应唯一",
        )


PHONE = "13800001234"


MOVED_NAMES_SESSION = (
    "_session_cache_now",
    "_session_cache_key",
    "_session_cache_ttl_hours",
    "get_session_cache",
    "set_session_cache",
    "clear_session_cache",
)


MOVED_CONSTANTS = (
    "SESSION_CACHE_TTL_HOURS_DEFAULT",
    "SESSION_CACHE_TTL_HOURS_MIN",
    "SESSION_CACHE_TTL_HOURS_MAX",
    "SESSION_CACHE_HKDF_INFO",
)


def _import_shell_SESSION():
    """`scripts/db.py` 兼容壳（旧 `import db` 路径）。"""
    sys.path.insert(0, os.path.join(BASE, "scripts"))
    return importlib.import_module("db")


class _SessionCacheSplitBase(unittest.TestCase):
    """连接状态与密钥路径的公共收发。

    连接收尾走门面（顺带覆盖写入转发）；`_connection._env_file` / `_db_file` 与
    `YIBAN_DB_FILE` 是**模块级状态**，必须保存/还原——只置空连接不还原路径，就会给后续
    用例留下本文件的临时库路径（顺序依赖隐患，与 `_ConnStateBase` 同款）。
    """

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-session-split-")
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
        path = os.path.join(tempfile.mkdtemp(prefix="yiban-session-split-db-"),
                            "yiban.db")
        self.addCleanup(shutil.rmtree, os.path.dirname(path), ignore_errors=True)
        impl.init_db(path, env_file=self.env_file, cleanup=False)
        return path


class SameObjectTest_SESSION(_SessionCacheSplitBase):
    """① 门面读取回落到 session_cache，且 db 不再持有自己的绑定。"""

    def test_functions_are_the_same_objects(self):
        for name in MOVED_NAMES_SESSION:
            self.assertIs(getattr(impl, name), getattr(cache_mod, name),
                          f"db.{name} 与 session_cache.{name} 不是同一对象")

    def test_definitions_live_only_in_session_cache(self):
        """唯一的定义点：session_cache 才是真正持有这些名字的模块。"""
        for name in MOVED_NAMES_SESSION:
            self.assertIn(name, vars(cache_mod), f"session_cache 应定义 {name}")
            self.assertNotIn(name, vars(impl), f"db 不该再持有自己的 {name} 绑定")

    def test_names_importable_from_db(self):
        from yiban.store.db import (  # noqa: F401  # 导入成功本身就是断言
            _session_cache_now,
            _session_cache_ttl_hours,
            clear_session_cache,
            get_session_cache,
            set_session_cache,
        )
        self.assertIs(get_session_cache, cache_mod.get_session_cache)
        self.assertIs(_session_cache_now, cache_mod._session_cache_now)

    def test_constants_readable_on_facade(self):
        """常量唯一定义点在本模块，门面再导出同一值（读面零损失）。"""
        for name in MOVED_CONSTANTS:
            self.assertIs(getattr(impl, name), getattr(cache_mod, name),
                          f"db.{name} 与 session_cache.{name} 不是同一值")
        self.assertEqual(impl.SESSION_CACHE_TTL_HOURS_DEFAULT, 6)


class WriteForwardingTest_SESSION(_SessionCacheSplitBase):
    """② 写入落到真定义点；③ 模块内部调用点看到门面上的替身。"""

    SENTINEL = object()

    def test_plain_assignment_lands_in_session_cache(self):
        original = cache_mod.get_session_cache
        try:
            impl.get_session_cache = self.SENTINEL
            self.assertIs(cache_mod.get_session_cache, self.SENTINEL,
                          "门面上的赋值没有落到真定义点")
            self.assertIs(impl.get_session_cache, self.SENTINEL)
        finally:
            impl.get_session_cache = original
        self.assertIs(cache_mod.get_session_cache, original)

    def test_internal_caller_sees_patched_clock(self):
        """`db._session_cache_now = 替身` 必须被内部 `set_session_cache` 看见（读写同钟）。"""
        self._init()
        now = datetime.datetime(2026, 9, 1, 15, 0, 0)
        with mock.patch.object(impl, "_session_cache_now", return_value=now):
            cache_mod.set_session_cache(PHONE, '{"a":"1"}', "c")
        row = impl.get_conn().execute(
            "SELECT updated_at FROM session_cache WHERE phone=?", (PHONE,)
        ).fetchone()
        self.assertEqual(row["updated_at"], "2026-09-01 15:00:00",
                         "内部调用点没走门面上的替身")

    def test_internal_caller_sees_patched_ttl(self):
        """打桩 `db._session_cache_ttl_hours` 后，内部 `get_session_cache` 必须按替身判定。"""
        self._init()
        impl.set_session_cache(PHONE, '{"a":"1"}', "c")
        self.assertIsNotNone(impl.get_session_cache(PHONE), "前置：默认 TTL 内应命中")
        with mock.patch.object(impl, "_session_cache_ttl_hours", return_value=0.0):
            self.assertIsNone(cache_mod.get_session_cache(PHONE),
                              "内部调用点没走门面上的替身")

    def test_patch_object_round_trips(self):
        real = cache_mod._session_cache_now
        with mock.patch.object(impl, "_session_cache_now", self.SENTINEL):
            self.assertIs(cache_mod._session_cache_now, self.SENTINEL)
        self.assertIs(cache_mod._session_cache_now, real,
                      "退出打桩必须把真函数恢复回来（丢成 None 即静默残留）")
        self.assertIs(impl._session_cache_now, real)

    def test_patch_string_target_round_trips(self):
        real = cache_mod.get_session_cache
        with mock.patch("yiban.store.db.get_session_cache", self.SENTINEL):
            self.assertIs(cache_mod.get_session_cache, self.SENTINEL)
        self.assertIs(cache_mod.get_session_cache, real)
        self.assertIs(impl.get_session_cache, real)

    def test_shell_write_forwarding_reaches_session_cache(self):
        """`scripts/db.py` 壳（旧 `import db`）的写入同样落到 session_cache。"""
        shell = _import_shell_SESSION()
        original = cache_mod.set_session_cache
        try:
            shell.set_session_cache = self.SENTINEL
            self.assertIs(cache_mod.set_session_cache, self.SENTINEL,
                          "壳上的赋值经 db 门面后没有落到真定义点")
        finally:
            shell.set_session_cache = original
        self.assertIs(cache_mod.set_session_cache, original)

    def test_shell_patch_object_round_trips(self):
        shell = _import_shell_SESSION()
        real_now, real_get = cache_mod._session_cache_now, cache_mod.get_session_cache
        with mock.patch.object(shell, "_session_cache_now", self.SENTINEL):
            self.assertIs(cache_mod._session_cache_now, self.SENTINEL)
        self.assertIs(cache_mod._session_cache_now, real_now)
        self.assertIs(cache_mod.get_session_cache, real_get)


class DeleteHidingTest_SESSION(_SessionCacheSplitBase):
    """④ delattr 只摘门面上的名字，真定义与模块内部调用不受影响。"""

    def test_delattr_hides_name_on_facade(self):
        real = cache_mod.clear_session_cache
        del impl.clear_session_cache
        self.assertFalse(hasattr(impl, "clear_session_cache"),
                         "删不掉的话 patch 撤销不会 setattr 回原值")
        with self.assertRaises(AttributeError):
            _ = impl.clear_session_cache
        self.assertIs(cache_mod.clear_session_cache, real, "摘名不该动真定义")
        impl.clear_session_cache = real
        self.assertIs(impl.clear_session_cache, real)

    def test_delattr_of_unknown_name_still_raises(self):
        with self.assertRaises(AttributeError):
            del impl.definitely_not_a_name

    def test_monkeypatch_delattr_undo_restores(self):
        real = cache_mod._session_cache_key
        mp = pytest.MonkeyPatch()
        try:
            mp.delattr(impl, "_session_cache_key")
            self.assertFalse(hasattr(impl, "_session_cache_key"))
            self.assertIs(cache_mod._session_cache_key, real)
        finally:
            mp.undo()
        self.assertIs(impl._session_cache_key, real)
        self.assertIs(cache_mod._session_cache_key, real)


class FacadeBehaviourTest_SESSION(_SessionCacheSplitBase):
    """真实可用：经门面走一遍写/读/作废/清除，并钉住日志通道细分。"""

    def setUp(self):
        super().setUp()
        self._init()

    def test_round_trip_through_facade(self):
        self.assertIsNone(impl.get_session_cache(PHONE), "未写入时返回 None")
        cookie_json = json.dumps({"sessionid": "sid-1", "csrf_token": "abc"})
        impl.set_session_cache(PHONE, cookie_json, "csrf-1")
        got = impl.get_session_cache(PHONE)
        self.assertEqual(got["cookies"], cookie_json, "cookies 应透明解密还原")
        self.assertEqual(got["csrf"], "csrf-1")
        raw = impl.get_conn().execute(
            "SELECT cookies_ct, csrf FROM session_cache WHERE phone=?", (PHONE,)
        ).fetchone()
        self.assertNotIn("sid-1", raw["cookies_ct"], "库内应为密文")
        self.assertNotIn("csrf-1", raw["csrf"], "csrf 同为准密文")

        impl.clear_session_cache(PHONE)
        self.assertIsNone(impl.get_session_cache(PHONE))
        impl.clear_session_cache(PHONE)  # 幂等：行不存在时不报错

    def test_invalidation_log_uses_new_channel_only(self):
        """日志通道细分为 `yiban.store.session_cache`；旧通道 `yiban.db` 不再收到本域日志。"""
        now = datetime.datetime(2026, 9, 1, 15, 0, 0)
        with mock.patch.object(impl, "_session_cache_now", return_value=now):
            impl.set_session_cache(PHONE, '{"a":"1"}', "c")
        two_days_later = now + datetime.timedelta(days=2)
        with mock.patch.object(impl, "_session_cache_now", return_value=two_days_later), \
                self.assertLogs("yiban.store.session_cache", level="INFO") as captured, \
                self.assertNoLogs("yiban.db", level="INFO"):
            self.assertIsNone(impl.get_session_cache(PHONE), "跨业务日缓存必须作废")
        self.assertIn("跨业务日", "\n".join(captured.output))
        self.assertEqual(
            impl.get_conn().execute(
                "SELECT COUNT(*) FROM session_cache").fetchone()[0],
            0, "作废行应在读取时被顺手清除",
        )


OTHER_PHONE = "13800005678"


ADMIN = "admin@test.local"


PAUSER = "user1@test.local"


MOVED_TO_TIME_PREFS = (
    "last_time_pref_set_at",
    "time_pref_set_count_since",
    "get_time_prefs",
    "get_time_pref",
    "set_time_pref",
    "clear_time_pref",
    "time_pref_stats",
)


MOVED_TO_EVENTS = (
    "last_pause_at",
    "pause_count_since",
)


MOVED_NAMES_PREFS = MOVED_TO_TIME_PREFS + MOVED_TO_EVENTS


def _import_shell_PREFS():
    """`scripts/db.py` 兼容壳（旧 `import db` 路径）。"""
    sys.path.insert(0, os.path.join(BASE, "scripts"))
    return importlib.import_module("db")


class _TimePrefsSplitBase(unittest.TestCase):
    """连接状态与密钥路径的公共收发。

    连接收尾走门面（顺带覆盖写入转发）；`_connection._env_file` / `_db_file` 与
    `YIBAN_DB_FILE` 是**模块级状态**，必须保存/还原——只置空连接不还原路径，就会给后续
    用例留下本文件的临时库路径（顺序依赖隐患，与 `_ConnStateBase` 同款）。
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


class SameObjectTest_PREFS(_TimePrefsSplitBase):
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
        for name in MOVED_NAMES_PREFS:
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


class WriteForwardingTest_PREFS(_TimePrefsSplitBase):
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
        shell = _import_shell_PREFS()
        original = prefs_mod.time_pref_stats
        try:
            shell.time_pref_stats = self.SENTINEL
            self.assertIs(prefs_mod.time_pref_stats, self.SENTINEL,
                          "壳上的赋值经 db 门面后没有落到真定义点")
        finally:
            shell.time_pref_stats = original
        self.assertIs(prefs_mod.time_pref_stats, original)

    def test_shell_patch_object_round_trips(self):
        shell = _import_shell_PREFS()
        real_pref, real_pause = prefs_mod.get_time_pref, events_mod.last_pause_at
        with mock.patch.object(shell, "get_time_pref", self.SENTINEL):
            self.assertIs(prefs_mod.get_time_pref, self.SENTINEL)
            self.assertIs(events_mod.last_pause_at, real_pause)
        self.assertIs(prefs_mod.get_time_pref, real_pref)
        self.assertIs(events_mod.last_pause_at, real_pause)


class DeleteHidingTest_PREFS(_TimePrefsSplitBase):
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


class FacadeBehaviourTest_PREFS(_TimePrefsSplitBase):
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


SALT_A = "A" * 32


SALT_B = "B" * 32


MOVED_TO_TRACKING = (
    "_write_track_salt_to_env_file",
    "_track_salt",
    "hash_ip",
    "hash_phone",
)


MOVED_TO_MIGRATIONS = (
    "_maybe_migrate",
    "_rename_backup",
)


MOVED_NAMES_TRACK = MOVED_TO_TRACKING + MOVED_TO_MIGRATIONS


FORWARDED_STATE_NAME = "_TRACK_SALT_CACHE"


def _import_shell_TRACK():
    """`scripts/db.py` 兼容壳（旧 `import db` 路径）。"""
    sys.path.insert(0, os.path.join(BASE, "scripts"))
    return importlib.import_module("db")


class _TrackingSplitBase(unittest.TestCase):
    """连接状态、密钥/盐路径与盐缓存的公共收发。

    `_connection._env_file` / `_db_file`、`YIBAN_DB_FILE` / `YIBAN_ENV_FILE` /
    `YIBAN_TRACK_SALT` 与进程内盐缓存都是**模块级状态**，必须保存/还原——只置空连接不还原
    路径或盐缓存，就会给后续用例留下本文件的临时源与盐（顺序依赖隐患，与
    `_ConnStateBase` 同款）。
    """

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-tracking-split-")
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
        self._prev_envs = {k: os.environ.get(k) for k in (
            "YIBAN_DB_FILE", "YIBAN_ENV_FILE", "YIBAN_TRACK_SALT")}
        self._prev_salt_cache = impl._TRACK_SALT_CACHE
        self._close_conn()
        os.environ.pop("YIBAN_TRACK_SALT", None)
        impl._env_file = None
        impl._TRACK_SALT_CACHE = None

    def tearDown(self):
        self._close_conn()
        for k, v in self._prev_envs.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        conn_mod._env_file = self._prev_env_file
        conn_mod._db_file = self._prev_db_file
        impl._TRACK_SALT_CACHE = self._prev_salt_cache

    def _close_conn(self):
        conn = conn_mod.current()
        if conn is not None:
            with contextlib.suppress(Exception):
                conn.close()
        impl._conn = None

    def _fresh_db_path(self):
        path = os.path.join(tempfile.mkdtemp(prefix="yiban-tracking-split-db-"),
                            "yiban.db")
        self.addCleanup(shutil.rmtree, os.path.dirname(path), ignore_errors=True)
        return path

    def _write_env(self, path, lines):
        with io.open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")


class SameObjectTest_TRACK(_TrackingSplitBase):
    """① 门面读取回落到定义点，且 db 不再持有自己的绑定。"""

    def test_functions_are_the_same_objects(self):
        for name in MOVED_TO_TRACKING:
            self.assertIs(getattr(impl, name), getattr(track_mod, name),
                          f"db.{name} 与 tracking.{name} 不是同一对象")
        for name in MOVED_TO_MIGRATIONS:
            self.assertIs(getattr(impl, name), getattr(migrations_mod, name),
                          f"db.{name} 与 migrations.{name} 不是同一对象")
        self.assertIs(impl._TRACK_SALT_CACHE, track_mod._TRACK_SALT_CACHE)

    def test_definitions_live_only_in_owning_modules(self):
        for name in (*MOVED_NAMES_TRACK, FORWARDED_STATE_NAME):
            self.assertNotIn(name, vars(impl), f"db 不该再持有自己的 {name} 绑定")
        for name in MOVED_TO_TRACKING:
            self.assertIn(name, vars(track_mod), f"tracking 应定义 {name}")
        for name in MOVED_TO_MIGRATIONS:
            self.assertIn(name, vars(migrations_mod), f"migrations 应定义 {name}")
        self.assertIn(FORWARDED_STATE_NAME, vars(track_mod))
        # 盐缓存锁只被本域内部使用、全仓无外部引用 → 随域朴素搬走，不做门面再导出
        self.assertIn("_TRACK_SALT_LOCK", vars(track_mod))
        self.assertNotIn("_TRACK_SALT_LOCK", vars(impl))

    def test_names_importable_from_db(self):
        from yiban.store.db import (
            _maybe_migrate,
            _rename_backup,
            _track_salt,
            _write_track_salt_to_env_file,
            hash_ip,
            hash_phone,
        )
        self.assertIs(hash_ip, track_mod.hash_ip)
        self.assertIs(hash_phone, track_mod.hash_phone)
        self.assertIs(_track_salt, track_mod._track_salt)
        self.assertIs(_write_track_salt_to_env_file, track_mod._write_track_salt_to_env_file)
        self.assertIs(_maybe_migrate, migrations_mod._maybe_migrate)
        self.assertIs(_rename_backup, migrations_mod._rename_backup)

    def test_account_crypto_binding_kept_on_facade(self):
        """唯一自用点随迁移域迁走，但 `db.account_crypto` 仍被测试取用 → 绑定保留。"""
        self.assertIn("account_crypto", vars(impl))


class WriteForwardingTest_TRACK(_TrackingSplitBase):
    """② 写入落到真定义点；③ 可变状态转发；④ 门面内的晚解析。"""

    SENTINEL = object()

    def test_plain_assignment_lands_in_defining_module(self):
        for name in MOVED_TO_TRACKING + MOVED_TO_MIGRATIONS:
            owner = track_mod if name in MOVED_TO_TRACKING else migrations_mod
            original = getattr(owner, name)
            try:
                setattr(impl, name, self.SENTINEL)
                self.assertIs(getattr(owner, name), self.SENTINEL,
                              f"门面上的 {name} 赋值没有落到真定义点")
                self.assertIs(getattr(impl, name), self.SENTINEL)
            finally:
                setattr(impl, name, original)
            self.assertIs(getattr(owner, name), original)

    def test_salt_cache_assignment_lands_in_tracking(self):
        original = track_mod._TRACK_SALT_CACHE
        try:
            impl._TRACK_SALT_CACHE = self.SENTINEL
            self.assertIs(track_mod._TRACK_SALT_CACHE, self.SENTINEL,
                          "盐缓存的赋值没有落到定义点（清缓存会静默失效）")
            self.assertIs(impl._TRACK_SALT_CACHE, self.SENTINEL)
        finally:
            impl._TRACK_SALT_CACHE = original
        self.assertIs(track_mod._TRACK_SALT_CACHE, original)

    def test_patch_object_round_trips(self):
        real = track_mod.hash_ip
        with mock.patch.object(impl, "hash_ip", self.SENTINEL):
            self.assertIs(track_mod.hash_ip, self.SENTINEL)
        self.assertIs(track_mod.hash_ip, real,
                      "退出打桩必须把真函数恢复回来（丢成 None 即静默残留）")
        self.assertIs(impl.hash_ip, real)

    def test_patch_object_round_trips_on_migrations_name(self):
        real = migrations_mod._rename_backup
        with mock.patch.object(impl, "_rename_backup", self.SENTINEL):
            self.assertIs(migrations_mod._rename_backup, self.SENTINEL)
        self.assertIs(migrations_mod._rename_backup, real)
        self.assertIs(impl._rename_backup, real)

    def test_patch_string_target_round_trips(self):
        real = track_mod.hash_phone
        with mock.patch("yiban.store.db.hash_phone", self.SENTINEL):
            self.assertIs(track_mod.hash_phone, self.SENTINEL)
        self.assertIs(track_mod.hash_phone, real)
        self.assertIs(impl.hash_phone, real)

    def test_shell_write_forwarding_reaches_tracking(self):
        """`scripts/db.py` 壳（旧 `import db`）的写入同样落到 tracking。"""
        shell = _import_shell_TRACK()
        original = track_mod.hash_ip
        try:
            shell.hash_ip = self.SENTINEL
            self.assertIs(track_mod.hash_ip, self.SENTINEL,
                          "壳上的赋值经 db 门面后没有落到真定义点")
        finally:
            shell.hash_ip = original
        self.assertIs(track_mod.hash_ip, original)

    def test_shell_patch_object_round_trips(self):
        shell = _import_shell_TRACK()
        real = track_mod._track_salt
        with mock.patch.object(shell, "_track_salt", self.SENTINEL):
            self.assertIs(track_mod._track_salt, self.SENTINEL)
        self.assertIs(track_mod._track_salt, real)
        self.assertIs(impl._track_salt, real)

    def test_maybe_migrate_stub_seen_by_init_db(self):
        """门面内的 `init_db` 必须看见 `db._maybe_migrate = 替身`（晚解析）。"""
        db_path = self._fresh_db_path()
        accounts_json = os.path.join(os.path.dirname(db_path), "accounts.json")
        with open(accounts_json, "w", encoding="utf-8") as f:
            json.dump([{"phone": "13800138000", "password": "pw"}], f)
        stub = mock.MagicMock()
        with mock.patch.object(impl, "_maybe_migrate", stub):
            conn = impl.init_db(db_path, migrate_from=accounts_json,
                                env_file=self.env_file, cleanup=False)
        stub.assert_called_once_with(conn, accounts_json)
        self.assertTrue(os.path.exists(accounts_json),
                        "替身在场时真导入一次也没跑（JSON 未被改名 .bak）")


class DeleteHidingTest_TRACK(_TrackingSplitBase):
    """⑤ delattr 只摘门面上的名字，真定义与模块内部调用不受影响。"""

    def test_delattr_hides_name_on_facade(self):
        real = track_mod.hash_ip
        del impl.hash_ip
        self.assertFalse(hasattr(impl, "hash_ip"),
                         "删不掉的话 patch 撤销不会 setattr 回原值")
        with self.assertRaises(AttributeError):
            _ = impl.hash_ip
        self.assertIs(track_mod.hash_ip, real, "摘名不该动真定义")
        impl.hash_ip = real
        self.assertIs(impl.hash_ip, real)

    def test_delattr_hides_migration_name_on_facade(self):
        real = migrations_mod._maybe_migrate
        del impl._maybe_migrate
        self.assertFalse(hasattr(impl, "_maybe_migrate"))
        self.assertIs(migrations_mod._maybe_migrate, real)
        impl._maybe_migrate = real
        self.assertIs(impl._maybe_migrate, real)

    def test_delattr_of_unknown_name_still_raises(self):
        with self.assertRaises(AttributeError):
            del impl.definitely_not_a_name

    def test_monkeypatch_delattr_undo_restores(self):
        real = track_mod._track_salt
        mp = pytest.MonkeyPatch()
        try:
            mp.delattr(impl, "_track_salt")
            self.assertFalse(hasattr(impl, "_track_salt"))
            self.assertIs(track_mod._track_salt, real)
        finally:
            mp.undo()
        self.assertIs(impl._track_salt, real)
        self.assertIs(track_mod._track_salt, real)


class SaltBehaviourTest(_TrackingSplitBase):
    """真实可用：加盐哈希对盐敏感、盐缓存转发可清、落盘保留其他行。"""

    def test_hash_ip_stable_and_salt_sensitive(self):
        os.environ["YIBAN_TRACK_SALT"] = SALT_A
        h1 = impl.hash_ip("1.2.3.4")
        self.assertEqual(h1, impl.hash_ip("1.2.3.4"), "同盐同输入必须稳定")
        self.assertNotEqual(h1, impl.hash_ip("5.6.7.8"), "不同 IP 必须不同")
        self.assertEqual(len(h1), 64)
        os.environ["YIBAN_TRACK_SALT"] = SALT_B
        self.assertNotEqual(impl.hash_ip("1.2.3.4"), h1,
                            "换盐必须改变 IP 哈希（盐是唯一密钥）")

    def test_hash_phone_stable_and_salt_sensitive(self):
        os.environ["YIBAN_TRACK_SALT"] = SALT_A
        h1 = impl.hash_phone(PHONE)
        self.assertEqual(h1, impl.hash_phone(PHONE), "同盐同手机号必须稳定（库内关联键）")
        self.assertEqual(len(h1), 64)
        os.environ["YIBAN_TRACK_SALT"] = SALT_B
        self.assertNotEqual(impl.hash_phone(PHONE), h1, "换盐必须改变手机号哈希")
        self.assertNotEqual(impl.hash_phone(PHONE), impl.hash_ip(PHONE),
                            "两名有意取不同口径（sha256 关联键 / HMAC 限速键）")

    def test_hash_uses_salt_from_cache_then_env_file(self):
        """③ 盐缓存：门面清空缓存后必须重新按 .env 解析（转发写到定义点才成立）。"""
        os.environ["YIBAN_TRACK_SALT"] = SALT_A
        h_a = impl.hash_ip("1.2.3.4")
        self.assertEqual(track_mod._TRACK_SALT_CACHE, SALT_A)
        # 环境变量撤下、改由另一份 .env 提供盐
        os.environ.pop("YIBAN_TRACK_SALT")
        env2 = os.path.join(self.tmp, "salt-b.env")
        self._write_env(env2, [f"YIBAN_ACCOUNTS_KEY={TEST_KEY}",
                               f"YIBAN_TRACK_SALT={SALT_B}"])
        impl._env_file = env2
        self.assertEqual(impl._track_salt(), SALT_A, "缓存命中：仍是上次解析出的盐")
        self.assertEqual(impl.hash_ip("1.2.3.4"), h_a)
        impl._TRACK_SALT_CACHE = None
        self.assertEqual(impl._track_salt(), SALT_B,
                         "门面上清缓存必须清到定义点那份，否则换源后仍返回旧盐")
        self.assertNotEqual(impl.hash_ip("1.2.3.4"), h_a)
        self.assertEqual(track_mod._TRACK_SALT_CACHE, SALT_B)

    def test_write_track_salt_reaches_env_file_via_facade(self):
        env3 = os.path.join(self.tmp, "write-salt.env")
        self._write_env(env3, ["YIBAN_OTHER=1"])
        self.assertEqual(impl._write_track_salt_to_env_file(env3, "salt123"), "salt123")
        content = io.open(env3, encoding="utf-8").read()
        self.assertIn("YIBAN_TRACK_SALT=salt123", content)
        self.assertIn("YIBAN_OTHER=1", content, "必须保留其他行")
        self.assertEqual([n for n in os.listdir(self.tmp) if "write-salt.env.tmp" in n], [],
                         "临时文件必须被 replace 掉，不得残留")
        # 已有盐时直接返回既有值（不覆盖），保证"一个 .env 一把盐"
        self.assertEqual(impl._write_track_salt_to_env_file(env3, "other"), "salt123")


class LogChannelTest(_TrackingSplitBase):
    """日志通道细分：新通道收到本域日志，旧通道 `yiban.db` 不再收到。"""

    def test_weak_salt_warning_uses_tracking_channel_only(self):
        os.environ["YIBAN_TRACK_SALT"] = "short"
        with self.assertLogs("yiban.store.tracking", level="WARNING") as captured, \
                self.assertNoLogs("yiban.db", level="WARNING"):
            self.assertEqual(impl._track_salt(), "short")
        self.assertIn("长度过短", "\n".join(captured.output))

    def test_import_failure_uses_migrations_channel_only(self):
        db_path = self._fresh_db_path()
        accounts_json = os.path.join(os.path.dirname(db_path), "accounts.json")
        with open(accounts_json, "w", encoding="utf-8") as f:
            f.write("{ this is not valid json !!!")
        with self.assertLogs("yiban.store.migrations", level="ERROR") as captured, \
                self.assertNoLogs("yiban.db", level="ERROR"):
            impl.init_db(db_path, migrate_from=accounts_json,
                         env_file=self.env_file, cleanup=False)
        joined = "\n".join(captured.output)
        self.assertIn("读取/解析失败", joined, "损坏 JSON 必须显式记 ERROR")
        self.assertNotIn("无 JSON 数据可迁移", joined, "不得误报为无数据")


class A2DecryptOutOfLockTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-a2-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_DB_FILE"] = cls.db_file
        os.environ["YIBAN_STATE_DIR"] = cls.tmp
        global db
        import db

    @classmethod
    def tearDownClass(cls):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        os.environ.pop("YIBAN_ACCOUNTS_KEY", None)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        for suffix in ("", "-wal", "-shm"):
            p = self.db_file + suffix
            if os.path.exists(p):
                os.remove(p)
        db.init_db(self.db_file, env_file=self.env_file, cleanup=False)

    def _add(self, phone, password="pw"):
        return db.add_account({"name": phone[-4:], "phone": phone, "password": password})

    # ---- 1. 核心性质：解密不在 _conn_lock 内 ----
    def test_decrypt_runs_outside_conn_lock(self):
        self._add("13800138001")
        real = db._decrypt_row
        probed = []

        def _spy(row):
            # 解密进行中，另一个线程应能立刻拿到 _conn_lock；
            # 若解密仍在锁内，acquire 会超时失败（False）
            def _probe():
                got = db._conn_lock.acquire(timeout=2.0)
                probed.append(got)
                if got:
                    db._conn_lock.release()

            t = threading.Thread(target=_probe)
            t.start()
            t.join(timeout=3.0)
            return real(row)

        with mock.patch.object(db, "_decrypt_row", side_effect=_spy):
            accts = db.load_accounts()
        self.assertTrue(accts, "应至少解出一行")
        self.assertTrue(probed, "_decrypt_row 应被调用")
        self.assertTrue(
            all(probed),
            "解密期间 _conn_lock 必须能被其他线程取得（即解密不在锁内）",
        )

    # ---- 2. 并发读 + 改绑手机号（换 AAD）不得崩 ----
    def test_concurrent_load_and_phone_rebind_no_crash(self):
        ids = [self._add(f"1380013810{i}", f"pw{i}") for i in range(6)]
        stop = threading.Event()
        errors = []

        def _reader():
            while not stop.is_set():
                try:
                    accts = db.load_accounts()
                    for a in accts:
                        if not a.get("phone"):
                            errors.append(AssertionError("返回了半成品行（phone 为空）"))
                except Exception as e:
                    errors.append(e)

        threads = [threading.Thread(target=_reader) for _ in range(4)]
        for t in threads:
            t.start()
        try:
            for _ in range(15):
                for i, aid in enumerate(ids[:3]):
                    db.update_account(aid, {"phone": f"139001382{i:02d}", "password": "npw"})
        finally:
            stop.set()
            for t in threads:
                t.join(timeout=15)

        self.assertEqual(errors, [], f"并发读不得抛错：{errors[:3]}")

    # ---- 3. AAD 失配重试一次；重试仍失败则抛出 ----
    def test_read_accounts_retries_once_on_runtime_error(self):
        calls = []

        def _flaky(rows):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("AAD 失配（模拟并发改绑手机号）")
            return ["ok"]

        with mock.patch.object(db, "decrypt_account_rows", side_effect=_flaky):
            self.assertEqual(db.read_accounts(db.accounts_snapshot), ["ok"])
        self.assertEqual(len(calls), 2, "首次失败后应重取快照重试一次")

    def test_read_accounts_raises_after_retry_fails(self):
        with mock.patch.object(
            db, "decrypt_account_rows", side_effect=RuntimeError("密文真实损坏")
        ), self.assertRaises(RuntimeError):
            db.read_accounts(db.accounts_snapshot)

    # ---- 4. 快照（不解密）与解密读口径一致 ----
    def test_snapshot_and_load_accounts_agree_on_order_and_identity(self):
        for i in range(4):
            self._add(f"1380013830{i}", f"pw{i}")
        snap = db.accounts_snapshot()
        accts = db.load_accounts()
        self.assertEqual([r["id"] for r in snap], [a["id"] for a in accts])
        self.assertEqual([r["phone"] for r in snap], [a["phone"] for a in accts])
        self.assertTrue(all(a["password"].startswith("pw") for a in accts), "应已解密为明文")
        self.assertTrue(all(isinstance(r["deleted"], bool) for r in snap))

    # ---- 5. 明文自愈在锁外解密后仍然生效 ----
    def test_plaintext_self_heal_still_persists(self):
        conn = db.get_conn()
        conn.execute(
            "INSERT INTO accounts (sort_order, name, phone, password) "
            "VALUES (1, '甲', '13800138099', 'PlainPW')"
        )
        conn.commit()
        with self.assertLogs("yiban.store.accounts", level="WARNING") as cm:
            accts = db.load_accounts()
        self.assertEqual(accts[0]["password"], "PlainPW", "明文值照常可用（不阻断业务）")
        self.assertTrue(any("已自动加密回写" in m for m in cm.output), cm.output)
        row = db.accounts_snapshot()[0]
        self.assertNotIn("PlainPW", str(row["password"]), "自愈必须落盘（不得只在内存）")


sys.path.insert(0, os.path.join(BASE, "scripts"))


ADMIN_PASS = "TestPass1234!"


USER_PASS = "secret1"


EMAIL = "u1@test.local"


PHONE_D6 = "13800138000"


class _WebBase(unittest.TestCase):
    """临时 .env/DB + 全新 app（与既有 web 类测试同一骨架）。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-d6-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        os.environ.update({
            "YIBAN_ACCOUNTS_KEY": TEST_KEY,
            "YIBAN_ENV_FILE": cls.env_file,
            "YIBAN_DB_FILE": cls.db_file,
            "YIBAN_STATE_DIR": cls.tmp,
            "YIBAN_LOG_FILE": os.path.join(cls.tmp, "sign.log"),
        })
        spec = importlib.util.spec_from_file_location(
            "webapp", os.path.join(BASE, "web", "app.py"))
        cls.webapp = importlib.util.module_from_spec(spec)
        sys.modules["webapp"] = cls.webapp
        with contextlib.suppress(Exception):
            spec.loader.exec_module(cls.webapp)

    @classmethod
    def tearDownClass(cls):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k in ("YIBAN_ACCOUNTS_KEY", "YIBAN_ENV_FILE", "YIBAN_DB_FILE",
                  "YIBAN_STATE_DIR", "YIBAN_LOG_FILE"):
            os.environ.pop(k, None)

    def setUp(self):
        with open(self.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
                    f"YIBAN_ADMIN_USER=admin\nYIBAN_ADMIN_PASSWORD={ADMIN_PASS}\n")
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        for suffix in ("", "-wal", "-shm"):
            p = self.db_file + suffix
            if os.path.exists(p):
                os.remove(p)
        db.init_db(self.db_file, env_file=self.env_file, cleanup=False)
        db.create_user(EMAIL, self.webapp.generate_password_hash(USER_PASS))
        db.add_account({"name": "我的号", "phone": PHONE_D6, "password": "pw",
                        "owner": EMAIL, "status": "active",
                        "phone_model": "", "phone_code": ""})
        self.app = self.webapp.create_app()
        self.c = self.app.test_client()

    def _login(self):
        r = self.c.post("/api/login", json={"username": EMAIL, "password": USER_PASS})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return self.c.get("/api/me").get_json()["csrf_token"]


class UserIndexDriftGuardTest(_WebBase):
    """DAT-5：用户侧写端点必须像 /api/accounts/* 一样拒绝错位请求。"""

    def test_delete_rejects_wrong_phone(self):
        token = self._login()
        r = self.c.delete("/api/my-accounts/0", json={"phone": "13900000000"},
                          headers={"X-CSRF-Token": token})
        self.assertEqual(r.status_code, 409, r.get_data(as_text=True))
        self.assertIn("已变化", r.get_json()["error"])
        self.assertFalse(self._acct()["deleted"], "错位请求不得改动任何账号")

    def test_pause_rejects_wrong_phone(self):
        token = self._login()
        r = self.c.put("/api/my-accounts/0/pause", json={"paused": True, "phone": "13900000000"},
                       headers={"X-CSRF-Token": token})
        self.assertEqual(r.status_code, 409, r.get_data(as_text=True))
        self.assertFalse(self._acct()["user_paused"])

    def test_update_rejects_wrong_phone(self):
        token = self._login()
        r = self.c.put("/api/my-accounts/0",
                       json={"name": "改名", "phone": "13900000000", "password": "pw"},
                       headers={"X-CSRF-Token": token})
        self.assertEqual(r.status_code, 409, r.get_data(as_text=True))
        self.assertEqual(self._acct()["name"], "我的号")

    def test_matching_phone_still_allowed(self):
        """携带正确手机号（脱敏形态，前端就是这么回传的）不得被误拦。"""
        token = self._login()
        masked = self.webapp._mask_phone(PHONE_D6)
        r = self.c.put("/api/my-accounts/0/pause", json={"paused": True, "phone": masked},
                       headers={"X-CSRF-Token": token})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(self._acct()["user_paused"])

    def test_without_phone_stays_compatible(self):
        """未携带 phone（旧客户端/测试）保持兼容不校验。"""
        token = self._login()
        r = self.c.delete("/api/my-accounts/0", headers={"X-CSRF-Token": token})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

    def _acct(self):
        return next(a for a in db.load_accounts() if a["phone"] == PHONE_D6)


class AccountDedupeTest(unittest.TestCase):
    """SCH-9：同号重复配置只保留一条。"""

    def setUp(self):
        self.env = dict(os.environ)
        os.environ["YIBAN_ACCOUNTS_JSON"] = json.dumps([
            {"phone": PHONE_D6, "password": "p1", "name": "第一条"},
            {"phone": PHONE_D6, "password": "p2", "name": "重复"},
            {"phone": "13800138001", "password": "p3"},
        ])
        os.environ.pop("YIBAN_ACCOUNTS", None)
        os.environ.pop("YIBAN_PHONE", None)
        os.environ["YIBAN_DB_FILE"] = os.path.join(tempfile.mkdtemp(prefix="yiban-dedupe-"), "x.db")

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.env)

    def test_duplicate_phone_kept_once(self):
        accs = signin.load_accounts()
        phones = [a.phone for a in accs]
        self.assertEqual(phones.count(PHONE_D6), 1, "同号必须去重")
        self.assertEqual(len(accs), 2)
        keep = next(a for a in accs if a.phone == PHONE_D6)
        self.assertEqual(keep.name, "第一条", "保留首次出现的那条（顺序即优先级）")

    def test_dedupe_logs_warning(self):
        with self.assertLogs("yiban", "WARNING") as cm:
            signin.load_accounts()
        self.assertTrue(any("重复手机号" in m for m in cm.output), cm.output)


class ConfigSummaryMaskingTest(unittest.TestCase):
    """cli --check-config 摘要不得打印完整手机号（会落在 CI 日志/会话/运维群里）。"""

    def test_summary_masks_phone(self):
        import io as _io
        from contextlib import redirect_stdout
        accs = [signin.Account(phone="13800138000", password="p",
                               phone_model="Vivo-XXXX", phone_code="code"),
                signin.Account(phone="13900139001", password="p")]
        buf = _io.StringIO()
        with redirect_stdout(buf):
            signin.print_config_summary(accs)
        out = buf.getvalue()
        self.assertNotIn("13800138000", out, "完整手机号不得出现在摘要里")
        self.assertNotIn("13900139001", out)
        self.assertIn("138****8000", out, "脱敏形态仍应可区分账号")
        self.assertNotIn("code", out.replace("识别码已配置", ""), "识别码不得打印")
