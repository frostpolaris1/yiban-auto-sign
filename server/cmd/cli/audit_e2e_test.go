package main

// audit anchor 的真库 e2e。
//
// # 功能
// 空链拒绝写锚点；有行时追加一行 seq/count/hash；第二次追加取新链头；
// 锚点文件可被巡检端读取；未配置路径是用法错误。
//
// # 口径
// 缺 YIBAN_TEST_DATABASE_URL 时 userTestSchema 显性 SKIP：跳过不是绿。

import (
	"context"
	"encoding/hex"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/anchor"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/pg"
	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	coreaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/audit"
)

func TestAuditAnchorE2E(t *testing.T) {
	dsn := userTestSchema(t)
	t.Setenv("YIBAN_DATABASE_URL", dsn)
	t.Setenv("YIBAN_HASH_PEPPER", cliTestPepper)
	ctx := context.Background()
	db, err := pg.Open(ctx, pg.Options{DSN: dsn, MaxConns: 2, StatementTimeout: 10 * time.Second})
	if err != nil {
		t.Fatalf("open db: %v", err)
	}
	defer db.Close()
	key, err := coreaudit.DeriveKey([]byte(cliTestPepper))
	if err != nil {
		t.Fatalf("derive key: %v", err)
	}
	audits, err := pg.NewAuditRepo(db, key)
	if err != nil {
		t.Fatalf("new audit repo: %v", err)
	}
	path := filepath.Join(t.TempDir(), "audit-anchor.log")

	// 空链：拒绝写行，退出码 1，文件不产生锚点行。
	if out, code := runCLI(t, []string{"audit", "anchor", "--file", path}); code != 1 {
		t.Fatalf("empty chain code = %d, out=%s", code, out)
	}
	if _, err := os.Stat(path); err == nil {
		t.Fatal("empty chain created an anchor file")
	}

	// 一行链：锚点 seq=1 count=1。
	if err := db.InTx(ctx, func(ctx context.Context) error {
		_, err := audits.Append(ctx, appaudit.Entry{
			TS:         time.Date(2026, 11, 11, 1, 0, 0, 0, time.UTC),
			ActorLabel: "anchor-cli",
			Action:     coreaudit.ActionUserCreate,
		})
		return err
	}); err != nil {
		t.Fatalf("append first row: %v", err)
	}
	out, code := runCLI(t, []string{"audit", "anchor", "--file", path})
	if code != 0 {
		t.Fatalf("first anchor code = %d, out=%s", code, out)
	}
	got, err := anchor.NewFile(path).Latest(ctx)
	if err != nil {
		t.Fatalf("read anchor: %v", err)
	}
	if got.Seq != 1 || got.Count != 1 || len(got.Hash) != 32 {
		t.Fatalf("anchor = %+v, want seq=1 count=1", got)
	}
	seq, err := db.QueryInt64(ctx, "SELECT seq FROM anchor_meta WHERE singleton")
	if err != nil {
		t.Fatalf("query anchor_meta seq: %v", err)
	}
	if seq != 1 {
		t.Fatalf("anchor_meta seq = %d, want 1", seq)
	}
	hash, err := db.QueryString(ctx, "SELECT encode(head_hash, 'hex') FROM anchor_meta WHERE singleton")
	if err != nil {
		t.Fatalf("query anchor_meta hash: %v", err)
	}
	if hash != hex.EncodeToString(got.Hash) {
		t.Fatalf("anchor_meta hash = %q, want %x", hash, got.Hash)
	}
	lineHash, err := db.QueryString(ctx, "SELECT encode(line_hash, 'hex') FROM anchor_meta WHERE singleton")
	if err != nil {
		t.Fatalf("query anchor_meta line_hash: %v", err)
	}
	if lineHash != hex.EncodeToString(got.LineHash) {
		t.Fatalf("anchor_meta line_hash = %q, want %x", lineHash, got.LineHash)
	}

	// 第二行：第二次追加取新链头，文件保留两行。
	if err := db.InTx(ctx, func(ctx context.Context) error {
		_, err := audits.Append(ctx, appaudit.Entry{
			TS:         time.Date(2026, 11, 11, 2, 0, 0, 0, time.UTC),
			ActorLabel: "anchor-cli",
			Action:     coreaudit.ActionUserCreate,
		})
		return err
	}); err != nil {
		t.Fatalf("append second row: %v", err)
	}
	if out, code := runCLI(t, []string{"audit", "anchor", "--file", path}); code != 0 {
		t.Fatalf("second anchor code = %d, out=%s", code, out)
	}
	got, err = anchor.NewFile(path).Latest(ctx)
	if err != nil {
		t.Fatalf("read anchor: %v", err)
	}
	if got.Seq != 2 || got.Count != 2 {
		t.Fatalf("anchor after second row = %+v, want seq=2 count=2", got)
	}
	seq, err = db.QueryInt64(ctx, "SELECT seq FROM anchor_meta WHERE singleton")
	if err != nil {
		t.Fatalf("query anchor_meta seq after second row: %v", err)
	}
	if seq != 2 {
		t.Fatalf("anchor_meta seq after second row = %d, want 2", seq)
	}
	body, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read file: %v", err)
	}
	if lines := strings.Count(string(body), "\n"); lines != 2 {
		t.Fatalf("anchor lines = %d, want 2", lines)
	}
	if runtime.GOOS != "windows" {
		info, err := os.Stat(path)
		if err != nil {
			t.Fatalf("stat: %v", err)
		}
		if perm := info.Mode().Perm(); perm != 0o600 {
			t.Fatalf("mode = %o, want 600", perm)
		}
	}

	// 审查探针：文件截空后再正常跑 CLI，必须 exit 1 且不洗白 meta。
	// CLI 先追加本次链头（文件变一行），随后发现 meta 记录的那一行
	// （seq=2 的旧行）在文件中缺失，判 anchor_truncated 并拒写 meta。
	if err := os.WriteFile(path, nil, 0o600); err != nil {
		t.Fatalf("truncate anchor file: %v", err)
	}
	out, code = runCLI(t, []string{"audit", "anchor", "--file", path})
	if code != 1 {
		t.Fatalf("truncated-file anchor code = %d, out=%s", code, out)
	}
	if !strings.Contains(out, "anchor_truncated") {
		t.Fatalf("stderr must name anchor_truncated: %s", out)
	}
	seq, err = db.QueryInt64(ctx, "SELECT seq FROM anchor_meta WHERE singleton")
	if err != nil {
		t.Fatalf("query anchor_meta after truncation: %v", err)
	}
	if seq != 2 {
		t.Fatalf("anchor_meta was whitewashed after truncation: seq=%d, want 2", seq)
	}
	body, err = os.ReadFile(path)
	if err != nil {
		t.Fatalf("read file after truncation: %v", err)
	}
	if lines := strings.Count(string(body), "\n"); lines != 1 {
		t.Fatalf("anchor lines after failed run = %d, want 1 (only the new head)", lines)
	}

	// 未配置路径：用法错误 2，不连库也不写文件。
	t.Setenv("YIBAN_AUDIT_ANCHOR_FILE", "")
	if out, code := runCLI(t, []string{"audit", "anchor"}); code != 2 {
		t.Fatalf("missing path code = %d, out=%s", code, out)
	}
}
