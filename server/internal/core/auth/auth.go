// Package auth defines the session contract between HTTP entry points and
// the session store. It is pure: implementations live in adapters.
//
// The session token is a 32-byte random value sent as an HttpOnly cookie. A
// store persists only its SHA-256 hash; the raw token never leaves the client.
package auth

import (
	"context"
	"errors"
)

// CookieName is the session cookie. The __Host- prefix requires Secure,
// Path=/ and no Domain attribute.
const CookieName = "__Host-yiban_session"

// Role is the authorization role of a user.
type Role string

// Roles. The set is frozen for v1.
const (
	RoleMaster Role = "master"
	RoleAdmin  Role = "admin"
	RoleUser   Role = "user"
)

// Status is the account status of a user.
type Status string

// User statuses. A disabled user cannot sign in.
const (
	StatusActive   Status = "active"
	StatusDisabled Status = "disabled"
)

// User is the authenticated principal.
type User struct {
	ID                 string
	Email              string
	Role               Role
	Status             Status
	MustChangePassword bool
}

// Session is the server-side session state attached to a request.
type Session struct {
	User      User
	CSRFToken string
}

// Issued is a freshly created session plus its raw token. The token is
// returned to the caller exactly once and never stored in plain form.
type Issued struct {
	Token string
	Session
}

// ErrNoSession reports a missing, expired or revoked session.
var ErrNoSession = errors.New("auth: no session")

// SessionIssuer creates, resolves and revokes sessions.
//
// Issue always mints a new token: a pre-login session is never reused.
// Resolve treats unknown, expired and revoked tokens the same way and returns
// ErrNoSession. Revoke is idempotent.
type SessionIssuer interface {
	Issue(ctx context.Context, user User) (Issued, error)
	Resolve(ctx context.Context, token string) (Session, error)
	Revoke(ctx context.Context, token string) error
}

type contextKey struct{}

var sessionKey contextKey

// WithSession returns a context that carries sess.
func WithSession(ctx context.Context, sess Session) context.Context {
	return context.WithValue(ctx, sessionKey, sess)
}

// FromContext returns the session carried by ctx.
func FromContext(ctx context.Context) (Session, bool) {
	sess, ok := ctx.Value(sessionKey).(Session)
	return sess, ok
}
