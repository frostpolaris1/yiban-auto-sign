# -*- coding: utf-8 -*-
"""对外输出脱敏：手机号、进入日志与通知的文本、URL。

三类数据一旦进入日志、通知或页面展示就不再受控（日志被转发、通知走 webhook、
页面被截图），故统一在这里收口：

- `sanitize_text`：服务端可控内容（异常消息、上游返回）落日志/通知前转义换行并
  抹掉可能内嵌的凭据字面量——防日志注入与凭据泄露；
- `sanitize_url`：URL 入日志前对 query 里的凭据类参数打码（OAuth code / CSRF /
  session 标识 / 未知高熵令牌）；
- `mask_phone`：11 位手机号 → `138****8000`。

**本地日志文件仍保留完整号**（排查需要），脱敏只用在对外通道（webhook、页面展示）
与「日志页」的展示层。

⚠ `mask_email` 不在这里：它与 signin 侧的邮箱脱敏公式不同，合并会改变用户可见输出，
须与前端展示口径一起改。
"""
import re
from urllib.parse import parse_qsl, urlsplit, urlunsplit

# URL query 敏感参数名片段（子串、不区分大小写匹配）：OAuth code/token、CSRF/session
# 标识、签名票据类——最终 URL 进诊断日志前值统一打码
_URL_SENSITIVE_KEY_PARTS = (
    "code", "token", "csrf", "session", "ticket", "sign",
    "auth", "key", "secret", "passwd", "password", "verify",
)


def sanitize_text(text):
    """服务端可控内容进入错误消息/日志/通知前转义换行与回车，防止日志与通知注入。"""
    s = str(text).replace("\r", "\\r").replace("\n", "\\n")
    # 异常消息可能含 Account dataclass repr（带明文密码/令牌）：
    # 整体替换 Account(...) 对象（正则处理引号转义边界），并兜底替换命名字段
    s = re.sub(r"Account\([^)]*\)", "Account(***)", s)
    s = re.sub(r"password\s*=\s*['\"][^'\"]*['\"]", "password='***'", s)
    s = re.sub(r"phone_code\s*=\s*['\"][^'\"]*['\"]", "phone_code='***'", s)
    # dict/repr 形态兜底：上面的 kwarg 正则覆盖不到 'password': 'xxx' /
    # "phone_code": "xxx"（vars()/json.dumps 调试输出进异常链时会这样出现）
    s = re.sub(r"(['\"])password\1\s*:\s*['\"][^'\"]*['\"]", r"\1password\1: '***'", s)
    s = re.sub(r"(['\"])phone_code\1\s*:\s*['\"][^'\"]*['\"]", r"\1phone_code\1: '***'", s)
    return s


def mask_phone(phone):
    """11 位手机号 → 138****8000；已脱敏（含 `*`）或非 11 位原样返回（**幂等**）。

    幂等是有意的：同一串可能被判据链上多处脱敏（日志页展示层再脱敏一次），
    不幂等会把 `138****8000` 二次打码成 `138*****8000` 之类的畸形串。
    """
    p = str(phone)
    if "*" in p:
        return p
    return p[:3] + "****" + p[7:] if len(p) == 11 else p


def sanitize_url(url):
    """URL 入日志前对 query 敏感参数脱敏。

    诊断日志需要的是 scheme/host/path 与"带了哪些参数"，不是参数值：可能携带
    凭据的（OAuth code、CSRF、session 标识等）一律替换为 ***；≥24 位连续
    URL-safe 字符的高熵值无论参数名一律打码，兜底未知令牌参数名（阈值取 24：
    真实 code/token 通常远长于此，避免误伤 client_id 这类恰好 16 位的公开标识）。
    解析失败返回占位符，绝不抛异常影响主流程。
    """
    raw = str(url)
    try:
        parts = urlsplit(raw)
        pairs = parse_qsl(parts.query, keep_blank_values=True)
    except ValueError:
        return "<url 解析失败已省略>"
    if not pairs:
        return raw

    def _masked(key, value):
        k = key.lower()
        if any(part in k for part in _URL_SENSITIVE_KEY_PARTS):
            return f"{key}=***"
        if len(value) >= 24 and re.fullmatch(r"[A-Za-z0-9_\-]+", value):
            return f"{key}=***"
        return f"{key}={value}"

    return urlunsplit(parts._replace(query="&".join(_masked(k, v) for k, v in pairs)))

#: URL 里的 userinfo（`scheme://user:pass@host`）——代理串按契约允许带凭据，
#: 而 `sanitize_url` 只管 query 参数，**不碰 userinfo**，故单独一个口径。
_URL_USERINFO_RE = re.compile(r"(?<=://)[^/?#\s]*@")


def mask_url_userinfo(text):
    """把 URL 里的 `user:pass@` 抹成 `***@`，其余原样——供两处共用：

    1. **必须回显用户输入的场合**（如"代理地址格式不正确: <你填的值>"）：回显能帮用户
       定位问题，但凭据绝不能进错误文案 / DOM / 日志；
    2. **异常消息落日志**（requests 的异常文本会内嵌完整 URL，含代理 userinfo）。

    无 userinfo 时原样返回；无 scheme 的裸写法（`user:pass@host:port`）也一并抹掉。
    """
    raw = str(text)
    out = _URL_USERINFO_RE.sub("***@", raw)
    if "://" not in out:
        out = re.sub(r"^[^@\s/]*@", "***@", out)
    return out
