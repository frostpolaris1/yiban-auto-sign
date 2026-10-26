package worker_test

import (
	"context"
	"errors"
	"testing"
	"time"

	appworker "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/worker"
	coreclock "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	coreegress "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/egress"
	coreerrs "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	coreschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/schedule"
	corestatus "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/status"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/contract"
)

// ---- 替身：任务队列状态机与库语义等价（pending/running/终态、单账号串行）----

type fakeStore struct {
	tasks     []coreschedule.TaskRow
	windows   map[string]coreschedule.WindowRecord
	todayTask string
	// 观测
	claimed  []string
	finished map[string]corestatus.TaskState
	results  map[string]string
	requeued map[string]time.Time
	resets   int64
}

func newStore(tasks ...coreschedule.TaskRow) *fakeStore {
	return &fakeStore{
		tasks:    tasks,
		windows:  map[string]coreschedule.WindowRecord{},
		finished: map[string]corestatus.TaskState{},
		results:  map[string]string{},
		requeued: map[string]time.Time{},
	}
}

func (f *fakeStore) ClaimDue(_ context.Context, now time.Time, limit int) ([]coreschedule.TaskRow, error) {
	runningAccount := map[string]bool{}
	for _, t := range f.tasks {
		if t.State == string(corestatus.TaskRunning) {
			runningAccount[t.AccountID] = true
		}
	}
	var out []coreschedule.TaskRow
	for i := range f.tasks {
		t := &f.tasks[i]
		if len(out) >= limit {
			break
		}
		if t.State != string(corestatus.TaskPending) || t.RunAt.After(now) {
			continue
		}
		if runningAccount[t.AccountID] {
			continue // 同账号串行：已有 running 不领
		}
		t.State = string(corestatus.TaskRunning)
		runningAccount[t.AccountID] = true
		f.claimed = append(f.claimed, t.ID)
		out = append(out, *t)
	}
	return out, nil
}

func (f *fakeStore) Finish(_ context.Context, accountID, schoolTaskID, day string, state corestatus.TaskState, result string, _ time.Time) error {
	for i := range f.tasks {
		t := &f.tasks[i]
		if t.AccountID == accountID && t.SchoolTaskID == schoolTaskID && t.Day == day {
			if t.State != string(corestatus.TaskRunning) {
				return coreerrs.ErrNotFound
			}
			t.State = string(state)
			t.Attempts++
			f.finished[t.ID] = state
			f.results[t.ID] = result
			return nil
		}
	}
	return coreerrs.ErrNotFound
}

func (f *fakeStore) Requeue(_ context.Context, taskID string, runAt time.Time, result string) error {
	for i := range f.tasks {
		t := &f.tasks[i]
		if t.ID == taskID && t.State == string(corestatus.TaskRunning) {
			t.State = string(corestatus.TaskPending)
			t.RunAt = runAt
			t.Attempts++
			f.requeued[taskID] = runAt
			f.results[taskID] = result
			return nil
		}
	}
	return coreerrs.ErrNotFound
}

func (f *fakeStore) ResetRunning(context.Context) (int64, error) {
	var n int64
	for i := range f.tasks {
		if f.tasks[i].State == string(corestatus.TaskRunning) {
			f.tasks[i].State = string(corestatus.TaskPending)
			n++
		}
	}
	f.resets += n
	return n, nil
}

func (f *fakeStore) GetWindow(_ context.Context, schoolTaskID, day string) (coreschedule.WindowRecord, error) {
	w, ok := f.windows[schoolTaskID+"|"+day]
	if !ok {
		return coreschedule.WindowRecord{}, coreerrs.ErrNotFound
	}
	return w, nil
}

func (f *fakeStore) EnsureTask(_ context.Context, accountID, schoolTaskID, day string, runAt time.Time) (coreschedule.TaskRow, bool, error) {
	for _, t := range f.tasks {
		if t.AccountID == accountID && t.SchoolTaskID == schoolTaskID && t.Day == day {
			return t, false, nil
		}
	}
	t := coreschedule.TaskRow{ID: "task-" + accountID, AccountID: accountID, SchoolTaskID: schoolTaskID, Day: day, RunAt: runAt, State: string(corestatus.TaskPending)}
	f.tasks = append(f.tasks, t)
	return t, true, nil
}

