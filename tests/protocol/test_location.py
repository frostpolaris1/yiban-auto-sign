"""SPEC §5 ``verify_request`` 用例组。"""

from __future__ import annotations

from yiban.protocol import extract_verify_request

_HEX = set("0123456789abcdef")


def test_fixture_location_yields_512_hex_token(read_fixture):
    token = extract_verify_request(read_fixture("iframe_location.txt"))
    assert token is not None
    assert len(token) == 512
    assert set(token) <= _HEX


def test_token_at_end_of_query_without_ampersand():
    """令牌位于 query 末位、其后无 & 也要能取。"""
    assert extract_verify_request("https://c.uyiban.com/#/?verify_request=deadbeef") == "deadbeef"


def test_token_in_middle_before_ampersand():
    """令牌在中间、后面还有其它参数也要能取。"""
    location = "https://c.uyiban.com/#/?verify_request=deadbeef&yb_uid=85118634"
    assert extract_verify_request(location) == "deadbeef"


def test_url_without_parameter_returns_none():
    """无该参数的 URL → None（不抛异常）。"""
    assert extract_verify_request("https://c.uyiban.com/#/") is None


def test_empty_input_returns_none():
    assert extract_verify_request("") is None
