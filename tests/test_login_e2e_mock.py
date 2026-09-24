# -*- coding: utf-8 -*-
"""登录 / 签到链的**端到端演练**：真 HTTP 往返 + 假易班服务端（绝不碰真实易班）。

为什么需要这一层：登录路径是"钱路"——请求形状、跳转顺序、成功标志任一处改错都可能
悄悄坏到线上，而单测用脚本化响应只能证明"我们发的确实是这个形状"，证明不了"这套形状
能跑完一整条链"。这里用 `scripts/loadtest/mock_yiban.py` 起一个假服务端，让真实客户端
从头跑到尾（默认 KillYiBan 流程 + 旧 iOS 流程两条），并按落盘 JSONL 断言**握手顺序**。

假服务端跑在回环**明文 HTTP** 上；客户端的 https URL 由**测试侧适配器**改写到本机
端口——不改被测代码的任何常量或分支，CI 也不需要 root/TLS/改 hosts。
"""
import importlib
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from urllib.parse import urlsplit

import requests
import signin

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_LOADTEST = os.path.join(_ROOT, "scripts", "loadtest")
if os.path.dirname(_LOADTEST) not in sys.path:
    sys.path.insert(0, os.path.dirname(_LOADTEST))

mock_yiban = importlib.import_module("loadtest.mock_yiban")


_REAL_MOCK_SEND = None  # 由下方在类定义后绑定：打补丁时不能再按类属性取（会递归）


class _LocalMockAdapter(requests.adapters.BaseAdapter):
    """把任意 https://<host>/path 的请求改写到本机假服务端（明文 HTTP）。

    Host 头保留原值：假服务端按 Host 统计，演练里也就顺带验证了"两个域的请求
    打到各自该打的地方"。改写发生在**连接层**，故断言仍是对真实发出的请求做的。
    """

    def __init__(self, base_url):
        super().__init__()
        self._base = base_url
        self._inner = requests.adapters.HTTPAdapter()

    def send(self, request, **kwargs):
        logical_url = request.url  # 逻辑 URL（https://<host>/…，连接层改写前）
        parts = urlsplit(request.url)
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        request.headers["Host"] = parts.netloc
        request.url = self._base + path
        resp = self._inner.send(request, **kwargs)
        # 改写只发生在连接层：响应 URL 必须保持逻辑形态，否则协议层基于
        # resp.url/history 的白名单判定（M8 逐跳校验）会把回环地址误判为
        # "跳转 URL 不在白名单"。history 里的每一跳也走本 send（重定向时
        # requests 会用 Location 重新构造逻辑 request），url 同样被还原。
        resp.url = logical_url
        return resp

    def close(self):
        self._inner.close()


_REAL_MOCK_SEND = _LocalMockAdapter.send


