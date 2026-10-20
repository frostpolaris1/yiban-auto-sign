# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""**算法溯源**
- Heartbeat Failure Detector（心跳失效检测器）：分布式系统判活的标准手段，HashiCorp
  Consul 的 session TTL 文档即以此措辞定义。`worker_presence` 的四态（running / finished /
  idle / stale）是它在本仓的落地：写侧按 `WORKER_HEARTBEAT_SEC` 节流刷新，读侧按 2 倍周期
  判"新鲜"，超时即 `stale`。ZooKeeper 的 ephemeral node（会话结束即删除）与 etcd 的 lease
  （TTL 到期撤销）是同族机制。
- Lock Delay（锁延迟）：Consul 对应设计——旧持有者失效后**不允许立刻重夺**，须等一个延迟
  窗口。**本仓已不再用这套**：任务级租约族（含按时间戳判活与宽限期）随工单 `l1rx` 删除，
  崩溃自愈改由监督进程按进程句柄做（子进程退出码为负即回收其名下在飞行）；
  心跳的时间戳不再参与任何"该不该回收"的判定。
- File Lock（文件锁）：跨进程互斥的常用原语，POSIX `fcntl.flock`；本仓用于状态文件的
  读—改—写临界区（见 `yiban/infra/locks.py`）。

**功能**
状态文件读写与判定：按日状态、全量收尾标记、账密熔断状态、执行体心跳。

两类执行体心跳都在这里：兜底常驻的执行体心跳（写侧 `_write_fallback_alive`、读侧
`fallback_alive`）与并行执行体心跳（写侧 `mark_worker_*`、读侧四态判定的
`worker_presence`）。

**心跳只服务 UI，不参与任何正确性判定**（工单 `l1rx`）：任务级租约族删除后，崩溃自愈
由监督进程按**进程句柄**（子进程退出码）做，不再靠心跳时间戳；`worker_presence` 的四态
只喂执行体页的存活显示。

这些文件是**跨进程事实源**：网页日历读按日状态，容器调度器与宿主 `run.sh` 读全量
收尾标记与"未了结账号"，告警读兜底心跳与 sched-run 标记，执行体接口读并行心跳。
因此三条纪律不能破：


1. **原子写**（tmp + `os.replace`）——半截 JSON 会被下游误读成"没跑过"；
2. **带锁读改写**（`cli_support._state_file_lock`）——签到主进程、手动 `--only`、
   探针三个进程会同时碰这些文件；按日状态在轮内**进程内累积、整批落盘**，读改写
   发生在每次落盘里（见下文"写放大治理"），锁纪律不变；
3. **失效方向偏安全**——文件缺失/损坏一律按"尚未了结"处理，宁可多跑一轮不漏签。

**归属**
`yiban.engine` 的持久化口径层（按日状态 / 全量收尾标记 / 心跳）；告警、探针、Web
日历与容器调度器都按这些文件判"跑没跑、结没结"。

**复用**
状态码别名取自 `yiban.status`（唯一事实源）；`mark_worker_*` / `worker_presence`、
`fallback_alive`、按日状态读写函数被 `runner` / `workers` / `probe` / `alerts` 与
web 服务层复用。

