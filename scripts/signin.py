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
6. 重试逻辑：失败账号分散重试——开启签到调度时重新安排到窗口内合适时间，否则放回队尾（风控类最多 2 次，其他最多 4 次）
7. 随机延迟：启动与账号间隔随机打散（YIBAN_START_DELAY_MAX / YIBAN_ACCOUNT_GAP_MAX）

参考项目：
- KillYiBan（默认登录流程的真实 App 请求特征来源；与同作者 FYIBAN 同源）
- OneFeiFan/FYIBAN 模块（多边形定位算法与 nightAttendance 签到流程）
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
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# 共享模块（同目录）：加密（web/tui/db 共用密钥与密文格式）与 SQLite 数据访问层
import account_crypto
import db  # 2026-08-16 审查轮：原 _load_accounts_from_file/build_schedule 函数内 import 上移（无循环依赖）
import env_lock  # 探针 once 模式自动关闭 .env（跨进程写锁）
import mailer  # A 线：管理员告警邮件 / B 线：用户签到失败邮件（SMTP，零依赖；不配置则不启用）
import requests
from Crypto.Cipher import PKCS1_v1_5
from Crypto.PublicKey import RSA
from requests.utils import cookiejar_from_dict, dict_from_cookiejar

# 密码学安全随机数生成器（用于定位生成等安全敏感场景）
_secure_random = secrets.SystemRandom()

# ---------------------------------------------------------------------------
# 日志配置
# ---------------------------------------------------------------------------
# 支持通过环境变量调整日志级别：DEBUG / INFO / WARNING / ERROR
LOG_LEVEL = os.environ.get("YIBAN_LOG_LEVEL", "INFO").upper()

try:
    import fcntl  # Unix/Linux 文件锁；Windows 不支持
except ImportError:
    fcntl = None


class _FlockFileHandler(logging.FileHandler):
    """带文件锁的日志处理器：防止多进程并发写入同一日志文件时行交错。

    Windows 无 fcntl 时退化为普通 FileHandler（仅限本地开发）。
    """

    def emit(self, record):
        try:
            if fcntl is not None and self.stream:
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX)
            super().emit(record)
        except Exception:
            self.handleError(record)
        finally:
            # 确保任何路径都释放锁（防 emit 异常后同进程死锁）
            if fcntl is not None and self.stream:
                try:
                    self.stream.flush()
                    fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
                except Exception:
                    pass


@contextmanager
def _state_file_lock(path):
    """状态文件读改写锁：POSIX 用 fcntl.flock，Windows 无 fcntl 时退化为无操作。

    锁文件单独使用 ``<path>.lock``，不要与日志 handler 的 flock 混用。
    """
    if fcntl is None:
        with nullcontext():
            yield
        return
    lock_path = path + ".lock"
    lock_dir = os.path.dirname(lock_path) or "."
    os.makedirs(lock_dir, exist_ok=True)
    with open(lock_path, "a+", encoding="utf-8") as lock_f:
        fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)


# 进程级签到单实例锁：全量模式等待其他进程退出的上限（秒）。
# 手动 --only 通常几十秒结束；cron 全量队列被手动阻塞时最多等这么久。
_RUN_LOCK_WAIT_DEFAULT = 600


class _RunLockHeld(Exception):
    """签到锁被其他进程持有（--only 模式下由 _acquire_run_lock 抛出）。"""


def _acquire_run_lock(only_mode):
    """进程级签到单实例锁：防 cron 全量队列与手动 --only 并发签到同一账号。

    对抗性审查（2026-08-20）P2：web 端防抖/terminate 只覆盖 web 自己 spawn 的
    子进程，cron 全量队列与手动 --only 之间无任何互斥——同账号可被两个进程
    并发登录易班（重复打卡/会话异常/风控画像）。锁文件 <STATE_DIR>/signin-run.lock：
    - 全量模式：阻塞等待至多 YIBAN_RUN_LOCK_WAIT 秒（默认 600s），超时告警后
      无锁继续——漏签一整天的代价高于极小概率的重叠；
    - --only 模式：立即尝试一次，被持有则抛 _RunLockHeld（调用方退出并留痕，
      管理员稍后重试）——手动触发不应在 web 已返回的后台进程里排队阻塞。
    返回持锁文件句柄（flock 随进程退出自动释放）；Windows 无 fcntl 或状态目录
    不可写时返回 None（不互斥、不阻断，与 _state_file_lock 降级策略一致）。
    """
    state_dir = os.environ.get("YIBAN_STATE_DIR", "/var/log/yiban")
    try:
        os.makedirs(state_dir, exist_ok=True)
        fh = open(os.path.join(state_dir, "signin-run.lock"), "a+", encoding="utf-8")
    except OSError:
        return None
    if fcntl is None:
        # 2026-08-28 审查 F5：Windows 无 fcntl 时锁退化为无互斥，且此前无任何
        # 提示——管理员在 Windows 上跑多进程（如 cron + 手动）时会静默出现
        # 同账号并发签到的可能（重复打卡/风控）。明确告警一次（每进程一次）。
        logger.warning(
            "当前平台无 fcntl（Windows），签到单实例锁未生效："
            "cron 全量队列与手动 --only 并发时可能对同一账号重复签到，"
            "建议在 Linux/容器环境运行或避免同时触发手动与定时签到"
        )
        return fh
    wait_sec = 0.0
    if not only_mode:
        try:
            wait_limit = float(
                os.environ.get("YIBAN_RUN_LOCK_WAIT", _RUN_LOCK_WAIT_DEFAULT)
            )
        except (TypeError, ValueError):
            wait_limit = _RUN_LOCK_WAIT_DEFAULT
    while True:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fh
        except OSError:
            pass
        if only_mode:
            fh.close()
            raise _RunLockHeld()
        if wait_sec >= wait_limit:
            logger.warning(
                "等待签到锁超时（%ss），本次无锁继续执行（可能与另一签到进程并发，请检查）",
                wait_limit,
            )
            return fh
        time.sleep(0.5)
        wait_sec += 0.5


# 按天日志文件路径（与 web/app.py log_path_for 一致）
def _signin_log_path():
    date_str = datetime.now().strftime("%Y-%m-%d")
    log_file = os.environ.get("YIBAN_LOG_FILE", "/var/log/yiban/sign.log")
    return os.path.join(os.path.dirname(log_file), f"sign-{date_str}.log")


def _make_log_handler():
    """创建日志处理器：目录不存在时尝试创建，仍失败则降级 stderr（不阻断签到执行）。"""
    path = _signin_log_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        return _FlockFileHandler(path, encoding="utf-8")
    except OSError:
        # 目录不可写/不存在：降级到 stderr（保持原始行为，签到不因日志中断）
        return logging.StreamHandler()


_handler = _make_log_handler()
_handler.setFormatter(logging.Formatter(
    "[%(asctime)s] [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
))
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    handlers=[_handler],
)
logger = logging.getLogger("yiban")


# 易班 App 版本特征：两处请求头（KILLYIBAN_HEADERS / usersure 提交）必须同值，
# 不一致可能触发服务端一致性校验；旧流程 iOS UA 尾段同步引用。
# 2026-08-22 由 5.1.2 升至应用商店真实最新版 5.2.2（下次升版只改这一行）
YIBAN_APP_VERSION = "5.2.2"

# 易班 iOS 客户端 UA（与 Auto-Test 保持一致）
HEADERS = {
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/4.0 "
    "Chrome/104.0.5112.97 Mobile Safari/537.36 yiban_iOS/" + YIBAN_APP_VERSION,
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "X-Requested-With": "com.yiban.app",
    "Origin": "https://app.uyiban.com",
    "Referer": "https://app.uyiban.com/",
    "Connection": "close",
}