class _FakeYiban:
    """进程内假易班（明文 HTTP，端口随机）。"""

    def __init__(self, **cfg):
        # 每个实例一个独立日志：跨用例共用文件会把上一轮的请求也读进来
        self.log_path = os.path.join(tempfile.mkdtemp(prefix="yiban-e2e-"), "mock.jsonl")
        state = mock_yiban.MockState(log_path=self.log_path)
        config = mock_yiban.MockConfig(**cfg)
        servers, state, config = mock_yiban.create_servers(
            host="127.0.0.1", port=0, cert=None, key=None,
            state=state, config=config, enable_ipv6=False,
        )
        self.server, self.state = servers[0], state
        self.port = self.server.server_address[1]
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()
        self._assert_loopback_is_not_intercepted()

    def _assert_loopback_is_not_intercepted(self):
        """响亮失败守卫：确认发往本机回环的请求**真的**落在本用例的假服务端上。

        为什么必须有：适配器把 URL 改写成 `http://127.0.0.1:<port>` 但**保留原始 Host 头**
        （假服务端按 Host 统计）。于是任何本机"加速器 / 透明代理 / TUN 模式"都可能按 Host
        把请求转发到**真实易班**——请求量、账号锁定、风控都会真实发生，而用例只是"失败"，
        看不出它其实打过真站。实测踩到一次：Steam++ 加速器监听 443 时，
        响应头带 `Server: WAF` 与 `.yiban.cn` 的真站 cookie，假服务端一条记录都没收到。

        判据：带**原始 Host 头**（`oauth.yiban.cn`）去探假服务端自己的运维入口 `/__health` ——
        这与真实请求走的是**同一条路径**（适配器改写 URL 但保留 Host），拦截层正是按 Host
        决定往真站转发；拿到的不是假服务端就报错，并给出可操作处置（退出加速器/系统代理后重跑）。
        """
        import requests as _rq
        # 判据只有一条：带**原始 Host** 探假服务端的运维入口（与真实请求同一条路径）。
        # 不查 `getproxies()` 是因为**本机常开着系统代理**，而 `_client()` 已把会话设为
        # `trust_env=False`（会话不看代理），此时系统代理并不影响本用例；
        # 真正要防的是**透明拦截**（加速器/TUN 按 Host 转发），那只能靠这一探发现。

        try:
            r = _rq.get(f"http://127.0.0.1:{self.port}/__health",
                        headers={"Host": "oauth.yiban.cn"}, timeout=3)
        except Exception as e:
            self._fail_intercepted(f"直连回环失败: {type(e).__name__}: {e}")
            return
        server = r.headers.get("Server", "")
        if "yiban-mock" not in server.lower():
            self._fail_intercepted(
                f"回环响应不是假服务端（Server={server!r}，body 前 60 字={r.text[:60]!r}）")

    @staticmethod
    def _fail_intercepted(detail):
        raise AssertionError(
            "本机回环流量被拦截，假服务端收不到请求——本用例需要真实回环才能保证"
            "「绝不连接真实易班」。请退出加速器/系统代理/TUN 模式后重跑。\n"
            f"细节: {detail}"
        )

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def requests(self):
        """逐请求记录（按发生顺序），来自假服务端的 JSONL。

        ⚠ 容错：读的是**另一个线程**正在追加的文件（服务端每写一条就 close），
        并发下可能读到只写了一半的末行 → `json.loads` 抛 JSONDecodeError。
        故只把**无法解析的末行**当作"尚未写完"跳过；中间出现坏行仍按错误抛出，
        免得真把"日志写坏了"当成正常。
        """
        rows = []
        with open(self.log_path, encoding="utf-8") as f:
            lines = [ln for ln in f if ln.strip()]
        for i, line in enumerate(lines):
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                if i == len(lines) - 1:
                    break  # 末行未写完（并发追加）
                raise
        # 运维入口（存活自检 / 统计）不算业务请求：本类 setUp 的"回环被拦截"守卫会探 /__health
        return [r for r in rows if r.get("path") not in ("/__health", "/__stats")]

    def wait_paths(self, count, timeout=3.0):
        """等落盘记录达到 `count` 条后返回路径序列。

        假服务端在**写完响应之后**才落盘，读早了会少最后一条（客户端已拿到响应
        返回，服务端线程还在写日志）。
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and len(self.requests()) < count:
            time.sleep(0.02)
        return [r["path"] for r in self.requests()]


def _waf_body(path_fragment):
    """构造"某个路径的响应变成 WAF 拦截页"的适配器补丁。"""

    def send(adapter, request, **kwargs):
        if path_fragment in request.url:
            resp = requests.Response()
            resp.status_code = 200
            resp._content = "您的访问存在风险访问，已被拦截".encode("utf-8")
            resp.url = request.url
            return resp
        return _REAL_MOCK_SEND(adapter, request, **kwargs)

    return send


class _E2EBase(unittest.TestCase):
    def setUp(self):
        self.mock = _FakeYiban()
        self.addCleanup(self.mock.close)

    def _client(self, *, legacy=False, account_id=0):
        """构造真实客户端并把它的 https 流量改写到假服务端。

        会话缓存（db）在演练里显式关闭：那不是协议链的一部分，且会引入库依赖；
        缓存本身的语义由 `tests/test_session.py` 与 `tests/test_login_protocol_shape.py`
        覆盖。
        """
        acc = signin.Account(phone="13800138000", password="secret-pw",
                             account_id=account_id)
        env = {"YIBAN_LEGACY_LOGIN": "1" if legacy else ""}
        with mock.patch.dict(os.environ, env, clear=False):
            os.environ.pop("YIBAN_PROXY", None)
            with mock.patch.object(signin.db, "is_initialized", return_value=False):
                client = signin.YibanClient(acc)
        client.session.mount("https://", _LocalMockAdapter(f"http://127.0.0.1:{self.mock.port}"))
        # **必须关掉系统/环境代理**：本用例要求完全离线，而 requests 默认 `trust_env=True`
        # 会读系统代理设置（Windows 注册表 / 环境变量）。实测踩到一次：本机开着 Clash 类代理
        # （127.0.0.1:7897）时，回环请求被代理按 **Host 头**（`oauth.yiban.cn`）转发到真实易班
        # ——假服务端一条记录都收不到，而用例只是报"登录失败"，看不出真站已被访问过。
        client.session.trust_env = False
        client.session.proxies = {}
        return client


class KillYiBanE2ETest(_E2EBase):
    """默认登录方式：完整跑通登录链 + 探针 + 一次签到。"""

    def test_full_chain_rehearsal(self):
        client = self._client()
        client.login_killyiban()
        self.assertTrue(client.logged_in, "登录链必须跑完并落到已登录态")

        ok, message = client.verify()
        self.assertTrue(ok, f"探针应成功: {message}")

        success, message, skip, status = client.signin()
        self.assertTrue(success, f"签到应成功: {message}")
        self.assertFalse(skip)
        self.assertEqual(status, signin.STATUS_SUCCESS)

        # 握手顺序（假服务端按发生顺序落盘，是这条链的"真实轨迹"）
        paths = self.mock.wait_paths(7)
        self.assertEqual(paths[:4], [
            "/code/html",                        # OAuth 登录页
            "/code/usersure",                    # 提交账号密码
            "/iframe/index",                     # 取 verify_request
            "/base/c/auth/yiban",                # 完成认证
        ], paths)
        self.assertEqual(paths[4], "/nightAttendance/student/index/signPosition")  # 探针
        self.assertEqual(paths[5], "/nightAttendance/student/index/signPosition")  # 签到前拉任务
        self.assertEqual(paths[6], "/nightAttendance/student/index/signIn")
        # 全程没有 404：形状与假服务端的路由一一对应
        self.assertEqual([r["status"] for r in self.mock.requests() if r["status"] >= 400], [])

    def test_waf_on_usersure_is_loud(self):
        """usersure 被风控拦截 → 响亮失败并给出代理指引，且不再往下打请求。"""
        client = self._client()
        with mock.patch.object(_LocalMockAdapter, "send", _waf_body("/code/usersure")),                 self.assertRaisesRegex(RuntimeError, "请求被 WAF 风控拦截"):
            client.login_killyiban()
        self.assertFalse(client.logged_in)
        # 被拦截的那次请求由补丁直接应答，假服务端只看到第 1 步
        self.assertNotIn("/base/c/auth/yiban", self.mock.wait_paths(1),
                         "被拦截后不得继续完成认证")

    def test_waf_on_login_page_surfaces_as_parse_failure(self):
        """登录页被风控（整页替换）时没有 JSON 可解析，故报"OAuth 页解析失败"。

        这是**有意的口径**：该文案在 `RISK_FAIL_KEYWORDS` 里，重试预算按风控类给
        （不反复重试加重账号标记），诊断日志里同时点明 WAF 特征与代理建议。
        """
        client = self._client()
        with mock.patch.object(_LocalMockAdapter, "send", _waf_body("/code/html")),                 self.assertRaisesRegex(RuntimeError, "OAuth 页解析失败") as ctx:
            client.login_killyiban()
        # 该文案必须仍被判为风控类失败（否则会拿满重试次数继续撞风控）
        self.assertEqual(signin.classify_failure(str(ctx.exception)),
                         signin.RISK_MAX_ATTEMPTS)


class LegacyLoginE2ETest(_E2EBase):
    """旧 iOS 流程（YIBAN_LEGACY_LOGIN=1）：形状与默认流程不同，同样要跑通。"""

    def test_legacy_chain_rehearsal(self):
        client = self._client(legacy=True)
        self.assertFalse(client.use_killyiban)
        client.login()
        self.assertTrue(client.logged_in)
        ok, message = client.verify()
        self.assertTrue(ok, f"探针应成功: {message}")

        paths = self.mock.wait_paths(7)
        self.assertEqual(paths[:6], [
            "/base/c/auth/yiban",                # 取 OAuth 入口 URL
            "/code/html",                        # OAuth 登录页
            "/code/usersure",                    # 提交账号密码（response 带 reUrl）
            "/iapp7463",                         # reUrl 落地
            "/iframe/index",                     # 取 verify_request
            "/base/c/auth/yiban",                # 完成认证
        ], paths)


class InjectedFailureE2ETest(_E2EBase):
    """失败注入：签到最后一步被服务端拒绝 → 业务层必须判失败（而不是抛异常/误判成功）。"""

    def test_signin_failure_from_server(self):
        self.mock.close()
        self.mock = _FakeYiban(fail_rate=1.0, fail_stage="signIn")
        self.addCleanup(self.mock.close)
        client = self._client()
        client.login_killyiban()
        success, message, skip, status = client.signin()
        self.assertFalse(success)
        self.assertFalse(skip)
        self.assertEqual(status, signin.STATUS_FAILED)
        self.assertIn("mock injected signIn failure", message)


if __name__ == "__main__":
    unittest.main(verbosity=2)
