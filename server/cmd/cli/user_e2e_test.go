package main

// user create-master 的真库 e2e（P7 审查 P2-1；修复 P1-3）。
//
// # 功能
// 干净库上建第一个 master：退出码、库内角色、口令可校验、
// 审计行存在、输出不含完整邮箱、重复建号冲突。
// 口令走 stdin 时，argv 不得携带口令、DSN 或 pepper。
//
// # 口径
// 缺 DSN 时打可见原因跳过；这是「部署完能不能登录」的唯一入口。

import (
	"context"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/pg"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
)

const cliTestPepper = "0123456789abcdef0123456789abcdef"

// userTestSchema 建独立 schema 并迁移，返回该 schema 的 DSN。
func userTestSchema(t *testing.T) string {
	t.Helper()
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
	t.Cleanup(func() {
		dropCtx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
		defer cancel()
		_, _ = admin.Exec(dropCtx, "DROP SCHEMA IF EXISTS "+schema+" CASCADE")
		admin.Close()
	})
	if _, err := admin.Exec(ctx, "CREATE SCHEMA "+schema); err != nil {
		t.Fatalf("create schema: %v", err)
	}
	dsn := withSearchPathQS(base, schema)
	if _, err := pg.Migrate(ctx, dsn); err != nil {
		t.Fatalf("migrate: %v", err)
	}
	return dsn
}

func TestUserCreateMasterE2E(t *testing.T) {
	dsn := userTestSchema(t)
	ctx := context.Background()

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

// TestCreateMasterKeepsSecretOutOfArgv 钉住口令/DSN/pepper 不进 argv（派单 P1-3）。
// 真实用户动作：运维按 install.sh 的提示先载入 .env，再把口令管道喂给
// `user create-master --password-stdin`。断言 argv 不含任何机密，
// 且 stdin 路径在真库上能建号、口令可校验。
func TestCreateMasterKeepsSecretOutOfArgv(t *testing.T) {
	dsn := userTestSchema(t)
	const password = "PipePass123!"
	const email = "pipe-master@example.com"
	t.Setenv("YIBAN_DATABASE_URL", dsn)
	t.Setenv("YIBAN_HASH_PEPPER", cliTestPepper)

	args := []string{"user", "create-master", "--email", email, "--password-stdin"}
	argv := strings.Join(args, " ")
	for _, secret := range []string{password, dsn, cliTestPepper} {
		if strings.Contains(argv, secret) {
			t.Fatalf("argv 含机密 %q：%s", secret, argv)
		}
	}

	out, code := runCLIWithStdin(t, password+"\n", args)
	if code != 0 {
		t.Fatalf("--password-stdin code = %d, out=%s", code, out)
	}
	for _, secret := range []string{password, dsn, cliTestPepper} {
		if strings.Contains(out, secret) {
			t.Fatalf("CLI 输出泄漏机密 %q：%s", secret, out)
		}
	}

	db, err := pg.Open(context.Background(), pg.Options{DSN: dsn, MaxConns: 2, StatementTimeout: 10 * time.Second})
	if err != nil {
		t.Fatalf("open schema db: %v", err)
	}
	defer db.Close()
	hash, err := db.QueryString(context.Background(), "SELECT password_hash FROM users WHERE email = $1", email)
	if err != nil {
		t.Fatalf("query hash: %v", err)
	}
	check, err := coreauth.VerifyPassword(password, hash)
	if err != nil || !check.Valid {
		t.Fatalf("stdin 口令不可校验: %+v, %v", check, err)
	}
}

// TestPasswordFileRequiresOwnerOnlyMode 钉住 --password-file 的 0600 判据（派单 P1-3）。
// 同机其他用户能读到的口令不得使用。Windows 无 POSIX 模式位，本测显性跳过。
func TestPasswordFileRequiresOwnerOnlyMode(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("SKIP: Windows 没有 POSIX 模式位，--password-file 的 0600 校验未实测")
	}
	path := filepath.Join(t.TempDir(), "master.pass")
	if err := os.WriteFile(path, []byte("FilePass123!\n"), 0o644); err != nil {
		t.Fatalf("write password file: %v", err)
	}
	if err := os.Chmod(path, 0o644); err != nil {
		t.Fatalf("chmod 0644: %v", err)
	}
	if _, err := readPasswordFile(path); err == nil {
		t.Fatal("0644 必须拒绝：组与其他位非零")
	}
	if err := os.Chmod(path, 0o600); err != nil {
		t.Fatalf("chmod 0600: %v", err)
	}
	pw, err := readPasswordFile(path)
	if err != nil {
		t.Fatalf("readPasswordFile(0600): %v", err)
	}
	if pw != "FilePass123!" {
		t.Fatalf("口令 = %q, want FilePass123!", pw)
	}
}

// TestPasswordFileReadsRegularFile 钉住读取路径与普通文件判据（跨平台）。
func TestPasswordFileReadsRegularFile(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "master.pass")
	if err := os.WriteFile(path, []byte("FilePass123!\n"), 0o600); err != nil {
		t.Fatalf("write password file: %v", err)
	}
	pw, err := readPasswordFile(path)
	if err != nil {
		t.Fatalf("readPasswordFile: %v", err)
	}
	if pw != "FilePass123!" {
		t.Fatalf("口令 = %q, want FilePass123!", pw)
	}
	if _, err := readPasswordFile(dir); err == nil {
		t.Fatal("目录必须拒绝：--password-file 只收普通文件")
	}
}

// TestResolvePasswordSourcesAreExclusive 钉住三种口令来源互斥且 stdin 只读一行。
func TestResolvePasswordSourcesAreExclusive(t *testing.T) {
	if _, err := resolvePassword("", "", false, nil); err == nil {
		t.Fatal("三种来源都没给时必须报错")
	}
	if _, err := resolvePassword("a", "b", true, strings.NewReader("c")); err == nil {
		t.Fatal("同时给多个来源时必须报错")
	}
	pw, err := resolvePassword("", "", true, strings.NewReader("PipePass\nsecond-line\n"))
	if err != nil {
		t.Fatalf("stdin 路径: %v", err)
	}
	if pw != "PipePass" {
		t.Fatalf("stdin 口令 = %q, want 第一行 PipePass", pw)
	}
	if _, err := resolvePassword("", "", true, strings.NewReader("")); err == nil {
		t.Fatal("stdin 为空时必须报错")
	}
}
