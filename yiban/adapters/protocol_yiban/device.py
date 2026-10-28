"""``device/student/index/getState`` 设备登记解析（SPEC §2.8）。

输入是设备信封的 ``data`` 对象——**服务端保存的"该账号已登记设备"**。

与身份解析（§2.4）的关键差别：**未登记设备是正常状态**，不是畸形响应。因此
`Code`/`PhoneModel`/`authorize`/`UpdateTime` 全部按"缺省即默认"处理；只有
**类型不符**（如 `Code` 是数字）才抛 `ParseError`。

`authorize` 为假同样合法：它表示"设备已登记但尚未授权"，是服务端会如实返回的
中间态，解析层不得把它当错误——该不该拦截由服务端在签到接口上裁决。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .errors import ParseError


@dataclass(frozen=True)
class DeviceState:
    """``device/student/index/getState`` 的 ``data`` 载荷。

    ``code`` 为空串表示该账号没有登记设备；``authorized`` 为假表示登记了但未授权。
    """

    authorized: bool
    code: str
    phone_model: str
    update_time: int | None


def _to_bool(value: Any) -> bool:
    """`authorize` 收敛为 bool；缺失/不可判 → False。

    上游实测为 JSON 布尔；同时容 ``"1"/"true"/"on"/"yes"`` 与数值 1 这类形态，
    避免把"已授权"误读成"未授权"（误读的代价是白跑一趟签到）。
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "on", "yes")
    return False


def _to_int(value: Any) -> int | None:
    """上游双态数字（int/str）收敛为 int，失败返回 None（SPEC §3.1）。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if text and text.lstrip("-").isdigit():
            return int(text)
    return None


def _opt_str(data: dict[str, Any], key: str) -> str:
    """取可选字符串字段：缺失/``None`` → 空串；非字符串 → ``ParseError``。"""
    if key not in data or data[key] is None:
        return ""
    value = data[key]
    if not isinstance(value, str):
        raise ParseError(key, value)
    return value


def parse_device_state(data: Any) -> DeviceState:
    """把 ``getState`` 的 ``data`` 解析为 :class:`DeviceState`。

    异常:
        ParseError: ``data`` 不是对象，或某个字段存在但类型不符。
    """
    if not isinstance(data, dict):
        raise ParseError("data", data)
    return DeviceState(
        authorized=_to_bool(data.get("authorize")),
        code=_opt_str(data, "Code"),
        phone_model=_opt_str(data, "PhoneModel"),
        update_time=_to_int(data.get("UpdateTime")),
    )
