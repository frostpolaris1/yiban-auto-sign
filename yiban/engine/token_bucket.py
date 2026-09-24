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
- `apply_ewma`：外环一步速率更新（平滑系数归调用侧）；
- 配置面收口：`limiter_from_env`（速率 + 突发额度一次读全，并在显式配速率时标记
  人工接管）、`burst_from_env` / `burst_cap`（`YIBAN_MIN_EXEC_GAP` → 突发额度）、
  `gap_gate_from_env`（gap 门）。

**通信**
输入：`now`（浮点秒，**必须由调用方注入**，测试不依赖真实时钟）、出口标识、风控信号；
输出：`acquire(...) -> bool` 与 `retry_after(...) -> float`（调用方据此 sleep）。
落库：`egress_state(egress, rate, burst, tat, updated_at)`，经 `queue_store.
load_egress_state` / `save_egress_state`——本模块**唯一**的持久化路径，10s 粒度由
调用方循环，写失败只告警不阻断签到。配置面只有三个旋钮：每出口目标速率
`YIBAN_EGRESS_RATE`（attempt/s，缺省 1.0，**值只由 `schedule.planner_config` 读**，
本模块经 `limiter_from_env` 取用而不另立字面量；该键**是否被显式写入**由
`schedule.egress_rate_explicit` 回答，用来判人工接管）、`YIBAN_MIN_EXEC_GAP`
（突发额度收口，见 `burst_from_env`）、`YIBAN_ACCOUNT_GAP_MAX` /
`YIBAN_ACCOUNT_GAP_ENFORCE`（gap 门）。
调用谁：`yiban.store.queue_store`。谁调用：执行体（通道循环取额度、回报 `on_success` /
`on_risk_signal`、10s 循环 `persist`；站点级熔断用 `downgrade_all`）。
唯一生产入口是 `executor_v3`，它受 `YIBAN_SCHEDULER_V3` 分流、**缺省 0**：开关未开时
本模块在产线零调用，`egress_state` 表也不会被写。
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
#: `YIBAN_ACCOUNT_GAP_MAX` 缺省值（秒）
DEFAULT_ACCOUNT_GAP_SEC = 10
#: 最小执行间隔的键（秒）：旧语义是"相邻两次尝试的最小间隔"（压缩模式防请求过密），
#: 在令牌桶形态下由 `burst_from_env` 收口进突发额度，读取点仍在 schedule
ENV_MIN_EXEC_GAP = "YIBAN_MIN_EXEC_GAP"
#: `YIBAN_MIN_EXEC_GAP` 缺省值（秒）：`RATE_MIN = 0.2` 正是它的倒数——最慢时一条尝试
#: 占满一个最小间隔，同一个物理量的两种写法。
DEFAULT_MIN_EXEC_GAP_SEC = 5
#: 开关类环境变量的假值字面量（与 `schedule._env_flag` 的真值表互补）：写这些值才是
#: "显式关闭"；既非真值也非假值的手写错值另有归属，见 `gap_gate_from_env`。
_FALSY_LITERALS = ("0", "false", "off", "no")


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
    """按出口聚合的限速器：`{egress: EgressBucket}` + AIMD 自适应 + 半开冷却。

    三层语义各管一段、别混着读：AIMD 是**信号驱动的事件式**回退/上探（`on_success` /
    `on_risk_signal`），半开是**按时间**结束的冷却窗口（`acquire` 压突发额度），manual 只锁
    上探这只脚、不锁安全回退（`_set_rate` 的 `_ceiling`）。每出口独立，互不影响。
    """

    def __init__(self, rate=RATE_DEFAULT, burst=DEFAULT_BURST, on_change=None, manual=False):
        self.rate = _clamp(rate)
        self.burst = float(burst)
        self.manual = bool(manual)
        # 速率上限：manual 时是管理员设定值（上探不得越过），否则是模块上限。
        self._ceiling = self.rate if self.manual else RATE_MAX
        self._on_change = on_change
        # 新建桶的起算速率：站点级降档把它粘到降档值（见 downgrade_all），此后冒出来的
        # 出口不再按出厂速率放行——只降已建桶会让熔断对新出口静默失效。
        self._baseline_rate = self.rate
        self._buckets = {}
        self._streak = {}
        self._half_open_until = {}

    def bucket(self, egress):
        """取该出口的桶；首次访问按本限速器的**起算速率**建桶（降档后即降档值）。"""
        b = self._buckets.get(egress)
        if b is None:
            b = EgressBucket(egress, rate=self._baseline_rate, burst=self.burst)
            self._buckets[egress] = b
        self._streak.setdefault(egress, 0)
        return b

    def is_half_open(self, egress, now):
        """该出口是否处于半开期（风控命中后的冷却，只放单通道探测）。"""
        return now < self._half_open_until.get(egress, 0.0)

    def acquire(self, egress, now):
        """取一次出口额度。半开期内突发额度压到 1（探测通道），冷却走完才恢复。"""
        b = self.bucket(egress)
        # 半开只在这里生效：它是按时间结束的冷却，不是"探测一次成功就放行"——窗口内该出口
        # 始终只放单通道（一次侥幸不该立刻换回 6 条并发），HALF_OPEN_SEC 走完才恢复突发额度
        burst = 1.0 if self.is_half_open(egress, now) else None
        return b.try_acquire(now, burst)

    def on_success(self, egress):
        """连续 `SUCCESS_STREAK` 次无风控 → `rate ×= 1.2`（封顶 `_ceiling`），返回生效后 rate。

        半开期内的成功只算"这一次没被拦"（进 streak、可触发上探），**不结束半开**。
        """
        b = self.bucket(egress)
        if self.manual:
            return b.rate  # 人工接管连 streak 都不累计：管理员定的值不该被自适应悄悄抬高
        n = self._streak.get(egress, 0) + 1
        if n < SUCCESS_STREAK:
            self._streak[egress] = n  # 阈值以下只累计不变速：一次侥幸就 ×1.2 会让速率来回抖
            return b.rate
        self._streak[egress] = 0
        return self._set_rate(egress, b.rate * GROWTH_FACTOR, "连续无风控上探")

    def on_risk_signal(self, egress, now):
        """风控信号 → `rate ÷= 2`（下限 `RATE_MIN`）+ 半开 `HALF_OPEN_SEC`，返回新 rate。

        回退是**安全反应**：人工接管下照做，只夹在不超过上限（见 `_set_rate`）。
        """
        self.bucket(egress)
        self._streak[egress] = 0  # 三步次序即语义：先销账，再压通道，最后减速
        self._half_open_until[egress] = now + HALF_OPEN_SEC
        if self.manual:
            logger.warning("出口 %s 风控信号，速率按安全回退下调（.env 人工接管的上限 "
                           "%.3f attempt/s，%ds 半开单通道探测）",
                           egress, self._ceiling, HALF_OPEN_SEC)
        return self._set_rate(egress, self._buckets[egress].rate * SHRINK_FACTOR,
                              "风控信号回退")  # 回退不吃延迟信号：单账号耗时 t≈1.9~3s 近常量，延迟信噪比差

    def downgrade_all(self, now, reason="站点级熔断"):
        """全体出口降档到 `RATE_MIN` 并进入半开，返回 `{egress: rate}`。

        站点级熔断（风控信号率超阈）的**降档入口**：阈值判定与告警归调用方，本方法只做
        降档本身；人工接管不阻止降档（安全反应优先于"速率由管理员定"）。
        """
        self._baseline_rate = RATE_MIN  # 降档是粘住的：新建桶也按降档值起算，否则熔断对后到的出口静默失效
        for egress in list(self._buckets):
            self._half_open_until[egress] = now + HALF_OPEN_SEC
            self._streak[egress] = 0
            self._set_rate(egress, RATE_MIN, reason)
        return {e: b.rate for e, b in self._buckets.items()}

    def snapshot(self):
        """落库用快照：`{egress: {"rate", "burst", "tat"}}`（与 `egress_state` 列同口径）。"""
        return {e: {"rate": b.rate, "burst": b.burst, "tat": b.tat}
                for e, b in self._buckets.items()}

    def persist(self, egress, stamp=None):
        """把该出口的桶状态落库（调用方按 10s 粒度循环）。失败只告警、不阻断签到。

        `stamp` 是**写入时刻的墙钟字符串**（缺省 `clock.ts()`），落进 `updated_at` 供运维
        看"上次落库时刻"。不要把通道循环注入的浮点 `now` 传进来——那是桶的时钟域，与
        `updated_at` 不同域（见 `queue_store.save_egress_state`）。
        """
        b = self._buckets.get(egress)
        if b is None:
            return False
        return queue_store.save_egress_state(egress, b.rate, b.burst, b.tat, stamp)

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
        """改速率（夹到上下限与速率上限）+ 审计日志/变更回调，返回生效后的 rate。

        上限即 `_ceiling`：非人工接管时是模块上限，人工接管时是管理员设定值——安全回退
        可以往下走，但任何路径都不许把速率抬过它。

        自适应决策本身是持久状态：调用方（执行体的 10s 落库循环）负责把它写进
        `egress_state`，本方法只发变更事件——持久化不该发生在每次调整里（写库会拖慢放行）。
        """
        b = self.bucket(egress)
        old, new = b.rate, min(max(_clamp(rate), RATE_MIN), self._ceiling)
        b.rate = new
        if new != old:
            logger.info("出口 %s 速率 %.3f → %.3f attempt/s（%s）", egress, old, new, reason)
            if self._on_change is not None:
                self._on_change(egress, old, new, reason)
        return new


