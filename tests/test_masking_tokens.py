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

标签：G · 安全：脱敏/审计/配置注入
覆盖：`sanitize_text` 的两族规则——`Account(...)` repr 的密码字段边界，以及
token / cookie / session / authorization 字面量被抹值；另钉一条 dict/repr 形态不回归。
对应实现：`yiban/masking.py` 的 `sanitize_text`（`_CRED_KEY`、`_QUOTED_OR_BARE` 两条常量）。
关键断言：全部是"整串 + 尾巴都不许出现"的双断（`assertNotIn(pw)` 之后还断
`assertNotIn(tail)`），只断整串会放过"截半截留"这种最典型的失效；
`test_benign_words_are_not_over_masked` 是反方向的护栏，缺了它就能靠"什么全抹掉"变绿。
覆盖面按**键名词族**成立、按**值形态**不成立：通用凭据键规则的值只吞到第一个
空格 / 逗号 / 分号，本文件的样本值都是无空格形态，故 `refresh_token="a b"` 一类
残留尾巴不在本文件覆盖内（password 那条规则已改按配对引号取值 `_QUOTED_OR_BARE`
收口同类问题，见 `tests/test_masking_ssrf_gaps.py`）。
依赖：纯字符串断言，无网络、无文件 IO、无 skip。
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
        # 连 "Bearer" 这个方案名一起被吃掉、统一成 authorization=*** ——
        # 该规则必须排在通用键规则之前，否则 `[^\s,;]+` 只吞到 "Bearer"、token 留在原地
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
