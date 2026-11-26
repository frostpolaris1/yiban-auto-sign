package router_test

// must_change_password 强制门的端到端测试。
//
// # 功能
// 钉住强制改密会话在鉴权中间件上的放行面：只允许 publicPaths 表中的路径、
// 精确 GET /api/auth/me 与精确 POST /api/auth/change-password；
// 账号、注销账号与管理面一律 403 forbidden。
// 本文件**不**连数据库，**不**校验口令。
//
// # 威胁模型
// 只把标志写进会话、靠 UI 自己引导改密，不构成强制：绕过 UI 直接调 API
// 的客户端仍能使用全部功能。注销账号也必须挡，否则先删号再恢复即可绕过改密。
// 登出必须在 publicPaths 之后放行，否则用户被永久卡在门内。

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
)

// mustChangeMessage 是门拒绝时的可行动文案；断言子串以便措辞微调不误红。
const mustChangeMessage = "password change required"

// mustChangeDeleteSpy 记录注销账号用例是否被调用。
// 门生效时处理器不得收到请求，计数必须为 0。
type mustChangeDeleteSpy struct {
	stubAuth
	calls int
}

func (s *mustChangeDeleteSpy) DeleteSelf(context.Context, auth.Session, string) (time.Time, error) {
	s.calls++
	return time.Time{}, nil
}

// issueMustChange 签发一个带指定强制改密标志的会话。
func issueMustChange(t *testing.T, issuer *stubIssuer, id string, mustChange bool) auth.Issued {
	t.Helper()
	issued, err := issuer.Issue(context.Background(), auth.User{
		ID: id, Email: id + "@example.com",
		Role: auth.RoleUser, Status: auth.StatusActive,
		MustChangePassword: mustChange,
	})
	if err != nil {
		t.Fatalf("issue %s: %v", id, err)
	}
	return issued
}

// assertGateForbidden 断言 403、稳定码 forbidden、可行动文案，
// 并确认响应没有回显会话令牌或请求体里的口令。
func assertGateForbidden(t *testing.T, rec *httptest.ResponseRecorder, secrets ...string) {
	t.Helper()
	if rec.Code != http.StatusForbidden {
		t.Fatalf("status = %d, want 403; body=%q", rec.Code, rec.Body.String())
	}
	env := decodeEnvelope(t, rec)
	if env.Error.Code != "forbidden" {
		t.Fatalf("code = %q, want forbidden; body=%q", env.Error.Code, rec.Body.String())
	}
	if !strings.Contains(strings.ToLower(env.Error.Message), mustChangeMessage) {
		t.Fatalf("message = %q, want actionable %q", env.Error.Message, mustChangeMessage)
	}
	body := rec.Body.String()
	for _, secret := range secrets {
		if secret != "" && strings.Contains(body, secret) {
			t.Fatalf("响应回显了敏感值 %q: %q", secret, body)
		}
	}
}

// 强制改密会话不得调用账号、注销账号与管理面 API。
// 注销账号也要挡：先删号再恢复即可绕过改密。
func TestMustChangePasswordGateBlocksOtherAPIs(t *testing.T) {
	issuer := newStubIssuer()
	issued := issueMustChange(t, issuer, "u-must", true)
	spy := &mustChangeDeleteSpy{}
	engine := newAuthEngine(t, issuer, spy)

	cases := []struct {
		name     string
		method   string
		path     string
		body     string
		withCSRF bool
	}{
		{"accounts", http.MethodGet, "/api/accounts", "", false},
		{"me delete", http.MethodPost, "/api/me/delete", `{"current_password":"leak-me-not"}`, true},
		{"admin settings", http.MethodGet, "/api/admin/settings", "", false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			csrf := ""
			if tc.withCSRF {
				csrf = issued.Session.CSRFToken
			}
			rec := serve(engine, requestWithSession(tc.method, tc.path, tc.body, csrf, &issued.Session))
			assertGateForbidden(t, rec, issued.Token, "leak-me-not")
		})
	}
	if spy.calls != 0 {
		t.Fatalf("注销账号用例被调用 %d 次，门失效", spy.calls)
	}
}

