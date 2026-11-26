package pg_test

// 执行监控快照的 e2e 判据。
//
// 语义契约（P1-6/2-15、P3-22、P2-10）：
//   - Inflight 只含租约未过期的 running 行（过期租约不是在飞）；
//   - DueNow / Retrying 只算 day >= 今天的行（跨日行不混算）；
//   - 在飞/到期/终态/待重试都按 scope 过滤：admin 空绑定是空快照，
//     绝不退化成全量；user scope 在仓储层被拒；
//   - 当日终态每账号只认最后一条任务行（updated_at DESC, id DESC），
//     软删账号整段排除；
//   - 待重试按 run_at, id 升序，行数截断于 EngineRetryLimit 并显式上报；
//   - 在飞按 claimed_at, id 升序。
//
// P2-10 把四段读合并成两次往返（一次 QueryRow 计数、一次 Query 行），
// 上述判据在合并后必须逐条成立。

import (
	"context"
	"fmt"
	"strings"
	"testing"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
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

// enginePhone 造合法手机号；测试 schema 独立，只需校内不撞
// uq_accounts_school_phone_live（phone 形如 ^1[0-9]{10}$）。
func enginePhone(n int) string { return fmt.Sprintf("138%08d", n) }

// engineTask 在指定学校登记一条任务，返回任务 ID。
func (e *testEnv) engineTask(schoolID, name string) string {
	e.t.Helper()
	id, err := e.sched.CreateSchoolTask(e.ctx, schoolID, name)
	if err != nil {
		e.t.Fatalf("create school task %q: %v", name, err)
	}
	return id
}

// engineNewAccount 建一个已审核账号（认领需要 active），返回账号 ID。
func (e *testEnv) engineNewAccount(schoolID string, n int) string {
	e.t.Helper()
	acct := e.createAccount(schoolID,
		e.createUser(fmt.Sprintf("engine-%d-%s@example.com", n, randomHex(4))).ID, enginePhone(n))
	e.activateAccount(acct.ID)
	return acct.ID
}

// enginePlan 为账号在 (任务, 业务日) 上排一行。
func (e *testEnv) enginePlan(accountID, taskID, day string, runAt time.Time) {
	e.t.Helper()
	if _, err := e.sched.InsertPlan(e.ctx, []coreschedule.PlanRow{{
		AccountID: accountID, SchoolTaskID: taskID, Day: day, RunAt: runAt,
	}}); err != nil {
		e.t.Fatalf("insert plan: %v", err)
	}
}

// engineExec 直接改种子行（state/attempts/claimed_at/lease/deleted_at）。
func (e *testEnv) engineExec(sql string, args ...any) {
	e.t.Helper()
	if _, err := e.db.Exec(e.ctx, sql, args...); err != nil {
		e.t.Fatalf("exec %q: %v", sql, err)
	}
}

// engineSnapshot 读快照；失败即终止用例。
func (e *testEnv) engineSnapshot(scope corescope.Scope, day string, now time.Time) coreschedule.EngineData {
	e.t.Helper()
	data, err := e.sched.EngineSnapshot(e.ctx, scope, day, now)
	if err != nil {
		e.t.Fatalf("EngineSnapshot(%v): %v", scope.Kind(), err)
	}
	return data
}

// TestEngineSnapshotAdminScopeIsolation：两校各有在飞/到期/终态/待重试，
// admin 只绑定 A 时只见 A；绑定空集时四者全零/空（绝不退化成全量）；
// user scope 在仓储层被拒。
func TestEngineSnapshotAdminScopeIsolation(t *testing.T) {
	e := newEnv(t)
	day := "2026-10-29"
	now := time.Date(2026, 10, 29, 7, 0, 0, 0, time.UTC)
	dueAt := now.Add(-10 * time.Minute)
	retryAt := now.Add(10 * time.Minute)

	schoolA := e.createSchool("engine-scope-a")
	schoolB := e.createSchool("engine-scope-b")
	taskA := e.engineTask(schoolA.ID, "晨间签到")
	taskB := e.engineTask(schoolB.ID, "晨间签到")

	// 校 A：四类各一行。
	inflightA := e.engineNewAccount(schoolA.ID, 1)
	e.enginePlan(inflightA, taskA, day, dueAt)
	if _, err := e.sched.ClaimPending(e.ctx, inflightA, taskA, day, now); err != nil {
		t.Fatalf("claim A inflight: %v", err)
	}
	dueA := e.engineNewAccount(schoolA.ID, 2)
	e.enginePlan(dueA, taskA, day, dueAt)
	successA := e.engineNewAccount(schoolA.ID, 3)
	e.enginePlan(successA, taskA, day, dueAt)
	e.engineExec(`UPDATE sign_tasks SET state = 'success'
	  WHERE account_id = $1::uuid AND school_task_id = $2::uuid AND day = $3::date`,
		successA, taskA, day)
	retryA := e.engineNewAccount(schoolA.ID, 4)
	e.enginePlan(retryA, taskA, day, retryAt)
	e.engineExec(`UPDATE sign_tasks SET attempts = 1
	  WHERE account_id = $1::uuid AND school_task_id = $2::uuid AND day = $3::date`,
		retryA, taskA, day)

	// 校 B：每类两行。A 的读数若退化成全量，会被这些行撑大。
	for i := 0; i < 2; i++ {
		acct := e.engineNewAccount(schoolB.ID, 10+i)
		e.enginePlan(acct, taskB, day, dueAt)
		if _, err := e.sched.ClaimPending(e.ctx, acct, taskB, day, now); err != nil {
			t.Fatalf("claim B inflight: %v", err)
		}
	}
	for i := 0; i < 2; i++ {
		e.enginePlan(e.engineNewAccount(schoolB.ID, 12+i), taskB, day, dueAt)
	}
	for i := 0; i < 2; i++ {
		acct := e.engineNewAccount(schoolB.ID, 14+i)
		e.enginePlan(acct, taskB, day, dueAt)
		e.engineExec(`UPDATE sign_tasks SET state = 'success'
		  WHERE account_id = $1::uuid AND school_task_id = $2::uuid AND day = $3::date`,
			acct, taskB, day)
	}
	for i := 0; i < 2; i++ {
		acct := e.engineNewAccount(schoolB.ID, 16+i)
		e.enginePlan(acct, taskB, day, dueAt)
		e.engineExec(`UPDATE sign_tasks SET state = 'failed'
		  WHERE account_id = $1::uuid AND school_task_id = $2::uuid AND day = $3::date`,
			acct, taskB, day)
	}
	for i := 0; i < 2; i++ {
		acct := e.engineNewAccount(schoolB.ID, 18+i)
		e.enginePlan(acct, taskB, day, retryAt)
		e.engineExec(`UPDATE sign_tasks SET attempts = 1
		  WHERE account_id = $1::uuid AND school_task_id = $2::uuid AND day = $3::date`,
			acct, taskB, day)
	}

	// master 先确认种行真的生效，否则 A 的断言可能空对空。
	master := e.engineSnapshot(corescope.Master(), day, now)
	if len(master.Inflight) != 3 || master.DueNow != 3 ||
		master.TodaySuccess != 3 || master.TodayFailed != 2 || len(master.Retrying) != 3 {
		t.Fatalf("master snapshot = inflight %d, due %d, success %d, failed %d, retry %d; "+
			"want 3/3/3/2/3（种子不生效时 A 的隔离断言没有意义）",
			len(master.Inflight), master.DueNow, master.TodaySuccess, master.TodayFailed, len(master.Retrying))
	}

	// admin 只绑定 A：四类都只见 A 的那一行。
	dataA := e.engineSnapshot(corescope.Admin([]string{schoolA.ID}), day, now)
	if len(dataA.Inflight) != 1 || dataA.Inflight[0].AccountID != inflightA ||
		dataA.Inflight[0].SchoolID != schoolA.ID || dataA.Inflight[0].SchoolName != "School engine-scope-a" {
		t.Fatalf("admin A Inflight = %+v, want only the A row", dataA.Inflight)
	}
	if dataA.DueNow != 1 {
		t.Fatalf("admin A DueNow = %d, want 1", dataA.DueNow)
	}
	if dataA.TodaySuccess != 1 || dataA.TodayFailed != 0 {
		t.Fatalf("admin A terminal = %d success / %d failed, want 1/0",
			dataA.TodaySuccess, dataA.TodayFailed)
	}
	if len(dataA.Retrying) != 1 || dataA.Retrying[0].AccountID != retryA {
		t.Fatalf("admin A Retrying = %+v, want only the A row", dataA.Retrying)
	}
	if dataA.RetryingTruncated {
		t.Fatal("admin A RetryingTruncated = true, want false")
	}

	// 绑定空集：空快照，绝不当全量。
	empty := e.engineSnapshot(corescope.Admin(nil), day, now)
	if len(empty.Inflight) != 0 || empty.DueNow != 0 ||
		empty.TodaySuccess != 0 || empty.TodayFailed != 0 ||
		len(empty.Retrying) != 0 || empty.RetryingTruncated {
		t.Fatalf("admin with no schools = inflight %d, due %d, success %d, failed %d, retry %d, truncated %v; want all zero/empty",
			len(empty.Inflight), empty.DueNow, empty.TodaySuccess, empty.TodayFailed,
			len(empty.Retrying), empty.RetryingTruncated)
	}

	// user scope 不属于执行监控。
	user := e.createUser("engine-scope-user@example.com")
	_, err := e.sched.EngineSnapshot(e.ctx, corescope.User(user.ID), day, now)
	if code := codeOf(t, err); code != errs.CodeValidation {
		t.Fatalf("user scope code = %v, want %v", code, errs.CodeValidation)
	}
}

// TestEngineSnapshotTerminalDailyDistinctOn：同账号同日多行只认
// (updated_at DESC, id DESC) 的最后一行；软删账号整段排除。
func TestEngineSnapshotTerminalDailyDistinctOn(t *testing.T) {
	e := newEnv(t)
	day := "2026-10-29"
	now := time.Date(2026, 10, 29, 7, 0, 0, 0, time.UTC)
	school := e.createSchool("engine-terminal")
	task1 := e.engineTask(school.ID, "晨间签到")
	task2 := e.engineTask(school.ID, "晚自习签到")
	setState := func(accountID, taskID, state string) {
		e.t.Helper()
		e.engineExec(`UPDATE sign_tasks SET state = $4
		  WHERE account_id = $1::uuid AND school_task_id = $2::uuid AND day = $3::date`,
			accountID, taskID, day, state)
	}

	// X：最后更新的是 failed 行 → 只计 failed。
	x := e.engineNewAccount(school.ID, 1)
	e.enginePlan(x, task1, day, now.Add(-2*time.Hour))
	e.enginePlan(x, task2, day, now.Add(-time.Hour))
	setState(x, task1, "success")
	time.Sleep(2 * time.Millisecond)
	setState(x, task2, "failed")

	// Y：最后更新的是 success 行 → 只计 success。
	y := e.engineNewAccount(school.ID, 2)
	e.enginePlan(y, task1, day, now.Add(-2*time.Hour))
	e.enginePlan(y, task2, day, now.Add(-time.Hour))
	setState(y, task1, "failed")
	time.Sleep(2 * time.Millisecond)
	setState(y, task2, "success")

	// Z：软删账号，最后一行是 success，整段排除。
	z := e.engineNewAccount(school.ID, 3)
	e.enginePlan(z, task1, day, now.Add(-time.Hour))
	setState(z, task1, "success")
	e.engineExec("UPDATE accounts SET deleted_at = now() WHERE id = $1::uuid", z)

	// W：两行在同一事务内更新，updated_at 相同；tie-break 取 id DESC。
	// 显式让较大 id 行 success，id ASC 的实现会把它数成 failed。
	w := e.engineNewAccount(school.ID, 4)
	e.enginePlan(w, task1, day, now.Add(-2*time.Hour))
	e.enginePlan(w, task2, day, now.Add(-time.Hour))
	idHi := e.scalarString(`SELECT id::text FROM sign_tasks
		WHERE account_id = $1::uuid AND day = $2::date ORDER BY id DESC LIMIT 1`, w, day)
	idLo := e.scalarString(`SELECT id::text FROM sign_tasks
		WHERE account_id = $1::uuid AND day = $2::date ORDER BY id ASC LIMIT 1`, w, day)
	if err := e.db.InTx(e.ctx, func(ctx context.Context) error {
		if _, err := e.db.Exec(ctx,
			"UPDATE sign_tasks SET state = 'success' WHERE id = $1::uuid", idHi); err != nil {
			return err
		}
		_, err := e.db.Exec(ctx,
			"UPDATE sign_tasks SET state = 'failed' WHERE id = $1::uuid", idLo)
		return err
	}); err != nil {
		t.Fatalf("update W terminal states in one tx: %v", err)
	}

	data := e.engineSnapshot(corescope.Master(), day, now)
	if data.TodaySuccess != 2 || data.TodayFailed != 1 {
		t.Fatalf("terminal = %d success / %d failed, want 2/1（X 计 failed、Y 与 W 计 success、软删 Z 不计）",
			data.TodaySuccess, data.TodayFailed)
	}
}

// TestEngineSnapshotRetryCap：待重试按 run_at, id 截断于 EngineRetryLimit，
// 多取的一行只用于上报截断；删到上限后不再报截断。
func TestEngineSnapshotRetryCap(t *testing.T) {
	e := newEnv(t)
	day := "2026-10-29"
	now := time.Date(2026, 10, 29, 7, 0, 0, 0, time.UTC)
	school := e.createSchool("engine-cap")
	acct := e.engineNewAccount(school.ID, 1)
	base := now.Add(time.Hour)
	total := coreschedule.EngineRetryLimit + 2
	for i := 0; i < total; i++ {
		taskID := e.engineTask(school.ID, fmt.Sprintf("任务-%03d", i))
		e.enginePlan(acct, taskID, day, base.Add(time.Duration(i)*time.Minute))
	}
	e.engineExec("UPDATE sign_tasks SET attempts = 1 WHERE account_id = $1::uuid AND day = $2::date", acct, day)

	wantIDs := strings.Split(e.scalarString(`SELECT string_agg(id::text, ',' ORDER BY run_at, id)
		FROM sign_tasks WHERE account_id = $1::uuid AND day = $2::date`, acct, day), ",")
	if len(wantIDs) != total {
		t.Fatalf("seeded rows = %d, want %d", len(wantIDs), total)
	}

	data := e.engineSnapshot(corescope.Master(), day, now)
	if len(data.Retrying) != coreschedule.EngineRetryLimit {
		t.Fatalf("Retrying = %d rows, want cap %d", len(data.Retrying), coreschedule.EngineRetryLimit)
	}
	if !data.RetryingTruncated {
		t.Fatal("RetryingTruncated = false, want true（超限必须显式上报）")
	}
	for i, row := range data.Retrying {
		if row.TaskID != wantIDs[i] {
			t.Fatalf("Retrying[%d].TaskID = %s, want %s（按 run_at, id 升序取前 %d 行）",
				i, row.TaskID, wantIDs[i], coreschedule.EngineRetryLimit)
		}
		if i > 0 && !row.RunAt.After(data.Retrying[i-1].RunAt) {
			t.Fatalf("Retrying[%d].RunAt = %s not after [%d] = %s", i, row.RunAt, i-1, data.Retrying[i-1].RunAt)
		}
	}

	// 删到恰好上限：LIMIT 上限+1 的探测行不再多出，截断应为 false。
	e.engineExec("DELETE FROM sign_tasks WHERE id = $1::uuid OR id = $2::uuid",
		wantIDs[coreschedule.EngineRetryLimit], wantIDs[coreschedule.EngineRetryLimit+1])
	data = e.engineSnapshot(corescope.Master(), day, now)
	if len(data.Retrying) != coreschedule.EngineRetryLimit || data.RetryingTruncated {
		t.Fatalf("after delete: retry rows = %d, truncated = %v; want %d/false",
			len(data.Retrying), data.RetryingTruncated, coreschedule.EngineRetryLimit)
	}
}

// TestEngineSnapshotInflightOrder：在飞按 claimed_at 升序；同一认领时刻
// 由 id 升序兜底。
func TestEngineSnapshotInflightOrder(t *testing.T) {
	e := newEnv(t)
	day := "2026-10-29"
	now := time.Date(2026, 10, 29, 7, 0, 0, 0, time.UTC)
	school := e.createSchool("engine-order")
	task := e.engineTask(school.ID, "晨间签到")
	a := e.engineNewAccount(school.ID, 1)
	b := e.engineNewAccount(school.ID, 2)
	runAt := now.Add(-time.Hour)
	e.enginePlan(a, task, day, runAt)
	e.enginePlan(b, task, day, runAt)
	claimedA := now.Add(-2 * time.Minute)
	claimedB := now.Add(-time.Minute)
	if _, err := e.sched.ClaimPending(e.ctx, a, task, day, claimedA); err != nil {
		t.Fatalf("claim A: %v", err)
	}
	if _, err := e.sched.ClaimPending(e.ctx, b, task, day, claimedB); err != nil {
		t.Fatalf("claim B: %v", err)
	}

	data := e.engineSnapshot(corescope.Master(), day, claimedB)
	if len(data.Inflight) != 2 || data.Inflight[0].AccountID != a || data.Inflight[1].AccountID != b {
		t.Fatalf("Inflight = %+v, want [A B] by claimed_at", data.Inflight)
	}

	// 同一认领时刻：id 升序兜底。
	e.engineExec(`UPDATE sign_tasks SET claimed_at = $1
	  WHERE account_id = $2::uuid AND school_task_id = $3::uuid AND day = $4::date`,
		claimedA, a, task, day)
	e.engineExec(`UPDATE sign_tasks SET claimed_at = $1
	  WHERE account_id = $2::uuid AND school_task_id = $3::uuid AND day = $4::date`,
		claimedA, b, task, day)
	wantOrder := strings.Split(e.scalarString(`SELECT string_agg(account_id::text, ',' ORDER BY id)
		FROM sign_tasks WHERE state = 'running' AND day = $1::date`, day), ",")
	if len(wantOrder) != 2 {
		t.Fatalf("running rows = %d, want 2", len(wantOrder))
	}
	data = e.engineSnapshot(corescope.Master(), day, claimedA)
	if len(data.Inflight) != 2 ||
		data.Inflight[0].AccountID != wantOrder[0] || data.Inflight[1].AccountID != wantOrder[1] {
		t.Fatalf("Inflight = %+v, want %v by id on equal claimed_at", data.Inflight, wantOrder)
	}
}

// TestEngineSnapshotRetryTieBreak：待重试同 run_at 时由 id 升序兜底，
// 不依赖数据库对等键行的偶然输出顺序。
func TestEngineSnapshotRetryTieBreak(t *testing.T) {
	e := newEnv(t)
	day := "2026-10-29"
	now := time.Date(2026, 10, 29, 7, 0, 0, 0, time.UTC)
	school := e.createSchool("engine-retry-tie")
	runAt := now.Add(30 * time.Minute)
	taskA := e.engineTask(school.ID, "晨间签到")
	taskB := e.engineTask(school.ID, "晚间签到")
	a := e.engineNewAccount(school.ID, 1)
	b := e.engineNewAccount(school.ID, 2)
	e.enginePlan(a, taskA, day, runAt)
	e.enginePlan(b, taskB, day, runAt)
	e.engineExec("UPDATE sign_tasks SET attempts = 1 WHERE day = $1::date", day)

	want := strings.Split(e.scalarString(`SELECT string_agg(id::text, ',' ORDER BY id)
		FROM sign_tasks WHERE state = 'pending' AND attempts > 0 AND day = $1::date`, day), ",")
	if len(want) != 2 {
		t.Fatalf("pending retry rows = %d, want 2", len(want))
	}
	data := e.engineSnapshot(corescope.Master(), day, now)
	if len(data.Retrying) != 2 {
		t.Fatalf("Retrying = %d rows, want 2", len(data.Retrying))
	}
	for i, row := range data.Retrying {
		if row.TaskID != want[i] {
			t.Fatalf("Retrying[%d].TaskID = %s, want %s（同 run_at 按 id 升序）", i, row.TaskID, want[i])
		}
	}
}

// TestEngineSnapshotRetryTieBreakAtLimit：同 run_at 的行超过上限时，
// 子查询必须按 id 升序取最小的 200 行；把子查询排序改成 id DESC 会红。
func TestEngineSnapshotRetryTieBreakAtLimit(t *testing.T) {
	e := newEnv(t)
	day := "2026-10-29"
	now := time.Date(2026, 10, 29, 7, 0, 0, 0, time.UTC)
	school := e.createSchool("engine-retry-tie-limit")
	acct := e.engineNewAccount(school.ID, 1)
	runAt := now.Add(time.Hour)
	total := coreschedule.EngineRetryLimit + 2
	for i := 0; i < total; i++ {
		taskID := e.engineTask(school.ID, fmt.Sprintf("同刻任务-%03d", i))
		e.enginePlan(acct, taskID, day, runAt)
	}
	e.engineExec("UPDATE sign_tasks SET attempts = 1 WHERE account_id = $1::uuid AND day = $2::date", acct, day)

	want := strings.Split(e.scalarString(`SELECT string_agg(id::text, ',' ORDER BY id)
		FROM (SELECT id FROM sign_tasks WHERE state = 'pending' AND attempts > 0
		      AND account_id = $1::uuid AND day = $2::date ORDER BY id LIMIT $3) x`, acct, day, coreschedule.EngineRetryLimit), ",")
	if len(want) != coreschedule.EngineRetryLimit {
		t.Fatalf("want ids = %d, want %d", len(want), coreschedule.EngineRetryLimit)
	}
	data := e.engineSnapshot(corescope.Master(), day, now)
	if len(data.Retrying) != coreschedule.EngineRetryLimit || !data.RetryingTruncated {
		t.Fatalf("Retrying = %d/truncated=%v, want %d/true", len(data.Retrying), data.RetryingTruncated, coreschedule.EngineRetryLimit)
	}
	for i, row := range data.Retrying {
		if row.TaskID != want[i] {
			t.Fatalf("Retrying[%d].TaskID = %s, want %s（同 run_at 超限时必须取最小 id）", i, row.TaskID, want[i])
		}
	}
}

// TestEngineSnapshotRetryLimitScope：LIMIT 只作用于待重试段。
// 同时有在飞行与超限重试行时，两段各自完整：在飞不被 LIMIT 挤掉，
// 重试仍取满上限并上报截断。
func TestEngineSnapshotRetryLimitScope(t *testing.T) {
	e := newEnv(t)
	day := "2026-10-29"
	now := time.Date(2026, 10, 29, 7, 0, 0, 0, time.UTC)
	school := e.createSchool("engine-limit-scope")

	inflightTask := e.engineTask(school.ID, "晨间签到")
	inflightAccounts := []string{
		e.engineNewAccount(school.ID, 1),
		e.engineNewAccount(school.ID, 2),
	}
	for _, acct := range inflightAccounts {
		e.enginePlan(acct, inflightTask, day, now.Add(-2*time.Hour))
		if _, err := e.sched.ClaimPending(e.ctx, acct, inflightTask, day, now.Add(-time.Minute)); err != nil {
			t.Fatalf("claim %s: %v", acct, err)
		}
	}

	retryAcct := e.engineNewAccount(school.ID, 3)
	base := now.Add(time.Hour)
	total := coreschedule.EngineRetryLimit + 2
	for i := 0; i < total; i++ {
		taskID := e.engineTask(school.ID, fmt.Sprintf("重试-%03d", i))
		e.enginePlan(retryAcct, taskID, day, base.Add(time.Duration(i)*time.Minute))
	}
	e.engineExec("UPDATE sign_tasks SET attempts = 1 WHERE account_id = $1::uuid AND day = $2::date", retryAcct, day)

	data := e.engineSnapshot(corescope.Master(), day, now)
	if len(data.Inflight) != 2 {
		t.Fatalf("Inflight = %d rows, want 2（LIMIT 只截待重试段）", len(data.Inflight))
	}
	if len(data.Retrying) != coreschedule.EngineRetryLimit || !data.RetryingTruncated {
		t.Fatalf("Retrying = %d/truncated=%v, want %d/true",
			len(data.Retrying), data.RetryingTruncated, coreschedule.EngineRetryLimit)
	}
}
