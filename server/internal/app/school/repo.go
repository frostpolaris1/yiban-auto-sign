// Package school declares the school and administrator-binding ports.
//
// The value types live in core/school so the PostgreSQL adapter never imports
// this package (canonical.tsv boundary.adapters).
package school

import (
	"context"

	coreschool "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/school"
)

// Status is the school lifecycle state.
type Status = coreschool.Status

// School statuses.
const (
	StatusActive   = coreschool.StatusActive
	StatusDisabled = coreschool.StatusDisabled
)

// Record is one schools row.
type Record = coreschool.Record

// New is the input of a school creation.
type New = coreschool.New

// Update carries optional school column changes.
type Update = coreschool.Update

// Binding is one admin_schools row.
type Binding = coreschool.AdminBinding

// Repo is the school repository port.
type Repo interface {
	Create(ctx context.Context, in New) (Record, error)
	Get(ctx context.Context, id string) (Record, error)
	GetByCode(ctx context.Context, code string) (Record, error)
	Update(ctx context.Context, id string, in Update) (Record, error)
}

// AdminRepo is the administrator-binding repository port.
//
// Grant is idempotent: granting an existing binding is not an error. Revoke
// deletes the row and reports not_found when no row existed.
type AdminRepo interface {
	Grant(ctx context.Context, binding Binding) error
	Revoke(ctx context.Context, userID, schoolID string) error
	SchoolsForUser(ctx context.Context, userID string) ([]string, error)
}
