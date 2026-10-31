package image

// 解码闸的判据：并发上限被守住，排队可取消，Prepare 真的用闸门。
//
// 单张 40 MP 的 RGBA 约 160 MB；没有闸门时十个并发上传就是 1.6 GB。

import (
	"context"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// 并发上限被守住：容量 2，四个协程同时在跑时观察到的峰值不超过 2。
func TestGateBoundsConcurrency(t *testing.T) {
	gate := NewGate(2)
	var cur, peak int32
	var wg sync.WaitGroup
	for i := 0; i < 4; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if err := gate.acquire(context.Background()); err != nil {
				t.Errorf("acquire: %v", err)
				return
			}
			defer gate.release()
			n := atomic.AddInt32(&cur, 1)
			for {
				p := atomic.LoadInt32(&peak)
				if n <= p || atomic.CompareAndSwapInt32(&peak, p, n) {
					break
				}
			}
			time.Sleep(20 * time.Millisecond)
			atomic.AddInt32(&cur, -1)
		}()
	}
	wg.Wait()
	if peak > 2 {
		t.Fatalf("并发峰值 = %d, want <= 2", peak)
	}
}

// 排队时可取消：槽位被占满后，带已取消 ctx 的取槽立即返回 ctx 错误。
func TestGateAcquireHonorsContext(t *testing.T) {
	gate := NewGate(1)
	if err := gate.acquire(context.Background()); err != nil {
		t.Fatalf("acquire: %v", err)
	}
	defer gate.release()

	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Millisecond)
	defer cancel()
	if err := gate.acquire(ctx); err == nil {
		t.Fatal("槽位已满时 acquire 应等 ctx 取消")
	}
}

// Prepare 真的经过闸门：占满唯一槽位时它不返回，归还后才完成。
func TestPrepareWaitsForGateSlot(t *testing.T) {
	gate := NewGate(1)
	if err := gate.acquire(context.Background()); err != nil {
		t.Fatalf("acquire: %v", err)
	}

	done := make(chan error, 1)
	go func() {
		_, err := Prepare(context.Background(), pngBytes(t, whiteImage(8, 8)), Options{Rand: noCrop, Decode: gate})
		done <- err
	}()
	select {
	case <-done:
		t.Fatal("闸门槽位被占满时 Prepare 不得返回")
	case <-time.After(50 * time.Millisecond):
	}

	gate.release()
	select {
	case err := <-done:
		if err != nil {
			t.Fatalf("Prepare: %v", err)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("归还槽位后 Prepare 未完成")
	}
}
