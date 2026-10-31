package pg_test

// 过期数据清理的 e2e：只删过期行，不动审计链。
// 依赖真实 PostgreSQL：批量 DELETE 与 date 比较都在库侧执行。

import (
	"context"
	"testing"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/pg"
)

// 过期会话与过期令牌被删，未过期的不动；90 天前的任务行被删。
func TestPruneDeletesOnlyExpiredRows(t *testing.T) {
	e := newEnv(t)
	_, taskID, accountID := e.seedTask(
		"prune", "晨间签到", "prune-owner@example.com", "13800022222")
	owner := e.createUser("prune-sessions@example.com")

	now := time.Date(2026, 10, 31, 9, 0, 0, 0, time.UTC)
	expired := now.Add(-2 * time.Hour)
	live := now.Add(2 * time.Hour)

	insertSession := func(hash int, expires time.Time) {
		e.t.Helper()
		if _, err := e.db.Exec(e.ctx, `
			INSERT INTO sessions (user_id, token_hash, idle_expires_at, expires_at)
			VALUES ($1::uuid, sha256($2::bytea), $3, $3)`,
			owner.ID, []byte{byte(hash)}, expires); err != nil {
			t.Fatalf("insert session: %v", err)
		}
	}
	insertSession(1, expired)
	insertSession(2, live)

	insertReset := func(hash int, expires time.Time) {
		e.t.Helper()
		if _, err := e.db.Exec(e.ctx, `
			INSERT INTO password_resets (user_id, token_hash, created_at, expires_at)
			VALUES ($1::uuid, sha256($2::bytea), $3, $4)`,
			owner.ID, []byte{byte(hash)}, now.Add(-24*time.Hour), expires); err != nil {
			t.Fatalf("insert reset: %v", err)
		}
	}
	insertReset(1, expired)
	insertReset(2, live)

	insertTask := func(day string) {
		e.t.Helper()
		if _, err := e.db.Exec(e.ctx, `
			INSERT INTO sign_tasks (account_id, school_task_id, day, run_at)
			VALUES ($1::uuid, $2::uuid, $3::date, now())`,
			accountID, taskID, day); err != nil {
			t.Fatalf("insert task %s: %v", day, err)
		}
	}
	insertTask("2026-05-01") // 183 天前：删
	insertTask("2026-10-30") // 昨天：留
	if _, err := e.db.Exec(e.ctx, `
		INSERT INTO audit_logs (ts, actor_label, action, target_type, target_id, prev_hash, hash)
		VALUES (now(), 'x***@example.test', 'test.prune', 'task', 'x',
		        decode(repeat('00', 32), 'hex'), decode(repeat('11', 32), 'hex'))`); err != nil {
		t.Fatalf("insert audit: %v", err)
	}

	// 只测 SQL：分批与保留期换算由 app/retention 的单测覆盖
	// （适配器不 import 用例层，canonical boundary.adapters）。
	repo := pg.NewMaintenanceRepo(e.db)
	pruned := map[string]int64{}
	for name, del := range map[string]func() (int64, error){
		"sessions": func() (int64, error) { return repo.DeleteExpiredSessions(context.Background(), now, 1000) },
		"resets":   func() (int64, error) { return repo.DeleteExpiredPasswordResets(context.Background(), now, 1000) },
		"tasks":    func() (int64, error) { return repo.DeleteTasksBefore(context.Background(), "2026-08-02", 1000) },
	} {
		n, err := del()
		if err != nil {
			t.Fatalf("delete %s: %v", name, err)
		}
		pruned[name] = n
	}
	if pruned["sessions"] != 1 || pruned["resets"] != 1 || pruned["tasks"] != 1 {
		t.Fatalf("pruned = %v, want 1/1/1", pruned)
	}
	if got := e.scalarInt("SELECT count(*) FROM sessions"); got != 1 {
		t.Fatalf("sessions = %d, want 1（只留未过期的）", got)
	}
	if got := e.scalarInt("SELECT count(*) FROM password_resets"); got != 1 {
		t.Fatalf("password_resets = %d, want 1", got)
	}
	if got := e.scalarInt("SELECT count(*) FROM sign_tasks WHERE day = '2026-05-01'"); got != 0 {
		t.Fatalf("旧任务行未删")
	}
	if got := e.scalarInt("SELECT count(*) FROM sign_tasks WHERE day = '2026-10-30'"); got != 1 {
		t.Fatalf("昨日任务行被误删")
	}
	if got := e.scalarInt("SELECT count(*) FROM audit_logs"); got != 1 {
		t.Fatalf("审计行被清理：append-only 表不得由保留期删除")
	}
}
