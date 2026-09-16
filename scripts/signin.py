#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# 易班自动签到脚本（AGPL-3.0，见项目根 LICENSE）
# 本项目为以下 AGPL-3.0 项目的衍生实现，保留上游版权与许可条款：
#   - OneFeiFan/FYIBAN（多边形内随机定位点算法：缩放质心 + 射线法验证；nightAttendance 签到流程）
#   - 同作者的 KillYiBan（脱胎于 FYIBAN）：默认登录流程的真实 App 请求特征来源
"""
易班自动签到脚本

功能：
1. 自动登录易班（支持多账号，默认 KillYiBan 同款真实 App 特征登录，与同作者 FYIBAN 同源）
2. 自动获取签到任务范围
3. 在签到范围内生成随机定位点（模拟真实定位）
4. 自动提交签到
5. 支持消息通知（Server 酱、Bark、企业微信等）
6. 重试逻辑：失败账号分散重试——开启签到调度时重新安排到窗口内合适时间，否则放回队尾（风控类最多 2 次，其他最多 3 次，MAX_ATTEMPTS 语义）
7. 账号间隔：相邻请求最小间隔下限（YIBAN_ACCOUNT_GAP_MAX，自动调度与手动队列同一语义）

参考项目：
- KillYiBan（默认登录流程的真实 App 请求特征来源；与同作者 FYIBAN 同源）
- OneFeiFan/FYIBAN 模块（多边形定位算法与 nightAttendance 签到流程）
- Auto-Test 项目（旧登录流程，YIBAN_LEGACY_LOGIN=1 启用）

—— 本文件当前有两种身份（按"执行一轮"的边界切分期间）——

1. **兼容壳**：`import signin` 与 `python3 scripts/signin.py ...` 都照旧可用。壳把
   `yiban/engine/*` 的模块级名字程序化转发到本模块（`_forward_all_names`），并把本
   模块的类换成 `_ForwardingModule`：这样 `signin.<名字> = 替身` /
   `mock.patch.object(signin, ...)` 的写入会落到**真正持有该名字的实现模块**，实现内部
   的调用点也能看到替身。不转发就会出现"打桩静默失效"——打桩只改了壳，实现内部照旧
   调真名，测试表面通过、实则什么都没测到。
   本文件顶部还显式 import 了其中的一些名字：那是本文件**自己的代码**要用到的（静态
   可见性与可读性），与程序化转发指向同一批对象；两种写入路径都会被
   `_ForwardingModule` 同步，故打桩对本文件内部的调用同样生效。
2. **尚未搬走的执行核心**：单账号尝试 / 一轮队列（含领取池与分级重试）/ 多执行体 /
   入口 `main` 仍在本文件里，随后按同一批次的后续步骤迁入 `yiban/engine/`，本文件
   随之收为薄壳。

实现模块：`yiban/engine/{cli_support,accounts,schedule,state_io,alerts,probe,config_check}.py`。
新代码请直接 `from yiban.engine import ...`；本壳只为部署面（run.sh / cron / 容器调度器
直接执行本文件）与既有调用方保留。
"""


import argparse
import json
import os
import random
import secrets
import signal
import socket
import subprocess
import sys
import time
import types
from datetime import datetime, timedelta

# 包导入引导：`yiban/` 在仓库根，而直接运行本脚本时 sys.path[0] 是 scripts/。这是
# **过渡机制**——M3 起转为兼容壳（`python -m yiban.cli`），届时随"清 sys.path 注入"移除。
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# 共享模块：`scripts/db.py` / `scripts/mailer.py` / `scripts/notify.py` 均已只剩兼容壳，
# 此处直连实现（yiban/store/db.py、yiban/mail、yiban/notify）。
from yiban import (  # noqa: E402  # 本项目版本（勿与易班 App 版本 YIBAN_APP_VERSION 混同）
    __version__ as RELEASE_VERSION,
)
from yiban import client as yiban_client  # noqa: E402  # 客户端外观 YibanClient
from yiban import (  # noqa: E402
    clock,
    egress,  # 出口（代理）分配：每个执行体可独立配置
    notify,  # Webhook 推送组件（实现已入包；scripts/notify.py 只剩兼容壳）
    security,  # 白名单 / WAF 判定口径（唯一实现）
    window,
)
from yiban import status as yiban_status  # noqa: E402
from yiban.engine import (  # noqa: E402
    accounts,
    alerts,
    cli_support,
    config_check,
    probe,
    schedule,
    state_io,
)

# 以下静态绑定是本文件自身代码要用到的实现名（其余名字由 _forward_all_names 程序化
# 转发，见模块说明第 1 条）：静态绑定只为可读性与静态检查可见，对象与实现模块同一份。
from yiban.engine.accounts import load_accounts  # noqa: E402
from yiban.engine.alerts import (  # noqa: E402
    _alert_slow_sign,
    _collect_admin_mail,
    _flush_admin_mail_summary,
    _flush_mail_on_sigterm,
    _maybe_alert_zero_success,
    send_notification,
    send_user_fail_mail,
)
from yiban.engine.cli_support import (  # noqa: E402
    _acquire_run_lock,
    _RunLockHeld,
    _setup_cli_logging,
    _state_file_lock,
    logger,
)
from yiban.engine.config_check import (  # noqa: E402
    _apply_only_filter,
    parse_env_int,
    print_config_summary,
)
from yiban.engine.probe import run_probe  # noqa: E402
from yiban.engine.schedule import (  # noqa: E402
    _env_int,
    _schedule_config,
    _window_closed,
    build_schedule,
    capacity_accounts,
)
from yiban.engine.state_io import (  # noqa: E402
    _clear_fallback_alive,
    _daily_statuses,
    _is_second_run,
    _load_cred_state,
    _save_cred_state,
    _second_run_drop_done,
    _write_fallback_alive,
    _write_sched_done,
    _write_sign_state,
    need_second_run,
)
from yiban.fyiban import algo as fyiban_algo  # noqa: E402
from yiban.fyiban import headers as fyiban_headers  # noqa: E402
from yiban.masking import mask_phone as _mask_phone  # noqa: E402
from yiban.masking import sanitize_text as _sanitize_text  # noqa: E402
from yiban.masking import sanitize_url as _sanitize_url  # noqa: E402
from yiban.store import accounts as accounts_store  # noqa: E402  # 账号运行期复核
from yiban.store import db  # noqa: E402  # SQLite 数据访问层（实现已入包，此即唯一出处）

# 密码学安全随机数生成器（用于定位生成等安全敏感场景）
_secure_random = secrets.SystemRandom()


# ---------------------------------------------------------------------------
# 兼容壳机制（两条转发纪律见模块说明第 1 条）
# ---------------------------------------------------------------------------
_IMPLS = (accounts, alerts, cli_support, config_check, probe, schedule, state_io)


def _forward_all_names(impls):
    """把实现模块的模块级名字（含下划线名）逐个绑定到本壳，转发的是同一对象。

    跳过 dunder：`__doc__` / `__name__` / `__file__` 等必须留在壳自己身上，否则
    `import signin` 拿到的模块元信息会指向实现模块，`run_worker_supervisor` 拉起
    子进程时用的 `__file__` 也会变成实现模块的路径（子进程就再也跑不到这个入口了）。
    """
    for impl in impls:
        for name, value in list(vars(impl).items()):
            if name.startswith("__") and name.endswith("__"):
                continue
            globals()[name] = value


_forward_all_names(_IMPLS)


class _ForwardingModule(types.ModuleType):
    """壳模块：读取与写入都落到真正持有该名字的实现模块（见模块说明第 1 条）。

    模块级赋值走 `__dict__` 直写、不触发 `__setattr__`，故本转发只影响外部打桩
    （`setattr`），不影响本文件自身的名字绑定；而 `setattr` 同时写壳自己的字典，
    使本文件内部函数里的裸名调用也能看到替身。
    """

    def __getattribute__(self, name):
        if name.startswith("__") and name.endswith("__"):
            return types.ModuleType.__getattribute__(self, name)
        for impl in types.ModuleType.__getattribute__(self, "_IMPLS"):
            try:
                return getattr(impl, name)
            except AttributeError:
                continue
        return types.ModuleType.__getattribute__(self, name)

    def __setattr__(self, name, value):
        if not name.startswith("__"):
            for impl in types.ModuleType.__getattribute__(self, "_IMPLS"):
                if hasattr(impl, name):
                    setattr(impl, name, value)
        types.ModuleType.__setattr__(self, name, value)


sys.modules[__name__].__class__ = _ForwardingModule


# 易班 App 请求头与版本特征：**衍生自上游 FYIBAN**，已迁到第三方隔离层
# （yiban/fyiban/headers.py，来源与差异见 yiban/fyiban/PROVENANCE.md）。
# 本模块继续以同名引用，调用方无需改动。
YIBAN_APP_VERSION = fyiban_headers.YIBAN_APP_VERSION
HEADERS = fyiban_headers.HEADERS
KILLYIBAN_HEADERS = fyiban_headers.KILLYIBAN_HEADERS


# ---------------------------------------------------------------------------
# 配置常量
# ---------------------------------------------------------------------------
# 队列重试配置：每账号"1 次初始尝试 + 最多 2 次队列重试"（总尝试上限 3 次）
MAX_ATTEMPTS = 3
# 风控类失败（e003/无效应用端等）加重标记风险，最多重试 1 次（总尝试上限 2 次）
RISK_MAX_ATTEMPTS = 2
# 同一账号两次尝试之间的最短间隔（秒），避免紧邻重试被识别为连击
RETRY_MIN_INTERVAL = 60
# 网络/瞬时类失败重试间隔打散上限（秒），作为账号间随机延迟之外的补充
RETRY_GAP_MAX = 30
# 会话陈旧类失败（总尝试上限 2 次：1 次复用缓存 + 1 次强制真重登）
SESSION_STALE_MAX_ATTEMPTS = 2
# 易班侧无签到点位（总尝试上限 1 次）：属数据/任务配置问题，重试拿不到就是拿不到
NO_POSITION_MAX_ATTEMPTS = 1

# 随机延迟默认值在 web/app.py 中维护（signin.py 不直接使用）。

# 签到模式：sequence（列表顺序，默认）/ random（列表随机打散）
# 由网页系统设置页写入 .env（YIBAN_SIGN_MODE），run.sh 加载后经环境变量传入
SIGN_MODE = os.environ.get("YIBAN_SIGN_MODE", "").strip().lower()

