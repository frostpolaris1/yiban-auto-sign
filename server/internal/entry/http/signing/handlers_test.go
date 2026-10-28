package signing_test

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"

	appaccount "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/account"
	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	appworker "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/worker"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/oapi"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/router"
	signinghttp "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/signing"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/contract"
)

type fakeService struct {
	cred appaccount.SigningCredential
	err  error
}

func (f *fakeService) SigningCredential(context.Context, corescope.Scope, string) (appaccount.SigningCredential, error) {
	return f.cred, f.err
}

type fakeAuthorizer struct {
	scope corescope.Scope
	err   error
}

func (f *fakeAuthorizer) Authorize(context.Context, coreauth.User) (corescope.Scope, appaudit.Actor, error) {
	return f.scope, appaudit.Actor{}, f.err
}

type fakeVerifier struct {
	profile contract.Profile
	err     error
	last    contract.VerifyRequest
	calls   int
}

func (f *fakeVerifier) Verify(_ context.Context, req contract.VerifyRequest) (contract.Profile, error) {
	f.last = req
	f.calls++
	return f.profile, f.err
}

// fakeIssuer 是进程内会话签发器：只为端点集成测试服务。
type fakeIssuer struct {
	secret []byte
	token  string
	sess   coreauth.Session
}

func (f *fakeIssuer) Issue(_ context.Context, user coreauth.User) (coreauth.Issued, error) {
	token := "test-session-token"
	session := coreauth.Session{User: user, CSRFToken: coreauth.CSRFToken(f.secret, token)}
	f.token = token
	f.sess = session
	return coreauth.Issued{Token: token, Session: session}, nil
}

func (f *fakeIssuer) Resolve(_ context.Context, token string) (coreauth.Session, error) {
	if token != f.token {
		return coreauth.Session{}, errors.New("unknown session")
	}
	return f.sess, nil
}

func (f *fakeIssuer) Revoke(context.Context, string) error { return nil }

func (f *fakeIssuer) RevokeAllForUser(context.Context, string) error { return nil }

func newTestRouter(t *testing.T, authz *fakeAuthorizer, svc *fakeService, verifier *fakeVerifier) (http.Handler, coreauth.Issued) {
	t.Helper()
	issuer := &fakeIssuer{secret: []byte("0123456789abcdef0123456789abcdef")}
	issued, err := issuer.Issue(context.Background(), coreauth.User{
		ID: "u1", Email: "boss@example.com", Role: coreauth.RoleMaster, Status: coreauth.StatusActive,
	})
	if err != nil {
		t.Fatalf("issue session: %v", err)
	}
	engine, err := router.New(router.Options{
		Sessions:        issuer,
		InsecureCookies: true,
		Signing:         signinghttp.NewHandler(svc, authz, verifier),
	})
	if err != nil {
		t.Fatalf("router: %v", err)
	}
	return engine, issued
}

func callVerify(t *testing.T, engine http.Handler, issued coreauth.Issued, withCSRF bool) *httptest.ResponseRecorder {
	t.Helper()
	req := httptest.NewRequest(http.MethodPost, "/api/accounts/acc-1/verify", nil)
	req.AddCookie(&http.Cookie{Name: coreauth.InsecureCookieName, Value: issued.Token})
	if withCSRF {
		req.Header.Set("X-CSRF-Token", issued.CSRFToken)
	}
	rec := httptest.NewRecorder()
	engine.ServeHTTP(rec, req)
	return rec
}

func decodeResult(t *testing.T, rec *httptest.ResponseRecorder) oapi.AccountVerifyResult {
	t.Helper()
	var result oapi.AccountVerifyResult
	if err := json.Unmarshal(rec.Body.Bytes(), &result); err != nil {
		t.Fatalf("decode %q: %v", rec.Body.String(), err)
	}
	return result
}

func TestVerifySuccess(t *testing.T) {
	svc := &fakeService{cred: appaccount.SigningCredential{
		SchoolID: "school-1", Phone: "13800000000", Password: "secret",
		DeviceCode: "device-code", PhoneModel: "Model-X",
	}}
	verifier := &fakeVerifier{profile: contract.Profile{DisplayName: "张三", SchoolName: "示例大学"}}
	engine, issued := newTestRouter(t, &fakeAuthorizer{scope: corescope.Master()}, svc, verifier)

	rec := callVerify(t, engine, issued, true)
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, body=%s", rec.Code, rec.Body.String())
	}
	result := decodeResult(t, rec)
	if !result.Ok || result.Code != string(contract.OutcomeSuccess) {
		t.Fatalf("result = %+v", result)
	}
	if verifier.last.Credential.Phone != "13800000000" ||
		verifier.last.Credential.Password != "secret" ||
		verifier.last.Credential.DeviceCode != "device-code" {
		t.Fatalf("verifier credential = %+v", verifier.last.Credential)
	}
}