# KillYiBan 同款请求头（默认登录方式；与同作者的 FYIBAN 同源，KillYiBan 脱胎于 FYIBAN）
# 注意：usersure 提交时会被显式覆盖为不带 Origin/Referer（见 login_killyiban 第 3 步，
# 实测带 Origin → e001 无效应用端编号），其余请求用此头
KILLYIBAN_HEADERS = {
    "User-Agent": "Yiban",
    "AppVersion": YIBAN_APP_VERSION,
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

    通过 Web 管理后台或 TUI 配置工具添加（存于 SQLite 数据库），
    一次输入一个账号的完整信息，无需用符号分隔。
    """

    phone: str
    # C-SIGN-04 已知局限：str 不可变无法原位清零，且重试队列需跨尝试复用，
    # 密码 str 本体只能随 accounts 列表生命周期存活（客户端侧可变副本见
    # YibanClient._wipe_credentials 的清零与局限说明）
    password: str
    phone_model: str = ""  # 设备型号（学校开启"设备绑定"时必填）
    phone_code: str = ""  # 设备唯一识别码（学校开启"设备绑定"时必填）
    name: str = ""  # 可选：自定义名称（TUI 输入，未填写时显示为"账号N"）
    user_paused: bool = False  # 用户自暂停签到（调度 v2；db.load_accounts 透传）
    owner: str = ""  # 账号归属用户邮箱（B 线：签到失败时向 owner 发提醒邮件；JSON/legacy 来源为空）

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

# 随机延迟默认值在 web/app.py 与 tui/app.py 中维护（signin.py 不直接使用）。

# 签到模式：sequence（列表顺序，默认）/ random（列表随机打散）
# 由网页系统设置页写入 .env（YIBAN_SIGN_MODE），run.sh 加载后经环境变量传入
SIGN_MODE = os.environ.get("YIBAN_SIGN_MODE", "").strip().lower()

# 周日签到开关：部分学校周日也有签到任务（默认关闭，与历史行为一致）
# 由网页系统设置页写入 .env（YIBAN_SUNDAY_SIGN=1），run.sh 加载后经环境变量传入
SUNDAY_SIGN = os.environ.get("YIBAN_SUNDAY_SIGN", "").strip().lower() in ("1", "true", "on", "yes")

# ---- 探针模式 / 注册时账号验证（2026-08-25）----
# 非签到时段对全部账号做只读健康检查（登录+拉任务，不提交签到），提前发现
# 「图形验证墙 / 校本化失效 / 密码错误」等无法自愈问题；注册提交账号时亦可即时验证打回。
# 配置经 web 系统设置写入 .env（YIBAN_PROBE_ENABLE / YIBAN_PROBE_TIME / YIBAN_PROBE_INTERVAL_DAYS /
# YIBAN_ACCOUNT_VERIFY），run.sh / run_probe.sh 加载后经环境变量传入。
PROBE_ENABLE = os.environ.get("YIBAN_PROBE_ENABLE", "").strip().lower() in ("1", "true", "on", "yes")
PROBE_TIME = os.environ.get("YIBAN_PROBE_TIME", "20:00").strip() or "20:00"
# 触发频率：正整数=每 N 天；once=下一次计划时间单次执行（执行后自动关闭）
PROBE_INTERVAL = os.environ.get("YIBAN_PROBE_INTERVAL_DAYS", "1").strip() or "1"

# 探针视为"无法自愈、需预警"的错误特征（复用错误分类思路；网络/Token 等可自愈失败不预警）
PROBE_HARD_FAIL_RE = re.compile(
    r"图形验证|图片验证|滑块验证|人机验证|captcha"
    r"|校本化|未授权|授权失效|Auth Error|Get Night Attendance Sign Tasks Error"
    r"|登录失败|密码错误|账号或密码"
    r"|授权设备|获取登录入口失败|登录响应异常|最终认证失败"
    r"|WAF|风控|拦截"
)

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
# 全局暂停（管理员 Web UI 一键暂停：整站停止自动签到）。
# 注意：本进程不产此状态——暂停时 main() exit(2)，由 run.sh 依据 YIBAN_GLOBAL_PAUSE=1
# 在「日状态文件」写入 GLOBAL_PAUSED（区别于普通 SKIPPED，供运维/监控区分）；
# 此处保留常量与符号，供显示层/TUI 消费日状态时映射。
STATUS_GLOBAL_PAUSED = "global_paused"

# 状态码 → 日志/日历符号（与 web/TUI 显示层一致）
STATUS_SYMBOL = {
    STATUS_SUCCESS: "✅", STATUS_ALREADY: "✅", STATUS_NO_TASK: "➖",
    STATUS_FAILED: "❌", STATUS_RETRYING: "🔄",
    STATUS_SKIPPED_WINDOW: "⛔", STATUS_SKIPPED_NORANGE: "⛔",
    STATUS_PAUSED: "⏸️", STATUS_USER_CANCELLED: "⏹️",
    STATUS_GLOBAL_PAUSED: "⏸",
}

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
_DEFAULT_MIN_EXEC_GAP = 5       # 请求最小间隔下限（秒，压缩模式防请求过密；F1 接线于 run_queue_retry）
_DEFAULT_AVG_ATTEMPT_SEC = 8    # 容量预检：单次执行平均耗时估算
_DEFAULT_RETRY_MIN_INTERVAL = 60
_DEFAULT_EXEC_GAP_MIN = 10      # 启动对齐：已过点账号相邻最小间隔（秒）
_DEFAULT_ALLOW_TIME_PREF = 0    # 用户自选时间片总开关（0=关默认，管理员开启后生效）
_DEFAULT_SLOW_SIGN_SEC = 30     # P6 耗时告警阈值（秒）：单次尝试耗时超此值 → warning + 通知

# 签到窗口配置异常的一次性告警标记（2026-08-28 审查 F3）：
# _schedule_config 每次调度都会调用，非法窗口回退默认窗口的告警只收集一次，
# 避免同一个配置错误在每日汇总邮件里重复出现 N 次
_invalid_window_notified = False


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
    返回 dict：order/dist/edge_front_sec/edge_back_sec/block_cap/mu/sigma 百分比/
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
        global _invalid_window_notified
        if not _invalid_window_notified:
            _invalid_window_notified = True
            _msg = (
                f"签到窗口 {start[0]:02d}:{start[1]:02d} ~ {end[0]:02d}:{end[1]:02d} 非法"
                "（start>=end，跨零点窗口不受支持），已回退默认 06:30~07:50，"
                "实际签到时间将与配置不符！请修改 YIBAN_SIGN_START / YIBAN_SIGN_END"
            )
            logger.error("%s", _msg)
            # 2026-08-28 审查 F3：原实现只写 WARNING 日志，管理员在 Web 界面看到的
            # 窗口设置"看起来生效"、实际签到时刻完全不同且无人知情。现并入当日
            # 汇总邮件（A 线），确保配置错误可被管理员发现。
            _collect_admin_mail("签到窗口配置异常", _msg)
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
    old_edge = _env_int("YIBAN_WINDOW_EDGE_SEC", None, 0, 600)
    return {
        "order": order,
        "dist": dist,
        # 掐头去尾（0.22.0 起前后独立，秒级，0.5 分钟=30s 粒度；UI 按 0.5 分钟步进）：
        # 新键 YIBAN_WINDOW_EDGE_FRONT_SEC / _BACK_SEC 优先；旧键 YIBAN_WINDOW_EDGE_SEC
        # 存在时（升级前部署）映射为前后对称，保证旧配置行为不变。
        "edge_front_sec": _env_int(
            "YIBAN_WINDOW_EDGE_FRONT_SEC",
            old_edge if old_edge is not None else _DEFAULT_EDGE_SEC,
            0, 300,
        ),
        "edge_back_sec": _env_int(
            "YIBAN_WINDOW_EDGE_BACK_SEC",
            old_edge if old_edge is not None else _DEFAULT_EDGE_SEC,
            0, 300,
        ),
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
        lng = center_lng + (max_lng - min_lng) * 0.2 * (_secure_random.random() - 0.5)
        lat = center_lat + (max_lat - min_lat) * 0.2 * (_secure_random.random() - 0.5)
        if point_in_polygon(lng, lat, scaled_points) and point_in_polygon(lng, lat, polygon_points):
            return (lng, lat)

    # 兜底：质心 + 小范围随机抖动。避免多账号/多次触发共用同一质心坐标
    # （固定坐标聚集会成为风控行为指纹），同时保持仍在签到范围内。
    jitter = min(max_lng - min_lng, max_lat - min_lat) * 0.01  # 范围边长的 1%，约几十米量级
    jitter = max(jitter, 1e-6)  # 极小多边形时防止抖动归零
    for _ in range(50):
        fallback = (center_lng + _secure_random.uniform(-jitter, jitter),
                    center_lat + _secure_random.uniform(-jitter, jitter))
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


def clear_session_cache_quiet(phone):
    """清除单账号会话缓存（模块级入口，供重试队列联动）；失败仅留痕不影响签到。

    db 未初始化（环境变量账号模式）时为无操作。
    """
    try:
        if db.is_initialized():
            db.clear_session_cache(phone)
    except Exception as e:
        logger.debug(f"[{phone}] 清除会话缓存失败: {_sanitize_text(e)}")


def random_delay(max_seconds, label):
    """随机等待 0~max_seconds 秒（打散固定执行规律，max_seconds<=0 时不等待）。"""
    if max_seconds <= 0:
        return
    wait = random.uniform(0, max_seconds)
    logger.debug(f"{label}: 随机延迟 {int(wait)} 秒（上限 {max_seconds} 秒）")
    time.sleep(wait)


def _sanitize_text(text):
    """服务端可控内容进入错误消息/日志/通知前转义换行与回车，防止日志与通知注入。"""
    s = str(text).replace("\r", "\\r").replace("\n", "\\n")
    # 脱敏：异常消息可能含 Account dataclass repr（含明文密码/令牌）
    # 整体替换 Account(...) 对象（正则处理引号转义边界），并兜底替换 password/phone_code 字段
    s = re.sub(r"Account\([^)]*\)", "Account(***)", s)
    s = re.sub(r"password\s*=\s*['\"][^'\"]*['\"]", "password='***'", s)
    s = re.sub(r"phone_code\s*=\s*['\"][^'\"]*['\"]", "phone_code='***'", s)
    # dict/repr 形态兜底（C-SIGN-03）：'phone_code': 'xxx' / "password": "xxx"——
    # kwarg 形态正则覆盖不到 dict repr（如 vars()/json.dumps 调试输出进异常链）
    s = re.sub(r"(['\"])password\1\s*:\s*['\"][^'\"]*['\"]", r"\1password\1: '***'", s)
    s = re.sub(r"(['\"])phone_code\1\s*:\s*['\"][^'\"]*['\"]", r"\1phone_code\1: '***'", s)
    return s


def _mask_phone(phone):
    """通知/对外输出脱敏：11 位手机号 → 138****8000（本地 sign.log 保留完整号供排查；
    对外 webhook 与 web 展示层不落完整号——规范审查 D2）。"""
    p = str(phone)
    return p[:3] + "****" + p[7:] if len(p) == 11 else p


# URL query 敏感参数名片段（子串、不区分大小写匹配）：OAuth code/token、CSRF/session
# 标识、签名票据类——最终 URL 进诊断日志前值统一打码（C-SIGN-01）
_URL_SENSITIVE_KEY_PARTS = (
    "code", "token", "csrf", "session", "ticket", "sign",
    "auth", "key", "secret", "passwd", "password", "verify",
)


def _sanitize_url(url):
    """URL 入日志前对 query 敏感参数脱敏（C-SIGN-01）。

    诊断日志需要的是 scheme/host/path 与"带了哪些参数"，不是参数值：可能携带
    凭据的（OAuth code、CSRF、session 标识等）一律替换为 ***；≥24 位连续
    URL-safe 字符的高熵值无论参数名一律打码，兜底未知令牌参数名（阈值取 24：
    真实 code/token 通常远长于此，避免误伤 client_id 这类恰好 16 位的公开标识）。
    解析失败返回占位符，绝不抛异常影响主流程。
    """
    raw = str(url)
    try:
        parts = urlsplit(raw)
        pairs = parse_qsl(parts.query, keep_blank_values=True)
    except ValueError:
        return "<url 解析失败已省略>"
    if not pairs:
        return raw

    def _masked(key, value):
        k = key.lower()
        if any(part in k for part in _URL_SENSITIVE_KEY_PARTS):
            return f"{key}=***"
        if len(value) >= 24 and re.fullmatch(r"[A-Za-z0-9_\-]+", value):
            return f"{key}=***"
        return f"{key}={value}"

    return urlunsplit(parts._replace(query="&".join(_masked(k, v) for k, v in pairs)))


# ---------------------------------------------------------------------------
# 账号配置加载
# ---------------------------------------------------------------------------
def _key_env_file():
    """密钥来源 .env 路径：YIBAN_ENV_FILE 优先（与 web/TUI 子进程约定一致），回退默认 .env。

    2026-08-21 对抗性审查修复：此前 web/TUI 为保证自定义 .env 路径下子进程能解密，
    把 YIBAN_ACCOUNTS_KEY 明文注入子进程环境变量（同 uid 进程可读 /proc/<pid>/environ，
    密钥暴露面扩大）。现统一改为传递【路径】而非密钥本身，本函数即子进程侧的解析入口。
    """
    return os.environ.get("YIBAN_ENV_FILE", "").strip() or None


def _parse_account_dict(data):
    """将账号 JSON 对象解析为 Account，校验必填字段。

    password/phone_code 支持 AES-GCM 密文对象（web/TUI 存储层加密落盘，
    0.17+ 数据在 yiban.db（SQLite），accounts.json 仅存于迁移前——解密依赖
    同一密钥：环境变量 YIBAN_ACCOUNTS_KEY → .env 同键（YIBAN_ENV_FILE 可指定
    路径）；密钥缺失/解密失败抛明确错误，绝不静默使用错误数据）。
    """
    phone = str(data.get("phone") or data.get("account") or "").strip()
    password = data.get("password") or data.get("pwd") or ""
    phone_code = data.get("phone_code") or ""
    if account_crypto.is_encrypted(password) or account_crypto.is_encrypted(phone_code):
        if not account_crypto.has_key(_key_env_file()):
            raise RuntimeError(
                "账号已加密但未配置 YIBAN_ACCOUNTS_KEY（请在 .env 中配置或恢复密钥备份）"
            )
        key = account_crypto.load_key(_key_env_file())
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
        # 用户自暂停（调度 v2）：显式解析 "1"/"true"/"on"/"yes"，避免 "0"/"false" 被 bool() 误判
        user_paused=str(data.get("user_paused", False)).strip().lower() in ("1", "true", "on", "yes"),
        # 归属用户邮箱（B 线用户失败提醒用；JSON/legacy 环境变量来源无此字段）
        owner=str(data.get("owner") or "").strip(),
    )


def _load_accounts_from_file():
    """从数据库加载（yiban.db，SQLite；web 后台写入，单行事务防并发覆盖）。

    db 层返回已解密明文；此处只做审核状态过滤。
    """
    db.init_db(env_file=_key_env_file())
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
    """旧格式兼容：YIBAN_ACCOUNTS（phone:password#...）与 YIBAN_PHONE/YIBAN_PASSWORD。

    2026-08-27 审查缺口 3：此路径仍接受明文凭据环境变量——进库前会加密，但明文源
    留在 .env 与进程环境（/proc/<pid>/environ 同 uid 可读）。保留兼容，但加载即告警，
    提示改用 Web 管理台 / YIBAN_ACCOUNTS_JSON；告警内容不含任何凭据明文。
    """
    accounts = []
    accounts_str = os.environ.get("YIBAN_ACCOUNTS", "")
    if accounts_str or os.environ.get("YIBAN_PASSWORD", ""):
        logger.warning(
            "检测到旧格式明文账号配置（YIBAN_ACCOUNTS/YIBAN_PASSWORD）：凭据明文存在于 "
            "环境变量与进程环境中，建议改用 Web 管理台或 YIBAN_ACCOUNTS_JSON 管理账号"
        )
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
def _is_fyiban_url(url):
    """严格校验易班跳转 URL：https + 主机精确为 f.yiban.cn + 不允许 userinfo。

    使用 urlsplit 避免 `https://f.yiban.cn.evil.com` 或
    `https://f.yiban.cn@evil.com` 这类前缀/userinfo 绕过。
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return (
        parts.scheme == "https"
        and parts.hostname == "f.yiban.cn"
        and parts.username is None
    )


class YibanClient:
    """易班客户端：封装登录与签到流程。"""

    def __init__(self, account):
        self.account = account
        # C-SIGN-04：密码缓冲用可变 bytearray 持有（str 不可原位清零），
        # 单次签到尝试结束由 _wipe_credentials 原位清零（attempt_signin finally）
        self.password = bytearray(account.password.encode("UTF-8"))
        # 登录方式：默认 KillYiBan 同款流程（真实 App 特征，与同作者 FYIBAN 同源，实测绕过 e003）；
        # 旧流程（Auto-Test 继承的 iOS 伪造 UA）仅在 YIBAN_LEGACY_LOGIN=1 时启用（GitHub Actions 等场景备选）
        self.use_killyiban = os.environ.get("YIBAN_LEGACY_LOGIN", "") != "1"
        if self.use_killyiban:
            self.csrf = secrets.token_hex(16)  # SecureRandom 真随机
            logger.debug(
                f"[{account.phone}] 登录方式: 标准 App 特征（UA=Yiban/AppVersion={YIBAN_APP_VERSION}/SecureRandom CSRF）"
            )
        else:
            self.csrf = secrets.token_hex(16)  # 使用安全随机数替代可预测的时间戳 md5
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
        # bytes(...) 产生一个短暂不可变副本（pycryptodome 接口要求），交由 GC 回收；
        # 可清零的 bytearray 本体在尝试结束后由 _wipe_credentials 原位覆写
        return b64encode(cipher.encrypt(bytes(self.password)))

    def _wipe_credentials(self):
        """凭据内存尽力清零（C-SIGN-04）：单次签到尝试结束（成败均然）由 attempt_signin 调用。

        - password 缓冲（bytearray）原位覆写 \\x00——唯一能保证失效的副本；
        - 解除 account/phone_model/phone_code 引用，缩短凭据可回收窗口。
        CPython 局限：不可变对象（str/bytes）无法原位清零，RSA 加密瞬态副本与
        Account.password 本体只能等 GC；core dump / swap 场景仍可能残留。彻底
        消除需全链路换可清零凭据容器（侵入 web/db/TUI 存储层，标注为已知限制）。
        """
        pwd = getattr(self, "password", None)
        if isinstance(pwd, bytearray):
            pwd[:] = b"\x00" * len(pwd)
        self.password = None
        self.phone_model = None
        self.phone_code = None
        self.account = None

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
            # 只落响应摘要（前 300 字符），避免整页 HTML/敏感内容进日志；
            # _sanitize_text 同时处理 \r（防日志伪造）并脱敏 Account repr；
            # 最终 URL 的 query 可能带 OAuth code/CSRF，经 _sanitize_url 打码（C-SIGN-01）
            body_preview = _sanitize_text(resp.text[:300].replace("\n", "\\n"))
            logger.error(f"[{self.account.phone}] OAuth 页解析失败诊断:")
            logger.error(f"  最终 URL: {_sanitize_url(resp.url)}")
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
                    "oauth_uname": self.account.phone,
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
            # 同上：_sanitize_text 处理 \r 与 Account repr 脱敏
            body_preview = _sanitize_text(resp.text[:300].replace("\n", "\\n"))
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
            if not _is_fyiban_url(target):
                raise RuntimeError("ydclearance 跳转目标不在白名单")
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
        resp = self.session.get(
            "https://api.uyiban.com/base/c/auth/yiban",
            params={"verifyRequest": verify_code, "CSRF": self.csrf},
            cookies={},
            allow_redirects=False,
            timeout=15,
        )
        if is_waf_blocked(resp.text):
            raise RuntimeError("请求被 WAF 风控拦截，请配置 YIBAN_PROXY 代理后重试")
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"最终认证失败: {_sanitize_text(data.get('msg'))}")

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
        # 0. 会话缓存：命中则还原 cookies + csrf，复用第 1 步 OAuth 探针判活——
        #    302 到 iapp7463 = 会话仍有效（免登录）；200 登录页 = 失效，清缓存走完整流程
        restored = self._restore_session_cache()
        if not restored:
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
            if restored:
                logger.info(f"[{phone}] 登录: 会话缓存命中，免登录复用")
            else:
                logger.info(f"[{phone}] 登录: 已登录状态（无需提交）")
            self.logged_in = True
            return
        if restored:
            # 缓存会话已被服务端判失效（探针返回登录页）：清缓存并还原干净初始会话
            self._clear_session_cache()
            self.session.cookies = cookiejar_from_dict({"csrf_token": self.csrf})

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
                "AppVersion": YIBAN_APP_VERSION,
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
        # 完整登录成功：保存会话缓存供下次免登录复用（失败仅告警，不影响签到）
        self._save_session_cache()

    # ---- 会话 Cookie 缓存（SQLite 表版，db.session_cache，2026-08-22）----
    # 我们的登录是 OAuth 会话 Cookie 流程（非 access_token）：login_killyiban 五步
    # 完成后认证态落在 session.cookies + self.csrf 上。缓存序列化 cookie jar + csrf，
    # 下次签到先探针判活复用会话——减少登录频率 = 降低风控触发面（调研吸收项，
    # docs/research-lumjiel-core-sign-20260822.md §七）。仅 db 已初始化（数据库账号
    # 模式）时启用；CI 环境变量账号模式不建缓存。
    def _restore_session_cache(self):
        """登录前查会话缓存：命中还原 cookies + csrf 并返回 True（会话是否仍有效
        由 login_killyiban 第 1 步 OAuth 探针判定）。任何缓存读失败都按未命中
        处理，绝不阻断正常登录。"""
        if not db.is_initialized():
            return False
        try:
            cached = db.get_session_cache(self.account.phone)
        except Exception as e:
            logger.debug(f"[{self.account.phone}] 读取会话缓存失败（按未命中处理）: {_sanitize_text(e)}")
            return False
        if not cached:
            return False
        try:
            cookies = json.loads(cached["cookies"])
        except (TypeError, ValueError):
            logger.warning(f"[{self.account.phone}] 会话缓存 cookies 非合法 JSON，已清除")
            self._clear_session_cache()
            return False
        self.session.cookies = cookiejar_from_dict(cookies)
        self.csrf = cached["csrf"]
        return True

    def _save_session_cache(self):
        """完整登录成功后保存 cookie jar + csrf（密文落库）；失败仅告警不影响签到。"""
        if not db.is_initialized():
            return
        cookies = dict_from_cookiejar(self.session.cookies)
        if not cookies:
            return  # 空会话无复用价值，不落库
        try:
            db.set_session_cache(self.account.phone, json.dumps(cookies), self.csrf)
        except Exception as e:
            logger.warning(f"[{self.account.phone}] 保存会话缓存失败（不影响签到）: {_sanitize_text(e)}")

    def _clear_session_cache(self):
        """清除本账号会话缓存（探针判死 / 风控类失败联动清除）。"""
        if not db.is_initialized():
            return
        try:
            db.clear_session_cache(self.account.phone)
        except Exception as e:
            logger.debug(f"[{self.account.phone}] 清除会话缓存失败: {_sanitize_text(e)}")

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
        target = path_m[0]
        if target.startswith("/"):
            target = "https://f.yiban.cn" + target
        if not _is_fyiban_url(target):
            raise RuntimeError("ydclearance 跳转目标不在白名单")
        return cookie_m[0], target

    # ---- 签到 -------------------------------------------------------------
    def signin(self):
        """执行签到，返回 (success: bool, message: str, skip: bool, status: str)。

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

        # 3. 解析多边形点（逐点容错：单个坏点跳过，不拖垮整个签到）
        points_raw = position.get("Points", [])
        polygon = []
        for p in points_raw:
            try:
                parts = str(p).split(",")
                if len(parts) >= 2:
                    polygon.append((float(parts[0]), float(parts[1])))
            except (TypeError, ValueError):
                continue

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

    def verify(self):
        """只读健康检查（登录后）：拉取签到位置，**不提交签到**。

        用于注册时预处理验证与探针模式。返回 (ok, message)：
        - ok=True：账号可正常签到（能登录且能拉到任务，含校本化授权正常）
        - ok=False：存在无法自愈的问题（登录失败/校本化失效/图形验证/WAF 等，
          message 已脱敏，供用户可见提示或探针预警）
        """
        if not self.logged_in:
            if self.use_killyiban:
                self.login_killyiban()
            else:
                self.login()
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
            return False, "请求被 WAF 风控拦截，请配置 YIBAN_PROXY 代理后重试"
        data = resp.json()
        if data.get("code") != 0:
            return False, f"获取签到任务失败: {_sanitize_text(data.get('msg'))}"
        return True, "账号健康，可正常签到"


# ---------------------------------------------------------------------------
# 消息通知
# ---------------------------------------------------------------------------
def _notify_url_desc(url):
    """通知日志只记录 scheme://host[:port]，避免把 query/token/userinfo 带进日志。"""
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


