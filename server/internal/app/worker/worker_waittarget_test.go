package worker_test

// Position 空（平台没放数据）的等待型重扫行为。
//
// # 判据
// 发现返回空或 no_position 结论时，行**不落终态**：退回 pending，
// result 写等待词 wait_target:N（N 是已排定的重扫次数），
// 落点按 base 5 分钟指数退避＋抖动，且不越过窗口尾；
// 重扫次数耗尽或窗口尾放不下下一跳才落 skipped(no_target)。
// 手动重开对等待行立即生效：退避清零、计数随 result 清空。
// State 2/3/4 的平台明确结论（no_target/already_done）不受影响，
// 等待中途出现明确结论直接终态。

import (
	"context"
	"testing"
	"time"

	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	corestatus "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/status"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/contract"
)

// waitTargetBase 是重扫基础间隔（与实现的常量同值）：抖动幅度 [0.5,1.5]。
const waitTargetTestBase = 5 * time.Minute

// 空点位列表：行退回 pending 等待重扫，不落终态、不计尝试。
func TestNoTargetEmptyListWaits(t *testing.T) {
	st := newStore(baseTask())
	win := openWindow()
	st.windows["st-1|2026-10-29"] = win
	signer := defaultSigner()
	signer.targets = nil
	svc := newService(t, st, signer, fakeLimiter{allow: true})
	n, err := svc.Step(context.Background(), 10)
	if err != nil || n != 1 {
		t.Fatalf("Step = (%d,%v), want (1,nil)", n, err)
	}
	if got := st.tasks[0].State; got != string(corestatus.TaskPending) {
		t.Fatalf("状态 = %s, want pending（Position 空不是结论）", got)
	}
	if _, done := st.finished["t1"]; done {
		t.Fatal("Position 空不得落终态")
	}
	if got := st.results["t1"]; got != "wait_target:1" {
		t.Fatalf("result = %q, want wait_target:1", got)
	}
	if st.tasks[0].Attempts != 0 {
		t.Fatalf("attempts = %d, want 0（等待不计尝试）", st.tasks[0].Attempts)
	}
	// 第 1 跳 = base×[0.5,1.5]，且不得越过窗口尾。
	low, high := testNow().Add(waitTargetTestBase/2), testNow().Add(waitTargetTestBase*3/2)
	if got := st.requeued["t1"]; got.Before(low) || got.After(high) {
		t.Fatalf("重扫落点 = %v, want [%v,%v]", got, low, high)
	}
	if !st.requeued["t1"].Before(win.End) {
		t.Fatalf("重扫落点 %v 越过窗口尾 %v", st.requeued["t1"], win.End)
	}
	if signer.signs != 0 {
		t.Fatalf("sign 调用 = %d, want 0（没点位不该提交）", signer.signs)
	}
}

// 发现阶段返回 no_position 结论（生产主路径）：同样进入等待重扫。
func TestNoPositionDiscoverOutcomeWaits(t *testing.T) {
	st := newStore(baseTask())
	st.windows["st-1|2026-10-29"] = openWindow()
	signer := &fakeSigner{err: &contract.OutcomeError{
		Outcome: contract.OutcomeNoPosition, Message: "signPosition returned no positions",
	}}
	svc := newService(t, st, signer, fakeLimiter{allow: true})
	if _, err := svc.Step(context.Background(), 10); err != nil {
		t.Fatalf("Step: %v", err)
	}
	if st.tasks[0].State != string(corestatus.TaskPending) || st.finished["t1"] != "" {
		t.Fatalf("状态 = %s/finished=%v, want pending 且无终态", st.tasks[0].State, st.finished["t1"])
	}
	if got := st.results["t1"]; got != "wait_target:1" {
		t.Fatalf("result = %q, want wait_target:1", got)
	}
	if st.tasks[0].Attempts != 0 {
		t.Fatalf("attempts = %d, want 0", st.tasks[0].Attempts)
	}
}

