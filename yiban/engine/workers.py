# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""多执行体：`--workers N` 的监督进程与 `--fallback` 兜底常驻执行体。

两者都是"进程编排"而非签到逻辑本身——真正的活儿都交回 `round.run_queue_retry`，
它们只负责：谁持哪把锁（监督进程持全局锁、子进程各持自己的锁文件）、谁用哪个出口
代理（`YIBAN_PROXY_LIST` / `YIBAN_PROXY_FALLBACK`）、退出码怎么汇总（取最严重者）。

**子进程入口是 `python -m yiban.cli sign`**：本模块是包内模块，不再能按文件路径直接
执行，故监督进程以模块方式拉起同一个 CLI（cwd 与 PYTHONPATH 都指向仓库根）。

跨模块调用纪律见包说明：跨模块一律走模块属性访问。
"""
import logging
import os
import socket
import subprocess
import sys
import time

from yiban import clock, egress
from yiban import status as yiban_status
from yiban.engine import accounts as accounts_mod
from yiban.engine import cli_support, schedule, state_io
from yiban.engine import round as round_mod

logger = logging.getLogger("yiban")

#: 兜底常驻执行体的锁文件名（与定时全量/手动签到并存，互斥交给领取池）
FALLBACK_LOCK_NAME = "signin-run.lock.fallback"

# 状态码别名（与 yiban.status 同一对象）
STATUS_FAILED = yiban_status.STATUS_FAILED

#: 仓库根（`yiban/engine/<模块>.py` 上溯三层）。**不是**导入引导（包内模块本就靠
#: `python -m` / 测试的 pythonpath 找到包）：这里只用来给拉起的子进程设 cwd 与
#: PYTHONPATH——`python -m yiban.cli` 要求仓库根在导入路径上。
_REPO_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _child_env_with_repo_root(env):
    """在子进程环境里确保仓库根在 `PYTHONPATH` 上（已含则不重复追加）。"""
    paths = [p for p in env.get("PYTHONPATH", "").split(os.pathsep) if p]
    if _REPO_DIR not in paths:
        paths.insert(0, _REPO_DIR)
    env["PYTHONPATH"] = os.pathsep.join(paths)
    return env


def run_worker_supervisor(n, argv):
    """拉起 n 个执行体子进程并汇总退出码（`--workers N`）。

    - **全局锁由本进程持有**：散落的另一轮全量（cron 与手动）仍会被挡住；
    - 子进程各持自己的锁文件 + 各自的执行体身份（领取池据此分工）；
    - 每个子进程可配一个独立出口代理（`YIBAN_PROXY_LIST`，见 `_worker_proxy`）；
    - 退出码汇总取"最严重"的一个：真失败(1) > 锁忙(3) > 跳过/窗口外(2) > 全成功(0)。
      调用方（run.sh）据此判断本轮是否需要补签，语义与单执行体一致。
    """
    # 全局锁：本进程持有直到子进程全部结束（句柄必须保活，不能只用一次就丢）
    _global_lock = cli_support._acquire_run_lock(False)
    # 先在本进程把库初始化/迁移做完并校验账号配置：否则 N 个子进程会在同一秒
    # 抢着 init_db（实测 `PRAGMA journal_mode=WAL` 会报 "database is locked"），
    # 而且配置错误的报错会变成 N 份、互相淹没。
    try:
        loaded_accounts = accounts_mod.load_accounts()
    except RuntimeError as e:
        logger.error(f"配置加载失败: {e}")
        return 1
    if not loaded_accounts:
        logger.error("未配置任何账号，不拉起执行体")
        return 1
    logger.info("多执行体：共 %d 个账号待签，拉起 %d 个执行体", len(loaded_accounts), n)
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
        # 子进程入口：模块方式执行同一个 CLI（cwd=仓库根 + PYTHONPATH 含仓库根，
        # `python -m` 才能找到 yiban 包）。子进程收到的命令行参数与旧版逐字相同。
        cmd = [sys.executable, "-m", "yiban.cli", "sign", *child_argv]
        logger.info("执行体 %d/%d 启动（出口: %s）", i + 1, n, egress.describe(proxy))
        children.append(subprocess.Popen(
            cmd, env=_child_env_with_repo_root(env), cwd=_REPO_DIR,
        ))
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
    interval = interval or schedule._env_int("YIBAN_FALLBACK_INTERVAL", 60, 5, 3600)
    os.environ.setdefault("YIBAN_RUN_LOCK_NAME", FALLBACK_LOCK_NAME)
    proxy = egress.resolve(egress.ROLE_FALLBACK)
    logger.info("兜底执行体启动（出口: %s，扫描间隔 %ss）", egress.describe(proxy), interval)
    if proxy:
        os.environ["YIBAN_PROXY"] = proxy

    sch_cfg = schedule._schedule_config()
    last_code = 0
    while True:
        now = clock.now()
        if deadline is not None and now >= deadline:
            logger.info("兜底执行体：到达截止时刻，退出")
            break
        if schedule._window_closed(sch_cfg, now):
            logger.info("兜底执行体：签到时段已结束，退出")
            break

        state_io._write_fallback_alive(now)
        try:
            accounts = accounts_mod.load_accounts()
        except RuntimeError as e:
            logger.error("兜底执行体：配置加载失败: %s", e)
            state_io._clear_fallback_alive()
            return 1
        if not accounts:
            logger.info("兜底执行体：当前没有账号，%ss 后再看", interval)
            time.sleep(interval)
            continue

        delegated = set()
        results = round_mod.run_queue_retry(accounts, os.environ.get("YIBAN_NOTIFY_URL", ""), 0,
                                            schedule._env_int("YIBAN_ACCOUNT_GAP_MAX", 10, 0, 3600),
                                            schedule=None, cred_state=state_io._load_cred_state(),
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
    state_io._clear_fallback_alive()
    logger.info("兜底执行体已退出（心跳已清除）")
    return last_code
