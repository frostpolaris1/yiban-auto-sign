package pg_test

// 决策 2A（P1-5d）的库侧判据：同账号同日只要有一行 success，
// 兄弟任务不再被认领；skipped/failed 不算成功，不拦兄弟任务。
//
// 判据必须由真实 SQL 承担：DISTINCT ON 只领同账号一条，
// 加上 success 排除后，A 成功即 B 不再被领；造数只走 newEnv。

import (
	"testing"
	"time"

	coreschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/schedule"
	corestatus "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/status"
)

const singleSuccessDay = "2026-10-29"

func singleSuccessAt() time.Time {
	return time.Date(2026, 10, 29, 6, 40, 0, 0, time.UTC)
}

// seedSiblingTasks 建同一学校、同一账号下的两个启用任务（A/B），返回两者 ID。
func seedSiblingTasks(t *testing.T, e *testEnv, code, email, phone string) (schoolID, accountID, taskA, taskB string) {
	t.Helper()
	schoolID, taskA, accountID = e.seedTask(code, "晨间签到", email, phone)
	var err error
	taskB, err = e.sched.CreateSchoolTask(e.ctx, schoolID, "午间签到")
	if err != nil {
		t.Fatalf("create sibling task: %v", err)
	}
	return schoolID, accountID, taskA, taskB
}

// setSignTaskState 直接落一条终态，模拟 A 已结算。
func setSignTaskState(t *testing.T, e *testEnv, accountID, taskID, day string, state corestatus.TaskState) {
	t.Helper()
	if _, err := e.db.Exec(e.ctx, `
		UPDATE sign_tasks SET state = $4::sign_task_state, result = $5
		 WHERE account_id = $1::uuid AND school_task_id = $2::uuid AND day = $3::date
		`,
		accountID, taskID, day, string(state), string(state)); err != nil {
		t.Fatalf("seed sign task state %s: %v", state, err)
	}
}

// TestClaimDueSkipsSiblingAfterSuccess 是决策 2A 在认领面的主判据：
// A 已 success 时，同日 B 的 pending 行不得被领走（状态保持 pending）。
//
// 删掉 picked CTE 的 NOT EXISTS(success) 时，B 会被认领成 running——本用例红。
func TestClaimDueSkipsSiblingAfterSuccess(t *testing.T) {
	e := newEnv(t)
	_, accountID, taskA, taskB := seedSiblingTasks(t, e,
		"claim-single-success", "claim-single-success@example.com", "13800060001")
	at := singleSuccessAt()
	if _, err := e.sched.InsertPlan(e.ctx, []coreschedule.PlanRow{
		{AccountID: accountID, SchoolTaskID: taskA, Day: singleSuccessDay, RunAt: at},
		{AccountID: accountID, SchoolTaskID: taskB, Day: singleSuccessDay, RunAt: at},
	}); err != nil {
		t.Fatalf("insert plan: %v", err)
	}
	setSignTaskState(t, e, accountID, taskA, singleSuccessDay, corestatus.TaskSuccess)
	bRowID := e.taskRowID(accountID, taskB, singleSuccessDay)

	claimed, err := e.sched.ClaimDue(e.ctx, at.Add(time.Minute), singleSuccessDay, 10)
	if err != nil {
		t.Fatalf("claim due: %v", err)
	}
	if len(claimed) != 0 {
		t.Fatalf("claimed %d rows after a sibling success, want 0: %+v", len(claimed), claimed)
	}
	if got := e.taskState(bRowID); got != string(corestatus.TaskPending) {
		t.Fatalf("sibling row state = %q, want pending (must not be claimed)", got)
	}
	if got := e.scalarInt("SELECT count(*) FROM sign_tasks WHERE account_id = $1::uuid AND day = $2::date AND state = 'running'",
		accountID, singleSuccessDay); got != 0 {
		t.Fatalf("running rows for the account = %d, want 0", got)
	}
}

