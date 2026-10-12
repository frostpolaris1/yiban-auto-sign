"""SPEC §5 ``加密`` 用例组。

本文件自带一对一次性 RSA-1024 测试密钥，仅用于 round-trip 断言，别无他用。
仓库中不存放任何真实密文。
"""

from __future__ import annotations

import base64
import importlib
import sys

import pytest

from yiban.protocol import parse_authorize_page
from yiban.protocol.crypto import encrypt_password

# 一次性测试用私钥（仅为本测试文件生成；非真实凭据）。
_TEST_PRIVATE_KEY_PEM = """-----BEGIN RSA PRIVATE KEY-----
MIICXAIBAAKBgQDYoJ1MXFgIIOwahCiEIGyVGDV0pDrPfZx+oJEjbOjCHaAw/UW/
LlgOqncRdwLPEkugdUwo7HGYcFsEPa7CDdl10t/o0Sv/rzswuK4c+dR3/tmkSdpR
Rg4nDzRF0xfEqBJDeaGBbOZGc5MrxZni3KqOIqr1+OcssoSzlbp6NSAIGwIDAQAB
AoGAN0AHvnES9sfG0CCC4OgQKZqqD5zPbxo/bsBvJBTj7JZ3w+blAhTE2sC5a5fp
/HxTE5K3IPzlIBcP7633w4CaxI0iDHTgnLAk//y4kaIFMbUS22T7f38HboVg4wtS
vOrdbjYSGgv5QQXRBL/InX61vUDZEpXgLhCvsLy2zejen7kCQQDl2PdHM+NX9Iij
IzeLoSJq5AS+vOEFRPjojeYc7VJmu7bGHsQp1CW4R2YmEg427tmkMghd/K1I/Vff
BFEvA7H9AkEA8UaSG9GFp5SUi7WHhYdgBONQXUMD/pPjkSXyO4mJfruhnVghKhYj
/iXbc25yznQqoSRRdqrohm7YyjcfjGuR9wJBAIcJ+f4zVhaO7NgsEK5QdVAnt0H4
5puZ8kNvWwsTw53oG3I7ETUiFyc1i6ZCZWeQ3P3DB3dwxL5lWgMFHk1o9mECQE84
+9q0hm1LJSdmmLQoikewl/+3dIVP7AYJ7qrL82CwnVV7zY/zKyhVJ+SUHJBbpm+4
7CLJ5YXWucpUJUDHRWsCQAJM4Br9hGa5gQNeIZpAAQXETgnKvMY8d/8FObe63emX
9aZkw45BBUAZ47JwW8HgH794u9ZkZ4TOSBC+C+3zrQ4=
-----END RSA PRIVATE KEY-----"""


def test_fixture_public_key_yields_172_char_ciphertext(read_fixture):
    """用夹具公钥加密：密文 172 字符、解码 128 字节（末位单个 =）。"""
    pem = parse_authorize_page(read_fixture("authorize_page.html")).public_key_pem
    ciphertext = encrypt_password("some-password", pem)
    assert len(ciphertext) == 172
    assert len(base64.b64decode(ciphertext)) == 128


def test_round_trip_with_test_key():
    """用测试自带密钥做 round-trip，断言能解回原文。"""
    from Crypto.Cipher import PKCS1_v1_5
    from Crypto.PublicKey import RSA

    private_key = RSA.import_key(_TEST_PRIVATE_KEY_PEM)
    public_pem = private_key.publickey().export_key().decode("ascii")

    ciphertext = encrypt_password("correct horse battery staple", public_pem)
    assert len(ciphertext) == 172

    plaintext = PKCS1_v1_5.new(private_key).decrypt(base64.b64decode(ciphertext), None)
    assert plaintext == b"correct horse battery staple"


def test_non_str_arguments_raise_type_error():
    with pytest.raises(TypeError):
        encrypt_password(b"bytes", "pem")
    with pytest.raises(TypeError):
        encrypt_password("pw", b"pem")


def test_invalid_pem_raises_value_error():
    with pytest.raises(ValueError):
        encrypt_password("pw", "not a pem")


def test_missing_dependency_guidance(monkeypatch):
    """无 pycryptodome 时导入本模块 → ImportError，且文案含安装指引。"""
    monkeypatch.delitem(sys.modules, "yiban.protocol.crypto", raising=False)
    for name in [n for n in list(sys.modules) if n == "Crypto" or n.startswith("Crypto.")]:
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setitem(sys.modules, "Crypto", None)

    with pytest.raises(ImportError, match="pycryptodome"):
        importlib.import_module("yiban.protocol.crypto")

    monkeypatch.undo()
    # 恢复正常模块，避免影响后续用例。
    importlib.import_module("yiban.protocol.crypto")