def send_notification(title, content, url):
    """通过 Server 酱 / Bark / 企业微信等 webhook 发送通知（即时推送）。

    2026-08-21 对抗性审查加固：禁用重定向——30x 会把通知内容 POST 到
    重定向目标主机（webhook 服务被劫持/恶意 301 时内容外泄到第三方）。
    说明：签到脚本给管理员的**邮件**不在此处发送（避免逐条轰炸），而是由
    各触发点 _collect_admin_mail 收集、任务结束 _flush_admin_mail_summary 汇总。
    """
    if not url:
        return
    url_desc = _notify_url_desc(url)
    try:
        if url.startswith("http"):
            resp = requests.post(
                url, json={"title": title, "content": content}, timeout=10,
                allow_redirects=False,
            )
            if resp.status_code < 400:
                logger.info("通知发送成功: %s", title)
            else:
                logger.warning(
                    "通知发送失败（%s）: 状态码 %s，URL %s",
                    title, resp.status_code, url_desc,
                )
    except Exception as e:
        # 不打印 exc_info，也不打印 str(e)：异常文本可能包含 webhook URL/token；
        # 只记录异常类型名与脱敏后的 scheme://host[:port]
        logger.warning(
            "通知发送失败（%s）: %s，URL %s",
            title, type(e).__name__, url_desc,
        )


