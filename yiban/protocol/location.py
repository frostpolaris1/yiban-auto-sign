"""从 302 响应的 ``Location`` 头提取 ``verify_request`` 令牌（SPEC §2.2）。

令牌可能位于 query 末位、其后没有 ``&``，因此正则不得要求后随分隔符。取不到
令牌**不是**本模块的错误：由调用方决定其语义。
"""

from __future__ import annotations

import re

_VERIFY_REQUEST_RE = re.compile(r"verify_request=([^&]+)")


def extract_verify_request(location: str) -> str | None:
    """返回 *location* 中原始的 ``verify_request`` 令牌；取不到返回 ``None``。

    令牌本身是 hex，无需 URL 解码，原样返回匹配文本。
    """
    if not isinstance(location, str) or not location:
        return None
    match = _VERIFY_REQUEST_RE.search(location)
    if match is None:
        return None
    return match.group(1)
