# -*- coding: utf-8 -*-
"""协议层账号标识脱敏与登录页 key 破损的返回契约。

`protocol.py` 模块头声明本层「不含本项目自有的安全判断」，账号标识的脱敏口径因此
经 `RequestPolicy.mask_account` 注入（`security.py` 实现为 `masking.mask_phone`）：
裸拼手机号会让注释与代码互相矛盾，并把完整号写进日志与异常消息。`parse_login_page`
对「key 命中但损坏」必须与「没命中」同价返回 `(None, None)`——底层 binascii/ValueError
不得越过返回契约落到调用方。

两处都用**真实 `ProtocolPolicy`** + 脚本化假会话钉住（不发网络请求）：断言的是注入
路径真的接上了 `masking.mask_phone`，而不是"文件里有这个词"。
"""
import unittest

from Crypto.PublicKey import RSA

from yiban.fyiban import protocol as fyiban_protocol
from yiban.security import ProtocolPolicy

# 测试用 RSA-1024 公钥：RSA 生成较慢，模块内生成一次共用（各用例互不影响）。
_TEST_PUBKEY_PEM = None


def _pubkey_pem():
    global _TEST_PUBKEY_PEM
    if _TEST_PUBKEY_PEM is None:
        _TEST_PUBKEY_PEM = RSA.generate(1024).publickey().export_key().decode("utf-8")
    return _TEST_PUBKEY_PEM


class _Resp:
    """最小响应替身：只需协议层真正读到的四个属性（text/json/url/headers）。"""

    def __init__(self, json_data=None, text="", url="", status=200, headers=None):
        self._json = json_data
        self.text = text
        self.url = url
        self.status_code = status
        self.headers = headers or {}
        self.history = []

    def json(self):
        return self._json


class _ScriptedSession:
    """按列表顺序吐出响应的假会话；`headers` / `cookies` 支持协议层的赋值与 update。"""

    def __init__(self, responses):
        self._responses = list(responses)
        self.headers = {}
        self.cookies = {}
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(("GET", url))
        return self._responses.pop(0)

    def post(self, url, **kwargs):
        self.calls.append(("POST", url))
        return self._responses.pop(0)


def _legacy_page(key_value):
    return (
        '<input type="hidden" id="key" value="%s">'
        "<script>page_use = 'pageuse12345';</script>"
    ) % key_value


def _killyiban_page(key_value):
    return (
        '<input type="hidden" id="key" value="%s"/>'
        "<script>var page_use = 'pageuse12345';</script>"
    ) % key_value


class LoginFailureMessageTest(unittest.TestCase):
    def test_failure_message_masks_phone(self):
        """usersure 回 error reUrl → 异常消息里的账号只能是 `138****8000` 形态。"""
        oauth_url = "https://oauth.yiban.cn/code/html"
        session = _ScriptedSession([
            _Resp(json_data={"code": 0, "data": {"Data": oauth_url}}),
            _Resp(text=_legacy_page(_pubkey_pem()), url=oauth_url),
            _Resp(json_data={"reUrl": "https://f.yiban.cn/iapp7463?error=1"}),
        ])
        with self.assertRaises(RuntimeError) as ctx:
            fyiban_protocol.login_legacy(
                session, phone="13800138000", password=b"pw", csrf="c",
                policy=ProtocolPolicy())
        message = str(ctx.exception)
        self.assertIn("138****8000", message)
        self.assertNotIn("13800138000", message)


class DiagnosticsHeaderTest(unittest.TestCase):
    def test_diagnostics_header_masks_phone(self):
        """诊断日志首行 `[账号] 阶段诊断:` 的账号同样走脱敏口径。"""
        policy = ProtocolPolicy()
        resp = _Resp(text="not a login page", url="https://oauth.yiban.cn/code/html")
        with self.assertLogs("yiban.security", level="ERROR") as logs:
            policy.log_response_diagnostics("13800138000", resp, stage="测试", advice=False)
        self.assertTrue(any("138****8000" in line for line in logs.output))
        self.assertFalse(any("13800138000" in line for line in logs.output))


class CorruptedKeyContractTest(unittest.TestCase):
    def test_corrupted_key_returns_none_pair(self):
        """key 命中但损坏（base64/DER 不可解）与没命中同价：不得抛底层异常。"""
        cases = (
            ("killyiban", _killyiban_page("!!!not-base64!!!")),
            ("killyiban", _killyiban_page("YWJjZGVm")),      # 合法 base64、非 DER
            ("legacy", _legacy_page("!!!not-a-key!!!")),
        )
        for flow, page in cases:
            with self.subTest(flow=flow, page=page):
                self.assertEqual(
                    fyiban_protocol.parse_login_page(page, flow=flow), (None, None))

    def test_valid_page_still_parses(self):
        """守卫不得变成"永远返回 (None, None)"：合法页两分支都要照常解出公钥。"""
        for flow, page in (("killyiban", _killyiban_page(_pubkey_pem())),
                           ("legacy", _legacy_page(_pubkey_pem()))):
            with self.subTest(flow=flow):
                page_use, key = fyiban_protocol.parse_login_page(page, flow=flow)
                self.assertEqual(page_use, "pageuse12345")
                self.assertEqual(key.n, RSA.import_key(_pubkey_pem()).n)


if __name__ == "__main__":
    unittest.main(verbosity=2)
