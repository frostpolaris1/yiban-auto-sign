# -*- coding: utf-8 -*-
"""store 两处 .env 写键（审计密钥 / 追踪盐）与 account_crypto 同款行模型。

根因是**校验用的行模型与写入用的行模型不是同一个**：旧实现用
`f.read().splitlines()` 读、`"\\n".join()` 写——`str.splitlines()` 除 \\n / \\r 外还把
\\u2028 等 8 个字符当行边界。值里潜伏这些字符时，旧写回会把后半截拼成**真配置行**
（`parse_env_file` 后写覆盖先写 → 载荷生效，实测可注入 `YIBAN_ADMIN_PASSWORD_HASH=`）。

三处写入方（账号密钥 / 审计密钥 / 追踪盐）共用 `env_io.write_env_key` 的窄行模型后，
本文件钉住 store 侧两处的行为：
- 潜伏分隔符 → ValueError 且**磁盘一个字节都不改**、不留 tmp；
- 旧键行折叠认得 `KEY = v`（= 前带空白）写法，不积累出同键第二行；
- 无潜伏分隔符时正常写入，其他行照旧保留。
"""
import io
import os
import shutil
import tempfile
import unittest

from yiban.infra import env_io
from yiban.store import audit_chain, tracking

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


if __name__ == "__main__":
    unittest.main()
