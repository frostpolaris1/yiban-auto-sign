# -*- coding: utf-8 -*-
"""登录与签到**协议形状**的保护存在性断言（第三方层抽取前后都必须绿）。

为什么单独有这一份：`yiban/fyiban/protocol.py` 的抽取会把"五步登录"的**顺序与请求形状**
从 `scripts/signin.py` 搬到隔离层，而**安全校验（WAF 判定、URL 白名单、脱敏、诊断）必须
留在本项目层并以策略注入**。既有测试只覆盖了"多任务任一成功即停""无点位""会话缓存"
这些**业务语义**，没有一条断言请求形状——直接搬代码时删掉一整段 WAF 分支或改掉
`usersure` 的表单字段，现有套件**照样全绿**。故先建这份断言，抽完再核对：

1. 登录五步的 URL / query / 表单字段 / 顺序 / 是否跟随重定向；
2. `usersure` **必须不带 Origin/Referer**（实测带 Origin → e001 无效应用端编号）；
3. 每个响应点上的 **WAF 拦截分支真的会被走到**（不是只在文件里存在）；
4. URL 白名单在协议路径上生效：跳转目标换成非白名单域必须响亮失败；
5. 签到两个接口的形状与三态语义。

请求形状用**真实的 `requests.Session`**（只把 `send` 换成脚本化替身）来断言，而不是
"mock 掉 session 再看调用参数"：`Origin: None` 这类删除头部的手法只有走真实的
`prepare_request`（headers 合并时丢弃 None 值）才能被观察到。
"""
import json
import os
import unittest
from unittest import mock
from urllib.parse import parse_qsl, urlsplit

import requests
import signin
from Crypto.PublicKey import RSA

# 测试用 RSA-1024 公钥（登录页里的 input#key 必须是合法 PEM，签名侧只做公钥加密）。
# 生成一次即可：RSA 生成较慢，且各用例共用不影响隔离性。
_TEST_PUBKEY_PEM = None


def _pubkey_pem():
    global _TEST_PUBKEY_PEM
    if _TEST_PUBKEY_PEM is None:
        _TEST_PUBKEY_PEM = RSA.generate(1024).publickey().export_key().decode("utf-8")
    return _TEST_PUBKEY_PEM


def _resp(json_data=None, *, text="", status=200, headers=None, cookies=None, url=""):
    """构造真实 requests.Response（`.json()` / `.headers` / `.cookies` 都按真实语义）。"""
    r = requests.Response()
    r.status_code = status
    r._content = (json.dumps(json_data) if json_data is not None else text).encode("utf-8")
    r.headers.update(headers or {})
    r.url = url
    if cookies:
        r.cookies = requests.cookies.cookiejar_from_dict(cookies)
    return r


def _patch_send(rec):
    """把脚本化 send 装到 Session 上。

    必须用一个**函数**（lambda）而不是可调用对象：可调用实例不是描述符，
    `session.send(...)` 不会把 self 传进来，`session.cookies` 也就拿不到——
    而登录态正是靠响应 cookie 并入会话 jar 才成立的。
    """
    return mock.patch.object(requests.Session, "send",
                             lambda self, request, **kw: rec(self, request, **kw))


