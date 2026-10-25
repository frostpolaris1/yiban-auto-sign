package router_test

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
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
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/router"
)

// stubIssuer 是 HTTP 面测试用的最小 SessionIssuer。
type stubIssuer struct {
	sessions map[string]auth.Session
	// resolveErr 非空时 Resolve 直接返回该错误，用来模拟数据库故障。
	resolveErr error
}

func newStubIssuer() *stubIssuer {
	return &stubIssuer{sessions: make(map[string]auth.Session)}
}

func (s *stubIssuer) Issue(_ context.Context, user auth.User) (auth.Issued, error) {
	sess := auth.Session{User: user, CSRFToken: "csrf-" + user.ID}
	s.sessions["token-"+user.ID] = sess
	return auth.Issued{Token: "token-" + user.ID, Session: sess}, nil
}

func (s *stubIssuer) Resolve(_ context.Context, token string) (auth.Session, error) {
	if s.resolveErr != nil {
		return auth.Session{}, s.resolveErr
	}
	sess, ok := s.sessions[token]
	if !ok {
		return auth.Session{}, auth.ErrNoSession
	}
	return sess, nil
}

func (s *stubIssuer) Revoke(_ context.Context, token string) error {
	delete(s.sessions, token)
	return nil
}

func (s *stubIssuer) RevokeAllForUser(_ context.Context, userID string) error {
	for token, sess := range s.sessions {
		if sess.User.ID == userID {
			delete(s.sessions, token)
		}
	}
	return nil
}

// errorEnvelope 对应冻结的错误信封形状。
type errorEnvelope struct {
	Error struct {
		Code    string         `json:"code"`
		Message string         `json:"message"`
		Details map[string]any `json:"details"`
		TraceID string         `json:"trace_id"`
	} `json:"error"`
}

func newTestEngine(t *testing.T) (*gin.Engine, *stubIssuer) {
	t.Helper()
	issuer := newStubIssuer()
	return newEngine(t, issuer, slog.New(slog.NewTextHandler(io.Discard, nil))), issuer
}

func newEngine(t *testing.T, issuer auth.SessionIssuer, logger *slog.Logger) *gin.Engine {
	t.Helper()
	gin.SetMode(gin.TestMode)
	engine, err := router.New(router.Options{
		Sessions: issuer,
		Clock:    clock.Fixed{T: time.Date(2026, 10, 25, 9, 0, 0, 0, time.UTC)},
		Logger:   logger,
	})
	if err != nil {
		t.Fatalf("router.New: %v", err)
	}
	return engine
}

func serve(engine *gin.Engine, req *http.Request) *httptest.ResponseRecorder {
	rec := httptest.NewRecorder()
	engine.ServeHTTP(rec, req)
	return rec
}

func decodeEnvelope(t *testing.T, rec *httptest.ResponseRecorder) errorEnvelope {
	t.Helper()
	var env errorEnvelope
	if err := json.Unmarshal(rec.Body.Bytes(), &env); err != nil {
		t.Fatalf("decode envelope: %v; body=%q", err, rec.Body.String())
	}
	return env
}

// E-1：未认证读取 /api/auth/me 返回 401 与冻结信封。
func TestE1UnauthenticatedMe(t *testing.T) {
	engine, _ := newTestEngine(t)
	rec := serve(engine, httptest.NewRequest(http.MethodGet, "/api/auth/me", nil))

	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("status = %d, want 401", rec.Code)
	}
	if got := rec.Header().Get("Content-Type"); got != "application/json; charset=utf-8" {
		t.Fatalf("content-type = %q", got)
	}
	env := decodeEnvelope(t, rec)
	if env.Error.Code != "unauthorized" {
		t.Fatalf("code = %q, want unauthorized", env.Error.Code)
	}
	if env.Error.Message == "" {
		t.Fatal("message is empty")
	}
	if env.Error.Details == nil {
		t.Fatal("details key is missing")
	}
	if env.Error.TraceID == "" {
		t.Fatal("trace_id is empty")
	}
	if got := rec.Header().Get("X-Request-Id"); got != env.Error.TraceID {
		t.Fatalf("header X-Request-Id = %q, body trace_id = %q", got, env.Error.TraceID)
	}
}

// 路由自造请求 id，从不复用客户端请求头，
// 故调用方无法左右审计层记录的内容。
func TestClientRequestIDIsNotReused(t *testing.T) {
	engine, _ := newTestEngine(t)
	for _, client := range []string{"13800138000", "a@b.com", "client-controlled-trace", "06317272289944f6e935132eb62042ec"} {
		req := httptest.NewRequest(http.MethodGet, "/healthz", nil)
		req.Header.Set("X-Request-Id", client)
		rec := serve(engine, req)

		minted := rec.Header().Get("X-Request-Id")
		if minted == client {
			t.Fatalf("client id %q was reused; the router must mint its own", client)
		}
		if len(minted) != 32 {
			t.Fatalf("minted id %q is not 32 hex characters", minted)
		}
		for _, r := range minted {
			isHex := (r >= '0' && r <= '9') || (r >= 'a' && r <= 'f')
			if !isHex {
				t.Fatalf("minted id %q is not lowercase hex", minted)
			}
		}
	}
}

