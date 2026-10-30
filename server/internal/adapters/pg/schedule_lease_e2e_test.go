package pg_test

// 执行租约的 e2e：认领写租约、过期回收、启动回收与 advisory lock。
// 依赖真实 PostgreSQL；租约与回收判据都在库侧执行。

import (
	"errors"
	"testing"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/pg"
	coreschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/schedule"
	corestatus "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/status"
)

// seedClaimable 建一条到期、可认领的计划行并返回行 id。
func seedClaimable(t *testing.T, e *testEnv, code string) (string, time.Time) {
	t.Helper()
	_, taskID, accountID := e.seedTask(code, "租约任务", code+"@example.com", "13800033333")
	day := "2026-10-29"
	due := time.Date(2026, 10, 29, 6, 40, 0, 0, time.UTC)
	if _, err := e.sched.InsertPlan(e.ctx, []coreschedule.PlanRow{
		{AccountID: accountID, SchoolTaskID: taskID, Day: day, RunAt: due},
	}); err != nil {
		t.Fatalf("insert plan: %v", err)
	}
	return e.taskRowID(accountID, taskID, day), due
}

// 认领写入租约：lease_expires_at = claimed_at + 默认 2 分钟。
func TestClaimDueSetsLease(t *testing.T) {
	e := newEnv(t)
	rowID, due := seedClaimable(t, e, "lease-set")
	now := due.Add(time.Minute)
	if _, err := e.sched.ClaimDue(e.ctx, now, 10); err != nil {
		t.Fatalf("claim: %v", err)
	}
	if got := e.scalarInt(
		"SELECT (lease_expires_at = claimed_at + interval '2 minutes')::int FROM sign_tasks WHERE id = $1::uuid",
		rowID); got != 1 {
		t.Fatal("lease_expires_at 未写入 claimed_at + 2 分钟")
	}
}

// 过期租约被回收：state 回 pending、租约清空、落点改为回收时刻，并写 task.reclaim 审计。
func TestReclaimExpiredRequeues(t *testing.T) {
	e := newEnv(t)
	rowID, due := seedClaimable(t, e, "lease-reclaim")
	now := due.Add(time.Minute)
	if _, err := e.sched.ClaimDue(e.ctx, now, 10); err != nil {
		t.Fatalf("claim: %v", err)
	}
	if _, err := e.db.Exec(e.ctx,
		`UPDATE sign_tasks SET lease_expires_at = $2 WHERE id = $1::uuid`,
		rowID, now.Add(-time.Minute)); err != nil {
		t.Fatalf("expire lease: %v", err)
	}
	reclaimer := pg.NewScheduleRepoWithAudit(e.db, e.audits)
	reclaimed, err := reclaimer.ReclaimExpired(e.ctx, now, 10)
	if err != nil {
		t.Fatalf("reclaim: %v", err)
	}
	if reclaimed != 1 {
		t.Fatalf("reclaimed = %d, want 1", reclaimed)
	}
	if got := e.taskState(rowID); got != string(corestatus.TaskPending) {
		t.Fatalf("state = %q, want pending", got)
	}
	if got := e.scalarInt(
		"SELECT (lease_expires_at IS NULL AND claimed_at IS NULL AND run_at = $2::timestamptz)::int FROM sign_tasks WHERE id = $1::uuid",
		rowID, now); got != 1 {
		t.Fatal("回收后租约/认领时刻未清空或落点未改为回收时刻")
	}
	if got := e.scalarInt(
		"SELECT count(*) FROM audit_logs WHERE action = 'task.reclaim' AND target_id = $1",
		rowID); got != 1 {
		t.Fatalf("task.reclaim 审计 = %d, want 1", got)
	}
}

