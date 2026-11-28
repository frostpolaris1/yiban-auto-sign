package pg

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"sync"
	"time"

	"github.com/jackc/pgx/v5"
)

// 单实例启动锁：会话级 advisory lock，进程存活期间由一条专用长连持有。
//
// # 功能
// AcquireStartupLock 用固定键尝试取锁：取不到即刻返回 ErrStartupLockHeld，
// 不等待、不重试；Close 显式解锁并关闭连接。
// 锁是会话级的：连接断开（进程退出、崩溃、连接销毁）即由服务端释放，
// 不留下需要人工清理的陈旧锁。
//
// # 失效窗口
// 进程存活而连接先断的场景必须显式处理：数据库重启、网络分区、
// 管理员 terminate_backend、探活语句超时。任一发生，服务端都已释放锁，
// 第二个实例可以启动；本进程若继续服务，就会有两个执行循环并存。
// 故持有期间每 DefaultStartupLockHeartbeat 探活一次；探活失败无法证明
// 锁仍属本会话，一律按失效处理，经 Lost 通知调用方 fail-stop。
// fail-stop 的代价是放弃优雅结算：在飞任务不再等待收尾，
// 租约到期后由下一次启动的 Recover 回收。
//
// # 归属
// adapters 层设施。组合根在迁移与建池之前取锁；
// 是否允许启动的判据由调用方持有，本类型只报告锁的归属与存续。
//
// # 口径
// 锁键固定，跨重启稳定；见 locks.go 的 StartupLockKey。
// 锁占一条独立连接，不进入连接池计数，故不再从池里借走一格。
// 解锁失败不阻断关闭：连接随后销毁时服务端同样释放会话锁。

// DefaultStartupLockHeartbeat 是持锁连接的探活周期。
// 周期越短，双实例并存窗口越短；取 20s，留在 30s 以内。
const DefaultStartupLockHeartbeat = 20 * time.Second

// startupLockHeartbeatTimeout 是单次探活的预算。
// 超时同样按失效处理：无法确认锁仍属于本会话。
const startupLockHeartbeatTimeout = 5 * time.Second

// startupUnlockTimeout 是 Close 里显式解锁的预算。
// 停机路径的调用方 ctx 已取消，解锁不能复用它。
const startupUnlockTimeout = 5 * time.Second

// ErrStartupLockHeld 表示同库已有进程持有启动锁。
// 调用方据此拒绝启动；建连失败、SQL 失败等其他错误
// 不得映射成本错误。
var ErrStartupLockHeld = errors.New("pg: startup lock is held by another instance")

// StartupLockOptions 配置启动锁。
type StartupLockOptions struct {
	// DSN 是 PostgreSQL 连接串。必填。
	DSN string
	// Heartbeat 是探活周期。零值取 DefaultStartupLockHeartbeat。
	Heartbeat time.Duration
}

// StartupLock 是启动锁的持有者。零值不可用，用 AcquireStartupLock 构造。
type StartupLock struct {
	conn      *pgx.Conn
	heartbeat time.Duration
	// lost 先收到探活错误再关闭；正常 Close 时直接关闭。
	lost chan error
	stop chan struct{}
	done chan struct{}
	// mu 串行化持锁连接上的探活与解锁；pgx.Conn 不支持并发使用。
	mu        sync.Mutex
	closeOnce sync.Once
	lostOnce  sync.Once
}

// AcquireStartupLock 在独立长连上取启动锁。
//
// 返回 ErrStartupLockHeld 时锁仍属另一会话。返回 nil 错误时，
// 调用方必须持有返回值并在退出前 Close，否则同进程再次启动会被自己挡住。
func AcquireStartupLock(ctx context.Context, opts StartupLockOptions) (*StartupLock, error) {
	dsn := strings.TrimSpace(opts.DSN)
	if dsn == "" {
		return nil, errors.New("pg: startup lock DSN is required")
	}
	heartbeat := opts.Heartbeat
	if heartbeat <= 0 {
		heartbeat = DefaultStartupLockHeartbeat
	}
	conn, err := pgx.Connect(ctx, dsn)
	if err != nil {
		return nil, fmt.Errorf("pg: acquire startup lock connection: %w", mapError(err))
	}
	var acquired bool
	if err := conn.QueryRow(ctx, "SELECT pg_try_advisory_lock($1)", StartupLockKey).Scan(&acquired); err != nil {
		_ = conn.Close(context.Background())
		return nil, fmt.Errorf("pg: try startup lock: %w", mapError(err))
	}
	if !acquired {
		_ = conn.Close(context.Background())
		return nil, ErrStartupLockHeld
	}
	lock := &StartupLock{
		conn:      conn,
		heartbeat: heartbeat,
		lost:      make(chan error, 1),
		stop:      make(chan struct{}),
		done:      make(chan struct{}),
	}
	go lock.watch()
	return lock, nil
}

// Lost 返回锁连接失效通知：探活失败时先收到错误，随后通道关闭；
// 正常 Close 时通道直接关闭。调用方收到错误必须 fail-stop。
func (l *StartupLock) Lost() <-chan error {
	return l.lost
}

// watch 周期探活，失败即通知并停止。
// 不能证明锁仍属本会话时，继续服务会与第二实例并存。
func (l *StartupLock) watch() {
	defer close(l.done)
	ticker := time.NewTicker(l.heartbeat)
	defer ticker.Stop()
	for {
		select {
		case <-l.stop:
			return
		case <-ticker.C:
			ctx, cancel := context.WithTimeout(context.Background(), startupLockHeartbeatTimeout)
			l.mu.Lock()
			err := l.conn.Ping(ctx)
			l.mu.Unlock()
			cancel()
			if err != nil {
				l.lost <- err
				l.closeLost()
				return
			}
		}
	}
}

func (l *StartupLock) closeLost() {
	l.lostOnce.Do(func() { close(l.lost) })
}

// Close 停止探活、解锁并关闭连接。可重复调用。
func (l *StartupLock) Close() {
	if l == nil {
		return
	}
	l.closeOnce.Do(func() {
		close(l.stop)
		<-l.done
		l.closeLost()
		ctx, cancel := context.WithTimeout(context.Background(), startupUnlockTimeout)
		defer cancel()
		// 解锁失败不阻断关闭：连接紧接着关闭，会话结束时服务端同样释放该锁。
		l.mu.Lock()
		_, _ = l.conn.Exec(ctx, "SELECT pg_advisory_unlock($1)", StartupLockKey)
		l.mu.Unlock()
		_ = l.conn.Close(context.Background())
	})
}