# A 线合并版收集器：签到脚本运行期把"发给管理员"的邮件先收集，任务结束统一汇总
# 发送（避免多账号失败时逐封轰炸）。B 线用户邮件不在此收集，保持逐条即时。
_mail_summary = []  # list[(subject, text)]

# 汇总邮件条数/体积封顶（2026-08-27 审查修复 P2-2）：巨量账号全失败场景下
# 不封顶会生成超大 MIME 被 SMTP 拒收，整封告警丢失。截断部分指引看后台日志。
MAIL_SUMMARY_MAX_ENTRIES = 200
MAIL_SUMMARY_MAX_CHARS = 200_000


def _collect_admin_mail(subject, text):
    """把一条管理员告警并入任务结束汇总（不立即发送）。"""
    _mail_summary.append((subject, text))


def _alert_slow_sign(phone, dur, slow_sec, status, message, notify_url):
    """P6 耗时告警：单次尝试超阈值 → warning 日志 + 管理员汇总邮件 + 即时通知。

    堆队列与旧队列两个分支共用（2026-08-27 冗余合并），统一口径防漂移。
    """
    logger.warning(f"[{phone}] ⏱️ 签到耗时 {dur:.1f}s 超过阈值 {slow_sec}s（结果: {status}）")
    _collect_admin_mail(
        "易班签到耗时告警",
        f"账号: {_mask_phone(phone)}\n耗时: {dur:.1f}s（阈值 {slow_sec}s）\n结果: {_sanitize_text(message)}",
    )
    if notify_url:
        send_notification(
            "易班签到耗时告警",
            f"账号: {_mask_phone(phone)}\n耗时: {dur:.1f}s（阈值 {slow_sec}s）\n结果: {_sanitize_text(message)}",
            notify_url,
        )


