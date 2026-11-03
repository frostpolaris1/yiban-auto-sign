package pg_test

// 轮末汇总的库面口径：一条 GROUP BY 汇总某业务日的 sign_tasks。
// 用替身测不出 SQL 的日期过滤与状态聚合，故走真库。判据：
//   - 按任务行计数，不做账号去重（同账号多任务算多行）；
//   - pending 与 running 都进 Pending；
//   - 只有 failed 行的 result 进失败分布，空串原样保留（归 unknown 在 app 层）；
//   - 别的业务日不计入。

import (
	"testing"
	"time"

	coreschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/schedule"
	corestatus "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/status"
)

// setTaskOutcome 把一条已存在的任务行改成给定状态与结果。
func setTaskOutcome(t *testing.T, e *testEnv, accountID, taskID, day string, state corestatus.TaskState, result string) {
	t.Helper()
	if _, err := e.db.Exec(e.ctx, `
		UPDATE sign_tasks SET state = $4::sign_task_state, result = $5
		 WHERE account_id = $1::uuid AND school_task_id = $2::uuid AND day = $3::date`,
		accountID, taskID, day, string(state), result); err != nil {
		t.Fatalf("seed %s/%s %s = %s/%q: %v", accountID, taskID, day, state, result, err)
	}
}

func TestDayOutcomeCountsCountsTaskRowsNotAccounts(t *testing.T) {
	e := newEnv(t)
	schoolID, taskA, acct1 := e.seedTask(
		"round-summary", "晨间签到", "round-summary-a@example.com", "13800010202")
	taskB, err := e.sched.CreateSchoolTask(e.ctx, schoolID, "晚间签到")
	if err != nil {
		t.Fatalf("create second task: %v", err)
	}
	acct2 := e.createAccount(schoolID, e.createUser("round-summary-b@example.com").ID, "13800010101").ID
	acct3 := e.createAccount(schoolID, e.createUser("round-summary-c@example.com").ID, "13800010303").ID

	const day = "2026-10-28"
	const otherDay = "2026-10-27"
	at, err := time.Parse("2006-01-02", day)
	if err != nil {
		t.Fatalf("parse day: %v", err)
	}
	if _, err := e.sched.InsertPlan(e.ctx, []coreschedule.PlanRow{
		{AccountID: acct1, SchoolTaskID: taskA, Day: day, RunAt: at.Add(6*time.Hour + 40*time.Minute)},
		{AccountID: acct1, SchoolTaskID: taskB, Day: day, RunAt: at.Add(6*time.Hour + 40*time.Minute)},
		{AccountID: acct2, SchoolTaskID: taskA, Day: day, RunAt: at.Add(6*time.Hour + 40*time.Minute)},
		{AccountID: acct2, SchoolTaskID: taskB, Day: day, RunAt: at.Add(6*time.Hour + 40*time.Minute)},
		{AccountID: acct3, SchoolTaskID: taskA, Day: day, RunAt: at.Add(6*time.Hour + 40*time.Minute)},
		{AccountID: acct3, SchoolTaskID: taskB, Day: day, RunAt: at.Add(6*time.Hour + 40*time.Minute)},
		// 别的业务日：不得计入 day 的汇总。
		{AccountID: acct1, SchoolTaskID: taskA, Day: otherDay, RunAt: at.Add(6*time.Hour + 40*time.Minute)},
	}); err != nil {
		t.Fatalf("insert plan: %v", err)
	}

	// 同账号两行：证明口径按任务行，不做账号去重。
	setTaskOutcome(t, e, acct1, taskA, day, corestatus.TaskSuccess, "success")
	setTaskOutcome(t, e, acct1, taskB, day, corestatus.TaskFailed, "rejected")
	// 空 result 的失败行：本层保留空键，app 层归 unknown。
	setTaskOutcome(t, e, acct2, taskA, day, corestatus.TaskFailed, "")
	setTaskOutcome(t, e, acct2, taskB, day, corestatus.TaskSkipped, "no_target")
	// pending 与 running 都算未了结；running 每账号至多一行。
	setTaskOutcome(t, e, acct3, taskA, day, corestatus.TaskPending, "")
	setTaskOutcome(t, e, acct3, taskB, day, corestatus.TaskRunning, "")
	// 别的日期：不计入 day。
	setTaskOutcome(t, e, acct1, taskA, otherDay, corestatus.TaskSuccess, "success")

	counts, err := e.sched.DayOutcomeCounts(e.ctx, day)
	if err != nil {
		t.Fatalf("DayOutcomeCounts: %v", err)
	}
	if counts.Total != 6 || counts.Success != 1 || counts.Failed != 2 ||
		counts.Skipped != 1 || counts.Pending != 2 {
		t.Fatalf("counts = %+v, want total=6 success=1 failed=2 skipped=1 pending=2", counts)
	}
	if len(counts.FailedByOutcome) != 2 {
		t.Fatalf("失败分布 = %v, want 2 个桶（rejected 与空串）", counts.FailedByOutcome)
	}
	if counts.FailedByOutcome["rejected"] != 1 {
		t.Fatalf("FailedByOutcome[rejected] = %d, want 1", counts.FailedByOutcome["rejected"])
	}
	if counts.FailedByOutcome[""] != 1 {
		t.Fatalf("FailedByOutcome[空串] = %d, want 1（原值保留，归 unknown 在 app 层）", counts.FailedByOutcome[""])
	}

	other, err := e.sched.DayOutcomeCounts(e.ctx, otherDay)
	if err != nil {
		t.Fatalf("DayOutcomeCounts(other day): %v", err)
	}
	if other.Total != 1 || other.Success != 1 || other.Failed != 0 ||
		other.Skipped != 0 || other.Pending != 0 || len(other.FailedByOutcome) != 0 {
		t.Fatalf("other day = %+v, want total=1 success=1 其余 0", other)
	}

	empty, err := e.sched.DayOutcomeCounts(e.ctx, "2026-10-01")
	if err != nil {
		t.Fatalf("DayOutcomeCounts(empty day): %v", err)
	}
	if empty.Total != 0 || empty.Success != 0 || empty.Failed != 0 ||
		empty.Skipped != 0 || empty.Pending != 0 || len(empty.FailedByOutcome) != 0 {
		t.Fatalf("空日 = %+v, want 零值", empty)
	}
}
