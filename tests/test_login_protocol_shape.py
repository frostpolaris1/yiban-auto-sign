# -*- coding: utf-8 -*-
"""登录与签到**协议形状**的保护存在性断言（第三方层抽取前后都必须绿）。

标签：K · 登录协议与第三方隔离
覆盖：旧流登录五步与默认流四步的 URL/query/表单字段/顺序/是否跟随重定向、usersure
   不带 Origin 与 Referer、每个响应点上的 WAF
   分支真的被走到、跳转目标换成非白名单域必须响亮失败、reUrl 为 null 不抛裸
   TypeError、签到两接口形状与三态语义、会话缓存命中与失效两分支（真临时库读写，
   不 mock 缓存写）、默认流假成功拒绝（code==0 无签发回执 ⇒ 不落"登录成功"日志、
   零缓存写入、落硬失败不可重试档）与真成功（回执齐全）写缓存回归、已登录标志的主机与路径判定（含子域伪装）、URL
   白名单边界与逐跳校验、风控形态判定的长度边界（挑战形态不受限、仅关键词维持上界）与转义解码、
   WAF 词元的非字母数字边界口径（base64 里的 `aWAFb` 不得判拦）、测试公钥夹具的跨进程确定性。
对应实现：yiban/platform.py（旧流与默认流的登录编排、usersure、已登录标志判定）、yiban/security.py（is_yiban_trusted_url、is_strict_yiban_url、WAF
   文案识别）、scripts/signin.py 的签到接口。
关键断言：这份断言的存在理由是「抽完再核对」：直接搬代码时删掉一整段 WAF 分支或改掉
   usersure 的表单字段，业务语义用例照样全绿，所以必须先钉形状。WAF
   分支要按「真的会被走到」来验（每个响应点各注入一次），文件里存在这段代码不算。usersure
   必须不带 Origin（实测带上得 e001）。令牌放在 query 末位（后面没有
   &）也必须提取——曾因此全站登录失败。已登录标志必须同时认主机与路径，子域伪装
   f.yiban.cn.evil.com 不得命中。断言走真实 requests.Session（只换
   send），Origin 置 None 这类删头手法只有真实 prepare_request 才观察得到。
依赖：真实 requests.Session + 脚本化 send 替身、`tests/fake_yiban_server.py` 里固定的
   1024 位测试公钥（不现场生成，跨进程同值）、会话缓存用例走真 db 门面 + 临时库/临时 .env（测试密钥）；
   不发任何真实网络请求。整文件在本机执行，无 skip。

1. 旧流登录 6 次请求的 URL / query / 表单字段 / 顺序 / 是否跟随重定向；
2. `usersure` **必须不带 Origin/Referer**（实测带 Origin → e001 无效应用端编号）；
3. 每个响应点上的 **WAF 拦截分支真的会被走到**（不是只在文件里存在）；
4. URL 白名单在协议路径上生效：跳转目标换成非白名单域必须响亮失败；
5. 签到两个接口的形状与三态语义。

请求形状用**真实的 `requests.Session`**（只把 `send` 换成脚本化替身）来断言，而不是
"mock 掉 session 再看调用参数"：`Origin: None` 这类删除头部的手法只有走真实的
`prepare_request`（headers 合并时丢弃 None 值）才能被观察到。
"""
import contextlib
import io
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from urllib.parse import parse_qsl, urlsplit

import requests
import signin
from fake_yiban_server import DEFAULT_PUBKEY_PEM, waf_challenge_body

from yiban import security

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 测试用 RSA-1024 公钥（登录页里的 input#key 必须是合法 PEM，签名侧只做公钥加密）。
#
# 夹具必须是**仓内固定字节**，不靠现场生成：随机 PEM 的 base64 正文能撞出 ASCII 词元
# `WAF`，抽中它的那个 xdist worker 里所有嵌这枚 PEM 的用例全被判成"被风控拦截"
# （工单 `yiban-auto-sign-u21x`）。名册只有一处定义——`tests/fake_yiban_server.py` 的
# `DEFAULT_PUBKEY_PEM`，本文件与 `tests/test_protocol_masking.py` 共用它。
def _pubkey_pem():
    """测试公钥 PEM（固定常量：同进程重复调用同值、跨进程同值）。"""
    return DEFAULT_PUBKEY_PEM


