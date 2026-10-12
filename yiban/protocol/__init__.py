"""协议库（洁净室）：易班网页 OAuth + nightAttendance 链路的**纯解析/构造**。

只做解析（bytes/dict → 结构化数据）与构造（结构化参数 → 请求体/URL）。无网络、
无会话管理、无重试、无日志、无全局状态。

**来源与许可**：本包是洁净室实现，唯一规格来源是第一手旁路实拍与端点响应观察；
实现过程未参考任何第三方易班项目源码。**MIT 许可**（版权与许可全文见同目录
`LICENSE`；分发与再分发都必须随代码保留它）。主仓其余部分为 AGPL-3.0——两段
许可各自约束自己的文件。

**单一源**：本包原先与一个独立库同源，纪律是"逐字节同步覆盖、禁止在本仓修改"。
本批把本包提升为主仓的一等模块，该纪律作废：**本仓即单一源**，可直接在本仓改本包，
修改记录以本仓提交为准。独立库的归档由负责人在本批合入后执行；归档与否都不改变
本仓的单一源地位。

``crypto.encrypt_password`` 位于可选附加件之后，经 :func:`__getattr__`
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
    "App",
    "AuthorizePage",
    "Envelope",
    "Identity",
    "ParseError",
    "Position",
    "SessionExpired",
    "SignPositionConfig",
    "TimeWindow",
    "UsersureResult",
    "YibanProtocolError",
    "__version__",
    "build_sign_in_body",
    "build_usersure_form",
    "extract_verify_request",
    "looks_like_challenge",
    "parse_api_envelope",
    "parse_authorize_page",
    "parse_identity",
    "parse_sign_position",
    "parse_usersure_response",
]


def __getattr__(name: str) -> Any:
    """惰性暴露 ``encrypt_password``，避免核心导入时载入可选 crypto extra。"""
    if name == "encrypt_password":
        from . import crypto

        return crypto.encrypt_password
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
