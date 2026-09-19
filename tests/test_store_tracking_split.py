# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""追踪盐域与 JSON 导入两名的拆分契约：唯一定义点各自落在新模块。

`_write_track_salt_to_env_file` / `_track_salt` / `hash_ip` / `hash_phone` 从
`yiban/store/db.py` 迁入新模块 `yiban/store/tracking.py`；JSON → SQLite 自动导入两名
`_maybe_migrate` / `_rename_backup` 并入既有 `yiban/store/migrations.py`（与建表/版本迁移
同属启动序列）。db 门面把这些名字纳入**模块级读写转发**（`_FORWARDED_STATE`）而不是
快照式再导出。本文件钉住五件事，任何一件破了都会**静默**改变全仓行为：

1. **同一对象**：`db.hash_ip is tracking.hash_ip`、`db._maybe_migrate is migrations._maybe_migrate`
   等六名，且这些名字不再出现在 db 自己的 `__dict__` 里；
2. **写转发落真定义点**：`db.hash_phone = 替身` 必须改到 `tracking.hash_phone`——只换掉门面
   那一份就是打桩静默失效（time_prefs 的冷却查询正是经门面读它）；
3. **可变状态的转发**：`_TRACK_SALT_CACHE` 是盐缓存，`db._TRACK_SALT_CACHE = None`
   （tests/test_rekey_key_source.py 清缓存）必须真的清掉定义点那份，否则换 .env 后仍返回旧盐；
4. **门面内的晚解析**：门面里的 `init_db` 按**属性**取 `migrations._maybe_migrate`——
   `db._maybe_migrate = 替身` 必须被它看见（否则替身被跳过、真导入照旧改 JSON 名）；
5. **delattr 隐藏名语义**：`del db.<名字>` 只把名字从门面摘下（真定义与模块内部调用不动），
   随后的 `setattr` 恢复让它重新可读——`mock.patch.object` / `monkeypatch.delattr` 的撤销
   依赖这条，删不掉的后果是原值永不恢复、打桩残留。

行为级断言：加盐哈希对盐敏感（改盐/换盐结果变化、同盐同输入稳定）；盐缓存经门面清空后
重新按 .env 解析。另外钉住本任务唯一可观察的行为面变化：日志通道由 `yiban.db` 细分为
`yiban.store.tracking` / `yiban.store.migrations`（文案未改），旧通道不再收到本域日志。
"""
import contextlib
import importlib
import io
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
SALT_A = "A" * 32
SALT_B = "B" * 32
PHONE = "13800001234"

# db 门面上的迁出名（与 db.py `_FORWARDED_STATE` 里的登记一致）
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
MOVED_NAMES = MOVED_TO_TRACKING + MOVED_TO_MIGRATIONS
# 盐缓存是可变状态，单独转发（读/写/删三面都落定义点）
FORWARDED_STATE_NAME = "_TRACK_SALT_CACHE"


def _import_shell():
    """`scripts/db.py` 兼容壳（旧 `import db` 路径）。"""
    sys.path.insert(0, os.path.join(BASE, "scripts"))
    return importlib.import_module("db")


from yiban.store import connection as conn_mod  # noqa: E402
from yiban.store import db as impl  # noqa: E402
from yiban.store import migrations as migrations_mod  # noqa: E402
from yiban.store import tracking as track_mod  # noqa: E402


class _TrackingSplitBase(unittest.TestCase):
    """连接状态、密钥/盐路径与盐缓存的公共收发。

    `_connection._env_file` / `_db_file`、`YIBAN_DB_FILE` / `YIBAN_ENV_FILE` /
    `YIBAN_TRACK_SALT` 与进程内盐缓存都是**模块级状态**，必须保存/还原——只置空连接不还原
    路径或盐缓存，就会给后续用例留下本文件的临时源与盐（顺序依赖隐患，与
    `test_store_connection_split.py` 同款）。
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


class SameObjectTest(_TrackingSplitBase):
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
        for name in (*MOVED_NAMES, FORWARDED_STATE_NAME):
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


class WriteForwardingTest(_TrackingSplitBase):
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
        shell = _import_shell()
        original = track_mod.hash_ip
        try:
            shell.hash_ip = self.SENTINEL
            self.assertIs(track_mod.hash_ip, self.SENTINEL,
                          "壳上的赋值经 db 门面后没有落到真定义点")
        finally:
            shell.hash_ip = original
        self.assertIs(track_mod.hash_ip, original)

    def test_shell_patch_object_round_trips(self):
        shell = _import_shell()
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


class DeleteHidingTest(_TrackingSplitBase):
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
