// Package auth declares the session and password-reset repository ports.
//
// The row types live in core/auth so the PostgreSQL adapter never imports
// this package (canonical.tsv boundary.adapters).
package auth

import (
	"context"
	"time"

	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
)

// Session is one sessions row.
type Session = coreauth.SessionRow

// NewSession is the input of a session creation.
type NewSession = coreauth.NewSessionRow

// ResolvedSession is a live session joined with its user.
type ResolvedSession = coreauth.ResolvedSession

// PasswordReset is one password_resets row.
type PasswordReset = coreauth.PasswordReset

// NewPasswordReset is the input of a reset token creation.
type NewPasswordReset = coreauth.NewPasswordReset

// SessionRepo is the session repository port.
//
// Resolve returns only live sessions: unknown, revoked, expired and idle
// sessions all produce errs.CodeNotFound. A successful resolve refreshes the
// idle window. Create keeps at most coreauth.MaxSessionsPerUser live sessions
// per user and evicts the oldest.
type SessionRepo interface {
	Create(ctx context.Context, in NewSession) (Session, error)
	Resolve(ctx context.Context, tokenHash []byte, now time.Time, idle time.Duration) (ResolvedSession, error)
	Revoke(ctx context.Context, tokenHash []byte, now time.Time) error
	RevokeAllForUser(ctx context.Context, userID string, now time.Time) (int64, error)
	DeleteExpired(ctx context.Context, before time.Time, limit int) (int64, error)
}

// PasswordResetRepo is the one-time reset token repository port.
//
// Consume marks the row used and returns it. A second Consume, an expired
// token and an unknown token all return errs.CodeNotFound.
type PasswordResetRepo interface {
	Create(ctx context.Context, in NewPasswordReset) (PasswordReset, error)
	Consume(ctx context.Context, tokenHash []byte, now time.Time) (PasswordReset, error)
}
