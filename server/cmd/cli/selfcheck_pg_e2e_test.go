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
	if pw := dsnPassword(base); pw != "" && strings.Contains(string(body), pw) {
		t.Fatalf("JSON report leaks the database password")
	}
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

// dsnPassword 取测试 DSN 的口令，仅用于断言报告不泄漏。
func dsnPassword(dsn string) string {
	u, err := url.Parse(dsn)
	if err != nil || u.User == nil {
		return ""
	}
	pw, _ := u.User.Password()
	return pw
}
