"""密码加密——可选附加件（SPEC §2.7）。

这是唯一允许依赖第三方包（``pycryptodome``）的模块。它刻意**不**被
``yiban_protocol/__init__`` 直接导入，以保持核心包零第三方依赖；安装 extra 后
再显式导入本模块（或使用 ``from yiban_protocol import encrypt_password``）::

    pip install 'yiban-protocol[crypto]'
"""

from __future__ import annotations

import base64

# 缺少依赖时抛出的可读指引文案。
_MISSING_DEP_MESSAGE = (
    "yiban_protocol.crypto requires the optional dependency 'pycryptodome'. "
    "Install it with: pip install 'yiban-protocol[crypto]'"
)

try:  # pragma: no cover - 缺依赖路径由测试以阻塞导入方式覆盖
    from Crypto.Cipher import PKCS1_v1_5
    from Crypto.PublicKey import RSA
except ImportError as exc:  # pragma: no cover
    raise ImportError(_MISSING_DEP_MESSAGE) from exc


def encrypt_password(password: str, public_key_pem: str) -> str:
    """用 *public_key_pem* 以 RSA PKCS#1 v1.5 加密 *password*，返回 base64 字符串。

    对观测到的 RSA-1024 公钥，密文为 128 字节，即 172 个 base64 字符、末尾单个 ``=``。

    异常:
        TypeError: 任一参数不是 ``str``。
        ValueError: *public_key_pem* 不是可用的 RSA 公钥。
    """
    if not isinstance(password, str):
        raise TypeError("password must be str")
    if not isinstance(public_key_pem, str):
        raise TypeError("public_key_pem must be str")

    try:
        key = RSA.import_key(public_key_pem)
    except (ValueError, IndexError, TypeError) as exc:
        raise ValueError("public_key_pem is not a valid RSA public key") from exc

    ciphertext = PKCS1_v1_5.new(key).encrypt(password.encode("utf-8"))
    return base64.b64encode(ciphertext).decode("ascii")
