// Loop 是执行循环的宿主：周期认领，停机先停领取再等在飞。
//
// # 功能
// Start 起一个 goroutine 周期调 Step；Stop 关闭领取并等当前在飞
// Step 返回，然后清点残余 running 行。超时按 ctx 错误返回，
// 残余交给下次启动的 Recover 回收。
// 本类型**不**记日志、**不**管 HTTP：那是 assembly 收尾编排的事。
//
// # 归属
// app 层用例的宿主件。Service 认领与结算；Loop 只管节奏与停机。
//
// # 口径
// 在飞 = 一次进行中的 Step。停机不取消在飞的 ctx：
// 优雅收尾是等它结算，不是打断它；打断会在平台侧留下未结算的
// 已提交签到。超时上限由调用方的 ctx 决定（assembly 用 10s）。
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
	lastErr error
}

// errLoopAlreadyStarted 是 Start 的重入错误。
var errLoopAlreadyStarted = errors.New("worker: loop already started")

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
// Start 启动循环。重复 Start 与 Stop 后再 Start 都返回错误：
// run() 的 defer close(done) 与 Stop 的 close 是同一通道，
// 重入会双关 panic（P7 审查 P3-1）。
func (l *Loop) Start() error {
	l.mu.Lock()
	defer l.mu.Unlock()
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
		// Step 用背景 ctx：停机**不**取消在飞（见包注释口径）。
		// 基础设施错误记下不退出：队列抖动不该杀掉执行循环。
		if _, err := l.svc.Step(context.Background(), l.limit); err != nil {
			l.mu.Lock()
			l.lastErr = err
			l.mu.Unlock()
		}
		timer.Reset(l.interval)
	}
}

// Stop 停止领取并等在飞返回，返回残余 running 数。
// ctx 到期时立即返回 (0, ctx.Err())：残余由重启回收兜底。
// 未 Start 过的实例立即返回 (0, nil)。
func (l *Loop) Stop(ctx context.Context) (int64, error) {
	l.mu.Lock()
	neverStarted := !l.started
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
		return 0, ctx.Err()
	}
	n, err := l.counter.CountRunning(context.WithoutCancel(ctx))
	if err != nil {
		return 0, err
	}
	return n, nil
}

// Err 返回最近一次 Step 的基础设施错误。无错误时为 nil。
func (l *Loop) Err() error {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.lastErr
}
