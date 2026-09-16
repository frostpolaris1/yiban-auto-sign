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
"""

import argparse
import contextlib
import json
import logging
import math
import os
import random
import re
import secrets
import signal
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta

# 包导入引导：`yiban/` 在仓库根，而直接运行本脚本时 sys.path[0] 是 scripts/。这是
# **过渡机制**——M3 起转为兼容壳（`python -m yiban.cli`），届时随"清 sys.path 注入"移除。
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# 共享模块（同目录）：加密（与 web 共用密钥与密文格式）与 SQLite 数据访问层
import db  # noqa: E402  # 2026-08-16 审查轮：原 _load_accounts_from_file/build_schedule 函数内 import 上移（无循环依赖）
import mailer  # noqa: E402  # A 线：管理员告警邮件 / B 线：用户签到失败邮件（SMTP，零依赖；不配置则不启用）
import notify  # noqa: E402  # Webhook 推送组件（Server酱/自定义 URL，加密配置+节流+响应检查）

from yiban import client as yiban_client  # noqa: E402  # 客户端外观 YibanClient
from yiban import (  # noqa: E402
    clock,
    cred_state,
    egress,  # 出口（代理）分配：每个执行体可独立配置
    security,  # 白名单 / WAF 判定口径（唯一实现）
    window,
)
from yiban import status as yiban_status  # noqa: E402
from yiban.fyiban import algo as fyiban_algo  # noqa: E402
from yiban.fyiban import headers as fyiban_headers  # noqa: E402
from yiban.infra import (  # noqa: E402
    account_crypto,
    env_lock,  # 探针 once 模式自动关闭 .env（跨进程写锁）
    locks,  # 跨进程文件锁统一原语（状态文件 / 日志 handler）
)
from yiban.logging_ext import FlockFileHandler  # noqa: E402
from yiban.masking import mask_phone as _mask_phone  # noqa: E402
from yiban.masking import sanitize_text as _sanitize_text  # noqa: E402
from yiban.masking import sanitize_url as _sanitize_url  # noqa: E402
from yiban.store import accounts as accounts_store  # noqa: E402  # 账号运行期复核

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


@contextmanager
def _state_file_lock(path):
    """状态文件读改写锁：经 `locks.file_lock` 统一（POSIX flock / Windows msvcrt）。

    锁文件由 locks 自行拼 `<path>.lock`，不要与日志 handler 的锁混用同一把。
    此前 Windows 上直接退化为 no-op 且**无任何告警**，5 处调用点的读-改-写因此
    失去原子性（可丢 cred-state 熔断暂停、丢按日状态）；现由统一原语保证，
    真无法加锁时也会告警留痕。
    """
    with locks.file_lock(path):
        yield


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
    # 多执行体形态下每个子进程用**各自的**锁文件（YIBAN_RUN_LOCK_NAME），全局锁由
    # 拉起它们的监督进程持有：这样既保住"散落的另一轮全量不得与本轮并发"的原有保护，
    # 又不让子进程之间互相阻塞。
    lock_name = os.environ.get("YIBAN_RUN_LOCK_NAME", "").strip() or "signin-run.lock"
    try:
        os.makedirs(state_dir, exist_ok=True)
        fh = open(os.path.join(state_dir, lock_name), "a+", encoding="utf-8")
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
    date_str = clock.now().strftime("%Y-%m-%d")
    log_file = os.environ.get("YIBAN_LOG_FILE", "/var/log/yiban/sign.log")
    return os.path.join(os.path.dirname(log_file), f"sign-{date_str}.log")


def _make_log_handler():
    """创建日志处理器：目录不存在时尝试创建，仍失败则降级 stderr（不阻断签到执行）。"""
    path = _signin_log_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        return FlockFileHandler(path, encoding="utf-8")
    except OSError:
        # 目录不可写/不存在：降级到 stderr（保持原始行为，签到不因日志中断）
        return logging.StreamHandler()


# CLI 日志装配幂等标记。原实现把 handler 装配放在模块导入期
# （logging.basicConfig(handlers=[_handler])）——web/app.py 导入 signin 时即向 root
# 挂 FlockFileHandler，create_app 随后再挂 DailyFlockFileHandler（其去重守卫只认
# 自身类），root 上出现两个指向同一日志目录的 FileHandler，每条日志写两遍。
_cli_logging_ready = False


def _setup_cli_logging():
    """CLI 入口日志装配：把按天文件 handler 挂到 root logger。

    装配从模块导入期延迟到 main() 入口（--check-config / --probe / --only 均经
    main()，覆盖全部 CLI 路径）。模块导入自此零副作用：web 进程 import signin 不再向 root
    挂 handler，双写症状（每条日志落盘两遍）消除；幂等保护重复调用不重复挂载。
    """
    global _cli_logging_ready
    if _cli_logging_ready:
        return
    _cli_logging_ready = True
    handler = _make_log_handler()
    handler.setFormatter(logging.Formatter(
        "[%(asctime)s] [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL, logging.INFO),
        handlers=[handler],
    )


logger = logging.getLogger("yiban")


# 易班 App 请求头与版本特征：**衍生自上游 FYIBAN**，已迁到第三方隔离层
# （yiban/fyiban/headers.py，来源与差异见 yiban/fyiban/PROVENANCE.md）。
# 本模块继续以同名引用，调用方无需改动。
YIBAN_APP_VERSION = fyiban_headers.YIBAN_APP_VERSION
HEADERS = fyiban_headers.HEADERS
KILLYIBAN_HEADERS = fyiban_headers.KILLYIBAN_HEADERS


# ---------------------------------------------------------------------------
# 账号数据模型
# ---------------------------------------------------------------------------
@dataclass
class Account:
    """单个易班账号配置。

    通过 Web 管理后台添加（存于 SQLite 数据库），
    一次输入一个账号的完整信息，无需用符号分隔。
    """

    phone: str
    # C-SIGN-04 已知局限：str 不可变无法原位清零，且重试队列需跨尝试复用，
    # 密码 str 本体只能随 accounts 列表生命周期存活（客户端侧可变副本见
    # YibanClient._wipe_credentials 的清零与局限说明）
    password: str
    phone_model: str = ""  # 设备型号（学校开启"设备绑定"时必填）
    phone_code: str = ""  # 设备唯一识别码（学校开启"设备绑定"时必填）
    name: str = ""  # 自定义名称（未填写时显示为"账号N"）
    user_paused: bool = False  # 用户自暂停签到（调度 v2；db.load_accounts 透传）
    owner: str = ""  # 账号归属用户邮箱（B 线：签到失败时向 owner 发提醒邮件；JSON/legacy 来源为空）
    # 库内账号行 id（db 来源才有，JSON/环境变量来源为 0）：运行期复核账号是否仍有效用
    account_id: int = 0

    @property
    def has_device_info(self):
        return bool(self.phone_model and self.phone_code)


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
# 容量预检与容量预估共用的单账号耗时估算（秒）。缺省按压测实测定档：
# 单账号（登录链 + 签到链共 6 次请求）实测 0.08s（零延迟）、1.87s（拟真 300ms）、
# 3.1s（含尾延迟）。旧缺省 8s 无实测依据，把可容纳账号数低估约 2.6 倍，
# 并使保存门误拒 261~360 个账号的站点；真实网络更慢时由 YIBAN_AVG_ATTEMPT_SEC 覆盖。
_DEFAULT_AVG_ATTEMPT_SEC = 3
_DEFAULT_RETRY_MIN_INTERVAL = 60
_DEFAULT_EXEC_GAP_MIN = 10      # 启动对齐：已过点账号相邻最小间隔（秒）
_DEFAULT_ALLOW_TIME_PREF = 0    # 用户自选时间片总开关（0=关默认，管理员开启后生效）
_DEFAULT_SLOW_SIGN_SEC = 30     # P6 耗时告警阈值（秒）：单次尝试耗时超此值 → warning + 通知

# 签到窗口配置异常的一次性告警标记（2026-08-28 审查 F3）：
# _schedule_config 每次调度都会调用，非法窗口回退默认窗口的告警只收集一次，
# 避免同一个配置错误在每日汇总邮件里重复出现 N 次
_invalid_window_notified = False

# 有效签到窗口为空的一次性告警标记：
# _schedule_blocks 每次调度都会调用（多账号/多轮），前后裁剪吃满窗口回退默认
# 窗口的邮件告警同样只收集一次（镜像上方 F3 去重模式），防汇总邮件刷屏
_edge_empty_window_notified = False


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


def avg_attempt_sec():
    """单账号签到耗时估算（秒）：YIBAN_AVG_ATTEMPT_SEC 显式配置优先，缺省 3s。"""
    return _env_int("YIBAN_AVG_ATTEMPT_SEC", _DEFAULT_AVG_ATTEMPT_SEC, 1, 300)


def capacity_accounts(window_sec, gap=0, avg=None):
    """有效窗口内可容纳的账号数（容量口径唯一源：引擎预检与 web 容量预估共用）。

    模型：首个账号立刻占用 avg 秒，此后每个账号按「上一次完成 + 间隔下限」推进，
    相邻账号墙钟间隔 = avg + gap（实测印证：gap=10、t=1.87s → 单账号周期 11.875s）。
    故 容量 = floor((窗口 − avg) ÷ (avg + gap)) + 1；窗口容不下单账号耗时为 0。

    window_sec：有效窗口秒数（已扣掐头去尾）
    gap：账号间隔下限秒（YIBAN_ACCOUNT_GAP_MAX）
    avg：单账号耗时秒；缺省取 avg_attempt_sec()
    """
    if avg is None:
        avg = avg_attempt_sec()
    avg = max(1, int(avg))
    gap = max(0, int(gap or 0))
    slack = int(window_sec) - avg
    if slack < 0:
        return 0
    return slack // (avg + gap) + 1


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
    # 窗口与前后裁剪的解析委托 yiban.window（排计划/判关闭/算容量同源）；告警仍在此处发
    start, end, _win_invalid = window.parse_window(os.environ)
    if _win_invalid:
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
    # 掐头去尾（0.22.0 起前后独立，秒级，0.5 分钟=30s 粒度；UI 按 0.5 分钟步进）：
    # 新键 YIBAN_WINDOW_EDGE_FRONT_SEC / _BACK_SEC 优先；旧键 YIBAN_WINDOW_EDGE_SEC
    # 存在时映射为前后对称（保证旧配置行为不变）——解析在 yiban.window.parse_edges。
    edge_front, edge_back = window.parse_edges(os.environ)
    return {
        "order": order,
        "dist": dist,
        "edge_front_sec": edge_front,
        "edge_back_sec": edge_back,
        "block_cap": _env_int("YIBAN_BLOCK_CAP", _DEFAULT_BLOCK_CAP, 1, 200),
        "mu_min_pct": mu_lo,
        "mu_max_pct": mu_hi,
        "sigma_min_pct": sigma_lo,
        "sigma_max_pct": sigma_hi,
        "min_exec_gap": _env_int("YIBAN_MIN_EXEC_GAP", _DEFAULT_MIN_EXEC_GAP, 1, 60),
        "avg_attempt_sec": avg_attempt_sec(),
        "retry_min_interval": _env_int("YIBAN_RETRY_MIN_INTERVAL", _DEFAULT_RETRY_MIN_INTERVAL, 1, 600),
        "exec_gap_min": _env_int("YIBAN_EXEC_GAP_MIN", _DEFAULT_EXEC_GAP_MIN, 0, 300),
        "allow_time_pref": _env_int("YIBAN_ALLOW_TIME_PREF", _DEFAULT_ALLOW_TIME_PREF, 0, 1),
        "sign_start": start,
        "sign_end": end,
    }

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
# 账号配置加载
# ---------------------------------------------------------------------------
def _key_env_file():
    """密钥来源 .env 路径：YIBAN_ENV_FILE 优先（与 web 子进程约定一致），回退默认 .env。

    2026-08-21 对抗性审查修复：此前 web 为保证自定义 .env 路径下子进程能解密，
    把 YIBAN_ACCOUNTS_KEY 明文注入子进程环境变量（同 uid 进程可读 /proc/<pid>/environ，
    密钥暴露面扩大）。现统一改为传递【路径】而非密钥本身，本函数即子进程侧的解析入口。
    """
    return os.environ.get("YIBAN_ENV_FILE", "").strip() or None


def _parse_account_dict(data):
    """将账号 JSON 对象解析为 Account，校验必填字段。

    password/phone_code 支持 AES-GCM 密文对象（web 存储层加密落盘，
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
        # 库内账号行 id（运行期复核账号是否仍有效用；JSON/legacy 来源无此字段 → 0）
        account_id=int(data.get("id") or 0),
    )


