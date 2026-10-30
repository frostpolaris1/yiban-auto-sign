package pg_test

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/pg"
	coreaccount "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/account"
	coreaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/audit"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	coreschool "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/school"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
)

// --- E-10：迁移连续执行两次 ------------------------------------------------

func TestMigrationUpIsIdempotent(t *testing.T) {
	e := newEnv(t) // newEnv 已经执行过一次 migrate up。

	historyBefore := e.scalarInt("SELECT count(*) FROM goose_db_version")

	v1, err := pg.Migrate(e.ctx, e.dsn)
	mustNotError(t, err, "second migrate up")
	historyAfterSecond := e.scalarInt("SELECT count(*) FROM goose_db_version")

	v2, err := pg.Migrate(e.ctx, e.dsn)
	mustNotError(t, err, "third migrate up")
	historyAfterThird := e.scalarInt("SELECT count(*) FROM goose_db_version")

	if v1 != v2 {
		t.Fatalf("version moved on a repeated migrate: %d then %d", v1, v2)
	}
	if historyAfterSecond != historyAfterThird {
		t.Fatalf("migration history grew on a repeated migrate: %d then %d",
			historyAfterSecond, historyAfterThird)
	}
	if historyBefore > historyAfterSecond {
		t.Fatalf("history shrank: %d then %d", historyBefore, historyAfterSecond)
	}
	embedded, err := pg.EmbeddedVersion()
	mustNotError(t, err, "embedded version")
	if v1 != embedded {
		t.Fatalf("database version %d does not match embedded version %d", v1, embedded)
	}

	// P1 的七张表加 password_resets 必须都存在于此 schema。
	for _, table := range []string{
		"users", "sessions", "schools", "admin_schools", "accounts",
		"credentials", "audit_logs", "password_resets",
	} {
		got := e.scalarInt(
			"SELECT count(*) FROM information_schema.tables WHERE table_schema = current_schema() AND table_name = $1",
			table)
		if got != 1 {
			t.Errorf("table %s missing from the migrated schema", table)
		}
	}
}

// --- E-11：并发创建第 10 001 个账号 ----------------------------------------

func TestCapacityAdmitsExactlyOneWinner(t *testing.T) {
	e := newEnv(t)
	owner := e.createUser("capacity@example.com")
	school := e.createSchool("capacity-school")

	// 一条语句填到 9 999 个活跃账号。
	inserted, err := e.db.Exec(e.ctx, `
		INSERT INTO accounts (school_id, owner_user_id, phone)
		SELECT $1::uuid, $2::uuid, '1' || lpad(i::text, 10, '0')
		  FROM generate_series(1, 9999) AS i`,
		school.ID, owner.ID)
	mustNotError(t, err, "prefill 9999 accounts")
	if inserted != 9999 {
		t.Fatalf("prefilled %d accounts, want 9999", inserted)
	}

	// 两个独立连接池给出两条独立后端连接。
	// 共用同一池的两个 goroutine 无法证明并发（AGENTS §32）。
	dbA, err := pg.Open(e.ctx, pg.Options{DSN: e.dsn, MaxConns: 1, StatementTimeout: 30 * time.Second})
	mustNotError(t, err, "open independent pool A")
	defer dbA.Close()
	dbB, err := pg.Open(e.ctx, pg.Options{DSN: e.dsn, MaxConns: 1, StatementTimeout: 30 * time.Second})
	mustNotError(t, err, "open independent pool B")
	defer dbB.Close()

	repoA := pg.NewAccountRepo(dbA)
	repoB := pg.NewAccountRepo(dbB)
	start := make(chan struct{})
	results := make(chan error, 2)
	for _, pair := range []struct {
		db    *pg.DB
		repo  *pg.AccountRepo
		phone string
	}{
		{dbA, repoA, "19999999991"},
		{dbB, repoB, "19999999992"},
	} {
		go func(db *pg.DB, repo *pg.AccountRepo, phone string) {
			<-start
			results <- db.InTx(e.ctx, func(ctx context.Context) error {
				_, err := repo.Create(ctx, coreaccount.New{
					SchoolID:    school.ID,
					OwnerUserID: owner.ID,
					Phone:       phone,
				}, 10000, 10000)
				return err
			})
		}(pair.db, pair.repo, pair.phone)
	}
	close(start)

	var succeeded, capacity int
	for i := 0; i < 2; i++ {
		err := <-results
		switch {
		case err == nil:
			succeeded++
		case codeOf(t, err) == errs.CodeCapacity:
			capacity++
		default:
			t.Fatalf("unexpected create error: %v (%+v)", err, errs.From(err))
		}
	}
	if succeeded != 1 || capacity != 1 {
		t.Fatalf("want exactly one success and one capacity error, got %d succeeded and %d capacity", succeeded, capacity)
	}
	live := e.scalarInt("SELECT count(*) FROM accounts WHERE deleted_at IS NULL")
	if live != 10000 {
		t.Fatalf("live account count is %d after the race; want 10000", live)
	}
}

// --- E-12：名下有账号的用户不可删除 ----------------------------------------