// 同一引擎的两次请求必须得到不同 id：
// 所有请求共用一个 trace 值会让审计关联失效。
func TestRequestIDIsFreshPerRequest(t *testing.T) {
	engine, _ := newTestEngine(t)
	first := serve(engine, httptest.NewRequest(http.MethodGet, "/healthz", nil)).Header().Get("X-Request-Id")
	second := serve(engine, httptest.NewRequest(http.MethodGet, "/healthz", nil)).Header().Get("X-Request-Id")
	if first == "" || first == second {
		t.Fatalf("ids are %q and %q; every request must mint a fresh one", first, second)
	}
}

// 访问日志记录的 id 就是响应头携带的 id，
// 故任何审计行都能对上调用方看到的响应。
func TestAccessLogRequestIDMatchesHeader(t *testing.T) {
	var logBuf bytes.Buffer
	engine := newEngine(t, newStubIssuer(), slog.New(slog.NewTextHandler(&logBuf, nil)))
	req := httptest.NewRequest(http.MethodGet, "/healthz", nil)
	rec := serve(engine, req)

	header := rec.Header().Get("X-Request-Id")
	if !strings.Contains(logBuf.String(), "request_id="+header) {
		t.Fatalf("log lacks the response id %q: %q", header, logBuf.String())
	}
}

// E-2：未知路由返回 404 与冻结信封。
func TestE2UnknownRoute(t *testing.T) {
	engine, _ := newTestEngine(t)
	rec := serve(engine, httptest.NewRequest(http.MethodGet, "/api/does-not-exist", nil))

	if rec.Code != http.StatusNotFound {
		t.Fatalf("status = %d, want 404", rec.Code)
	}
	env := decodeEnvelope(t, rec)
	if env.Error.Code != "not_found" {
		t.Fatalf("code = %q, want not_found", env.Error.Code)
	}
	if env.Error.Details == nil || env.Error.TraceID == "" {
		t.Fatalf("envelope = %#v", env.Error)
	}
}

// E-3：存活探针无会话也回 200。
func TestE3Healthz(t *testing.T) {
	engine, _ := newTestEngine(t)
	rec := serve(engine, httptest.NewRequest(http.MethodGet, "/healthz", nil))

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200; body=%q", rec.Code, rec.Body.String())
	}
	var body struct {
		Status string `json:"status"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if body.Status != "ok" {
		t.Fatalf("status = %q, want ok", body.Status)
	}
}

// E-5：有效会话 cookie 返回当前用户，**绝不返回会话令牌**。
func TestE5MeWithSession(t *testing.T) {
	engine, issuer := newTestEngine(t)
	user := auth.User{
		ID:                 "018f0000-0000-7000-8000-000000000001",
		Email:              "master@example.com",
		Role:               auth.RoleMaster,
		Status:             auth.StatusActive,
		MustChangePassword: false,
	}
	issued, err := issuer.Issue(context.Background(), user)
	if err != nil {
		t.Fatalf("issue: %v", err)
	}

	req := httptest.NewRequest(http.MethodGet, "/api/auth/me", nil)
	req.AddCookie(&http.Cookie{Name: auth.CookieName, Value: issued.Token})
	rec := serve(engine, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200; body=%q", rec.Code, rec.Body.String())
	}
	var body struct {
		ID                 string `json:"id"`
		Email              string `json:"email"`
		Role               string `json:"role"`
		Status             string `json:"status"`
		MustChangePassword bool   `json:"must_change_password"`
		CSRFToken          string `json:"csrf_token"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if body.ID != user.ID || body.Email != user.Email || body.Role != "master" {
		t.Fatalf("body = %#v", body)
	}
	if body.Status != "active" {
		t.Fatalf("status = %q, want active", body.Status)
	}
	if body.CSRFToken != "csrf-"+user.ID {
		t.Fatalf("csrf_token = %q", body.CSRFToken)
	}
	if strings.Contains(rec.Body.String(), issued.Token) {
		t.Fatal("response leaked the session token")
	}
}

