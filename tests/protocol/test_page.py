"""SPEC §5 ``授权页`` 用例组。"""

from __future__ import annotations

import pytest

from yiban.protocol import ParseError, parse_authorize_page

_HEX = set("0123456789abcdef")


def test_page_use_matches_usersure_ajax_sign(read_fixture):
    """page_use 必须等于夹具 usersure_url.txt 中的 ajax_sign。"""
    page = parse_authorize_page(read_fixture("authorize_page.html"))
    assert f"ajax_sign={page.page_use}" in read_fixture("usersure_url.txt")


def test_page_use_is_40_hex_chars(read_fixture):
    page = parse_authorize_page(read_fixture("authorize_page.html"))
    assert len(page.page_use) == 40
    assert set(page.page_use) <= _HEX


def test_public_key_pem_is_normalized(read_fixture):
    """PEM 归一化：头尾行保留，中间 base64 行以 \n 折叠。"""
    pem = parse_authorize_page(read_fixture("authorize_page.html")).public_key_pem
    lines = pem.split("\n")
    assert lines[0] == "-----BEGIN PUBLIC KEY-----"
    assert lines[-1] == "-----END PUBLIC KEY-----"
    assert len(lines) == 6  # 头 + 4 行 base64 + 尾
    assert "\r" not in pem
    assert not pem.startswith("\n") and not pem.endswith("\n")


def test_public_key_only_depends_on_id_not_type(read_fixture):
    """上游写成 type="test"，仍必须靠 id="key" 找到该元素。"""
    html = read_fixture("authorize_page.html")
    assert 'type="test" id="key"' in html
    page = parse_authorize_page(html)
    assert "BEGIN PUBLIC KEY" in page.public_key_pem


def test_double_quoted_page_use_is_tolerated():
    """page_use 使用双引号也要容。"""
    html = (
        '<script>var page_use = "abc123def456";</script>'
        '<input type="test" id="key" value="-----BEGIN PUBLIC KEY-----\nAAAA\n-----END PUBLIC KEY-----\n">'
    )
    assert parse_authorize_page(html).page_use == "abc123def456"


def test_residual_page_without_page_use_raises():
    """缺 page_use 的残页 → ParseError("page_use")。"""
    with pytest.raises(ParseError) as excinfo:
        parse_authorize_page("<html><body>no token here</body></html>")
    assert excinfo.value.field == "page_use"


def test_page_without_public_key_raises():
    """有 page_use 但无 id="key" 输入 → ParseError("public_key")。"""
    html = "<script>var page_use = 'abc123';</script>"
    with pytest.raises(ParseError) as excinfo:
        parse_authorize_page(html)
    assert excinfo.value.field == "public_key"
