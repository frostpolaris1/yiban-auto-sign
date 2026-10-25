// Package user holds the user value objects shared by use cases and the
// PostgreSQL adapter.
//
// The repository port is declared in internal/app/user so the adapter never
// imports app (canonical.tsv boundary.adapters).
package user

import (
	"time"

	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
)

// Record is one row of users.
type Record struct {
	ID                 string
	Email              string
	PasswordHash       string
	Role               coreauth.Role
	Status             coreauth.Status
	MustChangePassword bool
	PasswordChangedAt  *time.Time
	LastLoginAt        *time.Time
	CreatedAt          time.Time
	UpdatedAt          time.Time
	DeletedAt          *time.Time
	DeletedBy          string
}

// New is the input of a user creation. ID is optional; the store allocates a
// UUIDv7 when it is empty.
type New struct {
	ID                 string
	Email              string
	PasswordHash       string
	Role               coreauth.Role
	MustChangePassword bool
}
