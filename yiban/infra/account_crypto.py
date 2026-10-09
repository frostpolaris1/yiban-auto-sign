# SPDX-License-Identifier: AGPL-3.0-only
"""**功能**
易班账号敏感字段加密（AES-GCM）：web / signin 双进程共享。

- 存储层加密：账号的 password/phone_code 字段为密文对象（yiban.db 的 accounts 表，
  迁移前是 accounts.json）
- 密钥：环境变量 YIBAN_ACCOUNTS_KEY → 回退 .env 同键 → 缺失时生成并持久化（0600）
- AAD = 手机号（防密文跨账号互换）；解密 tag 校验失败即抛错

密文对象格式（v2，携带密钥标识 kid）：
    {"v": 2, "kid": "<16位hex>", "nonce": "<hex>", "ct": "<hex>", "tag": "<hex>"}
    kid = HMAC-SHA256(密钥, 固定上下文) 前 8 字节的十六进制——单向指纹，只用于
    "自证这份密文是哪把钥写的 / 当前钥是不是那把"，不可反推密钥材料。

兼容：v1 密文（{"v": 1, …}，无 kid）永久可读——按 tag 校验，行为与 v2 落地前逐字
一致（既有库与 `.env` 密文面不强制迁移）；v2 密文解密前先比 kid，错钥立刻拿到
"密文 kid vs 当前钥 kid"两个可比对的指纹，而不是等 AES tag 撞败后只知道"解不开"。
新写入一律 v2（含明文自愈回写与轮换后的手工重写）。

⚠️ 密钥丢失 = 已加密的账号密码不可恢复：备份数据时必须连同密钥一起备份
（密钥与数据分开放，如 .env 与 yiban.db 分开打包）。

**归属**
`yiban.infra` 的加密基础设施（无项目内依赖），是账号密文的唯一实现；web 与 signin
两个进程共享同一份密钥与格式。

**复用**
两族入口各有分工，别拿错：`encrypt_password` / `decrypt_password` 是账号字段口径
（AAD = 手机号，密文绑定所属账号），`encrypt_text` / `decrypt_text` 是固定 AAD 的
通用口径（通知 SendKey / webhook URL / 邮件 SMTPS 这类配置值）。二者共用 v2 密文格式
（v1 可读）与 `load_key` / `has_key` / `is_encrypted` / `SCHEMA_VERSION`；`.env` 读写复用同目录
`env_io`，跨进程锁复用 `env_lock`。

**通信**
输入：明文敏感字段 + 手机号（AAD）、密钥来源（环境变量或 .env 路径）。
输出：v2 密文对象（JSON 可序列化，携带 kid；v1 无 kid 密文照常可读）或解密后的明文；
密钥缺失时按 0600 生成并持久化。启动/取钥时两档（env 与 .env）都做同钥断言。
调用谁：`yiban.infra.env_io`、`yiban.infra.env_lock`、`Crypto.Cipher.AES`。
谁调用（import 点，未必穷尽）：`yiban.engine.accounts`、`yiban.store.accounts`、
`yiban.store.session_cache`、`yiban.store.migrations`（账号侧加解密）、
`yiban.notify.config`、`yiban.mail.config`、`web/routes/notify.py`、`web/security.py`
（配置密钥侧）。
前端调用点：`/api/accounts`、`/api/my-accounts`、`/api/me/password`
（`web/static/js/components/account-form.js`、`web/static/js/components/my-accounts.js`）提交的密码经本模块
加密落库——格式或密钥口径变化会直接影响这些页面保存/校验账号的成功与失败。
"""

import hashlib
import hmac
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

