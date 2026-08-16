#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# 易班自动签到脚本（AGPL-3.0，见项目根 LICENSE）
# 本项目为以下 AGPL-3.0 项目的衍生实现，保留上游版权与许可条款：
#   - OneFeiFan/FYIBAN（多边形内随机定位点算法：缩放质心 + 射线法验证；易班登录特征与 nightAttendance 签到流程）
"""
易班自动签到脚本

功能：
1. 自动登录易班（支持多账号，默认 fyiban 同款真实 App 特征登录）
2. 自动获取签到任务范围
3. 在签到范围内生成随机定位点（模拟真实定位）
4. 自动提交签到
5. 支持消息通知（Server 酱、Bark、企业微信等）
6. 重试逻辑：失败账号放队尾分散重试（风控类最多 2 次，其他最多 4 次）
7. 随机延迟：启动与账号间隔随机打散（YIBAN_START_DELAY_MAX / YIBAN_ACCOUNT_GAP_MAX）

参考项目：
- OneFeiFan/FYIBAN 模块（nightAttendance 签到流程与登录特征）
- Auto-Test 项目（旧登录流程，YIBAN_LEGACY_LOGIN=1 启用）
"""

import argparse
import json
import logging
import math
import os
import random
import re
import secrets
import sys
import time
from base64 import b64decode, b64encode
from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import md5
from urllib.parse import urlencode, urlsplit

# 共享模块（同目录）：加密（web/tui/db 共用密钥与密文格式）与 SQLite 数据访问层
import account_crypto
import db  # 2026-08-16 审查轮：原 _load_accounts_from_file/build_schedule 函数内 import 上移（无循环依赖）
import requests
from Crypto.Cipher import PKCS1_v1_5
from Crypto.PublicKey import RSA
from requests.utils import cookiejar_from_dict, dict_from_cookiejar

# ---------------------------------------------------------------------------
# 日志配置
# ---------------------------------------------------------------------------
# 支持通过环境变量调整日志级别：DEBUG / INFO / WARNING / ERROR
LOG_LEVEL = os.environ.get("YIBAN_LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),  # 非法值回退 INFO（不回退 DEBUG，防误配泄露详细堆栈）
    format="[%(asctime)s] [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("yiban")


# 易班 iOS 客户端 UA（与 Auto-Test 保持一致）
HEADERS = {
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/4.0 "
    "Chrome/104.0.5112.97 Mobile Safari/537.36 yiban_iOS/5.0.12",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "X-Requested-With": "com.yiban.app",
    "Origin": "https://app.uyiban.com",
    "Referer": "https://app.uyiban.com/",
    "Connection": "close",
}

# KillYiBan 同款请求头（默认登录方式）
# 注意：usersure 提交时会被显式覆盖为不带 Origin/Referer（见 login_killyiban 第 3 步，
# 实测带 Origin → e001 无效应用端编号），其余请求用此头
KILLYIBAN_HEADERS = {
    "User-Agent": "Yiban",
    "AppVersion": "5.1.2",
    "Origin": "https://c.uyiban.com",
    "Referer": "https://c.uyiban.com/",
    "Connection": "close",
}


# ---------------------------------------------------------------------------
# 账号数据模型
# ---------------------------------------------------------------------------
@dataclass
class Account:
    """单个易班账号配置。

    通过 TUI 配置工具或直接编辑 accounts.json 创建，
    一次输入一个账号的完整信息，无需用符号分隔。
    """

    phone: str
    password: str
    phone_model: str = ""  # 设备型号（学校开启"设备绑定"时必填）
    phone_code: str = ""  # 设备唯一识别码（学校开启"设备绑定"时必填）
    name: str = ""  # 可选：自定义名称（TUI 输入，未填写时显示为"账号N"）
    user_paused: bool = False  # 用户自暂停签到（调度 v2；db.load_accounts 透传）

    @property
    def has_device_info(self):
        return bool(self.phone_model and self.phone_code)


# ---------------------------------------------------------------------------
# 配置常量
# ---------------------------------------------------------------------------
# 队列重试配置：每账号"1 次初始尝试 + 最多 3 次队列重试"（总尝试上限 4 次）
MAX_ATTEMPTS = 4
# 风控类失败（e003/无效应用端等）加重标记风险，最多重试 1 次（总尝试上限 2 次）
RISK_MAX_ATTEMPTS = 2
# 同一账号两次尝试之间的最短间隔（秒），避免紧邻重试被识别为连击
RETRY_MIN_INTERVAL = 60
# 网络/瞬时类失败重试间隔打散上限（秒），作为账号间随机延迟之外的补充
RETRY_GAP_MAX = 30

# 随机延迟默认值（TUI 设置栏开启时采用；默认关闭=0）
# 打散"每天固定秒级执行"的脚本特征，作为 e003 登录特征修复之外的纵深防御
DEFAULT_START_DELAY_MAX = 60  # 启动后随机等待 0~60 秒
DEFAULT_ACCOUNT_GAP_MAX = 10  # 顺序模式账号间随机间隔 0~10 秒

# 签到模式：sequence（列表顺序，默认）/ random（列表随机打散）
# 由网页系统设置页写入 .env（YIBAN_SIGN_MODE），run.sh 加载后经环境变量传入
SIGN_MODE = os.environ.get("YIBAN_SIGN_MODE", "").strip().lower()

# 周日签到开关：部分学校周日也有签到任务（默认关闭，与历史行为一致）
# 由网页系统设置页写入 .env（YIBAN_SUNDAY_SIGN=1），run.sh 加载后经环境变量传入
SUNDAY_SIGN = os.environ.get("YIBAN_SUNDAY_SIGN", "").strip().lower() in ("1", "true", "on", "yes")

# 签到状态码（写 sign-state 状态文件，web/TUI 状态显示的事实源）与日志符号
STATUS_SUCCESS = "success"               # 签到成功（服务器确认打卡完成）
STATUS_ALREADY = "already"               # 今日已签到（重复执行时服务器告知）
STATUS_NO_TASK = "no_task"               # 今日无需签到（服务器确认今日无任务）
STATUS_FAILED = "failed"                 # 最终失败（重试耗尽）
STATUS_RETRYING = "retrying"             # 重试中
STATUS_SKIPPED_WINDOW = "skipped_window"  # 未在签到时段（窗口外）
STATUS_SKIPPED_NORANGE = "skipped_norange"  # 签到窗口缺失（Range 为空）
STATUS_PAUSED = "paused"                # 账密异常暂停（连续凭据失败，熔断器）
STATUS_USER_CANCELLED = "user_cancelled"  # 用户自取消（用户暂停自己的签到任务）
STATUS_PENDING = "pending"               # 待签（未执行/无记录）

# 状态码 → 日志/日历符号（与 web/TUI 显示层一致）
STATUS_SYMBOL = {
    STATUS_SUCCESS: "✅", STATUS_ALREADY: "✅", STATUS_NO_TASK: "➖",
    STATUS_FAILED: "❌", STATUS_RETRYING: "🔄",
    STATUS_SKIPPED_WINDOW: "⛔", STATUS_SKIPPED_NORANGE: "⛔",
    STATUS_PAUSED: "⏸️", STATUS_USER_CANCELLED: "⏹️",
}

# 凭据类失败关键词（熔断器计数用）：账号密码问题——连续失败达到阈值后暂停签到。
# 注意：不含 WAF/风控关键词（那是环境问题不是凭据问题，不计入）。
CRED_FAIL_KEYWORDS = [
    "账号或密码错误",
    "e003",
    "无效的应用端",
    "e001",
    "origin invalid",
    "登录失败",
    "登录响应异常",
    "OAuth 页解析失败",
]
# 熔断参数：连续 N 天凭据失败 → 暂停；暂停后每 N 天半开试探 1 次
CRED_FAIL_DAYS = 3          # 连续凭据失败天数阈值
PROBE_INTERVAL_DAYS = 7     # 暂停后半开试探周期（天）

# 签到窗口（与 web/app.py 一致；自动错峰在窗口内均匀分配时间点）
SIGN_START = (6, 30)
SIGN_END = (7, 50)

# ---------------------------------------------------------------------------
# 调度 v2（S1 demo）：统一填充框架配置
# 设计文档：docs/design/plan-scheduler-v2.md（2×2 组合 + 安全底座）
# ---------------------------------------------------------------------------
_DEFAULT_SIGN_START = (6, 30)
_DEFAULT_SIGN_END = (7, 50)
_DEFAULT_EDGE_SEC = 60          # 首尾缓冲：有效窗口 [SIGN_START+60s, SIGN_END-60s]
_DEFAULT_BLOCK_CAP = 15         # 块容量（每块最多人数，满则向后顺延）
_DEFAULT_MU_MIN_PCT = 40        # 正态高峰中心范围（有效窗口相对位置 %）
_DEFAULT_MU_MAX_PCT = 60
_DEFAULT_SIGMA_MIN_PCT = 15     # 正态分散程度范围（有效窗口宽度 %）
_DEFAULT_SIGMA_MAX_PCT = 25
_DEFAULT_MIN_EXEC_GAP = 5       # 压缩模式间隔下限（分钟）
_DEFAULT_AVG_ATTEMPT_SEC = 8    # 容量预检：单次执行平均耗时估算
_DEFAULT_RETRY_MIN_INTERVAL = 60
_DEFAULT_EXEC_GAP_MIN = 10      # 启动对齐：已过点账号相邻最小间隔（秒）
_DEFAULT_ALLOW_TIME_PREF = 0    # 用户自选时间片总开关（0=关默认，管理员开启后生效）
_DEFAULT_SLOW_SIGN_SEC = 30     # P6 耗时告警阈值（秒）：单次尝试耗时超此值 → warning + 通知


def _parse_hhmm(value, default):
    """解析 HH:MM → (h, m)；非法返回 default。"""
    try:
        h, m = value.strip().split(":")
        h, m = int(h), int(m)
        if not (0 <= h <= 23 and 0 <= m <= 59):
            return default
        return (h, m)
    except (ValueError, AttributeError):
        return default


def _env_int(name, default, lo=None, hi=None):
    """读整数环境变量；缺失/非法回退默认（配置校验：回退 + 警告，不崩溃）。"""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        v = int(raw)
    except ValueError:
        logger.warning("配置 %s=%r 非法，回退默认 %s", name, raw, default)
        return default
    if (lo is not None and v < lo) or (hi is not None and v > hi):
        logger.warning("配置 %s=%s 超出范围 [%s, %s]，回退默认 %s", name, v, lo, hi, default)
        return default
    return v


def _schedule_config():
    """读取调度 v2 配置（每次调用读取，便于测试与热改）。

    兼容旧 YIBAN_SIGN_MODE：sequence→顺序×均匀、random→随机×均匀、normal→顺序×正态；
    新参数 YIBAN_SIGN_ORDER / YIBAN_SIGN_DIST 优先。
    返回 dict：order/dist/edge_sec/block_cap/mu/sigma 百分比/
    min_exec_gap/avg_attempt_sec/retry_min_interval/exec_gap_min/sign_start/sign_end。
    """
    mode = os.environ.get("YIBAN_SIGN_MODE", "").strip().lower()
    order = os.environ.get("YIBAN_SIGN_ORDER", "").strip().lower()
    dist = os.environ.get("YIBAN_SIGN_DIST", "").strip().lower()
    if order not in ("sequence", "random"):
        order = "random" if mode == "random" else "sequence"
        if dist not in ("uniform", "normal"):
            dist = "normal" if mode == "normal" else "uniform"
    elif dist not in ("uniform", "normal"):
        dist = "uniform"
    start = _parse_hhmm(os.environ.get("YIBAN_SIGN_START", ""), _DEFAULT_SIGN_START)
    end = _parse_hhmm(os.environ.get("YIBAN_SIGN_END", ""), _DEFAULT_SIGN_END)
    if start >= end:
        logger.warning("签到窗口 %s >= %s 非法，回退默认 06:30/07:50", start, end)
        start, end = _DEFAULT_SIGN_START, _DEFAULT_SIGN_END
    mu_lo = _env_int("YIBAN_SCHEDULE_MU_MIN_PCT", _DEFAULT_MU_MIN_PCT, 0, 100)
    mu_hi = _env_int("YIBAN_SCHEDULE_MU_MAX_PCT", _DEFAULT_MU_MAX_PCT, 0, 100)
    if mu_lo >= mu_hi:
        logger.warning("μ 范围 %s~%s 非法，回退默认 40~60", mu_lo, mu_hi)
        mu_lo, mu_hi = _DEFAULT_MU_MIN_PCT, _DEFAULT_MU_MAX_PCT
    sigma_lo = _env_int("YIBAN_SCHEDULE_SIGMA_MIN_PCT", _DEFAULT_SIGMA_MIN_PCT, 0, 100)
    sigma_hi = _env_int("YIBAN_SCHEDULE_SIGMA_MAX_PCT", _DEFAULT_SIGMA_MAX_PCT, 0, 100)
    if sigma_lo >= sigma_hi:
        logger.warning("σ 范围 %s~%s 非法，回退默认 15~25", sigma_lo, sigma_hi)
        sigma_lo, sigma_hi = _DEFAULT_SIGMA_MIN_PCT, _DEFAULT_SIGMA_MAX_PCT
    return {
        "order": order,
        "dist": dist,
        "edge_sec": _env_int("YIBAN_WINDOW_EDGE_SEC", _DEFAULT_EDGE_SEC, 0, 600),
        "block_cap": _env_int("YIBAN_BLOCK_CAP", _DEFAULT_BLOCK_CAP, 1, 200),
        "mu_min_pct": mu_lo,
        "mu_max_pct": mu_hi,
        "sigma_min_pct": sigma_lo,
        "sigma_max_pct": sigma_hi,
        "min_exec_gap": _env_int("YIBAN_MIN_EXEC_GAP", _DEFAULT_MIN_EXEC_GAP, 1, 60),
        "avg_attempt_sec": _env_int("YIBAN_AVG_ATTEMPT_SEC", _DEFAULT_AVG_ATTEMPT_SEC, 1, 300),
        "retry_min_interval": _env_int("YIBAN_RETRY_MIN_INTERVAL", _DEFAULT_RETRY_MIN_INTERVAL, 1, 600),
        "exec_gap_min": _env_int("YIBAN_EXEC_GAP_MIN", _DEFAULT_EXEC_GAP_MIN, 0, 300),
        "allow_time_pref": _env_int("YIBAN_ALLOW_TIME_PREF", _DEFAULT_ALLOW_TIME_PREF, 0, 1),
        "sign_start": start,
        "sign_end": end,
    }

# WAF 风控关键词（用于判断是否被拦截）
WAF_KEYWORDS = ["风险访问", "风控", "访问服务禁用", "WAF", "拦截"]


# ---------------------------------------------------------------------------
# 定位生成：多边形内随机点
# ---------------------------------------------------------------------------
def point_in_polygon(x, y, polygon):
    """射线法判断点是否在多边形内。"""
    n = len(polygon)
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi + 1e-12) + xi):
            inside = not inside
        j = i
    return inside


