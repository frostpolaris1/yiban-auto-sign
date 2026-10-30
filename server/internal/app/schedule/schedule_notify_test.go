package schedule_test

// Notifier 发布点单测（P6）。
//
// 判据来自派单稿：Build/Override 成功且窗口真实变化后发
// window.changed；Build 的 Shortfall > 0 时发 capacity.changed
// （无历史值可比，取「有缺口即发」的保守口径，重复数值可能重复
// 发布——偏差已申报）。Preview 不发布。端口为 nil 时零开销，
// 现有全部用例即其回归。

import (
	"context"
	"testing"
	"time"

	appevents "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/events"
	appschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/schedule"
	coreschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/schedule"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/contract"
)

type recordedPlanEvent struct {
	schoolID string
	taskID   string
	day      string
	kind     string
	payload  map[string]any
}

type recordingPlanNotifier struct{ events []recordedPlanEvent }

func (n *recordingPlanNotifier) PlanEvent(_ context.Context, schoolID, taskID, day, kind string, payload map[string]any) {
	n.events = append(n.events, recordedPlanEvent{schoolID, taskID, day, kind, payload})
}

func (n *recordingPlanNotifier) kinds() []string {
	out := make([]string, len(n.events))
	for i, e := range n.events {
		out[i] = e.kind
	}
	return out
}

func newNotifyingSchedule(t *testing.T, w *fakeWindowStore, p *fakePlanStore, a fakeAccounts, d *fakeDiscover, n appschedule.Notifier) *appschedule.Service {
	t.Helper()
	svc, err := appschedule.NewService(appschedule.Deps{
		Windows: w, Plans: p, Accounts: a, Discover: d, Tx: &fakeTx{}, Notifier: n,
	})
	if err != nil {
		t.Fatalf("NewService: %v", err)
	}
	return svc
}

func notifyAccounts(n int) fakeAccounts {
	ids := make([]string, n)
	for i := range ids {
		ids[i] = "acct-" + string(rune('a'+i%26)) + string(rune('0'+i/26))
	}
	return fakeAccounts{ids: ids}
}

// 窗口首次落库（缓存为空）：Build 后发 window.changed。
// capacity.changed 随 Shortfall>0 出现、随 Shortfall==0 缺席。
func TestBuildPublishesWindowChanged(t *testing.T) {
	w := &fakeWindowStore{task: enabledTask()}
	p := &fakePlanStore{}
	d := &fakeDiscover{window: platformWindow(6, 30, 7, 50)}
	n := &recordingPlanNotifier{}
	svc := newNotifyingSchedule(t, w, p, notifyAccounts(500), d, n)

	prev, err := svc.Build(context.Background(), baseInput())
	if err != nil {
		t.Fatalf("Build: %v", err)
	}
	kinds := n.kinds()
	if len(kinds) == 0 || kinds[0] != appevents.TypeWindowChanged {
		t.Fatalf("事件序 = %v, want 首 %s", kinds, appevents.TypeWindowChanged)
	}
	e := n.events[0]
	if e.schoolID != "school-1" || e.taskID != "task-1" || e.day != "2026-10-29" {
		t.Fatalf("信封 = %s/%s/%s", e.schoolID, e.taskID, e.day)
	}
	if e.payload["source"] != string(contract.WindowPlatform) || e.payload["version"] != 1 {
		t.Fatalf("payload = %v", e.payload)
	}
	_, hasCap := false, false
	for _, k := range kinds {
		if k == appevents.TypeCapacityChanged {
			hasCap = true
		}
	}
	if prev.Shortfall > 0 && !hasCap {
		t.Fatalf("Shortfall=%d 却无 capacity.changed", prev.Shortfall)
	}
	if prev.Shortfall == 0 && hasCap {
		t.Fatal("Shortfall=0 却发了 capacity.changed")
	}
}

