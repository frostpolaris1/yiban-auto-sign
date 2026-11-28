package pg

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
)

// 单实例启动锁：会话级 advisory lock，进程存活期间由一条专用长连持有。
//
// # 功能
// AcquireStartupLock 用固定键尝试取锁：取不到即刻返回 ErrStartupLockHeld，
// 不等待、不重试；Close 显式解锁并归还连接。
// 锁是会话级的：连接断开（进程退出、崩溃、池关闭）即由服务端释放，
// 不留下需要人工清理的陈旧锁。
//
// # 归属
// adapters 层设施。组合根在装配连接池之后、开始对外服务之前调用；
// 是否允许启动的判据由调用方持有，本类型只报告锁的归属。
//
// # 口径
// 锁键固定，跨重启稳定；见 locks.go 的 StartupLockKey。
// 持锁连接从池中借出且不归还，池的实际可用连接数少 1（默认 8 → 7）。
// 解锁失败不阻断关闭：连接随后销毁时服务端同样释放会话锁。

// startupUnlockTimeout 是 Close 里显式解锁的预算。
// 停机路径的调用方 ctx 已取消，解锁不能复用它。
const startupUnlockTimeout = 5 * time.Second

// ErrStartupLockHeld 表示同库已有进程持有启动锁。
// 调用方据此拒绝启动；建连失败、SQL 失败等其他错误
// 不得映射成本错误。
var ErrStartupLockHeld = errors.New("pg: startup lock is held by another instance")

// StartupLock 是启动锁的持有者。零值不可用，用 AcquireStartupLock 构造。
type StartupLock struct {
	conn *pgxpool.Conn
}

// AcquireStartupLock 在专用长连上取启动锁。
//
// 返回 ErrStartupLockHeld 时锁仍属另一会话。返回 nil 错误时，
// 调用方必须持有返回值并在退出前 Close，否则同进程再次启动会被自己挡住。
func (d *DB) AcquireStartupLock(ctx context.Context) (*StartupLock, error) {
	conn, err := d.pool.Acquire(ctx)
	if err != nil {
		return nil, fmt.Errorf("pg: acquire startup lock connection: %w", mapError(err))
	}
	var acquired bool
	if err := conn.QueryRow(ctx, "SELECT pg_try_advisory_lock($1)", StartupLockKey).Scan(&acquired); err != nil {
		conn.Release()
		return nil, fmt.Errorf("pg: try startup lock: %w", mapError(err))
	}
	if !acquired {
		conn.Release()
		return nil, ErrStartupLockHeld
	}
	return &StartupLock{conn: conn}, nil
}

// Close 解锁并归还连接。可重复调用。
func (l *StartupLock) Close() {
	if l == nil || l.conn == nil {
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), startupUnlockTimeout)
	defer cancel()
	// 解锁失败不阻断关闭：连接紧接着归还并随池关闭销毁，
	// 会话结束时服务端同样释放该锁。
	_, _ = l.conn.Exec(ctx, "SELECT pg_advisory_unlock($1)", StartupLockKey)
	l.conn.Release()
	l.conn = nil
}
