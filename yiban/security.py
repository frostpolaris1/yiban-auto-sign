# -*- coding: utf-8 -*-
"""**功能**
安全策略层：WAF 拦截判定、URL 白名单、对外文本脱敏与失败现场诊断。

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
  等于 `f.yiban.cn`（求解器删除后仅作为白名单边界测试与历史契约保留）；
- `is_waf_blocked` —— **挑战形态**（`yiban/fyiban/waf.looks_like_challenge` 的特征并集）不受
  长度限制一律判拦；**仅关键词**命中才按**短响应**设界，避免把含"风控""拦截"字样的
  正常法律文本误判成拦截页。关键词支按**边界口径**匹配：ASCII 词元两侧必须都不是字母或
  数字（`aWAFb` 不命中），中文词元维持子串——完整口径见词元定义处的上方注释。
- `HARD_FAIL_TOKENS` / `is_hard_fail_message` / `hard_fail_pattern` —— 失败分类的**唯一
  真值源**（挑战解析/白名单/非 JSON 响应/无签发方回执四类确定性失败），`engine.attempts`
  （重试档位）与 `engine.probe`（硬失败预警）都从这里取，全仓不得出现第二份手抄清单。
  两族词元走同一条匹配规则（`_keyword_alternation`）：同一输入不会得出两种判据。

**归属**
`yiban` 包根的安全策略实现层，服务第三方隔离层：`yiban.client` 把本模块的函数组装成
`RequestPolicy` 注入 `yiban/fyiban/protocol.py`，故本模块是"本项目自有安全判断"与
"上游协议知识"的分界线。

**复用**
`is_yiban_trusted_url` / `is_fyiban_url` / `is_waf_blocked` 与
`WAF_BLOCKED_MESSAGE`、`_WHITELIST_MESSAGES`（对外文案单一来源）；失败分类的档位词元
（`HARD_FAIL_TOKENS`/`is_hard_fail_message`/`hard_fail_pattern`）与 `WAF_KEYWORDS` 同在本模块，
是重试档位（`yiban.engine.attempts`）与探针硬失败判据（`yiban.engine.probe`）的**唯一真值源**；
WAF 词元的命中判定 `matches_waf_keywords` 由 `attempts` 的风控族判据复用（那边喂的是失败**消息**，
不受"短响应"上界约束）；挑战形态特征复用 `yiban.fyiban.waf.looks_like_challenge`（不另抄特征串）；
脱敏口径复用 `yiban.masking`。

**通信**
输入：URL、响应文本/头部/状态码、待脱敏文本。输出：布尔判定或脱敏/诊断后的文本。
调用谁：`yiban.masking`、`urlsplit`、`yiban.fyiban.waf`（挑战形态特征，单向依赖：隔离层
不得反向 import 本模块）。
谁调用：`yiban.client`（组装 `RequestPolicy` 注入协议层）、`yiban/fyiban/protocol.py`
经注入的策略回调、`yiban/engine/attempts.py` 与 `yiban/engine/probe.py`（档位词元单一来源）、
以及各日志/错误消息点。
前端调用点：无直接调用点；本模块的结果经登录/签到错误消息（最终进入签到日志与
`/api/my-logs`、`/api/admin/sign-events` 页面）间接可见——白名单/脱敏口径变化会改变
这些页面的错误文案与打码效果。
"""
import logging
import re
from urllib.parse import urlsplit

from yiban import masking
from yiban.fyiban import waf as fyiban_waf

logger = logging.getLogger("yiban.security")

# WAF 拦截页特征词（易班返回 JSON 时中文会被转义成 \uXXXX，检测前先解码）。
#
# 匹配口径（工单 `yiban-auto-sign-u21x`）：**含 ASCII 字母或数字的词元，两侧必须都不
# 是字母或数字才算命中**；纯中文词元按子串匹配。依据两条：
# - base64 与 `\uXXXX` 转义文本能拼出三连续 `WAF`（实测随机 RSA 公钥 PEM 撞出它，
#   嵌这枚 PEM 的用例整类被判"被风控拦截"），而 `aWAFb` 不是任何拦截页的写法；
# - 中文四枚（"风险访问"/"风控"/"访问服务禁用"/"拦截"）在 base64 与转义文本里造不出来，
#   误报面为零，维持子串匹配即最省事的形态，也不再收紧（收紧它们只会漏判）。
WAF_KEYWORDS = ["风险访问", "风控", "访问服务禁用", "WAF", "拦截"]

# 被风控拦截时的统一对外文案（多处使用，文案变更必须同一处改）
WAF_BLOCKED_MESSAGE = "请求被 WAF 风控拦截，请配置 YIBAN_PROXY 代理后重试"