func TestForeignKeyBlocksUserDelete(t *testing.T) {
	e := newEnv(t)
	owner := e.createUser("fk@example.com")
	school := e.createSchool("fk-school")
	account := e.createAccount(school.ID, owner.ID, "13800138000")

	_, err := e.db.Exec(e.ctx, "DELETE FROM users WHERE id = $1", owner.ID)
	wantError(t, err, "delete a user that still owns an account")
	mapped := errs.From(err)
	if mapped.Code != errs.CodeConflict {
		t.Fatalf("want conflict from the foreign key, got %v", err)
	}
	// PostgreSQL 18 把 ON DELETE RESTRICT 报成 23001，
	// 普通外键违反是 23503。两者都映射为冲突。
	// 约束名是内部 schema 面：details 不得泄漏。
	if _, ok := mapped.Details["constraint"]; ok {
		t.Fatalf("details leaked the constraint name: %v", mapped.Details)
	}
	if _, err := e.users.GetByID(e.ctx, owner.ID); err != nil {
		t.Fatalf("user vanished after the blocked delete: %v", err)
	}
	if _, err := e.accts.Get(e.ctx, corescope.Master(), account.ID); err != nil {
		t.Fatalf("account vanished after the blocked delete: %v", err)
	}
}

// TestInsertForeignKeyViolationMapsToNotFound 钉住 schema §2.4：
// 插入时引用不存在的父行是 not_found（HTTP 404），
// 被拒的删除仍是冲突（HTTP 409）。
func TestInsertForeignKeyViolationMapsToNotFound(t *testing.T) {
	e := newEnv(t)
	owner := e.createUser("fk-insert@example.com")
	school := e.createSchool("fk-insert-school")

	const missingID = "0192f0f0-0000-7000-8000-00000000dead"

	err := e.db.InTx(e.ctx, func(ctx context.Context) error {
		_, err := e.accts.Create(ctx, coreaccount.New{
			SchoolID:    missingID,
			OwnerUserID: owner.ID,
			Phone:       "13400134000",
		}, 10000, 10000)
		return err
	})
	if got := codeOf(t, err); got != errs.CodeNotFound {
		t.Fatalf("account with a missing school: got %v (%s); want not_found", err, got)
	}

	err = e.db.InTx(e.ctx, func(ctx context.Context) error {
		return e.accts.InsertCredential(ctx, coreaccount.Credential{
			AccountID:  missingID,
			SchoolID:   school.ID,
			KID:        "0123456789abcdef",
			AADVersion: 1,
			Nonce:      bytes.Repeat([]byte{0x01}, 12),
			Ciphertext: bytes.Repeat([]byte{0x02}, 32),
			Version:    1,
		})
	})
	if got := codeOf(t, err); got != errs.CodeNotFound {
		t.Fatalf("credential for a missing account: got %v (%s); want not_found", err, got)
	}

	_, err = e.resets.Create(e.ctx, coreauthNewReset{
		UserID:    missingID,
		TokenHash: tokenHash("missing-user"),
		Now:       time.Now().UTC(),
		ExpiresAt: time.Now().UTC().Add(time.Hour),
	})
	if got := codeOf(t, err); got != errs.CodeNotFound {
		t.Fatalf("reset token for a missing user: got %v (%s); want not_found", err, got)
	}

	// 契约的另一半：删除受限仍是冲突（409）。
	account := e.createAccount(school.ID, owner.ID, "13400134001")
	_, err = e.db.Exec(e.ctx, "DELETE FROM users WHERE id = $1", owner.ID)
	if got := codeOf(t, err); got != errs.CodeConflict {
		t.Fatalf("delete of a user that owns an account: got %v (%s); want conflict", err, got)
	}
	if _, err := e.accts.Get(e.ctx, corescope.Master(), account.ID); err != nil {
		t.Fatalf("account vanished after the blocked delete: %v", err)
	}
}

// --- E-13：审计行只可追加 ------------------------------------------------

func TestAuditRowsAreAppendOnly(t *testing.T) {
	e := newEnv(t)
	e.auditEntry(coreaudit.ActionUserCreate, map[string]any{"field": "role"})

	for _, statement := range []string{
		"UPDATE audit_logs SET detail = '{}'",
		"DELETE FROM audit_logs",
		"TRUNCATE audit_logs",
	} {
		if _, err := e.db.Exec(e.ctx, statement); err == nil {
			t.Fatalf("%q was accepted; the append-only guard did not fire", statement)
		}
	}

	if rows := e.scalarInt("SELECT count(*) FROM audit_logs"); rows != 1 {
		t.Fatalf("audit row count is %d after rejected writes; want 1", rows)
	}
	if detail, err := e.db.QueryString(e.ctx, "SELECT detail FROM audit_logs ORDER BY id LIMIT 1"); err != nil || detail != `{"field":"role"}` {
		t.Fatalf("audit detail changed: %q, %v", detail, err)
	}
}

