# SPDX-License-Identifier: AGPL-3.0-only
"""易班账号敏感字段加密（AES-GCM）：web / signin / tui 三进程共享。

- 存储层加密：accounts.json 的 password/phone_code 为密文对象
- 密钥：环境变量 YIBAN_ACCOUNTS_KEY → 回退 .env 同键 → 缺失时生成并持久化（0600）
- AAD = 手机号（防密文跨账号互换）；解密 tag 校验失败即抛错

密文对象格式（v1）：
    {"v": 1, "nonce": "<hex>", "ct": "<hex>", "tag": "<hex>"}

⚠️ 密钥丢失 = 已加密的账号密码不可恢复：备份数据时必须连同密钥一起备份
（密钥与数据分开放，如 .env 与 accounts.json 分开打包）。
"""

import logging
import os
import secrets
import threading
from contextlib import suppress

from Crypto.Cipher import AES

logger = logging.getLogger("yiban-crypto")

# 密文对象格式版本（AES-256-GCM，v1）
SCHEMA_VERSION = 1
DEFAULT_ENV_FILE = ".env"

# 进程内密钥缓存（bytes）。环境变量优先级最高，其次 .env 文件；
# 两者都没有时自动生成并持久化（见 load_key）。
_KEY_CACHE = None
# 建钥互斥（对抗性审查 2026-08-15 F3）：防多线程首启各自生成不同密钥互相覆盖
# （跨进程已由 _write_key_to_env_file 的"写前重读"缓解，此处封同进程竞态）
_KEY_LOCK = threading.Lock()


def _parse_env_file(env_file):
    """读取 .env 全部键值，返回 dict（文件缺失/非法行静默跳过）。

    utf-8-sig：兼容带 BOM 的 .env（Windows 工具保存常见），否则首个键名带
    \ufeff 前缀会导致密钥读不到（误判未配置 → 静默生成新密钥 → 旧数据不可解）。
    """
    result = {}
    try:
        with open(env_file, encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    result[key.strip()] = value.strip()
    except OSError:
        pass
    return result


def _decode_key(raw):
    """把 hex 字符串密钥解码为 bytes；格式/长度非法抛 ValueError。"""
    try:
        key = bytes.fromhex(raw)
    except (TypeError, ValueError) as e:
        raise ValueError("YIBAN_ACCOUNTS_KEY 格式非法：应为 64 位十六进制字符串") from e
    if len(key) != 32:
        raise ValueError("YIBAN_ACCOUNTS_KEY 长度非法：应为 32 字节（64 位十六进制）")
    # 弱密钥检测：全零、单字节重复、顺序/逆序等明显弱模式 → 警告（不阻断，避免误杀合法密钥）
    if key == b"\x00" * 32:
        logger.warning("YIBAN_ACCOUNTS_KEY 为全零密钥，极易被破解，请立即更换")
    elif len(set(key)) == 1:
        logger.warning("YIBAN_ACCOUNTS_KEY 为单字节重复密钥，极易被破解，请立即更换")
    elif key == bytes(range(32)) or key == bytes(range(31, -1, -1)):
        logger.warning("YIBAN_ACCOUNTS_KEY 为顺序/逆序密钥，极易被破解，请立即更换")
    return key


def _write_key_to_env_file(env_file, key):
    """把新生成的密钥写入 .env（保留其他行，原子替换，Unix 权限 0600）。

    写入前再读一次 .env：若其他进程已写入密钥则复用（多进程首启竞态兜底，
    避免后写覆盖导致先前加密的数据无法解密）。
    """
    existing = _parse_env_file(env_file).get("YIBAN_ACCOUNTS_KEY", "").strip()
    if existing:
        return _decode_key(existing)
    lines = []
    if os.path.exists(env_file):
        with open(env_file, encoding="utf-8-sig") as f:  # utf-8-sig：兼容带 BOM 的 .env
            lines = f.read().splitlines()
    out = [ln for ln in lines if not ln.strip().startswith("YIBAN_ACCOUNTS_KEY=")]
    out.append(f"YIBAN_ACCOUNTS_KEY={key.hex()}")
    tmp = f"{env_file}.tmp{secrets.token_hex(4)}"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, env_file)
    with suppress(OSError):
        os.chmod(env_file, 0o600)  # 仅属主可读写（Windows 无实际效果，忽略失败）
    return key


