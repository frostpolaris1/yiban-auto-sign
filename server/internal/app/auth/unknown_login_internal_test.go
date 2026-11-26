package auth

// 未知邮箱审计采样器的内部边界用例（表上限与过期回收）。
// 外部行为由 unknown_login_test.go 覆盖；这里只测不可从包外构造的边界。

import (
	"fmt"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// samplerClock 是可推进的测试时钟。
type samplerClock struct {
	mu sync.Mutex
	t  time.Time
}

func newSamplerClock() *samplerClock {
	return &samplerClock{t: time.Date(2026, 11, 26, 10, 0, 0, 0, time.UTC)}
}

func (c *samplerClock) Now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.t
}

func (c *samplerClock) advance(d time.Duration) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.t = c.t.Add(d)
}

// 同一 IP 的并发放行必须恰好一次；锁外判定会重复放行。
func TestUnknownLoginSamplerConcurrentSameIPAllowsOnce(t *testing.T) {
	s := newUnknownLoginSampler(newSamplerClock())
	var allowed atomic.Int64
	var wg sync.WaitGroup
	for i := 0; i < 64; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if ok, _ := s.allow("203.0.113.77"); ok {
				allowed.Add(1)
			}
		}()
	}
	wg.Wait()
	if got := allowed.Load(); got != 1 {
		t.Fatalf("concurrent allows = %d, want exactly 1", got)
	}
}

// 表满 4096 后新键被抑制且长度不增；过期键会被 sweep 回收。
func TestUnknownLoginSamplerCapsTable(t *testing.T) {
	clk := newSamplerClock()
	s := newUnknownLoginSampler(clk)

	for i := 0; i < unknownLoginAuditMaxEntries; i++ {
		ip := fmt.Sprintf("198.51.%d.%d", i/256, i%256)
		allowed, fingerprint := s.allow(ip)
		if !allowed {
			t.Fatalf("key %d (%s) was rejected before the cap", i, ip)
		}
		if len(fingerprint) != 16 || fingerprint == ip {
			t.Fatalf("fingerprint = %q, want a 16-char hash distinct from %q", fingerprint, ip)
		}
	}
	if got := len(s.seen); got != unknownLoginAuditMaxEntries {
		t.Fatalf("table length = %d, want %d", got, unknownLoginAuditMaxEntries)
	}

	if allowed, _ := s.allow("203.0.113.99"); allowed {
		t.Fatal("a new key was admitted while the table was full")
	}
	if got := len(s.seen); got != unknownLoginAuditMaxEntries {
		t.Fatalf("table length after the rejected key = %d, want %d", got, unknownLoginAuditMaxEntries)
	}

	clk.advance(unknownLoginAuditWindow + time.Second)
	if allowed, _ := s.allow("203.0.113.99"); !allowed {
		t.Fatal("a new key was still rejected after the old keys expired")
	}
	if got := len(s.seen); got != 1 {
		t.Fatalf("table length after the sweep = %d, want 1", got)
	}
	if allowed, _ := s.allow("198.51.0.0"); !allowed {
		t.Fatal("an expired key was not reclaimed after the sweep")
	}
}

// 无 IP 的请求共用一个固定 fallback 桶。
func TestUnknownLoginSamplerFallbackBucket(t *testing.T) {
	s := newUnknownLoginSampler(newSamplerClock())
	allowed, fingerprint := s.allow("")
	if !allowed || fingerprint != "unknown" {
		t.Fatalf("first fallback allow = (%v, %q), want (true, unknown)", allowed, fingerprint)
	}
	if allowed, fingerprint := s.allow(""); allowed || fingerprint != "unknown" {
		t.Fatalf("second fallback allow = (%v, %q), want (false, unknown)", allowed, fingerprint)
	}
	if got := len(s.seen); got != 1 {
		t.Fatalf("table length = %d, want 1", got)
	}
}