// 会话主体端点必须放行，且如实返回强制改密标志：
// UI 靠它决定是否显示改密页。
func TestMustChangePasswordGateAllowsMe(t *testing.T) {
	issuer := newStubIssuer()
	issued := issueMustChange(t, issuer, "u-me", true)
	engine := newAuthEngine(t, issuer, &mustChangeDeleteSpy{})

	rec := serve(engine, requestWithSession(http.MethodGet, "/api/auth/me", "", "", &issued.Session))
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200; body=%q", rec.Code, rec.Body.String())
	}
	var body struct {
		MustChangePassword bool   `json:"must_change_password"`
		CsrfToken          string `json:"csrf_token"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("decode me: %v; body=%q", err, rec.Body.String())
	}
	if !body.MustChangePassword {
		t.Fatalf("must_change_password = false, want true; body=%q", rec.Body.String())
	}
	if body.CsrfToken == "" {
		t.Fatal("csrf_token is empty; the client cannot leave the gate")
	}
}

// 改密端点必须进得去：返回 204 说明处理器收到请求，而不是被门拦成 403。
func TestMustChangePasswordGateAllowsChangePassword(t *testing.T) {
	issuer := newStubIssuer()
	issued := issueMustChange(t, issuer, "u-change", true)
	svc := &changePasswordStub{}
	engine := newAuthEngine(t, issuer, svc)

	rec := serve(engine, changePasswordRequest(t, issued, `{"current_password":"old-pass","new_password":"new-pass"}`))
	if rec.Code == http.StatusForbidden {
		t.Fatalf("change-password was stopped by the gate; body=%q", rec.Body.String())
	}
	if rec.Code != http.StatusNoContent {
		t.Fatalf("status = %d, want 204; body=%q", rec.Code, rec.Body.String())
	}
	if svc.calls != 1 {
		t.Fatalf("改密用例调用 = %d, want 1", svc.calls)
	}
}

// 登出是 publicPaths 表内的路径，必须优先放行：
// 否则带标志的会话无法销毁，用户被卡死。
func TestMustChangePasswordGateAllowsLogout(t *testing.T) {
	issuer := newStubIssuer()
	issued := issueMustChange(t, issuer, "u-logout", true)
	engine := newAuthEngine(t, issuer, stubAuth{})

	rec := serve(engine, requestWithSession(http.MethodPost, "/api/auth/logout",
		"", issued.Session.CSRFToken, &issued.Session))
	if rec.Code == http.StatusForbidden {
		t.Fatalf("logout was stopped by the gate; body=%q", rec.Body.String())
	}
	if rec.Code != http.StatusNoContent {
		t.Fatalf("status = %d, want 204; body=%q", rec.Code, rec.Body.String())
	}
}

// 标志为 false 时同一批路径恢复原行为，门不得误伤普通会话。
func TestMustChangePasswordGateSkipsNormalSessions(t *testing.T) {
	issuer := newStubIssuer()
	issued := issueMustChange(t, issuer, "u-normal", false)
	spy := &mustChangeDeleteSpy{}
	engine := newAuthEngine(t, issuer, spy)

	// 账号处理器未装配时是 501；关键是不被门拦成 403。
	rec := serve(engine, requestWithSession(http.MethodGet, "/api/accounts", "", "", &issued.Session))
	if rec.Code == http.StatusForbidden {
		t.Fatalf("普通会话的账号列表被门拦了; body=%q", rec.Body.String())
	}
	if rec.Code != http.StatusNotImplemented {
		t.Fatalf("status = %d, want 501（未装配处理器）; body=%q", rec.Code, rec.Body.String())
	}

	rec = serve(engine, requestWithSession(http.MethodPost, "/api/me/delete",
		`{"current_password":"pw"}`, issued.Session.CSRFToken, &issued.Session))
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200; body=%q", rec.Code, rec.Body.String())
	}
	if spy.calls != 1 {
		t.Fatalf("注销账号用例调用 = %d, want 1", spy.calls)
	}

	rec = serve(engine, requestWithSession(http.MethodGet, "/api/auth/me", "", "", &issued.Session))
	if rec.Code != http.StatusOK {
		t.Fatalf("me status = %d, want 200; body=%q", rec.Code, rec.Body.String())
	}
	var body struct {
		MustChangePassword bool `json:"must_change_password"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("decode me: %v", err)
	}
	if body.MustChangePassword {
		t.Fatalf("must_change_password = true, want false; body=%q", rec.Body.String())
	}
}
