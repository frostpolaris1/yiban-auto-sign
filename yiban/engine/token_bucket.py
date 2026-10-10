# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""**算法溯源**
- GCRA（Generic Cell Rate Algorithm，通用单元速率算法）：Jim Kurose、Don Towsley 的
  速率整形理论。`EgressBucket` 的理论到达时间 `tat = max(now, tat) + T` 与放行判据
  `now >= tat − τ` 即该算法的整形点；`max(now, ...)` 使其**不累积额度**（长期空闲只放行
  burst 条，而不是攒够一次放行上千条）。
- AIMD（Additive Increase / Multiplicative Decrease，加性增乘性减）：Tahoe 算法与其后
  RFC 5681 的 TCP 拥塞控制。`GROWTH_FACTOR` / `SHRINK_FACTOR` 是它的两个动作。
- Adaptive Concurrency Limits（自适应并发上限）：Netflix 的并发限制方法论，见
  *Rethinking Concurrency Control for Microservices*（Mahadut 等）。核心主张是把速率当成
  TCP 拥塞窗口、按响应反馈自调，而不是人工设一个会随部署规模过期的固定值。
- EWMA（Exponentially Weighted Moving Average，指数加权移动平均）：`apply_ewma` 的外环
  速率平滑。
- Token Bucket（令牌桶）：ATM 网络整形的经典方法。本模块刻意**不**直接用容量型令牌桶——
  它会累积空闲额度，改用 GCRA（见 `EgressBucket` 说明）。

**功能**
出口限速件：按出口整形的令牌桶（GCRA/TAT，不累积额度）+ AIMD 自适应 + 全局聚合
速率上界 Λ + 每账号间隔安全件 + EWMA 外环微调。

**单位（全模块唯一口径）**：速率一律是**账号尝试/s**（attempt/s）。本项目单账号
= 6 次 HTTP 请求（登录链 4 + 定位 + 签到），故 `rate = 1` 的语义是 1 次尝试/s
≈ 6 次实际请求/s；常量、日志文案与 `egress_state.rate` 都是这个单位。按字面
"1 req/s" 实现会让实际请求量只有设计的 1/6。

**归属**
`yiban.engine` 的执行层限速件（调度 v3）：计划层决定"何时做"，本模块决定"多快做"。
风控是**外生约束**且按出口 IP 计数，故限速的粒度是"每出口"，预算由该出口上的执行体
**均分**；全局 Λ 是附加上界而非替代——Λ 存在时加出口/加进程不再线性放大总速率
（否则运维会用"加进程"解吞吐问题）。

**复用**
- `EgressBucket`：单出口 GCRA 桶（纯状态机，注入 `now` 即可测）；
- `EgressLimiter`：`{egress: EgressBucket}` + AIMD/半开 + **出口预算均分**（`shares`
  = 同出口执行体数，子桶速率 = 出口级 rate ÷ shares）+ `snapshot` / `persist` /
  `restore_from_store`（落库往返与崩溃重启恢复）+ `downgrade_all`（全体降档入口）；
- `GlobalLimiter`：Λ 上界；`AccountGapGate`：每账号 gap 安全件；
- `apply_ewma`：外环一步速率更新（平滑系数归调用侧）；
- 配置面收口：`limiter_from_env`（速率 + 突发额度一次读全，并在显式配速率时标记
  人工接管）、`burst_from_env` / `burst_cap`（`YIBAN_MIN_EXEC_GAP` → 突发额度）、
  `gap_gate_from_env`（gap 门）。

