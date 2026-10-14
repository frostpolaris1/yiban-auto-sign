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
- Claim-at-Dispatch（领取即执行）：调用方只在通道空闲时领**一行**、领到即执行。这是
  本模块的领取形态（工单 `l1rx`；`docs/refactor/130` §3.2）。批量预取与进程内
  `asyncio.PriorityQueue` 已退场——"先领一大批再慢慢等"会让行的持有时间无上界，正是
  任务级租约族复杂度的根源。任务排序由台账 `ORDER BY priority, run_at` 承担。
- Elbow（肘界谓词）：`yiban/core/elbow.py`。领取谓词可带肘界 `run_at <= now - elbow`，
  兜底常驻据此只领"已过期超过肘界"的行——"谁该做"是**行的函数**，不再是进程身份的函数；
  它取代了原先的让位探针（10-07 型启动竞态从机制上消失）。
- Fencing Token（栅栏令牌）：Martin Kleppmann。领取自增 `epoch`、结算按 epoch 比对，同项
  溯源见 `yiban/store/queue_store.py`。**注意**：任务级租约（`lease_until` / 宽限 /
  `reap_expired` / 豁免名册 / 死主接管）已删除；崩溃自愈改由**监督进程按进程句柄**做
  （`queue_store.reap_abandoned`），epoch 只服务"回退重排后原持有者不得再写"。
- Exponential Backoff with Jitter（指数退避加抖动）：`next_retry_at_v3` 用**Decorrelated
  Jitter** 变体，出自 Marc Brooker（AWS）抖动一文的三变体比较（Full / Equal /
  Decorrelated）。无抖动的指数退避会让同一批失败任务每轮同步重试（惊群）；Equal Jitter
  保留固定下界，次生尖峰最重；本模块取 Decorrelated 是因其上界随上次抖动放大，在风控
  场景下比 Full Jitter 更保守。

**功能**
调度 v3 的执行体核心：单进程 asyncio **M 条通道**各自在空闲时从 `sign_tasks` 领取**一行**
自己分片集内已到点的任务、领到即执行（claim-at-dispatch；`attempts.attempt_signin` 整体经
`asyncio.to_thread` 提交，线程池上限 = M），出口令牌桶限速、每账号 gap 门、装订式抖动退避、
终态批量收尾。

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
调用谁：`queue_store`（领取 / 收尾 / 重排 / 待办计数 / 桶状态落库）、`token_bucket`
（出口桶 + 全局上界 + gap 门）、`planner`（计划生成与落库、`has_plan`）、`hrw`（分片集）、
`state_io`（按日状态、执行体文件心跳——只服务 UI）、`attempts`（单次尝试与失败分级）、
`alerts`（最终放弃时的管理员告警与用户失败邮件）、`clock_meta`（当日虚分片数落库）、
`schedule`（配置、窗口关闭判定、通道数）——均在 `yiban.engine` / `yiban.store` / `yiban.core` 下。
谁调用：`runner.main`（`run_executor_v3` 是唯一执行入口，非 `--only` 时 `requeue_final`
取补签轮身份，`--only` 时 `claim_all` + `reclaim` + `requeue_final`）；兜底常驻
（`workers.run_fallback_worker`）用 `claim_all` + `requeue_during_run` + `elbow_sec=FALLBACK_ELBOW_SEC`。
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
from yiban.core import elbow as elbow_pred
from yiban.engine import alerts, attempts, hrw, planner, schedule, state_io, token_bucket
from yiban.masking import mask_phone as _mask_phone
from yiban.masking import mask_phones_in_text as _mask_phones_in_text
from yiban.masking import sanitize_text as _sanitize_text
from yiban.store import clock_meta, queue_store, run_events

logger = logging.getLogger("yiban")

#: 全局聚合速率上界 Λ 的键（attempt/s，缺省空 = 不限）。本模块是它唯一的读取点。
ENV_GLOBAL_RATE = "YIBAN_GLOBAL_RATE"
#: 退避落点的硬上限（秒）：安全类参数，恒为常量、不随窗口或速率自适应
RETRY_CAP_SEC = 600
#: 空闲轮询间隔（秒）：通道没领到行时的等一拍，也是收干判定的节拍（claim-at-dispatch 下
#: 不再有"补货批量领取"，本常量只剩"没活时等多久再看"这一个含义）。
REFILL_SEC = 5
#: 兜底常驻的**肘界**（秒）：只领 `run_at` 已过期超过该时长的行（`yiban/core/elbow.py`）。
#: 兜底因此不与计划轮抢新到点的活——"谁该做"是**行的函数**而非进程身份的函数，10-07 型
#: 启动竞态（兜底抢跑吸收整轮工作量）从机制上消失（工单 `l1rx`；`docs/refactor/130` §3.3）。
#:
#: **取值＝估算，不是实测**（工单 `l1rx` 必修 R10）：设计口径是"≈2× 单次尝试时长 P99"，
#: 而 P99 **未测**（`docs/refactor/133` §六同款登记），60 秒是拍的。方向安全：偏大只让兜底
#: 晚一点捡漏，偏小才可能碰到计划轮要签的行。**残余（部署后复算重定）**：10-17 部署后
#: `run_events` 在真实轮次上全量可用，用 `scripts/elbow_shadow.py` 对每天的兜底领取算
#: "提前量"分布重定此值。`elbow` 谓词本体只管算术，取值是应用层的职责。
FALLBACK_ELBOW_SEC = 60
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
#: **落在哪一层**：落在派发协调循环 `_dispatch_keeper`。只有循环知道自己读了几轮。
#: `queue_store` 是无状态访问层，把计数放进去等于让存储层替调用方做降级决策。
#: **触发时人看见什么**：一条 ERROR（按天日志 / 后台"日志"页）与一条管理员告警
#: （轮末汇总邮件；推送已配置时即时推送），见 `_alert_queue_unreadable`。
QUEUE_UNREADABLE_MAX_ROUNDS = 60
#: 会话内回炉的节流间隔（秒）：只有兜底腿（`requeue_during_run`）用它——把本轮刚弃权的
#: `retry:` 档行就地翻回 `pending` 的周期。**与租约无关**（租约族已删，见工单 `l1rx`）。
RECOVER_SEC = 60
#: 出口桶状态落库间隔（秒）
EGRESS_PERSIST_SEC = 10
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


