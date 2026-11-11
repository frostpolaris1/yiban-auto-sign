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
