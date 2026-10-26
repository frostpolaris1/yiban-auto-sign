package rate

import (
	"sync"
	"testing"
	"time"
)

// GCRA 判据：间隔 1s、无突发 ⇒ 连续两次放行至少差 1s。
// tat = max(now, tat) + T：不累积额度，长期空闲后只多放 burst 条。
func TestBucketStrictInterval(t *testing.T) {
	b := New("direct", 1.0, 0)
	t0 := time.Unix(1000, 0)
	if !b.Admit(t0) {
		t.Fatal("首条应放行")
	}
	if b.Admit(t0.Add(999 * time.Millisecond)) {
		t.Fatal("999ms 后的第二条该被拒")
	}
	if !b.Admit(t0.Add(time.Second)) {
		t.Fatal("恰好 1s 后该放行（闭区间）")
	}
}

func TestBucketBurst(t *testing.T) {
	// burst=3：同一时刻可连放 3 条，第 4 条拒。
	b := New("direct", 1.0, 3)
	t0 := time.Unix(1000, 0)
	for i := 0; i < 3; i++ {
		if !b.Admit(t0) {
			t.Fatalf("突发第 %d 条应放行", i+1)
		}
	}
	if b.Admit(t0) {
		t.Fatal("突发额度用尽后应拒")
	}
	// 长期空闲不攒额度：空闲 100s 后也只能放 burst 条。
	t1 := time.Unix(1100, 0)
	n := 0
	for b.Admit(t1) {
		n++
		if n > 10 {
			t.Fatal("空闲后放出超过 burst 条：额度被累积")
		}
	}
	if n != 3 {
		t.Fatalf("空闲后放行 %d 条, want 3", n)
	}
}

func TestBucketRetryAfter(t *testing.T) {
	b := New("direct", 2.0, 0) // T = 500ms
	t0 := time.Unix(1000, 0)
	b.Admit(t0)
	d := b.RetryAfter(t0.Add(100 * time.Millisecond))
	// 等待量向上取整到整毫秒：按返回值等待后必放行（审查 P2-1 的钉子）。
	t1 := t0.Add(100 * time.Millisecond).Add(d)
	if d < 400*time.Millisecond || d > 401*time.Millisecond {
		t.Fatalf("RetryAfter = %v, want ≈400ms", d)
	}
	if !b.Admit(t1) {
		t.Fatalf("按 RetryAfter(%v) 等待后仍被拒", d)
	}
	if d := b.RetryAfter(t0.Add(time.Second)); d != 0 {
		t.Fatalf("已可放行应返回 0, got %v", d)
	}
}

func TestBucketRateClamp(t *testing.T) {
	// 速率域 [minRate, 4]；非法输入回退出厂速率。
	if got := New("direct", 0, 0).rate; got != DefaultRate {
		t.Fatalf("rate=0 未夹到缺省: %v", got)
	}
	if got := New("direct", 100, 0).rate; got != RateMax {
		t.Fatalf("rate=100 未夹到上限: %v", got)
	}
	if got := New("direct", 0.001, 0).rate; got != RateMin {
		t.Fatalf("rate=0.001 未夹到下限: %v", got)
	}
}

// 并发判据：多 goroutine 同时取额度，放行数必须不超速率允许的总量。
// 锁只在被测的那一维（互斥推进 tat）上等价于单线程语义。
func TestBucketConcurrent(t *testing.T) {
	b := New("direct", 1.0, 0)
	t0 := time.Unix(1000, 0)
	var mu sync.Mutex
	admitted := 0
	var wg sync.WaitGroup
	for i := 0; i < 50; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if b.Admit(t0) {
				mu.Lock()
				admitted++
				mu.Unlock()
			}
		}()
	}
	wg.Wait()
	// 速率 1/s、无突发：同一时刻只有 1 条可过。
	if admitted != 1 {
		t.Fatalf("并发放行 %d 条, want 1", admitted)
	}
}

func TestLimiterPerEgress(t *testing.T) {
	// 两个出口互不串额度：同一条额定速率各管各的。
	l := NewLimiter(1.0, 0)
	t0 := time.Unix(1000, 0)
	if !l.Admit("p1", t0) || !l.Admit("p2", t0) {
		t.Fatal("两个出口的首条都应放行")
	}
	if l.Admit("p1", t0.Add(100*time.Millisecond)) {
		t.Fatal("p1 第二条该被 p1 自己的额度拒")
	}
}

// Limiter.RetryAfter：与 Admit 同一口径——按返回值等待后必放行；
// 未建桶的出口返回 0（首条本就即时放行）。
func TestLimiterRetryAfter(t *testing.T) {
	l := NewLimiter(1.0, 0)
	t0 := time.Unix(1000, 0)
	if d := l.RetryAfter("unknown", t0); d != 0 {
		t.Fatalf("未建桶出口 RetryAfter = %v, want 0", d)
	}
	if !l.Admit("p1", t0) {
		t.Fatal("首条应放行")
	}
	d := l.RetryAfter("p1", t0)
	if d <= 0 {
		t.Fatalf("刚放行后 RetryAfter = %v, want > 0", d)
	}
	if !l.Admit("p1", t0.Add(d)) {
		t.Fatalf("按 RetryAfter(%v) 等待后仍被拒", d)
	}
}
