package worker_test

// Notifier 发布点单测（P6）。
//
// 判据来自派单稿：finish 成功路径后发 task.concluded
// （payload：account_id、state、attempt）；RequeueWait/RequeueRetry
// 后发 task.progress；发布不得阻塞或改变结算语义——发布方 panic
// 时结算照常落库，事件丢弃。
// attempt 口径：事件落库后的 sign_tasks.attempts（已消耗尝试数）。
// Finish 与 RequeueRetry 各计一次，RequeueWait 不计。

import (
	"context"
	"errors"
	"sync"
	"testing"

	appevents "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/events"
	appworker "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/worker"
	coreclock "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	corestatus "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/status"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/contract"
)

type recordedEvent struct {
	schoolID string
	taskID   string
	day      string
	kind     string
	payload  map[string]any
}

type recordingNotifier struct {
	mu     sync.Mutex
	events []recordedEvent
}

func (n *recordingNotifier) TaskEvent(_ context.Context, schoolID, taskID, day, kind string, payload map[string]any) {
	n.mu.Lock()
	defer n.mu.Unlock()
	n.events = append(n.events, recordedEvent{schoolID, taskID, day, kind, payload})
}

type panickingNotifier struct{ called *bool }

func (n panickingNotifier) TaskEvent(context.Context, string, string, string, string, map[string]any) {
	*n.called = true
	panic("notifier exploded")
}

func newNotifyingService(t *testing.T, st *fakeStore, signer *fakeSigner, limiter fakeLimiter, n appworker.Notifier) *appworker.Service {
	t.Helper()
	svc, err := appworker.NewService(appworker.Deps{
		Store: st, Creds: fakeCreds{}, Signer: signer, Egress: fakeEgress{},
		Limiter: limiter, Clock: coreclock.Fixed{T: testNow()}, Location: loc, Notifier: n,
	})
	if err != nil {
		t.Fatalf("NewService: %v", err)
	}
	return svc
}

// 成功结算后恰发一条 task.concluded，信封与 payload 齐全。
func TestConcludedPublishedOnFinish(t *testing.T) {
	st := newStore(baseTask())
	st.windows["st-1|2026-10-29"] = openWindow()
	n := &recordingNotifier{}
	svc := newNotifyingService(t, st, defaultSigner(), fakeLimiter{allow: true}, n)

	if got, err := svc.Step(context.Background(), 10); err != nil || got != 1 {
		t.Fatalf("Step = (%d,%v)", got, err)
	}
	if len(n.events) != 1 {
		t.Fatalf("事件数 = %d, want 1", len(n.events))
	}
	e := n.events[0]
	if e.kind != appevents.TypeTaskConcluded {
		t.Fatalf("kind = %s, want %s", e.kind, appevents.TypeTaskConcluded)
	}
	if e.schoolID != "school-1" || e.taskID != "st-1" || e.day != "2026-10-29" {
		t.Fatalf("信封 = %s/%s/%s", e.schoolID, e.taskID, e.day)
	}
	if e.payload["account_id"] != "acct-1" || e.payload["state"] != string(corestatus.TaskSuccess) {
		t.Fatalf("payload = %v", e.payload)
	}
	if e.payload["attempt"] != 1 { // Finish 计一次尝试：0+1
		t.Fatalf("attempt = %v, want 1", e.payload["attempt"])
	}
}

// 限速退避后发 task.progress，attempt 不变（等待不计尝试）。
func TestProgressPublishedOnRateWait(t *testing.T) {
	st := newStore(baseTask())
	st.windows["st-1|2026-10-29"] = openWindow()
	n := &recordingNotifier{}
	svc := newNotifyingService(t, st, defaultSigner(), fakeLimiter{allow: false}, n)

	if _, err := svc.Step(context.Background(), 10); err != nil {
		t.Fatalf("Step: %v", err)
	}
	if len(n.events) != 1 || n.events[0].kind != appevents.TypeTaskProgress {
		t.Fatalf("事件 = %+v", n.events)
	}
	if n.events[0].payload["attempt"] != 0 {
		t.Fatalf("attempt = %v, want 0", n.events[0].payload["attempt"])
	}
}

