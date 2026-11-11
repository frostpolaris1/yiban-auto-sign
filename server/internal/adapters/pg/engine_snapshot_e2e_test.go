package pg_test

// 执行监控快照的跨日与租约判据（P1-6/2-15、P3-22）。
//
// 判据：
//   - Inflight 只含租约未过期的 running 行（过期租约不是在飞）；
//   - DueNow / Retrying 只算 day >= 今天的行（跨日行不混算）。

import (
	"testing"
	"time"

	coreschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/schedule"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
)

func TestEngineSnapshotExcludesCrossDayAndExpiredLease(t *testing.T) {
	e := newEnv(t)
	schoolID, taskID, staleAccount := e.seedTask(
		"sched-engine", "晨间签到", "engine-stale@example.com", "13800066661")
	dueAccount := e.createAccount(schoolID,
		e.createUser("engine-due@example.com").ID, "13800066662")
	e.activateAccount(dueAccount.ID)
	expiredAccount := e.createAccount(schoolID,
		e.createUser("engine-expired@example.com").ID, "13800066663")
	e.activateAccount(expiredAccount.ID)
	liveAccount := e.createAccount(schoolID,
		e.createUser("engine-live@example.com").ID, "13800066664")
	e.activateAccount(liveAccount.ID)

	yesterday := "2026-10-28"
	today := "2026-10-29"
	now := time.Date(2026, 10, 29, 7, 0, 0, 0, time.UTC)
	due := time.Date(2026, 10, 29, 6, 40, 0, 0, time.UTC)
	if _, err := e.sched.InsertPlan(e.ctx, []coreschedule.PlanRow{
		{AccountID: staleAccount, SchoolTaskID: taskID, Day: yesterday, RunAt: due.Add(-24 * time.Hour)},
		{AccountID: dueAccount.ID, SchoolTaskID: taskID, Day: today, RunAt: due},
		{AccountID: expiredAccount.ID, SchoolTaskID: taskID, Day: today, RunAt: due},
		{AccountID: liveAccount.ID, SchoolTaskID: taskID, Day: today, RunAt: due},
	}); err != nil {
		t.Fatalf("insert plan: %v", err)
	}
	if _, err := e.db.Exec(e.ctx,
		"UPDATE sign_tasks SET attempts = 1 WHERE account_id = $1::uuid AND day = $2::date",
		dueAccount.ID, today); err != nil {
		t.Fatalf("set attempts: %v", err)
	}
	if _, err := e.sched.ClaimPending(e.ctx, expiredAccount.ID, taskID, today, now); err != nil {
		t.Fatalf("claim expired account: %v", err)
	}
	if _, err := e.sched.ClaimPending(e.ctx, liveAccount.ID, taskID, today, now); err != nil {
		t.Fatalf("claim live account: %v", err)
	}
	if _, err := e.db.Exec(e.ctx,
		`UPDATE sign_tasks SET lease_expires_at = $2
		  WHERE account_id = $1::uuid AND day = $3::date`,
		expiredAccount.ID, now.Add(-time.Minute), today); err != nil {
		t.Fatalf("expire lease: %v", err)
	}

	data, err := e.sched.EngineSnapshot(e.ctx, corescope.Master(), today, now)
	if err != nil {
		t.Fatalf("EngineSnapshot: %v", err)
	}
	if len(data.Inflight) != 1 || data.Inflight[0].AccountID != liveAccount.ID {
		t.Fatalf("Inflight = %+v, want only the live-lease account（过期租约与跨日行都不算）", data.Inflight)
	}
	if data.DueNow != 1 {
		t.Fatalf("DueNow = %d, want 1（只算当日到期行）", data.DueNow)
	}
	if len(data.Retrying) != 1 || data.Retrying[0].AccountID != dueAccount.ID {
		t.Fatalf("Retrying = %+v, want only the today row", data.Retrying)
	}
}
