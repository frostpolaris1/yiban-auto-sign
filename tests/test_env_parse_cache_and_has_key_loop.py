# -*- coding: utf-8 -*-
"""工单 dnox ④：`.env` 解析进程内缓存 + `has_key` 循环内调用（e2e）。

**被测缺陷（两条，独立）**
1. `has_key` 在 `yiban/adapters/persistence/accounts.py::_decrypt_row` 的字段循环里被逐字段调用。
   每个账号行有 password / phone_code 两枚密文字段 ⇒ 每行判定两次。
   进程环境没有 `YIBAN_ACCOUNTS_KEY`（密钥写在 `.env`，README 教的就是这条）时，
   每次判定都把整份 `.env` 读盘解析一遍 ⇒ 一次全量装载读盘 **2N 次**。
2. `.env` 解析（`yiban.adapters.config.env_io.parse_env_file`）没有进程内缓存：
   同样的文件被反复读盘解析。

**钉住的不变量（不钉调用顺序）**
- A：一次全量装载（`db.load_accounts()`）读 `.env` 文件的次数与账号数**无关**，且是一个小常数。
- B：一次全量装载里"密钥存在性判定"的次数**不超过账号行数**（不随加密字段数翻倍）。
- C：`.env` 内容变化后，下一次解析**必须看到新值**——进程内写（`write_env_keys`）、
  等长原地改写都算。这是加缓存后的**陈旧读**判据（行为回归面）。
- D：`has_key` 仍如实回答"这个来源**现在**有没有钥"——从 `.env` 删钥后同进程内立刻变 False；
  密文账号遇无钥必须抛 RuntimeError（防"删钥后仍按缓存钥登录"）。

**口径**：通用工程语义。缓存不得让编辑后的 `.env` 读不到新值；
"有没有钥"的判定不得被密钥缓存顶替。

**运行环境**：WSL（`scripts/dev-verify.sh`）。用例自带临时目录与 `.env`，无网络、无 skip。
"""
import builtins
import contextlib
import os
import shutil
import tempfile
import unittest
from unittest import mock

import db

from yiban.adapters.config import account_crypto, env_io

TEST_KEY = "c" * 64
AUDIT_KEY = "d" * 64


