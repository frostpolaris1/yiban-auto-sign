# -*- coding: utf-8 -*-
"""易班**协议步骤**：登录握手与签到两接口的请求构造与响应解析。

**衍生声明**：本文件中的端点、参数名与取值、页面正则、RSA 编码方式与握手顺序
来自上游 `onefeifan/fyiban`（AGPL-3.0）及其同源实现 KillYiBan，逐块对照见同目录
`PROVENANCE.md`。本层是"平台要求怎么做"的知识，**不含**本项目自有的安全判断：

- URL 白名单、WAF 拦截判定、脱敏与诊断措辞一律经 `policy`（`RequestPolicy`）注入，
  由 `yiban/security.py` 实现、`yiban/client.py` 组装；
- 会话缓存（我们减少登录频率的手段，非上游概念）经 `session_store` 注入，
  未注入时即"不缓存"，本层不反向依赖 `yiban.store` / `yiban.client`。

第三方层因此可以独立核对、替换或升级：换平台时实现本文件的端点与形状即可，
安全策略与会话策略不必跟着重写。
"""
import json
import logging
import re
from base64 import b64decode, b64encode
from typing import Callable, NamedTuple, Protocol
from urllib.parse import urlencode

from Crypto.Cipher import PKCS1_v1_5
from Crypto.PublicKey import RSA
from requests.utils import cookiejar_from_dict, dict_from_cookiejar

from . import headers as fyiban_headers
from . import waf as fyiban_waf

logger = logging.getLogger("yiban.fyiban.protocol")

# ---------------------------------------------------------------------------
# 端点与参数（平台侧固定值）
# ---------------------------------------------------------------------------
OAUTH_CLIENT_ID = "95626fa3080300ea"
OAUTH_REDIRECT_URI = "https://f.yiban.cn/iapp7463"
API_AUTH_URL = "https://api.uyiban.com/base/c/auth/yiban"
OAUTH_CODE_HTML_URL = "https://oauth.yiban.cn/code/html"
OAUTH_USERSURE_URL = "https://oauth.yiban.cn/code/usersure"
IFRAME_INDEX_URL = "https://f.yiban.cn/iframe/index"
SIGN_POSITION_URL = "https://api.uyiban.com/nightAttendance/student/index/signPosition"
SIGN_IN_URL = "https://api.uyiban.com/nightAttendance/student/index/signIn"

#: 单请求超时（秒）：登录链与签到链一致
REQUEST_TIMEOUT = 15

# 旧流程（Auto-Test 继承）的页面正则
LEGACY_PAGE_USE_RE = re.compile(r"page_use ?= ?['|\"]([a-zA-Z0-9-_]+)['|\"]")
LEGACY_KEY_RE = re.compile(r'id="key"\s+value="([^"]+)"')
# KillYiBan（jsoup 等价）的页面正则
KILLYIBAN_KEY_RE = re.compile(r'<input[^>]*id="key"[^>]*value="([^"]+)"')
KILLYIBAN_PAGE_USE_RE = re.compile(r"var page_use = '([^']+)'")
# verify_request 取值：必须宽容——令牌可能位于 query 末位，若正则要求其后必跟 `&`
# 就会取不到，导致全站性登录失败
VERIFY_REQUEST_RE = re.compile(r"verify_request=([^&]+)&?")
# KillYiBan 判断"已登录"的方式：OAuth 探针 302 落到 redirect_uri
LOGGED_IN_MARKER = "iapp7463"


class RequestPolicy(Protocol):
    """协议层需要的安全策略（实现见 `yiban/security.py::ProtocolPolicy`）。"""

    #: 严格白名单判定函数，注入给挑战解析器（`fyiban.waf.solve_ydclearance`）
    allow_fyiban_url: Callable[[str], bool]

    def require_trusted(self, url, site):
        """宽松白名单；不合格抛 RuntimeError（site 用于定位到具体协议步骤）。"""

    def require_fyiban(self, url, site="ydclearance"):
        """严格白名单（挑战页跳转目标）。"""

    def is_blocked(self, resp):
        """该响应是否被 WAF 风控拦截（返回布尔，不抛错）。"""

    def require_not_blocked(self, resp):
        """被风控拦截即抛 RuntimeError。"""

    def sanitize(self, text):
        """对外文本脱敏。"""

    def describe_location(self, location):
        """302 Location 的诊断描述（脱敏：去 query 与 userinfo）。"""

    def log_response_diagnostics(self, phone, resp, *, stage, hits=None, advice=True):
        """按脱敏口径落"响应不符合预期"的现场日志。"""


