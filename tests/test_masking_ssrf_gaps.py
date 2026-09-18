# -*- coding: utf-8 -*-
"""对外脱敏与出站白名单的残余缺口回归（本轮对抗性审查活体复现的三条）。

三条都是"看起来已收口、但构造一个输入就漏"的形态，故钉确定性断言：
1. `sanitize_text` 的凭据键名——`\b` 在 `_`/`-` 处不构成边界，`refresh_token` /
   `session_id` / `id_token` / `JSESSIONID` / `x-csrf` / `api_key` 整族原样返回；
2. `sanitize_text` 的引号配对——口令含单引号时 repr 用双引号包裹，旧值类 `[^'"]*`
   在第一个单引号处截断，残留首引号之后的明文尾巴；
3. `is_safe_url` 的段覆盖——`ipaddress.is_private` 不含 CGNAT 100.64.0.0/10
   （云厂商元数据服务落在此段），组播/保留段与 IPv4-mapped IPv6 写法同样放行。
另附 `_mask_addr` 逗号列表与 `sanitize_url` 手机号两处。
"""
import unittest

from yiban.engine.accounts import Account
from yiban.mail import config as mail_config
from yiban.masking import sanitize_text, sanitize_url
from yiban.notify import config as notify_config

# 口令样本刻意覆盖：单引号、括号、双引号、空格
PASSWORDS = ["p@ss'w0rd)1", "abc'def'ghi", 'x"y"z', "a b c'd", "plain-123"]


class CredKeyNamesTest(unittest.TestCase):
    """复合凭据键名必须整族覆盖（旧实现 6/6 漏网）。"""

    def test_compound_credential_keys_are_masked(self):
        for key in ("refresh_token", "session_id", "id_token", "JSESSIONID",
                    "x-csrf", "api_key", "apikey", "auth-secret", "access_token"):
            value = f"SECRETVALUE_{key}"
            out = sanitize_text(f"{key}={value}")
            self.assertNotIn(value, out, f"{key} 的值不得出现在脱敏结果里: {out}")

    def test_credential_value_with_space_in_double_quotes_leaves_no_tail(self):
        """含空格/括号的值不得在引号处截断（旧实现残留 `w0rd)1`）。"""
        for pw in PASSWORDS:
            acc = Account(phone="13800000000", password=pw)
            for shape in (
                repr(acc),
                f"登录失败 Account(phone='13800000000', password={pw!r})",
                "cfg=" + repr({"phone": "13800000000", "password": pw}),
                '{"password": ' + repr(pw) + "}",
            ):
                out = sanitize_text(shape)
                tail = pw.split("'")[-1]
                self.assertNotIn(pw, out, f"整串泄漏: {out}")
                if len(tail) >= 3:
                    self.assertNotIn(tail, out, f"残留明文尾巴 {tail!r}: {out}")

    def test_benign_words_are_not_over_masked(self):
        """不含凭据词族的普通诊断文本保持原样（防误伤把日志改成不可读）。"""
        text = "sign failed: monkey=1 consider=2 task_id=88 range=10-20"
        self.assertEqual(sanitize_text(text), text)


class SafeUrlSegmentsTest(unittest.TestCase):
    def test_cgnat_multicast_reserved_are_blocked(self):
        for host in ("100.100.100.200", "100.64.0.1", "100.127.255.255",
                     "224.0.0.1", "239.255.255.255", "240.0.0.1"):
            self.assertFalse(notify_config.is_safe_url(f"https://{host}/hook"),
                             f"CGNAT/组播/保留段应被拒: {host}")
        self.assertFalse(notify_config.is_safe_url("https://[ff02::1]/hook"))
        self.assertFalse(notify_config.is_safe_url("https://[::2]/hook"))

    def test_ipv4_mapped_ipv6_cannot_bypass(self):
        for url in ("https://[::ffff:100.100.100.200]/hook",
                    "https://[::ffff:127.0.0.1]/hook",
                    "https://[::ffff:10.0.0.7]/hook"):
            self.assertFalse(notify_config.is_safe_url(url), f"映射写法绕过: {url}")

    def test_public_https_targets_still_allowed(self):
        for url in ("https://sctapi.ftqq.com/x.send", "https://example.com/hook",
                    "https://1.2.3.4/hook"):
            self.assertTrue(notify_config.is_safe_url(url), f"公网目标被误拒: {url}")


class MailAddrMaskingTest(unittest.TestCase):
    def test_comma_listed_recipients_masked_item_by_item(self):
        v = "alice@qq.com,bob@corp.cn,carolwang@secret.org"
        out = mail_config._mask_addr(v)
        for leaked in ("bob@", "carolwang@", "alice@"):
            self.assertNotIn(leaked, out, f"逗号列表泄漏完整地址: {out}")
        self.assertEqual(len(out.split(",")), 3)

    def test_single_and_empty_semantics_unchanged(self):
        self.assertEqual(mail_config._mask_addr("alice@qq.com"), "a****@qq.com")
        self.assertEqual(mail_config._mask_addr(""), "<未配置>")
        self.assertEqual(mail_config._mask_addr("not-an-email"), "not-an-email")


class NotifySecretMaskTest(unittest.TestCase):
    def test_secret_mask_is_fixed_width_and_keeps_both_ends(self):
        short_key = notify_config._mask_secret("SCT1234567890abcdef")
        long_key = notify_config._mask_secret("SCT1234567890abcdefghijklmnopqrstuv")
        self.assertEqual(short_key, "SCT***ef")
        self.assertEqual(long_key, "SCT***uv")
        self.assertEqual(short_key.count("*"), long_key.count("*"),
                         "星数不得随密钥长度变化（否则等于把精确长度也发出去）")

    def test_short_secret_gets_no_tail(self):
        # 6 位值若照"前 3 + 后 2"打码会露出 5/6，打码名不副实
        out = notify_config._mask_secret("abc123")
        self.assertEqual(out, "ab***")
        self.assertEqual(notify_config._mask_secret(""), "")


class UrlPhoneTest(unittest.TestCase):
    def test_phone_in_query_masked_by_name_and_by_value(self):
        by_name = sanitize_url("https://f.yiban.cn/cb?mobile=13800008000")
        self.assertNotIn("13800008000", by_name)
        by_value = sanitize_url("https://f.yiban.cn/cb?u=13800008000&keep=abc")
        self.assertNotIn("13800008000", by_value)
        self.assertIn("keep=abc", by_value, "非手机号参数不受影响")
        self.assertIn("138****8000", by_value, "按值打码保留前 3 后 4（与 mask_phone 同口径）")

    def test_high_entropy_value_rule_still_works(self):
        out = sanitize_url("https://f.yiban.cn/cb?x=" + "A" * 30)
        self.assertNotIn("A" * 30, out)


if __name__ == "__main__":
    unittest.main()
