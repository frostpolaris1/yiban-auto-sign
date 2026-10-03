# -*- coding: utf-8 -*-
"""**功能**
易班**协议步骤**：登录握手与签到两接口的请求**编排**（端点、顺序、请求形态与会话簿记）。

**核心换核（B'）**：授权页解析、usersure 表单构造、verify_request 提取、签到体构造这些
**纯解析/构造**已下沉到 vendored 洁净室库 `yiban/_vendor/yiban_protocol`（MIT；来源、
版本与同步纪律见 `yiban/_vendor/VENDORED.md`）。本文件只保留**编排层**——请求顺序、端点、
注入策略与重试/会话语义——即 TASK-C《六跳链路实拍契约》的实现：
`oauth.yiban.cn/code/html` → `/code/usersure` → `f.yiban.cn/iframe/index` →
`api.uyiban.com/base/c/auth/yiban` → `.../signPosition` → `.../signIn`。

**归属**
`yiban/fyiban/` 第三方隔离层的协议模块（"平台要求怎么做"的知识），**不含**本项目
自有的安全判断：

- URL 白名单、WAF 拦截判定、脱敏与诊断措辞一律经 `policy`（`RequestPolicy`）注入，
  由 `yiban/security.py` 实现、`yiban/client.py` 组装；
- 会话缓存（我们减少登录频率的手段，非上游概念）经 `session_store` 注入，
  未注入时即"不缓存"，本层不反向依赖 `yiban.store` / `yiban.client`。

**复用**
端点/参数常量（`OAUTH_CLIENT_ID`、`API_AUTH_URL` 等）、`RequestPolicy` / `SessionStore`
协议与登录/签到函数是隔离层的对外接口；解析与构造一律委托
`yiban._vendor.yiban_protocol`，本文件不得再内联一份正则或表单字段表。

**通信**
输入：`requests.Session`、注入的 `policy` 与 `session_store`、账号/密码与点位参数。
输出：签到结果与会话；每一步跳转都过 `policy.require_*` 校验，本层不自算裁决。
调用谁：`requests`、vendored `yiban_protocol`、同层 `waf`、注入的 `policy` / `session_store`。
谁调用：`yiban/client.py`（唯一生产调用方，负责组装 policy 与 session_store）。
前端调用点：无直接调用点（隔离层）。
"""
import contextlib
import logging
import re
from typing import NamedTuple, Protocol
from urllib.parse import urljoin

from Crypto.PublicKey import RSA
from requests.utils import cookiejar_from_dict, dict_from_cookiejar

from yiban._vendor.yiban_protocol import (
    ParseError,
    SessionExpired,
    build_sign_in_body,
    build_usersure_form,
    extract_verify_request,
    parse_api_envelope,
    parse_authorize_page,
    parse_sign_position,
)
from yiban._vendor.yiban_protocol import crypto as _lib_crypto

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

#: 最终认证手动跟跳的层数上限：正常只有一跳（落点才是 JSON），
#: 超过即视为重定向环，响亮失败而不是无限跟下去
MAX_FINAL_AUTH_REDIRECTS = 5

#: KillYiBan 判断"已登录"的方式：OAuth 探针 302 落到 redirect_uri
LOGGED_IN_MARKER = "iapp7463"

#: RSA-1024 公钥单次可加密的明文字节上限（PKCS#1 v1.5 填充开销 11 字节）
_RSA1024_MAX_BYTES = 117


class RequestPolicy(Protocol):
    """协议层需要的安全策略（实现见 `yiban/security.py::ProtocolPolicy`）。"""

    def require_trusted(self, url, site):
        """宽松白名单；不合格抛 RuntimeError（site 用于定位到具体协议步骤）。"""

    def is_blocked(self, resp):
        """该响应是否被 WAF 风控拦截（返回布尔，不抛错）。"""

    def is_logged_in_redirect(self, location):
        """302 Location 是否指向"已登录"标识页（仅 host+path，允许 query）。"""

    def require_redir_chain_trusted(self, resp, site):
        """跟随重定向后，校验最终 URL 与每一跳 history 都在白名单内。"""

    def require_not_blocked(self, resp):
        """被风控拦截即抛 RuntimeError。"""

    def sanitize(self, text):
        """对外文本脱敏。"""

    def mask_account(self, phone):
        """账号标识（手机号）入日志与错误消息前脱敏。"""

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
# 纯解析/构造：薄委托 vendored 洁净室库（本层不再内联正则与字段表）
# ---------------------------------------------------------------------------
def encrypt_password(password, public_key_pem):
    """RSA-1024 + PKCS1_v1_5 加密密码，返回 base64 密文（str，与 urlencode 兼容）。

    委托 `yiban_protocol.crypto.encrypt_password`（输入 PEM 文本）。本项目保留发送前的
    长度守卫：RSA-1024 单次最多 117 字节，超长时库底层的 pycryptodome 异常难以理解，
    这里换成可执行的处置建议（`PROVENANCE.md` 的"本地差异"）。
    """
    if isinstance(password, (bytes, bytearray)):
        # 客户端以 bytearray 持有密码（可原位清零），库接口要求 str：此处产生一份
        # 短暂不可变副本，尝试结束后由 YibanClient._wipe_credentials 覆写 bytearray 本体。
        password = bytes(password).decode("utf-8")
    if len(password.encode("utf-8")) > _RSA1024_MAX_BYTES:
        raise ValueError(
            "密码过长: RSA-1024 公钥单次最多加密 117 字节（约 39 个中文字符），"
            "当前密码无法加密提交，请缩短密码或联系管理员处理"
        )
    return _lib_crypto.encrypt_password(password, public_key_pem)


