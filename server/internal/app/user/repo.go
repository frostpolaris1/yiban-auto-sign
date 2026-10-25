// Package user declares the user repository port.
//
// Record and New live in core/user so the PostgreSQL adapter never imports
// this package (canonical.tsv boundary.adapters). The aliases keep the port
// readable here and guarantee that both sides name the same types.
package user

import (
	"context"
	"time"

	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	coreuser "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/user"
)

// Record is one users row.
type Record = coreuser.Record

// New is the input of a user creation.
type New = coreuser.New

// Repo is the user repository port.
//
// Create enforces the active-user limit: it must run inside a transaction and
// takes the user capacity advisory lock before counting. limit is the
// configured maximum of live users.
type Repo interface {
	Create(ctx context.Context, in New, limit int) (Record, error)
	GetByID(ctx context.Context, id string) (Record, error)
	GetByEmail(ctx context.Context, email string) (Record, error)
	UpdatePassword(ctx context.Context, id, passwordHash string, mustChange bool, at time.Time) error
	SetStatus(ctx context.Context, id string, status coreauth.Status, at time.Time) error
	MarkLogin(ctx context.Context, id string, at time.Time) error
	SoftDelete(ctx context.Context, id, by string, at time.Time) error
}
