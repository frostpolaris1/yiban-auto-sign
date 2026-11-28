package assembly

// 单实例启动锁的接线 e2e：真调 Build，不用 pg helper 代替装配路径。
//
// # 功能
// 另一个会话持启动锁时，Build 必须在迁移之前拒绝启动：返回包装后的
// pg.ErrStartupLockHeld、不给 Stack、打一行 assembly.startup_lock_held
// 错误日志，且被拒实例零 schema 副作用。
// 无人持锁时 Build 能完整装配；Stack.Close 之后同进程再次 Build 仍能取到锁。
// 持锁连接失效时打 assembly.startup_lock_lost 并 fail-stop。
//
// # 归属
// 组合根接线门。pg 包的 e2e 只证明锁本身互斥与探活；本测试证明 Build
// 真的调用了它、取锁在迁移之前、释放挂在 Stack.Close 的关机路径上。
//
// # 口径
// 缺 YIBAN_TEST_DATABASE_URL 时 SKIP；失效通知用接缝注入，不断真连接。
// 装配用独立 schema，不碰其他测试数据。
// advisory lock 按数据库生效，schema 隔离不了：与 pg 包、cmd/server 的
// 启动锁用例用同一串行键 pg.TestSerialLockKey 互斥，避免 go test
// 并行跑包时互抢同一把锁。

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

// startupLockTestDSN 建独立 schema，返回带 search_path 的 DSN 与 schema 名。
// 串行锁挂在 setup 连接的生命周期上，Cleanup 里释放。
func startupLockTestDSN(t *testing.T) (string, string) {
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
	for {
		var acquired bool
		if err := admin.QueryRow(ctx, "SELECT pg_try_advisory_lock($1)", pg.TestSerialLockKey).Scan(&acquired); err != nil {
			_ = admin.Close(context.Background())
			t.Fatalf("try serial advisory lock: %v", err)
		}
		if acquired {
			break
		}
		select {
		case <-ctx.Done():
			_ = admin.Close(context.Background())
			t.Fatalf("serial advisory lock not granted within 2m: %v", ctx.Err())
		case <-time.After(100 * time.Millisecond):
		}
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
	return withSearchPath(base, schema), schema
}

// withSearchPath 在 DSN 上追加 search_path 运行时参数。
func withSearchPath(dsn, schema string) string {
	if strings.Contains(dsn, "://") {
		sep := "?"
		if strings.Contains(dsn, "?") {
			sep = "&"
		}
		return dsn + sep + "search_path=" + schema
	}
	return dsn + " search_path=" + schema
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

// 持锁的第二个会话存在时，Build 必须在启动路径上被哨兵错误挡住，
// 且不得先跑迁移留下 schema 副作用。
func TestBuildRefusesStartupWhenLockHeld(t *testing.T) {
	dsn, schema := startupLockTestDSN(t)
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
	// 取锁在迁移之前：被拒实例一个关系（表/序列/索引）都不该留下。
	var relations int
	if err := holder.QueryRow(ctx,
		`SELECT count(*) FROM pg_class c
		   JOIN pg_namespace n ON n.oid = c.relnamespace
		  WHERE n.nspname = $1`, schema).Scan(&relations); err != nil {
		t.Fatalf("count schema relations: %v", err)
	}
	if relations != 0 {
		t.Fatalf("Build 在取锁失败前写入了 schema：%d 个关系", relations)
	}
}

// 正常装配后 Stack.Close 必须释放锁，同进程再次 Build 能成功。
func TestBuildReleasesStartupLockOnClose(t *testing.T) {
	dsn, _ := startupLockTestDSN(t)
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

// fakeStartupLock 只实现 Build 依赖的 Close/Lost 面。
type fakeStartupLock struct {
	lost chan error
}

func (f *fakeStartupLock) Close()             {}
func (f *fakeStartupLock) Lost() <-chan error { return f.lost }

// 持锁连接失效：Build 装配的 watcher 必须记 ERROR 并 fail-stop。
func TestBuildFailStopsWhenStartupLockLost(t *testing.T) {
	dsn, _ := startupLockTestDSN(t)
	cfg := capacityConfig(t)
	cfg.DatabaseURL = dsn
	h := &recordHandler{}
	lock := &fakeStartupLock{lost: make(chan error, 1)}

	origAcquire, origStop := acquireStartupLock, startupLockFailStop
	acquireStartupLock = func(context.Context, string) (startupLockHandle, error) {
		return lock, nil
	}
	stopped := make(chan struct{}, 1)
	startupLockFailStop = func() {
		select {
		case stopped <- struct{}{}:
		default:
		}
	}
	t.Cleanup(func() {
		acquireStartupLock, startupLockFailStop = origAcquire, origStop
	})

	stack, err := Build(cfg, slog.New(h))
	if err != nil {
		t.Fatalf("Build: %v", err)
	}
	defer stack.Close()

	lock.lost <- errors.New("connection reset by peer")
	select {
	case <-stopped:
	case <-time.After(5 * time.Second):
		t.Fatal("锁连接失效后 5s 内未触发 fail-stop")
	}
	rec := h.event("assembly.startup_lock_lost")
	if rec == nil {
		t.Fatalf("缺启动锁失效日志：%v", h.records)
	}
	if rec["level"] != slog.LevelError.String() {
		t.Fatalf("启动锁失效日志级别 = %v, want ERROR", rec["level"])
	}
}