# 周日签到开关：部分学校周日也有签到任务（默认关闭，与历史行为一致）
# 由网页系统设置页写入 .env（YIBAN_SUNDAY_SIGN=1），run.sh 加载后经环境变量传入
SUNDAY_SIGN = os.environ.get("YIBAN_SUNDAY_SIGN", "").strip().lower() in ("1", "true", "on", "yes")
# 周六签到开关：2026-09-07（v0.29.0）起默认关闭，与周日同语义——
# 缺省/空/非法一律视为关闭，仅显式 1/true/on/yes 开启（此前缺省=1 的 fail-open
# 解析随默认反转一并废止，_parse_saturday_sign 已删）。需要在周六签到的部署
# 在网页「系统设置 → 周末签到」开启，或 .env 显式写 YIBAN_SATURDAY_SIGN=1。
SATURDAY_SIGN = os.environ.get("YIBAN_SATURDAY_SIGN", "").strip().lower() in ("1", "true", "on", "yes")


# 签到状态码与日志/日历符号：**定义在 yiban.status（唯一事实源）**，此处为别名。
# 历史上 web/app.py 另定义了一份同名常量与映射表，两份会各自漂移（实测 web 侧缺
# no_position/global_paused、signin 侧缺 pending）；收口后状态码只有一处定义。
STATUS_SUCCESS = yiban_status.STATUS_SUCCESS
STATUS_ALREADY = yiban_status.STATUS_ALREADY
STATUS_NO_TASK = yiban_status.STATUS_NO_TASK
STATUS_FAILED = yiban_status.STATUS_FAILED
STATUS_RETRYING = yiban_status.STATUS_RETRYING
STATUS_SKIPPED_WINDOW = yiban_status.STATUS_SKIPPED_WINDOW
STATUS_SKIPPED_NORANGE = yiban_status.STATUS_SKIPPED_NORANGE
STATUS_NO_POSITION = yiban_status.STATUS_NO_POSITION
STATUS_PAUSED = yiban_status.STATUS_PAUSED
STATUS_USER_CANCELLED = yiban_status.STATUS_USER_CANCELLED
STATUS_PENDING = yiban_status.STATUS_PENDING
STATUS_GLOBAL_PAUSED = yiban_status.STATUS_GLOBAL_PAUSED

# 状态码 → 日志/日历符号（同一对象，非副本）
STATUS_SYMBOL = yiban_status.SYMBOL

# 凭据类失败关键词（熔断器计数用）：账号密码问题——连续失败达到阈值后暂停签到。
# 注意：不含 WAF/风控关键词（那是环境问题不是凭据问题，不计入）。
# 2026-08-21 对抗性审查修复：移除 "登录失败"/"登录响应异常"/"OAuth 页解析失败"
# 三个泛化关键词——它们对应的环境类失败（OAuth 页解析不出、响应缺 reUrl 等，
# 代码注释自认"通常是海外 IP 被风控"）此前被误计为凭据失败，连续 3 天即可把
# 账号错误冻结并诱导用户无谓改密。真实口令错误的消息含 "账号或密码错误"
# （signin 登录流程 raise 处），仍被首条关键词覆盖。
CRED_FAIL_KEYWORDS = [
    "账号或密码错误",
    "e003",
    "无效的应用端",
    "e001",
    "origin invalid",
]
# 熔断参数：连续 N 天凭据失败 → 暂停；暂停后每 N 天半开试探 1 次
CRED_FAIL_DAYS = 3          # 连续凭据失败天数阈值
PROBE_INTERVAL_DAYS = 7     # 暂停后半开试探周期（天）

# 签到窗口默认值由下方 _DEFAULT_SIGN_START/_DEFAULT_SIGN_END 维护（与 web/app.py 一致）。


# WAF 判定口径的唯一实现在 `yiban/security.py`（含"只在短响应里检测"的边界理由
# 与 Unicode 转义解码），此处只做同名转发——调用方与既有测试继续用 signin 的名字。
WAF_KEYWORDS = security.WAF_KEYWORDS
is_waf_blocked = security.is_waf_blocked


# ---------------------------------------------------------------------------
# 定位生成：多边形内随机点
# ---------------------------------------------------------------------------
# 算法**衍生自上游 FYIBAN**（缩放质心 + 射线法），实现在第三方隔离层
# `yiban/fyiban/algo.py`；采样分布与兜底策略的本地差异见同目录 PROVENANCE.md。
point_in_polygon = fyiban_algo.point_in_polygon
generate_position_in_polygon = fyiban_algo.generate_position_in_polygon


# ---------------------------------------------------------------------------
# 重试分级：判断一次失败是否值得重试、重试上限
# ---------------------------------------------------------------------------
# 确定性认证失败特征：账号密码本身错误或已被易班侧锁定（msgCN 原文），重试只会
# 把同一次错误登录再提交一遍，还会加速触发易班「错误尝试过多」的账号锁定
# （2026-09-04 生产复盘）——终态不重试。
AUTH_FAIL_KEYWORDS = [
    "账号或密码错误",
    "错误尝试过多",
]
# 确定性认证失败的总尝试上限：仅首试 1 次
AUTH_FAIL_MAX_ATTEMPTS = 1
# 风控/凭据类失败特征：重试不仅无用，还可能加重账号标记
RISK_FAIL_KEYWORDS = [
    "账号或密码错误",
    "e003",
    "无效的应用端",
    "e001",
    "origin invalid",
    "登录失败",
    "登录响应异常",
    "OAuth 页解析失败",
    # WAF 风控拦截：重试只会浪费请求并加重 IP/账号标记（与 WAF_KEYWORDS 对应）
    "风险访问",
    "风控",
    "访问服务禁用",
    "WAF",
    "拦截",
]
# 网络/瞬时类失败特征：值得重试
def classify_failure(message):
    """对失败信息分级，返回最大重试次数。

    - 风控/凭据类：最多重试 1 次（RISK_MAX_ATTEMPTS），避免加重账号标记
    - 其他失败（网络/未知）：最多重试 MAX_ATTEMPTS 次
    （确定性认证失败在 _retry_budget 处更早拦截，不会再走到这里）
    （2026-08-15 审查清理：原返回 (max_attempts, retryable) 的 retryable 恒为 True
    且无调用方使用——死返回值；TRANSIENT_FAIL_KEYWORDS 死常量一并删除）
    """
    for kw in RISK_FAIL_KEYWORDS:
        if kw in message:
            return RISK_MAX_ATTEMPTS
    return MAX_ATTEMPTS


# 会话陈旧类失败特征（2026-08-31 生产复盘新增）：缓存会话已被服务端作废
# （夜间自然过期、或本人用手机端易班登录把 Web 会话挤掉）。后四个是同一语义的
# 措辞变体：服务端文案一改即被判"网络类"，重新空跑满 4 次。
SESSION_STALE_FAIL_KEYWORDS = [
    "未登录",
    "登录已经超时",
    "登录已超时",
    "登录已失效",
    "会话已过期",
    "会话失效",
]

# 易班侧无签到点位特征：登录成功、signPosition 返回 code=0 但 Position 为空
# （任务未配置/当日任务已关闭等），与凭据、会话均无关。
NO_POSITION_FAIL_KEYWORDS = ["未找到签到位置数据"]


def _is_session_stale_failure(message):
    """是否为"缓存会话已被服务端作废"类失败——账密本身没问题，不是凭据类失败。"""
    return any(kw in message for kw in SESSION_STALE_FAIL_KEYWORDS)


def _retry_budget(message):
    """返回 (该失败下的最大尝试次数, 是否应清除该账号会话缓存)。

    确定性认证失败优先于一切：密码错误/账号锁定重试无意义且有害（多一次真实
    登录加速易班侧锁定）。会话陈旧次优先：它既不在风控关键词里（原实现据此给满
    4 次上限），又不是"再试一次就可能好"的瞬时故障——不清缓存重登，重试只是把
    同一份死缓存的失败原样复演。无签到点位同理且更绝对：易班侧没有数据，重试
    无意义。
    """
    if any(kw in message for kw in AUTH_FAIL_KEYWORDS):
        # 清缓存同样成立：登录失败的会话残片没有复用价值
        return AUTH_FAIL_MAX_ATTEMPTS, True
    if any(kw in message for kw in NO_POSITION_FAIL_KEYWORDS):
        return NO_POSITION_MAX_ATTEMPTS, False
    if _is_session_stale_failure(message):
        return SESSION_STALE_MAX_ATTEMPTS, True
    max_attempts = classify_failure(message)
    # 风控类（e003/WAF 等）会话已不可信；"授权设备"非风险关键词（属设备配置问题，
    # 仍可重试），但同样意味着当前会话不可信，一并清除
    return max_attempts, (max_attempts == RISK_MAX_ATTEMPTS or "授权设备" in message)


def clear_session_cache_quiet(phone):
    """清除单账号会话缓存（模块级入口，供重试队列联动）；失败仅留痕不影响签到。

    db 未初始化（环境变量账号模式）时为无操作。
    """
    try:
        if db.is_initialized():
            db.clear_session_cache(phone)
    except Exception as e:
        logger.debug(f"[{phone}] 清除会话缓存失败: {_sanitize_text(e)}")


# ---------------------------------------------------------------------------
# 易班登录
# ---------------------------------------------------------------------------
# 白名单口径的唯一实现在 `yiban/security.py`（宽松 = 登录链路跟随的跳转；
# 严格 = 挑战页吐出的跳转目标）；此处只做同名转发。
_is_yiban_trusted_url = security.is_yiban_trusted_url
_is_fyiban_url = security.is_fyiban_url


# 客户端外观（凭据托管 / 会话缓存 / 代理 / 设备绑定）在 `yiban/client.py`，
# 协议步骤在 `yiban/fyiban/protocol.py`，安全策略在 `yiban/security.py`。
# 此处转发**同一个类对象**（不是第二份实现）：既有调用点与
# `patch.object(signin.YibanClient, ...)` 的测试行为不变。
YibanClient = yiban_client.YibanClient


# ---------------------------------------------------------------------------
# 消息通知
# ---------------------------------------------------------------------------
# URL 描述（只留 scheme://host[:port]）的唯一实现在 `yiban/security.py`；
# 此处只做同名转发，通知侧 yiban/notify/config.py 也转发同一实现。
_notify_url_desc = security.url_desc


# ---------------------------------------------------------------------------
# 主流程（本阶段留在壳里的执行核心：单账号尝试 / 一轮队列 / 多执行体 / 入口）
# ---------------------------------------------------------------------------


# P6 耗时告警阈值（秒）：单次尝试耗时超此值 → warning + 管理员汇总邮件 + 即时通知
_DEFAULT_SLOW_SIGN_SEC = 30


# 运行期账号复核（"启动快照跑完整轮期间账号可能被删/停用"）的实现在
# `yiban/store/accounts.py::account_still_signable`——会话缓存的写入闸门
# （`yiban.client`）与本文件必须用**同一份**判据，各写一份会分叉。
account_still_signable = accounts_store.account_still_signable