**通信**
输入：`now`（浮点秒，**必须由调用方注入**，测试不依赖真实时钟）、出口标识、风控信号；
输出：`acquire(...) -> bool` 与 `retry_after(...) -> float`（调用方据此 sleep）。
落库：`egress_state(egress, rate, burst, tat, updated_at)`（`rate` / `burst` 是**出口级**量；
`tat` 不写——令牌位置不跨进程共享），经 `queue_store.
load_egress_state` / `save_egress_state`——本模块**唯一**的持久化路径，10s 粒度由
调用方循环，写失败只告警不阻断签到。配置面只有三个旋钮：每出口目标速率
`YIBAN_EGRESS_RATE`（attempt/s，缺省值住在名册里，**值只由 `schedule.planner_config` 读**，
本模块经 `limiter_from_env` 取用而不另立字面量；该键**是否被显式写入**由
`schedule.egress_rate_explicit` 回答，用来判人工接管）、`YIBAN_MIN_EXEC_GAP`
（突发额度收口，见 `burst_from_env`）、`YIBAN_ACCOUNT_GAP_MAX` /
`YIBAN_ACCOUNT_GAP_ENFORCE`（gap 门）。
调用谁：`yiban.store.queue_store`。谁调用：执行体（通道循环取额度、回报 `on_success` /
`on_risk_signal`、10s 循环 `persist`；站点级熔断用 `downgrade_all`）与探针
（`probe.run_probe` 每账号前取额度、轮末 `persist`；只消费额度、不喂 AIMD 信号）。
生产入口有两处：执行体 `executor_v3`（台账单池化后的唯一执行体）与探针 `probe`——探测是
真实登录、与签到同一风控暴露面，故与同出口的执行体**共用同一个出口标识**。**持久键**是
出口标识（`yiban.egress.egress_identity`：空出口归一为 `direct`，否则取去 userinfo 的
`scheme://host[:port]`），故 `egress_state` **一个出口一行**；**运行期**则按出口预算均分
（每进程子桶速率 = 出口级 λ ÷ 同出口执行体数 n，n 个进程合计 ≤ λ，见 `EgressLimiter`）。
"""
import logging
import os

from yiban import config_loader
from yiban import egress as yb_egress  # 别名必需：本模块的形参就叫 `egress`（桶键）
from yiban.store import queue_store

logger = logging.getLogger("yiban.engine.token_bucket")

#: AIMD 下限（attempt/s）：与 `YIBAN_MIN_EXEC_GAP` 的缺省秒数同源（互为倒数）。
RATE_MIN = 0.2
#: 子桶份额的下限（attempt/s）：出口级速率 ÷ n 可以远低于 `RATE_MIN`，子桶构造只夹到这个
#: 极小正值（防除零）。**不得**用 `RATE_MIN` 夹份额：n 个进程合计会超过出口预算 λ。
SHARE_RATE_FLOOR = 1e-6
#: AIMD 上限（attempt/s）
RATE_MAX = 4.0
#: 出厂速率（attempt/s）：单账号 = 6 次 HTTP 请求，故 ≈ 6 请求/s。
#: **缺省值住在名册**（`YIBAN_EGRESS_RATE`），代码里不得再写一份字面量。
RATE_DEFAULT = config_loader.default_required("YIBAN_EGRESS_RATE")
#: 无风控信号连续尝试数才加性探测（每满一轮 ×GROWTH_FACTOR）
SUCCESS_STREAK = 200
#: 风控命中后该出口的半开时长（秒）
HALF_OPEN_SEC = 300
#: 风控信号率 EWMA 的平滑系数（半衰期 ≈ 3 次采样）；由调用侧使用，见 `apply_ewma`
EWMA_ALPHA = 0.2
#: 外环目标跟踪的增益
EWMA_BETA = 0.5
#: 单次调整幅度上限（±20%）：防一次抖动把速率打到下限，宁可慢调
MAX_STEP = 0.20
#: AIMD 乘性步长：无风控上探 / 风控回退
GROWTH_FACTOR = 1.2
SHRINK_FACTOR = 0.5
#: 突发额度缺省 = 通道数 M（单出口 1 attempt/s、单账号 3s → 6）。执行体按
#: `M = min(16, ceil(rate × avg × 2))` 算好传入，本模块不自算通道数（口径唯一）；
#: 再用 `YIBAN_MIN_EXEC_GAP` 收口，见 `burst_from_env`。
DEFAULT_BURST = 6
#: 每账号 gap 的键：沿用既有键名（`capacity_accounts` 的 gap 入参同源）
ENV_ACCOUNT_GAP_MAX = "YIBAN_ACCOUNT_GAP_MAX"
#: gap 安全件的开关键（缺省 1=开）：上游是否按账号维度看间隔尚未实测裁决
ENV_GAP_ENFORCE = "YIBAN_ACCOUNT_GAP_ENFORCE"
#: `YIBAN_ACCOUNT_GAP_MAX` 缺省值（秒）：住在名册里，代码不再写第二份字面量。
DEFAULT_ACCOUNT_GAP_SEC = config_loader.default_required("YIBAN_ACCOUNT_GAP_MAX")
#: 最小执行间隔的键（秒）：旧语义是"相邻两次尝试的最小间隔"（压缩模式防请求过密），
#: 在令牌桶形态下由 `burst_from_env` 收口进突发额度，读取点仍在 schedule
ENV_MIN_EXEC_GAP = "YIBAN_MIN_EXEC_GAP"
#: `YIBAN_MIN_EXEC_GAP` 缺省值（秒）：`RATE_MIN = 0.2` 正是它的倒数——最慢时一条尝试
#: 占满一个最小间隔，同一个物理量的两种写法。缺省值住在名册里。
DEFAULT_MIN_EXEC_GAP_SEC = config_loader.default_required("YIBAN_MIN_EXEC_GAP")
#: 开关类环境变量的假值字面量（与 `schedule._env_flag` 的真值表互补）：写这些值才是
#: "显式关闭"；既非真值也非假值的手写错值另有归属，见 `gap_gate_from_env`。
_FALSY_LITERALS = ("0", "false", "off", "no")


def clamp_rate(rate, floor=None):
    """速率夹到 `[floor, RATE_MAX]`（attempt/s）；非法输入回退出厂速率。

    `floor` 缺省是出口级速率的域下界 `RATE_MIN`。子桶份额（出口级 rate ÷ n）可以低于它，
    故限速器构造子桶时传 `SHARE_RATE_FLOOR`——夹回出口级下界会让 n 个进程合计超过 λ
    （工单 2cwd 的预算均分就靠这一点）。

    桶的每个入口都调本函数，故"配置里写的速率"与"实际执行的速率"可以是两个数（
    `YIBAN_EGRESS_RATE` 的名册域比桶域宽）。按**出口级生效速率**算的地方（容量预检的
    出口预算告警）必须经过本函数，否则越界配置会让结论偏一个方向：偏大漏报、偏小多报。
    """
    floor = RATE_MIN if floor is None else floor
    try:
        v = float(rate)
    except (TypeError, ValueError):
        return RATE_DEFAULT
    return min(max(v, floor), RATE_MAX)


class EgressBucket:
    """单出口令牌桶（GCRA/TAT 实现，**不累积额度**）。

    `rate` 单位是**账号尝试/s**（attempt/s），T = 1/rate。`burst` 是突发额度（尝试数）：
    容量为 burst 的令牌桶在 GCRA 下等价于容差 τ = (burst−1)·T（burst=0 ⇒ τ=0，即严格
    1/T 间隔）。放行判据 `now >= tat − τ`，放行后 `tat = max(now, tat) + T`——`max(now, …)`
    是**不累积**的关键：长期空闲后只放行 burst 条，不会因"积攒"一次放行上千条
    （固定窗口边界突发的反面）。

    `rate_min` 是速率夹取的下界：出**口级**桶用域下界；**子桶份额**（出口级速率 ÷ n）
    传 `SHARE_RATE_FLOOR`——份额常低于出口级下界，夹回去会让 n 个进程合计超过 λ。
    """

    def __init__(self, egress, rate=RATE_DEFAULT, burst=DEFAULT_BURST, tat=0.0,
                 rate_min=None):
        self.egress = egress
        self.rate = clamp_rate(rate, rate_min)
        self.burst = float(burst)
        self.tat = float(tat)

    @property
    def interval(self):
        """T = 1/rate（秒/尝试）：rate=1 的 T 恰为 1.0s（1 attempt/s），不是 1/6。"""
        return 1.0 / self.rate

    def burst_sec(self, burst=None):
        """突发额度换算成时间：τ = (burst−1)·T（容量 burst 的令牌桶等价容差）。

        burst=0 ⇒ τ=0（严格 1/T 间隔）；burst=1 ⇒ τ=0（单通道，无突发）。`burst` 给了就
        按它算而不动自身额度——半开期把突发压到 1（单通道探测）用。
        """
        b = self.burst if burst is None else float(burst)
        return max(0.0, b - 1.0) * self.interval

    def admit_at(self, burst=None):
        """下一次可放行的最早时刻（`now >= admit_at` 即放行）。"""
        return self.tat - self.burst_sec(burst)

    def try_acquire(self, now, burst=None):
        """TAT 推进成功即放行；失败**不动状态**（等价于"等 retry_after 秒"）。"""
        if now < self.admit_at(burst):
            return False
        self.tat = max(now, self.tat) + self.interval
        return True

    def retry_after(self, now, burst=None):
        """放行需等多少秒（阻塞替代品：调用方 sleep 它之后下一次必放行）。"""
        return max(0.0, self.admit_at(burst) - now)

    def wait_sec(self, now, burst=None):
        """桶侧同一量：`max(0, tat − burst_sec − now)`。"""
        return self.retry_after(now, burst)


class EgressLimiter:
    """按出口聚合的限速器：`{egress: EgressBucket}` + AIMD 自适应 + 半开冷却 + **出口预算均分**。

    三层语义各管一段、别混着读：AIMD 是**信号驱动的事件式**回退/上探（`on_success` /
    `on_risk_signal`），半开是**按时间**结束的冷却窗口（`acquire` 压突发额度），manual 只锁
    上探这只脚、不锁安全回退（`_set_rate` 的 `_ceiling`）。

    **出口预算均分（工单 2cwd 修法 C）**：限速按**出口**计数，而每个执行体是独立进程、各有
    内存桶——持久键改成出口标识只让状态**共行**，不改运行期各进程各吃一份。故每进程只吃出口
    级速率 λ 的 **1/n**（`shares = n` = 同出口执行体数，由 `egress.outlet_executor_count` 数出）：
    子桶 `rate = λ/n`，n 个进程**运行期合计 ≤ λ**（各进程首条仍即时放行，合计 ≤ n 的初始小突发
    属可接受，见下）。`self.rate` / `self.burst` 是**出口级**量（AIMD 与落库的单位）；子桶用
    `share_rate` / `share_burst`（÷n）。

    **tat（令牌位置）不再共享、不落库**：`persist` 只写出口级 `rate`（与 `burst`），`tat` 写 0；
    `restore_from_store` 只装出口级 `rate`，子桶 TAT 从 0 起算（**重启即新鲜令牌** = 一次可接受的
    小突发）。这样多进程后写者胜（D6）只影响同一口径的减半/回升，语义无害。

    **n 是启动快照（F3）**：`shares` 在进程启动时按配置数出，**运行期不得改清单**——改了不重算
    份额。`persist` 每次按当前配置重算 n，与快照不同即**告警一次**（只告警），提示重启执行体。

    **探针不在 n 内（F2）**：探针是窗口外的单发动作，不预留份额；其瞬时放行的份额是 `λ/n`，
    故 `YIBAN_PROBE_TIME` 落在签到窗口内时窗口内该出口瞬时可能超 `λ/n` 一次（`probe` 侧告警）。
    """

    def __init__(self, rate=RATE_DEFAULT, burst=DEFAULT_BURST, on_change=None, manual=False,
                 shares=1):
        self.shares = max(1, int(shares))
        #: **出口级**速率（attempt/s）：AIMD 与落库用它；子桶速率 = 本值 ÷ shares
        self.rate = clamp_rate(rate)
        #: **出口级**突发额度（尝试数）：子桶额度 = 本值 ÷ shares
        self.burst = float(burst)
        self.manual = bool(manual)
        # 速率上限：manual 时是管理员设定值（上探不得越过），否则是模块上限。
        self._ceiling = self.rate if self.manual else RATE_MAX
        self._on_change = on_change
        self._buckets = {}
        self._streak = {}
        self._half_open_until = {}
        #: 份额分母漂移告警只喊一次（F3）：`shares` 是启动快照，运行期改清单不重算份额。
        self._shares_drift_warned = False

    @property
    def share_rate(self):
        """本进程子桶速率 = 出口级 `rate` ÷ `shares`（下限 `SHARE_RATE_FLOOR`，不夹到出口级下界）。"""
        return max(SHARE_RATE_FLOOR, self.rate / self.shares)

    @property
    def share_burst(self):
        """本进程子桶突发额度 = 出口级 `burst` ÷ `shares`（预算均分，含突发）。"""
        return max(0.0, self.burst / self.shares)

    def _new_bucket(self, egress):
        """按**份额**建子桶（`rate = share_rate`、`burst = share_burst`）。

        新建桶的起算速率随 `self.rate` 走：站点级降档把 `self.rate` 粘到出口级下界，故降档后
        冒出来的出口也按降档值起算（熔断不会对新出口静默失效）。
        """
        return EgressBucket(egress, rate=self.share_rate, burst=self.share_burst,
                            rate_min=SHARE_RATE_FLOOR)

    def bucket(self, egress):
        """取该出口的**本进程份额子桶**；首次访问按起算速率（降档后即降档值）建桶。"""
        b = self._buckets.get(egress)
        if b is None:
            b = self._new_bucket(egress)
            self._buckets[egress] = b
        self._streak.setdefault(egress, 0)
        return b

    def is_half_open(self, egress, now):
        """该出口是否处于半开期（风控命中后的冷却，只放单通道探测）。"""
        return now < self._half_open_until.get(egress, 0.0)

    def acquire(self, egress, now):
        """取一次出口额度（走**本进程份额子桶**）。半开期内突发额度压到 1，冷却走完才恢复。"""
        b = self.bucket(egress)
        # 半开只在这里生效：它是按时间结束的冷却，不是"探测一次成功就放行"——窗口内该出口
        # 始终只放单通道（一次侥幸不该立刻换回 6 条并发），HALF_OPEN_SEC 走完才恢复突发额度
        burst = 1.0 if self.is_half_open(egress, now) else None
        return b.try_acquire(now, burst)

    def on_success(self, egress):
        """连续 `SUCCESS_STREAK` 次无风控 → **出口级** `rate ×= 1.2`（封顶 `_ceiling`），
        返回生效后的出口级 rate。子桶份额随 `self.rate` 同步更新。

        半开期内的成功只算"这一次没被拦"（进 streak、可触发上探），**不结束半开**。
        """
        self.bucket(egress)
        if self.manual:
            return self.rate  # 人工接管连 streak 都不累计：管理员定的值不该被自适应悄悄抬高
        n = self._streak.get(egress, 0) + 1
        if n < SUCCESS_STREAK:
            self._streak[egress] = n  # 阈值以下只累计不变速：一次侥幸就 ×1.2 会让速率来回抖
            return self.rate
        self._streak[egress] = 0
        return self._set_rate(egress, self.rate * GROWTH_FACTOR, "连续无风控上探")

    def on_risk_signal(self, egress, now):
        """风控信号 → **出口级** `rate ÷= 2`（下限为出口级下界）+ 半开 `HALF_OPEN_SEC`，
        返回新出口级 rate。回退是**安全反应**：人工接管下照做（见 `_set_rate`）。
        """
        self.bucket(egress)
        self._streak[egress] = 0  # 三步次序即语义：先销账，再压通道，最后减速
        self._half_open_until[egress] = now + HALF_OPEN_SEC
        if self.manual:
            # 持久键是**出口标识**（直连归一为 `direct`，否则 scheme://host[:port]，无凭据）：
            # 日志回它的展示形态（口径在 `egress.outlet_label`，见其模块 docstring 的红线）。
            logger.warning("出口 %s 风控信号，速率按安全回退下调（.env 人工接管的上限 "
                           "%.3f attempt/s，%ds 半开单通道探测）",
                           yb_egress.outlet_label(egress), self._ceiling, HALF_OPEN_SEC)
        return self._set_rate(egress, self.rate * SHRINK_FACTOR,
                              "风控信号回退")  # 回退不吃延迟信号：单账号耗时 t≈1.9~3s 近常量，延迟信噪比差

    def downgrade_all(self, now, reason="站点级熔断"):
        """全体出口降档到 `RATE_MIN`（出口级）并进入半开，返回 `{egress: 子桶 rate}`。

        站点级熔断（风控信号率超阈）的**降档入口**：阈值判定与告警归调用方，本方法只做
        降档本身；人工接管不阻止降档（安全反应优先于"速率由管理员定"）。
        """
        self.rate = RATE_MIN  # 粘住的降档：新建桶（份额随 self.rate）也按降档值起算
        for egress in list(self._buckets):
            self._half_open_until[egress] = now + HALF_OPEN_SEC
            self._streak[egress] = 0
            self._set_rate(egress, RATE_MIN, reason)
        return {e: b.rate for e, b in self._buckets.items()}

    def snapshot(self):
        """落库用快照：`{egress: {"rate", "burst", "tat"}}`。`rate` / `burst` 是**出口级**量
        （与 `egress_state` 列同口径）；`tat` 是子桶的本进程令牌位置（只作观测，不落库）。
        """
        return {e: {"rate": self.rate, "burst": self.burst, "tat": b.tat}
                for e, b in self._buckets.items()}

    def persist(self, egress, stamp=None):
        """把**出口级** `rate` / `burst` 落库（调用方按 10s 粒度循环）。失败只告警、不阻断签到。

        `tat` **不落库**（多进程不共享令牌位置，写进去只会互相覆盖）：写 0。口径见类 docstring。
        `stamp` 是**写入时刻的墙钟字符串**（缺省 `clock.ts()`），落进 `updated_at` 供运维看
        "上次落库时刻"；不要把通道循环注入的浮点 `now` 传进来——那是桶的时钟域，与 `updated_at`
        不同域（见 `queue_store.save_egress_state`）。

        落库前先做**份额分母漂移检查**（F3）：`shares` 是启动快照，本方法按当前配置重算一次 n，
        与快照不同即告警一次（**只告警**，不重算份额）。运行期改清单不生效，须重启执行体。
        """
        if egress not in self._buckets:
            return False
        self._warn_if_shares_drifted(egress)
        return queue_store.save_egress_state(egress, self.rate, self.burst, 0.0, stamp)

    def _warn_if_shares_drifted(self, egress):
        """同出口执行体数变了就告警一次（仅告警，不重算份额）。"""
        if self._shares_drift_warned:
            return
        current = yb_egress.outlet_executor_count(egress)
        if current != self.shares:
            self._shares_drift_warned = True
            logger.warning("出口 %s 的同出口执行体数已变（启动 %d → 现在 %d）：份额按启动快照 %d "
                           "均分，运行期改清单不生效；请重启执行体",
                           yb_egress.outlet_label(egress), self.shares, current, self.shares)

    def restore_from_store(self, egress, now=None):
        """从 `egress_state` 装回该出口的**出口级** `rate`（崩溃重启后不"重启即全速"）。

        无记录 / 库不可用 → 保持出厂速率并返回 False（出厂速率不是全速，回退是保守的）。
        只装 `rate`（与 `burst`）：`tat` **不装**——多进程不共享令牌位置，装回别人的 TAT 会
        把本进程误锁（见类 docstring）。故子桶 TAT 从 0 起算。`now` 保留只为调用点签名兼容，
        本实现不用它（不再有跨时钟域的 TAT 要夹）。
        """
        st = queue_store.load_egress_state(egress)
        if not st:
            return False
        self.rate = clamp_rate(st.get("rate"))
        burst = float(st.get("burst") or 0.0)
        if burst > 0:
            self.burst = burst
        self._buckets[egress] = self._new_bucket(egress)
        self._streak.setdefault(egress, 0)
        return True

    def _set_rate(self, egress, rate, reason):
        """改**出口级**速率（夹到 `[出口级下界, _ceiling]`）+ 同步全部子桶份额 + 审计日志/
        变更回调，返回生效后的出口级 rate。

        上限即 `_ceiling`：非人工接管时是模块上限，人工接管时是管理员设定值——安全回退
        可以往下走，但任何路径都不许把速率抬过它。

        自适应决策本身是持久状态：调用方（执行体的 10s 落库循环）负责把它写进
        `egress_state`，本方法只发变更事件——持久化不该发生在每次调整里（写库会拖慢放行）。
        """
        self.bucket(egress)
        old = self.rate
        new = min(clamp_rate(rate), self._ceiling)  # `clamp_rate` 已把速率夹到域下界以上
        self.rate = new
        share = self.share_rate
        for b in self._buckets.values():
            b.rate = share
        if new != old:
            logger.info("出口 %s 速率 %.3f → %.3f attempt/s（%s）",
                        yb_egress.outlet_label(egress), old, new, reason)
            if self._on_change is not None:
                self._on_change(egress, old, new, reason)
        return new


class GlobalLimiter:
    """全局聚合速率上界 Λ（第 2 层；每出口桶是第 1 层）。

    不变量：**本实例**内任意 1s 实际放行的**尝试数** ≤ `⌈Λ⌉`，**与出口数 K 无关**——Λ 存在
    时加出口不再线性放大总速率（取整是 GCRA 的边界语义：`Λ=1.5` 首秒最多 2 条，此后按
    1.5 条/s 摊销）。`lam` 单位 = 账号尝试/s；None/≤0 = 不限（小站不强迫配置）。本层不带突发
    额度：全局多放一条就多一份并发冲击，突发额度属于每出口桶。

    **它不是进程间的上界**：`_tat` 就在实例内存里，每个执行体进程各持一份，故多执行体下
    全站总速率 = 执行体数 × Λ。跨进程共享需另做落库协调（用户 2026-10-01 裁决：不做，
    口径改文档——见 `docs/dev/scheduler-v3.md` 的 M19 节）。

    取值非法（如 `abc`）按"不限"处理，但这个事实不静默：`logger.warning` + `invalid` 置真，
    供接线侧据此决定是否拒绝启动（把 Λ 写错的部署等于没有全局上界，值得让人看见）。空值
    （None / 空串 / 纯空白）是 .env 既有约定的"未配置/关闭"，按不限且不告警、不标非法。
    """

    def __init__(self, lam):
        self.invalid = False
        text = lam.strip() if isinstance(lam, str) else lam
        if text is None or text == "":
            v = 0.0  # 空值=未配置/关闭：按既有约定，不告警也不标非法
        else:
            try:
                v = float(text)
            except (TypeError, ValueError):
                self.invalid = True  # 写错 Λ 等于没有全局上界，必须留下可被接线侧看见的痕迹
                logger.warning("全局速率上界 Λ=%r 非法（非数值），已按不限处理", lam)
                v = 0.0
        self.lam = v if v > 0 else None  # ≤0 归一成 None（不限），下游只需判 is None
        self._tat = 0.0

    def acquire(self, now):
        """放行一次全局额度；`lam` 为 None/≤0 时恒放行（不记账）。"""
        if self.lam is None:
            return True  # 不限：不记账也不阻塞，全局层缺席时出口桶仍在管速率
        if now < self._tat:
            return False  # 只判不改状态：等待由调用方按 1/Λ 重试（同出口桶的 GCRA 口径）
        self._tat = max(now, self._tat) + 1.0 / self.lam
        return True


class AccountGapGate:
    """每账号 GCRA(T=gap, τ≈0) 可选安全件：同账号两次尝试至少间隔 gap 秒。

    与出口令牌桶**并存**（不是替代）：出口桶管平台/出口维度，本门管账号维度——上游是否按
    账号维度看间隔两案都还没验，故保留为可选件；gap 本身不可移除（账号间隔、幂等、到期兜底
    是两种执行形态下的共同底限）。
    """

    def __init__(self, gap_sec, enabled=True):
        self.gap_sec = max(0.0, float(gap_sec))
        self.enabled = bool(enabled)
        self._tat = {}

    def allow(self, phone, now):
        """该账号现在是否已过 gap（**只判不推进**；关闭或 gap=0 时恒 True）。"""
        if not self.enabled or self.gap_sec <= 0:
            return True
        return now >= self._tat.get(phone, 0.0)

    def commit(self, phone, now):
        """推进该账号的 TAT：下一次放行要等到 `now + gap`（**只有真发起尝试才该调**）。"""
        if not self.enabled or self.gap_sec <= 0:
            return  # 与 allow 同一判据，否则关闭态下 commit 会白改状态
        self._tat[phone] = max(now, self._tat.get(phone, 0.0)) + self.gap_sec


def apply_ewma(prev_rate, risk_ratio_hat, r_target, beta=EWMA_BETA):
    """外环一步速率更新（目标跟踪的连续微调），返回新 rate。

    `bucket_rate = clamp(prev × (1 + β·(r̂ − R_target)/R_target), RATE_MIN, RATE_MAX)`，
    单次调整幅度再夹到 ±`MAX_STEP`——一次抖动不该把速率打到下限，宁可慢调。
    `risk_ratio_hat` 的 EWMA 平滑**在调用侧做**（α 见 `EWMA_ALPHA`）：本函数只吃平滑后的 r̂，
    故不收 α——α 的作用点在采样，不在这一步更新。

    与 AIMD 的分工与次序（调用方必须照此排）：先处理 AIMD 事件、再跑本函数，且只在两次尝试
    之间调整（单次尝试中途变速率会让半程限速的状态不一致）；人工接管的出口不再调用本函数。
    """
    prev = clamp_rate(prev_rate)
    if not r_target or float(r_target) <= 0:
        return prev
    target = prev * (1.0 + beta * (risk_ratio_hat - r_target) / float(r_target))
    step = MAX_STEP * prev
    return clamp_rate(min(max(clamp_rate(target), prev - step), prev + step))


def burst_cap(rate, gap_sec, channels):
    """把通道数收口成突发额度：`max(1, min(channels, 1 + gap_sec × rate))`。

    突发额度换算成时间是 τ=(burst−1)·T，即**桶允许超前发放的时间**。`gap_sec` 是相邻两次
    尝试的最小间隔（`YIBAN_MIN_EXEC_GAP` 的旧语义）：一次突发最多吃掉一个最小间隔的时间
    预算，即 τ ≤ gap ⇒ burst ≤ 1 + gap × rate。按名册缺省（gap 秒、rate 次尝试/s）恰好得
    6 = 通道数 M；收紧 gap 时突发随之收紧，"一次放几条"确实由这个键管住。
    """
    return max(1.0, min(float(channels or 0.0), 1.0 + float(gap_sec) * float(rate)))


def burst_from_env(channels, rate):
    """`burst_cap` 的环境口径：`gap_sec` 取 `YIBAN_MIN_EXEC_GAP`（缺省住在名册里，域同源）。

    键的读取口径复用 `schedule._env_int`（与 `_schedule_config` 的既有读取同一套回退与
    范围校验），`channels` 由调用方按通道数公式算好传入——本模块不自算通道数。
    """
    from yiban.engine import schedule
    gap = schedule._env_int(ENV_MIN_EXEC_GAP, DEFAULT_MIN_EXEC_GAP_SEC, 1, 60)
    return burst_cap(rate, gap, channels)


def limiter_from_env(channels=DEFAULT_BURST, on_change=None, shares=1):
    """按环境配置造出口限速器：速率 + 突发额度一次读全（配置面收口的入口）。

    - `rate` = `YIBAN_EGRESS_RATE`（**出口级** attempt/s，缺省住在名册里）——经
      `schedule.planner_config` 取，本模块不另立键名字面量；
    - `burst` = `burst_from_env(channels, rate)`（`YIBAN_MIN_EXEC_GAP` 收口，出口级）；
    - `channels` 缺省按出厂速率下的通道数（`DEFAULT_BURST`）；执行体按
      `M = min(16, ceil(rate × avg × 2))` 算好自己的通道数传入；
    - `shares` = **同出口执行体数 n**（`egress.outlet_executor_count` 数出）：本进程子桶速率
      = `rate / n`，n 个进程运行期合计 ≤ λ（出口预算均分，见 `EgressLimiter`）；
    - `manual` = 该键**是否被显式写入**（`schedule.egress_rate_explicit`）：写了即
      人工接管，该值成为速率**上限**（上探不生效），风控回退等安全反应照做
      （见 `EgressLimiter`）。
    """
    from yiban.engine import schedule
    cfg = schedule.planner_config()
    rate = cfg["bucket_rate"]
    return EgressLimiter(rate=rate, burst=burst_from_env(channels, rate),
                         on_change=on_change, manual=schedule.egress_rate_explicit(),
                         shares=shares)


def gap_gate_from_env():
    """按环境配置造每账号 gap 门。

    gap = `YIBAN_ACCOUNT_GAP_MAX`（缺省住在名册里，与 `capacity_accounts` 的 gap 入参、执行体读的是
    同一个键）；enabled 由 `YIBAN_ACCOUNT_GAP_ENFORCE` 决定，键语义是"缺省开"，故真值判据
    分四档、次序不可换（见下）。
    """
    # 局部导入：配置解析口径复用 schedule（避免第二套环境解析），且 schedule 反向引用
    # 本模块的可能性随执行体接线增大，模块级互引会成环。
    from yiban.engine import schedule
    gap = schedule._env_int(ENV_ACCOUNT_GAP_MAX, DEFAULT_ACCOUNT_GAP_SEC, 0, 3600)
    raw = str(os.environ.get(ENV_GAP_ENFORCE, "")).strip()
    if not raw:
        enabled = True  # ① 未设 / 空白 = 缺省开
    elif raw.lower() in _FALSY_LITERALS:
        enabled = False  # ② 显式假值字面量 = 关：这是唯一能关掉安全件的入口，故必须排在真值判之前
    elif schedule._env_flag(ENV_GAP_ENFORCE):
        enabled = True  # ③ 显式真值字面量 = 开
    else:
        # ④ 其余手写错值（如 abc）：错值不等于"关闭"。把安全件因一次笔误静默摘掉是 fail-open
        # 方向，宁可多一层间隔并让人看见这条告警
        logger.warning("配置 %s=%r 非法（既非真值也非假值），安全件按缺省开处理",
                       ENV_GAP_ENFORCE, raw)
        enabled = True
    return AccountGapGate(gap_sec=gap, enabled=enabled)
