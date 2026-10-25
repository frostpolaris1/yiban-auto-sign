// Package session provides the in-memory SessionIssuer. It serves tests and
// single-process development; the database-backed store lands in P1.
package session

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"sync"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
)

// Session lifetimes from decision 12.9.
const (
	DefaultIdle     = 30 * time.Minute
	DefaultAbsolute = 12 * time.Hour
)

type entry struct {
	session  auth.Session
	created  time.Time
	lastSeen time.Time
}

// Memory is a concurrency-safe in-memory SessionIssuer.
//
// It keeps entries keyed by the SHA-256 of the token, so a memory dump does
// not reveal usable tokens. The CSRF token is kept as issued because this
// store has no separate delivery channel yet.
type Memory struct {
	mu       sync.Mutex
	clock    clock.Clock
	idle     time.Duration
	absolute time.Duration
	entries  map[[sha256.Size]byte]entry
}

// NewMemory builds a store with the default lifetimes.
func NewMemory(c clock.Clock) *Memory { return NewMemoryWithTTL(c, DefaultIdle, DefaultAbsolute) }

// NewMemoryWithTTL builds a store with explicit lifetimes. Non-positive
// values fall back to the defaults.
func NewMemoryWithTTL(c clock.Clock, idle, absolute time.Duration) *Memory {
	if c == nil {
		c = clock.System{}
	}
	if idle <= 0 {
		idle = DefaultIdle
	}
	if absolute <= 0 {
		absolute = DefaultAbsolute
	}
	return &Memory{
		clock:    c,
		idle:     idle,
		absolute: absolute,
		entries:  make(map[[sha256.Size]byte]entry),
	}
}

// Issue mints a new token for user.
func (m *Memory) Issue(ctx context.Context, user auth.User) (auth.Issued, error) {
	if err := ctx.Err(); err != nil {
		return auth.Issued{}, err
	}
	token, err := randomToken()
	if err != nil {
		return auth.Issued{}, err
	}
	csrf, err := randomToken()
	if err != nil {
		return auth.Issued{}, err
	}
	now := m.clock.Now()
	sess := auth.Session{User: user, CSRFToken: csrf}
	m.mu.Lock()
	m.entries[sha256.Sum256([]byte(token))] = entry{session: sess, created: now, lastSeen: now}
	m.mu.Unlock()
	return auth.Issued{Token: token, Session: sess}, nil
}

// Resolve returns the live session for token.
func (m *Memory) Resolve(ctx context.Context, token string) (auth.Session, error) {
	if err := ctx.Err(); err != nil {
		return auth.Session{}, err
	}
	if token == "" {
		return auth.Session{}, auth.ErrNoSession
	}
	key := sha256.Sum256([]byte(token))
	now := m.clock.Now()

	m.mu.Lock()
	defer m.mu.Unlock()
	e, ok := m.entries[key]
	if !ok {
		return auth.Session{}, auth.ErrNoSession
	}
	if now.Sub(e.created) >= m.absolute || now.Sub(e.lastSeen) >= m.idle {
		delete(m.entries, key)
		return auth.Session{}, auth.ErrNoSession
	}
	e.lastSeen = now
	m.entries[key] = e
	return e.session, nil
}

// Revoke drops token. It is idempotent.
func (m *Memory) Revoke(ctx context.Context, token string) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	m.mu.Lock()
	delete(m.entries, sha256.Sum256([]byte(token)))
	m.mu.Unlock()
	return nil
}

func randomToken() (string, error) {
	var b [32]byte
	if _, err := rand.Read(b[:]); err != nil {
		return "", err
	}
	return base64.RawURLEncoding.EncodeToString(b[:]), nil
}
