"""SPEC §5 ``表单构造`` 用例组。"""

from __future__ import annotations

import base64
import json
from urllib.parse import parse_qsl, unquote, unquote_plus

from yiban.protocol import build_sign_in_body, build_usersure_form

_SIGN_ADDRESS = "示例省示例市示例区示例大学示例校区-示例苑·公共教学组团"
_SIGN_LNGLAT = (118.101837, 32.003954)


def _pairs(body: str) -> list[tuple[str, str]]:
    """按 & 与首个 = 手工切分（避免 parse_qsl 把 base64 里的 + 当成空格）。"""
    result = []
    for part in body.split("&"):
        key, _, value = part.partition("=")
        result.append((key, value))
    return result


def test_usersure_form_matches_fixture_field_by_field(read_fixture):
    """build_usersure_form 与夹具字段对逐一相等（含顺序）。"""
    expected = parse_qsl(read_fixture("usersure_form.txt"))
    values = dict(expected)
    built = build_usersure_form(
        values["oauth_uname"],
        values["oauth_upwd"],
        values["client_id"],
        values["redirect_uri"],
    )
    assert parse_qsl(built) == expected


def test_usersure_form_field_order(read_fixture):
    values = dict(parse_qsl(read_fixture("usersure_form.txt")))
    built = build_usersure_form(
        values["oauth_uname"], values["oauth_upwd"], values["client_id"], values["redirect_uri"]
    )
    assert [key for key, _ in parse_qsl(built)] == [
        "oauth_uname",
        "oauth_upwd",
        "client_id",
        "redirect_uri",
        "display",
    ]


def test_fixture_ciphertext_shape(read_fixture):
    """夹具 oauth_upwd 形状与修订版 SPEC §2.7 一致：172 字符、单个 =、解码 128 字节。"""
    raw = read_fixture("usersure_form.txt")
    ciphertext = raw.split("oauth_upwd=", 1)[1].split("&", 1)[0]
    assert len(ciphertext) == 172
    assert ciphertext.endswith("=") and not ciphertext.endswith("==")
    assert len(base64.b64decode(unquote(ciphertext))) == 128


def test_usersure_form_scope_only_when_requested():
    """scope 仅在显式传入时追加在末尾。"""
    without = build_usersure_form("13800000000", "cipher", "cid", "https://example.org/cb")
    assert "scope" not in without
    with_scope = build_usersure_form(
        "13800000000", "cipher", "cid", "https://example.org/cb", scope="basic"
    )
    assert with_scope.endswith("&scope=basic")


def test_usersure_form_custom_display():
    built = build_usersure_form("13800000000", "cipher", "cid", "https://example.org/cb", display="mobile")
    assert "display=mobile" in built


def test_sign_in_body_equals_urlencoded_fixture_exactly(read_fixture):
    """build_sign_in_body 与 urlencoded 夹具整串相等。"""
    expected = read_fixture("sign_in_body_urlencoded.txt")
    built = build_sign_in_body(lnglat=_SIGN_LNGLAT, address=_SIGN_ADDRESS)
    assert built == expected


def test_sign_in_body_reconstructs_from_plain_fixture(read_fixture):
    """从明文夹具反解参数再构造，仍与 urlencoded 夹具整串相等（两夹具互证）。"""
    expected = read_fixture("sign_in_body_urlencoded.txt")
    values = dict(_pairs(read_fixture("sign_in_body.txt")))
    sign_info = json.loads(values["SignInfo"])
    lng, lat = (float(part) for part in sign_info["LngLat"].split(","))
    built = build_sign_in_body(lnglat=(lng, lat), address=sign_info["Address"])
    assert built == expected


def test_sign_info_json_is_byte_identical():
    """SignInfo 的 JSON 序列化逐字节同构（键序、分隔符、浮点短表示）。"""
    built = build_sign_in_body(lnglat=(118.10389401234568, 32.0078387654321), address="A")
    # 用字面分隔符切出 SignInfo，避免 parse_qsl 改变 + 的语义。
    raw = built.split("&SignInfo=", 1)[1].rsplit("&OutState=", 1)[0]
    assert unquote_plus(raw) == (
        '{"Reason": "", "AttachmentFileName": "", '
        '"LngLat": "118.10389401234568,32.0078387654321", "Address": "A"}'
    )


def test_sign_in_body_honours_optional_fields():
    built = build_sign_in_body(
        lnglat=(1.0, 2.0),
        address="addr",
        out_state=2,
        reason="r",
        attachment_file_name="a.jpg",
        code="c",
        phone_model="m",
    )
    assert built.startswith("Code=c&PhoneModel=m&SignInfo=")
    assert built.endswith("&OutState=2")
    sign_info = unquote_plus(built.split("&SignInfo=", 1)[1].rsplit("&OutState=", 1)[0])
    assert json.loads(sign_info)["Reason"] == "r"
    assert json.loads(sign_info)["AttachmentFileName"] == "a.jpg"
    assert json.loads(sign_info)["LngLat"] == "1.0,2.0"
