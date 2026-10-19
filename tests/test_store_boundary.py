# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""store 层拆分后的**行为**回归：经 db 门面走一遍各域的真实读写。

`yiban/adapters/persistence/db.py` 是门面：各域定义点在同包 `accounts` / `clock_meta` / `connection` /
`session_cache` / `time_prefs` / `tracking` 等模块。本文件钉住的是**经门面能真用**：

- 各域 CRUD 往返（accounts 账号、clock_meta 单键、session_cache 作废、time_prefs 统计与
  冷却、tracking 加盐哈希与盐缓存）；
- `init_db` 各分支语义、`YIBAN_DB_FILE` 只写进 `.env` 时的库路径解析；
- 解密在锁外完成（A2）；
- web 侧行为：用户侧写端点的错位请求守卫、同号去重、`--check-config` 摘要脱敏；
- 包内不得裸 `import db/mailer/notify/signin`（依赖 `scripts/` 在 `sys.path` 的旧写法）。

标签：C · 存储：迁移与库完整性
对应实现：`yiban/adapters/persistence/*` 各域模块与 `yiban/adapters/persistence/db.py` 门面。
依赖：临时库 + 临时 `.env`，无网络、无 skip。

2026-10-16 饮食批：原五项**转发机制**契约（`SameObjectTest_*` 的 `is` 同一性、
`WriteForwardingTest_*` 的赋值落点、`DeleteHidingTest_*` 的 delattr 语义、
`ReExportNamesTest` / `PatchAndDeleteRoundTripTest` / `ConnectionIdentityTest`、以及
`ForwardedStateLedgerTest` / `FacadeDocstringLedgerTest` / `StoreForwardingMechanismLedgerTest`
三张名册）整族删除。理由：它们钉的是"名字面在哪、赋值是否转发、补丁是否穿透"——
**实现长相**，不是行为；门面若改成直接导入，行为一字不变而它们集体转红（判据见
AGENTS §一.6a：测不变式，不测编舞）。门面的可用性由本文件保留的行为用例承担。
"""
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
import signin

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(0, os.path.join(BASE, "scripts"))

from werkzeug.security import generate_password_hash  # noqa: E402

from yiban.adapters.persistence import connection as conn_mod  # noqa: E402
from yiban.adapters.persistence import db as impl  # noqa: E402
from yiban.adapters.persistence import tracking as track_mod  # noqa: E402
from yiban.core import clock  # noqa: E402

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
        # （见 yiban/adapters/persistence/connection.py 的 set_env_file / set_db_file）。子类
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


def _iter_py(root):
    for dirpath, dirnames, filenames in os.walk(os.path.join(BASE, root)):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for name in sorted(filenames):
            if name.endswith(".py"):
                yield os.path.join(dirpath, name)


def _read(path):
    with io.open(path, encoding="utf-8") as f:
        return f.read()


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
            "包内代码必须以 `from yiban.adapters.persistence import db`（或 `from yiban import notify`）"
            "的形式导入，不得裸名依赖 scripts/：" + "; ".join(bad),
        )


DB_PATH = os.path.join(BASE, "yiban", "adapters", "persistence", "db.py")


def _source():
    with open(DB_PATH, encoding="utf-8") as f:
        return f.read()


PHONE = "13800001234"


MOVED_NAMES_SESSION = (
    "_session_cache_now",
    "_session_cache_key",
    "_session_cache_ttl_hours",
    "get_session_cache",
    "set_session_cache",
    "clear_session_cache",
)


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
        db.create_user(EMAIL, generate_password_hash(USER_PASS))
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
        from contextlib import redirect_stderr
        accs = [signin.Account(phone="13800138000", password="p",
                               phone_model="Vivo-XXXX", phone_code="code"),
                signin.Account(phone="13900139001", password="p")]
        buf = _io.StringIO()
        with redirect_stderr(buf):     # 摘要走 stderr（stdout 只放结果，见 docs/dev/cli.md §2）
            signin.print_config_summary(accs)
        out = buf.getvalue()
        self.assertNotIn("13800138000", out, "完整手机号不得出现在摘要里")
        self.assertNotIn("13900139001", out)
        self.assertIn("138****8000", out, "脱敏形态仍应可区分账号")
        self.assertNotIn("code", out.replace("识别码已配置", ""), "识别码不得打印")


# ---------------------------------------------------------------------------
# 笔记：原「6c3-B 机制总账」段已随饮食批删除
# ---------------------------------------------------------------------------
# 该段（`StoreForwardingMechanismLedgerTest` 的 8 条参数化 + `_MECH_DOMAINS` 登记表，
# 以及更早六域各自的 SameObjectTest_* / WriteForwardingTest_* / DeleteHidingTest_*）
# 钉的是 db 门面 `_FORWARDED_STATE` 的**转发机制**：同一身份、可导入、写转发落真定义点、
# patch/delattr/undo 往返。它属"测实现长相"——门面若改成直接导入，行为一字不变而它集体
# 转红。2026-10-16 整族删除，判据见 AGENTS §一.6a。门面的**可用性**由本文件保留的
# `FacadeBehaviourTest_*`（各域 CRUD 往返）与 `UserIndexDriftGuardTest` 等行为用例承担。