func TestVerifyCredentialInvalid(t *testing.T) {
	svc := &fakeService{cred: appaccount.SigningCredential{SchoolID: "school-1", Phone: "13800000000"}}
	verifier := &fakeVerifier{err: &contract.OutcomeError{
		Outcome: contract.OutcomeCredentialBad, Message: "reject 13800138000",
	}}
	engine, issued := newTestRouter(t, &fakeAuthorizer{scope: corescope.Master()}, svc, verifier)

	rec := callVerify(t, engine, issued, true)
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d", rec.Code)
	}
	result := decodeResult(t, rec)
	if result.Ok || result.Code != "credential_invalid" {
		t.Fatalf("result = %+v", result)
	}
	if result.Message == nil || *result.Message != "reject 138****8000" {
		t.Fatalf("message = %v", result.Message)
	}
}

func TestVerifyChallenge(t *testing.T) {
	svc := &fakeService{cred: appaccount.SigningCredential{SchoolID: "school-1"}}
	verifier := &fakeVerifier{err: &contract.OutcomeError{Outcome: contract.OutcomeChallenge}}
	engine, issued := newTestRouter(t, &fakeAuthorizer{scope: corescope.Master()}, svc, verifier)

	result := decodeResult(t, callVerify(t, engine, issued, true))
	if result.Ok || result.Code != "challenge" {
		t.Fatalf("result = %+v", result)
	}
}

