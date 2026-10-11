# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""单账号一次尝试：登录 + 签到、失败分级、会话缓存联动、账密熔断计数。

本模块只管**一个账号的一次尝试**，以及失败之后的三个问题：值不值得重试、重试上限
是多少、要不要清掉会话缓存；排队、间隔、重试落点与领取池在 `round`，入口与汇总在
`runner`。

另有一批**同名转发**（客户端 `YibanClient`、请求头版本特征、白名单/WAF 判定、定位
算法）：它们都服务于"一次登录 + 签到"，实现分别在 `yiban/client.py`、`yiban/platform.py`、
`yiban/geo.py`、`yiban/security.py`；此处转发的是**同一对象**，不是第二份实现——旧调用点
（`signin.<名字>`、`web/app.py`）继续按原名引用。

跨模块调用纪律见包说明：跨模块一律走模块属性访问。
"""
import logging
from datetime import datetime, timedelta

from yiban import client as yiban_client
from yiban import geo as yiban_geo
from yiban import platform as yiban_platform
from yiban import security
from yiban import status as yiban_status
from yiban.masking import mask_url_userinfo as _mask_url_userinfo
from yiban.masking import sanitize_text as _sanitize_text
from yiban.masking import sanitize_url as _sanitize_url
from yiban.store import accounts as accounts_store
from yiban.store import db

logger = logging.getLogger("yiban")

# 状态码别名（与 yiban.status 同一对象）
STATUS_USER_CANCELLED = yiban_status.STATUS_USER_CANCELLED
STATUS_FAILED = yiban_status.STATUS_FAILED

# ---------------------------------------------------------------------------
# 重试分级：判断一次失败是否值得重试、重试上限（`_retry_budget`）
# ---------------------------------------------------------------------------
# 总尝试上限：1 次初始尝试 + 最多 2 次重试
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
# 挑战解析/白名单/非 JSON/假成功（无签发方回执）硬失败（总尝试上限 1 次）：判据词元在 `yiban.security.HARD_FAIL_TOKENS`
# （唯一真值源，档位与探针共用）。同一输入必然同一结果，重试只是把同一死页重发；会话停在
# 未通过的挑战/拦截链上，一并清除（`_retry_budget` 联动 clear_cache=True）。
HARD_FAIL_MAX_ATTEMPTS = 1

# 确定性认证失败特征：账号密码本身错误或已被易班侧锁定（msgCN 原文）。重试只会把同一次
# 错误登录再提交一遍，还会加速触发易班「错误尝试过多」的账号锁定——终态，不重试。
AUTH_FAIL_KEYWORDS = [
    "账号或密码错误",
    "错误尝试过多",
]
# 确定性认证失败的总尝试上限：仅首试 1 次
AUTH_FAIL_MAX_ATTEMPTS = 1

# 风控/凭据类失败特征：重试不仅无用，还可能加重账号标记。
#
# WAF 族**不写进本表**：名单与匹配口径都只有一份，在 `yiban.security`
# （`WAF_KEYWORDS` + `matches_waf_keywords`）——同一事实两个定义点必然各自演化
# （工单 `yiban-auto-sign-u21x` 的根因形状）。想加风控词元请改 security 的名册，
# 本表只列本层自有的凭据/协议措辞（都是服务端文案原文，按子串匹配）。
RISK_FAIL_KEYWORDS = [
    "账号或密码错误",
    "e003",
    "无效的应用端",
    "e001",
    "origin invalid",
    "登录失败",
    "登录响应异常",
    "OAuth 页解析失败",
]

# 会话陈旧类失败特征：缓存会话已被服务端作废（夜间自然过期、或本人用手机端易班登录
# 把 Web 会话挤掉）。后四个是同一语义的措辞变体——服务端文案一改就会被判"网络类"，
# 重新空跑满重试上限。
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

# 凭据类失败关键词（熔断器计数用）：账号密码问题——连续失败达到阈值后暂停签到。
# **不含 WAF/风控关键词**（那是环境问题不是凭据问题，不计入）；也不要加回
# "登录失败"/"登录响应异常"/"OAuth 页解析失败"——它们对应的是环境类失败
# （OAuth 页解析不出、响应缺 reUrl 等，通常是海外 IP 被风控），误计会在连续
# CRED_FAIL_DAYS 天后把账号错误冻结并诱导用户无谓改密。
# 真实口令错误的消息含 "账号或密码错误"（登录流程 raise 处），由首条关键词覆盖。
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

# ---------------------------------------------------------------------------
# 同名转发（转发的是同一对象，不是第二份实现）
# ---------------------------------------------------------------------------
# 定位生成：多边形内随机点（洁净室自研：偶奇射线法 + 拒绝采样 + 显式兜底），
# 实现在 `yiban/geo.py`，算法语义与边界条件清单见该模块文档串。
point_in_polygon = yiban_geo.point_in_polygon
generate_position_in_polygon = yiban_geo.generate_position_in_polygon

# 客户端外观（凭据托管 / 会话缓存 / 代理 / 设备绑定）在 `yiban/client.py`，
# 协议步骤在 `yiban/platform.py`，安全策略在 `yiban/security.py`。
# 转发同一类对象：既有调用点与 `patch.object(signin.YibanClient, ...)` 的测试行为不变。
YibanClient = yiban_client.YibanClient

# 易班 App 请求头与版本特征：**平台事实常量**（客户端版本号取自官方安装包清单、
# 头字段值取自实拍请求），实现在 `yiban/platform.py`。
YIBAN_APP_VERSION = yiban_platform.YIBAN_APP_VERSION
HEADERS = yiban_platform.HEADERS
KILLYIBAN_HEADERS = yiban_platform.KILLYIBAN_HEADERS

# WAF 判定口径的唯一实现在 `yiban/security.py`（形态判定不受长度限制、仅关键词匹配按
# "短响应"设界的边界理由、词元的非字母数字边界口径、Unicode 转义解码）；调用方与既有测试
# 继续用这里的名字。
WAF_KEYWORDS = security.WAF_KEYWORDS
is_waf_blocked = security.is_waf_blocked

# 白名单口径的唯一实现在 `yiban/security.py`（宽松 = 登录链路跟随的跳转；
# 严格 = 主机精确等于 f.yiban.cn）。
_is_yiban_trusted_url = security.is_yiban_trusted_url
_is_strict_yiban_url = security.is_strict_yiban_url

# 运行期账号复核（"启动快照跑完整轮期间账号可能被删/停用"）的实现在
# `yiban/store/accounts.py::account_still_signable`——会话缓存的写入闸门
# （`yiban.client`）与本模块必须用**同一份**判据，各写一份会分叉。
account_still_signable = accounts_store.account_still_signable


# ---------------------------------------------------------------------------
# 公开入口
# ---------------------------------------------------------------------------
def matches_risk_keywords(message):
    """失败消息是否命中"风控**或**凭据"族——**只给重试档位用**。

    读者只有 `classify_failure`（档位语义：凭据错重试无用，故少给重试）。**不要**拿它当
    "平台在限我们"的判据：执行体的风控信号另有一套只认 WAF 族与挑战形态的判据
    （`executor_v3._is_risk_signal`）——两处语义相反，共用会把口令错的账号当成平台风控
    （工单 `yiban-auto-sign-zggs`）。

    本层自有词元按子串（都是服务端文案原文，无误报面）；WAF 族走
    `security.matches_waf_keywords`——ASCII 词元要求两侧非字母数字，否则失败消息里嵌的
    base64（异常消息会带上响应片段或请求 URL）会把普通失败判成风控档：少一次重试、
    还白清一次会话缓存。这里**不设**`is_waf_blocked` 的"短响应"上界：入参是消息不是响应体。
    """
    return (any(kw in message for kw in RISK_FAIL_KEYWORDS)
            or security.matches_waf_keywords(message))


def classify_failure(message):
    """对失败信息分级，返回总尝试上限。

    - 挑战解析/白名单/非 JSON/假成功（无签发方回执）硬失败（词元在 `yiban.security.HARD_FAIL_TOKENS`，唯一真值源）：
      仅首试 1 次——同一输入必然同一结果，重试只会把同一死页重发
    - 风控/凭据类：最多重试 1 次（RISK_MAX_ATTEMPTS），避免加重账号标记
    - 其他失败（网络/未知）：最多重试 MAX_ATTEMPTS 次
    （确定性认证失败在 _retry_budget 处更早拦截，不会再走到这里）
    """
    if security.is_hard_fail_message(message):
        return HARD_FAIL_MAX_ATTEMPTS
    if matches_risk_keywords(message):
        return RISK_MAX_ATTEMPTS
    return MAX_ATTEMPTS


def clear_session_cache_quiet(phone):
    """清除单账号会话缓存（模块级入口，供重试队列联动）；失败仅留痕不影响签到。

    db 未初始化（环境变量账号模式）时为无操作。
    """
    try:
        if db.is_initialized():
            db.clear_session_cache(phone)
    except Exception as e:
        logger.debug(f"[{phone}] 清除会话缓存失败: {_sanitize_text(e)}")


def attempt_signin(account):
    """单次签到尝试（登录 + 签到），不重试。

    返回 (success, message, skip, status)：
    - success: 是否成功（含"已签到""非签到日"）
    - message: 结果说明（异常时已脱敏，不返回原始 str(e)）
    - skip: True 表示窗口外等无需重试的情况
    - status: 签到状态码（STATUS_*）

    通知不在这里发：逐次失败不通知（避免通知风暴），最终放弃时由 run_queue_retry
    统一通知一次。
    """
    phone = account.phone
    if not account_still_signable(account):
        # 运行期复核：账号在本轮执行期间被删除/停用 → 不发起任何请求。
        # 只在启动时筛一次的话，被删账号仍会被完整登录并签退，还会把会话缓存
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
            # 无论成败，尝试结束后立即清零/解除本客户端持有的凭据副本
            # （重试由 run_queue_retry 重新构造客户端，Account 本体不受影响）
            client._wipe_credentials()
    except Exception as e:
        # exc_info=False 是有意的：堆栈可能包含含敏感数据的源码上下文，异常消息经
        # _sanitize_text 脱敏后已足够定位。requests 异常消息内嵌完整请求 URL
        # （含 CSRF 令牌），故依次过 _sanitize_url（query）与 _mask_url_userinfo（userinfo）再落日志。
        safe_err = _sanitize_text(_mask_url_userinfo(_sanitize_url(str(e))))
        logger.error(f"[{phone}] ❌ 尝试失败: {safe_err}", exc_info=False)
        return False, safe_err, False, STATUS_FAILED


# ---------------------------------------------------------------------------
# 私有实现：失败分级细则与账密熔断
# ---------------------------------------------------------------------------
def _is_session_stale_failure(message):
    """是否为"缓存会话已被服务端作废"类失败——账密本身没问题，不是凭据类失败。"""
    return any(kw in message for kw in SESSION_STALE_FAIL_KEYWORDS)


def _retry_budget(message):
    """返回 (该失败下的最大尝试次数, 是否应清除该账号会话缓存)。

    四条特例都优先于风控分级：
    - 确定性认证失败：密码错误/账号锁定，重试无意义且有害（多一次真实登录会加速
      易班侧锁定）；
    - 无签到点位：易班侧没有数据，重试拿不到就是拿不到；
    - 挑战解析/白名单/非 JSON 硬失败：同一死页重发无益，且会话停在未通过的挑战/拦截
      链上，留着复用等于带病续跑——清掉；
    - 会话陈旧：不清缓存重登，重试只是把同一份死缓存的失败原样复演。
    """
    if any(kw in message for kw in AUTH_FAIL_KEYWORDS):
        # 清缓存同样成立：登录失败的会话残片没有复用价值
        return AUTH_FAIL_MAX_ATTEMPTS, True
    if any(kw in message for kw in NO_POSITION_FAIL_KEYWORDS):
        return NO_POSITION_MAX_ATTEMPTS, False
    if security.is_hard_fail_message(message):
        return HARD_FAIL_MAX_ATTEMPTS, True
    if _is_session_stale_failure(message):
        return SESSION_STALE_MAX_ATTEMPTS, True
    max_attempts = classify_failure(message)
    # 风控类（e003/WAF 等）会话已不可信；"授权设备"非风险关键词（属设备配置问题，
    # 仍可重试），但同样意味着当前会话不可信，一并清除
    return max_attempts, (max_attempts == RISK_MAX_ATTEMPTS or "授权设备" in message)


def _is_credential_failure(message):
    """凭据类失败判定（不含 WAF/风控——那是环境问题不是凭据问题）。"""
    return any(kw in message for kw in CRED_FAIL_KEYWORDS)


def _next_probe_date(today):
    """从 `today` 顺延一个半开试探周期，返回新的试探日。"""
    return (datetime.strptime(today, "%Y-%m-%d")
            + timedelta(days=PROBE_INTERVAL_DAYS)).strftime("%Y-%m-%d")


def _update_cred_state(cred_state, phone, success, message, today):
    """执行一次后更新账密熔断状态。

    - 成功：清除该账号记录（恢复 ACTIVE）
    - 凭据类失败：连续失败天数 +1（同一天多次失败只计 1 天）；达到阈值 → 暂停并设试探日
    - 已暂停账号在试探日的凭据类失败（半开试探失败）：顺延试探日一个周期
    - 其他失败（网络等）：不计数不动记录

    本函数是 `probe_date` 的**唯一写入点**，全部执行路径都经它更新熔断状态
    （生产执行体 `executor_v3`、`--only` 手动轮、旧领取池 `round`）。顺延必须留在
    这里：写在外层调用点会漏掉生产执行体那条路径，`probe_date` 便停在过去，
    `_probe_due` 从此每天为真，密码确实错的账号被每天真实登录一次（风控暴露面）。
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
    if cred.get("paused_since"):
        if _probe_due(cred, today):
            # 半开试探失败：只顺延试探日，暂停状态不变
            cred["probe_date"] = _next_probe_date(today)
            logger.warning("[%s] ⏸️ 半开试探失败，保持暂停（下次 %s 试探）",
                           phone, cred["probe_date"])
    elif cred["fail_days"] >= CRED_FAIL_DAYS:
        cred["paused_since"] = today
        cred["probe_date"] = _next_probe_date(today)
    cred_state[phone] = cred


def _probe_due(cred, today):
    """半开试探判定：暂停中且今天已到（或超过）试探日。"""
    if not cred.get("paused_since"):
        return False
    return not cred.get("probe_date") or today >= cred["probe_date"]
