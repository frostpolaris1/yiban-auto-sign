# -*- coding: utf-8 -*-
"""连接层拆分契约：`yiban/store/connection.py` 是连接状态与原语的唯一定义点。

2026-09-19 db.py（4400+ 行）按域拆分的第一刀把 `_conn`/`_conn_lock`/`_db_file`/
`_env_file`/`DB_DEFAULT`/`get_conn`/`is_initialized` 移入 `yiban/store/connection.py`，
`yiban/store/db.py` 再导出。本文件钉住三件事，任何一件破了都会**静默**改变全仓行为：

1. **同一对象**：`db._conn_lock is connection._conn_lock`（`_conn_lock` 定义后永不重绑，
   db.py 内部 73 处 `with _conn_lock` 与 19 处子模块 `with db._conn_lock` 共用同一把 RLock）。
2. **真实可用**：临时库 `init_db` 后 `is_initialized()` 为真且 `get_conn()` 能 `SELECT 1`。
3. **旧名字不缺失**：既有调用方（含 190+ 处 `db._conn = None` 的测试收尾、以及
   `test_rekey_key_source` 的 `db._env_file = path` 打桩）依赖的**下划线名字**在
   `yiban.store.db` 上仍然可读可写，且写入落到 connection 的真状态上。

第 3 条是本文件存在的主要理由。若 db.py 只做一次"快照式"再导出
（`_conn = connection._conn`），`db._conn = None` 会写在一份永远不会被 connection 读到的
副本上：上一个用例的临时库关不掉、下一个用例复用它的连接——**测试仍会"通过"很多条**，
直到某个用例断言到另一条库里的数据才以随机形式失败。故这里断言的是"读到的就是
connection 的真状态"，不是"db 有自己的 _conn 属性"。
"""
import os
import shutil
import sys
import tempfile
import threading
import unittest
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

from yiban.store import connection as conn_mod  # noqa: E402
from yiban.store import db as impl  # noqa: E402


def _import_shell():
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
            try:
                conn.close()
            except Exception:
                pass
        impl._conn = None

    def _temp_db(self):
        tmp = tempfile.mkdtemp(prefix="yiban-conn-split-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        return os.path.join(tmp, "yiban.db")


class ConnectionIdentityTest(_ConnStateBase):
    """① 再导出的名字与 connection 是同一对象（含唯一定义点这一事实）。"""

    def test_lock_is_the_same_object(self):
        self.assertIs(impl._conn_lock, conn_mod._conn_lock)
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
            impl.definitely_not_a_name           # __getattr__ 不得吞掉真缺失

        c.close()

    def test_shell_write_forwarding_reaches_connection(self):
        """`scripts/db.py` 壳（旧 `import db`）的读写同样落到 connection。

        这条是 190+ 处 `db._conn = None` 的真正守卫：壳把 setattr 转发到
        `yiban.store.db`，后者必须再转发到 connection，否则照样静默失效。
        """
        shell = _import_shell()
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


class InitDbSemanticsTest(_ConnStateBase):
    """② `init_db` 各分支语义与拆分前等价（连接、幂等、路径刷新、异常置空）。"""

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

        with mock.patch.object(impl, "_run_migrations", side_effect=boom):
            with self.assertRaises(RuntimeError):
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
