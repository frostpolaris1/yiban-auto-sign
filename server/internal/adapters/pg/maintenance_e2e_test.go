package pg_test

// 过期数据清理的 e2e：只删过期行，不动审计链。
// 依赖真实 PostgreSQL：批量 DELETE 与 date 比较都在库侧执行。

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/pg"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
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

// exec 造数辅助：失败即 Fatal，避免每条 INSERT 重复样板。
func (e *testEnv) exec(sql string, args ...any) {
	e.t.Helper()
	if _, err := e.db.Exec(e.ctx, sql, args...); err != nil {
		e.t.Fatalf("exec %q: %v", sql, err)
	}
}

// 软删满 30 天的账号被物理删除，credentials、sign_tasks、account_egress、
// account_location 不残留；软删 29 天的与活账号都不动。
// 批上限在仓储层同样成立：两个可删行、limit=1，一次只删一个。
func TestPruneDeletesSoftDeletedAccountsPastRetention(t *testing.T) {
	e := newEnv(t)
	now := time.Date(2026, 11, 6, 9, 0, 0, 0, time.UTC)
	schoolID, taskID, gone := e.seedTask(
		"prune-acct-a", "晨间签到", "prune-acct-a@example.com", "13800003333")
	other := e.createUser("prune-acct-b@example.com")
	kept := e.createAccount(schoolID, other.ID, "13800004444")
	live := e.createAccount(schoolID, other.ID, "13800005555")
	gone2 := e.createAccount(schoolID, other.ID, "13800006666")

	mustNotError(t, e.accts.SoftDelete(e.ctx, corescope.Master(), gone, other.ID, now), "soft delete gone")
	mustNotError(t, e.accts.SoftDelete(e.ctx, corescope.Master(), kept.ID, other.ID, now), "soft delete kept")
	mustNotError(t, e.accts.SoftDelete(e.ctx, corescope.Master(), gone2.ID, other.ID, now), "soft delete gone2")
	backdate := func(id string, at time.Time) {
		e.t.Helper()
		if _, err := e.db.Exec(e.ctx, `UPDATE accounts SET deleted_at = $1 WHERE id = $2::uuid`, at, id); err != nil {
			t.Fatalf("backdate account: %v", err)
		}
	}
	backdate(gone, now.Add(-31*24*time.Hour))
	backdate(kept.ID, now.Add(-29*24*time.Hour))
	backdate(gone2.ID, now.Add(-40*24*time.Hour))

	// 被删账号的从属行：任务行、凭据、出口绑定、定位策略。
	e.exec(`INSERT INTO sign_tasks (account_id, school_task_id, day, run_at)
		VALUES ($1::uuid, $2::uuid, '2026-05-01', now())`, gone, taskID)
	e.exec(`INSERT INTO credentials (account_id, school_id, kid, nonce, ciphertext)
		VALUES ($1::uuid, $2::uuid, '0123456789abcdef',
		        decode(repeat('ab', 12), 'hex'), decode(repeat('cd', 16), 'hex'))`, gone, schoolID)
	e.exec(`INSERT INTO egresses (identity) VALUES ('prune-acct-egress')`)
	e.exec(`INSERT INTO account_egress (account_id, egress_id)
		SELECT $1::uuid, id FROM egresses WHERE identity = 'prune-acct-egress'`, gone)
	e.exec(`INSERT INTO account_location (account_id, mode, lng, lat, reason)
		VALUES ($1::uuid, 'fixed', 0.5, 0.5, 'prune seed')`, gone)
	// 保留账号的任务行：证明子行不随批量删除被误删。
	e.exec(`INSERT INTO sign_tasks (account_id, school_task_id, day, run_at)
		VALUES ($1::uuid, $2::uuid, '2026-05-02', now())`, kept.ID, taskID)
	// 审计行无外键：账号物理删除不得使其消失。
	e.exec(`INSERT INTO audit_logs (ts, actor_label, action, target_type, target_id, prev_hash, hash)
		VALUES (now(), 'p***@example.test', 'test.prune', 'account', $1,
		        decode(repeat('00', 32), 'hex'), decode(repeat('44', 32), 'hex'))`, gone)
	e.exec(`INSERT INTO audit_logs (ts, actor_label, action, target_type, target_id, prev_hash, hash)
		VALUES (now(), 'p***@example.test', 'test.prune', 'account', $1,
		        decode(repeat('00', 32), 'hex'), decode(repeat('55', 32), 'hex'))`, gone2.ID)

	repo := pg.NewMaintenanceRepo(e.db)
	cutoff := now.Add(-30 * 24 * time.Hour)
	// 40 天与 31 天两行可删；批上限 1 时每次恰删一行，第三轮为 0。
	for i, want := range []int64{1, 1, 0} {
		n, err := repo.DeleteAccountsDeletedBefore(context.Background(), cutoff, 1)
		if err != nil {
			t.Fatalf("delete accounts round %d: %v", i+1, err)
		}
		if n != want {
			t.Fatalf("round %d deleted %d accounts, want %d", i+1, n, want)
		}
	}

	for name, q := range map[string]string{
		"credentials":      `SELECT count(*) FROM credentials WHERE account_id = $1::uuid`,
		"sign_tasks":       `SELECT count(*) FROM sign_tasks WHERE account_id = $1::uuid`,
		"account_egress":   `SELECT count(*) FROM account_egress WHERE account_id = $1::uuid`,
		"account_location": `SELECT count(*) FROM account_location WHERE account_id = $1::uuid`,
	} {
		if got := e.scalarInt(q, gone); got != 0 {
			t.Fatalf("账号已物理删除但 %s 残留 %d 行", name, got)
		}
	}
	if got := e.scalarInt(`SELECT count(*) FROM accounts WHERE id = ANY($1::text[]::uuid[])`,
		[]string{gone, gone2.ID}); got != 0 {
		t.Fatalf("过保留期账号残留 %d 行", got)
	}
	if got := e.scalarInt(`SELECT count(*) FROM accounts WHERE id = $1::uuid`, kept.ID); got != 1 {
		t.Fatalf("软删 29 天的账号被误删")
	}
	if got := e.scalarInt(`SELECT count(*) FROM accounts WHERE id = $1::uuid`, live.ID); got != 1 {
		t.Fatalf("活账号被误删")
	}
	if got := e.scalarInt(`SELECT count(*) FROM sign_tasks WHERE account_id = $1::uuid`, kept.ID); got != 1 {
		t.Fatalf("保留账号的任务行被动到")
	}
	if got := e.scalarInt(`SELECT count(*) FROM users WHERE email = 'prune-acct-a@example.com'`); got != 1 {
		t.Fatalf("账号清理不应碰用户行")
	}
	if got := e.scalarInt(`SELECT count(*) FROM audit_logs WHERE target_id = ANY($1::text[])`,
		[]string{gone, gone2.ID}); got != 2 {
		t.Fatalf("审计行随账号物理删除变化：append-only 表不受保留期影响, got %d want 2", got)
	}
}

