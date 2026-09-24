# -*- coding: utf-8 -*-
"""账号加密密钥的来源守卫：`load_key` 不得在"来源不确定"时自动建钥落盘。

`account_crypto.load_key` 的自动建钥分支只在"环境变量缺失 + .cenv 无键"时生成，
但不校验密钥来源是否确定——若调用方未传 `env_file`、`YIBAN_ENV_FILE` 未设、
cwd 又无 `.env`（四条同时满足，且该路径要**写**密文），会在错误目录落一份游离
`.env` 与新密钥（与 db 层已修的 `_assert_key_source_certain` 同源缺陷，
db 依赖本模块不能反向 import，故判定就地复刻）。
只读解密路径不受影响（`has_key()` 先判，不触发生成）。

标签：G · 安全：脱敏/审计/配置注入
覆盖：自动建钥的四个来源分支——三者全缺（拒绝）／cwd 已有 `.env`／显式 `env_file`／
`YIBAN_ENV_FILE`（这三条照常生成），外加只读解密路径一条。
对应实现：`yiban/infra/account_crypto.py` 的 `load_key` 自动建钥分支、`has_key`、
模块级密钥缓存 `_KEY_CACHE`。
关键断言：拒绝时抛 ValueError 且消息含"来源不确定"，**并且**临时空目录里不得出现新建的
`.env`——只断"抛了"不够，落盘和抛出是两个独立动作，守卫失效的典型形态正是"照样落盘"。
本文件守的是"在错误目录建出第二把钥"这一条，不覆盖"已存在 `.env` 但内含错钥"的情形。
依赖：无网络、无 skip。用例把 cwd 切到临时目录并在 finally 切回 `tests/`；`setUp` 清
`YIBAN_ACCOUNTS_KEY`/`YIBAN_ENV_FILE` 两个环境变量并重置 `_KEY_CACHE`。
"""
import os
import shutil
import tempfile
import unittest

from yiban.infra import account_crypto

TEST_KEY = "a" * 64


def _reset_cache():
    account_crypto._KEY_CACHE = None


class KeySourceGuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-keyguard-")
        self._old = {k: os.environ.get(k) for k in ("YIBAN_ACCOUNTS_KEY",
                                                    "YIBAN_ENV_FILE")}
        for k in ("YIBAN_ACCOUNTS_KEY", "YIBAN_ENV_FILE"):
            os.environ.pop(k, None)
        _reset_cache()

    def tearDown(self):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        _reset_cache()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_unspecified_source_refuses_generation(self):
        """来源不确定（无显式 env_file、无 YIBAN_ENV_FILE、cwd 无 .env）→ 拒绝。"""
        os.chdir(self.tmp)  # 空目录：无 .env（守卫的判定里 cwd 是第三档来源，必须真换目录）
        try:
            with self.assertRaises(ValueError) as ctx:
                account_crypto.load_key()
        finally:
            os.chdir(os.path.dirname(os.path.abspath(__file__)))
        self.assertIn("来源不确定", str(ctx.exception))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, ".env")),
                         "拒绝生成时不得在错误目录落 .env")

    def test_existing_cwd_dotenv_still_generates(self):
        """cwd 已有 .env（来源确定）→ 行为不变（正常首启在应用根生成）。"""
        os.chdir(self.tmp)
        try:
            with open(".env", "w", encoding="utf-8") as f:
                f.write("OTHER_KEY=1\n")
            key = account_crypto.load_key()
            self.assertEqual(len(key), 32)
            with open(".env", encoding="utf-8") as f:
                self.assertIn("YIBAN_ACCOUNTS_KEY=", f.read())
        finally:
            os.chdir(os.path.dirname(os.path.abspath(__file__)))

    def test_explicit_env_file_still_generates(self):
        """显式 env_file → 来源确定，照常生成（目录须已存在，与既有行为一致）。"""
        target_dir = os.path.join(self.tmp, "conf")
        os.makedirs(target_dir)
        target = os.path.join(target_dir, ".env")
        key = account_crypto.load_key(env_file=target)
        self.assertEqual(len(key), 32)
        self.assertTrue(os.path.exists(target), "显式来源应照常建钥落盘")

    def test_env_var_source_still_generates(self):
        """YIBAN_ENV_FILE 已设 → 来源确定，照常生成。"""
        os.chdir(self.tmp)
        try:
            env_dir = os.path.join(self.tmp, "envs")
            os.makedirs(env_dir)
            os.environ["YIBAN_ENV_FILE"] = os.path.join(env_dir, ".env")
            key = account_crypto.load_key()
            self.assertEqual(len(key), 32)
            self.assertTrue(os.path.exists(os.environ["YIBAN_ENV_FILE"]))
        finally:
            os.chdir(os.path.dirname(os.path.abspath(__file__)))

    def test_read_only_path_unaffected(self):
        """只读解密路径不触发生成，守卫不影响 has_key 判定。"""
        os.chdir(self.tmp)
        try:
            self.assertFalse(account_crypto.has_key())
        finally:
            os.chdir(os.path.dirname(os.path.abspath(__file__)))


if __name__ == "__main__":
    unittest.main()
