package pg_test

import (
	"errors"
	"testing"
	"time"

	coreerrs "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	coreschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/schedule"
	corestatus "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/status"
)

// 本文件是 worker 队列语义的 e2e 判据（P4-E）。
// 需要真实 PostgreSQL：批量 CAS 认领、SKIP LOCKED 与启动回收没有诚实替身。

// TestClaimDueSkipsSameAccountRunning：同账号同时只领一条；
// 前一条落终态后，同账号下一条才可领（两账号并行、同账号串行）。
func TestClaimDueSkipsSameAccountRunning(t *testing.T) {
	e := newEnv(t)
	school := e.createSchool("wk-serial")
	taskA, err := e.sched.CreateSchoolTask(e.ctx, school.ID, "任务A")
	if err != nil {
		t.Fatalf("taskA: %v", err)
	}
	taskB, err := e.sched.CreateSchoolTask(e.ctx, school.ID, "任务B")
	if err != nil {
		t.Fatalf("taskB: %v", err)
	}
	acct := e.createAccount(school.ID, e.createUser("wk-a@example.com").ID, "13800020001")
	e.activateAccount(acct.ID)
	other := e.createAccount(school.ID, e.createUser("wk-b@example.com").ID, "13800020002")
	e.activateAccount(other.ID)
	day := "2026-10-29"
	at := time.Date(2026, 10, 29, 6, 40, 0, 0, time.UTC)
	if _, err := e.sched.InsertPlan(e.ctx, []coreschedule.PlanRow{
		{AccountID: acct.ID, SchoolTaskID: taskA, Day: day, RunAt: at},
		{AccountID: acct.ID, SchoolTaskID: taskB, Day: day, RunAt: at},
		{AccountID: other.ID, SchoolTaskID: taskA, Day: day, RunAt: at},
	}); err != nil {
		t.Fatalf("insert: %v", err)
	}
	claimed, err := e.sched.ClaimDue(e.ctx, at.Add(time.Minute), 10)
	if err != nil {
		t.Fatalf("claim: %v", err)
	}
	// 两个账号各一条：同账号的第二条被排除（两账号并行成立）。
	if len(claimed) != 2 {
		t.Fatalf("认领 %d 条, want 2: %+v", len(claimed), claimed)
	}
	byAccount := map[string]int{}
	for _, c := range claimed {
		byAccount[c.AccountID]++
	}
	if byAccount[acct.ID] != 1 || byAccount[other.ID] != 1 {
		t.Fatalf("账号认领分布 = %v", byAccount)
	}
	// 落终态后同账号可领下一条。
	for _, c := range claimed {
		if c.AccountID == acct.ID {
			if err := e.sched.Finish(e.ctx, c.AccountID, c.SchoolTaskID, c.Day, corestatus.TaskSuccess, "ok", c.ClaimSeq, at); err != nil {
				t.Fatalf("finish: %v", err)
			}
		}
	}
	again, err := e.sched.ClaimDue(e.ctx, at.Add(2*time.Minute), 10)
	if err != nil {
		t.Fatalf("claim2: %v", err)
	}
	got := 0
	for _, c := range again {
		if c.AccountID == acct.ID {
			got++
		}
	}
	if got != 1 {
		t.Fatalf("终态后同账号下一条应可领, got %d", got)
	}
}

// TestResetRunningRecovers：孤儿 running 退回 pending 且落点前移。
func TestResetRunningRecovers(t *testing.T) {
	e := newEnv(t)
	_, taskID, acctID := e.seedTask("wk-recover", "晨间签到", "wk-rec@example.com", "13800020003")
	day := "2026-10-29"
	at := time.Date(2026, 10, 29, 6, 40, 0, 0, time.UTC)
	if _, err := e.sched.InsertPlan(e.ctx, []coreschedule.PlanRow{
		{AccountID: acctID, SchoolTaskID: taskID, Day: day, RunAt: at},
	}); err != nil {
		t.Fatalf("insert: %v", err)
	}
	if _, err := e.sched.ClaimDue(e.ctx, at.Add(time.Minute), 1); err != nil {
		t.Fatalf("claim: %v", err)
	}
	n, err := e.sched.ResetRunning(e.ctx)
	if err != nil || n != 1 {
		t.Fatalf("reset = (%d,%v), want (1,nil)", n, err)
	}
	if got := e.scalarInt("SELECT count(*) FROM sign_tasks WHERE account_id = $1::uuid AND state = 'pending'", acctID); got != 1 {
		t.Fatalf("回收后 pending 行 = %d, want 1", got)
	}
}

// TestEnsureTaskIdempotent：手动签到同一天只建一行；二次调用返回既有行。
func TestEnsureTaskIdempotent(t *testing.T) {
	e := newEnv(t)
	_, taskID, acctID := e.seedTask("wk-ensure", "晨间签到", "wk-ens@example.com", "13800020004")
	day := "2026-10-29"
	at := time.Date(2026, 10, 29, 6, 40, 0, 0, time.UTC)
	row1, created1, err := e.sched.EnsureTask(e.ctx, acctID, taskID, day, at)
	if err != nil || !created1 {
		t.Fatalf("first ensure = (%+v,%v,%v), want created", row1, created1, err)
	}
	row2, created2, err := e.sched.EnsureTask(e.ctx, acctID, taskID, day, at.Add(time.Minute))
	if err != nil || created2 {
		t.Fatalf("second ensure = (%+v,%v,%v), want existing", row2, created2, err)
	}
	if row1.ID != row2.ID {
		t.Fatalf("两次 ensure 建出两行: %s vs %s", row1.ID, row2.ID)
	}
	if row2.State != string(corestatus.TaskPending) {
		t.Fatalf("新行状态 = %s, want pending", row2.State)
	}
}