// 软删满 90 天的用户被物理删除：名下账号（即便软删不足 30 天）与其任务行、
// 凭据一并删除；sessions、time_prefs、photos、notifications 按既有级联无残留；
// 自建 window_overrides 保留且 created_by 置空；reviewed 对与定位引用成对处置；
// 审计行数不变。
func TestPruneDeletesSoftDeletedUsersPastRetention(t *testing.T) {
	e := newEnv(t)
	now := time.Date(2026, 11, 6, 9, 0, 0, 0, time.UTC)
	schoolID, taskID, _ := e.seedTask(
		"prune-user", "晨间签到", "prune-user-staff@example.com", "13800000020")
	userA := e.createUser("prune-user-a@example.com") // 软删 91 天：删
	userB := e.createUser("prune-user-b@example.com") // 软删 89 天：留
	userC := e.createUser("prune-user-c@example.com") // 活用户
	acctA := e.createAccount(schoolID, userA.ID, "13800008000")
	acctB := e.createAccount(schoolID, userB.ID, "13800008888")

	// acctA 只软删 10 天（不足账号保留期），仍须随用户清理一并删除。
	mustNotError(t, e.accts.SoftDelete(e.ctx, corescope.Master(), acctA.ID, userA.ID, now), "soft delete acctA")
	e.exec(`UPDATE accounts SET deleted_at = $1 WHERE id = $2::uuid`, now.Add(-10*24*time.Hour), acctA.ID)
	e.exec(`INSERT INTO sign_tasks (account_id, school_task_id, day, run_at)
		VALUES ($1::uuid, $2::uuid, '2026-05-01', now())`, acctA.ID, taskID)
	e.exec(`INSERT INTO credentials (account_id, school_id, kid, nonce, ciphertext)
		VALUES ($1::uuid, $2::uuid, 'fedcba9876543210',
		        decode(repeat('ab', 12), 'hex'), decode(repeat('cd', 16), 'hex'))`, acctA.ID, schoolID)

	// 级联面：会话、时间偏好、照片、站内信。
	e.newSession(userA.ID, "prune-user-a-token", now)
	e.newSession(userB.ID, "prune-user-b-token", now)
	e.exec(`INSERT INTO time_prefs (user_id, offset_min) VALUES ($1::uuid, 30)`, userA.ID)
	e.exec(`INSERT INTO photos (owner_user_id, sha256, mime, width, height, bytes)
		VALUES ($1::uuid, decode(repeat('ab', 32), 'hex'), 'image/jpeg', 1, 1, 1)`, userA.ID)
	e.exec(`INSERT INTO notifications (user_id, category, dedupe_key, title)
		VALUES ($1::uuid, 'review_pending', 'prune-user-a', 'x')`, userA.ID)

	// 窗口覆盖按日生效、不随创建者清除（00027 SET NULL）：userA 建的未来日
	// 覆盖保留、created_by 置空；userC 建的覆盖原样保留。
	e.exec(`INSERT INTO window_overrides (school_task_id, day, start_at, end_at, reason, created_by)
		VALUES ($1::uuid, '2027-01-01', '2027-01-01 07:00+00', '2027-01-01 08:00+00', 'prune seed', $2::uuid)`,
		taskID, userA.ID)
	e.exec(`INSERT INTO window_overrides (school_task_id, day, start_at, end_at, reason, created_by)
		VALUES ($1::uuid, '2026-05-03', '2026-05-03 07:00+00', '2026-05-03 08:00+00', 'prune seed', $2::uuid)`,
		taskID, userC.ID)

	// 他人账号（userB 的 acctB）的定位行由 userA 更新过：updated_by 无
	// ON DELETE，不清引用则删用户被 FK 拦下；行本身须保留。
	e.exec(`INSERT INTO account_location (account_id, mode, lng, lat, reason, updated_by)
		VALUES ($1::uuid, 'fixed', 0.5, 0.5, 'prune seed', $2::uuid)`, acctB.ID, userA.ID)
	// acctB 由 userA 审核：RI 只置 reviewed_by 会撞 ck_accounts_review_pair，
	// 清理须成对置空 reviewed_by/reviewed_at 并保留账号行。
	e.exec(`UPDATE accounts SET reviewed_by = $2::uuid, reviewed_at = now() WHERE id = $1::uuid`,
		acctB.ID, userA.ID)

	// 审计行无外键：物理删除不得使其消失。
	e.exec(`INSERT INTO audit_logs (ts, actor_label, action, target_type, target_id, prev_hash, hash)
		VALUES (now(), 'p***@example.test', 'test.prune', 'user', $1,
		        decode(repeat('00', 32), 'hex'), decode(repeat('11', 32), 'hex'))`, userA.ID)
	e.exec(`INSERT INTO audit_logs (ts, actor_label, action, target_type, target_id, prev_hash, hash)
		VALUES (now(), 'p***@example.test', 'test.prune', 'user', $1,
		        decode(repeat('00', 32), 'hex'), decode(repeat('22', 32), 'hex'))`, userA.ID)
	e.exec(`INSERT INTO audit_logs (ts, actor_label, action, target_type, target_id, prev_hash, hash)
		VALUES (now(), 'p***@example.test', 'test.prune', 'account', $1,
		        decode(repeat('00', 32), 'hex'), decode(repeat('33', 32), 'hex'))`, acctA.ID)

	mustNotError(t, e.users.SoftDelete(e.ctx, userA.ID, userC.ID, now), "soft delete userA")
	mustNotError(t, e.users.SoftDelete(e.ctx, userB.ID, userC.ID, now), "soft delete userB")
	e.exec(`UPDATE users SET deleted_at = $1 WHERE id = $2::uuid`, now.Add(-91*24*time.Hour), userA.ID)
	e.exec(`UPDATE users SET deleted_at = $1 WHERE id = $2::uuid`, now.Add(-89*24*time.Hour), userB.ID)

	repo := pg.NewMaintenanceRepo(e.db)
	n, err := repo.DeleteUsersDeletedBefore(context.Background(), now.Add(-90*24*time.Hour), 1000)
	if err != nil {
		t.Fatalf("delete users: %v", err)
	}
	if n != 1 {
		t.Fatalf("deleted %d users, want 1（只删 91 天的）", n)
	}

	if got := e.scalarInt(`SELECT count(*) FROM users WHERE id = $1::uuid`, userA.ID); got != 0 {
		t.Fatalf("软删 91 天的用户残留")
	}
	if got := e.scalarInt(`SELECT count(*) FROM users WHERE id = ANY($1::text[]::uuid[])`,
		[]string{userB.ID, userC.ID}); got != 2 {
		t.Fatalf("89 天用户或活用户被误删")
	}
	if got := e.scalarInt(`SELECT count(*) FROM accounts WHERE id = $1::uuid`, acctA.ID); got != 0 {
		t.Fatalf("被清理用户名下账号残留（账号软删不足 30 天也须随用户删）")
	}
	if got := e.scalarInt(`SELECT count(*) FROM accounts WHERE id = $1::uuid`, acctB.ID); got != 1 {
		t.Fatalf("保留用户的账号被误删")
	}
	for name, sql := range map[string]string{
		"sign_tasks":    `SELECT count(*) FROM sign_tasks WHERE account_id = $1::uuid`,
		"credentials":   `SELECT count(*) FROM credentials WHERE account_id = $1::uuid`,
		"sessions":      `SELECT count(*) FROM sessions WHERE user_id = $1::uuid`,
		"time_prefs":    `SELECT count(*) FROM time_prefs WHERE user_id = $1::uuid`,
		"photos":        `SELECT count(*) FROM photos WHERE owner_user_id = $1::uuid`,
		"notifications": `SELECT count(*) FROM notifications WHERE user_id = $1::uuid`,
	} {
		if got := e.scalarInt(sql, userA.ID); got != 0 {
			t.Fatalf("被清理用户的 %s 残留 %d 行", name, got)
		}
	}
	if got := e.scalarInt(`SELECT count(*) FROM sessions WHERE user_id = $1::uuid`, userB.ID); got != 1 {
		t.Fatalf("保留用户的会话被误删")
	}
	if got := e.scalarInt(`SELECT count(*) FROM window_overrides
		WHERE school_task_id = $1::uuid AND day = '2027-01-01' AND created_by IS NULL`, taskID); got != 1 {
		t.Fatalf("被清理用户创建的窗口覆盖应保留且 created_by 置空, got %d", got)
	}
	if got := e.scalarInt(`SELECT count(*) FROM window_overrides
		WHERE school_task_id = $1::uuid AND day = '2026-05-03' AND created_by = $2::uuid`,
		taskID, userC.ID); got != 1 {
		t.Fatalf("他人创建的窗口覆盖被误删或被动到")
	}
	if got := e.scalarInt(`SELECT count(*) FROM accounts
		WHERE id = $1::uuid AND reviewed_by IS NULL AND reviewed_at IS NULL`, acctB.ID); got != 1 {
		t.Fatalf("保留账号的审核对未成对置空：RI 单侧置空会撞 ck_accounts_review_pair")
	}
	if got := e.scalarInt(`SELECT count(*) FROM account_location WHERE account_id = $1::uuid`, acctB.ID); got != 1 {
		t.Fatalf("他人账号的定位行被误删")
	}
	if got := e.scalarInt(`SELECT count(*) FROM account_location
		WHERE account_id = $1::uuid AND updated_by IS NOT NULL`, acctB.ID); got != 0 {
		t.Fatalf("定位行的 updated_by 未置空：删用户将被 FK 拦下")
	}
	if got := e.scalarInt(`SELECT count(*) FROM audit_logs WHERE target_id = ANY($1::text[])`,
		[]string{userA.ID, acctA.ID}); got != 3 {
		t.Fatalf("审计行随物理删除变化：append-only 表不受保留期影响, got %d want 3", got)
	}
	// GetOverride 读侧不依赖 actor：创建者被清后覆盖可读、CreatedBy 为空。
	ov, err := e.sched.GetOverride(e.ctx, taskID, "2027-01-01")
	if err != nil {
		t.Fatalf("get override after purge: %v", err)
	}
	if ov.CreatedBy != "" {
		t.Fatalf("覆盖 CreatedBy = %q, want 空串", ov.CreatedBy)
	}
	wantStart := time.Date(2027, 1, 1, 7, 0, 0, 0, time.UTC)
	if !ov.Start.Equal(wantStart) || !ov.End.Equal(wantStart.Add(time.Hour)) {
		t.Fatalf("覆盖窗口被改: %v..%v, want %v..%v", ov.Start, ov.End, wantStart, wantStart.Add(time.Hour))
	}
}