def attempt_signin(account):
    """单次签到尝试（登录 + 签到），不重试。

    返回 (success, message, skip, status)：
    - success: 是否成功（含"已签到""非签到日"）
    - message: 结果说明（异常时已脱敏，不返回原始 str(e)）
    - skip: True 表示窗口外等无需重试的情况
    - status: 签到状态码（STATUS_*）

    2026-08-15 审查清理：原 notify_url 参数从未在函数体内使用
    （通知统一由 run_queue_retry 最终放弃时发送），已删除。
    """
    phone = account.phone
    if not account_still_signable(account):
        # 运行期复核：账号在本轮执行期间被删除/停用 → 不发起任何请求。
        # 原实现只在启动时筛一次，被删账号仍会被完整登录并签退，还会把会话缓存
        # 写回一个已不存在的账号（孤儿行，见 db.account_is_signable 的说明）。
        return False, "账号已被删除或停用", True, STATUS_USER_CANCELLED
    try:
        client = YibanClient(account)
        try:
            if client.use_killyiban:
                client.login_killyiban()
            else:
                client.login()
            return client.signin()
        finally:
            # C-SIGN-04：无论成败，尝试结束后立即清零/解除本客户端持有的凭据副本
            # （重试由 run_queue_retry 重新构造客户端，Account 本体不受影响）
            client._wipe_credentials()
    except Exception as e:
        # 2026-08-21 注释修正：代码为 exc_info=False（不落堆栈）——堆栈可能包含
        # 含敏感数据的源码上下文；异常消息经 _sanitize_text 脱敏后已足够定位
        # （原注释与行为矛盾）
        # 脱敏：异常消息可能含敏感数据（密码/令牌），替换后记录
        # requests 异常消息内嵌完整请求 URL（含 CSRF 令牌）与代理
        # userinfo，统一过 _sanitize_url 打码后再落日志
        safe_err = _sanitize_text(_sanitize_url(str(e)))
        logger.error(f"[{phone}] ❌ 尝试失败: {safe_err}", exc_info=False)
        # 逐次失败不通知（避免通知风暴），仅最终放弃时由 run_queue_retry 通知一次
        return False, safe_err, False, STATUS_FAILED


#: 兜底常驻执行体的锁文件名（与定时全量/手动签到并存，互斥交给领取池）
FALLBACK_LOCK_NAME = "signin-run.lock.fallback"


#: 领取池的"当日了结"口径（与 `_second_run_drop_done` 的剔除集合一致）：
#: 这三个状态意味着今天不必再签，其余状态（含窗口外跳过、无点位、失败）都仍开放，
#: 由补签轮或兜底执行体接手。
_CLAIM_DONE_STATUSES = (STATUS_SUCCESS, STATUS_ALREADY, STATUS_NO_TASK)

# `--second-run-check` 的退出码契约（run.sh 据此分支，勿随意改动）
SECOND_RUN_CHECK_NEED = 10   # 需要补跑第二轮
SECOND_RUN_CHECK_SKIP = 0    # 无需补跑


# ---------------------------------------------------------------------------
# 账密熔断器：连续凭据失败 → 暂停签到（零请求），周期性半开试探自动恢复
# ---------------------------------------------------------------------------


def _is_credential_failure(message):
    """凭据类失败判定（不含 WAF/风控——那是环境问题不是凭据问题）。"""
    return any(kw in message for kw in CRED_FAIL_KEYWORDS)


def _update_cred_state(cred_state, phone, success, message, today):
    """执行一次后更新账密熔断状态。

    - 成功：清除该账号记录（恢复 ACTIVE）
    - 凭据类失败：连续失败天数 +1（同一天多次失败只计 1 天）；达到阈值 → 暂停并设试探日
    - 其他失败（网络等）：不计数不动记录
    """
    if success:
        if phone in cred_state:
            del cred_state[phone]
        return
    if not _is_credential_failure(message):
        return
    cred = cred_state.get(phone, {})
    if cred.get("last_fail") == today:
        return  # 今天已计过
    cred["fail_days"] = cred.get("fail_days", 0) + 1
    cred["last_fail"] = today
    if cred["fail_days"] >= CRED_FAIL_DAYS and not cred.get("paused_since"):
        pause_day = (datetime.strptime(today, "%Y-%m-%d") + timedelta(days=PROBE_INTERVAL_DAYS)).strftime("%Y-%m-%d")
        cred["paused_since"] = today
        cred["probe_date"] = pause_day
    cred_state[phone] = cred


def _probe_due(cred, today):
    """半开试探判定：暂停中且今天已到（或超过）试探日。"""
    if not cred.get("paused_since"):
        return False
    return not cred.get("probe_date") or today >= cred["probe_date"]


def _next_retry_at(now_dt, sch_cfg, rng=None):
    """重试落点（调度 v2，2026-08-27）：失败账号重新采样到剩余有效窗口的偏早段。

    - 下界 now + retry_min_interval（防连击，保留原安全语义）
    - 上界 eff_hi = sign_end - edge_back（统一截止口径）
    - 剩余窗口"偏早随机"采样（前 60% 均匀）：不尾端扎堆（P2）、无固定尾序（P7）、
      不再回队尾立即执行（P1）；窗口不足返回 None → 调用方走放弃路径（P5）。
    """
    rng = rng or random.Random()
    base = now_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    end_min = sch_cfg["sign_end"][0] * 60 + sch_cfg["sign_end"][1]
    eff_hi = base + timedelta(minutes=end_min - sch_cfg["edge_back_sec"] / 60.0)
    lo = now_dt + timedelta(seconds=sch_cfg["retry_min_interval"])
    if lo >= eff_hi:
        return None
    window = (eff_hi - lo).total_seconds()
    return lo + timedelta(seconds=rng.uniform(0, window * 0.6))


