// backfill-time-prefs 子命令：旧库自选时间片 → 新库按用户的偏移（2gk）。
//
// # 功能
// 读 P8-A 导出物目录（accounts.jsonl + time_prefs.jsonl），沿 legacy_ids
// 找到新账号的归属用户，把旧 slot_min 写成新 time_prefs.offset_min。
// 默认 dry-run：报告将写/将跳过的清单与原因，不发写调用。
// `--apply` 在单事务内幂等 UPSERT；同值重跑零变更。
//
// # 归属
// 一次性迁移件。逻辑在 internal/adapters/legacy（backfill.go）；
// 本文件只做旗解析、依赖装配与退出码映射。
// 部署顺序：migrate → migrate-legacy → backfill-time-prefs。
//
// # 口径
// 退出码：0 成功（含 dry-run 与全部跳过）；1 读库或写库失败；
// 2 用法/输入错误（缺旗、导出物缺文件或坏行、DSN 缺失）。
// 报告以 JSON 打一行到 stdout；不写文件，不改 schema（不跑迁移）。
// 报告零明文手机号，一律 core/mask 掩码。
package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"strings"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/config"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/legacy"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/pg"
	coreclock "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
)

// runBackfillTimePrefs 执行一次补录，返回进程退出码。
func runBackfillTimePrefs(args []string, stdout, stderr io.Writer) int {
	fs := newFlagSet("backfill-time-prefs", stderr)
	dir := fs.String("dir", "", "P8-A 导出物目录（须含 accounts.jsonl 与 time_prefs.jsonl，必填）")
	apply := fs.Bool("apply", false, "写入数据库（默认 dry-run，不写）")
	dsnFlag := fs.String("dsn", "", "覆盖 YIBAN_DATABASE_URL")
	if err := fs.Parse(args); err != nil {
		return 2
	}
	if *dir == "" {
		fmt.Fprintln(stderr, "backfill-time-prefs: --dir 必填")
		return 2
	}
	dsn := strings.TrimSpace(*dsnFlag)
	if dsn == "" {
		v, _ := config.Env{}.Lookup("YIBAN_DATABASE_URL")
		dsn = strings.TrimSpace(v)
	}
	if dsn == "" {
		fmt.Fprintln(stderr, "backfill-time-prefs: YIBAN_DATABASE_URL 未设置（或用 --dsn 传入）")
		return 2
	}

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Minute)
	defer cancel()
	db, err := pg.Open(ctx, pg.Options{DSN: dsn, MaxConns: 2, StatementTimeout: 60 * time.Second})
	if err != nil {
		fmt.Fprintf(stderr, "backfill-time-prefs: open database: %v\n", err)
		return 1
	}
	defer db.Close()

	report, err := legacy.Backfill(ctx, legacy.BackfillOptions{
		Dir:      *dir,
		Apply:    *apply,
		Resolver: legacy.NewOwnerResolver(db),
		Writer:   legacy.NewTimePrefWriter(db),
		Now:      coreclock.System{}.Now().UTC(),
	})
	if err != nil {
		return printBackfillError(stderr, err)
	}
	encoded, err := json.MarshalIndent(report, "", "  ")
	if err != nil {
		fmt.Fprintf(stderr, "backfill-time-prefs: encode report: %v\n", err)
		return 1
	}
	fmt.Fprintln(stdout, string(encoded))
	return 0
}

// printBackfillError 给错误统一加一次前缀并映射退出码。
//
// # 口径
// `legacy.Error` 的 Msg 不带前缀（前缀只在本函数加一次）；退出码取 Code。
// 其他错误按 1 返回。双重前缀是 2gk 审查抓到的缺陷形状，
// 判据见 TestPrintBackfillErrorSinglePrefix。
func printBackfillError(w io.Writer, err error) int {
	var fatalErr *legacy.Error
	if errors.As(err, &fatalErr) {
		fmt.Fprintf(w, "backfill-time-prefs: %v\n", fatalErr.Msg)
		return fatalErr.Code
	}
	fmt.Fprintf(w, "backfill-time-prefs: %v\n", err)
	return 1
}
