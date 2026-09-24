# SPDX-License-Identifier: AGPL-3.0-only
"""**功能**
易班账号敏感字段加密（AES-GCM）：web / signin 双进程共享。

- 存储层加密：账号的 password/phone_code 字段为密文对象（yiban.db 的 accounts 表，
  迁移前是 accounts.json）
- 密钥：环境变量 YIBAN_ACCOUNTS_KEY → 回退 .env 同键 → 缺失时生成并持久化（0600）
- AAD = 手机号（防密文跨账号互换）；解密 tag 校验失败即抛错

密文对象格式（v1）：
    {"v": 1, "nonce": "<hex>", "ct": "<hex>", "tag": "<hex>"}

已知限制：密文不带密钥指纹（无 kid / 校验值）——"这把钥对不对"只能在解密撞
ValueError 时才知道，换钥中断、.env 与库不同步时无法在动手前预判；故 v1 格式不变、
存量密文不迁移（迁移需读写两侧同步，属 v2 的事）。

⚠️ 密钥丢失 = 已加密的账号密码不可恢复：备份数据时必须连同密钥一起备份
（密钥与数据分开放，如 .env 与 yiban.db 分开打包）。

**归属**
`yiban.infra` 的加密基础设施（无项目内依赖），是账号密文的唯一实现；web 与 signin
两个进程共享同一份密钥与格式。

**复用**
`encrypt_field` / `decrypt_field` 族与 `load_key` / `has_key`、`SCHEMA_VERSION` 是
唯一来源；`.env` 读写复用同目录 `env_io`，跨进程锁复用 `env_lock`。

**通信**
输入：明文敏感字段 + 手机号（AAD）、密钥来源（环境变量或 .env 路径）。
输出：v1 密文对象（JSON 可序列化）或解密后的明文；密钥缺失时按 0600 生成并持久化。
调用谁：`yiban.infra.env_io`、`yiban.infra.env_lock`、`Crypto.Cipher.AES`。
谁调用：`yiban.engine.accounts`（装载解密）、`yiban.store.db`（落库加密）、
web 服务层（账号增改与改密）。
前端调用点：`/api/accounts`、`/api/my-accounts`、`/api/me/password`
（`web/static/js/components/account-form.js`、`web/static/js/components/my-accounts.js`）提交的密码经本模块
加密落库——格式或密钥口径变化会直接影响这些页面保存/校验账号的成功与失败。
"""

import logging
import os
import secrets
import threading

from Crypto.Cipher import AES

from yiban.infra import (
    env_io,  # 同目录共享模块：.env 解析单一实现
    env_lock,
)

logger = logging.getLogger("yiban-crypto")

# 密文对象格式版本（AES-256-GCM，v1）
SCHEMA_VERSION = 1
DEFAULT_ENV_FILE = ".env"

# 进程内密钥缓存：dict[env_file] -> key（bytes），**按来源分开存**。
# 共用一个槽位就会串钥——load_key(a) 之后再 load_key(b) 既不读 b 的盘也不打缓存，
# 直接返回 a 的钥，而同进程的 has_key(b)=True 会说反话。
_KEY_CACHE = None
# 来源条目上限（2）：只在"新来源且已满"时整体腾空，再从当前来源重新积累——越限后
# 缓存里实际只剩当前这 1 格，腾空前那些来源下次一律重新读盘解析。单位是来源字符串——
# 同一个物理 .env 的不同拼写（相对路径 / 绝对路径 / 带 ./ 前缀）各占一格；
# 防 env_file 取值无界时缓存无限增长
_CACHE_MAX = 2
# 建钥互斥：防多线程首启各自生成不同密钥互相覆盖（跨进程已由 _write_key_to_env_file
# 的"写前重读"缓解，此处封同进程竞态）
_KEY_LOCK = threading.Lock()