// TestClaimDueClaimsSiblingWhenNotSuccess 钉住只判 success：
// A 是 skipped 或 failed 时，B 的 pending 行照常被领——非成功不是成功。
//
// 把排除条件写成 state <> 'pending' / state IN ('success','skipped') 时，
// skipped 子用例会红。
func TestClaimDueClaimsSiblingWhenNotSuccess(t *testing.T) {
	for _, state := range []corestatus.TaskState{corestatus.TaskSkipped, corestatus.TaskFailed} {
		t.Run(string(state), func(t *testing.T) {
			e := newEnv(t)
			_, accountID, taskA, taskB := seedSiblingTasks(t, e,
				"claim-non-success-"+string(state), "claim-non-success-"+string(state)+"@example.com", "13800060002")
			at := singleSuccessAt()
			if _, err := e.sched.InsertPlan(e.ctx, []coreschedule.PlanRow{
				{AccountID: accountID, SchoolTaskID: taskA, Day: singleSuccessDay, RunAt: at},
				{AccountID: accountID, SchoolTaskID: taskB, Day: singleSuccessDay, RunAt: at},
			}); err != nil {
				t.Fatalf("insert plan: %v", err)
			}
			setSignTaskState(t, e, accountID, taskA, singleSuccessDay, state)
			bRowID := e.taskRowID(accountID, taskB, singleSuccessDay)

			claimed, err := e.sched.ClaimDue(e.ctx, at.Add(time.Minute), singleSuccessDay, 10)
			if err != nil {
				t.Fatalf("claim due: %v", err)
			}
			if len(claimed) != 1 || claimed[0].ID != bRowID || claimed[0].SchoolTaskID != taskB {
				t.Fatalf("claimed = %+v, want the pending sibling %s", claimed, bRowID)
			}
			if got := e.taskState(bRowID); got != string(corestatus.TaskRunning) {
				t.Fatalf("sibling row state = %q, want running", got)
			}
		})
	}
}

// TestClaimDueWithoutSuccessKeepsSingleRowPerAccount 钉住未动既有语义：
// 无 success 时同账号仍只领一条（DISTINCT ON），另一条留 pending。
func TestClaimDueWithoutSuccessKeepsSingleRowPerAccount(t *testing.T) {
	e := newEnv(t)
	_, accountID, taskA, taskB := seedSiblingTasks(t, e,
		"claim-no-success", "claim-no-success@example.com", "13800060003")
	at := singleSuccessAt()
	if _, err := e.sched.InsertPlan(e.ctx, []coreschedule.PlanRow{
		{AccountID: accountID, SchoolTaskID: taskA, Day: singleSuccessDay, RunAt: at},
		{AccountID: accountID, SchoolTaskID: taskB, Day: singleSuccessDay, RunAt: at},
	}); err != nil {
		t.Fatalf("insert plan: %v", err)
	}

	claimed, err := e.sched.ClaimDue(e.ctx, at.Add(time.Minute), singleSuccessDay, 10)
	if err != nil {
		t.Fatalf("claim due: %v", err)
	}
	if len(claimed) != 1 {
		t.Fatalf("claimed %d rows without any success, want 1", len(claimed))
	}
	if got := e.scalarInt("SELECT count(*) FROM sign_tasks WHERE account_id = $1::uuid AND day = $2::date AND state = 'pending'",
		accountID, singleSuccessDay); got != 1 {
		t.Fatalf("pending sibling rows = %d, want 1", got)
	}
}

