# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""CLI 支撑：日志装配、进程级运行锁、状态文件读改写锁。

三件事都属"进程外壳"而非签到逻辑：谁在跑（单实例锁，防 cron 全量与手动 `--only`
并发登录同一账号）、日志写哪儿（按天文件 handler，装配延迟到入口、导入零副作用）、
状态文件怎么安全读改写（跨进程文件锁）。引擎其余部分与入口都依赖它们，故独立成模块。

跨模块调用纪律见包说明：本模块内部用裸名，跨模块一律走模块属性访问。
"""
import logging
import os
import time
from contextlib import contextmanager

from yiban import clock
from yiban.infra import locks
from yiban.logging_ext import FlockFileHandler

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
