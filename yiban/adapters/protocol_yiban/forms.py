"""请求体/URL 构造（SPEC §2.6）。

纯构造函数：无 I/O、无会话状态。输出恒为 ``application/x-www-form-urlencoded``
字符串。**两种编码语义并存，各自对齐各自的上游**：

- ``build_usersure_form``：``quote_plus``（空格 → ``+``）——上游网页登录页的形态，
  **未逐字节核过**（该页不是 SPA，没有可对照的自写请求包装器）。
- ``build_sign_in_body``：``encodeURIComponent``（空格 → ``%20``、``!'()*`` 不转义、
  ``/`` 转义）＋ ``JSON.stringify`` 的紧凑 JSON——对齐 H5 SPA（2026-10-18）。
"""

from __future__ import annotations

import json
from urllib.parse import quote, urlencode


def build_usersure_form(
    phone: str,
    encrypted_password: str,
    client_id: str,
    redirect_uri: str,
    *,
    display: str = "authorize",
    scope: str | None = None,
) -> str:
    """构造 usersure 的 POST 请求体。

    字段顺序与观测流程一致：``oauth_uname``（明文手机号）、``oauth_upwd``（密文）、
    ``client_id``、``redirect_uri``、``display``；``scope`` 仅在显式传入时追加。
    """
    pairs: list[tuple[str, str]] = [
        ("oauth_uname", phone),
        ("oauth_upwd", encrypted_password),
        ("client_id", client_id),
        ("redirect_uri", redirect_uri),
        ("display", display),
    ]
    if scope is not None:
        pairs.append(("scope", scope))
    return urlencode(pairs)


def build_sign_in_body(
    *,
    lnglat: tuple[float, float],
    address: str,
    out_state: int = 1,
    reason: str = "",
    attachment_file_name: str = "",
    code: str = "",
    phone_model: str = "",
) -> str:
    """构造 nightAttendance 签到 POST 请求体。

    字段集与顺序对齐**参考客户端**（H5 SPA）。顶层五字段
    ``AttachmentFileName, OutState, Code, PhoneModel, SignInfo``。
    依据是 SPA 调用点原样：``{...payload, Code, PhoneModel, SignInfo}``，其中
    ``payload`` 是 ``{AttachmentFileName, OutState}``，对象展开按入键序先展开。

    **2026-10-16 订正**：本函数原只发四字段（顶层缺 ``AttachmentFileName``）——那是
    按本项目 2026-10-03 自身流量实拍定的形状，当时可用；平台 10-16 起全量拒签
    （当日 0 成功，且**已配置设备码的账号同样被拒**），而 SPA 契约一直是五字段。
    以参考客户端为准对齐。

    **编码形态与 SPA 逐字节对齐（2026-10-18 改）**：SPA 的传输层是自写的请求包装器
    ——``SignInfo`` 走 ``JSON.stringify``（**紧凑，无空格**），值过 ``encodeURIComponent``
    （**空格→``%20``**；``!'()*`` 不转义、``/`` 转义；非 ASCII 原样）。原实现用
    ``json.dumps`` 的默认分隔符（``", "`` / ``": "``）与 ``urlencode`` 的 ``quote_plus``
    （空格→``+``），两处都与 SPA 不同。现在改为逐字节同构。

    **这不是修 bug**：服务端两种都解（10-03 至 10-17 生产可用即为证）。改的理由是
    **不留下可被指纹识别的编码差异**（用户 2026-10-18 口径："一点都不能变"）。
    历史证据保留：``fixtures/sign_in_body*.txt`` 是本站 2026-10-03 的旁路实拍
    （曾是本项目的参照物）；``fixtures/sign_in_body*_spa.txt`` 是按 SPA 的变换从同一组
    明文值**独立生成**的，不是从本函数回灌。
    """
    sign_info = {
        "Reason": reason,
        "AttachmentFileName": attachment_file_name,
        "LngLat": f"{lnglat[0]},{lnglat[1]}",
        "Address": address,
    }
    # `JSON.stringify` 的等价形态：无空白分隔符、非 ASCII 原样（不转 \uXXXX）。
    payload = json.dumps(sign_info, separators=(",", ":"), ensure_ascii=False)
    # `encodeURIComponent` 的非保留集 = quote 恒留的 `A-Za-z0-9_.-~` ＋ `!'()*`。
    return urlencode(
        [
            ("AttachmentFileName", attachment_file_name),
            ("OutState", out_state),
            ("Code", code),
            ("PhoneModel", phone_model),
            ("SignInfo", payload),
        ],
        quote_via=quote,
        safe="!'()*",
    )
