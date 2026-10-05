# -*- coding: utf-8 -*-
"""线路落盘适配器（协议核验诊断）的行为契约。

硬约束：默认关闭；请求行为零改变；凭据面（Cookie/Authorization/凭据字段/手机号）
只记形态不记值；落盘失败绝不影响请求。

凭据字段名口径只有一张表：`yiban.masking._CRED_KEY`（`pwd` 片段覆盖真实字段
`oauth_upwd`）；手机号值走 `masking.mask_phones_in_text`（覆盖 `oauth_uname`
这类承载手机号、键名不含凭据片段的字段）。改这里必须同时改 masking 的口径。
"""

import json
import os
import re
import tempfile
import unittest
from unittest import mock

import requests

from yiban import state_gc
from yiban._vendor import yiban_protocol
from yiban.infra import wire_dump

#: 真实登录体形态里的手机号与密码密文（本文件的红线对象）
LOGIN_PHONE = "13812345678"
LOGIN_CIPHER = "Q2lwaGVy9Xk3Q0Z1b2FvLW5vdC1hLXRva2Vu"


def _response(status=200, body="ok", headers=None, url="https://c.uyiban.com/x"):
    resp = requests.Response()
    resp.status_code = status
    resp._content = body.encode("utf-8")
    resp.headers.update(headers or {"Content-Type": "application/json"})
    resp.url = url
    return resp


class _Inner:
    """假内层适配器：记录是否被调用，返回给定响应。"""

    def __init__(self, resp=None, error=None):
        self.resp = resp or _response()
        self.error = error
        self.called = False

    def send(self, request, **kwargs):
        self.called = True
        if self.error:
            raise self.error
        return self.resp

    def close(self):
        pass


class WireDumpTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="yiban-wire-")

    def _req(self, body=None, headers=None):
        req = requests.Request("POST", "https://oauth.yiban.cn/code/usersure",
                               data=body, headers=headers)
        prepared = req.prepare()
        return prepared

    def _read_lines(self):
        path = os.path.join(self.dir, sorted(os.listdir(self.dir))[0])
        with open(path, encoding="utf-8") as f:
            return [json.loads(ln) for ln in f if ln.strip()]

    def _read_raw(self):
        path = os.path.join(self.dir, sorted(os.listdir(self.dir))[0])
        with open(path, encoding="utf-8") as f:
            return f.read()

    def test_off_by_default(self):
        session = requests.Session()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("YIBAN_WIRE_DUMP", None)
            self.assertFalse(wire_dump.maybe_mount(session, "13800138000"))
        self.assertNotIsInstance(session.get_adapter("https://"),
                                 wire_dump.WireDumpAdapter)

    def test_mount_wraps_and_records(self):
        session = requests.Session()
        with mock.patch.dict(os.environ, {wire_dump._ENV_KEY: self.dir}):
            self.assertTrue(wire_dump.maybe_mount(session, "13800138000"))
            self.assertTrue(session._wire_dump_mounted)
            # 幂等：二次调用不叠加包一层
            self.assertTrue(wire_dump.maybe_mount(session, "13800138000"))
        inner = _Inner()
        session.get_adapter("https://")._inner = inner
        resp = session.send(self._req(
            body="account=13800138000&password=super-secret",
            headers={"Cookie": "yibanMood=abc12345; CSRF=X"}, ), timeout=3)
        self.assertTrue(inner.called, "请求必须原样到达内层适配器")
        self.assertEqual(resp.status_code, 200)
        rows = self._read_lines()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["method"], "POST")
        self.assertEqual(row["phone"], "138****8000")
        self.assertNotIn("super-secret", json.dumps(row), "密码值不得落盘")
        self.assertNotIn("abc12345", json.dumps(row), "Cookie 值不得落盘")
        self.assertNotIn("13800138000", json.dumps(row), "手机号明文不得落盘")
        self.assertEqual(row["status"], 200)
        self.assertEqual(row["req_body"], "account=138****8000&password=%s"
                         % (wire_dump._REDACTED % len("super-secret")))

    def test_dump_failure_never_breaks_request(self):
        session = requests.Session()
        # 目录指向一个普通文件：os.write 必失败，请求仍须成功返回
        bogus = os.path.join(self.dir, "not-a-dir")
        with open(bogus, "w", encoding="utf-8") as f:
            f.write("x")
        with mock.patch.dict(os.environ, {wire_dump._ENV_KEY: bogus}):
            mounted = wire_dump.maybe_mount(session, "13800138000")
        inner = _Inner(resp=_response(body='{"ok":1}'))
        if mounted:
            session.get_adapter("https://")._inner = inner
            resp = session.send(self._req(body="a=1"), timeout=3)
            self.assertEqual(resp.status_code, 200, "落盘失败不得影响请求")
            self.assertTrue(inner.called)

    def test_body_clip_and_binary(self):
        adapter = wire_dump.WireDumpAdapter(_Inner(), self.dir, "13800138000")
        big = _response(body="x" * (wire_dump._BODY_CAP + 10))
        adapter._dump(self._req(body="a=1"), big, 0.001)
        row = self._read_lines()[0]
        self.assertTrue(row["resp_body"]["truncated"])
        self.assertEqual(len(row["resp_body"]["head"]), wire_dump._BODY_CAP)

    # ---- 缺陷正面判据：真实登录体形态不得把手机号/密文落盘 ----

    def test_login_body_dump_keeps_no_phone_and_no_cipher(self):
        """红线：读回落盘 JSONL，不含手机号明文、不含密码密文原文。

        真实登录体字段名是 `oauth_uname`（承载手机号）与 `oauth_upwd`（密文），
        旧门槛只认子串 `password`，两者都漏。
        """
        adapter = wire_dump.WireDumpAdapter(_Inner(), self.dir, LOGIN_PHONE)
        body = ("oauth_uname=%s&oauth_upwd=%s&csrf_token=%s&phone_model=MockPhone"
                % (LOGIN_PHONE, LOGIN_CIPHER, "csrf0abc123"))
        adapter._dump(self._req(body=body), _response(), 0.001)
        raw = self._read_raw()
        self.assertNotIn(LOGIN_PHONE, raw, "手机号明文不得落盘")
        self.assertNotIn(LOGIN_CIPHER, raw, "密码密文原文不得落盘")
        # 字段名与"非敏感值"仍在：落盘件的核验价值不得被抹掉
        self.assertIn("oauth_uname", raw)
        self.assertIn("oauth_upwd", raw)
        self.assertIn("phone_model=MockPhone", raw)
        self.assertIn("138****5678", raw, "手机号须按 masking 口径打码而非删除")

    def test_cred_field_variants_are_all_masked(self):
        """多形态：表单 k=v、JSON、字段名大小写/前后缀变体都要被同一张键名表覆盖。"""
        cases = [
            ("oauth_upwd=%s" % LOGIN_CIPHER, LOGIN_CIPHER),          # 真实字段名（pwd 片段）
            ("OAuth_Upwd=%s" % LOGIN_CIPHER, LOGIN_CIPHER),          # 大小写变体
            ("api_key=abcdef123456", "abcdef123456"),                # 前后缀复合名
            ("refresh_token=tok0abcdef", "tok0abcdef"),              # 后缀变体
            ('{"oauth_upwd": "%s", "phone_model": "MockPhone"}' % LOGIN_CIPHER,
             LOGIN_CIPHER),                                          # JSON 形态
            ('{"CSRF_TOKEN": "csrf0abc123"}', "csrf0abc123"),         # JSON 大写变体
        ]
        for body, secret in cases:
            with self.subTest(body=body):
                out = wire_dump._redact_body(body)
                self.assertNotIn(secret, out, "凭据值不得原样保留: %s" % body)
                self.assertIn(wire_dump._REDACTED.split("%")[0], out)

    def test_no_credential_body_is_not_dumped_verbatim(self):
        """判定为"无凭据"时也走同一套脱敏（幂等）：手机号仍被打码，不得原样全量落盘。"""
        out = wire_dump._redact_body("uname=%s&device=MockPhone" % LOGIN_PHONE)
        self.assertNotIn(LOGIN_PHONE, out)
        self.assertEqual(out, "uname=138****5678&device=MockPhone")

    def test_ordinary_values_are_not_over_masked(self):
        """反向：普通字段不得被过度打码，否则核验样本失去价值。"""
        body = "phone_model=MockPhone&app_version=6.0.1&device=Monkey&note=consider"
        self.assertEqual(wire_dump._redact_body(body), body)

    def test_url_faces_are_sanitized(self):
        """URL 面：`url` 与 `final_url` 的 query 凭据参数走 masking.sanitize_url。"""
        adapter = wire_dump.WireDumpAdapter(_Inner(), self.dir, LOGIN_PHONE)
        req = self._req(body="a=1")
        resp = _response(url="https://c.uyiban.com/cb?code=SECRETCODE&session=sess0")
        adapter._dump(req, resp, 0.001)
        row = self._read_lines()[0]
        self.assertNotIn("SECRETCODE", json.dumps(row))
        self.assertNotIn("sess0", json.dumps(row))

    # ---- D1：承载 URL 的响应头不得原样落盘 ----

    def test_url_bearing_response_headers_are_sanitized(self):
        """响应头里承载的 URL 走 `masking.sanitize_url`，令牌不得原样落盘。

        真 OAuth 第 4 步用 `allow_redirects=False`，令牌在 `Location` 头
        （`verify_request` / `code` / `#access_token=`）；`Content-Location`、
        `Link`、`Refresh` 同族。头名须保留，核验要看得见形状。
        """
        cases = [
            ("Location",
             "https://c.uyiban.com/cb?verify_request=VERIFYTOK&code=CODETOK",
             ["VERIFYTOK", "CODETOK"]),
            ("Location", "https://c.uyiban.com/cb#access_token=FRAGTOK", ["FRAGTOK"]),
            ("Content-Location", "https://c.uyiban.com/cb?code=CODETOK2", ["CODETOK2"]),
            ("Link", '<https://c.uyiban.com/n?code=CODETOK3>; rel="next"', ["CODETOK3"]),
            ("Refresh", "0; url=https://c.uyiban.com/n?code=CODETOK4", ["CODETOK4"]),
        ]
        for name, value, secrets in cases:
            with self.subTest(header=name, value=value):
                self.dir = tempfile.mkdtemp(prefix="yiban-wire-")
                adapter = wire_dump.WireDumpAdapter(_Inner(), self.dir, LOGIN_PHONE)
                resp = _response(headers={"Content-Type": "text/html", name: value})
                adapter._dump(self._req(body="a=1"), resp, 0.001)
                raw = self._read_raw()
                for secret in secrets:
                    self.assertNotIn(secret, raw, "%s 头内令牌不得落盘" % name)
                self.assertIn(name, raw, "头名须保留")

    # ---- D2：签到体 `Code`（=`phone_code` 设备绑定码）不得原样落盘 ----

    def test_sign_in_body_code_field_is_masked(self):
        """签到体 `Code` 字段值来自 `phone_code`（设备绑定码），须按键名脱敏。

        `Code` 不在 `_CRED_KEY`；宽表 `masking._QKEYED_CRED` 覆盖 `phone_code`
        但不覆盖裸 `Code`，故 wire 侧显式补 `code` 字段名口径。
        """
        device_code = "DEVICEBINDTOKEN9"
        body = yiban_protocol.build_sign_in_body(
            lnglat=(1.2, 3.4), address="A栋",
            code=device_code, phone_model="MockPhone",
        )
        adapter = wire_dump.WireDumpAdapter(_Inner(), self.dir, LOGIN_PHONE)
        adapter._dump(self._req(body=body), _response(), 0.001)
        raw = self._read_raw()
        self.assertNotIn(device_code, raw, "设备绑定码不得落盘")
        self.assertIn("Code", raw, "字段名须保留")
        self.assertIn("PhoneModel=MockPhone", raw, "非敏感字段值须保留")
        self.assertIn("OutState=1", raw)
        # 宽表口径覆盖 `phone_code` 字段名（与 masking 唯一事实源对齐）
        self.assertIsNotNone(re.fullmatch(r"(?i)" + wire_dump._CRED_NAME, "phone_code"))
        self.assertIsNotNone(re.fullmatch(r"(?i)" + wire_dump._CRED_NAME, "Code"))

    # ---- D3：`_redact_body` 幂等 ----

    def test_redact_body_is_idempotent(self):
        """`_redact_body` 幂等：占位串再次进入不被二次计量。"""
        form = "oauth_uname=%s&oauth_upwd=%s" % (LOGIN_PHONE, LOGIN_CIPHER)
        once = wire_dump._redact_body(form)
        self.assertEqual(wire_dump._redact_body(once), once)
        js = '{"oauth_upwd": "%s"}' % LOGIN_CIPHER
        once_j = wire_dump._redact_body(js)
        self.assertEqual(wire_dump._redact_body(once_j), once_j)

    def test_dump_appends_each_time(self):
        """落盘是追加写：两次落盘两行。"""
        adapter = wire_dump.WireDumpAdapter(_Inner(), self.dir, LOGIN_PHONE)
        adapter._dump(self._req(body="a=1"), _response(), 0.001)
        adapter._dump(self._req(body="a=2"), _response(), 0.001)
        self.assertEqual(len(self._read_lines()), 2)

    def test_dump_failure_warns_once_and_keeps_request(self):
        """目录不存在时行为与现状一致：请求正常返回，落盘失败只告警一次。"""
        wire_dump._warned = False
        missing = os.path.join(self.dir, "no-such-dir")
        broken = wire_dump.WireDumpAdapter(_Inner(), missing, LOGIN_PHONE)
        with self.assertLogs("yiban.infra.wire_dump", level="DEBUG") as cm:
            for _ in range(2):
                resp = broken.send(self._req(body="a=1"), timeout=3)
                self.assertEqual(resp.status_code, 200, "落盘失败不得影响请求")
        self.assertEqual(len(cm.output), 1, "落盘失败只告警一次")
        self.assertFalse(os.path.exists(missing))

    def test_wire_artifact_is_registered_for_cleanup(self):
        """名册：`wire-` 落盘件必须被 state_gc 的 match() 真认到（不只看常量表）。"""
        art, date = state_gc.match("wire-2026-10-05.jsonl")
        self.assertIsNotNone(art, "wire- 落盘件未登记进 ARTIFACTS，会无界增长")
        self.assertEqual(art.prefix, "wire-")
        self.assertEqual(art.suffix, ".jsonl")
        self.assertEqual(date, "2026-10-05")
        # 锁伴生文件同样按前缀匹配到
        art_lock, _ = state_gc.match("wire-2026-10-05.jsonl.lock")
        self.assertIsNotNone(art_lock)
        self.assertEqual(art_lock.prefix, "wire-")


if __name__ == "__main__":
    unittest.main()
