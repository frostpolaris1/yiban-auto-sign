"""错误分类法（SPEC §1）。

解析不 speculate：输入结构未知时抛 :class:`ParseError`，携带出错的字段名与
简短片段摘要（截断 ≤120 字符）。唯一的例外是上游信封 ``code == 999``——其语义是
“会话已失效，调用方必须重登”，因此抛 :class:`SessionExpired` 而不是返回数据。
"""

from __future__ import annotations

from typing import Any

_FRAGMENT_LIMIT = 120


def _summarize(value: Any, limit: int = _FRAGMENT_LIMIT) -> str:
    """把 *value* 渲染成单行片段，并截断到 *limit* 字符。"""
    if isinstance(value, str):
        text = value
    else:
        try:
            text = repr(value)
        except Exception:  # pragma: no cover - 敌意 __repr__
            text = object.__repr__(value)
    text = text.replace("\r", "\\r").replace("\n", "\\n")
    if len(text) > limit:
        return text[:limit] + "\u2026"
    return text


class YibanProtocolError(Exception):
    """本库所有错误的基类。"""


class ParseError(YibanProtocolError):
    """输入不符合任何已知上游形态（结构性失败）。

    属性：
        field: 无法解析的字段/元素名。
        fragment: 已截断的出错输入摘要。
    """

    def __init__(self, field: str, fragment: Any = "") -> None:
        self.field = field
        self.fragment = _summarize(fragment)
        detail = f" ({self.fragment})" if self.fragment else ""
        super().__init__(f"cannot parse {field!r}{detail}")


class SessionExpired(YibanProtocolError):
    """信封 ``code == 999``：调用方必须重新认证。"""