def _make_limiter(channels, shares=1):
    """出口限速器（配置面收口在 `token_bucket.limiter_from_env`）。

    `shares` = 同出口执行体数 n（`egress.outlet_executor_count`）：本进程子桶速率 = 出口级
    λ ÷ n，n 个进程运行期合计 ≤ λ（工单 2cwd 的出口预算均分）。
    """
    return token_bucket.limiter_from_env(channels=channels, shares=shares)


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
            return datetime.datetime.strptime(str(run_at), fmt)  # noqa: DTZ007 解析业务本地 naive 的 run_at 串
        except (TypeError, ValueError):
            continue
    logger.warning("run_at 无法解析（按立即执行处理）: %r", run_at)
    return _now()


def _stamp_ms(dt):
    """毫秒精度时间串（与 `queue_store._shift_stamp`、`planner._stamp_at` 同格式）。

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

    `inflight` 是"正跑在线程池里的尝试数"，`claiming` 是"通道手上还没处理完的条目数"
    （claim-at-dispatch：领取后立即执行，故它覆盖"已领取、正在 `_attempt`"的整段）。收干
    判据看 `claiming`：正被通道执行的行在库里已是 `claimed`、不在 `pending_count` 里，
    不数进来会在通道还在跑时提前收干。
    """

    def __init__(self, accounts, day, cfg, v, shards, executor_id, results,
                 cred_state, delegated, notify_url, event_sink, rng, slot=0,
                 runtime_id=None, requeue_during_run=False, reclaim=False,
                 allowed_phones=None, alive_role=None, elbow_sec=0.0, unreached=None,
                 round_no=None, log_tag=None):
        self.accounts = accounts
        self.day = day
        self.cfg = cfg
        self.v = v
        self.shards = shards
        # 稳定槽位名：HRW 分片成员判据（`hrw.shards_of` 要求它是 `cfg["executors"]`
        # 的成员）与界面"执行体"对象都用它。**不用于写库**，也不再是限速桶键。
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
        # 桶键 = **出口标识**（`egress.egress_identity`：空出口归一为 `direct`，否则去
        # userinfo 的 scheme://host[:port]）。限速按出口计数：这是**持久键与预算分母**，
        # 同一出口的多个执行体共用一个持久键、并均分该出口的速率预算（每进程子桶速率 =
        # 出口级 λ ÷ 同出口执行体数 n，n 个进程运行期合计 ≤ λ；见 `token_bucket` 模块头）。
        # 用出口标识而不是执行体身份：改槽位号/换主机名不再丢掉该出口的自适应速率。
        self.egress = egress.egress_identity(
            egress.resolve(_worker_role(executor_id), _worker_slot(executor_id)))
        self.m = schedule.channel_count(cfg["bucket_rate"], cfg["avg_attempt_sec"])
        self.results = results
        self.cred_state = cred_state
        self.delegated = delegated
        self.notify_url = notify_url
        self.event_sink = event_sink
        self.rng = rng
        self.limiter = _make_limiter(self.m, egress.outlet_executor_count(self.egress))
        self.global_limiter = _make_global_limiter()
        self.gap_gate = _make_gap_gate()
        self.inflight = 0
        self.claiming = 0
        #: 协调循环置位的停止信号（claim-at-dispatch 下没有进程内队列与哨兵）：一旦"确实
        #: 收干"或窗口关闭，`_dispatch_keeper` 置它，各通道在下一拍退出。由 `_run_async`
        #: 在事件循环里创建（不能在 `__init__` 里造 asyncio 原语）。
        self.stop = None
        self.last_delay = {}
        # 轮首回炉（`run_executor_v3` 里那一次）读不通的记号：交给派发协调循环计入同一条
        # 有界放弃闸门（`QUEUE_UNREADABLE_MAX_ROUNDS`）。轮首读不通不等于"没有要回炉的行"，
        # 不许就地丢掉（ba-p01-01）。
        self.queue_unreadable = False
        #: 通道最近一次领取"读不通"的记号（`_lane` 写、`_dispatch_keeper` 读后清空）。
        #: 领取已从协调循环移到各通道（claim-at-dispatch），但**有界放弃闸门**仍只该由
        #: 协调循环一处裁决：通道只如实登记"这一拍我读到读不通"，闸门计数与告警在
        #: `_dispatch_keeper`（一处计数、一条告警、一次停止），免得 M 条通道各报一份。
        self.claim_unreadable = False
        # 会话内恢复周期是否顺带回炉默认档（常驻/长会话的兜底腿用它"当日接手"，
        # 一次性定时轮靠轮首回炉即可，不开这个口子以免在同一轮里重开保守档之外的循环）
        self.requeue_during_run = requeue_during_run
        # 本角色的**肘界**（秒，见 `yiban/core/elbow.py`）：`_claim_one` 每次领取都带上它。
        # 定时执行体/手动/补签 = 0（到期即可领）；兜底常驻 = `FALLBACK_ELBOW_SEC`。
        self.elbow_sec = float(elbow_sec or 0.0)
        # 轮末"本执行体未执行"判因的出口（调用方传入的 dict，本函数往里填）：
        # 判因同时喂汇总计数与事件留痕，见 `_mark_unreached`。
        self.unreached = unreached
        # 轮次序号：`None` = **一次性轮**（定时全量与手动 `--only`），整数 = **常驻多轮**
        # 的第 N 轮（兜底常驻循环逐轮传）。除日志归因外，它还是收尾"逐账号重扫幂等"的
        # 判别面：常驻轮重扫到当日已有结论的用户自暂停账号时不再登记（见 `_prescan`），
        # 否则 `results` 永远非空 —— 兜底紧循环不停、且每轮重落事件与进度行。
        self.round_no = round_no
        # 手动 `--only` 的显式重签：豁免窗口关闭判定（用户主动触发应当放行，与旧领取池
        # 的手动链路一致），其余路径不受影响
        self.reclaim = reclaim
        # 本轮**允许领取/回炉的账号允许集**（`None` = 按分片集不限）：手动 `--only` 只
        # 处理本轮传进来的账号，宽分片集不得把当日别人的行领走或把别人的 `failed` 回炉
        # ——那些行会走 `_attempt` 的 `acc is None` 分支被误当"了结"（静默漏签）。
        self.allowed_phones = (None if allowed_phones is None
                               else frozenset(allowed_phones))
        # **本执行体真的执行过**的账号（过了 gap 闸门、发过请求）。轮末判因要靠它把
        # "跑过但没收尾（重试回炉）"与"没轮到"分开——前者不是"未执行"（见
        # `_mark_unreached`）。只在事件循环线程里改写，与本类其余计数同一纪律。
        self.attempted = set()
        # 本轮认定的**用户自暂停**账号（`_prescan` 写；`_mark_unreached` 读）。
        # 它们是"有意不动"、没有计划行的账号，既不是"未执行"（无从领取），也不是
        # "已由他人负责"。登记（状态/事件/进度行）当日**只做一次**：兜底常驻每 ~5s
        # 重扫，重复登记会按轮灌 `user_cancelled` 事件与 `pause` 进度行（2026-10-13
        # 生产单日 752+752），并把"有活"喂给紧循环（见 `_prescan`）。
        self.user_paused = set()


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
    成员表只认 `yiban.status.RETRYABLE_GIVE_UP_STATUSES`（v2 的 `claims.give_up` 与
    `round._settle_claims` 选档用的是同一 frozenset）。各写一份会漂移成
    "记成 retry、判成 final"——回炉口对默认档失明，当日失败又没人接手。
    """
    return (yiban_status.RESULT_RETRY_PREFIX
            if status in yiban_status.RETRYABLE_GIVE_UP_STATUSES
            else yiban_status.RESULT_FINAL_PREFIX)


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

    `item` 是 `_claim_one` 领到的那一行（`{"phone","run_at","attempts","epoch"}`）——
    claim-at-dispatch 下它就是"此刻轮到本通道执行的那一行"，没有中间态、没有本地队列。
    """
    phone = item["phone"]
    attempts_n = item["attempts"]
    epoch = item["epoch"]
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


