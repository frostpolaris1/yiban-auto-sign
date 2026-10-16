"""请求体/URL 构造（SPEC §2.6）。

纯构造函数：无 I/O、无会话状态。输出恒为 ``application/x-www-form-urlencoded``
字符串，采用 ``quote_plus`` 语义（空格 → ``+``；``+ / =`` → ``%2B %2F %3D``）。
"""

from __future__ import annotations

import json
from urllib.parse import urlencode


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

    **编码形态与 SPA 不同，这是已知事实**（2026-10-16 实测 SPA 资产）：SPA 的传输层
    是**自写的请求包装器**（不是 axios），值过 ``encodeURIComponent``（空格→``%20``，
    键名不转义），``SignInfo`` 走 ``JSON.stringify``（紧凑）；本站用 ``urlencode``
    （``quote_plus``，空格→``+``）与带空格的 ``json.dumps``。服务端两种都解
    （10-03 至 10-15 生产可用即为证）。故"逐字节同构"只成立于本站自己的夹具。
    """
    sign_info = {
        "Reason": reason,
        "AttachmentFileName": attachment_file_name,
        "LngLat": f"{lnglat[0]},{lnglat[1]}",
        "Address": address,
    }
    payload = json.dumps(sign_info, separators=(", ", ": "), ensure_ascii=False)
    return urlencode(
        [
            ("AttachmentFileName", attachment_file_name),
            ("OutState", out_state),
            ("Code", code),
            ("PhoneModel", phone_model),
            ("SignInfo", payload),
        ]
    )
