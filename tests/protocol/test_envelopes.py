"""SPEC §5 ``信封``、``usersure`` 与 ``挑战检测`` 用例组。"""

from __future__ import annotations

import pytest

from yiban.protocol import (
    Envelope,
    ParseError,
    SessionExpired,
    UsersureResult,
    looks_like_challenge,
    parse_api_envelope,
    parse_usersure_response,
)

# 全部正常夹具（含 59KB 授权页）都必须判为非挑战。
_ALL_GOOD_FIXTURES = [
    "auth_response.json",
    "authorize_page.html",
    "iframe_location.txt",
    "sign_in_body.txt",
    "sign_in_body_urlencoded.txt",
    "sign_in_response.json",
    "sign_position.json",
    "usersure_form.txt",
    "usersure_response.json",
    "usersure_url.txt",
]


# --- 信封 ------------------------------------------------------------------

def test_code_zero_returned_verbatim():
    """code:0 的信封原样返回。"""
    env = parse_api_envelope('{"code":0,"msg":"","data":true}')
    assert isinstance(env, Envelope)
    assert env.code == 0
    assert env.msg == ""
    assert env.data is True
    assert env.raw == {"code": 0, "msg": "", "data": True}


def test_fixture_sign_in_response(read_fixture):
    env = parse_api_envelope(read_fixture("sign_in_response.json"))
    assert env.code == 0
    assert env.data is True


def test_accepts_bytes_input():
    """输入可以是 bytes（上游 JSON 常挂在 text/html 头下）。"""
    env = parse_api_envelope(b'{"code":0,"msg":"","data":null}')
    assert env.code == 0


def test_code_999_raises_session_expired():
    with pytest.raises(SessionExpired) as excinfo:
        parse_api_envelope('{"code":999,"msg":"expired","data":null}')
    assert "expired" in str(excinfo.value)


def test_code_999_as_string_also_raises():
    with pytest.raises(SessionExpired):
        parse_api_envelope('{"code":"999","msg":"","data":null}')


def test_truncated_json_raises_parse_error():
    with pytest.raises(ParseError) as excinfo:
        parse_api_envelope('{"code":0,"msg":')
    assert excinfo.value.field == "envelope"


def test_non_object_json_raises_parse_error():
    with pytest.raises(ParseError):
        parse_api_envelope("[1, 2, 3]")


# --- usersure 端点 ---------------------------------------------------------

def test_usersure_s200_ok_with_re_url():
    result = parse_usersure_response('{"code":"s200","reUrl":"https://f.yiban.cn/iapp7463"}')
    assert isinstance(result, UsersureResult)
    assert result.ok is True
    assert result.re_url == "https://f.yiban.cn/iapp7463"
    assert result.code == "s200"


def test_usersure_fixture(read_fixture):
    result = parse_usersure_response(read_fixture("usersure_response.json"))
    assert result.ok is True
    assert result.re_url == "https://f.yiban.cn/iapp7463"


def test_usersure_failure_code_returned_as_data():
    """失败码作为数据返回，不抛异常。"""
    result = parse_usersure_response('{"code":"e001"}')
    assert result.ok is False
    assert result.re_url is None
    assert result.code == "e001"


def test_usersure_non_json_raises_parse_error():
    with pytest.raises(ParseError):
        parse_usersure_response("<html>not json</html>")


# --- 挑战检测 --------------------------------------------------------------

@pytest.mark.parametrize("name", _ALL_GOOD_FIXTURES)
def test_normal_fixtures_are_not_challenges(name, read_fixture):
    """硬约束：所有正常夹具一律 False（误报不可接受）。"""
    assert looks_like_challenge(read_fixture(name)) is False


def test_ydclearance_marker_detected():
    assert looks_like_challenge("<html><body>ydclearance=abc</body></html>") is True


def test_fengkongcloud_marker_detected():
    assert looks_like_challenge("... fengkongcloud ...") is True


def test_captcha_marker_case_insensitive():
    assert looks_like_challenge("<html>CAPTCHA</html>") is True


def test_acw_sc_challenge_script_detected():
    page = "<html><head><script>var acw_sc__v2 = 'x';</script></head></html>"
    assert looks_like_challenge(page) is True


def test_plain_json_is_not_a_challenge():
    assert looks_like_challenge('{"code":0,"msg":"","data":true}') is False
