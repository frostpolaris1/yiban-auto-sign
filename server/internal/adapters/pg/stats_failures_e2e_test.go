package pg_test

// 失败归因库面聚合 StatsRepo.FailureCounts 的真库 e2e。
//
// 判据：
//   - 按 sign_tasks 任务行计数，不做账号去重；
//   - 只统计指定业务日 state='failed' 行，排除 pending/running/success/skipped、跨日与软删账号；
//   - result 原值保留（空串与词表外取值都不改写，归 unknown 归 app 层）；
//   - SQL 强制带 scopePredicate：admin 仅看绑定校，空绑定返空集，user 范围拒绝对话。

import (
	"testing"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/pg"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	coreschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/schedule"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
	corestatus "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/status"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/contract"
)

func TestStatsRepoFailureCountsScopeAndRows(t *testing.T) {
	e := newEnv(t)
	stats := pg.NewStatsRepo(e.db)

	schoolA, taskA1, acctA1 := e.seedTask(
		"stats-fail-a", "晨间签到", "stats-fail-a1@example.com", "13800020001")
	taskA2, err := e.sched.CreateSchoolTask(e.ctx, schoolA, "晚间签到")
	if err != nil {
		t.Fatalf("create task A2: %v", err)
	}
	acctA2 := e.createAccount(schoolA, e.createUser("stats-fail-a2@example.com").ID, "13800020004").ID
	acctA3 := e.createAccount(schoolA, e.createUser("stats-fail-a3@example.com").ID, "13800020005").ID
	schoolB, taskB1, acctB1 := e.seedTask(
		"stats-fail-b", "B校签到", "stats-fail-b1@example.com", "13800020002")
	acctDeleted := e.createAccount(schoolA, e.createUser("stats-fail-del@example.com").ID, "13800020003").ID
	if _, err := e.db.Exec(e.ctx,
		`UPDATE accounts SET deleted_at = now() WHERE id = $1::uuid`, acctDeleted); err != nil {
		t.Fatalf("soft-delete account: %v", err)
	}

	const day = "2026-11-16"
	const otherDay = "2026-11-15"
	at, err := time.Parse("2006-01-02", day)
	if err != nil {
		t.Fatalf("parse day: %v", err)
	}
	if _, err := e.sched.InsertPlan(e.ctx, []coreschedule.PlanRow{
		{AccountID: acctA1, SchoolTaskID: taskA1, Day: day, RunAt: at.Add(6 * time.Hour)},
		{AccountID: acctA1, SchoolTaskID: taskA2, Day: day, RunAt: at.Add(18 * time.Hour)},
		{AccountID: acctA2, SchoolTaskID: taskA1, Day: day, RunAt: at.Add(6*time.Hour + 10*time.Minute)},
		{AccountID: acctA2, SchoolTaskID: taskA2, Day: day, RunAt: at.Add(18*time.Hour + 10*time.Minute)},
		{AccountID: acctA3, SchoolTaskID: taskA1, Day: day, RunAt: at.Add(6*time.Hour + 20*time.Minute)},
		{AccountID: acctA3, SchoolTaskID: taskA2, Day: day, RunAt: at.Add(18*time.Hour + 20*time.Minute)},
		{AccountID: acctDeleted, SchoolTaskID: taskA1, Day: day, RunAt: at.Add(7 * time.Hour)},
		{AccountID: acctB1, SchoolTaskID: taskB1, Day: day, RunAt: at.Add(8 * time.Hour)},
		{AccountID: acctA1, SchoolTaskID: taskA1, Day: otherDay, RunAt: at.Add(-18 * time.Hour)},
	}); err != nil {
		t.Fatalf("insert plan: %v", err)
	}

	setTaskOutcome(t, e, acctA1, taskA1, day, corestatus.TaskFailed, string(contract.OutcomeCredentialBad))
	setTaskOutcome(t, e, acctA1, taskA2, day, corestatus.TaskFailed, "")
	// 非 failed 状态（含带 result 的 pending/skipped/success 与 running）不得进入失败分布。
	setTaskOutcome(t, e, acctA2, taskA1, day, corestatus.TaskPending, string(contract.OutcomeTransient))
	setTaskOutcome(t, e, acctA2, taskA2, day, corestatus.TaskRunning, "")
	setTaskOutcome(t, e, acctA3, taskA1, day, corestatus.TaskSuccess, string(contract.OutcomeSuccess))
	setTaskOutcome(t, e, acctA3, taskA2, day, corestatus.TaskSkipped, string(contract.OutcomeNoTarget))
	setTaskOutcome(t, e, acctDeleted, taskA1, day, corestatus.TaskFailed, string(contract.OutcomeRejected))
	setTaskOutcome(t, e, acctB1, taskB1, day, corestatus.TaskFailed, "custom_drift")
	setTaskOutcome(t, e, acctA1, taskA1, otherDay, corestatus.TaskFailed, string(contract.OutcomeRejected))

	// master 全量：A 校两行（同一账号）+ B 校 custom_drift；空串原样保留。
	masterRows, err := stats.FailureCounts(e.ctx, corescope.Master(), coreschedule.SignFailureFilter{Day: day})
	if err != nil {
		t.Fatalf("FailureCounts(master): %v", err)
	}
	gotMaster := map[string]int{}
	for _, r := range masterRows {
		gotMaster[r.Result] += r.Count
	}
	if len(gotMaster) != 3 || gotMaster["credential_invalid"] != 1 || gotMaster[""] != 1 || gotMaster["custom_drift"] != 1 {
		t.Fatalf("master counts = %v, want credential_invalid=1, ''=1, custom_drift=1", gotMaster)
	}

	// admin(A)：只看 A 校，排除 B 校。
	adminRows, err := stats.FailureCounts(e.ctx, corescope.Admin([]string{schoolA}), coreschedule.SignFailureFilter{Day: day})
	if err != nil {
		t.Fatalf("FailureCounts(admin A): %v", err)
	}
	gotAdmin := map[string]int{}
	for _, r := range adminRows {
		gotAdmin[r.Result] += r.Count
	}
	if len(gotAdmin) != 2 || gotAdmin["credential_invalid"] != 1 || gotAdmin[""] != 1 {
		t.Fatalf("adminA counts = %v, want credential_invalid=1, ''=1", gotAdmin)
	}

	// admin 空绑定：必须返回空集，不得退化成全量。
	emptyAdmin, err := stats.FailureCounts(e.ctx, corescope.Admin(nil), coreschedule.SignFailureFilter{Day: day})
	if err != nil {
		t.Fatalf("FailureCounts(empty admin): %v", err)
	}
	if len(emptyAdmin) != 0 {
		t.Fatalf("unbound admin counts = %v, want empty", emptyAdmin)
	}

	// 即使调用方传入了 B 校 school_id，admin(A) 的 SQL 作用域条件仍拦截 B 校行。
	crossRows, err := stats.FailureCounts(e.ctx, corescope.Admin([]string{schoolA}), coreschedule.SignFailureFilter{
		Day: day, SchoolID: schoolB,
	})
	if err != nil {
		t.Fatalf("FailureCounts(admin A + school B): %v", err)
	}
	if len(crossRows) != 0 {
		t.Fatalf("cross school sql leak = %v, want empty", crossRows)
	}

	// 按 task_id 过滤。
	taskRows, err := stats.FailureCounts(e.ctx, corescope.Master(), coreschedule.SignFailureFilter{
		Day: day, SchoolID: schoolA, TaskID: taskA2,
	})
	if err != nil {
		t.Fatalf("FailureCounts(taskA2): %v", err)
	}
	if len(taskRows) != 1 || taskRows[0].Result != "" || taskRows[0].Count != 1 {
		t.Fatalf("taskA2 counts = %v, want [{\"\" 1}]", taskRows)
	}

	// user 作用域在适配器层同样被拒绝。
	if _, err := stats.FailureCounts(e.ctx, corescope.User("01900000-0000-7000-8000-000000000001"), coreschedule.SignFailureFilter{Day: day}); err == nil {
		t.Fatal("user scope should be rejected")
	}

	// SchoolExists 与 TaskSchoolID 判据。
	exists, err := stats.SchoolExists(e.ctx, schoolA)
	if err != nil || !exists {
		t.Fatalf("SchoolExists(A) = (%v, %v), want (true, nil)", exists, err)
	}
	exists, err = stats.SchoolExists(e.ctx, "01900000-0000-7000-8000-000000000999")
	if err != nil || exists {
		t.Fatalf("SchoolExists(missing) = (%v, %v), want (false, nil)", exists, err)
	}
	gotSchool, err := stats.TaskSchoolID(e.ctx, taskA1)
	if err != nil || gotSchool != schoolA {
		t.Fatalf("TaskSchoolID(taskA1) = (%q, %v), want %q", gotSchool, err, schoolA)
	}
	if _, err := stats.TaskSchoolID(e.ctx, "01900000-0000-7000-8000-000000000999"); errs.From(err).Code != errs.CodeNotFound {
		t.Fatalf("TaskSchoolID(missing) err = %v, want not_found", err)
	}
}
