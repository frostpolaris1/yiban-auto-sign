# -*- coding: utf-8 -*-
"""安全策略层：WAF 拦截判定、URL 白名单、对外文本脱敏与失败现场诊断。

**为什么单独一层**：`yiban/fyiban/` 承接的是上游（AGPL-3.0）的协议与算法，而
"什么算被风控拦截""服务端下发的跳转目标能不能信""日志里哪些字段必须打码"是
**本项目自有的安全判断**。若把它们内联进第三方层，将来替换或升级上游实现时会
连带丢掉这些保护。因此本模块提供策略**实现**，由 `yiban.client` 组装后注入协议层
（契约见 `yiban/fyiban/protocol.py` 的 `RequestPolicy`）。

三个判定的口径（由 `tests/test_login_protocol_shape.py` 与
`tests/test_fyiban_isolation.py` 钉住）：

- `is_yiban_trusted_url` —— **宽松**白名单：登录链路要跟随服务端下发的跳转，
  只放行 `yiban.cn` / `uyiban.com` 体系的 https 链接，防服务端被劫持时把登录态导流；
- `is_fyiban_url` —— **严格**白名单：ydclearance 挑战页吐出的跳转目标，主机必须精确
  等于 `f.yiban.cn`；
- `is_waf_blocked` —— 只在**短响应**里找拦截关键词，避免把含"风控""拦截"字样的
  正常法律文本误判成拦截页。
"""
import logging
import re
from urllib.parse import urlsplit

from yiban import masking

logger = logging.getLogger("yiban.security")

# WAF 拦截页特征词（易班返回 JSON 时中文会被转义成 \uXXXX，检测前先解码）
WAF_KEYWORDS = ["风险访问", "风控", "访问服务禁用", "WAF", "拦截"]

# 被风控拦截时的统一对外文案（多处使用，文案变更必须同一处改）
WAF_BLOCKED_MESSAGE = "请求被 WAF 风控拦截，请配置 YIBAN_PROXY 代理后重试"

# 白名单失败文案按"协议步骤"定位：说清是哪一步的 URL 不合格，管理员才能判断是
# 服务端被劫持、还是我们的解析出了偏差。
_WHITELIST_MESSAGES = {
    "login_entry": "登录入口 URL 不在白名单",
    "login_reurl": "登录 reUrl 不在白名单",
    "verify_request": "verify_request 跳转不在白名单",
    "ydclearance": "ydclearance 跳转目标不在白名单",
}


def is_waf_blocked(response_text):
    """判断响应是否为 WAF 风控拦截。

    WAF 拦截页通常很短（< 2000 字符），而正常页面（OAuth 授权页、服务协议等）
    内容较长且可能包含"风控""拦截"等正常法律文本，故仅在响应较短时才检测关键词。

    易班 WAF 返回 JSON 时中文会被 Unicode 转义（如 \\u98ce\\u9669 = "风险"），
    需先解码再匹配。
    """
    if len(response_text) > 2000:
        return False
    # 解码 \uXXXX 形式的 Unicode 转义序列后一并检测
    decoded = re.compile(r"\\u([0-9a-fA-F]{4})").sub(
        lambda m: chr(int(m.group(1), 16)), response_text
    )
    return any(keyword in response_text or keyword in decoded for keyword in WAF_KEYWORDS)


def is_yiban_trusted_url(url):
    """宽松白名单（纵深防御）：仅放行 yiban.cn / uyiban.com 体系的 https 链接。

    登录流程跟随服务端可控的跳转 URL（OAuth Data/reUrl/Location）——
    这些 URL 来自易班服务端自身，信任链成立，但与 ydclearance 分支的
    严格白名单口径不一致；此校验兜底防服务端被劫持时把登录态导流到任意域。
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    host = parts.hostname or ""
    return (
        parts.scheme == "https"
        and (host == "yiban.cn" or host.endswith(".yiban.cn")
             or host == "uyiban.com" or host.endswith(".uyiban.com"))
        and parts.username is None
    )


def is_fyiban_url(url):
    """严格校验易班跳转 URL：https + 主机精确为 f.yiban.cn + 不允许 userinfo。

    使用 urlsplit 避免 `https://f.yiban.cn.evil.com` 或
    `https://f.yiban.cn@evil.com` 这类前缀/userinfo 绕过。
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return (
        parts.scheme == "https"
        and parts.hostname == "f.yiban.cn"
        and parts.username is None
    )


def url_desc(url):
    """只保留 scheme://host[:port]，避免把 query/token/userinfo 带进日志。"""
    try:
        parts = urlsplit(url)
        if parts.hostname:
            desc = f"{parts.scheme}://{parts.hostname}"
            if parts.port:
                desc += f":{parts.port}"
            return desc
    except ValueError:
        pass
    return "<无法解析>"


def location_desc(location):
    """302 Location 进错误消息前的脱敏描述：只留 scheme://host[:port]/path。

    query 里可能带 verify_request 令牌，userinfo 更是直接的凭据，两者都不能进日志
    ——所以**不能**保留 `netloc`（那会把 `https://user:pass@host/` 的凭据一起写出去）。
    """
    try:
        parts = urlsplit(location or "")
    except ValueError:
        return "<无法解析>"
    if not parts.scheme:
        return parts.path
    host = parts.hostname or ""
    desc = f"{parts.scheme}://{host}"
    if parts.port:
        desc += f":{parts.port}"
    return desc + parts.path


