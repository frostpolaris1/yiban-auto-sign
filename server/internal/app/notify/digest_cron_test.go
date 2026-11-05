package notify_test

// 0v8 C 每日摘要宿主的判据：启动补发、业务日窗口、panic recover 与 Stop。
//
// 覆盖：Start 后台补发上一业务日且只补最近 1 天；Stop 幂等、可等待；
// 未启动即停止是空操作；panic 只记日志不终止进程；构造缺参即失败。
// 09:00 触发时刻由 digestSpec 常量固定，单测不读系统时钟等真实触发。

import (
	"bytes"
	"context"
	"log/slog"
	"strings"
	"testing"
	"time"

	appnotify "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/notify"
	coreclock "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	corenotify "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/notify"
)

// digestSH 是测试时区（上海，无夏令时）。
var digestSH = time.FixedZone("CST", 8*3600)

// 启动补发查上一业务日，只查一次；更旧不补。
func TestDigestCronStartupBackfillsPreviousBusinessDay(t *testing.T) {
	called := make(chan string, 4)
	source := &fakeDigestSource{tasks: map[string][]corenotify.FailedTask{}, called: called}
	dispatcher := newDigest(t, newFakeStore(), source, &fakeDigestAudience{},
		&fakeSender{}, newManualClock(), nil)
	now := time.Date(2026, 10, 31, 8, 0, 0, 0, digestSH)
	cron, err := appnotify.NewDigestCron(dispatcher, coreclock.Fixed{T: now}, digestSH, nil)
	if err != nil {
		t.Fatalf("NewDigestCron: %v", err)
	}
	if err := cron.Start(); err != nil {
		t.Fatalf("Start: %v", err)
	}
	select {
	case day := <-called:
		if day != "2026-10-30" {
			t.Fatalf("补发业务日 = %q, want 2026-10-30", day)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("启动补发未执行")
	}
	// 只补最近 1 天：没有第二次查询。
	select {
	case day := <-called:
		t.Fatalf("多补了业务日 %q", day)
	case <-time.After(50 * time.Millisecond):
	}
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	if err := cron.Stop(ctx); err != nil {
		t.Fatalf("Stop: %v", err)
	}
	cron.Stop(ctx) // 幂等
	if days := source.queriedDays(); len(days) != 1 || days[0] != "2026-10-30" {
		t.Fatalf("查询业务日 = %v", days)
	}
}

// 补发 panic 只记日志：Stop 仍能结束，进程不崩。
func TestDigestCronRecoversPanic(t *testing.T) {
	called := make(chan string, 1)
	source := &fakeDigestSource{
		tasks: map[string][]corenotify.FailedTask{}, panicNow: true, called: called,
	}
	var logs bytes.Buffer
	dispatcher := newDigest(t, newFakeStore(), source, &fakeDigestAudience{},
		&fakeSender{}, newManualClock(), &logs)
	cron, err := appnotify.NewDigestCron(dispatcher,
		coreclock.Fixed{T: time.Date(2026, 10, 31, 8, 0, 0, 0, digestSH)}, digestSH,
		slog.New(slog.NewTextHandler(&logs, nil)))
	if err != nil {
		t.Fatalf("NewDigestCron: %v", err)
	}
	if err := cron.Start(); err != nil {
		t.Fatalf("Start: %v", err)
	}
	select {
	case <-called:
	case <-time.After(2 * time.Second):
		t.Fatal("补发未执行")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	if err := cron.Stop(ctx); err != nil {
		t.Fatalf("Stop: %v", err)
	}
	if !strings.Contains(logs.String(), "daily digest panicked") {
		t.Fatalf("缺 panic 日志：%s", logs.String())
	}
}

// 未启动即停止是空操作。
func TestDigestCronStopWithoutStart(t *testing.T) {
	dispatcher := newDigest(t, newFakeStore(), newFakeDigestSource(),
		&fakeDigestAudience{}, &fakeSender{}, newManualClock(), nil)
	cron, err := appnotify.NewDigestCron(dispatcher,
		coreclock.Fixed{T: time.Date(2026, 10, 31, 8, 0, 0, 0, digestSH)}, digestSH, nil)
	if err != nil {
		t.Fatalf("NewDigestCron: %v", err)
	}
	if err := cron.Stop(context.Background()); err != nil {
		t.Fatalf("Stop: %v", err)
	}
}

// 参数不全即构造失败，不静默跳过。
func TestNewDigestCronRequiresDeps(t *testing.T) {
	dispatcher := newDigest(t, newFakeStore(), newFakeDigestSource(),
		&fakeDigestAudience{}, &fakeSender{}, newManualClock(), nil)
	now := coreclock.Fixed{T: time.Date(2026, 10, 31, 8, 0, 0, 0, digestSH)}
	if _, err := appnotify.NewDigestCron(nil, now, digestSH, nil); err == nil {
		t.Fatal("nil dispatcher 必须失败")
	}
	if _, err := appnotify.NewDigestCron(dispatcher, nil, digestSH, nil); err == nil {
		t.Fatal("nil clock 必须失败")
	}
	if _, err := appnotify.NewDigestCron(dispatcher, now, nil, nil); err == nil {
		t.Fatal("nil location 必须失败")
	}
}