def _flush_admin_mail_summary(phase=None):
    """签到任务结束：把运行期收集的管理员邮件汇总成一封发送。

    无异常则不发送（成功不打扰）；按主题分组，每个账号独立条目；
    条数超过 MAIL_SUMMARY_MAX_ENTRIES 或正文超长时截断并在尾部注明，
    明细以按天签到日志为准；mailer 内部静默失败，不影响退出码。发送后清空收集器。

    phase：任务阶段标签。定时签到缺省 None → 沿用「签到任务」文案；
    探针调用传「健康探测」，避免复用造成「并无当日签到却报签到结束」的误导
    （2026-08-27 审查 P3 修复）。
    """
    if not _mail_summary:
        return
    total = len(_mail_summary)
    entries = _mail_summary[:MAIL_SUMMARY_MAX_ENTRIES]
    truncated = total - len(entries)
    groups = {}
    order = []
    for subject, text in entries:
        if subject not in groups:
            groups[subject] = []
            order.append(subject)
        groups[subject].append(text)
    if phase:
        parts = [f"易班{phase}已完成，共 {total} 条异常/预警：\n"]
    else:
        parts = [f"易班签到任务已结束，共 {total} 条异常/预警：\n"]
    for subject in order:
        parts.append(f"【{subject}】")
        parts.extend(groups[subject])
        parts.append("")
    if truncated > 0:
        parts.append(
            f"（其余 {truncated} 条已截断以免邮件过大被拒收，"
            f"明细见管理后台「日志」页或 /var/log/yiban 按天日志）"
        )
    # 收件人 = ADMIN_TO（按个人开关过滤） + 所有开启接收的管理员用户邮箱：
    # 普通管理员自动获得告警收件权；关闭 mail_notify 后从收件人剔除。
    # 内置主管理员关闭 YIBAN_MAIL_ADMIN_NOTIFY 后不再收 ADMIN_TO 邮件。
    extra = mailer.admin_recipients() if mailer.admin_notify_enabled() else []
    recipients = db.admin_mail_recipients(extra)
    if recipients:
        body = "\n".join(parts).rstrip()
        if len(body) > MAIL_SUMMARY_MAX_CHARS:
            body = body[:MAIL_SUMMARY_MAX_CHARS].rstrip() + "\n…（超长截断，明细见日志）"
        mailer.send_admin_alert("易班签到汇总", body, to=",".join(recipients))
    _mail_summary.clear()


# B 线用户失败提醒每日限频（2026-08-27 审查修复 P2-1）：README/更新日志承诺
# 「每天每个账号最多 1 封」，原实现仅靠单次运行终态路径隐式保证——手动 --only
# 签到与探针进程可在同日追加发送。现以按天状态文件显式去重（0 或负数 = 不限）。
USER_FAIL_MAIL_DAILY_CAP = parse_env_int("YIBAN_MAIL_USER_FAIL_DAILY_CAP", 1)


def _user_fail_mail_state_path(today_str):
    state_dir = os.environ.get("YIBAN_STATE_DIR", "/var/log/yiban")
    return os.path.join(state_dir, f"mail-user-fail-{today_str}.json")


def _user_fail_mail_allow_and_record(phone, today_str):
    """检查该账号今日失败提醒额度：允许则占位并返回 True，超额返回 False。

    读-改-写整体持 M12 文件锁；跨进程（签到主进程 / 手动 --only / 探针）一致。
    文件按天命名自然轮转，无需清理历史。
    """
    cap = USER_FAIL_MAIL_DAILY_CAP
    if cap <= 0:
        return True
    path = _user_fail_mail_state_path(today_str)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with _state_file_lock(path):
            data = {}
            if os.path.exists(path):
                try:
                    with open(path, encoding="utf-8-sig") as f:
                        data = json.load(f)
                except (OSError, ValueError, TypeError):
                    data = {}
            if not isinstance(data, dict):
                data = {}
            used = int(data.get(phone, 0))
            if used >= cap:
                return False
            data[phone] = used + 1
            tmp = path + ".tmp" + str(os.getpid())
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, path)
        return True
    except OSError:
        # 状态目录不可写：退回不限频（不因限频设施故障吞掉真实失败告警）
        return True


def send_user_fail_mail(owner, phone, message, scenario="signin"):
    """B 线：向账号归属用户发送失败类提醒邮件。

    scenario="signin"（默认）：签到最终失败提醒（原行为，主题/正文不变）；
    scenario="probe"：健康探测发现账号异常——探测并无「当日签到」语义，
    沿用签到措辞会误导用户（2026-08-27 审查 P3）。

    仅当用户存在且开启 mail_notify（默认开）时发送；每账号每日上限
    USER_FAIL_MAIL_DAILY_CAP 封（默认 1，定时/手动/探针三个入口统一计算；
    发送成功才消耗额度，SMTP 故障不吞当日重试机会）；用户注销/关闭/未配置
    邮件时静默跳过；发送失败不影响签到（mailer 内部捕获）。
    内容脱敏：手机号打码、消息经 _sanitize_text 清洗（不含账号密码）。
    """
    if not owner:
        return
    try:
        user = db.find_user(owner)
    except Exception as e:
        # 留痕（2026-08-27 审查）：库瞬时故障时失败提醒被当"查无此人"静默跳过，
        # 恰是用户最需要触达的时刻；区别于用户不存在（find_user 正常返回 None，
        # 不走此分支）。打码手机号定位账号，不打印原始邮箱。
        logger.warning("查询账号 %s 的归属用户失败，本次失败提醒未发送: %s", _mask_phone(phone), e)
        user = None
    if not user:
        return
    if str(user.get("mail_notify", 1)).strip().lower() not in ("1", "true", "on", "yes"):
        return
    if not _user_fail_mail_allow_and_record(phone, datetime.now().strftime("%Y-%m-%d")):
        logger.info(
            "账号 %s 今日失败提醒已达上限（%d 封），跳过发送",
            _mask_phone(phone), USER_FAIL_MAIL_DAILY_CAP,
        )
        return
    if scenario == "probe":
        subject = "易班账号健康预警"
        body = (
            f"您的易班账号 {_mask_phone(phone)} 在系统例行健康检查中未能正常登录。\n"
            f"{_sanitize_text(message)}\n\n"
            f"这不影响已完成的签到；请尽快核对账号密码是否变更、或按提示处理验证问题，"
            f"避免下次签到失败。\n"
            f"（可在「我的账号」页面关闭本邮件提醒）"
        )
    else:
        subject = "易班签到失败提醒"
        body = (
            f"您的易班账号 {_mask_phone(phone)} 今日签到失败：\n{_sanitize_text(message)}\n\n"
            f"连续失败会被系统自动暂停；如账号正常，请登录网站检查或联系管理员。\n"
            f"（可在「我的账号」页面关闭本邮件提醒）"
        )
    mailer.send_user(owner, subject, body)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def verify_account(account):
    """只读健康检查（登录 + 拉取任务，不提交签到）。

    供注册时预处理验证（web 端）与探针模式（--probe）复用。
    返回 (ok, message)：ok=False 表示存在无法自愈的问题；message 已脱敏。
    """
    phone = account.phone
    try:
        client = YibanClient(account)
        try:
            if client.use_killyiban:
                client.login_killyiban()
            else:
                client.login()
            return client.verify()
        finally:
            client._wipe_credentials()
    except Exception as e:
        safe_err = _sanitize_text(str(e))
        logger.warning(f"[{phone}] 健康检查失败: {safe_err}", exc_info=False)
        return False, safe_err


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
        safe_err = _sanitize_text(str(e))
        logger.error(f"[{phone}] ❌ 尝试失败: {safe_err}", exc_info=False)
        # 逐次失败不通知（避免通知风暴），仅最终放弃时由 run_queue_retry 通知一次
        return False, safe_err, False, STATUS_FAILED


