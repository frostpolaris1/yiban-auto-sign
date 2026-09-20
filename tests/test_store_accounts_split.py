# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""账号域拆分契约：`yiban/store/accounts.py` 是 accounts 表 CRUD 与行加解密的唯一定义点。

账号域从 `yiban/store/db.py` 迁入同包已存在的 `accounts.py`，db 门面把全部迁出名纳入
**模块级读写转发**（`_FORWARDED_STATE`）而不是快照式再导出。本文件钉住四件事，任何一件
破了都会**静默**改变全仓行为：

1. **同一对象**：`db.add_account is accounts.add_account`、`db._decrypt_row is
   accounts._decrypt_row`，且这些名字不再出现在 db 自己的 `__dict__` 里。
2. **写转发落真定义点**：`db.add_account = 替身` 必须改到 `accounts.add_account`——
   只换掉门面那一份就是打桩静默失效。
3. **打桩可见于模块内部调用点**：`tests/test_a2_decrypt_out_of_lock.py` 打桩
   `db._decrypt_row` / `db.decrypt_account_rows`，而调用点在 accounts 模块内部
   （`load_accounts` → `read_accounts` → `decrypt_account_rows` → `_decrypt_row`）——
   快照式再导出会让内部照旧调真名，测试仍绿但打桩点不再是它以为的那一个。
4. **delattr 隐藏名语义**：`del db.<名字>` 只把名字从门面摘下（真定义与模块内部调用不动），
   随后的 `setattr` 恢复让它重新可读——`mock.patch.object` / `monkeypatch.delattr` 的撤销
   依赖这条，删不掉的后果是原值永不恢复、打桩残留。
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

# db 门面上的账号域迁出名（与 db.py `_FORWARDED_STATE` 里的登记一致）
MOVED_NAMES = (
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


def _import_shell():
    """`scripts/db.py` 兼容壳（旧 `import db` 路径）。"""
    sys.path.insert(0, os.path.join(BASE, "scripts"))
    return importlib.import_module("db")


from yiban.store import accounts as accounts_mod  # noqa: E402
from yiban.store import db as impl  # noqa: E402


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


class SameObjectTest(_AccountsSplitBase):
    """① 门面读取回落到 accounts，且 db 不再持有自己的绑定。"""

    def test_functions_are_the_same_objects(self):
        for name in MOVED_NAMES:
            self.assertIs(getattr(impl, name), getattr(accounts_mod, name),
                          f"db.{name} 与 accounts.{name} 不是同一对象")
        self.assertIs(impl.DuplicatePhoneError, accounts_mod.DuplicatePhoneError)

    def test_definitions_live_only_in_accounts(self):
        """唯一的定义点：accounts 才是真正持有这些名字的模块。"""
        for name in MOVED_NAMES:
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


class WriteForwardingTest(_AccountsSplitBase):
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
        shell = _import_shell()
        original = accounts_mod.add_account
        try:
            shell.add_account = self.SENTINEL
            self.assertIs(accounts_mod.add_account, self.SENTINEL,
                          "壳上的赋值经 db 门面后没有落到真定义点")
        finally:
            shell.add_account = original
        self.assertIs(accounts_mod.add_account, original)

    def test_shell_patch_object_round_trips(self):
        shell = _import_shell()
        real_decrypt, real_read = accounts_mod._decrypt_row, accounts_mod.decrypt_account_rows
        with mock.patch.object(shell, "_decrypt_row", self.SENTINEL):
            self.assertIs(accounts_mod._decrypt_row, self.SENTINEL)
        self.assertIs(accounts_mod._decrypt_row, real_decrypt)
        self.assertIs(accounts_mod.decrypt_account_rows, real_read)


class DeleteHidingTest(_AccountsSplitBase):
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


class FacadeBehaviourTest(_AccountsSplitBase):
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
