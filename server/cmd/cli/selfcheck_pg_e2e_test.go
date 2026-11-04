package main

// selfcheck 的真库 e2e：goose schema 版本与待执行迁移判据。
//
// # 口径
// 缺 YIBAN_TEST_DATABASE_URL 时显性 SKIP（环境不满足不是绿）。
// 每个用例建独立 schema 并在结束时删除；只读自检不写业务表。
// 待执行迁移用「建 schema 但不迁移」的真实空库验证。

import (
	"context"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/pg"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/app/updatecheck"
)

// selfcheckE2EDeps 返回生产依赖 + 临时目录输入。
func selfcheckE2EOptions(t *testing.T, dsn string) updatecheck.Options {
	t.Helper()
	return updatecheck.Options{
		DSN:           dsn,
		HTTPAddr:      "127.0.0.1:47183",
		PhotoDir:      t.TempDir(),
		BackupDir:     t.TempDir(),
		StatePath:     filepath.Join(t.TempDir(), "update-state.json"),
		CredentialKey: testCLICredentialKey,
		HashPepper:    testCLIPepper,
		Unit:          updatecheck.DefaultUnit,
	}
}

// createSelfcheckSchema 建独立 schema，返回带 search_path 的 DSN 与清理函数。
func createSelfcheckSchema(t *testing.T, base, prefix string) (string, func()) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	schema := prefix + "_" + cliRandomHex(8)
	admin, err := pg.Open(ctx, pg.Options{DSN: base, MaxConns: 1, StatementTimeout: 30 * time.Second})
	if err != nil {
		t.Fatalf("open admin: %v", err)
	}
	if _, err := admin.Exec(ctx, "CREATE SCHEMA "+schema); err != nil {
		admin.Close()
		t.Fatalf("create schema: %v", err)
	}
	cleanup := func() {
		dropCtx, dropCancel := context.WithTimeout(context.Background(), 30*time.Second)
		defer dropCancel()
		_, _ = admin.Exec(dropCtx, "DROP SCHEMA IF EXISTS "+schema+" CASCADE")
		admin.Close()
	}
	return withSearchPathQS(base, schema), cleanup
}

func TestSelfcheckReadsSchemaVersionFromPostgres(t *testing.T) {
	base := strings.TrimSpace(os.Getenv("YIBAN_TEST_DATABASE_URL"))
	if base == "" {
		t.Skip("SKIP: YIBAN_TEST_DATABASE_URL is not set. 自检真库 e2e 未跑。")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	dsn, cleanup := createSelfcheckSchema(t, base, "selfcheck")
	defer cleanup()
	if _, err := pg.Migrate(ctx, dsn); err != nil {
		t.Fatalf("migrate: %v", err)
	}
	embedded, err := pg.EmbeddedVersion()
	if err != nil {
		t.Fatalf("embedded version: %v", err)
	}

	rep, err := updatecheck.Run(ctx, newSelfcheckDeps(), selfcheckE2EOptions(t, dsn))
	if err != nil {
		t.Fatalf("Run: %v", err)
	}
	for _, name := range []string{
		"db.connect", "db.schema_version", "db.migrations_embedded", "db.migrations_pending",
	} {
		c := findCheck(t, rep, name)
		if c.Status != updatecheck.StatusPass {
			t.Fatalf("%s = %+v, want PASS\n%s", name, c, rep.Text())
		}
	}
	schema := findCheck(t, rep, "db.schema_version")
	if !strings.Contains(schema.Detail, strconv.FormatInt(embedded, 10)) {
		t.Fatalf("db.schema_version detail %q does not mention embedded version %d", schema.Detail, embedded)
	}
	body, err := rep.JSON()
	if err != nil {
		t.Fatalf("JSON: %v", err)
	}
	// 测试库的口令与库名同为 yiban_test；裸口令子串会合法出现在
	// config.dsn 的 host:port/dbname 里。这里只钉真正的泄漏形态。
	assertNoSensitiveDSN(t, string(body), base, dsn)
	assertNoSensitiveDSN(t, rep.Text(), base, dsn)
}

func TestSelfcheckDetectsPendingMigrations(t *testing.T) {
	base := strings.TrimSpace(os.Getenv("YIBAN_TEST_DATABASE_URL"))
	if base == "" {
		t.Skip("SKIP: YIBAN_TEST_DATABASE_URL is not set. 自检真库 e2e 未跑。")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	dsn, cleanup := createSelfcheckSchema(t, base, "selfcheck_empty")
	defer cleanup()

	rep, err := updatecheck.Run(ctx, newSelfcheckDeps(), selfcheckE2EOptions(t, dsn))
	if err != nil {
		t.Fatalf("Run: %v", err)
	}
	if c := findCheck(t, rep, "db.connect"); c.Status != updatecheck.StatusPass {
		t.Fatalf("db.connect = %+v", c)
	}
	schema := findCheck(t, rep, "db.schema_version")
	if schema.Status != updatecheck.StatusPass || !strings.Contains(schema.Detail, "尚未迁移") {
		t.Fatalf("db.schema_version = %+v", schema)
	}
	pending := findCheck(t, rep, "db.migrations_pending")
	if pending.Status != updatecheck.StatusFail || !strings.Contains(pending.Detail, "有待执行迁移") {
		t.Fatalf("db.migrations_pending = %+v", pending)
	}
	if rep.ExitCode() != 1 {
		t.Fatalf("exit = %d, want 1", rep.ExitCode())
	}
	assertNoSensitiveDSN(t, rep.Text(), base, dsn)
	body, err := rep.JSON()
	if err != nil {
		t.Fatalf("JSON: %v", err)
	}
	assertNoSensitiveDSN(t, string(body), base, dsn)
}

// findCheck 按名找检查行。
func findCheck(t *testing.T, rep updatecheck.Report, name string) updatecheck.Check {
	t.Helper()
	for _, c := range rep.Checks {
		if c.Name == name {
			return c
		}
	}
	t.Fatalf("check %q not found", name)
	return updatecheck.Check{}
}

// assertNoSensitiveDSN 断言输出不含 DSN 的敏感形态。
// 不按裸口令全串匹配：测试库 user/password/dbname 都是 yiban_test，
// 而 config.dsn 合法打印 host:port/dbname，裸子串必然命中。
// 钉四种泄漏形态：完整 DSN、password=、URI userinfo、user:pass@。
func assertNoSensitiveDSN(t *testing.T, out string, dsns ...string) {
	t.Helper()
	lower := strings.ToLower(out)
	if strings.Contains(lower, "password=") {
		t.Fatalf("output contains a password= field")
	}
	for _, dsn := range dsns {
		if dsn == "" {
			continue
		}
		if strings.Contains(out, dsn) {
			t.Fatalf("output echoes the full DSN")
		}
		u, err := url.Parse(dsn)
		if err != nil || u.User == nil {
			continue
		}
		if userinfo := u.User.String(); userinfo != "" && strings.Contains(out, userinfo+"@") {
			t.Fatalf("output contains URI userinfo")
		}
		pw, ok := u.User.Password()
		if !ok || pw == "" {
			continue
		}
		if strings.Contains(out, ":"+pw+"@") {
			t.Fatalf("output contains user:pass@ userinfo")
		}
		if encoded := url.QueryEscape(pw); encoded != pw && strings.Contains(out, ":"+encoded+"@") {
			t.Fatalf("output contains percent-encoded user:pass@ userinfo")
		}
	}
}
