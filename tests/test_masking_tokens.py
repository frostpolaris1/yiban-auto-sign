# -*- coding: utf-8 -*-
"""对外脱敏（`yiban/masking.py::sanitize_text`）的凭据字面量覆盖测试（M1）。

89 号对抗性审查发现两处缺口：
1. `Account(...)` 正则用 `[^)]*` 当右界——密码**含 `)`** 时在第一个 `)` 处截断，
   残留 `cd'` 之类密文尾巴（实测 `Account(username='u', password='ab)cd',
   phone_code='123456')` 残留 `cd'`）；
2. `access_token=…` / `Authorization: Bearer …` / cookie 等字面量**原样返回**
   （`sanitize_text` 没有任何 token/cookie 规则）。

修复后：① 密码替换按字段名边界（到下一个 `, 字段=` 或串尾）；② 追加
`token|cookie|csrf|session|access_token|authorization|secret` 字面量规则
与 `Authorization: Bearer` 一条。本文件钉住确定性断言。
"""
import unittest

from yiban.masking import sanitize_text


class TokenMaskingTest(unittest.TestCase):
    def test_password_with_close_paren_leaves_no_tail(self):
        """密码含 `)` 不得截断残留（原实现残留 `cd'`）。"""
        text = ("Account(username='u', password='ab)cd', phone_code='123456')")
        out = sanitize_text(text)
        self.assertNotIn("cd", out, f"不得残留密文尾巴: {out}")
        self.assertNotIn("ab", out)
        self.assertIn("Account(***)", out)

    def test_access_token_literal_is_masked(self):
        out = sanitize_text("access_token=abc123xyz, status=ok")
        self.assertNotIn("abc123xyz", out)
        self.assertIn("access_token=***", out)

    def test_authorization_bearer_is_masked(self):
        out = sanitize_text("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig")
        self.assertNotIn("eyJhbG", out)
        self.assertNotIn("Bearer", out)
        self.assertIn("authorization=***", out)

    def test_cookie_and_session_literals_are_masked(self):
        out = sanitize_text("cookie=PHPSESSID=deadbeef; path=/ ; session=xyz")
        self.assertNotIn("deadbeef", out)
        self.assertNotIn("xyz", out)

    def test_payment_object_repr_still_masked(self):
        """dict/repr 形态（'password': 'xxx'）仍被覆盖（既有行为不回归）。"""
        out = sanitize_text("{'username': 'u', 'password': 'ab)cd'}")
        self.assertNotIn("ab", out)


if __name__ == "__main__":
    unittest.main()
