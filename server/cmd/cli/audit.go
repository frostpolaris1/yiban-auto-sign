// audit 子命令：把当前审计链头追加到库外锚点文件。
//
// # 功能
// `yiban-cli audit anchor` 读取 audit_logs 的链头（最后一行 id、截至该行的
// 行数与存储哈希），追加一行到 YIBAN_AUDIT_ANCHOR_FILE（append-only，0600）。
// 巡检端点 GET /api/admin/audit-status 在配置了同一路径时比对锚点：
// 删前缀、截尾、整表清空与链尾重签都会翻红。
//
// # 归属
// cmd/cli 薄装配层。文件格式与读写住在 internal/adapters/anchor；
// 链头读取住在 adapters/pg.AuditRepo.Head。
//
// # 口径
// 退出码：0 写入成功；1 运行失败（连库/读链头/写文件）；2 用法错误。
// DSN 与 pepper 只从环境读（YIBAN_DATABASE_URL / YIBAN_HASH_PEPPER）：
// 没有命令行参数，避免秘密进 ps/argv 与 shell 历史。
// --file 只覆盖非敏感路径（YIBAN_AUDIT_ANCHOR_FILE）。
// 审计链为空时拒绝写行：空字段行会被后续解析整体错位，宁缺毋滥。
// 本命令只追加锚点，不改库、不校验链、不清理。
package main

import (
	"bytes"
	"context"
	"flag"
	"fmt"
	"io"
	"strings"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/anchor"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/config"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/pg"
	coreaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/audit"
	coreclock "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
)

// runAudit 处理 audit 子命令。当前只有 anchor。
func runAudit(args []string, stdout, stderr io.Writer) int {
	if len(args) == 0 || args[0] != "anchor" {
		_, _ = fmt.Fprintln(stderr, `用法: yiban-cli audit anchor [--file <路径>]
把当前审计链头追加到库外锚点文件（append-only，0600）。
DSN 与 pepper 只从环境读：YIBAN_DATABASE_URL / YIBAN_HASH_PEPPER。
路径缺省取 YIBAN_AUDIT_ANCHOR_FILE；未配置即用法错误。
建议由 systemd timer 每日跑一次（见 deploy/README.md §6.1）。`)
		return 2
	}
	fs := flag.NewFlagSet("audit anchor", flag.ContinueOnError)
	fs.SetOutput(stderr)
	file := fs.String("file", "", "覆盖 YIBAN_AUDIT_ANCHOR_FILE（非敏感路径）")
	if err := fs.Parse(args[1:]); err != nil {
		return 2
	}
	url := strings.TrimSpace(envValue("YIBAN_DATABASE_URL"))
	if url == "" {
		_, _ = fmt.Fprintln(stderr, "audit anchor: YIBAN_DATABASE_URL 未设置")
		return 2
	}
	pep := strings.TrimSpace(envValue("YIBAN_HASH_PEPPER"))
	if len(pep) < 16 {
		_, _ = fmt.Fprintln(stderr, "audit anchor: YIBAN_HASH_PEPPER 未设置或不足 16 字符（审计链钥由它派生）")
		return 2
	}
	path := strings.TrimSpace(*file)
	if path == "" {
		path = strings.TrimSpace(envValue("YIBAN_AUDIT_ANCHOR_FILE"))
	}
	if path == "" {
		_, _ = fmt.Fprintln(stderr, "audit anchor: YIBAN_AUDIT_ANCHOR_FILE 未设置（或用 --file 传入）")
		return 2
	}

	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	db, err := pg.Open(ctx, pg.Options{DSN: url, MaxConns: 1, StatementTimeout: 10 * time.Second})
	if err != nil {
		_, _ = fmt.Fprintf(stderr, "audit anchor: %v\n", err)
		return 1
	}
	defer db.Close()
	key, err := coreaudit.DeriveKey([]byte(pep))
	if err != nil {
		_, _ = fmt.Fprintf(stderr, "audit anchor: %v\n", err)
		return 2
	}
	audits, err := pg.NewAuditRepo(db, key)
	if err != nil {
		_, _ = fmt.Fprintf(stderr, "audit anchor: %v\n", err)
		return 1
	}
	head, ok, err := audits.Head(ctx)
	if err != nil {
		_, _ = fmt.Fprintf(stderr, "audit anchor: %v\n", err)
		return 1
	}
	if !ok {
		_, _ = fmt.Fprintln(stderr, "audit anchor: 审计链为空，拒绝写锚点行")
		return 1
	}
	anchorFile := anchor.NewFile(path)
	if err := anchorFile.Append(ctx, head, coreclock.System{}.Now()); err != nil {
		_, _ = fmt.Fprintf(stderr, "audit anchor: %v\n", err)
		return 1
	}
	// 文件追加成功后先读回末行，确认刚写的链头确实落在文件末尾。
	last, err := anchorFile.Last(ctx)
	if err != nil {
		_, _ = fmt.Fprintf(stderr, "audit anchor: 读回锚点末行失败: %v\n", err)
		return 1
	}
	if last.Seq != head.Seq || last.Count != head.Count || !bytes.Equal(last.Hash, head.Hash) {
		_, _ = fmt.Fprintf(stderr, "audit anchor: 锚点末行与本次链头不一致（seq=%d/%d）\n", last.Seq, head.Seq)
		return 1
	}
	// 高水位取文件里的最强锚点：回滚后新链头可能小于旧锚点，
	// 此时 meta 必须停在旧高水位，不能倒退。
	strongest, err := anchorFile.Latest(ctx)
	if err != nil {
		_, _ = fmt.Fprintf(stderr, "audit anchor: 读回最强锚点失败: %v\n", err)
		return 1
	}
	if strongest.Seq < head.Seq {
		_, _ = fmt.Fprintf(stderr, "audit anchor: 最强锚点 seq=%d 小于本次链头 %d\n", strongest.Seq, head.Seq)
		return 1
	}
	// 锚点文件与库内高水位两处一致才算锚定成功；写表失败即回报错。
	if err := db.InTx(ctx, func(ctx context.Context) error {
		return audits.RecordAnchorMeta(ctx, coreaudit.AnchorMeta{
			Seq: strongest.Seq, HeadHash: strongest.Hash,
		})
	}); err != nil {
		_, _ = fmt.Fprintf(stderr, "audit anchor: 写 anchor_meta 失败: %v\n", err)
		return 1
	}
	_, _ = fmt.Fprintf(stdout, "audit anchor: seq=%d count=%d hash=%x meta_seq=%d file=%s\n",
		head.Seq, head.Count, head.Hash, strongest.Seq, path)
	return 0
}

// envValue 读取进程环境。环境读点仍收敛在 adapters/config。
func envValue(key string) string {
	v, _ := config.Env{}.Lookup(key)
	return v
}
