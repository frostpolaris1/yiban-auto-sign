package auth_test

// 二次口令（reauth）用例的单元测试。
//
// # 功能
// 用进程内替身钉住四条不变式：错误口令统一 forbidden、
// 用户不存在同垫时同错误、窗口内 5 次失败后 429、成功清零。
// 另钉住「失败不写审计」。本文件**不**连数据库，**不**发 HTTP。
//
// # 口径
// 真库与真 HTTP 行为由 cmd/server 的 e2e 负责。
// 口令全部取固定合成值。

import (
	"errors"
	"io"
	"log/slog"
	"sync"
	"testing"
	"time"

	appauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/auth"
	appuser "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/user"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
)

// discardLogger 丢弃测试日志输出。
func discardLogger() *slog.Logger {
	return slog.New(slog.NewTextHandler(io.Discard, nil))
}

// mutableClock 让节流窗口测试能推时间。Fixed 的字段复制进接口后改不到。
type mutableClock struct {
	mu sync.Mutex
	t  time.Time
}

func newMutableClock() *mutableClock {
	return &mutableClock{t: time.Date(2026, 11, 6, 9, 0, 0, 0, time.UTC)}
}

func (c *mutableClock) Now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.t
}

func (c *mutableClock) advance(d time.Duration) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.t = c.t.Add(d)
}

// newHarnessWithClock 构造带可推时间源的用例骨架。
func newHarnessWithClock(t *testing.T, clk *mutableClock) *harness {
	t.Helper()
	h := &harness{
		users:         newFakeUsers(),
		sessions:      &fakeSessions{},
		resets:        &fakeResets{},
		registrations: &fakeRegistrations{},
		audits:        &fakeAudits{},
		kdf:           &countingKDF{},
	}
	svc, err := appauth.NewService(appauth.Deps{
		Users:         h.users,
		Sessions:      h.sessions,
		OtherSessions: h.sessions,
		Resets:        h.resets,
		Registrations: h.registrations,
		Audits:        h.audits,
		Tx:            fakeTx{},
		Clock:         clk,
		UserLimit:     10000,
		KDF:           h.kdf,
		Logger:        discardLogger(),
	})
	if err != nil {
		t.Fatalf("NewService: %v", err)
	}
	h.svc = svc
	return h
}

// seedActiveUser 写一个 argon2id 哈希的 active 用户并返回明文口令。
func seedActiveUser(t *testing.T, h *harness, id string) string {
	t.Helper()
	const password = "right-password-42"
	hash, err := coreauth.HashPassword(password)
	if err != nil {
		t.Fatalf("HashPassword: %v", err)
	}
	h.users.byID[id] = appuser.Record{
		ID: id, Email: id + "@example.com", PasswordHash: hash,
		Role: coreauth.RoleUser, Status: coreauth.StatusActive,
	}
	return password
}

// 正确口令通过，且不写任何审计。
func TestReauthenticateAcceptsCorrectPassword(t *testing.T) {
	h := newHarness(t)
	password := seedActiveUser(t, h, "u1")

	if err := h.svc.Reauthenticate(t.Context(), "u1", password); err != nil {
		t.Fatalf("Reauthenticate with the right password: %v", err)
	}
	if len(h.audits.actions) != 0 {
		t.Fatalf("audit actions = %v, want none", h.audits.actions)
	}
}

// 错误口令统一 forbidden；用户不存在同垫时同错误；都不写审计。
func TestReauthenticateRejectsWrongAndUnknown(t *testing.T) {
	h := newHarness(t)
	seedActiveUser(t, h, "u1")

	wrong := h.svc.Reauthenticate(t.Context(), "u1", "wrong-password-42")
	if wrong != appauth.ErrReauthFailed {
		t.Fatalf("wrong password error = %v, want ErrReauthFailed", wrong)
	}
	if errs.From(wrong).Code != errs.CodeForbidden {
		t.Fatalf("wrong password code = %q, want forbidden", errs.From(wrong).Code)
	}
	unknown := h.svc.Reauthenticate(t.Context(), "ghost", "wrong-password-42")
	if unknown != appauth.ErrReauthFailed {
		t.Fatalf("unknown user error = %v, want ErrReauthFailed", unknown)
	}
	if len(h.audits.actions) != 0 {
		t.Fatalf("failed reauth wrote audit rows: %v", h.audits.actions)
	}
	// 停用用户同样 forbidden：active 校验在校验路径上。
	h.users.byID["u1"] = appuser.Record{
		ID: "u1", Email: "u1@example.com",
		PasswordHash: h.users.byID["u1"].PasswordHash,
		Role:         coreauth.RoleUser, Status: coreauth.StatusDisabled,
	}
	if err := h.svc.Reauthenticate(t.Context(), "u1", "right-password-42"); err != appauth.ErrReauthFailed {
		t.Fatalf("disabled user error = %v, want ErrReauthFailed", err)
	}
}

