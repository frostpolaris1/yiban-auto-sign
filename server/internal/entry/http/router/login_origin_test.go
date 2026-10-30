package router_test

// 登录跨站判据：Origin 与 Sec-Fetch-Site 必须与部署同源。
// 登录免 CSRF，跨站表单顶号只能在这里挡。

import (
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/gin-gonic/gin"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	ratelimit "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/ratelimit"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/router"
)

func newOriginEngine(t *testing.T, allowed string) *gin.Engine {
	t.Helper()
	engine, err := router.New(router.Options{
		Sessions:    newStubIssuer(),
		Auth:        stubAuth{loginErr: errs.New(errs.CodeUnauthorized, "invalid email or password")},
		Clock:       clock.Fixed{T: time.Date(2026, 10, 30, 8, 0, 0, 0, time.UTC)},
		Logger:      slog.New(slog.NewTextHandler(io.Discard, nil)),
		LoginOrigin: allowed,
	})
	if err != nil {
		t.Fatalf("router.New: %v", err)
	}
	return engine
}

func postLogin(t *testing.T, engine *gin.Engine, headers map[string]string) *httptest.ResponseRecorder {
	t.Helper()
	req := httptest.NewRequest(http.MethodPost, "/api/auth/login",
		strings.NewReader(`{"email":"a@example.com","password":"x"}`))
	for k, v := range headers {
		req.Header.Set(k, v)
	}
	return serve(engine, req)
}

func TestLoginOriginAllowed(t *testing.T) {
	engine := newOriginEngine(t, "https://app.example.test")
	cases := []struct {
		name    string
		headers map[string]string
		want    int
	}{
		{"配置来源", map[string]string{"Origin": "https://app.example.test"}, http.StatusUnauthorized},
		{"同源请求头", map[string]string{"Sec-Fetch-Site": "same-origin"}, http.StatusUnauthorized},
		{"无浏览器头", nil, http.StatusUnauthorized},
		{"跨站 Origin", map[string]string{"Origin": "https://evil.example"}, http.StatusForbidden},
		{"空来源", map[string]string{"Origin": "null"}, http.StatusForbidden},
		{"跨站 Fetch-Site", map[string]string{"Sec-Fetch-Site": "cross-site"}, http.StatusForbidden},
		{"同站非同源", map[string]string{"Sec-Fetch-Site": "same-site"}, http.StatusForbidden},
		// 浏览器自证 same-origin 时，允许反向代理改写 Host。
		{"同源头覆盖来源", map[string]string{"Sec-Fetch-Site": "same-origin", "Origin": "https://evil.example"}, http.StatusUnauthorized},
	}
	for _, c := range cases {
		if got := postLogin(t, engine, c.headers).Code; got != c.want {
			t.Fatalf("%s status = %d, want %d", c.name, got, c.want)
		}
	}
}

// 未配置允许基址时退回同源判定：Origin 必须等于请求自身的 scheme://Host。
func TestLoginOriginFallsBackToRequestOrigin(t *testing.T) {
	engine := newOriginEngine(t, "")
	req := httptest.NewRequest(http.MethodPost, "/api/auth/login",
		strings.NewReader(`{"email":"a@example.com","password":"x"}`))
	req.Host = "app.example.test"
	req.Header.Set("Origin", "http://app.example.test")
	if got := serve(engine, req).Code; got != http.StatusUnauthorized {
		t.Fatalf("same-origin status = %d, want 401", got)
	}
	req = httptest.NewRequest(http.MethodPost, "/api/auth/login",
		strings.NewReader(`{"email":"a@example.com","password":"x"}`))
	req.Host = "app.example.test"
	req.Header.Set("Origin", "http://other.example.test")
	if got := serve(engine, req).Code; got != http.StatusForbidden {
		t.Fatalf("cross-origin status = %d, want 403", got)
	}
}

// 可信代理后的 TLS 终结：X-Forwarded-Proto 决定 scheme。
// 不可信对端的同名头不得采信。
func TestLoginOriginUsesForwardedProtoFromTrustedProxy(t *testing.T) {
	trusted, err := ratelimit.ParseTrustedProxies("127.0.0.1/32")
	if err != nil {
		t.Fatalf("ParseTrustedProxies: %v", err)
	}
	engine, err := router.New(router.Options{
		Sessions:       newStubIssuer(),
		Auth:           stubAuth{loginErr: errs.New(errs.CodeUnauthorized, "invalid email or password")},
		Clock:          clock.Fixed{T: time.Date(2026, 10, 30, 8, 0, 0, 0, time.UTC)},
		Logger:         slog.New(slog.NewTextHandler(io.Discard, nil)),
		RateLimit:      &ratelimit.Guard{Trusted: trusted},
		LoginOrigin:    "",
		TrustedProxies: trusted,
	})
	if err != nil {
		t.Fatalf("router.New: %v", err)
	}
	post := func(remote string) int {
		req := httptest.NewRequest(http.MethodPost, "/api/auth/login",
			strings.NewReader(`{"email":"a@example.com","password":"x"}`))
		req.Host = "app.example.test"
		req.RemoteAddr = remote
		req.Header.Set("Origin", "https://app.example.test")
		req.Header.Set("X-Forwarded-Proto", "https")
		return serve(engine, req).Code
	}
	if got := post("127.0.0.1:1234"); got != http.StatusUnauthorized {
		t.Fatalf("trusted proxy status = %d, want 401", got)
	}
	if got := post("203.0.113.9:1234"); got != http.StatusForbidden {
		t.Fatalf("untrusted peer status = %d, want 403", got)
	}

	// 关闭限流不影响登录 scheme：可信表与限流器解耦。
	off, err := router.New(router.Options{
		Sessions:       newStubIssuer(),
		Auth:           stubAuth{loginErr: errs.New(errs.CodeUnauthorized, "invalid email or password")},
		Clock:          clock.Fixed{T: time.Date(2026, 10, 30, 8, 0, 0, 0, time.UTC)},
		Logger:         slog.New(slog.NewTextHandler(io.Discard, nil)),
		TrustedProxies: trusted,
	})
	if err != nil {
		t.Fatalf("router.New: %v", err)
	}
	req := httptest.NewRequest(http.MethodPost, "/api/auth/login",
		strings.NewReader(`{"email":"a@example.com","password":"x"}`))
	req.Host = "app.example.test"
	req.RemoteAddr = "127.0.0.1:1234"
	req.Header.Set("Origin", "https://app.example.test")
	req.Header.Set("X-Forwarded-Proto", "https")
	if got := serve(off, req).Code; got != http.StatusUnauthorized {
		t.Fatalf("limiter-off trusted proxy status = %d, want 401", got)
	}
}
