# -*- coding: utf-8 -*-
"""对外输出脱敏：手机号 / 进入日志与通知的文本、URL。

三类数据一旦进入日志、通知或页面展示就不再受控（日志被转发、通知走 webhook、
页面被截图），故统一在这里收口：

- `sanitize_text`：服务端可控内容（异常消息、上游返回）落日志/通知前转义换行并
  抹掉可能内嵌的凭据字面量——防日志注入与凭据泄露；
- `sanitize_url`：URL 入日志前对 query 里的凭据类参数打码（OAuth code / CSRF /
  session 标识 / 未知高熵令牌）；
- `mask_phone`：11 位手机号 → `138****8000`。

**本地日志文件仍保留完整号**（排查需要），脱敏用在对外通道（webhook、页面展示）
与「日志页」的展示层——这是既有口径，移动实现不改行为。

⚠ `mask_email` 仍留在 `web/app.py`：它与 signin 侧的邮箱脱敏公式不同，合并会改变
用户可见输出，须与前端展示口径一起改，不在此处顺手统一。
"""
import re
from urllib.parse import parse_qsl, urlsplit, urlunsplit


def sanitize_text(text):
    """服务端可控内容进入错误消息/日志/通知前转义换行与回车，防止日志与通知注入。"""
    s = str(text).replace("\r", "\\r").replace("\n", "\\n")
    # 脱敏：异常消息可能含 Account dataclass repr（含明文密码/令牌）
    # 整体替换 Account(...) 对象（正则处理引号转义边界），并兜底替换 password/phone_code 字段
    s = re.sub(r"Account\([^)]*\)", "Account(***)", s)
    s = re.sub(r"password\s*=\s*['\"][^'\"]*['\"]", "password='***'", s)
    s = re.sub(r"phone_code\s*=\s*['\"][^'\"]*['\"]", "phone_code='***'", s)
    # dict/repr 形态兜底（C-SIGN-03）：'phone_code': 'xxx' / "password": "xxx"——
    # kwarg 形态正则覆盖不到 dict repr（如 vars()/json.dumps 调试输出进异常链）
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


# URL query 敏感参数名片段（子串、不区分大小写匹配）：OAuth code/token、CSRF/session
# 标识、签名票据类——最终 URL 进诊断日志前值统一打码（C-SIGN-01）
_URL_SENSITIVE_KEY_PARTS = (
    "code", "token", "csrf", "session", "ticket", "sign",
    "auth", "key", "secret", "passwd", "password", "verify",
)


def sanitize_url(url):
    """URL 入日志前对 query 敏感参数脱敏（C-SIGN-01）。

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