class SessionStore(Protocol):
    """会话缓存的存取（实现见 `yiban/client.py`）。

    `restore()` 命中的**同时**要把 cookies 装回会话并返回缓存里的 CSRF 令牌——
    令牌与会话是一体的，分开返回会让调用方有机会漏掉其中之一。
    """

    def restore(self):
        """命中返回 CSRF 令牌字符串；未命中返回 None。"""

    def save(self, session, csrf):
        """完整登录成功后保存会话与令牌。"""

    def clear(self):
        """清除本账号缓存（探针判死时调用）。"""


class LoginOutcome(NamedTuple):
    """登录结果：本次生效的 CSRF 令牌 + 是否走了免登录复用。"""

    csrf: str
    via_cache: bool = False


class SignResponse(NamedTuple):
    """签到链一次响应的解析结果。

    `blocked=True` 表示响应被风控拦截（此时 `data` 为 None）——签到/探针必须把它
    降级成"失败/不可用"而不是抛异常，与登录链的口径不同。
    """

    data: dict | None
    blocked: bool = False


# ---------------------------------------------------------------------------
# 请求构造：把平台要求的字段拼成可发送的形状
# ---------------------------------------------------------------------------
def encrypt_password(password, public_key):
    """RSA-1024 + PKCS1_v1_5 加密密码，返回 base64 密文（bytes，与 urlencode 兼容）。

    上游直接 `cipher.encrypt(密码)`；本项目在**发送前**加了一道长度守卫：RSA-1024
    公钥单次最多 117 字节，超长时 pycryptodome 会抛难以理解的底层异常，这里换成
    可执行的处置建议（见 `PROVENANCE.md` 的"本地差异"）。
    """
    if len(password) > 117:
        raise ValueError(
            "密码过长: RSA-1024 公钥单次最多加密 117 字节（约 39 个中文字符），"
            "当前密码无法加密提交，请缩短密码或联系管理员处理"
        )
    # bytes(...) 产生一个短暂不可变副本（pycryptodome 接口要求），交由 GC 回收；
    # 可清零的 bytearray 本体在尝试结束后由 YibanClient._wipe_credentials 原位覆写
    return b64encode(PKCS1_v1_5.new(public_key).encrypt(bytes(password)))


def parse_login_page(text, *, flow):
    """解析登录页，返回 `(page_use, 公钥)`；缺失任一即返回 `(None, None)`。

    两条流程的页面结构不同（旧流程用 `page_use = '…'` + `id="key" value="…"`；
    KillYiBan 用 `var page_use = '…'` + `<input … id="key" …>`），密钥的装载方式
    也不同——KillYiBan 的 key 带 PEM 头尾，需剥掉后按 X509 解码。
    """
    if flow == "killyiban":
        key_match = KILLYIBAN_KEY_RE.findall(text)
        page_use_match = KILLYIBAN_PAGE_USE_RE.findall(text)
        if not key_match or not page_use_match:
            return None, None
        key = RSA.import_key(b64decode(
            re.sub(r"\s+", "", key_match[0]
                   .replace("-----BEGIN PUBLIC KEY-----", "")
                   .replace("-----END PUBLIC KEY-----", ""))
        ))
        return page_use_match[0], key
    key_match = LEGACY_KEY_RE.findall(text)
    page_use_match = LEGACY_PAGE_USE_RE.findall(text)
    if not page_use_match or not key_match:
        return None, None
    return page_use_match[0], RSA.importKey(key_match[0])


def usersure_form(phone, encrypted_password, *, scope, display):
    """usersure 提交体（`scope`/`display` 是两条流程的唯一差异）。"""
    return urlencode({
        "oauth_uname": phone,
        "oauth_upwd": encrypted_password,
        "client_id": OAUTH_CLIENT_ID,
        "redirect_uri": OAUTH_REDIRECT_URI,
        "state": "",
        "scope": scope,
        "display": display,
    })