func (f *fakeStore) TodayTask(context.Context, string) (string, error) {
	if f.todayTask == "" {
		return "", coreerrs.ErrNotFound
	}
	return f.todayTask, nil
}

type fakeCreds struct{ err error }

func (f fakeCreds) Credential(context.Context, string) (contract.Credential, error) {
	if f.err != nil {
		return contract.Credential{}, f.err
	}
	return contract.Credential{Phone: "13800000000", Password: "x"}, nil
}

type fakeSigner struct {
	targets []contract.Target
	outcome contract.Outcome
	err     error
	signs   int
}

func (f *fakeSigner) DiscoverTargets(context.Context, contract.DiscoverRequest) ([]contract.Target, error) {
	if f.err != nil {
		return nil, f.err
	}
	return f.targets, nil
}

func (f *fakeSigner) Sign(_ context.Context, _ contract.SignRequest) (contract.Result, error) {
	f.signs++
	return contract.Result{Outcome: f.outcome}, f.err
}

type fakeEgress struct{ err error }

func (f fakeEgress) Assign(context.Context, string) (coreegress.Record, error) {
	if f.err != nil {
		return coreegress.Record{}, f.err
	}
	return coreegress.Record{ID: "e1", Identity: "https://e1"}, nil
}

type fakeLimiter struct {
	allow bool
	wait  time.Duration
}

func (f fakeLimiter) Admit(string, time.Time) bool { return f.allow }
func (f fakeLimiter) RetryAfter(string, time.Time) time.Duration {
	return f.wait
}

var loc = time.FixedZone("CST", 8*3600)

// stepClock 是可变时钟：多轮 Step 之间推进时间。
type stepClock struct{ t time.Time }

func (c *stepClock) Now() time.Time { return c.t }

func testNow() time.Time { return time.Date(2026, 10, 29, 7, 0, 0, 0, loc) }

func baseTask() coreschedule.TaskRow {
	return coreschedule.TaskRow{
		ID: "t1", AccountID: "acct-1", SchoolID: "school-1", SchoolTaskID: "st-1",
		Day: "2026-10-29", RunAt: testNow().Add(-time.Minute), State: string(corestatus.TaskPending),
	}
}

func openWindow() coreschedule.WindowRecord {
	return coreschedule.WindowRecord{
		SchoolTaskID: "st-1", Day: "2026-10-29",
		Start: time.Date(2026, 10, 29, 6, 30, 0, 0, loc),
		End:   time.Date(2026, 10, 29, 7, 50, 0, 0, loc),
	}
}

func newService(t *testing.T, st *fakeStore, signer *fakeSigner, limiter fakeLimiter) *appworker.Service {
	t.Helper()
	svc, err := appworker.NewService(appworker.Deps{
		Store: st, Creds: fakeCreds{}, Signer: signer, Egress: fakeEgress{},
		Limiter: limiter, Clock: coreclock.Fixed{T: testNow()}, Location: loc,
	})
	if err != nil {
		t.Fatalf("NewService: %v", err)
	}
	return svc
}

func defaultSigner() *fakeSigner {
	return &fakeSigner{
		targets: []contract.Target{{ID: "pos-1", Title: "校门"}},
		outcome: contract.OutcomeSuccess,
	}
}

// 成功路径：认领→执行→success；落库结果与结论词一致。
func TestStepSuccess(t *testing.T) {
	st := newStore(baseTask())
	st.windows["st-1|2026-10-29"] = openWindow()
	signer := defaultSigner()
	svc := newService(t, st, signer, fakeLimiter{allow: true})

	n, err := svc.Step(context.Background(), 10)
	if err != nil || n != 1 {
		t.Fatalf("Step = (%d,%v), want (1,nil)", n, err)
	}
	if signer.signs != 1 {
		t.Fatalf("sign 调用 = %d, want 1", signer.signs)
	}
	if st.finished["t1"] != corestatus.TaskSuccess || st.results["t1"] != string(contract.OutcomeSuccess) {
		t.Fatalf("终态 = %v/%q, want success", st.finished["t1"], st.results["t1"])
	}
}