def _write_sign_state(phone, status, message, scheduled=None, dur=None):
    """写按日结构化状态文件（web/TUI 状态显示的事实源，原子替换防半截文件）。

    文件：{YIBAN_STATE_DIR}/sign-state-YYYY-MM-DD.json
    结构：{phone: {status, message, time, task}}；task 预留多时段/多星期签到扩展。
    scheduled：今日计划签到时间（HH:MM:SS，自动错峰分配后写入，执行后保留）。
    dur：单次签到尝试耗时秒数（P6，2026-08-16：慢响应可据此判断网络/接口问题）。
    状态目录不可写时丢弃，不影响签到执行。
    """
    state_dir = os.environ.get("YIBAN_STATE_DIR", "/var/log/yiban")
    path = os.path.join(state_dir, f"sign-state-{datetime.now().strftime('%Y-%m-%d')}.json")
    try:
        os.makedirs(state_dir, exist_ok=True)
        # M12：读-改-写整体持有状态文件锁，避免并发覆盖丢失条目
        with _state_file_lock(path):
            data = {}
            if os.path.exists(path):
                try:
                    with open(path, encoding="utf-8") as f:
                        data = json.load(f)
                except (OSError, ValueError, TypeError, AttributeError):
                    logger.warning("状态文件 %s 损坏，按空数据重建", path)
                    data = {}
            if not isinstance(data, dict):
                logger.warning("状态文件 %s 非 dict，按空数据重建", path)
                data = {}
            now = datetime.now()
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
    except (OSError, ValueError, TypeError, AttributeError) as e:
        # 状态目录不可写/写入异常时丢弃但不静默：debug 留痕（日志审查 D6，不影响签到执行）；
        # 异常消息经 _sanitize_text 脱敏（sqlite/json 异常可能回显 cookie/csrf 值，C-SIGN-02）
        logger.debug("写入状态文件失败（%s）: %s", path, _sanitize_text(e))


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
        # M12：删除/写入账密状态也持有状态文件锁，防止与 _write_sign_state 等并发写串扰
        with _state_file_lock(path):
            if not data:
                if os.path.exists(path):
                    os.remove(path)
                return
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = f"{path}.tmp{os.getpid()}"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, path)
    except (OSError, ValueError, TypeError, AttributeError) as e:
        logger.debug("写入账密状态失败（%s）: %s", path, _sanitize_text(e))


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

    blocks: [(lo_min, hi_min), ...]（浮点分钟，支持 0.5 分钟=30s 的裁剪粒度）；
    eff_lo/eff_hi：有效窗口分钟边界（相对当天 0:00），由前后裁剪分别决定。
    有效窗口为空（前裁+后裁 >= 窗口宽度）时回退默认窗口，保证调用方永不拿到空块列表。
    """
    start_min = cfg["sign_start"][0] * 60 + cfg["sign_start"][1]
    end_min = cfg["sign_end"][0] * 60 + cfg["sign_end"][1]
    front = cfg["edge_front_sec"] / 60.0
    back = cfg["edge_back_sec"] / 60.0
    eff_lo = start_min + front
    eff_hi = end_min - back
    if eff_hi <= eff_lo:
        logger.warning(
            "有效签到窗口为空（窗口 %s~%s、前裁 %ss 后裁 %ss），回退默认窗口 06:30~07:50",
            cfg["sign_start"], cfg["sign_end"], cfg["edge_front_sec"], cfg["edge_back_sec"],
        )
        start_min = _DEFAULT_SIGN_START[0] * 60 + _DEFAULT_SIGN_START[1]
        end_min = _DEFAULT_SIGN_END[0] * 60 + _DEFAULT_SIGN_END[1]
        front = back = _DEFAULT_EDGE_SEC / 60.0
        eff_lo = start_min + front
        eff_hi = end_min - back
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
    front = cfg["edge_front_sec"] / 60.0
    back = cfg["edge_back_sec"] / 60.0
    m = {}
    bi = 0
    for b in range(start_min, end_min, 5):
        lo = max(b, start_min + front)
        hi = min(b + 5, end_min - back)
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
            # 2026-08-28 审查 F4：原校验 `0 <= slot < span`（span = 有效窗口宽度），
            # 而 _slot_to_bi 的键范围是完整窗口——窗口长度非 5 分钟整数倍时
            # （如 06:30~07:52），末尾片在 Web 端可点选、此处却被判"落窗外"
            # 而静默回退自动分配。改以 _slot_to_bi 的成员性为准（与 Web 端
            # _pref_slots 同一套可用性判定）。
            if slot not in slot_to_bi:  # 片无效/落窗外 → 回退自动分配
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


def _window_closed(sch_cfg, now_dt):
    """签到窗口是否已关闭：统一按 eff_hi = sign_end - edge_back 判定（P5，2026-08-27）。

    与计划 horizon（_schedule_blocks 的 eff_hi）一致：首 pass 与重试同口径，
    消除原"首 pass 裸 sign_end / 重试 end-edge_back"两处不一致。
    """
    now_sec = now_dt.hour * 3600 + now_dt.minute * 60 + now_dt.second
    end_sec = sch_cfg["sign_end"][0] * 3600 + sch_cfg["sign_end"][1] * 60
    return now_sec > end_sec - sch_cfg["edge_back_sec"]


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


def run_queue_retry(accounts, notify_url, start_delay_max, gap_max, schedule=None, cred_state=None):
    """轮询队列 + 分散重试执行全部账号签到。

    流程（schedule 为空=原行为）：启动随机延迟 → 按签到模式（列表顺序 / 列表随机）
    确定执行顺序逐个尝试（账号间随机间隔）；失败的账号不立即重试，放入队尾等待下一轮；
    每账号总尝试次数受 classify_failure 分级控制（风控类最多 2 次，其他最多 4 次）；
    同一账号两次尝试间隔不小于 RETRY_MIN_INTERVAL 秒，避免连击。

    schedule 非空（自动错峰模式，调度 v2 时间驱动队列）：按 {phone: datetime} 时间点到点执行
    （已过点立即执行），不再叠加启动/账号间随机延迟；失败的账号经 _next_retry_at 重新采样到
    剩余有效窗口的偏早段后非阻塞重插（不再回队尾 + 阻塞等待），窗口不足时明确放弃；
    相邻请求间隔受 min_exec_gap / exec_gap_min 兜底；截止保护统一按 eff_hi（sign_end - edge_back）。

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

        _now0 = datetime.now()
        for _acc in accounts:
            _t = schedule.get(_acc.phone)
            _push(_acc, _t if _t and _t > _now0 else _now0)
        while pending:
            _at_dt, _seq_no, acc = heapq.heappop(pending)
            phone = acc.phone
            # M14：每次尝试（含重试）重算 today，跨午夜执行不沿用启动日
            today = datetime.now().strftime("%Y-%m-%d")
            now_dt = datetime.now()
            # 截止保护（P5，统一 eff_hi 口径）：窗口关闭 → 剩余账号全部跳过
            if _window_closed(sch_cfg, now_dt):
                results[phone] = (False, "签到时段已结束", True, STATUS_SKIPPED_WINDOW)
                _write_sign_state(phone, STATUS_SKIPPED_WINDOW, "签到时段已结束")
                logger.info(f"[{phone}] ⛔ 签到时段已结束，跳过执行")
                for _rest in pending:
                    _rp = _rest[2].phone
                    results[_rp] = (False, "签到时段已结束", True, STATUS_SKIPPED_WINDOW)
                    _write_sign_state(_rp, STATUS_SKIPPED_WINDOW, "签到时段已结束")
                break
            # 到点执行（已过点立即）；重试落点已由 _next_retry_at 采样
            wait = (_at_dt - now_dt).total_seconds()
            if wait > 0:
                time.sleep(wait)
            # 请求最小间隔兜底（F1）：min_exec_gap 与 exec_gap_min（过点账号）取较大值
            if last_done is not None:
                min_gap = max(
                    sch_cfg["min_exec_gap"],
                    sch_cfg["exec_gap_min"] if wait <= 0 else 0,
                )
                gap = min_gap - (time.monotonic() - last_done)
                if gap > 0:
                    logger.debug(f"[{phone}] 间隔对齐: 补 {int(gap)}s（最小 {min_gap}s）")
                    time.sleep(gap)
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
            dur = last_done - t0
            _write_sign_state(phone, status, message, dur=dur)
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
                next_probe = (datetime.strptime(today, "%Y-%m-%d") + timedelta(days=PROBE_INTERVAL_DAYS)).strftime("%Y-%m-%d")
                cred_state[phone]["probe_date"] = next_probe
                logger.warning(f"[{phone}] ⏸️ 半开试探失败，保持暂停（下次 {next_probe} 试探）")
            if success:
                results[phone] = (True, message, skip, status)
                logger.info(f"[{phone}] {STATUS_SYMBOL[status]} {message}")
                continue
            # 失败：跳过类不重试；其余按分级重试
            if skip:
                results[phone] = (False, message, True, status)
                logger.info(f"[{phone}] ⛔ {message}（不重试）")
                continue
            max_attempts = classify_failure(message)
            # 会话缓存联动（2026-08-22）：风控类失败（e003/WAF 等）清除该账号缓存（原样）
            if max_attempts == RISK_MAX_ATTEMPTS or "授权设备" in message:
                clear_session_cache_quiet(phone)
            if attempts[phone] >= max_attempts:
                results[phone] = (False, message, False, status)
                logger.error(f"[{phone}] ❌ 已尝试 {attempts[phone]} 次，放弃: {message}")
                _collect_admin_mail("易班签到失败", f"账号: {_mask_phone(phone)}\n原因: {_sanitize_text(message)}")
                if notify_url:
                    send_notification("易班签到失败", f"账号: {_mask_phone(phone)}\n原因: {_sanitize_text(message)}", notify_url)
                send_user_fail_mail(acc.owner, phone, message)
                continue
            # 重试落点（P1/P2/P3/P7）：窗口内重新采样，非阻塞重插；窗口不足 → 放弃（P5）
            nxt = _next_retry_at(datetime.now(), sch_cfg)
            if nxt is None:
                results[phone] = (False, message, False, status)
                logger.error(f"[{phone}] ❌ 窗口剩余不足，不再重试: {message}")
                _collect_admin_mail("易班签到失败", f"账号: {_mask_phone(phone)}\n原因: {_sanitize_text(message)}")
                if notify_url:
                    send_notification("易班签到失败", f"账号: {_mask_phone(phone)}\n原因: {_sanitize_text(message)}", notify_url)
                send_user_fail_mail(acc.owner, phone, message)
                continue
            _write_sign_state(phone, STATUS_RETRYING, f"待重试（已 {attempts[phone]} 次）")
            _push(acc, nxt)
            logger.warning(f"[{phone}] ⏳ 待重试（已 {attempts[phone]} 次，上限 {max_attempts} 次，{nxt.strftime('%H:%M:%S')} 再试）")
        return results

    while queue:
        acc = queue.pop(0)
        phone = acc.phone
        # M14：每次尝试（含重试）重算 today，跨午夜执行不沿用启动日
        today = datetime.now().strftime("%Y-%m-%d")
        # 首轮（第一个账号）不等待，后续每个账号（含重试回队）先打散账号间间隔；
        # 标记在 pop 之后无条件置 False（原实现只在失败分支置 False，
        # 导致全成功路径账号间隔打散失效）
        if not first_round:
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
        # 会话缓存联动（2026-08-22）：风控类失败（e003/WAF 等）清除该账号缓存，
        # 避免下次签到复用已被服务端标记的会话；"授权设备"非风险关键词（属设备
        # 配置问题仍可重试），但同样意味着当前会话不可信，单列一并清除
        if max_attempts == RISK_MAX_ATTEMPTS or "授权设备" in message:
            clear_session_cache_quiet(phone)
        if attempts[phone] >= max_attempts:
            results[phone] = (False, message, False, status)
            logger.error(f"[{phone}] ❌ 已尝试 {attempts[phone]} 次，放弃: {message}")
            # A 线合并：失败并入任务结束汇总邮件（webhook 仍即时推送）
            _collect_admin_mail(
                "易班签到失败",
                f"账号: {_mask_phone(phone)}\n原因: {_sanitize_text(message)}",
            )
            if notify_url:
                send_notification(
                    "易班签到失败", f"账号: {_mask_phone(phone)}\n原因: {_sanitize_text(message)}", notify_url
                )
            # B 线：向账号归属用户发失败提醒（未开启/未绑定用户则静默跳过）
            send_user_fail_mail(acc.owner, phone, message)
            continue

        # 放回队尾：单次 sleep 保证总间隔 ≥ retry_min_interval，
        # 随机部分只用于打散，不允许把最小间隔缩水
        _write_sign_state(phone, STATUS_RETRYING, f"待重试（已 {attempts[phone]} 次）")
        retry_min_interval = RETRY_MIN_INTERVAL
        wait = max(retry_min_interval, retry_min_interval - gap_max + random.uniform(0, RETRY_GAP_MAX))
        logger.debug(f"[{phone}] 重试前等待 {wait:.1f}s（最小 {retry_min_interval}s）")
        time.sleep(wait)
        queue.append(acc)
        logger.warning(f"[{phone}] ⏳ 待重试（已 {attempts[phone]} 次，上限 {max_attempts} 次）")

    return results


