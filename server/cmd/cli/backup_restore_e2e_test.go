package main

// backup/restore 的真库 e2e（派单 P7 判据③）。
//
// # 功能
// 真库、真 pg_dump/psql/gpg：备份 → 清库 → 恢复 → 行数对账一致，
// 且恢复后的库可直接查询。模拟的真实动作：运维做一次恢复演练。
//
// # 口径
// 缺 DSN、缺 pg_dump/psql/gpg 任一工具时整测跳过并打印原因：
// 环境不满足不是绿，是显性跳过。

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/pg"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/assembly"
	coreaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/audit"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	coreschool "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/school"
	coreuser "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/user"
)

func TestBackupRestoreRoundTripReconciles(t *testing.T) {
	base := strings.TrimSpace(os.Getenv("YIBAN_TEST_DATABASE_URL"))
	if base == "" {
		t.Skip("SKIP: YIBAN_TEST_DATABASE_URL is not set. Backup e2e did NOT run.")
	}
	missing := []string{}
	for _, tool := range []string{"pg_dump", "psql", "gpg"} {
		if _, err := exec.LookPath(tool); err != nil {
			missing = append(missing, tool)
		}
	}
	if len(missing) > 0 {
		t.Skip("SKIP: missing tools: " + strings.Join(missing, ", ") +
			". Backup e2e did NOT run.")
	}

	ctx := context.Background()
	schema := "bkptest_" + cliRandomHex(8)
	admin, err := pg.Open(ctx, pg.Options{DSN: base, MaxConns: 1, StatementTimeout: 30 * time.Second})
	if err != nil {
		t.Fatalf("open admin: %v", err)
	}
	defer admin.Close()
	createSchema := func() {
		t.Helper()
		if _, err := admin.Exec(ctx, "CREATE SCHEMA "+schema); err != nil {
			t.Fatalf("create schema: %v", err)
		}
	}
	createSchema()
	t.Cleanup(func() {
		dropCtx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
		defer cancel()
		_, _ = admin.Exec(dropCtx, "DROP SCHEMA IF EXISTS "+schema+" CASCADE")
	})
	dsn := withSearchPathQS(base, schema)
	if _, err := pg.Migrate(ctx, dsn); err != nil {
		t.Fatalf("migrate: %v", err)
	}

	// 种子：一个用户 + 一所学校，行数对账就有了非零基线。
	db, err := pg.Open(ctx, pg.Options{DSN: dsn, MaxConns: 2, StatementTimeout: 10 * time.Second})
	if err != nil {
		t.Fatalf("open schema db: %v", err)
	}
	defer db.Close()
	users := pg.NewUserRepo(db)
	const pepper = "e2e-pepper-0123456789abcdef"
	key, err := assembly.DeriveAuditKey([]byte(pepper))
	if err != nil {
		t.Fatalf("derive audit key: %v", err)
	}
	audits, err := pg.NewAuditRepo(db, key)
	if err != nil {
		t.Fatalf("audit repo: %v", err)
	}
	// 种一行审计：恢复后验链才有的可验（空链走「跳过」分支）。
	err = db.InTx(ctx, func(ctx context.Context) error {
		u, txErr := users.Create(ctx, coreuser.New{
			Email: "bkp@example.com", PasswordHash: "argon2id$test", Role: coreauth.RoleUser,
		}, 10000)
		if txErr != nil {
			return txErr
		}
		_, txErr = audits.Append(ctx, coreaudit.Entry{
			TS: time.Date(2026, 10, 31, 0, 0, 0, 0, time.UTC), ActorLabel: "b***@example.test",
			Action: coreaudit.ActionAuthLogin, TargetType: "user", TargetID: u.ID,
		})
		return txErr
	})
	if err != nil {
		t.Fatalf("create user + audit: %v", err)
	}
	if _, err := pg.NewSchoolRepo(db).Create(ctx, coreschool.New{Code: "bkpschool", Name: "Bkp School"}); err != nil {
		t.Fatalf("create school: %v", err)
	}

	// 备份。
	out := filepath.Join(t.TempDir(), "backup.gpg")
	passFile := filepath.Join(t.TempDir(), "pass")
	if err := os.WriteFile(passFile, []byte("e2e-passphrase"), 0o600); err != nil {
		t.Fatalf("write pass: %v", err)
	}
	stdout, code := runCLI(t, []string{"backup", "--out", out, "--dsn", dsn, "--passphrase-file", passFile})
	if code != 0 {
		t.Fatalf("backup code = %d, stdout=%s", code, stdout)
	}
	if st, err := os.Stat(out); err != nil || st.Size() == 0 {
		t.Fatalf("archive missing or empty: %v", err)
	}

	// 清库：恢复判据是「清库 → 恢复 → 行数一致」。
	// 只 DROP 不重建：schema 限量的转储自带 CREATE SCHEMA。
	if _, err := admin.Exec(ctx, "DROP SCHEMA "+schema+" CASCADE"); err != nil {
		t.Fatalf("drop schema: %v", err)
	}

	// 恢复。恢复后要验审计链，故给同一个 pepper。
	t.Setenv("YIBAN_HASH_PEPPER", pepper)
	stdout, code = runCLI(t, []string{"restore", "--in", out, "--dsn", dsn, "--force", "--passphrase-file", passFile})
	if code != 0 {
		t.Fatalf("restore code = %d, stdout=%s", code, stdout)
	}
	if !strings.Contains(stdout, "对账一致") {
		t.Fatalf("restore must reconcile: %s", stdout)
	}
	if !strings.Contains(stdout, "审计链：1 行通过校验") {
		t.Fatalf("restore must verify the audit chain: %s", stdout)
	}

	// 恢复后的库可直接查询，行数与备份前一致。
	n, err := db.QueryInt64(ctx, "SELECT count(*) FROM users")
	if err != nil {
		t.Fatalf("query users after restore: %v", err)
	}
	if n != 1 {
		t.Fatalf("users = %d, want 1", n)
	}
	n, err = db.QueryInt64(ctx, "SELECT count(*) FROM schools")
	if err != nil {
		t.Fatalf("query schools after restore: %v", err)
	}
	if n != 1 {
		t.Fatalf("schools = %d, want 1", n)
	}
}

// runCLI 跑一次子命令函数，返回 stdout 与退出码。
func runCLI(t *testing.T, args []string) (string, int) {
	t.Helper()
	return runCLIWithStdin(t, "", args)
}

// runCLIWithStdin 同 runCLI，但给 stdin 喂内容（口令走管道的路径）。
func runCLIWithStdin(t *testing.T, stdin string, args []string) (string, int) {
	t.Helper()
	var out strings.Builder
	var code int
	switch args[0] {
	case "backup":
		code = runBackup(newBackupDeps(&out, &out), args[1:])
	case "restore":
		code = runRestore(newBackupDeps(&out, &out), args[1:])
	case "user":
		code = runUser(args[1:], strings.NewReader(stdin), &out, &out)
	default:
		t.Fatalf("unknown cli verb %q", args[0])
	}
	return out.String(), code
}

// cliRandomHex 生成测试 schema 的随机后缀。
func cliRandomHex(n int) string {
	b := make([]byte, n)
	if _, err := rand.Read(b); err != nil {
		panic("cli e2e: cannot read random bytes: " + err.Error())
	}
	return hex.EncodeToString(b)
}

// withSearchPathQS 给 URL 形 DSN 追加 search_path。
func withSearchPathQS(dsn, schema string) string {
	sep := "?"
	if strings.Contains(dsn, "?") {
		sep = "&"
	}
	return dsn + sep + "search_path=" + schema
}