def _resp(json_data=None, *, text="", status=200, headers=None, cookies=None, url=""):
    """构造真实 requests.Response（`.json()` / `.headers` / `.cookies` 都按真实语义）。"""
    r = requests.Response()
    r.status_code = status
    # 填 _content 而不是包装 json：让真实的 .json() 去解析，形状错在这里就露出来
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
        self.calls.append((request, kwargs)) # kwargs 也得记：allow_redirects 是 send 的参数，不在请求对象上
        if not self.responses:
            raise AssertionError(
                f"请求数超出脚本第 {len(self.calls)} 个: {request.method} {request.url}"
            )
        resp = self.responses.pop(0) # 脚本逐个消耗：多发出一请求就报错，这正是「形状」断言的一部分
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
        # keep_blank_values：空值参数也是协议形状，省掉就测不出「少了个键」
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
    # 旧流程与 KillYiBan 共用库 `parse_authorize_page` 的页面契约：
    # `var page_use = '…'` + `id="key"` 的 PEM 公钥输入。
    return (
        "<html><body>"
        '<input type="hidden" id="key" value="%s">'
        "<script>var page_use = 'pageuse12345';</script>"
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


def _killyiban_happy_responses():
    """默认流四步的成功脚本。最终认证载荷带**签发回执**（`data` 存在，可为空容器）：
    形状对照 `tests/fake_yiban_server.py` 录制的 `/base/c/auth/yiban` 成功应答。"""
    return [
        _resp(text=_KILLYIBAN_PAGE % _pubkey_pem(),
              url="https://oauth.yiban.cn/code/html"),
        _resp({"code": "s200", "msgCN": ""}),
        _resp(text="", status=302,
              headers={"Location": "https://api.uyiban.com/base/c/auth/yiban"
                                  "?verify_request=VTOK&CSRF=x"}),
        _resp({"code": 0, "data": {}, "msg": ""}),
    ]


@contextlib.contextmanager
def _capture_logs(name):
    """挂临时 StreamHandler 抓指定 logger 的 INFO 及以上输出（断"日志出没出现"用）。"""
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger = logging.getLogger(name)
    old_level = logger.level
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    try:
        yield buf
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)


