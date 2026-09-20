# SPDX-License-Identifier: AGPL-3.0-only
"""易班账号敏感字段加密（AES-GCM）：web / signin 双进程共享。

- 存储层加密：账号的 password/phone_code 字段为密文对象（yiban.db 的 accounts 表，
  迁移前是 accounts.json）
- 密钥：环境变量 YIBAN_ACCOUNTS_KEY → 回退 .env 同键 → 缺失时生成并持久化（0600）
- AAD = 手机号（防密文跨账号互换）；解密 tag 校验失败即抛错

密文对象格式（v1）：
    {"v": 1, "nonce": "<hex>", "ct": "<hex>", "tag": "<hex>"}

⚠️ 密钥丢失 = 已加密的账号密码不可恢复：备份数据时必须连同密钥一起备份
（密钥与数据分开放，如 .env 与 yiban.db 分开打包）。
"""

import logging
import os
import secrets
import threading
from contextlib import suppress

from Crypto.Cipher import AES

from yiban.infra import (
    env_io,  # 同目录共享模块：.env 解析单一实现
    env_lock,
)

logger = logging.getLogger("yiban-crypto")

# 密文对象格式版本（AES-256-GCM，v1）
SCHEMA_VERSION = 1
DEFAULT_ENV_FILE = ".env"
# 本键"这一行属于 YIBAN_ACCOUNTS_KEY"的判据，直接取自 env_io：折叠与解析必须是
# 同一套口径（见 _write_key_to_env_file），各写一份正则迟早漂移出影子行
_KEY_LINE_RE = env_io.key_line_pattern("YIBAN_ACCOUNTS_KEY")

# 进程内密钥缓存（bytes）。环境变量优先级最高，其次 .env 文件；
# 两者都没有时自动生成并持久化（见 load_key）。
_KEY_CACHE = None
# 建钥互斥：防多线程首启各自生成不同密钥互相覆盖（跨进程已由 _write_key_to_env_file
# 的"写前重读"缓解，此处封同进程竞态）
_KEY_LOCK = threading.Lock()


def load_key(env_file=None):
    """获取加密密钥：环境变量 YIBAN_ACCOUNTS_KEY 优先，回退 .env 同键。

    两者都不存在时生成随机 32 字节密钥并持久化到 .env（0600）后返回；
    同一进程内缓存复用（避免每次读写 .env）。
    读-生成-写-缓存全程持 _KEY_LOCK：多线程首启只生成一份密钥。

    **来源守卫（M3）**：自动建钥只允许在"密钥来源确定"时发生——调用方显式传了
    `env_file`、或设了 `YIBAN_ENV_FILE`、或当前目录已有 `.env`。三者都没有而该
    路径又要**写**密文时，就地生成会在错误目录落一份游离 `.env` 与新密钥
    （与 `db._assert_key_source_certain` 同源缺陷；db 依赖本模块不能反向 import，
    故此处按同口径就地复刻）。只读解密路径（`has_key` 先判）不受影响。
    """
    global _KEY_CACHE
    explicit = env_file is not None
    env_file = env_file or (os.environ.get("YIBAN_ENV_FILE") or "").strip() \
        or DEFAULT_ENV_FILE
    env_key = os.environ.get("YIBAN_ACCOUNTS_KEY", "").strip()
    if env_key:
        _KEY_CACHE = _decode_key(env_key)
        return _KEY_CACHE
    if _KEY_CACHE is not None:
        return _KEY_CACHE
    with _KEY_LOCK:
        if _KEY_CACHE is not None:  # 双检：等锁期间他线程已生成
            return _KEY_CACHE
        _assert_source_certain(env_file, explicit)
        file_key = _parse_env_file(env_file).get("YIBAN_ACCOUNTS_KEY", "").strip()
        if file_key:
            _KEY_CACHE = _decode_key(file_key)
            return _KEY_CACHE
        logger.info("未找到 YIBAN_ACCOUNTS_KEY，已生成新密钥并写入 %s（chmod 600）", env_file)
        _KEY_CACHE = _write_key_to_env_file(env_file, secrets.token_bytes(32))
        return _KEY_CACHE


def _assert_source_certain(env_file, explicit):
    """自动建钥前确认密钥来源确定：显式 env_file / YIBAN_ENV_FILE / cwd 已有 .env。

    `explicit`：调用方显式传了 env_file；`env_file != DEFAULT_ENV_FILE` 说明路径
    来自 YIBAN_ENV_FILE 解析（也算确定来源）。与 `db._resolve_key_env_file` 的
    口径一致（explicit 优先于 cwd 兜底）；目录存在性交给写路径报错，这里只拦
    "来源不确定却要建新钥"这一条。
    """
    if explicit or env_file != DEFAULT_ENV_FILE:
        return
    if os.path.exists(DEFAULT_ENV_FILE):
        return
    raise ValueError(
        "账号密钥来源不确定，拒绝生成新密钥；请用 YIBAN_ENV_FILE 或显式 env_file 指定"
    )


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
    except ValueError as e:
        raise ValueError("密码解密失败（密钥不匹配、密文被篡改或账号手机号不匹配）") from e
    # 必须单独 catch：UnicodeDecodeError 是 ValueError 的子类，若并进上面的 except
    # 会被误报成"密钥不匹配"，掩盖真实的密文损坏
    try:
        return plain.decode("utf-8")
    except UnicodeDecodeError as e:
        raise ValueError("密码解密失败（明文不是合法 UTF-8，密文已损坏）") from e


# ---------------------------------------------------------------------------
# 通用文本加解密（固定 AAD）：供非账号类敏感配置（如 webhook 密钥）复用
# ---------------------------------------------------------------------------

