# -*- coding: utf-8 -*-
"""密钥缓存按来源分开 + 解密入口的 key 校验。

缓存若只有一个槽位且不问来源，同进程先 load_key(a) 再 load_key(b) 会直接返回 a 的钥
（b 的盘既不读也不打缓存），而 has_key(b) 同时说"有钥"——两个 API 对同一个文件给出
相反答案。这里钉住：按 env_file 取值、环境变量来源不冒充文件来源、非法 key 抛 ValueError
而非 TypeError（调用方只 except ValueError）。
"""
import os
import shutil
import tempfile
import unittest

from yiban.infra import account_crypto

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


if __name__ == "__main__":
    unittest.main()
