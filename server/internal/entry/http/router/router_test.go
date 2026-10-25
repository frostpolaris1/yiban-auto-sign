package router_test

import (
	"bytes"
	"context"
	"encoding/json"
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
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/router"
)

// stubIssuer is a minimal SessionIssuer for the HTTP surface tests.
type stubIssuer struct {
	sessions map[string]auth.Session
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

// errorEnvelope mirrors the frozen error envelope shape.
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

// E-1: an unauthenticated read of /api/auth/me returns 401 and the frozen envelope.
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

// E-2: an unknown route returns 404 and the frozen envelope.
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

// E-3: the liveness probe answers 200 without a session.
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

// E-5: a valid session cookie returns the current user, never the session token.
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

// E-8: a state-changing request with a session but no CSRF header is rejected.
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

// E-9: the same request passes the CSRF gate when the derived token matches.
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

// E-10: logout needs the CSRF token because it destroys a session.
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

// Writes without a session are rejected before the CSRF gate.
func TestWriteWithoutSession(t *testing.T) {
	engine, _ := newTestEngine(t)
	req := httptest.NewRequest(http.MethodPost, "/api/accounts", strings.NewReader("{}"))
	rec := serve(engine, req)
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("status = %d, want 401", rec.Code)
	}
}

// E-6: access logs carry the path but never the query string.
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

// E-7: a panic becomes the 500 envelope, not a dropped connection.
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

// A panicking request still produces one access log line with status 500.
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

// A session-less logout must not be rejected by parameter binding. The
// status itself belongs to the phase; the invariant is that it is not a
// validation failure.
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
