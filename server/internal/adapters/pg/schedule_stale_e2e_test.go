package pg_test

// 跨日 pending 行的清理与认领过滤（P1-6）。
//
// 判据：
//   - 日切清理把 D-1 的 pending 行落 skipped(window_closed)，D 行不动；
//   - ClaimDue 只领 day >= 今天的行：清理后又被放回 pending 的 D-1 行
//     仍然领不到（belt-and-suspenders）。

import (
	"testing"
	"time"

	coreschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/schedule"
	corestatus "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/status"
)

func TestStalePendingRowsAreSkippedAndNotClaimed(t *testing.T) {
	e := newEnv(t)
	schoolID, taskID, staleAccount := e.seedTask(
		"sched-stale", "晨间签到", "stale-a@example.com", "13800055551")
	todayAccount := e.createAccount(schoolID,
		e.createUser("stale-b@example.com").ID, "13800055552")
	e.activateAccount(todayAccount.ID)

	yesterday := "2026-10-28"
	today := "2026-10-29"
	at := time.Date(2026, 10, 29, 6, 40, 0, 0, time.UTC)
	if _, err := e.sched.InsertPlan(e.ctx, []coreschedule.PlanRow{
		{AccountID: staleAccount, SchoolTaskID: taskID, Day: yesterday, RunAt: at.Add(-24 * time.Hour)},
		{AccountID: todayAccount.ID, SchoolTaskID: taskID, Day: today, RunAt: at},
	}); err != nil {
		t.Fatalf("insert plan: %v", err)
	}

	// 日切清理：只动 D-1。
	n, err := e.sched.SkipStalePending(e.ctx, today, "window_closed")
	if err != nil || n != 1 {
		t.Fatalf("SkipStalePending = (%d,%v), want (1,nil)", n, err)
	}
	staleRow := e.taskRowID(staleAccount, taskID, yesterday)
	if got := e.taskState(staleRow); got != string(corestatus.TaskSkipped) {
		t.Fatalf("D-1 行状态 = %q, want skipped", got)
	}
	if got := e.scalarString("SELECT result FROM sign_tasks WHERE id = $1::uuid", staleRow); got != "window_closed" {
		t.Fatalf("D-1 行 result = %q, want window_closed", got)
	}
	todayRow := e.taskRowID(todayAccount.ID, taskID, today)
	if got := e.taskState(todayRow); got != string(corestatus.TaskPending) {
		t.Fatalf("D 行状态 = %q, want pending（清理不得误伤当日）", got)
	}

	// 当日行照常认领。
	claimed, err := e.sched.ClaimDue(e.ctx, at.Add(time.Minute), today, 10)
	if err != nil || len(claimed) != 1 || claimed[0].Day != today {
		t.Fatalf("ClaimDue = (%+v,%v), want only the today row", claimed, err)
	}

	// 把 D-1 行放回 pending（模拟清理后又出现的旧行）：day 过滤必须挡住。
	if _, err := e.db.Exec(e.ctx,
		"UPDATE sign_tasks SET state = 'pending', result = '' WHERE id = $1::uuid", staleRow); err != nil {
		t.Fatalf("reopen stale row: %v", err)
	}
	claimed, err = e.sched.ClaimDue(e.ctx, at.Add(2*time.Minute), today, 10)
	if err != nil {
		t.Fatalf("ClaimDue after reopen: %v", err)
	}
	if len(claimed) != 0 {
		t.Fatalf("跨日 pending 行被认领: %+v（day 过滤失效）", claimed)
	}
	if got := e.taskState(staleRow); got != string(corestatus.TaskPending) {
		t.Fatalf("D-1 行状态 = %q, want pending（未被认领）", got)
	}
}
