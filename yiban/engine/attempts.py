# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""单账号一次尝试：登录 + 签到、失败分级、会话缓存联动、账密熔断计数。

本模块只管**一个账号的一次尝试**，以及失败之后的三个问题：值不值得重试、重试上限
是多少、要不要清掉会话缓存；排队、间隔、重试落点与领取池在 `round`，入口与汇总在
`runner`。

同处一并保留登录链路的**同名外观别名**（客户端 `YibanClient`、请求头版本特征、
白名单/WAF 判定、定位算法、安全随机数）：它们都服务于"一次登录 + 签到"，且旧调用点
（`signin.<名字>`、`web/app.py`）继续按原名引用——转发的是同一对象，不是第二份实现。

跨模块调用纪律见包说明：跨模块一律走模块属性访问。
"""
import logging
import secrets
from datetime import datetime, timedelta

from yiban import client as yiban_client
from yiban import security
from yiban import status as yiban_status
from yiban.fyiban import algo as fyiban_algo
from yiban.fyiban import headers as fyiban_headers
from yiban.masking import sanitize_text as _sanitize_text
from yiban.masking import sanitize_url as _sanitize_url
from yiban.store import accounts as accounts_store
from yiban.store import db

logger = logging.getLogger("yiban")

# 状态码别名（与 yiban.status 同一对象）
STATUS_USER_CANCELLED = yiban_status.STATUS_USER_CANCELLED
STATUS_FAILED = yiban_status.STATUS_FAILED

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


# ---------------------------------------------------------------------------
# 定位生成：多边形内随机点
# ---------------------------------------------------------------------------
# 算法**衍生自上游 FYIBAN**（缩放质心 + 射线法），实现在第三方隔离层
# `yiban/fyiban/algo.py`；采样分布与兜底策略的本地差异见同目录 PROVENANCE.md。
point_in_polygon = fyiban_algo.point_in_polygon
generate_position_in_polygon = fyiban_algo.generate_position_in_polygon

# 密码学安全随机数生成器（用于定位生成等安全敏感场景）
_secure_random = secrets.SystemRandom()


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
# 单账号尝试（一次登录 + 一次签到，不重试）
# ---------------------------------------------------------------------------
# 运行期账号复核（"启动快照跑完整轮期间账号可能被删/停用"）的实现在
# `yiban/store/accounts.py::account_still_signable`——会话缓存的写入闸门
# （`yiban.client`）与本模块必须用**同一份**判据，各写一份会分叉。
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


# ---------------------------------------------------------------------------
# 请求头版本特征与 WAF 判定（同名转发，同一对象）
# ---------------------------------------------------------------------------
# 易班 App 请求头与版本特征：**衍生自上游 FYIBAN**，已迁到第三方隔离层
# （yiban/fyiban/headers.py，来源与差异见 yiban/fyiban/PROVENANCE.md）。
YIBAN_APP_VERSION = fyiban_headers.YIBAN_APP_VERSION
HEADERS = fyiban_headers.HEADERS
KILLYIBAN_HEADERS = fyiban_headers.KILLYIBAN_HEADERS

# WAF 判定口径的唯一实现在 `yiban/security.py`（含"只在短响应里检测"的边界理由
# 与 Unicode 转义解码），此处只做同名转发——调用方与既有测试继续用 signin 的名字。
WAF_KEYWORDS = security.WAF_KEYWORDS
is_waf_blocked = security.is_waf_blocked


# ---------------------------------------------------------------------------
# 账密熔断器：连续凭据失败 → 暂停签到（零请求），周期性半开试探自动恢复
# ---------------------------------------------------------------------------
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
