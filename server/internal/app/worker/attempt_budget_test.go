package worker_test

// 一次尝试的总截止时间（YIBAN_ATTEMPT_BUDGET_SEC）。
//
// 没有它，卡住的提供方会把租约活活耗到过期：行被回收器放回队列，
// 而本执行体还活着，第二个执行体随即认领同一账号——库层唯一索引
// 只盖 state='running'，回收已经把旧行改成了 pending，挡不住这条路径。
//
// 判据三条：
//   1. 预算到期必须按"一次失败的尝试"结算（计 attempts、按退避重排），
//      不是当基础设施错误上抛（上抛会让行每 tick 重领、每 tick 再卡满一个预算）；
//   2. 预算未到期不截断：正常返回的结论照旧落终态；
//   3. 父 ctx 取消不算预算截断：那是"这一轮不跑了"，不是"提供方没及时答"。

import (
	"context"
	"log/slog"
	"sync/atomic"
	"testing"
	"time"

	appworker "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/worker"
	coreclock "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	corestatus "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/status"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/contract"
)

// gatedSigner 的 Sign 尊重 ctx：delay>0 时等到时返回成功，否则只听 ctx。
// 这正是预算要管的那类提供方——它不忽略取消，但没人给它设上限时
// 它可以一直等到租约过期。
type gatedSigner struct {
	entered chan struct{} // 容量 1：Sign 一进入就通知（区分"还没进提交"与"卡在提交"）
	delay   time.Duration
	signs   atomic.Int32
	started chan struct{} // 容量 1：本次 Sign 已进入
	// hangDiscovery=true 时发现阶段只听 ctx：预算必须覆盖发现，
	// 不能只管提交（发现也会卡在慢接口上）。
	hangDiscovery bool
}

func newGatedSigner(delay time.Duration) *gatedSigner {
	return &gatedSigner{
		entered: make(chan struct{}, 1),
		started: make(chan struct{}, 1),
		delay:   delay,
	}
}

func (g *gatedSigner) DiscoverTargets(ctx context.Context, _ contract.DiscoverRequest) ([]contract.Target, error) {
	if !g.hangDiscovery {
		return []contract.Target{{ID: "pos-1", Title: "校门"}}, nil
	}
	<-ctx.Done()
	return nil, ctx.Err()
}

func (g *gatedSigner) Sign(ctx context.Context, _ contract.SignRequest) (contract.Result, error) {
	g.signs.Add(1)
	select {
	case g.started <- struct{}{}:
	default:
	}
	if g.delay > 0 {
		timer := time.NewTimer(g.delay)
		defer timer.Stop()
		select {
		case <-timer.C:
			return contract.Result{Outcome: contract.OutcomeSuccess}, nil
		case <-ctx.Done():
			return contract.Result{}, ctx.Err()
		}
	}
	<-ctx.Done()
	return contract.Result{}, ctx.Err()
}

func newBudgetService(t *testing.T, st *fakeStore, signer *gatedSigner, budget time.Duration, sink *logSink) *appworker.Service {
	t.Helper()
	svc, err := appworker.NewService(appworker.Deps{
		Store: st, Creds: fakeCreds{}, Signer: signer, Egress: fakeEgress{},
		Limiter: fakeLimiter{allow: true}, Clock: coreclock.Fixed{T: testNow()},
		Location: loc, Paused: neverPaused,
		Logger:        slog.New(slog.NewJSONHandler(sink, nil)),
		AttemptBudget: budget,
	})
	if err != nil {
		t.Fatalf("NewService: %v", err)
	}
	return svc
}

// 预算到期：按 transient 计一次尝试并退避重排，不是上抛基础设施错误。
func TestAttemptBudgetCutCountsOneAttempt(t *testing.T) {
	st := newStore(baseTask())
	st.windows["st-1|2026-10-29"] = openWindow()
	signer := newGatedSigner(0) // 只听 ctx：模型"半开连接"
	sink := &logSink{}
	svc := newBudgetService(t, st, signer, 60*time.Millisecond, sink)

	clk := coreclock.System{}
	start := clk.Now()
	processed, err := svc.Step(context.Background(), 10)
	elapsed := time.Since(start)
	if err != nil {
		t.Fatalf("Step: %v（预算截断是失败的尝试，不是基础设施错误）", err)
	}
	if processed != 1 {
		t.Fatalf("processed = %d, want 1（结算成功即已处理）", processed)
	}
	if signer.signs.Load() != 1 {
		t.Fatalf("Sign 调用 %d 次, want 1", signer.signs.Load())
	}
	if elapsed > 5*time.Second {
		t.Fatalf("Step 耗时 %v：预算没有生效", elapsed)
	}
	row := st.tasks[0]
	if row.State != string(corestatus.TaskPending) {
		t.Fatalf("状态 = %s, want pending（预算内退避，不是终态）", row.State)
	}
	if row.Attempts != 1 {
		t.Fatalf("attempts = %d, want 1（预算截断是一次真实尝试）", row.Attempts)
	}
	if row.Result != string(contract.OutcomeTransient) {
		t.Fatalf("result = %q, want %q", row.Result, contract.OutcomeTransient)
	}
	if _, ok := st.requeued["t1"]; !ok {
		t.Fatal("预算截断必须重排（退避），否则本日再无机会")
	}
	if !hasReason(sink.records(t), string(contract.OutcomeTransient), "attempt_budget_exceeded") {
		t.Fatalf("日志缺 attempt_budget_exceeded 原因: %v", sink.records(t))
	}
}

