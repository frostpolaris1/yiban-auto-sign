package main

// user create-master 的真库 e2e（P7 审查 P2-1）。
//
// # 功能
// 干净库上建第一个 master：退出码、库内角色、口令可校验、
// 审计行存在、输出不含完整邮箱、重复建号冲突。
//
// # 口径
// 缺 DSN 时打可见原因跳过；这是「部署完能不能登录」的唯一入口。

import (
	"context"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/pg"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
)

const cliTestPepper = "0123456789abcdef0123456789abcdef"

func TestUserCreateMasterE2E(t *testing.T) {
	base := strings.TrimSpace(os.Getenv("YIBAN_TEST_DATABASE_URL"))
	if base == "" {
		t.Skip("SKIP: YIBAN_TEST_DATABASE_URL is not set. create-master e2e did NOT run.")
	}
	ctx := context.Background()
	schema := "usrtest_" + cliRandomHex(8)
	admin, err := pg.Open(ctx, pg.Options{DSN: base, MaxConns: 1, StatementTimeout: 30 * time.Second})
	if err != nil {
		t.Fatalf("open admin: %v", err)
	}
	defer admin.Close()
	if _, err := admin.Exec(ctx, "CREATE SCHEMA "+schema); err != nil {
		t.Fatalf("create schema: %v", err)
	}
	t.Cleanup(func() {
		dropCtx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
		defer cancel()
		_, _ = admin.Exec(dropCtx, "DROP SCHEMA IF EXISTS "+schema+" CASCADE")
	})
	dsn := withSearchPathQS(base, schema)
	if _, err := pg.Migrate(ctx, dsn); err != nil {
		t.Fatalf("migrate: %v", err)
	}

	// 口令/pepper 缺失与过短：用法错误 2，不连库。
	if _, code := runCLI(t, []string{"user", "create-master", "--dsn", dsn}); code != 2 {
		t.Fatalf("缺 email/password code = %d, want 2", code)
	}
	if _, code := runCLI(t, []string{"user", "create-master", "--email", "boss@example.com",
		"--password", "TestPass123!", "--dsn", dsn, "--pepper", "short"}); code != 2 {
		t.Fatalf("pepper 过短 code = %d, want 2", code)
	}

	// 正常创建。
	out, code := runCLI(t, []string{"user", "create-master", "--email", "Boss@Example.com",
		"--password", "TestPass123!", "--dsn", dsn, "--pepper", cliTestPepper})
	if code != 0 {
		t.Fatalf("create-master code = %d, out=%s", code, out)
	}
	if strings.Contains(out, "Boss@Example.com") || strings.Contains(out, "boss@example.com") {
		t.Fatalf("CLI 输出泄漏完整邮箱: %s", out)
	}

	db, err := pg.Open(ctx, pg.Options{DSN: dsn, MaxConns: 2, StatementTimeout: 10 * time.Second})
	if err != nil {
		t.Fatalf("open schema db: %v", err)
	}
	defer db.Close()
	role, err := db.QueryString(ctx, "SELECT role::text FROM users WHERE email = $1", "boss@example.com")
	if err != nil {
		t.Fatalf("query role: %v", err)
	}
	if role != "master" {
		t.Fatalf("role = %q, want master", role)
	}
	hash, err := db.QueryString(ctx, "SELECT password_hash FROM users WHERE email = $1", "boss@example.com")
	if err != nil {
		t.Fatalf("query hash: %v", err)
	}
	check, err := coreauth.VerifyPassword("TestPass123!", hash)
	if err != nil || !check.Valid {
		t.Fatalf("口令不可校验: %+v, %v", check, err)
	}
	n, err := db.QueryInt64(ctx,
		"SELECT count(*) FROM audit_logs WHERE action = 'user.create' AND target_id::text = (SELECT id::text FROM users WHERE email = $1)",
		"boss@example.com")
	if err != nil {
		t.Fatalf("query audit: %v", err)
	}
	if n != 1 {
		t.Fatalf("审计行 = %d, want 1", n)
	}

	// 重复建号：冲突 1。
	if _, code := runCLI(t, []string{"user", "create-master", "--email", "boss@example.com",
		"--password", "TestPass123!", "--dsn", dsn, "--pepper", cliTestPepper}); code != 1 {
		t.Fatalf("重复建号 code = %d, want 1", code)
	}
}
