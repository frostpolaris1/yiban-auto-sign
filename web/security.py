# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""安全域：内置管理员凭据与会话判定、口令存储/校验、IP 计数限速、原子落盘。

**功能**
内置（.env）主管理员的会话凭据族 `_new_admin_sid` / `_issue_admin_sid` /
`_admin_session_facts`、身份判定族 `_builtin_admin_email` / `_is_builtin_admin_session`
/ `_effective_role` / `_current_role`、口令存储与校验 `migrate_admin_password_to_hash` /
`_constant_time_dummy` / `reject_default_admin_password` / `check_admin_configured` /
`_builtin_admin_loginable` / `verify_admin`、客户端出口 `_client_ip`、IP 计数表的
回收与窗口计数 `_ip_store_trim` / `_bump_window_count` / `_bump_login_failure`、
敏感口令门禁的档位与旋钮 `_pw_gate_tier` / `_sensitive_gate_params`、账号校验配额/冷却
`_verify_attempt_allowed` / `_verify_fail_cooldown_remaining` / `_record_verify_failure`，
以及原子落盘 `_atomic_write` / `_replace_with_retry`。

**归属**
原 `web/app.py` 的模块级安全辅助，唯一真源在本模块；`web/app.py` 只保留名字面与转发，
把它自己持有、而本模块需要的模块级名字——`.env` 路径与读取器、写路径 `write_env_key` /
`write_env_batch`、整数配置读取器 `load_env_int`、键行计数 `_count_env_key_lines`、
告警出口 `send_notification`、会话绝对期 `SESSION_ABS_TTL_SECONDS`——在调用时刻现取后
注入。进程内锁 `_rate_lock` 的真实定义点在 `web/services/locks.py`（与 `_file_lock`
同址，勿在别处再建一把），本模块按名取用；口令类别正则与主管理员两档下限留在
`web/services/accounts_data.py`，本模块按名取用不自带一份。

**复用**
`_ip_store_trim` 只有一份实现，各限速/失败计数表（登录、注册、导出、详情、账号校验
配额与冷却、敏感口令门禁）在写入路径共用它回收过期条目；窗口计数只有
`_bump_window_count` 一份实现，登录频率、配额与门禁计数共用同一"先判后增"语义；
`_atomic_write` 是"每一次 .env 落盘"的唯一实现（`web/services/env_io.py` 与
`web/services/measure.py` 都以参数接收它）；`verify_admin` 是三处"内置管理员口令比对"
（登录、改密、敏感门禁复核）的单一来源。