def generate_position_in_polygon(polygon_points):
    """在多边形内生成随机点（缩放质心算法）。"""
    if not polygon_points:
        return None

    min_lng = min(p[0] for p in polygon_points)
    max_lng = max(p[0] for p in polygon_points)
    min_lat = min(p[1] for p in polygon_points)
    max_lat = max(p[1] for p in polygon_points)

    center_lng = sum(p[0] for p in polygon_points) / len(polygon_points)
    center_lat = sum(p[1] for p in polygon_points) / len(polygon_points)

    scaled_points = [
        ((p[0] - center_lng) * 0.7 + center_lng, (p[1] - center_lat) * 0.7 + center_lat)
        for p in polygon_points
    ]

    for _ in range(5000):
        lng = center_lng + (max_lng - min_lng) * 0.2 * (random.random() - 0.5)
        lat = center_lat + (max_lat - min_lat) * 0.2 * (random.random() - 0.5)
        if point_in_polygon(lng, lat, scaled_points) and point_in_polygon(lng, lat, polygon_points):
            return (lng, lat)

    # 兜底：质心 + 小范围随机抖动。避免多账号/多次触发共用同一质心坐标
    # （固定坐标聚集会成为风控行为指纹），同时保持仍在签到范围内。
    jitter = min(max_lng - min_lng, max_lat - min_lat) * 0.01  # 范围边长的 1%，约几十米量级
    jitter = max(jitter, 1e-6)  # 极小多边形时防止抖动归零
    for _ in range(50):
        fallback = (center_lng + random.uniform(-jitter, jitter),
                    center_lat + random.uniform(-jitter, jitter))
        if point_in_polygon(fallback[0], fallback[1], polygon_points):
            return fallback
    return (center_lng, center_lat)


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def is_waf_blocked(response_text):
    """判断响应是否为 WAF 风控拦截。

     WAF 拦截页通常很短（< 2000 字符），而正常页面（如 OAuth 授权页、
     服务协议等）内容较长且可能包含"风控""拦截"等正常法律文本。
     因此仅在响应内容较短时才检测 WAF 关键词，避免误报。

     注意：易班 WAF 返回 JSON 格式时，中文会被 Unicode 转义
    （如 \\u98ce\\u9669 = "风险"），需先解码再匹配关键词。
    """
    if len(response_text) > 2000:
        return False
    # 解码 \uXXXX 形式的 Unicode 转义序列后一并检测
    decoded = re.compile(r"\\u([0-9a-fA-F]{4})").sub(
        lambda m: chr(int(m.group(1), 16)), response_text
    )
    return any(keyword in response_text or keyword in decoded for keyword in WAF_KEYWORDS)


# ---------------------------------------------------------------------------
# 重试分级：判断一次失败是否值得重试、重试上限
# ---------------------------------------------------------------------------
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
    （2026-08-15 审查清理：原返回 (max_attempts, retryable) 的 retryable 恒为 True
    且无调用方使用——死返回值；TRANSIENT_FAIL_KEYWORDS 死常量一并删除）
    """
    for kw in RISK_FAIL_KEYWORDS:
        if kw in message:
            return RISK_MAX_ATTEMPTS
    return MAX_ATTEMPTS


def random_delay(max_seconds, label):
    """随机等待 0~max_seconds 秒（打散固定执行规律，max_seconds<=0 时不等待）。"""
    if max_seconds <= 0:
        return
    wait = random.uniform(0, max_seconds)
    logger.debug(f"{label}: 随机延迟 {int(wait)} 秒（上限 {max_seconds} 秒）")
    time.sleep(wait)


def _sanitize_text(text):
    """服务端可控内容进入错误消息/日志/通知前转义换行与回车，防止日志与通知注入。"""
    return str(text).replace("\r", "\\r").replace("\n", "\\n")


def _mask_phone(phone):
    """通知/对外输出脱敏：11 位手机号 → 138****8000（本地 sign.log 保留完整号供排查；
    对外 webhook 与 web 展示层不落完整号——规范审查 D2）。"""
    p = str(phone)
    return p[:3] + "****" + p[7:] if len(p) == 11 else p


# ---------------------------------------------------------------------------
# 账号配置加载
# ---------------------------------------------------------------------------
def _parse_account_dict(data):
    """将账号 JSON 对象解析为 Account，校验必填字段。

    password/phone_code 支持 AES-GCM 密文对象（web/tui 存储层加密落盘，
    accounts.json 内为密文，解密依赖同一密钥：环境变量 YIBAN_ACCOUNTS_KEY
    → .env 同键；密钥缺失/解密失败抛明确错误，绝不静默使用错误数据）。
    """
    phone = str(data.get("phone") or data.get("account") or "").strip()
    password = data.get("password") or data.get("pwd") or ""
    phone_code = data.get("phone_code") or ""
    if account_crypto.is_encrypted(password) or account_crypto.is_encrypted(phone_code):
        if not account_crypto.has_key():
            raise RuntimeError(
                "账号已加密但未配置 YIBAN_ACCOUNTS_KEY（请在 .env 中配置或恢复密钥备份）"
            )
        key = account_crypto.load_key()
        if account_crypto.is_encrypted(password):
            try:
                password = account_crypto.decrypt_password(password, key, phone)
            except ValueError as e:
                raise RuntimeError(f"账号 {phone} 密码解密失败: {e}") from e
        if account_crypto.is_encrypted(phone_code):
            try:
                phone_code = account_crypto.decrypt_password(phone_code, key, phone)
            except ValueError as e:
                raise RuntimeError(f"账号 {phone} 设备识别码解密失败: {e}") from e
    password = str(password).strip()
    if not phone or not password:
        # 异常消息只带 phone（登录名，非机密），绝不包含 password 明文
        missing = "phone" if not phone else "password"
        raise ValueError(f"账号配置缺少必填字段: {missing} 为空（phone={phone or '<空>'}）")
    return Account(
        phone=phone,
        password=password,
        phone_model=str(data.get("phone_model") or "").strip(),
        phone_code=str(phone_code).strip(),
        name=str(data.get("name") or "").strip(),
        user_paused=bool(data.get("user_paused", False)),  # 用户自暂停（调度 v2）
    )


def _load_accounts_from_file():
    """从数据库加载（yiban.db，SQLite；web 后台写入，单行事务防并发覆盖）。

    db 层返回已解密明文；此处只做审核状态过滤。
    """
    db.init_db()
    all_accounts = db.load_accounts()
    # 跳过待审核账号（status=pending：网页端普通用户提交、管理员尚未审核通过）、
    # 被拒绝账号（status=rejected：管理员审核不通过，不得签到）与待删除账号
    # （deleted：网页端软删除，保留期内可恢复，不参与签到）。
    # 注意：此处 "pending"/"rejected" 是账号审核态（web 侧 ACCOUNT_STATUS_*），
    # 与下方 STATUS_PENDING 等签到状态码是两套语义，勿混用（2026-08-16 审查轮注明）。
    # 旧数据可能没有 status 字段（等于通过审核），必须放行。
    active_raw = [
        item
        for item in all_accounts
        if item.get("status") != "pending"
        and item.get("status") != "rejected"
        and not item.get("deleted")
    ]
    accounts = [_parse_account_dict(item) for item in active_raw]
    logger.debug(f"已从数据库加载 {len(accounts)} 个账号")
    return accounts


def _load_accounts_from_json_env():
    """从 YIBAN_ACCOUNTS_JSON 环境变量加载（JSON 数组字符串，供 CI 使用）。"""
    raw = os.environ.get("YIBAN_ACCOUNTS_JSON", "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"YIBAN_ACCOUNTS_JSON 不是合法 JSON: {e}") from e
    if not isinstance(data, list):
        raise RuntimeError("YIBAN_ACCOUNTS_JSON 应为 JSON 数组")
    accounts = [_parse_account_dict(item) for item in data]
    logger.info(f"已从 YIBAN_ACCOUNTS_JSON 加载 {len(accounts)} 个账号")
    return accounts


def _load_accounts_from_legacy_env():
    """旧格式兼容：YIBAN_ACCOUNTS（phone:password#...）与 YIBAN_PHONE/YIBAN_PASSWORD。"""
    accounts = []
    accounts_str = os.environ.get("YIBAN_ACCOUNTS", "")
    for item in accounts_str.split("#"):
        item = item.strip()
        if not item:
            continue
        if ":" not in item:
            # 清洗后落日志：防畸形片段换行/回车注入（日志审查 P7）
            logger.error(f"账号配置格式错误（应为 phone:password）: {_sanitize_text(item)}")
            continue
        phone, pwd = item.split(":", 1)
        accounts.append(Account(phone.strip(), pwd.strip()))
    if not accounts:
        phone = os.environ.get("YIBAN_PHONE", "").strip()
        pwd = os.environ.get("YIBAN_PASSWORD", "").strip()
        if phone and pwd:
            accounts.append(Account(phone, pwd))
    return accounts