async def _lane(ctx, lane_id):
    """通道主体（claim-at-dispatch）：空闲即领一行、领到即执行。

    与批量预取版的差别：通道不再从进程内 `PriorityQueue` 取"早已领回的条目"，而是**自己
    在空闲时**向台账领一行。故一行的持有时间 = 一次尝试的时间（有界），不存在"领回来在
    本地等通道/等令牌/等间隔/等计划时刻"的无上界等待——那正是租约族复杂度的根源
    （`docs/refactor/130` §2.3 S1；工单 `l1rx`）。领取、执行、收尾串成一条：
    `claim → _attempt`，没有中间态。

    读不通（`None` 哨兵）时**不判收干**，原地等一拍让协调循环的有界闸门计数（ba-p01-01）。
    """
    while not ctx.stop.is_set():
        # 窗口门在**领取之前**判（与模型 130 §3.2 的 `while window_open()` 同形）：窗口一关
        # 就不再领，绝不会"轮内窗口关闭后还发一次真实登录"。手动 `--only`（reclaim）豁免
        # ——用户主动触发不看窗口（与旧领取池的手动链路同语义）。
        if not getattr(ctx, "reclaim", False) and schedule._window_closed(ctx.cfg, _now()):
            break
        rows = _claim_one(ctx)
        if rows is None:
            # 读不通：不判收干（ba-p01-01）。登记本拍"读到读不通"，交协调循环的有界闸门
            # 计数（一处裁决），通道原地等一拍。
            ctx.claim_unreadable = True
            await _sleep(REFILL_SEC)
            continue
        if not rows:
            await _sleep(REFILL_SEC)
            continue
        ctx.claiming += 1
        try:
            # 三闸限速在**领取之后、发出请求之前**：出口桶 → 全局 Λ → 每账号 gap。
            # 这道调用必须在领取与尝试之间——漏了它等于零限速（本批的一次实测差点漏掉，
            # 由 `LimiterWiringTest` 的调用序断言当场抓出）。
            await _throttle(ctx, rows[0]["phone"])
            await _attempt(ctx, rows[0])
        finally:
            ctx.claiming -= 1


