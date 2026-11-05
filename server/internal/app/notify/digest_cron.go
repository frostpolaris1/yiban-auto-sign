package notify

// 每日签到失败摘要的宿主：业务日 09:00 发上一日；启动时补发一次。
//
// # 功能
// DigestCron 用 robfig/cron 在配置时区的 09:00 触发 DispatchDigest，
// 业务日取 coreclock.BusinessDay 往前 1 天。启动时先在后台补发上一业务日，
// 再起定时器；更旧的业务日错过即错过，不追补。
// 本文件**不**做失败明细聚合、**不**写 SQL、**不**决定收件人 scope。
//
// # 归属
// app 层用例的宿主件。用例在 digest.go；取数端口由 adapters/pg 实现。
//
// # 口径
// 触发时刻固定 09:00，不做运行时可配；YIBAN_TZ 只影响业务日归属。
// 启动补发与定时任务同等待遇：panic recover 只记日志，不终止进程。
// 停机等待补发 goroutine 在 ctx 预算内结束；Stop 幂等。
// 重复触发由 DispatchDigest 的去重键吸收。

import (
	"context"
	"fmt"
	"log/slog"
	"sync"
	"time"

	"github.com/robfig/cron/v3"

	coreclock "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/logfield"
)

const (
	// digestSpec 固定在业务日 09:00，与设计稿拍板一致。
	digestSpec = "0 9 * * *"
	// digestRunLimit 是单轮摘要的取数与落库预算。
	digestRunLimit = 2 * time.Minute
)

// DigestCron 是每日摘要的宿主。零值不可用；请用 NewDigestCron 构造。
type DigestCron struct {
	dispatcher *Dispatcher
	clock      coreclock.Clock
	location   *time.Location
	log        *slog.Logger
	cron       *cron.Cron
	wg         sync.WaitGroup
	mu         sync.Mutex
	started    bool
	stopped    bool
}

// NewDigestCron 构造摘要宿主。dispatcher、时钟或时区缺一即失败。
func NewDigestCron(dispatcher *Dispatcher, clk coreclock.Clock,
	location *time.Location, logger *slog.Logger) (*DigestCron, error) {
	if dispatcher == nil || clk == nil || location == nil {
		return nil, fmt.Errorf("notify: incomplete digest cron")
	}
	log := logger
	if log == nil {
		log = slog.Default()
	}
	c := &DigestCron{dispatcher: dispatcher, clock: clk, location: location, log: log}
	c.cron = cron.New(cron.WithLocation(location))
	if _, err := c.cron.AddFunc(digestSpec, func() { c.run(context.Background(), "cron") }); err != nil {
		return nil, fmt.Errorf("notify: add digest job: %w", err)
	}
	return c, nil
}

// Start 先启动补发，再起定时器。重复 Start 返回错误。
func (c *DigestCron) Start() error {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.started {
		return fmt.Errorf("notify: digest cron already started")
	}
	if c.stopped {
		return fmt.Errorf("notify: digest cron already stopped")
	}
	c.started = true
	// 补发在后台跑：取数可能慢，不阻塞 HTTP 启动。
	c.wg.Add(1)
	go func() {
		defer c.wg.Done()
		// 后台补发不随请求取消；用独立 ctx。
		c.run(context.Background(), "startup")
	}()
	c.cron.Start()
	return nil
}

// Stop 停定时器并等补发结束。ctx 到期即返回错误，不无限等待。
func (c *DigestCron) Stop(ctx context.Context) error {
	c.mu.Lock()
	if c.stopped {
		c.mu.Unlock()
		return nil
	}
	c.stopped = true
	started := c.started
	c.mu.Unlock()
	if !started {
		return nil
	}
	stopCtx := c.cron.Stop()
	done := make(chan struct{})
	go func() { c.wg.Wait(); close(done) }()
	select {
	case <-done:
	case <-ctx.Done():
		return ctx.Err()
	}
	select {
	case <-stopCtx.Done():
	case <-ctx.Done():
		return ctx.Err()
	}
	return nil
}

// run 补发/触发一轮摘要并记结果。panic 只记日志，不终止进程。
func (c *DigestCron) run(parent context.Context, trigger string) {
	defer func() {
		if v := recover(); v != nil {
			c.log.Error("notify: daily digest panicked",
				logfield.KeyTrigger, trigger, logfield.KeyPanic, fmt.Sprint(v))
		}
	}()
	ctx, cancel := context.WithTimeout(parent, digestRunLimit)
	defer cancel()
	day := previousBusinessDay(c.clock.Now(), c.location)
	if err := c.dispatcher.DispatchDigest(ctx, day); err != nil {
		c.log.Warn("notify: daily digest failed",
			logfield.KeyTrigger, trigger, logfield.KeyDay, day, logfield.KeyError, err.Error())
		return
	}
	c.log.Info("notify: daily digest done",
		logfield.KeyTrigger, trigger, logfield.KeyDay, day)
}

// previousBusinessDay 返回 now 所在时区的上一业务日。
// 先按 loc 取本地时刻再减一天，跨夏令时也落在正确日期。
// 只回最近 1 天：更旧的业务日不追补。
func previousBusinessDay(now time.Time, loc *time.Location) string {
	if loc == nil {
		loc = time.UTC
	}
	return coreclock.BusinessDay(now.In(loc).AddDate(0, 0, -1), loc)
}