def run_queue_retry(accounts, notify_url, start_delay_max, gap_max, schedule=None, cred_state=None,
                    event_sink=None, reclaim=False, delegated=None):
    """轮询队列 + 分散重试执行全部账号签到。

    流程（schedule 为空=手动签到）：按签到模式（列表顺序 / 列表随机）
    确定执行顺序逐个尝试；失败的账号不立即重试，放入队尾等待下一轮；
    每账号总尝试次数受 _retry_budget 分级控制（确定性认证失败 1 次不重试、
    风控类最多 2 次，其他最多 3 次，MAX_ATTEMPTS=3 语义）；同一账号两次尝试间隔
    不小于 RETRY_MIN_INTERVAL 秒，避免连击。相邻账号请求间隔对齐到不小于
    gap_max（与自动调度、容量预估同一「最小间隔」语义）。

    schedule 非空（自动错峰模式，调度 v2 时间驱动队列）：按 {phone: datetime} 时间点到点执行
    （已过点立即执行），不再叠加启动/账号间随机延迟；失败的账号经 _next_retry_at 重新采样到
    剩余有效窗口的偏早段后非阻塞重插（不再回队尾 + 阻塞等待），窗口不足时明确放弃；
    相邻请求间隔受 min_exec_gap / exec_gap_min 兜底；截止保护统一按 eff_hi（sign_end - edge_back）。

    cred_state（账密熔断）：暂停中的账号零请求跳过（半开试探日除外）；
    执行后更新凭据失败计数（成功清除、凭据类失败累计、达阈值暂停）。

    event_sink（可选）：签到事件落库回调——每次尝试/状态迁移调用一次，
    传入 dict 行（sign_events 表字段）。None 时不收集（行为与旧版一致）；
    回调异常一律吞掉，事件留痕绝不影响签到主流程。

    reclaim（多执行体）：True 时允许重新领取"当日已了结"的账号——只有**手动指定账号**
    才这么传（用户主动点的签到应当照做）。补签轮与兜底 worker 不传：它们接手的是
    "未了结"账号，已了结的账号再登录一次纯属多余的风控暴露。

    **多执行体分工（动态领取 + 账号级租约，见 yiban/store/claims.py）**：每次尝试前
    领取该账号当日的领取记录，领不到即"别的执行体正在做它"→ 本进程不碰（不写状态、
    不重试、不告警）；本轮结束后统一收尾：已了结（成功/已签到/今日无任务）落 `done`，
    其余落 `failed` 并**放开租约**（补签轮/兜底执行体可立刻接手）。执行体进程崩溃时
    领取记录停在 `claimed`，租约到期后由其他执行体接管——不需要人工介入。

    `delegated`（可选出参）：把"不在本执行体范围内"的账号（领不到的那些）收集到
    这个 set 里。汇总与退出码必须据此把它们从"失败"里摘出去——否则每个执行体都会把
    别人的活报成自己的失败（实测：4 个执行体各带 10 个账号，却各报 28-30 个失败）。

    返回结果字典 {手机号: (success, message, skip, status)}。
    """
    schedule = schedule or {}
    cred_state = cred_state or {}
    # .env 直配超大值不得把队列睡死——与网页设置侧 3600 上限同口径
    # （该保护原本内置于共享随机延迟 helper，间隔改为确定性对齐后收口到入参处）
    gap_max = min(gap_max, 3600)
    # 启动延迟已废弃（v0.29.0），调度 v2 时间点分布 + 掐头去尾取代；
    # start_delay_max 参数仅为兼容旧调用签名保留（值不再使用）
    queue = list(accounts)
    if SIGN_MODE == "random" and not schedule:
        # 列表随机模式：每次运行打乱顺序（打破"固定顺序+固定时刻"的脚本指纹）；
        # 时间点模式下随机性已由 build_schedule 的槽位重排承担，此处不再重复打乱
        random.shuffle(queue)
        logger.debug(f"签到模式: 列表随机（顺序已打散，共 {len(queue)} 个账号）")
    else:
        logger.debug(f"签到模式: 列表顺序（共 {len(queue)} 个账号）")
    attempts = {acc.phone: 0 for acc in accounts}
    results = {}
    first_round = True

    # ---- 领取池（多执行体协调；单执行体形态下永远领得到，行为与旧版一致）----
    executor_id = (os.environ.get("YIBAN_EXECUTOR_ID", "").strip()
                   or db.claim_new_owner("exec-"))
    claimed_day = {}   # 本进程领到的账号 → 业务日（跨午夜时逐账号不同）

    def _claim(phone, day):
        """领取该账号当日的工作权；领不到返回 False（别人在做）。

        **库未初始化时直接放行且不碰库**：领取池只是"多执行体协调"的手段，
        而"不碰库"是有意的——否则纯状态文件部署（无 DB）会被这次调用顺手创建
        一个默认库，纯属副作用。
        """
        if not db.is_initialized():
            return True
        try:
            got = db.claim_sign_account(phone, day, executor_id, allow_settled=reclaim)
        except Exception as e:
            # 协调层故障不得让签到停摆（单执行体形态这个池可有可无）
            logger.debug(f"[{phone}] 领取失败（按可执行处理）: {e}")
            got = True
        if got:
            claimed_day[phone] = day
        return got

    def _settle_claims(res):
        """本轮结束后统一收尾本轮领到的账号（只认本轮领过的，避免误写他人在飞的记录）。

        了结口径与展示口径刻意一致：`success/already/no_task` 记为 `done`（当日无需再签），
        其余记为 `failed` 但**未了结**——补签轮与兜底执行体正是为接手它们而存在。
        """
        if not claimed_day:
            return   # 本轮没领过任何账号（库未初始化 / 全被他人领取）
        for ph, (_ok, _msg, _skip, st) in res.items():
            day = claimed_day.get(ph)
            if not day:
                continue
            try:
                if st in _CLAIM_DONE_STATUSES:
                    db.claim_settle(ph, day, executor_id, db.CLAIM_STATE_DONE, str(st))
                else:
                    db.claim_give_up(ph, day, executor_id, str(st))
            except Exception as e:
                logger.debug(f"[{ph}] 收尾领取记录失败（不影响签到结果）: {e}")

    def _emit_event(phone, status, message, dur=None, attempt_no=None):
        """签到事件留痕（v6 的 sign_events 表此前主流程零写入）。

        每次尝试与状态迁移（含重试/跳过）落一行，stage="sign"；探针沿用既有
        stage="probe" 写入口径。异常吞掉——留痕失败不得影响签到主流程。
        """
        if event_sink is None:
            return
        try:
            ts = clock.now().strftime("%Y-%m-%d %H:%M:%S")
            event_sink({
                "ts": ts,
                "phone": phone,
                "status": status,
                "message": _sanitize_text(str(message or ""))[:200],
                "stage": "sign",
                "attempt": attempts.get(phone, 0) if attempt_no is None else attempt_no,
                "dur_sec": dur,
                "finished_at": ts,
            })
        except Exception:
            pass

    def _mark_window_skip(rest_accs):
        """窗口关闭收尾：只把**当日尚无记录**的账号标记为窗口外跳过。

        不覆盖已有记录：本轮（或上一轮补签）已经得出的 failed / no_position 等真实
        原因必须保留——原实现无条件改写，会把"重试没赶上窗口"记成"窗口外"，
        日历上丢掉失败原因，`has_real_failure` 也一起变 False（失败告警被吞掉）。
        补签轮起跑时窗口已关闭同理：整轮零请求却不该改写首轮结论。
        """
        recorded = _daily_statuses()
        for _ra in rest_accs:
            _p = _ra.phone
            if _p in results or _p in recorded:
                continue
            results[_p] = (False, "签到时段已结束", True, STATUS_SKIPPED_WINDOW)
            _write_sign_state(_p, STATUS_SKIPPED_WINDOW, "签到时段已结束")
            _emit_event(_p, STATUS_SKIPPED_WINDOW, "签到时段已结束")

    # 调度 v2 安全底座参数（schedule 模式）：本地截止保护 + 启动对齐
    sch_cfg = _schedule_config() if schedule else None
    last_done = None  # 上次尝试结束时刻（monotonic），启动对齐用
    # P6 耗时告警：阈值可配（YIBAN_SLOW_SIGN_SEC），每账号每轮最多告警 1 次
    slow_sec = _env_int("YIBAN_SLOW_SIGN_SEC", _DEFAULT_SLOW_SIGN_SEC, 1, 600)
    slow_notified = set()

    # ---- 调度 v2 时间驱动队列（2026-08-27 阶段 2：重试重新尊重计划，P1-P5/P7）----
    # pending: (next_at, seq, acc) 按下次尝试时刻排序；首 attempt 落点=计划时刻（已过点立即）；
    # 重试经 _next_retry_at 重新采样落点后非阻塞重插，不再"回队尾 + 阻塞 sleep"（P4 消除）。
    if schedule:
        import heapq
        pending = []
        _seq = 0

        def _push(_acc, _at):
            nonlocal _seq
            heapq.heappush(pending, (_at, _seq, _acc))
            _seq += 1

        _now0 = clock.now()
        for _acc in accounts:
            _t = schedule.get(_acc.phone)
            _push(_acc, _t if _t and _t > _now0 else _now0)
        while pending:
            _at_dt, _seq_no, acc = heapq.heappop(pending)
            phone = acc.phone
            # M14：每次尝试（含重试）重算 today，跨午夜执行不沿用启动日
            today = clock.now().strftime("%Y-%m-%d")
            now_dt = clock.now()
            # 截止保护（P5，统一 eff_hi 口径）：窗口关闭 → 剩余账号全部跳过
            if _window_closed(sch_cfg, now_dt):
                logger.info(f"[{phone}] ⛔ 签到时段已结束，跳过执行")
                _mark_window_skip([acc] + [r[2] for r in pending])
                break
            # 先判后睡：将跳过的账号（用户自取消/熔断暂停）不睡到时段槽位——
            # 死号排在后段时，此前会先睡满槽位间隔才发现可跳过，把活号挤出窗口
            cred = cred_state.get(phone, {})
            if getattr(acc, "user_paused", False):
                results[phone] = (False, "用户已取消签到", True, STATUS_USER_CANCELLED)
                _write_sign_state(phone, STATUS_USER_CANCELLED, "用户已取消签到")
                _emit_event(phone, STATUS_USER_CANCELLED, "用户已取消签到")
                logger.info(f"[{phone}] ⏹️ 用户已取消签到，跳过执行")
                continue
            # 账密熔断：暂停中的账号零请求直接跳过（半开试探日除外——试探 1 次以验证恢复）
            if cred.get("paused_since") and not _probe_due(cred, today):
                results[phone] = (False, "账密异常已暂停，请修改密码", True, STATUS_PAUSED)
                _write_sign_state(phone, STATUS_PAUSED, "账密异常已暂停（连续失败），请修改密码")
                _emit_event(phone, STATUS_PAUSED, "账密异常已暂停（连续失败），请修改密码")
                logger.info(f"[{phone}] ⏸️ 账密异常已暂停，跳过执行")
                continue
            # 到点执行（已过点立即）；重试落点已由 _next_retry_at 采样
            wait = (_at_dt - now_dt).total_seconds()
            if wait > 0:
                time.sleep(wait)
            # 请求最小间隔兜底（F1）：min_exec_gap 与 exec_gap_min（过点账号）取较大值；
            # v0.29.0：账号间隔设置（gap_max）对自动调度同样生效，作为相邻请求间隔下限
            if last_done is not None:
                min_gap = max(
                    sch_cfg["min_exec_gap"],
                    sch_cfg["exec_gap_min"] if wait <= 0 else 0,
                    gap_max,
                )
                gap = min_gap - (time.monotonic() - last_done)
                if gap > 0:
                    logger.debug(f"[{phone}] 间隔对齐: 补 {int(gap)}s（最小 {min_gap}s）")
                    time.sleep(gap)
            # 睡眠/间隔对齐之后**再判一次**窗口：等待期间可能已越过 eff_hi，此时
            # 仍发起请求就落到窗口外（学校侧会拒），且会挤占后面的账号
            if wait > 0 and _window_closed(sch_cfg, clock.now()):
                logger.info(f"[{phone}] ⛔ 等待期间已越过签到时段，跳过执行")
                _mark_window_skip([acc] + [r[2] for r in pending])
                break
            # 领取（放在"要发请求"的最后一步之前：睡到计划时刻的过程中不占租约）
            if not _claim(phone, today):
                logger.debug(f"[{phone}] 已被其他执行体领取，本进程跳过")
                if delegated is not None:
                    delegated.add(phone)
                continue
            attempts[phone] += 1
            logger.debug(f"[{phone}] 🔄 第 {attempts[phone]} 次尝试")
            t0 = time.monotonic()  # 单次尝试耗时起点（P6：慢响应可判）
            success, message, skip, status = attempt_signin(acc)
            last_done = time.monotonic()  # 启动对齐：记录本次尝试结束时刻
            dur = last_done - t0
            _write_sign_state(phone, status, message, dur=dur)
            _emit_event(phone, status, message, dur=dur)
            # P6 耗时告警（2026-08-16）：单次尝试超阈值 → warning + 通知（原样）
            if dur > slow_sec and phone not in slow_notified:
                slow_notified.add(phone)
                _alert_slow_sign(phone, dur, slow_sec, status, message, notify_url)
            # 熔断计数：成功清除；凭据类失败累计（含半开试探结果——成功即恢复）
            _update_cred_state(cred_state, phone, success, message, today)
            # 半开试探"凭据健康"判定：签到成功，或已成功登录但被签到时段规则跳过
            # （SKIPPED_WINDOW/NORANGE 发生在登录并拉取任务之后，凭据已被证实可用）。
            # 2026-08-27 修复：原实现仅 success 时解冻，窗口跳过被误判为试探失败再冻 7 天。
            probe_healthy = success or (
                skip and status in (STATUS_SKIPPED_WINDOW, STATUS_SKIPPED_NORANGE)
            )
            if cred.get("paused_since") and probe_healthy:
                if not success:  # success 时 _update_cred_state 已清除；窗口跳过需显式清除
                    cred_state.pop(phone, None)
                logger.info(f"[{phone}] ✅ 半开试探确认账密可用，解除暂停")
            elif cred.get("paused_since") and _probe_due(cred, today):
                # 试探失败：仅凭据类失败才顺延试探日（网络类瞬时失败
                # 原来也顺延 7 天，把可自愈状态放大成周级停签）；网络类失败保持
                # probe_date 不变，次日即再试探
                if _is_credential_failure(message):
                    next_probe = (datetime.strptime(today, "%Y-%m-%d") + timedelta(days=PROBE_INTERVAL_DAYS)).strftime("%Y-%m-%d")
                    cred_state[phone]["probe_date"] = next_probe
                    logger.warning(f"[{phone}] ⏸️ 半开试探失败，保持暂停（下次 {next_probe} 试探）")
                else:
                    logger.warning(f"[{phone}] ⏸️ 半开试探遇网络类失败，保持暂停（试探日不变，次日再试）")
            if success:
                results[phone] = (True, message, skip, status)
                logger.info(f"[{phone}] {STATUS_SYMBOL[status]} {message}")
                continue
            # 失败：跳过类不重试；其余按分级重试
            if skip:
                results[phone] = (False, message, True, status)
                logger.info(f"[{phone}] ⛔ {message}（不重试）")
                continue
            max_attempts, clear_cache = _retry_budget(message)
            # 会话缓存联动：风控类（e003/WAF）、"授权设备"、会话陈旧三类当前会话都不可信，
            # 清掉缓存后下一次尝试会走真实登录（attempt_signin 每次新建 client）
            if clear_cache:
                clear_session_cache_quiet(phone)
            if attempts[phone] >= max_attempts:
                results[phone] = (False, message, False, status)
                if status == STATUS_NO_POSITION:
                    # 无点位=易班侧无数据（任务未配置/当日任务已关闭），非账号/凭据
                    # 问题：管理员无从修复，不按"签到失败"告警轰炸；仅留日志与状态
                    # （独立状态码供展示/统计），补签重试同样无意义。
                    logger.warning(
                        f"[{phone}] 🚫 易班未返回签到点位，当日不签到（重试无意义）: {message}"
                    )
                    continue
                logger.error(f"[{phone}] ❌ 已尝试 {attempts[phone]} 次，放弃: {message}")
                _collect_admin_mail("易班签到失败", f"账号: {_mask_phone(phone)}\n原因: {_sanitize_text(message)}")
                if notify.is_configured():
                    send_notification("易班签到失败", f"账号: {_mask_phone(phone)}\n原因: {_sanitize_text(message)}", notify_url)
                send_user_fail_mail(acc.owner, phone, message)
                continue
            # 重试落点（P1/P2/P3/P7）：窗口内重新采样，非阻塞重插；窗口不足 → 放弃（P5）
            nxt = _next_retry_at(clock.now(), sch_cfg)
            if nxt is None:
                results[phone] = (False, message, False, status)
                logger.error(f"[{phone}] ❌ 窗口剩余不足，不再重试: {message}")
                _collect_admin_mail("易班签到失败", f"账号: {_mask_phone(phone)}\n原因: {_sanitize_text(message)}")
                if notify.is_configured():
                    send_notification("易班签到失败", f"账号: {_mask_phone(phone)}\n原因: {_sanitize_text(message)}", notify_url)
                send_user_fail_mail(acc.owner, phone, message)
                continue
            # 重试入队统一兜底失败原因（用户需求）：把本次失败 message 原样补进
            # 状态/事件/日志三处出口，原因经 _sanitize_text 防换行/回车注入（与 web 展示一致）。
            _write_sign_state(phone, STATUS_RETRYING, f"待重试（已 {attempts[phone]} 次）: {_sanitize_text(message)}")
            _emit_event(phone, STATUS_RETRYING, f"待重试（已 {attempts[phone]} 次）: {_sanitize_text(message)}")
            _push(acc, nxt)
            logger.warning(f"[{phone}] ⏳ 待重试（已 {attempts[phone]} 次，上限 {max_attempts} 次，{nxt.strftime('%H:%M:%S')} 再试）: {_sanitize_text(message)}")
        _settle_claims(results)
        return results

    while queue:
        acc = queue.pop(0)
        phone = acc.phone
        # M14：每次尝试（含重试）重算 today，跨午夜执行不沿用启动日
        today = clock.now().strftime("%Y-%m-%d")
        is_first = first_round
        first_round = False
        # 先判后睡：将跳过的账号（用户自取消/熔断暂停）不占账号间隔——
        # 此前先睡满间隔再判跳过，死号排在前段时会白烧窗口
        if getattr(acc, "user_paused", False):
            results[phone] = (False, "用户已取消签到", True, STATUS_USER_CANCELLED)
            _write_sign_state(phone, STATUS_USER_CANCELLED, "用户已取消签到")
            _emit_event(phone, STATUS_USER_CANCELLED, "用户已取消签到")
            logger.info(f"[{phone}] ⏹️ 用户已取消签到，跳过执行")
            continue
        # 账密熔断：暂停中的账号零请求直接跳过（半开试探日除外——试探 1 次以验证恢复）
        cred = cred_state.get(phone, {})
        if cred.get("paused_since") and not _probe_due(cred, today):
            results[phone] = (False, "账密异常已暂停，请修改密码", True, STATUS_PAUSED)
            _write_sign_state(phone, STATUS_PAUSED, "账密异常已暂停（连续失败），请修改密码")
            _emit_event(phone, STATUS_PAUSED, "账密异常已暂停（连续失败），请修改密码")
            logger.info(f"[{phone}] ⏸️ 账密异常已暂停，跳过执行")
            continue
        # 首轮（第一个账号）不等待，后续每个账号（含重试回队）对齐相邻请求的
        # 最小间隔——与铺点路径、容量预估 (avg+gap) 同一「下限」语义。
        if not is_first and last_done is not None:
            gap = gap_max - (time.monotonic() - last_done)
            if gap > 0:
                logger.debug(f"[{phone}] 间隔对齐: 补 {int(gap)}s（最小 {gap_max}s）")
                time.sleep(gap)

        # 领取（与铺点路径同口径；手动指定账号时 reclaim=True，可重签当日已了结的账号）
        if not _claim(phone, today):
            logger.debug(f"[{phone}] 已被其他执行体领取，本进程跳过")
            if delegated is not None:
                delegated.add(phone)
            continue
        attempts[phone] += 1
        logger.debug(f"[{phone}] 🔄 第 {attempts[phone]} 次尝试")

        t0 = time.monotonic()  # 单次尝试耗时起点（P6：慢响应可判）
        success, message, skip, status = attempt_signin(acc)
        last_done = time.monotonic()  # 启动对齐：记录本次尝试结束时刻
        # 每次尝试结束即更新结构化状态文件（失败回队时显示 🔄 重试中；附耗时 dur）
        dur = last_done - t0
        _write_sign_state(phone, status, message, dur=dur)
        _emit_event(phone, status, message, dur=dur)
        # P6 耗时告警（2026-08-16）：单次尝试超阈值 → warning + 通知。
        # 节流：每账号每轮最多 1 次（重试连击不刷屏；最终失败另有失败通知，
        # 此处主要覆盖"慢但成功"的接口劣化预警）。通知失败不影响签到（内部已捕获）。
        if dur > slow_sec and phone not in slow_notified:
            slow_notified.add(phone)
            _alert_slow_sign(phone, dur, slow_sec, status, message, notify_url)
        # 熔断计数：成功清除；凭据类失败累计（含半开试探结果——成功即恢复）
        _update_cred_state(cred_state, phone, success, message, today)
        # 半开试探"凭据健康"判定：签到成功，或已成功登录但被签到时段规则跳过
        # （SKIPPED_WINDOW/NORANGE 发生在登录并拉取任务之后，凭据已被证实可用）。
        # 2026-08-27 修复：原实现仅 success 时解冻，窗口跳过被误判为试探失败再冻 7 天。
        probe_healthy = success or (
            skip and status in (STATUS_SKIPPED_WINDOW, STATUS_SKIPPED_NORANGE)
        )
        if cred.get("paused_since") and probe_healthy:
            if not success:  # success 时 _update_cred_state 已清除；窗口跳过需显式清除
                cred_state.pop(phone, None)
            logger.info(f"[{phone}] ✅ 半开试探确认账密可用，解除暂停")
        elif cred.get("paused_since") and _probe_due(cred, today):
            # 试探失败：仅凭据类失败才顺延试探日（理由同上）
            if _is_credential_failure(message):
                next_probe = (datetime.strptime(today, "%Y-%m-%d") + timedelta(days=PROBE_INTERVAL_DAYS)).strftime("%Y-%m-%d")
                cred_state[phone]["probe_date"] = next_probe
                logger.warning(f"[{phone}] ⏸️ 半开试探失败，保持暂停（下次 {next_probe} 试探）")
            else:
                logger.warning(f"[{phone}] ⏸️ 半开试探遇网络类失败，保持暂停（试探日不变，次日再试）")

        if success:
            results[phone] = (True, message, skip, status)
            # 符号按状态码输出：success/already→✅、no_task→➖（与界面显示一致）
            logger.info(f"[{phone}] {STATUS_SYMBOL[status]} {message}")
            continue

        # 失败：跳过类不重试；其余按分级放回队尾
        if skip:
            results[phone] = (False, message, True, status)
            logger.info(f"[{phone}] ⛔ {message}（不重试）")
            continue

        max_attempts, clear_cache = _retry_budget(message)
        # 会话缓存联动：与队列路径同口径——风控类/"授权设备"/会话陈旧都清缓存，
        # 避免下次尝试复用已被服务端作废的会话
        if clear_cache:
            clear_session_cache_quiet(phone)
        if attempts[phone] >= max_attempts:
            results[phone] = (False, message, False, status)
            if status == STATUS_NO_POSITION:
                # 同 schedule 分支：无点位非账号/凭据问题，不按"签到失败"告警轰炸。
                logger.warning(
                    f"[{phone}] 🚫 易班未返回签到点位，当日不签到（重试无意义）: {message}"
                )
                continue
            logger.error(f"[{phone}] ❌ 已尝试 {attempts[phone]} 次，放弃: {message}")
            # A 线合并：失败并入任务结束汇总邮件（webhook 仍即时推送）
            _collect_admin_mail(
                "易班签到失败",
                f"账号: {_mask_phone(phone)}\n原因: {_sanitize_text(message)}",
            )
            if notify.is_configured():
                send_notification(
                    "易班签到失败", f"账号: {_mask_phone(phone)}\n原因: {_sanitize_text(message)}", notify_url
                )
            # B 线：向账号归属用户发失败提醒（未开启/未绑定用户则静默跳过）
            send_user_fail_mail(acc.owner, phone, message)
            continue

        # 放回队尾：单次 sleep 保证总间隔 ≥ retry_min_interval，
        # 随机部分只用于打散，不允许把最小间隔缩水
        # 重试入队统一兜底失败原因（用户需求）：把本次失败 message 原样补进
        # 状态/事件/日志三处出口，原因经 _sanitize_text 防换行/回车注入（与 web 展示一致）。
        _write_sign_state(phone, STATUS_RETRYING, f"待重试（已 {attempts[phone]} 次）: {_sanitize_text(message)}")
        _emit_event(phone, STATUS_RETRYING, f"待重试（已 {attempts[phone]} 次）: {_sanitize_text(message)}")
        retry_min_interval = RETRY_MIN_INTERVAL
        wait = max(retry_min_interval, retry_min_interval - gap_max + random.uniform(0, RETRY_GAP_MAX))
        logger.debug(f"[{phone}] 重试前等待 {wait:.1f}s（最小 {retry_min_interval}s）")
        time.sleep(wait)
        queue.append(acc)
        logger.warning(f"[{phone}] ⏳ 待重试（已 {attempts[phone]} 次，上限 {max_attempts} 次）: {_sanitize_text(message)}")

    _settle_claims(results)
    return results