def _load_accounts_from_file():
    """从数据库加载（yiban.db，SQLite；web 后台写入，单行事务防并发覆盖）。

    db 层返回已解密明文；此处只做审核状态过滤。
    """
    db.init_db(env_file=_key_env_file(), cleanup=False)
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
            # 清洗后落日志：防畸形片段换行/回车注入（日志审查 P7）；片段缺 ":" 时
            # 常见是裸手机号（漏输密码），纯数字片段按 11 位号脱敏后落盘
            logger.error(
                f"账号配置格式错误（应为 phone:password）: "
                f"{_sanitize_text(_mask_phone(item) if item.isdigit() else item)}")
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


def _dedupe_by_phone(accounts):
    """同一手机号重复出现时只保留第一条并告警（返回新列表）。

    调度、重试预算、汇总与状态文件全以手机号为键：重复项会让同一账号被完整登录
    两次，且两次尝试共享同一份重试计数（预算错乱）。库内模式由 accounts.phone 的
    唯一索引天然兜底，但 JSON / 环境变量配置模式此前没有任何校验。
    """
    seen, kept, dup = set(), [], []
    for acc in accounts:
        if acc.phone in seen:
            dup.append(acc.phone)
            continue
        seen.add(acc.phone)
        kept.append(acc)
    if dup:
        logger.warning(
            "账号配置存在重复手机号 %d 个（已按首次出现去重）：%s",
            len(dup), ", ".join(_mask_phone(p) for p in dup),
        )
    return kept