def _claim_one(ctx):
    """领取**一行**到期任务（claim-at-dispatch）→ `[dict]` / `[]`（无到期行）/ `None`（读不通）。

    执行体唯一的领取点：通道空闲才调它、领到即执行（`docs/refactor/130` §3.2；工单
    `l1rx`）。`elbow_sec` 由角色决定（定时/手动/补签 0；兜底 `FALLBACK_ELBOW_SEC`），
    谓词**下推**到 `queue_store.claim_batch`（SQL 里 `run_at <= now - elbow`）。

    领取后**再用 `elbow.may_claim` 复核**一遍（谓词语义的唯一事实源在 `yiban/core/elbow.py`，
    SQL 只是它的下推实现）：不一致说明下推实现与谓词漂移，按"不可领"处置——把行退回
    `pending` 并响亮告警，**绝不执行一条谓词说不该领的行**。这条复核把"两个实现必须给同一
    答案"钉成生产路径上的不变式，而不是只写在测试里。

    下推实现若被改成 `limit > 1`，多余的行一律退回：claim-at-dispatch 每次只执行一行。
    """
    rows = queue_store.claim_batch(
        ctx.runtime_id, ctx.day, ctx.shards, now=_stamp_ms(_now()),
        elbow_sec=ctx.elbow_sec, phones=ctx.allowed_phones)
    if rows is None or not rows:
        return rows
    row = rows[0]
    for extra in rows[1:]:
        queue_store.requeue_task(extra["phone"], ctx.day, extra["run_at"],
                                 result="", epoch=extra["epoch"])
    if not _elbow_allows(row["run_at"], ctx.elbow_sec, _now()):
        logger.error("%s 领取谓词漂移：有一行的 run_at 不满足 elbow（run_at=%s，肘界 %ss），"
                     "已退回待领、不执行", ctx.log_tag, row["run_at"], ctx.elbow_sec)
        queue_store.requeue_task(row["phone"], ctx.day, row["run_at"], result="",
                                 epoch=row["epoch"])
        return []
    _report(ctx, run_events.NODE_CLAIM, row["phone"])
    return [row]


def _elbow_allows(run_at, elbow_sec, now_dt):
    """`yiban.core.elbow.may_claim` 的**适配器**：台账毫秒时间串 → epoch 秒。

    `elbow` 是领域核心层纯函数，只认 epoch 秒（它不解析时间串，见其模块说明）；把
    `run_at` 解析成 epoch 秒是适配器的职责。`run_at` 与 `now` 都是业务本地 naive 时间，
    `timestamp()` 用同一本地时区换算，**差值**因此正确（跨时区部署不在本仓支持面内，
    与 `_shift_stamp` 同一口径）。
    """
    return elbow_pred.may_claim(
        elbow_pred.ClaimRow(run_at=_parse_run_at(run_at).timestamp()),
        elbow_pred.ClaimRole(elbow_sec=float(elbow_sec or 0.0)),
        now_dt.timestamp())


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