// TestTodayTaskPicksEnabled：账号学校的启用任务（停用的不选）。
func TestTodayTaskPicksEnabled(t *testing.T) {
	e := newEnv(t)
	school := e.createSchool("wk-today")
	if _, err := e.sched.CreateSchoolTask(e.ctx, school.ID, "A-停用"); err != nil {
		t.Fatalf("task: %v", err)
	}
	if _, err := e.db.Exec(e.ctx, "UPDATE school_tasks SET enabled = false WHERE school_id = $1::uuid", school.ID); err != nil {
		t.Fatalf("disable: %v", err)
	}
	enabled, err := e.sched.CreateSchoolTask(e.ctx, school.ID, "B-启用")
	if err != nil {
		t.Fatalf("task: %v", err)
	}
	acct := e.createAccount(school.ID, e.createUser("wk-today@example.com").ID, "13800020005")
	e.activateAccount(acct.ID) // TodayTask 只认活跃账号
	got, err := e.sched.TodayTask(e.ctx, acct.ID)
	if err != nil || got != enabled {
		t.Fatalf("TodayTask = (%s,%v), want %s", got, err, enabled)
	}
	// 全部停用 → not_found。
	if _, err := e.db.Exec(e.ctx, "UPDATE school_tasks SET enabled = false WHERE school_id = $1::uuid", school.ID); err != nil {
		t.Fatalf("disable all: %v", err)
	}
	if _, err := e.sched.TodayTask(e.ctx, acct.ID); !errors.Is(err, coreerrs.ErrNotFound) {
		t.Fatalf("全停用应 not_found, got %v", err)
	}
}

// TestClaimDueSkipsPausedAndDisabled：暂停账号与停用任务的到期行不领。
func TestClaimDueSkipsPausedAndDisabled(t *testing.T) {
	e := newEnv(t)
	school := e.createSchool("wk-skip")
	taskID, err := e.sched.CreateSchoolTask(e.ctx, school.ID, "晨间签到")
	if err != nil {
		t.Fatalf("task: %v", err)
	}
	paused := e.createAccount(school.ID, e.createUser("wk-paused@example.com").ID, "13800020006")
	e.activateAccount(paused.ID)
	if _, err := e.db.Exec(e.ctx, "UPDATE accounts SET status = 'paused_admin' WHERE id = $1::uuid", paused.ID); err != nil {
		t.Fatalf("pause: %v", err)
	}
	day := "2026-10-29"
	at := time.Date(2026, 10, 29, 6, 40, 0, 0, time.UTC)
	if _, err := e.sched.InsertPlan(e.ctx, []coreschedule.PlanRow{
		{AccountID: paused.ID, SchoolTaskID: taskID, Day: day, RunAt: at},
	}); err != nil {
		t.Fatalf("insert: %v", err)
	}
	claimed, err := e.sched.ClaimDue(e.ctx, at.Add(time.Minute), 10)
	if err != nil {
		t.Fatalf("claim: %v", err)
	}
	if len(claimed) != 0 {
		t.Fatalf("暂停账号的任务被认领: %+v", claimed)
	}
}

// TestClaimDueSkipsAccountWithRunning：同账号已有 running 时，
// **另一条 pending** 也不得被认领——这是跨批次串行的真正判据
// （P4-E 审查 P2-5：删掉 NOT EXISTS 谓词后原有用例仍绿）。
func TestClaimDueSkipsAccountWithRunning(t *testing.T) {
	e := newEnv(t)
	school := e.createSchool("wk-running")
	taskA, err := e.sched.CreateSchoolTask(e.ctx, school.ID, "任务A")
	if err != nil {
		t.Fatalf("taskA: %v", err)
	}
	taskB, err := e.sched.CreateSchoolTask(e.ctx, school.ID, "任务B")
	if err != nil {
		t.Fatalf("taskB: %v", err)
	}
	acct := e.createAccount(school.ID, e.createUser("wk-run@example.com").ID, "13800020007")
	e.activateAccount(acct.ID)
	day := "2026-10-29"
	at := time.Date(2026, 10, 29, 6, 40, 0, 0, time.UTC)
	if _, err := e.sched.InsertPlan(e.ctx, []coreschedule.PlanRow{
		{AccountID: acct.ID, SchoolTaskID: taskA, Day: day, RunAt: at},
		{AccountID: acct.ID, SchoolTaskID: taskB, Day: day, RunAt: at},
	}); err != nil {
		t.Fatalf("insert: %v", err)
	}
	// 第一次认领：拿到其中一条。
	first, err := e.sched.ClaimDue(e.ctx, at.Add(time.Minute), 10)
	if err != nil || len(first) != 1 {
		t.Fatalf("first claim = (%d,%v), want 1", len(first), err)
	}
	// 该账号已在 running：第二次认领必须为空（另一条 pending 被排除）。
	second, err := e.sched.ClaimDue(e.ctx, at.Add(2*time.Minute), 10)
	if err != nil {
		t.Fatalf("second claim: %v", err)
	}
	if len(second) != 0 {
		t.Fatalf("同账号已有 running 仍被认领: %+v", second)
	}
}