// 未到期的租约不回收。
func TestReclaimExpiredSkipsLiveLease(t *testing.T) {
	e := newEnv(t)
	_, due := seedClaimable(t, e, "lease-live")
	now := due.Add(time.Minute)
	if _, err := e.sched.ClaimDue(e.ctx, now, 10); err != nil {
		t.Fatalf("claim: %v", err)
	}
	reclaimer := pg.NewScheduleRepoWithAudit(e.db, e.audits)
	reclaimed, err := reclaimer.ReclaimExpired(e.ctx, now, 10)
	if err != nil {
		t.Fatalf("reclaim: %v", err)
	}
	if reclaimed != 0 {
		t.Fatalf("reclaimed = %d, want 0（租约未到期）", reclaimed)
	}
}

// 启动回收清空租约并放回 pending。
func TestResetRunningClearsLease(t *testing.T) {
	e := newEnv(t)
	rowID, due := seedClaimable(t, e, "lease-reset")
	if _, err := e.sched.ClaimDue(e.ctx, due.Add(time.Minute), 10); err != nil {
		t.Fatalf("claim: %v", err)
	}
	n, err := e.sched.ResetRunning(e.ctx)
	if err != nil {
		t.Fatalf("reset: %v", err)
	}
	if n != 1 {
		t.Fatalf("reset = %d, want 1", n)
	}
	if got := e.scalarInt(
		"SELECT (state = 'pending' AND lease_expires_at IS NULL AND claimed_at IS NULL)::int FROM sign_tasks WHERE id = $1::uuid",
		rowID); got != 1 {
		t.Fatal("启动回收后未回 pending 或未清空租约")
	}
}

// 另一个会话持有回收锁时，启动回收直接返回 0，不改行。
func TestResetRunningRespectsAdvisoryLock(t *testing.T) {
	e := newEnv(t)
	rowID, due := seedClaimable(t, e, "lease-lock")
	if _, err := e.sched.ClaimDue(e.ctx, due.Add(time.Minute), 10); err != nil {
		t.Fatalf("claim: %v", err)
	}
	locker, err := pg.Open(e.ctx, pg.Options{DSN: e.dsn, MaxConns: 1, StatementTimeout: 10 * time.Second})
	if err != nil {
		t.Fatalf("open locker: %v", err)
	}
	defer locker.Close()
	if _, err := locker.Exec(e.ctx, "SELECT pg_advisory_lock($1)", pg.ResetRunningLockKey); err != nil {
		t.Fatalf("acquire lock: %v", err)
	}
	if n, err := e.sched.ResetRunning(e.ctx); err != nil || n != 0 {
		t.Fatalf("locked reset = (%d, %v), want (0, nil)", n, err)
	}
	if got := e.taskState(rowID); got != string(corestatus.TaskRunning) {
		t.Fatalf("锁内不得改行：state = %q", got)
	}
	if _, err := locker.Exec(e.ctx, "SELECT pg_advisory_unlock($1)", pg.ResetRunningLockKey); err != nil {
		t.Fatalf("release lock: %v", err)
	}
	if n, err := e.sched.ResetRunning(e.ctx); err != nil || n != 1 {
		t.Fatalf("unlocked reset = (%d, %v), want (1, nil)", n, err)
	}
}

// 认领 CAS：终态行不可能被外层 UPDATE 改回 running。
func TestClaimDueCASKeepsTerminalRows(t *testing.T) {
	e := newEnv(t)
	rowID, due := seedClaimable(t, e, "lease-cas")
	if _, err := e.db.Exec(e.ctx,
		`UPDATE sign_tasks SET state = $2::sign_task_state, result = $2 WHERE id = $1::uuid`,
		rowID, string(corestatus.TaskSuccess)); err != nil {
		t.Fatalf("seed terminal: %v", err)
	}
	claimed, err := e.sched.ClaimDue(e.ctx, due.Add(time.Minute), 10)
	if err != nil {
		t.Fatalf("claim: %v", err)
	}
	if len(claimed) != 0 {
		t.Fatalf("claimed %d terminal rows, want 0", len(claimed))
	}
	if got := e.taskState(rowID); got != string(corestatus.TaskSuccess) {
		t.Fatalf("terminal row state = %q, want success", got)
	}
}

