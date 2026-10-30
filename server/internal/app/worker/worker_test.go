package worker_test

import (
	"bytes"
	"context"
	"errors"
	"log/slog"
	"strings"
	"sync"
	"testing"
	"time"

	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	appworker "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/worker"
	coreclock "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	coreegress "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/egress"
	coreerrs "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	coreschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/schedule"
	corestatus "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/status"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/contract"
)

// ---- 替身 ----
//
// fakeStore 刻意实现与库语义相同的状态机：认领推进到 running、终态只进不出、
// 等待型重排不计尝试数。让 app 层测试能钉住"谁在什么情况下被记了一次尝试"。

type fakeStore struct {
	tasks   []coreschedule.TaskRow
	windows map[string]coreschedule.WindowRecord
	// windowErrFor 按 schoolTaskID 注入读窗口失败（基础设施错误测试用）。
	windowErrFor map[string]error
	// finishErr 注入结算失败（验证"已提交行不退回"）。
	finishErr error
	// dayTasks / enabledTasks 是手动签到的两个解析输入。
	dayTasks     []string
	enabledTasks []string

	// mu 保护下面全部字段：Step 批内并行，多个执行协程同时读写替身状态。
	// 断言在 Step 返回后读取，那时已无并发写者。
	mu sync.Mutex

	// 观测面
	claimed      []string
	finished     map[string]corestatus.TaskState
	results      map[string]string
	requeued     map[string]time.Time // 最近一次重排落点（含等待型）
	rearmed      map[string]string    // 重开行 → 来源状态
	rearmActor   map[string]appaudit.Actor
	resets       int64
	reclaimCalls int
}

func newStore(tasks ...coreschedule.TaskRow) *fakeStore {
	return &fakeStore{
		tasks:        tasks,
		windows:      map[string]coreschedule.WindowRecord{},
		windowErrFor: map[string]error{},
		finished:     map[string]corestatus.TaskState{},
		results:      map[string]string{},
		requeued:     map[string]time.Time{},
		rearmed:      map[string]string{},
		rearmActor:   map[string]appaudit.Actor{},
	}
}

func (f *fakeStore) ClaimDue(_ context.Context, now time.Time, limit int) ([]coreschedule.TaskRow, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	// 同账号串行：已有 running 的账号本次不领。
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
			continue
		}
		t.State = string(corestatus.TaskRunning)
		t.ClaimSeq++
		runningAccount[t.AccountID] = true
		f.claimed = append(f.claimed, t.ID)
		out = append(out, *t)
	}
	return out, nil
}