**通信**
输入/输出：直接读写 `YIBAN_STATE_DIR` 下的 JSON 文件——页面与调度器读同一批文件，
故格式与键名是跨进程契约。
调用谁：`cred_state`、`cli_support._state_file_lock`、`schedule`、`db`、`env_io`。
谁调用：`runner`、`workers`、`round`、`probe`、`alerts` 与 web 的日历/执行体接口。
**依赖方向**：本模块只额外依赖 `schedule` 的 `_env_int`（兜底心跳的扫描间隔下限）与
`_env_flag`（`YIBAN_SECOND_RUN` 的真值判定，与 run.sh 同口径），
而 `schedule` 不依赖本模块，故模块级导入安全；`alerts` 反向依赖本模块（告警要读
sched-run 标记与心跳）与 `schedule`（判窗口是否还开着），那条环路由 alerts 侧断开。
前端调用点：`/api/scheduler/executors*`（执行体存活四态）、`/api/my-calendar`、
`/api/my-logs`、`/api/admin/sign-events`（按日状态与事件）——键名或失效方向变化会改变
日历着色、执行体行与日志列表。
跨模块一律走模块属性访问。
"""
import atexit
import contextlib
import json
import logging
import os
import threading
import time
from datetime import datetime

from yiban import egress
from yiban.adapters.config import env_io
from yiban.adapters.persistence import db
from yiban.adapters.statefiles import cred_state
from yiban.core import clock
from yiban.core import status as yiban_status
from yiban.core.masking import sanitize_text as _sanitize_text
from yiban.engine import cli_support, config_check, schedule
from yiban.infra import private_json

logger = logging.getLogger("yiban")

# 状态码别名（与 yiban.status 同一对象）
STATUS_SUCCESS = yiban_status.STATUS_SUCCESS
STATUS_ALREADY = yiban_status.STATUS_ALREADY
STATUS_NO_TASK = yiban_status.STATUS_NO_TASK
STATUS_PENDING = yiban_status.STATUS_PENDING

#: 兜底执行体的心跳文件（刷新时间戳；退出时删除）。用途：让"还有没有下一轮兜底"
#: 变成可查的**事实**，而不是靠猜时刻——告警抑制与网页展示都用它。
FALLBACK_ALIVE_FILE = "fallback-alive.json"
#: 心跳写失败只告警一次（每进程）：本函数每轮扫描都调，逐次告警会变成噪声。
_FALLBACK_ALIVE_WARNED = False

# 并行执行体心跳（写侧：监督进程；读侧：`worker_presence`）。
# 语义前提：并行执行体是**一轮就退出的短命进程**，不是常驻服务——"没在跑"在多数时间
# 是正常的（定时任务还没到 / 本轮已完成），故判定回四态而不是 alive 布尔，只有
# "有开始、无收尾且心跳过期"才判成异常，详见 `worker_presence`。
# 心跳文件用**固定名、每轮覆盖**（不按日命名）：文件数 = 执行体数，不随时间增长，
# 因此不需要清理策略。
#: 心跳文件名前缀：`worker-alive-<身份键>.json`。身份键 = **角色 + 槽位号**：
#: `worker` 角色用槽位号（`worker-alive-0.json`），`single` / `fallback` 用角色名
#: （`worker-alive-single.json` / `worker-alive-fallback.json`）——见 `worker_alive_path`。
WORKER_ALIVE_FILE_PREFIX = "worker-alive-"
#: 心跳周期（秒）。写侧按它节流刷新心跳，读侧按 2 × 它判"新鲜"。
WORKER_HEARTBEAT_SEC = 30
#: 四态取值（接口原样回给前端）。
WORKER_STATE_RUNNING = "running"
WORKER_STATE_FINISHED = "finished"
WORKER_STATE_IDLE = "idle"
WORKER_STATE_STALE = "stale"

#: 状态文件里的时间串格式（与全库统一口径一致）。
_TS_FMT = "%Y-%m-%d %H:%M:%S"


def _has_conclusion(entry):
    """该账号当日是否已有"结论"：`status` 非空且非 `pending`（未知状态串也算有结论）。

    `pending` 只是计划（"打算什么时候签"），success/failed 等才是事实；"空/缺失"
    等价于无记录。判据取自 `yiban.status.is_concluded_status`（唯一定义处）：
    `_write_sign_state` 的 only_if_absent CAS 与窗口收尾预筛共用这一口径。
    """
    if not isinstance(entry, dict):
        return False
    return yiban_status.is_concluded_status(entry.get("status", ""))


def sched_run_path(day=None, state_dir=None):
    """`sched-run-<day>.json` 的**唯一路径定义点**（census-P2-sched-run）。

    写入（`_write_sched_done`）、存在性（`_sched_marker_exists`）与 completed 真值
    （`full_run_done_today`）三处过去各手写一遍 f-string；改形状时只有一处被带上，
    其余静默读旧路径。现在全部引用本函数。
    """
    directory = state_dir or _state_dir()
    today = day or clock.now().strftime("%Y-%m-%d")
    return os.path.join(directory, f"sched-run-{today}.json")


def _sched_marker_exists():
    """当日全量运行标记（sched-run-<date>.json）是否已存在。

    真实消费者只有 `_is_second_run()`：它把"标记已存在"当作补签轮的兜底判据（环境变量优先），
    决定补签轮要不要剔除已了结账号。**告警函数已经不拿它分轮次**——
    `alerts._maybe_alert_zero_success` 在 `is_second_run=None` 时仍会来取一次值，但抑制判据换成了
    两个事实（窗口还开着、后面还有没有人接着跑），取到的值无人读取；所以别按"首签轮/补签轮"
    去理解这条告警。标记本身在首签轮收尾写入（_write_sched_done），与容器 scheduler.py 的
    _full_run_done_today() 是同一事实源。
    """
    return os.path.exists(sched_run_path())


def _is_second_run():
    """本轮是否为补签轮（07:10）：环境变量优先，sched-run 标记兜底。

    标记不能单独作数：首签子进程被宿主 timeout 击杀（exit 124）时收尾未执行、sched-run 不
    写，只看标记会把它误判为首签轮 → 部分成功 + 窗口外零告警。`YIBAN_SECOND_RUN` 由 run.sh
    补签轮分支 / 容器 scheduler SECOND 时段显式注入，不依赖首签收尾，故优先看它。

    取值域统一走 `schedule._env_flag`（1/true/on/yes，大小写与两侧空白不敏感）——与
    `run.sh` 的 `_is_truthy` 同一口径；旧实现 `== "1"` 只认一个字面量，同一键在
    bash 侧判补签轮、在此处判首签轮（census P0-1 的同键分歧形状）。
    """
    return schedule._env_flag("YIBAN_SECOND_RUN") or _sched_marker_exists()


def sign_state_path(day=None, state_dir=None):
    """`sign-state-<day>.json` 的**唯一路径定义点**（census-P2-sign-state）。

    写侧（`_sign_state_path` / `_write_sign_state`）与清理名册（`state_gc.ARTIFACTS`
    的前后缀）都从这里取形状；其他进程（网页日志页、ledger_check、backup.sh）
    仍各有读取点，收口它们须单独裁决（本函数先让写入侧不再各手写 f-string）。
    """
    directory = state_dir or _state_dir()
    today = day or clock.now().strftime("%Y-%m-%d")
    return os.path.join(directory, f"sign-state-{today}.json")


def _sign_state_path():
    """当日 sign-state 状态文件路径（状态目录缺失/不可写由调用方处理）。"""
    return sign_state_path()


# ---------------------------------------------------------------------------
# 按日状态的写放大治理：进程内累积 ＋ 整批落盘
# ---------------------------------------------------------------------------
# 为什么有它：`_write_sign_state` 原本**每账号一次**做"整体持锁＋读整个文件＋改一键＋
# 全量写回"，一轮的写盘量与账号数平方成正比（N 次调用 × 每次 O(N) 字节）；1,200 档实测
# 约 97 MB／轮，且在 asyncio 事件循环线程内同步写。轮内改为"进程内累积、按界整批落盘"，
# 写盘量降为 O(N × 批数)，单次阻塞时间随之降为原来的一个批次分之一。
#
# **不改磁盘契约**：落盘仍是同一份 `sign-state-<date>.json`、同一套键名与同一套
# `_write_private_json` 原子写通道，跨进程读者（网页日历/日志页、宿主 run.sh、容器调度器、
# v20 迁移、ledger_check、backup.sh）一律不必改。
#
# **崩溃语义与可见性（本批必须交代清楚）**：
# - 落盘触发＝三条任一：① 待写条数 ≥ `_FLUSH_MAX`；② 距上次落盘 ≥ `_FLUSH_SEC`；
#   ③ 批量上下文退出（轮末）或 `flush_sign_state()` 显式调用。
# - `_FLUSH_SEC` 默认 10s ＝**跨进程读者最快的轮询周期**（`/api/accounts`、`/api/logs`、
#   `/api/users`、仪表盘待签卡均为 10s 轮询；日历页只在进入时取一次）。检查是**事件
#   驱动**（每次写入时比对），不是后台定时器：写入停顿时陈旧窗口可以超过 10s，直到
#   下一次写入或上下文退出才收口。窗口因此是"至多相差一个落盘界"，不是硬时限。
# - 进程被信号/异常终止时由 `flush_sign_state()`（信号链 + `atexit`）冲刷；**硬崩溃
#   （SIGKILL / os._exit）会丢"最后一个落盘界之后"的尾部条目**——这正是 `_FLUSH_MAX` /
#   `_FLUSH_SEC` 存在的理由：把可能丢失的尾部界定在有界的小批内，而不是"整轮"。
#   **丢失的下游代价**：这些账号**已真实登录过**，但状态文件仍记 `pending`/无记录 ⇒
#   补签轮把它们当未了结、**再真实登录一次**（多余真实登录，本项目第一类代价）。
#   上界＝ `_FLUSH_MAX`（默认 64 条）或 `_FLUSH_SEC`（默认 10s）内的到达量。
#   **不采用"轮末一次性落盘"**：`executor_v3.py` 每次尝试即物化状态（防当天网页日历空窗，
#   见该文件 707 行注释）是既有设计，轮末一次性落盘会把它整段推翻。
# - 已落盘条目**永不回退**：后续 `pending` 不覆盖已有结论（与本文件既有规则同一份），
#   且落盘走 `os.replace`，崩溃不留半截文件。
# - **信号处理器不得阻塞**：`_STATE_GATE` 是**不可重入**的 `threading.Lock`，处理器跑在
#   主线程上；主线程若正在临界区内，处理器再取同名锁＝同线程自杀式阻塞（实测：持锁收
#   SIGTERM 后进程不再响应任何信号，宿主 `timeout` 只能升级成 SIGKILL ⇒ 缓冲真丢）。
#   故信号链一律走 `flush_sign_state(blocking=False)`：取不到锁就跳过，交给异常展开
#   （上下文退出）与 `atexit` 兜底（守卫见 tests/test_state_write_batching.py 的
#   `test_sigterm_in_critical_section_exits_promptly`）。
_STATE_BUFFERS = {}       # sign-state 路径 -> _StateBuf
_STATE_GATE = threading.Lock()
_STATE_BATCH_DEPTH = 0    # 批量上下文嵌套层数；0 = 逐条立即落盘（既有行为）

#: 待写条数上界；达到即落盘（回归守卫见 tests/test_state_write_batching.py）。
_FLUSH_MAX = config_check.parse_env_int("YIBAN_SIGN_STATE_FLUSH_MAX", 64)
#: 落盘间隔上界（秒）；未达条数上界时最多陈旧这么久。默认＝最快读者轮询周期。
_FLUSH_SEC = config_check.parse_env_int("YIBAN_SIGN_STATE_FLUSH_SEC", 10)


class _StateBuf:
    """一个按日状态文件的进程内缓冲：待写调用序列 ＋ 累积视图。"""

    __slots__ = ("last_flush", "ops", "view")

    def __init__(self):
        self.ops = []          # list[(phone, status, message, scheduled, dur, only_if_absent, now)]
        self.view = None       # 累积视图（CAS 与 scheduled 继承据此判；落盘后刷新）
        self.last_flush = time.monotonic()


@contextlib.contextmanager
def batch_sign_state():
    """进入"进程内累积"区；退出时把本区累积的所有条目整批落盘。

    退出即落盘是**轮末数据必须可见**的保证（否则轮末条目随上下文消失）。可重入，
    只有最外层退出才落盘。未进入本区时 `_write_sign_state` 逐条立即落盘（既有行为不变）。
    """
    global _STATE_BATCH_DEPTH
    with _STATE_GATE:
        _STATE_BATCH_DEPTH += 1
    try:
        yield
    finally:
        with _STATE_GATE:
            _STATE_BATCH_DEPTH -= 1
            outermost = _STATE_BATCH_DEPTH <= 0
        if outermost:
            flush_sign_state()
            with _STATE_GATE:
                _STATE_BUFFERS.clear()


def flush_sign_state(blocking=True):
    """把进程内累积的所有按日状态条目整批落盘（幂等；无待写条目时为空操作）。

    信号链与 `atexit` 用它兜住"进程被终止时缓冲随进程消失"（崩溃语义的网络）。
    `blocking=False` 供**信号处理器**使用：`_STATE_GATE` 不可重入（见上），主线程正在
    临界区内时处理器必须**立即返回**而不是等锁，否则同线程自杀式阻塞、进程不再响应
    任何信号。取不到锁就跳过；已落盘条目不受影响，未落盘的尾批交给异常展开（上下文
    退出）与 `atexit` 兜底。
    """
    if not _STATE_GATE.acquire(blocking=blocking):
        return
    try:
        paths = list(_STATE_BUFFERS)
    finally:
        _STATE_GATE.release()
    for path in paths:
        _flush_sign_state_path(path, blocking=blocking)


def _flush_sign_state_path(path, blocking=True):
    """把一个路径的待写条目整批并入磁盘最新内容后原子落盘（读改写整体持锁）。

    `blocking=False`：取不到缓冲锁即返回（信号处理器专用，见 `flush_sign_state`）。
    """
    if not _STATE_GATE.acquire(blocking=blocking):
        return
    try:
        buf = _STATE_BUFFERS.get(path)
        if buf is None:
            return
        ops = buf.ops
        buf.ops = []
        buf.last_flush = time.monotonic()
    finally:
        _STATE_GATE.release()
    if not ops:
        return
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with cli_support._state_file_lock(path):  # 读改写整体持锁，否则并发覆盖丢条目
            data = _read_sign_state_file(path)
            any_written = False
            for op in ops:
                # 旧逐条形态在 CAS 拒绝时**不写文件**；一批全被拒不写，与旧形态同权
                if _apply_sign_state(data, *op):
                    any_written = True
            if any_written:
                _write_private_json(path, data)
    except (OSError, ValueError, TypeError, AttributeError) as e:
        # 落盘失败：把这批条目**放回队首**（下一次落盘重试），不静默丢已收的结论。
        # 日志级别与逐条写路径一致（debug）：状态目录不可写时整轮失败，不由本函数定级。
        _requeue_sign_state(path, ops)
        logger.debug("写入状态文件失败（%s）: %s", path, _sanitize_text(e))
        return
    except BaseException:
        # 信号/中断（SystemExit 等）打断落盘：先还原条目再让异常继续展开，不丢已收结论
        _requeue_sign_state(path, ops)
        raise
    if not any_written:
        return
    with _STATE_GATE:
        buf = _STATE_BUFFERS.get(path)
        if buf is not None:
            buf.view = data


def _requeue_sign_state(path, ops):
    """把一批未落盘的条目放回队首（保序），交给下一次落盘重试。"""
    with _STATE_GATE:
        buf = _STATE_BUFFERS.get(path)
        if buf is not None:
            buf.ops = ops + buf.ops


def _read_sign_state_file(path):
    """读按日状态文件；缺失/损坏/非 dict 一律按空数据（含既有告警文案）。"""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8-sig") as f:  # utf-8-sig：带 BOM 的文件不得被判损坏清空
            data = json.load(f)
    except (OSError, ValueError, TypeError, AttributeError):
        logger.warning("状态文件 %s 损坏，按空数据重建", path)
        return {}
    if not isinstance(data, dict):
        logger.warning("状态文件 %s 非 dict，按空数据重建", path)
        return {}
    return data


def _apply_sign_state(data, phone, status, message, scheduled, dur, only_if_absent, now=None):
    """把一次写入并入 `data`（就地），返回与旧实现逐字相同的布尔（`only_if_absent` 的 CAS 结果）。

    三条既有规则逐字复刻：`scheduled` 继承、`only_if_absent` 只写无结论账号、
    `pending` 计划态不覆盖已有结论。逐条写与整批落盘两条路径共用本函数，保证
    "批量产出 == 逐条产出"（等价性判据见 tests/test_state_write_batching.py）。

    `now`：本条结论的**发生时刻**。整批落盘时传入的是**调用时刻**（不是落盘时刻）——
    `time` 字段是"这个账号什么时候签的"，落盘被推迟不得改变它。
    """
    now = now or clock.now()
    existing = data.get(phone)  # 计划时间是当日事实：未显式传 scheduled 时保留既有值
    if not scheduled and isinstance(existing, dict):
        scheduled = existing.get("scheduled")
    if only_if_absent and _has_conclusion(existing):
        return False
    # 计划态不覆盖已有结果：pending 是"打算签"，success/failed 是"已发生"，事实优先。
    # 多执行体下每个执行体启动都写一遍全量计划，晚启动者的计划会把先启动者已写
    # 的结果抹回 pending（实测 4 执行体/40 账号抹掉 2 个），日历显示待签、补签重跑。
    if (
        status == STATUS_PENDING
        and isinstance(existing, dict)
        and str(existing.get("status", "")).strip() not in ("", STATUS_PENDING)
    ):
        if scheduled:
            existing["scheduled"] = scheduled
        data[phone] = existing
        return True
    entry = {
        "status": status,
        "message": message,
        "time": now.strftime("%H:%M:%S"),
        "task": "default",
    }
    if dur is not None:
        entry["dur"] = round(float(dur), 2)  # 两位秒足够判慢响应，不必留微秒
    if scheduled:
        entry["scheduled"] = scheduled
    data[phone] = entry
    return True


# 进程退出兜底：正常退出/异常退出（atexit 覆盖 SystemExit 与未捕获异常）冲刷缓冲。
atexit.register(flush_sign_state)


def _daily_statuses():
    """当日按日状态文件的 {phone: status}；缺失/损坏返回 {}（调用方按"无记录"处理）。

    条目缺 `status` 键按空串——与 `_has_conclusion` 同口径（"无记录"，而非"一条
    状态为 None 的结论"）：窗口收尾拿本快照预筛、落盘再走 `_has_conclusion` 的
    CAS，两处口径必须一致，否则预筛判"已有结论"而 CAS 判"可以写"。
    批量区内的进程内待写条目也计入（否则同进程的预筛与 CAS 会各判一套）。
    """
    path = _sign_state_path()
    with _STATE_GATE:
        buf = _STATE_BUFFERS.get(path)
        data = buf.view if (buf is not None and buf.view is not None) else None
    if data is None:
        data = _read_sign_state_file(path)
    if not isinstance(data, dict):
        return {}
    return {p: (v.get("status", "") if isinstance(v, dict) else "") for p, v in data.items()}


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
    # 已了结 = `yiban.status.CLAIM_DONE_STATUSES`（success/already/**no_task**：no_task
    # 指"今天没任务"，同样无需重跑）。与领取池记 `done` 的判据是**同一对象**——两处
    # 各写一份会漂移，让补签轮对这些账号再走一遍完整登录。
    done = {
        p for p, st in recorded.items()
        if st in yiban_status.CLAIM_DONE_STATUSES
    }
    if not done:
        return accounts
    kept = [a for a in accounts if a.phone not in done]
    logger.info(
        "补签轮定向重跑：%d 个账号当日已了结不再重跑，本次执行 %d 个",
        len(accounts) - len(kept), len(kept),
    )
    return kept


def _write_sign_state(phone, status, message, scheduled=None, dur=None,
                      only_if_absent=False):
    """写按日结构化状态文件（web 状态显示的事实源，原子替换防半截文件）。

    文件：{YIBAN_STATE_DIR}/sign-state-YYYY-MM-DD.json
    结构：{phone: {status, message, time, task}}；task 预留多时段/多星期签到扩展。
    scheduled：今日计划签到时间（HH:MM:SS，自动错峰分配后写入，执行后保留）。
    dur：单次签到尝试耗时秒数（慢响应可据此判断网络/接口问题）。
    only_if_absent：仅当该账号**当日无结论**时才写入（return True），否则不覆盖返回
    False。窗口收尾（`round._mark_window_skip`）拿它做 CAS：锁内再判一次，绝不让"窗口外
    跳过"覆盖掉真实失败（`has_real_failure` 随之变 False、失败告警被吞）。
    状态目录不可写时丢弃，不影响签到执行。

    **批量区（`batch_sign_state()`）内**：本调用只并入进程内缓冲（规则与返回值不变），
    由 `_FLUSH_MAX` / `_FLUSH_SEC` / 上下文退出触发整批落盘（见上方"写放大治理"说明）；
    区外逐条立即落盘，行为与本函数引入本机制之前逐字相同。

    **`only_if_absent=True` 是例外：它立即落盘，不进缓冲**。它是一条 CAS，判定与写入
    必须原子（旧形态在锁内一次性判定并写）。若把写推迟，判定时刻与落盘时刻之间会出现
    "返回 True 而盘上已是别的结论"的分歧：调用方（`_mark_window_skips`）按返回值把该号
    记成 `skipped_window`，而日历/状态行按盘上结论——两个事实源不一致。故本路径先冲刷
    本路径已有条目（让盘上含本轮结论），再走原子读改写。代价是该路径回到逐条写盘，
    但调用点只有窗口收尾那两处，且只对"本轮没轮到"的账号发。
    """
    path = _sign_state_path()
    if only_if_absent:
        _flush_sign_state_path(path)  # 让盘上含本轮已有结论，CAS 才看得见它们
        return _write_sign_state_now(path, phone, status, message, scheduled, dur, True)
    with _STATE_GATE:
        buf = None
        if _STATE_BATCH_DEPTH > 0:
            buf = _STATE_BUFFERS.get(path)
            if buf is None:
                buf = _StateBuf()
                _STATE_BUFFERS[path] = buf
        if buf is not None:
            if buf.view is None:
                buf.view = _read_sign_state_file(path)
            now = clock.now()  # 落盘被推迟，但 time 字段是"何时签的"：按调用时刻定
            ok = _apply_sign_state(buf.view, phone, status, message, scheduled, dur,
                                   only_if_absent, now)
            buf.ops.append((phone, status, message, scheduled, dur, only_if_absent, now))
            due = (len(buf.ops) >= _FLUSH_MAX
                   or (time.monotonic() - buf.last_flush) >= _FLUSH_SEC)
    if buf is not None:
        if due:
            _flush_sign_state_path(path)
        return ok
    return _write_sign_state_now(path, phone, status, message, scheduled, dur,
                                 only_if_absent)


def _write_sign_state_now(path, phone, status, message, scheduled, dur, only_if_absent):
    """逐条立即落盘（批量区外与既有单测走这里）：旧 `_write_sign_state` 的读改写路径原样保留。"""
    state_dir = _state_dir()
    try:
        os.makedirs(state_dir, exist_ok=True)
        with cli_support._state_file_lock(path):  # 读改写整体持锁，否则并发覆盖丢条目
            data = _read_sign_state_file(path)
            if not _apply_sign_state(data, phone, status, message, scheduled, dur,
                                     only_if_absent):
                return False  # CAS 拒绝：与旧实现一致，不动文件
            # 唯一临时名与 0600 创建由单通道保证（防跨进程固定 .tmp 名互相覆盖）
            _write_private_json(path, data)
            return True
    except (OSError, ValueError, TypeError, AttributeError) as e:
        # 状态目录不可写/写入异常时丢弃但不静默：debug 留痕（不影响签到执行）；
        # 异常消息经 _sanitize_text 脱敏（sqlite/json 异常可能回显 cookie/csrf 值）
        logger.debug("写入状态文件失败（%s）: %s", path, _sanitize_text(e))
        return False

# ---------------------------------------------------------------------------
# 全量收尾标记（调度器首签/补签闸门的事实源）
# ---------------------------------------------------------------------------
def _write_sched_done(counts=None):
    """写入当日「全量签到已运行」标记（sched-run-<date>.json）——调度器闸门事实源。

    调度器原 `_signed_today()` 以「任一账号 success」判定当日已签，
    手动签到/部分成功都会压制全站首签与补签。新契约：仅全量模式在收尾写本标记
    （--only 手动签到与 --probe 探针不写），调度器据此判定：
    - 首签：标记不存在 → 执行；
    - 补签：标记不存在，或存在未了结账号（failed/retrying/pending）→ 执行。
    """
    state_dir = _state_dir()
    path = sched_run_path(state_dir=state_dir)
    try:
        os.makedirs(state_dir, exist_ok=True)
        payload = {
            "completed": True,
            "finished_at": clock.now().strftime("%H:%M:%S"),
        }
        if isinstance(counts, dict):
            payload.update(counts)
        _write_private_json(path, payload)
    except OSError as e:
        logger.warning("写入全量完成标记失败（调度器可能重复触发当日签到）: %s", e)

# ---------------------------------------------------------------------------
# 补签轮判定（宿主 run.sh 与容器 docker/scheduler.py 共用的单一实现）
# ---------------------------------------------------------------------------
# 背景：宿主原先靠**第二个独立 cron**（07:12）做补签，
# 但首签进程要 sleep 到最晚自选时间片（生产实测 07:25）才结束，flock 由脚本持有至
# 退出 → 07:12 的 cron 每天撞锁 `exit 0`，补签轮从未真正执行（生产日志 12/12 天实证）。
# 修法：宿主改为与容器同语义——**同一进程内**首轮结束后再判定
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


def _state_dir():
    """按日状态目录：环境变量 → .env → 裸机默认。

    口径与 web/app.py 同源（`env_io.resolve_path`）。过去只读 os.environ，于是把
    `YIBAN_STATE_DIR` 写进 .env 的部署里，`run.sh`（自己 export）与直接调用的脚本
    会落到**不同目录**——一边是配置目录、一边是 /var/log/yiban。
    """
    return env_io.resolve_path("YIBAN_STATE_DIR", "/var/log/yiban")


def full_run_done_today(state_dir=None, day=None):
    """当日全量签到是否已收尾（sched-run-<date>.json 的 completed 标记）。"""
    path = sched_run_path(day=day, state_dir=state_dir)
    try:
        # utf-8-sig：容错 Windows 手工/工具写入的 BOM（与 _load_cred_state 同口径）
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and bool(data.get("completed"))


def has_undone_accounts_today(state_dir=None, day=None):
    """当日是否存在未了结账号；无记录/文件缺失/损坏按"未了结"处理（fail-safe 侧）。

    **两个事实源取并集，任一"有活"即未了结**：任务队列回答"领到的那些了结没有"
    （`done`/`skipped`=当日了结，`pending`/`claimed`/`failed`/`stolen`=未了结），状态文件
    回答"每个账号最后写成什么状态"。旧实现池里有行就只看池——一个执行体半途被杀/漏领时，
    已收尾的行全 `done`、没领过的账号在池里根本没有行，池判"无未了结"，而这批账号在状态
    文件里仍是 pending（计划从未被执行）——"整批没领被判没活"的跨轮翻版。故池判干净后
    仍要过一遍状态文件；池里没有行（无库/池未启用/当日还没人领过）时状态文件是唯一
    事实源，行为不变。台账单池化后读的是**任务队列**（唯一生产台账），不是旧领取池。

    **队列侧必须走 `db.task_open_count`（带 `vshard >= 0` 过滤），不能用
    `day_counts(day)["open"]`**：本函数没有分片上下文（宿主 run.sh / 容器调度器只给
    状态目录与业务日），不能调 `pending_count(day, vshards)`；而 `day_counts` 不过滤
    分片，历史平移/补账写入的 `vshard=-1`/`failed` 行永不被领取却属未了结态，把它算进来
    该日就**永远不了结**、补签轮反复空跑（与 `queue_store.open_count` 的核心口径一致）。

    **库读不通按"未了结"处理并出声（ba-p01-01）**：`db.task_open_count` 读不通回 `None`
    哨兵，读取抛异常同理。两者都说明"数不出未了结行"不等于"没有未了结行"。此时只回退
    状态文件，会让"状态文件干净 + 库读不通"判成了结。补签轮于是不跑 = 漏签。
    故这两条路径一律答"仍有未了结"，并各记一条 WARNING（不抛，补签轮照常起）。
    `is_initialized()` 为假不算读失败：那是"本部署没配库"的形态，照旧只读状态文件。
    """
    today = day or clock.now().strftime("%Y-%m-%d")
    try:
        if db.is_initialized():
            open_rows = db.task_open_count(today)
            if open_rows is None:
                logger.warning("任务队列读不通，数不出未了结行，按未了结处理: day=%s", today)
                return True
            if open_rows > 0:
                return True
    except Exception as e:
        logger.warning("读取任务队列失败，数不出未了结行，按未了结处理: %s", e)
        return True
    d = state_dir or _state_dir()
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
    state_dir = _state_dir()
    return os.path.join(state_dir, "cred-state.json")


def _load_cred_state():
    """读账密状态文件（唯一入口见 yiban/adapters/statefiles/cred_state.py）。"""
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

# ---------------------------------------------------------------------------
# 兜底执行体心跳（读侧：告警据此判断"后面还有没有人接着跑"）
# ---------------------------------------------------------------------------
def _fallback_alive_path():
    return os.path.join(_state_dir(), FALLBACK_ALIVE_FILE)


def _write_fallback_alive(at=None):
    """刷新兜底执行体心跳（每轮扫描写一次）。

    census-P2-fallback-alive：写失败**不能零留痕**——心跳缺失会让告警抑制把
    "后面还有人接着跑"判成假，进而吞掉当天应有的告警。失败按进程节流打一条
    warning（每进程至多一条；本函数每轮扫描都调，逐次告警会变成噪声）。
    """
    global _FALLBACK_ALIVE_WARNED
    path = _fallback_alive_path()
    try:
        _write_private_json(path, {"at": (at or clock.now()).strftime(_TS_FMT),
                                   "pid": os.getpid()})
    except OSError as e:
        if not _FALLBACK_ALIVE_WARNED:
            _FALLBACK_ALIVE_WARNED = True
            logger.warning("写入兜底心跳失败（告警抑制可能误判）: %s", _sanitize_text(e))


def _clear_fallback_alive():
    with contextlib.suppress(OSError):
        os.remove(_fallback_alive_path())


def fallback_alive(interval_sec=None, now=None):
    """兜底执行体是否在跑（按心跳新鲜度判定：超过 2 个扫描间隔即视为已停）。

    返回 `(alive: bool, age_sec: float | None)`。判定用**文件里的时间戳**而非文件是否存在：
    进程被 kill -9 时不会执行清理，只靠"文件还在"会永远报"在跑"。
    """
    path = _fallback_alive_path()
    try:
        with open(path, encoding="utf-8") as f:
            at = json.load(f).get("at", "")
        stamp = datetime.strptime(at, _TS_FMT)  # noqa: DTZ007 解析状态文件业务本地 naive 时间串
    except (OSError, ValueError, TypeError, AttributeError):
        return False, None
    now = now or clock.now()
    age = (now - stamp).total_seconds()
    limit = 2 * (interval_sec or schedule._env_int("YIBAN_FALLBACK_INTERVAL", 60, 5, 3600))
    return age <= limit, age


# ---------------------------------------------------------------------------
# 并行执行体心跳：写侧（监督进程调用）与读侧（执行体接口调用）
# ---------------------------------------------------------------------------
def worker_alive_key(index, role=None):
    """心跳身份键：角色的唯一口径在 `yiban.egress`（`worker` / `single` / `fallback`）。

    `worker` 用槽位号（身份串 `worker-{i}@host` 里带得出），`single` / `fallback` 用
    角色名——后两者的身份串都没有槽位号，`executor_v3._worker_slot` 会把它们都折成 0，
    若仍按纯槽位号命名，三者会共用 `worker-alive-0.json`：兜底每轮收尾把 worker-0 的
    心跳洗成 finished/running，四态被**别的身份**的退出污染。`role=None` 按 `worker`
    （既有纯槽位调用点的行为逐字不变）。
    """
    if role is None or role == egress.ROLE_WORKER:
        return int(index)
    return str(role)


def worker_alive_path(index, role=None):
    """并行执行体心跳文件路径（`<状态目录>/worker-alive-<身份键>.json`）。

    `index` / `role` 是**写侧与读侧共享的同一份身份口径**（见 `worker_alive_key`）：
    调用方必须把同一个 `(index, role)` 同时用在 `mark_worker_*` 与 `worker_presence`
    上，否则读到的是别的槽位/角色的心跳。worker 的心跳文件名与旧实现逐字相同
    （`worker-alive-<槽位>.json`），已有部署的存量文件不受影响。
    """
    key = worker_alive_key(index, role)
    return os.path.join(_state_dir(), f"{WORKER_ALIVE_FILE_PREFIX}{key}.json")


def worker_alive_identity(path):
    """心跳文件名 → 身份档位 `(role, index)`；不认识的文件名回 None。

    身份的唯一事实源是**文件名**（`worker_alive_key` 的逆函数）：记录体里的
    `role` / `index` 不可信。`_alive_record` 把 `role` 写死 `"worker"`、`index`
    写成调用方传入的槽位号，而 `single` / `fallback` 的槽位号在
    `executor_v3._worker_slot` 里都被折成 0——于是生产实测的兜底心跳文件
    内容自称 `{"role": "worker", "index": 0}`（工单 ba-p03-09，OPEN）。
    凡按身份展示或归因的读者一律走本函数，
    不得读记录体字段。

    `path` 可取完整路径、文件名或身份键三者的任一种形态（都按 basename 与
    前缀/后缀剥壳）。
    """
    base = os.path.basename(str(path or ""))
    if not base.startswith(WORKER_ALIVE_FILE_PREFIX):
        return None
    if not base.endswith(".json"):
        return None
    key = base[len(WORKER_ALIVE_FILE_PREFIX):-len(".json")]
    if key.isascii() and key.isdigit():
        slot = int(key)
        return (egress.ROLE_WORKER, slot) if slot >= 0 else None
    if key in (egress.ROLE_SINGLE, egress.ROLE_FALLBACK):
        return (key, 0)
    return None


def iter_worker_alive_files(state_dir=None):
    """状态目录里现存的心跳文件完整路径（按文件名升序，顺序稳定）。

    巡检面据此列出"真在写心跳的执行体"——包括清单里没有的（孤儿）：那些恰恰是
    "配置说没有、机器上却在跑"的信号。目录不可读按空列表（展示面不抛）。
    """
    directory = state_dir or _state_dir()
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    return [os.path.join(directory, n) for n in sorted(names)
            if worker_alive_identity(n) is not None]


def mark_worker_started(index, now=None, role=None):
    """记录"该槽位本轮已启动"（覆盖写，旧记录连同收尾标记一起被替换）。

    由监督进程在拉起子进程时调用——子进程是短命进程，且写心跳需要槽位序号，
    而序号只有监督进程有。`role` 见 `worker_alive_key`（单执行体 / 兜底传各自角色，
    免得与 worker-0 撞同一文件）。失败只留 debug 痕迹：心跳不该影响签到主流程。
    """
    at = now or clock.now()
    path = worker_alive_path(index, role)
    try:
        with cli_support._state_file_lock(path):
            _write_private_json(path, _alive_record(index, at))
    except OSError as e:
        logger.debug("写入执行体心跳失败（不影响签到）: %s", _sanitize_text(e))


def mark_worker_beat(index, now=None, role=None):
    """刷新该槽位心跳的 `ts`（保留 `started_at`）——存活期间的周期写盘（`role` 同上）。"""
    at = now or clock.now()
    path = worker_alive_path(index, role)
    try:
        with cli_support._state_file_lock(path):
            data = _read_worker_alive(index, role) or _alive_record(index, at)
            data["ts"] = at.strftime(_TS_FMT)
            _write_private_json(path, data)
    except OSError as e:
        logger.debug("刷新执行体心跳失败（不影响签到）: %s", _sanitize_text(e))


def mark_worker_finished(index, exit_code=None, now=None, role=None):
    """记录"该槽位本轮已正常退出"：在同一文件上补收尾标记（`ended_at`）（`role` 同上）。

    被信号杀掉（退出码为负）时调用方**不调本函数**：留"有开始、无收尾"，心跳过期后
    由 `worker_presence` 判成 `stale`——那正是"疑似被强杀/超时杀掉"需要用户注意的状态。

    **退出码 >= 0 一律算正常退出**，含子进程"本轮失败"（如 v3 返回空结果、runner 汇总成
    退出码 1）：那也会写收尾判 `finished`，失败信息靠退出码与告警体现，不由四态承担。
    只有**单进程直跑**（没有监督进程替它写收尾）时内部未预期异常才留"有开始、无收尾"。
    """
    at = now or clock.now()
    path = worker_alive_path(index, role)
    try:
        with cli_support._state_file_lock(path):
            data = _read_worker_alive(index, role) or _alive_record(index, at)
            data["ended_at"] = at.strftime(_TS_FMT)
            data["ts"] = at.strftime(_TS_FMT)
            if exit_code is not None:
                data["exit_code"] = int(exit_code)
            _write_private_json(path, data)
    except OSError as e:
        logger.debug("写入执行体收尾标记失败（不影响签到）: %s", _sanitize_text(e))


def worker_presence(index, now=None, role=None):
    """该并行执行体槽位此刻的存活四态 → `(state, last_seen_at)`（`role` 见 `worker_alive_key`）。

    | state | 判据 | 页面该做什么 |
    |-------|------|--------------|
    | `running` | 有心跳且新鲜（`now - ts <= 2 × WORKER_HEARTBEAT_SEC`） | 在线（正在跑本轮） |
    | `finished` | 有本轮收尾标记（`ended_at`，正常退出） | 已跑完（灰） |
    | `idle` | 本业务日无该槽位记录（今天还没跑） | 未运行（灰） |
    | `stale` | 有开始记录、无收尾，且心跳已过期 | **异常**（可能被强杀/超时杀掉；单进程直跑时内部未预期异常也不写收尾） |

    `last_seen_at` 是"最后一次见到它活着"的时间串（收尾态给 `ended_at`；无记录给
    None）。**为什么不回 alive 布尔**：短命进程"没在跑"多数时候是正常的，只有
    `stale` 才需要用户注意；把四态判定放在后端一处，前端不必自己拼（也不用知道
    心跳周期这种部署细节）。本函数只回与身份串无关的槽位事实，不含 pid/主机名。

    本函数是 `worker_alive_facts` 的二值投影：四态与新鲜度的判据只有那一份实现。
    """
    facts = worker_alive_facts(index, now=now, role=role)
    return facts["state"], facts["last_seen_at"]


def worker_alive_facts(index, now=None, role=None):
    """该槽位/角色此刻的心跳事实（四态 + 新鲜度）→ dict；判据的唯一实现处。

    `worker_presence` 是本函数的 `(state, last_seen_at)` 投影（既有调用面不变）。
    巡检面的健康卡要"心跳新鲜度 + 最后活动时刻"，两者都由本函数给出，免得前端
    或第二个读点各算一遍超时。

    返回键：
      `state`        四态之一（`WORKER_STATE_*`）
      `heartbeat_at` 最近一次心跳的时刻串（收尾态取 `ended_at`；无记录为 None）
      `age_sec`      心跳年龄（秒，`now - heartbeat_at`）；无记录/时刻读不出为 None
      `fresh`        是否为**活**心跳：`running` 为 True、`stale` 为 False，
                     收尾态与无记录为 None（新鲜度对它们无从说起，不编 0/False）
      `last_seen_at` 最后一次见到它活着（与 `heartbeat_at` 同值；无记录为 None）
      `day`          记录所属业务日（无记录为 None）
    """
    now = now or clock.now()
    data = _read_worker_alive(index, role)
    idle = {
        "state": WORKER_STATE_IDLE, "heartbeat_at": None, "age_sec": None,
        "fresh": None, "last_seen_at": None, "day": None,
    }
    # 无记录，或记录属于**别的业务日**（昨天的"已跑完"不等于今天已跑）→ 今天还没跑
    if not data or str(data.get("day") or "") != now.strftime("%Y-%m-%d"):
        return idle
    day = str(data.get("day") or "") or None
    if data.get("ended_at"):
        stamp = _parse_stamp(data.get("ended_at"))
        seen = stamp.strftime(_TS_FMT) if stamp else None
        return {"state": WORKER_STATE_FINISHED, "heartbeat_at": seen,
                "age_sec": (now - stamp).total_seconds() if stamp else None,
                "fresh": None, "last_seen_at": seen, "day": day}
    seen_dt = _parse_stamp(data.get("ts") or data.get("started_at"))
    if seen_dt is None:
        # 有记录却读不出时间：新鲜度无从判定。宁可提示"异常"也不静默当在线
        # （页面把 stale 提示成"可能被杀"，比漏报一个卡住的槽位安全）。
        return {"state": WORKER_STATE_STALE, "heartbeat_at": None, "age_sec": None,
                "fresh": False, "last_seen_at": None, "day": day}
    age = (now - seen_dt).total_seconds()
    last_seen = seen_dt.strftime(_TS_FMT)
    if age <= 2 * WORKER_HEARTBEAT_SEC:
        return {"state": WORKER_STATE_RUNNING, "heartbeat_at": last_seen,
                "age_sec": age, "fresh": True, "last_seen_at": last_seen, "day": day}
    return {"state": WORKER_STATE_STALE, "heartbeat_at": last_seen,
            "age_sec": age, "fresh": False, "last_seen_at": last_seen, "day": day}


def _read_worker_alive(index, role=None):
    """读该槽位/角色的心跳（缺失/损坏/非 dict → `{}`，与其余状态文件同口径不抛）。"""
    try:
        # utf-8-sig 容错 Windows 记事本/工具写入的 BOM（与 cred_state 同口径）
        with open(worker_alive_path(index, role), encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_private_json(path, payload):
    """原子写 JSON、**创建即 0600**——状态文件私有写的唯一通道。

    实现在 `yiban.infra.private_json`（infra 层，熔断/账本/调度器等不能反向依赖
    engine 的模块也共用同一条通道）；此处保留名字作为既有锚点，不再自带一份
    实现——"补 chmod"劣于"所有站点只走一个通道"。
    """
    private_json.write_private_json(path, payload)


def _alive_record(index, at):
    """心跳记录的公共字段（开始与收尾写同一组，收尾只多 `ended_at`）。"""
    stamp = at.strftime(_TS_FMT)
    return {"day": at.strftime("%Y-%m-%d"), "role": "worker", "index": int(index),
            "started_at": stamp, "ts": stamp}


def _parse_stamp(value):
    """状态文件里的时间串 → datetime；读不出返回 None。"""
    try:
        return datetime.strptime(str(value or ""), _TS_FMT)  # noqa: DTZ007 解析状态文件业务本地 naive 时间串
    except (TypeError, ValueError):
        return None
