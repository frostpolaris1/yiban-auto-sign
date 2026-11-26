package router_test

// must_change_password 强制门的端到端测试。
//
// # 功能
// 钉住强制改密会话在鉴权中间件上的放行面：只允许 publicPaths 表中的路径、
// 精确 GET /api/auth/me、精确 GET /api/auth/password-policy 与精确
// POST /api/auth/change-password；账号、注销账号与管理面一律 403 forbidden。
// policy 是改密对话框唯一的规则来源：不给它，UI 禁用保存，改密变死锁。
// 本文件**不**连数据库，**不**校验口令。
//
// # 威胁模型
// 只把标志写进会话、靠 UI 自己引导改密，不构成强制：绕过 UI 直接调 API
// 的客户端仍能使用全部功能。注销账号也必须挡，否则先删号再恢复即可绕过改密。
// 登出必须在 publicPaths 之后放行，否则用户被永久卡在门内。

import (
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

	appaccount "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/account"
	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	appauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
	accounthttp "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/account"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/router"
)

// mustChangeMessage 是门拒绝时的可行动文案；断言子串以便措辞微调不误红。
// 文案必须是中文：UI 的错误信封只保留含中文的 message，英文会被替换成
// 通用「权限不足」，用户看不出该去改密。
const mustChangeMessage = "请先修改密码后再操作"

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

// gateAccountsService 只实现列表：普通会话要真正跑进账号处理器回 200，
// 不能把 501「未装配」当作门没生效的信号。嵌入接口让其余方法保持零值；
// 本测试只触发 List。
type gateAccountsService struct{ accounthttp.Service }

func (gateAccountsService) List(context.Context, corescope.Scope, appaccount.ListFilter, string, int) (appaccount.Page, error) {
	return appaccount.Page{}, nil
}

// mustChangeRegistrationSpy 记录注册开关写入是否进到处理器。
// master 会话本身有权写；门生效时计数必须为 0。
type mustChangeRegistrationSpy struct {
	stubAuth
	calls int
}

func (s *mustChangeRegistrationSpy) SetRegistration(context.Context, corescope.Scope, appaudit.Actor, bool) (appauth.RegistrationStatus, error) {
	s.calls++
	return appauth.RegistrationStatus{}, nil
}

// gateAuthorizer 把会话主体映射成普通用户作用域；列表不校验角色。
type gateAuthorizer struct{}

func (gateAuthorizer) Authorize(_ context.Context, user auth.User) (corescope.Scope, appaudit.Actor, error) {
	return corescope.User(user.ID), appaudit.Actor{UserID: user.ID, Role: user.Role}, nil
}

