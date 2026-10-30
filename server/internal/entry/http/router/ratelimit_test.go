package router_test

// 入站限流的入口层判据：
//   - 每 IP 与每账号是两道独立的闸；
//   - 登录按归一邮箱计，大小写变体共用一桶；
//   - 无可信代理时 X-Forwarded-For 不改变桶；
//   - 429 带 Retry-After，且发生在用例（KDF）之前。

import (
	"context"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/gin-gonic/gin"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	ratelimit "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/ratelimit"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/router"
)

// countingAuth 记录用例被调用的次数：限流命中时用例不得再被调用。
type countingAuth struct {
	loginErr error
	logins   int
	resets   int
}

func (s *countingAuth) Login(context.Context, string, string) (auth.Issued, error) {
	s.logins++
	return auth.Issued{}, s.loginErr
}

func (s *countingAuth) Logout(context.Context, auth.Session, string) error { return nil }

func (s *countingAuth) ForgotPassword(context.Context, string) error { return nil }

func (s *countingAuth) ResetPassword(context.Context, string, string) error {
	s.resets++
	return errs.New(errs.CodeUnauthorized, "invalid or expired reset token")
}

func (s *countingAuth) ChangePassword(context.Context, auth.Session, string, string, string) error {
	return nil
}

func mustLimiter(t *testing.T, p ratelimit.Quota) *ratelimit.Throttle {
	t.Helper()
	l, err := ratelimit.NewThrottle(p)
	if err != nil {
		t.Fatalf("NewThrottle: %v", err)
	}
	return l
}

func newLimitedEngine(t *testing.T, svc router.AuthService, guard *ratelimit.Guard) *gin.Engine {
	t.Helper()
	engine, err := router.New(router.Options{
		Sessions:  newStubIssuer(),
		Auth:      svc,
		Clock:     clock.Fixed{T: time.Date(2026, 10, 30, 8, 0, 0, 0, time.UTC)},
		Logger:    slog.New(slog.NewTextHandler(io.Discard, nil)),
		RateLimit: guard,
	})
	if err != nil {
		t.Fatalf("router.New: %v", err)
	}
	return engine
}

// 登录超过每 IP 阈值回 429；Retry-After 是正整数秒。
// 第二次请求不得走到用例。
func TestLoginRateLimitedByIP(t *testing.T) {
	svc := &countingAuth{loginErr: errs.New(errs.CodeUnauthorized, "invalid email or password")}
	guard := &ratelimit.Guard{
		IP:      mustLimiter(t, ratelimit.Quota{PerMinute: 1, Burst: 1}),
		Account: mustLimiter(t, ratelimit.Quota{PerMinute: 1000, Burst: 1000}),
	}
	engine := newLimitedEngine(t, svc, guard)
	body := `{"email":"a@example.com","password":"x"}`
	first := serve(engine, httptest.NewRequest(http.MethodPost, "/api/auth/login", strings.NewReader(body)))
	if first.Code != http.StatusUnauthorized {
		t.Fatalf("first status = %d, want 401; body=%q", first.Code, first.Body.String())
	}
	second := serve(engine, httptest.NewRequest(http.MethodPost, "/api/auth/login", strings.NewReader(body)))
	if second.Code != http.StatusTooManyRequests {
		t.Fatalf("second status = %d, want 429; body=%q", second.Code, second.Body.String())
	}
	if retry := second.Header().Get("Retry-After"); retry == "" || retry == "0" {
		t.Fatalf("Retry-After = %q, want 正整数秒", retry)
	}
	if env := decodeEnvelope(t, second); env.Error.Code != "rate_limited" {
		t.Fatalf("code = %q, want rate_limited", env.Error.Code)
	}
	if svc.logins != 1 {
		t.Fatalf("用例调用 = %d, want 1（限流后不得再调用）", svc.logins)
	}
}

// 每账号阈值按归一邮箱计：大小写变体共用一桶，别的邮箱不受影响。
func TestLoginRateLimitedByNormalizedEmail(t *testing.T) {
	svc := &countingAuth{loginErr: errs.New(errs.CodeUnauthorized, "invalid email or password")}
	guard := &ratelimit.Guard{
		IP:      mustLimiter(t, ratelimit.Quota{PerMinute: 1000, Burst: 1000}),
		Account: mustLimiter(t, ratelimit.Quota{PerMinute: 1, Burst: 1}),
	}
	engine := newLimitedEngine(t, svc, guard)
	post := func(email string) *httptest.ResponseRecorder {
		body := `{"email":"` + email + `","password":"x"}`
		return serve(engine, httptest.NewRequest(http.MethodPost, "/api/auth/login", strings.NewReader(body)))
	}
	if rec := post("a@example.com"); rec.Code != http.StatusUnauthorized {
		t.Fatalf("first status = %d: %s", rec.Code, rec.Body.String())
	}
	if rec := post("A@Example.COM"); rec.Code != http.StatusTooManyRequests {
		t.Fatalf("大小写变体 status = %d, want 429: %s", rec.Code, rec.Body.String())
	}
	if rec := post("b@example.com"); rec.Code != http.StatusUnauthorized {
		t.Fatalf("另一个邮箱 status = %d, want 401: %s", rec.Code, rec.Body.String())
	}
}

