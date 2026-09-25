# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""**功能**
调度 v3 的执行体核心：单进程 asyncio **M 条通道**从 `sign_tasks` 批量领取自己分片集内
到点的任务，按计划时刻并发执行（`attempts.attempt_signin` 整体经 `asyncio.to_thread`
提交，线程池上限 = M），出口令牌桶限速、每账号 gap 门、装订式抖动退避、终态批量收尾。

**归属**
`yiban.engine` 的执行层（调度 v3）。计划层（`planner`）决定"何时做"、`token_bucket`
决定"多快做"，本模块把两者与队列消费接起来；入口与退出码汇总仍在 `runner`。

**当前是否生效**：否——`YIBAN_SCHEDULER_V3` 缺省 0，未开闸时本模块不执行签到，轮次走
`round.run_queue_retry`，这里只有 `scheduler_v3_enabled` 被 `runner.main` 与
`schedule.capacity_of` 读取；开闸后 `runner.main` 才把执行体换成 `run_executor_v3`。

**复用**
`scheduler_v3_enabled` 是开关的唯一判据（分流谓词的另一半在调用方）；`next_retry_at_v3`
是 v3 的重试落点（与 v2 的 `round._next_retry_at` 同窗口准绳、不同采样）；`shadow_stats`
是影子期（`dry_run`）的落点对账入口。

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
谁调用：`runner.main` 的分流点——`YIBAN_SCHEDULER_V3` 为真且非 `--only` 时替换
`round.run_queue_retry` 那一行调用。
"""
import asyncio
import contextlib
import datetime
import logging
import os
import random
import time
from concurrent.futures import ThreadPoolExecutor

from yiban import clock, egress, window
from yiban import status as yiban_status
from yiban.engine import alerts, attempts, hrw, planner, schedule, state_io, token_bucket
from yiban.masking import mask_phone as _mask_phone
from yiban.masking import mask_phones_in_text as _mask_phones_in_text
from yiban.masking import sanitize_text as _sanitize_text
from yiban.store import clock_meta, queue_store

logger = logging.getLogger("yiban")

#: 开关缺省值：关闭（未设 / `0` / `false` / 非法值全部走旧路径，开关即回滚）
DEFAULT_V3 = False
#: 分流开关的键：真值表口径复用 `schedule._env_flag`（1/true/on/yes，大小写不敏感）
ENV_SCHEDULER_V3 = "YIBAN_SCHEDULER_V3"
#: 全局聚合速率上界 Λ 的键（attempt/s，缺省空 = 不限）。本模块是它唯一的读取点。
ENV_GLOBAL_RATE = "YIBAN_GLOBAL_RATE"
#: 退避落点的硬上限（秒）：安全类参数，恒为常量、不随窗口或速率自适应
RETRY_CAP_SEC = 600
#: 补货间隔（秒）：批量领取的轮询周期
REFILL_SEC = 5
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
    """执行体槽位序号（文件心跳按槽位命名）：从身份串取，取不到用 0。

    `worker-{i}@{host}` → i；`single@` / `fallback@`（无序号）与解析不出的身份串 → 0。
    心跳只是可观测性，取不到序号不该让签到失败，故回退 0 而不是抛。
    """
    idx = egress.parse_owner(executor_id).get("index")
    return 0 if idx is None else int(idx)


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
# 开关与影子模式
# ---------------------------------------------------------------------------
def scheduler_v3_enabled(env=None):
    """`YIBAN_SCHEDULER_V3` 是否为真（真值表口径唯一在 `schedule._env_flag`）。

    未设 / 空 / `0` / `false` / 手写错值一律为假——缺省即走旧路径，开关即回滚。
    """
    return schedule._env_flag(ENV_SCHEDULER_V3, env)


def next_retry_at_v3(now_dt, sch_cfg, last_delay, rng=None):
    """装订式抖动退避的落点 → datetime；`None` = 有效窗口放不下，放弃重试。

    `delay = min(cap, uniform(base, max(base, last_delay × 3)))`，其中
    `cap = min(RETRY_CAP_SEC, 剩余有效窗口 × 0.3)`：下界是 `YIBAN_RETRY_MIN_INTERVAL`
    （防连击），上界随剩余窗口收缩（不在尾端扎堆），落点再夹到有效窗口结束——**绝不让
    重试越过窗口**，放不下就返回 `None`。窗口几何一律走 `window.bounds`（排计划、判
    关闭、容量预估的同一准绳）。`last_delay` 是上一次的 delay（进程内记账、无持久化）：
    换执行体接手或首次重试时未知，调用方传 `base` 即可——保守且收敛。
    """
    rng = rng or random.Random()
    base = max(1, int(sch_cfg["retry_min_interval"]))
    win = window.bounds(sch_cfg)
    remaining = win.remaining_sec(now_dt)
    if remaining <= 0:
        return None
    cap = min(RETRY_CAP_SEC, remaining * 0.3)
    delay = min(cap, rng.uniform(base, max(base, float(last_delay) * 3)))
    base_day = now_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    target = min(now_dt + datetime.timedelta(seconds=delay),
                 window.to_dt(base_day, win.hi_min))
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


def _ensure_plan(accounts, day, cfg):
    """保证当日有**可用**计划行（v3 的队列就是计划），返回当日 V。

    "有计划"的判据必须是当日有真实计划行（`vshard >= 0`）：历史平移与补账留下的
    `vshard=-1` 行是惰性的——不在任何分片集内、永不被领取，若把它们当成"计划已就绪"，
    建计划被跳过而队列里又没有可领的行，本轮零领取、零请求，汇总成"全部未执行"。
    这类行按设计保持原样，只是不再充当"计划已就绪"的证据（`write_plan` 是
    `INSERT OR IGNORE`，补建也不会覆盖它们）。

    `write_plan` 失败会抛，由调用方捕获后放弃本轮——没有计划行就没有队列，发不出任何
    请求，裸抛只会把 traceback 交给调用方。
    """
    top = _max_vshard(day)
    v = _plan_v(day, cfg, accounts, top)
    if top is None:
        if planner.has_plan(day):
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
                 runtime_id=None):
        self.accounts = accounts
        self.day = day
        self.cfg = cfg
        self.v = v
        self.shards = shards
        # 稳定槽位名：HRW 分片成员判据（`hrw.shards_of` 要求它是 `cfg["executors"]`
        # 的成员）与出口令牌桶的持久键（`egress_state.egress`）都用它。**不用于写库**。
        self.executor_id = executor_id
        # 写库的**持有者**身份（`sign_tasks.owner`）：稳定名再拼本进程的进程号/代次。
        # 与稳定名分开是必需的——同名进程（同槽位重启、同机两个进程）在 owner 上必须
        # 可分辨，否则收尾/重排/接管的 CAS 分不出"是不是同一个人"。
        self.runtime_id = runtime_id or executor_id
        # 文件心跳的槽位序号（`worker_presence` 按槽位读）：执行体页据此判存活四态
        self.slot = slot
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
            "attempt": attempt_no,
            "dur_sec": dur,
            "finished_at": ts,
        })
    except Exception:
        pass


def _settle(ctx, phone, epoch, state, message):
    """单行收尾：带上领取时的 fencing token——被接管者迟到的写会被拒。

    owner 用 `ctx.runtime_id`（领取时写进 `sign_tasks.owner` 的那个运行时身份）：
    收尾的 CAS 校验的是"还是不是我持有的这一行"，稳定名在这里会把同槽位的另一代
    当成自己人。
    """
    queue_store.settle_tasks(ctx.runtime_id, ctx.day, [(phone, message)],
                             state=state, epochs={phone: epoch})


def _finish(ctx, phone, epoch, result, state_message, state):
    """零请求收尾（暂停 / 账号已不在配置）：写状态、事件、结果并了结该行。"""
    _ok, _msg, _skip, status = result
    ctx.results[phone] = result
    state_io._write_sign_state(phone, status, state_message)
    _emit_event(ctx, phone, status, state_message)
    _settle(ctx, phone, epoch, state, state_message)


def _is_risk_signal(message):
    """风控信号判定：WAF 拦截或命中风控关键词（与失败分级同一批关键词）。

    `is_waf_blocked` 的入参契约是**响应体**（它按"短响应"设界，见 `yiban.security`），
    这里传的是失败 `message`：两者共享同一批关键词，且 `message` 可能内嵌服务端返回的
    `\\uXXXX` 转义 JSON——保留这一路解码。要按契约传响应体，得把响应对象一路带到这里
    （新数据源）；在那之前本判定以 `message` 为准。
    """
    return attempts.is_waf_blocked(message) or any(
        kw in message for kw in attempts.RISK_FAIL_KEYWORDS)


def _log_give_up(phone, tried, status, message):
    """最终放弃的留痕：与 v2 的放弃路径同级别、同语义。

    为什么分两级：无点位是易班侧没有数据（非账号/凭据问题，管理员无从修复，重试也拿不
    到），v2 对它只留 warning、不按"签到失败"告警；其余失败才是 error。手机号与原因都
    按全模块同一脱敏口径落日志。
    """
    if status == STATUS_NO_POSITION:
        logger.warning("[%s] 🚫 易班未返回签到点位，当日不签到（重试无意义）: %s",
                       _mask_phone(phone), _sanitize_text(message))
        return
    logger.error("[%s] ❌ 已尝试 %d 次，放弃: %s",
                 _mask_phone(phone), tried, _sanitize_text(message))


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
        return
    ctx.gap_gate.commit(phone, _mono())  # 走到这才是"真要发请求"：gap 的推进点必须与尝试一一对应
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
        _settle(ctx, phone, epoch, queue_store.STATE_FAILED, message)
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
            logger.warning("[%s] ⏳ %s", _mask_phone(phone), retry_msg)
            return
        logger.error("[%s] ❌ 窗口剩余不足，不再重试: %s",
                     _mask_phone(phone), _sanitize_text(message))
    else:
        _log_give_up(phone, attempts_n + 1, status, message)
    ctx.results[phone] = (False, message, False, status)
    _alert_give_up(ctx, acc, phone, status, message)
    _settle(ctx, phone, epoch, queue_store.STATE_FAILED, message)


async def _lane(queue, lane_id, ctx):
    """通道主体：取条目 → 等到点 → 取额度 → 提交一次尝试 → 收尾。

    每条通道对应线程池里的一个线程（默认执行器上限 = M，`asyncio.to_thread` 是唯一进
    线程的调用），故 M 条通道与 M 个线程一一对应，无嵌套提交造成的自我死锁。
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


