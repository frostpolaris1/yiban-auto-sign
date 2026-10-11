# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""容量与排期：容量预估口径、调度 v2（统一填充框架）与签到窗口判定。

调度 v2 是"排序维度（顺序/随机）× 分布维度（均匀/正态）"的 2×2 组合：块容量顺延、
自选时间片优先、正态钟形锚点、σ 自适应封顶、超容量压缩模式。窗口（起止 + 掐头去尾 +
是否已关闭）的唯一口径在 `yiban.window`，本模块只做调用与告警，不另存一份判断。

**告警去重**：窗口配置非法 / 缓冲过大被收缩 / 窗口不可用被回退各只并入当日汇总邮件
一次（`_invalid_window_notified`、`_window_clamped_notified`、
`_window_fallback_notified`）——这些函数每天被多账号多轮调用，不去重会把同一配置错误
刷成几十条。

跨模块调用纪律见包说明：跨模块一律走模块属性访问。
"""
import logging
import math
import os
import random
from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping

from yiban import clock, config_loader, window
from yiban.engine import hrw
from yiban.infra import env_io as _env_io
from yiban.store import db

logger = logging.getLogger("yiban")

# ---------------------------------------------------------------------------
# 调度 v2 配置默认值（设计文档：docs/design/plan-scheduler-v2.md）
# ---------------------------------------------------------------------------
_DEFAULT_SIGN_START = (6, 30)
_DEFAULT_SIGN_END = (7, 50)
_DEFAULT_BLOCK_CAP = 15         # 块容量（每块最多人数，满则向后顺延）
_DEFAULT_MU_MIN_PCT = 40        # 正态高峰中心范围（有效窗口相对位置 %）
_DEFAULT_MU_MAX_PCT = 60
_DEFAULT_SIGMA_MIN_PCT = 15     # 正态分散程度范围（有效窗口宽度 %）
_DEFAULT_SIGMA_MAX_PCT = 25
# 请求最小间隔下限（秒，压缩模式防请求过密，接线于 run_queue_retry）；缺省值住在名册里。
_DEFAULT_MIN_EXEC_GAP = config_loader.default_required("YIBAN_MIN_EXEC_GAP")
# 容量预检与容量预估共用的单账号耗时估算（秒）。缺省按压测实测定档：
# 单账号（登录链 + 签到链共 6 次请求）实测 0.08s（零延迟）、1.87s（拟真 300ms）、
# 3.1s（含尾延迟）。取 8s 会把可容纳账号数低估约 2.6 倍，并使保存门误拒 261~360 个
# 账号的站点；真实网络更慢时由 YIBAN_AVG_ATTEMPT_SEC 覆盖。
_DEFAULT_AVG_ATTEMPT_SEC = 3
_DEFAULT_RETRY_MIN_INTERVAL = 60
_DEFAULT_EXEC_GAP_MIN = 10      # 启动对齐：已过点账号相邻最小间隔（秒）
_DEFAULT_ALLOW_TIME_PREF = 0    # 用户自选时间片总开关（0=关默认，管理员开启后生效）
# V3 全局容量口径：出口令牌桶速率（次尝试/s）、通道数上限、重试与尾延迟降额系数。
# 桶速率键供引擎与 web 侧容量预估共用（web 侧尚未接入）。缺省值住在名册里。
_DEFAULT_BUCKET_RATE = config_loader.default_required("YIBAN_EGRESS_RATE")
_DEFAULT_CHANNELS_MAX = 16
_DEFAULT_UTIL = 0.8
# K 的自动公式里"重试占比" r 的缺省（总尝试量 T = N×(1+r)）：依据既有分级重试预算
# （`attempts.MAX_ATTEMPTS`=3、风控/会话陈旧 2 次、无点位/凭据错误 1 次）——多数失败类
# 最多多试 2 次，而失败本身不是常态；取 0.2（约每 5 个账号多 1 次尝试）把重试算进容量，
# 宁可略高估出口需求，也不按"零重试"把出口排满。
_DEFAULT_RETRY_RATIO = 0.2
# 账号间隔下限（秒）：`front` 铺点速率与容量估算共用的单账号周期项；缺省值住在名册里。
_DEFAULT_ACCOUNT_GAP_MAX = config_loader.default_required("YIBAN_ACCOUNT_GAP_MAX")

#: 分布模式词表（**唯一事实源**）：uniform=均匀铺满；normal=钟形高峰；front=提前铺完。
#: planner 的分布白名单与 web 取值域门都引用它——三处各写一份词表，加模式时必漏两处。
SIGN_DIST_CHOICES = ("uniform", "normal", "front")
#: 分布模式的缺省值：**提前铺完**（留足重试与兜底余量）。刻意不取词表首项。
DEFAULT_SIGN_DIST = "front"

# 签到窗口配置非法的一次性告警标记：_schedule_config 每次调度都会调用，
# 非法窗口回退默认窗口的告警只收集一次，避免同一配置错误在汇总邮件里重复出现
_invalid_window_notified = False

# 窗口退化的一次性告警标记（缓冲过大被收缩 / 窗口不可用被回退）：_schedule_blocks
# 每次调度都会调用（多账号/多轮），同一配置错误的告警只收集一次
_window_clamped_notified = False
_window_fallback_notified = False

#: 上面三个告警标记所属的**业务日**（"YYYY-MM-DD"）；空串=尚未复位过。
#: 复位点唯一：`reset_daily_alerts`（由 `_schedule_config` 每日首调触发）。
_alert_mark_day = ""


def reset_daily_alerts(now: datetime | None = None) -> bool:
    """业务日翻页时复位三个窗口告警去重标记；返回本次是否真的复位了。

    为什么必须有复位点：这三个标记是"同一配置错误当日只并入一次汇总邮件"的去重位，
    但它们原先**只在模块导入时为假、全仓没有任何生产复位点**。兜底常驻进程每 60 秒扫
    一轮且从不重启，于是同一配置错误在第一天报过之后，第二天起彻底无声——管理员看到的
    是"一切正常"，而配置依旧是错的（窗口非法/缓冲吃满/窗口回退三类都如此）。

    复位时机取**业务日翻页**：复位必须发生在三个标记的产生分支之前，而翻页必然早于当日
    窗口开启，故它等价于"新窗口开启时复位"，但只有一个可判时点（每次调用比日期）。
    同一业务日内的重复调用不复位——否则退化成"每轮一次"，去重失效、告警刷屏。
    """
    global _alert_mark_day, _invalid_window_notified
    global _window_clamped_notified, _window_fallback_notified
    day = (now or clock.now()).strftime("%Y-%m-%d")
    if day == _alert_mark_day:
        return False
    _alert_mark_day = day
    _invalid_window_notified = False
    _window_clamped_notified = False
    _window_fallback_notified = False
    return True


def _env_int(name: str, default: int, lo: int | None = None, hi: int | None = None,
             env: Mapping[str, str] | None = None) -> int:
    """读整数环境变量；缺失/非法回退默认（配置校验：回退 + 警告，不崩溃）。

    `env` 给出时只读它、不回落进程环境——口径与 `_env_flag` 一致。容量预估这类
    "一次估算多个键"的调用方必须用它：web 进程的环境里没有 `.env` 的键（run.sh
    才逐行 export），gap 从 `.env` 读而 avg 落进程环境就是跨两个配置层取值（MF-93）。
    """
    src = os.environ if env is None else env
    raw = str(src.get(name, "")).strip()
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


def avg_attempt_sec(env: Mapping[str, str] | None = None) -> int:
    """单账号签到耗时估算（秒）：YIBAN_AVG_ATTEMPT_SEC 显式配置优先，缺省 3s。

    `env` 口径见 `_env_int`：web 侧容量预估必须把生效配置层传进来，与 gap 同源。
    """
    return _env_int("YIBAN_AVG_ATTEMPT_SEC", _DEFAULT_AVG_ATTEMPT_SEC, 1, 300, env=env)


def warn_avg_attempt_sec(cfg_avg: int | None = None, *, days: int = 7, min_samples: int = 20) -> int:
    """容量**告警阈值**的 avg 输入：显式配置（管理员钉住）> 实测 p95（近 `days` 天
    `sign_events.dur_sec`）> 配置缺省档；读不到实测就回退缺省档，绝不把预检拖崩。
    只喂预检告警——保存闸门/展示按计划口径，"能不能保存"不该跟着昨天的网络抖。
    """
    if os.environ.get("YIBAN_AVG_ATTEMPT_SEC", "").strip():
        return cfg_avg if cfg_avg is not None else avg_attempt_sec()
    if cfg_avg is None:
        cfg_avg = avg_attempt_sec()
    try:
        p95 = db.attempt_dur_quantile(days=days, min_samples=min_samples)
    except Exception as e:  # 防御：db 替身缺属性（测试打桩面）也不炸预检
        logger.warning("读取实测尝试耗时失败（告警回退配置值）: %s", e)
        p95 = None
    return max(1, math.ceil(p95)) if p95 else max(1, int(cfg_avg))


def _env_float(name: str, default: float, lo: float | None = None, hi: float | None = None) -> float:
    """读浮点环境变量；缺失/非法回退默认（与 `_env_int` 同一套回退 + 告警口径）。"""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        v = float(raw)
    except ValueError:
        logger.warning("配置 %s=%r 非法，回退默认 %s", name, raw, default)
        return default
    if (lo is not None and v < lo) or (hi is not None and v > hi):
        logger.warning("配置 %s=%s 超出范围 [%s, %s]，回退默认 %s", name, v, lo, hi, default)
        return default
    return v


def capacity_accounts(window_sec: float, gap: float = 0, avg: int | None = None,
                      env: Mapping[str, str] | None = None) -> int:
    """有效窗口内可容纳的账号数（容量口径唯一源：引擎预检与 web 容量预估共用）。

    模型：首个账号立刻占用 avg 秒，此后每个账号按「上一次完成 + 间隔下限」推进，
    相邻账号墙钟间隔 = avg + gap（实测印证：gap=10、t=1.87s → 单账号周期 11.875s）。
    故 容量 = floor((窗口 − avg) ÷ (avg + gap)) + 1；窗口容不下单账号耗时为 0。

    window_sec：有效窗口秒数（已扣掐头去尾）
    gap：账号间隔下限秒（YIBAN_ACCOUNT_GAP_MAX）
    avg：单账号耗时秒；缺省取 avg_attempt_sec(env)
    env：avg 的取值层（口径见 `_env_int`）；同一次估算的所有键必须同一来源（MF-93）
    """
    if avg is None:
        avg = avg_attempt_sec(env)
    avg = max(1, int(avg))
    gap = max(0, int(gap or 0))
    slack = int(window_sec) - avg
    if slack < 0:
        return 0
    return slack // (avg + gap) + 1


def block_capacity(n_accounts: int, n_blocks: int,
                   env: Mapping[str, str] | None = None) -> int:
    """单块容量 K（调度 v2）：块的**唯一事实源**，计划/执行/Web 拥挤度三方共用。

    与 `build_schedule` 内联式同构：偏好/自动分配都以"每块最多 K 人"填块，超出
    `n_blocks × K` 进入压缩模式（K 放大到 `ceil(n / n_blocks)`）。返回：
    - `n_blocks <= 0`（无有效块）→ 0（调用方按"无容量"处理，不得拿它当除数）；
    - `n_accounts <= n_blocks × block_cap` → `block_cap`（不压缩）；
    - 否则 → `ceil(n_accounts / n_blocks)`。

    `block_cap` 经 `_env_int("YIBAN_BLOCK_CAP", _DEFAULT_BLOCK_CAP, 1, 200, env=env)`
    读取：缺失/非法/越界一律回退 15（与 `_schedule_config` 同一夹取口径）。
    **MF-93**：web 进程环境里没有 `.env` 的键，调用方必须传 `env=read_env(ENV_FILE)`，
    否则会读到默认 15、与引擎实际生效容量分叉（拥挤度百分比虚高/虚低）。
    """
    block_cap = _env_int("YIBAN_BLOCK_CAP", _DEFAULT_BLOCK_CAP, 1, 200, env=env)
    if n_blocks <= 0:
        return 0
    cap = n_blocks * block_cap
    return block_cap if n_accounts <= cap else math.ceil(n_accounts / n_blocks)


def channel_count(bucket_rate: float, avg: int | None = None) -> int:
    """每执行体的并发通道数 `M = min(_DEFAULT_CHANNELS_MAX, ceil(bucket_rate × avg × 2))`。

    通道能力 `M/avg` 只需略高于出口令牌桶上限（系数 2 是余量），瓶颈因此始终是两者中
    的较小者。**M 的唯一口径**：容量公式、令牌桶的突发额度与执行体的通道数/线程池上限
    都调本函数——三处各写一份式子，改一处必漏另两处。

    `bucket_rate` 非正 → 回退 `_DEFAULT_BUCKET_RATE` 并告警（与 `capacity_accounts_v3`
    同口径）：非法配置不能让通道数恒为 0，否则容量预估恒 0、执行体一条通道都不起。
    """
    if avg is None:
        avg = avg_attempt_sec()
    avg = max(1, int(avg))
    bucket_rate = float(bucket_rate)
    if bucket_rate <= 0:
        # 非法配置不能让容量恒 0：web 保存闸门会因此误拒一切设置
        logger.warning("出口桶速率 %s 非法，回退默认 %s", bucket_rate, _DEFAULT_BUCKET_RATE)
        bucket_rate = _DEFAULT_BUCKET_RATE
    return min(_DEFAULT_CHANNELS_MAX, math.ceil(bucket_rate * avg * 2))


def capacity_accounts_v3(window_sec: float, k: int = 1, avg: int | None = None,
                         bucket_rate: float = _DEFAULT_BUCKET_RATE,
                         util: float = 0.8,
                         env: Mapping[str, str] | None = None) -> int:
    """V3 全局容量：`容量 = K × min(M/avg, bucket_rate) × W × util`。

    `M = min(16, ceil(bucket_rate × avg × 2))` 是每执行体的并发通道数（唯一口径见
    `channel_count`）：通道能力 `M/avg` 只需略高于出口令牌桶上限，**瓶颈是两者中的
    较小者**。只按通道吞吐算容量、忽略桶封顶，会把容量高估 2.3 倍（桶 1/s、avg 3s 时
    2.67 次/s 并非有效速率）。

    `util` 缺省 0.8（重试与尾延迟降额）；`k` 是执行体数（默认 1 = 单执行体零额外配置）。
    单位是**账号尝试数**（单账号 ≈6 次 HTTP 请求），不是请求数。
    `avg`/`env` 口径同 `capacity_accounts`（同一次估算同一配置层）。
    """
    if avg is None:
        avg = avg_attempt_sec(env)
    avg = max(1, int(avg))
    k = max(1, int(k))
    util = min(max(float(util), 0.0), 1.0)
    channels = channel_count(bucket_rate, avg)
    bucket_rate = float(bucket_rate)
    if bucket_rate <= 0:
        bucket_rate = _DEFAULT_BUCKET_RATE
    rate_eff = min(channels / avg, bucket_rate)
    return math.floor(k * rate_eff * max(0, int(window_sec)) * util + 1e-9)


def capacity_of(window_sec: float, *, gap: float = 0, avg: int | None = None,
                k: int | None = None, bucket_rate: float = _DEFAULT_BUCKET_RATE,
                util: float = 0.8, enabled: bool | None = None,
                env: Mapping[str, str] | None = None, retry_reserve: bool = False) -> int:
    """按当日生效的调度版本选容量公式（**唯一选择函数**：四处调用点统一走它）。

    为什么要一个选择函数：两套公式若被各调用点分别内联，同一份配置会在"保存闸门"与
    "引擎预检"两处按不同口径算出不同容量，出现"保存被拒、计划却排得下"的分裂。收口到
    一处后，`enabled=False` **逐字**走 `capacity_accounts`（v2 公式，保留给既有的
    "按旧口径展示/测试"的调用方），`enabled=True` 走 `capacity_accounts_v3`。

    `enabled` 缺省取 `executor_v3.scheduler_v3_enabled(env)`——**台账单池化后该判定恒真**
    （v3 是唯一生产执行路径，开关已随单池消失），故缺省即 v3 口径；`enabled=False` 只为
    测试与"要按 v2 公式取值"的调用方显式传入。`env` 仍传给 `scheduler_v3_enabled` 只为
    保持签名形状（其实现已不读环境）。
    `k` 是执行体数，缺省 1（单执行体零额外配置，与 `capacity_accounts_v3` 的缺省一致）——
    需要按账号量自动定尺的调用方先用 `executor_count` 算出 K 再传入。`bucket_rate`/`util`
    只在 v3 侧参与。

    `env`：`avg`/`enabled` 未显式给出时的取值配置层（口径见 `_env_int`）。调用方一旦
    传了 `env`，本次估算的 avg 与开关就从**同一份** `env` 读，不再跨"进程环境 + .env"
    两层各取一半——那是容量高估 69% 的根（web 进程环境不含 `.env`，而 gap 又来自
    `.env`）。缺省 None 走 `os.environ`，引擎侧行为逐字不变。

    `retry_reserve`（**只喂 v2 公式分支**）：把每账号周期放大到 `MAX_ATTEMPTS×(avg+gap)`，
    重试同样吃墙钟——按零重试排满窗口正是 122–360 静默死带的根（计划/展示/闸门不传）。
    v3 不参与：`util` 缺省 0.8 本身就是重试降额，再扣一次是双重计算。
    """
    if enabled is None:
        # 局部导入：executor_v3 反向依赖本模块（配置快照、通道数），模块级互引会成环；
        # 本函数只被保存闸门/引擎预检/CLI 调用，频率低，局部导入的开销可忽略。
        # 单池后该判定恒真（v3 是唯一生产执行路径）。
        from yiban.engine import executor_v3
        enabled = executor_v3.scheduler_v3_enabled(env)
    if not enabled:
        if retry_reserve:
            # 储备 = (MAX_ATTEMPTS−1) 份单账号周期/账号，折进 gap 复用同一个式子
            from yiban.engine import attempts  # 局部导入：attempts 带整条客户端链
            avg_eff = max(1, int(avg if avg is not None else avg_attempt_sec(env)))
            gap_eff = max(0, int(gap or 0))
            gap = gap_eff + (attempts.MAX_ATTEMPTS - 1) * (avg_eff + gap_eff)
        return capacity_accounts(window_sec, gap, avg, env=env)
    return capacity_accounts_v3(window_sec, 1 if k is None else k, avg,
                                bucket_rate, util, env=env)


def executor_count(n_accounts: int, window_sec: float, *,
                   bucket_rate: float = _DEFAULT_BUCKET_RATE,
                   retry_ratio: float | None = None, egress_count: int = 1) -> int:
    """满足当日账号量的执行体数 `K = clamp(ceil(N×(1+r)/(W×bucket×0.8)), 1, 出口数)`。

    **K 的唯一口径**：容量公式、预检告警与"该开几个执行体"的建议都调本函数——三处各写
    一份式子，改一处必漏另两处。分子 `T = N×(1+r)` 是**总尝试量**：重试同样占出口额度，
    按零重试算会把 K 低估（`r` 缺省 0.2，依据见 `_DEFAULT_RETRY_RATIO`）。分母是单执行体
    的有效速率 `W×bucket×0.8`（`util` 与容量公式同口径：重试与尾延迟降额）。

    结果夹到 `[1, 出口数]`：至少 1（单执行体零配置），至多不超过出口数——再加执行体也
    只共享同一批出口，加进程不会放大总速率（见 `docs/dev/scheduler-v3.md`）。**与
    部署者实测的建议数不是同一口径**：实测按"每进程各持一桶"量取（实测值由部署者自行
    量取后录入 `YIBAN_CAPACITY_MEASURED`），"20–22 个桶" ≈要声明同数物理出口；未声明
    出口清单时 K≡1 是设计语义而非被夹死的缺陷（README
    「多执行体」同款说明）。`egress_count` 缺省 1；`bucket_rate` 非正回退出厂速率（与
    `channel_count` 同口径）；窗口 <= 0 时回退 1。
    """
    r = _DEFAULT_RETRY_RATIO if retry_ratio is None else max(0.0, float(retry_ratio))
    n = max(0, int(n_accounts))
    egress = max(1, int(egress_count))
    w = max(0, int(window_sec))
    rate = float(bucket_rate)
    if rate <= 0:
        rate = _DEFAULT_BUCKET_RATE
    if w <= 0:
        return 1
    need = math.ceil(n * (1.0 + r) / (w * rate * _DEFAULT_UTIL))
    return min(max(1, need), egress)


def egress_rate() -> float:
    """出口级目标速率 λ（`YIBAN_EGRESS_RATE`，次尝试/s）。**唯一读取点**：
    `planner_config`（限速桶速率）与容量预检的**出口预算告警**都调本函数——同一个量
    只许一处读，域与夹取也只此一份（两处各写一遍式子，改一处必漏另一处）。
    非法/越界回退名册缺省（与 `channel_count` 的"非法回退出厂速率"同口径）。
    """
    return _env_float("YIBAN_EGRESS_RATE", _DEFAULT_BUCKET_RATE, 0.01, 100)


def _schedule_config(now: datetime | None = None) -> dict[str, Any]:
    """读取调度 v2 配置（每次调用读取，便于测试与热改）。

    兼容旧 YIBAN_SIGN_MODE：sequence→顺序×提前铺完、random→随机×提前铺完、
    normal→顺序×正态；新参数 YIBAN_SIGN_ORDER / YIBAN_SIGN_DIST 优先。
    返回 dict：order/dist/edge_front_sec/edge_back_sec/block_cap/mu/sigma 百分比/
    min_exec_gap/avg_attempt_sec/retry_min_interval/exec_gap_min/sign_start/sign_end。

    `now` 只用于把"业务日"交给 `reset_daily_alerts`（见其文档）：常驻兜底这类每轮都
    已经取过当前时刻的调用方顺手传进来，省掉一次多余的 `clock.now()`。
    """
    # 业务日翻页先把三个告警去重标记复位（唯一复位点，理由见 reset_daily_alerts）：
    # 本函数是每一轮调度的入口，复位挂在这里，调用方不必记得手动清理。
    reset_daily_alerts(now)
    # 局部导入：alerts 反向依赖本模块的窗口判定（告警要判"窗口是否还开着"），
    # 模块级互引会成环；本函数每天只调用几次，局部导入的开销可忽略。
    from yiban.engine import alerts

    mode = os.environ.get("YIBAN_SIGN_MODE", "").strip().lower()
    order = os.environ.get("YIBAN_SIGN_ORDER", "").strip().lower()
    dist = os.environ.get("YIBAN_SIGN_DIST", "").strip().lower()
    if order not in ("sequence", "random"):
        order = "random" if mode == "random" else "sequence"
        if dist not in SIGN_DIST_CHOICES:
            dist = "normal" if mode == "normal" else DEFAULT_SIGN_DIST
    elif dist not in SIGN_DIST_CHOICES:
        dist = DEFAULT_SIGN_DIST
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
            # 只写日志不够：管理员在 Web 界面看到的窗口设置"看起来生效"、实际签到
            # 时刻完全不同且无人知情。故并入当日汇总邮件（A 线）。
            # 高级别：实际签到时刻与配置不符 = 当天可能全量漏签，运维须当机改 .env。
            alerts._collect_admin_mail("签到窗口配置异常", _msg,
                                       level=alerts.ALERT_LEVEL_CRITICAL)
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
    # 掐头去尾（前后独立，秒级，0.5 分钟=30s 粒度；UI 按 0.5 分钟步进）：
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


def _executor_ids(env: Mapping[str, str] | None = None) -> list[str]:
    """执行体身份串列表（HRW 分工的候选集）：清单里的并行执行体行；无清单 → 单执行体。

    身份串的唯一构造处是 `yiban.egress`（跨重启稳定、含主机名），这里只按清单行取
    ——计划里的 `owner` 必须与执行体自己算出的名字逐字一致，否则分片归属会对不上。
    """
    from yiban import egress
    src = os.environ if env is None else env
    rows = egress.parse_manifest(src.get(egress.ENV_MANIFEST))
    if rows:
        workers = egress.worker_rows(rows)
        if workers:
            return [egress.worker_owner(r["slot"]) for r in workers]
    return [egress.single_owner()]


def planner_config() -> dict[str, Any]:
    """Planner 用的配置快照（调度 v3）：窗口/裁剪 + 分布三态 + μσ + 桶速率 + 执行体/间隔。

    读法与 `_schedule_config` **同源**（直接复用它的结果），只补三项 Planner 独有的：
    `bucket_rate`（`YIBAN_EGRESS_RATE`，缺省住在名册里）、`executors`（HRW 候选集）与
    `account_gap_max`（`YIBAN_ACCOUNT_GAP_MAX`，`front` 铺点速率的单账号周期项）。
    不另存一份窗口/模式口径——两份口径迟早会分叉。
    """
    cfg = _schedule_config()
    cfg["bucket_rate"] = egress_rate()
    cfg["executors"] = _executor_ids()
    cfg["account_gap_max"] = _env_int("YIBAN_ACCOUNT_GAP_MAX", _DEFAULT_ACCOUNT_GAP_MAX, 0, 3600)
    return cfg


def egress_rate_explicit() -> bool:
    """`YIBAN_EGRESS_RATE` 是否被显式写入（供限速器判「人工接管」）。

    值本身仍由 `planner_config` 读（本键的唯一取值点），这里只回答"有没有配"：
    配了就不再让 AIMD 改写速率——管理员手写的值不该自己漂移。
    """
    return bool(os.environ.get("YIBAN_EGRESS_RATE", "").strip())


def _anchor_z(phone: str) -> float:
    """账号锚点分位（顺序×正态）：hash(phone) 派生标准正态值，零持久化、每天稳定。"""
    return random.Random(str(phone)).gauss(0, 1)


def _sigma_eff(sigma: float, n: int, span_minutes: float) -> float:
    """人数自适应 + 封顶：σ×(1+log2(n/20))，上限 有效窗口/3（防端点堆积）。"""
    if n > 20:
        sigma = sigma * (1 + math.log2(n / 20))
    return min(sigma, span_minutes / 3)


def day_mu_sigma_pct(cfg: Mapping[str, Any], day: Any) -> tuple[float, float]:
    """正态 μ/σ 的**当日取值**（占有效窗口的 %）：返回 `(mu_pct, sigma_pct)`。

    μ/σ 定义的是一个区间（`YIBAN_SCHEDULE_MU_MIN_PCT`~`_MAX_PCT` 等），落在区间的
    哪一点由 `day` 唯一决定（`hrw.u01`，blake2b 确定性推导），故同一天内任意进程、
    任意时刻重放都是同一组值：每日只取一次、全体账号共享。

    计划层（`planner._density`）与执行层（`build_schedule`）**必须共用本函数**：
    两侧各抽一次（哪怕抽法看起来相同、甚至同样确定性）会让计划里展示的时刻与实际
    签到时刻错开，而按 μ/σ 算的峰值速率整形也随之失去意义；执行层若用 `rng.uniform`
    重采样，更直接破坏"计划是纯函数、崩溃可重放"的既有不变量。

    `day` 接受 `date`/`datetime`/字符串，统一归一化为 `YYYY-MM-DD` 再进哈希——两侧
    一个传业务日字符串、一个传 `datetime` 时不会因此错日。返回 % 而非分钟：两侧的
    有效窗口边界与 σ 人数封顶各自已有口径，这里只交付"区间里的那个点"。
    区间非法（lo >= hi）已在 `_schedule_config` 回退默认值，本函数不再重判。
    """
    day = day.strftime("%Y-%m-%d") if hasattr(day, "strftime") else str(day).strip()
    mu_pct = cfg["mu_min_pct"] + hrw.u01(day, "mu") * (cfg["mu_max_pct"] - cfg["mu_min_pct"])
    sg_pct = (cfg["sigma_min_pct"] + hrw.u01(day, "sigma")
              * (cfg["sigma_max_pct"] - cfg["sigma_min_pct"]))
    return mu_pct, sg_pct


def _schedule_blocks(cfg: Mapping[str, Any]) -> tuple[list[tuple[float, float]], float, float]:
    """按时钟 5 分钟对齐切块（首尾块各 4 分钟），返回 (blocks, eff_lo, eff_hi)。

    blocks: [(lo_min, hi_min), ...]（浮点分钟，支持 0.5 分钟=30s 的裁剪粒度）；
    eff_lo/eff_hi：有效窗口分钟边界（相对当天 0:00），由前后裁剪分别决定。
    缓冲过大时 `window.bounds` 只收缩缓冲、保留窗口（`edges_clamped`）；窗口本身不可用
    （宽度 <= 0）才回退默认窗口（`fell_back`）——两种退化各告警一次，块列表永不空。
    """
    from yiban.engine import alerts  # 局部导入的理由同 _schedule_config

    win = window.bounds(cfg)
    start_min, end_min = win.start_min, win.end_min
    eff_lo, eff_hi = win.lo_min, win.hi_min
    _win_txt = (f"{cfg['sign_start'][0]:02d}:{cfg['sign_start'][1]:02d}"
                f"~{cfg['sign_end'][0]:02d}:{cfg['sign_end'][1]:02d}")
    if win.edges_clamped:
        logger.warning(
            "签到窗口 %s 的缓冲过大（前 %ss 后 %ss，合计已达窗口宽度），"
            "已收缩为 前 %ss 后 %ss，窗口本身未改动",
            _win_txt, cfg["edge_front_sec"], cfg["edge_back_sec"],
            win.front_sec, win.back_sec,
        )
        # 窗口没变、只是精修被牺牲，管理员仍需知道"保存的值与实际生效的值不同"
        global _window_clamped_notified
        if not _window_clamped_notified:
            _window_clamped_notified = True
            # 高级别：与下面 fell_back 那支"签到窗口配置异常"同属"窗口退化 ⇒ 配置与
            # 实际生效不符"一族（有效窗口被压到窗口宽度的 80%，重试空间随之减少）。
            # 判级只看语义、不看它在列表里的位置。
            alerts._collect_admin_mail(
                "签到窗口缓冲已收缩",
                (
                    f"签到窗口 {_win_txt} 的缓冲过大（前 {cfg['edge_front_sec']}s / 后 "
                    f"{cfg['edge_back_sec']}s，合计已达窗口宽度），已等比收缩为 前 "
                    f"{win.front_sec}s / 后 {win.back_sec}s，窗口本身未改动（有效窗口 = "
                    "窗口宽度的 80%）。请调小 YIBAN_WINDOW_EDGE_FRONT_SEC / "
                    "YIBAN_WINDOW_EDGE_BACK_SEC（或放宽 YIBAN_SIGN_START / YIBAN_SIGN_END）"
                ),
                level=alerts.ALERT_LEVEL_CRITICAL,
            )
    if win.fell_back:
        logger.warning("签到窗口 %s 不可用（宽度 <= 0），回退默认窗口 06:30~07:50", _win_txt)
        # 同 _schedule_config：窗口不可用会让"界面看到的设置"与实际签到时刻不符
        global _window_fallback_notified
        if not _window_fallback_notified:
            _window_fallback_notified = True
            # 高级别：窗口不可用而回退默认窗口，实际签到时刻与配置不符
            # （同 `_schedule_config` 那支"签到窗口配置异常"口径）。
            alerts._collect_admin_mail(
                "签到窗口配置异常",
                (
                    f"签到窗口 {_win_txt} 不可用（宽度 <= 0），已回退默认窗口 "
                    "06:30~07:50，实际签到时间将与配置不符！请检查 "
                    "YIBAN_SIGN_START / YIBAN_SIGN_END"
                ),
                level=alerts.ALERT_LEVEL_CRITICAL,
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


def _window_closed(sch_cfg: Mapping[str, Any], now_dt: datetime) -> bool:
    """签到窗口是否已关闭（与 _schedule_blocks 同源：都走 window.bounds，含同一套回退）。

    若这里只按 sign_end - edge_back 算，而 _schedule_blocks 在"有效窗口被裁剪吃空"时
    回退到默认窗口，就会出现"有完整计划、却整轮判时段已结束、零请求"。
    """
    return window.bounds(sch_cfg).is_closed(now_dt)


def _window_open(sch_cfg: Mapping[str, Any], now_dt: datetime) -> bool:
    """签到窗口是否已开始（同源同上）。"还没开"与"已经关"是两种情形，调用方要分开处理。"""
    return window.bounds(sch_cfg).is_open(now_dt)


def _window_opens_in(sch_cfg: Mapping[str, Any], now_dt: datetime) -> float:
    """距窗口开始还有多少秒（已开始为 <= 0）。"""
    return window.bounds(sch_cfg).opens_in_sec(now_dt)


#: 开关类环境变量的真值字面量。**单一事实源**在 `yiban.infra.env_io.ENV_TRUTHY_LITERALS`
#: （引擎 / web / 通知 / 容器调度 / bash 共用一套口径）；此处只保留别名供既有再导出
#: （`web/app.py` 的 `_TRUTHY_LITERALS`）与测试引用，勿在此另抄一份字面量。
_TRUTHY_LITERALS = _env_io.ENV_TRUTHY_LITERALS


def _env_flag(name: str, env: Mapping[str, str] | None = None) -> bool:
    """开关类环境变量真值（1/true/on/yes，大小写不敏感、两侧空白忽略）。

    判定单源在 `yiban.infra.env_io.parse_env_flag`：非预期取值按缺省（假）处理并出声一次，
    不再静默吞掉。未设/空/其它假值字面量一律为假。
    """
    src = os.environ if env is None else env
    return _env_io.parse_env_flag(src.get(name, ""), default=False, key=name, log=logger)


#: `day_off()` 的返回原因（空串表示照常签到）
DAY_OFF_SUNDAY = "sunday"
DAY_OFF_SATURDAY = "saturday"
DAY_OFF_PAUSED = "paused"


def weekend_flags(env: Mapping[str, str] | None = None) -> tuple[bool, bool]:
    """周末签到开关（周六, 周日）的**唯一解析口径**（`_env_flag`：1/true/on/yes 为真）。

    `day_off` 与 web 展示（面板状态行、我的日历置灰）都只读这里——原先 web 侧各自
    用整数解析，`=true` 时引擎照签而面板标休（两套值域分叉）。
    """
    return (_env_flag("YIBAN_SATURDAY_SIGN", env), _env_flag("YIBAN_SUNDAY_SIGN", env))


def day_off(now: datetime | None = None, sat: bool | None = None, sun: bool | None = None,
            env: Mapping[str, str] | None = None) -> str:
    """今天这一刻是否**有意不签到** → 原因串；空串=照常。

    周末门与一键暂停门的**唯一实现**（顺序与历史行为一致：周日 → 周六 → 暂停）。
    为什么必须唯一：这三道门原先只写在 `runner.main` 里，而 `--fallback` 在它们之前
    就 `return` 了（见 `runner.main` 的分支顺序），于是**兜底常驻把门全部绕过**——
    管理员在网页关掉周末签到、或点了一键暂停，兜底照样把账号签掉。

    `sat`/`sun` 可显式传入（定时轮传导入期快照常量，便于既有测试注入）；不给则读环境。
    """
    if sat is None or sun is None:
        def_sat, def_sun = weekend_flags(env)
        sat = def_sat if sat is None else sat
        sun = def_sun if sun is None else sun
    weekday = (now or clock.now()).weekday()
    if weekday == 6 and not sun:
        return DAY_OFF_SUNDAY
    if weekday == 5 and not sat:
        return DAY_OFF_SATURDAY
    if _env_flag("YIBAN_GLOBAL_PAUSE", env):
        return DAY_OFF_PAUSED
    return ""


def _nearest_available(bi: int, filled: list[int], blocks: list[Any], cap: int) -> int | None:
    """双向就近找未满块（自选溢出顺延用；同距离优先更早的块）。无可用返回 None。"""
    n = len(blocks)
    for d in range(n):
        for idx in (bi - d, bi + d):
            if 0 <= idx < n and filled[idx] < cap:
                return idx
    return None


def _next_available(bi: int, filled: list[int], blocks: list[Any], cap: int) -> int:
    """从 bi 向后（环回）找第一个未满块。"""
    n = len(blocks)
    for step in range(n):
        idx = (bi + step) % n
        if filled[idx] < cap:
            return idx
    return bi  # 全满（理论不会发生：cap 已按 n 放大）


def _slot_to_bi(cfg: Mapping[str, Any]) -> dict[int, int]:
    """自选片分钟偏移（相对窗口起点）→ 块索引。

    窗口起止与前后裁剪一律取 `window.bounds(cfg)`，与 `_schedule_blocks` 同准绳：有效
    窗口被裁剪吃空时两者都按回退后的默认窗口算——各自直读 `cfg` 的话，块照常在回退窗口
    里切，自选片却因 `hi <= lo` 恒不成立而整片落空，用户所选片被静默放弃。
    key = 块起点 - 窗口起点（窗口起点非 5 分钟倍数时同样成立），与
    `web.routes.my._pref_slots` 的 `slot_min` 同号。
    """
    win = window.bounds(cfg)
    start_min, end_min = win.start_min, win.end_min
    front = win.front_sec / 60.0
    back = win.back_sec / 60.0
    m = {}
    bi = 0
    for b in range(start_min, end_min, 5):
        lo = max(b, start_min + front)
        hi = min(b + 5, end_min - back)
        if hi > lo:
            m[b - start_min] = bi
            bi += 1
    return m


def build_schedule(accounts: Iterable[Any], order: str | None = None, dist: str | None = None,
                   now: datetime | None = None, rng: Any = None,
                   prefs: Mapping[str, Any] | None = None) -> dict[str, datetime]:
    """调度 v2：统一填充框架。

    排序维度 × 分布维度（2×2）：
    - 顺序×均匀：线性填块（第 i 账号 → 第 i/K 块，先到先签）
    - 随机×均匀：打乱后循环填块（每块人数均衡、铺满窗口）
    - 顺序×正态：z_i 锚点（hash(phone)）稳定作息 + 钟形
    - 随机×正态：每天重抽分位（重排 + 钟形，防风控最强）
    自选优先：prefs 传入 {phone: {slot_min, updated_at}} 时，自选账号固定所选片
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
    k = block_capacity(n, len(blocks), env=os.environ)
    if n > cap:
        logger.warning(
            "压缩模式: %d 个账号超出块容量 %d，块容量放大至 %d（间隔 ≈ %.1fs）",
            n, cap, k, (span * 60) / n,
        )

    # 自选优先占块：先到先得 + 溢出双向就近顺延
    chosen = {}  # phone -> bi
    if prefs is None:
        prefs = {}
        if cfg["allow_time_pref"]:
            try:
                prefs = db.get_time_prefs()
            except Exception as e:
                logger.warning("读取自选时间失败（忽略，走自动分配）: %s", e)
    if prefs:
        # 只保留当前账号集合内的 pref（换号/删号后的孤儿不占容量）
        valid_phones = {a.phone for a in accounts}
        slot_to_bi = _slot_to_bi(cfg)
        by_slot: dict[int, list[tuple[str, str]]] = {}
        for phone, p in prefs.items():
            if phone not in valid_phones:
                continue
            try:
                slot = int(p.get("slot_min", -1))
            except (TypeError, ValueError):
                continue
            # 可用性以 _slot_to_bi 的成员性为准（与 Web 端 _pref_slots 同一套判定）：
            # 校验 `0 <= slot < span`（span=有效窗口宽度）会误杀末尾片——_slot_to_bi
            # 的键范围是完整窗口，窗口长度非 5 分钟整数倍时（如 06:30~07:52），
            # 末尾片在 Web 端可点选、却在这里被判"落窗外"而静默回退自动分配。
            if slot not in slot_to_bi:  # 片无效/落窗外 → 回退自动分配
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
        # μ/σ 每天取一次、全体共享，**取法与计划层同一口径**（`day_mu_sigma_pct`）：
        # 计划（06:31 生成）与执行必须落在同一个点上，否则计划里排出的时刻与实际签到
        # 时刻对不上，planner 按 μ/σ 做的峰值速率整形也作用在错值上。此前这里用
        # `rng.uniform` 重采样，与 planner 的确定性推导分叉，正是"两口径"的现场。
        mu_pct, sg_pct = day_mu_sigma_pct(cfg, now)
        mu = eff_lo + span * mu_pct / 100.0
        sigma = _sigma_eff(span * sg_pct / 100.0, n, span)

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
            schedule[p] = base + timedelta(minutes=min(t, hi - 0.001))
    return schedule
