# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""**功能**
CLI 支撑：日志装配、进程级运行锁、状态文件读改写锁。

三件事都属"进程外壳"而非签到逻辑：谁在跑（单实例锁，防 cron 全量与手动 `--only`
并发登录同一账号）、日志写哪儿（按天文件 handler，装配延迟到入口、导入零副作用）、
状态文件怎么安全读改写（跨进程文件锁）。引擎其余部分与入口都依赖它们，故独立成模块。

**归属**
`yiban.engine` 的进程外壳支撑层（`runner` / `workers` / `round` / `probe` / `alerts`
与 web 服务层都依赖它）。

**复用**
`_setup_cli_logging`（幂等日志装配）、`_state_file_lock`（状态文件读改写锁）、
`GLOBAL_RUN_LOCK_NAME`、`_acquire_run_lock` 与 `_run_lock_held`；锁原语来自 `yiban.infra.locks`。
运行锁的两种拒绝信号是 `_RunLockHeld`（别人在跑）与 `_RunLockUnavailable`（本进程拿不到
互斥：锁文件不可写 / 平台无锁后端 / 等待超时），两者都 fail-closed，调用点按既有退出码族透出。

**通信**
输入：日志级别/路径等环境配置、状态文件路径与加锁范围。
输出：配置好的 logger/handler、被加锁的读改写上下文。
调用谁：`yiban.infra.env_io`、`yiban.infra.locks`、`yiban.logging_ext.FlockFileHandler`。
谁调用：`yiban/cli.py`（读 `last_fatal_error` 的 `--json` 摘要）、`runner`、`workers`、
`state_io`、`probe`、`alerts` 与 `scripts/signin.py` 兼容壳；web 服务层不调用本模块。
前端调用点：无直接调用点（前端经 `runner` / `state_io` 间接受影响）；执行体与手动签到
页面的"正在跑/存活"判定依赖本模块的全局运行锁。
本模块内部用裸名，跨模块一律走模块属性访问。
"""
import logging
import os
import sys
import time
from contextlib import contextmanager, suppress

from yiban import clock
from yiban.infra import env_io, locks
from yiban.logging_ext import FlockFileHandler, MaskingFormatter

logger = logging.getLogger("yiban")

try:
    import fcntl  # Unix/Linux 文件锁；Windows 不支持
except ImportError:
    fcntl = None

# 日志级别可用 YIBAN_LOG_LEVEL 调整：DEBUG / INFO / WARNING / ERROR
LOG_LEVEL = os.environ.get("YIBAN_LOG_LEVEL", "INFO").upper()

# 进程级签到单实例锁：全量模式等待其他进程退出的上限（秒）。
# 手动 --only 通常几十秒结束；cron 全量队列被手动阻塞时最多等这么久。
_RUN_LOCK_WAIT_DEFAULT = 600

#: **全局轮次锁**的文件名（"此刻是否有一轮全量/多执行体在跑"的唯一判据）。
#: 多执行体形态下只有监督进程持它、各子进程用各自的 `signin-run.lock.w{i}`；
#: 兜底常驻靠探测它决定是否让位（见 `_run_lock_held`）。
GLOBAL_RUN_LOCK_NAME = "signin-run.lock"

#: 迁移完整性拒启的专用退出码（MF-40）。`docs/dev/cli.md` §3 的 0/1/2/3/10 家族
#: **只增不改**，4 未被占用。触发链：`init_db(migrate=True)` →
#: `migrations._verify_migration_integrity` 抛 `db.MigrationIntegrityError`
#: （版本声称已过某迁移、但该迁移的完成记录或核心产物缺失）→ `runner.main` /
#: `workers.run_worker_supervisor` 捕获后以此码退出——调用方据此把"schema
#: 半升级（下次启动重试/人工介入）"与"配置错误(1)"区分开。
EXIT_SCHEMA_MIGRATION = 4

# CLI 日志装配幂等标记（见 _setup_cli_logging）
_cli_logging_ready = False


@contextmanager
def _state_file_lock(path):
    """状态文件读改写锁：经 `locks.file_lock` 统一（POSIX flock / Windows msvcrt）。

    锁文件由 locks 自行拼 `<path>.lock`，不要与日志 handler 的锁混用同一把。
    必须走这层统一原语：平台不支持文件锁时它会告警留痕，而各调用点的读-改-写
    （熔断暂停、按日状态、失败提醒额度）不加锁就会静默丢更新。
    """
    with locks.file_lock(path):
        yield


class _RunLockHeld(Exception):
    """签到锁被其他进程持有（--only 模式下由 _acquire_run_lock 抛出）。"""


class _RunLockUnavailable(RuntimeError):
    """运行锁**不可用**：本进程无法进入互斥保护区（锁文件不可写、平台无锁后端、等待超时）。

    与 `_RunLockHeld`（"别人正在跑"）分开，是因为处置不同但都必须 **fail-closed**：
    两者都不得"无锁继续"。无锁继续在多执行体下等于同一账号被两个进程并发真实登录
    （本项目第一红线），故拿不到互斥一律拒绝运行；调用点按既有退出码族把这一事实透出
    （见 `runner` / `workers` 各处 `except _RunLockUnavailable` 的注释）。
    """


#: `_run_lock_held` 的"探测不出"告警去重标记：兜底常驻每 `_YIELD_POLL_SEC` 探一次，
#: 不去重会把同一条平台缺陷刷满日志。
_PROBE_UNAVAILABLE_WARNED = False


def _acquire_run_lock(only_mode, name=None):
    """进程级签到单实例锁：防 cron 全量队列与手动 --only 并发签到同一账号。

    web 端的防抖/terminate 只覆盖 web 自己 spawn 的子进程，与 cron 全量队列之间没有
    任何互斥——同账号被两个进程并发登录易班会导致重复打卡/会话异常/风控画像。
    锁文件 <STATE_DIR>/signin-run.lock：
    - 全量模式：阻塞等待至多 YIBAN_RUN_LOCK_WAIT 秒（默认 600s），等到就继续；
      **超时即拒绝运行**（抛 `_RunLockUnavailable`）——旧行为是告警后"无锁继续"，
      那等于把"另一轮全量正在签同一批账号"这个事实忽略掉，两边各登录一次；
    - --only 模式：立即尝试一次，被持有则抛 `_RunLockHeld`（调用方退出并留痕，
      管理员稍后重试）——手动触发不应在 web 已返回的后台进程里排队阻塞。
    返回持锁文件句柄（flock 随进程退出自动释放）。

    **三条 fail-open 已全部收口**（拿不到互斥就拒绝，绝不交出未加锁的句柄）：
    等待超时、状态目录不可写/锁文件打不开（旧为静默 `return None`）、平台无 fcntl
    （旧为告警后返回未加锁句柄）。理由同第一红线：本锁是多执行体之外的**第二道**互斥
    （同机多个执行体进程、兜底与全量之间），静默失效等于让它在最需要的时候不存在。

    `name`：锁文件名，缺省取 `YIBAN_RUN_LOCK_NAME`（多执行体子进程用它换成自己的锁）
    再退到 `GLOBAL_RUN_LOCK_NAME`。**显式传参可绕过环境变量**——探测全局锁必须显式
    传（兜底进程自己的环境变量里放的是它自己的锁名）。
    """
    state_dir = env_io.resolve_path("YIBAN_STATE_DIR", "/var/log/yiban")
    # 多执行体形态下每个子进程用**各自的**锁文件（YIBAN_RUN_LOCK_NAME），全局锁由
    # 拉起它们的监督进程持有：这样既保住"散落的另一轮全量不得与本轮并发"的原有保护，
    # 又不让子进程之间互相阻塞。
    lock_name = name or os.environ.get("YIBAN_RUN_LOCK_NAME", "").strip() or GLOBAL_RUN_LOCK_NAME
    try:
        os.makedirs(state_dir, exist_ok=True)
        fh = open(os.path.join(state_dir, lock_name), "a+", encoding="utf-8")
    except OSError as e:
        logger.error(
            "签到运行锁不可用（状态目录不可写或锁文件打不开），本次拒绝运行: %s", e)
        raise _RunLockUnavailable(str(e)) from e
    if fcntl is None:
        # 无 fcntl 时锁退化为无互斥：管理员在 Windows 上跑多进程（cron + 手动）会
        # 静默出现同账号并发签到的可能（重复打卡/风控）。交出未加锁句柄等于把
        # "没有互斥"伪装成"持锁在跑"，故显式报不可用并拒绝运行。
        fh.close()
        logger.error(
            "当前平台无 fcntl，签到单实例锁不可用：无法保证同一账号不被并发真实登录，"
            "本次拒绝运行（请在 Linux/容器环境运行）"
        )
        raise _RunLockUnavailable("平台无 fcntl，运行锁不可用")
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
            fh.close()
            logger.error(
                "等待签到锁超时（%ss），无法取得互斥，本次拒绝运行（不再无锁继续："
                "另一轮可能正在签同一批账号）", wait_limit,
            )
            raise _RunLockUnavailable(f"等待签到锁超时（{wait_limit}s）")
        time.sleep(0.5)
        wait_sec += 0.5


def _run_lock_held(name=None):
    """此刻是否**有别的进程持着运行锁**（兜底常驻据此给全量轮让位）。

    做法就是"拿一下立刻放"：flock 只能靠尝试获取来问，拿到说明没人跑、当场释放。
    **显式传 `name`** 才能拿到全局锁的答案（不传时取 `YIBAN_RUN_LOCK_NAME`，
    而兜底进程自己的环境变量里放的是它自己的锁名）。

    代价：持有期间（微秒级）另一进程的 `--only` 手动签到可能被判成"队列忙"重试一次，
    概率极低且只影响一次手动触发；相比"兜底冲掉全量轮的计划"这个代价是划算的。

    **探测不出锁状态时按"有人在跑"处置**（`_RunLockUnavailable`，进程内只告警一次）：
    本函数只服务"该不该让位"这一个决策，而两种判错的代价不对称——错报"没人跑"会让
    兜底与全量轮抢同一批账号、把它精心错峰的计划冲掉；错报"有人在跑"只是兜底这一轮
    不捡漏（下一轮还会再看）。安全的判错方向是让位，故不再返回 False 假装锁不存在。
    """
    global _PROBE_UNAVAILABLE_WARNED
    try:
        fh = _acquire_run_lock(True, name=name or GLOBAL_RUN_LOCK_NAME)
    except _RunLockHeld:
        return True
    except _RunLockUnavailable as e:
        if not _PROBE_UNAVAILABLE_WARNED:
            _PROBE_UNAVAILABLE_WARNED = True
            logger.warning(
                "运行锁不可用（%s），无法探测是否有全量轮在跑：按'有人在跑'处置，"
                "兜底本轮让位（这条告警每进程只报一次）", e)
        return True
    if fh is not None:
        with suppress(OSError):
            fh.close()
    return False


# 按天日志文件路径（与 web/app.py log_path_for 一致）
def _signin_log_path():
    date_str = clock.now().strftime("%Y-%m-%d")
    log_file = env_io.resolve_path("YIBAN_LOG_FILE", "/var/log/yiban/sign.log")
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


# CLI 日志装配幂等标记（见 _setup_cli_logging）：装配必须**延迟到入口**，不能留在
# 模块导入期——否则 web/app.py 导入 signin 时就向 root 挂 FlockFileHandler，
# create_app 随后再挂 DailyFlockFileHandler（其去重守卫只认自身类），root 上出现
# 两个指向同一日志目录的 FileHandler，每条日志写两遍。
def _setup_cli_logging():
    """CLI 入口日志装配：把按天文件 handler 挂到 root logger。

    在 main() 入口调用（--check-config / --probe / --only 均经 main()，覆盖全部
    CLI 路径），故模块导入零副作用；幂等保护重复调用不重复挂载。
    """
    global _cli_logging_ready
    if _cli_logging_ready:
        return
    _cli_logging_ready = True
    handler = _make_log_handler()
    handler.setFormatter(MaskingFormatter(
        "[%(asctime)s] [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL, logging.INFO),
        handlers=[handler],
    )


#: 最近一次致命错误摘要（进程级，单线程 CLI 使用无需加锁）：`report_fatal_error`
#: 写入，`last_fatal_error` 读取。存在的理由：`--json` 模式下调用方只拿到退出码，
#: 需要一个机器可读的失败原因。
_LAST_FATAL_ERROR = None


def report_fatal_error(summary):
    """致命错误（配置加载失败/未配置任何账号类）：stderr 一行摘要 + 记录给 `--json`。

    为什么不能只靠 `logger.error`：CLI 日志装配只挂**按天文件** handler，stderr 上
    什么都没有——agent/CI 直调 `python -m yiban.cli sign` 时退出码 1 而 stdout/stderr
    全空，错误只进日志文件。stdout 仍保持"只有结果"
    （`docs/dev/cli.md` §2.2），摘要一律走 stderr；`--json` 的 error 字段由调用方
    （`yiban/cli.py`）经 `last_fatal_error()` 取用。
    """
    global _LAST_FATAL_ERROR
    _LAST_FATAL_ERROR = str(summary)
    try:
        sys.stderr.write(f"错误: {_LAST_FATAL_ERROR}\n")
        sys.stderr.flush()
    except (OSError, ValueError):
        pass  # stderr 不可写（已关闭/重定向坏）不得让致命错误处理本身再炸一次


def last_fatal_error():
    """最近一次 `report_fatal_error` 的摘要；无则 None。"""
    return _LAST_FATAL_ERROR


def clear_fatal_error():
    """清空上一轮的致命错误摘要（`runner.main` 入口调用）。

    没有这一步时，同一进程内多次调用 main（测试等场景）会把**上一轮**的失败原因
    附到本轮失败上——例如本轮是"队列忙（3）"而上轮是"未配置任何账号"，调用方
    拿到的 error 描述的是另一回事。生产进程一轮一次，这里只为进程内复用兜底。
    """
    global _LAST_FATAL_ERROR
    _LAST_FATAL_ERROR = None
