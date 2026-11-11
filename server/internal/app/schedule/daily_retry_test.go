package schedule

// 日切失败后的当日重试（P1-4）。
//
// 判据：00:10 一次故障不再让全天无计划——失败后按 15/30/60 分钟三次重试，
// 成功即止；Stop 取消待执行的退避等待。
// 测试注入 wait 钩子，不真实等待 15 分钟。

import (
	"context"
	"errors"
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

// retryCron 构造日切宿主并注入记录延迟的 wait 钩子。
func retryCron(t *testing.T, planner *retryPlanner) (*DailyCron, *[]time.Duration) {
	t.Helper()
	b := &DailyBuilder{
		Service: planner, Tasks: retryTasks{},
		Clock:    coreclock.Fixed{T: time.Date(2026, 10, 30, 0, 5, 0, 0, retrySH)},
		Location: retrySH,
		Params: BuildParams{
			Workers: 4, Avg: 3 * time.Second, Rate: 1, Util: 0.8,
			SlotsPerSlice: 10, SignDist: SignDistFront,
		},
	}
	cron, err := NewDailyCron(b, nil)
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