// TestAuditPurgeSwitchIsGone 钉住 yiban.audit_purge 逃逸开关已移除。
// 运行期角色可以 SET 任意自定义参数；触发器必须忽略它，
// 故 DELETE 与 TRUNCATE 始终不可行（schema §3.7、D-6）。
func TestAuditPurgeSwitchIsGone(t *testing.T) {
	e := newEnv(t)
	e.auditEntry(coreaudit.ActionUserCreate, map[string]any{"field": "role"})

	conn, err := pgx.Connect(e.ctx, e.dsn)
	mustNotError(t, err, "open raw connection")
	defer func() { _ = conn.Close(context.Background()) }()

	for _, statement := range []string{"DELETE FROM audit_logs", "TRUNCATE audit_logs"} {
		tx, err := conn.Begin(e.ctx)
		mustNotError(t, err, "begin transaction")
		if _, err := tx.Exec(e.ctx, "SET LOCAL yiban.audit_purge = 'on'"); err != nil {
			_ = tx.Rollback(context.Background())
			t.Fatalf("SET LOCAL yiban.audit_purge: %v", err)
		}
		if _, err := tx.Exec(e.ctx, statement); err == nil {
			_ = tx.Rollback(context.Background())
			t.Fatalf("%q was accepted with yiban.audit_purge=on; the switch is still a backdoor", statement)
		}
		if err := tx.Rollback(e.ctx); err != nil {
			t.Fatalf("rollback: %v", err)
		}
	}
	if rows := e.scalarInt("SELECT count(*) FROM audit_logs"); rows != 1 {
		t.Fatalf("audit row count is %d after the purge attempts; want 1", rows)
	}
}

// --- 审计链：正常追加，篡改可发现 ----------------------------------------

func TestAuditChainVerifiesAndDetectsTamper(t *testing.T) {
	e := newEnv(t)
	e.auditEntry(coreaudit.ActionUserCreate, map[string]any{"field": "role"})
	e.auditEntry(coreaudit.ActionSchoolCreate, map[string]any{"field": "code"})
	e.auditEntry(coreaudit.ActionAdminGrant, map[string]any{"field": "user_id"})

	report, err := e.audits.Verify(e.ctx, 0, 0)
	mustNotError(t, err, "verify a three-row chain")
	if report.Count != 3 {
		t.Fatalf("verified %d rows, want 3", report.Count)
	}

	// 绕过链插入的行列形状正确，哈希错误。
	// INSERT 是允许的；只有 UPDATE 与 DELETE 被拦。
	_, err = e.db.Exec(e.ctx, `
		INSERT INTO audit_logs (ts, actor_label, action, detail, prev_hash, hash)
		VALUES (now(), 'master', 'user.create', '{}', $1, $2)`,
		make([]byte, 32), bytes.Repeat([]byte{0x11}, 32))
	mustNotError(t, err, "insert a forged audit row")

	if _, err := e.audits.Verify(e.ctx, 0, 0); err == nil {
		t.Fatal("verify accepted a chain with a forged row")
	}
}

func TestAuditDetailRejectsPersonalData(t *testing.T) {
	e := newEnv(t)
	err := e.db.InTx(e.ctx, func(ctx context.Context) error {
		_, err := e.audits.Append(ctx, coreaudit.Entry{
			TS:         time.Now().UTC(),
			ActorLabel: "master",
			Action:     coreaudit.ActionUserCreate,
			Detail:     map[string]any{"reason": "called 13800138000"},
		})
		return err
	})
	wantError(t, err, "audit detail carrying a phone number")
	if rows := e.scalarInt("SELECT count(*) FROM audit_logs"); rows != 0 {
		t.Fatalf("rejected audit write left %d rows", rows)
	}
}

// TestAuditAppendStaysInsideCallerTransaction 钉住审计追加的事务边界。
//
// 两条不变式：
// 一，无事务调用必须报错，且不写行；
// 二，事务回滚后不得留下审计行。
// 用例先做一次提交的追加，证明追加确实落库；
// 再让第二次追加随事务回滚，行数必须回到提交后的值。
func TestAuditAppendStaysInsideCallerTransaction(t *testing.T) {
	e := newEnv(t)
	entry := coreaudit.Entry{
		TS:         time.Now().UTC(),
		ActorLabel: "master",
		Action:     coreaudit.ActionUserCreate,
		Detail:     map[string]any{"field": "role"},
	}

	if _, err := e.audits.Append(e.ctx, entry); !errors.Is(err, pg.ErrNoTransaction) {
		t.Fatalf("Append outside a transaction = %v, want ErrNoTransaction", err)
	}
	if rows := e.scalarInt("SELECT count(*) FROM audit_logs"); rows != 0 {
		t.Fatalf("rejected audit write left %d rows, want 0", rows)
	}

	mustNotError(t, e.db.InTx(e.ctx, func(ctx context.Context) error {
		_, err := e.audits.Append(ctx, entry)
		return err
	}), "append inside a committed transaction")
	committed := e.scalarInt("SELECT count(*) FROM audit_logs")
	if committed != 1 {
		t.Fatalf("audit rows after a committed append = %d, want 1", committed)
	}

	boom := errors.New("force rollback")
	err := e.db.InTx(e.ctx, func(ctx context.Context) error {
		if _, err := e.audits.Append(ctx, entry); err != nil {
			return err
		}
		return boom
	})
	if !errors.Is(err, boom) {
		t.Fatalf("rolled-back transaction returned %v, want the injected error", err)
	}
	if after := e.scalarInt("SELECT count(*) FROM audit_logs"); after != committed {
		t.Fatalf("audit row escaped a rolled-back transaction: before=%d after=%d", committed, after)
	}
}

// --- 会话 -----------------------------------------------------------------

