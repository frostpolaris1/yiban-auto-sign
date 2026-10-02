# -*- coding: utf-8 -*-
"""线路落盘适配器（协议核验诊断）的行为契约。

硬约束：默认关闭；请求行为零改变；凭据面（Cookie/Authorization/password）只记
形态不记值；落盘失败绝不影响请求。
"""

import json
import os
import tempfile
import unittest
from unittest import mock

import requests

from yiban.infra import wire_dump


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
        self.assertEqual(row["status"], 200)
        self.assertEqual(row["req_body"], "account=13800138000&password=%s"
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


if __name__ == "__main__":
    unittest.main()