def _apply_global_device_info(accounts):
    """账号未配置设备信息时，回退到全局环境变量（兼容旧配置方式）。"""
    model = os.environ.get("YIBAN_PHONE_MODEL", "").strip()
    code = os.environ.get("YIBAN_PHONE_CODE", "").strip()
    if not (model and code):
        return accounts
    for acc in accounts:
        if not acc.has_device_info:
            acc.phone_model = model
            acc.phone_code = code
    return accounts


def load_accounts():
    """按优先级加载账号配置：文件 > JSON 环境变量 > 旧格式环境变量。"""
    for loader in (
        _load_accounts_from_file,
        _load_accounts_from_json_env,
        _load_accounts_from_legacy_env,
    ):
        accounts = loader()
        if accounts:
            return _apply_global_device_info(accounts)
    return []


def parse_env_int(name, default):
    """读取非负整数环境变量：缺失/非法回退默认值，负值归零。"""
    try:
        return max(0, int(os.environ.get(name, "").strip()))
    except (TypeError, ValueError):
        return default


def print_config_summary(accounts):
    """打印账号配置摘要（密码脱敏），不发任何网络请求。"""
    print("==== 账号配置检查 ====")
    for i, acc in enumerate(accounts, 1):
        if acc.has_device_info:
            # 只显示是否已配置（不打印识别码任何前缀，防摘要泄露设备指纹）
            device = f"设备: {acc.phone_model} / 识别码已配置"
        else:
            device = "设备: 未配置（如学校开启设备绑定，签到将失败）"
        print(f"  {i}. {acc.phone} | 密码: {'*' * 8} | {device}")
    print(f"共 {len(accounts)} 个账号，配置检查通过。")


