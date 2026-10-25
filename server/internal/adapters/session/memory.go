// Package session provides the in-memory SessionIssuer. It serves tests and
// single-process development; the database-backed store lands in W1.
package session

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"sort"
	"sync"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
)

// Session lifetimes from decision 12.9.
const (
	DefaultIdle     = 30 * time.Minute
	DefaultAbsolute = 12 * time.Hour

	// MaxPerUser bounds how many live sessions one user can hold. Signing in
	// on a new device evicts the oldest session instead of growing without
	// bound.
	MaxPerUser = 20
)

type key [sha256.Size]byte

type entry struct {
	session  auth.Session
	created  time.Time
	lastSeen time.Time
}

// Memory is a concurrency-safe in-memory SessionIssuer.
//
// It keeps entries keyed by the SHA-256 of the token, so a memory dump does
// not reveal usable tokens. The CSRF token is derived from the token and a
// secret on every read; it is never stored.
type Memory struct {
	mu       sync.Mutex
	clock    clock.Clock
	idle     time.Duration
	absolute time.Duration
	secret   []byte
	entries  map[key]entry
	byUser   map[string]map[key]struct{}
}

// NewMemory builds a store with the default lifetimes.
func NewMemory(c clock.Clock, secret []byte) *Memory {
	return NewMemoryWithTTL(c, DefaultIdle, DefaultAbsolute, secret)
}

// NewMemoryWithTTL builds a store with explicit lifetimes. Non-positive
// values fall back to the defaults. An empty secret is replaced with a random
// one, which invalidates sessions on restart.
func NewMemoryWithTTL(c clock.Clock, idle, absolute time.Duration, secret []byte) *Memory {
	if c == nil {
		c = clock.System{}
	}
	if idle <= 0 {
		idle = DefaultIdle
	}
	if absolute <= 0 {
		absolute = DefaultAbsolute
	}
	if len(secret) == 0 {
		secret = make([]byte, 32)
		if _, err := rand.Read(secret); err != nil {
			panic("session: cannot read random bytes: " + err.Error())
		}
	}
	return &Memory{
		clock:    c,
		idle:     idle,
		absolute: absolute,
		secret:   append([]byte(nil), secret...),
		entries:  make(map[key]entry),
		byUser:   make(map[string]map[key]struct{}),
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
	now := m.clock.Now()
	sess := auth.Session{User: user, CSRFToken: auth.CSRFToken(m.secret, token)}

	m.mu.Lock()
	defer m.mu.Unlock()
	m.pruneLocked(now)
	k := sha256.Sum256([]byte(token))
	m.entries[k] = entry{session: sess, created: now, lastSeen: now}
	m.indexLocked(user.ID, k)
	m.evictOverflowLocked(user.ID)
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
	k := sha256.Sum256([]byte(token))
	now := m.clock.Now()

	m.mu.Lock()
	defer m.mu.Unlock()
	e, ok := m.entries[k]
	if !ok {
		return auth.Session{}, auth.ErrNoSession
	}
	if now.Sub(e.created) >= m.absolute || now.Sub(e.lastSeen) >= m.idle {
		m.dropLocked(k, e.session.User.ID)
		return auth.Session{}, auth.ErrNoSession
	}
	e.lastSeen = now
	e.session.CSRFToken = auth.CSRFToken(m.secret, token)
	m.entries[k] = e
	return e.session, nil
}

// Revoke drops token. It is idempotent.
func (m *Memory) Revoke(ctx context.Context, token string) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	k := sha256.Sum256([]byte(token))
	m.mu.Lock()
	defer m.mu.Unlock()
	e, ok := m.entries[k]
	if !ok {
		return nil
	}
	m.dropLocked(k, e.session.User.ID)
	return nil
}

// RevokeAllForUser drops every session of userID. It is idempotent.
func (m *Memory) RevokeAllForUser(ctx context.Context, userID string) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	for k := range m.byUser[userID] {
		delete(m.entries, k)
	}
	delete(m.byUser, userID)
	return nil
}

func (m *Memory) indexLocked(userID string, k key) {
	set, ok := m.byUser[userID]
	if !ok {
		set = make(map[key]struct{})
		m.byUser[userID] = set
	}
	set[k] = struct{}{}
}

func (m *Memory) dropLocked(k key, userID string) {
	delete(m.entries, k)
	if set, ok := m.byUser[userID]; ok {
		delete(set, k)
		if len(set) == 0 {
			delete(m.byUser, userID)
		}
	}
}

// pruneLocked removes expired entries. Issue calls it so abandoned sessions do
// not accumulate forever.
func (m *Memory) pruneLocked(now time.Time) {
	for k, e := range m.entries {
		if now.Sub(e.created) >= m.absolute || now.Sub(e.lastSeen) >= m.idle {
			m.dropLocked(k, e.session.User.ID)
		}
	}
}

// evictOverflowLocked keeps at most MaxPerUser live sessions per user and
// drops the oldest ones.
func (m *Memory) evictOverflowLocked(userID string) {
	set := m.byUser[userID]
	if len(set) <= MaxPerUser {
		return
	}
	type item struct {
		k        key
		lastSeen time.Time
	}
	items := make([]item, 0, len(set))
	for k := range set {
		items = append(items, item{k: k, lastSeen: m.entries[k].lastSeen})
	}
	sort.Slice(items, func(i, j int) bool { return items[i].lastSeen.Before(items[j].lastSeen) })
	for i := 0; i < len(items)-MaxPerUser; i++ {
		delete(m.entries, items[i].k)
		delete(set, items[i].k)
	}
}

func randomToken() (string, error) {
	var b [32]byte
	if _, err := rand.Read(b[:]); err != nil {
		return "", err
	}
	return base64.RawURLEncoding.EncodeToString(b[:]), nil
}