**通信**
本模块不反向导入 `web.app`（本仓测试以别名加载 `app.py`，普通 import 会再执行一份副本
模块）。注入的参数都是 `web.app` 上会被测试打桩或赋值改写的模块级名字——既有测试在
`web.app` 上打桩 `read_env` / `send_notification` / `_constant_time_dummy`，又直接赋值
`ENV_FILE`——本模块另持一份绑定会让这些改写静默失效。带 `session` / `request` 的函数
（身份判定、`_client_ip`）只在请求期被调用：`create_app` 启动路径与每日清理线程都只经
参数化的 `migrate_admin_password_to_hash` / `reject_default_admin_password`（它们自带
`env_path`），不依赖请求上下文。
"""

import contextlib
import logging
import os
import re
import secrets
import sys
import time

# 包导入引导：本模块按**文件路径**被直接导入时（`web/app.py` 的别名加载、部署入口
# 的独立探针等），`sys.path[0]` 只是该文件所在目录，仓库根与 scripts/ 都不在上面。
# 本模块 `from yiban.*` 取共享包，又经 `web.services.accounts_data` 传递依赖 scripts/
# 下的 `signin`（账号只读验证探针），故**两段都要**先入 sys.path——缺任一段都会在
# 无引导环境里 ModuleNotFoundError。已存在则不重复插入。
# 引导元变量刻意不叫 `_REPO_ROOT`（先例见 web/render.py）：该名字是 web.app 注入拆分
# 模块的文档根参数名，拆分契约判定拆分模块不得自持同名绑定（防「另存一份绑定」以更
# 隐蔽的形态回归），本模块作为拆分模块同样不例外。
_PACKAGE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPTS_DIR = os.path.join(_PACKAGE_ROOT, "scripts")
for _p in (_SCRIPTS_DIR, _PACKAGE_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from flask import request, session  # noqa: E402
from werkzeug.security import check_password_hash, generate_password_hash  # noqa: E402

from web.services.accounts_data import (  # noqa: E402
    _PASSWORD_CLASS_PATTERNS,
    ADMIN_PASSWORD_MIN_CLASSES,
    ADMIN_PASSWORD_MIN_LEN,
)
from web.services.locks import _rate_lock  # noqa: E402
from yiban.mail import layout as mail_layout  # noqa: E402
from yiban.store import db  # noqa: E402

# 与 web.app 同名的日志通道：安全域的日志落回既有通道，便于运维沿用同一处过滤
logger = logging.getLogger("web")

# 口令哈希算法（werkzeug scrypt，OWASP 推荐参数；check_password_hash 对旧哈希自动兼容）
SCRYPT_METHOD = "scrypt:65536:8:1"

#: 内置主管理员（.env 账号）的会话凭据键名。
ADMIN_SID_ENV_KEY = "YIBAN_ADMIN_SID"

# 登录失败限速：同一 IP 连续失败超过阈值后锁定
LOGIN_LOCK_SECONDS = 300

# 可信第一跳代理（nginx 反代）：仅当请求来自这些地址时才信任转发头。
# 生产部署：yiban-web 只监听回环地址，nginx 反代并以 `proxy_set_header X-Forwarded-For $remote_addr`
# 覆盖设置，故此处读取的 XFF 即真实客户端 IP；客户端伪造的 XFF 会被丢弃。
# 仅回环地址：若改为非回环地址，XFF 可被伪造绕过速率限制——**不要**引入配置项放开这里。
TRUSTED_PROXIES = ("127.0.0.1", "::1")

# IP 计数 dict（限速/登录失败/注册）的条目上限与最长保留：防公网扫描器多 IP 打爆内存
_IP_STORE_LIMIT = 10000
_IP_STORE_MAX_AGE = 3600

# 账号验证尝试限频（2026-08-27 P1-2）：每用户窗口内网络验证次数上限。
# 预验证 = 服务器代发真实易班登录，必须在资格预筛之外再加用户维度节流。
VERIFY_MAX = 6  # 每用户窗口内最大验证尝试次数（正常添加流程远用不到）
VERIFY_WINDOW = 600  # 窗口（秒）= 10 分钟

# 账号验证认证失败冷却（2026-09-04 生产复盘）：同一手机号窗口内认证失败达到
# 阈值后临时拒绝再验证。密码错误属确定性失败，重复验证每次都是一次真实易班
# 登录，连续少量错误易班侧即返回「错误尝试过多」锁定账号（生产实测 6 次即锁），
# 故按「被锁定对象 = 易班账号 = 手机号」设冷却；仅限 web 验证路径，探针与
# cron 签到不受影响。
VERIFY_FAIL_MAX = 2  # 窗口内第 2 次认证失败即触发冷却
VERIFY_FAIL_WINDOW = 900  # 失败计数窗口（秒）= 15 分钟
VERIFY_FAIL_COOLDOWN = 600  # 冷却时长（秒）= 10 分钟
# 确定性认证失败特征（与 signin 登录流程的实际文案对应：usersure reUrl error →
# 「登录失败（账号或密码错误）」，锁定 → msgCN「错误尝试过多…」）
VERIFY_FAIL_AUTH_KEYWORDS = ("账号或密码错误", "密码错误", "错误尝试过多")

# 敏感口令门禁的两个默认窗口（.env 可覆盖，唯一解析处见 _sensitive_gate_params）：
# - 豁免：本会话在本值秒内复核过口令、且出口 IP 未变 → 配置类动作免再输口令。
#   上限 PW_CONFIRM_TTL_MAX 是硬钳——豁免窗口比会话本身还长就等于取消门禁。
# - 冷却：独立计数达阈值后，这段时间内**一切需要复核的写操作**（含正确口令）一律拒绝。
PW_CONFIRM_TTL_DEFAULT = 300
PW_CONFIRM_TTL_MAX = 900
PW_CONFIRM_COOLDOWN_DEFAULT = 300

# 敏感口令门禁的档位（`.env` 键 `YIBAN_PW_GATE`，唯一解析处见 `_pw_gate_tier`）：
# - `full`：每个受保护操作都要当次口令（改造前的行为，逐字保留）；
# - `risk`：**默认档**——只有风控命中（短时密集 / 换出口 IP）才要口令；
# - `off`：永不要求口令，只留倒计时确认与事后告警。
# 非法值回退 `risk` 而不是 `off`：本键是安全件，一个 `.env` 笔误不得把门禁静默拆掉。
PW_GATE_OFF = "off"
PW_GATE_RISK = "risk"
PW_GATE_FULL = "full"
PW_GATE_TIERS = (PW_GATE_OFF, PW_GATE_RISK, PW_GATE_FULL)
PW_GATE_ENV_KEY = "YIBAN_PW_GATE"
PW_GATE_DEFAULT = PW_GATE_RISK

# `risk` 档的风控判据（同 session 危险操作密度）：窗口内达到该次数即升级为"当次要口令"。
# 与豁免 TTL / 复核冷却同处一份门禁参数，改动时一眼能看见三者的关系。
PW_GATE_RISK_WINDOW = 300
PW_GATE_RISK_MAX = 3

# 仓库公开模板（.env.docker.example）自带的字面量默认口令。
# 随仓库公开 = 众所周知字符串，忘改即后台口令为公开知识。
_DEFAULT_ADMIN_LITERALS = frozenset((
    "请修改为强密码",
    "admin123",
    "admin888",
    "123456",
    "12345678",
    "password",
    "admin",
))

#: `os.replace` 的瞬态失败重试预算（Windows 专有失败模式，见 `_replace_with_retry`）
_REPLACE_RETRY_ATTEMPTS = 6
_REPLACE_RETRY_BASE_SEC = 0.05

# 登录时延拉平占位哈希：用户名/账号不存在时也执行一次等价 scrypt 比对，
# 消除「响应耗时差异」造成的用户枚举时序侧信道（占位哈希无需真实有效，比对恒为 False）。
_dummy_pw_hash = None


# ---------------------------------------------------------------------------
# 内置主管理员会话凭据
# ---------------------------------------------------------------------------
def _new_admin_sid():
    """换发一个内置主管理员会话凭据（取值口径与 `users.sid` 同源：32 位 hex，
    不可能含行分隔符，故经 `write_env_batch` 的注入校验必定安全）。"""
    return secrets.token_hex(16)


def _issue_admin_sid(env_path, write_env_key, read_env):
    """为内置主管理员换发会话凭据并落 `.env`，返回**应当写进 session 的值**。

    为什么内置主管理员需要这条凭据：它此前只有 `YIBAN_ADMIN_PW_VERSION` 一个吊销维度，
    而版本号只在改口令时递增——于是本人**登出也踢不掉被盗的 Cookie 副本**（它只能用满
    SESSION_ABS_DAYS 的绝对期），唯一的止损手段是改自己的口令（连带把本人也踢下线）或
    换 `YIBAN_SECRET_KEY`（全站重登）。现在登出/改密/SSH 追回都会换发 sid，与注册用户
    的 `users.sid` 同一条吊销面。

    写失败时返回 `.env` 里的**旧值**而不是新值：本函数的返回值会被写进 session，
    而 `_effective_role` 拿 session 里的 sid 与 `.env` 比对——若落盘失败还返回新值，
    刚登录成功的这个会话自己就成了"sid 不匹配"，等于把管理员锁在门外（`.env` 只读
    或权限坏掉时必然踩中）。沿用旧值则本次登录照常可用，只是这一次换发没生效，
    与口令哈希迁移、`ensure_secret_key` 的降级策略同口径。

    `.env` 写路径与读取器由调用方传入（`web.app` 的 `write_env_key` / `read_env`）：
    两者都是会被测试打桩或赋值改写的模块级名字，本模块另持绑定会让打桩静默失效。
    """
    sid = _new_admin_sid()
    try:
        write_env_key(env_path, ADMIN_SID_ENV_KEY, sid)
    except OSError as e:
        logger.error(
            "内置主管理员会话凭据落盘失败（%s 不可写？）：%s；本次登录沿用旧值，"
            "服务端吊销面暂时缺位，请修复权限", env_path, e,
        )
        return read_env(env_path).get(ADMIN_SID_ENV_KEY, "").strip()
    return sid


def _admin_session_facts(env_path, read_env):
    """内置主管理员的两项会话吊销凭据，返回 (口令版本, 当前 sid)。

    一次 `.env` 读取取全两项：`_effective_role` 每个请求都要判一次，原实现已经为
    `YIBAN_ADMIN_PW_VERSION` 读一遍文件，若再加一遍就是把它的热路径开销翻倍。
    版本解析与 `load_env_int` 同语义（缺失/非法回退 1）；sid 缺失或空串返回 ""
    （= 未签发，存量部署兼容口径见 `_effective_role`）。

    `.env` 读取器由调用方传入（`web.app` 的 `read_env`）：它会被测试打桩，本模块
    另持绑定会让打桩静默失效。
    """
    env = read_env(env_path)
    try:
        version = max(0, int(env.get("YIBAN_ADMIN_PW_VERSION", "")))
    except (TypeError, ValueError):
        version = 1
    return version, (env.get(ADMIN_SID_ENV_KEY) or "").strip()


# ---------------------------------------------------------------------------
# 身份与角色判定（仅在请求期调用）
# ---------------------------------------------------------------------------
def _builtin_admin_email(env_path, read_env):
    """内置管理员（.env）标识（小写），用于防呆比较：不可改角色/删除。"""
    env = read_env(env_path)
    return env.get("YIBAN_ADMIN_USER", "").strip().lower()


def _is_builtin_admin_session(env_path, read_env):
    """当前会话是否确实是内置管理员（.env）登录，而非同邮箱注册用户/注册管理员。

    定为模块级而非 create_app 局部：个人域路由已迁往 web/routes/me.py，需按模块
    属性取用（打桩面要求）；其余调用点按模块全局名解析，取值与闭包直读一致。
    """
    return (
        session.get("auth_source") == "builtin"
        and str(session.get("username") or "").strip().lower()
        == _builtin_admin_email(env_path, read_env)
    )


def _effective_role(username, pw_version, env_path, read_env):
    """实时角色判定（每次请求读取，不依赖登录时固化的 session）：
    内置管理员 → admin；注册用户 → users 表的 role；查无此人 → None。
    管理员变更角色后，已登录用户的下一次请求立即生效，无需重新登录；
    被删除/取消权限的用户旧会话随之失效（None 视为未登录）；
    注册用户密码被重置/修改后（pw_version 递增）旧会话随之失效；
    内置管理员改密后（.env 版本递增）旧会话同样失效。
    """
    if not username:
        return None
    # 所有会话都必须带 auth_source；旧会话（无该字段）一律视为未登录
    if not session.get("auth_source"):
        return None
    if (
        username.strip().lower() == _builtin_admin_email(env_path, read_env)
        and session.get("auth_source") == "builtin"
    ):
        # 内置管理员：必须是 builtin 登录来源，且 session 里的两项凭据都与当前
        # .env 一致（口令版本 + 会话 sid）；auth_source == "user" 的同名注册用户
        # 继续按普通用户判定，不借内置邮箱提权
        cur, admin_sid = _admin_session_facts(env_path, read_env)
        if pw_version != cur:
            return None
        # 与上面注册用户的 users.sid 逐字同口径：空 = 未签发（升级日存量会话兼容，
        # 不强制重登），签发后不匹配即失效。这条凭据补上的是"改口令之外"的吊销面：
        # 登出即可踢掉被盗副本，不必再靠换口令或换 YIBAN_SECRET_KEY 全站重登。
        if admin_sid and session.get("sid") != admin_sid:
            return None
        return "admin"
    email = username.strip().lower()
    u = db.find_user(email)
    if u is not None:
        # 旧数据（无 pw_version 字段）不做会话吊销校验，兼容存量会话
        if "pw_version" in u and pw_version != u.get("pw_version", 1):
            return None
        # 服务端会话吊销：users.sid 为该用户当前唯一有效会话标识，
        # 登录时签发、登出/被重置密码/被踢时轮换——被盗 cookie 重放即失效。
        # sid 为空串视为未签发（升级日存量会话兼容），签发后不匹配即失效。
        sid = u.get("sid", "")
        if sid and session.get("sid") != sid:
            return None
        return "admin" if u.get("role") == "admin" else "user"
    return None


def _current_role(abs_ttl_seconds, env_path, read_env):
    """当前登录会话的实时角色；未登录 → None。

    会话绝对过期：滑动续期（14 天）之外另设「自登录起最多 N 天」硬上限，防止被盗
    Cookie 永久续命。时间戳在登录/恢复时写入 session["login_ts"]；存量旧会话无该
    字段则就地补记当下（升级日不强制全体重新登录）。超限即清空会话视为未登录。

    绝对期秒数由调用方传入（`web.app` 的 `SESSION_ABS_TTL_SECONDS`）：它在 create_app
    里按 `YIBAN_SESSION_ABS_DAYS` 重新解析后回写模块全局，本模块自持一份会读到启动
    前的默认值，会话吊销时限静默失准。

    模块级而非工厂局部：`web/routes/*` 的路由体经 `web.routes.appmod()` 取用本函数的
    转发包装（打桩面要求，见该函数的说明）。
    """
    if not session.get("auth"):
        return None
    ts = session.get("login_ts")
    now = time.time()
    if not isinstance(ts, (int, float)) or ts <= 0:
        session["login_ts"] = int(now)
    elif now - ts > abs_ttl_seconds:
        session.clear()
        return None
    return _effective_role(session.get("username"), session.get("pw_version"),
                           env_path, read_env)


# ---------------------------------------------------------------------------
# 口令存储形态与启动检测
# ---------------------------------------------------------------------------
def migrate_admin_password_to_hash(env_path, read_env, load_env_int, write_env_batch):
    """启动时安全迁移：检测到管理员口令以明文（YIBAN_ADMIN_PASSWORD）存储且无哈希时，
    自动生成 scrypt 哈希写入 YIBAN_ADMIN_PASSWORD_HASH 并清空明文。

    说明：仅改变口令的存储形态（明文 → 哈希），口令本身不变；已有哈希则跳过；
    明文回退比对路径（verify_admin）保留以兼容未迁移的存量部署。
    迁移失败（如 .env 对进程不可写）只告警不阻断启动——明文回退仍可登录。

    （SSH 追回路径堵漏）：检测到「明文与现存哈希不一致」——即运维通过
    SSH 重设了 YIBAN_ADMIN_PASSWORD（主管理员被盗后的追回操作）——重迁移哈希的
    同时递增 YIBAN_ADMIN_PW_VERSION，使全部被盗旧会话立即失效。原实现哈希存在
    即跳过，导致重设的明文被忽略（verify_admin 哈希优先）、攻击者旧密码 + 旧
    cookie 双通道继续掌控。
    """
    rotated = False
    try:
        env = read_env(env_path)
        existing_hash = env.get("YIBAN_ADMIN_PASSWORD_HASH", "").strip()
        plain = env.get("YIBAN_ADMIN_PASSWORD", "").strip()
        if not plain:
            return
        updates = {
            "YIBAN_ADMIN_PASSWORD_HASH": generate_password_hash(plain, method=SCRYPT_METHOD),
            "YIBAN_ADMIN_PASSWORD": "",
        }
        if existing_hash:
            try:
                same = check_password_hash(existing_hash, plain)
            except (ValueError, TypeError):
                same = False
            if same:
                return  # 明文与哈希一致（重复启动），无需任何写入
            cur_pwv = load_env_int(env_path, "YIBAN_ADMIN_PW_VERSION", 1)
            updates["YIBAN_ADMIN_PW_VERSION"] = str(cur_pwv + 1)
            # 追回 = 假定会话已失窃：与版本号一并在这一次批量写里换发内置会话凭据
            # （共用 write_env_batch = 单次原子写，既不多写一遍 .env，也不留下
            # "版本已递增、sid 还是旧的"的半成品状态）
            updates[ADMIN_SID_ENV_KEY] = _new_admin_sid()
            rotated = True
        write_env_batch(env_path, updates)
    except OSError as e:
        logger.warning(
            "管理员口令明文迁移失败（%s 不可写？）：%s；将暂时回退明文比对，"
            "请修复权限后重启或手动改密",
            env_path,
            e,
        )
        return
    if rotated:
        logger.warning(
            "检测到管理员口令被外部更改（%s，明文与现存哈希不一致）：已重迁移哈希"
            "并递增 YIBAN_ADMIN_PW_VERSION，全部旧会话已失效（SSH 追回场景）",
            env_path,
        )
    else:
        logger.warning(
            "检测到管理员口令明文存储（%s），已自动迁移为 scrypt 哈希并清空明文；"
            "口令本身未变更，请确认其强度足够（弱口令仍可被猜测）",
            env_path,
        )


def _constant_time_dummy(password):
    """对不存在的账号执行一次与真实校验等价的 scrypt 比对（耗时拉平）。"""
    global _dummy_pw_hash
    if _dummy_pw_hash is None:
        _dummy_pw_hash = generate_password_hash("dummy-placeholder", method=SCRYPT_METHOD)
    check_password_hash(_dummy_pw_hash, password)


def reject_default_admin_password(env_path, read_env):
    """启动检测：内置管理员仍为公开模板默认字面量/弱口令时拒绝启动（fail-closed）。

    仓库公开，忘改 YIBAN_ADMIN_PASSWORD 即主管理员口令为众所周
    知字符串，且此前应用侧无任何检测。仅检查明文口令（纯哈希部署无法逆向检查；
    迁移会清空明文，故本检测必须在 migrate_admin_password_to_hash 之前执行）。
    抛 SystemExit 使 gunicorn worker 退出——supervisor/systemd 会带清晰日志重启，
    运维按提示改口令即可，宁可起不来也不能带着公开口令上线。
    """
    try:
        plain = str(read_env(env_path).get("YIBAN_ADMIN_PASSWORD", "") or "").strip()
    except Exception:
        return
    if not plain:
        return
    if plain in _DEFAULT_ADMIN_LITERALS:
        logger.critical(
            "拒绝启动：YIBAN_ADMIN_PASSWORD 为公开模板默认字面量。"
            "请编辑 .env 将其改为强密码（或在设置哈希 YIBAN_ADMIN_PASSWORD_HASH 后"
            "清空明文），再重启服务。",
        )
        raise SystemExit(2)
    classes = sum(bool(re.search(pat, plain)) for pat in _PASSWORD_CLASS_PATTERNS)
    if len(plain) < ADMIN_PASSWORD_MIN_LEN or classes < ADMIN_PASSWORD_MIN_CLASSES:
        logger.critical(
            "拒绝启动：YIBAN_ADMIN_PASSWORD 弱于主管理员口令策略（至少 %d 位，且包含"
            "大写字母、小写字母、数字、符号中的至少 %d 类）。请编辑 .env 将其改为强密码"
            "（或在设置哈希 YIBAN_ADMIN_PASSWORD_HASH 后清空明文），再重启服务。",
            ADMIN_PASSWORD_MIN_LEN,
            ADMIN_PASSWORD_MIN_CLASSES,
        )
        raise SystemExit(2)


# ---------------------------------------------------------------------------
# 原子落盘
# ---------------------------------------------------------------------------
def _replace_with_retry(tmp, path):
    """`os.replace` + 瞬态失败重试；最终失败时**清掉临时文件**再原样抛出。

    为什么要重试：Windows 上"目标文件正被别的句柄打开"时替换会被拒（`WinError 5`
    拒绝访问 → Python 抛 `PermissionError`），而 `.env` 是**高频读取**的文件——同一
    进程其他线程的 `read_env`、引擎子进程、预览工具都可能正打开着它。2026-09-17 实测
    复现：一边持续读 `.env`、一边连续原子写，400 次写入**全部** WinError 5 失败（同期
    读 3.7 万次），表现为设置页/执行体页偶发 500。Windows 的 `open()` 不带
    `FILE_SHARE_DELETE`，读句柄会让替换失败；Linux 的 `rename` 不受读者影响，故生产
    形态（Linux）不涉及，但本机开发与 Windows 本机部署会踩到。

    失败时**必须删掉临时文件**：它是一份完整的 `.env` 副本（含密钥、口令哈希），
    留在磁盘上既是不该有的凭据副本，也会越攒越多（实测残留 256 个）。
    """
    delay = _REPLACE_RETRY_BASE_SEC
    for attempt in range(1, _REPLACE_RETRY_ATTEMPTS + 1):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt >= _REPLACE_RETRY_ATTEMPTS:
                with contextlib.suppress(OSError):
                    os.remove(tmp)
                raise
            time.sleep(delay)
            delay *= 2


def _atomic_write(path, content, chmod_priv=False):
    """原子写文件：先写临时文件再替换，避免半写状态（cron 并发读取安全）。

    chmod_priv=True 时写完后收紧为 0600（含密钥/口令的 .env 场景），
    防止默认 umask 下产生同主机其他用户可读的宽松权限。
    临时文件改为创建即 0600（os.open + fdopen，与
    account_crypto._write_key_to_env_file 口径一致）——open("w") 在默认 umask 下
    0644，写完到 replace 之间（及进程崩溃残留时）文件对同机其他用户可读；
    收尾的 os.chmod 保留（对既有 0644 旧文件幂等收紧，无害）。

    替换这一步走 `_replace_with_retry`（Windows 上并发读者会让 `os.replace` 瞬态失败）。
    """
    tmp = f"{path}.tmp{secrets.token_hex(4)}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())  # 落盘再替换：极端掉电场景不丢数据
    _replace_with_retry(tmp, path)
    if chmod_priv:
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)  # 仅属主可读写（Windows 无实际效果，忽略失败）


# ---------------------------------------------------------------------------
# 客户端出口与 IP 计数表
# ---------------------------------------------------------------------------
def _client_ip():
    """真实客户端 IP（限速/锁定/审计按真实 IP 隔离，防反代后全站共享同一桶）。

    - 反代场景：remote_addr 为代理地址且第一跳可信 → 取 X-Forwarded-For 首个值（nginx 已覆盖，不可伪造）；
    - 直连场景（无转发头/首跳不可信）：回退 remote_addr。
    注意：本函数假设应用不直接暴露公网（17892 仅监听回环 + 防火墙放行 22/443）。
    """
    r = request.remote_addr or "?"
    if r in TRUSTED_PROXIES:
        xff = request.headers.get("X-Forwarded-For", "")
        first = xff.split(",")[0].strip() if xff else ""
        if first and first != r:
            return first
    return r


def _ip_store_trim(store, max_age):
    """IP 计数 dict 超限时清理过期条目：仅当长度超上限才遍历，避免每请求开销。

    各 store 的值为二元/三元组，末位统一是时间戳；防止公网扫描器用海量
    不同 IP 打爆内存（无界增长 DoS）。
    由 create_app 内嵌套函数上提为模块级——_verify_attempt_allowed
    等模块级写入路径也要在同一口径下 trim，嵌套作用域够不到。
    """
    if len(store) <= _IP_STORE_LIMIT:
        return
    now = time.time()
    stale = [k for k, v in store.items() if now - v[-1] > max_age]
    for k in stale:
        store.pop(k, None)


def _bump_window_count(store, key, now, window, limit=None):
    """锁内递增窗口计数，返回 (count, window_start, allowed)。

    - limit 为 None：总是递增，allowed 恒为 True；
    - limit 非 None：达到 limit 后不再递增并返回 allowed=False（用于“先判断再递增”的限速语义，
      例如登录频率限制允许第 10 次、拒绝第 11 次）。
    H7：限速计数 dict 的读改写统一走这里，避免并发请求丢失更新。
    """
    with _rate_lock:
        cnt, start = store.get(key, (0, now))
        if now - start > window:
            cnt, start = 0, now
        if limit is not None and cnt >= limit:
            return cnt, start, False
        cnt += 1
        store[key] = (cnt, start)
        return cnt, start, True


def _bump_login_failure(store, key, now):
    """锁内递增失败计数，返回递增后的次数。

    H7：登录/改密/注销/恢复共用失败计数的读改写统一走这里。
    """
    with _rate_lock:
        fails, _, _ = store.get(key, (0, 0, 0))
        fails += 1
        store[key] = (fails, 0, now)
        return fails


def _sensitive_gate_params(env_path, load_env_int):
    """敏感口令门禁两个旋钮的唯一解析处，返回 (豁免 TTL 秒, 冷却秒数)。

    收在一处是为了"豁免窗口不可能被一个 .env 笔误放成永久"：TTL 上界硬钳
    `PW_CONFIRM_TTL_MAX`（900 秒），配得再大也只按 900 生效；0 = 关闭豁免
    （每次复核都要口令）。冷却同为 0 = 关闭（只留告警与独立计数），给运维
    留一条"宁可慢也不要被门禁挡住"的降级路。`load_env_int` 已把负值夹到 0。
    """
    ttl = min(load_env_int(env_path, "YIBAN_PW_CONFIRM_TTL", PW_CONFIRM_TTL_DEFAULT),
              PW_CONFIRM_TTL_MAX)
    cooldown = load_env_int(env_path, "YIBAN_PW_CONFIRM_COOLDOWN_SEC",
                            PW_CONFIRM_COOLDOWN_DEFAULT)
    return ttl, cooldown


def _pw_gate_tier(env_path, read_env):
    """敏感口令门禁的档位（`YIBAN_PW_GATE`）唯一解析处，返回 off/risk/full 之一。

    缺省与非法值都回退 `PW_GATE_DEFAULT`（risk）——本键是安全件，把拼错的档位当成
    `off` 等于一次 `.env` 笔误就静默拆掉全部门禁；反过来误判成 `full` 只是多要几次
    口令，是可接受的失败方向。非法值告警一次，让运维在日志里看见自己的笔误。
    枚举键没有通用读取工具（`load_env_int` 只处理整数），故按 sign_mode / sign_order
    的做法现读现校验。
    """
    raw = str(read_env(env_path).get(PW_GATE_ENV_KEY, "") or "").strip().lower()
    if raw in PW_GATE_TIERS:
        return raw
    if raw:
        logger.warning(
            "%s 的 %s=%r 非法（可选 %s），按 %s 档执行",
            env_path, PW_GATE_ENV_KEY, raw, "/".join(PW_GATE_TIERS), PW_GATE_DEFAULT,
        )
    return PW_GATE_DEFAULT


def _verify_attempt_allowed(store, username):
    """账号验证尝试配额（2026-08-27 对抗性审查 P1-2）。

    「注册/添加账号即时验证」会让服务器代用户向易班发起真实登录，必须防止
    被当作凭据试探的免费代理：在真正发起网络验证前按「会话用户名」扣减配额，
    超过 VERIFY_MAX 次 / VERIFY_WINDOW 秒即拒绝。全局 IP 限速之外的账号维度
    补充；计数语义与登录频率限制一致（先判后增）。store 由调用方传入
    （current_app.extensions 登记，经 web.routes.verify_limits() 取用）。
    """
    # 写入前顺带 trim（键为会话用户名/邮箱，长度有界但基数无界），
    # 与其余 IP 计数表同口径防无界增长
    with _rate_lock:
        _ip_store_trim(store, VERIFY_WINDOW + _IP_STORE_MAX_AGE)
    _, _, allowed = _bump_window_count(
        store,
        (username or "?").lower(),
        time.time(),
        VERIFY_WINDOW,
        limit=VERIFY_MAX,
    )
    return allowed


def _verify_fail_cooldown_remaining(store, phone, now):
    """同一手机号验证冷却剩余秒数（0 = 不在冷却中）。

    store 由调用方传入（current_app.extensions 登记，经 web.routes.verify_fails()
    取用）；gunicorn 单进程多线程，读写统一走 _rate_lock。
    """
    with _rate_lock:
        entry = store.get(phone)
        if not entry:
            return 0
        return max(0, int(entry[2] - now))


def _record_verify_failure(store, phone, message, now):
    """记录一次 web 触发验证的失败，返回原因类别（供审计 detail，不含原始消息）。

    仅「确定性认证失败」（密码错误/账号锁定）计入冷却——网络类失败不是用户
    过错、重试也不增加易班锁定风险；冷却触发后窗口计数清零，冷却到期重新计数。
    """
    if not any(kw in message for kw in VERIFY_FAIL_AUTH_KEYWORDS):
        return "其他失败"
    with _rate_lock:
        fails, window_start, cooldown_until = store.get(phone, (0, 0.0, 0.0))
        if now - window_start > VERIFY_FAIL_WINDOW:
            fails, window_start = 0, now
        fails += 1
        if fails >= VERIFY_FAIL_MAX:
            cooldown_until = now + VERIFY_FAIL_COOLDOWN
            fails, window_start = 0, now
        store[phone] = (fails, window_start, cooldown_until)
        # 防御性清理（同 _ip_store_trim 思路）：窗口与冷却均已过期的条目可回收，
        # 判定用 window_start（冷却至多再延 VERIFY_FAIL_COOLDOWN，已并入 max_age）
        if len(store) > _IP_STORE_LIMIT:
            max_age = VERIFY_FAIL_WINDOW + VERIFY_FAIL_COOLDOWN + _IP_STORE_MAX_AGE
            for k in [k for k, v in store.items() if now - v[1] > max_age]:
                store.pop(k, None)
    return "认证失败"


# ---------------------------------------------------------------------------
# 管理员认证
# ---------------------------------------------------------------------------
def check_admin_configured(env_path, read_env):
    """管理员账号是否已在 .env 配置（口令哈希或旧明文任一即可）。"""
    env = read_env(env_path)
    return bool(
        env.get("YIBAN_ADMIN_USER", "").strip()
        and (
            env.get("YIBAN_ADMIN_PASSWORD_HASH", "").strip()
            or env.get("YIBAN_ADMIN_PASSWORD", "").strip()
        )
    )


def _builtin_admin_loginable(env_path, read_env, count_env_key_lines):
    """内置（.env）主管理员**此刻是否真的进得来**——"至少保留 1 个管理员"的判据。

    只看 `YIBAN_ADMIN_USER` 非空是不够的（原实现如此）。用户名配了而内置实际登不进
    的三种态，`verify_admin` 都会 fail-closed 拒绝：
      1) 哈希与明文都没有（凭据没配齐）；
      2) 只有明文没有哈希（启动迁移写 .env 失败的降级态，明文比对已停用）；
      3) `YIBAN_ADMIN_PASSWORD_HASH` 不是恰好一行（多行歧义，或统计读取失败计得 0）。
    这三种态下若再把最后一个注册管理员降权/删除，Web 管理面**一个入口都不剩**
    （内置进不来 + 注册管理员清空），而态 2 恰恰是"改过 .env 权限"后最容易出现的。
    判据与 `verify_admin` 的三道 fail-closed 逐条对齐，一致性由用例对拍（防两侧漂移）。
    """
    env = read_env(env_path)
    if not env.get("YIBAN_ADMIN_USER", "").strip():
        return False
    if not env.get("YIBAN_ADMIN_PASSWORD_HASH", "").strip():
        return False
    return count_env_key_lines(env_path, "YIBAN_ADMIN_PASSWORD_HASH") == 1


def verify_admin(username, password, env_path, read_env, count_env_key_lines,
                 send_notification, constant_time_dummy):
    """校验管理员账号（每次登录实时读 .env，修改立即生效）。

    口令哈希（YIBAN_ADMIN_PASSWORD_HASH，scrypt）优先；哈希缺失而明文仍在
    （启动迁移失败态）按 M1 fail-closed 直接拒绝——明文比对路径已停用，
    修复 .env 权限重启即自动补齐哈希。
    主凭据歧义同样 fail-closed：该键在 .env 中无法确认恰好一行（多于一行，或
    统计读取失败）时拒绝认证并告警（生效行由"后写覆盖先写"决定，歧义即注入面），
    修成唯一一行即恢复，无需重启。
    注意：compare_digest 不支持非 ASCII 直接比较，先编码为 UTF-8 字节。

    `.env` 路径与读取器、键行计数、告警出口、时延拉平都由调用方传入（`web.app` 的
    `read_env` / `_count_env_key_lines` / `send_notification` / `_constant_time_dummy`）：
    它们既会被测试打桩也会随 `--env-file` 改写（`ENV_FILE`），本模块另持绑定会让
    打桩与运行参数静默失效。
    """
    env = read_env(env_path)
    admin_user = env.get("YIBAN_ADMIN_USER", "").strip()
    # 2026-08-20 对抗性审查修复（P1）：凭据未配置完整时直接拒绝——
    # 原实现 admin_user/admin_pass 均为空串时 compare_digest(b"", b"") 恒真，
    # "只配了用户名没配密码"（或完全未配置）的部署可用空口令登录管理员。
    # 该状态 check_admin_configured() 明确判定为"未配置"，此处口径对齐。
    if not admin_user or not (
        env.get("YIBAN_ADMIN_PASSWORD_HASH", "").strip()
        or env.get("YIBAN_ADMIN_PASSWORD", "").strip()
    ):
        constant_time_dummy(password)  # 时延拉平：与真实比对等开销
        return False
    # 用户名比较统一小写——登录成功后 session 存小写（历史修复），
    # 而此处大小写敏感比对导致混合大小写 YIBAN_ADMIN_USER 永远无法自助改密，
    # 且会被失败计数锁定（管理员被自己的改密界面锁死）
    if not secrets.compare_digest(
        username.strip().lower().encode("utf-8"), admin_user.strip().lower().encode("utf-8")
    ):
        constant_time_dummy(password)
        return False
    pw_hash = env.get("YIBAN_ADMIN_PASSWORD_HASH", "").strip()
    if pw_hash:
        # 刻意放在"即将拿哈希去比对"的决策点、而不是启动时炸掉：一个坏配置
        # 不该把还在正常提供服务的部署直接 brick；把 .env 修成唯一一行即自动恢复，
        # 无需重启。检测口径与 write_env_batch 的旧行折叠同源（count_env_key_lines）；
        # 统计读取失败计得 0 行，与多行同判 fail-closed（读失败 ≠ 未配置）。
        dup = count_env_key_lines(env_path, "YIBAN_ADMIN_PASSWORD_HASH")
        if dup != 1:
            logger.error(
                "拒绝管理员登录：%s 的 YIBAN_ADMIN_PASSWORD_HASH 无法确认为恰好一行"
                "（统计得 %d 行，0 = 读取失败），解析器按后写覆盖先写取值——可能是"
                "配置错误，也可能是 .env 行分隔符注入提权尝试（2026-09-07）。"
                "请核对文件属主与内容，只保留唯一一行后恢复正常（无需重启）",
                env_path, dup,
            )
            send_notification(
                "主管理员凭据歧义告警",
                mail_layout.Mail(
                    summary=".env 中 YIBAN_ADMIN_PASSWORD_HASH 未确认为恰好一行，"
                            "主管理员登录已被拒绝。",
                    fields=[("实测行数", f"{dup} 行（0 表示读取失败）")],
                    notes=["该状态要么是配置错误，要么是配置注入提权。"],
                    advice=["立即核对 .env 内容与文件属主，只保留唯一一行",
                            "排查近期登录与改密记录"],
                    level="urgent",
                ),
                urgent=True,
                # 独立账本 login_fail：本告警可由未认证的 POST /api/login 触发，
                # 不得挤占共享紧急额度（登录失败账本正是为高频可达的告警设立）
                ledger="login_fail",
            )
            constant_time_dummy(password)
            return False
        return check_password_hash(pw_hash, password)
    admin_pass = env.get("YIBAN_ADMIN_PASSWORD", "").strip()
    if admin_pass:
        # M1 明文回退 fail-closed：走到这里 = 哈希缺失而明文仍在，即启动迁移
        # （migrate_admin_password_to_hash）写 .env 失败的降级态。此前回退明文比对，
        # 意味着迁移失败被静默容忍、明文口令路径长期可用；直接拒绝并指导修复，
        # 修复 .env 权限/可写性后重启即自动补齐哈希（迁移逻辑本身不变）。
        logger.error(
            "管理员口令仍为明文存储（%s 缺 YIBAN_ADMIN_PASSWORD_HASH，启动迁移失败态），"
            "明文比对已停用（fail-closed）。请修复 .env 的属主/权限（属主可写）后重启服务，"
            "迁移会自动将口令转为哈希；或手工设置 YIBAN_ADMIN_PASSWORD_HASH",
            env_path,
        )
        constant_time_dummy(password)
        return False
    # 2026-08-27 审查 P3：此处两种既有分支均已返回——
    # 哈希存在 → 已比对返回；明文存在 → M1 fail-closed 返回 False。
    # 能走到这 = 哈希与明文都为空（配置在两次 read_env 之间被清空的极端竞态），
    # 原 compare_digest 行在该态对空密码恒真，属理论上的失效盲区，显式拒绝。
    return False