// 窗口内第 6 次尝试 429，即使口令正确；窗口滑过之后恢复。
func TestReauthenticateThrottlesAfterFiveFailures(t *testing.T) {
	clk := newMutableClock()
	h := newHarnessWithClock(t, clk)
	password := seedActiveUser(t, h, "u1")
	ctx := t.Context()

	for i := 0; i < appauth.ReauthFailureLimit; i++ {
		if err := h.svc.Reauthenticate(ctx, "u1", "wrong-password-42"); err != appauth.ErrReauthFailed {
			t.Fatalf("failure %d error = %v, want ErrReauthFailed", i+1, err)
		}
	}
	// 第 6 次：即使口令正确也 429，不再付 KDF 成本。
	verifies := h.kdf.verifies.Load()
	err := h.svc.Reauthenticate(ctx, "u1", password)
	if err == nil {
		t.Fatal("sixth attempt = nil error, want throttled")
	}
	if errs.From(err).Code != errs.CodeRateLimited {
		t.Fatalf("sixth attempt code = %q, want rate_limited", errs.From(err).Code)
	}
	if h.kdf.verifies.Load() != verifies {
		t.Fatal("a throttled attempt still ran a KDF verify")
	}
	// 窗口滑过（5 分钟后）：计数清空，正确口令通过。
	clk.advance(appauth.ReauthWindow + time.Second)
	if err := h.svc.Reauthenticate(ctx, "u1", password); err != nil {
		t.Fatalf("after the window: %v", err)
	}
}

// 成功一次清零：3 次失败后成功，再 5 次失败才 429。
func TestReauthenticateSuccessClearsCounter(t *testing.T) {
	clk := newMutableClock()
	h := newHarnessWithClock(t, clk)
	password := seedActiveUser(t, h, "u1")
	ctx := t.Context()

	for i := 0; i < 3; i++ {
		if err := h.svc.Reauthenticate(ctx, "u1", "wrong-password-42"); err != appauth.ErrReauthFailed {
			t.Fatalf("failure %d = %v", i+1, err)
		}
	}
	if err := h.svc.Reauthenticate(ctx, "u1", password); err != nil {
		t.Fatalf("success after failures: %v", err)
	}
	for i := 0; i < appauth.ReauthFailureLimit; i++ {
		if err := h.svc.Reauthenticate(ctx, "u1", "wrong-password-42"); err != appauth.ErrReauthFailed {
			t.Fatalf("failure %d after reset = %v", i+1, err)
		}
	}
	err := h.svc.Reauthenticate(ctx, "u1", password)
	if err == nil {
		t.Fatal("sixth failure after reset = nil error, want throttled")
	}
	if errs.From(err).Code != errs.CodeRateLimited {
		t.Fatalf("sixth failure after reset code = %v, want rate_limited", err)
	}
}

// 并发占坑：20 个 goroutine 同用户同错口令，恰好 5 个付 KDF 并失败，
// 其余 15 个不得付 KDF，直接节流。占坑与判限在同一临界区才守得住这条界。
func TestReauthenticateThrottleIsConcurrencySafe(t *testing.T) {
	h := newHarness(t)
	seedActiveUser(t, h, "u1")
	ctx := t.Context()

	const attempts = 20
	var wg sync.WaitGroup
	results := make([]error, attempts)
	start := make(chan struct{})
	for i := 0; i < attempts; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			<-start
			results[i] = h.svc.Reauthenticate(ctx, "u1", "wrong-password-42")
		}(i)
	}
	close(start)
	wg.Wait()

	failed, throttled := 0, 0
	for _, err := range results {
		if err == nil {
			t.Fatal("a wrong password must never succeed")
		}
		switch {
		case errors.Is(err, appauth.ErrReauthFailed):
			failed++
		default:
			var throttledErr *appauth.ReauthThrottled
			if !errors.As(err, &throttledErr) {
				t.Fatalf("unexpected error = %v", err)
			}
			if throttledErr.RetryAfter <= 0 {
				t.Fatalf("throttled error carries no Retry-After: %v", throttledErr.RetryAfter)
			}
			throttled++
		}
	}
	if failed != appauth.ReauthFailureLimit {
		t.Fatalf("paid attempts = %d, want %d", failed, appauth.ReauthFailureLimit)
	}
	if throttled != attempts-appauth.ReauthFailureLimit {
		t.Fatalf("throttled = %d, want %d", throttled, attempts-appauth.ReauthFailureLimit)
	}
	if h.kdf.verifies.Load() != appauth.ReauthFailureLimit {
		t.Fatalf("KDF verifies = %d, want %d", h.kdf.verifies.Load(), appauth.ReauthFailureLimit)
	}
}

// 节流按用户分桶：另一个用户的失败不挤占本用户的额度。
func TestReauthenticateThrottleIsPerUser(t *testing.T) {
	h := newHarness(t)
	seedActiveUser(t, h, "u1")
	seedActiveUser(t, h, "u2")
	ctx := t.Context()

	for i := 0; i < appauth.ReauthFailureLimit; i++ {
		if err := h.svc.Reauthenticate(ctx, "u1", "wrong-password-42"); err != appauth.ErrReauthFailed {
			t.Fatalf("failure %d = %v", i+1, err)
		}
	}
	if err := h.svc.Reauthenticate(ctx, "u2", "right-password-42"); err != nil {
		t.Fatalf("another user was throttled by u1's failures: %v", err)
	}
}
