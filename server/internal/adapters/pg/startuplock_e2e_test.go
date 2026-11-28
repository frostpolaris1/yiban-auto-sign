package pg_test

// 单实例启动锁 e2e：会话级 advisory lock 的互斥、释放与重取。
//
// # 功能
// 同一测试库上的第一条连接取锁成功后，另一条连接的获取必须返回
// ErrStartupLockHeld；持锁者 Close 后，全新会话能重新取到锁，
// Close 本身不阻塞。断言的是会话级锁的对外行为，不含装配接线。
//
// # 归属
// adapters/pg 的契约测试。会话语义没有诚实的进程内替身，
// 故依赖真实 PostgreSQL。
//
// # 口径
// 缺 YIBAN_TEST_DATABASE_URL 时 SKIP，不把未跑当绿。
// 启动锁按数据库生效，schema 隔离不了；本测试与 assembly 的接线 e2e
// 用 startupLockSerialKey 串行，避免 go test 并行跑包时互抢同一把锁。

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/pg"
)

// startupLockSerialKey 是 e2e 包之间的串行键，与启动锁键不同。
// advisory lock 按数据库生效：两个测试包并行时，谁先持锁谁先跑，
// 不能靠 schema 隔离。
const startupLockSerialKey int64 = 71019999

// lockStartupSerial 在专用连接上等串行锁，返回释放函数。
// 等锁上限 2 分钟：对方跑完整装配时不能无限挂住。
func lockStartupSerial(t *testing.T, dsn string) func() {
	t.Helper()
	ctx, cancel := context.WithTimeout(t.Context(), 2*time.Minute)
	defer cancel()
	conn, err := pgx.Connect(ctx, dsn)
	if err != nil {
		t.Fatalf("connect serial connection: %v", err)
	}
	if _, err := conn.Exec(ctx, "SELECT pg_advisory_lock($1)", startupLockSerialKey); err != nil {
		_ = conn.Close(context.Background())
		t.Fatalf("acquire serial advisory lock: %v", err)
	}
	return func() { _ = conn.Close(context.Background()) }
}

func TestStartupLockExclusiveUntilReleased(t *testing.T) {
	base := requireDSN(t)
	releaseSerial := lockStartupSerial(t, base)
	defer releaseSerial()
	e := newEnv(t)

	first, err := e.db.AcquireStartupLock(e.ctx)
	if err != nil {
		t.Fatalf("first acquire: %v", err)
	}

	// 第二实例是另一条连接（独立连接池），不是同一会话的重入。
	second, err := pg.Open(e.ctx, pg.Options{DSN: e.dsn, MaxConns: 1})
	if err != nil {
		t.Fatalf("open second pool: %v", err)
	}
	defer second.Close()
	if _, err := second.AcquireStartupLock(e.ctx); !errors.Is(err, pg.ErrStartupLockHeld) {
		t.Fatalf("second acquire error = %v, want ErrStartupLockHeld", err)
	}

	// 显式关闭必须在预算内返回，不能卡在解锁或归还连接上。
	closed := make(chan struct{})
	go func() {
		first.Close()
		close(closed)
	}()
	select {
	case <-closed:
	case <-time.After(10 * time.Second):
		t.Fatal("StartupLock.Close 10s 内未返回")
	}

	// 第三池是全新会话：能取到锁证明服务端已释放，
	// 而不是被持锁者的同一会话重入。
	third, err := pg.Open(e.ctx, pg.Options{DSN: e.dsn, MaxConns: 1})
	if err != nil {
		t.Fatalf("open third pool: %v", err)
	}
	defer third.Close()
	reacquired, err := third.AcquireStartupLock(e.ctx)
	if err != nil {
		t.Fatalf("reacquire after release: %v", err)
	}
	reacquired.Close()
	reacquired.Close()

	// Close 后再取仍是新会话成功：释放可重复，锁不会残留在池连接上。
	secondLock, err := second.AcquireStartupLock(e.ctx)
	if err != nil {
		t.Fatalf("reacquire from second pool: %v", err)
	}
	secondLock.Close()
}
