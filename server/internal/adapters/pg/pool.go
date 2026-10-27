// Package pg 用 pgx 在 PostgreSQL 上实现全部仓储端口。
//
// # 功能
// 实现 app 层声明的仓储端口、事务边界、迁移执行与 SQLSTATE 归一。
// 本包**不**做 HTTP 编码、**不**编排用例、**不**持有业务判据。
//
// # 归属
// adapters 层设施包。**全仓只有本包与 adapters/legacy（一次性迁移导入器）
// 写 SQL**；SQLSTATE 归一仍只在 Adapters/pg。legacy 是显式登记的例外，
// 不是旁路先例。
// 数据库失败在此映射为 core/errs 码，用例层不见驱动错误。
//
// # 复用
// DB、Options、Open 及默认值常量；
// InTx、Exec、QueryInt64、QueryString；
// Migrate、EmbeddedVersion、ErrDatabaseAhead、ErrNoTransaction；
// 各仓储：UserRepo、SessionRepo、SchoolRepo、AdminSchoolRepo、
// AccountRepo、AuditRepo、PasswordResetRepo。
//
// # 口径
// 会话时区固定 UTC；statement_timeout 缺省 5s。
// advisory lock 用固定数字，锁序固定：容量在前，审计在后。
package pg

import (
	"context"
	"errors"
	"fmt"
	"strconv"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"
)

// 连接池默认值。
// statement_timeout 是 advisory lock 等待的兜底上界：
// PostgreSQL 未承诺 lock_timeout 覆盖 advisory lock，
// 故卡住的锁必须由上层取消（schema §6.2）。
const (
	DefaultMaxConns         = int32(8)
	DefaultStatementTimeout = 5 * time.Second
)

// Options 配置连接池。
type Options struct {
	// DSN 是 PostgreSQL 连接串。必填。
	DSN string
	// MaxConns 限制后端连接数。零值取 DefaultMaxConns。
	MaxConns int32
	// MinConns 保持空闲连接数。零值取 pgxpool 默认。
	MinConns int32
	// StatementTimeout 取消执行过久的语句，也是锁等待的上界。
	// 零值取 DefaultStatementTimeout。
	StatementTimeout time.Duration
}

// DB 是连接池加仓储所需的辅助方法。
//
// 零值不可用。请用 Open 构造，用 Close 释放。
type DB struct {
	pool *pgxpool.Pool
}

// Open 建池并确认服务端应答。
// 会话时区固定 UTC；statement_timeout 设到每条连接上。
func Open(ctx context.Context, opts Options) (*DB, error) {
	if strings.TrimSpace(opts.DSN) == "" {
		return nil, errors.New("pg: DSN is required")
	}
	cfg, err := pgxpool.ParseConfig(opts.DSN)
	if err != nil {
		return nil, fmt.Errorf("pg: parse DSN: %w", err)
	}
	if opts.MaxConns <= 0 {
		opts.MaxConns = DefaultMaxConns
	}
	if opts.StatementTimeout <= 0 {
		opts.StatementTimeout = DefaultStatementTimeout
	}
	cfg.MaxConns = opts.MaxConns
	if opts.MinConns > 0 {
		cfg.MinConns = opts.MinConns
	}
	if cfg.ConnConfig.RuntimeParams == nil {
		cfg.ConnConfig.RuntimeParams = map[string]string{}
	}
	cfg.ConnConfig.RuntimeParams["timezone"] = "UTC"
	cfg.ConnConfig.RuntimeParams["statement_timeout"] = strconv.FormatInt(opts.StatementTimeout.Milliseconds(), 10)

	pool, err := pgxpool.NewWithConfig(ctx, cfg)
	if err != nil {
		return nil, fmt.Errorf("pg: create pool: %w", err)
	}
	db := &DB{pool: pool}
	if err := db.Ping(ctx); err != nil {
		pool.Close()
		return nil, fmt.Errorf("pg: ping: %w", mapError(err))
	}
	return db, nil
}

// Close 释放全部连接。
func (d *DB) Close() {
	if d == nil || d.pool == nil {
		return
	}
	d.pool.Close()
}

// Ping 确认服务端应答。
func (d *DB) Ping(ctx context.Context) error { return d.pool.Ping(ctx) }

// Exec 在显式事务之外执行一条语句。
// 它只服务维护任务与测试；用例一律走仓储。返回受影响行数。
func (d *DB) Exec(ctx context.Context, sql string, args ...any) (int64, error) {
	tag, err := d.pool.Exec(ctx, sql, args...)
	if err != nil {
		return 0, mapError(err)
	}
	return tag.RowsAffected(), nil
}

// QueryInt64 从单行读一个整数列。
func (d *DB) QueryInt64(ctx context.Context, sql string, args ...any) (int64, error) {
	var value int64
	if err := d.pool.QueryRow(ctx, sql, args...).Scan(&value); err != nil {
		return 0, mapError(err)
	}
	return value, nil
}

// QueryString 从单行读一个文本列。
func (d *DB) QueryString(ctx context.Context, sql string, args ...any) (string, error) {
	var value string
	if err := d.pool.QueryRow(ctx, sql, args...).Scan(&value); err != nil {
		return "", mapError(err)
	}
	return value, nil
}

// queryer 是连接池与事务共用的语句接口。
type queryer interface {
	Exec(ctx context.Context, sql string, args ...any) (pgconn.CommandTag, error)
	Query(ctx context.Context, sql string, args ...any) (pgx.Rows, error)
	QueryRow(ctx context.Context, sql string, args ...any) pgx.Row
}

// q 返回 ctx 携带的事务；没有事务时返回连接池。
func (d *DB) q(ctx context.Context) queryer {
	if tx, ok := ctx.Value(txKey{}).(pgx.Tx); ok {
		return tx
	}
	return d.pool
}

// hasTx 报告 ctx 是否携带显式事务。
func (d *DB) hasTx(ctx context.Context) bool {
	_, ok := ctx.Value(txKey{}).(pgx.Tx)
	return ok
}

// txQueryer 返回 ctx 携带的显式事务。
//
// 没有事务时报 ErrNoTransaction，**绝不回退到连接池**。
// q 会回落到连接池，故「先判 hasTx、再调 q」的写法
// 只在两次判断都看同一 ctx 时成立。
// 审计链必须经本函数取连接：回滚要能带走审计行（W2C-R2-01）。
// 容量写入仍走 q 加前置 hasTx，行为等价；统一到本函数属后续批。
func (d *DB) txQueryer(ctx context.Context) (queryer, error) {
	if tx, ok := ctx.Value(txKey{}).(pgx.Tx); ok {
		return tx, nil
	}
	return nil, ErrNoTransaction
}