// lockHolder 用独立连接持有一行 FOR UPDATE。返回的 join 函数关闭放行
// 通道、等待 holder 事务回滚（锁随之释放）并校验其结果。
// 全程通道编排，无 sleep。
func lockHolder(t *testing.T, e *testEnv, lockSQL string, arg any) (join func()) {
	t.Helper()
	holder, err := pg.Open(e.ctx, pg.Options{DSN: e.dsn, MaxConns: 1})
	if err != nil {
		t.Fatalf("open lock holder: %v", err)
	}
	t.Cleanup(holder.Close)
	acquired := make(chan error, 1)
	releaseCh := make(chan struct{})
	done := make(chan error, 1)
	go func() {
		done <- holder.InTx(e.ctx, func(ctx context.Context) error {
			if _, err := holder.Exec(ctx, lockSQL, arg); err != nil {
				acquired <- err
				return err
			}
			close(acquired)
			<-releaseCh
			return errors.New("lock holder rollback")
		})
	}()
	if err := <-acquired; err != nil {
		t.Fatalf("lock holder 取锁: %v", err)
	}
	return func() {
		close(releaseCh)
		if err := <-done; err == nil {
			t.Fatal("lock holder 应以回滚结束")
		}
	}
}

// 用户行被另一事务持锁时，清理等锁到 lock_timeout 报错、行原样；
// 放锁后重跑正常删除。
func TestDeleteUsersDeletedBeforeBlocksOnRowLock(t *testing.T) {
	e := newEnv(t)
	now := time.Date(2026, 11, 6, 9, 0, 0, 0, time.UTC)
	user := e.createUser("prune-lock-user@example.com")
	mustNotError(t, e.users.SoftDelete(e.ctx, user.ID, user.ID, now), "soft delete user")
	e.exec(`UPDATE users SET deleted_at = $1 WHERE id = $2::uuid`, now.Add(-91*24*time.Hour), user.ID)

	join := lockHolder(t, e, `SELECT id FROM users WHERE id = $1::uuid FOR UPDATE`, user.ID)

	repo := pg.NewMaintenanceRepo(e.db)
	cutoff := now.Add(-90 * 24 * time.Hour)
	if _, err := repo.DeleteUsersDeletedBefore(context.Background(), cutoff, 1000); err == nil {
		t.Fatal("用户行被持锁时清理应因 lock_timeout 报错")
	}
	if got := e.scalarInt(`SELECT count(*) FROM users WHERE id = $1::uuid`, user.ID); got != 1 {
		t.Fatalf("持锁期间用户行被删")
	}

	join()
	n, err := repo.DeleteUsersDeletedBefore(context.Background(), cutoff, 1000)
	if err != nil || n != 1 {
		t.Fatalf("放锁后重跑: deleted %d, err %v; want 1, nil", n, err)
	}
	if got := e.scalarInt(`SELECT count(*) FROM users WHERE id = $1::uuid`, user.ID); got != 0 {
		t.Fatalf("放锁后用户行未被删")
	}
}