# ---------------------------------------------------------------------------
# 易班登录
# ---------------------------------------------------------------------------
class YibanClient:
    """易班客户端：封装登录与签到流程。"""

    def __init__(self, account):
        self.account = account
        self.password = account.password.encode("UTF-8")
        # 登录方式：默认 KillYiBan 同款流程（真实 App 特征，实测绕过 e003）；
        # 旧流程（Auto-Test 继承的 iOS 伪造 UA）仅在 YIBAN_LEGACY_LOGIN=1 时启用（GitHub Actions 等场景备选）
        self.use_killyiban = os.environ.get("YIBAN_LEGACY_LOGIN", "") != "1"
        if self.use_killyiban:
            self.csrf = secrets.token_hex(16)  # SecureRandom 真随机
            logger.debug(
                f"[{account.phone}] 登录方式: 标准 App 特征（UA=Yiban/AppVersion=5.1.2/SecureRandom CSRF）"
            )
        else:
            self.csrf = md5(str(datetime.now()).encode("UTF-8")).hexdigest()
            logger.debug(f"[{account.phone}] 登录方式: 旧流程（iOS 伪造 UA，YIBAN_LEGACY_LOGIN=1）")
        self.session = requests.Session()
        self.session.headers = dict(KILLYIBAN_HEADERS if self.use_killyiban else HEADERS)
        # 代理配置：GitHub Actions 海外 IP 可能被易班 WAF 地域风控拦截
        proxy = os.environ.get("YIBAN_PROXY", "").strip()
        if proxy:
            self.session.proxies = {"http": proxy, "https": proxy}
            # 日志只记录 scheme://host:port，绝不落 userinfo（账号密码）明文
            proxy_parsed = urlsplit(proxy)
            if proxy_parsed.hostname:
                proxy_desc = f"{proxy_parsed.scheme}://{proxy_parsed.hostname}"
                if proxy_parsed.port:
                    proxy_desc += f":{proxy_parsed.port}"
            else:
                proxy_desc = "<无法解析>"
            logger.debug(f"[{account.phone}] 已启用代理: {proxy_desc}")
        else:
            logger.debug(
                f"[{account.phone}] 未配置代理，如遇 WAF 拦截可配置 YIBAN_PROXY"
            )
        # 设备信息：部分学校开启了"设备绑定"，签到时需校验设备型号和唯一识别码
        self.phone_model = account.phone_model
        self.phone_code = account.phone_code
        self.logged_in = False

    def _rsa_encrypt(self, cipher):
        """RSA-1024 + PKCS1_v1_5 加密密码，超长时给出明确报错（而非底层 ValueError 裸抛）。"""
        if len(self.password) > 117:
            raise ValueError(
                "密码过长: RSA-1024 公钥单次最多加密 117 字节（约 39 个中文字符），"
                "当前密码无法加密提交，请缩短密码或联系管理员处理"
            )
        return b64encode(cipher.encrypt(self.password))

    def login(self):
        """登录易班，成功返回 True，失败抛出异常。"""
        self.session.cookies = cookiejar_from_dict({"csrf_token": self.csrf})
        self.session.headers.update(
            Referer="https://c.uyiban.com/",
            Origin="https://c.uyiban.com",
        )

        # 1. 获取跳转 URL
        resp = self.session.get(
            "https://api.uyiban.com/base/c/auth/yiban",
            params={"CSRF": self.csrf},
            allow_redirects=False,
            timeout=15,
        )
        if is_waf_blocked(resp.text):
            raise RuntimeError("请求被 WAF 风控拦截，请配置 YIBAN_PROXY 代理后重试")
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"获取登录入口失败: {_sanitize_text(data.get('msg'))}")

        # 2. 跳转到 OAuth 页面，解析 RSA 公钥与 page_use
        resp = self.session.get(data["data"]["Data"], allow_redirects=True, timeout=15)

        # 检查是否被 WAF 拦截
        if is_waf_blocked(resp.text):
            raise RuntimeError("请求被 WAF 风控拦截，请配置 YIBAN_PROXY 代理后重试")

        page_use_match = re.compile(r"page_use ?= ?[\'|\"]([a-zA-Z0-9-_]+)[\'|\"]").findall(
            resp.text
        )
        key_match = re.compile(r'id="key"\s+value="([^"]+)"').findall(resp.text)
        if not page_use_match or not key_match:
            # 只落响应摘要（前 300 字符），避免整页 HTML/敏感内容进日志
            body_preview = resp.text[:300].replace("\n", "\\n")
            logger.error(f"[{self.account.phone}] OAuth 页解析失败诊断:")
            logger.error(f"  最终 URL: {resp.url}")
            logger.error(f"  状态码: {resp.status_code}")
            logger.error(f"  响应长度: {len(resp.text)}")
            logger.error(f"  响应前300字符: {body_preview}")
            logger.error(f"  page_use 命中: {len(page_use_match)}, key 命中: {len(key_match)}")
            if is_waf_blocked(resp.text):
                logger.error("  检测到 WAF 风控拦截特征，通常是 GitHub Actions 海外 IP 被易班风控")
            logger.error(
                "  若响应为 WAF 挑战页/拦截页，通常是 GitHub Actions 海外 IP 被易班风控，"
                "请配置 YIBAN_PROXY 代理后重试。"
            )
            raise RuntimeError("登录页面解析失败（page_use / RSA key 未找到），详见上方诊断日志")

        cipher = PKCS1_v1_5.new(RSA.importKey(key_match[0]))
        self.session.headers.update(
            Referer=resp.url,
            Origin="https://oauth.yiban.cn",
        )

        # 3. 提交账号密码
        resp = self.session.post(
            "https://oauth.yiban.cn/code/usersure",
            params={"ajax_sign": page_use_match[0]},
            data=urlencode(
                {
                    "oauth_uname": self.account,
                    "oauth_upwd": self._rsa_encrypt(cipher),
                    "client_id": "95626fa3080300ea",
                    "redirect_uri": "https://f.yiban.cn/iapp7463",
                    "state": "",
                    "scope": "1,2,3,4,",
                    "display": "html",
                }
            ),
            allow_redirects=False,
            timeout=15,
        )
        # 先检测 WAF 拦截再做 JSON 解析（拦截页是 HTML，直接 json() 会抛解析异常）
        if is_waf_blocked(resp.text):
            raise RuntimeError("请求被 WAF 风控拦截，请配置 YIBAN_PROXY 代理后重试")
        result = resp.json()

        if "reUrl" not in result:
            body_preview = resp.text[:300].replace("\n", "\\n")
            logger.error(f"[{self.account.phone}] usersure 响应无 reUrl 字段，诊断:")
            logger.error(f"  状态码: {resp.status_code}")
            logger.error(f"  响应前300字符: {body_preview}")
            raise RuntimeError(f"登录响应异常（无 reUrl）: {_sanitize_text(result)}")
        if "error" in result.get("reUrl", ""):
            raise RuntimeError(f"登录失败（账号或密码错误）: {self.account.phone}")

        # 4. 跳转回 f.yiban.cn，可能遇到 ydclearance 反爬
        self.session.headers.update(Referer="https://oauth.yiban.cn")
        resp = self.session.get(result["reUrl"], allow_redirects=False, timeout=15)

        if self._is_ydclearance_challenge(resp):
            # 纯 Python 解析挑战（不执行任何远程 JS），得出 cookie 与跳转路径
            clearance = self._solve_ydclearance(resp.text)
            cookies = dict_from_cookiejar(self.session.cookies)
            cookies["https_ydclearance"] = clearance[0]
            self.session.cookies = cookiejar_from_dict(cookies)
            self.session.headers.update(Referer=resp.url, Origin="https://f.yiban.cn")
            target = clearance[1]
            if not target.startswith("http"):
                target = "https://f.yiban.cn" + target
            resp = self.session.get(target, allow_redirects=False, timeout=15)
            self.session.headers.update(Referer=resp.url)
        else:
            self.session.headers.update(Referer=resp.url, Origin="https://f.yiban.cn")

        # 5. 获取 verify_request
        location = resp.headers.get("Location", "")
        if not location:
            raise RuntimeError(
                f"获取 verify_request 失败: 上一步响应缺少 Location 头"
                f"（状态码 {resp.status_code}，响应长度 {len(resp.text)}）"
            )
        resp = self.session.get(location, allow_redirects=False, timeout=15)
        verify_match = re.compile(r"verify_request=([^&]+)&?").findall(
            resp.headers.get("Location", "")
        )
        if not verify_match:
            raise RuntimeError(
                f"获取 verify_request 失败: 重定向响应缺少 verify_request 参数"
                f"（状态码 {resp.status_code}，响应长度 {len(resp.text)}）"
            )
        verify_code = verify_match[0]

        # 6. 完成登录
        self.session.headers.update(
            Referer="https://c.uyiban.com/",
            Origin="https://c.uyiban.com",
        )
        self.session.get(
            "https://api.uyiban.com/base/c/auth/yiban",
            params={"verifyRequest": verify_code, "CSRF": self.csrf},
            cookies={},
            allow_redirects=False,
            timeout=15,
        )

        cookies = dict_from_cookiejar(self.session.cookies)
        if "csrf_token" not in cookies:
            raise RuntimeError("登录失败：未获取到 csrf_token")

        self.logged_in = True
        logger.info(f"[{self.account.phone}] 登录成功")

    # ---- KillYiBan 同款登录（默认登录方式，e003 修复）----
    def login_killyiban(self):
        """完全复刻反编译 KillYiBan (p101w2/b.java) 的登录流程。

        差异点（vs 原 login）：
        - 入口直接打 oauth.yiban.cn/code/html（不先打 api.uyiban.com）
        - usersure 请求不带 Referer/Origin（原 App 传空 headers）
        - scope 传空、display 传 "authorize"（原 App 实值）
        - 成功标志判断 code == "s200"（原 App 判断方式）
        - CSRF 为 SecureRandom 真随机
        - 页面解析用 jsoup 等价正则（key 去掉 BEGIN/END 后按 X509 解码）
        """
        phone = self.account.phone
        # 设置 csrf_token cookie（服务器用其校验 CSRF 参数，缺失会报 CSRF invalid）
        self.session.cookies = cookiejar_from_dict({"csrf_token": self.csrf})
        # session 头已是 KILLYIBAN_HEADERS，此处无需再改

        # 1. 打开 OAuth 登录页（client_id/redirect_uri 参数，无 CSRF）
        #    注意：不跟随重定向，直接取登录页 HTML（diag 实测 allow_redirects=False 返回 200 登录页）
        resp = self.session.get(
            "https://oauth.yiban.cn/code/html",
            params={"client_id": "95626fa3080300ea", "redirect_uri": "https://f.yiban.cn/iapp7463"},
            allow_redirects=False,
            timeout=15,
        )
        # 若直接返回 iapp7463 说明已登录（正常流程是停留在登录页）
        if "iapp7463" in (resp.headers.get("Location", "")):
            logger.info(f"[{phone}] 登录: 已登录状态（无需提交）")
            self.logged_in = True
            return

        # 2. 解析 RSA 公钥与 page_use（jsoup input#key 等价正则）
        key_match = re.compile(r'<input[^>]*id="key"[^>]*value="([^"]+)"').findall(resp.text)
        page_use_match = re.compile(r"var page_use = '([^']+)'").findall(resp.text)
        if not key_match or not page_use_match:
            logger.error(
                f"[{phone}] 登录 OAuth 页解析失败（key={len(key_match)}, page_use={len(page_use_match)}）"
            )
            raise RuntimeError("登录: OAuth 页解析失败")
        # key 去掉 PEM 头尾后按 X509 解码
        key_b64 = re.compile(r"\s+").sub(
            "",
            key_match[0]
            .replace("-----BEGIN PUBLIC KEY-----", "")
            .replace("-----END PUBLIC KEY-----", ""),
        )
        cipher = PKCS1_v1_5.new(RSA.import_key(b64decode(key_b64)))

        # 3. 提交账号密码（实测：usersure 必须不带 Origin/Referer 才返回 s200；
        #    带 Origin → e001"无效的应用端编号"；scope 空 + display=authorize 与 App 一致）
        resp = self.session.post(
            "https://oauth.yiban.cn/code/usersure",
            params={"ajax_sign": page_use_match[0]},
            headers={
                "User-Agent": "Yiban",
                "AppVersion": "5.1.2",
                "Origin": None,
                "Referer": None,
                "X-Requested-With": None,
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            },
            data=urlencode(
                {
                    "oauth_uname": phone,
                    "oauth_upwd": self._rsa_encrypt(cipher),
                    "client_id": "95626fa3080300ea",
                    "redirect_uri": "https://f.yiban.cn/iapp7463",
                    "state": "",
                    "scope": "",
                    "display": "authorize",
                }
            ),
            allow_redirects=False,
            timeout=15,
        )
        # 先检测 WAF 拦截再做 JSON 解析（拦截页是 HTML，直接 json() 会抛解析异常）
        if is_waf_blocked(resp.text):
            raise RuntimeError("请求被 WAF 风控拦截，请配置 YIBAN_PROXY 代理后重试")
        result = resp.json()
        # App 用 code == "s200" 判断成功
        if result.get("code") != "s200":
            raise RuntimeError(f"登录失败: {_sanitize_text(result.get('msgCN', result))}")

        # 4. 打开 iframe/index 获取 Location → verify_request（默认三个头）
        resp = self.session.get(
            "https://f.yiban.cn/iframe/index",
            params={"act": "iapp7463"},
            allow_redirects=False,
            timeout=15,
        )
        location = resp.headers.get("Location", "")
        verify_match = re.compile(r"verify_request=(.*?)&").findall(location)
        if not verify_match:
            # Location 的 query 中含 verify_request 令牌，错误消息只留 host/path
            loc_desc = ""
            try:
                parts = urlsplit(location)
                if parts.scheme:
                    loc_desc = f"{parts.scheme}://{parts.netloc}{parts.path}"
                else:
                    loc_desc = parts.path
            except ValueError:
                loc_desc = "<无法解析>"
            raise RuntimeError(f"无法提取 verify_request（Location={loc_desc}）")

        # 5. 完成认证（默认三个头 + 跟随重定向，最终返回 JSON）
        resp = self.session.get(
            "https://api.uyiban.com/base/c/auth/yiban",
            params={"verifyRequest": verify_match[0], "CSRF": self.csrf},
            allow_redirects=True,
            timeout=15,
        )
        if is_waf_blocked(resp.text):
            raise RuntimeError("请求被 WAF 风控拦截，请配置 YIBAN_PROXY 代理后重试")
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"最终认证失败: {_sanitize_text(data.get('msg'))}")
        self.logged_in = True
        logger.info(f"[{phone}] 登录成功")

    def _is_ydclearance_challenge(self, resp):
        """判断响应是否触发 ydclearance 反爬挑战。

        特征判定（不依赖响应长度）：
        - Set-Cookie 已下发 https_ydclearance（说明已过挑战）；
        - 或响应包含挑战 JS 特征（window.onload=setTimeout + eval("qo=eval;qo(po);")）。
        """
        if "https_ydclearance" in resp.headers.get("Set-Cookie", ""):
            return True
        return "window.onload=setTimeout" in resp.text and 'eval("qo=eval;qo(po);")' in resp.text

    def _solve_ydclearance(self, text):
        """纯 Python 解析易盾 WAF（https_ydclearance）挑战，不执行任何远程 JS。

        挑战模板固定（易盾 WAF v1，f.yiban.cn 与 kuaidaili/89ip 同款）：
        `oo` 十六进制字节数组 + 三步固定变换（取反+旋转-常量、逆向差分、
        加常量+旋转），最后跳过 `qo % K` 的下标、逐字节异或挑战参数拼出
        `po` 字符串（含 cookie 赋值与跳转路径）。各步数值常量随挑战变化，
        用正则从 JS 中提取后在 Python 中复刻运算；任何一步提取失败都抛
        明确错误，绝不 eval 远程代码。
        """
        fn_m = re.compile(r"(function ([a-z]{2,})\(.+) ?</script>").findall(text)
        if not fn_m:
            raise RuntimeError("ydclearance 挑战解析失败: 未找到挑战函数")
        js_code = fn_m[0][0]
        if 'eval("qo=eval;qo(po);")' not in js_code:
            raise RuntimeError("ydclearance 挑战解析失败: 模板特征缺失（eval qo/po 未找到）")

        # 挑战参数：window.onload=setTimeout("<fn>(<arg>)", 200)
        arg_m = re.compile(r'window\.onload=setTimeout\("' + fn_m[0][1] + r"\(([0-9]+).+").findall(
            text
        )
        if not arg_m:
            raise RuntimeError("ydclearance 挑战解析失败: 未找到挑战参数")
        arg = int(arg_m[0])

        # oo 字节数组
        arr_m = re.compile(r"oo = (\[[0-9a-fA-Fx,\s]+?\])").findall(js_code)
        if not arr_m:
            raise RuntimeError("ydclearance 挑战解析失败: 未找到 oo 数组")
        oo = [int(x, 16) for x in re.findall(r"0x([0-9a-fA-F]+)", arr_m[0])]
        if len(oo) < 4:
            raise RuntimeError("ydclearance 挑战解析失败: oo 数组过短")

        # 变换 A（尾部到头部）：取反 → 旋转 → 减常量
        ta = re.search(
            r'"qo=(\d+); do\{oo\[qo\]=\(-oo\[qo\]\)&0xff;(.+?)\} while\(--qo>=2\);',
            js_code,
        )
        if not ta:
            raise RuntimeError("ydclearance 挑战解析失败: 变换 A 未找到")
        n_a = int(ta.group(1))
        ta_num = re.search(r">>(\d+)", ta.group(2))
        ta_shift_l = re.search(r"<<(\d+)", ta.group(2))
        ta_sub = re.search(r"-(\d+)\)&0xff", ta.group(2))
        if not (ta_num and ta_shift_l and ta_sub):
            raise RuntimeError("ydclearance 挑战解析失败: 变换 A 常量未找到")
        for i in range(n_a, 1, -1):
            oo[i] = (-oo[i]) & 0xFF
            oo[i] = (
                ((oo[i] >> int(ta_num.group(1))) | ((oo[i] << int(ta_shift_l.group(1))) & 0xFF))
                - int(ta_sub.group(1))
            ) & 0xFF

        # 变换 B（尾部到头部）：逆向差分
        tb = re.search(r"qo = (\d+); do \{ oo\[qo\] = \(oo\[qo\] - oo\[qo - 1\]\)", js_code)
        if not tb:
            raise RuntimeError("ydclearance 挑战解析失败: 变换 B 未找到")
        for i in range(int(tb.group(1)), 2, -1):
            oo[i] = (oo[i] - oo[i - 1]) & 0xFF

        # 变换 C（头部到尾部）：加常量 → 旋转
        tc = re.search(r"if \(qo > (\d+)\) break; oo\[qo\] = (.+?); qo\+\+", js_code)
        if not tc:
            raise RuntimeError("ydclearance 挑战解析失败: 变换 C 未找到")
        tc_num = re.search(r"\+ (\d+)\) & 0xff\) \+ (\d+)\) & 0xff\) << (\d+)", tc.group(2))
        tc_shift_r = re.search(r">> (\d+)\)", tc.group(2))
        if not (tc_num and tc_shift_r):
            raise RuntimeError("ydclearance 挑战解析失败: 变换 C 常量未找到")
        n_c = int(tc.group(1))
        tc_add1, tc_add2, tc_shift_l = (int(x) for x in tc_num.groups())
        for i in range(1, n_c + 1):
            v = (oo[i] + tc_add1) & 0xFF
            v = (v + tc_add2) & 0xFF
            oo[i] = ((v << tc_shift_l) & 0xFF) | (v >> int(tc_shift_r.group(1)))

        # 拼 po：跳过 qo % K 的下标，逐字节异或挑战参数
        tk = re.search(r"if \(qo % (\d+)\) po \+= String\.fromCharCode\(oo\[qo\] \^ [A-Za-z_]+\)", js_code)
        if not tk:
            raise RuntimeError("ydclearance 挑战解析失败: po 拼接逻辑未找到")
        k = int(tk.group(1))
        po = "".join(chr(oo[i] ^ arg) for i in range(1, n_c + 1) if i % k)

        cookie_m = re.compile(r"https?_ydclearance=([0-9a-zA-Z-_]+);?").findall(po)
        path_m = re.compile(r'window\.document\.location="(.+)"').findall(po)
        if not cookie_m or not path_m:
            raise RuntimeError("ydclearance 挑战解析失败: 解码结果中未提取到 cookie/跳转路径")
        return cookie_m[0], path_m[0]

    # ---- 签到 -------------------------------------------------------------
    def signin(self):
        """执行签到，返回 (success: bool, message: str, skip: bool)。

        skip=True 表示当前不在签到时间窗口内，不需要重试。
        """
        if not self.logged_in:
            # 与 attempt_signin 保持一致：按配置选择登录流程（防止直接调 signin() 时走错）
            if self.use_killyiban:
                self.login_killyiban()
            else:
                self.login()

        # 1. 获取签到位置范围
        if not self.use_killyiban:
            self.session.headers.update(
                Origin="https://app.uyiban.com", Referer="https://app.uyiban.com/"
            )
        resp = self.session.get(
            "https://api.uyiban.com/nightAttendance/student/index/signPosition",
            params={"CSRF": self.csrf},
            allow_redirects=False,
            timeout=15,
        )
        if is_waf_blocked(resp.text):
            return False, "请求被 WAF 风控拦截，请配置 YIBAN_PROXY 代理后重试", False, STATUS_FAILED
        data = resp.json()
        if data.get("code") != 0:
            return False, f"获取签到任务失败: {_sanitize_text(data.get('msg'))}", False, STATUS_FAILED

        data_obj = data["data"]
        msg = data_obj.get("Msg", "")
        if "已签到" in msg:
            return True, "今日已签到（无需重复签到）", False, STATUS_ALREADY
        if "今日无需签到" in msg:
            return True, "今日无需签到（非签到日）", False, STATUS_NO_TASK

        position_list = data_obj.get("Position", [])
        if not position_list:
            return False, "未找到签到位置数据", False, STATUS_FAILED
        position = position_list[0]
        range_obj = data_obj.get("Range", {})

        # 2. 校验签到时间
        now_ts = int(datetime.now().timestamp())
        start_ts = int(range_obj.get("StartTime", 0))
        end_ts = int(range_obj.get("EndTime", 0))
        if not start_ts or not end_ts:
            # 签到时间窗口缺失（Range 为空），视为 skip，不直接提交
            return False, "签到时间窗口缺失（无 Range），已跳过", True, STATUS_SKIPPED_NORANGE
        if not (start_ts <= now_ts <= end_ts):
            # 不在签到时间窗口内，标记为 skip（不需要重试）
            return (
                False,
                f"未在签到时间内（{datetime.fromtimestamp(start_ts)} ~ {datetime.fromtimestamp(end_ts)}）",
                True,
                STATUS_SKIPPED_WINDOW,
            )

        # 3. 解析多边形点
        points_raw = position.get("Points", [])
        polygon = []
        for p in points_raw:
            parts = p.split(",")
            if len(parts) >= 2:
                polygon.append((float(parts[0]), float(parts[1])))

        if not polygon:
            return False, "签到范围点解析失败", False, STATUS_FAILED

        # 4. 在多边形内生成随机点
        lng, lat = generate_position_in_polygon(polygon)
        logger.info(
            f"[{self.account.phone}] 生成定位: ({lng},{lat}) 地址: {position.get('Address', '')}"
        )

        # 5. 构建签到数据并提交
        sign_info = {
            "Reason": "",
            "AttachmentFileName": "",
            "LngLat": f"{lng},{lat}",
            "Address": position.get("Address", ""),
        }
        if not self.phone_model or not self.phone_code:
            logger.warning(
                f"[{self.account.phone}] 未配置设备信息（YIBAN_PHONE_MODEL/YIBAN_PHONE_CODE），"
                "如学校开启了设备绑定，签到将失败"
            )
        resp = self.session.post(
            "https://api.uyiban.com/nightAttendance/student/index/signIn",
            params={"CSRF": self.csrf},
            data={
                "Code": self.phone_code,
                "PhoneModel": self.phone_model,
                "SignInfo": json.dumps(sign_info, ensure_ascii=False),
                # KillYiBan 用 MINI_VERSION="1"，原脚本用 "1.0"
                "OutState": "1" if self.use_killyiban else "1.0",
            },
            allow_redirects=False,
            timeout=15,
        )
        if is_waf_blocked(resp.text):
            return False, "请求被 WAF 风控拦截，请配置 YIBAN_PROXY 代理后重试", False, STATUS_FAILED
        result = resp.json()
        if result.get("code") == 0 and result.get("data"):
            return True, "签到成功", False, STATUS_SUCCESS
        err_msg = _sanitize_text(result.get("msg", "未知错误"))
        if "授权设备" in err_msg:
            err_msg += "（请配置 YIBAN_PHONE_MODEL 和 YIBAN_PHONE_CODE 环境变量）"
        return False, f"签到失败: {err_msg}", False, STATUS_FAILED