def load_key(env_file=None):
    """获取加密密钥：环境变量 YIBAN_ACCOUNTS_KEY 优先，回退 .env 同键。

    两者都不存在时生成随机 32 字节密钥并持久化到 .env（0600）后返回；
    同一 env_file 的钥在同一进程内缓存复用（避免每次读 .env，见 _KEY_CACHE）。
    读-生成-写-缓存全程持 _KEY_LOCK：多线程首启只生成一份密钥。
    自动建钥会抛错而不落盘（调用方须按"启动失败"处理）：密钥来源不确定（M3 守卫），
    或既有 .env 有行含潜伏行分隔符（见 _write_key_to_env_file）。

    **来源守卫（M3）**：自动建钥只允许在"密钥来源确定"时发生——调用方显式传了
    `env_file`、或设了 `YIBAN_ENV_FILE`、或当前目录已有 `.env`。三者都没有而该
    路径又要**写**密文时，就地生成会在错误目录落一份游离 `.env` 与新密钥
    （与 `db._assert_key_source_certain` 同源缺陷；db 依赖本模块不能反向 import，
    故此处按同口径就地复刻）。只读解密路径（`has_key` 先判）不受影响。
    """
    explicit = env_file is not None
    env_file = env_file or (os.environ.get("YIBAN_ENV_FILE") or "").strip() \
        or DEFAULT_ENV_FILE
    env_key = os.environ.get("YIBAN_ACCOUNTS_KEY", "").strip()
    if env_key:
        # 环境变量档每次现取现解码，既不读缓存也不落缓存：同进程改环境变量必须当场换钥，
        # 而落缓存会让它在撤掉后继续冒充 .env 的钥（缓存的每一格都只代表它的来源）。
        return _decode_key(env_key)
    cached = _cache_get(env_file)
    if cached is not None:
        return cached
    with _KEY_LOCK:
        cached = _cache_get(env_file)  # 双检：等锁期间他线程已解析完
        if cached is not None:
            return cached
        _assert_source_certain(env_file, explicit)
        file_key = _parse_env_file(env_file).get("YIBAN_ACCOUNTS_KEY", "").strip()
        if file_key:
            key = _decode_key(file_key)
            _cache_put(env_file, key)
            return key
        logger.info("未找到 YIBAN_ACCOUNTS_KEY，已生成新密钥并写入 %s（chmod 600）", env_file)
        key = _write_key_to_env_file(env_file, secrets.token_bytes(32))
        _cache_put(env_file, key)
        return key


def _cache_get(source):
    """按来源取缓存密钥；None（含被整体置 None 的复位）视为空缓存。"""
    if _KEY_CACHE is None:
        return None
    return _KEY_CACHE.get(source)


def _cache_put(source, key):
    """把解析结果按来源落缓存；新来源且已满时整体腾空，再从当前来源重新积累（上限 2）。"""
    global _KEY_CACHE
    if _KEY_CACHE is None:
        _KEY_CACHE = {}
    if source not in _KEY_CACHE and len(_KEY_CACHE) >= _CACHE_MAX:
        _KEY_CACHE.clear()
    _KEY_CACHE[source] = key


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
    # 故意不查 _KEY_CACHE：本函数每次读盘答"这个来源有没有钥"，load_key 允许走缓存答"本轮用哪把钥"
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


def _check_key(key):
    """解密前校验密钥：非 bytes / 非 32 字节 → ValueError（消息与 _decode_key 同口径）。

    不校验的话 `AES.new(None, …)` 抛 TypeError、错长度的钥抛 pycryptodome 的
    "Incorrect AES key length"，而调用方（db / 运维脚本）只 `except ValueError`，
    会冒成未分类异常。16 字节的钥不得放行——它会被 AES 当 AES-128 接受，
    报成"密钥不匹配"掩盖真正的钥长约定。
    """
    if not isinstance(key, (bytes, bytearray)):
        raise ValueError("YIBAN_ACCOUNTS_KEY 格式非法：应为 32 字节的 bytes")
    if len(key) != 32:
        raise ValueError("YIBAN_ACCOUNTS_KEY 长度非法：应为 32 字节（64 位十六进制）")


def decrypt_password(entry, key, phone):
    """解密密文对象为明文 str。

    key 不是 32 字节 bytes / entry 不是密文对象 / 密文被篡改 / 密钥不匹配 /
    AAD 手机号不匹配（tag 校验失败）时抛 ValueError——绝不静默返回错误结果。
    """
    _check_key(key)
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

    key 不是 32 字节 bytes / entry 不是密文对象 / 密文被篡改 / 密钥不匹配
    （tag 校验失败）时抛 ValueError——绝不静默返回错误结果。
    """
    _check_key(key)
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
    行模型（窄行读 + 逐行行分隔符校验 + 键行折叠 + 原子 0600 替换）下沉在
    `env_io.write_env_key`，与审计密钥/追踪盐两处写入方共用同一份实现；
    既有行含潜伏行分隔符（U+2028 等）时由它抛 ValueError 且磁盘上一个字节都不改。
    """
    with env_lock.env_write_lock(env_file):
        existing = _parse_env_file(env_file).get("YIBAN_ACCOUNTS_KEY", "").strip()
        if existing:
            return _decode_key(existing)
        env_io.write_env_key(env_file, "YIBAN_ACCOUNTS_KEY", key.hex())
        return key
