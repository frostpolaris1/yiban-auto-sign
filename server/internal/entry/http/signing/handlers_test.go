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
