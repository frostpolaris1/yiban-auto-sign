# -*- coding: utf-8 -*-
"""Webhook 推送的配置层：env 读取（含密钥密文解密）、脱敏展示与 URL 安全判定。

只做"读配置 + 判定通道是否可用"，不碰账本与发送；唯一例外是 `get_config` 需要
账本余额（每日剩余条数），故在函数内延迟导入 `ledger`——否则会形成
config → ledger → config 的模块级导入环（ledger 需要本层的 `_env_int` 等）。
"""
import ipaddress
import json
import logging
import os
from urllib.parse import urlparse

from yiban.infra import account_crypto, env_io

logger = logging.getLogger("notify")

_PREFIX = "YIBAN_NOTIFY_"


DEFAULT_COOLDOWN = 60
DEFAULT_DAILY_MAX = 5
# 紧急告警另开一本独立额度，保证噪声烧完非紧急额度后仍有手机通道
DEFAULT_URGENT_DAILY_MAX = 3
# 登录失败告警独立账本的日额度默认值；键名无 NOTIFY_ 前缀（独立
# 命名），读取口径（环境变量优先、回退 .env、非法值回退默认）与其他 notify 键一致
DEFAULT_LOGINFAIL_DAILY_MAX = 3
LOGINFAIL_DAILY_MAX_KEY = "YIBAN_LOGINFAIL_DAILY_MAX"


def _env_path():
    """本模块解析 .env 的唯一口径：YIBAN_ENV_FILE 优先（去空白），否则当前目录 .env。"""
    return env_io.env_path()


def _read_env_file():
    """读取 .env（utf-8-sig 兼容 BOM），供读配置用（宽松策略，实现见 env_io）。"""
    return env_io.parse_env_file(_env_path())


def _env_str(key, envs=None):
    """环境变量优先，回退 .env（与 web/signin 惯例一致）。

    envs：调用方本轮已解析好的 .env 快照（_read_env_file() 的返回值）。不传则本函数
    自己读文件——一次调用读一遍全文件，get_config 里 6 个键就是 6 次磁盘 + 6 次解析，
    故允许把同一轮的解析结果传进来复用。
    """
    value = os.environ.get(_PREFIX + key, "").strip()
    if value:
        return value
    if envs is None:
        envs = _read_env_file()
    return envs.get(_PREFIX + key, "").strip()


def _env_int(key, default, envs=None):
    try:
        return max(0, int(_env_str(key, envs)))
    except (TypeError, ValueError):
        return default


def _mask_secret(secret):
    """密钥打码：保留前 3 位，其余星号。空返回空。"""
    if not secret:
        return ""
    if len(secret) <= 6:
        return secret[:2] + "**"
    return secret[:3] + "*" * max(4, len(secret) - 3)


def _host_of(url):
    """脱敏 URL 描述：仅 scheme://host[:port]，不含 userinfo/路径/查询（token 不外泄）。

    与 signin._notify_url_desc 同口径，供日志使用。
    """
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(url)
        if parts.hostname:
            desc = f"{parts.scheme}://{parts.hostname}"
            if parts.port:
                desc += f":{parts.port}"
            return desc
    except ValueError:
        pass
    return "<无法解析>"


def is_safe_url(url):
    """自定义通知地址 SSRF 白名单：https + 非回环/内网/链路本地/未指定。

    is_safe_url 是本口径的**唯一**实现：自定义通知地址走 SSRF 白名单（https + 非回环/
    内网/链路本地/未指定），防 http 明文外泄与 SSRF 跳板。域名目标放行（DNS rebinding
    由超时兜底）。
    """
    try:
        o = urlparse(url)
    except ValueError:
        return False
    if o.scheme != "https" or not o.hostname:
        return False
    host = o.hostname.strip().lower()
    if host == "localhost":
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True  # 域名：非 IP 字面量，放行
    return not (ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_unspecified)


