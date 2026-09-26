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
覆盖面按**键名词族**与**值形态**同时成立：凭据值按配对引号或整段裸值取
（裸值只在"后续出现新的 `key=value` 对"处收尾），本文件钉登记实测五例、
`password=abc,def` 同族例与 authorization 跨空格例，并含三个反方向的对照例
（非键值正文、后续非凭据对、cookie 逐对语义）——只有负例的话"全部抹掉"也算过。
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


class CredentialValueSpanTest(unittest.TestCase):
    """凭据值必须整段遮没：空格/逗号/分号/引号都不许截半留明文尾。

    判据（"值的结束 = 下一段看起来是新的 `key=value` 对"）写在
    `yiban/masking.py` 的 `_QUOTED_OR_BARE` 注释里；兜底层按宁过遮不漏取舍，
    值后不含 `=` 的散文会被一并遮掉——对照例钉住**不该遮**的三类不被误伤。
    """

    def test_registered_truncation_examples_fully_masked(self):
        """登记实测五例 + password 同族例：旧实现全部残留明文尾（`*** def"` 等）。"""
        for text, want in (
            ('refresh_token="abc def"', "refresh_token=***"),
            ('session_cookie="a b c"', "session_cookie=***"),
            ('csrf="x y"', "csrf=***"),
            ("api_key=foo bar", "api_key=***"),
            ("refresh_token=abc,def", "refresh_token=***"),
            ("password=abc,def", "password=***"),
        ):
            with self.subTest(text=text):
                self.assertEqual(sanitize_text(text), want)

    def test_authorization_bearer_spanning_space(self):
        """`Bearer <token> <尾巴>` 整段遮：旧实现只吞到 token 首段。"""
        out = sanitize_text("Authorization: Bearer abc def")
        self.assertNotIn("def", out)
        self.assertNotIn("Bearer", out)
        self.assertIn("authorization=***", out)

    def test_bare_value_after_unclosed_quote_masked(self):
        """引号未配对时裸值分支兜底：`token="abc def` 不得留下 ` def"`。"""
        out = sanitize_text('token="abc def')
        self.assertNotIn("def", out)
        self.assertIn("token=***", out)

    def test_comma_prose_without_pair_shape_untouched(self):
        """对照例①：含逗号的正文没有键值形态，一字不改。"""
        text = "登录失败，已重试 2 次, next attempt in 60s"
        self.assertEqual(sanitize_text(text), text)

    def test_following_non_credential_pair_kept(self):
        """对照例②：值在"后续新 key= 对"处收尾——非凭据对不被连坐吞掉。"""
        out = sanitize_text("token=abc next=ok")
        self.assertEqual(out, "token=*** next=ok")

    def test_cookie_pairs_masked_item_by_item(self):
        """对照例③（也是裸值不放宽到行尾的理由）：cookie 串逐对遮，非凭据属性留在原地。"""
        out = sanitize_text("session_id=x; path=/; token=y")
        self.assertEqual(out, "session_id=***; path=/; token=***")


if __name__ == "__main__":
    unittest.main()
