# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""会话缓存域拆分契约：`yiban/store/session_cache.py` 是 session_cache 表族的唯一定义点。

会话缓存域从 `yiban/store/db.py` 迁入新模块 `session_cache.py`，db 门面把全部迁出名纳入
**模块级读写转发**（`_FORWARDED_STATE`）而不是快照式再导出。本文件钉住四件事，任何一件
破了都会**静默**改变全仓行为：

1. **同一对象**：`db.get_session_cache is session_cache.get_session_cache`、
   `db._session_cache_now is session_cache._session_cache_now`，且这些名字不再出现在 db
   自己的 `__dict__` 里。
2. **写转发落真定义点**：`db.get_session_cache = 替身` 必须改到 `session_cache.get_session_cache`
   ——只换掉门面那一份就是打桩静默失效。
3. **打桩可见于模块内部调用点**：`tests/test_session_cache_db.py` 打桩
   `db._session_cache_now`（写入与判定同钟），而调用点在 session_cache 模块内部
   （`set_session_cache` / `get_session_cache`）——快照式再导出会让内部照旧调真名，
   测试仍绿但打桩点不再是它以为的那一个。`_session_cache_ttl_hours` 同理（读侧判据）。
4. **delattr 隐藏名语义**：`del db.<名字>` 只把名字从门面摘下（真定义与模块内部调用不动），
   随后的 `setattr` 恢复让它重新可读——`mock.patch.object` / `monkeypatch.delattr` 的撤销
   依赖这条，删不掉的后果是原值永不恢复、打桩残留。

另外钉住本次迁出唯一可观察的行为面变化：日志通道由 `yiban.db` 细分为
`yiban.store.session_cache`（文案未改），旧通道不再收到本域日志。
"""
import contextlib
import datetime
import importlib
import json
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

# db 门面上的会话缓存域迁出名（与 db.py `_FORWARDED_STATE` 里的登记一致）
MOVED_NAMES = (
    "_session_cache_now",
    "_session_cache_key",
    "_session_cache_ttl_hours",
    "get_session_cache",
    "set_session_cache",
    "clear_session_cache",
)

# 同域常量：不可变配置，唯一定义点在本模块，门面按常量再导出（`db.<常量>` 读取不变）
MOVED_CONSTANTS = (
    "SESSION_CACHE_TTL_HOURS_DEFAULT",
    "SESSION_CACHE_TTL_HOURS_MIN",
    "SESSION_CACHE_TTL_HOURS_MAX",
    "SESSION_CACHE_HKDF_INFO",
)


def _import_shell():
    """`scripts/db.py` 兼容壳（旧 `import db` 路径）。"""
    sys.path.insert(0, os.path.join(BASE, "scripts"))
    return importlib.import_module("db")


from yiban.store import connection as conn_mod  # noqa: E402
from yiban.store import db as impl  # noqa: E402
from yiban.store import session_cache as cache_mod  # noqa: E402


class _SessionCacheSplitBase(unittest.TestCase):
    """连接状态与密钥路径的公共收发。

    连接收尾走门面（顺带覆盖写入转发）；`_connection._env_file` / `_db_file` 与
    `YIBAN_DB_FILE` 是**模块级状态**，必须保存/还原——只置空连接不还原路径，就会给后续
    用例留下本文件的临时库路径（顺序依赖隐患，与 `test_store_connection_split.py` 同款）。
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


class SameObjectTest(_SessionCacheSplitBase):
    """① 门面读取回落到 session_cache，且 db 不再持有自己的绑定。"""

    def test_functions_are_the_same_objects(self):
        for name in MOVED_NAMES:
            self.assertIs(getattr(impl, name), getattr(cache_mod, name),
                          f"db.{name} 与 session_cache.{name} 不是同一对象")

    def test_definitions_live_only_in_session_cache(self):
        """唯一的定义点：session_cache 才是真正持有这些名字的模块。"""
        for name in MOVED_NAMES:
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


class WriteForwardingTest(_SessionCacheSplitBase):
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
        shell = _import_shell()
        original = cache_mod.set_session_cache
        try:
            shell.set_session_cache = self.SENTINEL
            self.assertIs(cache_mod.set_session_cache, self.SENTINEL,
                          "壳上的赋值经 db 门面后没有落到真定义点")
        finally:
            shell.set_session_cache = original
        self.assertIs(cache_mod.set_session_cache, original)

    def test_shell_patch_object_round_trips(self):
        shell = _import_shell()
        real_now, real_get = cache_mod._session_cache_now, cache_mod.get_session_cache
        with mock.patch.object(shell, "_session_cache_now", self.SENTINEL):
            self.assertIs(cache_mod._session_cache_now, self.SENTINEL)
        self.assertIs(cache_mod._session_cache_now, real_now)
        self.assertIs(cache_mod.get_session_cache, real_get)


class DeleteHidingTest(_SessionCacheSplitBase):
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


class FacadeBehaviourTest(_SessionCacheSplitBase):
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