class EnvParseCacheAndHasKeyLoopE2ETest(unittest.TestCase):
    """真库、真 `.env`、真 `account_crypto`：密钥只写 `.env`（不设进程环境变量）。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-envcache-e2e-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls._saved_env = {k: os.environ.get(k) for k in (
            "YIBAN_ACCOUNTS_KEY", "YIBAN_AUDIT_KEY", "YIBAN_ENV_FILE",
            "YIBAN_DB_FILE", "YIBAN_STATE_DIR")}
        # 关键：**不设** YIBAN_ACCOUNTS_KEY 进程环境变量——密钥只存在于 .env，
        # 这正是 README 教的部署形态，也是 has_key 走读盘分支的前提。
        for k in ("YIBAN_ACCOUNTS_KEY", "YIBAN_AUDIT_KEY"):
            os.environ.pop(k, None)
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_DB_FILE"] = cls.db_file
        os.environ["YIBAN_STATE_DIR"] = cls.tmp
        cls._write_env_full()

    @classmethod
    def tearDownClass(cls):
        cls._close_db()
        for key, val in cls._saved_env.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val
        _reset_caches()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def _write_env_full(cls):
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\nYIBAN_AUDIT_KEY={AUDIT_KEY}\n")

    @classmethod
    def _close_db(cls):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None

    def setUp(self):
        self._close_db()
        for suffix in ("", "-wal", "-shm"):
            p = self.db_file + suffix
            if os.path.exists(p):
                os.remove(p)
        _reset_caches()
        self._write_env_full()

    def tearDown(self):
        self._close_db()

    # -- 夹具 -----------------------------------------------------------------
    def _build_db(self, n):
        """建库并写入 n 个账号；password 与 phone_code 都由 add_account 各自加密。"""
        self._close_db()
        for suffix in ("", "-wal", "-shm"):
            p = self.db_file + suffix
            if os.path.exists(p):
                os.remove(p)
        db.init_db(self.db_file, env_file=self.env_file, cleanup=False)
        for i in range(n):
            db.add_account({
                "name": f"u{i}",
                "phone": f"1390000{i:04d}",
                "password": f"pw-{i}",
                "phone_code": f"dev-{i}",
                "status": "active",
            })

    def _count_env_opens(self, fn):
        """执行 fn，统计对被测 `.env` 路径发起 open() 的次数（= 读盘解析次数）。"""
        target = os.path.abspath(self.env_file)
        opens = []
        real_open = builtins.open

        def counting_open(file, *args, **kwargs):
            try:
                if isinstance(file, (str, bytes, os.PathLike)) \
                        and os.path.abspath(os.fspath(file)) == target:
                    opens.append(1)
            except (TypeError, ValueError):
                pass
            return real_open(file, *args, **kwargs)

        with mock.patch.object(builtins, "open", counting_open):
            fn()
        return len(opens)

    # -- A：读盘次数与账号数无关（缓存不变量）---------------------------------
    def test_full_load_reads_env_file_constant_times(self):
        small, large = 3, 12
        self._build_db(small)
        n_small = self._count_env_opens(db.load_accounts)
        self._build_db(large)
        n_large = self._count_env_opens(db.load_accounts)
        self.assertEqual(
            n_small, n_large,
            f"装载 {small} 个与 {large} 个账号读 .env 次数必须相同："
            f"{n_small} vs {n_large}（读盘次数随账号数增长 = 逐账号重复解析）")
        self.assertLessEqual(
            n_large, 3,
            f"装载 {large} 个账号读 .env 次数应为小常数，实测 {n_large}")

    # -- B：每行密钥判定次数不随加密字段数翻倍 --------------------------------
    def test_key_presence_check_not_per_field(self):
        n = 5
        self._build_db(n)
        calls = []
        real_has_key = account_crypto.has_key

        def spy(env_file=None):
            calls.append(1)
            return real_has_key(env_file)

        with mock.patch.object(account_crypto, "has_key", side_effect=spy):
            accts = db.load_accounts()
        self.assertEqual(len(accts), n)
        self.assertLessEqual(
            len(calls), n,
            f"每个账号行只该判定一次密钥存在性，实测 {len(calls)} 次 / {n} 行"
            f"（每行有 password 与 phone_code 两枚密文字段，逐字段判定会翻倍）")

    # -- C：缓存不得引入陈旧读 ------------------------------------------------
    def test_external_in_place_edit_is_seen_by_next_parse(self):
        env_io.parse_env_file(self.env_file)  # 先填充缓存
        with open(self.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\nYIBAN_EXTRA=1\n")
        parsed = env_io.parse_env_file(self.env_file)
        self.assertEqual(parsed.get("YIBAN_EXTRA"), "1", "外部改写后解析必须看到新键")

    def test_same_size_in_place_edit_is_seen_by_next_parse(self):
        self._write_env_full()
        first = env_io.parse_env_file(self.env_file)
        self.assertEqual(first.get("YIBAN_ACCOUNTS_KEY"), TEST_KEY)
        # 等长换值（大小不变）：只靠 size 判会漏，必须靠 mtime/ino
        other = "e" * 64
        with open(self.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={other}\nYIBAN_AUDIT_KEY={AUDIT_KEY}\n")
        self.assertEqual(env_io.parse_env_file(self.env_file).get("YIBAN_ACCOUNTS_KEY"),
                         other, "等长原地改写后解析必须看到新值（不得命中陈旧缓存）")

    def test_write_env_keys_edit_is_seen_by_next_parse(self):
        env_io.parse_env_file(self.env_file)  # 先填充缓存
        env_io.write_env_keys(self.env_file, {"YIBAN_MARK": "1"})
        self.assertEqual(env_io.parse_env_file(self.env_file).get("YIBAN_MARK"), "1",
                         "进程内写 .env 后解析必须看到新值")

    # -- D：has_key 仍如实回答当前磁盘状态（防陈旧钥顶替）--------------------
    def test_has_key_tracks_current_disk_state(self):
        self.assertTrue(account_crypto.has_key(self.env_file))
        # 运维从 .env 删钥（进程环境本就没有该变量）→ 同进程内必须立刻 False
        env_io.write_env_keys(self.env_file, {"YIBAN_ACCOUNTS_KEY": ""}, delete_empty=True)
        self.assertFalse(
            account_crypto.has_key(self.env_file),
            "删钥后 has_key 必须立刻回答 False——不得被进程内密钥缓存顶替")

    def test_encrypted_accounts_without_key_still_raise(self):
        self._build_db(2)
        env_io.write_env_keys(self.env_file, {"YIBAN_ACCOUNTS_KEY": ""}, delete_empty=True)
        with self.assertRaises(RuntimeError):
            db.load_accounts()


def _reset_caches():
    """清进程内缓存，防用例之间串味（与既有 `_KEY_CACHE = None` 同一习惯）。

    `.env` 解析缓存的复位钩子按存在与否取用：本批尚未落地时该符号不存在，
    此时留着不退，测试照常按"无缓存"实现跑（正是先红所需的底本）。
    """
    account_crypto._KEY_CACHE = None
    reset = getattr(env_io, "clear_parse_cache", None)
    if reset is not None:
        reset()


if __name__ == "__main__":
    unittest.main()
