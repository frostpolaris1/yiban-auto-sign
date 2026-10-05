"""``base/c/auth/yiban`` 身份解析（SPEC §2.4）。

输入是认证信封的 ``data`` 对象。学校/人员相关的五个字段为必填；``State`` 按 §3.1
做数值收敛；``Container`` 缺省为 ``"StudentDefault"``；``Apps`` 缺失即空列表。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .errors import ParseError

#: Container 缺失时的默认容器。
_DEFAULT_CONTAINER = "StudentDefault"
#: 每个 App 必填的键（AuthCode 可缺省）。
_REQUIRED_APP_KEYS = ("Id", "ServiceId", "AppName", "AppUrl")


@dataclass(frozen=True)
class App:
    """身份载荷中的单个应用条目。"""

    id: str
    service_id: str
    name: str
    url: str
    auth_code: str


@dataclass(frozen=True)
class Identity:
    """``base/c/auth/yiban`` 的身份载荷。"""

    university_name: str
    university_id: str
    person_id: str
    person_name: str
    person_type: str
    state: int | None
    container: str
    apps: list[App]


def _required_str(data: dict, key: str, field_name: str) -> str:
    """取必填字符串；缺失/``None`` 或非字符串 → ``ParseError``。"""
    if key not in data or data[key] is None:
        raise ParseError(field_name, data.get(key))
    value = data[key]
    if not isinstance(value, str):
        raise ParseError(field_name, value)
    return value


def _to_int(value: Any) -> int | None:
    """把上游双态数字（int/float 或数字字符串）收敛为 int，失败返回 None。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return int(text)
        except ValueError:
            try:
                return int(float(text))
            except ValueError:
                return None
    return None


def _parse_app(item: Any) -> App:
    """解析单个 App 条目。"""
    if not isinstance(item, dict):
        raise ParseError("Apps", item)
    for key in _REQUIRED_APP_KEYS:
        if key not in item or item[key] is None:
            raise ParseError(f"Apps[].{key}", item)

    auth_code = item.get("AuthCode")
    if auth_code is None:
        auth_code = ""
    elif not isinstance(auth_code, str):
        auth_code = str(auth_code)

    return App(
        id=str(item["Id"]),
        service_id=str(item["ServiceId"]),
        name=str(item["AppName"]),
        url=str(item["AppUrl"]),
        auth_code=auth_code,
    )


def parse_identity(data: dict) -> Identity:
    """解析 ``base/c/auth/yiban`` 响应的 ``data`` 对象。

    异常:
        ParseError: 必填字段缺失，或 ``Apps`` 不是数组。
    """
    if not isinstance(data, dict):
        raise ParseError("identity", data)

    container_raw = data.get("Container")
    if isinstance(container_raw, str) and container_raw:
        container = container_raw
    elif container_raw is None or container_raw == "":
        container = _DEFAULT_CONTAINER
    else:
        container = str(container_raw)

    apps_raw = data.get("Apps")
    if apps_raw is None:
        apps = []
    elif isinstance(apps_raw, list):
        apps = [_parse_app(item) for item in apps_raw]
    else:
        raise ParseError("Apps", apps_raw)

    return Identity(
        university_name=_required_str(data, "UniversityName", "UniversityName"),
        university_id=_required_str(data, "UniversityId", "UniversityId"),
        person_id=_required_str(data, "PersonId", "PersonId"),
        person_name=_required_str(data, "PersonName", "PersonName"),
        person_type=_required_str(data, "PersonType", "PersonType"),
        state=_to_int(data.get("State")),
        container=container,
        apps=apps,
    )