# 硬失败词元（失败分类的**唯一真值源**，重试档位与探针判据都从这里取）：
# - "ydclearance"：挑战检测命中的响亮失败文案（`yiban/fyiban/waf.CHALLENGE_DETECTED_MESSAGE`
#   的前缀词元；求解器已按既定裁决删除，检测命中即失败）与挑战跳转白名单拒绝——同一输入
#   必然得出同一结果，重试只会把同一死页重发；会话残片停在未通过的挑战链上，没有复用价值
#   （attempts 据此联动清缓存）。
# - "Expecting value"：requests 对非 JSON 响应调 .json() 的固定报错开头——JSON 期望
#   端点返回了整页 HTML（典型为漏过关键词检测的长拦截页），属响应形状问题，与凭据和
#   网络瞬断都无关，同样重试无用。
# - "无签发方回执"：最终认证应答 code==0 但缺 data 载荷（协议层的签发回执判据）——
#   重发同一请求只会再拿到同一份无回执应答，且会话残破没有复用价值，与上两类同档。
# 这些消息是 `waf.py`/requests/`protocol.py` 的 raise **输出**，本表按词元匹配、
# 不复制文案全文；匹配规则与 WAF 族同一条（见 `WAF_KEYWORDS` 上方口径）——两条腿共用
# `_keyword_alternation`，档位判据与探针判据对同一输入同真同假。
# 新增解析失败路径只要消息仍含词元即自动入档（词元变更须与产生方同批核对）。
HARD_FAIL_TOKENS = ("ydclearance", "Expecting value", "无签发方回执")

_ALNUM_RE = re.compile(r"[0-9A-Za-z]")
_UNICODE_ESCAPE_RE = re.compile(r"\\u([0-9a-fA-F]{4})")


def _token_source(token):
    """词元的匹配正则片段：含 ASCII 字母/数字者要求两侧非字母数字，纯中文者按子串。"""
    escaped = re.escape(token)
    if _ALNUM_RE.search(token):
        return rf"(?<![0-9A-Za-z]){escaped}(?![0-9A-Za-z])"
    return escaped


def _keyword_alternation(tokens):
    """词元表 → 一条非捕获交替式（名单一份、规则一份，两者不分开演化）。"""
    return "|".join(f"(?:{_token_source(token)})" for token in tokens)


#: 词元表在**导入期**编译成的匹配式（名单唯一、规则唯一；运行时不改名册）。
WAF_KEYWORD_RE = re.compile(_keyword_alternation(WAF_KEYWORDS))
#: `HARD_FAIL_TOKENS` 的编译形态（同上）。
HARD_FAIL_RE = re.compile(_keyword_alternation(HARD_FAIL_TOKENS))


def _decode_unicode_escapes(text):
    r"""把 `\uXXXX` 转义还原成字符（易班 JSON 响应的中文在转义里，匹配前必须先解）。"""
    return _UNICODE_ESCAPE_RE.sub(lambda m: chr(int(m.group(1), 16)), text)


def matches_waf_keywords(text):
    """文本是否含 WAF 词元（**不设长度上界**；按词元表的边界口径匹配）。

    长度上界是 `is_waf_blocked` 对**响应体**的额外防误伤边界，不属词元判据本身；
    重试档位与执行体风控信号喂进来的是失败**消息**（可以很长），所以那条腿要的是
    本函数的无界判定。
    """
    if not text:
        return False
    if WAF_KEYWORD_RE.search(text):
        return True
    return bool(WAF_KEYWORD_RE.search(_decode_unicode_escapes(text)))


def is_hard_fail_message(message):
    """该失败消息是否属"确定性硬失败"（挑战解析/白名单/非 JSON/无回执）——档位判据的唯一入口。"""
    return bool(HARD_FAIL_RE.search(message))


def hard_fail_pattern():
    """风控/硬失败家族的**正则片段源文**（`engine.probe` 构造硬失败判据用）。

    探针与档位必须共享同一批词元（`WAF_KEYWORDS` + `HARD_FAIL_TOKENS`）——探针此前
    手抄了一份不含解析失败的词表，导致该族失败对探针零预警。两族走同一条边界规则
    （`_keyword_alternation`），所以本式与 `is_hard_fail_message` 不会出现同输入两判据。
    """
    return _keyword_alternation((*WAF_KEYWORDS, *HARD_FAIL_TOKENS))


# 白名单失败文案按"协议步骤"定位：说清是哪一步的 URL 不合格，管理员才能判断是
# 服务端被劫持、还是我们的解析出了偏差。
_WHITELIST_MESSAGES = {
    "login_entry": "登录入口 URL 不在白名单",
    "login_reurl": "登录 reUrl 不在白名单",
    "verify_request": "verify_request 跳转不在白名单",
    "final_auth": "最终认证跳转不在白名单",
}


def is_waf_blocked(response_text):
    """判断响应是否为 WAF 风控拦截。

    两条判据、两种长度口径：
    - **形态**判据（`waf.looks_like_challenge` 的双 JS 特征）不受长度限制：挑战/拦截页是
      平台产物，其形状就是判据本身；真实拦截页可以很长，旧"`len>2000` 一律不判"的短路
      失效方向是 fail-open（长拦截页被放行、被当「网络抖动」打满重试）。
    - **关键词**判据只在短响应里找：正常长文（服务协议、法律文本）合法含"风控""拦截"
      字样，防误伤的长度上界照旧保留。词元的匹配口径见词元定义处的上方注释——
      ASCII 词元必须两侧非字母数字才算命中，否则 base64 响应体会被误判成拦截页。

    易班 WAF 返回 JSON 时中文会被 Unicode 转义（如 \\u98ce\\u9669 = "风险"），
    需先解码再匹配（`matches_waf_keywords` 内部完成）。
    """
    if fyiban_waf.looks_like_challenge(response_text):
        return True
    if len(response_text) > 2000:
        return False
    return matches_waf_keywords(response_text)


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
