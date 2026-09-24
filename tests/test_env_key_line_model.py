# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""`.env` 写键的行模型：折叠旧键行 + 拒绝潜伏分隔符。

`.env` 的解析（`env_io.parse_env_file`）与写回必须共用同一套行模型：`str.splitlines()`
除 `\n` / `\r` 外还把 U+2028 等 8 个字符当行边界，值里潜伏这些字符时旧写回会把后半截
拼成**真配置行**（实测可注入 `YIBAN_ADMIN_PASSWORD_HASH=`）。本文件把三处写键方
（账号密钥 / 审计密钥 / 追踪盐）与密钥缓存来源的断言并到一处：

- 潜伏分隔符 → ValueError 且磁盘一个字节都不改、不留 tmp；
- 折叠认得 `KEY = v`（= 前带空白）写法，不积累出同键第二行；
- 密钥缓存按 `env_file` 分开取值，环境变量来源不冒充文件来源，非法 key 抛 ValueError
  （调用方只 except ValueError，TypeError 会漏出去）。

功能：字段密钥与写键行模型的注入面回归。
归属：`yiban/infra`（env_io / account_crypto）与 `yiban/store` 写键方的测试。
复用：`account_crypto.load_key/has_key/encrypt_field/decrypt_field`、`env_io.write_env_key`。
通信：直接调用上述函数并读写临时 `.env` 文件；由 pytest 收集 `unittest.TestCase`。
"""
import io
import os
import shutil
import tempfile
import unittest
from unittest import mock

from yiban.infra import account_crypto, env_io
from yiban.store import audit_chain, tracking

KEY_A = "1" * 64


KEY_B = "2" * 64


class KeyCacheSourceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-key-cache-")
        self._old = {k: os.environ.get(k) for k in ("YIBAN_ACCOUNTS_KEY", "YIBAN_ENV_FILE")}
        for k in self._old:
            os.environ.pop(k, None)
        account_crypto._KEY_CACHE = None

    def tearDown(self):
        account_crypto._KEY_CACHE = None
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _env_file(self, name, key_hex):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={key_hex}\n")
        return path

    def test_second_env_file_gets_its_own_key(self):
        """load a → load b：b 必须拿到 b 文件里的钥（缓存按来源分开）。"""
        a = self._env_file("a.env", KEY_A)
        b = self._env_file("b.env", KEY_B)

        self.assertEqual(account_crypto.load_key(a).hex(), KEY_A)
        self.assertEqual(account_crypto.load_key(b).hex(), KEY_B, "第二个 .env 串到了第一个的钥")
        self.assertEqual(account_crypto.load_key(a).hex(), KEY_A, "回到 a 仍须取 a 的钥")

    def test_env_var_key_does_not_stand_in_for_file_key(self):
        """环境变量撤掉后不得再拿它的钥冒充 .env 的钥（缓存不记环境变量这一档）。"""
        a = self._env_file("a.env", KEY_A)
        os.environ["YIBAN_ACCOUNTS_KEY"] = KEY_B

        self.assertEqual(account_crypto.load_key(a).hex(), KEY_B, "环境变量优先级最高")
        os.environ.pop("YIBAN_ACCOUNTS_KEY")
        self.assertEqual(account_crypto.load_key(a).hex(), KEY_A, "撤掉环境变量后应回退到 .env")

    def test_decrypt_rejects_bad_key_with_value_error(self):
        """key=None / 非 bytes / 长度错 → ValueError（TypeError 会越过调用方的收口）。"""
        key = bytes.fromhex(KEY_A)
        blob = account_crypto.encrypt_password("pw", key, "13800138000")
        text = account_crypto.encrypt_text("secret", key)

        for bad in (None, "1" * 64, b"short", bytearray(31)):
            with self.assertRaises(ValueError):
                account_crypto.decrypt_password(blob, bad, "13800138000")
            with self.assertRaises(ValueError):
                account_crypto.decrypt_text(text, bad)


NEW_KEY = bytes(range(32))


class CryptoLineModelTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-crypto-line-")
        self.env_file = os.path.join(self.tmp, ".env")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_env(self, text):
        with open(self.env_file, "w", encoding="utf-8") as f:
            f.write(text)

    def _read_env(self):
        with open(self.env_file, encoding="utf-8") as f:
            return f.read()

    def test_space_shadow_key_line_is_folded(self):
        """带空格影子行被折掉，不追加出同键第二行；同前缀的其他键、其他行都保留。"""
        self._write_env("YIBAN_ACCOUNTS_FILE=accounts.json\nYIBAN_ACCOUNTS_KEY = \n")

        self.assertEqual(
            account_crypto._write_key_to_env_file(self.env_file, NEW_KEY), NEW_KEY)

        self.assertEqual(
            env_io.count_key_lines(self.env_file, "YIBAN_ACCOUNTS_KEY"), 1,
            "影子行残留 = 同键两行，解析取哪行由落盘顺序决定")
        parsed = env_io.parse_env_file(self.env_file)
        self.assertEqual(parsed["YIBAN_ACCOUNTS_KEY"], NEW_KEY.hex())
        self.assertEqual(parsed["YIBAN_ACCOUNTS_FILE"], "accounts.json",
                         "同前缀的其他键不得被折叠误伤")

    def test_latent_separator_in_existing_line_refuses_write(self):
        """既有行值藏 U+2028：写回会坐实其后的半截 → 抛错且 .env 未被改写。"""
        self._write_env("OTHER=ok\nSECRET=a\u2028YIBAN_ADMIN_PASSWORD_HASH=evil\n")
        before = self._read_env()

        with self.assertRaises(ValueError) as ctx:
            account_crypto._write_key_to_env_file(self.env_file, NEW_KEY)

        msg = str(ctx.exception)
        self.assertIn("行分隔符", msg)
        # 定位线索是行号（1-based）而非行原文：行原文会带出值里的口令（实测如
        # postgres://user:S3cretPw@…）与裸 U+2028，跟着异常消息落日志与 HTTP 500
        self.assertIn("第 2 行", msg, "消息须给出问题行的行号线索")
        self.assertNotIn("SECRET", msg, "消息不得回带行原文（键名也不行）")
        self.assertNotIn("\u2028", msg, "消息里不得出现分隔符字符本身")
        self.assertEqual(self._read_env(), before, "拒绝写入时不得改动 .env")
        leftovers = [p for p in os.listdir(self.tmp) if ".tmp" in p]
        self.assertEqual(leftovers, [], "拒绝写入时不得先落 tmp 文件")

    def test_latent_separator_in_dropped_old_key_line_still_refuses(self):
        """含 U+2028 的旧键行会被折叠丢弃——丢弃前同样要过校验，不得静默删掉。"""
        self._write_env("YIBAN_ACCOUNTS_KEY=aa\u2028bb\nYIBAN_ACCOUNTS_KEY=\n")
        before = self._read_env()
        self.assertEqual(
            env_io.parse_env_file(self.env_file)["YIBAN_ACCOUNTS_KEY"], "",
            "前置条件：末行空值键让本函数走进写入分支（而非早退返回既有密钥）")

        with self.assertRaises(ValueError) as ctx:
            account_crypto._write_key_to_env_file(self.env_file, NEW_KEY)

        self.assertIn("行分隔符", str(ctx.exception))
        self.assertEqual(self._read_env(), before, "拒绝写入时不得改动 .env")


AUDIT_KEY = bytes(range(32))


LATENT = "notify\u2028YIBAN_ADMIN_PASSWORD_HASH=injected"


class _EnvFileBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-store-env-key-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.env_file = os.path.join(self.tmp, ".env")

    def _write(self, text):
        with io.open(self.env_file, "w", encoding="utf-8") as f:
            f.write(text)

    def _read(self):
        with io.open(self.env_file, encoding="utf-8") as f:
            return f.read()

    def _physical_lines(self):
        """磁盘物理行数（按 \\n 计）：潜伏分隔符不增行，实体化才 +1。"""
        with open(self.env_file, "rb") as f:
            return f.read().count(b"\n")

    def _leftover_tmp(self):
        return [n for n in os.listdir(self.tmp) if ".tmp" in n]


class TrackingSaltLineModelTest(_EnvFileBase):
    def test_latent_separator_in_other_line_refuses_write(self):
        """既有无关键行里藏 U+2028：写盐会坐实其后的注入行 → 拒绝且零改动。"""
        self._write(f"YIBAN_OTHER=ok\nYIBAN_ANNOUNCEMENT={LATENT}\n")
        before = self._read()
        before_lines = self._physical_lines()

        with self.assertRaises(ValueError) as ctx:
            tracking._write_track_salt_to_env_file(self.env_file, "salt123")

        self.assertIn("行分隔符", str(ctx.exception))
        self.assertEqual(self._read(), before, "拒绝写入时不得改动 .env（一个字节都不改）")
        self.assertEqual(self._physical_lines(), before_lines,
                         "物理行数 +1 = 潜伏分隔符被某次读-改-写实体化")
        self.assertNotIn("YIBAN_ADMIN_PASSWORD_HASH",
                         env_io.parse_env_file(self.env_file),
                         "潜伏载荷一旦实体化，解析器就能看到注入键")
        self.assertEqual(self._leftover_tmp(), [], "拒绝写入时不得先落 tmp 文件")

    def test_space_shadow_line_is_folded(self):
        """`YIBAN_TRACK_SALT = `（= 前带空格、值为空）是同一条键的行，必须折掉。"""
        self._write("YIBAN_TRACK_SALT = \nYIBAN_OTHER=1\n")
        self.assertEqual(env_io.parse_env_file(self.env_file)["YIBAN_TRACK_SALT"], "",
                         "前置条件：空值键让写入方走进写入分支")

        self.assertEqual(
            tracking._write_track_salt_to_env_file(self.env_file, "salt123"), "salt123")

        self.assertEqual(
            env_io.count_key_lines(self.env_file, "YIBAN_TRACK_SALT"), 1,
            "影子行残留 = 同键两行，解析取哪行由落盘顺序决定")
        self.assertEqual(env_io.parse_env_file(self.env_file)["YIBAN_TRACK_SALT"], "salt123")
        self.assertEqual(env_io.parse_env_file(self.env_file)["YIBAN_OTHER"], "1",
                         "折叠不得丢掉无关配置")

    def test_plain_write_preserves_other_lines(self):
        self._write("YIBAN_OTHER=1\n")
        tracking._write_track_salt_to_env_file(self.env_file, "salt123")
        self.assertEqual(self._physical_lines(), 2)
        self.assertEqual(env_io.parse_env_file(self.env_file)["YIBAN_OTHER"], "1")


class AuditKeyLineModelTest(_EnvFileBase):
    def test_latent_separator_in_other_line_refuses_write(self):
        self._write(f"YIBAN_OTHER=ok\nYIBAN_ANNOUNCEMENT={LATENT}\n")
        before = self._read()
        before_lines = self._physical_lines()

        with self.assertRaises(ValueError) as ctx:
            audit_chain._write_audit_key_to_env_file(self.env_file, AUDIT_KEY)

        self.assertIn("行分隔符", str(ctx.exception))
        self.assertEqual(self._read(), before, "拒绝写入时不得改动 .env（一个字节都不改）")
        self.assertEqual(self._physical_lines(), before_lines)
        self.assertNotIn("YIBAN_ADMIN_PASSWORD_HASH",
                         env_io.parse_env_file(self.env_file))
        self.assertEqual(self._leftover_tmp(), [])

    def test_space_shadow_line_is_folded(self):
        self._write("YIBAN_AUDIT_KEY = \nYIBAN_OTHER=1\n")
        self.assertEqual(env_io.parse_env_file(self.env_file)["YIBAN_AUDIT_KEY"], "")

        self.assertEqual(
            audit_chain._write_audit_key_to_env_file(self.env_file, AUDIT_KEY), AUDIT_KEY)

        self.assertEqual(env_io.count_key_lines(self.env_file, "YIBAN_AUDIT_KEY"), 1)
        self.assertEqual(env_io.parse_env_file(self.env_file)["YIBAN_AUDIT_KEY"],
                         AUDIT_KEY.hex())
        self.assertEqual(env_io.parse_env_file(self.env_file)["YIBAN_OTHER"], "1")

    def test_plain_write_preserves_other_lines(self):
        self._write("YIBAN_OTHER=1\n")
        audit_chain._write_audit_key_to_env_file(self.env_file, AUDIT_KEY)
        self.assertEqual(self._physical_lines(), 2)
        self.assertEqual(env_io.parse_env_file(self.env_file)["YIBAN_OTHER"], "1")


class WriteEnvKeysTmpCleanupTest(_EnvFileBase):
    """写失败不得把装着新密钥/新盐的 tmp 永久留在盘上。"""

    def test_failed_replace_leaves_no_tmp(self):
        self._write("YIBAN_OTHER=1\n")
        with mock.patch.object(env_io.os, "replace", side_effect=OSError("disk full")), \
                self.assertRaises(OSError):
            env_io.write_env_keys(self.env_file, {"YIBAN_SECRET_KEY": "d" * 64})
        self.assertEqual(self._leftover_tmp(), [], "含新钥的 tmp 不得留盘")
        self.assertEqual(env_io.parse_env_file(self.env_file), {"YIBAN_OTHER": "1"},
                         "写失败不得改动原文件")
