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
	// current_schema 过滤：测试库并发跑多个 schema 时，
	// 只聚合本 schema 的枚举（否则跨 schema 词表拼接、偶发红）。
	got, err := e.db.QueryString(e.ctx,
		`SELECT string_agg(e.enumlabel, ',' ORDER BY e.enumsortorder)
		 FROM pg_enum e
		 JOIN pg_type t ON t.oid = e.enumtypid
		 JOIN pg_namespace n ON n.oid = t.typnamespace
		 WHERE t.typname = 'sign_task_state' AND n.nspname = current_schema()`)
	if err != nil {
		t.Fatalf("query pg_enum: %v", err)
	}
	if got != joined {
		t.Fatalf("ENUM 词表 = %q, want %q（库与 status 词表漂移）", got, joined)
	}
}

// TestRearrangeOnlyPending：重排只动 pending；running 与终态行的 run_at 保持。
// 判据：计划版本变化不得改写当日已发生的执行事实。
func TestRearrangeOnlyPending(t *testing.T) {
	e := newEnv(t)
	_, taskID, acctA := e.seedTask("sched-rearr", "晨间签到", "rearr-a@example.com", "13800005555")
	acctB := e.createAccount(e.scalarSchool(taskID), e.createUser("rearr-b@example.com").ID, "13800006666")
	e.activateAccount(acctB.ID)
	day := "2026-10-29"
	oldAt := time.Date(2026, 10, 29, 6, 40, 0, 0, time.UTC)
	newAt := time.Date(2026, 10, 29, 7, 10, 0, 0, time.UTC)
	if _, err := e.sched.InsertPlan(e.ctx, []coreschedule.PlanRow{
		{AccountID: acctA, SchoolTaskID: taskID, Day: day, RunAt: oldAt},
		{AccountID: acctB.ID, SchoolTaskID: taskID, Day: day, RunAt: oldAt},
	}); err != nil {
		t.Fatalf("insert: %v", err)
	}
	// B 认领后进入 running：重排不得动它。
	if err := e.sched.ClaimPending(e.ctx, acctB.ID, taskID, day, time.Now()); err != nil {
		t.Fatalf("claim B: %v", err)
	}
	rows := []coreschedule.PlanRow{
		{AccountID: acctA, SchoolTaskID: taskID, Day: day, RunAt: newAt},
		{AccountID: acctB.ID, SchoolTaskID: taskID, Day: day, RunAt: newAt},
	}
	n, err := e.sched.RearrangePlan(e.ctx, taskID, day, rows)
	if err != nil {
		t.Fatalf("rearrange: %v", err)
	}
	if n != 1 {
		t.Fatalf("重排行数 = %d, want 1（只 pending）", n)
	}
	if got := e.scalarInt("SELECT (run_at = $4::timestamptz)::int FROM sign_tasks WHERE account_id = $1::uuid AND school_task_id = $2::uuid AND day = $3", acctA, taskID, day, newAt); got != 1 {
		t.Fatalf("pending 行 run_at 未更新")
	}
	if got := e.scalarInt("SELECT (run_at = $4::timestamptz)::int FROM sign_tasks WHERE account_id = $1::uuid AND school_task_id = $2::uuid AND day = $3", acctB.ID, taskID, day, oldAt); got != 1 {
		t.Fatalf("running 行被改写")
	}
}

// scalarSchool 反查任务所属学校。
func (e *testEnv) scalarSchool(taskID string) string {
	e.t.Helper()
	return e.scalarString("SELECT school_id::text FROM school_tasks WHERE id = $1::uuid", taskID)
}

// TestActiveAccountIDsStableAndFiltered：只取活跃未删账号，按手机号稳定排序。
func TestActiveAccountIDsStableAndFiltered(t *testing.T) {
	e := newEnv(t)
	school := e.createSchool("sched-active")
	taskID, err := e.sched.CreateSchoolTask(e.ctx, school.ID, "晨间签到")
	if err != nil {
		t.Fatalf("task: %v", err)
	}
	a := e.createAccount(school.ID, e.createUser("act-a@example.com").ID, "13800007777")
	b := e.createAccount(school.ID, e.createUser("act-b@example.com").ID, "13800008888")
	e.activateAccount(a.ID)
	e.activateAccount(b.ID)
	_ = taskID
	// 第三个账号保持 pending：不得出现在活跃面。
	pending := e.createAccount(school.ID, e.createUser("act-c@example.com").ID, "13800009999")
	// 第四个暂停、第五个软删：同样不得出现。
	paused := e.createAccount(school.ID, e.createUser("act-d@example.com").ID, "13800010001")
	e.activateAccount(paused.ID)
	if _, err := e.db.Exec(e.ctx, "UPDATE accounts SET status = 'paused_admin' WHERE id = $1::uuid", paused.ID); err != nil {
		t.Fatalf("pause: %v", err)
	}
	deleted := e.createAccount(school.ID, e.createUser("act-e@example.com").ID, "13800010002")
	e.activateAccount(deleted.ID)
	if _, err := e.db.Exec(e.ctx, "UPDATE accounts SET deleted_at = now() WHERE id = $1::uuid", deleted.ID); err != nil {
		t.Fatalf("soft delete: %v", err)
	}
	ids, err := e.sched.ActiveAccountIDs(e.ctx, school.ID)
	if err != nil {
		t.Fatalf("active: %v", err)
	}
	if len(ids) != 2 {
		t.Fatalf("活跃账号数 = %d, want 2: %v", len(ids), ids)
	}
	for _, id := range ids {
		if id == pending.ID || id == paused.ID || id == deleted.ID {
			t.Fatalf("不活跃账号出现在活跃面: %s", id)
		}
	}
	// 顺序按手机号：7777 在 8888 前。
	if ids[0] != a.ID || ids[1] != b.ID {
		t.Fatalf("顺序不稳定: %v", ids)
	}
}
