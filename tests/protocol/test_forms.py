"""SPEC §5 ``表单构造`` 用例组。"""

from __future__ import annotations

import base64
import json
from urllib.parse import parse_qsl, unquote, unquote_plus

from yiban.adapters.protocol_yiban import build_sign_in_body, build_usersure_form

_SIGN_ADDRESS = "示例省示例市示例区示例大学示例校区-示例苑·公共教学组团"
_SIGN_LNGLAT = (0.101837, 1.003954)


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


def test_sign_in_body_equals_spa_fixture_exactly(read_fixture):
    """build_sign_in_body 与「按 SPA 变换独立生成」的夹具整串相等（逐字节）。

    `*_spa.txt` 由 `sign_in_body.txt` 的明文值经 `JSON.stringify` ＋
    `encodeURIComponent` 的等价变换生成，**不经过本仓实现**（避免自我循环）。
    """
    expected = read_fixture("sign_in_body_urlencoded_spa.txt")
    built = build_sign_in_body(lnglat=_SIGN_LNGLAT, address=_SIGN_ADDRESS)
    assert built == expected


def test_sign_in_body_reconstructs_from_spa_plain_fixture(read_fixture):
    """从 SPA 明文夹具反解参数再构造，仍与 SPA 编码夹具整串相等（两夹具互证）。"""
    expected = read_fixture("sign_in_body_urlencoded_spa.txt")
    values = dict(_pairs(read_fixture("sign_in_body_spa.txt")))
    sign_info = json.loads(values["SignInfo"])
    lng, lat = (float(part) for part in sign_info["LngLat"].split(","))
    built = build_sign_in_body(lnglat=(lng, lat), address=sign_info["Address"])
    assert built == expected


def test_sign_in_body_differs_from_our_own_2026_10_03_capture_only_in_encoding(read_fixture):
    """与本站 2026-10-03 旁路实拍相比，差异**只**在两处编码，不在语义。

    那条实拍曾是本项目的参照物（`sign_in_body*.txt`，保留不动）。它证明的是
    「服务端吃这套编码」，**不是**「这是 SPA 的编码」——这一点 2026-10-18 才分清。

    两处差异，逐条钉住：
    ① `SignInfo` 内部：本站用 `", "` / `": "`（有空格），SPA 是 `JSON.stringify`（紧凑）；
    ② 值的空格：本站 `+`，SPA `%20`。
    """
    ours_old = read_fixture("sign_in_body_urlencoded.txt")
    now = build_sign_in_body(lnglat=_SIGN_LNGLAT, address=_SIGN_ADDRESS)
    assert ours_old != now, "两者相等 ⇒ 本用例的前提已失效（编码已改回或未生效）"
    assert "%3A+%22" in ours_old and "%3A%22" in now, "① SignInfo 内部分隔符差异消失了"
    assert json.loads(unquote_plus(ours_old.split("&SignInfo=", 1)[1])) == \
        json.loads(unquote(now.split("&SignInfo=", 1)[1])), "② 语义不得随编码一起变"


def test_sign_in_body_encodes_space_as_percent_20():
    """值里的空格编成 `%20`（`encodeURIComponent`），不是 `+`（`quote_plus`）。"""
    built = build_sign_in_body(lnglat=(1.0, 2.0), address="A B")
    assert "%20" in built and "A+B" not in built


def test_sign_info_json_is_byte_identical():
    """SignInfo 的 JSON 序列化逐字节同构（键序、`JSON.stringify` 的紧凑分隔符、浮点短表示）。"""
    built = build_sign_in_body(lnglat=(0.10389401234568, 1.0078387654321), address="A")
    # `SignInfo` 是顶层最后一个字段（SPA 定序），切出其后半段即可；
    # 用字面分隔符切，避免 parse_qsl 改变 `+` 的语义。
    raw = built.split("&SignInfo=", 1)[1]
    assert unquote(raw) == (
        '{"Reason":"","AttachmentFileName":"",'
        '"LngLat":"0.10389401234568,1.0078387654321","Address":"A"}'
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
    # 顶层五字段、顺序对齐参考客户端（2026-10-16 订正：原只发四字段）
    assert built.startswith(
        "AttachmentFileName=a.jpg&OutState=2&Code=c&PhoneModel=m&SignInfo="
    )
    sign_info = unquote_plus(built.split("&SignInfo=", 1)[1])
    assert json.loads(sign_info)["Reason"] == "r"
    assert json.loads(sign_info)["AttachmentFileName"] == "a.jpg"
    assert json.loads(sign_info)["LngLat"] == "1.0,2.0"