// E-8：带会话但不带 CSRF 头的改状态请求被拒。
func TestE8MissingCSRF(t *testing.T) {
	engine, issuer := newTestEngine(t)
	issued, err := issuer.Issue(context.Background(), auth.User{ID: "u1", Email: "u1@example.com", Role: auth.RoleUser, Status: auth.StatusActive})
	if err != nil {
		t.Fatal(err)
	}

	req := httptest.NewRequest(http.MethodPost, "/api/accounts", strings.NewReader("{}"))
	req.Header.Set("Content-Type", "application/json")
	req.AddCookie(&http.Cookie{Name: auth.CookieName, Value: issued.Token})
	rec := serve(engine, req)

	if rec.Code != http.StatusForbidden {
		t.Fatalf("status = %d, want 403; body=%q", rec.Code, rec.Body.String())
	}
	if env := decodeEnvelope(t, rec); env.Error.Code != "forbidden" {
		t.Fatalf("code = %q, want forbidden", env.Error.Code)
	}
}

// E-9：派生令牌匹配时，同一请求通过 CSRF 门禁。
func TestE9ValidCSRF(t *testing.T) {
	engine, issuer := newTestEngine(t)
	issued, err := issuer.Issue(context.Background(), auth.User{ID: "u1", Email: "u1@example.com", Role: auth.RoleUser, Status: auth.StatusActive})
	if err != nil {
		t.Fatal(err)
	}

	req := httptest.NewRequest(http.MethodPost, "/api/accounts", strings.NewReader("{}"))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-CSRF-Token", issued.CSRFToken)
	req.AddCookie(&http.Cookie{Name: auth.CookieName, Value: issued.Token})
	rec := serve(engine, req)

	if rec.Code != http.StatusNotImplemented {
		t.Fatalf("status = %d, want 501; body=%q", rec.Code, rec.Body.String())
	}
}

// E-10：登出销毁会话，故需要 CSRF 令牌。
func TestE10LogoutRequiresCSRF(t *testing.T) {
	engine, issuer := newTestEngine(t)
	issued, err := issuer.Issue(context.Background(), auth.User{ID: "u1", Email: "u1@example.com", Role: auth.RoleUser, Status: auth.StatusActive})
	if err != nil {
		t.Fatal(err)
	}

	req := httptest.NewRequest(http.MethodPost, "/api/auth/logout", nil)
	req.AddCookie(&http.Cookie{Name: auth.CookieName, Value: issued.Token})
	rec := serve(engine, req)
	if rec.Code != http.StatusForbidden {
		t.Fatalf("status = %d, want 403", rec.Code)
	}
}

// 无会话的写请求在 CSRF 门禁之前就被拒。
func TestWriteWithoutSession(t *testing.T) {
	engine, _ := newTestEngine(t)
	req := httptest.NewRequest(http.MethodPost, "/api/accounts", strings.NewReader("{}"))
	rec := serve(engine, req)
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("status = %d, want 401", rec.Code)
	}
}

// E-6：访问日志带路径，但**从不带查询串**。
func TestE6AccessLogHasNoQuery(t *testing.T) {
	var logBuf bytes.Buffer
	engine := newEngine(t, newStubIssuer(), slog.New(slog.NewTextHandler(&logBuf, nil)))
	serve(engine, httptest.NewRequest(http.MethodGet, "/healthz?token=supersecret", nil))

	logged := logBuf.String()
	if !strings.Contains(logged, "path=/healthz") {
		t.Fatalf("log lacks the path: %q", logged)
	}
	if strings.Contains(logged, "supersecret") || strings.Contains(logged, "token=") {
		t.Fatalf("log leaked the query string: %q", logged)
	}
}

type panicIssuer struct{}

func (panicIssuer) Issue(context.Context, auth.User) (auth.Issued, error) {
	return auth.Issued{}, nil
}
func (panicIssuer) Resolve(context.Context, string) (auth.Session, error) { panic("boom") }
func (panicIssuer) Revoke(context.Context, string) error                  { return nil }
func (panicIssuer) RevokeAllForUser(context.Context, string) error        { return nil }

// E-7：panic 变成 500 信封，而不是被掐断的连接。
func TestE7PanicRecovery(t *testing.T) {
	engine := newEngine(t, panicIssuer{}, slog.New(slog.NewTextHandler(io.Discard, nil)))
	req := httptest.NewRequest(http.MethodGet, "/healthz", nil)
	req.AddCookie(&http.Cookie{Name: auth.CookieName, Value: "any"})
	rec := serve(engine, req)

	if rec.Code != http.StatusInternalServerError {
		t.Fatalf("status = %d, want 500", rec.Code)
	}
	env := decodeEnvelope(t, rec)
	if env.Error.Code != "internal_error" {
		t.Fatalf("code = %q, want internal_error", env.Error.Code)
	}
}