def load_accounts():
    """按优先级加载账号配置：文件 > JSON 环境变量 > 旧格式环境变量（按手机号去重）。"""
    for loader in (
        _load_accounts_from_file,
        _load_accounts_from_json_env,
        _load_accounts_from_legacy_env,
    ):
        accounts = loader()
        if accounts:
            return _dedupe_by_phone(_apply_global_device_info(accounts))
    return []


def parse_env_int(name, default):
    """读取非负整数环境变量：缺失/非法回退默认值，负值归零。"""
    try:
        return max(0, int(os.environ.get(name, "").strip()))
    except (TypeError, ValueError):
        return default


def print_config_summary(accounts):
    """打印账号配置摘要（手机号与密码脱敏），不发任何网络请求。

    手机号必须打码：本摘要会落在 CI 日志、终端记录与他人可读的会话里（运维常贴到
    群里排查），完整号码属个人信息。脱敏后仍可区分账号（138****8000）。设备识别码
    只报"已配置"、不打印任何前缀（防摘要泄露设备指纹），型号按原值展示便于排查。
    """
    print("==== 账号配置检查 ====")
    for i, acc in enumerate(accounts, 1):
        if acc.has_device_info:
            device = f"设备: {acc.phone_model} / 识别码已配置"
        else:
            device = "设备: 未配置（如学校开启设备绑定，签到将失败）"
        print(f"  {i}. {_mask_phone(acc.phone)} | 密码: {'*' * 8} | {device}")
    print(f"共 {len(accounts)} 个账号，配置检查通过。")


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


def send_notification(title, content, url=None, urgent=False, force=False):
    """通过 Webhook 推送组件发送通知（Server酱/自定义 URL，见 scripts/notify.py）。

    2026-08-29 组件化：Server酱适配（title+desp）、同类型告警节流、服务端响应
    检查（配额/限频可见）、自定义 URL SSRF 白名单；兼容旧明文 YIBAN_NOTIFY_URL
    （notify.get_secret 回退，url 参数与组件配置等价，由组件统一处理）。
    透传 urgent/force 到 notify.send——汇总邮件发送失败降级
    webhook 时以 urgent=True + force=True 调用（绕过节流与当日额度，保证兜底必达）；
    默认 False，既有调用方行为不变。
    说明：签到脚本给管理员的**邮件**不在此处发送（避免逐条轰炸），而是由
    各触发点 _collect_admin_mail 收集、任务结束 _flush_admin_mail_summary 汇总。
    """
    try:
        notify.send(title, content, urgent=urgent, force=force)
    except Exception as e:
        # 组件异常不得拖累签到主流程；只记类型名（异常文本可能含 URL/token）
        logger.warning("通知推送组件调用失败: %s", type(e).__name__)


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
    if notify.is_configured():
        send_notification(
            "易班签到耗时告警",
            f"账号: {_mask_phone(phone)}\n耗时: {dur:.1f}s（阈值 {slow_sec}s）\n结果: {_sanitize_text(message)}",
            notify_url,
        )


