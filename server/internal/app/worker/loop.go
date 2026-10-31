// Loop 是执行循环的宿主：周期认领，停机先停领取再等在飞。
//
// # 功能
// Start 起一个 goroutine 周期调 Step；Stop 关闭领取并等当前在飞
// Step 返回，然后清点残余 running 行。超时按 ctx 错误返回；
// 有效租约的残余行由 tick 回收器在到期后回收，无租约或已过期行由下次启动的 Recover 清。
// 本类型**不**记日志、**不**管 HTTP：那是 assembly 收尾编排的事。
//
// # 归属
// app 层用例的宿主件。Service 认领与结算；Loop 只管节奏与停机。
//
// # 口径
// 在飞 = 一次进行中的 Step。停机先**不**打断在飞：优雅收尾是等它结算，
// 打断会在平台侧留下未结算的已提交签到。超时上限由调用方的 ctx 决定
// （assembly 用 10s）。预算耗尽后 Stop 取消在飞的 ctx，并给 drainSettle
// 收尾——调用方随即关连接池，不先收尾的话结算写不进去（D 批 P2-16）。
package worker

import (
	"context"
	"errors"
	"sync"
	"time"
)

// Stepper 是执行循环的认领结算面（*Service 实现）。
type Stepper interface {
	Step(ctx context.Context, limit int) (int, error)
}

// RunningCounter 是 running 行清点面（*Service 经 Store 实现）。
type RunningCounter interface {
	CountRunning(ctx context.Context) (int64, error)
}

// Loop 周期执行 Step。零值不可用，用 NewLoop 构造。
type Loop struct {
	svc      Stepper
	counter  RunningCounter
	interval time.Duration
	limit    int

	stop chan struct{}
	done chan struct{}
	once sync.Once

	mu      sync.Mutex
	started bool
	stopped bool
	lastErr error
	// cancelInFlight 取消当前 Step 的 ctx；无在飞时为 nil。
	// 只在停机预算耗尽后调用：调用方随即关连接池，结算写不进去
	// 会让已提交的签到留在 running（D 批 P2-16）。
	cancelInFlight context.CancelFunc
}

// errLoopAlreadyStarted 是 Start 的重入错误。
var errLoopAlreadyStarted = errors.New("worker: loop already started")

// errLoopStopped 是 Stop 之后再 Start 的错误。
// Stop 关闭了 stop 与 done 通道，重启会二次关闭 panic。
var errLoopStopped = errors.New("worker: loop already stopped")

// NewLoop 构造循环。interval 是空转轮询间隔；limit 是每次认领上限。
func NewLoop(svc Stepper, counter RunningCounter, interval time.Duration, limit int) *Loop {
	if interval <= 0 {
		interval = time.Second
	}
	if limit < 1 {
		limit = 1
	}
	return &Loop{
		svc:      svc,
		counter:  counter,
		interval: interval,
		limit:    limit,
		stop:     make(chan struct{}),
		done:     make(chan struct{}),
	}
}

// Start 起循环。每个实例只允许 Start 一次。
// 重复 Start 与 Stop 后再 Start 都返回错误：
// run() 的 defer close(done) 与 Stop 的 close 是同一通道，
// 重入会双关 panic（P7 审查 P3-1）。
func (l *Loop) Start() error {
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.stopped {
		return errLoopStopped
	}
	if l.started {
		return errLoopAlreadyStarted
	}
	l.started = true
	go l.run()
	return nil
}

// run 是循环体：先跑一轮再等间隔，收 Stop 即退。
func (l *Loop) run() {
	defer close(l.done)
	timer := time.NewTimer(0) // 启动即刻认领一轮
	defer timer.Stop()
	for {
		select {
		case <-l.stop:
			return
		case <-timer.C:
		}
		// Step 用可取消的 ctx：停机**不主动**取消在飞（见包注释口径），
		// 只有停机预算耗尽时 Stop 才取消它，给结算留一次收尾机会。
		// 基础设施错误记下不退出：队列抖动不该杀掉执行循环。
		stepCtx, cancel := context.WithCancel(context.Background())
		l.mu.Lock()
		l.cancelInFlight = cancel
		l.mu.Unlock()
		_, err := l.svc.Step(stepCtx, l.limit)
		l.mu.Lock()
		l.cancelInFlight = nil
		l.mu.Unlock()
		cancel()
		if err != nil {
			l.mu.Lock()
			l.lastErr = err
			l.mu.Unlock()
		}
		timer.Reset(l.interval)
	}
}

// Stop 停止领取并等在飞返回，返回残余 running 数。
// ctx 到期时先取消在飞 Step，再给它 drainSettle 收尾：
// 调用方（assembly）随后关连接池，直接返回会让结算写不进去。
// 收尾仍超时则返回 (0, ctx.Err())，残余行的租约到期后由回收器回收。
// 未 Start 过的实例立即返回 (0, nil)。
func (l *Loop) Stop(ctx context.Context) (int64, error) {
	l.mu.Lock()
	neverStarted := !l.started
	l.stopped = true
	l.mu.Unlock()
	l.once.Do(func() {
		close(l.stop)
		if neverStarted {
			close(l.done)
		}
	})
	select {
	case <-l.done:
	case <-ctx.Done():
		if !l.stopInFlight(ctx) {
			return 0, ctx.Err()
		}
	}
	n, err := l.counter.CountRunning(context.WithoutCancel(ctx))
	if err != nil {
		return 0, err
	}
	return n, nil
}

// drainSettle 是取消在飞后等它收尾的上限。
// 必须大于 settlementTimeout：结算的写入上限若比等待窗口还长，
// 上限就兑不了——Stop 一返回，调用方随即关连接池（审查 P3-1）。
const drainSettle = settlementTimeout + 200*time.Millisecond

// stopInFlight 取消在飞 Step 并等它返回；返回 true 表示已停下。
func (l *Loop) stopInFlight(ctx context.Context) bool {
	l.mu.Lock()
	cancel := l.cancelInFlight
	l.mu.Unlock()
	if cancel != nil {
		cancel()
	}
	settle, done := context.WithTimeout(context.WithoutCancel(ctx), drainSettle)
	defer done()
	select {
	case <-l.done:
		return true
	case <-settle.Done():
		return false
	}
}

// Err 返回最近一次 Step 的基础设施错误。无错误时为 nil。
func (l *Loop) Err() error {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.lastErr
}
