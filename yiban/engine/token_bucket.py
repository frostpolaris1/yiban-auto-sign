# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""**功能**
出口限速件：按出口整形的令牌桶（GCRA/TAT，不累积额度）+ AIMD 自适应 + 全局聚合
速率上界 Λ + 每账号间隔安全件 + EWMA 外环微调。

**单位（全模块唯一口径）**：速率一律是**账号尝试/s**（attempt/s）。本项目单账号
= 6 次 HTTP 请求（登录链 4 + 定位 + 签到），故 `rate = 1` 的语义是 1 次尝试/s
≈ 6 次实际请求/s；常量、日志文案与 `egress_state.rate` 都是这个单位。按字面
"1 req/s" 实现会让实际请求量只有设计的 1/6。

**归属**
`yiban.engine` 的执行层限速件（调度 v3）：计划层决定"何时做"，本模块决定"多快做"。
风控是**外生约束**且按出口 IP 计数，故桶的粒度是"每出口"；全局 Λ 是附加上界而非替代
——Λ 存在时加出口/加进程不再线性放大总速率（否则运维会用"加进程"解吞吐问题）。

**复用**
- `EgressBucket`：单出口 GCRA 桶（纯状态机，注入 `now` 即可测）；
- `EgressLimiter`：`{egress: EgressBucket}` + AIMD/半开 + `snapshot` / `persist` /
  `restore_from_store`（落库往返与崩溃重启恢复）+ `downgrade_all`（全体降档入口）；
- `GlobalLimiter`：Λ 上界；`AccountGapGate`：每账号 gap 安全件；
- `apply_ewma`：外环一步速率更新；`gap_gate_from_env`：gap 门的环境配置口径。