def parse_login_page(text, *, flow):
    """解析授权页，返回 `(page_use, RSA 公钥对象)`；缺失/损坏一律 `(None, None)`。

    主路径委托 vendored `parse_authorize_page`（killyiban 页契约，TASK-C 实拍：
    `var page_use = …` + `id="key"` 隐藏 input 内多行 PEM）。`flow=="legacy"` 允许一次
    **宽容重试**：旧 legacy 页的令牌赋值不要求 `var` 关键字；回退的令牌定位与主路径
    **不同源**（库失败后独立找 `page_use = …` 并补 `var ` 重试同一条库路径），默认流程
    页面结构变化、主路径失效时不致两路同时失效。key 命中但损坏（缺 PEM 头尾/非法 DER）
    与"没命中"同价返回 `(None, None)`——底层 `ParseError` 不得越过本函数的返回契约。
    """
    try:
        page = parse_authorize_page(text)
    except ParseError as exc:
        page = None
        if flow != "legacy" or getattr(exc, "field", "") != "page_use":
            return None, None
        m = re.search(r"page_use\s*=\s*['\"]", text or "")
        if m is None:
            return None, None
        try:
            page = parse_authorize_page(text[: m.start()] + "var " + text[m.start():])
        except ParseError:
            return None, None
    try:
        return page.page_use, RSA.import_key(page.public_key_pem)
    except (ValueError, TypeError, IndexError):  # 损坏 key：导入抛底层异常
        return None, None


def build_sign_info(lng, lat, address):
    """签到附件的业务字段（前端展示为"签到位置说明"）。"""
    return {
        "Reason": "",
        "AttachmentFileName": "",
        "LngLat": f"{lng},{lat}",
        "Address": address,
    }


def _sign_in_body(phone_code, phone_model, sign_info, out_state):
    """把 `sign_info` 字典还原为库 `build_sign_in_body` 的入参并构造提交体。

    与观测流量逐字节同构的保证在库侧（键序 Reason/AttachmentFileName/LngLat/Address、
    JSON 默认分隔符、整串 urlencode）。
    """
    lng_text, _, lat_text = str(sign_info.get("LngLat", "")).partition(",")
    return build_sign_in_body(
        lnglat=(float(lng_text), float(lat_text)),
        address=str(sign_info.get("Address", "")),
        out_state=out_state,
        reason=str(sign_info.get("Reason", "")),
        attachment_file_name=str(sign_info.get("AttachmentFileName", "")),
        code=phone_code,
        phone_model=phone_model,
    )


def _parse_api_body(resp):
    """解析 uyiban API 信封；`code == 999` 翻译为"会话失效→重登"。

    库 `parse_api_envelope` 的 `SessionExpired` 是本层唯一会引入的"会话已失效"信号，
    适配层把它翻成既有引擎能识别的消息（含 `会话失效` 词元，落
    `yiban.engine.attempts.SESSION_STALE_FAIL_KEYWORDS` 的会话陈旧档：清缓存后强制
    真重登），不新造异常面。非 JSON 响应沿用 requests `.json()` 的既有报错口径
    （"Expecting value…" 是 `security.HARD_FAIL_TOKENS` 的硬失败词元）。
    """
    try:
        envelope = parse_api_envelope(resp.text)
    except SessionExpired as exc:
        raise RuntimeError("会话失效（上游 code=999），需重新登录") from exc
    except ParseError:
        return resp.json()
    return envelope.raw