func TestRevokeAllForUserRevokesOnlyThatUser(t *testing.T) {
	e := newEnv(t)
	alice := e.createUser("alice@example.com")
	bob := e.createUser("bob@example.com")
	now := time.Now().UTC()

	e.newSession(alice.ID, "alice-1", now)
	e.newSession(alice.ID, "alice-2", now.Add(time.Minute))
	e.newSession(bob.ID, "bob-1", now)

	revoked, err := e.ses.RevokeAllForUser(e.ctx, alice.ID, now.Add(2*time.Minute))
	mustNotError(t, err, "revoke all sessions of one user")
	if revoked != 2 {
		t.Fatalf("revoked %d sessions, want 2", revoked)
	}

	for _, token := range []string{"alice-1", "alice-2"} {
		if _, err := e.ses.Resolve(e.ctx, tokenHash(token), now.Add(3*time.Minute), 30*time.Minute); codeOf(t, err) != errs.CodeNotFound {
			t.Fatalf("%s still resolves after RevokeAllForUser: %v", token, err)
		}
	}
	sess, err := e.ses.Resolve(e.ctx, tokenHash("bob-1"), now.Add(3*time.Minute), 30*time.Minute)
	mustNotError(t, err, "resolve bob's session")
	if sess.User.Email != "bob@example.com" || sess.Session.UserID != bob.ID {
		t.Fatalf("resolved the wrong session: %+v", sess)
	}
}

func TestSessionExpiryAndRevocationAreNotResolvable(t *testing.T) {
	e := newEnv(t)
	user := e.createUser("session@example.com")
	now := time.Now().UTC()

	// 绝对过期时刻已过。
	if err := e.db.InTx(e.ctx, func(ctx context.Context) error {
		_, err := e.ses.Create(ctx, newExpiredSession(user.ID, "expired", now))
		return err
	}); err != nil {
		t.Fatalf("create expired session: %v", err)
	}
	if _, err := e.ses.Resolve(e.ctx, tokenHash("expired"), now, 30*time.Minute); codeOf(t, err) != errs.CodeNotFound {
		t.Fatalf("expired session resolved: %v", err)
	}
	deleted, err := e.ses.DeleteExpired(e.ctx, now, 10)
	mustNotError(t, err, "delete expired sessions")
	if deleted != 1 {
		t.Fatalf("cleanup deleted %d sessions; want 1", deleted)
	}

	// 空闲窗口：resolve 刷新 last_seen_at，刷新也延长窗口。
	// 31 分钟无请求**必须失败**。
	e.newSession(user.ID, "idle", now)
	if _, err := e.ses.Resolve(e.ctx, tokenHash("idle"), now.Add(10*time.Minute), 30*time.Minute); err != nil {
		t.Fatalf("session inside the idle window: %v", err)
	}
	if _, err := e.ses.Resolve(e.ctx, tokenHash("idle"), now.Add(39*time.Minute), 30*time.Minute); err != nil {
		t.Fatalf("sliding refresh did not extend the idle window: %v", err)
	}
	if _, err := e.ses.Resolve(e.ctx, tokenHash("idle"), now.Add(70*time.Minute), 30*time.Minute); codeOf(t, err) != errs.CodeNotFound {
		t.Fatalf("idle session resolved after the window: %v", err)
	}

	// 撤销立即关闭会话。
	e.newSession(user.ID, "revoked", now)
	mustNotError(t, e.ses.Revoke(e.ctx, tokenHash("revoked"), now.Add(time.Minute)), "revoke one session")
	if _, err := e.ses.Resolve(e.ctx, tokenHash("revoked"), now.Add(2*time.Minute), 30*time.Minute); codeOf(t, err) != errs.CodeNotFound {
		t.Fatalf("revoked session resolved: %v", err)
	}
}

func newExpiredSession(userID, token string, now time.Time) coreauthNewSession {
	return coreauthNewSession{
		UserID:        userID,
		TokenHash:     tokenHash(token),
		AuthMethod:    "password",
		Now:           now.Add(-2 * time.Hour),
		IdleExpiresAt: now.Add(-time.Minute),
		ExpiresAt:     now.Add(-time.Minute),
	}
}

