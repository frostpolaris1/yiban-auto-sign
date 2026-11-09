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
	// done 非 nil 时每次 Build 投递一个信号；用于等待后台补建。
	done chan struct{}
	// lastInput 记录最近一次 Build 的参数（sett2 覆盖链断言用）。
	lastInput appschedule.Input
}

func (f *fakePlanner) Build(_ context.Context, in appschedule.Input) (appschedule.Preview, error) {
	f.calls++
	if f.done != nil {
		select {
		case f.done <- struct{}{}:
		default:
		}
	}
	f.lastDay, f.lastTask = in.Day, in.SchoolTaskID
	f.lastInput = in
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
	done := make(chan struct{}, 1)
	planner := &fakePlanner{done: done}
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
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("启动补建未执行")
	}
	// Stop 等到补建 goroutine 结束；此后 planner 不再被访问。
	if err := cron.Stop(context.Background()); err != nil {
		t.Fatalf("Stop: %v", err)
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

// sett2 覆盖链：构建时取 DB 解析值建当日计划；解析失败则本轮构建失败。
func TestBuildTodayUsesResolvedOverrides(t *testing.T) {
	planner := &fakePlanner{}
	b := testBuilder(fakeTasks{tasks: []coreschedule.TaskInfo{
		{ID: "t1", SchoolID: "s1", Enabled: true},
	}}, planner)
	b.Params.UsePrefs = false
	b.Params.EdgeFront, b.Params.EdgeBack = 60*time.Second, 60*time.Second
	b.Overrides = func(context.Context) (appschedule.BuildOverrides, error) {
		return appschedule.BuildOverrides{
			UsePrefs: true, EdgeFront: 120 * time.Second, EdgeBack: 30 * time.Second,
		}, nil
	}
	if _, err := b.BuildToday(context.Background()); err != nil {
		t.Fatalf("BuildToday: %v", err)
	}
	in := planner.lastInput
	if !in.UsePrefs || in.EdgeFront != 120*time.Second || in.EdgeBack != 30*time.Second {
		t.Fatalf("input = usePrefs %v front %v back %v, want db resolved values", in.UsePrefs, in.EdgeFront, in.EdgeBack)
	}
}

// 未接写回层（Overrides 为 nil）时构建用 Params 原值：行为与基线一致。
func TestBuildTodayWithoutOverridesKeepsParams(t *testing.T) {
	planner := &fakePlanner{}
	b := testBuilder(fakeTasks{tasks: []coreschedule.TaskInfo{
		{ID: "t1", SchoolID: "s1", Enabled: true},
	}}, planner)
	b.Params.UsePrefs = true
	b.Params.EdgeFront, b.Params.EdgeBack = 45*time.Second, 15*time.Second
	if _, err := b.BuildToday(context.Background()); err != nil {
		t.Fatalf("BuildToday: %v", err)
	}
	in := planner.lastInput
	if !in.UsePrefs || in.EdgeFront != 45*time.Second || in.EdgeBack != 15*time.Second {
		t.Fatalf("input = usePrefs %v front %v back %v, want params values", in.UsePrefs, in.EdgeFront, in.EdgeBack)
	}
}

// 解析失败（存储故障）时本轮构建失败：错值建计划比不建更糟。
func TestBuildTodayOverridesFailureFailsBuild(t *testing.T) {
	planner := &fakePlanner{}
	b := testBuilder(fakeTasks{tasks: []coreschedule.TaskInfo{
		{ID: "t1", SchoolID: "s1", Enabled: true},
	}}, planner)
	b.Overrides = func(context.Context) (appschedule.BuildOverrides, error) {
		return appschedule.BuildOverrides{}, errors.New("db down")
	}
	built, err := b.BuildToday(context.Background())
	if err == nil || built != 0 {
		t.Fatalf("BuildToday = (%d, %v), want 0 builds with error", built, err)
	}
	if planner.calls != 0 {
		t.Fatalf("Build calls = %d, want 0", planner.calls)
	}
}

// ===== 批 3b：周末门（schedule.sunday_sign / saturday_sign）=====
//
// 判据：按当日 weekday 判对应键，缺行=false=当日不铺任何计划行
//（返回 0 行零构建，行为与旧线缺省一致——这是本批唯一预期行为反转）；
// 键=true 照常铺行；读取失败上抛不铺（保守方向，急停同向）。

// weekendBuilder 构造指定时刻的日切构建器（周末门测试用）。
func weekendBuilder(now time.Time, tasks fakeTasks, planner *fakePlanner, ov appschedule.BuildOverrides) *appschedule.DailyBuilder {
	b := testBuilder(tasks, planner)
	b.Clock = coreclock.Fixed{T: now}
	b.Overrides = func(context.Context) (appschedule.BuildOverrides, error) {
		return ov, nil
	}
	return b
}

// 2026-11-14 是周六、2026-11-15 是周日（业务日 00:10 触发形态）。
var (
	weekendSaturday = time.Date(2026, 11, 14, 0, 10, 0, 0, dailySH)
	weekendSunday   = time.Date(2026, 11, 15, 0, 10, 0, 0, dailySH)
)

var weekendTasks = fakeTasks{tasks: []coreschedule.TaskInfo{
	{ID: "t1", SchoolID: "s1", Enabled: true},
	{ID: "t2", SchoolID: "s2", Enabled: true},
}}

// 周六键缺行（false）：当日不铺任何计划行——行为反转钉住。
func TestBuildTodayWeekendGateClosesSaturday(t *testing.T) {
	planner := &fakePlanner{}
	b := weekendBuilder(weekendSaturday, weekendTasks, planner, appschedule.BuildOverrides{})
	built, err := b.BuildToday(context.Background())
	if err != nil {
		t.Fatalf("BuildToday: %v", err)
	}
	if built != 0 || planner.calls != 0 {
		t.Fatalf("built=%d calls=%d, want 0/0（周六门关闭）", built, planner.calls)
	}
}

// 周日键缺行（false）：当日不铺任何计划行。
func TestBuildTodayWeekendGateClosesSunday(t *testing.T) {
	planner := &fakePlanner{}
	b := weekendBuilder(weekendSunday, weekendTasks, planner, appschedule.BuildOverrides{})
	built, err := b.BuildToday(context.Background())
	if err != nil {
		t.Fatalf("BuildToday: %v", err)
	}
	if built != 0 || planner.calls != 0 {
		t.Fatalf("built=%d calls=%d, want 0/0（周日门关闭）", built, planner.calls)
	}
}

// 键=true 照常铺行：周六开闸铺全部任务。
func TestBuildTodayWeekendGateOpenSaturday(t *testing.T) {
	planner := &fakePlanner{}
	b := weekendBuilder(weekendSaturday, weekendTasks, planner,
		appschedule.BuildOverrides{SaturdaySign: true})
	built, err := b.BuildToday(context.Background())
	if err != nil {
		t.Fatalf("BuildToday: %v", err)
	}
	if built != 2 || planner.calls != 2 {
		t.Fatalf("built=%d calls=%d, want 2/2", built, planner.calls)
	}
	if planner.lastDay != "2026-11-14" {
		t.Fatalf("day = %q, want 2026-11-14（周六业务日）", planner.lastDay)
	}
}

// 键=true 照常铺行：周日开闸铺全部任务。
func TestBuildTodayWeekendGateOpenSunday(t *testing.T) {
	planner := &fakePlanner{}
	b := weekendBuilder(weekendSunday, weekendTasks, planner,
		appschedule.BuildOverrides{SundaySign: true})
	built, err := b.BuildToday(context.Background())
	if err != nil {
		t.Fatalf("BuildToday: %v", err)
	}
	if built != 2 || planner.calls != 2 {
		t.Fatalf("built=%d calls=%d, want 2/2", built, planner.calls)
	}
}

// 周末门读取失败上抛不铺：保守方向（急停同 3a），错值不铺。
func TestBuildTodayWeekendGateFailureFailsBuild(t *testing.T) {
	planner := &fakePlanner{}
	b := weekendBuilder(weekendSaturday, weekendTasks, planner, appschedule.BuildOverrides{})
	b.Overrides = func(context.Context) (appschedule.BuildOverrides, error) {
		return appschedule.BuildOverrides{}, errors.New("db down")
	}
	built, err := b.BuildToday(context.Background())
	if err == nil || built != 0 {
		t.Fatalf("BuildToday = (%d, %v), want failure with 0 builds", built, err)
	}
	if planner.calls != 0 {
		t.Fatalf("Build calls = %d, want 0", planner.calls)
	}
}