def _sched_marker_exists():
    """当日全量运行标记（sched-run-<date>.json）是否已存在。

    告警函数用它区分「首签轮」与「补签轮」——
    标记在首签轮收尾写入（_write_sched_done），因此：
      - 首签轮调用本函数时标记尚不存在 → 本轮是首签；
      - 补签轮（07:10）调用时标记已存在 → 本轮是补签。
    与容器 scheduler.py 的 _full_run_done_today() 语义一致（同一事实源）。
    """
    state_dir = os.environ.get("YIBAN_STATE_DIR", "/var/log/yiban")
    path = os.path.join(state_dir, f"sched-run-{clock.now().strftime('%Y-%m-%d')}.json")
    return os.path.exists(path)


def _is_second_run():
    """本轮是否为补签轮（07:10）：run.sh 补签轮 / 容器 scheduler SECOND 时段注入的
    YIBAN_SECOND_RUN=1 优先，sched-run 标记兜底。

    首签子进程被宿主 timeout 击杀（exit 124）时收尾未执行、sched-run
    标记不写，07:10 补签轮仅靠标记会误判为首签轮 → 部分成功+窗口外零告警（B12-2
    分支复发）。环境变量由 run.sh 补签轮分支 / 容器 scheduler SECOND 时段显式注入，
    不依赖首签收尾，天然免疫 exit 124。
    """
    return os.environ.get("YIBAN_SECOND_RUN") == "1" or _sched_marker_exists()


def _sign_state_path():
    """当日 sign-state 状态文件路径（状态目录缺失/不可写由调用方处理）。"""
    state_dir = os.environ.get("YIBAN_STATE_DIR", "/var/log/yiban")
    return os.path.join(state_dir, f"sign-state-{clock.now().strftime('%Y-%m-%d')}.json")


