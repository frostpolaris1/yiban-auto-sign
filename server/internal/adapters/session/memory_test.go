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

func testUser() auth.User {
	return auth.User{ID: "u1", Email: "u1@example.com", Role: auth.RoleUser, Status: auth.StatusActive}
}

// A revoked or expired token must never resolve again.
func TestMemoryRevokeAndExpiry(t *testing.T) {
	clk := &stepClock{t: time.Date(2026, 10, 25, 9, 0, 0, 0, time.UTC)}
	store := NewMemoryWithTTL(clk, 30*time.Minute, 12*time.Hour)
	ctx := context.Background()

	issued, err := store.Issue(ctx, testUser())
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

	second, err := store.Issue(ctx, testUser())
	if err != nil {
		t.Fatalf("issue 2: %v", err)
	}
	clk.t = clk.t.Add(13 * time.Hour)
	if _, err := store.Resolve(ctx, second.Token); !errors.Is(err, auth.ErrNoSession) {
		t.Fatalf("resolve after absolute expiry = %v, want ErrNoSession", err)
	}
}
