package schedule_test

// 0v8 B 接线判据：容量缺口拒建点必须把事件交给通知端口。
//
// 覆盖：Shortfall>0 时 Build 发管理员事件（数字与预览同源）；
// 无缺口与 Preview 不发；Notifier 为 nil 时通知照发（两端口各自判空）；
// 重复缺口重复提交同一业务键（库层唯一键吸收）；
// 端口失败或 panic 不改 Build 的拒建结论。

import (
	"bytes"
	"context"
	"errors"
	"log/slog"
	"strings"
	"testing"
	"time"

	appschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/schedule"
	corenotify "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/notify"
)

// recordingCapacityAlerts 记录容量缺口事件。可注入错误或 panic。
type recordingCapacityAlerts struct {
	events   []corenotify.Event
	err      error
	panicNow bool
}

func (a *recordingCapacityAlerts) Dispatch(_ context.Context, event corenotify.Event) error {
	if a.panicNow {
		panic("capacity alert panic")
	}
	a.events = append(a.events, event)
	return a.err
}

func newCapacityAlertSchedule(t *testing.T, w *fakeWindowStore, p *fakePlanStore,
	a fakeAccounts, d *fakeDiscover, alerts appschedule.CapacityAlerts) *appschedule.Service {
	t.Helper()
	svc, err := appschedule.NewService(appschedule.Deps{
		Windows: w, Plans: p, Accounts: a, Discover: d, Tx: &fakeTx{},
		CapacityAlerts: alerts,
	})
	if err != nil {
		t.Fatalf("NewService: %v", err)
	}
	return svc
}

// shortfallInput 让 500 个账号在 1 通道、60s 尝试下必然缺口。
func shortfallInput() appschedule.Input {
	in := baseInput()
	in.Workers = 1
	in.Avg = 60 * time.Second
	in.Rate = 1.0
	return in
}

// 缺口拒建：事件带业务键与预览里的数字。
func TestBuildDispatchesCapacityAlertOnShortfall(t *testing.T) {
	w := &fakeWindowStore{task: enabledTask()}
	p := &fakePlanStore{}
	d := &fakeDiscover{window: platformWindow(6, 30, 7, 50)}
	alerts := &recordingCapacityAlerts{}
	svc := newCapacityAlertSchedule(t, w, p, notifyAccounts(500), d, alerts)

	if _, err := svc.Build(context.Background(), shortfallInput()); err == nil {
		t.Fatal("缺口计划应被拒")
	}
	if len(alerts.events) != 1 {
		t.Fatalf("容量事件 = %d, want 1", len(alerts.events))
	}
	event := alerts.events[0]
	if event.Category != corenotify.CategoryCapacityRejected ||
		event.SchoolID != "school-1" || event.Day != "2026-10-29" ||
		event.DedupeKey != "capacity_rejected/task-1/2026-10-29" {
		t.Fatalf("事件 = %+v", event)
	}
	if event.Capacity.Demand != 500 || event.Capacity.Shortfall <= 0 ||
		event.Capacity.Binding != "workers" || event.Capacity.SuggestedWorkers != 8 {
		t.Fatalf("容量数字 = %+v", event.Capacity)
	}
}

// 两个端口各自判空：缺 SSE 端口时通知仍要发。
func TestCapacityAlertDispatchesWithoutNotifier(t *testing.T) {
	w := &fakeWindowStore{task: enabledTask()}
	d := &fakeDiscover{window: platformWindow(6, 30, 7, 50)}
	alerts := &recordingCapacityAlerts{}
	svc := newCapacityAlertSchedule(t, w, &fakePlanStore{}, notifyAccounts(500), d, alerts)

	if _, err := svc.Build(context.Background(), shortfallInput()); err == nil {
		t.Fatal("缺口计划应被拒")
	}
	if len(alerts.events) != 1 {
		t.Fatalf("容量事件 = %d, want 1（SSE 端口为 nil 不影响通知）", len(alerts.events))
	}
}