def extract_verify_request(location):
    """从 302 的 Location 里取 verify_request 令牌（取不到返回 None）。"""
    matched = VERIFY_REQUEST_RE.findall(location or "")
    return matched[0] if matched else None


def build_sign_info(lng, lat, address):
    """签到附件的业务字段（前端展示为"签到位置说明"）。"""
    return {
        "Reason": "",
        "AttachmentFileName": "",
        "LngLat": f"{lng},{lat}",
        "Address": address,
    }


def sign_in_form(phone_code, phone_model, sign_info, *, out_state):
    """signIn 提交体（`out_state` 是两条流程的唯一差异：MINI_VERSION "1" vs "1.0"）。"""
    return {
        "Code": phone_code,
        "PhoneModel": phone_model,
        "SignInfo": json.dumps(sign_info, ensure_ascii=False),
        "OutState": out_state,
    }


# ---------------------------------------------------------------------------
# 登录握手
# ---------------------------------------------------------------------------
def login_legacy(session, *, phone, password, csrf, policy):
    """旧流程（Auto-Test 继承，iOS 伪造 UA）六次请求完成登录。

    仅在 `YIBAN_LEGACY_LOGIN=1` 时启用；失败抛 RuntimeError（消息已脱敏）。
    返回本次生效的 `LoginOutcome`。
    """
    session.cookies = cookiejar_from_dict({"csrf_token": csrf})
    session.headers.update(Referer="https://c.uyiban.com/", Origin="https://c.uyiban.com")

    # 1. 获取跳转 URL
    resp = session.get(
        API_AUTH_URL, params={"CSRF": csrf}, allow_redirects=False, timeout=REQUEST_TIMEOUT
    )
    policy.require_not_blocked(resp)
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"获取登录入口失败: {policy.sanitize(data.get('msg'))}")

    # 2. 跳转到 OAuth 页面，解析 RSA 公钥与 page_use
    oauth_url = data["data"]["Data"]
    policy.require_trusted(oauth_url, "login_entry")
    resp = session.get(oauth_url, allow_redirects=True, timeout=REQUEST_TIMEOUT)
    policy.require_not_blocked(resp)

    page_use, key = parse_login_page(resp.text, flow="legacy")
    if page_use is None:
        # 诊断要看形状：最终 URL（query 已打码）、状态码、长度、前 300 字符、正则命中数
        policy.log_response_diagnostics(
            phone, resp, stage="OAuth 页解析失败",
            hits={"page_use": len(LEGACY_PAGE_USE_RE.findall(resp.text)),
                  "key": len(LEGACY_KEY_RE.findall(resp.text))},
        )
        raise RuntimeError("登录页面解析失败（page_use / RSA key 未找到），详见上方诊断日志")
    session.headers.update(Referer=resp.url, Origin="https://oauth.yiban.cn")

    # 3. 提交账号密码
    resp = session.post(
        OAUTH_USERSURE_URL,
        params={"ajax_sign": page_use},
        data=usersure_form(phone, encrypt_password(password, key),
                           scope="1,2,3,4,", display="html"),
        allow_redirects=False,
        timeout=REQUEST_TIMEOUT,
    )
    # 先检测 WAF 拦截再做 JSON 解析（拦截页是 HTML，直接 json() 会抛解析异常）
    policy.require_not_blocked(resp)
    result = resp.json()
    if "reUrl" not in result:
        policy.log_response_diagnostics(phone, resp, stage="usersure 响应无 reUrl 字段，", advice=False)
        raise RuntimeError(f"登录响应异常（无 reUrl）: {policy.sanitize(result)}")
    if "error" in result.get("reUrl", ""):
        raise RuntimeError(f"登录失败（账号或密码错误）: {phone}")

    # 4. 跳转回 f.yiban.cn，可能遇到 ydclearance 反爬
    session.headers.update(Referer="https://oauth.yiban.cn")
    policy.require_trusted(str(result.get("reUrl", "")), "login_reurl")
    resp = session.get(result["reUrl"], allow_redirects=False, timeout=REQUEST_TIMEOUT)

    if fyiban_waf.looks_like_challenge(resp.text, resp.headers.get("Set-Cookie", "")):
        # 纯 Python 解析挑战（不执行任何远程 JS），得出 cookie 与跳转路径。
        # 白名单是**本项目**的安全策略，以参数注入解析器。
        clearance = fyiban_waf.solve_ydclearance(
            resp.text, allow_url=policy.allow_fyiban_url)
        cookies = dict_from_cookiejar(session.cookies)
        cookies["https_ydclearance"] = clearance[0]
        session.cookies = cookiejar_from_dict(cookies)
        session.headers.update(Referer=resp.url, Origin="https://f.yiban.cn")
        policy.require_fyiban(clearance[1])
        resp = session.get(clearance[1], allow_redirects=False, timeout=REQUEST_TIMEOUT)
        session.headers.update(Referer=resp.url)
    else:
        session.headers.update(Referer=resp.url, Origin="https://f.yiban.cn")

    # 5. 获取 verify_request
    location = resp.headers.get("Location", "")
    if not location:
        raise RuntimeError(
            f"获取 verify_request 失败: 上一步响应缺少 Location 头"
            f"（状态码 {resp.status_code}，响应长度 {len(resp.text)}）"
        )
    policy.require_trusted(location, "verify_request")
    resp = session.get(location, allow_redirects=False, timeout=REQUEST_TIMEOUT)
    verify_code = extract_verify_request(resp.headers.get("Location", ""))
    if not verify_code:
        raise RuntimeError(
            f"获取 verify_request 失败: 重定向响应缺少 verify_request 参数"
            f"（状态码 {resp.status_code}，响应长度 {len(resp.text)}）"
        )

    # 6. 完成登录
    session.headers.update(Referer="https://c.uyiban.com/", Origin="https://c.uyiban.com")
    resp = session.get(
        API_AUTH_URL,
        params={"verifyRequest": verify_code, "CSRF": csrf},
        cookies={},
        allow_redirects=False,
        timeout=REQUEST_TIMEOUT,
    )
    policy.require_not_blocked(resp)
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"最终认证失败: {policy.sanitize(data.get('msg'))}")
    if "csrf_token" not in dict_from_cookiejar(session.cookies):
        raise RuntimeError("登录失败：未获取到 csrf_token")
    logger.info(f"[{phone}] 登录成功")
    return LoginOutcome(csrf=csrf)