# ---------------------------------------------------------------------------
# 旧流程（YIBAN_LEGACY_LOGIN=1）：实测 6 次请求（命中挑战 7 次）
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
        self.assertEqual(form["scope"], "1,2,3,4,")  # 旧流程的实值
        self.assertEqual(form["display"], "html")
        # 换核后表单由库构造：字段集为 oauth_uname/oauth_upwd/client_id/redirect_uri/
        # display(+scope)，不再含旧的 `state` 空字段（与 TASK-C §2[2] 实拍一致）
        self.assertNotIn("state", form)
        self.assertEqual(rec.header(2, "Origin"), "https://oauth.yiban.cn")
        self.assertEqual(rec.header(2, "Referer"), oauth_page_url)
        # usersure 的表单体同样是预编码字符串（requests 不补头）：旧流程的头来自
        # 会话（`HEADERS`），删掉会话里的 Content-Type 即上游读不到表单。
        self.assertEqual(rec.header(2, "Content-Type"),
                         "application/x-www-form-urlencoded; charset=UTF-8")
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

    def test_reurl_null_fails_loudly_without_type_error(self):
        """N5/N6：reUrl 为 null 时不得 `in None` 抛裸 TypeError；白名单校验与请求同源。"""
        resp = self._happy_responses("https://oauth.yiban.cn/code/html")
        resp[2] = _resp({"reUrl": None})
        client, rec = _legacy_client(resp)
        with self.assertRaisesRegex(RuntimeError, "登录 reUrl 不在白名单"):
            _run(rec, client.login)
        self.assertEqual(len(rec.calls), 3, "空 reUrl 不得被请求")

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
        return _killyiban_happy_responses()

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
        self.assertNotIn("state", form)               # 库构造表单不含旧 `state` 空字段
        self.assertEqual(form["client_id"], "95626fa3080300ea")
        self.assertEqual(form["redirect_uri"], "https://f.yiban.cn/iapp7463")
        # 关键：usersure **不得**带 Origin/Referer（实测带 Origin → e001）
        self.assertIsNone(rec.header(1, "Origin"))
        self.assertIsNone(rec.header(1, "Referer"))
        self.assertIsNone(rec.header(1, "X-Requested-With"))
        # 但 App 指纹必须保留
        self.assertEqual(rec.header(1, "User-Agent"), "Yiban")
        self.assertEqual(rec.header(1, "AppVersion"), signin.YIBAN_APP_VERSION)
        # usersure 的表单体是预编码字符串，requests **不补** Content-Type：这两处
        # 请求级显式头是唯一的来源，删掉即上游读不到表单（工单 3kmt 的同族缺口）。
        # 与 signIn 两流程合起来，构成「两流程 × 两端点」的表单头完整矩阵。
        self.assertEqual(rec.header(1, "Content-Type"),
                         "application/x-www-form-urlencoded; charset=UTF-8")

        # 第 3 步：iframe/index 取 verify_request（不跟随重定向）
        self.assertEqual(rec.base(2), "f.yiban.cn")
        self.assertEqual(rec.path(2), "/iframe/index")
        self.assertEqual(rec.query(2), {"act": "iapp7463"})
        self.assertIs(rec.redirects(2), False)

        # 第 4 步：完成认证——手动逐跳校验白名单后再跟跳，不让 requests 自动跟随
        # （自动跟随会在校验前把域名为空的 csrf_token cookie 发往任意 302 落点）
        self.assertEqual(rec.base(3), "api.uyiban.com")
        self.assertEqual(rec.query(3), {"verifyRequest": "VTOK", "CSRF": client.csrf})
        self.assertIs(rec.redirects(3), False)

    def test_verify_request_regex_tolerates_token_at_query_end(self):
        """令牌放在 query 末位（后面没有 `&`）也必须能提取——曾因此全站登录失败。"""
        client, rec = _killyiban_client([
            _resp(text=_KILLYIBAN_PAGE % _pubkey_pem()),
            _resp({"code": "s200"}),
            _resp(text="", status=302,
                  headers={"Location": "https://f.yiban.cn/iapp7463?verify_request=TAILTOKEN"}),
            _resp({"code": 0, "data": {}}),
        ])
        _run(rec, client.login_killyiban)
        self.assertEqual(rec.query(3)["verifyRequest"], "TAILTOKEN")

    def test_final_auth_redirect_outside_whitelist_is_rejected(self):
        """N1：最终认证那一跳是默认流唯一会跟随的重定向——302 到白名单外必须在
        发出请求**之前**响亮失败（会话 jar 里 csrf_token 域名为空，对任意主机都会带出）。"""
        resp = self._happy_responses()
        resp[3] = _resp(text="", status=302,
                        headers={"Location": "https://evil.example.com/steal"},
                        url="https://api.uyiban.com/base/c/auth/yiban")
        client, rec = _killyiban_client(resp)
        with self.assertRaisesRegex(RuntimeError, "最终认证跳转不在白名单"):
            _run(rec, client.login_killyiban)
        self.assertEqual(len(rec.calls), 4, "非白名单落点不得被请求")

    def test_final_auth_redirect_inside_whitelist_is_followed_manually(self):
        """白名单内 302（落点才是 JSON）：手动跟跳取回落点响应，登录照常成功。"""
        resp = self._happy_responses()
        resp[3] = _resp(text="", status=302,
                        headers={"Location": "https://api.uyiban.com/base/c/auth/yiban/done"},
                        url="https://api.uyiban.com/base/c/auth/yiban")
        resp.insert(4, _resp({"code": 0, "data": {}, "msg": ""}))
        client, rec = _killyiban_client(resp)
        _run(rec, client.login_killyiban)
        self.assertTrue(client.logged_in)
        self.assertEqual(rec.path(4), "/base/c/auth/yiban/done")
        self.assertIs(rec.redirects(4), False)

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

_TEST_ACCOUNTS_KEY = "a" * 64  # 测试专用密钥，与生产无任何关系
_TEST_AUDIT_KEY = "b" * 64


