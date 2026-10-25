// Package session 提供内存版 SessionIssuer。
//
// # 功能
// 令牌签发、解析、撤销与每用户上限淘汰，全部在进程内存内完成。
// 本包**不**持久化会话、**不**做密码校验、**不**清库。
// 数据库会话仓储见 adapters/pg（本包不接线）。
//
// # 归属
// adapters 层设施包。cmd/server 用它装配 HTTP 服务。
// 单进程是明文设计不变量，故内存态可行。
//
// # 复用
// Memory、NewMemory、NewMemoryWithTTL；
// 常量 DefaultIdle、DefaultAbsolute、MaxPerUser。
//
// # 口径
// 内存条目的键是令牌的 SHA-256 摘要，转储不泄露可用令牌。
// CSRF 令牌每次读取时派生，**从不存储**。
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

// 会话时长取自决策 12.9。
const (
	DefaultIdle     = 30 * time.Minute
	DefaultAbsolute = 12 * time.Hour

	// MaxPerUser 限制每用户有效会话数。
	// 新设备登录淘汰最旧会话，**不无界增长**。
	MaxPerUser = 20
)

type key [sha256.Size]byte

type entry struct {
	session  auth.Session
	created  time.Time
	lastSeen time.Time
}

// Memory 是并发安全的内存 SessionIssuer。
//
// 条目以令牌的 SHA-256 为键，故内存转储不泄露可用令牌。
// CSRF 令牌每次读取时由令牌与密钥派生，**从不存储**。
type Memory struct {
	mu       sync.Mutex
	clock    clock.Clock
	idle     time.Duration
	absolute time.Duration
	secret   []byte
	entries  map[key]entry
	byUser   map[string]map[key]struct{}
}

// NewMemory 用默认时长构造存储。
func NewMemory(c clock.Clock, secret []byte) *Memory {
	return NewMemoryWithTTL(c, DefaultIdle, DefaultAbsolute, secret)
}

// NewMemoryWithTTL 用显式时长构造存储。
// 非正值回退默认值。
// 空密钥换成随机密钥，**重启即让旧会话失效**。
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

// Issue 为 user 签发新令牌。
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

// Resolve 返回 token 对应的有效会话。
// 绝对过期或空闲超时都会当场删除该条目。
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

// Revoke 丢弃 token。幂等。
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

// RevokeAllForUser 丢弃 userID 的全部会话。幂等。
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

// pruneLocked 删除过期条目。
// Issue 调用它，防止废弃会话**无限累积**。
func (m *Memory) pruneLocked(now time.Time) {
	for k, e := range m.entries {
		if now.Sub(e.created) >= m.absolute || now.Sub(e.lastSeen) >= m.idle {
			m.dropLocked(k, e.session.User.ID)
		}
	}
}

// evictOverflowLocked 每用户最多保留 MaxPerUser 个有效会话，
// 超出即丢掉最旧的。
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
