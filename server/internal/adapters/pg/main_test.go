package pg_test

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/pg"
	coreaccount "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/account"
	coreaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/audit"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	coreschool "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/school"
	coreuser "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/user"
)

// dsnEnv names the test database. The tests need a real PostgreSQL 18 server:
// triggers, partial indexes, advisory locks and uuidv7() have no honest
// in-process substitute (AGENTS §32).
const dsnEnv = "YIBAN_TEST_DATABASE_URL"

// auditKey is a fixed 32-byte key. A real key comes from configuration.
var auditKey = []byte("0123456789abcdef0123456789abcdef")

// testEnv owns one database schema. Every test gets an empty schema, so runs
// never depend on each other or on leftovers from a crashed run.
type testEnv struct {
	t       *testing.T
	ctx     context.Context
	base    string
	dsn     string
	schema  string
	admin   *pg.DB
	db      *pg.DB
	users   *pg.UserRepo
	ses     *pg.SessionRepo
	schools *pg.SchoolRepo
	admins  *pg.AdminSchoolRepo
	accts   *pg.AccountRepo
	audits  *pg.AuditRepo
	resets  *pg.PasswordResetRepo
}

func requireDSN(t *testing.T) string {
	t.Helper()
	dsn := strings.TrimSpace(os.Getenv(dsnEnv))
	if dsn == "" {
		t.Skip("SKIP: " + dsnEnv + " is not set. PostgreSQL e2e did NOT run. " +
			"An unset database is never a green result.")
	}
	return dsn
}

func newEnv(t *testing.T) *testEnv {
	t.Helper()
	base := requireDSN(t)
	ctx := t.Context()
	schema := "pgtest_" + randomHex(8)

	admin, err := pg.Open(ctx, pg.Options{DSN: base, MaxConns: 1, StatementTimeout: 30 * time.Second})
	if err != nil {
		t.Fatalf("open admin connection: %v", err)
	}
	if _, err := admin.Exec(ctx, "CREATE SCHEMA "+schema); err != nil {
		admin.Close()
		t.Fatalf("create schema %s: %v", schema, err)
	}
	dsn := withSchema(base, schema)
	db, err := pg.Open(ctx, pg.Options{DSN: dsn, MaxConns: 4, StatementTimeout: 10 * time.Second})
	if err != nil {
		dropSchema(t, admin, schema)
		admin.Close()
		t.Fatalf("open test connection: %v", err)
	}
	e := &testEnv{
		t:      t,
		ctx:    ctx,
		base:   base,
		dsn:    dsn,
		schema: schema,
		admin:  admin,
		db:     db,
	}
	e.users = pg.NewUserRepo(db)
	e.ses = pg.NewSessionRepo(db)
	e.schools = pg.NewSchoolRepo(db)
	e.admins = pg.NewAdminSchoolRepo(db)
	e.accts = pg.NewAccountRepo(db)
	e.resets = pg.NewPasswordResetRepo(db)
	e.audits, err = pg.NewAuditRepo(db, auditKey)
	if err != nil {
		t.Fatalf("new audit repo: %v", err)
	}
	t.Cleanup(func() {
		db.Close()
		dropSchema(t, admin, schema)
		admin.Close()
	})
	if _, err := pg.Migrate(ctx, dsn); err != nil {
		t.Fatalf("migrate test schema: %v", err)
	}
	return e
}

func dropSchema(t *testing.T, admin *pg.DB, schema string) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	if _, err := admin.Exec(ctx, "DROP SCHEMA IF EXISTS "+schema+" CASCADE"); err != nil {
		t.Logf("drop schema %s: %v", schema, err)
	}
}

func withSchema(dsn, schema string) string {
	if strings.Contains(dsn, "://") {
		sep := "?"
		if strings.Contains(dsn, "?") {
			sep = "&"
		}
		return dsn + sep + "search_path=" + schema
	}
	return dsn + " search_path=" + schema
}

func randomHex(n int) string {
	b := make([]byte, n)
	if _, err := rand.Read(b); err != nil {
		panic("pg tests: cannot read random bytes: " + err.Error())
	}
	return hex.EncodeToString(b)
}

// tokenHash builds the typed digest the repositories accept. A raw token
// cannot be passed to a token_hash parameter (D-7).
func tokenHash(token string) coreauth.TokenHash {
	return coreauth.HashToken(token)
}