// 无可信代理时 X-Forwarded-For 不改变桶：伪造头不能绕开每 IP 闸。
func TestLoginIgnoresSpoofedForwardedFor(t *testing.T) {
	svc := &countingAuth{loginErr: errs.New(errs.CodeUnauthorized, "invalid email or password")}
	guard := &ratelimit.Guard{
		IP:      mustLimiter(t, ratelimit.Quota{PerMinute: 1, Burst: 1}),
		Account: mustLimiter(t, ratelimit.Quota{PerMinute: 1000, Burst: 1000}),
	}
	engine := newLimitedEngine(t, svc, guard)
	post := func(spoof string) *httptest.ResponseRecorder {
		req := httptest.NewRequest(http.MethodPost, "/api/auth/login",
			strings.NewReader(`{"email":"a@example.com","password":"x"}`))
		req.RemoteAddr = "198.51.100.9:5555"
		req.Header.Set("X-Forwarded-For", spoof)
		return serve(engine, req)
	}
	if rec := post("203.0.113.1"); rec.Code != http.StatusUnauthorized {
		t.Fatalf("first status = %d: %s", rec.Code, rec.Body.String())
	}
	if rec := post("203.0.113.2"); rec.Code != http.StatusTooManyRequests {
		t.Fatalf("伪造新 XFF status = %d, want 429（同一对端共用桶）", rec.Code)
	}
}

// 可信对端才采信 XFF：不同客户端地址落不同桶。
func TestLoginUsesForwardedForFromTrustedProxy(t *testing.T) {
	svc := &countingAuth{loginErr: errs.New(errs.CodeUnauthorized, "invalid email or password")}
	trusted, err := ratelimit.ParseTrustedProxies("127.0.0.1/32")
	if err != nil {
		t.Fatalf("ParseTrustedProxies: %v", err)
	}
	guard := &ratelimit.Guard{
		IP:      mustLimiter(t, ratelimit.Quota{PerMinute: 1, Burst: 1}),
		Account: mustLimiter(t, ratelimit.Quota{PerMinute: 1000, Burst: 1000}),
		Trusted: trusted,
	}
	engine := newLimitedEngine(t, svc, guard)
	post := func(client string) *httptest.ResponseRecorder {
		req := httptest.NewRequest(http.MethodPost, "/api/auth/login",
			strings.NewReader(`{"email":"a@example.com","password":"x"}`))
		req.RemoteAddr = "127.0.0.1:5555"
		req.Header.Set("X-Forwarded-For", client)
		return serve(engine, req)
	}
	if rec := post("203.0.113.1"); rec.Code != http.StatusUnauthorized {
		t.Fatalf("client1 status = %d: %s", rec.Code, rec.Body.String())
	}
	if rec := post("203.0.113.2"); rec.Code != http.StatusUnauthorized {
		t.Fatalf("client2 status = %d, want 401（不同客户端不同桶）: %s", rec.Code, rec.Body.String())
	}
	if rec := post("203.0.113.1"); rec.Code != http.StatusTooManyRequests {
		t.Fatalf("client1 第二次 status = %d, want 429", rec.Code)
	}
}

// 重置令牌的垃圾请求在用例之前被挡；用例只跑一次。
func TestResetRateLimitedBeforeUseCase(t *testing.T) {
	svc := &countingAuth{}
	guard := &ratelimit.Guard{
		IP:      mustLimiter(t, ratelimit.Quota{PerMinute: 1000, Burst: 1000}),
		Account: mustLimiter(t, ratelimit.Quota{PerMinute: 1, Burst: 1}),
	}
	engine := newLimitedEngine(t, svc, guard)
	body := `{"token":"garbage-token","new_password":"new-password"}`
	first := serve(engine, httptest.NewRequest(http.MethodPost, "/api/auth/reset-password", strings.NewReader(body)))
	if first.Code != http.StatusUnauthorized {
		t.Fatalf("first status = %d, want 401: %s", first.Code, first.Body.String())
	}
	second := serve(engine, httptest.NewRequest(http.MethodPost, "/api/auth/reset-password", strings.NewReader(body)))
	if second.Code != http.StatusTooManyRequests {
		t.Fatalf("second status = %d, want 429: %s", second.Code, second.Body.String())
	}
	if svc.resets != 1 {
		t.Fatalf("重置用例调用 = %d, want 1", svc.resets)
	}
}