// 预算未到期：提供方的结论照旧落终态，不因引入预算而改变语义。
func TestAttemptBudgetKeepsFastSuccess(t *testing.T) {
	st := newStore(baseTask())
	st.windows["st-1|2026-10-29"] = openWindow()
	signer := newGatedSigner(5 * time.Millisecond)
	sink := &logSink{}
	svc := newBudgetService(t, st, signer, 2*time.Second, sink)

	if _, err := svc.Step(context.Background(), 10); err != nil {
		t.Fatalf("Step: %v", err)
	}
	if st.finished["t1"] != corestatus.TaskSuccess {
		t.Fatalf("终态 = %v, want success", st.finished["t1"])
	}
}

// 父 ctx 取消：不能冒充预算截断（"这一轮不跑了" ≠ "提供方没及时答"）。
func TestAttemptBudgetDoesNotClaimParentCancel(t *testing.T) {
	st := newStore(baseTask())
	st.windows["st-1|2026-10-29"] = openWindow()
	signer := newGatedSigner(0) // 只听 ctx
	sink := &logSink{}
	svc := newBudgetService(t, st, signer, time.Minute, sink)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() {
		<-signer.started
		cancel()
	}()
	// 提交面已发出请求：取消照旧按一次尝试结算（存量语义，本次不改），
	// 关键是原因词不得冒充预算截断。
	if _, err := svc.Step(ctx, 10); err != nil {
		t.Fatalf("Step: %v（提交面取消按存量语义结算，不上抛）", err)
	}
	if hasReason(sink.records(t), string(contract.OutcomeTransient), "attempt_budget_exceeded") {
		t.Fatal("父 ctx 取消被误判成预算截断")
	}
}

// 零值不设限：端口语义保持引入预算前的行为（装配层负责派生缺省值）。
func TestAttemptBudgetZeroDisablesCutoff(t *testing.T) {
	st := newStore(baseTask())
	st.windows["st-1|2026-10-29"] = openWindow()
	signer := newGatedSigner(80 * time.Millisecond)
	svc := newBudgetService(t, st, signer, 0, &logSink{})

	if _, err := svc.Step(context.Background(), 10); err != nil {
		t.Fatalf("Step: %v", err)
	}
	if st.finished["t1"] != corestatus.TaskSuccess {
		t.Fatalf("终态 = %v, want success（零值=不设限）", st.finished["t1"])
	}
}

// 预算覆盖发现阶段：卡在 DiscoverTargets 上同样按一次失败尝试结算，
// 且不得继续走到提交（Sign 调用 0 次）。
func TestAttemptBudgetCutAtDiscovery(t *testing.T) {
	st := newStore(baseTask())
	st.windows["st-1|2026-10-29"] = openWindow()
	signer := newGatedSigner(0)
	signer.hangDiscovery = true // 发现阶段半开
	sink := &logSink{}
	svc := newBudgetService(t, st, signer, 60*time.Millisecond, sink)

	processed, err := svc.Step(context.Background(), 10)
	if err != nil {
		t.Fatalf("Step: %v（发现面截断也是失败的尝试，不是基础设施错误）", err)
	}
	if processed != 1 {
		t.Fatalf("processed = %d, want 1", processed)
	}
	if signer.signs.Load() != 0 {
		t.Fatalf("Sign 调用 %d 次, want 0（发现被截断就不该进入提交）", signer.signs.Load())
	}
	row := st.tasks[0]
	if row.State != string(corestatus.TaskPending) || row.Attempts != 1 {
		t.Fatalf("状态 = %s attempts = %d, want pending/1", row.State, row.Attempts)
	}
	if row.Result != string(contract.OutcomeTransient) {
		t.Fatalf("result = %q, want %q", row.Result, contract.OutcomeTransient)
	}
	if !hasReason(sink.records(t), string(contract.OutcomeTransient), "attempt_budget_exceeded") {
		t.Fatalf("日志缺 attempt_budget_exceeded 原因: %v", sink.records(t))
	}
}

// 预算耗尽：最后一次截断必须落 failed，不能留在 pending 上每 tick 再撞一次。
func TestAttemptBudgetExhaustedFinishesFailed(t *testing.T) {
	task := baseTask()
	task.Attempts = 2 // transient 档 MaxAttempts=3：这一次就是第 3 次
	st := newStore(task)
	st.windows["st-1|2026-10-29"] = openWindow()
	signer := newGatedSigner(0)
	svc := newBudgetService(t, st, signer, 60*time.Millisecond, &logSink{})

	if _, err := svc.Step(context.Background(), 10); err != nil {
		t.Fatalf("Step: %v", err)
	}
	if st.finished["t1"] != corestatus.TaskFailed {
		t.Fatalf("终态 = %v, want failed（预算耗尽不得回 pending）", st.finished["t1"])
	}
	if _, ok := st.requeued["t1"]; ok {
		t.Fatal("预算耗尽仍然重排了：会每 tick 重领同一行")
	}
}

// 调用方自己传了更早的 deadline：那不是"提供方没及时答"，
// 不得冒充预算截断（否则一次请求白吃一次尝试预算）。
func TestAttemptBudgetDoesNotClaimParentDeadline(t *testing.T) {
	st := newStore(baseTask())
	st.windows["st-1|2026-10-29"] = openWindow()
	signer := newGatedSigner(0) // 只听 ctx
	sink := &logSink{}
	svc := newBudgetService(t, st, signer, time.Minute, sink)

	ctx, cancel := context.WithTimeout(context.Background(), 80*time.Millisecond)
	defer cancel()
	if _, err := svc.Step(ctx, 10); err != nil {
		t.Fatalf("Step: %v（父截止按存量语义结算，不上抛）", err)
	}
	if hasReason(sink.records(t), string(contract.OutcomeTransient), "attempt_budget_exceeded") {
		t.Fatal("父 ctx 截止被误判成预算截断")
	}
}
