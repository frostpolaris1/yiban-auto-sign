package pg_test

import (
	"errors"
	"sync"
	"testing"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/pg"
	coreschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/schedule"
	corestatus "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/status"
)

// 本文件是排期四表的 e2e 判据（P4-B）。
// 需要真实 PostgreSQL：CAS 单赢家、唯一约束与 ENUM 都没有诚实替身（AGENTS §32）。
// 无 DSN 时整套跳过，跳过显性可见（main_test.go 的 requireDSN）。

// seedTask 建学校＋任务＋账号，返回 (schoolID, taskID, accountID)。
func (e *testEnv) seedTask(code, taskName, ownerEmail, phone string) (string, string, string) {
	e.t.Helper()
	school := e.createSchool(code)
	taskID, err := e.sched.CreateSchoolTask(e.ctx, school.ID, taskName)
	if err != nil {
		e.t.Fatalf("create school task: %v", err)
	}
	acct := e.createAccount(school.ID, e.createUser(ownerEmail).ID, phone)
	e.activateAccount(acct.ID)
	return school.ID, taskID, acct.ID
}

// TestSchoolTaskIdempotent：同名任务幂等——两次建任务得同一 ID。
// 计划层按 (school, name) 幂等登记平台发现的任务，重复发现不得建重复行。
func TestSchoolTaskIdempotent(t *testing.T) {
	e := newEnv(t)
	schoolID, id1 := "", ""
	{
		school := e.createSchool("sched-t1")
		schoolID = school.ID
		var err error
		id1, err = e.sched.CreateSchoolTask(e.ctx, schoolID, "晚自习签到")
		if err != nil {
			t.Fatalf("create: %v", err)
		}
	}
	id2, err := e.sched.CreateSchoolTask(e.ctx, schoolID, "晚自习签到")
	if err != nil {
		t.Fatalf("second create: %v", err)
	}
	if id1 != id2 {
		t.Fatalf("同名任务建出两行: %s vs %s", id1, id2)
	}
}

// TestCASClaimSingleWinner：并发认领同一行，赢家恰一个（P4 验收 3）。
// 判据是「任何时刻该行只能被一个 worker 持有」，不是调用顺序。
func TestCASClaimSingleWinner(t *testing.T) {
	e := newEnv(t)
	_, taskID, acctID := e.seedTask("sched-cas", "晨间签到", "cas-owner@example.com", "13800001111")
	day := "2026-10-29"
	runAt := time.Date(2026, 10, 29, 6, 40, 0, 0, time.UTC)
	_, err := e.sched.InsertPlan(e.ctx, []coreschedule.PlanRow{
		{AccountID: acctID, SchoolTaskID: taskID, Day: day, RunAt: runAt},
	})
	if err != nil {
		t.Fatalf("insert plan: %v", err)
	}

	const workers = 8
	wins := make(chan struct{}, workers)
	var wg sync.WaitGroup
	for i := 0; i < workers; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			err := e.sched.ClaimPending(e.ctx, acctID, taskID, day, time.Now())
			if err == nil {
				wins <- struct{}{}
			}
		}()
	}
	wg.Wait()
	close(wins)
	n := 0
	for range wins {
		n++
	}
	if n != 1 {
		t.Fatalf("并发认领赢家 = %d, want 1（CAS 单赢家）", n)
	}
	// 落终态后，同日的第二认领也必须失败：一天一任务一行。
	if err := e.sched.Finish(e.ctx, acctID, taskID, day, "success", "ok", time.Now()); err != nil {
		t.Fatalf("finish: %v", err)
	}
	if err := e.sched.ClaimPending(e.ctx, acctID, taskID, day, time.Now()); !errors.Is(err, pg.ErrScheduleConflict) {
		t.Fatalf("终态行再认领应冲突, got %v", err)
	}
	if done, err := e.sched.CountDone(e.ctx, acctID, day); err != nil || done != 1 {
		t.Fatalf("CountDone = (%d,%v), want (1,nil)", done, err)
	}
}

// TestPlanIdempotentReplay：计划重放不覆盖——同输入二次落库写 0 行，
// 既有结论（终态）不被冲掉（AGENTS §14：不许静默改写当日结论）。
func TestPlanIdempotentReplay(t *testing.T) {
	e := newEnv(t)
	_, taskID, acctID := e.seedTask("sched-replay", "晨间签到", "replay-owner@example.com", "13800002222")
	day := "2026-10-29"
	rows := []coreschedule.PlanRow{
		{AccountID: acctID, SchoolTaskID: taskID, Day: day, RunAt: time.Date(2026, 10, 29, 6, 40, 0, 0, time.UTC)},
	}
	n, err := e.sched.InsertPlan(e.ctx, rows)
	if err != nil || n != 1 {
		t.Fatalf("first insert = (%d,%v), want (1,nil)", n, err)
	}
	if err := e.sched.ClaimPending(e.ctx, acctID, taskID, day, time.Now()); err != nil {
		t.Fatalf("claim: %v", err)
	}
	if err := e.sched.Finish(e.ctx, acctID, taskID, day, "success", "ok", time.Now()); err != nil {
		t.Fatalf("finish: %v", err)
	}
	// 重放同一批：0 行写入，终态保持。
	n, err = e.sched.InsertPlan(e.ctx, rows)
	if err != nil || n != 0 {
		t.Fatalf("replay = (%d,%v), want (0,nil)", n, err)
	}
	if done, _ := e.sched.CountDone(e.ctx, acctID, day); done != 1 {
		t.Fatalf("重放后终态被破坏: done=%d", done)
	}
}