# ---------------------------------------------------------------------------
# 消息通知
# ---------------------------------------------------------------------------
def send_notification(title, content, url):
    """通过 Server 酱 / Bark / 企业微信等 webhook 发送通知。"""
    if not url:
        return
    try:
        if url.startswith("http"):
            requests.post(url, json={"title": title, "content": content}, timeout=10)
            logger.info("通知发送成功: %s", title)
    except Exception as e:
        # exc_info 可追溯告警通道不可达（日志审查 P4：原无渠道/标题标识）
        logger.warning(f"通知发送失败（{title}）: {e}", exc_info=True)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def attempt_signin(account):
    """单次签到尝试（登录 + 签到），不重试。

    返回 (success, message, skip)：
    - success: 是否成功（含"已签到""非签到日"）
    - message: 结果说明
    - skip: True 表示窗口外等无需重试的情况

    2026-08-15 审查清理：原 notify_url 参数从未在函数体内使用
    （通知统一由 run_queue_retry 最终放弃时发送），已删除。
    """
    phone = account.phone
    try:
        client = YibanClient(account)
        if client.use_killyiban:
            client.login_killyiban()
        else:
            client.login()
        return client.signin()
    except Exception as e:
        # exc_info 落堆栈（INFO 级别也可见）：登录/签到异常可定位具体失败步骤
        # （日志审查 P1：原仅 debug 记录，默认 INFO 下堆栈丢失）
        logger.error(f"[{phone}] ❌ 尝试失败: {e}", exc_info=True)
        # 逐次失败不通知（避免通知风暴），仅最终放弃时由 run_queue_retry 通知一次
        return False, str(e), False, STATUS_FAILED


