package schedule_test

// 日切构建后的统计刷新（P0-3）：
// 只有真的铺了行才值得 ANALYZE；失败只记 Warn，不改构建结论。

import (
	"bytes"
	"context"
	"errors"
	"log/slog"
	"strings"
	"testing"

	appschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/schedule"
	coreschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/schedule"
)

// fakeAnalyzer 记录 ANALYZE 调用次数，可按需返回错误。
type fakeAnalyzer struct {
	calls int
	err   error
}

func (f *fakeAnalyzer) AnalyzeSignTasks(context.Context) error {
	f.calls++
	return f.err
}

// userAnalyzerBuilder 在 testBuilder 上接 Analyzer 与日志缓冲。
func userAnalyzerBuilder(tasks fakeTasks, planner *fakePlanner, an *fakeAnalyzer) (*appschedule.DailyBuilder, *bytes.Buffer) {
	b := testBuilder(tasks, planner)
	var buf bytes.Buffer
	b.Analyzer = an
	b.Logger = slog.New(slog.NewTextHandler(&buf, &slog.HandlerOptions{Level: slog.LevelDebug}))
	return b, &buf
}

// built>0 时恰好调用一次统计刷新。
func TestBuildTodayAnalyzesAfterBuilding(t *testing.T) {
	planner := &fakePlanner{}
	an := &fakeAnalyzer{}
	b, _ := userAnalyzerBuilder(fakeTasks{tasks: []coreschedule.TaskInfo{
		{ID: "t1", SchoolID: "s1", Enabled: true},
		{ID: "t2", SchoolID: "s2", Enabled: true},
	}}, planner, an)
	built, err := b.BuildToday(context.Background())
	if err != nil {
		t.Fatalf("BuildToday: %v", err)
	}
	if built != 2 || an.calls != 1 {
		t.Fatalf("built=%d analyze_calls=%d, want 2/1", built, an.calls)
	}
}

// 无任务：built=0，零调用。
func TestBuildTodaySkipsAnalyzeWithoutTasks(t *testing.T) {
	planner := &fakePlanner{}
	an := &fakeAnalyzer{}
	b, _ := userAnalyzerBuilder(fakeTasks{}, planner, an)
	built, err := b.BuildToday(context.Background())
	if err != nil {
		t.Fatalf("BuildToday: %v", err)
	}
	if built != 0 || an.calls != 0 {
		t.Fatalf("built=%d analyze_calls=%d, want 0/0（无任务不刷新统计）", built, an.calls)
	}
}

// 周末门关闭：built=0，零调用（连任务名册都不查）。
func TestBuildTodaySkipsAnalyzeWhenWeekendGateClosed(t *testing.T) {
	planner := &fakePlanner{}
	an := &fakeAnalyzer{}
	b := weekendBuilder(weekendSaturday, weekendTasks, planner, appschedule.BuildOverrides{})
	var buf bytes.Buffer
	b.Analyzer = an
	b.Logger = slog.New(slog.NewTextHandler(&buf, &slog.HandlerOptions{Level: slog.LevelDebug}))
	built, err := b.BuildToday(context.Background())
	if err != nil {
		t.Fatalf("BuildToday: %v", err)
	}
	if built != 0 || an.calls != 0 {
		t.Fatalf("built=%d analyze_calls=%d, want 0/0（门关不刷新统计）", built, an.calls)
	}
}

// 全部任务构建失败时 built=0，同样不刷新统计（失败已由返回错误表达）。
func TestBuildTodaySkipsAnalyzeWhenAllBuildsFailed(t *testing.T) {
	planner := &fakePlanner{failID: "t1"}
	an := &fakeAnalyzer{}
	b, _ := userAnalyzerBuilder(fakeTasks{tasks: []coreschedule.TaskInfo{
		{ID: "t1", SchoolID: "s1", Enabled: true},
	}}, planner, an)
	built, err := b.BuildToday(context.Background())
	if err == nil {
		t.Fatal("任务失败必须返回错误")
	}
	if built != 0 || an.calls != 0 {
		t.Fatalf("built=%d analyze_calls=%d, want 0/0（零行不刷新统计）", built, an.calls)
	}
}

// ANALYZE 失败只记 Warn：built 与返回错误都不变（统计陈旧不阻断排期）。
func TestBuildTodayAnalyzeFailureOnlyWarns(t *testing.T) {
	planner := &fakePlanner{}
	an := &fakeAnalyzer{err: errors.New("analyze failed")}
	b, buf := userAnalyzerBuilder(fakeTasks{tasks: []coreschedule.TaskInfo{
		{ID: "t1", SchoolID: "s1", Enabled: true},
		{ID: "t2", SchoolID: "s2", Enabled: true},
	}}, planner, an)
	built, err := b.BuildToday(context.Background())
	if err != nil {
		t.Fatalf("ANALYZE 失败不得改变返回错误: %v", err)
	}
	if built != 2 {
		t.Fatalf("built=%d, want 2（ANALYZE 失败不改构建结论）", built)
	}
	if an.calls != 1 {
		t.Fatalf("analyze_calls=%d, want 1", an.calls)
	}
	logged := buf.String()
	if !strings.Contains(logged, "level=WARN") {
		t.Fatalf("ANALYZE 失败必须产生 Warn 日志；got %q", logged)
	}
	if !strings.Contains(logged, "schedule.analyze_sign_tasks_failed") {
		t.Fatalf("Warn 日志需带事件词 schedule.analyze_sign_tasks_failed；got %q", logged)
	}
}
