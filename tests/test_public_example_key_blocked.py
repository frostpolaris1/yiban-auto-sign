# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""公开模板内置示例钥必须被**阻断**（精确比对 ⇒ ValueError），模板本体换占位。

实算登记：`.env.example` 曾把 `0123456789abcdef`×4 作为注释态示例钥——解出
`01 23 45 67 89 ab cd ef` 的 8 字节循环节，`_decode_key` 的三条弱钥判据
（全零／单字节重复／字节值顺序·逆序）**全部逃过**：判据 3 比的是字节值连续，
而模板串连续的是十六进制字符。它随公开仓库人人可读，用它 ⇒ 存量 AES-GCM 密文
等同明文。

裁定修法（本文件钉住）：**精确比对公开串 ⇒ 阻断 + 模板换占位**。精确比对零误杀
（随机钥命中固定 32 字节的概率为 2^-256，工程上为 0）；刻意不做 KDF/通用熵检测——
Web 侧不管理这把钥、没有写侧校验点可挂，"读侧启动即崩"的通用判据会把外泄风险换成
全站不可用并撞存量密钥不可轮换。三条弱钥判据维持 WARNING 不阻断（存量部署换钥
代价大，登记语义不变）。

标签：G · 安全：脱敏/审计/配置注入
覆盖：公开串精确比对在环境变量档与 .env 档两条 load_key 路径上均抛 ValueError（消息
    点名公开模板与更换指引）；大小写十六进制写法同拦；200 把随机钥零误杀；三条弱钥
    模式仍只 WARNING 不阻断；.env.example 不再含
    可用示例钥（占位形态 + 全文件无该公开串）。
    （批 6c3-A 注：旧"三条判据抓不住模板"的实算反向钉属判据自证元测试——它自我比较
    弱判据算术，真契约由 test_exact_public_string_blocked 与 load_key 端到端格钉住，
    已按对表裁撤。）
对应实现：`yiban/infra/account_crypto.py` 的 `_PUBLISHED_EXAMPLE_KEY` 与
    `_decode_key` 精确比对分支、`load_key` 两档取值路径；`.env.example` 密钥段占位。
关键断言：阻断格必须断**抛 ValueError 且消息含"公开示例模板"指引**（只断 raise 会把
    "格式非法"误拦也算过）；零误杀格必须用**足量随机钥**（200 把）而不是单把样本；
    模板格必须直接扫文件文本（占位是否有效，取决于仓库本体而不是代码路径）。
依赖：无网络、无 skip；用例自备临时目录并清 `YIBAN_ACCOUNTS_KEY`/`YIBAN_ENV_FILE`
    环境变量、重置 `_KEY_CACHE`（与 test_account_crypto_key_source 同纪律）。