func (e *testEnv) createUser(email string) coreuser.Record {
	e.t.Helper()
	var rec coreuser.Record
	err := e.db.InTx(e.ctx, func(ctx context.Context) error {
		var err error
		rec, err = e.users.Create(ctx, coreuser.New{
			Email:        email,
			PasswordHash: "argon2id$test",
			Role:         coreauth.RoleUser,
		}, 10000)
		return err
	})
	if err != nil {
		e.t.Fatalf("create user %s: %v", email, err)
	}
	return rec
}

func (e *testEnv) createSchool(code string) coreschool.Record {
	e.t.Helper()
	rec, err := e.schools.Create(e.ctx, coreschool.New{Code: code, Name: "School " + code})
	if err != nil {
		e.t.Fatalf("create school %s: %v", code, err)
	}
	return rec
}

func (e *testEnv) createAccount(schoolID, ownerID, phone string) coreaccount.Record {
	e.t.Helper()
	var rec coreaccount.Record
	err := e.db.InTx(e.ctx, func(ctx context.Context) error {
		var err error
		rec, err = e.accts.Create(ctx, coreaccount.New{
			SchoolID:    schoolID,
			OwnerUserID: ownerID,
			Phone:       phone,
		}, 10000)
		return err
	})
	if err != nil {
		e.t.Fatalf("create account %s: %v", phone, err)
	}
	return rec
}

func (e *testEnv) newSession(userID, token string, now time.Time) coreauth.SessionRow {
	e.t.Helper()
	var rec coreauth.SessionRow
	err := e.db.InTx(e.ctx, func(ctx context.Context) error {
		var err error
		rec, err = e.ses.Create(ctx, coreauth.NewSessionRow{
			UserID:        userID,
			TokenHash:     tokenHash(token),
			AuthMethod:    "password",
			Now:           now,
			IdleExpiresAt: now.Add(30 * time.Minute),
			ExpiresAt:     now.Add(12 * time.Hour),
			IPHash:        coreauth.HashSecret("127.0.0.1"),
			UserAgentHash: coreauth.HashSecret("test-agent"),
		})
		return err
	})
	if err != nil {
		e.t.Fatalf("create session %s: %v", token, err)
	}
	return rec
}

func (e *testEnv) scalarInt(sql string, args ...any) int64 {
	e.t.Helper()
	v, err := e.db.QueryInt64(e.ctx, sql, args...)
	if err != nil {
		e.t.Fatalf("query %q: %v", sql, err)
	}
	return v
}

func (e *testEnv) auditEntry(action coreaudit.Action, detail map[string]any) coreaudit.Row {
	e.t.Helper()
	var row coreaudit.Row
	err := e.db.InTx(e.ctx, func(ctx context.Context) error {
		var err error
		row, err = e.audits.Append(ctx, coreaudit.Entry{
			TS:         time.Now().UTC(),
			ActorLabel: "a***@example.com",
			Action:     action,
			TargetType: "user",
			TargetID:   "target-1",
			Detail:     detail,
		})
		return err
	})
	if err != nil {
		e.t.Fatalf("append audit %s: %v", action, err)
	}
	return row
}

func wantError(t *testing.T, err error, what string) {
	t.Helper()
	if err == nil {
		t.Fatalf("%s: want an error, got nil", what)
	}
}

func mustNotError(t *testing.T, err error, what string) {
	t.Helper()
	if err != nil {
		t.Fatalf("%s: %v", what, err)
	}
}

// TestMain makes a missing database explicit. `go test` hides t.Skip logs
// unless -v is set, so an environment-skipped database run must never look
// green silently (AGENTS §30).
//
// In CI a missing DSN is a failure: the pipeline would otherwise report a
// green run in which zero e2e tests executed. Locally the banner goes to
// stdout, where the operator still sees it, and the run skips.
func TestMain(m *testing.M) {
	if strings.TrimSpace(os.Getenv(dsnEnv)) == "" {
		if strings.TrimSpace(os.Getenv("CI")) != "" {
			fmt.Fprintf(os.Stderr, "\nFAIL: %s is not set in CI. The PostgreSQL e2e tests "+
				"in this package DID NOT RUN. An unset database is never a green result.\n\n", dsnEnv)
			os.Exit(1)
		}
		fmt.Fprintf(os.Stdout, "\n"+
			"**********************************************************************\n"+
			"* SKIP: %s is not set.\n"+
			"* The PostgreSQL e2e tests in this package DID NOT RUN.\n"+
			"* An unset database is never a green result.\n"+
			"**********************************************************************\n\n", dsnEnv)
	}
	os.Exit(m.Run())
}