def _validate_sign_position(data):
    """成功信封里的签到配置过一次库解析（TASK-C §2[5] 契约）——结构不合即抛 ParseError。

    只在"形状已足以进入客户端点位分支"时才解析：`code == 0`、`Position` 为非空数组、
    顶层 `Range` 带 `StartTime/EndTime`。这样既不改变既有"缺 Range / 空 Position"的
    容错分支，又让完整响应真实走库解析路径（含 `Range` 时间窗对象与 `IsNeedPhoto`）。
    """
    if not isinstance(data, dict) or data.get("code") != 0:
        return
    data_obj = data.get("data")
    if not isinstance(data_obj, dict):
        return
    positions = data_obj.get("Position")
    if not isinstance(positions, list) or not positions:
        return
    rng = data_obj.get("Range")
    if not isinstance(rng, dict) or "StartTime" not in rng or "EndTime" not in rng:
        return
    # 解析是**观察性**的：库契约逐点强制 Id/Type/Title/LngLat，而客户端只消费
    # Name/Points/Address；畸形候选点不得把（可能幂等成功的）拉取翻成失败，
    # 也不得落普通重试档——回退原始 data，由既有容错分支处置。
    with contextlib.suppress(ParseError):
        parse_sign_position(data_obj)


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
    # 跟随重定向可能把白名单内主机 302 到白名单外（RSA 公钥取自落点 HTML）——
    # 首跳白名单只校验入口，落点与中间每一跳都要再验一次（M8）。
    policy.require_redir_chain_trusted(resp, "login_entry")

    page_use, key = parse_login_page(resp.text, flow="legacy")
    if page_use is None:
        # 诊断要看形状：最终 URL（query 已打码）、状态码、长度、前 300 字符
        policy.log_response_diagnostics(phone, resp, stage="OAuth 页解析失败")
        raise RuntimeError("登录页面解析失败（page_use / RSA key 未找到），详见上方诊断日志")
    session.headers.update(Referer=resp.url, Origin="https://oauth.yiban.cn")

    # 3. 提交账号密码
    resp = session.post(
        OAUTH_USERSURE_URL,
        params={"ajax_sign": page_use},
        data=build_usersure_form(
            phone, encrypt_password(password, key.export_key().decode("utf-8")),
            OAUTH_CLIENT_ID, OAUTH_REDIRECT_URI, scope="1,2,3,4,", display="html",
        ),
        allow_redirects=False,
        timeout=REQUEST_TIMEOUT,
    )
    # 先检测 WAF 拦截再做 JSON 解析（拦截页是 HTML，直接 json() 会抛解析异常）
    policy.require_not_blocked(resp)
    result = resp.json()
    if "reUrl" not in result:
        policy.log_response_diagnostics(phone, resp, stage="usersure 响应无 reUrl 字段，", advice=False)
        raise RuntimeError(f"登录响应异常（无 reUrl）: {policy.sanitize(result)}")
    # reUrl 可能为 null/非字符串：先归一成字符串再判错，避免 `in None` 抛 TypeError；
    # 后续白名单校验与实际请求**同取这一个局部变量**（此前校验 str(...) 却用原值请求）
    reurl = str(result.get("reUrl", "") or "")
    if "error" in reurl:
        # 账号标识入消息前经 policy 脱敏（打码规则属本项目，不在本层内联）
        raise RuntimeError(f"登录失败（账号或密码错误）: {policy.mask_account(phone)}")

    # 4. 跳转回 f.yiban.cn，可能遇到 ydclearance 反爬
    session.headers.update(Referer="https://oauth.yiban.cn")
    policy.require_trusted(reurl, "login_reurl")
    resp = session.get(reurl, allow_redirects=False, timeout=REQUEST_TIMEOUT)

    if fyiban_waf.looks_like_challenge(resp.text, resp.headers.get("Set-Cookie", "")):
        # 既定裁决：不再尝试求解挑战（生产全历史零触发的求解器已删除）。命中即响亮失败；
        # 文案含 "ydclearance" 词元 → 落不可重试硬失败档并联动清会话缓存。
        raise RuntimeError(fyiban_waf.CHALLENGE_DETECTED_MESSAGE)
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
    logger.info(f"[{policy.mask_account(phone)}] 登录成功")
    return LoginOutcome(csrf=csrf)