def _write_sign_state(phone, status, message, scheduled=None, dur=None):
    """写按日结构化状态文件（web/TUI 状态显示的事实源，原子替换防半截文件）。

    文件：{YIBAN_STATE_DIR}/sign-state-YYYY-MM-DD.json
    结构：{phone: {status, message, time, task}}；task 预留多时段/多星期签到扩展。
    scheduled：今日计划签到时间（HH:MM:SS，自动错峰分配后写入，执行后保留）。
    dur：单次签到尝试耗时秒数（P6，2026-08-16：慢响应可据此判断网络/接口问题）。
    状态目录不可写时丢弃，不影响签到执行。
    """
    state_dir = os.environ.get("YIBAN_STATE_DIR", "/var/log/yiban")
    try:
        os.makedirs(state_dir, exist_ok=True)
        now = datetime.now()
        date = now.strftime("%Y-%m-%d")
        path = os.path.join(state_dir, f"sign-state-{date}.json")
        data = {}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        # 计划时间是当日事实：后续写入（执行结果/重试）未显式传 scheduled 时保留既有值
        if not scheduled and isinstance(data.get(phone), dict):
            scheduled = data[phone].get("scheduled")
        entry = {
            "status": status,
            "message": message,
            "time": now.strftime("%H:%M:%S"),
            "task": "default",
        }
        if dur is not None:
            entry["dur"] = round(float(dur), 2)  # 单次尝试耗时秒数（P6）
        if scheduled:
            entry["scheduled"] = scheduled
        data[phone] = entry
        # 唯一临时名：防跨进程（cron + 手动 --only 并发）固定 .tmp 名互相覆盖（对抗性审查发现）
        tmp = f"{path}.tmp{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError as e:
        # 状态目录不可写时丢弃但不静默：debug 留痕（日志审查 D6，不影响签到执行）
        logger.debug("写入状态文件失败（%s）: %s", path, e)


# ---------------------------------------------------------------------------
# 账密熔断器：连续凭据失败 → 暂停签到（零请求），周期性半开试探自动恢复
# ---------------------------------------------------------------------------
def _cred_state_path():
    state_dir = os.environ.get("YIBAN_STATE_DIR", "/var/log/yiban")
    return os.path.join(state_dir, "cred-state.json")


def _load_cred_state():
    """读账密状态文件：{phone: {fail_days, last_fail, paused_since, probe_date}}。"""
    try:
        # utf-8-sig：兼容 Windows 记事本/手工编辑可能写入的 UTF-8 BOM（BOM 会让 json.load 抛错）
        with open(_cred_state_path(), encoding="utf-8-sig") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_cred_state(data):
    """原子写账密状态文件；无任何暂停记录时删除文件（保持"无暂停 = 文件不存在"语义）。

    2026-08-16 修复：原实现无条件写 `{}`，与设计语义不符（TODO P5b）；目录不可写时丢弃，不影响签到执行。
    """
    path = _cred_state_path()
    try:
        if not data:
            if os.path.exists(path):
                os.remove(path)
            return
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError as e:
        logger.debug("写入账密状态失败（%s）: %s", path, e)


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


def _anchor_z(phone):
    """账号锚点分位（顺序×正态）：hash(phone) 派生标准正态值，零持久化、每天稳定。"""
    return random.Random(str(phone)).gauss(0, 1)


def _sigma_eff(sigma, n, span_minutes):
    """人数自适应 + 封顶：σ×(1+log2(n/20))，上限 有效窗口/3（防端点堆积）。"""
    if n > 20:
        sigma = sigma * (1 + math.log2(n / 20))
    return min(sigma, span_minutes / 3)