def encrypt_text(plain, key, aad=b"yiban-notify"):
    """AES-256-GCM 加密任意文本为密文对象；空明文返回空字符串。

    AAD 固定（默认 b"yiban-notify"）：与账号密码加密（AAD=手机号）区分，
    用于通知类敏感配置（如 Server酱 SendKey / 自定义 webhook URL）的静态加密。
    """
    if not plain:
        return ""
    nonce = secrets.token_bytes(12)
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
    cipher.update(aad)
    ct, tag = cipher.encrypt_and_digest(str(plain).encode("utf-8"))
    return {
        "v": SCHEMA_VERSION,
        "nonce": nonce.hex(),
        "ct": ct.hex(),
        "tag": tag.hex(),
    }


def decrypt_text(entry, key, aad=b"yiban-notify"):
    """解密密文对象为明文 str（AAD 固定）。

    entry 不是密文对象 / 密文被篡改 / 密钥不匹配（tag 校验失败）时抛
    ValueError——绝不静默返回错误结果。
    """
    if not is_encrypted(entry):
        raise ValueError("密文对象格式非法（缺 v/ct 键）")
    if entry.get("v") != SCHEMA_VERSION:
        raise ValueError(f"不支持的密文版本: {entry.get('v')}（当前支持 v{SCHEMA_VERSION}）")
    try:
        nonce = bytes.fromhex(str(entry["nonce"]))
        ct = bytes.fromhex(str(entry["ct"]))
        tag = bytes.fromhex(str(entry["tag"]))
    except (KeyError, TypeError, ValueError) as e:
        raise ValueError("密文对象字段非法（nonce/ct/tag 应为十六进制字符串）") from e
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
    cipher.update(aad)
    try:
        plain = cipher.decrypt_and_verify(ct, tag)
    except ValueError as e:
        raise ValueError("解密失败（密钥不匹配或密文被篡改）") from e
    try:
        return plain.decode("utf-8")
    except UnicodeDecodeError as e:
        raise ValueError("解密失败（明文不是合法 UTF-8，密文已损坏）") from e


# ---- 内部实现 ----
def _parse_env_file(env_file):
    """读取 .env 全部键值，返回 dict（文件缺失返回空；非法行跳过）。

    严格策略：文件存在但读取失败 → 记 ERROR 并重抛（解析实现在 env_io）。
    """
    try:
        return env_io.parse_env_file(env_file, strict=True)
    except OSError as e:
        # 绝不静默当作"未配置"——否则 load_key 会误判无密钥而自动生成新钥覆盖旧钥，
        # 致存量密文永久不可解。宁可启动失败也不生成替代密钥。
        logger.error("密钥文件存在但读取失败，按错误处理而非未配置（请检查权限）: %s [%s]", env_file, e)
        raise


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

    读-写-替换整体包进共享 env_lock：与 web 写 .env 互斥，避免多进程首启
    同时生成不同密钥互相覆盖；锁内仍保留"写入前重读"的既有兜底。
    """
    with env_lock.env_write_lock(env_file):
        existing = _parse_env_file(env_file).get("YIBAN_ACCOUNTS_KEY", "").strip()
        if existing:
            return _decode_key(existing)
        raw = ""
        if os.path.exists(env_file):
            with open(env_file, encoding="utf-8-sig") as f:  # utf-8-sig：兼容带 BOM 的 .env
                raw = f.read()
        # 行模型取窄模型（只认 \r\n / \r / \n）而非 splitlines()：值里藏的 U+2028 之类
        # 在窄模型下仍留在本行内，下面逐行 has_line_break 才看得见——splitlines() 会先把
        # 它当行边界吃掉。无潜伏分隔符时两者逐行等价，正常 .env 写回结果与旧实现逐字相同。
        lines = raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        if lines and lines[-1] == "":
            lines.pop()  # 结尾换行不构成空配置行（对齐 splitlines 的行数）
        # 旧键行折叠必须与解析口径同源（env_io.key_line_pattern）：parse_env_file 按
        # "首个 = 切分 + 两侧 strip"认键，`YIBAN_ACCOUNTS_KEY = v` 正是同一条键的行；
        # 只认字面前缀 KEY= 折不掉它，残留影子行后谁生效由落盘顺序决定（后写覆盖先写）。
        out = [ln for ln in lines if not _KEY_LINE_RE.match(ln.strip())]
        out.append(f"YIBAN_ACCOUNTS_KEY={key.hex()}")
        # fail-closed：本函数只保留别人的行、没有清理权；潜伏分隔符写回后仍是潜伏态，
        # 迟早被某次 splitlines 读-改-写实体化成生效配置行（启动时 find_env_key_collisions
        # 已报出这类行）。故拒写并在消息里点名待清理的行，不静默留下歧义的 .env。
        for ln in out:
            if env_io.has_line_break(ln):
                raise ValueError(
                    f"{env_file} 有行含潜伏行分隔符（U+2028 等），写回会把它后面的内容"
                    f"实体化成新配置行，故拒绝写入密钥；请人工清理该行后重试"
                    f"（定位线索，该行首个键名：{(ln.partition('=')[0].strip()[:40] or '?')}）"
                )
        tmp = f"{env_file}.tmp{secrets.token_hex(4)}"
        # 创建即 0600——open("w") 在默认 umask 下 0644，写完到 replace
        # 之间（及进程崩溃残留时）密钥对同机其他用户可读，AES-GCM 防线归零
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("\n".join(out) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, env_file)
        with suppress(OSError):
            os.chmod(env_file, 0o600)  # 仅属主可读写（Windows 无实际效果，忽略失败）
        return key