def login_killyiban(session, *, phone, password, csrf, policy, session_store=None):
    """KillYiBan 同款登录（默认登录方式）：OAuth 页 → usersure → iframe → 认证。

    与旧流程的差异（差异本身即上游事实，改动等于换登录方式）：
    - 入口直接打 `oauth.yiban.cn/code/html`（不先打 api.uyiban.com）；
    - usersure **不带 Referer/Origin**（原 App 传空 headers；带 Origin 会得 e001）；
    - `scope` 传空、`display` 传 "authorize"；
    - 成功标志是 `code == "s200"`；
    - 页面解析用 jsoup 等价正则，key 去掉 BEGIN/END 后按 X509 解码。

    `session_store` 提供时会先探活复用会话（少登录 = 少风控暴露面），未提供即不缓存。
    """
    restored = False
    if session_store is not None:
        cached_csrf = session_store.restore()
        if cached_csrf:
            restored = True
            csrf = cached_csrf
    if not restored:
        # 设置 csrf_token cookie（服务器用其校验 CSRF 参数，缺失会报 CSRF invalid）
        session.cookies = cookiejar_from_dict({"csrf_token": csrf})
    # session 头已是 KILLYIBAN_HEADERS，此处无需再改

    # 1. 打开 OAuth 登录页；不跟随重定向，直接取登录页 HTML
    resp = session.get(
        OAUTH_CODE_HTML_URL,
        params={"client_id": OAUTH_CLIENT_ID, "redirect_uri": OAUTH_REDIRECT_URI},
        allow_redirects=False,
        timeout=REQUEST_TIMEOUT,
    )
    # 若直接返回 redirect_uri 说明服务端已是登录态（正常流程是停留在登录页）
    if LOGGED_IN_MARKER in (resp.headers.get("Location", "")):
        if restored:
            logger.info(f"[{phone}] 登录: 会话缓存命中，免登录复用")
        else:
            logger.info(f"[{phone}] 登录: 已登录状态（无需提交）")
        return LoginOutcome(csrf=csrf, via_cache=restored)
    if restored:
        # 缓存会话已被服务端判失效（探针返回登录页）：清缓存并还原干净初始会话
        session_store.clear()
        session.cookies = cookiejar_from_dict({"csrf_token": csrf})

    # 2. 解析 RSA 公钥与 page_use
    page_use, key = parse_login_page(resp.text, flow="killyiban")
    if page_use is None:
        policy.log_response_diagnostics(
            phone, resp, stage="登录 OAuth 页解析失败",
            hits={"key": len(KILLYIBAN_KEY_RE.findall(resp.text)),
                  "page_use": len(KILLYIBAN_PAGE_USE_RE.findall(resp.text))},
            advice=False,
        )
        raise RuntimeError("登录: OAuth 页解析失败")

    # 3. 提交账号密码。usersure 必须不带 Origin/Referer 才返回 s200（带 Origin 会得
    #    e001"无效的应用端编号"）；scope 空 + display=authorize 与 App 一致
    resp = session.post(
        OAUTH_USERSURE_URL,
        params={"ajax_sign": page_use},
        headers={
            "User-Agent": "Yiban",
            "AppVersion": fyiban_headers.KILLYIBAN_HEADERS["AppVersion"],
            "Origin": None,
            "Referer": None,
            "X-Requested-With": None,
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        },
        data=usersure_form(phone, encrypt_password(password, key),
                           scope="", display="authorize"),
        allow_redirects=False,
        timeout=REQUEST_TIMEOUT,
    )
    policy.require_not_blocked(resp)
    result = resp.json()
    if result.get("code") != "s200":
        raise RuntimeError(f"登录失败: {policy.sanitize(result.get('msgCN', result))}")

    # 4. 打开 iframe/index 获取 Location → verify_request（默认三个头）
    resp = session.get(
        IFRAME_INDEX_URL, params={"act": LOGGED_IN_MARKER},
        allow_redirects=False, timeout=REQUEST_TIMEOUT,
    )
    location = resp.headers.get("Location", "")
    verify_code = extract_verify_request(location)
    if not verify_code:
        # Location 的 query 含 verify_request 令牌，错误消息只留 host/path
        raise RuntimeError(
            f"无法提取 verify_request（Location={policy.describe_location(location)}）"
        )

    # 5. 完成认证（默认三个头 + 跟随重定向，最终返回 JSON）
    resp = session.get(
        API_AUTH_URL,
        params={"verifyRequest": verify_code, "CSRF": csrf},
        allow_redirects=True,
        timeout=REQUEST_TIMEOUT,
    )
    policy.require_not_blocked(resp)
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"最终认证失败: {policy.sanitize(data.get('msg'))}")
    logger.info(f"[{phone}] 登录成功")
    if session_store is not None:
        # 完整登录成功：保存会话缓存供下次免登录复用（失败仅告警，不影响签到）
        session_store.save(session, csrf)
    return LoginOutcome(csrf=csrf)


# ---------------------------------------------------------------------------
# 签到链
# ---------------------------------------------------------------------------
def fetch_sign_position(session, csrf, policy):
    """拉取签到任务（点位 + 时间窗）。返回 `SignResponse`。"""
    resp = session.get(
        SIGN_POSITION_URL, params={"CSRF": csrf},
        allow_redirects=False, timeout=REQUEST_TIMEOUT,
    )
    if policy.is_blocked(resp):
        return SignResponse(data=None, blocked=True)
    return SignResponse(data=resp.json())


def submit_sign_in(session, csrf, *, phone_code, phone_model, sign_info, out_state, policy):
    """提交一次签到。返回 `SignResponse`（`data` 为服务端返回体）。"""
    resp = session.post(
        SIGN_IN_URL,
        params={"CSRF": csrf},
        data=sign_in_form(phone_code, phone_model, sign_info, out_state=out_state),
        allow_redirects=False,
        timeout=REQUEST_TIMEOUT,
    )
    if policy.is_blocked(resp):
        return SignResponse(data=None, blocked=True)
    return SignResponse(data=resp.json())