def login_killyiban(session, *, phone, password, csrf, policy, session_store=None):
    """KillYiBan 同款登录（默认登录方式）：OAuth 页 → usersure → iframe → 认证。

    与旧流程的差异（差异本身即上游事实，改动等于换登录方式）：
    - 入口直接打 `oauth.yiban.cn/code/html`（不先打 api.uyiban.com）；
    - usersure **不带 Referer/Origin**（原 App 传空 headers；带 Origin 会得 e001）；
    - `scope` 传空、`display` 传 "authorize"；
    - 成功标志是 `code == "s200"`。

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
    # 若直接返回 redirect_uri 说明服务端已是登录态（正常流程是停留在登录页）。
    # 判定经 policy 注入（本层不做域名比对）：只认 f.yiban.cn/iapp7463（允许 query），
    # 恶意 host / 子域伪装的 Location 一律不当"已登录"（M7）。
    if policy.is_logged_in_redirect(resp.headers.get("Location", "")):
        if restored:
            logger.info(f"[{policy.mask_account(phone)}] 登录: 会话缓存命中，免登录复用")
        else:
            logger.info(f"[{policy.mask_account(phone)}] 登录: 已登录状态（无需提交）")
        return LoginOutcome(csrf=csrf, via_cache=restored)
    if restored:
        # 缓存会话已被服务端判失效（探针返回登录页）：清缓存并还原干净初始会话
        session_store.clear()
        session.cookies = cookiejar_from_dict({"csrf_token": csrf})

    # 2. 解析 RSA 公钥与 page_use
    page_use, key = parse_login_page(resp.text, flow="killyiban")
    if page_use is None:
        policy.log_response_diagnostics(phone, resp, stage="登录 OAuth 页解析失败", advice=False)
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
        data=build_usersure_form(
            phone, encrypt_password(password, key.export_key().decode("utf-8")),
            OAUTH_CLIENT_ID, OAUTH_REDIRECT_URI, scope="", display="authorize",
        ),
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

    # 5. 完成认证（默认三个头，最终返回 JSON）。这一跳服务端可能 302（落点才是
    #    JSON），而请求自带的 csrf_token cookie 域名为空——对任意主机会一并带出。
    #    故**不**让 requests 自动跟随：自动跟随会在白名单校验之前就把令牌发往
    #    落点（SSRF 面），改为取 Location、先过宽松白名单、再发下一跳；判据与
    #    login_legacy 服务端下发跳转同一口径（require_trusted）。
    url = API_AUTH_URL
    resp = session.get(
        url,
        params={"verifyRequest": verify_code, "CSRF": csrf},
        allow_redirects=False,
        timeout=REQUEST_TIMEOUT,
    )
    policy.require_not_blocked(resp)
    for _ in range(MAX_FINAL_AUTH_REDIRECTS):
        if not resp.is_redirect:
            break
        location = resp.headers.get("Location", "")
        policy.require_trusted(location, "final_auth")
        resp = session.get(
            urljoin(getattr(resp, "url", "") or url, location),
            allow_redirects=False,
            timeout=REQUEST_TIMEOUT,
        )
        policy.require_not_blocked(resp)
    else:
        raise RuntimeError("最终认证重定向层数过多，疑似重定向环")
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"最终认证失败: {policy.sanitize(data.get('msg'))}")
    # code==0 只代表"签发方没有否认"，不代表签发发生过：成功签发附带的回执在 data
    # 载荷（同一端点的入口步应答即 `data.Data`，签到成功门也 code/data 并读）。
    # `data` 键缺失或为 null 即无签发信封形状——网关/降级层伪造 code:0 的假成功正落在
    # 这一形状上，不得写"登录成功"日志、更不得把残破会话送进缓存密文库。空容器
    # （`{}`）是录制到的真实成功形状之一，放行；判据只拒"无回执"。
    # 错误文案含 security.HARD_FAIL_TOKENS 词元——重试同一无回执应答必然同果，
    # 落不可重试档并联动清会话。
    if data.get("data") is None:
        _msg = data.get("msg")
        raise RuntimeError("最终认证失败: 无签发方回执（code=0 但 data 载荷缺失）"
                           + (f": msg={policy.sanitize(_msg)}" if _msg else ""))
    logger.info(f"[{policy.mask_account(phone)}] 登录成功")
    if session_store is not None:
        # 完整登录成功：保存会话缓存供下次免登录复用（失败仅告警，不影响签到）
        session_store.save(session, csrf)
    return LoginOutcome(csrf=csrf)


# ---------------------------------------------------------------------------
# 签到链
# ---------------------------------------------------------------------------
def fetch_sign_position(session, csrf, policy):
    """拉取签到任务（点位 + 时间窗）。返回 `SignResponse`。

    信封 `code == 999` 经库 `SessionExpired` 翻译为会话失效（清缓存重登）；
    成功信封里的配置过一次库解析（结构契约）。
    """
    resp = session.get(
        SIGN_POSITION_URL, params={"CSRF": csrf},
        allow_redirects=False, timeout=REQUEST_TIMEOUT,
    )
    if policy.is_blocked(resp):
        return SignResponse(data=None, blocked=True)
    data = _parse_api_body(resp)
    _validate_sign_position(data)
    return SignResponse(data=data)


def submit_sign_in(session, csrf, *, phone_code, phone_model, sign_info, out_state, policy):
    """提交一次签到。返回 `SignResponse`（`data` 为服务端返回体）。"""
    resp = session.post(
        SIGN_IN_URL,
        params={"CSRF": csrf},
        data=_sign_in_body(phone_code, phone_model, sign_info, out_state),
        allow_redirects=False,
        timeout=REQUEST_TIMEOUT,
    )
    if policy.is_blocked(resp):
        return SignResponse(data=None, blocked=True)
    return SignResponse(data=_parse_api_body(resp))
