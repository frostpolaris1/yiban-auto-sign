"""``nightAttendance .../signPosition`` 响应解析（SPEC §2.5）。

输入是响应信封的 ``data`` 对象。``Position`` 恒为数组——调用方不得假设只有一个
候选点。数值字段上游可能发数字也可能发字符串，按 §3.1 收敛；顶层 ``Range`` 是
签到时间窗对象（§3.7），**不走**数值收敛；字面量 ``"None"`` 视作 ``None``（§3.2）。
``IsNeedPhoto`` 是数值枚举，**原样保留**，不臆断为布尔。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .errors import ParseError

#: 每个签到点必填的键（缺一即结构性失败）。
_REQUIRED_POSITION_KEYS = ("Id", "Type", "Title", "Address", "LngLat")


@dataclass(frozen=True)
class Position:
    """单个候选签到点（精确点或校区边界多边形）。"""

    id: str
    type: str
    title: str
    address: str
    lnglat: tuple[float, float]
    range_m: int | None
    points: list[tuple[float, float]]
    map_type: int | None
    address_name: str | None
    campus: str | None
    building_id: str | None
    create_time: str | None


@dataclass(frozen=True)
class TimeWindow:
    """顶层 ``Range``：当日签到时间窗（epoch 秒）。"""

    start_time: int
    end_time: int
    sign_day: int | None
    relat_type: int | None
    relat_time_type: int | None


@dataclass(frozen=True)
class SignPositionConfig:
    """``signPosition`` 的 ``data`` 对象，含全部候选签到点。"""

    state: int | None
    msg: str
    acs_state: str | None
    out_state: str | None
    remark: str | None
    file_url: str | None
    type_: str | None
    is_need_photo: int | None
    attachment_file_name: str | None
    time_window: TimeWindow | None
    positions: list[Position]


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


def _optional_str(value: Any) -> str | None:
    """可选字符串：缺失/``None`` → ``None``，其余保持（必要时转 str）。"""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return str(value)


def _none_like(value: Any) -> str | None:
    """字面量 ``"None"`` 与空串均表示“无值”（§3.2）。"""
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if text == "" or text == "None":
            return None
        return value
    return str(value)


def _parse_lnglat(value: Any) -> tuple[float, float]:
    """把 ``"lng,lat"``（容忍两侧空白）拆成浮点元组，失败抛 ``ParseError``。"""
    if not isinstance(value, str):
        raise ParseError("LngLat", value)
    parts = value.split(",")
    if len(parts) != 2:
        raise ParseError("LngLat", value)
    try:
        return (float(parts[0].strip()), float(parts[1].strip()))
    except ValueError as exc:
        raise ParseError("LngLat", value) from exc


def _parse_points(value: Any) -> list[tuple[float, float]]:
    """解析多边形顶点数组；缺省 → 空列表。"""
    if value is None:
        return []
    if not isinstance(value, list):
        raise ParseError("Points", value)
    return [_parse_lnglat(item) for item in value]


def _required_str(item: dict, key: str, field_name: str) -> str:
    """取必填字符串；缺失/``None`` 或非字符串 → ``ParseError``。"""
    value = item.get(key)
    if value is None:
        raise ParseError(field_name, item)
    if not isinstance(value, str):
        raise ParseError(field_name, value)
    return value


def _parse_position(item: Any) -> Position:
    """解析单个候选签到点。"""
    if not isinstance(item, dict):
        raise ParseError("Position", item)
    for key in _REQUIRED_POSITION_KEYS:
        if key not in item or item[key] is None:
            raise ParseError(key, item)

    return Position(
        id=_required_str(item, "Id", "Id"),
        type=_required_str(item, "Type", "Type"),
        title=_required_str(item, "Title", "Title"),
        address=_required_str(item, "Address", "Address"),
        lnglat=_parse_lnglat(item["LngLat"]),
        range_m=_to_int(item.get("Range")),
        points=_parse_points(item.get("Points")),
        map_type=_to_int(item.get("MapType")),
        address_name=_optional_str(item.get("AddressName")),
        campus=_optional_str(item.get("Campus")),
        building_id=_none_like(item.get("BuildingId")),
        create_time=_optional_str(item.get("CreateTime")),
    )


def _parse_time_window(value: Any) -> TimeWindow | None:
    """解析顶层 ``Range`` 时间窗对象（§3.7）。

    缺失 → ``None``；非对象形态视为结构性失败（数值收敛规则不适用于它）；
    ``StartTime``/``EndTime`` 必填。
    """
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ParseError("Range", value)

    start_time = _to_int(value.get("StartTime"))
    end_time = _to_int(value.get("EndTime"))
    if start_time is None or end_time is None:
        raise ParseError("Range", value)

    return TimeWindow(
        start_time=start_time,
        end_time=end_time,
        sign_day=_to_int(value.get("SignDay")),
        relat_type=_to_int(value.get("RelatType")),
        relat_time_type=_to_int(value.get("RelatTimeType")),
    )


def parse_sign_position(data: dict) -> SignPositionConfig:
    """解析 ``signPosition`` 响应的 ``data`` 对象。

    异常:
        ParseError: ``Position`` 缺失/非数组、候选点缺必填键，或
            ``LngLat``/``Points``/``Range`` 形态非法。
    """
    if not isinstance(data, dict):
        raise ParseError("signPosition", data)

    if "Position" not in data or data["Position"] is None:
        raise ParseError("Position", data)
    positions_raw = data["Position"]
    if not isinstance(positions_raw, list):
        raise ParseError("Position", positions_raw)

    positions = [_parse_position(item) for item in positions_raw]

    msg = data.get("Msg")
    if not isinstance(msg, str):
        msg = "" if msg is None else str(msg)

    return SignPositionConfig(
        state=_to_int(data.get("State")),
        msg=msg,
        acs_state=_optional_str(data.get("AcsState")),
        out_state=_optional_str(data.get("OutState")),
        remark=_optional_str(data.get("Remark")),
        file_url=_optional_str(data.get("FileUrl")),
        type_=_optional_str(data.get("Type")),
        # IsNeedPhoto 是数值枚举，按 §3.1 收敛为 int 后**原样保留**（观测值 2）。
        is_need_photo=_to_int(data.get("IsNeedPhoto")),
        attachment_file_name=_optional_str(data.get("AttachmentFileName")),
        # 顶层 Range 是签到时间窗对象，不走数值收敛（§3.7）。
        time_window=_parse_time_window(data.get("Range")),
        positions=positions,
    )
