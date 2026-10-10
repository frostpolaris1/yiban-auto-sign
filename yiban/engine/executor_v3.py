# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""**算法溯源**
- Work Stealing / Self-Scheduling（自调度与工作窃取）：Richard D. Blumofe、Charles E.
  Leiserson，*Scheduling Multithreaded Computations by Work Stealing*（FOCS 1994 /
  JACM 1999）。本模块取其**分布式形态**——共享任务池 + 执行体轮询领取（Temporal 的
  Task Queue、Celery 的队列模型属同类），而非内存内的"偷队列"：跨进程没有共享内存可偷，
  仲裁者只能是数据库。
- Exponential Backoff with Jitter（指数退避加抖动）：`next_retry_at_v3` 用**Decorrelated
  Jitter** 变体，出自 Marc Brooker（AWS）抖动一文的三变体比较（Full / Equal /
  Decorrelated）。无抖动的指数退避会让同一批失败任务每轮同步重试（惊群）；Equal Jitter
  保留固定下界，次生尖峰最重；本模块取 Decorrelated 是因其上界随上次抖动放大，在风控
  场景下比 Full Jitter 更保守。
- Adaptive Concurrency Limits（自适应并发上限）：通道数 M 由限速配置推导而非写死，思路
  出自 Netflix *Rethinking Concurrency Control for Microservices*（Mahadut 等），同项
  溯源见 `token_bucket.py`。
- Fencing Token（栅栏令牌）：Martin Kleppmann。领取自增 `epoch`、结算按 epoch 比对，同项
  溯源见 `yiban/store/queue_store.py`；`queue_store.reap_expired` 的豁免名册（本模块的
  `_live_row_owners`）与 `_widen_with_dead_peers` 按心跳四态把"持锁者其实还活着"从误判里
  豁免出来——这正对应 Consul 的 lock-delay 一类设计。
- Priority Queue + Delayed Requeue（优先级队列与延迟重投）：任务按 `(priority, run_at)`
  进 `asyncio.PriorityQueue`；重试未成的行退回 `pending`、`run_at` 推到未来，下一拍重新
  可领，是 SQS 延迟队列一类的"定时再投"形态。

**功能**
调度 v3 的执行体核心：单进程 asyncio **M 条通道**从 `sign_tasks` 批量领取自己分片集内
到点的任务，按计划时刻并发执行（`attempts.attempt_signin` 整体经 `asyncio.to_thread`
提交，线程池上限 = M），出口令牌桶限速、每账号 gap 门、装订式抖动退避、终态批量收尾。

**归属**
`yiban.engine` 的执行层（调度 v3）。计划层（`planner`）决定"何时做"、`token_bucket`
决定"多快做"，本模块把两者与队列消费接起来；入口与退出码汇总仍在 `runner`。

**是否生效**：是——台账单池化后本模块是**唯一的生产执行体**，`runner.main`（定时全量、
手动 `--only`）与兜底常驻都恒定调 `run_executor_v3`。旧领取池（`sign_claims`）的实现
（`round.run_queue_retry` / `store.claims`）保留在仓内但已无生产调用点（冻结）。

**复用**
`next_retry_at_v3` 是重试落点（与旧领取池的 `round._next_retry_at` 同窗口准绳、不同采样）；
`shadow_stats` 是影子期（`dry_run`）的落点对账入口。

