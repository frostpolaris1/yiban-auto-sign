package pg_test

// 自选拥挤度按选中任务过滤（P2-13）。
//
// 判据：同校两个任务各自只有自己的账号计入；没有该任务计划行的账号
// 不计入。旧实现按全校统计，两个窗口不同的任务会互相混算。

import (
	"testing"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/pg"
	coreschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/schedule"
)

func TestOffsetCountsScopesToTaskAndDay(t *testing.T) {
	e := newEnv(t)
	school := e.createSchool("stats-offset")
	taskA, err := e.sched.CreateSchoolTask(e.ctx, school.ID, "任务A")
	if err != nil {
		t.Fatalf("task A: %v", err)
	}
	taskB, err := e.sched.CreateSchoolTask(e.ctx, school.ID, "任务B")
	if err != nil {
		t.Fatalf("task B: %v", err)
	}
	userA := e.createUser("offset-a@example.com")
	acctA := e.createAccount(school.ID, userA.ID, "13800088881")
	e.activateAccount(acctA.ID)
	userB := e.createUser("offset-b@example.com")
	acctB := e.createAccount(school.ID, userB.ID, "13800088882")
	e.activateAccount(acctB.ID)
	// userC 有偏好但没有当日计划行：不得计入任何任务。
	userC := e.createUser("offset-c@example.com")
	acctC := e.createAccount(school.ID, userC.ID, "13800088883")
	e.activateAccount(acctC.ID)

	day := "2026-10-29"
	at := time.Date(2026, 10, 29, 6, 40, 0, 0, time.UTC)
	if _, err := e.sched.InsertPlan(e.ctx, []coreschedule.PlanRow{
		{AccountID: acctA.ID, SchoolTaskID: taskA, Day: day, RunAt: at},
		{AccountID: acctB.ID, SchoolTaskID: taskB, Day: day, RunAt: at},
	}); err != nil {
		t.Fatalf("insert plan: %v", err)
	}
	prefs := pg.NewTimePrefRepo(e.db)
	now := time.Date(2026, 10, 29, 0, 0, 0, 0, time.UTC)
	zero, five := 0, 5
	for _, pref := range []struct {
		user string
		off  *int
	}{
		{userA.ID, &zero},
		{userB.ID, &zero},
		{userC.ID, &five},
	} {
		if _, err := prefs.PutWithin(e.ctx, pref.user, pref.off, now, 0); err != nil {
			t.Fatalf("put pref for %s: %v", pref.user, err)
		}
	}

	stats := pg.NewStatsRepo(e.db)
	countsA, err := stats.OffsetCounts(e.ctx, school.ID, taskA, day)
	if err != nil {
		t.Fatalf("counts A: %v", err)
	}
	if len(countsA) != 1 || countsA[0] != 1 {
		t.Fatalf("task A counts = %v, want {0:1}（只含 A 的账号）", countsA)
	}
	countsB, err := stats.OffsetCounts(e.ctx, school.ID, taskB, day)
	if err != nil {
		t.Fatalf("counts B: %v", err)
	}
	if len(countsB) != 1 || countsB[0] != 1 {
		t.Fatalf("task B counts = %v, want {0:1}（只含 B 的账号）", countsB)
	}
	// 另一个业务日：没有计划行，计数为空。
	countsOther, err := stats.OffsetCounts(e.ctx, school.ID, taskA, "2026-10-30")
	if err != nil {
		t.Fatalf("counts other day: %v", err)
	}
	if len(countsOther) != 0 {
		t.Fatalf("other day counts = %v, want empty", countsOther)
	}
}
