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
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/logfield"
	coreschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/schedule"
)

// 日切参数。
const (
	dailyBuildSpec  = "10 0 * * *" // 业务日 00:10
	dailyBuildLimit = 30 * time.Minute
)

// dailyRetryDelays 是日切失败后的当日重试间隔：15/30/60 分钟三次，成功即止。
// 复用同一 cron 宿主：失败后在当前 goroutine 里等待并重跑，不新增调度器。
// 00:10 一次故障不再让全天无计划（P1-4）。
var dailyRetryDelays = []time.Duration{15 * time.Minute, 30 * time.Minute, 60 * time.Minute}

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
	SignDist      SignDist
	UsePrefs      bool
	EdgeFront     time.Duration
	EdgeBack      time.Duration
}

// BuildOverrides 是一次构建解析出的可写回键读数（sett2；批 3b 扩周末门；
// 批 3c 扩铺点分布档）。
type BuildOverrides struct {
	UsePrefs  bool
	EdgeFront time.Duration
	EdgeBack  time.Duration
	// SundaySign / SaturdaySign 是周末门生效值（批 3b DB-only 键）：
	// false=当日不铺任何计划行。缺行=false 由解析端（assembly）保证。
	SundaySign   bool
	SaturdaySign bool
	// SignDist 是铺点分布档生效值（批 3c DB-only 枚举）：
	// 缺行=SignDistFront（现行默认档，与既有 Front=true 铺点同形），
	// 由解析端（assembly）按缺行语义传入。
	SignDist SignDist
}

// Overrides 解析构建参数里的可写回键（settings 写回，DB>env）。
// nil 表示装配未接写回层：BuildToday 用 Params 原值，行为与既有完全一致。
// 返回错误时本次构建失败：构建值错了比不建更糟，下一轮定时重试。
type Overrides func(ctx context.Context) (BuildOverrides, error)

// DailyPlanner 是日切构建需要的计划面（*Service 实现）。
type DailyPlanner interface {
	Build(ctx context.Context, in Input) (Preview, error)
	// SkipStalePending 把 day 之前的 pending 行落 skipped：跨业务日的行
	// 不会再被认领，日切时必须了结（P1-6）。
	SkipStalePending(ctx context.Context, day string) (int64, error)
}

// DailyBuilder 为全部启用任务构建当日计划。
type DailyBuilder struct {
	Service  DailyPlanner
	Tasks    TaskLister
	Clock    coreclock.Clock
	Location *time.Location
	Params   BuildParams
	// Overrides 每次构建重读可写回键（settings 写回，批 sett2）。
	// nil 时用 Params 原值。口径：**当日已铺行不回改**——
	// 改值自下一次构建（次日凌晨 00:10）生效。
	Overrides Overrides
	// Logger 可为 nil：用 slog.Default。周末门关闭时如实记一行
	//（批 3b：不铺行不留日志就没有任何痕迹）。
	Logger *slog.Logger
}