def _daily_statuses():
    """当日按日状态文件的 {phone: status}；缺失/损坏返回 {}（调用方按"无记录"处理）。"""
    try:
        with open(_sign_state_path(), encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {p: (v.get("status") if isinstance(v, dict) else "") for p, v in data.items()}


def _second_run_drop_done(accounts):
    """补签轮定向重跑：剔除当日已了结（success/already）的账号。

    依据当日 sign-state 状态文件（与容器调度器 _has_undone_today 同一事实源）：
    存在未了结账号才触发的补签轮此前会整站重跑，把当日已 success 的账号
    再次完整登录（风控暴露）。文件缺失/损坏时按「无记录」处理返回全量
    （宁可多跑，不可漏签）。
    """
    recorded = _daily_statuses()
    if not recorded:
        return accounts
    # 已了结 = success/already/**no_task**（按 main 自身的"已执行"口径：no_task 指
    # "今天没任务"，同样无需重跑）。原实现漏了 no_task，补签轮会对这些账号再走一遍
    # 完整登录——多一轮全站真实登录，且与 UNDONE_STATUSES 口径矛盾。
    done = {
        p for p, st in recorded.items()
        if st in (STATUS_SUCCESS, STATUS_ALREADY, STATUS_NO_TASK)
    }
    if not done:
        return accounts
    kept = [a for a in accounts if a.phone not in done]
    logger.info(
        "补签轮定向重跑：%d 个账号当日已了结不再重跑，本次执行 %d 个",
        len(accounts) - len(kept), len(kept),
    )
    return kept


# 当天最后一轮触发点（= 补签轮时刻）：唯一事实源在 yiban.window.retry_hm()
# （宿主 run.sh 补签 cron 用同一键配置；容器形态由 scheduler 把它的实际触发点
# 注入子进程环境）。**不在此处缓存常量**——见 retry_hm 的 docstring。


def _maybe_alert_zero_success(accounts, results, ok_n, is_second_run=None):
    """窗口外未了结账号的管理员告警。

    场景：学校签到窗口晚于本地配置（或 Range 延迟放出），账号落
    skipped_window/skipped_norange。容器调度闸门已把 skip 类
    计入未了结使补签得以重跑；宿主 run.sh 退出码语义同样保证补签
    不被「部分成功」吞掉。

    告警时机（避免首签误报噪音）：
      - 零成功（ok_n==0）且存在窗口外跳过：任何轮次都告警（原语义，
        全员窗口外 = 当天可能无签，必须当天知情）；
      - 部分成功 + 窗口外跳过：仅补签轮告警（is_second_run=True）——
        首签有 skipped 属正常（07:10 会重跑），补签轮仍有 skipped 说明
        当天已无下一触发点（宿主 cron 只有 06:31/07:10 两轮），需当天知情。

    is_second_run 判定：调用方（main()）传入
    `os.environ.get("YIBAN_SECOND_RUN") == "1" or _sched_marker_exists()`——
    环境变量优先（run.sh 补签轮 / 容器调度器 SECOND 时段注入，首签轮不设），
    sched-run 标记兜底。二者均缺省时（本函数被单独调用，is_second_run=None）
    回退到 _sched_marker_exists()。环境变量之所以优先：首签子进程被宿主 timeout
    击杀（exit 124）时收尾未执行、sched-run 标记不写，07:10 补签轮仅靠标记
    会误判为首签轮 → 部分成功+窗口外零告警（B12-2 分支复发）。

    返回是否产生了告警（测试用）。
    """
    if not accounts:
        return False
    window_skips = [
        acc.phone for acc in accounts
        if results.get(acc.phone, (False, "", False, ""))[3]
        in (STATUS_SKIPPED_WINDOW, STATUS_SKIPPED_NORANGE)
    ]
    if not window_skips:
        return False
    if is_second_run is None:
        is_second_run = _sched_marker_exists()
    if ok_n > 0 and not is_second_run:
        # 首签轮部分成功 + 部分窗口外：补签轮会重跑，不打扰。
        # 例外：当前时刻已越过补签触发点（06:31 关机、补签点之后才被拉起起的场景），
        # 本轮虽挂首签身份（当日 run 触发标记此刻才首次创建）却是当天最后一轮，
        # 不再有第三次触发兜底 → 仍告警，防真异常无声。
        _now = clock.now()
        if (_now.hour, _now.minute) < window.retry_hm():
            return False
    title = "当日签到异常告警" if ok_n == 0 else "签到窗口异常告警"
    if ok_n == 0:
        body = (
            f"本次全量签到 0 个账号成功，{len(window_skips)} 个账号因窗口外/Range 缺失被跳过。\n"
            f"时间: {clock.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            "请核查 YIBAN_SIGN_START / YIBAN_SIGN_END 与学校实际放号窗口是否匹配"
            "（容器部署另需确认 YIBAN_RUN_TIMEOUT_SEC 未过早截断子进程）。"
        )
    else:
        body = (
            f"本次签到 {ok_n} 个账号成功，但仍有 {len(window_skips)} 个账号因窗口外/Range "
            "缺失未了结（补签轮后仍未签到）。\n"
            f"时间: {clock.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            "请核查 YIBAN_SIGN_START / YIBAN_SIGN_END 与学校实际放号窗口是否匹配。"
        )
    _collect_admin_mail(title, body)
    return True


def _flush_admin_mail_summary(phase=None):
    """签到任务结束：把运行期收集的管理员邮件汇总成一封发送。

    无异常则不发送（成功不打扰）；按主题分组，每个账号独立条目；
    条数超过 MAIL_SUMMARY_MAX_ENTRIES 或正文超长时截断并在尾部注明，
    明细以按天签到日志为准；mailer 内部静默失败，不影响退出码。
    收件人集为空且推送通道已配置时，同一份汇总改走推送兜底（urgent+force）——
    「无收件人」本身不得成为第二处静默点，零成功且零收件人的一轮仍可被观测。
    发送后清空收集器。

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
    body = "\n".join(parts).rstrip()
    if len(body) > MAIL_SUMMARY_MAX_CHARS:
        body = body[:MAIL_SUMMARY_MAX_CHARS].rstrip() + "\n…（超长截断，明细见日志）"
    if recipients:
        sent = False
        try:
            sent = mailer.send_admin_alert("易班签到汇总", body, to=",".join(recipients))
        except Exception as e:
            # mailer 自身承诺内部静默，此处兜底防调用链变化引入的异常外泄
            logger.warning("签到汇总邮件发送异常（%s），降级走 webhook", type(e).__name__)
        if not sent:
            # 邮件通道不可用（未配置/发送失败/异常）→ webhook 兜底
            # （urgent=True + force=True 绕过节流与当日额度）。零成功/窗口外类告警
            # （_maybe_alert_zero_success 等经 _collect_admin_mail 汇总至此）自此
            # 双通道：不再单点依赖 SMTP 可用性。
            send_notification("易班签到汇总", body, urgent=True, force=True)
    else:
        # 收件人集为空原实现静默跳过——告警"看起来发了"实则全灭，且无从排障。
        # 显式 warning 留痕；推送通道已配置时把同一份汇总整卷改推（urgent+force
        # 绕过节流与当日额度），使"零成功 + 无收件人"的一轮仍可观测；推送也未
        # 配置时无事可做，仅留痕供日志页/状态页排查。
        logger.warning(
            "签到汇总告警无可用收件人（ADMIN_TO 与开启接收的管理员均为空），"
            "%d 条告警未走邮件通道，请检查邮件配置", total,
        )
        if notify.is_configured():
            send_notification(
                "易班签到汇总", "邮件无可用收件人，改推：\n" + body, urgent=True, force=True,
            )
    _mail_summary.clear()


def _flush_mail_on_sigterm(signum, frame):
    """SIGTERM（run.sh timeout / 手动 terminate / 容器超时终止）兜底冲刷。

    签到轮被超时击杀时进程内 _mail_summary 随进程死亡——整轮已收集的告警
    （含部分成功的汇总）一并消失。信号处理器在退出前冲刷一次；汇总为空时
    不产生任何发送（不重复告警），正常收尾路径已清空收集器。
    """
    # 冲刷失败不改变退出码：SIGTERM 路径的首要契约是尽快退出
    with contextlib.suppress(Exception):
        _flush_admin_mail_summary(phase="签到超时终止")
    sys.exit(128 + int(signum or 15))


# B 线用户失败提醒每日限频（2026-08-27 审查修复 P2-1）：README/更新日志承诺
# 「每天每个账号最多 1 封」，原实现仅靠单次运行终态路径隐式保证——手动 --only
# 签到与探针进程可在同日追加发送。现以按天状态文件显式去重（0 或负数 = 不限）。
USER_FAIL_MAIL_DAILY_CAP = parse_env_int("YIBAN_MAIL_USER_FAIL_DAILY_CAP", 1)


def _user_fail_mail_state_path(today_str):
    state_dir = os.environ.get("YIBAN_STATE_DIR", "/var/log/yiban")
    return os.path.join(state_dir, f"mail-user-fail-{today_str}.json")


def _user_fail_mail_reserve(phone, today_str):
    """预占该账号今日失败提醒额度：允许则占位并返回 True，超额返回 False。

    读-改-写整体持状态文件锁；跨进程（签到主进程 / 手动 --only / 探针）一致。
    文件按天命名自然轮转，无需清理历史。

    **调用约定**：占位后若邮件实际未发出（未启用 / SMTP 失败），必须调
    `_user_fail_mail_release` 归还，否则一次 SMTP 抖动就会吞掉该账号当天
    唯一的提醒机会（本函数旧实现正是如此，与 docstring 承诺相反）。
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
            try:
                used = int(data.get(phone, 0))
            except (TypeError, ValueError):
                # 状态文件被手工改成非数字时不得冒泡中断整轮签到
                used = 0
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


def _user_fail_mail_release(phone, today_str):
    """归还一个失败提醒额度（邮件未真正发出时调用）；下限 0，失败仅告警。

    状态目录不可写时与 reserve 同口径静默放行（限频设施故障不得放大成业务故障）。
    """
    path = _user_fail_mail_state_path(today_str)
    try:
        with _state_file_lock(path):
            if not os.path.exists(path):
                return
            try:
                with open(path, encoding="utf-8-sig") as f:
                    data = json.load(f)
            except (OSError, ValueError, TypeError):
                return
            if not isinstance(data, dict) or phone not in data:
                return
            try:
                used = int(data.get(phone, 0))
            except (TypeError, ValueError):
                return
            data[phone] = max(0, used - 1)
            tmp = path + ".tmp" + str(os.getpid())
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, path)
    except OSError as e:
        logger.warning("归还失败提醒额度失败（不影响签到）: %s", e)


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
    _today = clock.now().strftime("%Y-%m-%d")
    if not _user_fail_mail_reserve(phone, _today):
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
    if not mailer.send_user(owner, subject, body):
        # 未真正发出（邮件未启用 / 无收件人 / SMTP 全部失败）：归还额度，让当天
        # 还有机会重试——额度语义是"每天最多成功提醒 N 次"
        _user_fail_mail_release(phone, _today)
        logger.info(
            "账号 %s 的失败提醒未发出（邮件未启用或发送失败），已归还今日额度",
            _mask_phone(phone),
        )


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def verify_account(account):
    """只读健康检查（登录 + 拉取任务，不提交签到）。

    供注册时预处理验证（web 端）与探针模式（--probe）复用。
    返回 (ok, message)：ok=False 表示存在无法自愈的问题；message 已脱敏。
    """
    phone = account.phone
    if not account_still_signable(account):
        # 探针同样会完整登录（与签到同一风控暴露面）：账号已被删除/停用则不发起
        return False, "账号已被删除或停用"
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
        # 同 attempt_signin——异常消息统一过 _sanitize_url 打码
        safe_err = _sanitize_text(_sanitize_url(str(e)))
        logger.warning(f"[{phone}] 健康检查失败: {safe_err}", exc_info=False)
        return False, safe_err


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


