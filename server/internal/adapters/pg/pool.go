// Package pg implements the repository ports on PostgreSQL with pgx.
//
// This package is the only place that knows SQL and SQLSTATE codes. It maps
// database failures to core/errs codes so use cases never inspect PostgreSQL
// errors.
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

// Defaults for a pool. The statement timeout is the fallback that bounds an
// advisory-lock wait: PostgreSQL does not document lock_timeout as covering
// advisory locks, so a stuck lock must be cancelled from above (schema §6.2).
const (
	DefaultMaxConns         = int32(8)
	DefaultStatementTimeout = 5 * time.Second
)

// Options configures a pool.
type Options struct {
	// DSN is the PostgreSQL connection string. Required.
	DSN string
	// MaxConns bounds the number of backend connections. Zero uses
	// DefaultMaxConns.
	MaxConns int32
	// MinConns keeps idle connections open. Zero uses the pgxpool default.
	MinConns int32
	// StatementTimeout cancels a statement that runs too long and is the
	// upper bound for lock waits. Zero uses DefaultStatementTimeout.
	StatementTimeout time.Duration
}

// DB is a connection pool plus the helpers repositories use.
//
// The zero value is not usable. Build one with Open and close it with Close.
type DB struct {
	pool *pgxpool.Pool
}

// Open builds a pool and verifies that the server answers. The session time
// zone is pinned to UTC and statement_timeout is set on every connection.
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

// Close releases every connection.
func (d *DB) Close() {
	if d == nil || d.pool == nil {
		return
	}
	d.pool.Close()
}

// Ping verifies that the server answers.
func (d *DB) Ping(ctx context.Context) error { return d.pool.Ping(ctx) }

// Exec runs one statement outside an explicit transaction. It exists for
// maintenance tasks and tests; use cases go through repositories. It returns
// the number of affected rows.
func (d *DB) Exec(ctx context.Context, sql string, args ...any) (int64, error) {
	tag, err := d.pool.Exec(ctx, sql, args...)
	if err != nil {
		return 0, mapError(err)
	}
	return tag.RowsAffected(), nil
}

// QueryInt64 reads one integer column from one row.
func (d *DB) QueryInt64(ctx context.Context, sql string, args ...any) (int64, error) {
	var value int64
	if err := d.pool.QueryRow(ctx, sql, args...).Scan(&value); err != nil {
		return 0, mapError(err)
	}
	return value, nil
}

// QueryString reads one text column from one row.
func (d *DB) QueryString(ctx context.Context, sql string, args ...any) (string, error) {
	var value string
	if err := d.pool.QueryRow(ctx, sql, args...).Scan(&value); err != nil {
		return "", mapError(err)
	}
	return value, nil
}

// queryer is the statement interface shared by a pool and a transaction.
type queryer interface {
	Exec(ctx context.Context, sql string, args ...any) (pgconn.CommandTag, error)
	Query(ctx context.Context, sql string, args ...any) (pgx.Rows, error)
	QueryRow(ctx context.Context, sql string, args ...any) pgx.Row
}

// q returns the transaction carried by ctx, or the pool when there is none.
func (d *DB) q(ctx context.Context) queryer {
	if tx, ok := ctx.Value(txKey{}).(pgx.Tx); ok {
		return tx
	}
	return d.pool
}

// hasTx reports whether ctx carries an explicit transaction.
func (d *DB) hasTx(ctx context.Context) bool {
	_, ok := ctx.Value(txKey{}).(pgx.Tx)
	return ok
}
