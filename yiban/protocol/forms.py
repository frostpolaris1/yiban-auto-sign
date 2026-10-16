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

    字段集与顺序对齐**参考客户端**（H5 SPA 未变更，仍在线可用）：

    * 顶层五字段 ``AttachmentFileName, OutState, Code, PhoneModel, SignInfo``——
      SPA 的实现在 `{...payload, Code, PhoneModel, SignInfo}`，其中 payload 是
      ``{AttachmentFileName, OutState}``（对象展开先入键序）；axios 按入键序序列化，
      故线上顺序即此。
    * ``SignInfo`` JSON 键序 ``Reason, AttachmentFileName, LngLat, Address``，用
      ``json.dumps(separators=(", ", ": "), ensure_ascii=False)``。

    **2026-10-16 订正**：本函数原只发四字段（顶层缺 ``AttachmentFileName``）——那是
    按本项目 2026-10-03 自身流量实拍定的形状，当时可用；平台 10-16 起全量拒签
    （当日 0 成功，且**已配置设备码的账号同样被拒**），而 SPA 契约一直是五字段。
    以参考客户端为准对齐。
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