// 提交阶段点位消失（Sign 返回 no_position 结论）：同样等待重扫，
// 且等待不计业务尝试——之后 transient 的预算不受等待影响。
func TestNoPositionSubmitOutcomeWaits(t *testing.T) {
	st := newStore(baseTask())
	st.windows["st-1|2026-10-29"] = openWindow()
	signer := defaultSigner()
	signer.outcome = contract.OutcomeNoPosition
	svc := newService(t, st, signer, fakeLimiter{allow: true})
	if _, err := svc.Step(context.Background(), 10); err != nil {
		t.Fatalf("Step: %v", err)
	}
	if st.tasks[0].State != string(corestatus.TaskPending) || st.finished["t1"] != "" {
		t.Fatalf("状态 = %s/finished=%v, want pending 且无终态", st.tasks[0].State, st.finished["t1"])
	}
	if got := st.results["t1"]; got != "wait_target:1" {
		t.Fatalf("result = %q, want wait_target:1", got)
	}
	if st.tasks[0].Attempts != 0 {
		t.Fatalf("attempts = %d, want 0（提交被平台拒收不算业务尝试）", st.tasks[0].Attempts)
	}
}

// 重扫按档推进：已排 2 次的行再扫仍空，第 3 跳继续退避
// （曲线在 MaxBackoff 饱和后为 base×2×[0.5,1.5]，即 [5m,15m] 档），
// 且不越过窗口尾。
func TestWaitTargetRescanAdvances(t *testing.T) {
	task := baseTask()
	task.Result = "wait_target:2"
	st := newStore(task)
	win := openWindow()
	st.windows["st-1|2026-10-29"] = win
	signer := defaultSigner()
	signer.targets = nil
	svc := newService(t, st, signer, fakeLimiter{allow: true})
	if _, err := svc.Step(context.Background(), 10); err != nil {
		t.Fatalf("Step: %v", err)
	}
	if got := st.results["t1"]; got != "wait_target:3" {
		t.Fatalf("result = %q, want wait_target:3", got)
	}
	low := testNow().Add(waitTargetTestBase)
	high := win.End // 曲线档位与 remaining×0.3=15m 的小者
	if got := st.requeued["t1"]; got.Before(low) || got.After(high) {
		t.Fatalf("第 3 跳落点 = %v, want [%v,%v]", got, low, high)
	}
	if st.tasks[0].Attempts != 0 {
		t.Fatalf("attempts = %d, want 0", st.tasks[0].Attempts)
	}
}

// K=3 耗尽：再扫仍空，当日落 skipped(no_target) 终态。
func TestWaitTargetKExhaustedLandsSkipped(t *testing.T) {
	task := baseTask()
	task.Result = "wait_target:3"
	st := newStore(task)
	st.windows["st-1|2026-10-29"] = openWindow()
	signer := defaultSigner()
	signer.targets = nil
	svc := newService(t, st, signer, fakeLimiter{allow: true})
	if _, err := svc.Step(context.Background(), 10); err != nil {
		t.Fatalf("Step: %v", err)
	}
	if st.finished["t1"] != corestatus.TaskSkipped || st.results["t1"] != string(contract.OutcomeNoTarget) {
		t.Fatalf("终态 = %v/%q, want skipped/no_target", st.finished["t1"], st.results["t1"])
	}
	if _, again := st.requeued["t1"]; again {
		t.Fatal("K 耗尽后不得再重排")
	}
}

// 窗口尾放不下下一跳（remaining×0.3 < base）：立即落终态，
// 不把落点写越窗口尾，也不无限等下去。
func TestWaitTargetWindowTailStops(t *testing.T) {
	task := baseTask()
	st := newStore(task)
	win := openWindow()
	win.End = testNow().Add(time.Minute) // cap = 60s×0.3 = 18s < base 5m
	st.windows["st-1|2026-10-29"] = win
	signer := defaultSigner()
	signer.targets = nil
	svc := newService(t, st, signer, fakeLimiter{allow: true})
	if _, err := svc.Step(context.Background(), 10); err != nil {
		t.Fatalf("Step: %v", err)
	}
	if st.finished["t1"] != corestatus.TaskSkipped || st.results["t1"] != string(contract.OutcomeNoTarget) {
		t.Fatalf("终态 = %v/%q, want skipped/no_target", st.finished["t1"], st.results["t1"])
	}
	if _, again := st.requeued["t1"]; again {
		t.Fatal("窗口尾放不下下一跳，不得重排")
	}
}

