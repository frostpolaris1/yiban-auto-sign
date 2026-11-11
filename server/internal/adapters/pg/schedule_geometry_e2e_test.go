package pg_test

// PendingPlan 与名册几何重排的真库判据（M3）。
//
// 判据一：PendingPlan 只返回 state='pending' 的行；把 SQL 的
// `state = 'pending'` 改成 `state = 'running'` 时，本用例会拿到 running 行
// 或漏掉 pending 行而红。
// 判据二：名册变化（更小手机号账号加入）后重建，真实 run_at 必须重排到
// 新几何，而不只是行数不变。

import (
	"encoding/json"
	"testing"
	"time"

	appschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/schedule"
	coreschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/schedule"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/contract"
)

func TestPendingPlanFiltersByState(t *testing.T) {
	e := newEnv(t)
	school := e.createSchool("sched-pending")
	taskID, err := e.sched.CreateSchoolTask(e.ctx, school.ID, "晨间签到")
	if err != nil {
		t.Fatalf("create task: %v", err)
	}
	owner := e.createUser("pending-owner@example.com")
	pendingAcct := e.createAccount(school.ID, owner.ID, "13800099101")
	runningAcct := e.createAccount(school.ID, owner.ID, "13800099102")
	for _, id := range []string{pendingAcct.ID, runningAcct.ID} {
		if _, err := e.db.Exec(e.ctx,
			"UPDATE accounts SET status = 'active' WHERE id = $1::uuid", id); err != nil {
			t.Fatalf("activate account: %v", err)
		}
	}
	day := "2026-10-29"
	at := time.Date(2026, 10, 29, 6, 40, 0, 0, time.UTC)
	if _, err := e.sched.InsertPlan(e.ctx, []coreschedule.PlanRow{
		{AccountID: pendingAcct.ID, SchoolTaskID: taskID, Day: day, RunAt: at},
		{AccountID: runningAcct.ID, SchoolTaskID: taskID, Day: day, RunAt: at},
	}); err != nil {
		t.Fatalf("insert plan: %v", err)
	}
	if _, err := e.sched.ClaimPending(e.ctx, runningAcct.ID, taskID, day, at); err != nil {
		t.Fatalf("claim running row: %v", err)
	}

	rows, err := e.sched.PendingPlan(e.ctx, taskID, day)
	if err != nil {
		t.Fatalf("PendingPlan: %v", err)
	}
	if len(rows) != 1 || rows[0].AccountID != pendingAcct.ID {
		t.Fatalf("PendingPlan = %+v, want only the pending account %s", rows, pendingAcct.ID)
	}
}

// geometryRow 是 json_agg 解出的行形状。
type geometryRow struct {
	AccountID string    `json:"account_id"`
	RunAt     time.Time `json:"run_at"`
}

func TestBuildRosterChangeRearrangesRealGeometry(t *testing.T) {
	e := newEnv(t)
	school := e.createSchool("sched-geometry")
	taskID, err := e.sched.CreateSchoolTask(e.ctx, school.ID, "晨间签到")
	if err != nil {
		t.Fatalf("create task: %v", err)
	}
	owner := e.createUser("geometry-owner@example.com")
	acctB := e.createAccount(school.ID, owner.ID, "13800099201")
	acctC := e.createAccount(school.ID, owner.ID, "13800099202")
	for _, id := range []string{acctB.ID, acctC.ID} {
		if _, err := e.db.Exec(e.ctx,
			"UPDATE accounts SET status = 'active' WHERE id = $1::uuid", id); err != nil {
			t.Fatalf("activate account: %v", err)
		}
	}

	loc := time.FixedZone("CST", 8*3600)
	day := "2026-10-29"
	start := time.Date(2026, 10, 29, 6, 30, 0, 0, loc)
	end := start.Add(80 * time.Minute)
	svc, err := appschedule.NewService(appschedule.Deps{
		Windows:  e.sched,
		Plans:    e.sched,
		Accounts: e.sched,
		Tx:       e.db,
		Discover: fixedWindowDiscoverer{window: contract.Window{
			Start: start, End: end, Source: contract.WindowPlatform,
		}},
	})
	if err != nil {
		t.Fatalf("new schedule service: %v", err)
	}
	in := appschedule.Input{
		SchoolID: school.ID, SchoolTaskID: taskID, Day: day, Location: loc,
		Workers: 1, Avg: time.Second, Rate: 1, Util: 1.0,
		SlotsPerSlice: 1,
	}
	if _, err := svc.Build(e.ctx, in); err != nil {
		t.Fatalf("first build: %v", err)
	}

	// 更小手机号账号加入：名册整体前移，旧行必须重排。
	acctA := e.createAccount(school.ID, owner.ID, "13800099200")
	if _, err := e.db.Exec(e.ctx,
		"UPDATE accounts SET status = 'active' WHERE id = $1::uuid", acctA.ID); err != nil {
		t.Fatalf("activate new account: %v", err)
	}
	if _, err := svc.Build(e.ctx, in); err != nil {
		t.Fatalf("rebuild: %v", err)
	}

	raw := e.scalarString(`SELECT json_agg(json_build_object(
		'account_id', account_id::text, 'run_at', run_at) ORDER BY run_at)::text
		FROM sign_tasks WHERE school_task_id = $1::uuid AND day = $2::date`, taskID, day)
	var rows []geometryRow
	if err := json.Unmarshal([]byte(raw), &rows); err != nil {
		t.Fatalf("decode rows: %v", err)
	}
	want := []struct {
		account string
		runAt   time.Time
	}{
		{acctA.ID, start},
		{acctB.ID, start.Add(5 * time.Minute)},
		{acctC.ID, start.Add(10 * time.Minute)},
	}
	if len(rows) != len(want) {
		t.Fatalf("rows = %d, want %d: %+v", len(rows), len(want), rows)
	}
	for i, w := range want {
		if rows[i].AccountID != w.account || !rows[i].RunAt.Equal(w.runAt) {
			t.Fatalf("row %d = %s@%v, want %s@%v（真实几何未重排）",
				i, rows[i].AccountID, rows[i].RunAt, w.account, w.runAt)
		}
	}
}