def _widen_with_dead_peers(ctx, shards):
    """把心跳已过期的执行体的分片并入本轮领取范围，并把这些分片内的 `pending` 行改归本
    执行体；返回并入后的分片集（升序去重）。

    判据是**既有文件心跳的四态**（`state_io.worker_presence`）：只有 `stale`（有开始记录、
    无收尾且心跳过期）才算死。监督进程未启动 / 单进程直跑时该槽位没有当日记录，四态回
    `idle`——**不是** `stale`，故不会误接管活着的执行体。`vshard=-1` 的历史行不属于任何
    分片集（`hrw.shards_of` 产出 `0..V-1`），天然不在接管范围内，不需要额外过滤。

    **并入与"偷到多少行"解耦**：判死就并入分片，`steal_shards` 的返回值只用于日志。
    若拿 `taken > 0` 当门，死主"把分片内的待办全领成 `claimed` 后崩"就漏了——
    `reap_expired` 回收这些行时把 `owner` 清成 `''`，分片内已没有 `owner=<死主>` 的
    `pending` 行，`steal_shards` 返回 0，分片不进领取集；随后回收出的 `pending` 行
    无人可领 = "崩溃即卡死"（`claim_batch` 不筛 `owner`，并入即可领）。
    归属修正仍要做：owner 与实际接管者一致，展示与后续判死才有意义。日志只在真改归了
    行时打（判死后的分片每 `RECOVER_SEC` 都会再并一次，按"有行"打不会刷屏）。
    """
    v = getattr(ctx, "v", 0)
    if v <= 0:
        return tuple(shards)
    extra = []
    for peer in ctx.cfg.get("executors", ()):
        if peer == ctx.executor_id:
            continue
        state, _seen = state_io.worker_presence(_worker_slot(peer), now=_now())
        if state != state_io.WORKER_STATE_STALE:
            continue
        peer_shards = hrw.shards_of(peer, ctx.cfg["executors"], ctx.day, v)
        if not peer_shards:
            continue
        # `me` 用运行时身份（写库的持有者），`peer` 用稳定槽位名：死主的行有两类
        # owner（计划 owner 是稳定名、被重排回来的行带着它的运行时身份），
        # `steal_shards` 按前缀把两类都算进来。
        taken = queue_store.steal_shards(ctx.runtime_id, peer, peer_shards, ctx.day)
        if taken:
            logger.warning("接管心跳过期的执行体 %s 的分片集，%d 条待办改归本执行体",
                           peer, taken)
        extra.extend(peer_shards)
    if not extra:
        return tuple(shards)
    return tuple(sorted(set(shards) | set(extra)))


