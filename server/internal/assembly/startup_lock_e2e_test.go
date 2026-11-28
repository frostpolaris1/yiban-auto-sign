package assembly

// 单实例启动锁的接线 e2e：真调 Build，不用 pg helper 代替装配路径。
//
// # 功能
// 另一个会话持启动锁时，Build 必须返回包装后的 pg.ErrStartupLockHeld、
// 不给 Stack，并打一行 assembly.startup_lock_held 错误日志。
// 无人持锁时 Build 能完整装配；Stack.Close 之后同进程再次 Build 仍能取到锁。
//
// # 归属
// 组合根接线门。pg 包的 e2e 只证明锁本身互斥；本测试证明 Build 真的
// 调用了它，且释放挂在 Stack.Close 的关机路径上。
//
// # 口径
// 缺 YIBAN_TEST_DATABASE_URL 时 SKIP，不做进程内替身。
// 装配用独立 schema，不碰其他测试数据。
// advisory lock 按数据库生效，schema 隔离不了：与 pg 包的启动锁 e2e
// 用同一串行键互斥，避免 go test 并行跑包时互抢同一把锁。

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"io"
	"log/slog"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/pg"
)

// startupLockE2ESerialKey 与 pg 包 startuplock_e2e_test.go 的串行键同值。
const startupLockE2ESerialKey int64 = 71019999

// startupLockTestDSN 建独立 schema 并返回带 search_path 的 DSN。
// 串行锁挂在 setup 连接的生命周期上，Cleanup 里释放。
func startupLockTestDSN(t *testing.T) string {
	t.Helper()
	base := strings.TrimSpace(os.Getenv("YIBAN_TEST_DATABASE_URL"))
	if base == "" {
		t.Skip("SKIP: YIBAN_TEST_DATABASE_URL is not set. startup lock e2e did NOT run.")
	}
	ctx, cancel := context.WithTimeout(t.Context(), 2*time.Minute)
	defer cancel()
	admin, err := pgx.Connect(ctx, base)
	if err != nil {
		t.Fatalf("connect admin: %v", err)
	}
	if _, err := admin.Exec(ctx, "SELECT pg_advisory_lock($1)", startupLockE2ESerialKey); err != nil {
		_ = admin.Close(context.Background())
		t.Fatalf("acquire serial advisory lock: %v", err)
	}
	schema := "assembly_lock_" + startupLockSuffix(t)
	if _, err := admin.Exec(ctx, "CREATE SCHEMA "+schema); err != nil {
		_ = admin.Close(context.Background())
		t.Fatalf("create schema %s: %v", schema, err)
	}
	t.Cleanup(func() {
		dropCtx, dropCancel := context.WithTimeout(context.Background(), 30*time.Second)
		defer dropCancel()
		_, _ = admin.Exec(dropCtx, "DROP SCHEMA IF EXISTS "+schema+" CASCADE")
		_ = admin.Close(context.Background())
	})
	if strings.Contains(base, "://") {
		sep := "?"
		if strings.Contains(base, "?") {
			sep = "&"
		}
		return base + sep + "search_path=" + schema
	}
	return base + " search_path=" + schema
}

// startupLockSuffix 返回 8 字节十六进制，供 schema 名去重。
func startupLockSuffix(t *testing.T) string {
	t.Helper()
	var buf [8]byte
	if _, err := rand.Read(buf[:]); err != nil {
		t.Fatalf("random schema suffix: %v", err)
	}
	return hex.EncodeToString(buf[:])
}

// 持锁的第二个会话存在时，Build 必须在启动路径上被哨兵错误挡住。
func TestBuildRefusesStartupWhenLockHeld(t *testing.T) {
	dsn := startupLockTestDSN(t)
	ctx := t.Context()

	holder, err := pgx.Connect(ctx, dsn)
	if err != nil {
		t.Fatalf("connect lock holder: %v", err)
	}
	defer func() { _ = holder.Close(context.Background()) }()
	var acquired bool
	if err := holder.QueryRow(ctx,
		"SELECT pg_try_advisory_lock($1)", pg.StartupLockKey).Scan(&acquired); err != nil {
		t.Fatalf("holder acquire startup lock: %v", err)
	}
	if !acquired {
		t.Fatal("holder 未取到启动锁：测试前置不成立")
	}

	cfg := capacityConfig(t)
	cfg.DatabaseURL = dsn
	h := &recordHandler{}
	stack, err := Build(cfg, slog.New(h))
	if stack != nil {
		stack.Close()
		t.Fatal("Build 在启动锁被占时仍给了 Stack")
	}
	if !errors.Is(err, pg.ErrStartupLockHeld) {
		t.Fatalf("Build error = %v, want ErrStartupLockHeld", err)
	}
	rec := h.event("assembly.startup_lock_held")
	if rec == nil {
		t.Fatalf("缺启动锁错误日志：%v", h.records)
	}
	if rec["level"] != slog.LevelError.String() {
		t.Fatalf("启动锁日志级别 = %v, want ERROR", rec["level"])
	}
}

// 正常装配后 Stack.Close 必须释放锁，同进程再次 Build 能成功。
func TestBuildReleasesStartupLockOnClose(t *testing.T) {
	dsn := startupLockTestDSN(t)
	cfg := capacityConfig(t)
	cfg.DatabaseURL = dsn
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))

	first, err := Build(cfg, logger)
	if err != nil {
		t.Fatalf("first Build: %v", err)
	}
	first.Close()

	second, err := Build(cfg, logger)
	if err != nil {
		t.Fatalf("second Build after Close: %v", err)
	}
	second.Close()
}
