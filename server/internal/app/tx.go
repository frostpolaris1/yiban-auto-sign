// Package app holds use-case ports that more than one domain needs.
package app

import "context"

// TxManager runs fn inside one database transaction.
//
// The transaction is passed through the context. A nested call reuses the
// outer transaction instead of opening a second one. The implementation uses
// READ COMMITTED: capacity counting depends on a fresh snapshot after the
// advisory lock is granted (schema §6.5).
type TxManager interface {
	InTx(ctx context.Context, fn func(ctx context.Context) error) error
}