// TestResolveSlidesIdleExpiry 钉住 §3.2：
// 成功解析把空闲截止按距上次请求的时间前移，
// 返回行自洽（idle <= absolute，last_seen 即本次请求时刻）。
func TestResolveSlidesIdleExpiry(t *testing.T) {
	e := newEnv(t)
	user := e.createUser("idle-slide@example.com")
	now := time.Now().UTC().Truncate(time.Microsecond)

	created := e.newSession(user.ID, "slide", now)
	if !created.IdleExpiresAt.Equal(now.Add(30 * time.Minute)) {
		t.Fatalf("fresh session idle expiry is %s; want %s", created.IdleExpiresAt, now.Add(30*time.Minute))
	}

	at := now.Add(20 * time.Minute)
	resolved, err := e.ses.Resolve(e.ctx, tokenHash("slide"), at, 30*time.Minute)
	mustNotError(t, err, "resolve twenty minutes after creation")
	session := resolved.Session
	if !session.LastSeenAt.Equal(at) {
		t.Fatalf("last_seen_at is %s; want %s", session.LastSeenAt, at)
	}
	if want := at.Add(30 * time.Minute); !session.IdleExpiresAt.Equal(want) {
		t.Fatalf("idle_expires_at is %s; want %s (now + idle window)", session.IdleExpiresAt, want)
	}
	if session.IdleExpiresAt.After(session.ExpiresAt) {
		t.Fatalf("idle expiry %s is after absolute expiry %s", session.IdleExpiresAt, session.ExpiresAt)
	}

	// 空闲截止会越过绝对过期时，封顶保证
	// CHECK idle_expires_at <= expires_at 恒成立。
	mustNotError(t, e.db.InTx(e.ctx, func(ctx context.Context) error {
		_, err := e.ses.Create(ctx, coreauth.NewSessionRow{
			UserID:        user.ID,
			TokenHash:     tokenHash("cap-slide"),
			AuthMethod:    "password",
			Now:           now,
			IdleExpiresAt: now.Add(30 * time.Minute),
			ExpiresAt:     now.Add(30 * time.Minute),
		})
		return err
	}), "create a session whose absolute expiry equals the idle expiry")
	resolved, err = e.ses.Resolve(e.ctx, tokenHash("cap-slide"), at, 30*time.Minute)
	mustNotError(t, err, "resolve a session near its absolute expiry")
	if !resolved.Session.IdleExpiresAt.Equal(resolved.Session.ExpiresAt) {
		t.Fatalf("idle expiry is %s; want the absolute cap %s",
			resolved.Session.IdleExpiresAt, resolved.Session.ExpiresAt)
	}
}

func TestNewSessionEvictsTheOldestBeyondTheCap(t *testing.T) {
	e := newEnv(t)
	user := e.createUser("cap-sessions@example.com")
	now := time.Now().UTC()

	for i := 0; i < 21; i++ {
		e.newSession(user.ID, fmt.Sprintf("cap-%02d", i), now.Add(time.Duration(i)*time.Minute))
	}
	live := e.scalarInt("SELECT count(*) FROM sessions WHERE user_id = $1 AND revoked_at IS NULL", user.ID)
	if live != 20 {
		t.Fatalf("live session count is %d; want the cap of 20", live)
	}
	if _, err := e.ses.Resolve(e.ctx, tokenHash("cap-00"), now.Add(25*time.Minute), 30*time.Minute); codeOf(t, err) != errs.CodeNotFound {
		t.Fatalf("oldest session was not evicted: %v", err)
	}
	if _, err := e.ses.Resolve(e.ctx, tokenHash("cap-20"), now.Add(25*time.Minute), 30*time.Minute); err != nil {
		t.Fatalf("newest session was evicted: %v", err)
	}
}

// --- 密码重置 -------------------------------------------------------------

func TestPasswordResetTokenIsSingleUse(t *testing.T) {
	e := newEnv(t)
	user := e.createUser("reset@example.com")
	now := time.Now().UTC()

	if err := e.db.InTx(e.ctx, func(ctx context.Context) error {
		_, err := e.resets.Create(ctx, coreauthNewReset{
			UserID:    user.ID,
			TokenHash: tokenHash("reset-ok"),
			Now:       now,
			ExpiresAt: now.Add(time.Hour),
		})
		return err
	}); err != nil {
		t.Fatalf("create reset token: %v", err)
	}

	used, err := e.resets.Consume(e.ctx, tokenHash("reset-ok"), now.Add(time.Minute))
	mustNotError(t, err, "first use of the reset token")
	if used.UsedAt == nil {
		t.Fatal("consumed reset row has no used_at")
	}
	if _, err := e.resets.Consume(e.ctx, tokenHash("reset-ok"), now.Add(2*time.Minute)); codeOf(t, err) != errs.CodeNotFound {
		t.Fatalf("a reset token was accepted twice: %v", err)
	}

	// 过期令牌一次也不可用。
	if err := e.db.InTx(e.ctx, func(ctx context.Context) error {
		_, err := e.resets.Create(ctx, coreauthNewReset{
			UserID:    user.ID,
			TokenHash: tokenHash("reset-expired"),
			Now:       now.Add(-2 * time.Hour),
			ExpiresAt: now.Add(-time.Hour),
		})
		return err
	}); err != nil {
		t.Fatalf("create expired reset token: %v", err)
	}
	if _, err := e.resets.Consume(e.ctx, tokenHash("reset-expired"), now); codeOf(t, err) != errs.CodeNotFound {
		t.Fatalf("an expired reset token was accepted: %v", err)
	}
}

// --- 账号 -----------------------------------------------------------------