// 等待中途平台给出明确结论（State 2 → no_target）：直接终态，
// 不再进入下一轮等待。State 3/4（already_done）同理由词表用例钉住。
func TestExplicitNoTargetOverridesWaiting(t *testing.T) {
	task := baseTask()
	task.Result = "wait_target:1"
	st := newStore(task)
	st.windows["st-1|2026-10-29"] = openWindow()
	signer := &fakeSigner{err: &contract.OutcomeError{
		Outcome: contract.OutcomeNoTarget, Message: "今日无需签到",
	}}
	svc := newService(t, st, signer, fakeLimiter{allow: true})
	if _, err := svc.Step(context.Background(), 10); err != nil {
		t.Fatalf("Step: %v", err)
	}
	if st.finished["t1"] != corestatus.TaskSkipped || st.results["t1"] != string(contract.OutcomeNoTarget) {
		t.Fatalf("终态 = %v/%q, want skipped/no_target", st.finished["t1"], st.results["t1"])
	}
}

// 等待行的重扫拿到全新幂等键：上一次提交面 no_position 已入
// provider 缓存，复用旧键只会读回缓存、不发请求。
func TestWaitTargetRescanUsesFreshAttemptID(t *testing.T) {
	task := baseTask()
	task.Result = "wait_target:1"
	st := newStore(task)
	st.windows["st-1|2026-10-29"] = openWindow()
	signer := defaultSigner()
	svc := newService(t, st, signer, fakeLimiter{allow: true})
	if _, err := svc.Step(context.Background(), 10); err != nil {
		t.Fatalf("Step: %v", err)
	}
	if signer.signs != 1 {
		t.Fatalf("sign 调用 = %d, want 1", signer.signs)
	}
	if got := signer.attemptIDs[0]; got != "t1#0.1" {
		t.Fatalf("幂等键 = %q, want t1#0.1（与等待前扫描区分）", got)
	}
}

// 手动重开对等待行立即生效：退避清零（落点=现在）、
// 计数随 result 清空（新一轮等待周期）、审计沿用 task.rearm。
func TestRequestHastensWaitTargetRow(t *testing.T) {
	ctx := context.Background()
	actor := appaudit.Actor{UserID: "u1", Label: "u***@example.test"}
	task := baseTask()
	task.RunAt = testNow().Add(10 * time.Minute) // 退避中
	task.Result = "wait_target:2"
	st := newStore(task)
	st.enabledTasks = []string{"st-1"}
	st.windows["st-1|2026-10-29"] = openWindow()
	svc := newService(t, st, defaultSigner(), fakeLimiter{allow: true})
	id, err := svc.Request(ctx, "acct-1", actor)
	if err != nil || id == "" {
		t.Fatalf("Request = (%q,%v)", id, err)
	}
	if st.tasks[0].State != string(corestatus.TaskPending) {
		t.Fatalf("状态 = %s, want pending", st.tasks[0].State)
	}
	if !st.tasks[0].RunAt.Equal(testNow()) {
		t.Fatalf("落点 = %v, want %v（退避清零）", st.tasks[0].RunAt, testNow())
	}
	if st.tasks[0].Result != "" {
		t.Fatalf("result = %q, want 空（K 计数清零，新一轮等待）", st.tasks[0].Result)
	}
	if st.rearmed[st.tasks[0].ID] != string(corestatus.TaskPending) {
		t.Fatalf("重开审计来源 = %q, want pending", st.rearmed[st.tasks[0].ID])
	}
	if got := st.rearmActor[st.tasks[0].ID]; got != actor {
		t.Fatalf("重开审计主体 = %+v, want %+v", got, actor)
	}
}