// 关窗：落 skipped(window_closed)，不得调用签到。
func TestWindowClosedSkips(t *testing.T) {
	task := baseTask()
	task.RunAt = time.Date(2026, 10, 29, 8, 0, 0, 0, loc)
	st := newStore(task)
	st.windows["st-1|2026-10-29"] = openWindow()
	signer := defaultSigner()
	svc, err := appworker.NewService(appworker.Deps{
		Store: st, Creds: fakeCreds{}, Signer: signer, Egress: fakeEgress{},
		Limiter:  fakeLimiter{allow: true},
		Clock:    coreclock.Fixed{T: time.Date(2026, 10, 29, 8, 0, 30, 0, loc)},
		Location: loc,
	})
	if err != nil {
		t.Fatalf("NewService: %v", err)
	}
	n, err := svc.Step(context.Background(), 10)
	if err != nil || n != 1 {
		t.Fatalf("Step = (%d,%v)", n, err)
	}
	if st.finished["t1"] != corestatus.TaskSkipped || st.results["t1"] != "window_closed" {
		t.Fatalf("终态 = %v/%q, want skipped/window_closed", st.finished["t1"], st.results["t1"])
	}
	if signer.signs != 0 {
		t.Fatalf("关窗仍签到 %d 次", signer.signs)
	}
}

// 提前到点：退回窗口起点，不签到。
func TestEarlyTaskRequeued(t *testing.T) {
	task := baseTask()
	task.RunAt = time.Date(2026, 10, 29, 5, 0, 0, 0, loc) // 已在队列里，但窗口未开
	st := newStore(task)
	st.windows["st-1|2026-10-29"] = openWindow()
	signer := defaultSigner()
	svc, err := appworker.NewService(appworker.Deps{
		Store: st, Creds: fakeCreds{}, Signer: signer, Egress: fakeEgress{},
		Limiter:  fakeLimiter{allow: true},
		Clock:    coreclock.Fixed{T: time.Date(2026, 10, 29, 6, 0, 0, 0, loc)},
		Location: loc,
	})
	if err != nil {
		t.Fatalf("NewService: %v", err)
	}
	if _, err := svc.Step(context.Background(), 10); err != nil {
		t.Fatalf("Step: %v", err)
	}
	if got := st.requeued["t1"]; !got.Equal(time.Date(2026, 10, 29, 6, 30, 0, 0, loc)) {
		t.Fatalf("退回落点 = %v, want 06:30", got)
	}
	if signer.signs != 0 {
		t.Fatal("提前任务不应签到")
	}
}

// 限速拒绝：按 RetryAfter 退避，不签到。
func TestRateLimitedRequeues(t *testing.T) {
	st := newStore(baseTask())
	st.windows["st-1|2026-10-29"] = openWindow()
	signer := defaultSigner()
	svc := newService(t, st, signer, fakeLimiter{allow: false, wait: 2 * time.Second})
	if _, err := svc.Step(context.Background(), 10); err != nil {
		t.Fatalf("Step: %v", err)
	}
	if got := st.requeued["t1"]; !got.Equal(testNow().Add(2 * time.Second)) {
		t.Fatalf("限速退避 = %v, want now+2s", got)
	}
	if signer.signs != 0 {
		t.Fatal("限速拒绝仍签到")
	}
}

// 瞬时失败：预算内退避；耗尽落 failed。
func TestTransientBounded(t *testing.T) {
	st := newStore(baseTask())
	st.windows["st-1|2026-10-29"] = openWindow()
	signer := defaultSigner()
	signer.outcome = contract.OutcomeTransient
	clk := &stepClock{t: testNow()}
	svc, err := appworker.NewService(appworker.Deps{
		Store: st, Creds: fakeCreds{}, Signer: signer, Egress: fakeEgress{},
		Limiter: fakeLimiter{allow: true}, Clock: clk, Location: loc,
	})
	if err != nil {
		t.Fatalf("NewService: %v", err)
	}
	for i := 0; i < 3; i++ {
		if _, err := svc.Step(context.Background(), 10); err != nil {
			t.Fatalf("Step %d: %v", i, err)
		}
		clk.t = clk.t.Add(10 * time.Minute) // 越过退避落点
	}
	if st.finished["t1"] != corestatus.TaskFailed {
		t.Fatalf("预算耗尽应 failed, got %v", st.finished["t1"])
	}
}

