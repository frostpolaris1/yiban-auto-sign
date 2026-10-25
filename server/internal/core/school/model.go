// Package school holds the school and administrator-binding value objects.
package school

import (
	"time"

	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
)

// Status is the lifecycle state of a school. A disabled school keeps its data
// and stops new work; it is not deleted.
type Status string

// School statuses. The set is frozen for v1.
const (
	StatusActive   Status = "active"
	StatusDisabled Status = "disabled"
)

// Valid reports whether s is a known status.
func (s Status) Valid() bool { return s == StatusActive || s == StatusDisabled }

// Record is one row of schools.
type Record struct {
	ID        string
	Code      string
	Name      string
	Status    Status
	TimeZone  string
	CreatedAt time.Time
	UpdatedAt time.Time
}

// New is the input of a school creation. TimeZone may be empty; the database
// default applies.
type New struct {
	ID       string
	Code     string
	Name     string
	TimeZone string
}

// Update carries optional column changes. A nil field is not changed.
type Update struct {
	Name     *string
	Status   *Status
	TimeZone *string
}

// AdminBinding binds one user to one school as an administrator.
type AdminBinding struct {
	UserID    string
	SchoolID  string
	GrantedBy string
	CreatedAt time.Time
	Email     string
	Role      coreauth.Role
}