def _write_sign_state(phone, status, message, scheduled=None, dur=None):
    """写按日结构化状态文件（web 状态显示的事实源，原子替换防半截文件）。

    文件：{YIBAN_STATE_DIR}/sign-state-YYYY-MM-DD.json
    结构：{phone: {status, message, time, task}}；task 预留多时段/多星期签到扩展。
    scheduled：今日计划签到时间（HH:MM:SS，自动错峰分配后写入，执行后保留）。
    dur：单次签到尝试耗时秒数（P6，2026-08-16：慢响应可据此判断网络/接口问题）。
    状态目录不可写时丢弃，不影响签到执行。
    """
    state_dir = os.environ.get("YIBAN_STATE_DIR", "/var/log/yiban")
    path = _sign_state_path()
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
            now = clock.now()
            # 计划时间是当日事实：后续写入（执行结果/重试）未显式传 scheduled 时保留既有值
            existing = data.get(phone)
            if not scheduled and isinstance(existing, dict):
                scheduled = existing.get("scheduled")
            # **计划态不覆盖已有结果**：`pending` 是"打算什么时候签"的预测，success/failed
            # 等是"已经发生"的事实——事实优先。单执行体形态下计划写在前、结果写在后，看不出
            # 差别；多执行体下每个执行体启动都会写一遍全量计划，晚启动者的计划会把先启动者
            # 已写完的结果抹回 pending（实测：4 执行体 40 账号，2 个账号被抹成 pending），
            # 既让日历显示"待签"，又让补签闸门把已签账号当未了结重跑一遍。
            if (
                status == STATUS_PENDING
                and isinstance(existing, dict)
                and str(existing.get("status", "")).strip() not in ("", STATUS_PENDING)
            ):
                if scheduled:
                    existing["scheduled"] = scheduled
                data[phone] = existing
                tmp = f"{path}.tmp{os.getpid()}"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False)
                os.replace(tmp, path)
                return
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
def _write_sched_done(counts=None):
    """写入当日「全量签到已运行」标记（sched-run-<date>.json）——调度器闸门事实源。

    调度器原 `_signed_today()` 以「任一账号 success」判定当日已签，
    手动签到/部分成功都会压制全站首签与补签。新契约：仅全量模式在收尾写本标记
    （--only 手动签到与 --probe 探针不写），调度器据此判定：
    - 首签：标记不存在 → 执行；
    - 补签：标记不存在，或存在未了结账号（failed/retrying/pending）→ 执行。
    """
    state_dir = os.environ.get("YIBAN_STATE_DIR", "/var/log/yiban")
    path = os.path.join(state_dir, f"sched-run-{clock.now().strftime('%Y-%m-%d')}.json")
    try:
        os.makedirs(state_dir, exist_ok=True)
        payload = {
            "completed": True,
            "finished_at": clock.now().strftime("%H:%M:%S"),
        }
        if isinstance(counts, dict):
            payload.update(counts)
        tmp = path + ".tmp" + str(os.getpid())
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError as e:
        logger.warning("写入全量完成标记失败（调度器可能重复触发当日签到）: %s", e)


