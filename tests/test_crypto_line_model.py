# -*- coding: utf-8 -*-
"""account_crypto 写钥的行模型：旧键行折叠与解析口径同源，且不实体化潜伏分隔符。

- 折叠：`YIBAN_ACCOUNTS_KEY = `（= 前带空格、值为空）在 `env_io.parse_env_file` 口径下
  是同一条键的行，只认字面前缀 `KEY=` 折不掉它，残留影子行后生效值由行序决定；
- 潜伏分隔符：既有行的值里藏着 U+2028 时，旧写回（splitlines 读 + "\\n".join 写）
  会把它后面的半截拼成真配置行。该情形必须 fail-closed：拒绝写入且一个字节都不改。
"""
import os
import shutil
import tempfile
import unittest

from yiban.infra import account_crypto, env_io

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
        self._write_env("OTHER=a\u2028YIBAN_ADMIN_PASSWORD_HASH=evil\n")
        before = self._read_env()

        with self.assertRaises(ValueError) as ctx:
            account_crypto._write_key_to_env_file(self.env_file, NEW_KEY)

        self.assertIn("行分隔符", str(ctx.exception))
        self.assertIn("OTHER", str(ctx.exception), "消息须给出待清理行的键名线索")
        self.assertEqual(self._read_env(), before, "拒绝写入时不得改动 .env")
        leftovers = [p for p in os.listdir(self.tmp) if ".tmp" in p]
        self.assertEqual(leftovers, [], "拒绝写入时不得先落 tmp 文件")


if __name__ == "__main__":
    unittest.main()
