package schedule_test

import (
	"context"
	"errors"
	"testing"
	"time"

	appschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/schedule"
	coreclock "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	coreschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/schedule"
)

var dailySH = time.FixedZone("CST", 8*3600)
var dailyNow = time.Date(2026, 10, 30, 0, 5, 0, 0, dailySH)

type fakeTasks struct {
	tasks []coreschedule.TaskInfo
	err   error
}

func (f fakeTasks) EnabledTasks(context.Context) ([]coreschedule.TaskInfo, error) {
	return f.tasks, f.err
}

type fakePlanner struct {
	calls    int
	failID   string
	lastDay  string
	lastTask string
}

func (f *fakePlanner) Build(_ context.Context, in appschedule.Input) (appschedule.Preview, error) {
	f.calls++
	f.lastDay, f.lastTask = in.Day, in.SchoolTaskID
	if in.SchoolTaskID == f.failID {
		return appschedule.Preview{}, errors.New("discover failed")
	}
	return appschedule.Preview{}, nil
}

func testBuilder(tasks fakeTasks, planner *fakePlanner) *appschedule.DailyBuilder {
	return &appschedule.DailyBuilder{
		Service: planner, Tasks: tasks,
		Clock: coreclock.Fixed{T: dailyNow}, Location: dailySH,
		Params: appschedule.BuildParams{
			Workers: 4, Avg: 3 * time.Second, Rate: 1, Util: 0.8,
			SlotsPerSlice: 10, Front: true,
		},
	}
}

// 日切构建覆盖全部启用任务，业务日取时钟与时区。
func TestBuildTodayBuildsEveryTask(t *testing.T) {
	planner := &fakePlanner{}
	b := testBuilder(fakeTasks{tasks: []coreschedule.TaskInfo{
		{ID: "t1", SchoolID: "s1", Enabled: true},
		{ID: "t2", SchoolID: "s2", Enabled: true},
	}}, planner)
	built, err := b.BuildToday(context.Background())
	if err != nil {
		t.Fatalf("BuildToday: %v", err)
	}
	if built != 2 || planner.calls != 2 {
		t.Fatalf("built=%d calls=%d, want 2/2", built, planner.calls)
	}
	if planner.lastDay != "2026-10-30" {
		t.Fatalf("day = %q, want 2026-10-30（业务日）", planner.lastDay)
	}
}

// 单个任务失败不阻断其余任务；返回首个错误与已建数。
func TestBuildTodayContinuesAfterFailure(t *testing.T) {
	planner := &fakePlanner{failID: "t1"}
	b := testBuilder(fakeTasks{tasks: []coreschedule.TaskInfo{
		{ID: "t1", SchoolID: "s1", Enabled: true},
		{ID: "t2", SchoolID: "s2", Enabled: true},
	}}, planner)
	built, err := b.BuildToday(context.Background())
	if err == nil {
		t.Fatal("首个任务失败必须返回错误")
	}
	if built != 1 || planner.calls != 2 {
		t.Fatalf("built=%d calls=%d, want 1/2", built, planner.calls)
	}
}

// 任务列表失败即整体失败；不调用任何构建。
func TestBuildTodayListFailure(t *testing.T) {
	planner := &fakePlanner{}
	b := testBuilder(fakeTasks{err: errors.New("db down")}, planner)
	if _, err := b.BuildToday(context.Background()); err == nil {
		t.Fatal("列表失败必须返回错误")
	}
	if planner.calls != 0 {
		t.Fatalf("calls = %d, want 0", planner.calls)
	}
}

// 启动补建在后台执行；Stop 等它结束。
func TestDailyCronStartupBackfillAndStop(t *testing.T) {
	planner := &fakePlanner{}
	b := testBuilder(fakeTasks{tasks: []coreschedule.TaskInfo{
		{ID: "t1", SchoolID: "s1", Enabled: true},
	}}, planner)
	cron, err := appschedule.NewDailyCron(b, nil)
	if err != nil {
		t.Fatalf("NewDailyCron: %v", err)
	}
	if err := cron.Start(); err != nil {
		t.Fatalf("Start: %v", err)
	}
	deadline := time.After(2 * time.Second)
	for planner.calls == 0 {
		select {
		case <-deadline:
			t.Fatal("启动补建未执行")
		case <-time.After(10 * time.Millisecond):
		}
	}
	if err := cron.Stop(context.Background()); err != nil {
		t.Fatalf("Stop: %v", err)
	}
	// 停止后不得再触发构建。
	before := planner.calls
	time.Sleep(50 * time.Millisecond)
	if planner.calls != before {
		t.Fatalf("Stop 后仍有构建：%d -> %d", before, planner.calls)
	}
}

// 未启动即停止是空操作。
func TestDailyCronStopWithoutStart(t *testing.T) {
	b := testBuilder(fakeTasks{}, &fakePlanner{})
	cron, err := appschedule.NewDailyCron(b, nil)
	if err != nil {
		t.Fatalf("NewDailyCron: %v", err)
	}
	if err := cron.Stop(context.Background()); err != nil {
		t.Fatalf("Stop: %v", err)
	}
}

// 参数不全即构造失败，不静默跳过。
func TestNewDailyCronRequiresBuilder(t *testing.T) {
	if _, err := appschedule.NewDailyCron(nil, nil); err == nil {
		t.Fatal("nil builder 必须失败")
	}
}
