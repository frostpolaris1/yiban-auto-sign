package pg

import (
	"context"
	"errors"

	"github.com/jackc/pgx/v5"
)

// ErrNoTransaction 表示一次必须在事务内运行的写入落在了事务外。
// 容量计数与审计链只有在「锁、计数、写入同属一个事务」时才正确，
// 故存储层**拒绝执行**，不静默做一件更弱的事。
var ErrNoTransaction = errors.New("pg: write requires an explicit transaction")

type txKey struct{}

// InTx 在一个 READ COMMITTED 事务内运行 fn。
//
// READ COMMITTED 是必需项，不是装饰：
// 容量语句在 advisory lock 授予之后才统计活跃行。
// 在 REPEATABLE READ 下快照取于第一条语句，
// 等过锁的事务会统计到旧行，从而突破上限（schema §6.5）。
//
// 嵌套 InTx 复用外层事务。
// 只有最外层函数体返回 nil 时才提交。
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
		// 提交成功后这里报 ErrTxClosed，忽略即可。
		_ = tx.Rollback(context.WithoutCancel(ctx))
	}()
	// 给行锁与 advisory lock 的等待设上界。
	// 连接池的 statement_timeout 是 lock_timeout 覆盖不到时的第二道上界。
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