# ---------------------------------------------------------------------------
# 补签轮判定（宿主 run.sh 与容器 docker/scheduler.py 共用的单一实现）
# ---------------------------------------------------------------------------
# 背景（2026-09-10 批次20 B3）：宿主原先靠**第二个独立 cron**（07:12）做补签，
# 但首签进程要 sleep 到最晚自选时间片（生产实测 07:25）才结束，flock 由脚本持有至
# 退出 → 07:12 的 cron 每天撞锁 `exit 0`，补签轮从未真正执行（生产日志 12/12 天实证）。
# 修法（用户裁决方案一）：宿主改为与容器同语义——**同一进程内**首轮结束后再判定
# 一次"是否需要补跑"，判定口径收敛到此处，两侧不再各写一份。
#
# 判定 = 「当日全量未收尾」或「当日存在未了结账号」：
#   - 全量未收尾（sched-run-<date>.json 缺失/completed=false）：首轮被 timeout 击杀、
#     崩溃或压根没跑起来 → 必须补跑；
#   - 存在未了结账号 = 状态文件里任一账号落 UNDONE_STATUSES。
# 无状态文件/文件损坏一律按"未了结"处理（宁多跑一轮，不漏签）。
# 「未了结」状态集合：定义在 yiban.status（唯一事实源），此处为同一对象的别名
# （docker/scheduler.py 亦别名引用它；测试断言三者同一身份）。
UNDONE_STATUSES = yiban_status.UNDONE_STATUSES

#: 兜底常驻执行体的锁文件名（与定时全量/手动签到并存，互斥交给领取池）
FALLBACK_LOCK_NAME = "signin-run.lock.fallback"

#: 领取池的"当日了结"口径（与 `_second_run_drop_done` 的剔除集合一致）：
#: 这三个状态意味着今天不必再签，其余状态（含窗口外跳过、无点位、失败）都仍开放，
#: 由补签轮或兜底执行体接手。
_CLAIM_DONE_STATUSES = (STATUS_SUCCESS, STATUS_ALREADY, STATUS_NO_TASK)

# `--second-run-check` 的退出码契约（run.sh 据此分支，勿随意改动）
SECOND_RUN_CHECK_NEED = 10   # 需要补跑第二轮
SECOND_RUN_CHECK_SKIP = 0    # 无需补跑


def _state_dir():
    return os.environ.get("YIBAN_STATE_DIR", "/var/log/yiban")


