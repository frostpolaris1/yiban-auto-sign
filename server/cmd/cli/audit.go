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
// 审计链为空时拒绝写行：空字段行会被后续解析整体错位，宁缺毋滥。
// 密钥原文没有命令行参数：只从环境读，避免出现在 ps/argv 与 shell 历史里。
// 本命令只追加锚点，不改库、不校验链、不清理。
package main

import (
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
		_, _ = fmt.Fprintln(stderr, `用法: yiban-cli audit anchor [--dsn <串>] [--pepper <串>] [--file <路径>]
把当前审计链头追加到库外锚点文件（append-only，0600）。
路径缺省取 YIBAN_AUDIT_ANCHOR_FILE；未配置即用法错误。
建议 cron 每日跑一次：yiban-cli audit anchor >> /var/log/yiban/audit-anchor.log 2>&1`)
		return 2
	}
	fs := flag.NewFlagSet("audit anchor", flag.ContinueOnError)
	fs.SetOutput(stderr)
	dsn := fs.String("dsn", "", "覆盖 YIBAN_DATABASE_URL")
	pepper := fs.String("pepper", "", "覆盖 YIBAN_HASH_PEPPER（至少 16 字符）")
	file := fs.String("file", "", "覆盖 YIBAN_AUDIT_ANCHOR_FILE")
	if err := fs.Parse(args[1:]); err != nil {
		return 2
	}
	url := strings.TrimSpace(*dsn)
	if url == "" {
		v, _ := config.Env{}.Lookup("YIBAN_DATABASE_URL")
		url = strings.TrimSpace(v)
	}
	if url == "" {
		_, _ = fmt.Fprintln(stderr, "audit anchor: YIBAN_DATABASE_URL 未设置（或用 --dsn 传入）")
		return 2
	}
	pep := strings.TrimSpace(*pepper)
	if pep == "" {
		v, _ := config.Env{}.Lookup("YIBAN_HASH_PEPPER")
		pep = strings.TrimSpace(v)
	}
	if len(pep) < 16 {
		_, _ = fmt.Fprintln(stderr, "audit anchor: YIBAN_HASH_PEPPER 未设置或不足 16 字符（审计链钥由它派生）")
		return 2
	}
	path := strings.TrimSpace(*file)
	if path == "" {
		v, _ := config.Env{}.Lookup("YIBAN_AUDIT_ANCHOR_FILE")
		path = strings.TrimSpace(v)
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
	if err := anchor.NewFile(path).Append(ctx, head, coreclock.System{}.Now()); err != nil {
		_, _ = fmt.Fprintf(stderr, "audit anchor: %v\n", err)
		return 1
	}
	_, _ = fmt.Fprintf(stdout, "audit anchor: seq=%d count=%d hash=%x file=%s\n",
		head.Seq, head.Count, head.Hash, path)
	return 0
}
