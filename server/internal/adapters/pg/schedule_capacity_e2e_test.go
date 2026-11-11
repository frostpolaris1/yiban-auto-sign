package pg_test

// 容量缩容的端到端判据（P0-1）。
//
// 判据：先按容量 80 建 64 行，再把容量降到 32 重建——库里 pending 恰 32 行，
// 另 32 行落 skipped(capacity_released)，ClaimDue 只领到 32 行。
// 这是「缩容后仍全量签」缺陷的回归门：只测 helper 不够，必须打真实
// Build → 事务 → 认领的整条接线。

import (
	"context"
	"fmt"
	"testing"
	"time"

	appschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/schedule"
	coreschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/schedule"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/contract"
)

// fixedWindowDiscoverer 是计划构建的平台窗口替身：固定窗口，零网络。
type fixedWindowDiscoverer struct {
	window contract.Window
}

func (d fixedWindowDiscoverer) DiscoverWindow(context.Context, string) (contract.Window, error) {
	return d.window, nil
}

func TestBuildCapacityShrinkReleasesExcessRows(t *testing.T) {
	e := newEnv(t)
	school := e.createSchool("sched-shrink")
	taskID, err := e.sched.CreateSchoolTask(e.ctx, school.ID, "晨间签到")
	if err != nil {
		t.Fatalf("create school task: %v", err)
	}
	const demand = 64
	for i := 0; i < demand; i++ {
		owner := e.createUser(fmt.Sprintf("shrink-%d@example.com", i))
		acct := e.createAccount(school.ID, owner.ID, fmt.Sprintf("138%08d", i))
		if _, err := e.db.Exec(e.ctx,
			"UPDATE accounts SET status = 'active' WHERE id = $1::uuid", acct.ID); err != nil {
			t.Fatalf("activate account %d: %v", i, err)
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
		Workers: 1, Avg: 60 * time.Second, Rate: 1, Util: 1.0,
		SlotsPerSlice: 10,
	}
	if _, err := svc.Build(e.ctx, in); err != nil {
		t.Fatalf("first build: %v", err)
	}
	if got := e.scalarInt(`SELECT count(*) FROM sign_tasks
		WHERE school_task_id = $1::uuid AND day = $2::date AND state = 'pending'`,
		taskID, day); got != demand {
		t.Fatalf("first build pending = %d, want %d", got, demand)
	}

	in.Util = 0.4 // 容量 32
	if _, err := svc.Build(e.ctx, in); err != nil {
		t.Fatalf("shrink build: %v", err)
	}
	if got := e.scalarInt(`SELECT count(*) FROM sign_tasks
		WHERE school_task_id = $1::uuid AND day = $2::date AND state = 'pending'`,
		taskID, day); got != 32 {
		t.Fatalf("after shrink pending = %d, want 32", got)
	}
	if got := e.scalarInt(`SELECT count(*) FROM sign_tasks
		WHERE school_task_id = $1::uuid AND day = $2::date
		  AND state = 'skipped' AND result = 'capacity_released'`,
		taskID, day); got != 32 {
		t.Fatalf("capacity_released rows = %d, want 32", got)
	}

	claimed, err := e.sched.ClaimDue(e.ctx, end.Add(time.Minute), day, 100)
	if err != nil {
		t.Fatalf("claim due: %v", err)
	}
	if len(claimed) != 32 {
		t.Fatalf("claimed = %d, want 32（超容量行不得再被认领）", len(claimed))
	}
}

// 容量回升只复活 capacity_released，且复活行 result 清空（N1/N3）。
// 把 ReviveCapacityReleased 的 `AND result = 'capacity_released'` 删掉时，
// window_closed 行会被误复活，本用例红。
func TestBuildCapacityRecoveryRevivesOnlyCapacityReleased(t *testing.T) {
	e := newEnv(t)
	school := e.createSchool("sched-revive")
	taskID, err := e.sched.CreateSchoolTask(e.ctx, school.ID, "晨间签到")
	if err != nil {
		t.Fatalf("create school task: %v", err)
	}
	const demand = 64
	for i := 0; i < demand; i++ {
		owner := e.createUser(fmt.Sprintf("revive-%d@example.com", i))
		acct := e.createAccount(school.ID, owner.ID, fmt.Sprintf("138%08d", 10000100+i))
		if _, err := e.db.Exec(e.ctx,
			"UPDATE accounts SET status = 'active' WHERE id = $1::uuid", acct.ID); err != nil {
			t.Fatalf("activate account %d: %v", i, err)
		}
	}
	windowOwner := e.createUser("revive-window@example.com")
	windowAcct := e.createAccount(school.ID, windowOwner.ID, "13800099999")
	if _, err := e.db.Exec(e.ctx,
		"UPDATE accounts SET status = 'active' WHERE id = $1::uuid", windowAcct.ID); err != nil {
		t.Fatalf("activate window account: %v", err)
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
		Workers: 1, Avg: 60 * time.Second, Rate: 1, Util: 1.0,
		SlotsPerSlice: 10,
	}
	if _, err := svc.Build(e.ctx, in); err != nil {
		t.Fatalf("first build: %v", err)
	}
	in.Util = 0.4 // 容量 32：32 行被释放
	if _, err := svc.Build(e.ctx, in); err != nil {
		t.Fatalf("shrink build: %v", err)
	}

	// 造一条 window_closed：先插行、落结论，再停用账号，避免重建把它当需求。
	if _, err := e.sched.InsertPlan(e.ctx, []coreschedule.PlanRow{{
		AccountID: windowAcct.ID, SchoolTaskID: taskID, Day: day, RunAt: start,
	}}); err != nil {
		t.Fatalf("insert window row: %v", err)
	}
	if _, err := e.sched.SkipPlanRows(e.ctx, taskID, day, []string{windowAcct.ID}, "window_closed"); err != nil {
		t.Fatalf("skip window row: %v", err)
	}
	if _, err := e.db.Exec(e.ctx,
		"UPDATE accounts SET status = 'paused_admin' WHERE id = $1::uuid", windowAcct.ID); err != nil {
		t.Fatalf("pause window account: %v", err)
	}

	in.Util = 1.0 // 容量回到 80：32 行复活
	if _, err := svc.Build(e.ctx, in); err != nil {
		t.Fatalf("recovery build: %v", err)
	}

	if got := e.scalarInt(`SELECT count(*) FROM sign_tasks
		WHERE school_task_id = $1::uuid AND day = $2::date
		  AND state = 'pending' AND result = ''`, taskID, day); got != demand {
		t.Fatalf("pending with empty result = %d, want %d（复活行 result 必须清空）", got, demand)
	}
	if got := e.scalarInt(`SELECT count(*) FROM sign_tasks
		WHERE school_task_id = $1::uuid AND day = $2::date
		  AND state = 'pending' AND result = 'capacity_released'`, taskID, day); got != 0 {
		t.Fatalf("pending with capacity_released = %d, want 0", got)
	}
	if got := e.scalarInt(`SELECT count(*) FROM sign_tasks
		WHERE school_task_id = $1::uuid AND day = $2::date
		  AND state = 'skipped' AND result = 'window_closed'`, taskID, day); got != 1 {
		t.Fatalf("window_closed rows = %d, want 1（不得被回升误复活）", got)
	}
}