// panic 的请求仍产出一行状态 500 的访问日志。
func TestAccessLogCoversPanics(t *testing.T) {
	var logBuf bytes.Buffer
	engine := newEngine(t, panicIssuer{}, slog.New(slog.NewTextHandler(&logBuf, nil)))
	req := httptest.NewRequest(http.MethodGet, "/healthz", nil)
	req.AddCookie(&http.Cookie{Name: auth.CookieName, Value: "any"})
	serve(engine, req)

	logged := logBuf.String()
	if !strings.Contains(logged, "http request") {
		t.Fatalf("no access log line: %q", logged)
	}
	if !strings.Contains(logged, "status=500") {
		t.Fatalf("access log lacks status 500: %q", logged)
	}
}

// 无会话的登出不得被参数绑定拒绝。
// 状态码归本阶段决定；不变式是「不是校验失败」。
func TestLogoutWithoutSession(t *testing.T) {
	engine, _ := newTestEngine(t)
	rec := serve(engine, httptest.NewRequest(http.MethodPost, "/api/auth/logout", nil))
	if rec.Code == http.StatusBadRequest {
		t.Fatalf("session-less logout was rejected by binding: %q", rec.Body.String())
	}
	if rec.Body.Len() > 0 {
		if env := decodeEnvelope(t, rec); env.Error.Code == "validation_failed" {
			t.Fatalf("code = validation_failed, want a non-binding outcome")
		}
	}
}

// stubAuth 是最小认证用例替身，用来让认证端点真正走到请求体绑定。
type stubAuth struct {
	loginErr error
}

func (s stubAuth) Login(context.Context, string, string) (auth.Issued, error) {
	return auth.Issued{}, s.loginErr
}

func (stubAuth) Logout(context.Context, auth.Session, string) error { return nil }

func (stubAuth) ForgotPassword(context.Context, string) error { return nil }

func (stubAuth) ResetPassword(context.Context, string, string) error { return nil }

func newAuthEngine(t *testing.T, issuer auth.SessionIssuer, svc router.AuthService) *gin.Engine {
	t.Helper()
	engine, err := router.New(router.Options{
		Sessions: issuer,
		Auth:     svc,
		Clock:    clock.Fixed{T: time.Date(2026, 10, 25, 9, 0, 0, 0, time.UTC)},
		Logger:   slog.New(slog.NewTextHandler(io.Discard, nil)),
	})
	if err != nil {
		t.Fatalf("router.New: %v", err)
	}
	return engine
}

// 数据库故障不是「未登录」。
// 会话解析返回非 ErrNoSession 的错误时必须回 500，
// 否则一次数据库故障会表现成全体用户被登出。
func TestSessionResolutionFailureIsNotUnauthenticated(t *testing.T) {
	engine, issuer := newTestEngine(t)
	req := func() *http.Request {
		r := httptest.NewRequest(http.MethodGet, "/api/auth/me", nil)
		r.AddCookie(&http.Cookie{Name: auth.CookieName, Value: "token-1"})
		return r
	}

	issuer.resolveErr = errors.New("db down")
	rec := serve(engine, req())
	if rec.Code != http.StatusInternalServerError {
		t.Fatalf("database failure status = %d, want 500; body=%q", rec.Code, rec.Body.String())
	}
	if env := decodeEnvelope(t, rec); env.Error.Code != "internal_error" {
		t.Fatalf("database failure code = %q, want internal_error", env.Error.Code)
	}

	issuer.resolveErr = auth.ErrNoSession
	rec = serve(engine, req())
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("unknown session status = %d, want 401; body=%q", rec.Code, rec.Body.String())
	}
	if env := decodeEnvelope(t, rec); env.Error.Code != "unauthorized" {
		t.Fatalf("unknown session code = %q, want unauthorized", env.Error.Code)
	}
}

// 请求体超过 1 MiB 上界时必须回 413，而不是 400 或内存无界读取。
// 上界内的请求保持原状态码：绑定成功，由用例决定结果。
func TestOversizedRequestBodyIsRejectedWith413(t *testing.T) {
	engine := newAuthEngine(t, newStubIssuer(),
		stubAuth{loginErr: errs.New(errs.CodeUnauthorized, "invalid email or password")})

	normal := `{"email":"a@example.com","password":"x"}`
	rec := serve(engine, httptest.NewRequest(http.MethodPost, "/api/auth/login", strings.NewReader(normal)))
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("well-formed body status = %d, want 401; body=%q", rec.Code, rec.Body.String())
	}

	oversized := `{"email":"a@example.com","password":"` + strings.Repeat("x", 2<<20) + `"}`
	rec = serve(engine, httptest.NewRequest(http.MethodPost, "/api/auth/login", strings.NewReader(oversized)))
	if rec.Code != http.StatusRequestEntityTooLarge {
		t.Fatalf("oversized body status = %d, want 413; body=%q", rec.Code, rec.Body.String())
	}
	if env := decodeEnvelope(t, rec); env.Error.Code == "" || env.Error.TraceID == "" {
		t.Fatalf("413 envelope is incomplete: %#v", env.Error)
	}
}