func TestSoftDeleteAndRestoreRespectCapacity(t *testing.T) {
	e := newEnv(t)
	owner := e.createUser("restore@example.com")
	school := e.createSchool("restore-school")

	create := func(phone string, limit int) (coreaccount.Record, error) {
		var rec coreaccount.Record
		err := e.db.InTx(e.ctx, func(ctx context.Context) error {
			var err error
			rec, err = e.accts.Create(ctx, coreaccount.New{
				SchoolID:    school.ID,
				OwnerUserID: owner.ID,
				Phone:       phone,
			}, limit, 10000)
			return err
		})
		return rec, err
	}

	first, err := create("13800138001", 2)
	mustNotError(t, err, "create first account")
	second, err := create("13800138002", 2)
	mustNotError(t, err, "create second account")
	if _, err := create("13800138003", 2); codeOf(t, err) != errs.CodeCapacity {
		t.Fatalf("third account at the cap: want capacity_exceeded, got %v", err)
	}

	// 软删行同时退出容量计数与手机号唯一索引。
	mustNotError(t, e.db.InTx(e.ctx, func(ctx context.Context) error {
		return e.accts.SoftDelete(ctx, corescope.Master(), first.ID, owner.ID, time.Now().UTC())
	}), "soft delete first account")
	reused, err := create("13800138001", 2)
	mustNotError(t, err, "reuse a soft-deleted phone number")

	// 恢复该行会突破上限，**必须失败**。
	restore := func(id string) (coreaccount.Record, error) {
		var rec coreaccount.Record
		err := e.db.InTx(e.ctx, func(ctx context.Context) error {
			var err error
			rec, err = e.accts.Restore(ctx, corescope.Master(), id, 2, 10000)
			return err
		})
		return rec, err
	}
	if _, err := restore(first.ID); codeOf(t, err) != errs.CodeCapacity {
		t.Fatalf("restore beyond the cap: want capacity_exceeded, got %v", err)
	}

	// 腾出一个名额，恢复才会成功。
	mustNotError(t, e.db.InTx(e.ctx, func(ctx context.Context) error {
		return e.accts.SoftDelete(ctx, corescope.Master(), reused.ID, owner.ID, time.Now().UTC())
	}), "soft delete reused account")
	restored, err := restore(first.ID)
	mustNotError(t, err, "restore after freeing a slot")
	if restored.ID != first.ID || restored.DeletedAt != nil {
		t.Fatalf("restore returned %+v", restored)
	}
	if _, err := e.accts.Get(e.ctx, corescope.Master(), second.ID); err != nil {
		t.Fatalf("second account missing: %v", err)
	}
}

func TestCredentialSchoolMustMatchAccount(t *testing.T) {
	e := newEnv(t)
	owner := e.createUser("cred@example.com")
	schoolA := e.createSchool("cred-a")
	schoolB := e.createSchool("cred-b")
	account := e.createAccount(schoolA.ID, owner.ID, "13900139000")

	cred := coreaccount.Credential{
		AccountID:  account.ID,
		SchoolID:   schoolB.ID,
		Alg:        "AES-256-GCM",
		KID:        "0123456789abcdef",
		AADVersion: 1,
		Nonce:      bytes.Repeat([]byte{0x01}, 12),
		Ciphertext: bytes.Repeat([]byte{0x02}, 32),
		Version:    1,
	}
	if err := e.db.InTx(e.ctx, func(ctx context.Context) error {
		return e.accts.InsertCredential(ctx, cred)
	}); err == nil {
		t.Fatal("a credential for another school was accepted")
	}

	cred.SchoolID = schoolA.ID
	mustNotError(t, e.db.InTx(e.ctx, func(ctx context.Context) error {
		return e.accts.InsertCredential(ctx, cred)
	}), "insert a credential for the owning school")

	got, err := e.accts.Credential(e.ctx, corescope.Master(), account.ID)
	mustNotError(t, err, "read the credential")
	if !bytes.Equal(got.Ciphertext, cred.Ciphertext) || got.KID != cred.KID {
		t.Fatalf("credential round trip mismatch: %+v", got)
	}

	cred.Version = 2
	mustNotError(t, e.db.InTx(e.ctx, func(ctx context.Context) error {
		return e.accts.ReplaceCredential(ctx, cred, 1)
	}), "replace a credential with the current version")

	cred.Version = 3
	if err := e.db.InTx(e.ctx, func(ctx context.Context) error {
		return e.accts.ReplaceCredential(ctx, cred, 1)
	}); codeOf(t, err) != errs.CodeConflict {
		t.Fatalf("stale credential version: want conflict, got %v", err)
	}
}