// BuildToday 构建当日全部启用任务的计划。
// 先做跨日清理：day 之前的 pending 行落 skipped(window_closed)，
// 周末门关闭也照做——旧行不会再被认领，必须当日了结（P1-6）。
// 返回成功构建的任务数与首个错误；单个任务失败不阻断其余任务。
// 批 3b 周末门：当日 weekday 对应键为 false（缺行=false=不签，
// 对齐旧线 SUNDAY/SATURDAY_SIGN 缺省 0）时整日不铺行——
// 返回 0 行并如实记一行「周末门关闭」；键读取失败上抛不铺（保守方向）。
// 批 3c：SignDist 覆盖值一路传入铺点（缺行=front 默认档）。
func (b *DailyBuilder) BuildToday(ctx context.Context) (int, error) {
	if b == nil || b.Service == nil || b.Tasks == nil || b.Clock == nil || b.Location == nil {
		return 0, fmt.Errorf("schedule: incomplete daily builder")
	}
	day := coreclock.BusinessDay(b.Clock.Now(), b.Location)
	// 跨日清理先于周末门与构建：上一日的 pending 行永远不会再被认领，
	// 无论今天是否铺行都必须落结论（P1-6）。失败上抛，由日切重试兜底。
	if _, err := b.Service.SkipStalePending(ctx, day); err != nil {
		return 0, fmt.Errorf("schedule: skip stale pending rows: %w", err)
	}
	usePrefs, edgeFront, edgeBack := b.Params.UsePrefs, b.Params.EdgeFront, b.Params.EdgeBack
	sundaySign, saturdaySign := false, false
	signDist := b.Params.SignDist
	if b.Overrides != nil {
		overrides, err := b.Overrides(ctx)
		if err != nil {
			// 读取失败上抛不铺：构建值错了比不建更糟，下一轮定时重试。
			return 0, err
		}
		usePrefs, edgeFront, edgeBack = overrides.UsePrefs, overrides.EdgeFront, overrides.EdgeBack
		sundaySign, saturdaySign = overrides.SundaySign, overrides.SaturdaySign
		// 3c：缺行时解析端传 SignDistFront（现行默认档）。
		signDist = overrides.SignDist
	}
	// 周末门：先于任务列表判定——不铺行即零负载，也不必查任务名册。
	// weekday 从业务日串解析，与铺行日严格一致。
	dayDate, err := time.ParseInLocation("2006-01-02", day, b.Location)
	if err != nil {
		return 0, fmt.Errorf("schedule: daily builder day %q: %w", day, err)
	}
	switch weekday := dayDate.Weekday(); weekday {
	case time.Sunday:
		if !sundaySign {
			b.logWeekendGateClosed(day, "sunday")
			return 0, nil
		}
	case time.Saturday:
		if !saturdaySign {
			b.logWeekendGateClosed(day, "saturday")
			return 0, nil
		}
	}
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
			SignDist:      signDist,
			UsePrefs:      usePrefs,
			EdgeFront:     edgeFront,
			EdgeBack:      edgeBack,
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

// logWeekendGateClosed 如实记一行「周末门关闭」：不铺行不留日志
// 就没有任何痕迹，运维会误以为构建空转。weekday 词取 sunday/saturday。
func (b *DailyBuilder) logWeekendGateClosed(day, weekday string) {
	log := b.Logger
	if log == nil {
		log = slog.Default()
	}
	log.Info("schedule: weekend gate closed; no plan rows built",
		logfield.KeyEvent, "schedule.weekend_gate_closed",
		logfield.KeyDay, day,
		logfield.KeyReason, weekday,
	)
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
	// retryDelays 是失败后的当日重试间隔；空表示不重试。
	retryDelays []time.Duration
	// wait 等待一次退避；返回 false 表示被 Stop 取消。测试可注入。
	wait func(ctx context.Context, d time.Duration) bool
	// retryCtx 取消待执行的退避等待；Stop 时取消。
	retryCtx    context.Context
	retryCancel context.CancelFunc
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
	retryCtx, retryCancel := context.WithCancel(context.Background())
	d := &DailyCron{
		builder: builder, log: log,
		retryDelays: dailyRetryDelays, wait: waitContext,
		retryCtx: retryCtx, retryCancel: retryCancel,
	}
	d.cron = cron.New(
		cron.WithLocation(builder.Location),
		cron.WithChain(cron.Recover(cronLog{log: log})),
	)
	if _, err := d.cron.AddFunc(dailyBuildSpec, func() { d.run(context.Background(), "daily") }); err != nil {
		retryCancel()
		return nil, fmt.Errorf("schedule: add daily job: %w", err)
	}
	return d, nil
}

// waitContext 等待 d 或 ctx 取消；返回 false 表示被取消。
func waitContext(ctx context.Context, d time.Duration) bool {
	timer := time.NewTimer(d)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return false
	case <-timer.C:
		return true
	}
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
		// 启动补建与 cron 任务同等待遇：panic 只记日志，不终止进程。
		defer func() {
			if v := recover(); v != nil {
				d.log.Error("schedule: startup build panicked", logfield.KeyPanic, fmt.Sprint(v))
			}
		}()
		// 后台补建不随请求取消；用独立 ctx。
		d.run(context.Background(), "startup")
	}()
	d.cron.Start()
	return nil
}

// Stop 停定时器并等补建结束。ctx 到期即返回错误，不无限等待。
// 先取消待执行的退避等待：否则 Stop 要等满 15/30/60 分钟。
func (d *DailyCron) Stop(ctx context.Context) error {
	d.mu.Lock()
	if d.stopped {
		d.mu.Unlock()
		return nil
	}
	d.stopped = true
	started := d.started
	d.mu.Unlock()
	d.retryCancel()
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

// run 执行一轮构建并记结果；失败按 dailyRetryDelays 当日重试，成功即止。
// 重试在同一个 goroutine 里等待：不新增调度器，也不与下一轮并跑。
func (d *DailyCron) run(parent context.Context, trigger string) {
	for retry := 0; ; retry++ {
		ctx, cancel := context.WithTimeout(parent, dailyBuildLimit)
		built, err := d.builder.BuildToday(ctx)
		cancel()
		if err == nil {
			d.log.Info("schedule: daily build done",
				logfield.KeyTrigger, trigger, logfield.KeyBuilt, built)
			return
		}
		d.log.Error("schedule: daily build failed",
			logfield.KeyTrigger, trigger, logfield.KeyBuilt, built, logfield.KeyError, err.Error())
		if retry >= len(d.retryDelays) {
			return
		}
		delay := d.retryDelays[retry]
		d.log.Warn("schedule: daily build retry scheduled",
			logfield.KeyTrigger, trigger,
			logfield.KeyAttempt, retry+1,
			logfield.KeyDurationMS, delay.Milliseconds())
		if !d.wait(d.retryCtx, delay) {
			return
		}
	}
}

// cronLog 把 robfig/cron 的日志接到 slog。
type cronLog struct{ log *slog.Logger }

func (l cronLog) Info(msg string, keysAndValues ...any) {
	//logfield:allow robfig/cron 透传键
	l.log.Info(msg, keysAndValues...)
}

func (l cronLog) Error(err error, msg string, keysAndValues ...any) {
	args := append([]any{logfield.KeyError, err.Error()}, keysAndValues...)
	l.log.Error(msg, args...)
}
