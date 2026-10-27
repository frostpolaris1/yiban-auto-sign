// migrate 子命令：只应用迁移并打印 schema 版本，不起服务。
package main

import (
	"context"
	"flag"
	"fmt"
	"io"
	"strings"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/config"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/pg"
)

// runMigrate 应用全部待执行迁移并打印版本。
// DSN 来源：--dsn > 环境变量 YIBAN_DATABASE_URL。
// 返回进程退出码：0 成功；用法或迁移失败非 0。
func runMigrate(args []string, stdout, stderr io.Writer) int {
	fs := flag.NewFlagSet("migrate", flag.ContinueOnError)
	fs.SetOutput(stderr)
	dsn := fs.String("dsn", "", "覆盖 YIBAN_DATABASE_URL")
	if err := fs.Parse(args); err != nil {
		return 2
	}
	url := strings.TrimSpace(*dsn)
	if url == "" {
		v, _ := config.Env{}.Lookup("YIBAN_DATABASE_URL")
		url = strings.TrimSpace(v)
	}
	if url == "" {
		fmt.Fprintln(stderr, "migrate: YIBAN_DATABASE_URL 未设置（或用 --dsn 传入）")
		return 2
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Minute)
	defer cancel()
	version, err := pg.Migrate(ctx, url)
	if err != nil {
		fmt.Fprintf(stderr, "migrate: %v\n", err)
		return 1
	}
	fmt.Fprintf(stdout, "schema version %d\n", version)
	return 0
}