// 双连接并发认领同一行：只有一个 goroutine 拿到。
func TestConcurrentClaimsExecuteOnce(t *testing.T) {
	e := newEnv(t)
	_, due := seedClaimable(t, e, "lease-race")
	now := due.Add(time.Minute)
	type result struct {
		n   int
		err error
	}
	results := make(chan result, 2)
	for i := 0; i < 2; i++ {
		go func() {
			claimed, err := e.sched.ClaimDue(e.ctx, now, 10)
			results <- result{n: len(claimed), err: err}
		}()
	}
	total := 0
	for i := 0; i < 2; i++ {
		r := <-results
		if r.err != nil {
			t.Fatalf("claim: %v", r.err)
		}
		total += r.n
	}
	if total != 1 {
		t.Fatalf("two claims got %d rows, want 1", total)
	}
}

// 昨天的 running 过期后不再阻塞今天：回收后账号可再次被认领。
func TestReclaimUnblocksNextDay(t *testing.T) {
	e := newEnv(t)
	_, taskID, accountID := e.seedTask("lease-cross-day", "跨天任务", "lease-cross-day@example.com", "13800044444")
	yesterday := "2026-10-28"
	today := "2026-10-29"
	dueYesterday := time.Date(2026, 10, 28, 6, 40, 0, 0, time.UTC)
	dueToday := time.Date(2026, 10, 29, 6, 40, 0, 0, time.UTC)
	if _, err := e.sched.InsertPlan(e.ctx, []coreschedule.PlanRow{
		{AccountID: accountID, SchoolTaskID: taskID, Day: yesterday, RunAt: dueYesterday},
		{AccountID: accountID, SchoolTaskID: taskID, Day: today, RunAt: dueToday},
	}); err != nil {
		t.Fatalf("insert plan: %v", err)
	}
	// 昨天那行卡在 running，租约已过期。
	if _, err := e.db.Exec(e.ctx, `
		UPDATE sign_tasks SET state = 'running', claimed_at = $3::timestamptz,
		       lease_expires_at = $3::timestamptz
		 WHERE account_id = $1::uuid AND school_task_id = $2::uuid AND day = $4`,
		accountID, taskID, dueToday.Add(-time.Hour), yesterday); err != nil {
		t.Fatalf("seed stale running: %v", err)
	}
	now := dueToday.Add(time.Minute)
	if claimed, err := e.sched.ClaimDue(e.ctx, now, 10); err != nil {
		t.Fatalf("claim before reclaim: %v", err)
	} else if len(claimed) != 0 {
		t.Fatalf("claimed %d rows while a stale running row blocks the account", len(claimed))
	}
	reclaimer := pg.NewScheduleRepoWithAudit(e.db, e.audits)
	if n, err := reclaimer.ReclaimExpired(e.ctx, now, 10); err != nil || n != 1 {
		t.Fatalf("reclaim = (%d, %v), want (1, nil)", n, err)
	}
	claimed, err := e.sched.ClaimDue(e.ctx, now, 10)
	if err != nil {
		t.Fatalf("claim after reclaim: %v", err)
	}
	if len(claimed) != 1 {
		t.Fatalf("claimed %d rows after reclaim, want 1", len(claimed))
	}
}

// 装配的租约时长生效：WithLease(45s) 写 claimed_at + 45 秒。
func TestClaimDueUsesConfiguredLease(t *testing.T) {
	e := newEnv(t)
	rowID, due := seedClaimable(t, e, "lease-config")
	now := due.Add(time.Minute)
	if _, err := e.sched.WithLease(45*time.Second).ClaimDue(e.ctx, now, 10); err != nil {
		t.Fatalf("claim: %v", err)
	}
	if got := e.scalarInt(
		"SELECT (lease_expires_at = claimed_at + interval '45 seconds')::int FROM sign_tasks WHERE id = $1::uuid",
		rowID); got != 1 {
		t.Fatal("WithLease(45s) 未生效")
	}
}