class _Recorder:
    """替换 `Session.send`：记录每个**已构造完成**的请求及其 send 参数。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []  # [(PreparedRequest, send_kwargs)]

    def __call__(self, session, request, **kwargs):
        self.calls.append((request, kwargs))
        if not self.responses:
            raise AssertionError(
                f"请求数超出脚本第 {len(self.calls)} 个: {request.method} {request.url}"
            )
        resp = self.responses.pop(0)
        if resp.cookies:
            # 真实 send 会把响应 Set-Cookie 并入会话 jar——登录态就落在这一步
            session.cookies.update(resp.cookies)
        return resp

    # ---- 断言辅助 ----
    @property
    def urls(self):
        return [c[0].url for c in self.calls]

    def base(self, i):
        return urlsplit(self.calls[i][0].url).netloc

    def path(self, i):
        return urlsplit(self.calls[i][0].url).path

    def query(self, i):
        return dict(parse_qsl(urlsplit(self.calls[i][0].url).query, keep_blank_values=True))

    def form(self, i):
        """表单体解析（body 是 urlencode 后的字符串）。"""
        body = self.calls[i][0].body
        if isinstance(body, bytes):
            body = body.decode("utf-8")
        return dict(parse_qsl(body or "", keep_blank_values=True))

    def header(self, i, name):
        return self.calls[i][0].headers.get(name)

    def redirects(self, i):
        return self.calls[i][1].get("allow_redirects")


_KILLYIBAN_PAGE = (
    "<html><body>"
    '<input type="hidden" id="key" value="%s"/>'
    "<script>var page_use = 'pageuse12345';</script>"
    "</body></html>"
)


def _legacy_page(pubkey):
    # 旧流程的正则：`page_use ?= ?['|"]…` 与 `id="key"\s+value="…"`
    return (
        "<html><body>"
        '<input type="hidden" id="key" value="%s">'
        "<script>page_use = 'pageuse12345';</script>"
        "</body></html>"
    ) % pubkey


def _killyiban_client(responses):
    """构造 KillYiBan 流程的客户端（默认登录方式）并接管 send。"""
    rec = _Recorder(responses)
    acc = signin.Account(phone="13800138000", password="secret")
    with mock.patch.dict(os.environ, {"YIBAN_LEGACY_LOGIN": ""}, clear=False):
        os.environ.pop("YIBAN_PROXY", None)
        with _patch_send(rec):
            client = signin.YibanClient(acc)
    return client, rec


def _legacy_client(responses):
    """构造旧流程客户端（YIBAN_LEGACY_LOGIN=1）并接管 send。"""
    rec = _Recorder(responses)
    acc = signin.Account(phone="13800138000", password="secret")
    with mock.patch.dict(os.environ, {"YIBAN_LEGACY_LOGIN": "1"}, clear=False):
        os.environ.pop("YIBAN_PROXY", None)
        with _patch_send(rec):
            client = signin.YibanClient(acc)
    return client, rec


def _run(recorder, fn):
    with _patch_send(recorder):
        return fn()


# ---------------------------------------------------------------------------
# 旧流程（YIBAN_LEGACY_LOGIN=1）：五步登录
# ---------------------------------------------------------------------------
class LegacyLoginShapeTest(unittest.TestCase):
    def _happy_responses(self, oauth_page_url):
        return [
            # 1. 取跳转 URL
            _resp({"code": 0, "data": {"Data": oauth_page_url}}),
            # 2. OAuth 页面（跟随重定向后落在这里）
            _resp(text=_legacy_page(_pubkey_pem()), url=oauth_page_url),
            # 3. usersure
            _resp({"reUrl": "https://f.yiban.cn/iapp7463?verify_request=TOK"}),
            # 4. reUrl
            _resp(text="", headers={"Location": "https://f.yiban.cn/x"}, url="https://f.yiban.cn/x"),
            # 5. verify_request 跳转
            _resp(text="", headers={"Location": "https://c.uyiban.com/?verify_request=VTOK"},
                  url="https://c.uyiban.com/"),
            # 6. 最终认证
            _resp({"code": 0, "msg": ""}),
        ]

    def test_five_step_request_shapes(self):
        oauth_page_url = "https://oauth.yiban.cn/code/html?client_id=95626fa3080300ea"
        client, rec = _legacy_client(self._happy_responses(oauth_page_url))

        _run(rec, client.login)
        self.assertTrue(client.logged_in)

        # ---- 第 1 步：入口带 CSRF，不跟随重定向 ----
        self.assertEqual(rec.base(0), "api.uyiban.com")
        self.assertEqual(rec.path(0), "/base/c/auth/yiban")
        self.assertEqual(rec.query(0), {"CSRF": client.csrf})
        self.assertIs(rec.redirects(0), False)
        self.assertEqual(rec.header(0, "Origin"), "https://c.uyiban.com")
        self.assertEqual(rec.header(0, "Referer"), "https://c.uyiban.com/")

        # ---- 第 2 步：跟随重定向取 OAuth 页 ----
        self.assertEqual(rec.calls[1][0].url, oauth_page_url)
        self.assertIs(rec.redirects(1), True)

        # ---- 第 3 步：usersure 的表单字段与头部口径 ----
        self.assertEqual(rec.base(2), "oauth.yiban.cn")
        self.assertEqual(rec.path(2), "/code/usersure")
        self.assertEqual(rec.query(2), {"ajax_sign": "pageuse12345"})
        self.assertIs(rec.redirects(2), False)
        form = rec.form(2)
        self.assertEqual(form["oauth_uname"], "13800138000")
        self.assertEqual(form["client_id"], "95626fa3080300ea")
        self.assertEqual(form["redirect_uri"], "https://f.yiban.cn/iapp7463")
        self.assertEqual(form["state"], "")
        self.assertEqual(form["scope"], "1,2,3,4,")  # 旧流程的实值
        self.assertEqual(form["display"], "html")
        self.assertEqual(rec.header(2, "Origin"), "https://oauth.yiban.cn")
        self.assertEqual(rec.header(2, "Referer"), oauth_page_url)
        # 密码必须是 RSA-1024 + PKCS1_v1_5 的 base64（128 字节密文）
        import base64
        self.assertEqual(len(base64.b64decode(form["oauth_upwd"])), 128)

        # ---- 第 4 步：reUrl 不跟随重定向（要读 Location 判 ydclearance）----
        self.assertEqual(rec.path(3), "/iapp7463")
        self.assertIs(rec.redirects(3), False)

        # ---- 第 5 步：跳转到第 4 步响应的 Location，从它的 Location 取 verify_request ----
        self.assertEqual(rec.base(4), "f.yiban.cn")
        self.assertEqual(rec.path(4), "/x")
        self.assertIs(rec.redirects(4), False)

        # ---- 第 6 步：最终认证带 verifyRequest + CSRF ----
        self.assertEqual(rec.base(5), "api.uyiban.com")
        self.assertEqual(rec.path(5), "/base/c/auth/yiban")
        self.assertEqual(rec.query(5), {"verifyRequest": "VTOK", "CSRF": client.csrf})
        self.assertIs(rec.redirects(5), False)

    def test_waf_branch_is_live_on_every_response(self):
        """WAF 分支必须真的会被走到（4 个响应点各验一次），不是"文件里有这段"。"""
        for idx, label in ((0, "入口"), (1, "OAuth 页"), (2, "usersure"), (5, "最终认证")):
            with self.subTest(step=label):
                resp = self._happy_responses("https://oauth.yiban.cn/code/html")
                resp[idx] = _resp(text="您的访问存在风险访问，已被拦截")
                client, rec = _legacy_client(resp)
                with self.assertRaisesRegex(RuntimeError, "请求被 WAF 风控拦截"):
                    _run(rec, client.login)

    def test_oauth_url_not_whitelisted_is_rejected(self):
        client, rec = _legacy_client(
            [_resp({"code": 0, "data": {"Data": "https://evil.example.com/login"}})])
        with self.assertRaisesRegex(RuntimeError, "登录入口 URL 不在白名单"):
            _run(rec, client.login)
        self.assertEqual(len(rec.calls), 1, "非白名单目标不得被请求")

    def test_reurl_not_whitelisted_is_rejected(self):
        resp = self._happy_responses("https://oauth.yiban.cn/code/html")
        resp[2] = _resp({"reUrl": "https://evil.example.com/steal"})
        client, rec = _legacy_client(resp)
        with self.assertRaisesRegex(RuntimeError, "登录 reUrl 不在白名单"):
            _run(rec, client.login)
        self.assertEqual(len(rec.calls), 3, "非白名单 reUrl 不得被请求")

    def test_verify_request_location_not_whitelisted_is_rejected(self):
        resp = self._happy_responses("https://oauth.yiban.cn/code/html")
        resp[3] = _resp(text="", headers={"Location": "https://evil.example.com/x"})
        client, rec = _legacy_client(resp)
        with self.assertRaisesRegex(RuntimeError, "verify_request 跳转不在白名单"):
            _run(rec, client.login)


# ---------------------------------------------------------------------------
# KillYiBan 流程（默认）：四步
# ---------------------------------------------------------------------------
class KillyibanLoginShapeTest(unittest.TestCase):
    def _happy_responses(self):
        return [
            _resp(text=_KILLYIBAN_PAGE % _pubkey_pem(),
                  url="https://oauth.yiban.cn/code/html"),
            _resp({"code": "s200", "msgCN": ""}),
            _resp(text="", status=302,
                  headers={"Location": "https://api.uyiban.com/base/c/auth/yiban"
                                      "?verify_request=VTOK&CSRF=x"}),
            _resp({"code": 0, "msg": ""}),
        ]

    def test_four_step_request_shapes(self):
        client, rec = _killyiban_client(self._happy_responses())
        _run(rec, client.login_killyiban)
        self.assertTrue(client.logged_in)

        # 第 1 步：直接打 OAuth 页，带 client_id / redirect_uri，不跟随重定向
        self.assertEqual(rec.path(0), "/code/html")
        self.assertEqual(rec.query(0), {"client_id": "95626fa3080300ea",
                                        "redirect_uri": "https://f.yiban.cn/iapp7463"})
        self.assertIs(rec.redirects(0), False)

        # 第 2 步：usersure —— 表单字段与 App 实值一致
        self.assertEqual(rec.path(1), "/code/usersure")
        self.assertEqual(rec.query(1), {"ajax_sign": "pageuse12345"})
        form = rec.form(1)
        self.assertEqual(form["oauth_uname"], "13800138000")
        self.assertEqual(form["scope"], "")           # App 实值为空
        self.assertEqual(form["display"], "authorize")
        self.assertEqual(form["state"], "")
        self.assertEqual(form["client_id"], "95626fa3080300ea")
        self.assertEqual(form["redirect_uri"], "https://f.yiban.cn/iapp7463")
        # 关键：usersure **不得**带 Origin/Referer（实测带 Origin → e001）
        self.assertIsNone(rec.header(1, "Origin"))
        self.assertIsNone(rec.header(1, "Referer"))
        self.assertIsNone(rec.header(1, "X-Requested-With"))
        # 但 App 指纹必须保留
        self.assertEqual(rec.header(1, "User-Agent"), "Yiban")
        self.assertEqual(rec.header(1, "AppVersion"), signin.YIBAN_APP_VERSION)

        # 第 3 步：iframe/index 取 verify_request（不跟随重定向）
        self.assertEqual(rec.base(2), "f.yiban.cn")
        self.assertEqual(rec.path(2), "/iframe/index")
        self.assertEqual(rec.query(2), {"act": "iapp7463"})
        self.assertIs(rec.redirects(2), False)

        # 第 4 步：完成认证（跟随重定向）
        self.assertEqual(rec.base(3), "api.uyiban.com")
        self.assertEqual(rec.query(3), {"verifyRequest": "VTOK", "CSRF": client.csrf})
        self.assertIs(rec.redirects(3), True)

    def test_verify_request_regex_tolerates_token_at_query_end(self):
        """令牌放在 query 末位（后面没有 `&`）也必须能提取——曾因此全站登录失败。"""
        client, rec = _killyiban_client([
            _resp(text=_KILLYIBAN_PAGE % _pubkey_pem()),
            _resp({"code": "s200"}),
            _resp(text="", status=302,
                  headers={"Location": "https://f.yiban.cn/iapp7463?verify_request=TAILTOKEN"}),
            _resp({"code": 0}),
        ])
        _run(rec, client.login_killyiban)
        self.assertEqual(rec.query(3)["verifyRequest"], "TAILTOKEN")

    def test_s200_is_the_success_marker(self):
        """成功标志是 code == "s200"（App 判定方式），其他值一律当失败并回报 msgCN。"""
        client, rec = _killyiban_client([
            _resp(text=_KILLYIBAN_PAGE % _pubkey_pem()),
            _resp({"code": "e001", "msgCN": "无效的应用端编号"}),
        ])
        with self.assertRaisesRegex(RuntimeError, "无效的应用端编号"):
            _run(rec, client.login_killyiban)
        self.assertFalse(client.logged_in)

    def test_waf_branch_is_live_on_usersure_and_final_auth(self):
        for idx, label in ((1, "usersure"), (3, "最终认证")):
            with self.subTest(step=label):
                resp = self._happy_responses()
                resp[idx] = _resp(text="访问服务禁用", status=403)
                client, rec = _killyiban_client(resp)
                with self.assertRaisesRegex(RuntimeError, "请求被 WAF 风控拦截"):
                    _run(rec, client.login_killyiban)

    def test_session_cache_hit_skips_usersure(self):
        """缓存会话仍有效（探针 302 到 iapp7463）→ 免登录：**不得**提交账号密码。"""
        client, rec = _killyiban_client(
            [_resp(text="", status=302,
                   headers={"Location": "https://f.yiban.cn/iapp7463?x=1"})])
        with mock.patch.object(signin.db, "is_initialized", return_value=True), \
                mock.patch.object(signin.db, "get_session_cache",
                                  return_value={"cookies": json.dumps({"csrf_token": "c"}),
                                                "csrf": "cached-csrf"}):
            _run(rec, client.login_killyiban)
        self.assertEqual(len(rec.calls), 1, "命中缓存只做一次探针")
        self.assertEqual(client.csrf, "cached-csrf")
        self.assertTrue(client.logged_in)

    def test_stale_cache_is_cleared_and_full_login_runs(self):
        """探针返回登录页（缓存已失效）→ 清缓存并走完整流程。"""
        # 探针返回的是登录页（200）→ 缓存已失效，必须走完整流程
        client, rec = _killyiban_client(self._happy_responses())
        with mock.patch.object(signin.db, "is_initialized", return_value=True), \
                mock.patch.object(signin.db, "get_session_cache",
                                  return_value={"cookies": "{}", "csrf": "stale"}), \
                mock.patch.object(signin.db, "clear_session_cache") as cleared, \
                mock.patch.object(signin.db, "set_session_cache"):
            _run(rec, client.login_killyiban)
        cleared.assert_called_once_with("13800138000")
        self.assertEqual(rec.path(1), "/code/usersure", "缓存失效后必须重新提交登录")

    def test_logged_in_marker_requires_fyiban_host_and_path(self):
        """M7：302 落在非 f.yiban.cn 的 /iapp7463 不得判"已登录"。

        原判定是子串 `in`：`https://evil.example/iapp7463`、
        `https://f.yiban.cn.evil.com/x?iapp7463` 都会命中——在未认证会话上置
        `logged_in=True`，把"登录失败"退化成一个通用失败（可诊断信号丢失）。
        收严后只认 host == f.yiban.cn 且 path == /iapp7463（query 允许，
        见 test_session_cache_hit_skips_usersure 的 `?x=1`）。
        """
        client, rec = _killyiban_client(
            [_resp(text=_KILLYIBAN_PAGE % _pubkey_pem(), status=302,
                   headers={"Location": "https://evil.example/iapp7463"}),
             _resp({"code": "s200", "msgCN": ""}),
             _resp(text="", status=302,
                   headers={"Location": "https://api.uyiban.com/base/c/auth/yiban"
                                       "?verify_request=VTOK&CSRF=x"}),
             _resp({"code": 0, "msg": ""}),
             ])
        with mock.patch.object(signin.db, "is_initialized", return_value=True), \
                mock.patch.object(signin.db, "get_session_cache",
                                  return_value={"cookies": "{}", "csrf": "stale"}), \
                mock.patch.object(signin.db, "clear_session_cache"), \
                mock.patch.object(signin.db, "set_session_cache"):
            _run(rec, client.login_killyiban)
        self.assertEqual(rec.path(1), "/code/usersure",
                         "非易班主机的 /iapp7463 不得被当成已登录")

    def test_logged_in_marker_rejects_subdomain_spoof(self):
        """子域伪装 f.yiban.cn.evil.com 带 query 里的 iapp7463 不得命中。"""
        client, rec = _killyiban_client(
            [_resp(text=_KILLYIBAN_PAGE % _pubkey_pem(), status=302,
                   headers={"Location": "https://f.yiban.cn.evil.com/x?iapp7463"}),
             _resp({"code": "s200", "msgCN": ""}),
             _resp(text="", status=302,
                   headers={"Location": "https://api.uyiban.com/base/c/auth/yiban"
                                       "?verify_request=VTOK&CSRF=x"}),
             _resp({"code": 0, "msg": ""}),
             ])
        with mock.patch.object(signin.db, "is_initialized", return_value=True), \
                mock.patch.object(signin.db, "get_session_cache",
                                  return_value={"cookies": "{}", "csrf": "stale"}), \
                mock.patch.object(signin.db, "clear_session_cache"), \
                mock.patch.object(signin.db, "set_session_cache"):
            _run(rec, client.login_killyiban)
        self.assertEqual(rec.path(1), "/code/usersure",
                         "子域伪装不得被当成已登录")


# ---------------------------------------------------------------------------
# 签到两个接口的形状与三态
# ---------------------------------------------------------------------------
def _sign_position_data(*, msg="", positions=None, rng=None):
    now = int(signin.datetime.now().timestamp())
    return {
        "code": 0,
        "data": {
            "Msg": msg,
            "Position": positions if positions is not None else [{
                "Name": "任务A",
                "Points": ["118.0,31.0", "118.1,31.0", "118.1,31.1", "118.0,31.1"],
                "Address": "点A",
            }],
            "Range": rng if rng is not None else {"StartTime": now - 3600, "EndTime": now + 3600},
        },
    }


class SigninShapeTest(unittest.TestCase):
    def _client(self, responses, *, killyiban=True):
        rec = _Recorder(responses)
        client = signin.YibanClient.__new__(signin.YibanClient)
        client.account = signin.Account(phone="13800138000", password="secret")
        client.logged_in = True
        client.use_killyiban = killyiban
        client.csrf = "csrf-token"
        client.phone_model = "Vivo-Test"
        client.phone_code = "C" * 64
        client.session = requests.Session()
        client.session.headers = dict(signin.KILLYIBAN_HEADERS if killyiban
                                      else signin.HEADERS)
        return client, rec

    def _run_sign(self, client, rec):
        with _patch_send(rec), \
                mock.patch.object(signin.random, "shuffle", side_effect=lambda lst: None):
            return client.signin()

    def test_sign_position_and_sign_in_request_shapes(self):
        client, rec = self._client([_resp(_sign_position_data()),
                                    _resp({"code": 0, "data": {"Id": "1"}})])
        ok, msg, skip, status = self._run_sign(client, rec)
        self.assertTrue(ok, msg)
        self.assertFalse(skip)
        self.assertEqual(status, signin.STATUS_SUCCESS)

        self.assertEqual(rec.base(0), "api.uyiban.com")
        self.assertEqual(rec.path(0), "/nightAttendance/student/index/signPosition")
        self.assertEqual(rec.query(0), {"CSRF": "csrf-token"})
        self.assertIs(rec.redirects(0), False)

        self.assertEqual(rec.path(1), "/nightAttendance/student/index/signIn")
        self.assertEqual(rec.query(1), {"CSRF": "csrf-token"})
        form = rec.form(1)
        self.assertEqual(form["Code"], "C" * 64)
        self.assertEqual(form["PhoneModel"], "Vivo-Test")
        self.assertEqual(form["OutState"], "1")  # KillYiBan 用 "1"，旧脚本用 "1.0"
        info = json.loads(form["SignInfo"])
        self.assertEqual(set(info), {"Reason", "AttachmentFileName", "LngLat", "Address"})
        self.assertEqual(info["Address"], "点A")
        lng, lat = (float(x) for x in info["LngLat"].split(","))
        self.assertTrue(118.0 <= lng <= 118.1 and 31.0 <= lat <= 31.1,
                        f"定位点必须落在签到范围内: {info['LngLat']}")

    def test_legacy_flow_uses_app_out_state_and_headers(self):
        client, rec = self._client([_resp(_sign_position_data()),
                                    _resp({"code": 0, "data": {"Id": "1"}})],
                                   killyiban=False)
        self._run_sign(client, rec)
        self.assertEqual(rec.form(1)["OutState"], "1.0")
        self.assertEqual(rec.header(0, "Origin"), "https://app.uyiban.com")

    def test_three_states_and_window_skips(self):
        cases = [
            (_sign_position_data(msg="今日已签到"), signin.STATUS_ALREADY, "已签到"),
            (_sign_position_data(msg="今日无需签到"), signin.STATUS_NO_TASK, "无需签到"),
            (_sign_position_data(positions=[]), signin.STATUS_NO_POSITION, "未找到签到位置数据"),
            (_sign_position_data(rng={}), signin.STATUS_SKIPPED_NORANGE, "时间窗口缺失"),
            (_sign_position_data(rng={"StartTime": 1, "EndTime": 2}),
             signin.STATUS_SKIPPED_WINDOW, "未在签到时间内"),
        ]
        for payload, want_status, want_in_msg in cases:
            with self.subTest(status=want_status):
                client, rec = self._client([_resp(payload)])
                _ok, msg, skip, status = self._run_sign(client, rec)
                self.assertEqual(status, want_status)
                self.assertIn(want_in_msg, msg)
                self.assertEqual(skip, want_status in (signin.STATUS_SKIPPED_NORANGE,
                                                       signin.STATUS_SKIPPED_WINDOW))
                # 三态/窗口判定都不允许提交签到
                self.assertTrue(all(c[0].method == "GET" for c in rec.calls),
                                "判定为跳过/已签时不得提交 signIn")

    def test_waf_branch_is_live_on_signin_path(self):
        client, rec = self._client([_resp(text="您的访问存在风险访问，已被拦截")])
        ok, msg, skip, status = self._run_sign(client, rec)
        self.assertFalse(ok)
        self.assertFalse(skip)
        self.assertEqual(status, signin.STATUS_FAILED)
        self.assertIn("WAF", msg)


# ---------------------------------------------------------------------------
# 策略函数自身的边界（安全策略属本项目层，抽取后仍须在协议路径上生效）
# ---------------------------------------------------------------------------
class UrlWhitelistBoundaryTest(unittest.TestCase):
    def test_trusted_yiban_url_boundaries(self):
        ok = ["https://yiban.cn/x", "https://api.uyiban.com/base/c/auth/yiban",
              "https://oauth.yiban.cn/code/html", "https://f.yiban.cn/iapp7463"]
        bad = ["http://api.uyiban.com/x",                    # 非 https
               "https://yiban.cn.evil.com/x",                # 前缀伪装
               "https://yiban.cn@evil.com/x",                # userinfo 绕过
               "https://evil.com/?u=https://yiban.cn"]       # 只出现在 query
        for url in ok:
            with self.subTest(url=url):
                self.assertTrue(signin._is_yiban_trusted_url(url))
        for url in bad:
            with self.subTest(url=url):
                self.assertFalse(signin._is_yiban_trusted_url(url))

    def test_strict_fyiban_url_boundaries(self):
        self.assertTrue(signin._is_fyiban_url("https://f.yiban.cn/iapp7463"))
        for url in ("https://f.yiban.cn.evil.com/iapp7463",
                    "https://f.yiban.cn@evil.com/iapp7463",
                    "http://f.yiban.cn/iapp7463",
                    "https://c.uyiban.com/iapp7463"):
            with self.subTest(url=url):
                self.assertFalse(signin._is_fyiban_url(url))

    def test_redir_chain_requires_every_hop_trusted(self):
        """M8：跟随重定向后每一跳（含落点）都必须在白名单内。"""
        from yiban.security import ProtocolPolicy
        policy = ProtocolPolicy()

        def _resp(url, history=()):
            r = requests.Response()
            r.url = url
            r.history = list(history)
            return r

        # 白名单内单跳：放行
        ok = _resp("https://oauth.yiban.cn/code/html")
        policy.require_redir_chain_trusted(ok, "login_entry")

        # 中间某一跳到白名单外：拒绝
        chain = _resp("https://oauth.yiban.cn/code/html",
                      history=[_resp("https://evil.example.com/steal")])
        with self.assertRaisesRegex(RuntimeError, "登录入口 URL 不在白名单"):
            policy.require_redir_chain_trusted(chain, "login_entry")

        # 落点本身在白名单外：拒绝
        landing = _resp("https://evil.example.com/steal",
                        history=[_resp("https://oauth.yiban.cn/code/html")])
        with self.assertRaisesRegex(RuntimeError, "登录入口 URL 不在白名单"):
            policy.require_redir_chain_trusted(landing, "login_entry")

    def test_waf_detection_is_length_bounded_and_decodes_escapes(self):
        """长页面（正常协议文本）不算拦截；Unicode 转义的风控文案要能识别。"""
        long_text = "风险访问" + "正文" * 2000
        self.assertFalse(signin.is_waf_blocked(long_text))
        self.assertTrue(signin.is_waf_blocked("\\u98ce\\u9669\\u8bbf\\u95ee"))  # 风险访问
        self.assertTrue(signin.is_waf_blocked("访问服务禁用"))
        self.assertFalse(signin.is_waf_blocked('{"code":0,"msg":""}'))


if __name__ == "__main__":
    unittest.main(verbosity=2)
