# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""多执行体：`--workers N` 的监督进程与 `--fallback` 兜底常驻执行体。

两者都是"进程编排"而非签到逻辑本身——真正的活儿都交回 `round.run_queue_retry`，
它们只负责：谁持哪把锁（监督进程持全局锁、子进程各持自己的锁文件）、谁用哪个出口
代理（执行体清单 `YIBAN_EXECUTORS` 每行一个出口；清单缺失时回退旧三键
`YIBAN_PROXY_LIST` / `YIBAN_PROXY_FALLBACK`，见 `egress.resolve`）、每个并行执行体的
**心跳**（开始/存活期/收尾，按**槽位号**写在 `state_io`，供接口判存活四态）、
退出码怎么汇总（取最严重者，但补签轮判定的「需要补跑」原样透出）。清单里的拉起列表由
`runner` 取 `egress.launch_slots` 后按槽位传进来，故**停用行不会被拉起**。

**子进程入口是 `python -m yiban.cli sign`**：本模块是包内模块，不再能按文件路径直接
执行，故监督进程以模块方式拉起同一个 CLI（cwd 与 PYTHONPATH 都指向仓库根）。

跨模块调用纪律见包说明：跨模块一律走模块属性访问。
"""
import logging
import os
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

#: 给全量轮让位时的轮询间隔（秒）：只是一次 flock 探测，比常规扫描密，全量轮一结束就接手
_YIELD_POLL_SEC = 30

#: 三道门（周日/周六未开、一键暂停）的日志措辞——门本身在 `schedule.day_off`（唯一实现），
#: 这里只给兜底自己的说法（定时轮的措辞在 `runner._GATE_SKIP_MESSAGES`）。
_GATE_REASON_TEXT = {
    schedule.DAY_OFF_SUNDAY: "周日签到未开启（系统设置里可开启）",
    schedule.DAY_OFF_SATURDAY: "周六签到已关闭（系统设置里可开启）",
    schedule.DAY_OFF_PAUSED: "管理员已一键暂停签到",
}

# 状态码别名（与 yiban.status 同一对象）
STATUS_FAILED = yiban_status.STATUS_FAILED

#: 仓库根（`yiban/engine/<模块>.py` 上溯三层）。**不是**导入引导（包内模块本就靠
#: `python -m` / 测试的 pythonpath 找到包）：这里只用来给拉起的子进程设 cwd 与
#: PYTHONPATH——`python -m yiban.cli` 要求仓库根在导入路径上。
_REPO_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

#: 等待子进程退出的轮询粒度（秒）。取 1s：子进程一退出就补收尾标记（页面立刻从
#: "在线"变"已跑完"），又不至于把监督进程变成忙等。心跳的刷新节流由
#: `state_io.WORKER_HEARTBEAT_SEC` 决定，与这个粒度无关。
_WAIT_POLL_SEC = 1.0

#: 补签轮判定「需要补跑」的退出码：与 `runner.SECOND_RUN_CHECK_NEED` 是同一契约值
#: （`docs/dev/cli.md` §3）。此处复写而非从 runner 取，是因为 runner 反向依赖本模块，
#: 导入期取不到它；两处一致性由测试对账。子进程带它退出时监督进程必须原样透出——
#: 归一成 0 会让宿主 run.sh 把「需要补签」读成「一切正常」，补签轮被静默吞掉。
_SECOND_RUN_CHECK_NEED = 10


def run_worker_supervisor(n, argv, slots=None):
    """拉起 n 个执行体子进程并汇总退出码（`--workers N`）。

    - **全局锁由本进程持有**：散落的另一轮全量（cron 与手动）仍会被挡住；
    - 子进程各持自己的锁文件 + 各自的执行体身份（领取池据此分工）；
    - 每个子进程可配一个独立出口代理（`egress.resolve`）；
    - 子进程的 argv 是本进程 argv 去掉 `--workers` 及其后随值（`--workers=N` 同样）后的
      逐字复刻：多执行体的身份靠注入的 `YIBAN_EXECUTOR_ID` 表达，`--workers` 下传只会
      让子进程再当一次监督进程；
    - `slots` = **执行体清单给出的槽位号**（拉起列表，`egress.launch_slots`）。省略时
      用旧口径的 `0..n-1`（行为逐字不变）。传了槽位时子进程的身份/锁/心跳都按
      **槽位号**算，故清单里**停用/删除的行不会被拉起**，且删中间行不影响其余槽位；
    - 退出码汇总取"最严重"的一个：补签轮判定的「需要补跑」(10) 原样透出且优先于其余判定
      （它表达调用方必须区分的语义，归一成 0 会让补签轮被静默吞掉），其后才是
      真失败(1) > 锁忙(3) > 跳过/窗口外(2) > 全成功(0)。
      调用方（run.sh）据此判断本轮是否需要补签，语义与单执行体一致；容器侧不消费本
      退出码（docker/scheduler.py 的补签闸门读状态文件判定）。
    """
    slot_list = list(range(n)) if slots is None else list(slots)
    n = len(slot_list)
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
    logger.info("多执行体：共 %d 个账号待签，拉起 %d 个执行体（槽位 %s）",
                len(loaded_accounts), n, slot_list)
    children = []
    for i, slot in enumerate(slot_list):
        env = os.environ.copy()
        # 身份串的唯一构造处在 egress（写入与解析同一份口径）：稳定槽位名
        # `worker-{槽位}@{主机名}`——跨重启不变，故重启后立刻认领自己上一轮的在飞账号；
        # 代价是同一槽位名不得两台机器同时跑（跨主机靠 @主机名 区分，同机由本进程
        # 持有的全局锁 signin-run.lock 挡住，故那把锁不能去掉）。
        env["YIBAN_EXECUTOR_ID"] = egress.worker_owner(slot)
        env["YIBAN_RUN_LOCK_NAME"] = f"signin-run.lock.w{slot}"
        proxy = egress.resolve(egress.ROLE_WORKER, slot)
        if proxy:
            env["YIBAN_PROXY"] = proxy
        # 复刻本轮其余参数：剔除 `--workers` **及其后随值**（按 argv 序位解析，与
        # argparse 同语义；`--workers=N` 形态一并剔除）。不能按值匹配：清单模式下拉起
        # 的槽位数与命令行 `--workers N` 的 N 可以不等——网页改执行体清单只写
        # YIBAN_EXECUTORS、不回写 YIBAN_WORKERS，于是 N 留在 argv 里变成子进程的位置
        # 参数，argparse 直接报错退出 → 每个执行体都零请求退出，全天静默不签到。
        child_argv = []
        _skip_next = False
        for a in argv:
            if _skip_next:          # 上一个是 `--workers`：本项是它的后随值，一并剔除
                _skip_next = False
                continue
            if a == "--workers":
                _skip_next = True
                continue
            if a.startswith("--workers="):
                continue
            child_argv.append(a)
        # 子进程入口：模块方式执行同一个 CLI（cwd=仓库根 + PYTHONPATH 含仓库根，
        # `python -m` 才能找到 yiban 包）。子进程收到的命令行参数与旧版逐字相同。
        cmd = [sys.executable, "-m", "yiban.cli", "sign", *child_argv]
        logger.info("执行体 %d/%d 启动（槽位 %d，出口: %s）",
                    i + 1, n, slot, egress.describe(proxy))
        children.append(subprocess.Popen(
            cmd, env=_child_env_with_repo_root(env), cwd=_REPO_DIR,
        ))
        # 开始心跳：本槽位"本轮已启动"的事实。放在 Popen 之后，故页面上"在跑"的
        # 槽位必然真有子进程（不是拿"文件在不在"猜）。
        state_io.mark_worker_started(slot)
        # 错开启动：既避开"同一秒争库"，也让首轮请求不要在同一瞬间齐发（风控面）
        if i + 1 < n:
            time.sleep(0.5)

    codes = _await_workers(children, slot_list)
    for i, rc in enumerate(codes):
        logger.info("执行体 %d/%d（槽位 %d）结束，退出码 %s", i + 1, n, slot_list[i], rc)

    if any(c == _SECOND_RUN_CHECK_NEED for c in codes):
        return _SECOND_RUN_CHECK_NEED
    if any(c == 1 for c in codes):
        return 1
    if any(c == 3 for c in codes):
        return 3
    if any(c == 2 for c in codes):
        return 2
    return 0


def run_fallback_worker(argv_rest, interval=None, deadline=None):
    """兜底常驻执行体：窗口内反复扫"还没了结"的账号并接手，窗口关闭即退出。

    为什么需要它：学校晚放号、窗口内新审核通过的账号、被慢账号拖住的、失败待重试的
    ——都能被**随手接手**，而不是等下一轮定时任务。

    做法刻意简单可靠：每一轮重新加载账号并调用同一条执行路径（`run_queue_retry`，
    schedule 为空=立即执行）。**分工由领取池承担**：已了结的账号领不到、别的执行体
    正在做的领不到，所以"全量账号列表"作为输入也不会重复签——不需要在这里再写一套筛选。

    独立锁 `signin-run.lock.fallback` **由调用方**（`runner.main` 的兜底分支）取：
    本函数只把锁名写进环境变量，好让子路径/日志口径一致；撞上已在跑的兜底时由调用方
    退出 3。故它与定时全量、手动签到、其他执行体都能并存（账号级互斥交给领取池）。

    **运行前会先过四道关**（每轮重判，不是启动时判一次）：周末签到未开、一键暂停
    （都由 `schedule.day_off` 判定，与定时轮同源）→ 直接退出；签到时段**尚未开始**
     → 等到开始再扫（提前拉起是 cron 模板的常态，窗口外发请求等于白登陆一次）；
     **全量轮正在跑**（全局锁被持有）→ 让位，等它结束再扫。故它的运行区间严格落在
    "配置的有效窗口内、今天该签、且没有全量轮在跑"——它是**捡漏**的那个，不与
    定时轮/多执行体抢活。

    退出：窗口关闭 / 三道门命中 / 到达 `deadline` / 账号列表为空且已过窗口。
    返回退出码语义与单执行体一致（0 全成功、1 有真失败、2 存在窗口外未了结）。
    """
    interval = interval or schedule._env_int("YIBAN_FALLBACK_INTERVAL", 60, 5, 3600)
    os.environ.setdefault("YIBAN_RUN_LOCK_NAME", FALLBACK_LOCK_NAME)
    # 身份串：兜底常驻与单执行体/并行执行体各用**稳定的槽位名**（`fallback@{主机名}`），
    # 跨重启不变 ⇒ 重启后立刻接手自己上一轮的在飞账号；代价是同一槽位名不得两台机器
    # 同时跑（跨主机靠 @主机名 区分，同机由下面那把 `signin-run.lock.fallback` 挡住）。
    os.environ.setdefault("YIBAN_EXECUTOR_ID", egress.fallback_owner())
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
        # 周末门 / 一键暂停门：走**与定时轮同一实现**（schedule.day_off）。
        # 这两道门原先只写在 runner.main 里，而本进程在它之前就 return（见分支顺序），
        # 于是管理员关掉周末签到或点了一键暂停，兜底照签（2026-09-17 实测）。
        # 每轮重判（不是启动时判一次）⇒ 窗口中途改设置也能在下一次扫描生效。
        gate = schedule.day_off(now)
        if gate:
            logger.info("兜底执行体：%s，退出（本日不签到）", _GATE_REASON_TEXT[gate])
            break
        if schedule._window_closed(sch_cfg, now):
            logger.info("兜底执行体：签到时段已结束，退出")
            break
        if not schedule._window_open(sch_cfg, now):
            # 提前拉起（cron 模板 06:05、窗口 06:30）时**必须等**，不能照走：
            # 窗口外每次尝试都是一次真实登录（易班侧照实计数），而且可能把账号签在
            # 管理员配置的窗口之外——`run_queue_retry` 的手动链路本身不判本项目的窗口。
            opens_in = int(schedule._window_opens_in(sch_cfg, now))
            wait = min(interval, max(1, opens_in))
            logger.info("兜底执行体：签到时段尚未开始（%d 秒后开始），%d 秒后再看", opens_in, wait)
            time.sleep(wait)
            continue
        if cli_support._run_lock_held():
            # **给全量轮让位**：全量轮/多执行体在跑时不抢账号——否则兜底会按列表顺序
            # 一路签下去，把全量轮的错峰计划与多执行体的分工一起冲掉（它抢的是"还没被
            # 领走的"，而全量轮只在每个账号的计划时刻才领取）。
            # 让位期间的轮询比常规扫描密：全量轮一结束就接手，而这次探测只是一次 flock。
            logger.info("兜底执行体：全量轮正在运行，让位（%d 秒后再看）", _YIELD_POLL_SEC)
            time.sleep(_YIELD_POLL_SEC)
            continue

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
        # 本轮熔断状态快照：`read()` 每轮返回新 dict，`run_queue_retry` 就地改它，
        # 故必须持有引用以便轮末写回（不能像过去那样现取现传、写回时已无对象）。
        cred_state = state_io._load_cred_state()
        results = round_mod.run_queue_retry(accounts, os.environ.get("YIBAN_NOTIFY_URL", ""), 0,
                                            schedule._env_int("YIBAN_ACCOUNT_GAP_MAX", 10, 0, 3600),
                                            schedule=None, cred_state=cred_state,
                                            delegated=delegated,
                                            # 兜底是"替全量轮捡漏"：窗口已关就该停手，
                                            # 一轮扫描内部不再对剩余账号发起真实登录
                                            window_guard=True)
        # 轮末写回熔断计数（口径与 runner 全量轮一致：按本轮账号增量合并）。不写回则
        # "连续凭据失败达阈值 → 暂停"只在磁盘上不存在：下一轮 read() 又从零开始，
        # 错密码账号被无限次真实登录（易班侧照实计数，加重风控）。
        state_io._save_cred_state(cred_state, touched={a.phone for a in accounts})
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


def _await_workers(children, slots=None):
    """等全部子进程结束，期间按心跳周期刷新各槽位心跳；返回按槽位排列的退出码。

    `slots` = 各子进程对应的**槽位号**（省略 = 旧口径的 `0..len-1`）：心跳文件按槽位
    命名，清单模式下槽位号可以不连续（删中间行不重排），故不能拿"第几个子进程"当槽位。

    为什么不再逐个 `child.wait()`：心跳要覆盖**进程存活期间**（一轮可能十几分钟），
    只在开始/结束两个时刻写盘会让长轮次过了 2 × 周期就被判成"过期未收尾"、页面把
    正常在跑的轮次报成异常。轮询 `poll()` 在子进程退出的下一秒就能补收尾标记；
    退出码语义与逐个 wait **完全一致**（仍是每个子进程的真实返回码，按槽位排列）。

    收尾标记只在**正常退出**（返回码 >= 0）时写：被信号杀掉（返回码为负，如宿主
    `timeout` 的 SIGTERM/SIGKILL）时留"有开始、无收尾"，心跳过期后由接口判成
    `stale`——那正是需要用户注意的那种异常。
    """
    slots = list(range(len(children))) if slots is None else list(slots)
    codes = [None] * len(children)
    alive = set(range(len(children)))
    last_beat = {i: time.monotonic() for i in alive}
    while alive:
        time.sleep(_WAIT_POLL_SEC)
        for i in sorted(alive):
            rc = children[i].poll()
            if rc is None:
                if time.monotonic() - last_beat[i] >= state_io.WORKER_HEARTBEAT_SEC:
                    state_io.mark_worker_beat(slots[i])
                    last_beat[i] = time.monotonic()
                continue
            alive.discard(i)
            codes[i] = rc
            if rc >= 0:
                state_io.mark_worker_finished(slots[i], rc)
    return codes


def _child_env_with_repo_root(env):
    """在子进程环境里确保仓库根在 `PYTHONPATH` 上（已含则不重复追加）。"""
    paths = [p for p in env.get("PYTHONPATH", "").split(os.pathsep) if p]
    if _REPO_DIR not in paths:
        paths.insert(0, _REPO_DIR)
    env["PYTHONPATH"] = os.pathsep.join(paths)
    return env