class GlobalLimiter:
    """全局聚合速率上界 Λ（第 2 层；每出口桶是第 1 层）。

    不变量：任意 1s 内实际放行的**尝试数** ≤ `⌈Λ⌉`，**与出口数 K、执行体数无关**——Λ 存在
    时加出口不再线性放大总速率（取整是 GCRA 的边界语义：`Λ=1.5` 首秒最多 2 条，此后按
    1.5 条/s 摊销）。`lam` 单位 = 账号尝试/s；None/≤0 = 不限（小站不强迫配置）。本层不带突发
    额度：全局多放一条就多一份并发冲击，突发额度属于每出口桶。

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
    prev = _clamp(prev_rate)
    if not r_target or float(r_target) <= 0:
        return prev
    target = prev * (1.0 + beta * (risk_ratio_hat - r_target) / float(r_target))
    step = MAX_STEP * prev
    return _clamp(min(max(_clamp(target), prev - step), prev + step))


def burst_cap(rate, gap_sec, channels):
    """把通道数收口成突发额度：`max(1, min(channels, 1 + gap_sec × rate))`。

    突发额度换算成时间是 τ=(burst−1)·T，即**桶允许超前发放的时间**。`gap_sec` 是相邻两次
    尝试的最小间隔（`YIBAN_MIN_EXEC_GAP` 的旧语义）：一次突发最多吃掉一个最小间隔的时间
    预算，即 τ ≤ gap ⇒ burst ≤ 1 + gap × rate。缺省（gap=5s、rate=1）恰好得 6 = 通道数 M；
    收紧 gap（如 1s）时突发随之收紧到 2，"一次放几条"确实由这个键管住。
    """
    return max(1.0, min(float(channels or 0.0), 1.0 + float(gap_sec) * float(rate)))


def burst_from_env(channels, rate):
    """`burst_cap` 的环境口径：`gap_sec` 取 `YIBAN_MIN_EXEC_GAP`（缺省 5s，夹 1~60）。

    键的读取口径复用 `schedule._env_int`（与 `_schedule_config` 的既有读取同一套回退与
    范围校验），`channels` 由调用方按通道数公式算好传入——本模块不自算通道数。
    """
    from yiban.engine import schedule
    gap = schedule._env_int(ENV_MIN_EXEC_GAP, DEFAULT_MIN_EXEC_GAP_SEC, 1, 60)
    return burst_cap(rate, gap, channels)


def limiter_from_env(channels=DEFAULT_BURST, on_change=None):
    """按环境配置造出口限速器：速率 + 突发额度一次读全（配置面收口的入口）。

    - `rate` = `YIBAN_EGRESS_RATE`（attempt/s，缺省 1.0）——经 `schedule.planner_config`
      取，本模块不另立键名字面量；
    - `burst` = `burst_from_env(channels, rate)`（`YIBAN_MIN_EXEC_GAP` 收口）；
    - `channels` 缺省按出厂速率下的通道数（`DEFAULT_BURST`）；执行体按
      `M = min(16, ceil(rate × avg × 2))` 算好自己的通道数传入；
    - `manual` = 该键**是否被显式写入**（`schedule.egress_rate_explicit`）：写了即
      人工接管，该值成为速率**上限**（上探不生效），风控回退等安全反应照做
      （见 `EgressLimiter`）。
    """
    from yiban.engine import schedule
    cfg = schedule.planner_config()
    rate = cfg["bucket_rate"]
    return EgressLimiter(rate=rate, burst=burst_from_env(channels, rate),
                         on_change=on_change, manual=schedule.egress_rate_explicit())


def gap_gate_from_env():
    """按环境配置造每账号 gap 门。

    gap = `YIBAN_ACCOUNT_GAP_MAX`（缺省 10s，与 `capacity_accounts` 的 gap 入参、执行体读的是
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
