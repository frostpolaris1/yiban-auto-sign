package main

// 对账表与真库 schema 的完整性 e2e（派单 F17）。
//
// # 功能
// 在独立 schema 里跑完全部迁移，再从 information_schema 取表清单，
// 断言每张表要么在 manifestTables、要么在 manifestExemptTables 且带理由。
// 覆盖的是「新增迁移忘了加对账表」这一形状：恢复后丢行不会被发现。
//
// # 口径
// 缺 DSN 时显性跳过（环境不满足不是绿）。豁免面只放迁移工具的簿记表；
// 业务表一张都不豁免。

import (
	"context"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/pg"
)

func TestManifestTablesCoverLiveSchema(t *testing.T) {
	base := strings.TrimSpace(os.Getenv("YIBAN_TEST_DATABASE_URL"))
	if base == "" {
		t.Skip("SKIP: YIBAN_TEST_DATABASE_URL is not set. 对账表完整性 e2e 未跑。")
	}
	ctx := context.Background()
	schema := "bkpmf_" + cliRandomHex(8)
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

	db, err := pg.Open(ctx, pg.Options{DSN: dsn, MaxConns: 1, StatementTimeout: 30 * time.Second})
	if err != nil {
		t.Fatalf("open schema db: %v", err)
	}
	defer db.Close()
	names, err := db.QueryString(ctx,
		`SELECT coalesce(string_agg(table_name, ',' ORDER BY table_name), '')
		   FROM information_schema.tables WHERE table_schema = $1`, schema)
	if err != nil {
		t.Fatalf("list tables: %v", err)
	}
	if strings.TrimSpace(names) == "" {
		t.Fatalf("schema %s 没有任何表：扫空不得当绿", schema)
	}

	covered := map[string]bool{}
	for _, table := range manifestTables {
		covered[table] = true
	}
	uncovered := []string{}
	for _, table := range strings.Split(names, ",") {
		if covered[table] {
			continue
		}
		if _, exempt := manifestExemptTables[table]; exempt {
			continue
		}
		uncovered = append(uncovered, table)
	}
	if len(uncovered) > 0 {
		t.Fatalf("这些表既不在 manifestTables 也未豁免，恢复后丢行不会被发现：%v", uncovered)
	}
}