// TestReplaceCredentialIncrementsVersion 钉住 §7.3：
// 语句自增存储版本，两个读到同一版本的写者不可能同时成功。
func TestReplaceCredentialIncrementsVersion(t *testing.T) {
	e := newEnv(t)
	owner := e.createUser("cred-race@example.com")
	school := e.createSchool("cred-race")
	account := e.createAccount(school.ID, owner.ID, "13300133000")

	cred := coreaccount.Credential{
		AccountID:  account.ID,
		SchoolID:   school.ID,
		Alg:        "AES-256-GCM",
		KID:        "0123456789abcdef",
		AADVersion: 1,
		Nonce:      bytes.Repeat([]byte{0x01}, 12),
		Ciphertext: bytes.Repeat([]byte{0x02}, 32),
		Version:    1,
	}
	mustNotError(t, e.db.InTx(e.ctx, func(ctx context.Context) error {
		return e.accts.InsertCredential(ctx, cred)
	}), "insert credential")

	storedVersion := func() int64 {
		return e.scalarInt("SELECT version FROM credentials WHERE account_id = $1::uuid", account.ID)
	}

	// 入参版本被忽略：传入 99，存储为 2。
	cred.Version = 99
	cred.Ciphertext = bytes.Repeat([]byte{0x03}, 32)
	mustNotError(t, e.db.InTx(e.ctx, func(ctx context.Context) error {
		return e.accts.ReplaceCredential(ctx, cred, 1)
	}), "replace a credential with the current version")
	if got := storedVersion(); got != 2 {
		t.Fatalf("stored version is %d after one replace; want 2", got)
	}

	// 两个独立连接池给出两条独立后端连接；
	// 同一池上的两个 goroutine 无法证明并发（AGENTS §32）。
	dbA, err := pg.Open(e.ctx, pg.Options{DSN: e.dsn, MaxConns: 1, StatementTimeout: 30 * time.Second})
	mustNotError(t, err, "open independent pool A")
	defer dbA.Close()
	dbB, err := pg.Open(e.ctx, pg.Options{DSN: e.dsn, MaxConns: 1, StatementTimeout: 30 * time.Second})
	mustNotError(t, err, "open independent pool B")
	defer dbB.Close()

	start := make(chan struct{})
	results := make(chan error, 2)
	for _, repo := range []*pg.AccountRepo{pg.NewAccountRepo(dbA), pg.NewAccountRepo(dbB)} {
		go func(repo *pg.AccountRepo) {
			<-start
			update := cred
			update.Ciphertext = bytes.Repeat([]byte{0x04}, 32)
			results <- repo.ReplaceCredential(context.Background(), update, 2)
		}(repo)
	}
	close(start)

	var succeeded, conflicted int
	for i := 0; i < 2; i++ {
		err := <-results
		switch {
		case err == nil:
			succeeded++
		case codeOf(t, err) == errs.CodeConflict:
			conflicted++
		default:
			t.Fatalf("unexpected replace error: %v (%+v)", err, errs.From(err))
		}
	}
	if succeeded != 1 || conflicted != 1 {
		t.Fatalf("want one success and one conflict, got %d succeeded and %d conflict", succeeded, conflicted)
	}
	if got := storedVersion(); got != 3 {
		t.Fatalf("stored version is %d after the race; want 3", got)
	}
}

func TestScopeFiltersAccounts(t *testing.T) {
	e := newEnv(t)
	alice := e.createUser("scope-alice@example.com")
	bob := e.createUser("scope-bob@example.com")
	schoolA := e.createSchool("scope-a")
	schoolB := e.createSchool("scope-b")
	inA := e.createAccount(schoolA.ID, alice.ID, "13700137001")
	inB := e.createAccount(schoolB.ID, bob.ID, "13700137002")

	for _, tc := range []struct {
		name    string
		scope   corescope.Scope
		visible map[string]bool
	}{
		{"master sees every school", corescope.Master(), map[string]bool{inA.ID: true, inB.ID: true}},
		{"admin of school A", corescope.Admin([]string{schoolA.ID}), map[string]bool{inA.ID: true}},
		{"admin of no school", corescope.Admin(nil), map[string]bool{}},
		{"owner alice", corescope.User(alice.ID), map[string]bool{inA.ID: true}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			for _, account := range []coreaccount.Record{inA, inB} {
				_, err := e.accts.Get(e.ctx, tc.scope, account.ID)
				visible := err == nil
				if visible != tc.visible[account.ID] {
					t.Fatalf("account %s visible=%v, want %v (err=%v)", account.ID, visible, tc.visible[account.ID], err)
				}
				if !visible && codeOf(t, err) != errs.CodeNotFound {
					t.Fatalf("out-of-scope account must be not_found, got %v", err)
				}
			}
		})
	}
}

// --- 连接池 ---------------------------------------------------------------

func TestPoolAppliesStatementTimeoutAndUTC(t *testing.T) {
	e := newEnv(t)
	timeout := e.scalarInt("SELECT setting::bigint FROM pg_settings WHERE name = 'statement_timeout'")
	if timeout != 10000 {
		t.Fatalf("statement_timeout is %d ms; want 10000 (from Options.StatementTimeout)", timeout)
	}
	zone, err := e.db.QueryString(e.ctx, "SHOW TimeZone")
	mustNotError(t, err, "read TimeZone")
	if zone != "UTC" {
		t.Fatalf("session time zone is %q; want UTC", zone)
	}
}

// --- 上文用到的编译期别名 -------------------------------------------------

// coreauthNewSession 与 coreauthNewReset 缩短会话和重置辅助调用，
// 同时仍点名生产代码所用的 core 类型。
type coreauthNewSession = coreauth.NewSessionRow
type coreauthNewReset = coreauth.NewPasswordReset

// --- 生命周期与绑定 -------------------------------------------------------

