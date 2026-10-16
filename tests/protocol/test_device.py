"""SPEC §5 ``设备状态`` 用例组（`device/student/index/getState` 的 data）。

预期行为（先于实现写下，不依现状）：
- 登记设备是**正常状态**：字段缺失即"该账号没有登记设备"，不是畸形响应；
- 类型不符才是畸形（如 `Code` 是数字）——抛 `ParseError`；
- `authorize` 为假是**合法状态**（登记了但未授权），解析层不得把它当错误。
"""

from __future__ import annotations

import copy

import pytest

from yiban.adapters.protocol_yiban import DeviceState, ParseError, parse_device_state


@pytest.fixture()
def device_data(read_fixture_json):
    """夹具的 data 字段深拷贝，供各用例独立改动。"""
    return copy.deepcopy(read_fixture_json("device_state.json")["data"])


def test_fixture_fields_match_exactly(device_data):
    """夹具 → 逐字段相等。"""
    state = parse_device_state(device_data)
    assert state.authorized is False
    assert state.code == "a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f90"
    assert state.phone_model == "Example-EX1000"
    assert state.update_time == 1761517844


def test_authorized_true_is_parsed_as_bool(device_data):
    """`authorize` 为真 → `authorized is True`（授权状态是合法状态，不是错误）。"""
    device_data["authorize"] = True
    assert parse_device_state(device_data).authorized is True


def test_unregistered_device_is_empty_not_error(device_data):
    """未登记设备（Code/PhoneModel 空）是正常状态，不抛错。"""
    device_data["Code"] = ""
    device_data["PhoneModel"] = ""
    state = parse_device_state(device_data)
    assert state.code == ""
    assert state.phone_model == ""


def test_missing_keys_fall_back_to_defaults(device_data):
    """缺键 = 无登记设备（默认值），不是畸形响应。"""
    for key in ("authorize", "Code", "PhoneModel", "UpdateTime"):
        device_data.pop(key, None)
    state = parse_device_state(device_data)
    assert state.authorized is False
    assert state.code == ""
    assert state.phone_model == ""
    assert state.update_time is None


def test_non_string_code_raises(device_data):
    """类型不符才是畸形：`Code` 为数字 → `ParseError`（点名 Code）。"""
    device_data["Code"] = 12345
    with pytest.raises(ParseError) as excinfo:
        parse_device_state(device_data)
    assert excinfo.value.field == "Code"


def test_non_dict_data_raises():
    """整体不是对象 → `ParseError`。"""
    with pytest.raises(ParseError):
        parse_device_state(["not", "a", "dict"])  # type: ignore[arg-type]


def test_update_time_accepts_numeric_string(device_data):
    """上游双态数字（SPEC §3.1）：时间戳以字符串下发也要收敛为 int。"""
    device_data["UpdateTime"] = "1761517844"
    assert parse_device_state(device_data).update_time == 1761517844


def test_device_state_is_frozen(device_data):
    """解析结果是不可变值对象（防止下游就地改写共享夹具）。"""
    from dataclasses import FrozenInstanceError

    state: DeviceState = parse_device_state(device_data)
    with pytest.raises(FrozenInstanceError):
        state.code = "x"  # type: ignore[misc]