class _RealSessionStoreFixture(unittest.TestCase):
    """临时库 + 临时 `.env` 的真实会话缓存底座：读、写、清都走真 db 门面。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-login-shape-")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with io.open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={_TEST_ACCOUNTS_KEY}\n"
                    f"YIBAN_AUDIT_KEY={_TEST_AUDIT_KEY}\n")
        os.environ["YIBAN_ACCOUNTS_KEY"] = _TEST_ACCOUNTS_KEY
        os.environ["YIBAN_AUDIT_KEY"] = _TEST_AUDIT_KEY
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_DB_FILE"] = cls.db_file

    @classmethod
    def tearDownClass(cls):
        db = signin.db
        if getattr(db, "_conn", None) is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        for key in ("YIBAN_ACCOUNTS_KEY", "YIBAN_AUDIT_KEY", "YIBAN_ENV_FILE",
                    "YIBAN_DB_FILE", "YIBAN_SESSION_TTL_HOURS"):
            os.environ.pop(key, None)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        db = signin.db
        # 逐用例重建空库：上一用例留下的连接/行不得串台（与 test_session.py 同法）
        if getattr(db, "_conn", None) is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        for suffix in ("", "-wal", "-shm"):
            p = self.db_file + suffix
            if os.path.exists(p):
                os.remove(p)
        os.environ.pop("YIBAN_SESSION_TTL_HOURS", None)
        db.init_db(self.db_file, env_file=self.env_file)

    def _cache_rows(self):
        return signin.db.get_conn().execute(
            "SELECT COUNT(*) FROM session_cache").fetchone()[0]


class KillyibanSessionStoreTest(_RealSessionStoreFixture):
    """默认流登录 × 会话缓存的**真读真写**：命中复用、失效重建、假成功零写入。

    这里不得把 `set_session_cache` 整体 mock 掉——"假成功是否落密文库"正是本类要断的
    事，mock 写入闸门等于对缺口重新闭眼。
    """

    PHONE = "13800138000"  # 与 _killyiban_client 的账号一致

    def test_session_cache_hit_skips_usersure(self):
        """缓存会话仍有效（探针 302 到 iapp7463）→ 免登录：**不得**提交账号密码。"""
        client, rec = _killyiban_client(
            [_resp(text="", status=302,
                   headers={"Location": "https://f.yiban.cn/iapp7463?x=1"})])
        signin.db.set_session_cache(self.PHONE, json.dumps({"csrf_token": "c"}), "cached-csrf")
        _run(rec, client.login_killyiban)
        self.assertEqual(len(rec.calls), 1, "命中缓存只做一次探针")
        self.assertEqual(client.csrf, "cached-csrf")
        self.assertTrue(client.logged_in)
        # 复用路径没有新的签发事件，不得重写缓存行
        self.assertEqual(signin.db.get_session_cache(self.PHONE)["csrf"], "cached-csrf")

    def test_stale_cache_is_cleared_and_full_login_runs(self):
        """探针返回登录页（缓存已失效）→ 清缓存走完整流程，新会话**覆盖式**重建。"""
        client, rec = _killyiban_client(_killyiban_happy_responses())
        signin.db.set_session_cache(self.PHONE, "{}", "stale-csrf")
        _run(rec, client.login_killyiban)
        self.assertEqual(rec.path(1), "/code/usersure", "缓存失效后必须重新提交登录")
        row = signin.db.get_session_cache(self.PHONE)
        self.assertIsNotNone(row, "回执齐全的完整登录应重建缓存")
        self.assertEqual(row["csrf"], client.csrf)
        # 种子行 cookies 为空 dict；重建后的行必须带真实会话 cookie（证明是覆盖写，
        # 而不是清完没写/压根没清）
        self.assertTrue(json.loads(row["cookies"]), "重建的缓存行必须带会话 cookies")

    def test_logged_in_marker_requires_app_host_and_path(self):
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
             _resp({"code": 0, "data": {}, "msg": ""}),
             ])
        signin.db.set_session_cache(self.PHONE, "{}", "stale-csrf")
        _run(rec, client.login_killyiban)
        self.assertEqual(rec.path(1), "/code/usersure",
                         "非易班主机的 /iapp7463 不得被当成已登录")
        row = signin.db.get_session_cache(self.PHONE)
        self.assertEqual(row["csrf"], client.csrf,
                         "失效缓存被清后由真实登录重建（不留 stale 行）")
        self.assertTrue(json.loads(row["cookies"]), "重建的缓存行必须带会话 cookies")

    def test_logged_in_marker_rejects_subdomain_spoof(self):
        """子域伪装 f.yiban.cn.evil.com 带 query 里的 iapp7463 不得命中。"""
        client, rec = _killyiban_client(
            [_resp(text=_KILLYIBAN_PAGE % _pubkey_pem(), status=302,
                   headers={"Location": "https://f.yiban.cn.evil.com/x?iapp7463"}),
             _resp({"code": "s200", "msgCN": ""}),
             _resp(text="", status=302,
                   headers={"Location": "https://api.uyiban.com/base/c/auth/yiban"
                                       "?verify_request=VTOK&CSRF=x"}),
             _resp({"code": 0, "data": {}, "msg": ""}),
             ])
        signin.db.set_session_cache(self.PHONE, "{}", "stale-csrf")
        _run(rec, client.login_killyiban)
        self.assertEqual(rec.path(1), "/code/usersure",
                         "子域伪装不得被当成已登录")
        row = signin.db.get_session_cache(self.PHONE)
        self.assertEqual(row["csrf"], client.csrf,
                         "子域伪装路径同样不得保留 stale 缓存行")
        self.assertTrue(json.loads(row["cookies"]), "重建的缓存行必须带会话 cookies")

    def _assert_receiptless_rejected(self, final_body):
        resp = _killyiban_happy_responses()
        resp[-1] = _resp(final_body)
        client, rec = _killyiban_client(resp)
        with _capture_logs("yiban.platform") as buf, \
                self.assertRaises(RuntimeError) as ctx:
            _run(rec, client.login_killyiban)
        msg = str(ctx.exception)
        self.assertIn("无签发方回执", msg)
        self.assertNotIn("登录成功", buf.getvalue(),
                         "假成功不得落「登录成功」日志（审计可信是登记的直接后果）")
        self.assertFalse(client.logged_in)
        self.assertEqual(self._cache_rows(), 0, "假成功不得写会话缓存密文库")
        self.assertIsNone(signin.db.get_session_cache(self.PHONE))
        # 落 A 段不可重试档：档位判据单一真值源 + 联动清缓存语义
        self.assertTrue(security.is_hard_fail_message(msg))
        self.assertEqual(signin._retry_budget(msg), (signin.HARD_FAIL_MAX_ATTEMPTS, True))
        # 签发请求照常发出过一次（判据在响应侧，不改变协议形状）
        self.assertEqual(rec.path(1), "/code/usersure")

    def test_fake_success_missing_receipt_rejected(self):
        """活体反例：code==0 但响应缺 data 载荷 → 拒绝、零缓存写入、无成功日志。"""
        self._assert_receiptless_rejected({"code": 0, "msg": "ok"})

    def test_fake_success_null_variants_rejected(self):
        """回执缺失的两种退化形状：data 键不存在 / data 显式 null，一律拒绝。"""
        for body in ({"code": 0}, {"code": 0, "data": None, "msg": ""}):
            with self.subTest(body=body):
                self._assert_receiptless_rejected(body)

    def test_fake_success_does_not_update_existing_cache(self):
        """已有缓存行时遇假成功：拒绝路径自身零写入（行只会被既有探针判死清掉）。"""
        resp = _killyiban_happy_responses()
        resp[-1] = _resp({"code": 0, "msg": "ok"})
        client, rec = _killyiban_client(resp)
        signin.db.set_session_cache(self.PHONE, json.dumps({"csrf_token": "old"}), "old-csrf")
        with _capture_logs("yiban.platform") as buf, self.assertRaises(RuntimeError):
            _run(rec, client.login_killyiban)
        self.assertNotIn("登录成功", buf.getvalue())
        self.assertEqual(self._cache_rows(), 0,
                         "假成功不得产生/更新任何缓存行（哪怕覆盖旧行也不行）")

    def test_real_success_with_receipt_saves_cache(self):
        """真成功回归：回执齐全 → "登录成功"日志恰一次 + 缓存正常写入，行为与现状一致。"""
        resp = _killyiban_happy_responses()
        resp[-1] = _resp({"code": 0, "data": {"Token": "mock-receipt"}, "msg": ""},
                         cookies={"yiban_sess": "mock|sess"})
        client, rec = _killyiban_client(resp)
        with _capture_logs("yiban.platform") as buf:
            _run(rec, client.login_killyiban)
        self.assertTrue(client.logged_in)
        self.assertEqual(buf.getvalue().count("登录成功"), 1)
        row = signin.db.get_session_cache(self.PHONE)
        self.assertIsNotNone(row)
        self.assertEqual(row["csrf"], client.csrf)
        self.assertEqual(self._cache_rows(), 1)


# ---------------------------------------------------------------------------
# 签到两个接口的形状与三态
# ---------------------------------------------------------------------------
def _sign_position_data(*, state=0, msg="", positions=None, rng=None):
    """signPosition 信封：判定走 `State`（平台 stateEnum），`msg` 只作日志原文。

    默认 `state=0`（可签到）＝照常取点位并提交；判定用例逐值传 `state`。
    """
    now = int(signin.datetime.now().timestamp())
    return {
        "code": 0,
        "data": {
            "State": state,
            "Msg": msg,
            "IsNeedPhoto": 2,
            "Position": positions if positions is not None else [{
                "Id": "pos-1",
                "Type": "campus",
                "Title": "任务A",
                "Name": "任务A",
                "Points": ["118.0,31.0", "118.1,31.0", "118.1,31.1", "118.0,31.1"],
                "Address": "点A",
                "LngLat": "118.05,31.05",
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
        # 表单体必须**显式**声明 Content-Type。requests 只对 dict 形态的 data 自动补
        # `application/x-www-form-urlencoded`，对预编码字符串**不补**——缺这个头时
        # 上游 servlet 容器不解析表单（getParameter 全 null），读不到 SignInfo，
        # 回 msg="定位获取失败"（2026-10-08 实机 4 轮全拒的根因，工单 3kmt）。
        self.assertEqual(rec.header(1, "Content-Type"),
                         "application/x-www-form-urlencoded")
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
        # 旧流程的会话头自带带 charset 的 Content-Type：请求级补头**不得**覆盖
        # 它（补头只在会话缺头时发生，见 platform.submit_sign_in）。
        self.assertEqual(rec.header(1, "Content-Type"),
                         "application/x-www-form-urlencoded; charset=UTF-8")

    def test_three_states_and_window_skips(self):
        # 判定逐值走 `State`（平台 stateEnum）：3 已签到 / 4 已更改（班委手动补签，2026-10-08
        # 实拍形态：Msg="已更改"、Position 为空）/ 2 无需签到；`msg` 只作日志原文。
        cases = [
            (_sign_position_data(state=3, msg="已签到"), signin.STATUS_ALREADY, "已签到"),
            (_sign_position_data(state=4, msg="已更改", positions=[]),
             signin.STATUS_ALREADY, "已签到"),
            (_sign_position_data(state=2, msg="今日无需签到", positions=[]),
             signin.STATUS_NO_TASK, "无需签到"),
            (_sign_position_data(state=1, positions=[]), signin.STATUS_NO_POSITION,
             "未找到签到位置数据"),
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

    def test_session_expired_999_maps_to_relogin_path(self):
        """信封 code==999 → 库 SessionExpired → 翻成"会话失效"，并入既有重登档（清缓存重登）。

        这是 TASK-A/C 的会话过期语义：不新造异常面，靠消息里的"会话失效"词元落
        `SESSION_STALE_FAIL_KEYWORDS`（attempts 清缓存后强制真重登）。
        """
        client, rec = self._client([_resp({"code": 999, "msg": "登录已超时"})])
        with self.assertRaisesRegex(RuntimeError, "会话失效") as ctx:
            self._run_sign(client, rec)
        msg = str(ctx.exception)
        self.assertEqual(signin._retry_budget(msg),
                         (signin.SESSION_STALE_MAX_ATTEMPTS, True))


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

    def test_strict_yiban_url_boundaries(self):
        self.assertTrue(signin._is_strict_yiban_url("https://f.yiban.cn/iapp7463"))
        for url in ("https://f.yiban.cn.evil.com/iapp7463",
                    "https://f.yiban.cn@evil.com/iapp7463",
                    "http://f.yiban.cn/iapp7463",
                    "https://c.uyiban.com/iapp7463"):
            with self.subTest(url=url):
                self.assertFalse(signin._is_strict_yiban_url(url))

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

    def test_waf_detection_shape_unbounded_keyword_length_bounded(self):
        """挑战形态判定不受长度限制；仅关键词匹配维持长度上界（法律文本防误伤）。

        旧口径"`len>2000` 一律不判"是 fail-open：真实拦截/挑战页可以很长，被放行后按
        「网络抖动」打满重试（裁决 #9 判缺陷）。形态判定直接走 `waf.looks_like_challenge`
        的双 JS 特征对（不另抄特征串）；长度上界只保留给关键词支——正常长文（服务协议、
        法律文本）合法含"风控""拦截"字样。Unicode 转义解码口径不变。
        """
        challenge = ('<script>window.onload=setTimeout("yy(1701368163)", 200);'
                     'eval("qo=eval;qo(po);");</script>')
        self.assertTrue(signin.is_waf_blocked(challenge),
                        "短挑战页判拦截（形态支新行为：旧 len>2000 短路下判 False）")
        self.assertTrue(signin.is_waf_blocked("x" * 3000 + challenge),
                        "长挑战页必须判拦截——旧 len>2000 短路在此为红")
        long_text = "风险访问" + "正文" * 2000
        self.assertFalse(signin.is_waf_blocked(long_text),
                         "长文本仅关键词命中仍不拦（防误伤边界保留）")
        self.assertTrue(signin.is_waf_blocked("\\u98ce\\u9669\\u8bbf\\u95ee"))  # 风险访问
        self.assertTrue(signin.is_waf_blocked("访问服务禁用"))
        self.assertFalse(signin.is_waf_blocked('{"code":0,"msg":""}'))


#: 现网拦截页形态（中文词元 + 带边界的 ASCII 词元同页）：收紧后仍必须判拦。
REAL_BLOCK_PAGE = ('<!DOCTYPE html><html><head><title>访问拦截</title></head><body>'
                   '<p>您的访问存在风险访问，已被拦截，请联系管理员</p>'
                   '<p>WAF: request blocked</p></body></html>')

#: 只靠 ASCII 词元命中的真拦截页：证明收紧没有把 WAF 这一枚判据改瞎。
ASCII_ONLY_BLOCK_PAGE = '<html><body>Request blocked by WAF. ID=7f3a</body></html>'


class WafKeywordBoundaryTest(unittest.TestCase):
    """ASCII 词元的命中必须两侧都非字母数字；中文词元维持子串；长度上界口径不动。

    缺陷形状（工单 `yiban-auto-sign-u21x`）：登录页内嵌的 RSA 公钥 PEM 是 base64，
    base64 字母表能拼出三连续 `WAF`。`is_waf_blocked` 原先按裸子串匹配，且关键词支
    只在 `len<=2000` 的短响应里生效——PEM 页约 230 字符正落在启用区，于是抽中这枚
    PEM 的那个 xdist worker 里，所有嵌它的用例全被判成"请求被 WAF 风控拦截"。
    收紧只削掉 base64 这类造不出人话的误报面，真拦截页与真挑战页的判据一律不动。
    """

    #: base64 正文里的 aWAFb 形态：词元左右都是 base64 字符（字母或数字）。
    _BASE64_AWAFB = ("MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDBC7aWAFbrWLsOSBrj"
                     "57z0KDgqI2dvq7iKq6CZgXp2GvS5RufTg2d3L4A2fvWEMeH5y2LRkfPx")

    def test_base64_ascii_triple_is_not_blocked(self):
        """aWAFb 形态不得判拦截——这条在裸子串匹配下为红。"""
        self.assertFalse(signin.is_waf_blocked(self._BASE64_AWAFB))

    def test_login_page_embedding_such_a_key_is_not_blocked(self):
        """工单原形状：假登录页 = 模板 + 含 aWAFb 的公钥正文，整页必须放行。"""
        self.assertFalse(signin.is_waf_blocked(_KILLYIBAN_PAGE % self._BASE64_AWAFB))

    def test_ascii_token_with_non_alnum_neighbors_is_blocked(self):
        """两侧非字母数字即算命中：串首、串尾、空格、冒号、连字符、斜杠都算。"""
        for text in ("WAF blocked request", "risk/WAF/", "<title>:WAF</title>",
                     "请求被 WAF 拦截", "-WAF-"):
            with self.subTest(text=text):
                self.assertTrue(signin.is_waf_blocked(text),
                                "带边界的 ASCII 词元必须照常判拦（收紧不得削弱检测）")

    def test_real_block_pages_are_still_blocked(self):
        """真实形态拦截页夹具收紧后仍判拦——这条防把判据改瞎。

        `waf_challenge_body()` 逐字对照 `waf.looks_like_challenge` 的输入形态；
        `REAL_BLOCK_PAGE` 取现网拦截页文案（中文词元 + 带边界的 WAF 同页）。
        """
        self.assertTrue(signin.is_waf_blocked(waf_challenge_body()),
                        "真挑战页（形态支）必须照常判拦")
        self.assertTrue(signin.is_waf_blocked(REAL_BLOCK_PAGE))
        self.assertTrue(signin.is_waf_blocked(ASCII_ONLY_BLOCK_PAGE),
                        "只靠 ASCII 词元命中的真拦截页也必须判拦")

    def test_chinese_tokens_keep_substring_matching(self):
        """四枚中文词元维持子串匹配：base64 与转义文本造不出它们，误报面为零。"""
        for text in ("风控", "拦截", "访问服务禁用", "风险访问",
                     '{"msg":"\\u98ce\\u9669\\u8bbf\\u95ee"}'):
            with self.subTest(text=text):
                self.assertTrue(signin.is_waf_blocked(text))

    def test_keyword_leg_length_cap_unchanged(self):
        """长度上界口径不动：>2000 的纯关键词命中仍不判拦。

        中文长文的同口径由 `test_waf_detection_shape_unbounded_keyword_length_bounded`
        钉住，本条只补 ASCII 词元的超长形态（收紧后新形状的对照）。
        """
        self.assertFalse(signin.is_waf_blocked("x" * 3000 + " WAF "))


class PubkeyFixtureDeterminismTest(unittest.TestCase):
    """测试公钥夹具必须是仓内固定字节：随机生成是本文件非确定性的唯一来源。

    旧夹具每进程抽一枚（模块级 memo 让同进程内所有用例共用同一枚）。抽中含 `WAF`
    的那枚即整类同红、同提交两个进程一红一绿、本地不复现。夹具固定后这四条一起消失。
    """

    def test_pubkey_pem_is_stable_within_process(self):
        self.assertEqual(_pubkey_pem(), _pubkey_pem())

    def test_pubkey_pem_is_stable_across_processes(self):
        """子进程实跑一次比对：夹具不得随进程变化。"""
        code = ("import sys\n"
                "for p in ('.', 'scripts', 'tests'):\n"
                "    sys.path.insert(0, p)\n"
                "import test_login_protocol_shape as shape\n"
                "sys.stdout.write(shape._pubkey_pem())\n")
        proc = subprocess.run([sys.executable, "-c", code], cwd=BASE,
                              capture_output=True, text=True, timeout=300)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, _pubkey_pem(),
                         "公钥夹具跨进程不同值 = 每 worker 各抽一枚 = 非确定复发")

    def test_pubkey_pem_contains_no_waf_keyword(self):
        """夹具本身不得含任何一枚词元的命中形态。"""
        self.assertFalse(security.matches_waf_keywords(_pubkey_pem()))

    def test_fixtures_never_generate_keys_at_runtime(self):
        """守卫有牙：两个受害文件的公钥夹具改回随机生成即红（不靠口头约定）。"""
        # 探针名拆成两段拼接：整写会让本行自己命中，守卫就永远绿。
        needle = "RSA" + ".generate"
        for rel in ("tests/test_login_protocol_shape.py", "tests/test_protocol_masking.py"):
            src = io.open(os.path.join(BASE, *rel.split("/")), encoding="utf-8").read()
            with self.subTest(path=rel):
                self.assertNotIn(needle, src,
                                 "测试公钥必须固定字节，不得回到随机生成")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class LegacyPageFallbackTest(unittest.TestCase):
    """legacy 页宽容回退（评审 #5）：令牌赋值无 `var` 关键字时仅 legacy 流程可解析。

    主路径（killyiban 契约）严格要求 `var page_use`；legacy 是兜底流程，其页面
    解析路径必须与主路径不同源——主路径失效时不致双双失效。
    """

    def test_legacy_page_without_var_keyword_parses_only_for_legacy_flow(self):
        from yiban import platform as platform_module

        pem = _pubkey_pem()
        html = (
            "<script>page_use = '" + "a" * 40 + "';</script>"
            '<input type="test" id="key" value="' + pem + '">'
        )
        page_use, key = platform_module.parse_login_page(html, flow="legacy")
        self.assertEqual(page_use, "a" * 40)
        self.assertIsNotNone(key)
        # killyiban 契约仍严格要求 var 关键字：同一页面对默认流程解析失败
        self.assertEqual(
            platform_module.parse_login_page(html, flow="killyiban"), (None, None)
        )