func TestVerifyUpstreamFailure(t *testing.T) {
	svc := &fakeService{cred: appaccount.SigningCredential{SchoolID: "school-1"}}
	verifier := &fakeVerifier{err: &url.Error{Op: "Get", URL: "https://api.uyiban.com", Err: errors.New("timeout")}}
	engine, issued := newTestRouter(t, &fakeAuthorizer{scope: corescope.Master()}, svc, verifier)

	rec := callVerify(t, engine, issued, true)
	if rec.Code != http.StatusBadGateway {
		t.Fatalf("status = %d, body=%s", rec.Code, rec.Body.String())
	}
	var envelope struct {
		Error struct {
			Code string `json:"code"`
		} `json:"error"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &envelope); err != nil {
		t.Fatalf("decode %q: %v", rec.Body.String(), err)
	}
	if envelope.Error.Code != "upstream_unavailable" {
		t.Fatalf("error code = %q, body=%s", envelope.Error.Code, rec.Body.String())
	}
}

func TestVerifyRequiresSession(t *testing.T) {
	svc := &fakeService{cred: appaccount.SigningCredential{SchoolID: "school-1"}}
	engine, _ := newTestRouter(t, &fakeAuthorizer{scope: corescope.Master()}, svc, &fakeVerifier{})

	req := httptest.NewRequest(http.MethodPost, "/api/accounts/acc-1/verify", nil)
	rec := httptest.NewRecorder()
	engine.ServeHTTP(rec, req)
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("status = %d, body=%s", rec.Code, rec.Body.String())
	}
}

func TestVerifyUnwired(t *testing.T) {
	issuer := &fakeIssuer{secret: []byte("0123456789abcdef0123456789abcdef")}
	issued, err := issuer.Issue(context.Background(), coreauth.User{
		ID: "u1", Email: "boss@example.com", Role: coreauth.RoleMaster, Status: coreauth.StatusActive,
	})
	if err != nil {
		t.Fatalf("issue: %v", err)
	}
	engine, err := router.New(router.Options{Sessions: issuer, InsecureCookies: true})
	if err != nil {
		t.Fatalf("router: %v", err)
	}
	rec := callVerify(t, engine, issued, true)
	if rec.Code != http.StatusNotImplemented {
		t.Fatalf("status = %d, body=%s", rec.Code, rec.Body.String())
	}
}

func TestVerifyMessageCap(t *testing.T) {
	svc := &fakeService{cred: appaccount.SigningCredential{SchoolID: "school-1"}}
	verifier := &fakeVerifier{err: &contract.OutcomeError{
		Outcome: contract.OutcomeRejected, Message: strings.Repeat("中", 300),
	}}
	engine, issued := newTestRouter(t, &fakeAuthorizer{scope: corescope.Master()}, svc, verifier)

	result := decodeResult(t, callVerify(t, engine, issued, true))
	if result.Message == nil || len([]rune(*result.Message)) != 200 {
		t.Fatalf("message = %v", result.Message)
	}
	if len(*result.Message) != 600 {
		t.Fatalf("message bytes = %d, want 600 (rune cap must not cut bytes)", len(*result.Message))
	}
}

func TestVerifyAccountNotFound(t *testing.T) {
	svc := &fakeService{err: errs.New(errs.CodeNotFound, "account not found")}
	engine, issued := newTestRouter(t, &fakeAuthorizer{scope: corescope.Master()}, svc, &fakeVerifier{})

	rec := callVerify(t, engine, issued, true)
	if rec.Code != http.StatusNotFound {
		t.Fatalf("status = %d, body=%s", rec.Code, rec.Body.String())
	}
}

func TestVerifyAdminAllowed(t *testing.T) {
	svc := &fakeService{cred: appaccount.SigningCredential{SchoolID: "school-1"}}
	verifier := &fakeVerifier{profile: contract.Profile{DisplayName: "张三"}}
	engine, issued := newTestRouter(t,
		&fakeAuthorizer{scope: corescope.Admin([]string{"school-1"})}, svc, verifier)

	rec := callVerify(t, engine, issued, true)
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, body=%s", rec.Code, rec.Body.String())
	}
	result := decodeResult(t, rec)
	if !result.Ok || verifier.calls != 1 {
		t.Fatalf("result = %+v calls=%d", result, verifier.calls)
	}
}

func TestVerifyUserForbidden(t *testing.T) {
	svc := &fakeService{cred: appaccount.SigningCredential{SchoolID: "school-1"}}
	engine, issued := newTestRouter(t, &fakeAuthorizer{scope: corescope.User("u1")}, svc, &fakeVerifier{})

	rec := callVerify(t, engine, issued, true)
	if rec.Code != http.StatusForbidden {
		t.Fatalf("status = %d, body=%s", rec.Code, rec.Body.String())
	}
}

func TestVerifyRequiresCSRF(t *testing.T) {
	svc := &fakeService{cred: appaccount.SigningCredential{SchoolID: "school-1"}}
	engine, issued := newTestRouter(t, &fakeAuthorizer{scope: corescope.Master()}, svc, &fakeVerifier{})

	rec := callVerify(t, engine, issued, false)
	if rec.Code != http.StatusForbidden {
		t.Fatalf("status = %d, body=%s", rec.Code, rec.Body.String())
	}
}

type fakeManual struct {
	taskID string
	err    error
	calls  int
}

func (f *fakeManual) Request(context.Context, string) (string, error) {
	f.calls++
	return f.taskID, f.err
}

type fakeAccess struct {
	err   error
	calls int
}

func (f *fakeAccess) Get(context.Context, corescope.Scope, string) (appaccount.Record, error) {
	f.calls++
	return appaccount.Record{}, f.err
}

// newSignRouter 装配带手动签到接缝的路由。
func newSignRouter(t *testing.T, authz *fakeAuthorizer, manual *fakeManual) (http.Handler, coreauth.Issued) {
	return newSignRouterWithAccess(t, authz, manual, &fakeAccess{})
}

// newSignRouterWithAccess 装配带账号可见面判据的路由。
func newSignRouterWithAccess(t *testing.T, authz *fakeAuthorizer, manual *fakeManual, access *fakeAccess) (http.Handler, coreauth.Issued) {
	t.Helper()
	issuer := &fakeIssuer{secret: []byte("0123456789abcdef0123456789abcdef")}
	issued, err := issuer.Issue(context.Background(), coreauth.User{
		ID: "u1", Email: "boss@example.com", Role: coreauth.RoleMaster, Status: coreauth.StatusActive,
	})
	if err != nil {
		t.Fatalf("issue session: %v", err)
	}
	handler := signinghttp.NewHandler(&fakeService{}, authz, &fakeVerifier{}).
		WithManual(manual).
		WithAccess(access)
	engine, err := router.New(router.Options{Sessions: issuer, InsecureCookies: true, Signing: handler})
	if err != nil {
		t.Fatalf("router: %v", err)
	}
	return engine, issued
}

// k0y-8：可见面判据未接线＝装配错误。已知角色走到 nil 判据时必须 500
// 响亮失败（fail-closed），不得静默放行或假装「user 被拒绝」。
func TestSignWithoutAccessWiringFailsLoud(t *testing.T) {
	issuer := &fakeIssuer{secret: []byte("0123456789abcdef0123456789abcdef")}
	issued, err := issuer.Issue(context.Background(), coreauth.User{
		ID: "u1", Email: "boss@example.com", Role: coreauth.RoleMaster, Status: coreauth.StatusActive,
	})
	if err != nil {
		t.Fatalf("issue session: %v", err)
	}
	handler := signinghttp.NewHandler(&fakeService{}, &fakeAuthorizer{scope: corescope.Master()}, &fakeVerifier{}).
		WithManual(&fakeManual{})
	engine, err := router.New(router.Options{Sessions: issuer, InsecureCookies: true, Signing: handler})
	if err != nil {
		t.Fatalf("router: %v", err)
	}
	rec := callSign(t, engine, issued)
	if rec.Code != http.StatusInternalServerError {
		t.Fatalf("未接线 status = %d, want 500: %s", rec.Code, rec.Body.String())
	}
}

func callSign(t *testing.T, engine http.Handler, issued coreauth.Issued) *httptest.ResponseRecorder {
	t.Helper()
	req := httptest.NewRequest(http.MethodPost, "/api/accounts/acc-1/sign", nil)
	req.AddCookie(&http.Cookie{Name: coreauth.InsecureCookieName, Value: issued.Token})
	req.Header.Set("X-CSRF-Token", issued.CSRFToken)
	rec := httptest.NewRecorder()
	engine.ServeHTTP(rec, req)
	return rec
}

// 手动签到成功：202 且回 task_id（入队而非完成）。
func TestSignAccepted(t *testing.T) {
	manual := &fakeManual{taskID: "task-1"}
	engine, issued := newSignRouter(t, &fakeAuthorizer{scope: corescope.Master()}, manual)
	rec := callSign(t, engine, issued)
	if rec.Code != http.StatusAccepted {
		t.Fatalf("status = %d, want 202: %s", rec.Code, rec.Body.String())
	}
	var body oapi.AccountSignAccepted
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if body.TaskId != "task-1" || manual.calls != 1 {
		t.Fatalf("body = %+v calls=%d", body, manual.calls)
	}
}

// 三类冲突都映射 409 与稳定错误码。
func TestSignConflicts(t *testing.T) {
	cases := []struct {
		err  error
		code string
	}{
		{appworker.ErrAlreadySigned, "already_signed"},
		{appworker.ErrSignRunning, "sign_running"},
		{appworker.ErrWindowClosed, "window_closed"},
	}
	for _, c := range cases {
		engine, issued := newSignRouter(t, &fakeAuthorizer{scope: corescope.Master()}, &fakeManual{err: c.err})
		rec := callSign(t, engine, issued)
		if rec.Code != http.StatusConflict {
			t.Fatalf("%s: status = %d, want 409", c.code, rec.Code)
		}
		if !strings.Contains(rec.Body.String(), c.code) {
			t.Fatalf("%s: body = %s", c.code, rec.Body.String())
		}
	}
}

// 账号不存在映射 404。
func TestSignNotFound(t *testing.T) {
	engine, issued := newSignRouter(t, &fakeAuthorizer{scope: corescope.Master()},
		&fakeManual{err: errs.New(errs.CodeNotFound, "no enabled task for this account")})
	rec := callSign(t, engine, issued)
	if rec.Code != http.StatusNotFound {
		t.Fatalf("status = %d, want 404: %s", rec.Code, rec.Body.String())
	}
}

// 普通用户可签自己的账号：可见面通过即可入队。
func TestSignUserOwnAccount(t *testing.T) {
	access := &fakeAccess{}
	manual := &fakeManual{taskID: "task-1"}
	engine, issued := newSignRouterWithAccess(t,
		&fakeAuthorizer{scope: corescope.User("u1")}, manual, access)
	rec := callSign(t, engine, issued)
	if rec.Code != http.StatusAccepted {
		t.Fatalf("status = %d, want 202: %s", rec.Code, rec.Body.String())
	}
	if access.calls != 1 || manual.calls != 1 {
		t.Fatalf("access=%d manual=%d", access.calls, manual.calls)
	}
}

// 可见面外（跨校/非本人）必须 404，且不得入队。
func TestSignCrossScopeNotFound(t *testing.T) {
	access := &fakeAccess{err: errs.New(errs.CodeNotFound, "account not found")}
	manual := &fakeManual{taskID: "task-1"}
	engine, issued := newSignRouterWithAccess(t,
		&fakeAuthorizer{scope: corescope.Admin([]string{"school-1"})}, manual, access)
	rec := callSign(t, engine, issued)
	if rec.Code != http.StatusNotFound {
		t.Fatalf("status = %d, want 404: %s", rec.Code, rec.Body.String())
	}
	if manual.calls != 0 {
		t.Fatalf("可见面失败后不得入队, manual=%d", manual.calls)
	}
}