# ---------------------------------------------------------------------------
# 探针模式（健康探测，2026-08-25）
# ---------------------------------------------------------------------------
def _probe_state_path():
    """探针最近执行日状态文件（由探针进程独占维护，避免频繁写 .env）。"""
    state_dir = os.environ.get("YIBAN_STATE_DIR", "/var/log/yiban")
    return os.path.join(state_dir, "probe-state.json")


def _read_probe_state():
    try:
        # utf-8-sig：容错 Windows 手工编辑留下的 BOM（对齐 _load_cred_state，2026-08-27 审查修复）
        with open(_probe_state_path(), encoding="utf-8-sig") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _write_probe_state(state):
    try:
        path = _probe_state_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp" + str(os.getpid())
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        logger.warning("探针状态文件不可写（不影响本次探测）")


def _update_probe_state_run(today_str):
    """记录探针当日已执行（读-改-写整体持 M12 文件锁，防并发覆盖丢写入）。

    2026-08-27 对抗性审查修复：原实现裸读写，与其它状态文件口径不一致。
    """
    with _state_file_lock(_probe_state_path()):
        state = _read_probe_state()
        state["last_run"] = today_str
        _write_probe_state(state)


def _health_probe_due(now=None):
    """是否应在本次入口执行健康探针：开启 + 已达触发时间 + 满足频率（once=下一次单次）。"""
    now = now or datetime.now()
    if not PROBE_ENABLE:
        return False
    try:
        hh, mm = (int(x) for x in PROBE_TIME.split(":"))
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            return False
    except (TypeError, ValueError):
        return False
    if (now.hour, now.minute) < (hh, mm):
        return False
    state = _read_probe_state()
    last = state.get("last_run", "")
    today = now.strftime("%Y-%m-%d")
    if last == today:
        return False
    interval = PROBE_INTERVAL.strip().lower()
    if interval == "once":
        # 单次模式：开启且已到时间且今天未执行 → 本次执行（执行后自动关闭）
        return True
    try:
        n = int(interval)
        if n <= 0:
            return False
    except (TypeError, ValueError):
        return False
    if not last:
        return True
    try:
        last_dt = datetime.strptime(last, "%Y-%m-%d")
    except ValueError:
        return True
    return (now.date() - last_dt.date()).days >= n


def _env_update_probe(auto_disable=False):
    """探针执行后更新 .env：once 模式自动关闭 YIBAN_PROBE_ENABLE（跨进程写锁）。

    仅在 once 单次执行后调用；失败只记日志，不影响本次探测结果。
    """
    if not auto_disable:
        return
    env_path = os.environ.get("YIBAN_ENV_FILE", "").strip() or ".env"
    try:
        with env_lock.env_write_lock(env_path):
            lines = []
            if os.path.exists(env_path):
                with open(env_path, encoding="utf-8-sig") as f:
                    lines = f.read().splitlines()
            out = [ln for ln in lines if not ln.strip().startswith("YIBAN_PROBE_ENABLE=")]
            out.append("YIBAN_PROBE_ENABLE=0")
            tmp = env_path + ".tmp" + str(os.getpid())
            with open(tmp, "w", encoding="utf-8") as f:
                f.write("\n".join(out) + "\n")
            os.replace(tmp, env_path)
            try:
                os.chmod(env_path, 0o600)
            except OSError:
                pass
    except Exception as e:
        # 升级为 ERROR（2026-08-27 审查）：once 自动关闭失败会让"单次探针"事实变成
        # 每晚全量探测（反复真实登录扩大风控面 + 每日重复告警）；选了 once 的运维
        # 不会回来盯日志，必须醒目留痕提示手动关闭。
        logger.error(
            "探针 once 自动关闭失败，YIBAN_PROBE_ENABLE 仍为开启——单次探针将变成每日重复执行，请手动关闭: %s",
            _sanitize_text(str(e)),
        )