def run_worker_supervisor(n, argv):
    """拉起 n 个执行体子进程并汇总退出码（`--workers N`）。

    - **全局锁由本进程持有**：散落的另一轮全量（cron 与手动）仍会被挡住；
    - 子进程各持自己的锁文件 + 各自的执行体身份（领取池据此分工）；
    - 每个子进程可配一个独立出口代理（`YIBAN_PROXY_LIST`，见 `_worker_proxy`）；
    - 退出码汇总取"最严重"的一个：真失败(1) > 锁忙(3) > 跳过/窗口外(2) > 全成功(0)。
      调用方（run.sh）据此判断本轮是否需要补签，语义与单执行体一致。
    """
    # 全局锁：本进程持有直到子进程全部结束（句柄必须保活，不能只用一次就丢）
    _global_lock = _acquire_run_lock(False)
    # 先在本进程把库初始化/迁移做完并校验账号配置：否则 N 个子进程会在同一秒
    # 抢着 init_db（实测 `PRAGMA journal_mode=WAL` 会报 "database is locked"），
    # 而且配置错误的报错会变成 N 份、互相淹没。
    try:
        accounts = load_accounts()
    except RuntimeError as e:
        logger.error(f"配置加载失败: {e}")
        return 1
    if not accounts:
        logger.error("未配置任何账号，不拉起执行体")
        return 1
    logger.info("多执行体：共 %d 个账号待签，拉起 %d 个执行体", len(accounts), n)
    children = []
    for i in range(n):
        env = os.environ.copy()
        env["YIBAN_EXECUTOR_ID"] = f"{socket.gethostname()}:workers:{os.getpid()}:w{i}"
        env["YIBAN_RUN_LOCK_NAME"] = f"signin-run.lock.w{i}"
        proxy = egress.resolve(egress.ROLE_WORKER, i)
        if proxy:
            env["YIBAN_PROXY"] = proxy
        # 复刻本轮其余参数（去掉 --workers，避免递归拉起）
        child_argv = [a for a in argv if a != "--workers" and a != str(n)]
        cmd = [sys.executable, os.path.abspath(__file__), *child_argv]
        logger.info("执行体 %d/%d 启动（出口: %s）", i + 1, n, egress.describe(proxy))
        children.append(subprocess.Popen(cmd, env=env))
        # 错开启动：既避开"同一秒争库"，也让首轮请求不要在同一瞬间齐发（风控面）
        if i + 1 < n:
            time.sleep(0.5)

    codes = []
    for i, child in enumerate(children):
        rc = child.wait()
        codes.append(rc)
        logger.info("执行体 %d/%d 结束，退出码 %s", i + 1, n, rc)

    if any(c == 1 for c in codes):
        return 1
    if any(c == 3 for c in codes):
        return 3
    if any(c == 2 for c in codes):
        return 2
    return 0