// 缺口拒建时仍要发 capacity.changed：拒建不是静默失败，
// 订阅方要能实时看到缺口数（C2）。window.changed 不发——本次没有落库。
func TestBuildShortfallRejectsAndPublishesCapacity(t *testing.T) {
	w := &fakeWindowStore{task: enabledTask()}
	p := &fakePlanStore{}
	d := &fakeDiscover{window: platformWindow(6, 30, 7, 50)}
	n := &recordingPlanNotifier{}
	svc := newNotifyingSchedule(t, w, p, notifyAccounts(500), d, n)
	in := baseInput()
	in.Workers = 1
	in.Avg = 60 * time.Second
	in.Rate = 1.0

	if _, err := svc.Build(context.Background(), in); err == nil {
		t.Fatal("缺口计划应被拒")
	}
	kinds := n.kinds()
	if len(kinds) != 1 || kinds[0] != appevents.TypeCapacityChanged {
		t.Fatalf("事件序 = %v, want 仅 %s", kinds, appevents.TypeCapacityChanged)
	}
	if e := n.events[0]; e.payload["demand"] != 500 {
		t.Fatalf("payload = %v, want demand=500", e.payload)
	}
	if len(p.inserted) != 0 {
		t.Fatalf("缺口计划不得落库, inserted = %d 批", len(p.inserted))
	}
}

// 窗口未变化的重算不发 window.changed（TestBuildUnchangedSkipsRearrange 同款设置）。
func TestBuildUnchangedDoesNotPublishWindowChanged(t *testing.T) {
	w := &fakeWindowStore{task: enabledTask()}
	d := &fakeDiscover{window: platformWindow(6, 30, 7, 50)}
	n := &recordingPlanNotifier{}
	svc := newNotifyingSchedule(t, w, &fakePlanStore{}, notifyAccounts(2), d, n)
	in := baseInput()

	if _, err := svc.Build(context.Background(), in); err != nil {
		t.Fatalf("第一次 Build: %v", err)
	}
	n.events = nil
	if _, err := svc.Build(context.Background(), in); err != nil {
		t.Fatalf("第二次 Build: %v", err)
	}
	for _, e := range n.events {
		if e.kind == appevents.TypeWindowChanged {
			t.Fatalf("窗口未变化仍发布：%+v", n.events)
		}
	}
}

// Override 成功后发 window.changed，来源为 override。
func TestOverridePublishesWindowChanged(t *testing.T) {
	w := &fakeWindowStore{task: coreschedule.TaskInfo{ID: "task-1", SchoolID: "school-1", Enabled: true}}
	d := &fakeDiscover{window: platformWindow(6, 30, 7, 50)}
	n := &recordingPlanNotifier{}
	svc := newNotifyingSchedule(t, w, &fakePlanStore{}, notifyAccounts(2), d, n)
	in := baseInput()
	start := time.Date(2026, 10, 29, 7, 0, 0, 0, shanghai)
	end := time.Date(2026, 10, 29, 7, 40, 0, 0, shanghai)
	scope := corescope.Master()
	if _, err := svc.Override(context.Background(), scope, in, start, end, "调度调整", "master-1"); err != nil {
		t.Fatalf("Override: %v", err)
	}
	if len(n.events) == 0 || n.events[0].kind != appevents.TypeWindowChanged {
		t.Fatalf("事件 = %+v", n.events)
	}
	if n.events[0].payload["source"] != string(contract.WindowOverride) {
		t.Fatalf("payload = %v", n.events[0].payload)
	}
}

// Preview 只算不写，不产生事件。
func TestPreviewDoesNotPublish(t *testing.T) {
	w := &fakeWindowStore{}
	d := &fakeDiscover{window: platformWindow(6, 30, 7, 50)}
	n := &recordingPlanNotifier{}
	svc := newNotifyingSchedule(t, w, &fakePlanStore{}, notifyAccounts(2), d, n)

	if _, err := svc.Preview(context.Background(), baseInput()); err != nil {
		t.Fatalf("Preview: %v", err)
	}
	if len(n.events) != 0 {
		t.Fatalf("Preview 发布了 %v", n.kinds())
	}
}
