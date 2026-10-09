"""SPEC §5 ``身份`` 用例组。"""

from __future__ import annotations

import copy

import pytest

from yiban.protocol import ParseError, parse_identity


@pytest.fixture()
def identity_data(read_fixture_json):
    """夹具的 data 字段深拷贝，供各用例独立改动。"""
    return copy.deepcopy(read_fixture_json("auth_response.json")["data"])


def test_fixture_fields_match_exactly(identity_data):
    """夹具 → 逐字段相等。"""
    ident = parse_identity(identity_data)
    assert ident.university_name == "示例大学"
    assert ident.university_id == "7ce839ad472bd9b9166ad3fb51f7b3ef"
    assert ident.person_id == "ff4640efb14da8aeaaa1d50c7a30fcdc"
    assert ident.person_name == "张三"
    assert ident.person_type == "student"
    assert ident.state == 1
    assert ident.container == "StudentDefault"


def test_fixture_has_three_apps(identity_data):
    assert len(parse_identity(identity_data).apps) == 3


def test_nightattendance_app_is_present(identity_data):
    ident = parse_identity(identity_data)
    matches = [app for app in ident.apps if app.auth_code == "nightattendance.student.*"]
    assert len(matches) == 1
    assert matches[0].url == "https://app.uyiban.com/nightattendance/student/"
    assert matches[0].name == "早操签到"
    assert matches[0].id == "ddff3b902972f25df78087d9fd004772"
    assert matches[0].service_id == "1ab8139127501809e2b58a72587b5037"


def test_missing_person_name_raises(identity_data):
    del identity_data["PersonName"]
    with pytest.raises(ParseError) as excinfo:
        parse_identity(identity_data)
    assert excinfo.value.field == "PersonName"


def test_missing_apps_key_yields_empty_list(identity_data):
    """Apps 键缺失 → 空列表。"""
    del identity_data["Apps"]
    assert parse_identity(identity_data).apps == []


def test_missing_auth_code_defaults_to_empty_string(identity_data):
    del identity_data["Apps"][0]["AuthCode"]
    assert parse_identity(identity_data).apps[0].auth_code == ""


def test_missing_container_defaults_to_student_default(identity_data):
    del identity_data["Container"]
    assert parse_identity(identity_data).container == "StudentDefault"


def test_state_as_numeric_string_is_coerced(identity_data):
    """State 以字符串下发时按 §3.1 收敛。"""
    identity_data["State"] = "1"
    assert parse_identity(identity_data).state == 1


def test_app_missing_required_key_raises(identity_data):
    del identity_data["Apps"][0]["ServiceId"]
    with pytest.raises(ParseError):
        parse_identity(identity_data)
