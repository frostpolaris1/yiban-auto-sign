// Package rate 是出口限速的唯一定义。
//
// # 功能
// 定义单出口的 GCRA 令牌桶与按出口聚合的限速器。
// 本包**不**发请求、**不**探测出口、**不**落库、**不**做 AIMD 自适应。
//
// # 归属
// core 层纯领域包。风控按出口 IP 计数，故限速粒度是"每出口"；
// worker 池在放行前向本包取额度。
//
// # 复用
// Bucket、Limiter、New、NewLimiter，以及常量 RateMin、RateMax、DefaultRate。
//
// # 口径
// GCRA：tat = max(now, tat) + T，放行判据 now >= tat − (burst−1)·T。
// max(now, …) 使额度**不累积**：长期空闲只放 burst 条，不攒出一次放行上千条。
// 失败不动状态（调用方按 RetryAfter 等待后必放行）。
// 速率域 [RateMin, RateMax]，非法输入回退出厂速率 DefaultRate。
// burst = 0/1 都是严格 1/T 间隔（τ = (burst−1)·T ≤ 0）。
// 速率单位是**账号尝试/s**（单账号 ≈ 6 次 HTTP 请求），不是请求数。
package rate

import (
	"math"
	"sync"
	"time"
)

// 速率域常量（attempt/s）。RateMin 与旧名册最小执行间隔 5s 互为倒数。
const (
	RateMin     = 0.2
	RateMax     = 4.0
	DefaultRate = 1.0
)

// Bucket 是单出口的 GCRA 桶。并发安全：一把锁保护 tat。
type Bucket struct {
	mu     sync.Mutex
	egress string
	rate   float64
	burst  float64
	tat    float64 // 秒域；0 起算（重启即新鲜额度）
}

// New 构造单出口桶，速率夹进域内。
func New(egress string, rate, burst float64) *Bucket {
	return &Bucket{egress: egress, rate: clampRate(rate), burst: max(0, burst)}
}

func clampRate(rate float64) float64 {
	if rate <= 0 || rate != rate { // 非正或 NaN 回缺省
		return DefaultRate
	}
	return min(max(rate, RateMin), RateMax)
}

// interval 是 T = 1/rate（秒/尝试）。
func (b *Bucket) interval() float64 {
	return 1.0 / b.rate
}

// burstSec 是突发额度换算的时间：τ = (burst−1)·T。
func (b *Bucket) burstSec() float64 {
	return max(0, b.burst-1) * b.interval()
}

// Admit 报告 now 是否可放行；可放行则推进 tat，否则不动状态。
func (b *Bucket) Admit(now time.Time) bool {
	b.mu.Lock()
	defer b.mu.Unlock()
	t := float64(now.UnixMilli()) / 1e3 // 秒域（毫秒精度）
	if t < b.tat-b.burstSec() {
		return false
	}
	b.tat = max(t, b.tat) + b.interval()
	return true
}

// RetryAfter 返回距下一次可放行的等待秒；已可放行返回 0。
// 等待量**向上取整到整毫秒**：tat 以毫秒精度推进，若按纳秒截断，
// 调用方 sleep 返回值后仍差 1ns 被拒——违反"等待后必放行"（审查 P2-1）。
func (b *Bucket) RetryAfter(now time.Time) time.Duration {
	b.mu.Lock()
	defer b.mu.Unlock()
	t := float64(now.UnixMilli()) / 1e3
	waitMs := math.Ceil(max(0, b.tat-b.burstSec()-t) * 1e3)
	return time.Duration(waitMs) * time.Millisecond
}

// Limiter 是按出口聚合的限速器：每出口一个独立桶，互不串额度。
type Limiter struct {
	mu      sync.Mutex
	buckets map[string]*Bucket
	rate    float64
	burst   float64
}

// NewLimiter 构造聚合限速器：新出口按同一 rate/burst 起桶。
func NewLimiter(rate, burst float64) *Limiter {
	return &Limiter{buckets: map[string]*Bucket{}, rate: rate, burst: burst}
}

// Admit 取该出口的一次额度；出口无桶时按聚合参数现建。
func (l *Limiter) Admit(egress string, now time.Time) bool {
	l.mu.Lock()
	b := l.buckets[egress]
	if b == nil {
		b = New(egress, l.rate, l.burst)
		l.buckets[egress] = b
	}
	l.mu.Unlock()
	return b.Admit(now)
}

// RetryAfter 返回该出口距下一次可放行的等待；无桶或可放行返回 0。
// 与 Admit 同一把锁秩序：先取桶引用再问桶，避免持有聚合锁做算术。
func (l *Limiter) RetryAfter(egress string, now time.Time) time.Duration {
	l.mu.Lock()
	b := l.buckets[egress]
	l.mu.Unlock()
	if b == nil {
		return 0
	}
	return b.RetryAfter(now)
}
