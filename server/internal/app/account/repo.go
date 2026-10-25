// Package account declares the account and credential repository port.
//
// The value types live in core/account so the PostgreSQL adapter never
// imports this package (canonical.tsv boundary.adapters).
package account

import (
	"context"
	"time"

	coreaccount "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/account"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
)

// Status is the account lifecycle state.
type Status = coreaccount.Status

// Account statuses.
const (
	StatusPending         = coreaccount.StatusPending
	StatusActive          = coreaccount.StatusActive
	StatusRejected        = coreaccount.StatusRejected
	StatusPausedAdmin     = coreaccount.StatusPausedAdmin
	StatusPausedUser      = coreaccount.StatusPausedUser
	StatusCredentialError = coreaccount.StatusCredentialError
)

// Record is one accounts row.
type Record = coreaccount.Record

// New is the input of an account creation.
type New = coreaccount.New

// Credential is one credentials row.
type Credential = coreaccount.Credential

// Repo is the account repository port.
//
// Create and Restore enforce the active-account limit. Both must run inside a
// transaction: they take the account capacity advisory lock and count live
// rows in the same transaction. A zero-row conditional statement reports
// errs.CodeCapacity.
//
// SetStatus is a compare-and-set: it changes the status only when the row is
// currently from. Zero rows report errs.CodeConflict. Status transition rules
// belong to the use case, not to this port.
//
// Get, SetStatus, SoftDelete, Restore and Credential apply scope. An
// out-of-scope row is reported as errs.CodeNotFound; it is never reported as
// forbidden.
type Repo interface {
	Create(ctx context.Context, in New, limit int) (Record, error)
	Get(ctx context.Context, scope corescope.Scope, id string) (Record, error)
	SetStatus(ctx context.Context, scope corescope.Scope, id string, from, to Status, at time.Time, reason string) (Record, error)
	SoftDelete(ctx context.Context, scope corescope.Scope, id, by string, at time.Time) error
	Restore(ctx context.Context, scope corescope.Scope, id string, limit int) (Record, error)
	Credential(ctx context.Context, scope corescope.Scope, accountID string) (Credential, error)
	InsertCredential(ctx context.Context, in Credential) error
	ReplaceCredential(ctx context.Context, in Credential, expectedVersion int32) error
}