// 业务失败退避后发 task.progress，attempt 已计一次（RequeueRetry 语义）。
func TestProgressPublishedOnRetryBackoff(t *testing.T) {
	st := newStore(baseTask())
	st.windows["st-1|2026-10-29"] = openWindow()
	signer := defaultSigner()
	signer.outcome = contract.OutcomeTransient
	n := &recordingNotifier{}
	svc := newNotifyingService(t, st, signer, fakeLimiter{allow: true}, n)

	if _, err := svc.Step(context.Background(), 10); err != nil {
		t.Fatalf("Step: %v", err)
	}
	if len(n.events) != 1 || n.events[0].kind != appevents.TypeTaskProgress {
		t.Fatalf("事件 = %+v", n.events)
	}
	if n.events[0].payload["attempt"] != 1 {
		t.Fatalf("attempt = %v, want 1", n.events[0].payload["attempt"])
	}
}

// 窗口未发现的重排也发 progress（同族调用点同口径）。
func TestProgressPublishedOnWindowMissing(t *testing.T) {
	st := newStore(baseTask()) // 无窗口缓存
	n := &recordingNotifier{}
	svc := newNotifyingService(t, st, defaultSigner(), fakeLimiter{allow: true}, n)

	if _, err := svc.Step(context.Background(), 10); err != nil {
		t.Fatalf("Step: %v", err)
	}
	if len(n.events) != 1 || n.events[0].kind != appevents.TypeTaskProgress {
		t.Fatalf("事件 = %+v", n.events)
	}
}

// 发布方 panic 只丢事件：结算照常落库，Step 正常返回。
func TestNotifierPanicDoesNotBreakSettlement(t *testing.T) {
	st := newStore(baseTask())
	st.windows["st-1|2026-10-29"] = openWindow()
	called := false
	n := panickingNotifier{called: &called}
	svc := newNotifyingService(t, st, defaultSigner(), fakeLimiter{allow: true}, n)

	got, err := svc.Step(context.Background(), 10)
	if err != nil || got != 1 {
		t.Fatalf("Step = (%d,%v), want (1,nil)", got, err)
	}
	if !called {
		t.Fatal("发布方未被调用")
	}
	if st.finished["t1"] != corestatus.TaskSuccess {
		t.Fatalf("终态 = %v, want success", st.finished["t1"])
	}
}

// 基础设施失败退回时，成功行发 concluded、退回行发 progress。
// 批内并行，事件顺序不固定：只断言两种事件各一条（不测编舞）。
func TestProgressPublishedForBatchRelease(t *testing.T) {
	t1 := baseTask()
	t2 := baseTask()
	t2.ID = "t2"
	t2.AccountID = "acct-2"
	t2.SchoolTaskID = "st-2"
	st := newStore(t1, t2)
	st.windows["st-1|2026-10-29"] = openWindow()
	st.windowErrFor["st-2"] = errors.New("db down")
	n := &recordingNotifier{}
	svc := newNotifyingService(t, st, defaultSigner(), fakeLimiter{allow: true}, n)

	if _, err := svc.Step(context.Background(), 10); err == nil {
		t.Fatal("Step 应上抛基础设施错误")
	}
	if len(n.events) != 2 {
		t.Fatalf("事件数 = %d, want 2", len(n.events))
	}
	kinds := map[string]int{}
	for _, e := range n.events {
		kinds[e.kind]++
	}
	if kinds[appevents.TypeTaskConcluded] != 1 || kinds[appevents.TypeTaskProgress] != 1 {
		t.Fatalf("事件计数 = %v, want concluded 与 progress 各一条", kinds)
	}
}
