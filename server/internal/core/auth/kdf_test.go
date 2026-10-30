package auth

import (
	"context"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// 并发闸的上限是硬上限：n=2 时任何时刻最多 2 个动作在跑。
// 计数器在 f 内部增减，故峰值直接反映闸的放行度。
func TestBoundedLimitsConcurrency(t *testing.T) {
	const limit = 2
	const callers = 8
	gate := NewBounded(limit)

	var inFlight, peak int64
	var wg sync.WaitGroup
	start := make(chan struct{})
	for i := 0; i < callers; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			<-start
			if err := gate.run(context.Background(), func() error {
				now := atomic.AddInt64(&inFlight, 1)
				for {
					old := atomic.LoadInt64(&peak)
					if now <= old || atomic.CompareAndSwapInt64(&peak, old, now) {
						break
					}
				}
				time.Sleep(10 * time.Millisecond)
				atomic.AddInt64(&inFlight, -1)
				return nil
			}); err != nil {
				t.Errorf("run: %v", err)
			}
		}()
	}
	close(start)
	wg.Wait()
	if peak != limit {
		t.Fatalf("并发峰值 = %d, want %d", peak, limit)
	}
}

// 取消等槽位的调用方：不得继续执行，也不得漏放槽位。
func TestBoundedCancelWhileWaiting(t *testing.T) {
	gate := NewBounded(1)
	hold := make(chan struct{})
	started := make(chan struct{})
	go func() {
		_ = gate.run(context.Background(), func() error {
			close(started)
			<-hold
			return nil
		})
	}()
	<-started
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	called := false
	err := gate.run(ctx, func() error {
		called = true
		return nil
	})
	if err == nil {
		t.Fatal("取消的等待必须返回错误")
	}
	if called {
		t.Fatal("取消的调用方不得执行 f")
	}
	close(hold)
	// 槽位已释放：下一次调用立即可跑。
	done := make(chan struct{})
	go func() {
		_ = gate.run(context.Background(), func() error { close(done); return nil })
	}()
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("槽位泄漏：后续调用被永久阻塞")
	}
}

// n < 1 按 1 处理：0 会让每次调用永久阻塞。
func TestNewBoundedClampsToOne(t *testing.T) {
	gate := NewBounded(0)
	done := make(chan struct{})
	go func() {
		_ = gate.run(context.Background(), func() error { close(done); return nil })
	}()
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("n=0 未收敛到 1")
	}
}
