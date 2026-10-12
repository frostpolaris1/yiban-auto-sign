# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""追踪盐与加盐匿名哈希域：YIBAN_TRACK_SALT 的取用/落盘与 IP、手机号哈希。

**功能**
- `_track_salt`：取追踪盐——环境变量 `YIBAN_TRACK_SALT` 优先，回退 .env，缺失时按
  来源判定后生成并写盘；进程内缓存（`_TRACK_SALT_CACHE`）与并发锁（`_TRACK_SALT_LOCK`）；
- `_write_track_salt_to_env_file`：把新生成的盐写进 .env（保留其他行、原子替换、创建即 0600）；
- `hash_ip`：IP 的 HMAC-SHA256 加盐哈希（web 限速键与审计 target 的匿名形态）；
- `hash_phone`：手机号的 SHA-256 加盐哈希（稳定匿名关联键，供 time_pref 冷却等按账号
  关联审计记录，同时不把真实手机号写进审计 target）。

**归属**
追踪盐与审计密钥同住一个 .env、共用同一条路径回落链（`init_db(env_file=…)` →
`YIBAN_ENV_FILE` → 当前目录 `.env`，含"来源只能靠 cwd 兜底且文件不存在时拒绝生成"的
防游离落盘判据），故密钥来源解析仍由 `yiban/store/audit_chain.py` 提供、本模块经门面按
属性取；本模块只负责盐的取用与两个哈希口径。加盐哈希是**库内关联键与限速键**的共同
来源，换盐会使全部存量关联失效，故 `hash_phone` 的口径刻意与 `hash_ip` 不同并已在函数
说明中记录理由。

**复用**
`web/app.py` 与 `web/routes/{auth,me,pages}.py` 的审计 target、登录/改密限速键经 db 门面
调 `hash_ip`；`web/routes/my.py` 的审计 target 与 `yiban/store/time_prefs.py` 的保存冷却
查询（经门面按属性取，`db.hash_phone = 替身` 必须被它看见）调 `hash_phone`。

**通信**
`_parse_env_file` / `_resolve_key_env_file` / `_assert_key_source_certain` 与进程内锁
（`_conn_lock` 不涉及本域）一律经 `_facade()` 按属性取——必须按属性取而非模块级
from-import，`db._track_salt = 替身` 一类打桩才会在函数体里生效。跨进程 .env 写锁沿用
真源 `yiban.infra.env_lock.env_write_lock`（与 audit_chain / account_crypto 同一把锁）。
"""
import hashlib
import hmac
import logging
import os
import secrets
import threading

from yiban.infra import env_io, env_lock

logger = logging.getLogger("yiban.store.tracking")


def _facade():
    """db 门面（函数内延迟导入，避免与 `yiban.store.db` 形成导入环）。

    打桩可见性见模块说明：必须按**属性**取而不是模块级 from-import，`db.<名字> = 替身`
    才会在函数体里生效。
    """
    from yiban.store import db
    return db


# 加盐哈希的进程内盐缓存与互斥
_TRACK_SALT_CACHE = None
_TRACK_SALT_LOCK = threading.Lock()


def _write_track_salt_to_env_file(env_file, salt):
    """把新生成的 YIBAN_TRACK_SALT 写入 .env（保留其他行、原子替换、创建即 0600）。

    读-写-替换整体包进共享 env_lock：与 web 写 .env 互斥；锁内仍保留
    “写入前重读”的既有兜底，避免多进程首启竞态覆盖。
    行模型（窄行读 + 逐行行分隔符校验 + 键行折叠 + 原子 0600 替换）下沉在
    `env_io.write_env_key`：与账号密钥、审计密钥两处写入方共用同一份实现，
    值里潜伏的 U+2028 之类不会被实体化成真配置行（盐泄漏 = IP/手机号哈希可离线反查）。
    """
    db = _facade()
    with env_lock.env_write_lock(env_file):
        existing = db._parse_env_file(env_file).get("YIBAN_TRACK_SALT", "").strip()
        if existing:
            return existing
        env_io.write_env_key(env_file, "YIBAN_TRACK_SALT", salt)
        return salt


def _track_salt():
    """获取 IP 加盐哈希用的盐：环境变量优先，回退 .env，缺失时生成。

    .env 路径回落顺序与审计密钥一致：init_db(env_file=…) →
    YIBAN_ENV_FILE → 当前目录 ".env"；来源只能靠 cwd 兜底且文件不存在时拒绝生成。
    """
    global _TRACK_SALT_CACHE
    db = _facade()
    env_file, from_cwd = db._resolve_key_env_file()
    env_salt = os.environ.get("YIBAN_TRACK_SALT", "").strip()
    if env_salt:
        if len(env_salt) < 16:
            # 弱盐告警（不拒绝——存量部署换盐会使既有哈希关联失效）；
            # 盐被猜测即可离线反查 IP/手机号哈希
            logger.warning("YIBAN_TRACK_SALT 长度过短（<16），易被枚举，建议更换为 32 位以上随机串")
        _TRACK_SALT_CACHE = env_salt
        return env_salt
    if _TRACK_SALT_CACHE is not None:
        return _TRACK_SALT_CACHE
    with _TRACK_SALT_LOCK:
        if _TRACK_SALT_CACHE is not None:
            return _TRACK_SALT_CACHE
        file_salt = db._parse_env_file(env_file).get("YIBAN_TRACK_SALT", "").strip()
        if file_salt:
            _TRACK_SALT_CACHE = file_salt
            return file_salt
        db._assert_key_source_certain("追踪盐", env_file, from_cwd)
        logger.info("未找到 YIBAN_TRACK_SALT，已生成新盐并写入 %s（chmod 600）", env_file)
        _TRACK_SALT_CACHE = _write_track_salt_to_env_file(env_file, secrets.token_hex(32))
        return _TRACK_SALT_CACHE


def hash_ip(ip):
    """对 IP 加盐哈希（YIBAN_TRACK_SALT），返回十六进制字符串。

    原实现为 `sha256(salt + ":" + ip)` 字符串拼接——构造上接近 HMAC 但非标准；
    改用 HMAC-SHA256(salt, ip)（密钥前向填充，防长度扩展类问题）。注意：盐与库同盘时
    （.env + yiban.db 同时被拿），IPv4 空间仍可离线枚举还原——本函数用于限速计数/统计，
    不承担凭据级保密。
    """
    salt = _track_salt()
    return hmac.new(salt.encode("utf-8"), str(ip).encode("utf-8"), hashlib.sha256).hexdigest()


def hash_phone(phone):
    """对手机号做稳定匿名哈希（YIBAN_TRACK_SALT），返回十六进制字符串。

    与审计脱敏不同：同一手机号总是得到相同哈希，可供 time_pref 冷却等
    需要按账号关联审计记录的逻辑使用，同时不把真实手机号写入审计 target。

    有意与 hash_ip 的 HMAC 口径不同：本函数的输出会作为**库内关联键**存储
    （time_pref 冷却等），更换算法将使全部存量关联失效；等值查询用途下
    sha256(salt:input) 无现实攻击面（长度扩展需要构造可验证的 MAC，此处
    哈希仅用于存储比对）。评审结论：保持口径并记录理由。
    """
    salt = _track_salt()
    return hashlib.sha256(f"{salt}:{phone}".encode("utf-8")).hexdigest()
