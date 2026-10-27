package pg_test

// sign_tasks 仓储 e2e：认领判据与退避记账。
// 依赖真实 PostgreSQL：CTE 认领语句、CAS 与 CHECK 都在库侧执行。

import (
	"testing"
	"time"

	coreschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/schedule"
	corestatus "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/status"
)

// TestClaimDueClaimsOnlyPendingRows 钉住 ClaimDue 的 state 判据。
//
// 造两条「到期、账号活跃、任务启用、同账号无 running」的行：
// 一条 pending，一条已是终态 success。
// 判据：只领 pending；终态行保持原状态，**绝不改回 running**。
//
// 删掉 picked 的 state='pending' 过滤时，终态行会被改写成 running——本用例红。
func TestClaimDueClaimsOnlyPendingRows(t *testing.T) {
	e := newEnv(t)
	schoolID, taskID, pendingAccount := e.seedTask(
		"claim-due", "晨间签到", "claim-due-a@example.com", "13800011111")
	terminalAccount := e.createAccount(schoolID,
		e.createUser("claim-due-b@example.com").ID, "13800022222")
	e.activateAccount(terminalAccount.ID)

	day := "2026-10-29"
	due := time.Date(2026, 10, 29, 6, 40, 0, 0, time.UTC)
	if _, err := e.sched.InsertPlan(e.ctx, []coreschedule.PlanRow{
		{AccountID: pendingAccount, SchoolTaskID: taskID, Day: day, RunAt: due},
		{AccountID: terminalAccount.ID, SchoolTaskID: taskID, Day: day, RunAt: due},
	}); err != nil {
		t.Fatalf("insert plan: %v", err)
	}
	// 终态行：到期 + 活跃账号 + 任务启用，只差 state 不是 pending。
	if _, err := e.db.Exec(e.ctx, `
		UPDATE sign_tasks SET state = $4::sign_task_state, result = $5, attempts = 1
		 WHERE account_id = $1::uuid AND school_task_id = $2::uuid AND day = $3`,
		terminalAccount.ID, taskID, day,
		string(corestatus.TaskSuccess), string(corestatus.TaskSuccess)); err != nil {
		t.Fatalf("seed the terminal row: %v", err)
	}
	terminalRowID := e.taskRowID(terminalAccount.ID, taskID, day)
	pendingRowID := e.taskRowID(pendingAccount, taskID, day)

	now := due.Add(time.Minute)
	claimed, err := e.sched.ClaimDue(e.ctx, now, 10)
	if err != nil {
		t.Fatalf("claim due: %v", err)
	}
	if len(claimed) != 1 {
		t.Fatalf("claimed %d rows, want 1 (only the pending row)", len(claimed))
	}
	if claimed[0].ID != pendingRowID || claimed[0].AccountID != pendingAccount {
		t.Fatalf("claimed the wrong row: %+v", claimed[0])
	}
	if claimed[0].State != string(corestatus.TaskRunning) {
		t.Fatalf("claimed row state is %q; want running", claimed[0].State)
	}

	if got := e.taskState(terminalRowID); got != string(corestatus.TaskSuccess) {
		t.Fatalf("terminal row state is %q after claim; want success", got)
	}
	if got := e.scalarInt("SELECT (claimed_at = $2::timestamptz)::int FROM sign_tasks WHERE id = $1::uuid",
		pendingRowID, now); got != 1 {
		t.Fatalf("claimed_at was not set to the claim instant")
	}
}

// TestRearmClearsResultAndKeepsAttempts 钉住 Rearm 的记账口径。
//
// failed 行重新入队：state 回 pending、result 清空、claimed_at 清空、落点更新。
// attempts **保留**：重排不退还当日已消耗的预算。
//
// 删掉清空 result 的赋值时旧结论残留——本用例红。
// 删掉清空 claimed_at 的赋值时旧认领时刻残留——本用例红。
func TestRearmClearsResultAndKeepsAttempts(t *testing.T) {
	e := newEnv(t)
	_, taskID, accountID := e.seedTask(
		"rearm", "晨间签到", "rearm-owner@example.com", "13800033333")
	day := "2026-10-29"
	oldAt := time.Date(2026, 10, 29, 6, 40, 0, 0, time.UTC)
	if _, err := e.sched.InsertPlan(e.ctx, []coreschedule.PlanRow{
		{AccountID: accountID, SchoolTaskID: taskID, Day: day, RunAt: oldAt},
	}); err != nil {
		t.Fatalf("insert plan: %v", err)
	}
	if _, err := e.db.Exec(e.ctx, `
		UPDATE sign_tasks SET state = $4, result = 'rejected', attempts = 2, claimed_at = $5
		 WHERE account_id = $1::uuid AND school_task_id = $2::uuid AND day = $3`,
		accountID, taskID, day, string(corestatus.TaskFailed), oldAt); err != nil {
		t.Fatalf("seed the failed row: %v", err)
	}
	rowID := e.taskRowID(accountID, taskID, day)
	if got := e.scalarInt("SELECT count(*) FROM sign_tasks WHERE id = $1::uuid AND claimed_at = $2::timestamptz", rowID, oldAt); got != 1 {
		t.Fatalf("seed did not set claimed_at; the clear assertion would be vacuous")
	}

	newAt := oldAt.Add(90 * time.Minute)
	if err := e.sched.Rearm(e.ctx, rowID, newAt); err != nil {
		t.Fatalf("rearm: %v", err)
	}

	if got := e.taskState(rowID); got != string(corestatus.TaskPending) {
		t.Fatalf("state after rearm is %q; want pending", got)
	}
	if got := e.scalarString("SELECT result FROM sign_tasks WHERE id = $1::uuid", rowID); got != "" {
		t.Fatalf("result after rearm is %q; want empty", got)
	}
	if got := e.scalarInt("SELECT attempts FROM sign_tasks WHERE id = $1::uuid", rowID); got != 2 {
		t.Fatalf("attempts after rearm is %d; want 2 (rearm does not refund budget)", got)
	}
	if got := e.scalarInt("SELECT count(*) FROM sign_tasks WHERE id = $1::uuid AND claimed_at IS NULL", rowID); got != 1 {
		t.Fatalf("claimed_at was not cleared by rearm")
	}
	if got := e.scalarInt("SELECT (run_at = $2::timestamptz)::int FROM sign_tasks WHERE id = $1::uuid", rowID, newAt); got != 1 {
		t.Fatalf("run_at was not moved by rearm")
	}
}

