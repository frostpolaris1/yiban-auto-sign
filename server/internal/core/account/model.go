// Package account holds the account, credential and status value objects.
//
// Status is the account state machine. It is a separate vocabulary from
// task state (core/status) even where the words overlap.
package account

import "time"

// Status is the lifecycle state of a signing account.
type Status string

// Account statuses. The set is frozen for v1. credential_error is written by
// the engine in P4 only.
const (
	StatusPending         Status = "pending"
	StatusActive          Status = "active"
	StatusRejected        Status = "rejected"
	StatusPausedAdmin     Status = "paused_admin"
	StatusPausedUser      Status = "paused_user"
	StatusCredentialError Status = "credential_error"
)

// Valid reports whether s is a known status.
func (s Status) Valid() bool {
	switch s {
	case StatusPending, StatusActive, StatusRejected, StatusPausedAdmin, StatusPausedUser, StatusCredentialError:
		return true
	default:
		return false
	}
}

// Active reports whether the account may take part in signing. Only active
// accounts are claimed by the engine.
func (s Status) Active() bool { return s == StatusActive }

// Record is one row of accounts.
type Record struct {
	ID           string
	SchoolID     string
	OwnerUserID  string
	Phone        string
	Status       Status
	RejectReason string
	ReviewedAt   *time.Time
	ReviewedBy   string
	CreatedAt    time.Time
	UpdatedAt    time.Time
	DeletedAt    *time.Time
	DeletedBy    string
}

// New is the input of an account creation. ID is optional.
type New struct {
	ID          string
	SchoolID    string
	OwnerUserID string
	Phone       string
}

// Credential is one encrypted credential row. The plaintext never reaches the
// database; the adapter stores these bytes unchanged.
type Credential struct {
	AccountID      string
	SchoolID       string
	Alg            string
	KID            string
	AADVersion     int16
	Nonce          []byte
	Ciphertext     []byte
	Version        int32
	CreatedAt      time.Time
	UpdatedAt      time.Time
	RekeyedAt      *time.Time
	LastVerifiedAt *time.Time
}