def _schedule_blocks(cfg):
    """按时钟 5 分钟对齐切块（首尾块各 4 分钟），返回 (blocks, eff_lo, eff_hi)。

    blocks: [(lo_min, hi_min), ...]；eff_lo/eff_hi：有效窗口分钟边界（相对当天 0:00）。
    有效窗口为空（缓冲 >= 窗口宽度）时回退默认窗口，保证调用方永不拿到空块列表。
    """
    start_min = cfg["sign_start"][0] * 60 + cfg["sign_start"][1]
    end_min = cfg["sign_end"][0] * 60 + cfg["sign_end"][1]
    edge = max(0, cfg["edge_sec"] // 60)
    eff_lo = start_min + edge
    eff_hi = end_min - edge
    if eff_hi <= eff_lo:
        logger.warning(
            "有效签到窗口为空（窗口 %s~%s、缓冲 %ss），回退默认窗口 06:30~07:50",
            cfg["sign_start"], cfg["sign_end"], cfg["edge_sec"],
        )
        start_min = _DEFAULT_SIGN_START[0] * 60 + _DEFAULT_SIGN_START[1]
        end_min = _DEFAULT_SIGN_END[0] * 60 + _DEFAULT_SIGN_END[1]
        edge = max(0, _DEFAULT_EDGE_SEC // 60)
        eff_lo = start_min + edge
        eff_hi = end_min - edge
    blocks = []
    b = start_min
    while b < end_min:
        lo = max(b, eff_lo)
        hi = min(b + 5, eff_hi)
        if hi > lo:
            blocks.append((lo, hi))
        b += 5
    return blocks, eff_lo, eff_hi


def _minute_to_dt(base_date, minute):
    """当天分钟数 → datetime（base_date 提供日期）。"""
    return base_date + timedelta(minutes=minute)


def _nearest_available(bi, filled, blocks, cap):
    """双向就近找未满块（自选溢出顺延用；同距离优先更早的块）。无可用返回 None。"""
    n = len(blocks)
    for d in range(n):
        for idx in (bi - d, bi + d):
            if 0 <= idx < n and filled[idx] < cap:
                return idx
    return None


def _next_available(bi, filled, blocks, cap):
    """从 bi 向后（环回）找第一个未满块。"""
    n = len(blocks)
    for step in range(n):
        idx = (bi + step) % n
        if filled[idx] < cap:
            return idx
    return bi  # 全满（理论不会发生：cap 已按 n 放大）


def _slot_to_bi(cfg):
    """自选片分钟偏移（相对窗口起点）→ 块索引。

    口径与 web/app.py `_pref_slots` 完全一致：块起点 = 窗口起点 + 5k 对齐，
    key = 块起点 - 窗口起点（窗口起点非 5 分钟倍数时同样成立）。
    """
    start_min = cfg["sign_start"][0] * 60 + cfg["sign_start"][1]
    end_min = cfg["sign_end"][0] * 60 + cfg["sign_end"][1]
    edge = max(0, cfg["edge_sec"] // 60)
    m = {}
    bi = 0
    for b in range(start_min, end_min, 5):
        lo = max(b, start_min + edge)
        hi = min(b + 5, end_min - edge)
        if hi > lo:
            m[b - start_min] = bi
            bi += 1
    return m


def build_schedule(accounts, order=None, dist=None, now=None, rng=None, prefs=None):
    """调度 v2（S1 demo）：统一填充框架。

    排序维度 × 分布维度（2×2）：
    - 顺序×均匀：线性填块（第 i 账号 → 第 i/K 块，先到先签）
    - 随机×均匀：打乱后循环填块（每块人数均衡、铺满窗口）
    - 顺序×正态：z_i 锚点（hash(phone)）稳定作息 + 钟形
    - 随机×正态：每天重抽分位（重排 + 钟形，防风控最强）
    自选优先（S2）：prefs 传入 {phone: {slot_min, updated_at}} 时，自选账号固定所选片
    （片内等分），片满先到先得（updated_at 早者留），溢出双向就近顺延；未选走四组合。
    安全底座：首尾缓冲有效窗口、块容量顺延、块内等分 + 抖动、
    σ_eff 封顶、反射兜底、n≤小人数免分块、超容量压缩模式。

    参数（None → 读环境变量，见 _schedule_config）：
    order: "sequence"|"random"；dist: "uniform"|"normal"
    now: 注入当天日期（默认 datetime.now()）；rng: 注入随机源（测试固定 seed）
    prefs: 自选 {phone: {"slot_min": int, "updated_at": str}}；None → 总开关开时读 db
    返回 {phone: datetime}。
    """
    cfg = _schedule_config()
    order = (order or cfg["order"]).strip().lower()
    dist = (dist or cfg["dist"]).strip().lower()
    if order not in ("sequence", "random"):
        order = "sequence"
    if dist not in ("uniform", "normal"):
        dist = "uniform"
    rng = rng or random.Random()
    now = now or datetime.now()

    # 用户自暂停账号不参与调度（零占位；执行侧 run_queue_retry 也会跳过）
    accounts = [a for a in accounts if not getattr(a, "user_paused", False)]
    n = len(accounts)
    if n == 0:
        return {}
    base = now.replace(hour=0, minute=0, second=0, microsecond=0)
    blocks, eff_lo, eff_hi = _schedule_blocks(cfg)
    span = eff_hi - eff_lo
    if not blocks:  # 纵深防御：任何原因导致无块（理论上已被 _schedule_blocks 回退兜底）
        logger.error("有效签到窗口为空，无法生成调度计划（请检查签到窗口与缓冲配置）")
        return {}

    # 小人数复用同一分块机制（统一逻辑，后续加人行为连续，无需特判）：
    # 顺序×均匀 = 线性填块（n=2 → 两人同块等分）；随机×均匀 = 循环填块；正态 = 采样落块
    # 容量：块数 × K；超出 → 压缩模式（K 放大到能容纳所有人，间隔下限告警）
    cap = len(blocks) * cfg["block_cap"]
    k = cfg["block_cap"] if n <= cap else math.ceil(n / len(blocks))
    if n > cap:
        logger.warning(
            "压缩模式: %d 个账号超出块容量 %d，块容量放大至 %d（间隔 ≈ %.1fs）",
            n, cap, k, (span * 60) / n,
        )

    # 自选优先占块（S2）：先到先得 + 溢出双向就近顺延
    chosen = {}  # phone -> bi
    if prefs is None:
        prefs = {}
        if cfg["allow_time_pref"]:
            try:
                prefs = db.get_time_prefs()
            except Exception as e:
                logger.warning("读取自选时间失败（忽略，走自动分配）: %s", e)
    if prefs:
        # 对抗性审查补：只保留当前账号集合内的 pref（换号/删号后的孤儿不占容量）
        valid_phones = {a.phone for a in accounts}
        slot_to_bi = _slot_to_bi(cfg)
        by_slot = {}
        for phone, p in prefs.items():
            if phone not in valid_phones:
                continue
            try:
                slot = int(p.get("slot_min", -1))
            except (TypeError, ValueError):
                continue
            if not (0 <= slot < span):  # 片落窗外 → 回退自动分配
                continue
            by_slot.setdefault(slot, []).append((str(p.get("updated_at", "")), phone))
        filled = [0] * len(blocks)
        overflow = []
        for slot, items in sorted(by_slot.items()):
            items.sort()  # updated_at 升序 → 先到先得
            bi = slot_to_bi.get(slot)
            if bi is None:
                for _u, phone in items:
                    overflow.append((slot, phone))
                continue
            for _u, phone in items:
                if filled[bi] < k:
                    chosen[phone] = bi
                    filled[bi] += 1
                else:
                    overflow.append((slot, phone))
        for slot, phone in overflow:  # 溢出：双向就近顺延（±1 块 → ±2 块 → …）
            base_bi = slot_to_bi.get(slot)
            if base_bi is None:
                continue
            bi = _nearest_available(base_bi, filled, blocks, k)
            if bi is not None:
                chosen[phone] = bi
                filled[bi] += 1
        if overflow:
            logger.info("自选顺延: %d 个账号所选时段已满，已就近调整到附近时段", len(overflow))

    # 排序维度：决定账号→位置的映射是否每天重排（自选账号不参与）
    ordered = [a for a in accounts if a.phone not in chosen]
    if order == "random":
        rng.shuffle(ordered)

    # 分布维度：uniform = 确定性位置；normal = 钟形采样位置
    mu = sigma = None
    if dist == "normal" and ordered:
        if order == "random":
            zs = [rng.gauss(0, 1) for _ in ordered]
            rng.shuffle(zs)
            zmap = {acc.phone: zs[i] for i, acc in enumerate(ordered)}
        else:
            zmap = {acc.phone: _anchor_z(acc.phone) for acc in ordered}
        # μ/σ 每天采样一次、全体共享（对抗性审查 2026-08-15：原实现在循环内每账号
        # 重采样，偏离设计"高峰中心每日一次全体共享"，导致分布趋平/作息漂移放大）
        mu = eff_lo + span * rng.uniform(cfg["mu_min_pct"], cfg["mu_max_pct"]) / 100.0
        sigma = _sigma_eff(
            span * rng.uniform(cfg["sigma_min_pct"], cfg["sigma_max_pct"]) / 100.0,
            n, span,
        )

    # 阶段 1：分配块归属（容量满向后顺延；自选已占位）
    assign = dict(chosen)
    filled = [0] * len(blocks)
    for phone in assign:
        filled[assign[phone]] += 1
    for rank, acc in enumerate(ordered):
        if dist == "uniform":
            bi = (rank // k) if order == "sequence" else (rank % len(blocks))
        else:
            x = mu + sigma * zmap[acc.phone] + rng.gauss(0, 2)  # 个人小抖动 N(0, 2min)
            # 反射回有效窗口（while 兜底，失败回退均匀随机）
            for _ in range(10):
                if x < eff_lo:
                    x = 2 * eff_lo - x
                elif x > eff_hi:
                    x = 2 * eff_hi - x
                else:
                    break
            else:
                x = rng.uniform(eff_lo, eff_hi)
            # 落块：按块边界定位（与 _schedule_blocks 的块一致；edge 掐掉首块时
            # (x - start_min)//5 会错位，改用边界匹配，杜绝越界/串块）
            bi = next((i for i, (lo, hi) in enumerate(blocks) if lo <= x < hi), None)
            if bi is None:
                bi = 0 if x < blocks[0][0] else len(blocks) - 1
        bi = _next_available(bi, filled, blocks, k)
        assign[acc.phone] = bi
        filled[bi] += 1

    # 阶段 2：块内等分 + 抖动（基于块内实际人数 m，避免人数少时挤前段）
    schedule = {}
    for bi, (lo, hi) in enumerate(blocks):
        phones = [p for p in assign if assign[p] == bi]
        m = len(phones)
        if m == 0:
            continue
        dur = hi - lo
        for j, p in enumerate(phones):
            t = lo + dur * (j + 0.5) / m + rng.uniform(0, min(0.8, dur / m / 2))
            schedule[p] = _minute_to_dt(base, min(t, hi - 0.001))
    return schedule


def run_queue_retry(accounts, notify_url, start_delay_max, gap_max, schedule=None, cred_state=None):
    """轮询队列 + 分散重试执行全部账号签到。

    流程（schedule 为空=原行为）：启动随机延迟 → 按签到模式（列表顺序 / 列表随机）
    确定执行顺序逐个尝试（账号间随机间隔）；失败的账号不立即重试，放入队尾等待下一轮；
    每账号总尝试次数受 classify_failure 分级控制（风控类最多 2 次，其他最多 4 次）；
    同一账号两次尝试间隔不小于 RETRY_MIN_INTERVAL 秒，避免连击。

    schedule 非空（自动错峰模式）：按 {phone: datetime} 时间点到点执行（已过点立即执行），
    不再叠加启动/账号间随机延迟；重试仍按队列逻辑尽快进行（不等待计划）。

    cred_state（账密熔断）：暂停中的账号零请求跳过（半开试探日除外）；
    执行后更新凭据失败计数（成功清除、凭据类失败累计、达阈值暂停）。

    返回结果字典 {手机号: (success, message, skip, status)}。
    """
    schedule = schedule or {}
    cred_state = cred_state or {}
    if not schedule:
        random_delay(start_delay_max, "启动延迟")
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
    today = datetime.now().strftime("%Y-%m-%d")
    # 调度 v2 安全底座参数（schedule 模式）：本地截止保护 + 启动对齐
    sch_cfg = _schedule_config() if schedule else None
    last_done = None  # 上次尝试结束时刻（monotonic），启动对齐用
    # P6 耗时告警：阈值可配（YIBAN_SLOW_SIGN_SEC），每账号每轮最多告警 1 次
    slow_sec = _env_int("YIBAN_SLOW_SIGN_SEC", _DEFAULT_SLOW_SIGN_SEC, 1, 600)
    slow_notified = set()

    while queue:
        acc = queue.pop(0)
        phone = acc.phone
        # 首轮（第一个账号）不等待，后续每个账号（含重试回队）先打散账号间间隔；
        # 标记在 pop 之后无条件置 False（原实现只在失败分支置 False，
        # 导致全成功路径账号间隔打散失效）
        if schedule:
            # 本地截止保护（调度 v2）：超过签到窗口末端 → 不再登录，直接跳过
            now_dt = datetime.now()
            if now_dt.hour * 3600 + now_dt.minute * 60 + now_dt.second > (
                sch_cfg["sign_end"][0] * 3600 + sch_cfg["sign_end"][1] * 60
            ):
                results[phone] = (False, "签到时段已结束", True, STATUS_SKIPPED_WINDOW)
                _write_sign_state(phone, STATUS_SKIPPED_WINDOW, "签到时段已结束")
                logger.info(f"[{phone}] ⛔ 签到时段已结束，跳过执行")
                continue
            # 自动错峰：到点执行（已过时间点立即执行）；重试回队的账号时间点已过，直接执行
            t = schedule.get(phone)
            if t:
                wait = (t - now_dt).total_seconds()
                if wait > 0:
                    time.sleep(wait)
                # 启动对齐（调度 v2）：已过点账号补足最小执行间隔，防开头连发
                elif sch_cfg["exec_gap_min"] > 0 and last_done is not None:
                    gap = sch_cfg["exec_gap_min"] - (time.monotonic() - last_done)
                    if gap > 0:
                        logger.debug(f"[{phone}] 启动对齐: 补间隔 {int(gap)}s")
                        time.sleep(gap)
        elif not first_round:
            random_delay(gap_max, f"账号 {phone} 间隔")
        first_round = False

        # 用户自暂停（调度 v2）：零请求直接跳过，状态显示"已取消"
        if getattr(acc, "user_paused", False):
            results[phone] = (False, "用户已取消签到", True, STATUS_USER_CANCELLED)
            _write_sign_state(phone, STATUS_USER_CANCELLED, "用户已取消签到")
            logger.info(f"[{phone}] ⏹️ 用户已取消签到，跳过执行")
            continue

        # 账密熔断：暂停中的账号零请求直接跳过（半开试探日除外——试探 1 次以验证恢复）
        cred = cred_state.get(phone, {})
        if cred.get("paused_since") and not _probe_due(cred, today):
            results[phone] = (False, "账密异常已暂停，请修改密码", True, STATUS_PAUSED)
            _write_sign_state(phone, STATUS_PAUSED, "账密异常已暂停（连续失败），请修改密码")
            logger.info(f"[{phone}] ⏸️ 账密异常已暂停，跳过执行")
            continue

        attempts[phone] += 1
        logger.debug(f"[{phone}] 🔄 第 {attempts[phone]} 次尝试")

        t0 = time.monotonic()  # 单次尝试耗时起点（P6：慢响应可判）
        success, message, skip, status = attempt_signin(acc)
        last_done = time.monotonic()  # 启动对齐：记录本次尝试结束时刻
        # 每次尝试结束即更新结构化状态文件（失败回队时显示 🔄 重试中；附耗时 dur）
        dur = last_done - t0
        _write_sign_state(phone, status, message, dur=dur)
        # P6 耗时告警（2026-08-16）：单次尝试超阈值 → warning + 通知。
        # 节流：每账号每轮最多 1 次（重试连击不刷屏；最终失败另有失败通知，
        # 此处主要覆盖"慢但成功"的接口劣化预警）。通知失败不影响签到（内部已捕获）。
        if dur > slow_sec and phone not in slow_notified:
            slow_notified.add(phone)
            logger.warning(f"[{phone}] ⏱️ 签到耗时 {dur:.1f}s 超过阈值 {slow_sec}s（结果: {status}）")
            if notify_url:
                send_notification(
                    "易班签到耗时告警",
                    f"账号: {_mask_phone(phone)}\n耗时: {dur:.1f}s（阈值 {slow_sec}s）\n结果: {_sanitize_text(message)}",
                    notify_url,
                )
        # 熔断计数：成功清除；凭据类失败累计（含半开试探结果——成功即恢复）
        _update_cred_state(cred_state, phone, success, message, today)
        if cred.get("paused_since") and success:
            logger.info(f"[{phone}] ✅ 半开试探成功，账密恢复，解除暂停")
        elif cred.get("paused_since") and not success and _probe_due(cred, today):
            # 试探失败：更新下次试探日，保持暂停
            next_probe = (datetime.strptime(today, "%Y-%m-%d") + timedelta(days=PROBE_INTERVAL_DAYS)).strftime("%Y-%m-%d")
            cred_state[phone]["probe_date"] = next_probe
            logger.warning(f"[{phone}] ⏸️ 半开试探失败，保持暂停（下次 {next_probe} 试探）")

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

        max_attempts = classify_failure(message)
        if attempts[phone] >= max_attempts:
            results[phone] = (False, message, False, status)
            logger.error(f"[{phone}] ❌ 已尝试 {attempts[phone]} 次，放弃: {message}")
            if notify_url:
                send_notification(
                    "易班签到失败", f"账号: {_mask_phone(phone)}\n原因: {_sanitize_text(message)}", notify_url
                )
            continue

        # 本地截止保护（调度 v2）：窗口剩余不足一个重试周期 → 不再回队，直接判失败
        if schedule:
            now_sec = datetime.now().hour * 3600 + datetime.now().minute * 60 + datetime.now().second
            end_sec = sch_cfg["sign_end"][0] * 3600 + sch_cfg["sign_end"][1] * 60
            if now_sec + sch_cfg["retry_min_interval"] >= end_sec - sch_cfg["edge_sec"]:
                results[phone] = (False, message, False, status)
                logger.error(f"[{phone}] ❌ 窗口剩余不足，不再重试: {message}")
                if notify_url:
                    send_notification(
                        "易班签到失败", f"账号: {_mask_phone(phone)}\n原因: {_sanitize_text(message)}", notify_url
                    )
                continue

        # 放回队尾：先固定补足最短间隔（可为 0），再打散一段随机延迟，
        # 总等待 = remaining + uniform(0, RETRY_GAP_MAX)，避免总间隔可能为 0
        _write_sign_state(phone, STATUS_RETRYING, f"待重试（已 {attempts[phone]} 次）")
        remaining = max(0, sch_cfg["retry_min_interval"] - gap_max) if schedule else max(0, RETRY_MIN_INTERVAL - gap_max)
        time.sleep(remaining)
        random_delay(RETRY_GAP_MAX, f"账号 {phone} 重试前等待")
        queue.append(acc)
        logger.warning(f"[{phone}] ⏳ 待重试（已 {attempts[phone]} 次，上限 {max_attempts} 次）")

    return results


def main():
    """主函数：加载账号配置并执行签到。

    支持：
    - 数据库 yiban.db（SQLite，web 后台 / TUI 配置工具写入）与 YIBAN_ACCOUNTS_JSON
    - 旧格式 YIBAN_ACCOUNTS 或 YIBAN_PHONE/YIBAN_PASSWORD（向后兼容）
    - 队列重试：账号顺序执行，失败账号放队尾分散重试（分级上限）
    - 随机延迟：YIBAN_START_DELAY_MAX（启动）/ YIBAN_ACCOUNT_GAP_MAX（账号间隔）
    - --only 指定手机号（逗号分隔），仅供 TUI 手动签到单个账号
    - --check-config 仅检查配置，不发任何网络请求
    """
    parser = argparse.ArgumentParser(description="易班自动签到")
    parser.add_argument(
        "--check-config", action="store_true", help="仅检查账号配置（脱敏打印），不发起任何网络请求"
    )
    parser.add_argument(
        "--only", default="", help="仅签到指定手机号（逗号分隔，用于 TUI 手动签到）"
    )
    args = parser.parse_args()

    notify_url = os.environ.get("YIBAN_NOTIFY_URL", "")

    # 加载账号配置（文件 > JSON 环境变量 > 旧格式，详见 load_accounts）
    try:
        accounts = load_accounts()
    except RuntimeError as e:
        logger.error(f"配置加载失败: {e}")
        sys.exit(1)

    if not accounts:
        logger.error("未配置任何账号，请通过以下任一方式配置：")
        logger.error("  1. yiban.db 数据库（推荐，用网页后台或 TUI 配置工具添加）")
        logger.error("  2. YIBAN_ACCOUNTS_JSON 环境变量（JSON 数组）")
        logger.error("  3. YIBAN_ACCOUNTS 环境变量（旧格式 phone:password#phone2:password2）")
        logger.error("  4. YIBAN_PHONE / YIBAN_PASSWORD 环境变量（单账号）")
        sys.exit(1)

    # --only 过滤：只保留指定手机号（TUI 手动签到单个账号）
    if args.only:
        only_set = {p.strip() for p in args.only.split(",") if p.strip()}
        accounts = [a for a in accounts if a.phone in only_set]
        if not accounts:
            logger.error(f"--only 指定账号不在配置中: {args.only}")
            sys.exit(1)

    # 仅检查配置模式：不发任何网络请求，用于部署验证
    if args.check_config:
        print_config_summary(accounts)
        sys.exit(0)

    # 随机延迟（TUI 设置栏可开关；默认关闭，不影响现有行为）
    start_delay_max = parse_env_int("YIBAN_START_DELAY_MAX", 0)
    gap_max = parse_env_int("YIBAN_ACCOUNT_GAP_MAX", 0)

    # 周日签到开关：关闭时周日跳过（cron 已改为每天执行，靠此开关维持周日不签）；
    # 手动签到（--only）不受限——用户主动触发应当放行
    if not args.only and datetime.now().weekday() == 6 and not SUNDAY_SIGN:
        logger.info("==== 周日签到未开启（系统设置中开启后周日也会尝试签到），跳过执行 ====")
        sys.exit(2)  # SKIPPED 语义：run.sh 写 SKIPPED 状态，次日正常执行

    logger.info(f"==== 开始执行签到，共 {len(accounts)} 个账号，队列重试模式 ====")
    # 状态文件以"尝试开始时刻"的日期命名（防跨午夜执行写错当天）
    attempt_date = datetime.now().strftime("%Y-%m-%d")
    # 自动错峰（仅自动签到；--only 手动签到立即执行，不走计划）
    schedule = {} if args.only else build_schedule(accounts)
    if schedule:
        # 容量预检（调度 v2 第三层）：n × 平均耗时 > 有效窗口秒数 → 告警不静默
        _cfg = _schedule_config()
        _span_min = (
            (_cfg["sign_end"][0] * 60 + _cfg["sign_end"][1])
            - (_cfg["sign_start"][0] * 60 + _cfg["sign_start"][1])
            - 2 * (_cfg["edge_sec"] // 60)
        )
        if len(accounts) * _cfg["avg_attempt_sec"] > _span_min * 60:
            logger.warning(
                "容量预检: %d 个账号 × 平均 %ds > 有效窗口 %d 秒，部分账号可能无法在窗口内完成",
                len(accounts), _cfg["avg_attempt_sec"], _span_min * 60,
            )
            # 超载提醒（对抗性审查补）：通知管理员，避免"超限只在日志里"无人知情
            # （send_notification 内部已捕获异常，失败不影响签到）
            send_notification(
                "易班签到容量超载",
                f"当前 {len(accounts)} 个账号 × 平均 {_cfg['avg_attempt_sec']}s "
                f"> 有效窗口 {_span_min * 60}s，部分账号可能无法在窗口内完成签到。\n"
                f"建议：增加窗口时长或减少账号数量（.env 调整）。",
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
                json.dump({"snapshot_at": datetime.now().strftime("%H:%M:%S")}, _f)
            os.replace(_snap_tmp, _snap_path)
        except OSError:
            pass  # 标记不可写时 web 端回退旧分界，不影响签到
    # 账密熔断状态：跨天计数（暂停账号零请求；手动签到 --only 不受限）
    cred_state = {} if args.only else _load_cred_state()
    results = run_queue_retry(
        accounts, notify_url, start_delay_max, gap_max, schedule=schedule, cred_state=cred_state,
    )
    _save_cred_state(cred_state)

    # 汇总（合并为一行统计；逐账号结果已在执行中输出，不再逐行重复）
    # 口径：成功=success/already；跳过=no_task+skipped（无需签到与时段外同列）；
    # 已执行=已了结（success/already/no_task），窗口外等跳过不算（7:10 还会再跑）
    has_real_failure = False
    has_executed = False
    ok_n = fail_n = skip_n = 0
    for acc in accounts:
        _s, _m, _sk, status = results.get(acc.phone, (False, "未执行", False, STATUS_PENDING))
        if status in (STATUS_SUCCESS, STATUS_ALREADY):
            ok_n += 1
        elif status in (STATUS_NO_TASK, STATUS_SKIPPED_WINDOW, STATUS_SKIPPED_NORANGE, STATUS_PAUSED, STATUS_USER_CANCELLED):
            skip_n += 1
        else:
            fail_n += 1
            has_real_failure = True
        if status in (STATUS_SUCCESS, STATUS_ALREADY, STATUS_NO_TASK):
            has_executed = True
    summary = f"✅ {ok_n} 成功，❌ {fail_n} 失败"
    if skip_n:
        summary += f"，➖ {skip_n} 跳过"
    logger.info(f"==== 签到汇总：{summary} ====")

    # 写按日状态文件（供网页日历组件读取；窗口外跳过不写，当天留空）
    # 符号按状态码：success/already→✅、no_task→➖、failed→❌
    state_dir = os.environ.get("YIBAN_STATE_DIR", "/var/log/yiban")
    try:
        os.makedirs(state_dir, exist_ok=True)
        daily_path = os.path.join(state_dir, f"sign-daily-{attempt_date}.json")
        daily = {}
        if os.path.exists(daily_path):
            with open(daily_path, encoding="utf-8") as f:
                daily = json.load(f)
        for acc in accounts:
            _s, _m, _sk, status = results.get(acc.phone, (False, "未执行", False, STATUS_PENDING))
            if status in (STATUS_SUCCESS, STATUS_ALREADY, STATUS_NO_TASK, STATUS_FAILED):
                daily[acc.phone] = STATUS_SYMBOL[status]
        with open(daily_path, "w", encoding="utf-8") as f:
            json.dump(daily, f, ensure_ascii=False)
    except OSError as e:
        logger.warning("写入按日状态文件失败: %s", e)

    # 退出码（run.sh 依据退出码写状态文件）：
    # 0 - 全部成功（有实际签到执行；含"已签到""窗口外部分跳过但至少执行过"）
    # 1 - 有真正的失败（登录失败、签到失败等）
    # 2 - 全部 skip（无实际执行：全部未在签到时间内/非签到日/窗口缺失），
    #     由 run.sh 写 SKIPPED 而非 SUCCESS，避免备份等下游任务被吞
    if has_real_failure:
        sys.exit(1)
    if not has_executed:
        sys.exit(2)
    sys.exit(0)


if __name__ == "__main__":
    main()