// 无点位：skipped(no_target)。
func TestNoTargetSkipped(t *testing.T) {
	st := newStore(baseTask())
	st.windows["st-1|2026-10-29"] = openWindow()
	signer := defaultSigner()
	signer.targets = nil
	svc := newService(t, st, signer, fakeLimiter{allow: true})
	if _, err := svc.Step(context.Background(), 10); err != nil {
		t.Fatalf("Step: %v", err)
	}
	if st.finished["t1"] != corestatus.TaskSkipped || st.results["t1"] != string(contract.OutcomeNoTarget) {
		t.Fatalf("终态 = %v/%q", st.finished["t1"], st.results["t1"])
	}
}

// 同账号两条到期任务：只认领一条（单账号串行）。
func TestSameAccountOnlyOneClaimed(t *testing.T) {
	t1 := baseTask()
	t2 := baseTask()
	t2.ID = "t2"
	t2.SchoolTaskID = "st-2"
	st := newStore(t1, t2)
	st.windows["st-1|2026-10-29"] = openWindow()
	st.windows["st-2|2026-10-29"] = openWindow()
	svc := newService(t, st, defaultSigner(), fakeLimiter{allow: true})
	if _, err := svc.Step(context.Background(), 10); err != nil {
		t.Fatalf("Step: %v", err)
	}
	if len(st.claimed) != 1 {
		t.Fatalf("同账号认领 %d 条, want 1: %v", len(st.claimed), st.claimed)
	}
}

// 启动回收：running 全部退回 pending。
func TestRecoverResetsRunning(t *testing.T) {
	task := baseTask()
	task.State = string(corestatus.TaskRunning)
	st := newStore(task)
	svc := newService(t, st, defaultSigner(), fakeLimiter{allow: true})
	n, err := svc.Recover(context.Background())
	if err != nil || n != 1 {
		t.Fatalf("Recover = (%d,%v), want (1,nil)", n, err)
	}
	if st.tasks[0].State != string(corestatus.TaskPending) {
		t.Fatalf("回收后状态 = %s, want pending", st.tasks[0].State)
	}
}

// 手动签到：关窗/已了结/执行中/正常入队/无任务。
func TestRequestManualSign(t *testing.T) {
	ctx := context.Background()
	base := func() *fakeStore {
		st := newStore()
		st.todayTask = "st-1"
		st.windows["st-1|2026-10-29"] = openWindow()
		return st
	}
	st := base()
	closed := openWindow()
	closed.Start = time.Date(2026, 10, 29, 5, 0, 0, 0, loc)
	closed.End = time.Date(2026, 10, 29, 6, 0, 0, 0, loc)
	st.windows["st-1|2026-10-29"] = closed
	svc := newService(t, st, defaultSigner(), fakeLimiter{allow: true})
	if _, err := svc.Request(ctx, "acct-1"); !errors.Is(err, appworker.ErrWindowClosed) {
		t.Fatalf("关窗应 ErrWindowClosed, got %v", err)
	}
	st = base()
	svc = newService(t, st, defaultSigner(), fakeLimiter{allow: true})
	id, err := svc.Request(ctx, "acct-1")
	if err != nil || id == "" {
		t.Fatalf("Request = (%q,%v)", id, err)
	}
	st.tasks[0].State = string(corestatus.TaskSuccess)
	if _, err := svc.Request(ctx, "acct-1"); !errors.Is(err, appworker.ErrAlreadySigned) {
		t.Fatalf("已成功应 ErrAlreadySigned, got %v", err)
	}
	st.tasks[0].State = string(corestatus.TaskRunning)
	if _, err := svc.Request(ctx, "acct-1"); !errors.Is(err, appworker.ErrSignRunning) {
		t.Fatalf("执行中应 ErrSignRunning, got %v", err)
	}
	st2 := newStore()
	svc2 := newService(t, st2, defaultSigner(), fakeLimiter{allow: true})
	if _, err := svc2.Request(ctx, "acct-1"); err == nil {
		t.Fatal("无任务应报错")
	}
}
