"""上游响应信封与挑战页检测（SPEC §2.3）。

上游会把 JSON 以 ``text/html`` 的 Content-Type 下发，因此解析只看响应体内容、
从不看头部。``code == 999`` 是唯一会抛异常的业务失败（``SessionExpired``），
其余一律作为数据返回。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .errors import ParseError, SessionExpired

# 命中任一即判定为疑似挑战页。
_CHALLENGE_MARKERS = ("ydclearance", "fengkongcloud")
# "captcha" 是弱信号：正常页面引用 captcha.js 之类也会命中子串——只在 HTML 上下文
# 里才算（与 acw_sc 同门槛），否则违背本函数"误报不可接受"的自我约束。
_WEAK_HTML_MARKERS = ("captcha",)
# 用于判定“响应体是 HTML”的标记。
_HTML_MARKERS = ("<html", "<!doctype", "<script")


@dataclass(frozen=True)
class Envelope:
    """通用 ``{"code": ..., "msg": ..., "data": ...}`` API 信封。"""

    code: object
    msg: str
    data: object
    raw: object


@dataclass(frozen=True)
class UsersureResult:
    """usersure 端点响应（扁平的、非信封式 JSON 对象）。"""

    ok: bool
    re_url: str | None
    code: str


def _loads(body: str | bytes, field_name: str) -> Any:
    """把 *body*（bytes 先按 utf-8 解码）交由 ``json.loads``，失败映射为 ParseError。"""
    if isinstance(body, (bytes, bytearray)):
        try:
            body = bytes(body).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ParseError(field_name, "<undecodable bytes>") from exc
    if not isinstance(body, str):
        raise ParseError(field_name, body)
    try:
        return json.loads(body)
    except ValueError as exc:
        raise ParseError(field_name, body) from exc


def parse_api_envelope(body: str | bytes) -> Envelope:
    """解析 ``api.uyiban.com`` 系列的 JSON 信封。

    异常:
        ParseError: *body* 不是 JSON 对象。
        SessionExpired: 信封 ``code`` 为 ``999``。
    """
    raw = _loads(body, "envelope")
    if not isinstance(raw, dict):
        raise ParseError("envelope", body)

    code = raw.get("code")
    if code == 999 or code == "999":
        msg = raw.get("msg")
        raise SessionExpired(msg if isinstance(msg, str) else "")

    msg = raw.get("msg")
    if not isinstance(msg, str):
        msg = "" if msg is None else str(msg)

    return Envelope(code=code, msg=msg, data=raw.get("data"), raw=raw)


def code_is_ok(raw_code: object) -> bool:
    """信封 ``code`` 是否为"成功"：数字 ``0`` 与字符串 ``"0"`` 都算成功。

    上游同一字段可能发数字也可能发数字字符串（SPEC §3.1；本文件 ``parse_api_envelope``
    对 ``999`` 已按双态收敛）。判据必须与解析层同一容忍度：严格 ``== 0`` 会把载荷完好的
    字符串 ``"0"`` 判成失败，落到 RISK/通用档打满重试（每次都是真实登录）。
    ``bool`` 不是成功码（Python 里 ``False == 0``，不显式排除就会把 ``false`` 判成功）。
    """
    if isinstance(raw_code, bool):
        return False
    return raw_code == 0 or raw_code == "0"


def parse_usersure_response(body: str | bytes) -> UsersureResult:
    """解析 usersure 响应。

    业务失败码（如 ``e001``）作为数据返回——只有结构性失败才抛异常。

    异常:
        ParseError: *body* 不是 JSON 对象。
    """
    raw = _loads(body, "usersure")
    if not isinstance(raw, dict):
        raise ParseError("usersure", body)

    raw_code = raw.get("code")
    code = raw_code if isinstance(raw_code, str) else ("" if raw_code is None else str(raw_code))

    re_url = raw.get("reUrl")
    if not isinstance(re_url, str) or not re_url:
        re_url = None

    return UsersureResult(ok=code == "s200", re_url=re_url, code=code)


def looks_like_challenge(body: str | bytes) -> bool:
    """尽力检测 WAF/风控挑战页——只检测，不求解。

    刻意保守：误报（把正常页当挑战）不可接受，漏报可接受。所有已知正常夹具
    都必须返回 ``False``。
    """
    if isinstance(body, (bytes, bytearray)):
        text = bytes(body).decode("utf-8", "replace")
    elif isinstance(body, str):
        text = body
    else:
        return False

    lowered = text.lower()
    if any(marker in lowered for marker in _CHALLENGE_MARKERS):
        return True
    html_context = any(marker in lowered for marker in _HTML_MARKERS)
    if not html_context:
        return False
    # 弱信号（`_WEAK_HTML_MARKERS`）与 `acw_sc` 同门槛：必须带 HTML 上下文
    return "acw_sc" in lowered or any(marker in lowered for marker in _WEAK_HTML_MARKERS)
