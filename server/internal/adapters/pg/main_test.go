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
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
	coreuser "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/user"
)

// dsnEnv 是测试库的环境变量名。
// 测试需要真实 PostgreSQL 18：触发器、部分索引、advisory lock 与 uuidv7()
// 都没有**诚实的进程内替身**（AGENTS §32）。
const dsnEnv = "YIBAN_TEST_DATABASE_URL"

// auditKey 是固定 32 字节密钥。真实密钥来自配置。
var auditKey = []byte("0123456789abcdef0123456789abcdef")

// testPepper 是 IP 与 User-Agent 摘要的密钥。
var testPepper = []byte("fedcba9876543210fedcba9876543210")

// testEnv 独占一个数据库 schema。
// 每个测试拿到空 schema，故各次运行互不依赖，
// 也不依赖崩溃残留。
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
	sched   *pg.ScheduleRepo
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
	e.sched = pg.NewScheduleRepo(db)
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

// tokenHash 构造仓储接受的定型摘要。
// 原始令牌无法传给 token_hash 参数（D-7）。
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

// activateAccount 把账号审核为 active。排期 e2e 的 CAS 纵深判据
// （只领活跃账号）需要它；种子缺省是 pending。
func (e *testEnv) activateAccount(accountID string) {
	e.t.Helper()
	// reviewed_by 是 uuid 列：现建一个 master 用户当审核人（per-test schema 无内置管理员）。
	admin := e.createUser("reviewer-" + randomHex(4) + "@example.com")
	e.db.Exec(e.ctx, "UPDATE users SET role = 'master' WHERE id = $1", admin.ID)
	adminID := admin.ID
	if _, err := e.accts.Review(e.ctx, corescope.Master(), accountID, true, "", adminID, time.Now()); err != nil {
		e.t.Fatalf("activate account %s: %v", accountID, err)
	}
}

func (e *testEnv) newSession(userID, token string, now time.Time) coreauth.SessionRow {
	e.t.Helper()
	ipHash, hashErr := coreauth.HashIP(testPepper, "127.0.0.1")
	if hashErr != nil {
		e.t.Fatalf("HashIP: %v", hashErr)
	}
	uaHash, hashErr := coreauth.HashUserAgent(testPepper, "test-agent")
	if hashErr != nil {
		e.t.Fatalf("HashUserAgent: %v", hashErr)
	}
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
			IPHash:        ipHash,
			UserAgentHash: uaHash,
		})
		return err
	})
	if err != nil {
		e.t.Fatalf("create session %s: %v", token, err)
	}
	return rec
}

func (e *testEnv) scalarString(sql string, args ...any) string {
	e.t.Helper()
	v, err := e.db.QueryString(e.ctx, sql, args...)
	if err != nil {
		e.t.Fatalf("query %q: %v", sql, err)
	}
	return v
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

// TestMain 让缺失数据库显性可见。
// 未加 -v 时 go test 会吞掉 t.Skip 日志，
// 故因环境跳过的数据库运行**绝不可静默显得绿**（AGENTS §30）。
//
// CI 中缺少 DSN 即失败：否则流水线会报告一次零 e2e 执行的绿。
// 本地把横幅打到 stdout，操作者仍能看到，然后跳过。
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