def full_run_done_today(state_dir=None, day=None):
    """当日全量签到是否已收尾（sched-run-<date>.json 的 completed 标记）。"""
    d = state_dir or _state_dir()
    today = day or clock.now().strftime("%Y-%m-%d")
    try:
        # utf-8-sig：容错 Windows 手工/工具写入的 BOM（与 _load_cred_state 同口径）
        with open(os.path.join(d, f"sched-run-{today}.json"), encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and bool(data.get("completed"))


def has_undone_accounts_today(state_dir=None, day=None):
    """当日是否存在未了结账号；无记录/文件缺失/损坏按"未了结"处理（fail-safe 侧）。"""
    d = state_dir or _state_dir()
    today = day or clock.now().strftime("%Y-%m-%d")
    try:
        with open(os.path.join(d, f"sign-state-{today}.json"), encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return True
    if not isinstance(data, dict) or not data:
        return True
    return any(
        isinstance(v, dict) and str(v.get("status", "")).strip() in UNDONE_STATUSES
        for v in data.values()
    )


def need_second_run(state_dir=None, day=None):
    """是否需要补跑第二轮（宿主 run.sh 与容器调度器共用）。

    True = 当日全量未收尾，或存在未了结账号。调用方（run.sh）在**首轮结束之后、
    仍持锁期间**调用，因此不用担心与其它进程的竞态；容器侧在首轮子进程 wait()
    返回后调用，语义一致。
    """
    return (not full_run_done_today(state_dir, day)) or has_undone_accounts_today(state_dir, day)


def _cred_state_path():
    state_dir = os.environ.get("YIBAN_STATE_DIR", "/var/log/yiban")
    return os.path.join(state_dir, "cred-state.json")


def _load_cred_state():
    """读账密状态文件（唯一入口见 yiban/cred_state.py）。"""
    return cred_state.read()


def _save_cred_state(data, touched=None):
    """保存账密状态。

    `touched` 给出本次实际处理的手机号集合时**按账号增量合并**（磁盘最新值为准，
    其余账号不受影响）；为 None 时整体覆盖（保留给测试/极端场景）。

    增量合并是必需的：全量轮从启动起就持有内存快照，若收尾整体覆盖，运行期间
    Web 端刚清除的暂停会被重新写回——该账号继续用错密码登录、加重风控。
    """
    try:
        if touched is None:
            def _replace(d):  # 整体覆盖（兼容入口：仅测试与极端场景使用）
                d.clear()
                d.update(data)
                return True

            cred_state.update(_replace)
        else:
            cred_state.merge(touched, data)
    except Exception as e:  # 锁/磁盘异常都只告警：状态文件不得影响签到主流程
        logger.debug("写入账密状态失败（%s）: %s", _cred_state_path(), _sanitize_text(e))


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
    win = window.bounds(cfg)
    start_min, end_min = win.start_min, win.end_min
    eff_lo, eff_hi = win.lo_min, win.hi_min
    if win.fell_back:
        logger.warning(
            "有效签到窗口为空（窗口 %s~%s、前裁 %ss 后裁 %ss），回退默认窗口 06:30~07:50",
            cfg["sign_start"], cfg["sign_end"], cfg["edge_front_sec"], cfg["edge_back_sec"],
        )
        # 镜像 _schedule_config 的 F3 模式（一次性去重）——原实现
        # 只写 WARNING 日志，管理员在 Web 界面看到的裁剪设置"看起来生效"、实际
        # 签到时刻完全不同且无人知情。现并入当日汇总邮件（A 线）一次，确保
        # 前后裁剪配置错误可被管理员发现；去重防多账号/多轮调用刷屏。
        global _edge_empty_window_notified
        if not _edge_empty_window_notified:
            _edge_empty_window_notified = True
            _collect_admin_mail(
                "签到窗口配置异常",
                (
                    f"有效签到窗口为空：窗口 "
                    f"{cfg['sign_start'][0]:02d}:{cfg['sign_start'][1]:02d}"
                    f"~{cfg['sign_end'][0]:02d}:{cfg['sign_end'][1]:02d}"
                    f" 被前后裁剪吃满（前 {cfg['edge_front_sec']}s / 后 "
                    f"{cfg['edge_back_sec']}s），已回退默认窗口 06:30~07:50，"
                    "实际签到时间将与配置不符！请调小 "
                    "YIBAN_WINDOW_EDGE_FRONT_SEC / YIBAN_WINDOW_EDGE_BACK_SEC"
                    "（或放宽 YIBAN_SIGN_START / YIBAN_SIGN_END）"
                ),
            )
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
    now: 注入当天日期（默认 clock.now()）；rng: 注入随机源（测试固定 seed）
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
    now = now or clock.now()

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
                # 留痕——退化窗口下自选片被丢弃此前完全静默
                logger.warning(f"[{phone}] 自选时间片 {slot} 不在今日可选范围，回退自动分配")
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
    """签到窗口是否已关闭（口径唯一源：yiban.window，与 _schedule_blocks 的 horizon 同源）。

    此前本函数只按 sign_end - edge_back 算，而 _schedule_blocks 在"有效窗口被裁剪
    吃空"时会回退到默认窗口——于是出现"有 80 分钟的完整计划、却整轮判时段已结束、
    零请求"。现两处都走 window.bounds（含同一套回退）。
    """
    return window.bounds(sch_cfg).is_closed(now_dt)


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
    now = now or clock.now()
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
            # 创建即 0600——open("w") 在默认 umask 下 0644，写完到 replace 之间
            # （及崩溃残留时）整个 .env 对同机其他用户可读。原先只靠事后 chmod，
            # 且默认 umask 未必是 077（交互 shell 手工跑 --probe 即可能命中）
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write("\n".join(out) + "\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, env_path)
            with contextlib.suppress(OSError):
                os.chmod(env_path, 0o600)
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
        # 周期轮询的常态路径（容器调度器每 600s / 宿主 cron */10 都会走到）：
        # "未到触发点"属预期行为而非异常，逐次 INFO 会刷屏（约 144 条/日）。
        # 降为 DEBUG——默认级别下日志只保留签到结果与探针实际执行结果；
        # 需排查轮询是否如期触发时，开 DEBUG 级别即可看到每次尝试轨迹。
        logger.debug("==== 探针模式：已开启，但未到触发时间/频率，本次跳过 ====")
        return
    # last_run 占位前置——探测开始前先记账，双探针/调度重启并发时
    # 只放行一个（原实现探测结束后才写，两个探针都能通过 _health_probe_due 判定）
    _update_probe_state_run(clock.now().strftime("%Y-%m-%d"))
    logger.info(f"==== 探针模式：对 {len(accounts)} 个账号进行健康检查 ====")
    # 探针确认健康 → 清除熔断暂停（原实现探针与熔断互不相通，
    # 误冻账号即使每晚探针证明凭据可用也要熬到 7 天后半开试探）
    cred_state = _load_cred_state()
    fuse_cleared = False
    now = clock.now()
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
            cred_entry = cred_state.get(acc.phone)
            if isinstance(cred_entry, dict) and cred_entry.get("paused_since"):
                cred_state.pop(acc.phone, None)
                fuse_cleared = True
                logger.info(f"[{_mask_phone(acc.phone)}] ✅ 探针确认凭据健康，解除熔断暂停")
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
    if fuse_cleared:
        with contextlib.suppress(Exception):
            _save_cred_state(cred_state, touched={a.phone for a in accounts})
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
    # once 自动关闭（last_run 已在探测开始前置记录）
    if PROBE_INTERVAL.strip().lower() == "once":
        _env_update_probe(auto_disable=True)
        logger.info("==== 探针模式（单次）执行完成，已自动关闭探针 ====")
    logger.info(
        f"==== 探针模式完成：健康 {healthy_n}，网络类失败 {soft_fail_n}，预警 {len(hard_fail)} ===="
    )


def _apply_only_filter(accounts, only_arg):
    """--only 过滤：只保留指定手机号，返回 (保留账号, 未命中号码列表)。

    对每个未命中号码落 warning 日志——此前仅"全部不命中"才报错，
    `--only "存在号,手滑号"` 时未命中号码被静默丢弃，用户误以为全部已处理。
    调用方负责"过滤后为空则报错退出"。
    """
    only_set = {p.strip() for p in only_arg.split(",") if p.strip()}
    filtered = [a for a in accounts if a.phone in only_set]
    missing = sorted(only_set - {a.phone for a in filtered})
    for phone in missing:
        # 裸号不带 [] 定界符，web 侧 _mask_log_phones（只认 [11 位号]）盖不住，
        # 落盘即脱敏——web 展示层/导出不得出现完整号（「本地日志保留完整号」
        # 的设计约定仅覆盖 [号] 形态行，见 _mask_phone 注释）
        logger.warning("--only 指定账号不在配置中: %s", _mask_phone(phone))
    return filtered, missing


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

        try:
            accounts = load_accounts()
        except RuntimeError as e:
            logger.error("兜底执行体：配置加载失败: %s", e)
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
            f"退出码 {SECOND_RUN_CHECK_NEED}（需要补跑），否则 0。不读账号、不联网。"
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

    logger.info(f"==== 开始执行签到，共 {len(accounts)} 个账号，队列重试模式 ====")
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
    logger.info(f"==== 签到汇总：{summary} ====")

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