**通信**
输入：`now`（浮点秒，**必须由调用方注入**，测试不依赖真实时钟）、出口标识、风控信号；
输出：`acquire(...) -> bool` 与 `retry_after(...) -> float`（调用方据此 sleep）。
落库：`egress_state(egress, rate, burst, tat, updated_at)`，经 `queue_store.
load_egress_state` / `save_egress_state`——本模块**唯一**的持久化路径，10s 粒度由
调用方循环，写失败只告警不阻断签到。配置面收口为"每出口目标速率"
`YIBAN_EGRESS_RATE`（attempt/s，缺省 1.0，经 `schedule.planner_config` 读）；每账号
gap 沿用 `YIBAN_ACCOUNT_GAP_MAX`（见 `gap_gate_from_env`）。调用谁：`yiban.store.
queue_store`。谁调用：执行体（通道循环取额度、回报 `on_success` / `on_risk_signal`、
10s 循环 `persist`；站点级熔断用 `downgrade_all`）。
"""
import logging
import os

from yiban.store import queue_store

logger = logging.getLogger("yiban.engine.token_bucket")

#: AIMD 下限（attempt/s）：与 `YIBAN_MIN_EXEC_GAP` 缺省 5s 同源（1/5 = 0.2）。
RATE_MIN = 0.2
#: AIMD 上限（attempt/s）
RATE_MAX = 4.0
#: 出厂速率（attempt/s）：单账号 = 6 次 HTTP 请求，故 ≈ 6 请求/s
RATE_DEFAULT = 1.0
#: 无风控信号连续尝试数才加性探测（每满一轮 ×GROWTH_FACTOR）
SUCCESS_STREAK = 200
#: 风控命中后该出口的半开时长（秒）
HALF_OPEN_SEC = 300
#: 风控信号率 EWMA 的平滑系数（半衰期 ≈ 3 次采样）；口径归调用侧，见 `apply_ewma`
EWMA_ALPHA = 0.2
#: 外环目标跟踪的增益
EWMA_BETA = 0.5
#: 单次调整幅度上限（±20%）：防一次抖动把速率打到下限，宁可慢调
MAX_STEP = 0.20
#: AIMD 乘性步长：无风控上探 / 风控回退
GROWTH_FACTOR = 1.2
SHRINK_FACTOR = 0.5
#: 突发额度缺省 = 通道数 M（单出口 1 attempt/s、单账号 3s → 6）。执行体按
#: `M = min(16, ceil(rate × avg × 2))` 算好传入，本模块不自算通道数（口径唯一）。
DEFAULT_BURST = 6
#: 每账号 gap 的键：沿用既有键名（`capacity_accounts` 的 gap 入参同源）
ENV_ACCOUNT_GAP_MAX = "YIBAN_ACCOUNT_GAP_MAX"
#: gap 安全件的开关键（缺省 1=开）：上游是否按账号维度看间隔尚未实测裁决
ENV_GAP_ENFORCE = "YIBAN_ACCOUNT_GAP_ENFORCE"
#: `YIBAN_ACCOUNT_GAP_MAX` 缺省值（秒）
DEFAULT_ACCOUNT_GAP_SEC = 10
#: 每出口目标速率键（attempt/s）：本模块**不自算**它，由 `schedule.planner_config()`
#: 统一解析后传给 `EgressLimiter(rate=...)`——配置面只有这一个旋钮。
EGRESS_RATE_ENV = "YIBAN_EGRESS_RATE"


def _clamp(rate):
    """速率夹到 `[RATE_MIN, RATE_MAX]`（attempt/s）；非法输入回退出厂速率。"""
    try:
        v = float(rate)
    except (TypeError, ValueError):
        return RATE_DEFAULT
    return min(max(v, RATE_MIN), RATE_MAX)


class EgressBucket:
    """单出口令牌桶（GCRA/TAT 实现，**不累积额度**）。

    `rate` 单位是**账号尝试/s**（attempt/s），T = 1/rate。`burst` 是突发额度（尝试数）：
    容量为 burst 的令牌桶在 GCRA 下等价于容差 τ = (burst−1)·T（burst=0 ⇒ τ=0，即严格
    1/T 间隔）。放行判据 `now >= tat − τ`，放行后 `tat = max(now, tat) + T`——`max(now, …)`
    是**不累积**的关键：长期空闲后只放行 burst 条，不会因"积攒"一次放行上千条
    （固定窗口边界突发的反面）。
    """

    def __init__(self, egress, rate=RATE_DEFAULT, burst=DEFAULT_BURST, tat=0.0):
        self.egress = egress
        self.rate = _clamp(rate)
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
    """按出口聚合的限速器：`{egress: EgressBucket}` + AIMD 自适应 + 半开。

    AIMD 是**信号驱动的事件式**回退/上探（每出口独立）：连续 `SUCCESS_STREAK` 次无风控
    → `rate ×= 1.2`（封顶 `RATE_MAX`）；风控信号（e003 / WAF / 验证码 / 被拦页）→
    `rate ÷= 2`（下限 `RATE_MIN`）且该出口半开 `HALF_OPEN_SEC`。计数器是"连续"的：
    任何风控信号清零成功 streak。回退信号**不用延迟**——本项目单账号耗时 t≈1.87~3s
    近常量，延迟触顶信噪比差，用它回退等于按抖动误伤。
    """

    def __init__(self, rate=RATE_DEFAULT, burst=DEFAULT_BURST, on_change=None):
        self.rate = _clamp(rate)
        self.burst = float(burst)
        self._on_change = on_change
        self._buckets = {}
        self._streak = {}
        self._half_open_until = {}

    def bucket(self, egress):
        """取该出口的桶；首次访问按本限速器的出厂速率建桶（`rate` 参数）。"""
        b = self._buckets.get(egress)
        if b is None:
            b = EgressBucket(egress, rate=self.rate, burst=self.burst)
            self._buckets[egress] = b
        self._streak.setdefault(egress, 0)
        return b

    def is_half_open(self, egress, now):
        """该出口是否处于半开期（风控命中后的冷却，只放单通道探测）。"""
        return now < self._half_open_until.get(egress, 0.0)

    def acquire(self, egress, now):
        """取一次出口额度。半开期内突发额度压到 1（探测通道）。"""
        b = self.bucket(egress)
        burst = 1.0 if self.is_half_open(egress, now) else None
        return b.try_acquire(now, burst)

    def on_success(self, egress):
        """连续 `SUCCESS_STREAK` 次无风控 → `rate ×= 1.2`（封顶），返回生效后的 rate。

        半开期内探测成功即结束半开（熔断器的半开语义：探测通过=恢复正常），但速率仍停在
        回退后的值，由 streak 重新累计上探——恢复靠"再攒 200 次"，不靠一次侥幸。
        """
        b = self.bucket(egress)
        self._half_open_until.pop(egress, None)
        n = self._streak.get(egress, 0) + 1
        if n < SUCCESS_STREAK:
            self._streak[egress] = n
            return b.rate
        self._streak[egress] = 0
        return self._set_rate(egress, b.rate * GROWTH_FACTOR, "连续无风控上探")

    def on_risk_signal(self, egress, now):
        """风控信号 → `rate ÷= 2`（下限 `RATE_MIN`）+ 半开 `HALF_OPEN_SEC`，返回新 rate。"""
        self.bucket(egress)
        self._streak[egress] = 0
        self._half_open_until[egress] = now + HALF_OPEN_SEC
        return self._set_rate(egress, self._buckets[egress].rate * SHRINK_FACTOR,
                              "风控信号回退")

    def downgrade_all(self, now, reason="站点级熔断"):
        """全体出口降档到 `RATE_MIN` 并进入半开，返回 `{egress: rate}`。

        站点级熔断（风控信号率超阈）的**降档入口**：阈值判定与告警归调用方，本方法只做
        降档本身——已建桶的出口逐一下调，未建桶的出口以本限速器的出厂速率起算（尚未
        放行过，谈不上降档）。
        """
        for egress in list(self._buckets):
            self._half_open_until[egress] = now + HALF_OPEN_SEC
            self._streak[egress] = 0
            self._set_rate(egress, RATE_MIN, reason)
        return {e: b.rate for e, b in self._buckets.items()}

    def snapshot(self):
        """落库用快照：`{egress: {"rate", "burst", "tat"}}`（与 `egress_state` 列同口径）。"""
        return {e: {"rate": b.rate, "burst": b.burst, "tat": b.tat}
                for e, b in self._buckets.items()}

    def persist(self, egress, now=None):
        """把该出口的桶状态落库（调用方按 10s 粒度循环）。失败只告警、不阻断签到。"""
        b = self._buckets.get(egress)
        if b is None:
            return False
        return queue_store.save_egress_state(egress, b.rate, b.burst, b.tat, now)

    def restore_from_store(self, egress, now=None):
        """从 `egress_state` 装回该出口的速率与 TAT（崩溃重启后不"重启即全速"）。

        无记录 / 库不可用 → 保持出厂速率并返回 False（出厂速率不是全速，回退是保守的）。
        给了 `now` 时把 `tat` 夹到 `now + T`：持久化的 TAT 可能与本次进程的时钟不同域
        （`time.monotonic()` 跨重启归零），超前的 TAT 会把桶误锁很久，而**合法**的 TAT
        不可能超过写入时刻 + T（放行后 `tat = max(now, tat) + T`）。
        """
        st = queue_store.load_egress_state(egress)
        if not st:
            return False
        rate = _clamp(st.get("rate"))
        burst = float(st.get("burst") or 0.0)
        tat = float(st.get("tat") or 0.0)
        if now is not None:
            tat = min(tat, now + 1.0 / rate)
        self._buckets[egress] = EgressBucket(egress, rate=rate, burst=burst, tat=tat)
        self._streak.setdefault(egress, 0)
        return True

    def _set_rate(self, egress, rate, reason):
        """改速率（夹到上下限）+ 审计日志/变更回调，返回生效后的 rate。

        自适应决策本身是持久状态：调用方（执行体的 10s 落库循环）负责把它写进
        `egress_state`，本方法只发变更事件——持久化不该发生在每次调整里（写库会拖慢放行）。
        """
        b = self.bucket(egress)
        old, new = b.rate, _clamp(rate)
        b.rate = new
        if new != old:
            logger.info("出口 %s 速率 %.3f → %.3f attempt/s（%s）", egress, old, new, reason)
            if self._on_change is not None:
                self._on_change(egress, old, new, reason)
        return new


class GlobalLimiter:
    """全局聚合速率上界 Λ（第 2 层；每出口桶是第 1 层）。

    不变量：任意 1s 内实际放行的**尝试数** ≤ Λ（**与出口数 K、执行体数无关**）——Λ 存在
    时加出口不再线性放大总速率。`lam` 单位 = 账号尝试/s；None/≤0 = 不限（小站不强迫配置）。
    本层不带突发额度：全局多放一条就多一份并发冲击，突发额度属于每出口桶。
    """

    def __init__(self, lam):
        try:
            self.lam = float(lam) if lam is not None and float(lam) > 0 else None
        except (TypeError, ValueError):
            self.lam = None
        self._tat = 0.0

    def acquire(self, now):
        """放行一次全局额度；`lam` 为 None/≤0 时恒放行（不记账）。"""
        if self.lam is None:
            return True
        if now < self._tat:
            return False
        self._tat = max(now, self._tat) + 1.0 / self.lam
        return True


class AccountGapGate:
    """每账号 GCRA(T=gap, τ≈0) 可选安全件：同账号两次尝试至少间隔 gap 秒。

    与出口令牌桶**并存**（不是替代）：出口桶管平台/出口维度，本门管账号维度——上游是否
    按账号维度看间隔两案都还没验，故保留为可选件。`allow` 只判不推进、`commit` 才推进
    TAT：调用方可以先判后做（尝试真正发起时才 commit），中途放弃不白占间隔。
    """

    def __init__(self, gap_sec, enabled=True):
        self.gap_sec = max(0.0, float(gap_sec))
        self.enabled = bool(enabled)
        self._tat = {}

    def allow(self, phone, now):
        """该账号现在是否已过 gap（不推进 TAT；关闭或缺省 gap=0 时恒 True）。"""
        if not self.enabled or self.gap_sec <= 0:
            return True
        return now >= self._tat.get(phone, 0.0)

    def commit(self, phone, now):
        """推进该账号的 TAT：下一次放行要等到 `now + gap`。"""
        if not self.enabled or self.gap_sec <= 0:
            return
        self._tat[phone] = max(now, self._tat.get(phone, 0.0)) + self.gap_sec


def apply_ewma(prev_rate, risk_ratio_hat, r_target, alpha=EWMA_ALPHA, beta=EWMA_BETA):
    """外环一步速率更新（目标跟踪的连续微调），返回新 rate。

    `bucket_rate = clamp(prev × (1 + β·(r̂ − R_target)/R_target), RATE_MIN, RATE_MAX)`，
    单次调整幅度再夹到 ±`MAX_STEP`——一次抖动不该把速率打到下限，宁可慢调。
    `risk_ratio_hat` 是风控信号率的 EWMA，平滑（`r̂_t = α·r_t + (1−α)·r̂_{t−1}`）在调用侧
    做，`alpha` 因此只作口径占位：外环与调用侧取同一套 α/β 才可比。

    与 AIMD 的分工：AIMD 是信号驱动的事件式回退/上探，本函数是事件之间的微调；调用方须
    先跑 AIMD 事件、再跑本函数，且**只在两次尝试之间**调整（不在单次尝试中途变速率，
    否则半程限速会让状态不一致）。
    """
    prev = _clamp(prev_rate)
    if not r_target or float(r_target) <= 0:
        return prev
    target = prev * (1.0 + beta * (risk_ratio_hat - r_target) / float(r_target))
    step = MAX_STEP * prev
    return _clamp(min(max(_clamp(target), prev - step), prev + step))


def gap_gate_from_env():
    """按环境配置造每账号 gap 门。

    gap = `YIBAN_ACCOUNT_GAP_MAX`（缺省 10s，与 `capacity_accounts` 的 gap 入参、执行体
    读的是同一个键）；enabled = `YIBAN_ACCOUNT_GAP_ENFORCE` 真值（**缺省 1=开**）——上游
    是否按账号维度看间隔尚未实测裁决，故留开关。gap 本身不可移除（账号间隔、幂等、到期
    兜底是两种形态下的共同底限）。
    """
    # 局部导入：配置解析口径复用 schedule（避免第二套环境解析），且 schedule 反向引用
    # 本模块的可能性随执行体接线增大，模块级互引会成环。
    from yiban.engine import schedule
    gap = schedule._env_int(ENV_ACCOUNT_GAP_MAX, DEFAULT_ACCOUNT_GAP_SEC, 0, 3600)
    raw = str(os.environ.get(ENV_GAP_ENFORCE, "")).strip()
    # 这是"默认开"的键：未设/空 ⇒ 开，显式写入才按真值字面量判——与 `_env_flag`
    # 的"缺省即假"语义相反，故不能直接用它的缺省分支。
    enabled = schedule._env_flag(ENV_GAP_ENFORCE) if raw else True
    return AccountGapGate(gap_sec=gap, enabled=enabled)
