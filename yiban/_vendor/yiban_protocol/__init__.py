"""yiban-protocol——易班网页 OAuth + nightAttendance 链路的洁净室解析/构造库。

只做解析（bytes/dict → 结构化数据）与构造（结构化参数 → 请求体/URL）。无网络、
无会话管理、无重试、无日志、无全局状态。

``crypto.encrypt_password`` 位于可选 ``[crypto]`` extra 之后，经 :func:`__getattr__`
惰性暴露，因此导入核心包永不依赖第三方包。
"""

from __future__ import annotations

from typing import Any

from .envelopes import (
    Envelope,
    UsersureResult,
    looks_like_challenge,
    parse_api_envelope,
    parse_usersure_response,
)
from .errors import ParseError, SessionExpired, YibanProtocolError
from .forms import build_sign_in_body, build_usersure_form
from .identity import App, Identity, parse_identity
from .location import extract_verify_request
from .page import AuthorizePage, parse_authorize_page
from .position import Position, SignPositionConfig, TimeWindow, parse_sign_position

__version__ = "0.1.0"

__all__ = [
    "__version__",
    "YibanProtocolError",
    "ParseError",
    "SessionExpired",
    "AuthorizePage",
    "parse_authorize_page",
    "extract_verify_request",
    "Envelope",
    "parse_api_envelope",
    "UsersureResult",
    "parse_usersure_response",
    "looks_like_challenge",
    "App",
    "Identity",
    "parse_identity",
    "Position",
    "TimeWindow",
    "SignPositionConfig",
    "parse_sign_position",
    "build_usersure_form",
    "build_sign_in_body",
]


def __getattr__(name: str) -> Any:
    """惰性暴露 ``encrypt_password``，避免核心导入时载入可选 crypto extra。"""
    if name == "encrypt_password":
        from . import crypto

        return crypto.encrypt_password
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
