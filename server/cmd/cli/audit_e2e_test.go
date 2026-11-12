package main

// audit anchor 的真库 e2e。
//
// # 功能
// 空链拒绝写锚点；有行时追加一行 seq/count/hash；第二次追加取新链头；
// 链头未前进时同 seq 重跑幂等跳过（不追加、不写 meta、exit 0）；
// 文件截空后的失败运行不得留下噪音行；--reseed 显式重播种覆盖 meta；
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

	// 审查探针①-1：链头未前进时同 seq 重跑必须幂等跳过——exit 0、stdout
	// 一句 already anchored、文件不追加新行、meta 不变。旧行为在此追加
	// "同 seq 新时间戳"行：Latest 取新行 ≠ meta.LineHash，判 anchor_rewritten
	// 退 1，每日 timer 在安静日必误报。判等只看 seq/count/head_hash，
	// 时间戳不参与身份。
	out, code = runCLI(t, []string{"audit", "anchor", "--file", path})
	if code != 0 {
		t.Fatalf("same-seq rerun code = %d, out=%s", code, out)
	}
	if !strings.Contains(out, "already anchored") {
		t.Fatalf("same-seq rerun stdout = %q, want a line with already anchored", out)
	}
	body, err = os.ReadFile(path)
	if err != nil {
		t.Fatalf("read file after same-seq rerun: %v", err)
	}
	if lines := strings.Count(string(body), "\n"); lines != 2 {
		t.Fatalf("anchor lines after same-seq rerun = %d, want 2 (no append)", lines)
	}
	strongest, err := anchor.NewFile(path).Latest(ctx)
	if err != nil {
		t.Fatalf("read strongest after rerun: %v", err)
	}
	lineHash, err = db.QueryString(ctx, "SELECT encode(line_hash, 'hex') FROM anchor_meta WHERE singleton")
	if err != nil {
		t.Fatalf("query anchor_meta line_hash after rerun: %v", err)
	}
	if lineHash != hex.EncodeToString(strongest.LineHash) {
		t.Fatalf("meta line_hash after rerun = %q, want file strongest %x", lineHash, strongest.LineHash)
	}

	// 审查探针②-1：文件截空后再正常跑 CLI，必须 exit 1、不洗白 meta，
	// 并且**不得往文件里追加噪音行**。门在追加之前跑：旧实现先追加再判,
	// 失败时把新链头行留在空文件里，meta 记录行永远找不回，巡检在
	// truncated/unrecorded/rewritten 之间循环、永不自愈。
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
	if lines := strings.Count(string(body), "\n"); lines != 0 {
		t.Fatalf("anchor lines after failed run = %d, want 0 (gate must run before append)", lines)
	}

	// 审查探针②-2：合法恢复走 --reseed。移走/截空锚点文件后显式重播种：
	// 忽略"meta 记录行在文件中缺失"检查，以当前链头追加新行并无条件覆盖
	// anchor_meta（独立 SQL 路径，不走棘轮）；stderr 必须写明这是运维显式
	// 动作。旧实现下此场景永远 exit 1（anchor_truncated），恢复走不通。
	out, code = runCLI(t, []string{"audit", "anchor", "--reseed", "--file", path})
	if code != 0 {
		t.Fatalf("reseed code = %d, out=%s", code, out)
	}
	if !strings.Contains(out, "reseed") {
		t.Fatalf("reseed must flag itself as an explicit ops action: %s", out)
	}
	body, err = os.ReadFile(path)
	if err != nil {
		t.Fatalf("read file after reseed: %v", err)
	}
	if lines := strings.Count(string(body), "\n"); lines != 1 {
		t.Fatalf("anchor lines after reseed = %d, want 1", lines)
	}
	got, err = anchor.NewFile(path).Latest(ctx)
	if err != nil {
		t.Fatalf("read anchor after reseed: %v", err)
	}
	if got.Seq != 2 || got.Count != 2 || len(got.LineHash) != 32 {
		t.Fatalf("anchor after reseed = %+v, want seq=2 count=2", got)
	}
	lineHash, err = db.QueryString(ctx, "SELECT encode(line_hash, 'hex') FROM anchor_meta WHERE singleton")
	if err != nil {
		t.Fatalf("query anchor_meta line_hash after reseed: %v", err)
	}
	if lineHash != hex.EncodeToString(got.LineHash) {
		t.Fatalf("meta line_hash after reseed = %q, want the new row %x", lineHash, got.LineHash)
	}

	// reseed 之后同链头再跑普通命令：链头未前进，幂等跳过，文件不再长行。
	out, code = runCLI(t, []string{"audit", "anchor", "--file", path})
	if code != 0 {
		t.Fatalf("post-reseed rerun code = %d, out=%s", code, out)
	}
	if !strings.Contains(out, "already anchored") {
		t.Fatalf("post-reseed rerun stdout = %q, want already anchored", out)
	}
	body, err = os.ReadFile(path)
	if err != nil {
		t.Fatalf("read file after post-reseed rerun: %v", err)
	}
	if lines := strings.Count(string(body), "\n"); lines != 1 {
		t.Fatalf("anchor lines after post-reseed rerun = %d, want 1", lines)
	}

	// 未配置路径：用法错误 2，不连库也不写文件。
	t.Setenv("YIBAN_AUDIT_ANCHOR_FILE", "")
	if out, code := runCLI(t, []string{"audit", "anchor"}); code != 2 {
		t.Fatalf("missing path code = %d, out=%s", code, out)
	}
}