async def _dispatch_keeper(ctx):
    """派发协调循环：判窗口、刷心跳、节流会话内回炉、判**收干**。

    claim-at-dispatch 下，领取由各通道自己在空闲时做（`_lane` / `_claim_one`），本循环
    不再领取任何行——它只回答"这一轮完了没有"以及维护三件与领取无关的周期动作：

    - **收干判定**：台账里本执行体分片集内**没有可领行**（`pending_count == 0`）且**没有
      通道在飞**（`ctx.claiming == 0`）⇒ 置 `ctx.stop`，全部通道随即退出。为什么必须带
      `ctx.claiming`：正被某通道执行的行在库里已是 `claimed`、不在 `pending_count` 里，
      不数进来会在通道还在跑时提前收干，把刚重排回 `pending` 的重试任务留在库里没人领。
      为什么必须带 `pending_count`：`claim_batch` 在"下次到点还没到"时本来就返回空，只看
      空返回会把还有待办的一轮误判成收干（`docs/refactor/130` §3.2 的收干判据）。
    - **心跳刷新**：一轮可能十几分钟，只在起跑/收尾写盘会让长轮次被执行体页判成 `stale`。
      心跳**只服务 UI**（工单 `l1rx`：退出一切正确性判定），故此处写盘是展示动作。
    - **会话内回炉**（只有 `requeue_during_run` 的兜底腿）：每 `RECOVER_SEC` 把本轮刚弃权的
      `retry:` 档行翻回 `pending`，不等下一场会话。只回炉默认档——`final:`/无前缀保守档的
      第二次机会只留给有界的显式路径（补签轮）。**与租约无关**：这里没有回收，只有翻档。

    **读不通不判收干（ba-p01-01）**：`pending_count` 的 `0` 与 `None` 是两件事。0 才允许
    收干；`None`（读不通）计入 `QUEUE_UNREADABLE_MAX_ROUNDS` 那条有界闸门，连续满上界后
    告警并停止本轮。不空转，也不抛异常打断整轮。
    """
    last_beat = _mono()
    last_recover = _mono()
    # 有界放弃闸门：连续读不通的轮数。轮首那次回炉也是一次读库失败，故按
    # `ctx.queue_unreadable` 把起点取成 1，与循环内的读共用同一条上界。
    unreadable_rounds = 1 if getattr(ctx, "queue_unreadable", False) else 0
    while not ctx.stop.is_set():
        # 本拍是否读到"库读不通"：通道领取面、待办面、会话内回炉三处任一。领取面的记号
        # 读后即清（通道只登记"本拍读到过"，计数与告警由本循环一处裁决）。
        round_unreadable = bool(ctx.claim_unreadable)
        ctx.claim_unreadable = False
        if not getattr(ctx, "reclaim", False) and schedule._window_closed(ctx.cfg, _now()):
            break
        if _mono() - last_beat >= state_io.WORKER_HEARTBEAT_SEC:
            state_io.mark_worker_beat(getattr(ctx, "slot", 0), now=_now(),
                                      role=getattr(ctx, "alive_role", None))
            last_beat = _mono()
        if (not getattr(ctx, "reclaim", False)
                and getattr(ctx, "requeue_during_run", False)
                and _mono() - last_recover >= RECOVER_SEC):
            # 会话内回炉（默认档）：本轮刚弃权的 `retry:` 档行立刻翻回 `pending`，下一拍
            # 就能被自己的通道重新领到——不等下一场会话。回 `None` = 读不通：当日 `failed`
            # 行仍留在原态，等于"没回炉"，计入同一条有界闸门（ba-p01-01）。
            flipped = queue_store.requeue_failed(ctx.day, ctx.shards, include_final=False,
                                                 phones=ctx.allowed_phones)
            if flipped is None:
                round_unreadable = True
            last_recover = _mono()
        pending = queue_store.pending_count(ctx.day, ctx.shards, phones=ctx.allowed_phones)
        if pending is None:
            round_unreadable = True
        elif pending == 0 and ctx.claiming == 0:
            break
        if round_unreadable:
            unreadable_rounds += 1
            if unreadable_rounds >= QUEUE_UNREADABLE_MAX_ROUNDS:
                _alert_queue_unreadable(ctx, unreadable_rounds)
                break
        else:
            unreadable_rounds = 0
        await _sleep(REFILL_SEC)
    ctx.stop.set()


async def _persist_loop(ctx):
    """每 `EGRESS_PERSIST_SEC` 秒把出口桶状态落库：崩溃重启后不"重启即全速"。

    写失败由 `EgressLimiter.persist` 内部告警，不阻断签到（桶状态是记忆不是业务事实）。
    """
    while True:
        await _sleep(EGRESS_PERSIST_SEC)
        ctx.limiter.persist(ctx.egress)