class ProtocolPolicy:
    """注入给协议层（`yiban/fyiban/protocol.py`）的策略实现。

    协议层只描述"平台要求怎么做"，**所有**判定与措辞都回到这里：它能看到的
    只有 URL、响应对象与文本，不自己做域名比对，也不自己拼日志文案。
    """

    #: 严格白名单判定函数，供挑战解析器（`fyiban.waf`）注入使用
    allow_fyiban_url = staticmethod(is_fyiban_url)

    # ---- 白名单 ----
    def require_trusted(self, url, site):
        """宽松白名单校验；不合格则抛错并指明步骤。"""
        if is_yiban_trusted_url(url):
            return
        message = _WHITELIST_MESSAGES.get(site, "跳转 URL 不在白名单")
        if site == "login_entry":
            # 入口 URL 只落 scheme://host[:port]：它的 query 可能带 OAuth 参数
            message = f"{message}: {url_desc(str(url))}"
        raise RuntimeError(message)

    def require_fyiban(self, url, site="ydclearance"):
        """严格白名单校验（挑战页跳转目标）。"""
        if not is_fyiban_url(url):
            raise RuntimeError(_WHITELIST_MESSAGES.get(site, "跳转 URL 不在白名单"))

    def is_logged_in_redirect(self, location):
        """302 Location 是否指向"已登录"标识页（`f.yiban.cn/iapp7463`，允许 query）。

        只认 host == f.yiban.cn 且 path == /iapp7463：恶意 host（evil.example/iapp7463）
        与子域伪装（f.yiban.cn.evil.com/x?iapp7463）都不能置登录态——否则"登录失败"
        会被误判成"已登录"短路，可诊断信号退化成通用失败。
        """
        try:
            parts = urlsplit(location or "")
        except ValueError:
            return False
        return parts.hostname == "f.yiban.cn" and parts.path == "/iapp7463"

    def require_redir_chain_trusted(self, resp, site):
        """跟随重定向后校验整条链都在白名单内（最终 URL + 每一跳 history）。

        只校验首跳不足够：`allow_redirects=True` 时中间人可以把白名单内主机 302 到
        白名单外，RSA 公钥取自最终落点 HTML——逐跳校验是纵深缺口（M8）。格式非法
        的 URL 一律判不合格（不碰 history 之外的东西）。
        """
        hops = [getattr(r, "url", "") for r in getattr(resp, "history", []) or []]
        hops.append(getattr(resp, "url", "") or "")
        for u in hops:
            if not is_yiban_trusted_url(u):
                raise RuntimeError(_WHITELIST_MESSAGES.get(site, "跳转 URL 不在白名单"))

    # ---- WAF 拦截 ----
    def is_blocked(self, resp):
        """该响应是否被 WAF 风控拦截（不抛错，供需要自行降级为状态码的调用方使用）。"""
        return is_waf_blocked(getattr(resp, "text", "") or "")

    def require_not_blocked(self, resp):
        """被风控拦截即抛错——登录链路里没有"忽略拦截继续跑"的余地。"""
        if self.is_blocked(resp):
            raise RuntimeError(WAF_BLOCKED_MESSAGE)

    # ---- 脱敏与失败现场诊断 ----
    def sanitize(self, text):
        """对外文本脱敏（换行转义 + 抹掉可能内嵌的凭据字面量）。"""
        return masking.sanitize_text(text)

    def mask_account(self, phone):
        """账号标识（手机号）入日志与错误消息前脱敏。`mask_phone` 幂等，重复打码无副作用。"""
        return masking.mask_phone(phone)

    def describe_location(self, location):
        """302 Location 的诊断描述（脱敏：去 query 与 userinfo）。"""
        return location_desc(location)

    def log_response_diagnostics(self, phone, resp, *, stage, hits=None, advice=True):
        """把"响应不符合预期"的现场按脱敏口径落日志。

        诊断要看的是**形状**（最终 URL 的 host/path、状态码、长度、前 300 字符、
        正则命中数），而不是整页 HTML：整页既可能含账号信息又不便阅读。
        """
        text = getattr(resp, "text", "") or ""
        logger.error(f"[{masking.mask_phone(phone)}] {stage}诊断:")
        logger.error(f"  最终 URL: {masking.sanitize_url(getattr(resp, 'url', '') or '')}")
        logger.error(f"  状态码: {getattr(resp, 'status_code', '')}")
        logger.error(f"  响应长度: {len(text)}")
        logger.error(f"  响应前300字符: {self.sanitize(text[:300].replace(chr(10), chr(92) + 'n'))}")
        if hits:
            logger.error("  " + ", ".join(f"{name} 命中: {count}" for name, count in hits.items()))
        if self.is_blocked(resp):
            logger.error("  检测到 WAF 风控拦截特征，通常是 GitHub Actions 海外 IP 被易班风控")
        if advice:
            logger.error(
                "  若响应为 WAF 挑战页/拦截页，通常是 GitHub Actions 海外 IP 被易班风控，"
                "请配置 YIBAN_PROXY 代理后重试。"
            )