// 未到期的租约不被启动回收清掉：滚动重启不得打断同伴在飞行。
func TestResetRunningKeepsLiveLease(t *testing.T) {
	e := newEnv(t)
	rowID, due := seedClaimable(t, e, "lease-live-reset")
	if _, err := e.sched.ClaimDue(e.ctx, due.Add(time.Minute), 10); err != nil {
		t.Fatalf("claim: %v", err)
	}
	if _, err := e.db.Exec(e.ctx,
		`UPDATE sign_tasks SET lease_expires_at = now() + interval '1 hour' WHERE id = $1::uuid`,
		rowID); err != nil {
		t.Fatalf("extend lease: %v", err)
	}
	if n, err := e.sched.ResetRunning(e.ctx); err != nil || n != 0 {
		t.Fatalf("reset = (%d, %v), want (0, nil)", n, err)
	}
	if got := e.taskState(rowID); got != string(corestatus.TaskRunning) {
		t.Fatalf("live lease row state = %q, want running", got)
	}
}

// 过期持有者的结算必须 0 行；新持有者的租约不得被清。
func TestStaleClaimCannotSettleNewLease(t *testing.T) {
	e := newEnv(t)
	rowID, due := seedClaimable(t, e, "lease-fence")
	now := due.Add(time.Minute)
	first, err := e.sched.ClaimDue(e.ctx, now, 10)
	if err != nil || len(first) != 1 {
		t.Fatalf("first claim = (%d, %v)", len(first), err)
	}
	if _, err := e.db.Exec(e.ctx,
		`UPDATE sign_tasks SET lease_expires_at = $2 WHERE id = $1::uuid`,
		rowID, now.Add(-time.Minute)); err != nil {
		t.Fatalf("expire lease: %v", err)
	}
	reclaimer := pg.NewScheduleRepoWithAudit(e.db, e.audits)
	if n, err := reclaimer.ReclaimExpired(e.ctx, now, 10); err != nil || n != 1 {
		t.Fatalf("reclaim = (%d, %v), want (1, nil)", n, err)
	}
	second, err := e.sched.ClaimDue(e.ctx, now, 10)
	if err != nil || len(second) != 1 {
		t.Fatalf("second claim = (%d, %v)", len(second), err)
	}
	if second[0].ClaimSeq != first[0].ClaimSeq+1 {
		t.Fatalf("claim_seq = %d -> %d, want +1", first[0].ClaimSeq, second[0].ClaimSeq)
	}
	accountID := e.scalarString("SELECT account_id::text FROM sign_tasks WHERE id = $1::uuid", rowID)
	taskID := e.scalarString("SELECT school_task_id::text FROM sign_tasks WHERE id = $1::uuid", rowID)
	day := e.scalarString("SELECT day::text FROM sign_tasks WHERE id = $1::uuid", rowID)
	// 旧持有者用旧序号结算：必须冲突。
	err = e.sched.Finish(e.ctx, accountID, taskID, day, corestatus.TaskSuccess, "stale_owner", first[0].ClaimSeq, now)
	if !errors.Is(err, pg.ErrScheduleConflict) {
		t.Fatalf("stale finish = %v, want ErrScheduleConflict", err)
	}
	if got := e.taskState(rowID); got != string(corestatus.TaskRunning) {
		t.Fatalf("stale finish changed the state: %q", got)
	}
	if got := e.scalarInt(
		"SELECT (lease_expires_at IS NOT NULL)::int FROM sign_tasks WHERE id = $1::uuid", rowID); got != 1 {
		t.Fatal("stale finish cleared the new holder's lease")
	}
	// 新持有者用当前序号结算：成功。
	if err := e.sched.Finish(e.ctx, accountID, taskID, day, corestatus.TaskSuccess, "new_owner", second[0].ClaimSeq, now); err != nil {
		t.Fatalf("new finish: %v", err)
	}
	if got := e.taskState(rowID); got != string(corestatus.TaskSuccess) {
		t.Fatalf("state = %q, want success", got)
	}
}