# ---------------------------------------------------------------------------
# 配置读取（加密存储）
# ---------------------------------------------------------------------------

def get_secret(envs=None):
    """返回当前加密配置解出的明文密钥（serverchan=SendKey；custom=URL）。

    未配置 / 密文损坏 / 密钥不匹配均返回空（并记日志），绝不抛异常。
    envs：调用方本轮已解析的 .env 快照，省略则自行读取（见 _env_str）。
    """
    enc = _env_str("SECRET_ENC", envs)
    if not enc:
        return _env_str("URL", envs) or ""  # 兼容旧明文 YIBAN_NOTIFY_URL
    try:
        entry = json.loads(enc)
    except ValueError:
        logger.warning("YIBAN_NOTIFY_SECRET_ENC 解析失败，消息推送不可用")
        return ""
    try:
        # 必须显式传路径。load_key() 不带参数会回落到 cwd/.env，而本模块
        # 的密文是按 YIBAN_ENV_FILE 读的——容器里 cwd=/app、真实配置在 /data/.env，
        # 于是"cwd 下没有 .env"→ 就地生成一把游离新密钥并写盘（P2-5 同源），
        # 结果是用错钥解密 → 推送通道静默死亡，还额外在镜像工作目录留下密钥文件。
        return account_crypto.decrypt_text(entry, account_crypto.load_key(_env_path()))
    except (ValueError, OSError) as e:
        # OSError：密钥文件存在但读不到（权限/占用），按"解不出"处理而非炸主流程
        logger.warning("消息推送密钥解密失败: %s", e)
        return ""


def get_config():
    """配置概览（脱敏），供设置页/日志展示。"""
    # 每日上限与余额的口径在 ledger 层（要读账本锁与磁盘账本）；此处函数内导入
    # 以免 config → ledger → config 的模块级循环。
    from . import ledger

    # 本轮所有键共用一份 .env 解析结果：下面 6 个字段各读一遍文件是 6 次磁盘 + 6 次
    # 全文件解析，设置页轮询时这笔开销并不便宜
    envs = _read_env_file()
    ntype = _env_str("TYPE", envs).strip().lower()
    secret = get_secret(envs)
    if not ntype and secret:
        ntype = "custom"  # 兼容旧明文 YIBAN_NOTIFY_URL
    enabled = bool(ntype and secret)
    # 上限各解析一次，紧接着复用给 daily_max / daily_remaining（原两者各自再读一遍文件）
    general_max = ledger._daily_limit("general", envs)
    urgent_max = ledger._daily_limit("urgent", envs)
    return {
        "ok": True,
        "enabled": enabled,
        "type": ntype if enabled else "",
        "secret_masked": _mask_secret(secret) if enabled else "",
        "configured": bool(ntype or secret),
        "cooldown": _env_int("COOLDOWN", DEFAULT_COOLDOWN, envs),
        "urgent_only": bool(_env_int("URGENT_ONLY", 0, envs)),
        # daily_* 两字段语义收窄为「非紧急账」（字段名不变，前端与既有
        # 调用方无需改），紧急账并列暴露为 urgent_daily_*
        "daily_max": general_max,
        "daily_remaining": ledger._daily_remaining("general", general_max),
        "urgent_daily_max": urgent_max,
        "urgent_daily_remaining": ledger._daily_remaining("urgent", urgent_max),
    }


def is_configured():
    """推送通道是否已配置可用：类型已设（或回退旧明文 URL）且密钥可解出。

    与 send() 自身的未配置短路同一口径——未配置时 send 必然返回 False，
    先判定可省一次发送尝试。供调用方在发送前判断「推送出口是否存在」
    （如 signin 的即时告警门控、汇总告警收件人为空时的推送兜底）。
    """
    envs = _read_env_file()
    secret = get_secret(envs)
    ntype = _env_str("TYPE", envs).strip().lower() or ("custom" if secret else "")
    return bool(ntype and secret)