def run_fallback_worker(argv_rest, interval=None, deadline=None):
    """兜底常驻执行体：窗口内反复扫"还没了结"的账号并接手，窗口关闭即退出。

    为什么需要它（`63` §2 用户反问"等某个 worker 做完才能开始？"→ 不要等）：
    学校晚放号、窗口内新审核通过的账号、被慢账号拖住的、失败待重试的——都能被
    **随手接手**，而不是等下一轮定时任务。

    做法刻意简单可靠：每一轮重新加载账号并调用同一条执行路径（`run_queue_retry`，
    schedule 为空=立即执行）。**分工由领取池承担**：已了结的账号领不到、别的执行体
    正在做的领不到，所以"全量账号列表"作为输入也不会重复签——不需要在这里再写一套筛选。

    自己持一个独立锁文件（`YIBAN_RUN_LOCK_NAME`），因此与定时全量、手动签到、
    其他执行体都能并存（互斥交给领取池）。

    退出：窗口关闭 / 到达 `deadline` / 账号列表为空且已过窗口。返回退出码语义与
    单执行体一致（0 全成功、1 有真失败、2 存在窗口外未了结）。
    """
    interval = interval or _env_int("YIBAN_FALLBACK_INTERVAL", 60, 5, 3600)
    os.environ.setdefault("YIBAN_RUN_LOCK_NAME", FALLBACK_LOCK_NAME)
    proxy = egress.resolve(egress.ROLE_FALLBACK)
    logger.info("兜底执行体启动（出口: %s，扫描间隔 %ss）", egress.describe(proxy), interval)
    if proxy:
        os.environ["YIBAN_PROXY"] = proxy

    sch_cfg = _schedule_config()
    last_code = 0
    while True:
        now = clock.now()
        if deadline is not None and now >= deadline:
            logger.info("兜底执行体：到达截止时刻，退出")
            break
        if _window_closed(sch_cfg, now):
            logger.info("兜底执行体：签到时段已结束，退出")
            break

        _write_fallback_alive(now)
        try:
            accounts = load_accounts()
        except RuntimeError as e:
            logger.error("兜底执行体：配置加载失败: %s", e)
            _clear_fallback_alive()
            return 1
        if not accounts:
            logger.info("兜底执行体：当前没有账号，%ss 后再看", interval)
            time.sleep(interval)
            continue

        delegated = set()
        results = run_queue_retry(accounts, os.environ.get("YIBAN_NOTIFY_URL", ""), 0,
                                  _env_int("YIBAN_ACCOUNT_GAP_MAX", 10, 0, 3600),
                                  schedule=None, cred_state=_load_cred_state(),
                                  delegated=delegated)
        settled = 0
        for acc in accounts:
            if acc.phone in delegated:
                settled += 1
        own = len(results)
        # 本轮自己没活干（全部已被别人接手/已了结）→ 睡一会儿再看
        logger.info("兜底执行体：本轮处理 %d 个账号（%d 个已由他人负责），%ss 后再扫",
                    own, settled, interval)
        for _ok, _msg, skip, status in results.values():
            if status in (STATUS_FAILED,) and not skip:
                last_code = 1
        if own == 0:
            time.sleep(interval)
        else:
            # 有活干就连续扫（不睡满间隔），直到没活为止——窗口是有限的
            time.sleep(min(interval, 5))
    _clear_fallback_alive()
    logger.info("兜底执行体已退出（心跳已清除）")
    return last_code