func TestDisabledAndDeletedUserSessionsDoNotResolve(t *testing.T) {
	e := newEnv(t)
	user := e.createUser("Lifecycle@Example.com")
	now := time.Now().UTC()

	byEmail, err := e.users.GetByEmail(e.ctx, "  lifecycle@example.com  ")
	mustNotError(t, err, "read the user by normalized email")
	if byEmail.ID != user.ID {
		t.Fatalf("GetByEmail returned %s; want %s", byEmail.ID, user.ID)
	}
	e.newSession(user.ID, "life", now)

	if _, err := e.ses.Resolve(e.ctx, tokenHash("life"), now.Add(time.Minute), 30*time.Minute); err != nil {
		t.Fatalf("live session of an active user: %v", err)
	}
	mustNotError(t, e.users.SetStatus(e.ctx, user.ID, coreauth.StatusDisabled, now), "disable user")
	if _, err := e.ses.Resolve(e.ctx, tokenHash("life"), now.Add(2*time.Minute), 30*time.Minute); codeOf(t, err) != errs.CodeNotFound {
		t.Fatalf("session of a disabled user resolved: %v", err)
	}

	mustNotError(t, e.users.UpdatePassword(e.ctx, user.ID, "argon2id$new", false, now), "update password")
	updated, err := e.users.GetByID(e.ctx, user.ID)
	mustNotError(t, err, "read updated user")
	if updated.PasswordHash != "argon2id$new" || updated.MustChangePassword {
		t.Fatalf("password update not stored: %+v", updated)
	}
	mustNotError(t, e.users.MarkLogin(e.ctx, user.ID, now), "mark login")

	mustNotError(t, e.users.SoftDelete(e.ctx, user.ID, user.ID, now), "soft delete user")
	if _, err := e.users.GetByID(e.ctx, user.ID); codeOf(t, err) != errs.CodeNotFound {
		t.Fatalf("deleted user is still readable: %v", err)
	}
}

func TestSchoolAndAdminBindings(t *testing.T) {
	e := newEnv(t)
	school := e.createSchool("alpha")

	if _, err := e.schools.Create(e.ctx, coreschool.New{Code: "alpha", Name: "Duplicate"}); codeOf(t, err) != errs.CodeConflict {
		t.Fatalf("duplicate school code: want conflict, got %v", err)
	}

	name := "Renamed School"
	status := coreschool.StatusDisabled
	updated, err := e.schools.Update(e.ctx, school.ID, coreschool.Update{Name: &name, Status: &status})
	mustNotError(t, err, "update school")
	if updated.Name != name || updated.Status != coreschool.StatusDisabled || updated.Code != "alpha" {
		t.Fatalf("school update not applied: %+v", updated)
	}
	byCode, err := e.schools.GetByCode(e.ctx, "alpha")
	mustNotError(t, err, "read school by code")
	if byCode.ID != school.ID {
		t.Fatalf("GetByCode returned %s; want %s", byCode.ID, school.ID)
	}

	admin := e.createUser("admin@example.com")
	binding := coreschool.AdminBinding{UserID: admin.ID, SchoolID: school.ID}
	mustNotError(t, e.admins.Grant(e.ctx, binding), "grant admin")
	mustNotError(t, e.admins.Grant(e.ctx, binding), "grant admin twice")
	ids, err := e.admins.SchoolsForUser(e.ctx, admin.ID)
	mustNotError(t, err, "list admin schools")
	if len(ids) != 1 || ids[0] != school.ID {
		t.Fatalf("admin schools are %v; want [%s]", ids, school.ID)
	}
	mustNotError(t, e.admins.Revoke(e.ctx, admin.ID, school.ID), "revoke admin")
	ids, err = e.admins.SchoolsForUser(e.ctx, admin.ID)
	mustNotError(t, err, "list admin schools after revoke")
	if len(ids) != 0 {
		t.Fatalf("admin still bound to %v", ids)
	}
	if err := e.admins.Revoke(e.ctx, admin.ID, school.ID); codeOf(t, err) != errs.CodeNotFound {
		t.Fatalf("second revoke: want not_found, got %v", err)
	}
}

func TestMigrateRefusesNewerDatabase(t *testing.T) {
	e := newEnv(t)
	if _, err := e.db.Exec(e.ctx,
		"INSERT INTO goose_db_version (version_id, is_applied) VALUES (999, true)"); err != nil {
		t.Fatalf("insert a future version: %v", err)
	}
	if _, err := pg.Migrate(e.ctx, e.dsn); !errors.Is(err, pg.ErrDatabaseAhead) {
		t.Fatalf("a newer database must stop the binary; got %v", err)
	}
}

func TestAccountStatusCompareAndSet(t *testing.T) {
	e := newEnv(t)
	owner := e.createUser("cas@example.com")
	school := e.createSchool("cas-school")
	account := e.createAccount(school.ID, owner.ID, "13600136000")
	master := corescope.Master()
	now := time.Now().UTC()

	active, err := e.accts.SetStatus(e.ctx, master, account.ID,
		coreaccount.StatusPending, coreaccount.StatusActive, now, "")
	mustNotError(t, err, "activate a pending account")
	if active.Status != coreaccount.StatusActive {
		t.Fatalf("status is %s; want active", active.Status)
	}

	if _, err := e.accts.SetStatus(e.ctx, master, account.ID,
		coreaccount.StatusPending, coreaccount.StatusActive, now, ""); codeOf(t, err) != errs.CodeConflict {
		t.Fatalf("repeated transition: want conflict, got %v", err)
	}
	if _, err := e.accts.SetStatus(e.ctx, master, account.ID,
		coreaccount.StatusActive, coreaccount.StatusRejected, now, "  "); codeOf(t, err) != errs.CodeValidation {
		t.Fatalf("reject without a reason: want validation, got %v", err)
	}
	rejected, err := e.accts.SetStatus(e.ctx, master, account.ID,
		coreaccount.StatusActive, coreaccount.StatusRejected, now, "photo rejected")
	mustNotError(t, err, "reject with a reason")
	if rejected.Status != coreaccount.StatusRejected || rejected.RejectReason != "photo rejected" {
		t.Fatalf("reject result is %+v", rejected)
	}
}
