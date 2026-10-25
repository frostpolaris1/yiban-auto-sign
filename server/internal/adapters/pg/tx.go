package pg

import (
	"context"
	"errors"

	"github.com/jackc/pgx/v5"
)

// ErrNoTransaction reports a write that must run inside a transaction but did
// not. Capacity counting and audit chaining are only correct when the lock,
// the count and the write share one transaction, so the store refuses instead
// of silently doing a weaker thing.
var ErrNoTransaction = errors.New("pg: write requires an explicit transaction")

type txKey struct{}

// InTx runs fn inside one READ COMMITTED transaction.
//
// READ COMMITTED is required, not cosmetic: the capacity statement counts
// live rows after the advisory lock is granted. Under REPEATABLE READ the
// snapshot is taken at the first statement, so a transaction that waited for
// the lock could count stale rows and exceed the limit (schema §6.5).
//
// A nested InTx reuses the outer transaction. The transaction is committed
// only when the outermost body returns nil.
func (d *DB) InTx(ctx context.Context, fn func(ctx context.Context) error) error {
	if fn == nil {
		return errors.New("pg: nil transaction body")
	}
	if _, ok := ctx.Value(txKey{}).(pgx.Tx); ok {
		return fn(ctx)
	}
	tx, err := d.pool.BeginTx(ctx, pgx.TxOptions{IsoLevel: pgx.ReadCommitted})
	if err != nil {
		return mapError(err)
	}
	defer func() {
		// After a successful commit this reports ErrTxClosed and is ignored.
		_ = tx.Rollback(context.WithoutCancel(ctx))
	}()
	// Bound row locks and advisory lock waits. The pooled statement_timeout
	// is the second bound for cases lock_timeout does not cover.
	if _, err := tx.Exec(ctx, "SET LOCAL lock_timeout = '2s'"); err != nil {
		return mapError(err)
	}
	if err := fn(context.WithValue(ctx, txKey{}, tx)); err != nil {
		return err
	}
	if err := tx.Commit(ctx); err != nil {
		return mapError(err)
	}
	return nil
}