// newAccountsEngine 装配一个返回空页的账号处理器：普通会话的列表请求
// 会真正进入处理器并回 200，从而证明门没有误伤，而不是靠 501 推测。
func newAccountsEngine(t *testing.T, issuer auth.SessionIssuer, svc router.AuthService) *gin.Engine {
	t.Helper()
	engine, err := router.New(router.Options{
		Sessions: issuer,
		Auth:     svc,
		Accounts: accounthttp.NewHandler(gateAccountsService{}, gateAuthorizer{}),
		Clock:    clock.Fixed{T: time.Date(2026, 10, 25, 9, 0, 0, 0, time.UTC)},
		Logger:   slog.New(slog.NewTextHandler(io.Discard, nil)),
	})
	if err != nil {
		t.Fatalf("router.New: %v", err)
	}
	return engine
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
	if !strings.Contains(env.Error.Message, mustChangeMessage) {
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
		{"registration write", http.MethodPut, "/api/auth/registration", `{"enabled":true}`, true},
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

// master 会话也必须被门拦住注册开关写入：它有权写设置，但强制改密期间
// 不应再拥有业务写权限。计数为 0 证明请求没进处理器，而不是处理器拒绝。
func TestMustChangePasswordGateBlocksRegistrationWrite(t *testing.T) {
	issuer := newStubIssuer()
	issued, err := issuer.Issue(context.Background(), auth.User{
		ID: "u-master", Email: "master@example.com",
		Role: auth.RoleMaster, Status: auth.StatusActive,
		MustChangePassword: true,
	})
	if err != nil {
		t.Fatalf("issue: %v", err)
	}
	spy := &mustChangeRegistrationSpy{}
	engine, err := router.New(router.Options{
		Sessions: issuer,
		Auth:     spy,
		Clock:    clock.Fixed{T: time.Date(2026, 10, 25, 9, 0, 0, 0, time.UTC)},
		Logger:   slog.New(slog.NewTextHandler(io.Discard, nil)),
	})
	if err != nil {
		t.Fatalf("router.New: %v", err)
	}
	rec := serve(engine, requestWithSession(http.MethodPut, "/api/auth/registration",
		`{"enabled":true}`, issued.Session.CSRFToken, &issued.Session))
	assertGateForbidden(t, rec, issued.Token)
	if spy.calls != 0 {
		t.Fatalf("注册开关用例被调用 %d 次，门失效", spy.calls)
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

// 口令策略必须放行：它是 UI 改密对话框唯一的规则来源，
// 读不到就禁用保存，强制改密会变成死锁。响应只含三个数字。
func TestMustChangePasswordGateAllowsPasswordPolicy(t *testing.T) {
	issuer := newStubIssuer()
	issued := issueMustChange(t, issuer, "u-policy", true)
	engine := newAuthEngine(t, issuer, &mustChangeDeleteSpy{})

	rec := serve(engine, requestWithSession(http.MethodGet, "/api/auth/password-policy", "", "", &issued.Session))
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200; body=%q", rec.Code, rec.Body.String())
	}
	var body struct {
		MinChars   int `json:"min_chars"`
		MinClasses int `json:"min_classes"`
		MaxBytes   int `json:"max_bytes"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("decode policy: %v; body=%q", err, rec.Body.String())
	}
	if body.MinChars != 10 || body.MinClasses != 2 || body.MaxBytes != 1024 {
		t.Fatalf("policy = %d/%d/%d, want 10/2/1024; body=%q",
			body.MinChars, body.MinClasses, body.MaxBytes, rec.Body.String())
	}
	for _, secret := range []string{issued.Token, issued.CSRFToken, issued.Session.User.Email} {
		if strings.Contains(rec.Body.String(), secret) {
			t.Fatalf("policy 响应回显了敏感值 %q: %q", secret, rec.Body.String())
		}
	}

	// 其它方法与前缀不得借道：都落到 NoRoute 404，不进任何处理器。
	rec = serve(engine, requestWithSession(http.MethodPost, "/api/auth/password-policy",
		"", issued.Session.CSRFToken, &issued.Session))
	if rec.Code != http.StatusNotFound {
		t.Fatalf("POST password-policy status = %d, want 404; body=%q", rec.Code, rec.Body.String())
	}
	if env := decodeEnvelope(t, rec); env.Error.Code != "not_found" {
		t.Fatalf("POST password-policy code = %q, want not_found; body=%q", env.Error.Code, rec.Body.String())
	}
	rec = serve(engine, requestWithSession(http.MethodGet, "/api/auth/password-policy/extra", "", "", &issued.Session))
	if rec.Code != http.StatusNotFound {
		t.Fatalf("前缀路径 status = %d, want 404; body=%q", rec.Code, rec.Body.String())
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
	engine := newAccountsEngine(t, issuer, spy)

	// 账号处理器已真实装配：普通会话必须进处理器并回 200 空页，
	// 证明门没有误伤，而不是靠 501「未装配」当信号。
	rec := serve(engine, requestWithSession(http.MethodGet, "/api/accounts", "", "", &issued.Session))
	if rec.Code != http.StatusOK {
		t.Fatalf("账号列表 status = %d, want 200; body=%q", rec.Code, rec.Body.String())
	}
	var page struct {
		Items []json.RawMessage `json:"items"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &page); err != nil {
		t.Fatalf("decode accounts: %v; body=%q", err, rec.Body.String())
	}
	if len(page.Items) != 0 {
		t.Fatalf("items = %d, want 0（空页）; body=%q", len(page.Items), rec.Body.String())
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