async def _run_async(ctx):
    """起 M 条通道 + 一条派发协调循环 + 一条桶状态落库，跑到协调循环置 `ctx.stop` 为止。

    claim-at-dispatch：**没有进程内队列、没有补货任务、没有哨兵**。每条通道自己在空闲时
    领一行、立即执行；协调循环只在"确实收干"（或窗口关闭 / 连续读不通到上界）时置
    `ctx.stop`，通道在下一拍退出。任务名用 `%` 拼而不是 f-string：`"<前缀>-{…}"` 的形状
    会被"按日文件登记"元测试认成新的状态文件前缀；名字本身用于在 asyncio 的 traceback 里
    区分通道。
    """
    asyncio.get_running_loop().set_default_executor(_new_thread_pool(ctx.m))
    ctx.stop = asyncio.Event()
    lanes = [asyncio.create_task(_lane(ctx, i), name="v3-lane-%d" % i)
             for i in range(ctx.m)]
    keeper = asyncio.create_task(_dispatch_keeper(ctx), name="v3-dispatch")
    persist = asyncio.create_task(_persist_loop(ctx), name="v3-egress-persist")
    try:
        await asyncio.gather(keeper, *lanes)
    finally:
        # 协调循环先退出（窗口关闭 / 收干）时，通道可能还在等一拍：置 stop 让它们尽快退出。
        ctx.stop.set()
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

    登记当日**只做一次**（判据＝当日状态文件里已是 `user_cancelled`）：兜底常驻每 ~5s
    重扫、窗口内可上百轮，重复登记会按轮灌 `user_cancelled` 事件与 `pause` 进度行
    （2026-10-13 生产单日 752+752，工单 uuqb），并把"本轮有活"喂给紧循环（`own` 恒 > 0
    ⇒ 扫描间隔从 60s 掉到 5s）。两个调用形态的差异必须保留：

    - **一次性轮**（`round_no` 为 `None`：定时全量、手动 `--only`）即便状态文件已有该
      结论**仍登记进 `results`**——汇总按 `results` 计"跳过"，不登记就会被算成"未执行"
      （真失败 + 退出码 1）。写状态/事件/进度行的副作用则按"当日只做一次"跳过，避免
      同一账号当日重复留痕。
    - **常驻多轮**（`round_no` 有值：兜底）在已有结论时**不进 `results`**：这正是"本轮
      无活"的如实口径，紧循环据此回到配置的扫描间隔；该账号的当日结论本就在状态文件里，
      汇总侧由 `ctx.user_paused` 排除它，不会回落成"未执行"。
    """
    recorded = None
    for acc in accounts:
        phone = acc.phone
        if (ctx.delegated is not None
                and hrw.vshard_of(phone, ctx.day, ctx.v) not in ctx.shards):
            ctx.delegated.add(phone)
            continue
        if getattr(acc, "user_paused", False):
            ctx.user_paused.add(phone)
            if recorded is None:
                # 当日状态快照按需取一次：没暂停账号的部署（常态）不必每轮多读一次盘。
                recorded = state_io._daily_statuses()
            already = str(recorded.get(phone, "")).strip() == STATUS_USER_CANCELLED
            if already and ctx.round_no is not None:
                # 常驻重复轮：当日结论已在，本轮无活可做（见 docstring）。
                continue
            if not already:
                state_io._write_sign_state(phone, STATUS_USER_CANCELLED, "用户已取消签到")
                _emit_event(ctx, phone, STATUS_USER_CANCELLED, "用户已取消签到")
                # 用户自暂停是账号级「暂停」节点：它没有计划行、不经过队列，故没有
                # claim/start 行——进度流里只出现这一行 pause 是正确形态。
                _report(ctx, run_events.NODE_PAUSE, phone, "用户已取消签到")
            ctx.results[phone] = (False, "用户已取消签到", True, STATUS_USER_CANCELLED)


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

#: 上一次落下的「无人接手」批量留痕**组成**（`_mark_unreached` 的去重依据）：
#: `(执行体, 分因明细)`。去重理由与 `_UNREACHED_PEER_LAST` 相同（组成不变即整段跳过、
#: 身份必须进键、归零要能复活），但**形状不同**：peer 那枚是三元的 `(执行体, 账号数,
#: 分因明细)`，本枚是二元的——无人接手档的账号数就是明细之和，不必另存一列。
#: 真异常健康时该档为 0；但**队列读不通**时本执行体全部账号都落进这一档——逐账号落库
#: 就是 O(账号数) 的事件与日志（2026-10-13 生产的 `pending` 洪泛形状），故同样收成一条
#: 批量并按组成去重。真异常的**规模与分因**由批量行与 `unreached` 出参承担；逐账号事实源
#: 是台账 `sign_tasks` 的行状态——**仅当台账读得通且该账号当日有行**：「判因不可得」
#: （队列读不通）读不到台账，「队列中没有当日行」本就没有行，这两种分因只有批量明细可查。
_UNREACHED_UNCLAIMED_LAST = None


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

    `ctx.user_paused`（用户自暂停）不在判因范围内：计划里本就没有它的行，它既不是
    未执行也不是别人负责（应由 `_prescan` 登记）。

    为什么必须留痕：2026-10-07 生产里任务被别人领走时执行层**零事件、零日志**，
    `sign_tasks.result` 又被实际领取者覆盖成"签到成功"，数据侧完全看不出"没轮到"，
    运维只能靠人工比对日志时间线。

    留痕一律收成**一条批量**（日志一行 + 事件一行，带账号数与分因明细），按**组成**去重
    （见上面两个 `_UNREACHED_*_LAST`）——兜底常驻循环每轮扫描，组成不变就整段跳过：

    - 逐账号落库会按账号数线性灌噪声——2026-10-10 生产该扫描因此落 1830 行 `pending`
      事件与 1830 行日志（工单 81xt）；2026-10-13 生产更在 `pending` 档上单日灌入
      49,148 行。批量事件是**摘要行**（`phone=""`，没有单一账号），读者面按摘要行口径
      排除其账号统计（见 `store.events` 的 sign_event_stats / sign_event_accounts_summary），
      故它**永不成为某账号当日最后一条事件**（看板终态读的是每账号最后一条）。
    - 真异常（`UNCLAIMED`）的可查性不靠逐条事件行：**规模与分因**在批量行里（账号数 +
      `{分因文本: 账号数}` 明细），**逐账号判因**经 `unreached` 出参喂运行汇总的独立桶。
      逐账号事实源是台账 `sign_tasks` 的行状态——**仅当台账读得通且该账号当日有行**：
      「判因不可得」（队列读不通）时台账读不到，「队列中没有当日行」时台账本就没有行，
      这两种分因只有批量明细一项可查，往下追账号得另开手段。

    两档与调用方的汇总计数**同一份判因**——两处口径不会互相漂移。

    `ctx.unreached` 为 `None`（调用方不要判因，例如只关心自己退出码的子执行体）时本函数
    只做事件留痕，不填 out-param。
    """
    global _UNREACHED_PEER_LAST, _UNREACHED_UNCLAIMED_LAST
    attempted = getattr(ctx, "attempted", ())
    paused = getattr(ctx, "user_paused", ())
    phones = [a.phone for a in accounts
              if a.phone not in ctx.results
              and a.phone not in (ctx.delegated or ())
              and a.phone not in paused]
    if not phones:
        # 没有要判因的账号：两个去重键都清回 `None`（同组成 N→0→N 的第三次留痕不得被吞）。
        _UNREACHED_PEER_LAST = None
        _UNREACHED_UNCLAIMED_LAST = None
        return
    pending_judge = [p for p in phones if p not in attempted]
    rows = queue_store.row_owners(ctx.day, pending_judge) if pending_judge else {}
    mine = {ctx.executor_id, ctx.runtime_id}
    if rows is None:
        # 判因不可得：**一条** warning（不是逐账号一条——大站一轮有上百个账号，逐号
        # 告警会把日志淹掉）；逐账号判因经 `ctx.unreached` 出参喂汇总，可查性不受损。
        logger.warning("%s 未执行判因不可得（当日任务归属读不通），%d 个账号按「无人接手」计",
                       ctx.log_tag, len(pending_judge))
    # 两档判因各收进一批，循环里**不逐账号落事件与日志**（见 docstring）。
    peer_n = 0
    #: `{role/state: 账号数}`——只用于那条 peer 批量日志的分因明细（日志不截断）。
    peer_detail = {}
    #: `{分因文本: 账号数}`——无人接手档的分因明细（同样只给批量用）。
    unclaimed_detail = {}
    for phone in phones:
        if phone in attempted:
            reason = UNREACHED_RETRY
            # 已由那次尝试落过事件与状态，不重复写
        elif rows is None:
            reason = UNREACHED_UNCLAIMED
            unclaimed_detail["判因不可得（当日任务归属读不通）"] = (
                unclaimed_detail.get("判因不可得（当日任务归属读不通）", 0) + 1)
        else:
            state, owner = rows.get(phone, ("", ""))
            reason = UNREACHED_PEER if (owner and owner not in mine) else UNREACHED_UNCLAIMED
            if reason == UNREACHED_PEER:
                role = egress.parse_owner(owner)["role"]
                peer_n += 1
                key = f"{role}/{state or '?'}"
                peer_detail[key] = peer_detail.get(key, 0) + 1
            elif state == queue_store.STATE_PENDING:
                unclaimed_detail["行仍待领（state=pending）"] = (
                    unclaimed_detail.get("行仍待领（state=pending）", 0) + 1)
            elif state:
                unclaimed_detail[f"行状态={state} 且无归属执行体"] = (
                    unclaimed_detail.get(f"行状态={state} 且无归属执行体", 0) + 1)
            else:
                unclaimed_detail["队列中没有当日行"] = (
                    unclaimed_detail.get("队列中没有当日行", 0) + 1)
        if ctx.unreached is not None:
            ctx.unreached[phone] = reason
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
    if unclaimed_detail:
        # 无人接手（真异常）的**一条批量**留痕：形状与上面那条 peer 批量一致。
        # 摘要行（`phone=""`）不进账号聚合，故不污染看板终态（见 docstring）。
        unclaimed_sig = (ctx.executor_id, tuple(sorted(unclaimed_detail.items())))
        if unclaimed_sig != _UNREACHED_UNCLAIMED_LAST:
            n_all = sum(unclaimed_detail.values())
            digest = "，".join(f"{k}×{n}" for k, n in sorted(unclaimed_detail.items()))
            logger.info("%s 未执行：%d 个账号（无人接手，%s）",
                        ctx.log_tag, n_all, digest)
            _emit_event(ctx, "", yiban_status.STATUS_PENDING,
                        f"本执行体未执行：{n_all} 个账号（无人接手，{digest}）")
            _UNREACHED_UNCLAIMED_LAST = unclaimed_sig
    else:
        _UNREACHED_UNCLAIMED_LAST = None


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
                    elbow_sec=0.0, unreached=None, round_no=None):
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
    - `requeue_during_run=True`：派发协调循环的恢复周期（每 `RECOVER_SEC`）顺带把本轮刚
      弃权的默认档翻回 `pending`，不等下一场会话。只回炉默认档，保守档不变。
    - `reclaim=True`：**手动 `--only` 专用**的有界显式路径——把到手账号当日已了结
      （`done` / `skipped`）的行翻回 `pending` 并补建计划行，使"用户主动点的那一下照做"
      的旧领取池语义（`allow_settled=True`）在单池下仍有等价形态。**领取/回炉/待办计数
      全部收窄到本轮账号的虚分片 + 账号允许集**（回收也按允许集收窄：只回收本轮账号
      自己的陈旧 `claimed`，不碰别人的行），且遇 `phone ∉ accounts` 的行绝不了结——手动轮
      不得碰别人的行（否则那些行会被误判"已了结"⇒ 静默漏签）。缺省关，定时轮与兜底都不传。

    `dry_run=True` 只转调 `shadow_stats`（零落库 / 零领取 / 零请求）并返回空结果。
    `cred_state` **就地改传入的那个 dict**：调用方持有同一引用并在收尾保存，重新绑定会让熔断
    计数写不回。`notify_url` 只为与旧调用签名对齐：逐次失败不在这里通知（汇总邮件是
    runner 收尾的职责，只在最终放弃时通知一次）。
    计划不可用时返回空结果并留 error：队列就是 `sign_tasks`，没有可用的库就没有队列。

    `elbow_sec`：**本角色的肘界**（秒，见 `yiban/core/elbow.py`）。定时执行体 / 手动 /
    补签 = 0（到期即可领）；兜底常驻 = `FALLBACK_ELBOW_SEC`，只领"已过期超过肘界"的行
    ⇒ 它不与计划轮抢新到点的活，"谁该做"是**行的函数**（工单 `l1rx`）。它**取代了**原先的
    让位探针 `yield_probe`：轮内止损不再靠"每领取前问一次锁状态"，而是靠领取谓词本身
    （`docs/refactor/130` §3.3；10-07 型启动竞态从机制上消失）。

    `unreached`：调用方传入的 **out-param dict**，本函数在轮末往里填
    `{phone: 判因}`（`UNREACHED_PEER` / `UNREACHED_UNCLAIMED`，见 `_mark_unreached`）——
    本执行体范围内、`results` 里没有的账号各一条。判因同时喂两处：调用方的汇总计数
    与 `sign_events` 留痕（同源同口径，见 `_mark_unreached`）。缺省 `None` = 调用方不要。

    `round_no`：**既是日志归因面，也是收尾的判定面**。它区分两种调用形态：
    `None` = **一次性轮**（定时全量、手动 `--only`，一个进程只跑一轮）；整数 = **常驻
    多轮**的第 N 轮（只有兜底常驻会传）。判定侧：`_prescan` 据此决定"当日已有结论的
    用户自暂停账号是否还登记进 `results`"（见 `_prescan`）——一次性轮必须登记（否则
    汇总按"未执行"计失败），常驻重复轮不得登记（登记了 `results` 恒非空，兜底紧循环
    的"有活"判据 `own = len(results)` 就恒 > 0：扫描间隔从 60s 掉到 5s，且每轮重落
    事件与进度行）。改这个参数会同时改扫描间隔，不只是日志。日志侧：本执行体的日志行
    都带 `egress.owner_tag` 渲染的前缀（角色+槽位，不含主机名），`round_no` 传进来则
    再带一个轮次序号（兜底 `[fallback r7]`；一次性轮不带序号，行上是 `[worker-3]`）——
    多执行体并发写同一个按天日志文件，排障要能答出"这个账号是哪个执行体在第几轮动的"。
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
    #   `cfg["executors"]` 的成员，否则一件活都领不到）；
    # - 运行时身份（`runtime_id = egress.runtime_owner(稳定名)`）：写进 `sign_tasks.owner`
    #   的**持有者**身份，含本进程的进程号与代次。
    # 限速桶键另走一路：`_Ctx.egress = egress_identity(该出口)`（出口标识），与这两个都不同。
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
    # 显示 idle。心跳**只服务 UI**（工单 `l1rx`：租约族已删，判活靠进程句柄，不看心跳
    # 时间戳）——此处写盘纯是展示动作，写失败只留 debug，不阻断签到。
    state_io.mark_worker_started(slot, now=_now(), role=alive_role)
    # **起跑收尸（工单 `l1rx` 必修 R1）**：按**进程句柄口径**回收"本稳定名"名下的在飞行。
    # 判据不是时间戳，是**本进程持有本稳定名的运行锁**（单执行体＝全局锁 `signin-run.lock`、
    # worker 子进程＝`signin-run.lock.w{i}`、兜底＝`signin-run.lock.fallback`）：拿到锁
    # ⇒ 同稳定名的上一个持有者必然已不在 ⇒ 它留下的 `claimed` 行是尸体。翻回 `pending`
    # 并换代，原持有者迟到的写仍被 `settle_tasks(..., epochs=…)` fence。
    #
    # 为什么必须有它（删除租约族后缺口的收口）：`claim_batch` 只领 `pending` ⇒ 停在
    # `claimed` 的行当天**永久卡死＝漏签**；手工 `--only` 那条逃生口只覆盖用户点名的账号
    # （见 `reclaim_tasks`），覆盖不了"没人点"的日常崩溃。原 `reap_expired` 覆盖了这些
    # 路径，它随租约族删除后必须由本条补上。覆盖：①单进程部署被 SIGKILL/异常；
    # ②worker 子进程未预期异常（rc=1，监督进程轮末另有一道，见 `workers`）；③兜底进程被
    # 杀后其**下一次起跑**（兜底每 ~60s 一轮，故尸体最多留一拍）。
    queue_store.reap_abandoned(executor_id, day=day)
    # 归因前缀算**一次**：ctx 建立之前的行（计划不可用）与 ctx 可能已丢的行（异常收尾）
    # 都要用它，而那时 ctx 取不到；渲染本身仍只有 `egress.owner_tag` 一处。
    log_tag = egress.owner_tag(executor_id, round_no)
    # 本轮**允许领取/回炉的账号允许集**：手动 `--only` 只处理本轮传进来的账号，宽分片集
    # 不得把当日别人的行领走或把别人的 `failed` 回炉。
    allowed_phones = frozenset(a.phone for a in accounts) if reclaim else None
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
            allowed_phones=allowed_phones, elbow_sec=elbow_sec,
            unreached=unreached, round_no=round_no, log_tag=log_tag)
        # 显式重签（手动 `--only`）：先把到手账号当日已了结的行翻回 `pending`，否则
        # `claim_batch` 只取 `pending`，用户点下的那一下会沦为"未执行"。
        if reclaim:
            queue_store.reclaim_tasks(day, [a.phone for a in accounts])
        # 轮首回炉（作用域=本轮领取集 + 本业务日 + 允许集）：默认档 failed 翻回 pending
        # 才谈得上被本轮领到；回炉逐行走 `requeue_task` 的 state+epoch 门，在飞/终态行绝不
        # 复活。放在预扫之前。
        # 回炉回 `None` = 读不通：不当作"没有要回炉的行"，把记号交给派发协调循环计入同一条
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
