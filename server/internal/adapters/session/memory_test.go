package session

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
)

type stepClock struct{ t time.Time }

func (c *stepClock) Now() time.Time { return c.t }

func testUser(id string) auth.User {
	return auth.User{ID: id, Email: id + "@example.com", Role: auth.RoleUser, Status: auth.StatusActive}
}

var testSecret = []byte("0123456789abcdef0123456789abcdef")

func newStore(clk *stepClock) *Memory {
	return NewMemoryWithTTL(clk, 30*time.Minute, 12*time.Hour, testSecret)
}

// 已撤销或已过期的令牌绝不能再次解析成功。
func TestMemoryRevokeAndExpiry(t *testing.T) {
	clk := &stepClock{t: time.Date(2026, 10, 25, 9, 0, 0, 0, time.UTC)}
	store := newStore(clk)
	ctx := context.Background()

	issued, err := store.Issue(ctx, testUser("u1"))
	if err != nil {
		t.Fatalf("issue: %v", err)
	}
	if issued.Token == "" || issued.CSRFToken == "" {
		t.Fatal("issue returned an empty token")
	}
	if _, err := store.Resolve(ctx, issued.Token); err != nil {
		t.Fatalf("resolve after issue: %v", err)
	}

	if err := store.Revoke(ctx, issued.Token); err != nil {
		t.Fatalf("revoke: %v", err)
	}
	if _, err := store.Resolve(ctx, issued.Token); !errors.Is(err, auth.ErrNoSession) {
		t.Fatalf("resolve after revoke = %v, want ErrNoSession", err)
	}

	second, err := store.Issue(ctx, testUser("u1"))
	if err != nil {
		t.Fatalf("issue 2: %v", err)
	}
	clk.t = clk.t.Add(13 * time.Hour)
	if _, err := store.Resolve(ctx, second.Token); !errors.Is(err, auth.ErrNoSession) {
		t.Fatalf("resolve after absolute expiry = %v, want ErrNoSession", err)
	}
}

// Resolve 刷新空闲窗口；超过空闲时长的间隔，
// 在绝对过期之前就会结束会话。
func TestMemoryIdleSliding(t *testing.T) {
	clk := &stepClock{t: time.Date(2026, 10, 25, 9, 0, 0, 0, time.UTC)}
	store := newStore(clk)
	ctx := context.Background()

	issued, err := store.Issue(ctx, testUser("u1"))
	if err != nil {
		t.Fatal(err)
	}

	clk.t = clk.t.Add(20 * time.Minute)
	if _, err := store.Resolve(ctx, issued.Token); err != nil {
		t.Fatalf("resolve at +20m: %v", err)
	}

	clk.t = clk.t.Add(25 * time.Minute)
	if _, err := store.Resolve(ctx, issued.Token); err != nil {
		t.Fatalf("resolve at +45m after activity: %v", err)
	}

	clk.t = clk.t.Add(31 * time.Minute)
	if _, err := store.Resolve(ctx, issued.Token); !errors.Is(err, auth.ErrNoSession) {
		t.Fatalf("resolve after idle gap = %v, want ErrNoSession", err)
	}
}

func TestMemoryRevokeIsIdempotent(t *testing.T) {
	clk := &stepClock{t: time.Date(2026, 10, 25, 9, 0, 0, 0, time.UTC)}
	store := newStore(clk)
	ctx := context.Background()

	issued, err := store.Issue(ctx, testUser("u1"))
	if err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 3; i++ {
		if err := store.Revoke(ctx, issued.Token); err != nil {
			t.Fatalf("revoke %d: %v", i, err)
		}
	}
}

// RevokeAllForUser 结束一个用户的全部会话，其他用户不受影响。
func TestMemoryRevokeAllForUser(t *testing.T) {
	clk := &stepClock{t: time.Date(2026, 10, 25, 9, 0, 0, 0, time.UTC)}
	store := newStore(clk)
	ctx := context.Background()

	first, err := store.Issue(ctx, testUser("u1"))
	if err != nil {
		t.Fatal(err)
	}
	second, err := store.Issue(ctx, testUser("u1"))
	if err != nil {
		t.Fatal(err)
	}
	other, err := store.Issue(ctx, testUser("u2"))
	if err != nil {
		t.Fatal(err)
	}

	if err := store.RevokeAllForUser(ctx, "u1"); err != nil {
		t.Fatal(err)
	}
	for _, token := range []string{first.Token, second.Token} {
		if _, err := store.Resolve(ctx, token); !errors.Is(err, auth.ErrNoSession) {
			t.Fatalf("resolve revoked u1 token = %v, want ErrNoSession", err)
		}
	}
	if _, err := store.Resolve(ctx, other.Token); err != nil {
		t.Fatalf("resolve u2 token: %v", err)
	}
	if err := store.RevokeAllForUser(ctx, "u1"); err != nil {
		t.Fatalf("second revoke all: %v", err)
	}
}

// Issue 每次都签发全新令牌。
func TestMemoryIssueIsUnique(t *testing.T) {
	clk := &stepClock{t: time.Date(2026, 10, 25, 9, 0, 0, 0, time.UTC)}
	store := newStore(clk)
	ctx := context.Background()

	seen := map[string]bool{}
	for i := 0; i < 50; i++ {
		issued, err := store.Issue(ctx, testUser("u1"))
		if err != nil {
			t.Fatal(err)
		}
		if seen[issued.Token] {
			t.Fatal("issue returned a duplicate token")
		}
		seen[issued.Token] = true
	}
}

// CSRF 令牌靠派生得来，从不存储，且同一会话令牌下保持稳定。
func TestMemoryCSRFDerivation(t *testing.T) {
	clk := &stepClock{t: time.Date(2026, 10, 25, 9, 0, 0, 0, time.UTC)}
	store := newStore(clk)
	ctx := context.Background()

	issued, err := store.Issue(ctx, testUser("u1"))
	if err != nil {
		t.Fatal(err)
	}
	if issued.CSRFToken == issued.Token {
		t.Fatal("csrf token equals the session token")
	}
	resolved, err := store.Resolve(ctx, issued.Token)
	if err != nil {
		t.Fatal(err)
	}
	if resolved.CSRFToken != issued.CSRFToken {
		t.Fatalf("csrf token changed: %q then %q", issued.CSRFToken, resolved.CSRFToken)
	}
	if want := auth.CSRFToken(testSecret, issued.Token); resolved.CSRFToken != want {
		t.Fatalf("csrf token is not the derived value")
	}
}

// 一个用户最多持有 MaxPerUser 个有效会话。
func TestMemoryPerUserCap(t *testing.T) {
	clk := &stepClock{t: time.Date(2026, 10, 25, 9, 0, 0, 0, time.UTC)}
	store := newStore(clk)
	ctx := context.Background()

	var tokens []string
	for i := 0; i < MaxPerUser+5; i++ {
		issued, err := store.Issue(ctx, testUser("u1"))
		if err != nil {
			t.Fatal(err)
		}
		tokens = append(tokens, issued.Token)
		clk.t = clk.t.Add(time.Second)
	}
	live := 0
	for _, token := range tokens {
		if _, err := store.Resolve(ctx, token); err == nil {
			live++
		}
	}
	if live != MaxPerUser {
		t.Fatalf("live sessions = %d, want %d", live, MaxPerUser)
	}
}