// TestRequeueRetryCountsEveryAttempt 钉住 RequeueRetry 的尝试计数。
//
// 判据一：每次业务失败退避把 attempts 加一。
// 判据二：RequeueWait 是等待，不计尝试数——等待不是「试过了没成」。
//
// 删掉 attempts = attempts + 1 时计数不动——本用例红。
func TestRequeueRetryCountsEveryAttempt(t *testing.T) {
	e := newEnv(t)
	_, taskID, accountID := e.seedTask(
		"requeue", "晨间签到", "requeue-owner@example.com", "13800044444")
	day := "2026-10-29"
	base := time.Date(2026, 10, 29, 6, 40, 0, 0, time.UTC)
	if _, err := e.sched.InsertPlan(e.ctx, []coreschedule.PlanRow{
		{AccountID: accountID, SchoolTaskID: taskID, Day: day, RunAt: base},
	}); err != nil {
		t.Fatalf("insert plan: %v", err)
	}
	rowID := e.taskRowID(accountID, taskID, day)

	claim := func(at time.Time) {
		t.Helper()
		claimed, err := e.sched.ClaimDue(e.ctx, at, 10)
		if err != nil {
			t.Fatalf("claim due at %s: %v", at, err)
		}
		if len(claimed) != 1 || claimed[0].ID != rowID {
			t.Fatalf("claimed %+v; want the seeded row", claimed)
		}
	}

	// 第一次业务失败：attempts 0 → 1。
	claim(base.Add(time.Minute))
	if err := e.sched.RequeueRetry(e.ctx, rowID, base.Add(2*time.Minute), "rejected"); err != nil {
		t.Fatalf("requeue retry 1: %v", err)
	}
	if got := e.scalarInt("SELECT attempts FROM sign_tasks WHERE id = $1::uuid", rowID); got != 1 {
		t.Fatalf("attempts after the first retry is %d; want 1", got)
	}
	if got := e.taskState(rowID); got != string(corestatus.TaskPending) {
		t.Fatalf("state after requeue retry is %q; want pending", got)
	}
	if got := e.scalarInt("SELECT count(*) FROM sign_tasks WHERE id = $1::uuid AND claimed_at IS NULL", rowID); got != 1 {
		t.Fatalf("claimed_at was not cleared by requeue retry")
	}

	// 第二次业务失败：1 → 2。把 +1 写死成 =1 会在这一步红。
	claim(base.Add(3 * time.Minute))
	if err := e.sched.RequeueRetry(e.ctx, rowID, base.Add(4*time.Minute), "rejected"); err != nil {
		t.Fatalf("requeue retry 2: %v", err)
	}
	if got := e.scalarInt("SELECT attempts FROM sign_tasks WHERE id = $1::uuid", rowID); got != 2 {
		t.Fatalf("attempts after the second retry is %d; want 2", got)
	}

	// 等待退避不计尝试数：2 保持 2。
	claim(base.Add(5 * time.Minute))
	if err := e.sched.RequeueWait(e.ctx, rowID, base.Add(6*time.Minute), "wait_window_open"); err != nil {
		t.Fatalf("requeue wait: %v", err)
	}
	if got := e.scalarInt("SELECT attempts FROM sign_tasks WHERE id = $1::uuid", rowID); got != 2 {
		t.Fatalf("attempts after a wait requeue is %d; want 2 (waiting is not an attempt)", got)
	}
}

// taskRowID 取一行 sign_tasks 的主键。
func (e *testEnv) taskRowID(accountID, taskID, day string) string {
	e.t.Helper()
	return e.scalarString(
		"SELECT id::text FROM sign_tasks WHERE account_id = $1::uuid AND school_task_id = $2::uuid AND day = $3",
		accountID, taskID, day)
}

// taskState 取一行 sign_tasks 的状态字面量。
func (e *testEnv) taskState(rowID string) string {
	e.t.Helper()
	return e.scalarString("SELECT state::text FROM sign_tasks WHERE id = $1::uuid", rowID)
}