// 账号行被另一事务持锁时同上：等锁报错、行原样；放锁后重跑成功删除。
func TestDeleteAccountsDeletedBeforeBlocksOnRowLock(t *testing.T) {
	e := newEnv(t)
	now := time.Date(2026, 11, 6, 9, 0, 0, 0, time.UTC)
	school := e.createSchool("prune-lock-acct")
	acct := e.createAccount(school.ID, e.createUser("prune-lock-acct-owner@example.com").ID, "13800009001")
	mustNotError(t, e.accts.SoftDelete(e.ctx, corescope.Master(), acct.ID, acct.OwnerUserID, now), "soft delete account")
	e.exec(`UPDATE accounts SET deleted_at = $1 WHERE id = $2::uuid`, now.Add(-31*24*time.Hour), acct.ID)

	join := lockHolder(t, e, `SELECT id FROM accounts WHERE id = $1::uuid FOR UPDATE`, acct.ID)

	repo := pg.NewMaintenanceRepo(e.db)
	cutoff := now.Add(-30 * 24 * time.Hour)
	if _, err := repo.DeleteAccountsDeletedBefore(context.Background(), cutoff, 1000); err == nil {
		t.Fatal("账号行被持锁时清理应因 lock_timeout 报错")
	}
	if got := e.scalarInt(`SELECT count(*) FROM accounts WHERE id = $1::uuid`, acct.ID); got != 1 {
		t.Fatalf("持锁期间账号行被删")
	}

	join()
	n, err := repo.DeleteAccountsDeletedBefore(context.Background(), cutoff, 1000)
	if err != nil || n != 1 {
		t.Fatalf("放锁后重跑: deleted %d, err %v; want 1, nil", n, err)
	}
	if got := e.scalarInt(`SELECT count(*) FROM accounts WHERE id = $1::uuid`, acct.ID); got != 0 {
		t.Fatalf("放锁后账号行未被删")
	}
}

