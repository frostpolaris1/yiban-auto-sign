package pg_test

// 单实例启动锁 e2e：会话级 advisory lock 的互斥、释放与重取。
//
// # 功能
// 同一测试库上的第一条连接取锁成功后，另一条连接的获取必须返回
// ErrStartupLockHeld；持锁者 Close 后，全新会话能重新取到锁，
// Close 本身不阻塞。持锁连接被服务端终止时，Lost 必须报告失效。
// 断言的是会话级锁与探活的对外行为，不含装配接线。
//
// # 归属
// adapters/pg 的契约测试。会话语义没有诚实的进程内替身，
// 故依赖真实 PostgreSQL。
//
// # 口径
// 缺 YIBAN_TEST_DATABASE_URL 时 SKIP，不把未跑当绿。
// 启动锁按数据库生效，schema 隔离不了；本测试与 assembly、cmd/server
// 的装配用例用 pg.TestSerialLockKey 串行，避免 go test 并行跑包时
// 互抢同一把锁。

import (
	"context"
	"errors"
	"strings"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/pg"
)

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
	for {
		var acquired bool
		if err := conn.QueryRow(ctx, "SELECT pg_try_advisory_lock($1)", pg.TestSerialLockKey).Scan(&acquired); err != nil {
			_ = conn.Close(context.Background())
			t.Fatalf("try serial advisory lock: %v", err)
		}
		if acquired {
			break
		}
		select {
		case <-ctx.Done():
			_ = conn.Close(context.Background())
			t.Fatalf("serial advisory lock not granted within 2m: %v", ctx.Err())
		case <-time.After(100 * time.Millisecond):
		}
	}
	return func() { _ = conn.Close(context.Background()) }
}

func TestStartupLockExclusiveUntilReleased(t *testing.T) {
	base := requireDSN(t)
	releaseSerial := lockStartupSerial(t, base)
	defer releaseSerial()

	first, err := pg.AcquireStartupLock(t.Context(), pg.StartupLockOptions{DSN: base})
	if err != nil {
		t.Fatalf("first acquire: %v", err)
	}

	// 第二实例是一条新连接，不是同一会话的重入。
	if _, err := pg.AcquireStartupLock(t.Context(), pg.StartupLockOptions{DSN: base}); !errors.Is(err, pg.ErrStartupLockHeld) {
		t.Fatalf("second acquire error = %v, want ErrStartupLockHeld", err)
	}

	// 显式关闭必须在预算内返回，不能卡在解锁或关闭连接上。
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

	// 全新会话：能取到锁证明服务端已释放，
	// 而不是被持锁者的同一会话重入；Close 可重复，锁不残留。
	reacquired, err := pg.AcquireStartupLock(t.Context(), pg.StartupLockOptions{DSN: base})
	if err != nil {
		t.Fatalf("reacquire after release: %v", err)
	}
	reacquired.Close()
	reacquired.Close()
}

// withApplicationName 在 DSN 后追加运行时参数，用于精确定位持锁后端。
func withApplicationName(dsn, appName string) string {
	sep := "?"
	if strings.Contains(dsn, "?") {
		sep = "&"
	}
	return dsn + sep + "application_name=" + appName
}

// 持锁连接被服务端掐断后，探活必须把锁判为失效并通知调用方。
// 用独立 application_name 定位后端，只终止持锁会话。
func TestStartupLockLostAfterBackendTerminated(t *testing.T) {
	base := requireDSN(t)
	releaseSerial := lockStartupSerial(t, base)
	defer releaseSerial()

	appName := "startup_lock_hb_" + randomHex(6)
	lock, err := pg.AcquireStartupLock(t.Context(), pg.StartupLockOptions{
		DSN:       withApplicationName(base, appName),
		Heartbeat: 100 * time.Millisecond,
	})
	if err != nil {
		t.Fatalf("acquire startup lock: %v", err)
	}
	defer lock.Close()

	ctx, cancel := context.WithTimeout(t.Context(), 10*time.Second)
	defer cancel()
	admin, err := pgx.Connect(ctx, base)
	if err != nil {
		t.Fatalf("connect admin: %v", err)
	}
	defer func() { _ = admin.Close(context.Background()) }()
	if _, err := admin.Exec(ctx,
		`SELECT pg_terminate_backend(pid) FROM pg_stat_activity
		  WHERE application_name = $1 AND pid <> pg_backend_pid()`, appName); err != nil {
		t.Fatalf("terminate lock backend: %v", err)
	}

	select {
	case err, ok := <-lock.Lost():
		if !ok {
			t.Fatal("Lost 在连接被终止后直接关闭，没有报告错误")
		}
		if err == nil {
			t.Fatal("Lost 报告 nil 错误")
		}
	case <-time.After(10 * time.Second):
		t.Fatal("持锁连接被终止后，探活 10s 内没有报告失效")
	}
}
