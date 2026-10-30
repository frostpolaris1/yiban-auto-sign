// Package ratelimit 限制入站请求速率。
//
// # 功能
// 按字符串键（IP、账号标识）维护令牌桶，给出放行判据与建议等待；
// 解析可信代理链，决定客户端 IP。本包**不**读请求体、**不**写响应、
// **不**决定哪些路由限流。
//
// # 归属
// entry/http 传输层。路由把端点与键交给本包；账号与会话判据仍由用例层给出。
//
// # 复用
// Quota、Throttle、NewThrottle；Guard、ParseTrustedProxies、ClientIP。
//
// # 口径
// 令牌桶取 golang.org/x/time/rate；时间由注入的时钟给出，测试可用假时钟。
// 空闲桶按 2 倍充满时间回收；键数超过上限时淘汰最久未见者，内存有界。
// 拒绝不放贷：每次拒绝都取消预留，连续打不中不会把等待窗口推远。
package ratelimit

import (
	"errors"
	"fmt"
	"sync"
	"time"

	"golang.org/x/time/rate"
)

// Quota 是一个限流面参数。
// PerMinute 是每分钟补充的令牌数；Burst 是桶容量（缺省 1）。
type Quota struct {
	PerMinute int
	Burst     int
}

// defaultMaxKeys 是单实例的键上限。超过即淘汰最久未见者。
const defaultMaxKeys = 4096

// Throttle 是按键的令牌桶集合。并发安全。
// 零值不可用；请用 NewThrottle 构造。
type Throttle struct {
	mu      sync.Mutex
	limit   rate.Limit
	burst   int
	ttl     time.Duration
	maxKeys int
	calls   uint64
	now     func() time.Time
	buckets map[string]*bucket
}

type bucket struct {
	lim  *rate.Limiter
	seen time.Time
}

// NewThrottle 构造限流器。PerMinute 必须为正：零或负数即配置错误。
// 失败关闭优先于静默全放行。
func NewThrottle(p Quota) (*Throttle, error) {
	if p.PerMinute <= 0 {
		return nil, fmt.Errorf("ratelimit: PerMinute must be positive, got %d", p.PerMinute)
	}
	burst := p.Burst
	if burst <= 0 {
		burst = 1
	}
	// 充满时间 = burst / (PerMinute/60) 秒。空闲 2 倍充满时间即回收。
	fill := time.Duration(float64(time.Minute) * float64(burst) / float64(p.PerMinute))
	ttl := 2 * fill
	if ttl < time.Minute {
		ttl = time.Minute
	}
	return &Throttle{
		limit:   rate.Limit(float64(p.PerMinute) / 60.0),
		burst:   burst,
		ttl:     ttl,
		maxKeys: defaultMaxKeys,
		now:     time.Now,
		buckets: make(map[string]*bucket),
	}, nil
}

// Allow 报告 key 是否可放行；拒绝时给出建议等待。
// nil 接收者视为不限流：测试与未装配路由保持原行为。
func (l *Throttle) Allow(key string) (bool, time.Duration) {
	if l == nil {
		return true, 0
	}
	now := l.now()
	l.mu.Lock()
	defer l.mu.Unlock()
	l.calls++
	if l.calls&0x3ff == 0 || len(l.buckets) >= l.maxKeys {
		l.sweepLocked(now)
	}
	b := l.buckets[key]
	if b == nil {
		if len(l.buckets) >= l.maxKeys {
			l.evictOldestLocked()
		}
		b = &bucket{lim: rate.NewLimiter(l.limit, l.burst)}
		l.buckets[key] = b
	}
	b.seen = now
	res := b.lim.ReserveN(now, 1)
	if !res.OK() {
		// 只有 n > burst 才会失败；本调用恒为 1。走到这里是契约被破坏。
		return false, time.Second
	}
	if delay := res.DelayFrom(now); delay > 0 {
		// 取消预留：拒绝不放贷，桶状态回到拒绝前。
		res.CancelAt(now)
		return false, delay
	}
	return true, 0
}

// sweepLocked 回收空闲桶。调用方持锁。
func (l *Throttle) sweepLocked(now time.Time) {
	for key, b := range l.buckets {
		if now.Sub(b.seen) > l.ttl {
			delete(l.buckets, key)
		}
	}
}

// evictOldestLocked 淘汰最久未见的键。调用方持锁。
// 只在到达上限时发生：O(n) 扫描不进入常规路径。
func (l *Throttle) evictOldestLocked() {
	var oldestKey string
	var oldest time.Time
	for key, b := range l.buckets {
		if oldestKey == "" || b.seen.Before(oldest) {
			oldestKey, oldest = key, b.seen
		}
	}
	if oldestKey != "" {
		delete(l.buckets, oldestKey)
	}
}

// Guard 把两个限流面与可信代理表绑在一起。
// nil Guard 表示不限流；生产装配必须给值。
type Guard struct {
	IP      *Throttle
	Account *Throttle
	Trusted []Prefix
}

// AllowIP 按客户端 IP 判定。
func (g *Guard) AllowIP(remoteAddr string, forwarded []string) (bool, time.Duration) {
	if g == nil {
		return true, 0
	}
	return g.IP.Allow(ClientIP(remoteAddr, forwarded, g.Trusted))
}

// AllowAccount 按账号标识判定。
func (g *Guard) AllowAccount(key string) (bool, time.Duration) {
	if g == nil {
		return true, 0
	}
	return g.Account.Allow(key)
}

// ErrNoTrustedProxy 是可信代理表解析失败的错误前缀。
var ErrNoTrustedProxy = errors.New("ratelimit: invalid trusted proxy")
