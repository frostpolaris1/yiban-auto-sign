package schedule

// 日切构建：业务日 00:10 为全部启用任务构建当日计划；启动时补建一次。
//
// # 功能
// DailyBuilder 列出启用任务，按当日业务日为每个任务调用 Build。
// DailyCron 用 robfig/cron 在配置时区的 00:10 触发；启动时先补建一次。
// 本文件**不**做窗口发现、**不**写 SQL、**不**决定计划几何。
//
// # 归属
// app 层用例的宿主件。Build 在 service.go；任务列表由仓储提供。
//
// # 口径
// 构建幂等：同一任务同一天重复构建只更新当日行。
// 单个任务失败不阻断其余任务；返回已建数与首个错误。
// 启动补建在后台跑，不阻塞 HTTP 启动；停机等待它在预算内结束。
// 触发时刻固定在业务日 00:10；时区取 YIBAN_TZ。

import (
	"context"
	"fmt"
	"log/slog"
	"sync"
	"time"

	"github.com/robfig/cron/v3"

	coreclock "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	coreschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/schedule"
)

// 日切参数。
const (
	dailyBuildSpec  = "10 0 * * *" // 业务日 00:10
	dailyBuildLimit = 30 * time.Minute
)

// TaskLister 列出全部启用任务（实现为 pg.ScheduleRepo）。
type TaskLister interface {
	EnabledTasks(ctx context.Context) ([]coreschedule.TaskInfo, error)
}

// BuildParams 是构建计划的部署参数，与 HTTP 建计划同源。
type BuildParams struct {
	Workers       int
	Avg           time.Duration
	Rate          float64
	Util          float64
	SlotsPerSlice int
	Front         bool
	UsePrefs      bool
	EdgeFront     time.Duration
	EdgeBack      time.Duration
}

// DailyPlanner 是日切构建需要的计划面（*Service 实现）。
type DailyPlanner interface {
	Build(ctx context.Context, in Input) (Preview, error)
}

// DailyBuilder 为全部启用任务构建当日计划。
type DailyBuilder struct {
	Service  DailyPlanner
	Tasks    TaskLister
	Clock    coreclock.Clock
	Location *time.Location
	Params   BuildParams
}

// BuildToday 构建当日全部启用任务的计划。
// 返回成功构建的任务数与首个错误；单个任务失败不阻断其余任务。
func (b *DailyBuilder) BuildToday(ctx context.Context) (int, error) {
	if b == nil || b.Service == nil || b.Tasks == nil || b.Clock == nil || b.Location == nil {
		return 0, fmt.Errorf("schedule: incomplete daily builder")
	}
	day := coreclock.BusinessDay(b.Clock.Now(), b.Location)
	tasks, err := b.Tasks.EnabledTasks(ctx)
	if err != nil {
		return 0, err
	}
	built := 0
	var firstErr error
	for _, task := range tasks {
		if err := ctx.Err(); err != nil {
			return built, err
		}
		_, err := b.Service.Build(ctx, Input{
			SchoolID:      task.SchoolID,
			SchoolTaskID:  task.ID,
			Day:           day,
			Location:      b.Location,
			Workers:       b.Params.Workers,
			Avg:           b.Params.Avg,
			Rate:          b.Params.Rate,
			Util:          b.Params.Util,
			SlotsPerSlice: b.Params.SlotsPerSlice,
			Front:         b.Params.Front,
			UsePrefs:      b.Params.UsePrefs,
			EdgeFront:     b.Params.EdgeFront,
			EdgeBack:      b.Params.EdgeBack,
		})
		if err != nil {
			if firstErr == nil {
				firstErr = fmt.Errorf("task %s: %w", task.ID, err)
			}
			continue
		}
		built++
	}
	return built, firstErr
}

// DailyCron 是日切构建的宿主。零值不可用；请用 NewDailyCron 构造。
type DailyCron struct {
	builder *DailyBuilder
	log     *slog.Logger
	cron    *cron.Cron
	wg      sync.WaitGroup
	mu      sync.Mutex
	started bool
	stopped bool
}

// NewDailyCron 构造日切宿主。builder 与 logger 缺一即失败。
func NewDailyCron(builder *DailyBuilder, logger *slog.Logger) (*DailyCron, error) {
	if builder == nil || builder.Location == nil {
		return nil, fmt.Errorf("schedule: nil daily builder")
	}
	log := logger
	if log == nil {
		log = slog.Default()
	}
	d := &DailyCron{builder: builder, log: log}
	d.cron = cron.New(
		cron.WithLocation(builder.Location),
		cron.WithChain(cron.Recover(cronLog{log: log})),
	)
	if _, err := d.cron.AddFunc(dailyBuildSpec, func() { d.run(context.Background(), "daily") }); err != nil {
		return nil, fmt.Errorf("schedule: add daily job: %w", err)
	}
	return d, nil
}

// Start 先启动补建，再起定时器。重复 Start 返回错误。
func (d *DailyCron) Start() error {
	d.mu.Lock()
	defer d.mu.Unlock()
	if d.started {
		return fmt.Errorf("schedule: daily cron already started")
	}
	if d.stopped {
		return fmt.Errorf("schedule: daily cron already stopped")
	}
	d.started = true
	// 补建在后台跑：平台发现可能慢，不阻塞 HTTP 启动。
	d.wg.Add(1)
	go func() {
		defer d.wg.Done()
		// 后台补建不随请求取消；用独立 ctx。
		d.run(context.Background(), "startup")
	}()
	d.cron.Start()
	return nil
}

// Stop 停定时器并等补建结束。ctx 到期即返回错误，不无限等待。
func (d *DailyCron) Stop(ctx context.Context) error {
	d.mu.Lock()
	if d.stopped {
		d.mu.Unlock()
		return nil
	}
	d.stopped = true
	started := d.started
	d.mu.Unlock()
	if !started {
		return nil
	}
	stopCtx := d.cron.Stop()
	done := make(chan struct{})
	go func() { d.wg.Wait(); close(done) }()
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

// run 执行一轮构建并记结果。
func (d *DailyCron) run(parent context.Context, trigger string) {
	ctx, cancel := context.WithTimeout(parent, dailyBuildLimit)
	defer cancel()
	built, err := d.builder.BuildToday(ctx)
	if err != nil {
		d.log.Error("schedule: daily build failed",
			"trigger", trigger, "built", built, "error", err.Error())
		return
	}
	d.log.Info("schedule: daily build done", "trigger", trigger, "built", built)
}

// cronLog 把 robfig/cron 的日志接到 slog。
type cronLog struct{ log *slog.Logger }

func (l cronLog) Info(msg string, keysAndValues ...any) {
	l.log.Info(msg, keysAndValues...)
}

func (l cronLog) Error(err error, msg string, keysAndValues ...any) {
	args := append([]any{"error", err.Error()}, keysAndValues...)
	l.log.Error(msg, args...)
}