// TestWindowVersionReplan：窗口版本重排——乐观锁只允许最新版本覆盖，
// 过期版本写不进（P4 计划：版本重排输入）。
func TestWindowVersionReplan(t *testing.T) {
	e := newEnv(t)
	_, taskID, _ := e.seedTask("sched-win", "晨间签到", "win-owner@example.com", "13800003333")
	day := "2026-10-29"
	w1 := coreschedule.WindowRecord{
		SchoolTaskID: taskID, Day: day,
		Start:  time.Date(2026, 10, 29, 6, 30, 0, 0, time.UTC),
		End:    time.Date(2026, 10, 29, 7, 50, 0, 0, time.UTC),
		Source: "platform",
	}
	v1, err := e.sched.PutWindow(e.ctx, w1, 0)
	if err != nil || v1 != 1 {
		t.Fatalf("first put = (%d,%v), want (1,nil)", v1, err)
	}
	// 过期版本（拿 v1 当 expected）在 v2 写入后再试，必须 stale。
	v2, err := e.sched.PutWindow(e.ctx, w1, v1)
	if err != nil || v2 != 2 {
		t.Fatalf("second put = (%d,%v), want (2,nil)", v2, err)
	}
	if _, err := e.sched.PutWindow(e.ctx, w1, v1); !errors.Is(err, pg.ErrWindowStale) {
		t.Fatalf("过期版本应 stale, got %v", err)
	}
	got, err := e.sched.GetWindow(e.ctx, taskID, day)
	if err != nil || got.Version != 2 {
		t.Fatalf("get = (%+v,%v), want version 2", got, err)
	}
}

// TestOnlyOneRunningPerAccount：库级门 I-7——同账号同时至多一条 running
// （P4 验收 3「同账号不双登录」，P4-B 复审 P1-1）。
// 进程内串行挡不住崩溃重启；门必须在库层。两条任务各自 pending，
// 认领第一条成功后，第二条必须被部分唯一索引拒绝。
func TestOnlyOneRunningPerAccount(t *testing.T) {
	e := newEnv(t)
	school := e.createSchool("sched-i7")
	taskA, err := e.sched.CreateSchoolTask(e.ctx, school.ID, "任务A")
	if err != nil {
		t.Fatalf("create taskA: %v", err)
	}
	taskB, err := e.sched.CreateSchoolTask(e.ctx, school.ID, "任务B")
	if err != nil {
		t.Fatalf("create taskB: %v", err)
	}
	acct := e.createAccount(school.ID, e.createUser("i7-owner@example.com").ID, "13800004444")
	e.activateAccount(acct.ID)
	day := "2026-10-29"
	runAt := time.Date(2026, 10, 29, 6, 40, 0, 0, time.UTC)
	if _, err := e.sched.InsertPlan(e.ctx, []coreschedule.PlanRow{
		{AccountID: acct.ID, SchoolTaskID: taskA, Day: day, RunAt: runAt},
		{AccountID: acct.ID, SchoolTaskID: taskB, Day: day, RunAt: runAt},
	}); err != nil {
		t.Fatalf("insert plan: %v", err)
	}
	if err := e.sched.ClaimPending(e.ctx, acct.ID, taskA, day, time.Now()); err != nil {
		t.Fatalf("claim taskA: %v", err)
	}
	// taskB 同账号同时认领：撞 uq_sign_tasks_one_running，必须 conflict。
	if err := e.sched.ClaimPending(e.ctx, acct.ID, taskB, day, time.Now()); !errors.Is(err, pg.ErrScheduleConflict) {
		t.Fatalf("同账号第二条 running 应冲突, got %v", err)
	}
	// taskA 落终态后 taskB 可认领：门只锁"同时"，不锁"当日先后"。
	if err := e.sched.Finish(e.ctx, acct.ID, taskA, day, corestatus.TaskSuccess, "ok", time.Now()); err != nil {
		t.Fatalf("finish taskA: %v", err)
	}
	if err := e.sched.ClaimPending(e.ctx, acct.ID, taskB, day, time.Now()); err != nil {
		t.Fatalf("taskB 在 taskA 终态后应可认领: %v", err)
	}
}

// TestSignTaskStateEnumMatchesStatus：库层 ENUM 与 core/status 五常量同形
// （P4-B 复审 P2-4 的门）。漂移即红：词表在库与代码两处定义，必须钉住。
func TestSignTaskStateEnumMatchesStatus(t *testing.T) {
	e := newEnv(t)
	want := []string{
		string(corestatus.TaskPending),
		string(corestatus.TaskRunning),
		string(corestatus.TaskSuccess),
		string(corestatus.TaskFailed),
		string(corestatus.TaskSkipped),
	}
	// DB 层无多行查询助手（PG e2e 全走单行标量）：按序聚合成逗号串比对。
	joined := ""
	for i, w := range want {
		if i > 0 {
			joined += ","
		}
		joined += w
	}
	got, err := e.db.QueryString(e.ctx,
		`SELECT string_agg(enumlabel, ',' ORDER BY enumsortorder)
		 FROM pg_enum e
		 JOIN pg_type t ON t.oid = e.enumtypid
		 WHERE t.typname = 'sign_task_state'`)
	if err != nil {
		t.Fatalf("query pg_enum: %v", err)
	}
	if got != joined {
		t.Fatalf("ENUM 词表 = %q, want %q（库与 status 词表漂移）", got, joined)
	}
}