// 无缺口与 Preview 都不发：只有拒建点产生事件。
func TestCapacityAlertAbsentWithoutShortfall(t *testing.T) {
	w := &fakeWindowStore{task: enabledTask()}
	d := &fakeDiscover{window: platformWindow(6, 30, 7, 50)}
	alerts := &recordingCapacityAlerts{}
	svc := newCapacityAlertSchedule(t, w, &fakePlanStore{}, notifyAccounts(2), d, alerts)

	if _, err := svc.Preview(context.Background(), baseInput()); err != nil {
		t.Fatalf("Preview: %v", err)
	}
	if _, err := svc.Build(context.Background(), baseInput()); err != nil {
		t.Fatalf("Build: %v", err)
	}
	if len(alerts.events) != 0 {
		t.Fatalf("无缺口仍投递 %d 条", len(alerts.events))
	}
}

// 缺口期重复触发：调度层每次都发，业务键相同，由库层唯一键吸收成一行。
func TestCapacityAlertRepeatsSameDedupeKey(t *testing.T) {
	w := &fakeWindowStore{task: enabledTask()}
	d := &fakeDiscover{window: platformWindow(6, 30, 7, 50)}
	alerts := &recordingCapacityAlerts{}
	svc := newCapacityAlertSchedule(t, w, &fakePlanStore{}, notifyAccounts(500), d, alerts)

	for i := 0; i < 2; i++ {
		if _, err := svc.Build(context.Background(), shortfallInput()); err == nil {
			t.Fatal("缺口计划应被拒")
		}
	}
	if len(alerts.events) != 2 {
		t.Fatalf("事件 = %d, want 2", len(alerts.events))
	}
	if alerts.events[0].DedupeKey != alerts.events[1].DedupeKey {
		t.Fatalf("业务键漂移：%q vs %q", alerts.events[0].DedupeKey, alerts.events[1].DedupeKey)
	}
}

// 端口失败或 panic 不改拒建结论：Build 仍返回 shortfall 错误。
func TestCapacityAlertFailureDoesNotChangeRejection(t *testing.T) {
	for _, tc := range []struct {
		name   string
		alerts *recordingCapacityAlerts
	}{
		{"错误", &recordingCapacityAlerts{err: errors.New("dispatch failed")}},
		{"panic", &recordingCapacityAlerts{panicNow: true}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			w := &fakeWindowStore{task: enabledTask()}
			d := &fakeDiscover{window: platformWindow(6, 30, 7, 50)}
			svc := newCapacityAlertSchedule(t, w, &fakePlanStore{}, notifyAccounts(500), d, tc.alerts)

			if _, err := svc.Build(context.Background(), shortfallInput()); err == nil {
				t.Fatal("缺口计划应被拒，投递失败不得吞掉结论")
			}
		})
	}
}

// 投递端口 panic 要留一条不含 panic 值的显性告警。
func TestCapacityAlertPanicIsLogged(t *testing.T) {
	var logs bytes.Buffer
	svc, err := appschedule.NewService(appschedule.Deps{
		Windows:        &fakeWindowStore{task: enabledTask()},
		Plans:          &fakePlanStore{},
		Accounts:       notifyAccounts(500),
		Discover:       &fakeDiscover{window: platformWindow(6, 30, 7, 50)},
		Tx:             &fakeTx{},
		CapacityAlerts: &recordingCapacityAlerts{panicNow: true},
		Logger:         slog.New(slog.NewTextHandler(&logs, nil)),
	})
	if err != nil {
		t.Fatalf("NewService: %v", err)
	}
	if _, err := svc.Build(context.Background(), shortfallInput()); err == nil {
		t.Fatal("缺口计划应被拒")
	}
	logged := logs.String()
	if !strings.Contains(logged, "notify.dispatch_panic") {
		t.Fatalf("缺 notify.dispatch_panic 告警：%s", logged)
	}
	if strings.Contains(logged, "capacity alert panic") {
		t.Fatalf("日志复述了 panic 值：%s", logged)
	}
}