// TestClaimDueSuccessScopeIsAccountAndDay 钉住排除条件的作用域：
// 别的账号的成功、别的业务日的成功都不能误伤本账号今日的 pending 行。
func TestClaimDueSuccessScopeIsAccountAndDay(t *testing.T) {
	t.Run("other-account", func(t *testing.T) {
		e := newEnv(t)
		schoolID, taskID, accountA := e.seedTask(
			"claim-scope-account", "晨间签到", "claim-scope-account@example.com", "13800060004")
		accountB := e.createAccount(schoolID,
			e.createUser("claim-scope-account-b@example.com").ID, "13800060005")
		e.activateAccount(accountB.ID)
		at := singleSuccessAt()
		if _, err := e.sched.InsertPlan(e.ctx, []coreschedule.PlanRow{
			{AccountID: accountA, SchoolTaskID: taskID, Day: singleSuccessDay, RunAt: at},
			{AccountID: accountB.ID, SchoolTaskID: taskID, Day: singleSuccessDay, RunAt: at},
		}); err != nil {
			t.Fatalf("insert plan: %v", err)
		}
		setSignTaskState(t, e, accountA, taskID, singleSuccessDay, corestatus.TaskSuccess)

		claimed, err := e.sched.ClaimDue(e.ctx, at.Add(time.Minute), singleSuccessDay, 10)
		if err != nil {
			t.Fatalf("claim due: %v", err)
		}
		if len(claimed) != 1 || claimed[0].AccountID != accountB.ID {
			t.Fatalf("claimed = %+v, want the other account's row", claimed)
		}
	})

	t.Run("other-day", func(t *testing.T) {
		e := newEnv(t)
		_, taskID, accountID := e.seedTask(
			"claim-scope-day", "晨间签到", "claim-scope-day@example.com", "13800060006")
		at := singleSuccessAt()
		yesterday := at.Add(-24 * time.Hour)
		if _, err := e.sched.InsertPlan(e.ctx, []coreschedule.PlanRow{
			{AccountID: accountID, SchoolTaskID: taskID, Day: yesterday.Format("2006-01-02"), RunAt: yesterday},
			{AccountID: accountID, SchoolTaskID: taskID, Day: singleSuccessDay, RunAt: at},
		}); err != nil {
			t.Fatalf("insert plan: %v", err)
		}
		setSignTaskState(t, e, accountID, taskID, yesterday.Format("2006-01-02"), corestatus.TaskSuccess)

		claimed, err := e.sched.ClaimDue(e.ctx, at.Add(time.Minute), singleSuccessDay, 10)
		if err != nil {
			t.Fatalf("claim due: %v", err)
		}
		if len(claimed) != 1 || claimed[0].Day != singleSuccessDay {
			t.Fatalf("claimed = %+v, want today's row despite yesterday's success", claimed)
		}
	})
}

// TestHasSuccessOnOnlyCountsSuccess 钉住仓储判据本身：
// 只有 success 算今日已签到；skipped/failed 不算，跨日/跨账号不算。
//
// 把 SQL 条件放宽成 state <> 'pending' 时 skipped 用例红；
// 丢掉 day 条件时 other-day 用例红；丢掉 account_id 时 other-account 红。
func TestHasSuccessOnOnlyCountsSuccess(t *testing.T) {
	e := newEnv(t)
	day := singleSuccessDay
	schoolID, taskID, accountID := e.seedTask(
		"has-success", "晨间签到", "has-success@example.com", "13800060007")
	at := singleSuccessAt()

	if ok, err := e.sched.HasSuccessOn(e.ctx, accountID, day); err != nil || ok {
		t.Fatalf("HasSuccessOn without rows = (%v,%v), want (false,nil)", ok, err)
	}

	if _, err := e.sched.InsertPlan(e.ctx, []coreschedule.PlanRow{
		{AccountID: accountID, SchoolTaskID: taskID, Day: day, RunAt: at},
	}); err != nil {
		t.Fatalf("insert plan: %v", err)
	}

	for _, tc := range []struct {
		state corestatus.TaskState
		want  bool
	}{
		{corestatus.TaskSkipped, false},
		{corestatus.TaskFailed, false},
		{corestatus.TaskRunning, false},
		{corestatus.TaskSuccess, true},
	} {
		t.Run(string(tc.state), func(t *testing.T) {
			setSignTaskState(t, e, accountID, taskID, day, tc.state)
			ok, err := e.sched.HasSuccessOn(e.ctx, accountID, day)
			if err != nil || ok != tc.want {
				t.Fatalf("HasSuccessOn(state=%s) = (%v,%v), want (%v,nil)", tc.state, ok, err, tc.want)
			}
		})
	}

	t.Run("other-day", func(t *testing.T) {
		ok, err := e.sched.HasSuccessOn(e.ctx, accountID, "2026-10-28")
		if err != nil || ok {
			t.Fatalf("HasSuccessOn(yesterday) = (%v,%v), want (false,nil)", ok, err)
		}
	})

	t.Run("other-account", func(t *testing.T) {
		sibling := e.createAccount(schoolID,
			e.createUser("has-success-other@example.com").ID, "13800060008")
		e.activateAccount(sibling.ID)
		ok, err := e.sched.HasSuccessOn(e.ctx, sibling.ID, day)
		if err != nil || ok {
			t.Fatalf("HasSuccessOn(other account) = (%v,%v), want (false,nil)", ok, err)
		}
	})
}
