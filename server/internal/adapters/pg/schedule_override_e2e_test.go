package pg_test

// 人工覆盖与计划物化的原子性（P2-10）。
//
// 判据：注入 InsertPlan 失败 → 覆盖行、窗口缓存与计划行都不落库。
// 旧实现先写覆盖再重建，重建失败会留下「覆盖已写、计划未建」的半态。

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/pg"
	appschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/schedule"
	coreschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/schedule"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
)

// failingPlans 包装真实排期仓储，按开关让 InsertPlan 失败。
type failingPlans struct {
	*pg.ScheduleRepo
	fail bool
}

func (f failingPlans) InsertPlan(ctx context.Context, rows []coreschedule.PlanRow) (int64, error) {
	if f.fail {
		return 0, errors.New("insert plan failed")
	}
	return f.ScheduleRepo.InsertPlan(ctx, rows)
}

func TestOverrideRollsBackWhenPlanWriteFails(t *testing.T) {
	e := newEnv(t)
	school := e.createSchool("sched-override")
	taskID, err := e.sched.CreateSchoolTask(e.ctx, school.ID, "晨间签到")
	if err != nil {
		t.Fatalf("create school task: %v", err)
	}
	owner := e.createUser("override-owner@example.com")
	acct := e.createAccount(school.ID, owner.ID, "13800077771")
	if _, err := e.db.Exec(e.ctx,
		"UPDATE accounts SET status = 'active' WHERE id = $1::uuid", acct.ID); err != nil {
		t.Fatalf("activate account: %v", err)
	}
	actor := e.createUser("override-actor@example.com")

	loc := time.FixedZone("CST", 8*3600)
	day := "2026-10-29"
	svc, err := appschedule.NewService(appschedule.Deps{
		Windows:  e.sched,
		Plans:    failingPlans{ScheduleRepo: e.sched, fail: true},
		Accounts: e.sched,
		Tx:       e.db,
		Discover: fixedWindowDiscoverer{},
	})
	if err != nil {
		t.Fatalf("new schedule service: %v", err)
	}
	in := appschedule.Input{
		SchoolID: school.ID, SchoolTaskID: taskID, Day: day, Location: loc,
		Workers: 4, Avg: 3 * time.Second, Rate: 1, Util: 0.8,
		SlotsPerSlice: 10,
	}
	start := time.Date(2026, 10, 29, 10, 0, 0, 0, loc)
	end := time.Date(2026, 10, 29, 11, 0, 0, 0, loc)
	if _, err := svc.Override(e.ctx, corescope.Master(), in, start, end, "调整", actor.ID); err == nil {
		t.Fatal("InsertPlan 失败必须上抛")
	}

	if got := e.scalarInt(`SELECT count(*) FROM window_overrides
		WHERE school_task_id = $1::uuid AND day = $2::date`, taskID, day); got != 0 {
		t.Fatalf("覆盖行 = %d, want 0（必须随事务回滚）", got)
	}
	if got := e.scalarInt(`SELECT count(*) FROM window_cache
		WHERE school_task_id = $1::uuid AND day = $2::date`, taskID, day); got != 0 {
		t.Fatalf("窗口缓存 = %d, want 0（必须随事务回滚）", got)
	}
	if got := e.scalarInt(`SELECT count(*) FROM sign_tasks
		WHERE school_task_id = $1::uuid AND day = $2::date`, taskID, day); got != 0 {
		t.Fatalf("计划行 = %d, want 0", got)
	}
}