"""
import io
import os
import secrets
import shutil
import tempfile
import unittest
from unittest import mock

from yiban.infra import account_crypto

PUBLISHED_HEX = "0123456789abcdef" * 4          # 仓库历史模板内置示例钥（本文件钉它被拦）
TEST_KEY = "a" * 64                              # 形状合法但非公开串：三条弱判据全过、不拦


class PublishedExampleKeyDecodeBlockedTest(unittest.TestCase):
    def test_exact_public_string_blocked(self):
        for raw, why in ((PUBLISHED_HEX, "小写原样"),
                         (PUBLISHED_HEX.upper(), "大写写法（fromhex 不分大小写）")):
            with self.subTest(form=why):
                with self.assertRaises(ValueError) as cm:
                    account_crypto._decode_key(raw)
                msg = str(cm.exception)
                self.assertIn("公开示例模板", msg, "错误文案必须点名命中原因，便于运维定位")
                self.assertIn("拒绝", msg)
                self.assertNotIn(PUBLISHED_HEX.lower(), msg, "文案不回带钥值原文")

    def test_random_keys_never_false_blocked(self):
        """零误杀：200 把 `token_hex(32)` 随机钥全部正常解出（随机命中公开串的
        概率 2^-256，工程为 0；把数给足，防判据写成"看起来随机就拦"）。"""
        for _ in range(200):
            raw = secrets.token_hex(32)
            self.assertEqual(account_crypto._decode_key(raw), bytes.fromhex(raw))
        # 形状合法的普通钥同样放行：
        self.assertEqual(account_crypto._decode_key(TEST_KEY), bytes.fromhex(TEST_KEY))

    def test_demo_seed_public_key_also_blocked(self):
        """`scripts/dev_visual_seed.py` 内置演示钥同样随仓库公开：与模板钥一起精确拉黑。

        它逃过全零/单字节/顺序三条弱判据（与模板钥同因），照抄进真实 `.env` 会让存量
        密文等同明文。演示脚本已改为导入同一常量，本格钉住"换钥只改一处也会被拦"。
        """
        for raw in (account_crypto.PUBLISHED_DEMO_ACCOUNTS_KEY,
                    account_crypto.PUBLISHED_DEMO_ACCOUNTS_KEY.upper()):
            with self.subTest(raw=raw[:8]):
                with self.assertRaises(ValueError) as cm:
                    account_crypto._decode_key(raw)
                msg = str(cm.exception)
                self.assertIn("公开示例模板", msg)
                self.assertNotIn(account_crypto.PUBLISHED_DEMO_ACCOUNTS_KEY, msg,
                                 "文案不回带钥值原文")

    def test_weak_patterns_still_warn_only(self):
        """三条弱钥判据维持 WARNING 不阻断（存量部署语义不变，登记口径不动）。"""
        for raw in ("00" * 32, "7f" * 32, bytes(range(32)).hex()):
            with self.subTest(key=raw[:8]):
                with self.assertLogs("yiban-crypto", level="WARNING") as logs:
                    self.assertEqual(account_crypto._decode_key(raw), bytes.fromhex(raw))
                self.assertTrue(any("极易被破解" in ln for ln in logs.output), logs.output)


class LoadKeyBlockedEndToEndTest(unittest.TestCase):
    """load_key 的两档取值路径（环境变量 > .env）都必须把公开串拦在建钥/使用前。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-pubkey-")
        self._old = {k: os.environ.get(k) for k in ("YIBAN_ACCOUNTS_KEY", "YIBAN_ENV_FILE")}
        for k in ("YIBAN_ACCOUNTS_KEY", "YIBAN_ENV_FILE"):
            os.environ.pop(k, None)
        account_crypto._KEY_CACHE = None

    def tearDown(self):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        account_crypto._KEY_CACHE = None
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_env_var_source_blocked(self):
        with mock.patch.dict(os.environ, {"YIBAN_ACCOUNTS_KEY": PUBLISHED_HEX}):
            with self.assertRaises(ValueError) as cm:
                account_crypto.load_key()
            self.assertIn("公开示例模板", str(cm.exception))

    def test_demo_seed_key_env_var_blocked(self):
        """两把公开钥（模板钥 + dev_visual_seed 演示钥）作 YIBAN_ACCOUNTS_KEY 均启动失败。"""
        with mock.patch.dict(os.environ, {
                "YIBAN_ACCOUNTS_KEY": account_crypto.PUBLISHED_DEMO_ACCOUNTS_KEY}):
            with self.assertRaises(ValueError) as cm:
                account_crypto.load_key()
            self.assertIn("公开示例模板", str(cm.exception))

    def test_env_file_source_blocked_and_file_untouched(self):
        env = os.path.join(self.tmp, "app.env")
        with io.open(env, "w", encoding="utf-8") as f:
            f.write("YIBAN_ACCOUNTS_KEY=" + PUBLISHED_HEX + "\n")
        before = io.open(env, "rb").read()
        with self.assertRaises(ValueError) as cm:
            account_crypto.load_key(env_file=env)
        self.assertIn("公开示例模板", str(cm.exception))
        self.assertEqual(io.open(env, "rb").read(), before,
                         "拦截发生在读侧：磁盘不得被顺手改写/换钥")

    def test_existing_file_recheck_on_key_write_also_blocks(self):
        """`_write_key_to_env_file` 的"写前重读既有值"分支同走 `_decode_key`：
        重读命中公开串也要抛，绝不把新钥折掉旧行后照常返回。"""
        env = os.path.join(self.tmp, "app.env")
        with io.open(env, "w", encoding="utf-8") as f:
            f.write("YIBAN_ACCOUNTS_KEY=" + PUBLISHED_HEX + "\n")
        with self.assertRaises(ValueError):
            account_crypto._write_key_to_env_file(env, secrets.token_bytes(32))
        self.assertIn(PUBLISHED_HEX, io.open(env, encoding="utf-8").read(),
                      "被拦时磁盘保持原样（旧行为会把公开串折成新钥——既不解决问题又留痕混淆）")

    def test_random_key_end_to_end_ok(self):
        env = os.path.join(self.tmp, "ok.env")
        key = account_crypto.load_key(env_file=env)   # 自动建钥路径
        self.assertEqual(len(key), 32)
        self.assertIn("YIBAN_ACCOUNTS_KEY=" + key.hex(), io.open(env, encoding="utf-8").read())


class EnvExamplePlaceholderTest(unittest.TestCase):
    """模板本体：公开串不得再出现在 .env.example；该键必须是注释态占位。"""

    def test_template_no_longer_contains_the_published_key(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with io.open(os.path.join(root, ".env.example"), encoding="utf-8") as f:
            text = f.read()
        self.assertNotIn(PUBLISHED_HEX, text.lower())
        # 键赋值形态的行（剥掉注释符后 `YIBAN_ACCOUNTS_KEY=` 开头）：模板里只能有一条，
        # 且必须是注释态占位（"优先级：环境变量 YIBAN_ACCOUNTS_KEY >"这类散文行不算）。
        assign = [ln for ln in text.splitlines()
                  if ln.lstrip().lstrip("#").lstrip().startswith("YIBAN_ACCOUNTS_KEY=")]
        self.assertEqual(len(assign), 1, "模板里该键的赋值形态只出现一次")
        self.assertTrue(assign[0].lstrip().startswith("#"), "占位保持注释态")
        self.assertIn("<", assign[0], "占位形态：非合法十六进制，误用会 fail-loud")


if __name__ == "__main__":
    unittest.main(verbosity=2)
