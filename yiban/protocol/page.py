"""授权页解析（SPEC §2.1）。

从 OAuth 授权页中提取两样东西：

* ``var page_use = '<token>';``——页面一次性令牌，后续作为 usersure 请求的
  ``ajax_sign`` query 参数；
* 隐藏表单项 ``<input id="key" ...>`` 的 ``value``，内含多行 PEM 形式的 RSA 公钥。
  上游把该 input 的 ``type`` 写成了 ``"test"``，因此**严格靠 ``id="key"`` 定位**，
  绝不依赖 ``type``。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .errors import ParseError

# page_use 令牌只允许字母数字，单双引号都要容。
_PAGE_USE_RE = re.compile(r"""var\s+page_use\s*=\s*['"]([A-Za-z0-9]+)['"]""")
# 定位 id="key" 的 input 标签（PEM 属性值含真实换行/无 '>'，故 [^>] 配合 DOTALL 可行）。
_KEY_INPUT_RE = re.compile(r"""<input\b[^>]*\bid\s*=\s*['"]key['"][^>]*>""", re.IGNORECASE | re.DOTALL)
# 在标签文本内取 value 属性（非贪婪，PEM 内部无引号）。
_VALUE_ATTR_RE = re.compile(r"""\bvalue\s*=\s*['"](.*?)['"]""", re.DOTALL)


@dataclass(frozen=True)
class AuthorizePage:
    """从授权页提取出的值。"""

    page_use: str
    public_key_pem: str


def _normalize_pem(raw: str) -> str:
    """把嵌在 HTML 属性里的 PEM 归一化为规范的单行折叠文本。

    保留头尾行，中间 base64 行以 ``\\n`` 连接，去掉首尾空白与 CRLF。
    """
    lines = [line.strip() for line in raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    return "\n".join(line for line in lines if line)


def parse_authorize_page(html: str) -> AuthorizePage:
    """把授权页 HTML 解析为 :class:`AuthorizePage`。

    异常:
        ParseError: 找不到 ``page_use`` 令牌或 ``id="key"`` 公钥输入。
    """
    if isinstance(html, (bytes, bytearray)):
        html = bytes(html).decode("utf-8", "replace")
    if not isinstance(html, str):
        raise ParseError("page_use", html)

    page_use_match = _PAGE_USE_RE.search(html)
    if page_use_match is None or not page_use_match.group(1):
        raise ParseError("page_use", html)

    key_input_match = _KEY_INPUT_RE.search(html)
    if key_input_match is None:
        raise ParseError("public_key", html)

    value_match = _VALUE_ATTR_RE.search(key_input_match.group(0))
    if value_match is None:
        raise ParseError("public_key", key_input_match.group(0))

    pem = _normalize_pem(value_match.group(1))
    if "-----BEGIN PUBLIC KEY-----" not in pem or "-----END PUBLIC KEY-----" not in pem:
        raise ParseError("public_key", pem)

    return AuthorizePage(page_use=page_use_match.group(1), public_key_pem=pem)
