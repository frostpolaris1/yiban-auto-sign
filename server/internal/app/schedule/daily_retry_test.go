package schedule

// 日切失败后的当日重试（P1-4）。
//
// 判据：00:10 一次故障不再让全天无计划——失败后按 15/30/60 分钟三次重试，
// 成功即止；Stop 取消待执行的退避等待。
// 测试注入 wait 钩子，不真实等待 15 分钟。

import (
	"bytes"
	"context"
	"errors"
	"log/slog"
	"strings"
	"testing"
	"time"

	coreclock "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	coreschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/schedule"
)

var retrySH = time.FixedZone("CST", 8*3600)

// retryPlanner 前 failUntil 次构建失败，之后成功。
type retryPlanner struct {
	calls     int
	failUntil int
}

func (p *retryPlanner) Build(context.Context, Input) (Preview, error) {
	p.calls++
	if p.calls <= p.failUntil {
		return Preview{}, errors.New("build failed")
	}
	return Preview{}, nil
}

func (p *retryPlanner) SkipStalePending(context.Context, string) (int64, error) {
	return 0, nil
}

type retryTasks struct{}

func (retryTasks) EnabledTasks(context.Context) ([]coreschedule.TaskInfo, error) {
	return []coreschedule.TaskInfo{{ID: "t1", SchoolID: "s1", Enabled: true}}, nil
}

// retryBuilder 构造日切构建器：时钟固定在 2026-10-30 00:05（周五，无周末门）。
func retryBuilder(planner DailyPlanner) *DailyBuilder {
	return &DailyBuilder{
		Service: planner, Tasks: retryTasks{},
		Clock:    coreclock.Fixed{T: time.Date(2026, 10, 30, 0, 5, 0, 0, retrySH)},
		Location: retrySH,
		Params: BuildParams{
			Workers: 4, Avg: 3 * time.Second, Rate: 1, Util: 0.8,
			SlotsPerSlice: 10, SignDist: SignDistFront,
		},
	}
}

// retryCron 构造日切宿主并注入记录延迟的 wait 钩子。
func retryCron(t *testing.T, planner *retryPlanner) (*DailyCron, *[]time.Duration) {
	t.Helper()
	cron, err := NewDailyCron(retryBuilder(planner), nil)
	if err != nil {
		t.Fatalf("NewDailyCron: %v", err)
	}
	delays := []time.Duration{}
	cron.wait = func(_ context.Context, d time.Duration) bool {
		delays = append(delays, d)
		return true
	}
	return cron, &delays
}

// 首败后恢复：只重试一次，延迟 15 分钟。
func TestDailyCronRetriesFailedBuild(t *testing.T) {
	planner := &retryPlanner{failUntil: 1}
	cron, delays := retryCron(t, planner)
	cron.run(context.Background(), "daily")
	if planner.calls != 2 {
		t.Fatalf("构建次数 = %d, want 2（首败 + 一次重试）", planner.calls)
	}
	if len(*delays) != 1 || (*delays)[0] != 15*time.Minute {
		t.Fatalf("重试延迟 = %v, want [15m]", *delays)
	}
}

// 持续失败：首跑 + 三次重试后停止，延迟恰为 15/30/60 分钟。
func TestDailyCronStopsAfterThreeRetries(t *testing.T) {
	planner := &retryPlanner{failUntil: 100}
	cron, delays := retryCron(t, planner)
	cron.run(context.Background(), "daily")
	if planner.calls != 4 {
		t.Fatalf("构建次数 = %d, want 4（首跑 + 三次重试）", planner.calls)
	}
	want := []time.Duration{15 * time.Minute, 30 * time.Minute, 60 * time.Minute}
	if len(*delays) != len(want) {
		t.Fatalf("重试延迟 = %v, want %v", *delays, want)
	}
	for i := range want {
		if (*delays)[i] != want[i] {
			t.Fatalf("重试延迟[%d] = %v, want %v", i, (*delays)[i], want[i])
		}
	}
}

// Stop 取消退避等待：不再重跑，也不阻塞到延迟结束。
func TestDailyCronRetryWaitCanceled(t *testing.T) {
	planner := &retryPlanner{failUntil: 100}
	cron, delays := retryCron(t, planner)
	cron.wait = func(_ context.Context, d time.Duration) bool {
		*delays = append(*delays, d)
		return false
	}
	cron.run(context.Background(), "daily")
	if planner.calls != 1 {
		t.Fatalf("构建次数 = %d, want 1（等待被取消后不得重跑）", planner.calls)
	}
	if len(*delays) != 1 || (*delays)[0] != 15*time.Minute {
		t.Fatalf("重试延迟 = %v, want [15m]", *delays)
	}
}

// blockingPlanner 在 Build 里阻塞，用于验证单飞锁。
type blockingPlanner struct {
	started chan struct{}
	release chan struct{}
	calls   int
}

func (p *blockingPlanner) Build(context.Context, Input) (Preview, error) {
	p.calls++
	close(p.started)
	<-p.release
	return Preview{}, nil
}

func (p *blockingPlanner) SkipStalePending(context.Context, string) (int64, error) {
	return 0, nil
}

// 启动补建与 00:10 轮不得重叠：后到者跳过本轮并记 Warn（M1/N2）。
func TestDailyCronSingleFlightSkipsOverlappingRun(t *testing.T) {
	planner := &blockingPlanner{started: make(chan struct{}), release: make(chan struct{})}
	var logs bytes.Buffer
	logger := slog.New(slog.NewTextHandler(&logs, nil))
	cron, err := NewDailyCron(retryBuilder(planner), logger)
	if err != nil {
		t.Fatalf("NewDailyCron: %v", err)
	}
	done := make(chan struct{})
	go func() {
		cron.run(context.Background(), "daily")
		close(done)
	}()
	select {
	case <-planner.started:
	case <-time.After(2 * time.Second):
		t.Fatal("首轮未进入构建")
	}
	cron.run(context.Background(), "daily") // 单飞锁：后到者立即返回
	if planner.calls != 1 {
		t.Fatalf("构建次数 = %d, want 1（重叠轮必须跳过）", planner.calls)
	}
	if logText := logs.String(); !strings.Contains(logText, "daily build skipped") ||
		!strings.Contains(logText, "another run is in flight") {
		t.Fatalf("重叠轮缺 Warn 日志: %q", logText)
	}
	close(planner.release)
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("首轮未结束")
	}
}

// Stop 取消真实 waitContext 的 1 小时退避：必须 1s 内返回（M2）。
func TestDailyCronStopCancelsRealRetryWait(t *testing.T) {
	planner := &retryPlanner{failUntil: 100}
	cron, err := NewDailyCron(retryBuilder(planner), nil)
	if err != nil {
		t.Fatalf("NewDailyCron: %v", err)
	}
	cron.retryDelays = []time.Duration{time.Hour}
	entered := make(chan struct{})
	cron.wait = func(ctx context.Context, d time.Duration) bool {
		close(entered)
		return waitContext(ctx, d)
	}
	if err := cron.Start(); err != nil {
		t.Fatalf("Start: %v", err)
	}
	select {
	case <-entered:
	case <-time.After(2 * time.Second):
		t.Fatal("未进入退避等待")
	}
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	if err := cron.Stop(ctx); err != nil {
		t.Fatalf("Stop 未在 1s 内返回: %v", err)
	}
}