**通信**
输入：账号序列、业务日、可选 `cfg`（`schedule.planner_config()` 的同形快照）与 `rng`
（随机源可注入，测试固定种子）。输出：`{phone: (success, message, skip, status)}`——
**与 `round.run_queue_retry` 同形**，故调用方的账密状态收尾、事件批量落库、收尾标记与
退出码汇总零改动；另有 `sign-state-<day>.json`（每次尝试与重试入队即写，网页日历的事实源）。
调用谁：`queue_store`（批量领取 / 收尾 / 重排 / 待办计数 / 桶状态落库 / 租约回收 /
死主接管）、`token_bucket`
（出口桶 + 全局上界 + gap 门）、`planner`（计划生成与落库、`has_plan`）、`hrw`（分片集）、
`state_io`（按日状态、执行体文件心跳）、`attempts`（单次尝试与失败分级）、`alerts`（最终放弃时的管理员
告警与用户失败邮件）、`clock_meta`（当日虚分片数落库）、`schedule`（配置、窗口关闭判定、
通道数）——均在 `yiban.engine` / `yiban.store` 下。
谁调用：`runner.main`（`run_executor_v3` 是唯一执行入口，非 `--only` 时 `requeue_final`
取补签轮身份，`--only` 时 `claim_all` + `reclaim` + `requeue_final`）；兜底常驻
（`workers.run_fallback_worker`）用 `claim_all` + `requeue_during_run`。
"""
import asyncio
import contextlib
import datetime
import logging
import os
import random
import time
from concurrent.futures import ThreadPoolExecutor

from yiban import challenge, clock, egress, security, window
from yiban import status as yiban_status
from yiban.engine import alerts, attempts, hrw, planner, schedule, state_io, token_bucket
from yiban.masking import mask_phone as _mask_phone
from yiban.masking import mask_phones_in_text as _mask_phones_in_text
from yiban.masking import sanitize_text as _sanitize_text
from yiban.store import claims as claims_mod
from yiban.store import clock_meta, queue_store, run_events

logger = logging.getLogger("yiban")

#: 全局聚合速率上界 Λ 的键（attempt/s，缺省空 = 不限）。本模块是它唯一的读取点。
ENV_GLOBAL_RATE = "YIBAN_GLOBAL_RATE"
#: 退避落点的硬上限（秒）：安全类参数，恒为常量、不随窗口或速率自适应
RETRY_CAP_SEC = 600
#: 补货间隔（秒）：批量领取的轮询周期
REFILL_SEC = 5
#: 队列**连续**读不通多少轮就停止本轮领取（ba-p01-01 的有界性担保）。
#:
#: **为什么必须有界**：降级口径改成 fail-closed 后，"读不通"不再判收干。库一直坏就会让
#: 补货循环空转到窗口关闭。那比原来的静默漏签更糟：占着运行锁，压着宿主 timeout。
#: **为什么取 60**：判据是"连续"，任何一轮读通即归零。故它挡的是"持续读不通"，不是
#: "偶发一次"。连接层 `busy_timeout=15000`（`yiban/store/db.py`）⇒ 单次锁等待占 3 拍，
#: 60 拍容得下约 20 次连续锁等待。整表重插级别的长事务压不住写锁 15 分钟；压住就说明
#: 库真坏了，继续等没有意义。墙钟上界 ≈ 60 × (5s 轮询 + 15s 锁等待) = 20 分钟。
#: 会话内回炉那次读再多一次锁等待 ⇒ 最坏 35 分钟。两者都小于签到窗口的 80 分钟，
#: 停下来的那一轮当天仍补得回来。
#: **落在哪一层**：落在补货循环 `_refiller`。只有循环知道自己读了几轮。`queue_store`
#: 是无状态访问层，把计数放进去等于让存储层替调用方做降级决策。
#: **触发时人看见什么**：一条 ERROR（按天日志 / 后台"日志"页）与一条管理员告警
#: （轮末汇总邮件；推送已配置时即时推送），见 `_alert_queue_unreadable`。
QUEUE_UNREADABLE_MAX_ROUNDS = 60
#: 崩溃恢复间隔（秒）：租约回收与死主接管的周期。由补货循环节流（函数内不持时间状态）；
#: 60s 与任务级租约同量级——租约到期后最多再等一个周期就被回收。
RECOVER_SEC = 60
#: 出口桶状态落库间隔（秒）
EGRESS_PERSIST_SEC = 10
#: 出队排序的 priority 基准（`planner.PRIORITY_DEFAULT`）：领取不返回 priority，
#: 重试任务的"排在新任务之后"由退避后的 run_at 承担，不以 priority 表达
PRIORITY_ORDER_BASE = planner.PRIORITY_DEFAULT
#: 收尾哨兵的 priority：大于任何真实 priority，故哨兵只在队列空时被通道取到
SENTINEL_PRIORITY = 10 ** 6
#: 当日虚分片数在 `app_meta` 的键前缀（键 = 前缀 + 业务日）
V_META_KEY_PREFIX = "scheduler_v3_v_"

# 状态码别名（与 yiban.status 同一对象）
STATUS_RETRYING = yiban_status.STATUS_RETRYING
STATUS_PAUSED = yiban_status.STATUS_PAUSED
STATUS_USER_CANCELLED = yiban_status.STATUS_USER_CANCELLED
STATUS_SKIPPED_WINDOW = yiban_status.STATUS_SKIPPED_WINDOW
STATUS_NO_POSITION = yiban_status.STATUS_NO_POSITION

#: `run_at` 的两种精度：计划与重排都写毫秒精度，v17 平移的存量行只有秒精度
_RUN_AT_FORMATS = ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# 可注入接缝（测试与可测性：用例不依赖真实时钟、真实线程池、真实网络）
# ---------------------------------------------------------------------------
def _now():
    """墙钟（`run_at` 调度用）；与 `_mono` 刻意分开，见 `_throttle`。"""
    return clock.now()


async def _sleep(sec):
    """让出控制权的等待：阻塞睡眠会让 M 条通道退化成串行。"""
    await asyncio.sleep(sec)


def _mono():
    """单调浮点秒：**令牌桶的时钟域**（不随墙钟跳变漂移）。"""
    return time.monotonic()


def _new_thread_pool(max_workers):
    """执行尝试的线程池：上限 = 通道数 M（`asyncio.to_thread` 的并发上限即此）。"""
    return ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="yiban-v3")


def _make_limiter(channels):
    """出口限速器（配置面收口在 `token_bucket.limiter_from_env`）。"""
    return token_bucket.limiter_from_env(channels=channels)


def _make_global_limiter():
    """全局聚合速率上界：空/未配置 = 不限且不告警，非法 = 告警 + `invalid`（不拒绝启动）。"""
    return token_bucket.GlobalLimiter(os.environ.get(ENV_GLOBAL_RATE, ""))


def _make_gap_gate():
    """每账号 gap 门（缺省开、不可摘除；显式假值才关）。"""
    return token_bucket.gap_gate_from_env()


def _worker_slot(executor_id):
    """执行体槽位序号（文件心跳按**身份键**命名，见 `state_io.worker_alive_key`）：从身份串取，取不到用 0。

    `worker-{i}@{host}` → i；`single@` / `fallback@`（无序号）与解析不出的身份串 → 0。
    心跳只是可观测性，取不到序号不该让签到失败，故回退 0 而不是抛。

    `single` / `fallback` 与 `worker-0` 的槽位号都是 0 —— 光靠槽位号分不开三者，故
    写/读心跳必须同时带上 `_worker_role` 的角色档（否则三者共用同一心跳文件）。
    """
    idx = egress.parse_owner(executor_id).get("index")
    return 0 if idx is None else int(idx)


def _worker_role(executor_id):
    """执行体心跳的**角色档**（心跳身份键的另一半）：`worker` / `single` / `fallback`。

    `single@` / `fallback@` 解析出的槽位都是 0（`_worker_slot`），只有角色能把它们与
    `worker-0` 分开。解析不出角色（空串、历史遗留格式）按 `worker`——与旧行为一致，
    且这种情况的槽位号同样来自 `_worker_slot`，键形状不变。
    """
    role = egress.parse_owner(executor_id).get("role")
    if role in (egress.ROLE_WORKER, egress.ROLE_SINGLE, egress.ROLE_FALLBACK):
        return role
    return egress.ROLE_WORKER


def _parse_run_at(run_at):
    """`run_at` 串 → datetime；解析不出按"立即执行"处理（不打断整轮）。"""
    for fmt in _RUN_AT_FORMATS:
        try:
            return datetime.datetime.strptime(str(run_at), fmt)
        except (TypeError, ValueError):
            continue
    logger.warning("run_at 无法解析（按立即执行处理）: %r", run_at)
    return _now()


def _stamp_ms(dt):
    """毫秒精度时间串（与 `queue_store._lease_until`、`planner._stamp_at` 同格式）。

    `run_at` 与 `claim_batch` 的 `now` 都是**字符串序**比较，格式不一致会让"到点"判定
    静默错位（秒精度还会把同一秒内的落点判成"还没到点"）。
    """
    return dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


# ---------------------------------------------------------------------------
# 执行体启用判定
# ---------------------------------------------------------------------------
def scheduler_v3_enabled(env=None):
    """恒真（单池后保留）：台账单池化后 v3 是唯一生产执行路径，本函数不再读任何开关。

    保留它只有一个消费方——`schedule.capacity_of` 的 `enabled` 缺省形参（容量口径按
    同一份判定分派）。单池后该判定恒为真，故此处不再读环境变量，也不再存在双轨开关。
    """
    return True


def next_retry_at_v3(now_dt, sch_cfg, last_delay, rng=None):
    """装订式抖动退避的落点 → datetime；`None` = 有效窗口放不下，放弃重试。

    `delay = min(cap, uniform(base, max(base, last_delay × 3)))`，其中
    `cap = min(RETRY_CAP_SEC, 剩余有效窗口 × 0.3)`：下界是 `YIBAN_RETRY_MIN_INTERVAL`
    （防连击），上界随剩余窗口收缩（不在尾端扎堆），落点再夹到有效窗口结束——**绝不让
    重试越过窗口**，放不下就返回 `None`。窗口几何一律走 `window.bounds`（排计划、判
    关闭、容量预估的同一准绳）。`last_delay` 是上一次的 delay（进程内记账、无持久化）：
    换执行体接手或首次重试时未知，调用方传 `base` 即可——保守且收敛。

    **落点用绝对时刻夹取，不拿「当天第几分钟」配零点**（M16）：`win.hi_min` 是不含
    日期的分钟数，用 `to_dt(now 的零点, hi_min)` 当上界，在长轮次跨过午夜后算的是
    **次日**的窗口结束；而 `remaining_sec` 同样只看分钟数，00:10 被算成"今天还剩一整
    窗"，于是 `now + delay` 小于上界而原样胜出——落点变成 00:1x，**窗口之前**。
    通道只判"窗口没关"（`_window_closed` 对 00:10 为假），那条行遂在窗口外被真实登录，
    还会被记成 `skipped_window`。故：

    - 已经进窗口（正常路径）：`min(now + delay, hi_dt)`，与旧实现逐字同值；
    - 还没进窗口（跨零点的长轮次、或提前拉起的执行体）：落点钉进**同一时间窗**内的
      绝对时刻（`min(lo_dt + delay, hi_dt)`），而不是窗口外的 `now + delay`。

    窗口已过（`remaining <= 0`）仍返回 `None`：当天不再重试。
    """
    rng = rng or random.Random()
    base = max(1, int(sch_cfg["retry_min_interval"]))
    win = window.bounds(sch_cfg)
    remaining = win.remaining_sec(now_dt)
    if remaining <= 0:
        return None
    cap = min(RETRY_CAP_SEC, remaining * 0.3)
    delay = min(cap, rng.uniform(base, max(base, float(last_delay) * 3)))
    lo_dt, hi_dt = win.bounds_dt(now_dt)
    if now_dt < lo_dt:
        target = min(lo_dt + datetime.timedelta(seconds=delay), hi_dt)
    else:
        target = min(now_dt + datetime.timedelta(seconds=delay), hi_dt)
    return target if target > now_dt else None


def shadow_stats(accounts, *, day=None, cfg=None):
    """影子模式（`dry_run`）：只算计划与落点分布，零落库 / 零领取 / 零请求。

    与真实落点对比的入口是 `planner.plan_stats` 的 `hist`（桶键 = 相对有效窗口起点的
    5 分钟格）；V 只用于本次对账、不落库（影子期没有写 `sign_tasks.vshard` 的一方）。
    """
    cfg = cfg or schedule.planner_config()
    day = day or _now().strftime("%Y-%m-%d")
    v = hrw.v_for(len(planner._phones(accounts)))
    rows = planner.build_plan(accounts, day, cfg["executors"], v=v, cfg=cfg)
    return planner.plan_stats(rows, cfg=cfg, day=day)


# ---------------------------------------------------------------------------
# 当日虚分片数 V：与计划同源、当日稳定
# ---------------------------------------------------------------------------
def _v_meta_key(day):
    """当日 V 在 `app_meta` 的键（按业务日分键，跨天不复用）。"""
    return V_META_KEY_PREFIX + day


def _stored_v(day):
    """读当日已落库的 V；无记录 / 非法返回 None。"""
    try:
        v = int(str(clock_meta.get_meta(_v_meta_key(day), "")).strip())
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def _max_vshard(day):
    """当日已写行里的最大分片号（只认 `vshard >= 0`）；无行返回 None。"""
    try:
        conn, lock = queue_store._queue_conn()
        with lock:
            row = conn.execute(
                "SELECT MAX(vshard) FROM sign_tasks WHERE day=? AND vshard >= 0",
                (day,)).fetchone()
    except Exception as e:
        logger.warning("读取当日最大分片号失败（按无记录处理）: %s", e)
        return None
    if row is None or row[0] is None:
        return None
    return int(row[0])


def _plan_v(day, cfg, accounts, top):
    """当日虚分片数 V：**首次建计划时落库，此后只读**（读不到或与已写行不一致才兜底）。

    为什么必须落库：V 是"账号 → 分片索引"的模数，写进计划行的 `vshard` 与执行体
    `hrw.shards_of(..., v)` 用的必须是同一个值。若每轮按当日账号数重算，同日删号会让 V
    变小，已写行的索引落到扫描范围 `[0, V)` 之外 ⇒ 那些行**永远不会被任何执行体领取 =
    静默漏签**。故落库值优先；落库值缺失、或已写行的最大分片号已经不小于它（同样有行
    落在范围外）时，按最大分片号放宽并留 error——放宽 V **不会**改变既有索引的归属
    （`hrw.owner_of` 与 V 无关），只会把扫描范围撑到盖住已写行。

    `top` 由调用方一次查出后与"当日是否已有可用计划"共用（`None` = 无 `vshard >= 0`
    的行）；V 的取值口径只此一处，不另起第二条。
    """
    stored = _stored_v(day)
    if stored is not None and (top is None or top < stored):
        return stored
    if top is not None:
        logger.error(
            "当日计划已存在但虚分片数不可用（落库值 %s / 已写行最大分片号 %s），按 %s "
            "兜底：分片集可能不完整，请检查 app_meta 与队列库状态",
            stored, top, top + 1)
    v = top + 1 if top is not None else hrw.v_for(len(planner._phones(accounts)))
    clock_meta.set_meta(_v_meta_key(day), v)
    return v


def _plan_covers(day, accounts):
    """当日已有计划行是否**覆盖本轮全部账号**（每条账号都有一条 `vshard >= 0` 的行）。

    为什么不能只看"当日有行"：手动 `--only`（`force=True`）只给目标账号补了计划行，
    随后同日的定时全量轮若只看"有行"就整段跳过 `write_plan`，其余账号当天在队列里
    根本没有行 ⇒ 全天零签到。`write_plan` 是 `INSERT OR IGNORE`，补建不会覆盖 manual
    行（也不会复活 `vshard = -1` 的历史惰性行——本函数只对 `vshard >= 0` 计数，与
    `_max_vshard` 同界）。
    """
    phones = planner._phones(accounts)
    if not phones:
        return True
    try:
        conn, lock = queue_store._queue_conn()
        with lock:
            placeholders = ",".join("?" for _ in phones)
            row = conn.execute(
                "SELECT COUNT(DISTINCT phone) FROM sign_tasks "
                f"WHERE day=? AND vshard >= 0 AND phone IN ({placeholders})",
                (day, *phones)).fetchone()
    except Exception as e:
        logger.warning("读取当日计划覆盖面失败（按未覆盖处理）: %s", e)
        return False
    return int(row[0] or 0) == len(phones)


def _ensure_plan(accounts, day, cfg, force=False):
    """保证当日有**可用**计划行（v3 的队列就是计划），返回当日 V。

    "有计划"的判据必须是当日有真实计划行（`vshard >= 0`）：历史平移与补账留下的
    `vshard=-1` 行是惰性的——不在任何分片集内、永不被领取，若把它们当成"计划已就绪"，
    建计划被跳过而队列里又没有可领的行，本轮零领取、零请求，汇总成"全部未执行"。
    这类行按设计保持原样，只是不再充当"计划已就绪"的证据（`write_plan` 是
    `INSERT OR IGNORE`，补建也不会覆盖它们）。

    `force=True`（手动 `--only`）时**无计划也补建**：给到手的账号补上计划行
    （`INSERT OR IGNORE` 不覆盖既有行），使当日中途新增/此前未进过计划的账号也能被
    手动签到领到。普通轮不传，行为逐字不变。

    判据除"当日有真实计划行"外，还要求**覆盖本轮账号集**（`_plan_covers`）：手动
    `--only` 先跑一轮只写目标号的行，随后同日的定时全量轮必须给其余账号补建计划，
    否则它们当天在队列里没有行、全天零签到。

    `write_plan` 失败会抛，由调用方捕获后放弃本轮——没有计划行就没有队列，发不出任何
    请求，裸抛只会把 traceback 交给调用方。
    """
    top = _max_vshard(day)
    v = _plan_v(day, cfg, accounts, top)
    if top is None or force or not _plan_covers(day, accounts):
        if top is None and planner.has_plan(day):
            logger.error(
                "当日只有历史惰性行（无 vshard >= 0 的计划行），视为无可用计划："
                "按 %s 补建计划，历史行保持原样", v)
        planner.write_plan(
            planner.build_plan(accounts, day, cfg["executors"], v=v, cfg=cfg), day)
    return v


# ---------------------------------------------------------------------------
# 运行期上下文与事件/收尾出口
# ---------------------------------------------------------------------------
class _Ctx:
    """一轮 v3 的运行期上下文：通道间共享，**只在事件循环线程里改写**。

    `inflight` 是"正跑在线程池里的尝试数"，`busy` 是"通道手上还没处理完的条目数"（含
    正等待到点的那些）。收干判据看 `busy`：等待中的条目在库里已是 `claimed`、不在
    `pending_count` 里，不数进来会在通道还在跑时提前收干。
    """

    def __init__(self, accounts, day, cfg, v, shards, executor_id, results,
                 cred_state, delegated, notify_url, event_sink, rng, slot=0,
                 runtime_id=None, requeue_during_run=False, reclaim=False,
                 allowed_phones=None, alive_role=None, yield_probe=None, unreached=None,
                 round_no=None, log_tag=None):
        self.accounts = accounts
        self.day = day
        self.cfg = cfg
        self.v = v
        self.shards = shards
        # 稳定槽位名：HRW 分片成员判据（`hrw.shards_of` 要求它是 `cfg["executors"]`
        # 的成员）与出口令牌桶的持久键（`egress_state.egress`）都用它。**不用于写库**。
        self.executor_id = executor_id
        #: 日志归因前缀（`[worker-3 r2]` / `[fallback]`）：**只**由 `egress.owner_tag` 渲染
        #: （角色+槽位+可选轮次，不含主机名——身份原串带部署信息，不得入日志）。
        #: 每账号行靠它答出"哪个执行体、第几轮"：多执行体并发写同一个按天日志文件，
        #: 没有这个标记就只能靠时间戳猜。`round_no` 只有常驻多轮的兜底会传。
        #: `log_tag` 由运行入口算好传进来（渲染一处、值同）；直接构造本类的调用方
        #: （测试）不传时按 `executor_id`/`round_no` 现算，两者同值。
        self.log_tag = log_tag or egress.owner_tag(executor_id, round_no)
        # 写库的**持有者**身份（`sign_tasks.owner`）：稳定名再拼本进程的进程号/代次。
        # 与稳定名分开是必需的——同名进程（同槽位重启、同机两个进程）在 owner 上必须
        # 可分辨，否则收尾/重排/接管的 CAS 分不出"是不是同一个人"。
        self.runtime_id = runtime_id or executor_id
        # 文件心跳的槽位序号 + 角色档（`worker_presence` 按**身份键**读）：执行体页据此
        # 判存活四态。两者一起才分得开 `worker-0` 与无槽位号的 `single` / `fallback`。
        self.slot = slot
        self.alive_role = alive_role or egress.ROLE_WORKER
        # 桶键 = 执行体身份串（每进程一个出口，与 egress.resolve 的代理一一对应）；
        # 用稳定名：跨重启同名才能续上自适应速率
        self.egress = executor_id
        self.m = schedule.channel_count(cfg["bucket_rate"], cfg["avg_attempt_sec"])
        self.results = results
        self.cred_state = cred_state
        self.delegated = delegated
        self.notify_url = notify_url
        self.event_sink = event_sink
        self.rng = rng
        self.limiter = _make_limiter(self.m)
        self.global_limiter = _make_global_limiter()
        self.gap_gate = _make_gap_gate()
        self.inflight = 0
        self.busy = 0
        self.last_delay = {}
        # 轮首回炉（`run_executor_v3` 里那一次）读不通的记号：交给补货循环计入同一条有界
        # 放弃闸门（`QUEUE_UNREADABLE_MAX_ROUNDS`）。轮首读不通不等于"没有要回炉的行"，
        # 不许就地丢掉（ba-p01-01）。
        self.queue_unreadable = False
        # 会话内恢复周期是否顺带回炉默认档（常驻/长会话的兜底腿用它"当日接手"，
        # 一次性定时轮靠轮首回炉即可，不开这个口子以免在同一轮里重开保守档之外的循环）
        self.requeue_during_run = requeue_during_run
        # 让位的运行期判据（见 `run_executor_v3` 的同名说明）：补货循环每次领取前问一次。
        self.yield_probe = yield_probe
        # 轮末"本执行体未执行"判因的出口（调用方传入的 dict，本函数往里填）：
        # 判因同时喂汇总计数与事件留痕，见 `_mark_unreached`。
        self.unreached = unreached
        # 手动 `--only` 的显式重签：豁免窗口关闭判定（用户主动触发应当放行，与旧领取池
        # 的手动链路一致），其余路径不受影响
        self.reclaim = reclaim
        # 本轮**允许领取/回炉的账号允许集**（`None` = 按分片集不限）：手动 `--only` 只
        # 处理本轮传进来的账号，宽分片集不得把当日别人的行领走或把别人的 `failed` 回炉
        # ——那些行会走 `_attempt` 的 `acc is None` 分支被误当"了结"（静默漏签）。
        self.allowed_phones = (None if allowed_phones is None
                               else frozenset(allowed_phones))
        # **在途**账号集合（M17）：已领回 `claimed`、还没处理完的条目——正躺在本进程的
        # 通道队列里等通道/等限速/等 gap/等计划时刻，或正在线程池里跑那一次尝试。
        # 只在事件循环线程里改写（领取处加、通道收尾处删，与本类其余计数同一纪律）。
        #
        # 为什么必须有：等待时间不受租约约束，而这些行在库里已是 `claimed`、租约 60s 起步。
        # 队列一积压（限速/逐账号 gap/慢签到），条目能等过「租约 60s + 宽限 120s」，
        # 回收器便把它们判死、回退 `pending`，**下一次 `claim_batch` 又原地领回来**——
        # 同一账号当天两次真实登录（`epoch+1` 只挡迟到的结论写回，挡不住第二次登录，
        # 易班侧会锁号）。回收器按本集合豁免（`queue_store.reap_expired(held=...)`），
        # 这是"按持有者身份判活"而不是"把租约加长"：集合随处理完成而收缩，本进程真正
        # 泄漏的行（已领却没进集合）仍会被回收自愈。
        self.held = set()
        # **本执行体真的执行过**的账号（过了 gap 闸门、发过请求）。轮末判因要靠它把
        # "跑过但没收尾（重试回炉）"与"没轮到"分开——前者不是"未执行"（见
        # `_mark_unreached`）。只在事件循环线程里改写，与本类其余计数同一纪律。
        self.attempted = set()


def _emit_event(ctx, phone, status, message, dur=None, attempt_no=None):
    """签到事件留痕（与 `round._emit_event` 同形）：留痕失败不得影响签到主流程。"""
    if ctx.event_sink is None:
        return
    try:
        ts = _now().strftime("%Y-%m-%d %H:%M:%S")
        ctx.event_sink({
            "ts": ts,
            "phone": phone,
            "status": status,
            "message": _sanitize_text(str(message or ""))[:200],
            "stage": "sign",
            # attempt 列 NOT NULL 且批量落库是单事务：状态迁移/收尾事件没有尝试号，
            # None 原样上报会把本轮整批事件一起回滚掉——非尝试事件落 0。
            "attempt": attempt_no if attempt_no is not None else 0,
            "dur_sec": dur,
            "finished_at": ts,
        })
    except Exception:
        pass


def _report(ctx, node, phone="", message=""):
    """进度打点（执行体侧唯一入口）：身份与业务日从 ctx 取，调用方只给节点与账号。

    身份用**稳定槽位名**（`ctx.executor_id`，如 `worker-0@host` / `single@host`），
    不是写 `sign_tasks.owner` 的运行时串（含进程号/代次）：进度流的读者按槽位名认
    执行体（与执行体页、HRW 分片成员同一套名字），跨重启同名才能续成一条线。

    写入失败在 `run_events.report` 内被隔离（只告警），故此处不捕获、也不看返回值：
    打点是观测面，任何失败都不得改变本轮的签到路径（工单行为四）。
    """
    run_events.report(node, day=ctx.day, executor=ctx.executor_id,
                      phone=phone, message=message)


def _report_many(ctx, node, phones, message=""):
    """一批进度打点（执行体侧唯一批量入口）：同一时刻的一批行走一次写锁。

    领取发生在事件循环线程里，逐行提交会把"一行一次锁等待"叠加成整条通道的停顿，
    故领取用批量、单条用 `_report`。身份与业务日与 `_report` 同源（稳定槽位名 +
    `ctx.day`）。
    """
    run_events.report_many([{"node": node, "day": ctx.day,
                             "executor": ctx.executor_id, "phone": p,
                             "message": message} for p in phones])


def _settle(ctx, phone, epoch, state, message):
    """单行收尾：带上领取时的 fencing token——被接管者迟到的写会被拒。

    owner 用 `ctx.runtime_id`（领取时写进 `sign_tasks.owner` 的那个运行时身份）：
    收尾的 CAS 校验的是"还是不是我持有的这一行"，稳定名在这里会把同槽位的另一代
    当成自己人。
    """
    queue_store.settle_tasks(ctx.runtime_id, ctx.day, [(phone, message)],
                             state=state, epochs={phone: epoch})


def _finish(ctx, phone, epoch, result, state_message, state):
    """零请求收尾（暂停 / 账号已不在配置）：写状态、事件、结果并了结该行。

    以 `failed` 了结时同样带档位前缀（账密暂停属保守档，只有显式路径可回炉——
    自动回炉等于绕开凭据熔断反复真实登录）；`done` 是了结态，不带前缀。

    进度打点按状态取节点：账密暂停落 `pause`，其余非成功终态落 `fail`。本函数是
    **所有零请求收尾的唯一出口**，故打点挂在这里而不是两个调用点各写一遍。
    """
    _ok, _msg, _skip, status = result
    ctx.results[phone] = result
    state_io._write_sign_state(phone, status, state_message)
    _emit_event(ctx, phone, status, state_message)
    _report(ctx, run_events.NODE_PAUSE if status == STATUS_PAUSED
            else run_events.NODE_FAIL, phone, state_message)
    if state == queue_store.STATE_FAILED:
        state_message = _tier_prefix(status) + state_message
    _settle(ctx, phone, epoch, state, state_message)


def _is_risk_signal(message):
    """风控信号判定：只认 WAF 族与挑战形态，**不含凭据族**（工单 `yiban-auto-sign-zggs`）。

    两个消费方对同一批文案的语义相反，判据必须分开：
    - 重试档位（`attempts.classify_failure`）把风控**与**凭据都算"少给重试"——凭据错重试无用；
    - 本函数命中即认为"平台在限我们"，调用方据此把**整条出口**的速率砍半。
    复用档位判据是把凭据错当成平台风控：一个口令错的账号就砍掉整条出口一半速率
    （生产实证两天三次，fallback 落到 1/4）。凭据族留在 `attempts.RISK_FAIL_KEYWORDS`
    里不动——那张表是档位的唯一真值源。

    `is_waf_blocked` 的入参契约是**响应体**（它按"短响应"设界，见 `yiban.security`），
    这里传的是失败 `message`，故按**无界**口径判：`security.matches_waf_keywords` 不设长度
    上界、按词元边界匹配（ASCII 词元两侧非字母数字），并解码内嵌的 `\\uXXXX` 转义；
    挑战形态走 `challenge.looks_like_challenge`（与 `is_waf_blocked` 的形态腿同源，也不受
    长度限制）。要按响应体契约判定，得把响应对象一路带到这里（新数据源）；在那之前本判定
    以 `message` 为准。
    """
    return (security.matches_waf_keywords(message)
            or challenge.looks_like_challenge(message))


def _tier_prefix(status):
    """弃权收尾的档位前缀——**分档判据与领取层同一份**，不另造第二套协议。

    `sign_tasks.result` 的 `retry:`/`final:` 前缀是当日回炉口
    （`queue_store.requeue_failed`）区分"默认档自动回炉 / 须显式路径"的唯一判据；
    成员表只认 `claims.RETRYABLE_GIVE_UP_STATUSES`（v2 的 `claims.give_up` 与
    `round._settle_claims` 选档用的是同一 frozenset）。各写一份会漂移成
    "记成 retry、判成 final"——回炉口对默认档失明，当日失败又没人接手。
    """
    return (claims_mod.RESULT_RETRY_PREFIX
            if status in claims_mod.RETRYABLE_GIVE_UP_STATUSES
            else claims_mod.RESULT_FINAL_PREFIX)


def _log_give_up(phone, tried, status, message, tag=""):
    """最终放弃的留痕：与 v2 的放弃路径同级别、同语义。

    为什么分两级：无点位是易班侧没有数据（非账号/凭据问题，管理员无从修复，重试也拿不
    到），v2 对它只留 warning、不按"签到失败"告警；其余失败才是 error。手机号与原因都
    按全模块同一脱敏口径落日志。`tag` 是执行体归因前缀（`[worker-3 r2]`），由调用方从
    `ctx.log_tag` 传进来——本函数拿不到 ctx。
    """
    if status == STATUS_NO_POSITION:
        logger.warning("%s [%s] 🚫 易班未返回签到点位，当日不签到（重试无意义）: %s",
                       tag, _mask_phone(phone), _sanitize_text(message))
        return
    logger.error("%s [%s] ❌ 已尝试 %d 次，放弃: %s",
                 tag, _mask_phone(phone), tried, _sanitize_text(message))


def _alert_give_up(ctx, acc, phone, status, message):
    """最终放弃时的通知：管理员入口 + 账号归属用户的失败提醒。

    与 v2 的放弃路径同一函数、同一触发条件——**只在最终放弃时通知一次**：逐次失败不
    通知（否则每次尝试都发一封，且 `attempts.attempt_signin` 亦承诺逐次失败不通知）。
    `no_position` 是唯一例外：易班侧没有点位不是账号/凭据问题，管理员无从修复，v2 对
    它刻意不告警，重试也拿不到。通知内部自捕获异常，不得影响签到主流程。
    """
    if status == STATUS_NO_POSITION:
        return
    alerts.notify_admin_entry("易班签到失败", [
        ("账号", _mask_phone(phone)),
        ("原因", _sanitize_text(message)),
    ], ctx.notify_url)
    alerts.send_user_fail_mail(acc.owner, phone, message)


# ---------------------------------------------------------------------------
# 通道、补货、桶状态落库
# ---------------------------------------------------------------------------
async def _sleep_until(run_at):
    """等到计划时刻（剩余 <= 0 就不等）——非阻塞等待，见 `_sleep`。"""
    delay = (_parse_run_at(run_at) - _now()).total_seconds()
    if delay > 0:
        await _sleep(delay)


async def _throttle(ctx, phone):
    """出口限速 → 全局上界 → 每账号 gap：三件都在事件循环线程里同步取额度。

    `_mono` 是桶的时钟域（`tat` 用单调秒），与 `_now`（墙钟、用于 run_at）分开：墙钟跳变不该
    让桶的放行节奏漂移。
    """
    # 三道闸的次序就是成本次序：出口桶（本地字典）→ 全局 Λ（单进程计数）→ gap 门（按账号），
    # 便宜且易命中的排最前；三件全过才允许走到发起尝试，中途放弃不 commit gap
    while not ctx.limiter.acquire(ctx.egress, _mono()):
        await _sleep(ctx.limiter.bucket(ctx.egress).retry_after(_mono()))  # 睡到桶算好的放行时刻，不自旋空烧
    while not ctx.global_limiter.acquire(_mono()):
        await _sleep(1.0 / ctx.global_limiter.lam)  # 能进这条说明 lam 非 None（None 时 acquire 恒放行）
    while not ctx.gap_gate.allow(phone, _mono()):
        await _sleep(ctx.gap_gate.gap_sec)  # 这里只 allow 不 commit：真正推进 TAT 在 `_attempt` 发起尝试处


async def _attempt(ctx, item):
    """一次尝试的完整闭环：跳过判定 → 线程池执行 → 状态物化 → 终态收尾或重排。

    终态映射与 `round._settle_claims` 同口径：`CLAIM_DONE_STATUSES` → done，其余（含
    `skip=True` 的窗口外/无任务/用户取消/账密暂停、预算用尽、窗口放不下重试）→ failed。
    **只有最终态进 `results`**：重试中的账号由下一轮领取重新处理。
    """
    _priority, _run_at, phone, attempts_n, epoch = item
    today = _now().strftime("%Y-%m-%d")
    acc = ctx.accounts.get(phone)
    if acc is None:
        if getattr(ctx, "reclaim", False):
            # 手动 `--only` 的保险：本轮允许集已把它挡在领取/回炉之外，正常不会到达这里。
            # 真到了也**绝不了结**它——既不 `_finish`（会在执行体里被当成"本轮账号已不在
            # 配置"记 `done`/`user_cancelled`）、也不写 sign-state；留给下一轮/别的执行体。
            logger.warning("%s 手动轮遇到不在本轮账号集里的行，"
                           "跳过不动（不改状态/不写了结）: %s",
                           ctx.log_tag, _mask_phone(phone))
            return
        # 运行期被删/停用：不发请求，但必须了结该行——否则它永远 pending，本轮收不干
        _finish(ctx, phone, epoch, (False, "账号已不在本轮配置", True,
                                    STATUS_USER_CANCELLED),
                "账号已不在本轮配置", queue_store.STATE_DONE)
        return
    cred = ctx.cred_state.get(phone, {})
    if cred.get("paused_since") and not attempts._probe_due(cred, today):
        # 熔断判定排在取额度之后、发请求之前：这里必须把行了结成 failed 而不是丢着不管，
        # 否则该行永远 pending、本轮收不干（同上一处 acc is None 的处置理由）
        _finish(ctx, phone, epoch, (False, "账密异常已暂停，请修改密码", True, STATUS_PAUSED),
                "账密异常已暂停（连续失败），请修改密码", queue_store.STATE_FAILED)
        logger.info("%s [%s] ⏸️ 账密异常已暂停，请修改密码", ctx.log_tag, _mask_phone(phone))
        return
    ctx.gap_gate.commit(phone, _mono())  # 走到这才是"真要发请求"：gap 的推进点必须与尝试一一对应
    # 本执行体**真的执行过**这个账号（轮末判因用：执行过却没收尾的（重试回炉）不是
    # "没轮到"，见 `_mark_unreached`）。登记点与上面那条注释同一处：过了 gap 闸门才算执行。
    ctx.attempted.add(phone)
    # 进度打点「开始」：闸门全过、请求即将发出。三件限速替身（出口桶/全局 Λ/gap）都在
    # 这行之前，故 start 行只代表"真的要发请求了"，把"排队等额度"记成开始会虚报进度。
    _report(ctx, run_events.NODE_START, phone)
    ctx.inflight += 1
    t0 = _mono()
    try:
        success, message, skip, status = await asyncio.to_thread(
            attempts.attempt_signin, acc)
    finally:
        ctx.inflight -= 1
    dur = _mono() - t0
    # 每次尝试结束即物化状态：v2 是逐账号增量写，v3 只在收尾物化会让当天网页日历空窗
    state_io._write_sign_state(phone, status, message, dur=dur)
    _emit_event(ctx, phone, status, message, dur=dur, attempt_no=attempts_n + 1)
    # 进度打点「成功/失败」：一次尝试的结论，与上一行的 sign_events 同一时刻、同一判据
    # （了结态集合 `CLAIM_DONE_STATUSES`）。重试中的尝试同样落 fail——那是这次尝试的
    # 事实；该账号稍后重新被领到时会有新的 claim/start 行，进度流据此如实反映次数。
    _report(ctx, run_events.NODE_SUCCESS if status in yiban_status.CLAIM_DONE_STATUSES
            else run_events.NODE_FAIL, phone, message)
    # 每账号结果行：行内手机号为遮罩形态（日志文件本身脱敏，展示层统一再脱敏）。
    # 日历"我的日志"面板与日志页靠该行向账号归属用户回显每次尝试结果——只在
    # 重试/放弃时落行的话，成功/跳过账号在面板里查无记录。
    _sym = (yiban_status.DISPLAY.get(status) or {}).get("symbol") or ""
    logger.info("%s [%s] %s", ctx.log_tag, _mask_phone(phone),
                (_sym + " " if _sym else "") + _sanitize_text(message))
    attempts._update_cred_state(ctx.cred_state, phone, success, message, today)
    if status in yiban_status.CLAIM_DONE_STATUSES:
        ctx.limiter.on_success(ctx.egress)
        ctx.results[phone] = (success, message, skip, status)
        _settle(ctx, phone, epoch, queue_store.STATE_DONE, message)
        return
    if _is_risk_signal(message):
        ctx.limiter.on_risk_signal(ctx.egress, _mono())
    if skip:
        ctx.results[phone] = (False, message, True, status)
        _settle(ctx, phone, epoch, queue_store.STATE_FAILED,
                _tier_prefix(status) + message)
        return
    max_attempts, clear_cache = attempts._retry_budget(message)
    if clear_cache:
        attempts.clear_session_cache_quiet(phone)
    if attempts_n + 1 < max_attempts:
        base = max(1, int(ctx.cfg["retry_min_interval"]))
        nxt = next_retry_at_v3(_now(), ctx.cfg, ctx.last_delay.get(phone, base), ctx.rng)
        if nxt is not None:
            ctx.last_delay[phone] = (nxt - _now()).total_seconds()
            queue_store.requeue_task(phone, ctx.day, _stamp_ms(nxt), priority_delta=1,
                                     result=message, epoch=epoch)
            retry_msg = f"待重试（已 {attempts_n + 1} 次）: {_sanitize_text(message)}"
            state_io._write_sign_state(phone, STATUS_RETRYING, retry_msg)
            _emit_event(ctx, phone, STATUS_RETRYING, retry_msg, attempt_no=attempts_n + 1)
            logger.warning("%s [%s] ⏳ %s", ctx.log_tag, _mask_phone(phone), retry_msg)
            return
        logger.error("%s [%s] ❌ 窗口剩余不足，不再重试: %s",
                     ctx.log_tag, _mask_phone(phone), _sanitize_text(message))
    else:
        _log_give_up(phone, attempts_n + 1, status, message, ctx.log_tag)
    ctx.results[phone] = (False, message, False, status)
    _alert_give_up(ctx, acc, phone, status, message)
    # 弃权收尾带档位前缀：这是回炉口唯一的判据（窗口外/无点位→默认档当日自动回炉，
    # 预算耗尽/风控→保守档，只由显式路径放行）。不带前缀的 failed 行会被当作
    # 保守档——判不清原因的宁可要求显式路径，也不要无上限重复真实登录。
    _settle(ctx, phone, epoch, queue_store.STATE_FAILED,
            _tier_prefix(status) + message)


async def _lane(queue, lane_id, ctx):
    """通道主体：取条目 → 等到点 → 取额度 → 提交一次尝试 → 收尾。

    每条通道对应线程池里的一个线程（默认执行器上限 = M，`asyncio.to_thread` 是唯一进
    线程的调用），故 M 条通道与 M 个线程一一对应，无嵌套提交造成的自我死锁。

    `finally` 里从 `ctx.held` 摘除该条目（M17）：摘除点是"通道不再持有这条"而不是
    "库里已收尾"——`_attempt` 的手动轮 `acc is None` 分支会**故意不收尾**（把行留给下一
    轮/别的执行体），那一行一旦留在 `held` 里就再也回收不到，当天该账号无人再签。
    正常路径下 `_settle` / `requeue_task` 已先把行挪出 `claimed`，此处只是把进程内的
    在途登记与之一致；异常上抛（整轮中止）时同样摘除，进程内不留悬空的"在途"。
    """
    while True:
        item = await queue.get()
        if item[0] >= SENTINEL_PRIORITY:
            return
        ctx.busy += 1
        try:
            await _sleep_until(item[1])
            await _throttle(ctx, item[2])
            await _attempt(ctx, item)
        finally:
            ctx.busy -= 1
            ctx.held.discard(item[2])


#: 本进程内已打过接管日志的 `(peer, day)` 集合：判死后的分片每 `RECOVER_SEC` 都会再并
#: 一次，逐次打会刷屏，故同日同一 peer 只播报首次。
_TAKEN_OVER_PEERS: set = set()


def _live_row_owners(day, executor_id):
    """**在途 `claimed` 行**持有者中，心跳未判死者（含无心跳）的**稳定槽位名**（回收豁免名册，M17）。

    名册**取自队列本身**（`queue_store.claimed_owners(day)`：当日所有 `claimed` 行的
    `owner` 去重），不再枚举 `cfg["executors"]`——清单只是 HRW 分片候选集，兜底常驻身份
    （`egress.fallback_owner()`）从不在其中；按清单枚举会让兜底手上的行整段落在豁免之外
    （ba-p03-01：兜底与定时轮并发 ⇒ 同一账号当天两次真实登录，易班侧锁号）。

    判活口径**「非 `stale` 即活」**：`state_io.worker_presence` 的四态里，`running`
    （心跳新鲜）、`finished`（当轮正常收尾）、`idle`（当日无该身份心跳记录）都算活；
    只有 `stale`（有开始记录、无收尾、心跳过期）算死。这与 `_widen_with_dead_peers` 同源：
    两处共用同一份四态事实，都只把 `stale` 当死。

    **`idle` 必须算活（第一红线）**：`idle` 有三条真实来源，三条都落在"持有者其实活着"上——
    - 心跳写失败：`mark_worker_started` / `mark_worker_beat` 把写盘 `OSError` 只记 debug
      （`state_io` 明文容忍该失败），活持有者写不出心跳文件；
    - 跨午夜长轮次：`mark_worker_beat` 只刷 `ts`、不刷 `day`，跨过午夜后心跳记录仍写着
      前一业务日，`worker_presence` 见"记录属别的业务日"即回 `idle`；
    - 多主机共库（见下）。
    把 `idle` 判死，这三类活持有者的行都会被回收 ⇒ 同一账号当天二次真实登录。

    遗留（如实登记）：持有者真死且心跳文件缺失（`idle`）时，其行**当日不回收**，该账号
    当日漏签（只有 `stale` 才回收）。这是**既有行为**，本刀不改——红线（二次登录）严于
    漏签，故取保守侧；当日漏签由后续轮次处置。

    **多主机共库局限（如实登记）**：判活只看**本机**状态目录的心跳。多主机共库时，别的
    主机的活持有者在本机读不到心跳文件 ⇒ `worker_presence` 回 `idle`。「非 `stale` 即活」
    把 `idle` 算活，故别机持有者在本机算**活**、其行不回收——方向**恰好与第一红线一致**
    （不误杀、不重复登录）。既有实现（遍历本机执行体清单）有同一局限；判活口径改回
    `idle` 算活后，这条局限不造成跨主机误杀。项目未见共库多主机的支持声明（`egress` 只
    声明"同一槽位名不得有两台机器同时跑"）。

    取 `(day, executor_id)` 而非 `ctx`：轮首那次回收发生在 `_Ctx` 构造之前，两处调用点
    才能共用同一份判活。**按稳定槽位名排除本执行体自己**（`egress.stable_owner` 把历史代次
    的运行时串也折回同一个稳定名）：本进程的在飞行由更精确的 `ctx.held` 逐行豁免；若把自己
    的稳定名也塞进名单，本进程重启前遗留的旧代次行就永远回收不到（当天该账号无人再签）。

    名册读失败（`claimed_owners` 回 `None`）时返回 `None`——**不得**当成空名册：调用方把
    它直接传给 `reap_expired`，后者见到 `None` 即**跳过本轮回收**（fail-closed），否则空
    名册会放宽回收面、把活持有者的行判死（同账号二次登录）。
    """
    owners = queue_store.claimed_owners(day)
    if owners is None:
        return None
    live = []
    seen = set()
    for owner in owners:
        stable = egress.stable_owner(owner)
        if not stable or stable == executor_id:
            continue
        state, _seen = state_io.worker_presence(_worker_slot(owner), now=_now(),
                                                role=_worker_role(owner))
        if state == state_io.WORKER_STATE_STALE:
            continue
        if stable not in seen:
            seen.add(stable)
            live.append(stable)
    return live


def _widen_with_dead_peers(ctx, shards):
    """把心跳已过期的执行体的分片并入本轮领取范围；返回并入后的分片集（升序去重）。

    判据是**既有文件心跳的四态**（`state_io.worker_presence`）：只有 `stale`（有开始记录、
    无收尾且心跳过期）才算死。监督进程未启动 / 单进程直跑时该槽位没有当日记录，四态回
    `idle`——**不是** `stale`，故不会误接管活着的执行体。`vshard=-1` 的历史行不属于任何
    分片集（`hrw.shards_of` 产出 `0..V-1`），天然不在接管范围内，不需要额外过滤。

    **只并入分片集，不改任何行**：行归属改写已裁（原 `queue_store.steal_shards`，按
    "分片内 `pending` 且 owner 指向死主"改写 owner）——`claim_batch` 不筛 `owner`
    （领取动作本身 `SET owner=<领取者>`、`epoch+1`），`reap_expired` 回收时把 `owner`
    清空，改写对"行能否被领到"、围栏与回收判定零贡献。代价只是接管窗口期（租约 60s +
    回收宽限 120s + 回收间隔 60s，最坏约 4 分钟）展示页的归属列仍显示死主，可接受。

    判死即并入是超时回收在多执行体下生效的必要条件：`reap_expired` 按当日全表回收、把
    死主分片的行变回 `pending`，但 `claim_batch` 只领 `vshard IN (本执行体的分片集)`——
    不并入，回收出的行无人能领 = "崩溃即卡死"。拿"改到几行"当门更不行：死主把待办全领成
    `claimed` 后崩，回收出的行 `owner` 已清空，按 owner 匹配的改写返回 0。

    日志报"接管了哪些分片"而非行数（行归属已不再被改写，没有行数可报），按"本进程内该
    peer 首次判死并入"打一次（`_TAKEN_OVER_PEERS` 去重）；同日重并的分片集与首次相同，
    重复播报无信息量。
    """
    v = getattr(ctx, "v", 0)
    if v <= 0:
        return tuple(shards)
    extra = []
    for peer in ctx.cfg.get("executors", ()):
        if peer == ctx.executor_id:
            continue
        state, _seen = state_io.worker_presence(_worker_slot(peer), now=_now(),
                                                role=_worker_role(peer))
        if state != state_io.WORKER_STATE_STALE:
            continue
        peer_shards = hrw.shards_of(peer, ctx.cfg["executors"], ctx.day, v)
        if not peer_shards:
            continue
        if (peer, ctx.day) not in _TAKEN_OVER_PEERS:
            _TAKEN_OVER_PEERS.add((peer, ctx.day))
            logger.warning("接管心跳过期的执行体 %s 的分片集 %s（仅并入本轮领取范围，不改行归属）",
                           egress.owner_tag(peer), sorted(peer_shards))
        extra.extend(peer_shards)
    if not extra:
        return tuple(shards)
    return tuple(sorted(set(shards) | set(extra)))


def _alert_queue_unreadable(ctx, rounds):
    """队列连续读不通到上界：一条 ERROR 与一条管理员告警。

    为什么必须响亮：降级口径是 fail-closed 后，领取面与待办面读不通就不判收干。
    库一直坏时本轮会走到上界再停止领取。现场若只有 `queue_store` 那几条 WARNING，
    运维看到的就是"今天没活"——那正是 ba-p01-01 要消灭的形状。

    本条一轮至多一条，且运维须当机处理，故定级 CRITICAL。
    逐账号明细挤满 200 条时，本条仍先占额度，不被挤出邮件正文。
    """
    logger.error("%s 队列连续 %d 轮读不通（上界 %s），本轮停止领取；当天可能零签到",
                 ctx.log_tag, rounds, QUEUE_UNREADABLE_MAX_ROUNDS)
    alerts.notify_admin_entry("易班队列读不通：本轮已放弃领取", [
        ("日期", ctx.day),
        ("连续读不通轮数", "%d（上界 %s）" % (rounds, QUEUE_UNREADABLE_MAX_ROUNDS)),
        ("处置", "查库锁与磁盘空间；库恢复后跑补签轮"),
    ], level=alerts.ALERT_LEVEL_CRITICAL)


async def _refiller(queue, shards, ctx):
    """补货：每 `REFILL_SEC` 秒批量领取自己分片集内到点的任务投进通道队列。

    出队排序键是 `(PRIORITY_ORDER_BASE, run_at)`：`claim_batch` 不返回 `priority`，
    重试任务的 priority 只在库里递增、这里拿不到——但"重试排在新任务之后"由 `run_at`
    承担（退避已把重试落点推到未来），故固定 priority 入队不影响顺序。

    收干判据必须带 `pending_count`：`claim_batch` 在"下次到点在 `REFILL_SEC` 之后"时
    本来就返回空，只看空返回会把还有待办的一轮误判成收干；也**必须**带 `busy`：正等待
    到点的条目在库里已是 `claimed`、不在 `pending_count` 里，不数进来会在通道还在跑时
    提前收干，把刚重排回 `pending` 的重试任务留在库里没人领。

    该循环开头还有一道**让位复检**（`ctx.yield_probe`，只给兜底腿）：答 True 就冻结领取、
    已领的照常收尾。理由是让位不能依赖"进入这一轮那一刻"的锁状态——一轮扫描可能跑满整个
    窗口，一次赢下启动竞态就等于吸收全天的工作量（2026-10-07 生产 84/100）。
    同一循环按间隔驱动两件恢复动作（**函数内不持时间状态**，间隔常量在模块级）：
    - `reap_expired`：回收本业务日内租约**超出宽限期**的 `claimed` 行——不回收的话崩溃
      通道留下的行永远不被重领（`claim_batch` 只取 `pending`），即"崩溃即卡死"。**判活按
      持有者身份**：本进程通道队列里在途的行（`held=ctx.held`）与队列里**每个仍在途持有者**
      中判为活着者（`live_owners=`，见 `_live_row_owners`）都豁免。少了这道豁免，队列里排队
      的条目会等过「租约 60s + 宽限 120s」被回收成 `pending`、再被本循环下一次
      `claim_batch` 原地领回 ⇒ 同一账号当天两次真实登录（M17，详见
      `queue_store.reap_expired` 与 `_Ctx.held`）。
      宽限期挡住的是"单次尝试比租约慢"，挡不住"在队列里排队"——两者的等待都没有上界。
      **只回收本业务日**（`day=ctx.day`）：不带 `day` 会连历史业务日的行一起回退成
      `pending`，而次日进程只按当日领取，那些行只会变成永不被领的空转行；跨午夜长轮次
      仍在飞的行也会被次日进程重置。
    - 死主接管：对心跳过期的执行体，把其分片并入领取范围（只并入，不改行归属——归属在
      领取时落到领取者名下）——否则死主分片里回收出来的行没有任何人领。
    另按 `WORKER_HEARTBEAT_SEC` 刷新本执行体心跳：一轮可能十几分钟，只在起跑/收尾写盘会
    让长轮次被执行体页判成 `stale`（异常），比"显示 idle"更糟。

    领取/回炉/待办计数一律带上 `ctx.allowed_phones`（`None` = 按分片集不限）：手动
    `--only` 轮**只领、只回炉本轮传进来的账号**，且在 reclaim 轮**跳过跨账号恢复**
    （`reap_expired` / 死主接管会改别人的行，手动轮不该做）。

    **读不通不判收干（ba-p01-01）**：`claim_batch` 回 `[]` 与回 `None` 是两件事。前者是
    "本轮无到期行"，后者是"库读不通"；`pending_count` 的 `0` 与 `None` 同理。本循环只在
    两面都读通且待办为 0 时才收干。任一面回哨兵就计入 `QUEUE_UNREADABLE_MAX_ROUNDS`
    那条有界闸门：连续满上界后告警并停止领取。不空转，也不抛异常打断整轮。
    """
    shards = tuple(shards)
    last_beat = _mono()
    last_recover = _mono()
    allowed = getattr(ctx, "allowed_phones", None)
    held = ctx.held
    # 有界放弃闸门：连续读不通的轮数。轮首那次回炉也是一次读库失败，故按
    # `ctx.queue_unreadable` 把起点取成 1，与循环内的读共用同一条上界。
    unreadable_rounds = 1 if getattr(ctx, "queue_unreadable", False) else 0
    while True:
        # 本拍是否读到"库读不通"：领取面、待办面、会话内回炉三处任一
        round_unreadable = False
        # 手动 `--only` 豁免窗口判定：用户主动触发应当放行（与旧领取池的手动链路同语义）。
        if not getattr(ctx, "reclaim", False) and schedule._window_closed(ctx.cfg, _now()):
            break
        if _mono() - last_beat >= state_io.WORKER_HEARTBEAT_SEC:
            state_io.mark_worker_beat(getattr(ctx, "slot", 0), now=_now(),
                                      role=getattr(ctx, "alive_role", None))
            last_beat = _mono()
        if (not getattr(ctx, "reclaim", False)
                and _mono() - last_recover >= RECOVER_SEC):
            queue_store.reap_expired(now=_stamp_ms(_now()), day=ctx.day,
                                     held=held,
                                     live_owners=_live_row_owners(ctx.day,
                                                                  ctx.executor_id))
            shards = _widen_with_dead_peers(ctx, shards)
            if getattr(ctx, "requeue_during_run", False):
                # 会话内回炉（默认档）：本轮刚弃权的 `retry:` 档行立刻翻回
                # `pending`，下一拍就能被自己的通道重新领到——不等下一场会话。
                # **只回炉默认档**：`final:`/无前缀保守档的第二次机会只留给有界的
                # 显式路径（补签轮），常驻会话没有预算上界，自动复活等于无上限重登。
                # 回炉回 `None` = 读不通：当日 `failed` 行仍留在原态，等于"没回炉"。
                # 这不是"没有要回炉的行"，计入同一条有界闸门（ba-p01-01）。
                flipped = queue_store.requeue_failed(ctx.day, shards, include_final=False,
                                                     phones=allowed)
                if flipped is None:
                    round_unreadable = True
            last_recover = _mono()
        # 让位复检（`ctx.yield_probe`，只给兜底腿）：判据不能只看"进入这一轮那一刻"的
        # 状态——一轮扫描可能跑满整个窗口，一次赢下启动竞态就吸收全天的工作量
        # （2026-10-07 生产 84/100）。这里每次领取前复检一次：答 True 就**冻结领取**
        # （已领的照常收尾，半路丢弃会留下租约空转与"未了结"行），本轮很快自然收干。
        probe = getattr(ctx, "yield_probe", None)
        if probe is not None and probe():
            logger.info(
                "%s 让位：检测到全量轮开始运行，停止继续领取（本轮已领 %d 个账号照常收尾）",
                ctx.log_tag, len(held))
            break
        rows = queue_store.claim_batch(
            ctx.runtime_id, ctx.day, shards, now=_stamp_ms(_now()),
            limit=queue_store.CLAIM_BATCH_LIMIT, lease_sec=queue_store.LEASE_SECONDS,
            phones=allowed)
        if rows is not None:
            for r in rows:
                # 领取与登记在途之间**没有 await**：同一轮事件循环里同步完成，回收器不可能
                # 在"行已 claimed、还没进 held"的缝里把它判死（那正是本条要堵的重复登录）。
                held.add(r["phone"])
                queue.put_nowait((PRIORITY_ORDER_BASE, r["run_at"], r["phone"],
                                  r["attempts"], r["epoch"]))
            # 进度打点「领取」：整批一次写（见 `_report_many`）。放在投递之后，故
            # "claim_batch → held 登记"那段无 await 的豁免窗口一字未动。
            _report_many(ctx, run_events.NODE_CLAIM, [r["phone"] for r in rows])
        # 收干候选：领取面**读通**且回空、通道全闲、队列空。只有候选才问第二面——
        # 领取面已是哨兵时再问一次只是白等一次锁等待。
        drained = (rows is not None and not rows and ctx.inflight == 0
                   and ctx.busy == 0 and queue.empty())
        pending = (queue_store.pending_count(ctx.day, shards, phones=allowed)
                   if drained else None)
        # **唯一一条真·收干出口**：两面都必须读通（都不是 `None` 哨兵）且待办为 0。
        # 把哨兵折成"没有待办"就是全天零签到而现场只有两条 WARNING（ba-p01-01）。
        if rows is not None and pending is not None and not rows and pending == 0:
            break
        if rows is None or (drained and pending is None):
            round_unreadable = True
        if round_unreadable:
            unreadable_rounds += 1
            if unreadable_rounds >= QUEUE_UNREADABLE_MAX_ROUNDS:
                _alert_queue_unreadable(ctx, unreadable_rounds)
                break
        else:
            unreadable_rounds = 0
        await _sleep(REFILL_SEC)
    for _ in range(ctx.m):
        queue.put_nowait((SENTINEL_PRIORITY, "", "", 0, 0))


async def _persist_loop(ctx):
    """每 `EGRESS_PERSIST_SEC` 秒把出口桶状态落库：崩溃重启后不"重启即全速"。

    写失败由 `EgressLimiter.persist` 内部告警，不阻断签到（桶状态是记忆不是业务事实）。
    """
    while True:
        await _sleep(EGRESS_PERSIST_SEC)
        ctx.limiter.persist(ctx.egress)


async def _run_async(ctx):
    """起 M 条通道 + 一条补货 + 一条桶状态落库，跑到通道全部收到哨兵为止。

    任务名用 `%` 拼而不是 f-string：`"<前缀>-{…}"` 的形状会被"按日文件登记"元测试认成
    新的状态文件前缀；名字本身用于在 asyncio 的 traceback 里区分通道。
    """
    queue = asyncio.PriorityQueue()
    asyncio.get_running_loop().set_default_executor(_new_thread_pool(ctx.m))
    lanes = [asyncio.create_task(_lane(queue, i, ctx), name="v3-lane-%d" % i)
             for i in range(ctx.m)]
    refill = asyncio.create_task(_refiller(queue, ctx.shards, ctx), name="v3-refiller")
    persist = asyncio.create_task(_persist_loop(ctx), name="v3-egress-persist")
    try:
        await asyncio.gather(refill, *lanes)
    finally:
        persist.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await persist


# ---------------------------------------------------------------------------
# 起跑前的登记与收尾物化
# ---------------------------------------------------------------------------
def _prescan(ctx, accounts):
    """起跑前登记两类不经过队列的账号。

    - **不在本执行体分片集**：多执行体分工下由别的执行体负责，登记进 `delegated` 让
      汇总不把别人的活报成自己的失败（与 `round.run_queue_retry` 同口径）。**归属判定
      必须排在自暂停登记之前**：自暂停号没有计划行、每个执行体的账号列表里都有它，
      若先登记结果再判归属，N 个执行体会各登记一遍——状态/事件 ×N、每份汇总各计一遍
      "跳过"，看起来就是 N 倍暂停（2026-10 生产事件表实测每号每天 2 行 user_cancelled）；
    - **用户自暂停**：计划里本就没有它的行（`planner._phones` 剔除自暂停账号），不登记
      就会在汇总里变成"未执行"（按失败计），与 v2 的"跳过"口径不符。由**归属执行体**
      登记一次。
    """
    for acc in accounts:
        phone = acc.phone
        if (ctx.delegated is not None
                and hrw.vshard_of(phone, ctx.day, ctx.v) not in ctx.shards):
            ctx.delegated.add(phone)
            continue
        if getattr(acc, "user_paused", False):
            ctx.results[phone] = (False, "用户已取消签到", True, STATUS_USER_CANCELLED)
            state_io._write_sign_state(phone, STATUS_USER_CANCELLED, "用户已取消签到")
            _emit_event(ctx, phone, STATUS_USER_CANCELLED, "用户已取消签到")
            # 用户自暂停是账号级「暂停」节点：它没有计划行、不经过队列，故没有
            # claim/start 行——进度流里只出现这一行 pause 是正确形态。
            _report(ctx, run_events.NODE_PAUSE, phone, "用户已取消签到")


def _mark_window_skips(ctx, accounts):
    """窗口已关时，把本执行体分片集内没轮到结果的账号落 `skipped_window`。

    只在窗口已关时动手（与旧领取池的 `round._mark_window_skip` 同时机）：窗口还开着却没有
    结果，只可能是计划行缺失或库异常，那属于要暴露的异常，不该被"窗口外"盖掉。已有当日
    结论的账号按原结论透传（不覆盖真实失败），写入走 `only_if_absent` 的 CAS 兜住并发。
    手动 `--only`（`reclaim`）豁免本判定：用户主动触发不看窗口，"窗口外跳过"不许盖住
    他这一下的真实结论。
    """
    if getattr(ctx, "reclaim", False) or not schedule._window_closed(ctx.cfg, _now()):
        return
    recorded = state_io._daily_statuses()
    for acc in accounts:
        phone = acc.phone
        if phone in ctx.results:
            continue
        if hrw.vshard_of(phone, ctx.day, ctx.v) not in ctx.shards:
            continue
        rec = str(recorded.get(phone, "")).strip()
        if yiban_status.is_concluded_status(rec):
            ctx.results[phone] = (False, "已有当日结论", False, rec)
            continue
        if not state_io._write_sign_state(phone, STATUS_SKIPPED_WINDOW, "签到时段已结束",
                                          only_if_absent=True):
            continue
        ctx.results[phone] = (False, "签到时段已结束", True, STATUS_SKIPPED_WINDOW)
        _emit_event(ctx, phone, STATUS_SKIPPED_WINDOW, "签到时段已结束")
        # 窗口外收尾同样落终态行（非成功终态一律 fail）：本轮该账号没有 claim/start
        # （计划行缺失或库异常由本函数兜住），只出现一行 fail 是如实形态——汇总里的
        # "跳过"计数与进度流就此对得上。
        _report(ctx, run_events.NODE_FAIL, phone, "签到时段已结束")
        logger.info("%s [%s] ⛔ 签到时段已结束，跳过执行", ctx.log_tag, _mask_phone(phone))


#: 「本执行体未执行」的三种判因（`_mark_unreached` 写进调用方的 `unreached`）。
#: 三档必须分开：`PEER` 是**跨执行体交接的正常形态**（别人已经接手），`RETRY` 是**本轮
#: 执行过但没成**（已回炉待重试），`UNCLAIMED` 才是**没人接手**的故障；并成一档就会把
#: 正常交接报成故障，或者反过来把故障报成正常。
UNREACHED_PEER = "peer"
UNREACHED_RETRY = "retry"
UNREACHED_UNCLAIMED = "unclaimed"

#: 上一次落下的「已由他人负责」批量留痕**组成**（`_mark_unreached` 的去重依据）：
#: `(执行体, 账号数, 分因明细)`。兜底常驻循环每 ~5s 一拍、窗口内可上百轮；同一批被别人
#: 领走的账号在每轮组成不变，逐轮重落只是刷屏（2026-10-10 工单 81xt 的 LOW-1）。状态是
#: 进程内模块级变量：不落盘、不跨进程。取组成而非轮次/时间：轮次每轮都变，并进去就永远
#: "变了"。身份必须在键里：同进程换执行体（测试夹具、进程内多执行体）若只按组成判，
#: 第二个执行体会被判成"没变"而漏掉它唯一的一条批量——与 `_log_banner` 的 `_BANNER_LAST`
#: 同一形状。peer 为 0 时清回 `None`：同组成 N→0→N 的第三次留痕不得被吞。
_UNREACHED_PEER_LAST = None


def _mark_unreached(ctx, accounts):
    """轮末判因：本执行体范围内、`results` 里没有的账号到底落到哪儿去了。

    判据取两处**本进程握着的事实**，不猜：

    - `ctx.attempted`（本执行体跑过这个账号）⇒ `RETRY`：这一轮执行过、没成而回炉待重试
      （`retrying` 档）。它不是"没轮到"，事件/状态文件已由那次尝试写过，本函数不再补行。
    - 队列（唯一台账）里那一行的 `state` 与 `owner`（`queue_store.row_owners`）：
      归属不是本执行体 ⇒ `PEER`（这件活派给了别的执行体，或已被它领走/了结，
      **本执行体没领到不等于签到失败**）；归属是本执行体、或无人归属且仍 `pending`
      ⇒ `UNCLAIMED`（没人接手，那是要暴露的故障）；
    - 队列读不通（哨兵）⇒ 判因不可得，按 `UNCLAIMED` 计并留 warning：不把"读不出来"
      说成"别人做了"（那是掩盖），也不静默丢账。

    为什么必须留痕：2026-10-07 生产里任务被别人领走时执行层**零事件、零日志**，
    `sign_tasks.result` 又被实际领取者覆盖成"签到成功"，数据侧完全看不出"没轮到"，
    运维只能靠人工比对日志时间线。

    留痕按判因的**性质**分两档：

    - `PEER`（跨执行体交接的正常形态，不是故障）收成**一条批量**：日志一行 + 事件一行，
      带账号数。逐账号落库会按账号数线性灌噪声——2026-10-10 生产该扫描因此落 1830 行
      `pending` 事件与 1830 行日志（工单 81xt）。措辞与形状对齐本函数里那条
      "未执行判因不可得"批量告警。该批量按**组成**去重（见 `_UNREACHED_PEER_LAST`）：
      执行体、账号数与分因明细都不变时整段跳过，不重复写日志与事件。
    - `UNCLAIMED`（无人接手，真异常）**逐账号**留痕：判因文本与故障账号逐条可查。
      健康运行下这类账号为 0，逐条不产生日常噪声；一旦出现，运维要能按账号定位。
      `_emit_event` 的 message 截断在 200 字符，一条批量装不下上百账号的明细，
      故不并进批量。

    两档与调用方的汇总计数**同一份判因**——两处口径不会互相漂移。

    `ctx.unreached` 为 `None`（调用方不要判因，例如只关心自己退出码的子执行体）时本函数
    只做事件留痕，不填 out-param。
    """
    global _UNREACHED_PEER_LAST
    attempted = getattr(ctx, "attempted", ())
    phones = [a.phone for a in accounts
              if a.phone not in ctx.results
              and a.phone not in (ctx.delegated or ())]
    if not phones:
        return
    pending_judge = [p for p in phones if p not in attempted]
    rows = queue_store.row_owners(ctx.day, pending_judge) if pending_judge else {}
    mine = {ctx.executor_id, ctx.runtime_id}
    if rows is None:
        # 判因不可得：**一条** warning（不是逐账号一条——大站一轮有上百个账号，逐号
        # 告警会把日志淹掉）；逐账号的判因文本仍落事件表与 INFO 行，可查性不受损。
        logger.warning("%s 未执行判因不可得（当日任务归属读不通），%d 个账号按「无人接手」计",
                       ctx.log_tag, len(pending_judge))
    # 「已由他人负责」的账号收进这一批，循环里不逐账号落事件与日志（见 docstring）。
    peer_n = 0
    #: `{role/state: 账号数}`——只用于那条批量日志的分因明细（日志不截断）。
    peer_detail = {}
    for phone in phones:
        message = ""
        if phone in attempted:
            reason = UNREACHED_RETRY
            # 已由那次尝试落过事件与状态，不重复写
        elif rows is None:
            reason = UNREACHED_UNCLAIMED
            message = "本执行体未执行：判因不可得（当日任务归属读不通）"
        else:
            state, owner = rows.get(phone, ("", ""))
            reason = UNREACHED_PEER if (owner and owner not in mine) else UNREACHED_UNCLAIMED
            if reason == UNREACHED_PEER:
                role = egress.parse_owner(owner)["role"]
                peer_n += 1
                key = f"{role}/{state or '?'}"
                peer_detail[key] = peer_detail.get(key, 0) + 1
            elif state == queue_store.STATE_PENDING:
                message = f"本执行体未执行：行仍待领（state={state}）"
            elif state:
                message = f"本执行体未执行：行状态={state} 且无归属执行体"
            else:
                message = "本执行体未执行：队列中没有当日行"
        if ctx.unreached is not None:
            ctx.unreached[phone] = reason
        if message:
            # 真异常（无人接手）：**逐账号**留痕，判因文本与账号逐条可查。
            _emit_event(ctx, phone, yiban_status.STATUS_PENDING, message)
            logger.info("%s [%s] ⏳ %s", ctx.log_tag, _mask_phone(phone), message)
    if peer_n:
        # 跨执行体交接的**一条批量**留痕：日志一行 + 事件一行，带账号数与分因。
        # 事件是摘要行（`phone=""`，没有单一账号），读者面按摘要行口径排除其账号统计
        # （见 `store.events` 的 sign_event_stats / sign_event_accounts_summary）。
        # 组成去重：签名取 `(执行体, 账号数, 分因明细)`，**不含轮次/时间**——兜底常驻循环
        # 每轮扫描，组成不变就整段跳过（工单 81xt 返修 LOW-1）；组成一变立刻回到留痕。
        # 身份进键：同进程换执行体不得互相压掉（同 `_BANNER_LAST`）。
        signature = (ctx.executor_id, peer_n, tuple(sorted(peer_detail.items())))
        if signature != _UNREACHED_PEER_LAST:
            digest = "，".join(f"{k}×{n}" for k, n in sorted(peer_detail.items()))
            logger.info("%s 未领取：任务已由其他执行体领取，%d 个账号（跨执行体交接，%s）",
                        ctx.log_tag, peer_n, digest)
            _emit_event(ctx, "", yiban_status.STATUS_PENDING,
                        f"本执行体未领取：任务已由其他执行体领取，共 {peer_n} 个账号")
            _UNREACHED_PEER_LAST = signature
    else:
        # peer 为 0：清回 `None`。同组成 N→0→N 时，中间的 0 拍必须让第三次留痕复活。
        _UNREACHED_PEER_LAST = None


#: 上一次打出的 v3 横幅**规模与归属**（`_log_banner` 的去重依据）：`(身份, 通道数, 分片数)`。
#: 身份必须在键里：去重键只留规模时，同一进程里换一个执行体（测试夹具、将来的进程内
#: 多执行体）会被判成"没变"而漏掉它起跑的唯一一条 INFO。
_BANNER_LAST = None


def _log_banner(ctx):
    """起跑横幅（通道数/分片数/执行体标识）：**规模变了才打 INFO**，没变降 DEBUG。

    横幅报的是本轮的规模与归属。常驻空转轮（兜底每 ~5s 一拍、窗口内可上百轮）会逐轮
    重复同一行——生产 2026-10-07 实测全天 184 次，全是同一串「6 条通道 / 64 个分片」。
    降级而不是静默：DEBUG 下仍逐轮留痕，排障时数得清轮次。

    去重键取**身份 + 规模**（不含轮次与时间）：轮次每轮都变，把它并进去就永远"变了"，
    等于没降噪。身份或规模一变（换执行体、通道数或分片集变了）立刻回到 INFO。

    执行体标识走 `ctx.log_tag`（角色+槽位+可选轮次），**不再打身份原串**——原串带主机名，
    属部署信息，本模块 docstring 的红线是"任何接口/日志都不得回串"。
    """
    global _BANNER_LAST
    scale = (ctx.executor_id, ctx.m, len(ctx.shards))
    line = "%s v3 执行体：%d 条通道 / %d 个分片" % (ctx.log_tag, ctx.m, len(ctx.shards))
    if scale != _BANNER_LAST:
        logger.info(line)
    else:
        logger.debug(line)
    _BANNER_LAST = scale


# ---------------------------------------------------------------------------
# 同步入口
# ---------------------------------------------------------------------------
def run_executor_v3(accounts, *, day=None, dry_run=False, delegated=None,
                    cred_state=None, notify_url="", event_sink=None,
                    cfg=None, rng=None, requeue_final=False, claim_all=False,
                    requeue_during_run=False, reclaim=False,
                    yield_probe=None, unreached=None, round_no=None):
    """同步入口（内部 `asyncio.run`）：跑一轮，返回
    `{phone: (success, message, skip, status)}`——调用方的收尾与退出码汇总零改动。

    当日回炉口（队列以 `pending` 为唯一可领态，`failed` 行不翻态就当日无人接手）：

    - 轮首回炉默认发生，且**只回炉默认档**（`retry:` 前缀）——与旧领取池"默认参数可
      再领 retry: 档"同一条档位纪律；`final:` 与无前缀历史行是保守档，缺省绝不自动复活
      （复活 = 风控账号每轮重登）。
    - `requeue_final=True`：显式路径口子（对齐旧领取池的 `retry_failed`：补签轮/有界一次性
      轮次才传），连同 `final:` 档与无前缀历史行一并回炉。常驻兜底与定时轮都不传。
    - `claim_all=True`：领取范围从"本执行体 HRW 分片集"放宽为全部分片。**只给兜底腿**
      （`fallback@…` 身份不在执行体候选集里，不放宽就是"看着在跑、其实零领取"的静默
      空转）；兜底传的是全量账号，故宽分片不会越界。手动 `--only` **不用**它——它改用
      "本轮账号自己的虚分片 + 账号允许集"的窄范围（见 `reclaim`）。
    - `requeue_during_run=True`：补货循环的恢复周期（每 `RECOVER_SEC`）顺带把本轮刚
      弃权的默认档翻回 `pending`，不等下一场会话。只回炉默认档，保守档不变。
    - `reclaim=True`：**手动 `--only` 专用**的有界显式路径——把到手账号当日已了结
      （`done` / `skipped`）的行翻回 `pending` 并补建计划行，使"用户主动点的那一下照做"
      的旧领取池语义（`allow_settled=True`）在单池下仍有等价形态。**领取/回收/回炉/待办
      计数全部收窄到本轮账号的虚分片 + 账号允许集**（回收也按允许集收窄：只回收本轮账号
      自己的陈旧 `claimed`，不碰别人的行），并跳过跨账号的死主接管，且遇
      `phone ∉ accounts` 的行绝不了结——手动轮不得碰别人的行（否则那些行会被误判
      "已了结"⇒ 静默漏签）。缺省关，定时轮与兜底都不传。

    `dry_run=True` 只转调 `shadow_stats`（零落库 / 零领取 / 零请求）并返回空结果。
    `cred_state` **就地改传入的那个 dict**：调用方持有同一引用并在收尾保存，重新绑定会让熔断
    计数写不回。`notify_url` 只为与旧调用签名对齐：逐次失败不在这里通知（汇总邮件是
    runner 收尾的职责，只在最终放弃时通知一次）。
    计划不可用时返回空结果并留 error：队列就是 `sign_tasks`，没有可用的库就没有队列。

    `yield_probe`：**让位的运行期判据**（缺省 `None` = 不让位）。补货循环每次领取前问一次，
    答 True 就立即停止领取、已领的照常收尾。为什么必须有它：让位判据写在调用方（兜底主
    循环）的**循环顶**，而一轮扫描是一次调用、可能跑满整个窗口（2026-10-07 生产：兜底
    一次赢下启动竞态就吸收了整个窗口 84/100 的工作量）。让位因此不能依赖"进入这一轮时
    那一刻的锁状态"，必须在轮内复检——这正是本形参的存在理由。**只给兜底腿**：定时轮的
    子执行体在监督进程持全局锁期间本就该照常干活，它们传它就会全员停摆。

    `unreached`：调用方传入的 **out-param dict**，本函数在轮末往里填
    `{phone: 判因}`（`UNREACHED_PEER` / `UNREACHED_UNCLAIMED`，见 `_mark_unreached`）——
    本执行体范围内、`results` 里没有的账号各一条。判因同时喂两处：调用方的汇总计数
    与 `sign_events` 留痕（同源同口径，见 `_mark_unreached`）。缺省 `None` = 调用方不要。

    `round_no`：**只影响日志归因**，不参与任何判定。本执行体的日志行都带
    `egress.owner_tag` 渲染的前缀（角色+槽位，不含主机名），`round_no` 传进来则再带一个
    轮次序号（`[fallback r7]`）——多执行体并发写同一个按天日志文件，排障要能答出
    "这个账号是哪个执行体在第几轮动的"。只有**同一进程内跑多轮**的兜底常驻会传；
    定时轮/手动轮一个进程只跑一轮，不传（行上是 `[worker-3]`）。
    口径边界（**不许在这里写"每一条"**）：带前缀的是"这一轮/这一个账号"的日志行与起跑横幅；
    计划构建期的行（`_ensure_plan` 及其"当日计划不可用"、`_max_vshard` / `_plan_covers` /
    `_parse_run_at`）与监督进程关于子进程的行不带——那时身份虽已可算，但判据是"ctx 是否已
    建立"（这些行与 ctx 生命周期无关），后者则逐条点名槽位号。

    **未预期异常一律不外逃**：本函数是 `runner` 退出码汇总的前置调用，traceback 逃出去退出码
    就落到契约（0/1/2/3/10）之外，`run.sh` 的补签闸门与状态写入随之失真。按"结果是否已成型"
    分两档处置，各自的理由见函数体内两处注释。两档都只兜 `Exception`；`KeyboardInterrupt` /
    `SystemExit`（超时击杀、显式退出）必须照常外逃。
    """
    cfg = cfg or schedule.planner_config()
    day = day or _now().strftime("%Y-%m-%d")
    if dry_run:
        shadow_stats(accounts, day=day, cfg=cfg)
        return {}
    cred_state = cred_state if cred_state is not None else {}
    try:
        v = _ensure_plan(accounts, day, cfg, force=reclaim)
    except Exception as e:
        # 丢结果档①：ctx 构建 → 预扫 → 装桶 → asyncio.run 这一段失败时，ctx.results 里可能已有
        # 跑完的账号，但一律丢弃（异常可能在写状态/收尾中途冒出，留下的结果集不完整、不可信），
        # 记 error 后返回空结果——与"计划不可用"同一处置，由 runner 汇总成契约内的"未执行"
        logger.error("当日计划不可用，本轮不执行（v3 需要可用的队列库）: %s", e)
        return {}
    # **两个身份显式分开**（详见 `_Ctx` 的字段注释）：
    # - 稳定槽位名（`executor_id`）：HRW 分片成员判据（`hrw.shards_of` 要求它是
    #   `cfg["executors"]` 的成员，否则一件活都领不到）与出口令牌桶的持久键
    #   （`egress_state.egress`，跨重启必须同名才能续上自适应速率）；
    # - 运行时身份（`runtime_id = egress.runtime_owner(稳定名)`）：写进 `sign_tasks.owner`
    #   的**持有者**身份，含本进程的进程号与代次。
    # 为什么持有者必须含进程号/代次：同名进程在 v3 仍可能并存（同槽位重启后的新进程、
    # 同机手工再起一个），而收尾/重排/接管的 CAS 按 owner 做作用域校验——名字相同就
    # 分不出"是不是同一个持有者"。计划行（`planner.write_plan`）仍写稳定名：那是 HRW
    # 归属（"这件活归哪个槽位"），与"此刻谁在持有"不是一回事。
    executor_id = (os.environ.get("YIBAN_EXECUTOR_ID", "").strip()
                   or egress.single_owner())
    runtime_id = egress.runtime_owner(executor_id)
    slot = _worker_slot(executor_id)
    alive_role = _worker_role(executor_id)
    # 起跑写文件心跳：执行体页读的是监督进程写的**文件心跳**，单进程 v3 路径不写就只会
    # 显示 idle。回收必须在领取之前——崩溃通道留下的 `claimed` 行只有先回到 `pending`
    # 才会被 `claim_batch` 重新领取（不回收就是"崩溃即卡死"）。回收只碰本业务日
    # （`day=day`）：跨日回收会把历史行回退成永不被领的空转行，还会重置跨午夜长轮次的
    # 在飞行。心跳写失败只留 debug，回收失败由 queue_store 内部吞掉并告警，两者都不阻断签到。
    #
    # 起跑也要判死接管，且顺序是「回收 → 接管 → 预扫/首轮领取」：回收把死主留下的过期
    # `claimed` 行变回 `pending`，接管才有东西可并。为什么不能只靠补货循环里那次接管——
    # 它按 `RECOVER_SEC`（60s）节流，而补货首轮的计时差恒为 0；短轮次里本执行体干完自己
    # 的活就收干退出（90 账号的小站是常态），等不到那一刻。死主分片的 `pending` 行于是
    # 整轮无人领取，补签轮沿用同一套分片划分仍无人领 ⇒ **静默漏签**。判死口径与补货循环
    # 共用 `_widen_with_dead_peers`（不另起第二份），只有 `stale` 才算死，活着的执行体不受影响。
    state_io.mark_worker_started(slot, now=_now(), role=alive_role)
    # 归因前缀算**一次**：ctx 建立之前的行（计划不可用）与 ctx 可能已丢的行（异常收尾）
    # 都要用它，而那时 ctx 取不到；渲染本身仍只有 `egress.owner_tag` 一处。
    log_tag = egress.owner_tag(executor_id, round_no)
    # 允许集在**回收之前**就要定下来（回收也要按它收窄）：手动 `--only` 轮只回收本轮
    # 账号自己的陈旧 `claimed` 行，不碰别人的行（回收别人的行是跨账号写，`epoch+1` 还会
    # fence 掉一个仍存活但慢的持有者的迟到收尾）。**不整段跳过回收**——手动账号自身若是
    # 陈旧 `claimed`，正需要这条路径把它拉回来（`reclaim_tasks` 只翻 `done`/`skipped`）。
    allowed_phones = frozenset(a.phone for a in accounts) if reclaim else None
    # 轮首回收同样要带**存活持有者豁免**（M17）：此刻本进程还没领任何行（`held` 是空
    # 集），但兄弟执行体可能正在跑上一段会话、在飞的行租约刚过期。不豁免就会把活着的
    # 持有者手上的行回收成 `pending`，本轮 `claim_batch` 领走它，而对方通道队列里还躺着
    # 同一行 ⇒ 同一账号两次真实登录。判活与补货循环内那次同一份（`_live_row_owners`，
    # 按**队列里在途持有者**判，兜底常驻身份也在这份名册里）。
    queue_store.reap_expired(now=_stamp_ms(_now()), day=day, phones=allowed_phones,
                             live_owners=_live_row_owners(day, executor_id))
    try:
        ctx = _Ctx(
            accounts={a.phone: a for a in accounts},
            day=day, cfg=cfg, v=v,
            # 手动 `--only`：分片集取**本轮账号自己的虚分片**（不依赖本执行体是否是 HRW
            # 候选集成员，也不用通配 `claim_all`——宽分片会顺手领走别人的行）；配合
            # `allowed_phones` 的与关系，只有本轮传进来的账号可被领取/回炉。
            shards=((tuple(sorted({hrw.vshard_of(a.phone, day, v) for a in accounts}))
                     if reclaim else
                     (tuple(range(v)) if claim_all
                      else hrw.shards_of(executor_id, cfg["executors"], day, v)))),
            executor_id=executor_id, runtime_id=runtime_id, results={},
            cred_state=cred_state,
            delegated=delegated, notify_url=notify_url, event_sink=event_sink,
            rng=rng or random.Random(), slot=slot, alive_role=alive_role,
            requeue_during_run=requeue_during_run, reclaim=reclaim,
            allowed_phones=allowed_phones, yield_probe=yield_probe,
            unreached=unreached, round_no=round_no, log_tag=log_tag)
        # 接管须在预扫之前：预扫按 `ctx.shards` 判"不在本执行体分片集"的账号，接管把死主
        # 分片并入后这些账号已归本执行体，不该再被登记成"别人负责的活"。
        # 手动 `--only` 轮不接管死主分片：那是跨账号写别人的行，不是用户点这一下的范围。
        if not reclaim:
            ctx.shards = _widen_with_dead_peers(ctx, ctx.shards)
        # 显式重签（手动 `--only`）：先把到手账号当日已了结的行翻回 `pending`，否则
        # `claim_batch` 只取 `pending`，用户点下的那一下会沦为"未执行"。
        if reclaim:
            queue_store.reclaim_tasks(day, [a.phone for a in accounts])
        # 轮首回炉（作用域=本轮领取集 + 本业务日 + 允许集）：默认档 failed 翻回 pending
        # 才谈得上被本轮领到；回炉逐行走 `requeue_task` 的 state+epoch 门，在飞/终态行绝不
        # 复活。放在接管之后、预扫之前：死主分片并入后一并扫到。
        # 回炉回 `None` = 读不通：不当作"没有要回炉的行"，把记号交给补货循环计入同一条
        # 有界闸门（`QUEUE_UNREADABLE_MAX_ROUNDS`），不许就地丢掉（ba-p01-01）。
        if queue_store.requeue_failed(day, ctx.shards, include_final=bool(requeue_final),
                                      phones=ctx.allowed_phones) is None:
            ctx.queue_unreadable = True
        _prescan(ctx, accounts)
        ctx.limiter.restore_from_store(ctx.egress, now=_mono())
        if ctx.global_limiter.invalid:
            logger.warning("%s %s 非法，全局速率上界按不限处理（不阻断签到）",
                           ctx.log_tag, ENV_GLOBAL_RATE)
        _log_banner(ctx)
        asyncio.run(_run_async(ctx))
    except Exception as e:
        # 异常文本经 _sanitize_text 防注入、_mask_phones_in_text 抹手机号后再落日志
        logger.error("%s v3 执行体未预期异常，本轮按无结果收尾（不外逃）: %s",
                     log_tag, _mask_phones_in_text(_sanitize_text(str(e))))
        return {}
    try:
        _mark_window_skips(ctx, accounts)
    except Exception as e:
        # 只丢收尾档②：到这里结果集已经成型，照常返回 ctx.results，只把这一段记 error 并继续。
        # 并进"丢结果"那一档会把一轮基本成功的活汇总成"全部未执行"（退出码 1 + 失败邮件），与
        # 事实相反；没被收尾的账号由 runner 按"未执行"计入失败，可见性不受损
        logger.error("%s v3 窗口收尾失败，已完成的账号结果照常返回: %s",
                     log_tag, _mask_phones_in_text(_sanitize_text(str(e))))
    try:
        _mark_unreached(ctx, accounts)
    except Exception as e:
        # 与窗口收尾同一档：结果集已成型，判因失败只丢"未执行"的分因（调用方退回按
        # "没人接手"计），照常返回结果。不并进"丢结果"那档——那会把一轮基本成功的活
        # 汇总成"全部未执行"。
        logger.error("%s v3 未执行判因失败，已完成的账号结果照常返回: %s",
                     log_tag, _mask_phones_in_text(_sanitize_text(str(e))))
    # 收尾写心跳**只在正常返回路径**（不是 finally）：四态从 running 落到 finished。
    # 异常/中断路径不写——`KeyboardInterrupt` / `SystemExit` 是 BaseException、会直接
    # 外逃，写进去就把"被信号杀掉"记成"正常跑完"；留"有开始、无收尾"让心跳过期后判
    # `stale`（疑似被强杀，要用户注意），与监督进程不写收尾的口径一致。
    state_io.mark_worker_finished(slot, now=_now(), role=alive_role)
    # 进度打点「收尾」：与上一行同时机、同一条"只在正常返回路径"的口径——被信号
    # 杀掉的轮次留"有开始、无收尾"，进度流据此与执行体页的四态判定一致。
    # message 前缀是本层的判别面（见 run_events 模块说明的"收尾有两层"）：单执行体
    # 路径下 runner 另落一行"轮次收尾："，两行同 (业务日, 执行体, 节点) 但事实不同。
    _report(ctx, run_events.NODE_FINALIZE,
            message=f"执行体会话收尾：本轮完成 {len(ctx.results)} 个账号")
    return ctx.results