def run_probe(accounts):
    """探针模式主流程：对全部账号做只读健康检查。

    - 未到触发时间/频率（或未开启）则直接返回（零请求）。
    - 结果写入 sign_events（stage=probe，复用 db 写锁 _conn_lock，天然并发安全），
      时间戳为当前时刻，追加在最近签到日志之后。
    - 无法自愈问题：管理员合并预警邮件（复用 A 线 _collect/_flush）+ 对应用户个人
      预警（复用 B 线 send_user_fail_mail，尊重用户开关）。
    - 执行后更新 last_run；once 模式自动关闭探针（.env 写锁）。
    """
    if not PROBE_ENABLE:
        # 探针关闭：完全静默退出（不产生任何日志、不落库、不写状态）
        return
    if not _health_probe_due():
        logger.info("==== 探针模式：已开启，但未到触发时间/频率，本次跳过 ====")
        return
    logger.info(f"==== 探针模式：对 {len(accounts)} 个账号进行健康检查 ====")
    now = datetime.now()
    ts = now.strftime("%Y-%m-%d %H:%M:%S")
    hard_fail = []  # [(Account, message)]
    healthy_n = 0
    soft_fail_n = 0
    for acc in accounts:
        ok, message = verify_account(acc)
        hard = (not ok) and bool(PROBE_HARD_FAIL_RE.search(message or ""))
        # 落库：stage=probe（复用 db.add_sign_event，内部 _conn_lock 并发保护）
        try:
            db.add_sign_event(
                ts, acc.phone, "failed" if hard else "success",
                _sanitize_text(message), stage="probe",
            )
        except Exception as e:
            logger.debug("探针日志写入失败（不影响探测）: %s", e)
        if hard:
            hard_fail.append((acc, message))
        elif ok:
            healthy_n += 1
        else:
            # 网络类失败（超时/DNS 等）：不含硬失败特征、通常可自愈，不计入预警，
            # 但必须与「确认健康」区分留痕——否则探针自身故障会被误读为全员健康
            # （2026-08-27 审查修复 P2-8）
            soft_fail_n += 1
            logger.info(
                "探针：账号 %s 网络类失败（不计预警）：%s",
                _mask_phone(acc.phone), _sanitize_text(message),
            )
    # 预警（复用 A/B 线邮件机制；用户邮件按「健康探测」措辞，避免误报为当日签到失败）
    for acc, message in hard_fail:
        _collect_admin_mail(
            "健康探测预警",
            f"账号: {_mask_phone(acc.phone)}\n原因: {_sanitize_text(message)}",
        )
        send_user_fail_mail(acc.owner, acc.phone, message, scenario="probe")
    if soft_fail_n and not hard_fail:
        # 无硬失败时单独提示，避免管理员把「零预警」误读为「全员可用」
        _collect_admin_mail(
            "健康探测提示",
            f"{soft_fail_n} 个账号在探测期间出现网络类失败"
            f"（超时/连接异常等，通常可自愈），未计入预警。",
        )
    _flush_admin_mail_summary(phase="健康探测")
    # 记录执行（last_run 写状态文件；once 自动关闭）
    _update_probe_state_run(now.strftime("%Y-%m-%d"))
    if PROBE_INTERVAL.strip().lower() == "once":
        _env_update_probe(auto_disable=True)
        logger.info("==== 探针模式（单次）执行完成，已自动关闭探针 ====")
    logger.info(
        f"==== 探针模式完成：健康 {healthy_n}，网络类失败 {soft_fail_n}，预警 {len(hard_fail)} ===="
    )


def main():
    """主函数：加载账号配置并执行签到。

    支持：
    - 数据库 yiban.db（SQLite，web 后台 / TUI 配置工具写入）与 YIBAN_ACCOUNTS_JSON
    - 旧格式 YIBAN_ACCOUNTS 或 YIBAN_PHONE/YIBAN_PASSWORD（向后兼容）
    - 队列重试：失败账号分散重试——开启签到调度时重新安排到窗口内合适时间，否则放回队尾（分级上限）
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
    parser.add_argument(
        "--probe", action="store_true",
        help="探针模式：非签到时段对全部账号做只读健康检查（需 .env 开启且到触发时间/频率）",
    )
    args = parser.parse_args()

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

    # 全局暂停（管理员 Web UI 一键暂停）：下一轮生效，当前进程照常跑完。
    # 手动签到（--only）不受限——用户主动触发应当放行（与周日开关语义一致）。
    # YIBAN_GLOBAL_PAUSE 由 .env 写入，run.sh 加载后经环境变量传入。
    if not args.only and str(os.environ.get("YIBAN_GLOBAL_PAUSE", "")).strip().lower() in ("1", "true", "on", "yes"):
        logger.info("==== 签到已暂停（管理员通过 Web UI 一键暂停），跳过执行 ====")
        sys.exit(2)  # SKIPPED 语义：run.sh 写 SKIPPED 状态，恢复后次日正常执行

    # 进程级单实例锁（2026-08-20 对抗性审查 P2）：防 cron 全量队列与手动 --only
    # 并发签到同一账号。--only 被持有 → 留痕退出；全量被持有 → 等待至多
    # YIBAN_RUN_LOCK_WAIT 秒后继续（不因手动签到阻塞而漏签一整天）。
    try:
        _run_lock_fh = _acquire_run_lock(bool(args.only))
    except _RunLockHeld:
        logger.warning("已有签到进程在运行，本次手动签到跳过（防同账号并发，稍后可重试）")
        sys.exit(0)

    logger.info(f"==== 开始执行签到，共 {len(accounts)} 个账号，队列重试模式 ====")
    # 状态文件以"尝试开始时刻"的日期命名（防跨午夜执行写错当天）
    attempt_date = datetime.now().strftime("%Y-%m-%d")
    # 自动错峰（仅自动签到；--only 手动签到立即执行，不走计划）
    schedule = {} if args.only else build_schedule(accounts)
    if schedule:
        # 容量预检（调度 v2 第三层）：n × 平均耗时 > 有效窗口秒数 → 告警不静默
        # 用户自暂停账号不参与调度，也不计入容量
        _cfg = _schedule_config()
        _span_min = (
            (_cfg["sign_end"][0] * 60 + _cfg["sign_end"][1])
            - (_cfg["sign_start"][0] * 60 + _cfg["sign_start"][1])
            - (_cfg["edge_front_sec"] + _cfg["edge_back_sec"]) / 60.0
        )
        active_n = sum(1 for a in accounts if not getattr(a, "user_paused", False))
        if active_n * _cfg["avg_attempt_sec"] > _span_min * 60:
            logger.warning(
                "容量预检: %d 个账号 × 平均 %ds > 有效窗口 %d 秒，部分账号可能无法在窗口内完成",
                active_n, _cfg["avg_attempt_sec"], _span_min * 60,
            )
            # 超载提醒（对抗性审查补）：通知管理员，避免"超限只在日志里"无人知情。
            # A 线合并：并入任务结束汇总邮件；webhook 仍即时推送。
            _collect_admin_mail(
                "易班签到容量超载",
                f"当前 {active_n} 个账号 × 平均 {_cfg['avg_attempt_sec']}s "
                f"> 有效窗口 {_span_min * 60}s，部分账号可能无法在窗口内完成签到。\n"
                f"建议：增加窗口时长或减少账号数量（.env 调整）。",
            )
            send_notification(
                "易班签到容量超载",
                f"当前 {active_n} 个账号 × 平均 {_cfg['avg_attempt_sec']}s "
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
    # 2026-08-20 对抗性审查修复（P1）：--only 此前无条件以本次（仅含目标账号的）状态
    # 整体覆盖保存——空 dict 时直接删除状态文件，其他账号的 fail_days/paused_since
    # 全部丢失，账密熔断保护被任意一次手动签到全局重置。现改为：--only 只把本次
    # 处理账号的熔断增量合并回存量状态（成功→清除该账号记录；凭据失败→按日累计；
    # 其他失败→不动），未处理账号保持原状。全量模式语义不变（本轮本就基于存量计算）。
    if args.only:
        merged = _load_cred_state()
        _merge_today = datetime.now().strftime("%Y-%m-%d")
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
        _save_cred_state(merged)
    else:
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
        # M14：汇总文件以写盘时日期命名（跨午夜不沿用启动时的 attempt_date）
        daily_path = os.path.join(state_dir, f"sign-daily-{datetime.now().strftime('%Y-%m-%d')}.json")
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
                if status in (STATUS_SUCCESS, STATUS_ALREADY, STATUS_NO_TASK, STATUS_FAILED):
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
