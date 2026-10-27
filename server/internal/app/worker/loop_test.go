package worker

// Loop 的单元测试：停机三步的语义。
//
// # 功能
// 钉住停机判据（派单 P7）：Stop 先停领取、等在飞结算、
// 返回残余 running 数；超时按 ctx 错误返回。
// 全部依赖注入替身，本文件**不**连数据库。
//
// # 口径
// 在飞 = 一次进行中的 Step 调用。Stop 返回只在在飞返回之后。

import (
	"context"
	"errors"
	"sync"
	"testing"
	"time"
)

// fakeStepper 是可阻塞的 Stepper 替身。
type fakeStepper struct {
	mu      sync.Mutex
	steps   int
	begun   chan struct{} // 首次 Step 进入时关闭
	release chan struct{} // 放行在飞
}

func (f *fakeStepper) Step(context.Context, int) (int, error) {
	f.mu.Lock()
	f.steps++
	n := f.steps
	f.mu.Unlock()
	if n == 1 && f.begun != nil {
		close(f.begun)
	}
	<-f.release
	return 1, nil
}

func (f *fakeStepper) count() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.steps
}

// fakeCounter 是 RunningCounter 替身。
type fakeCounter struct{ n int64 }

func (f *fakeCounter) CountRunning(context.Context) (int64, error) { return f.n, nil }

func TestLoopStopWaitsForInFlight(t *testing.T) {
	begun := make(chan struct{})
	release := make(chan struct{})
	svc := &fakeStepper{begun: begun, release: release}
	loop := NewLoop(svc, &fakeCounter{}, 10*time.Millisecond, 1)
	loop.Start()

	select {
	case <-begun:
	case <-time.After(2 * time.Second):
		t.Fatal("Step 未被启动")
	}

	type stopResult struct {
		remain int64
		err    error
	}
	resCh := make(chan stopResult, 1)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	go func() {
		n, err := loop.Stop(ctx)
		resCh <- stopResult{n, err}
	}()

	// 在飞未放行时 Stop 不得返回：这是「先停领取、再等在飞」的判据。
	select {
	case r := <-resCh:
		t.Fatalf("Stop 在在飞结算前返回: remain=%d err=%v", r.remain, r.err)
	case <-time.After(200 * time.Millisecond):
	}

	close(release)
	select {
	case r := <-resCh:
		if r.err != nil {
			t.Fatalf("Stop: %v", r.err)
		}
		if r.remain != 0 {
			t.Fatalf("remain = %d, want 0", r.remain)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("放行后 Stop 未返回")
	}
}

func TestLoopStopTimeoutReturnsCtxErr(t *testing.T) {
	begun := make(chan struct{})
	release := make(chan struct{})
	defer close(release) // 测试结束后放行，避免 goroutine 泄漏
	svc := &fakeStepper{begun: begun, release: release}
	loop := NewLoop(svc, &fakeCounter{}, 10*time.Millisecond, 1)
	loop.Start()

	// 等在飞真正开始再停：否则 Stop 可能在首轮认领前关闭，
	// 循环无在飞可等，立即返回是合法行为。
	select {
	case <-begun:
	case <-time.After(2 * time.Second):
		t.Fatal("Step 未被启动")
	}

	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancel()
	remain, err := loop.Stop(ctx)
	if !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("Stop err = %v, want DeadlineExceeded", err)
	}
	if remain != 0 {
		t.Fatalf("remain = %d, want 0 on timeout", remain)
	}
}

func TestLoopStopWithoutStartIsImmediate(t *testing.T) {
	loop := NewLoop(&fakeStepper{release: make(chan struct{})}, &fakeCounter{}, time.Second, 1)
	remain, err := loop.Stop(context.Background())
	if err != nil {
		t.Fatalf("Stop: %v", err)
	}
	if remain != 0 {
		t.Fatalf("remain = %d, want 0", remain)
	}
}

func TestLoopStopsClaimingAfterStop(t *testing.T) {
	// Step 恒立即完成（release 预先关闭）：不等在飞的场景已有
	// TestLoopStopWaitsForInFlight 钉住，这里只钉「Stop 返回后零新领取」。
	release := make(chan struct{})
	close(release)
	svc := &fakeStepper{release: release}
	loop := NewLoop(svc, &fakeCounter{}, 5*time.Millisecond, 1)
	loop.Start()

	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	if _, err := loop.Stop(ctx); err != nil {
		t.Fatalf("Stop: %v", err)
	}
	n1 := svc.count()
	time.Sleep(30 * time.Millisecond) // 数倍于间隔：期间不该有任何新 Step
	if n2 := svc.count(); n2 != n1 {
		t.Fatalf("Stop 后仍有新领取：at-return=%d after-wait=%d", n1, n2)
	}
}

// TestLoopStartAfterStopFails 钉住 Stop 之后不能再 Start（派单 F13）。
// Stop 会关闭 done 通道；未启动过的实例 Start 后 run() 的
// defer close(done) 会二次关闭，直接 panic。
func TestLoopStartAfterStopFails(t *testing.T) {
	loop := NewLoop(&fakeStepper{release: make(chan struct{})}, &fakeCounter{}, time.Second, 1)
	if _, err := loop.Stop(context.Background()); err != nil {
		t.Fatalf("Stop: %v", err)
	}
	if err := loop.Start(); err == nil {
		t.Fatal("Stop 之后 Start 必须返回错误，不得启动循环")
	}
}

// TestLoopRestartAfterStopFails 覆盖已启动实例的同一条边界。
func TestLoopRestartAfterStopFails(t *testing.T) {
	release := make(chan struct{})
	close(release)
	loop := NewLoop(&fakeStepper{release: release}, &fakeCounter{}, 5*time.Millisecond, 1)
	if err := loop.Start(); err != nil {
		t.Fatalf("Start: %v", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	if _, err := loop.Stop(ctx); err != nil {
		t.Fatalf("Stop: %v", err)
	}
	if err := loop.Start(); err == nil {
		t.Fatal("Stop 之后 Start 必须返回错误")
	}
}