// 任务行分块删除：一个账号 seed 分块阈值 +1 条任务行，清理后全部删净——
// 覆盖「恰好删满一块后循环继续」的分支。
func TestDeleteAccountsDeletedBeforeChunksSignTasks(t *testing.T) {
	e := newEnv(t)
	now := time.Date(2026, 11, 6, 9, 0, 0, 0, time.UTC)
	school := e.createSchool("prune-chunk")
	taskID, err := e.sched.CreateSchoolTask(e.ctx, school.ID, "分块签到")
	if err != nil {
		t.Fatalf("create school task: %v", err)
	}
	acct := e.createAccount(school.ID, e.createUser("prune-chunk-owner@example.com").ID, "13800009002")
	mustNotError(t, e.accts.SoftDelete(e.ctx, corescope.Master(), acct.ID, acct.OwnerUserID, now), "soft delete account")
	e.exec(`UPDATE accounts SET deleted_at = $1 WHERE id = $2::uuid`, now.Add(-31*24*time.Hour), acct.ID)
	// 1001 = 单语句分块上限 + 1：第一语句删满一块，循环必须再来一轮。
	e.exec(`INSERT INTO sign_tasks (account_id, school_task_id, day, run_at)
		SELECT $1::uuid, $2::uuid, DATE '2026-01-01' + gs, now()
		FROM generate_series(0, 1000) AS gs`, acct.ID, taskID)

	repo := pg.NewMaintenanceRepo(e.db)
	n, err := repo.DeleteAccountsDeletedBefore(context.Background(), now.Add(-30*24*time.Hour), 1000)
	if err != nil {
		t.Fatalf("delete account: %v", err)
	}
	if n != 1 {
		t.Fatalf("deleted %d accounts, want 1", n)
	}
	if got := e.scalarInt(`SELECT count(*) FROM sign_tasks WHERE account_id = $1::uuid`, acct.ID); got != 0 {
		t.Fatalf("分块循环未删净任务行: 残留 %d", got)
	}
	if got := e.scalarInt(`SELECT count(*) FROM accounts WHERE id = $1::uuid`, acct.ID); got != 0 {
		t.Fatalf("账号行未被删")
	}
}