def main():
    """主函数：加载账号配置并执行签到。

    支持：
    - 数据库 yiban.db（SQLite，web 后台写入）与 YIBAN_ACCOUNTS_JSON
    - 旧格式 YIBAN_ACCOUNTS 或 YIBAN_PHONE/YIBAN_PASSWORD（向后兼容）
    - 队列重试：失败账号分散重试——开启签到调度时重新安排到窗口内合适时间，否则放回队尾（分级上限）
    - 随机延迟：YIBAN_START_DELAY_MAX（启动）/ YIBAN_ACCOUNT_GAP_MAX（账号间隔）
    - --only 指定手机号（逗号分隔），仅供手动签到单个账号
    - --check-config 仅检查配置，不发任何网络请求
    """
    # 进程 umask 077——状态/凭据/邮件配额文件（含完整手机号键）
    # 创建即 0600。宿主 run.sh 已有 umask 077；本处覆盖 web 子进程、容器
    # scheduler 与无宿主脚本的裸调路径（Windows 无实际效果，忽略）。
    os.umask(0o077)
    # 日志装配从模块导入期延迟到 CLI 入口（幂等；覆盖
    # --check-config / --probe / --only 全部路径），模块导入零副作用。
    _setup_cli_logging()
    parser = argparse.ArgumentParser(description="易班自动签到")
    parser.add_argument(
        "--check-config", action="store_true", help="仅检查账号配置（脱敏打印），不发起任何网络请求"
    )
    parser.add_argument(
        "--only", default="", help="仅签到指定手机号（逗号分隔，用于手动签到）"
    )
    parser.add_argument(
        "--probe", action="store_true",
        help="探针模式：非签到时段对全部账号做只读健康检查（需 .env 开启且到触发时间/频率）",
    )
    parser.add_argument(
        "--second-run-check", action="store_true",
        help=(
            "补签轮判定（供宿主 run.sh 调用）：当日全量未收尾或存在未了结账号时"
            f"退出码 {SECOND_RUN_CHECK_NEED}（需要补跑），否则 0。"
            "只读本地状态（领取池/状态文件），不加载账号、不发起任何网络请求。"
        ),
    )
    parser.add_argument(
        "--workers", type=int, default=1, metavar="N",
        help=(
            "多执行体：拉起 N 个并行执行体共同完成本轮（默认 1 = 单执行体，行为不变）。"
            "分工靠数据库里的领取池（动态领取 + 账号级租约），账号不会被两个执行体同时签；"
            "每个执行体可用 YIBAN_PROXY_LIST 配一个独立出口代理"
        ),
    )
    parser.add_argument(
        "--fallback", action="store_true",
        help=(
            "兜底常驻模式：在签到时段内反复扫描'尚未了结'的账号并随手接手（晚放号、"
            "窗口内新审核通过的账号、慢账号、待重试账号），时段结束自动退出。"
            "自己持独立锁、可用 YIBAN_PROXY_FALLBACK 配独立出口，与定时全量并存"
        ),
    )
    args = parser.parse_args()

    # 兜底常驻执行体：先于其他分支（它自带循环与退出条件）
    if args.fallback:
        sys.exit(run_fallback_worker(sys.argv[1:]))

    # 多执行体：本进程只做监督（持全局锁 + 汇总退出码），活儿由子进程干。
    # 放在补签轮判定之前不必要——补签轮判定只读文件，先走它更快。
    if args.workers and args.workers > 1:
        sys.exit(run_worker_supervisor(args.workers, sys.argv[1:]))

    # 补签轮判定必须最先处理：只读状态文件，不加载账号、不建连接、不发请求。
    # 宿主 run.sh 在首轮结束仍持锁时调用本开关，据退出码决定是否补跑第二轮
    # （与容器 docker/scheduler.py 的 SECOND 闸门同语义，判定实现在 need_second_run）。
    if args.second_run_check:
        if need_second_run():
            logger.info("补签轮判定：需要补跑（当日全量未收尾或存在未了结账号）")
            sys.exit(SECOND_RUN_CHECK_NEED)
        logger.info("补签轮判定：无需补跑（当日已收尾且无未了结账号）")
        sys.exit(SECOND_RUN_CHECK_SKIP)

    # 超时击杀前的告警兜底：宿主 run.sh timeout / 容器 / 手动 terminate 均以
    # SIGTERM 结束子进程；注册在探针分支之前，签到与探针子进程同享。
    signal.signal(signal.SIGTERM, _flush_mail_on_sigterm)

    notify_url = os.environ.get("YIBAN_NOTIFY_URL", "")

    # 加载账号配置（文件 > JSON 环境变量 > 旧格式，详见 load_accounts）
    try:
        accounts = load_accounts()
    except RuntimeError as e:
        logger.error(f"配置加载失败: {e}")
        sys.exit(1)

    # 探针模式必须先于「零账号守卫」处理（2026-08-27 审查修复）：空账号部署
    # 误开探针时此前会夜夜走「未配置任何账号」ERROR 分支且 once 永不关闭；
    # 探针语义下零账号=无事可做，静默成功退出。
    if args.probe:
        # 探针对全部账号做完整登录（等同一次真实签到，风控敏感）：一键暂停 /
        # 周末签到关闭期间照跑会把暂停语义打穿。门在探针分支内部判定——
        # 不上移全局门，保住「探针先于零账号守卫」的既有语义与 --check-config 路径。
        _paused = str(os.environ.get("YIBAN_GLOBAL_PAUSE", "")).strip().lower() in ("1", "true", "on", "yes")
        _weekday = clock.now().weekday()
        if _paused or (_weekday == 6 and not SUNDAY_SIGN) or (_weekday == 5 and not SATURDAY_SIGN):
            logger.info("==== 签到已暂停/周末签到关闭，本轮探针跳过（避免暂停期完整登录） ====")
            sys.exit(0)
        # 探针会对全部账号做完整登录，必须与真实签到互斥——
        # 原实现绕过运行锁，23:55 探针与手动签到并发时同一账号被两进程并发登录。
        try:
            _probe_lock_fh = _acquire_run_lock(only_mode=True)
        except _RunLockHeld:
            logger.warning("已有签到进程在运行，本轮探针跳过（防同账号并发）")
            sys.exit(0)
        if accounts:
            run_probe(accounts)
        sys.exit(0)

    # 超期软删账号物理清理（2026-08-20 随读路径清理外移而显式化）：cron/Actions
    # 部署可能没有常驻 web 进程，每日签到进程是清理的唯一时机，失败不阻断签到
    try:
        db.purge_expired_deleted_accounts()
    except Exception as e:
        logger.debug("清理超期软删除账号失败（不影响签到）: %s", e)

    if not accounts:
        logger.error("未配置任何账号，请通过以下任一方式配置：")
        logger.error("  1. yiban.db 数据库（推荐，用网页后台添加）")
        logger.error("  2. YIBAN_ACCOUNTS_JSON 环境变量（JSON 数组）")
        logger.error("  3. YIBAN_ACCOUNTS 环境变量（旧格式 phone:password#phone2:password2）")
        logger.error("  4. YIBAN_PHONE / YIBAN_PASSWORD 环境变量（单账号）")
        sys.exit(1)

    # --only 过滤：只保留指定手机号（手动签到单个账号）
    # 未命中号码逐号 warning；全不命中时报错退出（既有行为）。
    if args.only:
        accounts, _missing = _apply_only_filter(accounts, args.only)
        if not accounts:
            # 与 _apply_only_filter 同口径脱敏（args.only 是完整裸号）
            logger.error("--only 指定账号不在配置中: %s",
                         ", ".join(_mask_phone(p) for p in _missing))
            sys.exit(1)

    # 仅检查配置模式：不发任何网络请求，用于部署验证
    if args.check_config:
        print_config_summary(accounts)
        sys.exit(0)

    # 启动延迟已废弃（v0.29.0）：仅保持旧签名兼容，值不再使用（read 后仅透传给
    # run_queue_retry 的兼容参数位）；账号间隔 gap_max 仍生效
    start_delay_max = parse_env_int("YIBAN_START_DELAY_MAX", 0)
    # 缺省 10 与 web 设置页「默认开启 10 秒」口径一致（web 端 DEFAULT_ACCOUNT_GAP_MAX）：
    # 纯 signin 部署（.env 未配置该键）升级后自动获得 10s 账号间隔
    gap_max = parse_env_int("YIBAN_ACCOUNT_GAP_MAX", 10)

    # 周日签到开关：关闭时周日跳过（cron 已改为每天执行，靠此开关维持周日不签）；
    # 手动签到（--only）不受限——用户主动触发应当放行
    if not args.only and clock.now().weekday() == 6 and not SUNDAY_SIGN:
        logger.info("==== 周日签到未开启（系统设置中开启后周日也会尝试签到），跳过执行 ====")
        sys.exit(2)  # SKIPPED 语义：run.sh 写 SKIPPED 状态，次日正常执行

    # 周六签到开关：默认开启（周六照常签到）；管理员关闭后周六跳过。
    # 手动签到（--only）不受限——用户主动触发应当放行（与周日开关语义一致）。
    if not args.only and clock.now().weekday() == 5 and not SATURDAY_SIGN:
        logger.info("==== 周六签到已关闭（系统设置中开启后周六也会尝试签到），跳过执行 ====")
        sys.exit(2)  # SKIPPED 语义：run.sh 写 SKIPPED 状态，次日正常执行

    # 全局暂停（管理员 Web UI 一键暂停）：下一轮生效，当前进程照常跑完。
    # 手动签到（--only）不受限——用户主动触发应当放行（与周日开关语义一致）。
    # YIBAN_GLOBAL_PAUSE 由 .env 写入，run.sh 加载后经环境变量传入。
    if not args.only and str(os.environ.get("YIBAN_GLOBAL_PAUSE", "")).strip().lower() in ("1", "true", "on", "yes"):
        logger.info("==== 签到已暂停（管理员通过 Web UI 一键暂停），跳过执行 ====")
        sys.exit(2)  # SKIPPED 语义：run.sh 写 SKIPPED 状态，恢复后次日正常执行

    # 补签轮定向重跑：存在未了结账号时补签闸门整站重跑，会把当日已 success 的
    # 账号再次完整登录（风控暴露）。现剔除已了结账号（success/already），
    # 只重跑未完成者；全部已了结则静默结束（退出码 0，不空跑一轮）。
    # --only 手动签到不受影响。
    if not args.only and _is_second_run():
        accounts = _second_run_drop_done(accounts)
        if not accounts:
            logger.info("==== 补签轮：当日账号均已了结，无需重跑 ====")
            sys.exit(0)

    # 进程级单实例锁（2026-08-20 对抗性审查 P2）：防 cron 全量队列与手动 --only
    # 并发签到同一账号。--only 被持有 → 留痕退出；全量被持有 → 等待至多
    # YIBAN_RUN_LOCK_WAIT 秒后继续（不因手动签到阻塞而漏签一整天）。
    try:
        _run_lock_fh = _acquire_run_lock(bool(args.only))
    except _RunLockHeld:
        logger.warning("已有签到进程在运行，本次手动签到跳过（防同账号并发，稍后可重试）")
        # 原 exit 0 让 web 把"静默跳过"当成功展示；3 = 队列忙，
        # 调用方可据此向用户如实提示（退出码语义见文件头/退出码表）
        sys.exit(3)

    # 版本号写进轮次横幅：发布门槛靠它把"生产跑过的轮次"与提交对齐
    # （docs/dev/release-gate.md §4），生产日志本身不带版本信息。
    logger.info(
        f"==== 开始执行签到（v{RELEASE_VERSION}），共 {len(accounts)} 个账号，队列重试模式 ===="
    )
    # 状态文件以"尝试开始时刻"的日期命名（防跨午夜执行写错当天）
    attempt_date = clock.now().strftime("%Y-%m-%d")
    # 自动错峰（仅自动签到；--only 手动签到立即执行，不走计划）
    schedule = {} if args.only else build_schedule(accounts)
    if schedule:
        # 容量预检（调度 v2 第三层）：可容纳账号数 < 待签到账号数 → 告警不静默
        # 用户自暂停账号不参与调度，也不计入容量
        _cfg = _schedule_config()
        _win = window.bounds(_cfg)
        # 预检按**剩余**有效窗口算：本进程此刻才起跑，已流逝的窗口签不了。
        # 原实现用完整窗口算，迟启动时按满容量放行且不告警，超出的账号只能落
        # skipped_window——管理员看不到任何提示。
        _rest_sec = _win.remaining_sec(clock.now())
        _win_end = window.to_dt(
            clock.now().replace(hour=0, minute=0, second=0, microsecond=0),
            _win.hi_min,
        ).strftime("%H:%M")
        active_n = sum(1 for a in accounts if not getattr(a, "user_paused", False))
        # 与 web 容量预估同一函数：账号间隔是「上一次完成 → 下一次开始」的下限，
        # 故单账号周期 = avg + gap（旧实现只算 n × avg，与预估口径相差 ~2.3 倍）
        _cap = capacity_accounts(max(0.0, _rest_sec), gap_max, _cfg["avg_attempt_sec"])
        if _rest_sec <= 0:
            logger.warning(
                "容量预检: 本进程起跑时签到时段已结束（有效窗口至 %s），本轮不会发起任何请求",
                _win_end,
            )
            _collect_admin_mail(
                "易班签到容量超载",
                f"本次签到进程起跑时已过有效签到窗口（窗口至 {_win_end}），"
                f"{active_n} 个账号本轮不会执行。如非预期，请检查触发时刻（cron / 容器调度）"
                "与签到窗口设置（YIBAN_SIGN_START / YIBAN_SIGN_END）。",
            )
        elif active_n > _cap:
            logger.warning(
                "容量预检: %d 个账号 > 剩余有效窗口 %d 秒可容纳的 %d 个"
                "（单账号 %.0fs + 账号间隔 %ds，窗口至 %s），部分账号可能无法在窗口内完成",
                active_n, int(_rest_sec), _cap, _cfg["avg_attempt_sec"], gap_max, _win_end,
            )
            # 超载提醒（对抗性审查补）：通知管理员，避免"超限只在日志里"无人知情。
            # A 线合并：并入任务结束汇总邮件；webhook 仍即时推送。
            _collect_admin_mail(
                "易班签到容量超载",
                f"当前 {active_n} 个账号，剩余有效窗口 {int(_rest_sec)}s（至 {_win_end}）"
                f"仅可容纳 {_cap} 个"
                f"（单账号 {_cfg['avg_attempt_sec']}s + 账号间隔 {gap_max}s），"
                "部分账号可能无法在窗口内完成签到。\n"
                f"建议：增加窗口时长、缩短账号间隔或减少账号数量（.env 调整）。",
            )
            send_notification(
                "易班签到容量超载",
                f"当前 {active_n} 个账号，剩余有效窗口 {int(_rest_sec)}s（至 {_win_end}）"
                f"仅可容纳 {_cap} 个"
                f"（单账号 {_cfg['avg_attempt_sec']}s + 账号间隔 {gap_max}s），"
                "部分账号可能无法在窗口内完成签到。\n"
                f"建议：增加窗口时长、缩短账号间隔或减少账号数量（.env 调整）。",
                notify_url,
            )
        # 计划写入状态文件（pending 态展示"今日计划 HH:MM"）；执行时按时间点排序
        for acc in accounts:
            t = schedule.get(acc.phone)
            if t:
                _write_sign_state(
                    acc.phone, STATUS_PENDING,
                    f"计划 {t.strftime('%H:%M')}", scheduled=t.strftime("%H:%M:%S"),
                )
        accounts = sorted(accounts, key=lambda a: schedule.get(a.phone, datetime.max))
        # 调度快照标记（2026-08-15 用户反馈：卡点缓冲）：web 端保存自选时以此时刻为
        # "今日/明日生效"分界——改选在快照后必为明日生效，提示与实际 100% 一致
        # （原固定"窗口起点+1 分钟"与 cron 实际读取时刻有几秒偏差窗口）
        try:
            _snap_dir = os.environ.get("YIBAN_STATE_DIR", "/var/log/yiban")
            os.makedirs(_snap_dir, exist_ok=True)
            _snap_path = os.path.join(_snap_dir, f"sched-snapshot-{attempt_date}.json")
            _snap_tmp = _snap_path + ".tmp" + str(os.getpid())
            with open(_snap_tmp, "w", encoding="utf-8") as _f:
                json.dump({"snapshot_at": clock.now().strftime("%H:%M:%S")}, _f)
            os.replace(_snap_tmp, _snap_path)
        except OSError:
            pass  # 标记不可写时 web 端回退旧分界，不影响签到

    # 账密熔断状态：跨天计数（暂停账号零请求；手动签到 --only 不受限）
    cred_state = {} if args.only else _load_cred_state()
    # 签到事件收集器——run_queue_retry 每次尝试/迁移经 sink 上报，
    # 任务结束后单事务批量落库（见 results 赋值后的 add_sign_events_batch）。
    event_rows = []
    delegated = set()   # 不在本执行体范围内的账号（多执行体分工，见 run_queue_retry 说明）
    results = run_queue_retry(
        accounts, notify_url, start_delay_max, gap_max, schedule=schedule, cred_state=cred_state,
        event_sink=event_rows.append, delegated=delegated,
        # 手动指定账号（--only）允许重签当日已了结的账号：用户主动点的那一下应当照做
        reclaim=bool(args.only),
    )
    # 2026-08-20 对抗性审查修复（P1）：--only 此前无条件以本次（仅含目标账号的）状态
    # 整体覆盖保存——空 dict 时直接删除状态文件，其他账号的 fail_days/paused_since
    # 全部丢失，账密熔断保护被任意一次手动签到全局重置。现改为：--only 只把本次
    # 处理账号的熔断增量合并回存量状态（成功→清除该账号记录；凭据失败→按日累计；
    # 其他失败→不动），未处理账号保持原状。全量模式语义不变（本轮本就基于存量计算）。
    if args.only:
        # 增量合并（唯一入口内的读-改-写持锁）：只覆盖本次处理账号的熔断增量
        merged = _load_cred_state()
        _merge_today = clock.now().strftime("%Y-%m-%d")
        for _acc in accounts:
            _res = results.get(_acc.phone)
            if _res is None:
                continue
            _ok, _msg, _skip, _status = _res
            _was_paused = bool(merged.get(_acc.phone, {}).get("paused_since"))
            _update_cred_state(merged, _acc.phone, _ok, _msg, _merge_today)
            # 2026-08-21 对抗性审查补充：手动试探已暂停账号且凭据仍失败时，
            # 顺延下次试探日（对齐全量模式语义）——否则存量过期 probe_date 会让
            # 下一轮全量签到立即再试探，失去半开试探的间隔保护
            if (
                not _ok
                and _was_paused
                and _is_credential_failure(_msg)
                and _probe_due(merged.get(_acc.phone, {}), _merge_today)
            ):
                merged[_acc.phone]["probe_date"] = (
                    datetime.strptime(_merge_today, "%Y-%m-%d")
                    + timedelta(days=PROBE_INTERVAL_DAYS)
                ).strftime("%Y-%m-%d")
        _save_cred_state(merged, touched={a.phone for a in accounts})
    else:
        # 全量轮：按账号增量合并（内存快照不能整体覆盖磁盘——见 _save_cred_state 文档）
        _save_cred_state(cred_state, touched={a.phone for a in accounts})

    # 汇总（合并为一行统计；逐账号结果已在执行中输出，不再逐行重复）
    # 口径：成功=success/already；跳过=no_task+skipped（无需签到与时段外同列）；
    # 已执行=已了结（success/already/no_task），窗口外等跳过不算（7:10 还会再跑）。
    # 2026-09-01：no_position（易班侧无点位）归入跳过计数但单独展示——非账号失败，
    # 不参与 has_real_failure；但归入"未了结"（与容器调度器 _UNDONE_STATUSES 同语义），
    # 宿主 exit 2 / 容器 07:10 补签轮均会重跑一次——学校延迟放位时仍有兜底
    # （无点位账号 1 次即止、幂等无害）。
    has_real_failure = False
    has_executed = False
    # 窗口外/缺失（skipped_window/skipped_norange）属"未了结"——
    # 与容器调度器 _UNDONE_STATUSES（docker/scheduler.py）同一语义。宿主 run.sh 的
    # 07:10 补签闸门只认状态文件 SUCCESS 文本：若本轮有成功就把 skipped 账号的
    # 退出码判成 0，run.sh 写 SUCCESS → 补签被吞，被跳过的账号当天失去兜底
    # （容器侧已修此洞，宿主侧是本轮补齐）。
    has_window_skip = False
    ok_n = fail_n = skip_n = no_pos_n = other_n = 0
    for acc in accounts:
        if acc.phone in delegated:
            # 由其他执行体负责：既不算成功也不算失败。若把它当失败，多执行体形态下
            # 每个执行体都会把别人的活报成自己的失败（退出码与告警都会失真）。
            other_n += 1
            continue
        _s, _m, _sk, status = results.get(acc.phone, (False, "未执行", False, STATUS_PENDING))
        if status in (STATUS_SUCCESS, STATUS_ALREADY):
            ok_n += 1
        elif status in (STATUS_NO_TASK, STATUS_SKIPPED_WINDOW, STATUS_SKIPPED_NORANGE,
                        STATUS_PAUSED, STATUS_USER_CANCELLED, STATUS_NO_POSITION):
            skip_n += 1
            if status in (STATUS_SKIPPED_WINDOW, STATUS_SKIPPED_NORANGE):
                has_window_skip = True
            if status == STATUS_NO_POSITION:
                no_pos_n += 1
        else:
            fail_n += 1
            has_real_failure = True
        if status in (STATUS_SUCCESS, STATUS_ALREADY, STATUS_NO_TASK):
            has_executed = True
    summary = f"✅ {ok_n} 成功，❌ {fail_n} 失败"
    if skip_n:
        summary += f"，➖ {skip_n} 跳过"
    if no_pos_n:
        summary += f"，🚫 {no_pos_n} 无点位"
    if other_n:
        summary += f"，⇄ {other_n} 由其他执行体负责"
    logger.info(f"==== 签到汇总（v{RELEASE_VERSION}）：{summary} ====")

    # 窗口外未了结专项告警。
    # is_second_run：run.sh 补签轮（07:10）导出的 YIBAN_SECOND_RUN=1 优先
    # （首签子进程被 timeout 击杀、exit 124 未写 sched-run 标记时，
    # 标记兜底失效，必须靠 run.sh 的补签轮环境变量识别）；容器调度器 SECOND
    # 时段同样注入该变量；sched-run 标记作为兜底（手动/其他启动路径）。
    _maybe_alert_zero_success(
        accounts, results, ok_n, is_second_run=_is_second_run()
    )

    # 签到事件落库——v6 建了 sign_events 表但签到主流程零写入
    # （仅探针 stage=probe 有写入），统计/时间线读取函数零调用方，基础设施空转。
    # 现每次尝试与状态迁移落一行（stage=sign），批量单事务写入；失败仅告警
    # （add_sign_events_batch 内部捕获），不影响签到退出码。
    if event_rows:
        db.add_sign_events_batch(event_rows)

    # 写按日状态文件（供网页日历组件读取；窗口外跳过不写，当天留空）
    # 符号按状态码：success/already→✅、no_task→➖、failed→❌、no_position→🚫
    state_dir = os.environ.get("YIBAN_STATE_DIR", "/var/log/yiban")
    try:
        os.makedirs(state_dir, exist_ok=True)
        # M14：汇总文件以写盘时日期命名（跨午夜不沿用启动时的 attempt_date）
        daily_path = os.path.join(state_dir, f"sign-daily-{clock.now().strftime('%Y-%m-%d')}.json")
        with _state_file_lock(daily_path):
            daily = {}
            if os.path.exists(daily_path):
                try:
                    with open(daily_path, encoding="utf-8") as f:
                        daily = json.load(f)
                except (OSError, ValueError, TypeError):
                    logger.warning("按日状态文件 %s 损坏，按空数据重建", daily_path)
                    daily = {}
            if not isinstance(daily, dict):
                logger.warning("按日状态文件 %s 非 dict，按空数据重建", daily_path)
                daily = {}
            for acc in accounts:
                _s, _m, _sk, status = results.get(acc.phone, (False, "未执行", False, STATUS_PENDING))
                if status in (STATUS_SUCCESS, STATUS_ALREADY, STATUS_NO_TASK,
                              STATUS_FAILED, STATUS_NO_POSITION):
                    daily[acc.phone] = STATUS_SYMBOL[status]
            # M15：tmp + os.replace 原子写，避免半截文件
            daily_tmp = daily_path + ".tmp" + str(os.getpid())
            with open(daily_tmp, "w", encoding="utf-8") as f:
                json.dump(daily, f, ensure_ascii=False)
            os.replace(daily_tmp, daily_path)
    except (OSError, ValueError, TypeError) as e:
        logger.warning("写入按日状态文件失败: %s", e)

    # A 线合并：签到任务彻底结束后，把运行期收集的管理员告警汇总成一封邮件发送。
    # 无异常则不发送（成功不打扰）；mailer 内部静默失败，不影响退出码。
    _flush_admin_mail_summary()

    # 全量运行完成标记：调度器首签/补签闸门的事实源。
    # 仅全量模式写入；--only 手动签到不写——手动成功不得压制调度器当日判定。
    if not args.only:
        _write_sched_done({"ok_n": ok_n, "fail_n": fail_n, "skip_n": skip_n})

    # 退出码（run.sh 依据退出码写状态文件）：
    # 0 - 全部成功（有实际签到执行；含"已签到""无需签到"，且无窗口外未了结账号）
    # 1 - 有真正的失败（登录失败、签到失败等）
    # 2 - 全部 skip 或存在窗口外未了结账号（无实际执行，或首签窗口外账号需 07:10 补签
    #     重跑；此时 run.sh 写 SKIPPED 而非 SUCCESS，补签 cron 才会继续尝试）
    #     由 run.sh 写 SKIPPED 而非 SUCCESS，避免备份等下游任务被吞
    if has_real_failure:
        sys.exit(1)
    if not has_executed or has_window_skip:
        sys.exit(2)
    sys.exit(0)


if __name__ == "__main__":
    main()
