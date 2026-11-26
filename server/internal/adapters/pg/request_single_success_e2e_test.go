package pg_test

// 手动签到的库侧判据（决策 2A）：同账号同日已有 success 时，
// Request 先于解析任务/建行返回 already_signed，兄弟任务的行不被重开。
//
// 与 worker 单测的分工：单测钉调用顺序（不读日任务、不建行），
// 这里钉真实 SQL 能读出 success、且 Request 走的是前置检查而不是
// EnsureTask 的兜底分支——兄弟行留 pending/failed/skipped 原状。

import (
	"errors"
	"testing"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/pg"
	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	appworker "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/worker"
	coreschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/schedule"
	corestatus "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/status"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/contract"
)

// putOpenWindow 给任务铺一个在执行时刻开着的窗口。
func putOpenWindow(t *testing.T, e *testEnv, taskID string) {
	t.Helper()
	at := db1At()
	if _, err := e.sched.PutWindow(e.ctx, coreschedule.WindowRecord{
		SchoolTaskID: taskID, Day: db1Day,
		Start: at.Add(-10 * time.Minute), End: at.Add(20 * time.Minute),
		Source: string(contract.WindowPlatform),
	}, 0); err != nil {
		t.Fatalf("put window for %s: %v", taskID, err)
	}
}

// TestRequestPGAlreadySignedAfterSiblingSuccess：A 已 success、B 当日已有行时，
// 手动签到必须回 already_signed，且 B 行保持原状态（不被认领/重开）。
//
// 删掉 Request 的前置 HasSuccessOn 时：pending 的 B 被 EnsureTask 取到并
// 原样返回（err=nil）；failed/skipped 的 B 被 Rearm 重开（err=nil）——均红。
func TestRequestPGAlreadySignedAfterSiblingSuccess(t *testing.T) {
	for _, state := range []corestatus.TaskState{
		corestatus.TaskPending, corestatus.TaskFailed, corestatus.TaskSkipped,
	} {
		t.Run(string(state), func(t *testing.T) {
			e := newEnv(t)
			_, accountID, taskA, taskB := seedSiblingTasks(t, e,
				"req-single-"+string(state), "req-single-"+string(state)+"@example.com", "13800061001")
			at := db1At()
			if _, err := e.sched.InsertPlan(e.ctx, []coreschedule.PlanRow{
				{AccountID: accountID, SchoolTaskID: taskA, Day: db1Day, RunAt: at},
				{AccountID: accountID, SchoolTaskID: taskB, Day: db1Day, RunAt: at},
			}); err != nil {
				t.Fatalf("insert plan: %v", err)
			}
			// A 没有窗口（不受候选排序偏爱），B 窗口开着：坏实现会选中 B。
			putOpenWindow(t, e, taskB)
			setSignTaskState(t, e, accountID, taskA, db1Day, corestatus.TaskSuccess)
			if state != corestatus.TaskPending {
				setSignTaskState(t, e, accountID, taskB, db1Day, state)
			}
			bRowID := e.taskRowID(accountID, taskB, db1Day)
			beforeState := e.taskState(bRowID)

			store := pg.NewScheduleRepoWithAudit(e.db, e.audits)
			svc := db1Worker(t, store)
			_, err := svc.Request(e.ctx, accountID, appaudit.Actor{UserID: "u-req-single"})
			if !errors.Is(err, appworker.ErrAlreadySigned) {
				t.Fatalf("Request err = %v, want ErrAlreadySigned", err)
			}
			if got := e.taskState(bRowID); got != beforeState {
				t.Fatalf("sibling row state = %q, want unchanged %q", got, beforeState)
			}
			if got := e.scalarInt(
				"SELECT count(*) FROM sign_tasks WHERE account_id = $1::uuid AND day = $2::date",
				accountID, db1Day); got != 2 {
				t.Fatalf("rows for account/day = %d, want 2 (no new row)", got)
			}
		})
	}
}

// TestRequestPGDoesNotCreateSiblingRow：当日只有 A 的 success 行时，
// 手动签到不得为另一启用任务 B 新建行。
//
// 这条兜底覆盖"先建行再判状态"的写法：Request 必须 already_signed，
// 且 B 的行数保持 0。
func TestRequestPGDoesNotCreateSiblingRow(t *testing.T) {
	e := newEnv(t)
	_, accountID, taskA, taskB := seedSiblingTasks(t, e,
		"req-single-norow", "req-single-norow@example.com", "13800061002")
	at := db1At()
	if _, err := e.sched.InsertPlan(e.ctx, []coreschedule.PlanRow{
		{AccountID: accountID, SchoolTaskID: taskA, Day: db1Day, RunAt: at},
	}); err != nil {
		t.Fatalf("insert plan: %v", err)
	}
	putOpenWindow(t, e, taskA)
	putOpenWindow(t, e, taskB)
	setSignTaskState(t, e, accountID, taskA, db1Day, corestatus.TaskSuccess)

	store := pg.NewScheduleRepoWithAudit(e.db, e.audits)
	svc := db1Worker(t, store)
	if _, err := svc.Request(e.ctx, accountID, appaudit.Actor{UserID: "u-req-norow"}); !errors.Is(err, appworker.ErrAlreadySigned) {
		t.Fatalf("Request err = %v, want ErrAlreadySigned", err)
	}
	if got := e.scalarInt(
		"SELECT count(*) FROM sign_tasks WHERE account_id = $1::uuid AND school_task_id = $2::uuid AND day = $3::date",
		accountID, taskB, db1Day); got != 0 {
		t.Fatalf("sibling task rows = %d, want 0", got)
	}
}

// TestRequestPGSuccessOnAnotherDayStillQueues：昨天的 success 不算今天已签到。
// 今天无行、仅一个启用任务时 Request 正常建行并返回行 ID。
func TestRequestPGSuccessOnAnotherDayStillQueues(t *testing.T) {
	e := newEnv(t)
	_, taskID, accountID := e.seedTask(
		"req-single-yesterday", "晨间签到", "req-single-yesterday@example.com", "13800061003")
	yesterday := db1At().Add(-24 * time.Hour)
	if _, err := e.sched.InsertPlan(e.ctx, []coreschedule.PlanRow{
		{AccountID: accountID, SchoolTaskID: taskID, Day: yesterday.Format("2006-01-02"), RunAt: yesterday},
	}); err != nil {
		t.Fatalf("insert plan: %v", err)
	}
	setSignTaskState(t, e, accountID, taskID, yesterday.Format("2006-01-02"), corestatus.TaskSuccess)
	putOpenWindow(t, e, taskID)

	store := pg.NewScheduleRepoWithAudit(e.db, e.audits)
	svc := db1Worker(t, store)
	id, err := svc.Request(e.ctx, accountID, appaudit.Actor{UserID: "u-req-yesterday"})
	if err != nil || id == "" {
		t.Fatalf("Request = (%q,%v), want a queued row", id, err)
	}
	if got := e.taskState(id); got != string(corestatus.TaskPending) {
		t.Fatalf("new row state = %q, want pending", got)
	}
}