# 密文对象格式版本（AES-256-GCM）：v2 起携带 kid（密钥单向指纹）；v1（无 kid）
# 永久可读——存量库内两列与 .env 密文面不强制迁移，读出按需重写。
SCHEMA_VERSION = 2
SUPPORTED_SCHEMA_VERSIONS = (1, 2)
# kid 派生上下文：域分隔常量，防"同钥同式子"与其他 HMAC 用途产出同值指纹。
KID_CONTEXT = b"yiban-accounts-kid-v1"
# kid 取 HMAC-SHA256 前 8 字节（16 位 hex）：肉眼可比对、日志可打印，8 字节截断
# 只影响碰撞概率，不影响"由 kid 反推密钥"的不可能性。
KID_HEX_LEN = 16
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
    自动建钥会抛错而不落盘（调用方须按"启动失败"处理）：密钥来源不确定（来源守卫），
    或既有 .env 有行含潜伏行分隔符（见 _write_key_to_env_file）。配置值（环境变量与
    .env 两档）命中公开模板内置示例钥同样抛 ValueError——精确比对、零误杀、即阻断
    （见 _PUBLISHED_EXAMPLE_KEY）。
    **两侧同钥断言（fail-closed，见 _assert_env_matches_env_file）**：环境变量档命中
    时若目标 .env 也带 YIBAN_ACCOUNTS_KEY，两档必须解出同一把钥，不一致即抛——
    "env 优先于 .env"的静默覆盖正是现网 web（EnvironmentFile 注入）与引擎（读 .env）
    分叉的成因，这里把它从"另一侧静默解不开"换成"本侧立即拒启动"。

    **来源守卫**：自动建钥只允许在"密钥来源确定"时发生——调用方显式传了
    `env_file`、或设了 `YIBAN_ENV_FILE`、或当前目录已有 `.env`。三者都没有而该
    路径又要**写**密文时，就地生成会在错误目录落一份游离 `.env` 与新密钥
    （与 `db._assert_key_source_certain` 同源缺陷；db 依赖本模块不能反向 import，
    故此处按同口径就地复刻）。只读解密路径（`has_key` 先判）不受影响。
    """
    explicit = env_file is not None
    env_file = env_file or env_io.env_path(default=DEFAULT_ENV_FILE)
    env_key = os.environ.get("YIBAN_ACCOUNTS_KEY", "").strip()
    if env_key:
        # 环境变量档每次现取现解码，既不读缓存也不落缓存：同进程改环境变量必须当场换钥，
        # 而落缓存会让它在撤掉后继续冒充 .env 的钥（缓存的每一格都只代表它的来源）。
        key = _decode_key(env_key)
        _assert_env_matches_env_file(env_file, key)
        return key
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


def _assert_env_matches_env_file(env_file, env_key):
    """env 档密钥已取用时，核对 .env 档：两档都在且不同 ⇒ 抛（同钥才放行）。

    只有一档可读时没有可对比的另一侧，放行（单档形态的"对面会分叉"风险提示在
    `assert_key_sources_agree` 的启动日志里）。对比的是**解码后的字节**——
    十六进制大小写写法不同不算分叉（避免误杀）。.env 档存在但格式非法同样抛：
    那一侧的进程解不出钥，两侧必然不一致，"非法"不是"没有"。
    """
    file_key_raw = _parse_env_file(env_file).get("YIBAN_ACCOUNTS_KEY", "").strip()
    if not file_key_raw:
        return
    file_key = _decode_key(file_key_raw)
    if file_key != env_key:
        raise _key_fork_error(env_key, file_key, env_file)


def assert_key_sources_agree(env_file=None):
    """启动自证：断言"两侧（env 变量档 / .env 文件档）读到同一把钥"，并把 kid 打进日志。

    web `create_app` 与引擎 `runner.main` 启动时各调一次——两侧进程启动即自证同钥；
    不一致抛 ValueError（拒绝启动，fail-closed），绝不退回"env 静默压住 .env"的
    旧拓扑（systemd EnvironmentFile 注入 env 档，照旧流程只改 .env 必致分叉）。
    返回当前生效密钥的 kid（无钥可断言时返回 None），供调用方留痕。
    单档形态下没有可比对的第二侧，不抛，但 env-only 要 WARNING：未被注入的进程
    会在同一文件里自动生成**第二把**钥（正是轮换事故的路径）。
    """
    env_file = env_file or env_io.env_path(default=DEFAULT_ENV_FILE)
    env_key_raw = os.environ.get("YIBAN_ACCOUNTS_KEY", "").strip()
    file_key_raw = _parse_env_file(env_file).get("YIBAN_ACCOUNTS_KEY", "").strip()
    env_key = _decode_key(env_key_raw) if env_key_raw else None
    file_key = _decode_key(file_key_raw) if file_key_raw else None
    if env_key is not None and file_key is not None:
        if env_key != file_key:
            raise _key_fork_error(env_key, file_key, env_file)
        logger.info("账号密钥自证：env 档与 %s 文件档为同一把钥（kid=%s）",
                    env_file, key_fingerprint(env_key))
        return key_fingerprint(env_key)
    if env_key is not None:
        kid = key_fingerprint(env_key)
        logger.warning(
            "账号密钥只来自环境变量档（%s 内无 YIBAN_ACCOUNTS_KEY，当前 kid=%s）——"
            "未注入该环境变量的进程会在这份文件里自动生成第二把钥，轮换时必须两侧"
            "同步维护（见 README『账号凭据密钥泄露处置』）", env_file, kid)
        return kid
    if file_key is not None:
        kid = key_fingerprint(file_key)
        logger.info("账号密钥自证：仅 %s 文件档（kid=%s）", env_file, kid)
        return kid
    # 两档皆无 = 首启尚未建钥（load_key 的自动生成路径自有守卫），无断言对象
    return None


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
    """AES-256-GCM 加密明文为 v2 密文对象（携带 kid）；空明文返回空字符串（保持空值语义）。

    AAD = 手机号（UTF-8）：密文绑定所属账号，跨账号互换密文会在解密时失败。
    """
    if not plain:
        return ""
    _check_key(key)
    nonce = secrets.token_bytes(12)
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
    cipher.update(str(phone).encode("utf-8"))
    ct, tag = cipher.encrypt_and_digest(str(plain).encode("utf-8"))
    return {
        "v": SCHEMA_VERSION,
        "kid": key_fingerprint(key),
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


def key_fingerprint(key):
    """密钥单向指纹（kid）：HMAC-SHA256(key, KID_CONTEXT) 前 8 字节的十六进制（16 位）。

    用途是让"这份密文是哪把钥写的 / 我手上这把钥是不是那把"在**不解密、不泄露密钥
    材料**的前提下可比对可留痕：密文自带 kid，日志与巡检脚本打印 kid 即可自证，
    错钥从"撞 tag 后只知道解不开"变成"两个指纹直接对比"。
    """
    _check_key(key)
    return hmac.new(bytes(key), KID_CONTEXT, hashlib.sha256).digest()[:KID_HEX_LEN // 2].hex()


def _key_fork_error(env_key, file_key, env_file):
    """两侧（env 档 / .env 档）读到两把钥时的统一拒启错误。"""
    return ValueError(
        "YIBAN_ACCOUNTS_KEY 两侧不一致：环境变量档 kid=%s，%s 文件档 kid=%s——"
        "环境变量优先于 .env，这种不对称下 web（systemd EnvironmentFile 注入）与引擎"
        "（读 .env）各用一把钥，一侧写入的密文另一侧静默不可解。拒绝启动；请把两侧"
        "改成同一把钥（或删去过时一侧）后重启，处置步骤见 README『账号凭据密钥泄露处置』。"
        % (key_fingerprint(env_key), env_file, key_fingerprint(file_key))
    )


def _check_entry_version(entry, key):
    """版本兼容 + kid 自证：v1 放行（按 tag），v2 先比 kid 再进 AES。

    返回密文版本号。v2 且 kid 不匹配时抛的 ValueError 同时带密文 kid 与当前钥
    kid——错钥可自证；v1 无 kid 可依，只能撞 tag（兼容红线：既有密文必须仍可解）。
    """
    ver = entry.get("v")
    if ver not in SUPPORTED_SCHEMA_VERSIONS:
        raise ValueError(
            f"不支持的密文版本: {ver}（当前可读: "
            f"{'/'.join(map(str, SUPPORTED_SCHEMA_VERSIONS))}）")
    if ver == 2:
        kid = str(entry.get("kid") or "")
        mine = key_fingerprint(key)
        if kid != mine:
            raise ValueError(
                f"密钥不匹配：密文 kid={kid or '<缺失>'}，当前钥 kid={mine}"
                "（YIBAN_ACCOUNTS_KEY 用错或两侧分叉，见 README 密钥处置）")
    return ver


def decrypt_password(entry, key, phone):
    """解密密文对象为明文 str（v1/v2 均可读，v2 先验 kid）。

    key 不是 32 字节 bytes / entry 不是密文对象 / 版本不可读 / v2 kid 不匹配 /
    密文被篡改 / 密钥不匹配 / AAD 手机号不匹配（tag 校验失败）时抛 ValueError
    ——绝不静默返回错误结果。
    """
    _check_key(key)
    if not is_encrypted(entry):
        raise ValueError("密码字段不是有效的密文对象（缺 v/ct 键）")
    _check_entry_version(entry, key)
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
    _check_key(key)
    nonce = secrets.token_bytes(12)
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
    cipher.update(aad)
    ct, tag = cipher.encrypt_and_digest(str(plain).encode("utf-8"))
    return {
        "v": SCHEMA_VERSION,
        "kid": key_fingerprint(key),
        "nonce": nonce.hex(),
        "ct": ct.hex(),
        "tag": tag.hex(),
    }


def decrypt_text(entry, key, aad=b"yiban-notify"):
    """解密密文对象为明文 str（AAD 固定；v1/v2 均可读，v2 先验 kid）。

    key 不是 32 字节 bytes / entry 不是密文对象 / 版本不可读 / v2 kid 不匹配 /
    密文被篡改 / 密钥不匹配（tag 校验失败）时抛 ValueError——绝不静默返回错误结果。
    """
    _check_key(key)
    if not is_encrypted(entry):
        raise ValueError("密文对象格式非法（缺 v/ct 键）")
    _check_entry_version(entry, key)
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


# 仓库公开示例模板（.env.example 历史上第 18 行）内置的示例钥。它**逃过**下面全部
# 三条弱钥判据——判据 3 比的是**字节值**连续，而模板串连续的是**十六进制字符**
# （解出 01 23 45 67 89 ab cd ef 的 8 字节循环节，既非全零、也非单字节、更非
# 0..31 连续）——但它随公开仓库人人可读，用它 ⇒ 存量密文等同明文。修法裁为
# **精确比对 ⇒ 阻断**：随机钥不可能命中这条定长公开串，零误杀；命中即拒绝启动
# （load_key 抛 ValueError，调用方按启动失败处理）。刻意**不做**通用熵/KDF 检测：
# Web 侧不管理这把钥（没有写侧校验点可挂），把判定做成"读侧启动即崩"的通用判据会
# 把外泄风险换成全站不可用，且撞存量密钥不可轮换的现实约束。
#: `scripts/dev_visual_seed.py` 内置的演示钥（hex）：同样随仓库公开、任何人可读，
#: 照抄进真实 `.env` 会让存量密文等同明文，且三条弱钥判据全不命中（非全零/非单字节/
#: 非字节值连续），故与模板钥一起精确拉黑。演示脚本改为从本常量取用（单一事实源）。
PUBLISHED_DEMO_ACCOUNTS_KEY = (
    "7f3a9c1e5b2d8046af17c3e9b5d2084c6ea93f7b1d5c8042a6e93f1b7d5c2084")
_PUBLISHED_EXAMPLE_KEYS = frozenset({
    bytes.fromhex("0123456789abcdef" * 4),          # 仓库历史模板内置示例钥
    bytes.fromhex(PUBLISHED_DEMO_ACCOUNTS_KEY),     # dev_visual_seed 演示钥
})


def _decode_key(raw):
    """把 hex 字符串密钥解码为 bytes；格式/长度非法抛 ValueError。"""
    try:
        key = bytes.fromhex(raw)
    except (TypeError, ValueError) as e:
        raise ValueError("YIBAN_ACCOUNTS_KEY 格式非法：应为 64 位十六进制字符串") from e
    if len(key) != 32:
        raise ValueError("YIBAN_ACCOUNTS_KEY 长度非法：应为 32 字节（64 位十六进制）")
    # 精确比对公开模板内置串 ⇒ 阻断（判据与理由见 _PUBLISHED_EXAMPLE_KEYS 注释）。
    # 大小写十六进制写法都命中：bytes.fromhex 不分大小写，比对的是解出的字节。
    if key in _PUBLISHED_EXAMPLE_KEYS:
        raise ValueError(
            "YIBAN_ACCOUNTS_KEY 命中仓库公开示例模板内置的示例钥——任何读过本仓库的人"
            "都能解密存量密文，拒绝使用。请生成随机密钥替换（python3 -c "
            '"import secrets;print(secrets.token_hex(32))"）后重启')
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