async def _refiller(queue, shards, ctx):
    """补货：每 `REFILL_SEC` 秒批量领取自己分片集内到点的任务投进通道队列。

    出队排序键是 `(PRIORITY_ORDER_BASE, run_at)`：`claim_batch` 不返回 `priority`，
    重试任务的 priority 只在库里递增、这里拿不到——但"重试排在新任务之后"由 `run_at`
    承担（退避已把重试落点推到未来），故固定 priority 入队不影响顺序。

    收干判据必须带 `pending_count`：`claim_batch` 在"下次到点在 `REFILL_SEC` 之后"时
    本来就返回空，只看空返回会把还有待办的一轮误判成收干；也**必须**带 `busy`：正等待
    到点的条目在库里已是 `claimed`、不在 `pending_count` 里，不数进来会在通道还在跑时
    提前收干，把刚重排回 `pending` 的重试任务留在库里没人领。

    同一循环按间隔驱动两件恢复动作（**函数内不持时间状态**，间隔常量在模块级）：
    - `reap_expired`：回收本业务日内租约**超出宽限期**的 `claimed` 行——不回收的话崩溃
      通道留下的行永远不被重领（`claim_batch` 只取 `pending`），即"崩溃即卡死"；宽限期
      挡住"还在飞但租约已到"的慢尝试被误回收（详见 `queue_store.REAP_GRACE_SEC`）。
      **只回收本业务日**（`day=ctx.day`）：不带 `day` 会连历史业务日的行一起回退成
      `pending`，而次日进程只按当日领取，那些行只会变成永不被领的空转行；跨午夜长轮次
      仍在飞的行也会被次日进程重置。
    - 死主接管：对心跳过期的执行体，把其分片集内的 `pending` 行改归本执行体并把分片并入
      领取范围——否则死主的行没有任何人领。
    另按 `WORKER_HEARTBEAT_SEC` 刷新本执行体心跳：一轮可能十几分钟，只在起跑/收尾写盘会
    让长轮次被执行体页判成 `stale`（异常），比"显示 idle"更糟。
    """
    shards = tuple(shards)
    last_beat = _mono()
    last_recover = _mono()
    while True:
        if schedule._window_closed(ctx.cfg, _now()):
            break
        if _mono() - last_beat >= state_io.WORKER_HEARTBEAT_SEC:
            state_io.mark_worker_beat(getattr(ctx, "slot", 0), now=_now())
            last_beat = _mono()
        if _mono() - last_recover >= RECOVER_SEC:
            queue_store.reap_expired(now=_stamp_ms(_now()), day=ctx.day)
            shards = _widen_with_dead_peers(ctx, shards)
            last_recover = _mono()
        rows = queue_store.claim_batch(
            ctx.runtime_id, ctx.day, shards, now=_stamp_ms(_now()),
            limit=queue_store.CLAIM_BATCH_LIMIT, lease_sec=queue_store.LEASE_SECONDS)
        for r in rows:
            queue.put_nowait((PRIORITY_ORDER_BASE, r["run_at"], r["phone"],
                              r["attempts"], r["epoch"]))
        if (not rows and ctx.inflight == 0 and ctx.busy == 0
                and queue.empty() and queue_store.pending_count(ctx.day, shards) == 0):
            break
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

    - **用户自暂停**：计划里本就没有它的行（`planner._phones` 剔除自暂停账号），不登记
      就会在汇总里变成"未执行"（按失败计），与 v2 的"跳过"口径不符；
    - **不在本执行体分片集**：多执行体分工下由别的执行体负责，登记进 `delegated` 让
      汇总不把别人的活报成自己的失败（与 `round.run_queue_retry` 同口径）。
    """
    for acc in accounts:
        phone = acc.phone
        if getattr(acc, "user_paused", False):
            ctx.results[phone] = (False, "用户已取消签到", True, STATUS_USER_CANCELLED)
            state_io._write_sign_state(phone, STATUS_USER_CANCELLED, "用户已取消签到")
            _emit_event(ctx, phone, STATUS_USER_CANCELLED, "用户已取消签到")
            continue
        if (ctx.delegated is not None
                and hrw.vshard_of(phone, ctx.day, ctx.v) not in ctx.shards):
            ctx.delegated.add(phone)


def _mark_window_skips(ctx, accounts):
    """窗口已关时，把本执行体分片集内没轮到结果的账号落 `skipped_window`。

    只在窗口已关时动手（与 v2 的 `round._mark_window_skip` 同时机）：窗口还开着却没有
    结果，只可能是计划行缺失或库异常，那属于要暴露的异常，不该被"窗口外"盖掉。已有当日
    结论的账号按原结论透传（不覆盖真实失败），写入走 `only_if_absent` 的 CAS 兜住并发。
    """
    if not schedule._window_closed(ctx.cfg, _now()):
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


# ---------------------------------------------------------------------------
# 同步入口
# ---------------------------------------------------------------------------
def run_executor_v3(accounts, *, day=None, dry_run=False, delegated=None,
                    cred_state=None, notify_url="", event_sink=None,
                    cfg=None, rng=None):
    """同步入口（内部 `asyncio.run`）：跑一轮 v3，返回
    `{phone: (success, message, skip, status)}`——**与 `round.run_queue_retry` 同形**，故调用方
    的收尾与退出码汇总零改动。

    **当前默认不生效**：`YIBAN_SCHEDULER_V3` 缺省 0，未开闸时轮次走 `round.run_queue_retry`，
    只有 `scheduler_v3_enabled` 被 `runner.main` 与 `schedule.capacity_of` 读取。

    `dry_run=True` 只转调 `shadow_stats`（零落库 / 零领取 / 零请求）并返回空结果。
    `cred_state` **就地改传入的那个 dict**：调用方持有同一引用并在收尾保存，重新绑定会让熔断
    计数写不回（见 `round.run_queue_retry` 的持有引用注释）。`notify_url` 只为与 v2 的调用签名
    对齐：逐次失败不在这里通知（汇总邮件是 runner 收尾的职责，v2 亦只在最终放弃时通知一次）。
    计划不可用时返回空结果并留 error：v3 的队列就是 `sign_tasks`，没有可用的库就没有队列。

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
        v = _ensure_plan(accounts, day, cfg)
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
    state_io.mark_worker_started(slot, now=_now())
    queue_store.reap_expired(now=_stamp_ms(_now()), day=day)
    try:
        ctx = _Ctx(
            accounts={a.phone: a for a in accounts},
            day=day, cfg=cfg, v=v,
            shards=hrw.shards_of(executor_id, cfg["executors"], day, v),
            executor_id=executor_id, runtime_id=runtime_id, results={},
            cred_state=cred_state,
            delegated=delegated, notify_url=notify_url, event_sink=event_sink,
            rng=rng or random.Random(), slot=slot)
        # 接管须在预扫之前：预扫按 `ctx.shards` 判"不在本执行体分片集"的账号，接管把死主
        # 分片并入后这些账号已归本执行体，不该再被登记成"别人负责的活"。
        ctx.shards = _widen_with_dead_peers(ctx, ctx.shards)
        _prescan(ctx, accounts)
        ctx.limiter.restore_from_store(ctx.egress, now=_mono())
        if ctx.global_limiter.invalid:
            logger.warning("%s 非法，全局速率上界按不限处理（不阻断签到）",
                           ENV_GLOBAL_RATE)
        logger.info("v3 执行体：%d 条通道 / %d 个分片 / 出口 %s",
                    ctx.m, len(ctx.shards), ctx.egress)
        asyncio.run(_run_async(ctx))
    except Exception as e:
        # 异常文本经 _sanitize_text 防注入、_mask_phones_in_text 抹手机号后再落日志
        logger.error("v3 执行体未预期异常，本轮按无结果收尾（不外逃）: %s",
                     _mask_phones_in_text(_sanitize_text(str(e))))
        return {}
    try:
        _mark_window_skips(ctx, accounts)
    except Exception as e:
        # 只丢收尾档②：到这里结果集已经成型，照常返回 ctx.results，只把这一段记 error 并继续。
        # 并进"丢结果"那一档会把一轮基本成功的活汇总成"全部未执行"（退出码 1 + 失败邮件），与
        # 事实相反；没被收尾的账号由 runner 按"未执行"计入失败，可见性不受损
        logger.error("v3 窗口收尾失败，已完成的账号结果照常返回: %s",
                     _mask_phones_in_text(_sanitize_text(str(e))))
    # 收尾写心跳**只在正常返回路径**（不是 finally）：四态从 running 落到 finished。
    # 异常/中断路径不写——`KeyboardInterrupt` / `SystemExit` 是 BaseException、会直接
    # 外逃，写进去就把"被信号杀掉"记成"正常跑完"；留"有开始、无收尾"让心跳过期后判
    # `stale`（疑似被强杀，要用户注意），与监督进程不写收尾的口径一致。
    state_io.mark_worker_finished(slot, now=_now())
    return ctx.results