def load_key(env_file=None):
    """获取加密密钥：环境变量 YIBAN_ACCOUNTS_KEY 优先，回退 .env 同键。

    两者都不存在时生成随机 32 字节密钥并持久化到 .env（0600）后返回；
    同一进程内缓存复用（避免每次读写 .env）。
    读-生成-写-缓存全程持 _KEY_LOCK：多线程首启只生成一份密钥（F3）。
    """
    global _KEY_CACHE
    env_file = env_file or DEFAULT_ENV_FILE
    env_key = os.environ.get("YIBAN_ACCOUNTS_KEY", "").strip()
    if env_key:
        _KEY_CACHE = _decode_key(env_key)
        return _KEY_CACHE
    if _KEY_CACHE is not None:
        return _KEY_CACHE
    with _KEY_LOCK:
        if _KEY_CACHE is not None:  # 双检：等锁期间他线程已生成
            return _KEY_CACHE
        file_key = _parse_env_file(env_file).get("YIBAN_ACCOUNTS_KEY", "").strip()
        if file_key:
            _KEY_CACHE = _decode_key(file_key)
            return _KEY_CACHE
        logger.info("未找到 YIBAN_ACCOUNTS_KEY，已生成新密钥并写入 %s（chmod 600）", env_file)
        _KEY_CACHE = _write_key_to_env_file(env_file, secrets.token_bytes(32))
        return _KEY_CACHE


def has_key(env_file=None):
    """环境中（环境变量或 .env 文件）是否已有密钥，供明文兼容/降级判定。"""
    if os.environ.get("YIBAN_ACCOUNTS_KEY", "").strip():
        return True
    return bool(_parse_env_file(env_file or DEFAULT_ENV_FILE).get("YIBAN_ACCOUNTS_KEY", "").strip())


def is_encrypted(value):
    """判断字段是否为密文对象（dict 且含 v/ct 键）。"""
    return isinstance(value, dict) and "v" in value and "ct" in value


def encrypt_password(plain, key, phone):
    """AES-256-GCM 加密明文为密文对象；空明文返回空字符串（保持空值语义）。

    AAD = 手机号（UTF-8）：密文绑定所属账号，跨账号互换密文会在解密时失败。
    """
    if not plain:
        return ""
    nonce = secrets.token_bytes(12)
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
    cipher.update(str(phone).encode("utf-8"))
    ct, tag = cipher.encrypt_and_digest(str(plain).encode("utf-8"))
    return {
        "v": SCHEMA_VERSION,
        "nonce": nonce.hex(),
        "ct": ct.hex(),
        "tag": tag.hex(),
    }


def decrypt_password(entry, key, phone):
    """解密密文对象为明文 str。

    entry 不是密文对象 / 密文被篡改 / 密钥不匹配 / AAD 手机号不匹配
    （tag 校验失败）时抛 ValueError——绝不静默返回错误结果。
    """
    if not is_encrypted(entry):
        raise ValueError("密码字段不是有效的密文对象（缺 v/ct 键）")
    if entry.get("v") != SCHEMA_VERSION:
        raise ValueError(f"不支持的密文版本: {entry.get('v')}（当前支持 v{SCHEMA_VERSION}）")
    try:
        nonce = bytes.fromhex(str(entry["nonce"]))
        ct = bytes.fromhex(str(entry["ct"]))
        tag = bytes.fromhex(str(entry["tag"]))
    except (KeyError, TypeError, ValueError) as e:
        raise ValueError("密文对象字段非法（nonce/ct/tag 应为十六进制字符串）") from e
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
    cipher.update(str(phone).encode("utf-8"))  # AAD 必须与加密时一致
    try:
        plain = cipher.decrypt_and_verify(ct, tag)
        return plain.decode("utf-8")
    except ValueError as e:
        raise ValueError("密码解密失败（密钥不匹配、密文被篡改或账号手机号不匹配）") from e
    except UnicodeDecodeError as e:
        raise ValueError("密码解密失败（明文不是合法 UTF-8，密文已损坏）") from e