func (f *fakeStore) Finish(ctx context.Context, accountID, schoolTaskID, day string, state corestatus.TaskState, result string, claimSeq int, _ time.Time) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.finishErr != nil {
		return f.finishErr
	}
	for i := range f.tasks {
		t := &f.tasks[i]
		if t.AccountID == accountID && t.SchoolTaskID == schoolTaskID && t.Day == day {
			if t.State != string(corestatus.TaskRunning) || t.ClaimSeq != claimSeq {
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

// FinishCredentialInvalid 按库语义落 failed(credential_invalid)。
// 账号状态是存储层副作用，由 pg e2e 钉住，本 fake 不复制。
func (f *fakeStore) FinishCredentialInvalid(ctx context.Context, _ /*schoolID*/, accountID, schoolTaskID, day string, claimSeq int, _ time.Time) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.finishErr != nil {
		return f.finishErr
	}
	for i := range f.tasks {
		t := &f.tasks[i]
		if t.AccountID == accountID && t.SchoolTaskID == schoolTaskID && t.Day == day {
			if t.State != string(corestatus.TaskRunning) || t.ClaimSeq != claimSeq {
				return coreerrs.ErrNotFound
			}
			t.State = string(corestatus.TaskFailed)
			t.Attempts++
			f.finished[t.ID] = corestatus.TaskFailed
			f.results[t.ID] = "credential_invalid"
			return nil
		}
	}
	return coreerrs.ErrNotFound
}

// requeue 是两种重排的公共实现；bumpAttempts 区分"等待"与"业务重试"。
func (f *fakeStore) requeue(ctx context.Context, taskID string, runAt time.Time, result string, bumpAttempts bool, claimSeq int) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	for i := range f.tasks {
		t := &f.tasks[i]
		if t.ID == taskID && t.State == string(corestatus.TaskRunning) && t.ClaimSeq == claimSeq {
			t.State = string(corestatus.TaskPending)
			t.RunAt = runAt
			if bumpAttempts {
				t.Attempts++
			}
			f.requeued[taskID] = runAt
			f.results[taskID] = result
			return nil
		}
	}
	return coreerrs.ErrNotFound
}

func (f *fakeStore) RequeueWait(ctx context.Context, taskID string, runAt time.Time, result string, claimSeq int) error {
	return f.requeue(ctx, taskID, runAt, result, false, claimSeq)
}

func (f *fakeStore) RequeueRetry(ctx context.Context, taskID string, runAt time.Time, result string, claimSeq int) error {
	return f.requeue(ctx, taskID, runAt, result, true, claimSeq)
}

// Rearm 按库语义：failed 与 skipped 都可重开，尝试数**保留**（重开不退预算）。
// 状态不符时回 conflict（库层是 CAS 未命中）。
func (f *fakeStore) Rearm(_ context.Context, taskID string, runAt time.Time, actor appaudit.Actor) error {
	for i := range f.tasks {
		t := &f.tasks[i]
		if t.ID != taskID {
			continue
		}
		state := corestatus.TaskState(t.State)
		if state != corestatus.TaskFailed && state != corestatus.TaskSkipped {
			break
		}
		f.rearmed[taskID] = t.State
		f.rearmActor[taskID] = actor
		t.State = string(corestatus.TaskPending)
		t.RunAt = runAt
		return nil
	}
	return coreerrs.New(coreerrs.CodeConflict, "sign task already moved")
}

func (f *fakeStore) ReclaimExpired(context.Context, time.Time, int) (int64, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.reclaimCalls++
	return 0, nil
}

func (f *fakeStore) ResetRunning(context.Context) (int64, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
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

func (f *fakeStore) CountRunning(context.Context) (int64, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	var n int64
	for i := range f.tasks {
		if f.tasks[i].State == string(corestatus.TaskRunning) {
			n++
		}
	}
	return n, nil
}

func (f *fakeStore) GetWindow(_ context.Context, schoolTaskID, day string) (coreschedule.WindowRecord, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if err := f.windowErrFor[schoolTaskID]; err != nil {
		return coreschedule.WindowRecord{}, err
	}
	w, ok := f.windows[schoolTaskID+"|"+day]
	if !ok {
		return coreschedule.WindowRecord{}, coreerrs.ErrNotFound
	}
	return w, nil
}

func (f *fakeStore) EnsureTask(_ context.Context, accountID, schoolTaskID, day string, runAt time.Time) (coreschedule.TaskRow, bool, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	for _, t := range f.tasks {
		if t.AccountID == accountID && t.SchoolTaskID == schoolTaskID && t.Day == day {
			return t, false, nil
		}
	}
	t := coreschedule.TaskRow{
		ID: "task-" + accountID, AccountID: accountID, SchoolTaskID: schoolTaskID,
		Day: day, RunAt: runAt, State: string(corestatus.TaskPending),
	}
	f.tasks = append(f.tasks, t)
	return t, true, nil
}

// DayTasks 与 AccountEnabledTasks 是手动签到的解析输入。
// 零行返回空列表：清单类查询的空结果不是错误。
func (f *fakeStore) DayTasks(context.Context, string, string) ([]string, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.dayTasks, nil
}

func (f *fakeStore) AccountEnabledTasks(context.Context, string) ([]string, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.enabledTasks, nil
}

type fakeCreds struct{ err error }

func (f fakeCreds) Credential(context.Context, string) (contract.Credential, error) {
	if f.err != nil {
		return contract.Credential{}, f.err
	}
	return contract.Credential{Phone: "13800000000", Password: "x"}, nil
}

type fakeSigner struct {
	mu      sync.Mutex
	targets []contract.Target
	outcome contract.Outcome
	err     error
	// discoverErr / submitErr 把两个面分开；任一为 nil 时回落到 err（存量用例口径）。
	discoverErr error
	submitErr   error
	signs       int
	// attemptIDs 记录每次提交的幂等键：重试必须每次不同。
	attemptIDs []string
	// requests 记录每次提交的完整请求：P5-C 断言 Location 与 Image。
	requests []contract.SignRequest
	// onSign 在 Sign 返回前调用一次：测试用它模拟停机取消。
	onSign func()
	// entered / release 是并发判据的闸门：Sign 到达即通知并等待放行。
	// **在取得 mu 之前等待**：持锁等待会让第二个协程进不来，测的是自己。
	entered chan<- struct{}
	release <-chan struct{}
}

func (f *fakeSigner) DiscoverTargets(context.Context, contract.DiscoverRequest) ([]contract.Target, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if err := firstErr(f.discoverErr, f.err); err != nil {
		return nil, err
	}
	return f.targets, nil
}

func (f *fakeSigner) Sign(_ context.Context, req contract.SignRequest) (contract.Result, error) {
	if f.entered != nil {
		f.entered <- struct{}{}
		<-f.release
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	f.signs++
	f.attemptIDs = append(f.attemptIDs, req.AttemptID)
	f.requests = append(f.requests, req)
	hook := f.onSign
	if hook != nil {
		f.mu.Unlock()
		hook()
		f.mu.Lock()
	}
	return contract.Result{Outcome: f.outcome}, firstErr(f.submitErr, f.err)
}

// firstErr 取第一个非 nil 错误：面级错误优先，未设则回落到通用 err。
func firstErr(primary, fallback error) error {
	if primary != nil {
		return primary
	}
	return fallback
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

func testNow() time.Time { return time.Date(2026, 10, 29, 7, 0, 0, 0, loc) }

// stepClock 是可变时钟：多轮 Step 之间推进时间。
type stepClock struct{ t time.Time }

func (c *stepClock) Now() time.Time { return c.t }

func baseTask() coreschedule.TaskRow {
	return coreschedule.TaskRow{
		ID: "t1", AccountID: "acct-1", SchoolID: "school-1", SchoolTaskID: "st-1",
		Day: "2026-10-29", RunAt: testNow().Add(-time.Minute), State: string(corestatus.TaskPending),
	}
}

func openWindow() coreschedule.WindowRecord {
	return coreschedule.WindowRecord{
		SchoolTaskID: "st-1", Day: "2026-10-29",
		Start:  time.Date(2026, 10, 29, 6, 30, 0, 0, loc),
		End:    time.Date(2026, 10, 29, 7, 50, 0, 0, loc),
		Source: "platform",
	}
}

func newService(t *testing.T, st *fakeStore, signer *fakeSigner, limiter fakeLimiter) *appworker.Service {
	t.Helper()
	return newServiceLogged(t, st, signer, limiter, nil)
}

func newServiceLogged(t *testing.T, st *fakeStore, signer *fakeSigner, limiter fakeLimiter, logger *slog.Logger) *appworker.Service {
	t.Helper()
	svc, err := appworker.NewService(appworker.Deps{
		Store: st, Creds: fakeCreds{}, Signer: signer, Egress: fakeEgress{},
		Limiter: limiter, Clock: coreclock.Fixed{T: testNow()}, Location: loc,
		Logger: logger,
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

// 结算被栅栏拒绝时必须写错误日志：运行期可见，不只等停机。
func TestStepLogsSettlementConflict(t *testing.T) {
	st := newStore(baseTask())
	st.windows["st-1|2026-10-29"] = openWindow()
	st.finishErr = coreerrs.New(coreerrs.CodeConflict, "fenced")
	var buf bytes.Buffer
	svc := newServiceLogged(t, st, defaultSigner(), fakeLimiter{allow: true},
		slog.New(slog.NewTextHandler(&buf, nil)))
	if _, err := svc.Step(context.Background(), 1); err == nil {
		t.Fatal("结算冲突必须上抛")
	}
	if !strings.Contains(buf.String(), "settlement rejected by the claim fence") {
		t.Fatalf("日志缺栅栏拒绝行: %s", buf.String())
	}
}

// Step 每 tick 先回收过期租约，再认领。
func TestStepCallsReclaimOnce(t *testing.T) {
	st := newStore()
	svc := newService(t, st, defaultSigner(), fakeLimiter{allow: true})
	if _, err := svc.Step(context.Background(), 1); err != nil {
		t.Fatalf("Step: %v", err)
	}
	if st.reclaimCalls != 1 {
		t.Fatalf("reclaim 调用 = %d, want 1（每 tick 一次）", st.reclaimCalls)
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

// 发现阶段返回业务结论（OutcomeError）时必须按结论分档，不得当 transient
// 重试（P4-E 审查 P1-1）。逐词断言：already_done→success、no_target/
// invalid_target/photo_missing/challenge/device_unbound→skipped、
// blocked/credential_invalid→failed，且各只发生一次、不重排。
func TestDiscoverOutcomeErrorSettlesByVocabulary(t *testing.T) {
	cases := []struct {
		outcome contract.Outcome
		want    corestatus.TaskState
	}{
		{contract.OutcomeAlreadyDone, corestatus.TaskSuccess},
		{contract.OutcomeNoTarget, corestatus.TaskSkipped},
		{contract.OutcomeInvalidTarget, corestatus.TaskSkipped},
		{contract.OutcomePhotoMissing, corestatus.TaskSkipped},
		{contract.OutcomeChallenge, corestatus.TaskSkipped},
		{contract.OutcomeBlocked, corestatus.TaskFailed},
		{contract.OutcomeDeviceUnbound, corestatus.TaskSkipped},
		{contract.OutcomeCredentialBad, corestatus.TaskFailed},
	}
	for _, c := range cases {
		st := newStore(baseTask())
		st.windows["st-1|2026-10-29"] = openWindow()
		signer := &fakeSigner{err: &contract.OutcomeError{Outcome: c.outcome, Message: "upstream"}}
		svc := newService(t, st, signer, fakeLimiter{allow: true})
		if _, err := svc.Step(context.Background(), 10); err != nil {
			t.Fatalf("%s: Step: %v", c.outcome, err)
		}
		if st.finished["t1"] != c.want {
			t.Fatalf("%s: 终态 = %v, want %v", c.outcome, st.finished["t1"], c.want)
		}
		if signer.signs != 0 {
			t.Fatalf("%s: 发现即结论，不该再提交签到", c.outcome)
		}
		if _, retried := st.requeued["t1"]; retried {
			t.Fatalf("%s: 确定结论不该重排", c.outcome)
		}
	}
}

// 瞬时失败重试时，每次提交的 AttemptID 必须不同，否则 provider 幂等缓存
// 会把"重试"折叠成首次结果（P4-E 审查 P1-2）。
func TestRetryUsesFreshAttemptID(t *testing.T) {
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
	for i := 0; i < 2; i++ {
		if _, err := svc.Step(context.Background(), 10); err != nil {
			t.Fatalf("Step %d: %v", i, err)
		}
		clk.t = clk.t.Add(10 * time.Minute)
	}
	if len(signer.attemptIDs) != 2 {
		t.Fatalf("提交次数 = %d, want 2", len(signer.attemptIDs))
	}
	if signer.attemptIDs[0] == signer.attemptIDs[1] {
		t.Fatalf("重试复用了同一 AttemptID: %q", signer.attemptIDs[0])
	}
	if signer.attemptIDs[0] != "t1#0" || signer.attemptIDs[1] != "t1#1" {
		t.Fatalf("AttemptID 形状 = %v, want [t1#0 t1#1]", signer.attemptIDs)
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

// 提前到点：退回窗口起点，不签到、不消耗尝试数。
func TestEarlyTaskRequeued(t *testing.T) {
	task := baseTask()
	task.RunAt = time.Date(2026, 10, 29, 5, 0, 0, 0, loc)
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
	if st.tasks[0].Attempts != 0 {
		t.Fatalf("等待型重排消耗了尝试数: %d", st.tasks[0].Attempts)
	}
	if signer.signs != 0 {
		t.Fatal("提前任务不应签到")
	}
}

// 限速拒绝：按 RetryAfter 退避，不签到、不消耗尝试数。
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
	if st.tasks[0].Attempts != 0 {
		t.Fatalf("限速等待消耗了尝试数: %d", st.tasks[0].Attempts)
	}
	if signer.signs != 0 {
		t.Fatal("限速拒绝仍签到")
	}
}

// 瞬时失败：业务重试计尝试数；预算耗尽落 failed。
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
		clk.t = clk.t.Add(10 * time.Minute)
	}
	if st.finished["t1"] != corestatus.TaskFailed {
		t.Fatalf("预算耗尽应 failed, got %v", st.finished["t1"])
	}
}

// 无点位（发现返回空列表）：skipped(no_target)。
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

// 基础设施失败：错误上抛，同批未执行的行退回队列（P4-E 审查 P2-2）。
func TestInfraErrorReleasesRemainingClaims(t *testing.T) {
	t1 := baseTask()
	t2 := baseTask()
	t2.ID = "t2"
	t2.AccountID = "acct-2"
	t2.SchoolTaskID = "st-2"
	st := newStore(t1, t2)
	st.windows["st-1|2026-10-29"] = openWindow()
	st.windowErrFor["st-2"] = errors.New("db down")
	svc := newService(t, st, defaultSigner(), fakeLimiter{allow: true})
	_, err := svc.Step(context.Background(), 10)
	if err == nil {
		t.Fatal("基础设施失败应上抛")
	}
	// t1 正常成功；t2 退回 pending，不得停在 running。
	if st.finished["t1"] != corestatus.TaskSuccess {
		t.Fatalf("t1 应为 success, got %v", st.finished["t1"])
	}
	var t2row coreschedule.TaskRow
	for _, row := range st.tasks {
		if row.ID == "t2" {
			t2row = row
		}
	}
	if t2row.State != string(corestatus.TaskPending) {
		t.Fatalf("t2 状态 = %s, want pending", t2row.State)
	}
}

// 凭据基础设施错误（DB/KEK）上抛，不写成业务失败（P4-E 审查 P2-3）。
func TestCredentialInfraErrorPropagates(t *testing.T) {
	st := newStore(baseTask())
	st.windows["st-1|2026-10-29"] = openWindow()
	svc, err := appworker.NewService(appworker.Deps{
		Store: st, Creds: fakeCreds{err: errors.New("db down")},
		Signer: defaultSigner(), Egress: fakeEgress{},
		Limiter: fakeLimiter{allow: true}, Clock: coreclock.Fixed{T: testNow()}, Location: loc,
	})
	if err != nil {
		t.Fatalf("NewService: %v", err)
	}
	if _, err := svc.Step(context.Background(), 10); err == nil {
		t.Fatal("凭据基础设施错误应上抛")
	}
}

// 凭据业务错误（账号不可用）落 failed(credential_invalid)。
func TestCredentialBusinessErrorFails(t *testing.T) {
	st := newStore(baseTask())
	st.windows["st-1|2026-10-29"] = openWindow()
	svc, err := appworker.NewService(appworker.Deps{
		Store: st, Creds: fakeCreds{err: coreerrs.New(coreerrs.CodeNotFound, "account gone")},
		Signer: defaultSigner(), Egress: fakeEgress{},
		Limiter: fakeLimiter{allow: true}, Clock: coreclock.Fixed{T: testNow()}, Location: loc,
	})
	if err != nil {
		t.Fatalf("NewService: %v", err)
	}
	if _, err := svc.Step(context.Background(), 10); err != nil {
		t.Fatalf("Step: %v", err)
	}
	if st.finished["t1"] != corestatus.TaskFailed || st.results["t1"] != string(contract.OutcomeCredentialBad) {
		t.Fatalf("终态 = %v/%q, want failed/credential_invalid", st.finished["t1"], st.results["t1"])
	}
}

// 手动签到：关窗/已了结/执行中/正常入队/无任务/重开。
func TestRequestManualSign(t *testing.T) {
	ctx := context.Background()
	actor := appaudit.Actor{UserID: "u1", Label: "u***@example.test"}
	base := func() *fakeStore {
		st := newStore()
		st.enabledTasks = []string{"st-1"}
		st.windows["st-1|2026-10-29"] = openWindow()
		return st
	}
	st := base()
	closed := openWindow()
	closed.Start = time.Date(2026, 10, 29, 5, 0, 0, 0, loc)
	closed.End = time.Date(2026, 10, 29, 6, 0, 0, 0, loc)
	st.windows["st-1|2026-10-29"] = closed
	svc := newService(t, st, defaultSigner(), fakeLimiter{allow: true})
	if _, err := svc.Request(ctx, "acct-1", actor); !errors.Is(err, appworker.ErrWindowClosed) {
		t.Fatalf("关窗应 ErrWindowClosed, got %v", err)
	}
	// 正常入队。
	st = base()
	svc = newService(t, st, defaultSigner(), fakeLimiter{allow: true})
	id, err := svc.Request(ctx, "acct-1", actor)
	if err != nil || id == "" {
		t.Fatalf("Request = (%q,%v)", id, err)
	}
	// 已成功 → already_signed。
	st.tasks[0].State = string(corestatus.TaskSuccess)
	if _, err := svc.Request(ctx, "acct-1", actor); !errors.Is(err, appworker.ErrAlreadySigned) {
		t.Fatalf("已成功应 ErrAlreadySigned, got %v", err)
	}
	// 执行中 → sign_running。
	st.tasks[0].State = string(corestatus.TaskRunning)
	if _, err := svc.Request(ctx, "acct-1", actor); !errors.Is(err, appworker.ErrSignRunning) {
		t.Fatalf("执行中应 ErrSignRunning, got %v", err)
	}
	// 当日 failed → 重新放回队列（P4-E 审查 P2-4），并记来源状态与主体。
	st.tasks[0].State = string(corestatus.TaskFailed)
	st.tasks[0].Attempts = 3
	id, err = svc.Request(ctx, "acct-1", actor)
	if err != nil || id == "" {
		t.Fatalf("failed 行应可重排: (%q,%v)", id, err)
	}
	if st.tasks[0].State != string(corestatus.TaskPending) {
		t.Fatalf("重排后状态 = %s, want pending", st.tasks[0].State)
	}
	if st.tasks[0].Attempts != 3 {
		t.Fatalf("重开后尝试数 = %d, want 3（重开不退预算）", st.tasks[0].Attempts)
	}
	if st.rearmed[st.tasks[0].ID] != string(corestatus.TaskFailed) {
		t.Fatalf("重开审计来源 = %q, want failed", st.rearmed[st.tasks[0].ID])
	}
	if got := st.rearmActor[st.tasks[0].ID]; got != actor {
		t.Fatalf("重开审计主体 = %+v, want %+v", got, actor)
	}
	// 无启用任务 → not_found。
	st2 := newStore()
	svc2 := newService(t, st2, defaultSigner(), fakeLimiter{allow: true})
	if _, err := svc2.Request(ctx, "acct-1", actor); err == nil {
		t.Fatal("无任务应报错")
	}
}

// skipped 行可重开（C2 验收③）：用户补了图片或风控解除后要能当天重试。
func TestRequestRearmsSkippedRow(t *testing.T) {
	ctx := context.Background()
	actor := appaudit.Actor{UserID: "u1"}
	st := newStore()
	st.enabledTasks = []string{"st-1"}
	st.windows["st-1|2026-10-29"] = openWindow()
	svc := newService(t, st, defaultSigner(), fakeLimiter{allow: true})
	if _, err := svc.Request(ctx, "acct-1", actor); err != nil {
		t.Fatalf("首次入队: %v", err)
	}
	st.tasks[0].State = string(corestatus.TaskSkipped)
	id, err := svc.Request(ctx, "acct-1", actor)
	if err != nil || id == "" {
		t.Fatalf("skipped 行应可重开: (%q,%v)", id, err)
	}
	if st.tasks[0].State != string(corestatus.TaskPending) {
		t.Fatalf("重开后状态 = %s, want pending", st.tasks[0].State)
	}
	if st.rearmed[st.tasks[0].ID] != string(corestatus.TaskSkipped) {
		t.Fatalf("重开审计来源 = %q, want skipped", st.rearmed[st.tasks[0].ID])
	}
}

// 任务解析（C2）：当日行优先；候选不唯一一律 task_ambiguous。
func TestRequestResolvesTaskFromTodayRows(t *testing.T) {
	ctx := context.Background()
	actor := appaudit.Actor{UserID: "u1"}
	// 当日只有一个任务行，学校另有启用任务：必须签当日那一行，不另找。
	st := newStore()
	st.dayTasks = []string{"st-today"}
	st.enabledTasks = []string{"st-other"}
	st.windows["st-today|2026-10-29"] = openWindow()
	svc := newService(t, st, defaultSigner(), fakeLimiter{allow: true})
	if _, err := svc.Request(ctx, "acct-1", actor); err != nil {
		t.Fatalf("当日行优先: %v", err)
	}
	if got := st.tasks[0].SchoolTaskID; got != "st-today" {
		t.Fatalf("入队任务 = %q, want st-today", got)
	}

	// 当日两个任务行：服务端不猜。
	st = newStore()
	st.dayTasks = []string{"st-a", "st-b"}
	st.enabledTasks = []string{"st-a", "st-b"}
	st.windows["st-a|2026-10-29"] = openWindow()
	svc = newService(t, st, defaultSigner(), fakeLimiter{allow: true})
	if _, err := svc.Request(ctx, "acct-1", actor); !errors.Is(err, appworker.ErrTaskAmbiguous) {
		t.Fatalf("当日多行应 ErrTaskAmbiguous, got %v", err)
	}

	// 当日无行、学校两条启用任务：同样不猜。
	st = newStore()
	st.enabledTasks = []string{"st-a", "st-b"}
	st.windows["st-a|2026-10-29"] = openWindow()
	svc = newService(t, st, defaultSigner(), fakeLimiter{allow: true})
	if _, err := svc.Request(ctx, "acct-1", actor); !errors.Is(err, appworker.ErrTaskAmbiguous) {
		t.Fatalf("多启用任务应 ErrTaskAmbiguous, got %v", err)
	}
}

// 批内并行（C2 验收⑤）：一批两个任务必须同时在执行。
//
// 判据：两个 Sign 都要到达闸门才放行。串行执行时第一个 Sign 永远等不到
// 第二个到达，超时即红——这正是"容量按并行算、执行按串行跑"的形状。
func TestStepRunsBatchInParallel(t *testing.T) {
	second := baseTask()
	second.ID, second.AccountID = "t2", "acct-2"
	st := newStore(baseTask(), second)
	st.windows["st-1|2026-10-29"] = openWindow()

	entered := make(chan struct{}, 2)
	release := make(chan struct{})
	signer := defaultSigner()
	signer.entered, signer.release = entered, release

	svc := newService(t, st, signer, fakeLimiter{allow: true})
	done := make(chan error, 1)
	go func() {
		_, err := svc.Step(context.Background(), 2)
		done <- err
	}()
	for i := 1; i <= 2; i++ {
		select {
		case <-entered:
		case <-time.After(2 * time.Second):
			t.Fatalf("第 %d 个任务未并发到达：Step 仍在串行执行", i)
		}
	}
	close(release)
	if err := <-done; err != nil {
		t.Fatalf("Step: %v", err)
	}
	if st.finished["t1"] != corestatus.TaskSuccess || st.finished["t2"] != corestatus.TaskSuccess {
		t.Fatalf("finished = %v, want 两行都 success", st.finished)
	}
}

// 提交面终态词逐词断言：改错任何一档都必须红（P4-E 复审 P2-1）。
func TestSignOutcomeSettlesByVocabulary(t *testing.T) {
	cases := []struct {
		outcome contract.Outcome
		want    corestatus.TaskState
	}{
		{contract.OutcomeSuccess, corestatus.TaskSuccess},
		{contract.OutcomeAlreadyDone, corestatus.TaskSuccess},
		{contract.OutcomeNoTarget, corestatus.TaskSkipped},
		{contract.OutcomeInvalidTarget, corestatus.TaskSkipped},
		{contract.OutcomePhotoMissing, corestatus.TaskSkipped},
		{contract.OutcomeChallenge, corestatus.TaskSkipped},
		{contract.OutcomeBlocked, corestatus.TaskFailed},
		{contract.OutcomeDeviceUnbound, corestatus.TaskSkipped},
		{contract.OutcomeRejected, corestatus.TaskFailed},
		{contract.OutcomePhotoRejected, corestatus.TaskFailed},
		{contract.OutcomeCredentialBad, corestatus.TaskFailed},
	}
	for _, c := range cases {
		st := newStore(baseTask())
		st.windows["st-1|2026-10-29"] = openWindow()
		signer := defaultSigner()
		signer.outcome = c.outcome
		svc := newService(t, st, signer, fakeLimiter{allow: true})
		if _, err := svc.Step(context.Background(), 10); err != nil {
			t.Fatalf("%s: Step: %v", c.outcome, err)
		}
		if st.finished["t1"] != c.want {
			t.Fatalf("%s: 终态 = %v, want %v", c.outcome, st.finished["t1"], c.want)
		}
	}
}

// 停机取消后已提交的行仍要落库（D 批审查 P1-2）：
// Sign 返回时取消 ctx，结算不得被取消打断——写不进去，行留在 running，
// 租约到期后会被重新认领，平台侧就收到第二次签到。
func TestStepSettlesSubmittedRowAfterCancel(t *testing.T) {
	// 三条结算写入路径都要在取消之后仍然落库（审查 P3-2）：
	// 终态两条各取一条代表，重排取 transient。
	cases := []struct {
		name      string
		configure func(*fakeSigner)
		want      corestatus.TaskState
		attempts  int
	}{
		{"success 落终态", func(s *fakeSigner) { s.outcome = contract.OutcomeSuccess },
			corestatus.TaskSuccess, 1},
		{"transient 重排", func(s *fakeSigner) { s.outcome = contract.OutcomeTransient },
			corestatus.TaskPending, 1},
		{"credential_invalid 落 failed", func(s *fakeSigner) {
			s.submitErr = &contract.OutcomeError{Outcome: contract.OutcomeCredentialBad, Message: "bad"}
		}, corestatus.TaskFailed, 1},
	}
	for _, c := range cases {
		st := newStore(baseTask())
		st.windows["st-1|2026-10-29"] = openWindow()
		signer := defaultSigner()
		c.configure(signer)
		svc := newService(t, st, signer, fakeLimiter{allow: true})
		ctx, cancel := context.WithCancel(context.Background())
		signer.onSign = cancel
		if _, err := svc.Step(ctx, 10); err != nil {
			t.Fatalf("%s: Step: %v（结算被取消打断）", c.name, err)
		}
		if got := st.tasks[0].State; got != string(c.want) {
			t.Fatalf("%s: 状态 = %s, want %s（结算必须落库）", c.name, got, c.want)
		}
		if st.tasks[0].Attempts != c.attempts {
			t.Fatalf("%s: attempts = %d, want %d", c.name, st.tasks[0].Attempts, c.attempts)
		}
		cancel()
	}
}

// 等待型重排同样要在取消之后落库：窗口未发现 → 行退回队列，不许停在 running。
func TestStepRequeuesWaitAfterCancel(t *testing.T) {
	st := newStore(baseTask()) // 无窗口缓存：走 wait_window_missing
	svc := newService(t, st, defaultSigner(), fakeLimiter{allow: true})
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := svc.Step(ctx, 10); err != nil {
		t.Fatalf("Step: %v（等待型退回被取消打断）", err)
	}
	if st.tasks[0].State != string(corestatus.TaskPending) {
		t.Fatalf("状态 = %s, want pending（未提交的行必须退回队列）", st.tasks[0].State)
	}
}

// WAF 拦截不排重试：1 次尝试即 failed（C2 验收①）。
// 判据落在两处：终态是 failed，且没有任何重排。
func TestWAFBlockedFailsWithoutRetry(t *testing.T) {
	st := newStore(baseTask())
	st.windows["st-1|2026-10-29"] = openWindow()
	signer := &fakeSigner{err: &contract.OutcomeError{
		Outcome: contract.OutcomeBlocked, Message: "WAF blocked response",
	}}
	svc := newService(t, st, signer, fakeLimiter{allow: true})
	if _, err := svc.Step(context.Background(), 10); err != nil {
		t.Fatalf("Step: %v", err)
	}
	if st.finished["t1"] != corestatus.TaskFailed {
		t.Fatalf("终态 = %v, want failed", st.finished["t1"])
	}
	if len(st.requeued) != 0 {
		t.Fatalf("blocked 不得重排, got %v", st.requeued)
	}
	if st.tasks[0].Attempts != 1 {
		t.Fatalf("attempts = %d, want 1", st.tasks[0].Attempts)
	}
}

// 已提交签到后结算失败：当前行必须留在 running（不得退回，防重复签到），
// 只有后续行退回（P4-E 复审 P2-2 的反向判据）。
func TestSettleFailureKeepsSubmittedRowRunning(t *testing.T) {
	st := newStore(baseTask())
	st.windows["st-1|2026-10-29"] = openWindow()
	st.finishErr = errors.New("db down on finish")
	svc := newService(t, st, defaultSigner(), fakeLimiter{allow: true})
	_, err := svc.Step(context.Background(), 10)
	if err == nil {
		t.Fatal("结算失败应上抛")
	}
	var row coreschedule.TaskRow
	for _, r := range st.tasks {
		if r.ID == "t1" {
			row = r
		}
	}
	if row.State != string(corestatus.TaskRunning) {
		t.Fatalf("已提交行状态 = %s, want running（不得退回）", row.State)
	}
}

// 重试面的其余词也必须有落点：pending_upstream 与 unknown 按预算重排。
func TestRetryWordsRequeue(t *testing.T) {
	for _, outcome := range []contract.Outcome{contract.OutcomePendingUpstream, contract.OutcomeUnknown} {
		st := newStore(baseTask())
		st.windows["st-1|2026-10-29"] = openWindow()
		signer := defaultSigner()
		signer.outcome = outcome
		svc := newService(t, st, signer, fakeLimiter{allow: true})
		if _, err := svc.Step(context.Background(), 10); err != nil {
			t.Fatalf("%s: Step: %v", outcome, err)
		}
		if _, ok := st.requeued["t1"]; !ok {
			t.Fatalf("%s: 应退避重排", outcome)
		}
		if st.tasks[0].Attempts != 1 {
			t.Fatalf("%s: 业务重试应计一次尝试, got %d", outcome, st.tasks[0].Attempts)
		}
	}
}
