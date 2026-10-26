package location_test

// PUT /api/accounts/{id}/location 的入口层判据：
// 200 回显策略、400 校验、404 跨面、403 角色不符、501 未装配。

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"testing"

	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	applocation "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/location"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
	locationhttp "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/location"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/oapi"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/router"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/contract"
)

type fakeService struct {
	out contract.Location
	err error
	// lastIn 记录最后一次入参，供断言透传。
	lastIn applocation.Input
	calls  int
}

func (f *fakeService) Set(_ context.Context, _ corescope.Scope, _ string, in applocation.Input, _ appaudit.Actor) (contract.Location, error) {
	f.calls++
	f.lastIn = in
	return f.out, f.err
}

type fakeAuthorizer struct {
	scope corescope.Scope
	actor appaudit.Actor
	err   error
}

func (f *fakeAuthorizer) Authorize(context.Context, coreauth.User) (corescope.Scope, appaudit.Actor, error) {
	return f.scope, f.actor, f.err
}

type fakeIssuer struct {
	secret []byte
	token  string
	sess   coreauth.Session
}

func (f *fakeIssuer) Issue(_ context.Context, user coreauth.User) (coreauth.Issued, error) {
	const token = "test-session-token"
	session := coreauth.Session{User: user, CSRFToken: coreauth.CSRFToken(f.secret, token)}
	f.token, f.sess = token, session
	return coreauth.Issued{Token: token, Session: session}, nil
}

func (f *fakeIssuer) Resolve(_ context.Context, token string) (coreauth.Session, error) {
	if token != f.token {
		return coreauth.Session{}, errors.New("unknown session")
	}
	return f.sess, nil
}

func (f *fakeIssuer) Revoke(context.Context, string) error           { return nil }
func (f *fakeIssuer) RevokeAllForUser(context.Context, string) error { return nil }

func newRouter(t *testing.T, authz *fakeAuthorizer, svc *fakeService) (http.Handler, coreauth.Issued) {
	t.Helper()
	issuer := &fakeIssuer{secret: []byte("0123456789abcdef0123456789abcdef")}
	issued, err := issuer.Issue(context.Background(), coreauth.User{
		ID: "u1", Email: "user@example.com", Role: coreauth.RoleUser, Status: coreauth.StatusActive,
	})
	if err != nil {
		t.Fatalf("issue session: %v", err)
	}
	if authz == nil {
		authz = &fakeAuthorizer{}
	}
	engine, err := router.New(router.Options{
		Sessions: issuer, InsecureCookies: true,
		Locations: locationhttp.NewHandler(svc, authz),
	})
	if err != nil {
		t.Fatalf("router: %v", err)
	}
	return engine, issued
}

func doJSON(t *testing.T, engine http.Handler, issued coreauth.Issued, method, path string, body any) *httptest.ResponseRecorder {
	t.Helper()
	var reader *bytes.Reader
	if body != nil {
		raw, err := json.Marshal(body)
		if err != nil {
			t.Fatalf("marshal: %v", err)
		}
		reader = bytes.NewReader(raw)
	} else {
		reader = bytes.NewReader(nil)
	}
	req := httptest.NewRequest(method, path, reader)
	req.Header.Set("Content-Type", "application/json")
	req.AddCookie(&http.Cookie{Name: coreauth.InsecureCookieName, Value: issued.Token})
	req.Header.Set("X-CSRF-Token", issued.CSRFToken)
	rec := httptest.NewRecorder()
	engine.ServeHTTP(rec, req)
	return rec
}

// fixed 设点成功：200 回显完整策略，入参原样透传。
func TestSetLocationFixedOK(t *testing.T) {
	svc := &fakeService{out: contract.Location{
		Mode: contract.LocationFixed, Lng: 116.4, Lat: 39.9, Reason: "在校门口",
	}}
	engine, issued := newRouter(t, &fakeAuthorizer{scope: corescope.User("u1")}, svc)
	rec := doJSON(t, engine, issued, http.MethodPut, "/api/accounts/a1/location", map[string]any{
		"mode": "fixed", "lng": 116.4, "lat": 39.9, "reason": "在校门口",
	})
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d: %s", rec.Code, rec.Body.String())
	}
	var out oapi.AccountLocation
	if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if string(out.Mode) != "fixed" || out.Lng == nil || *out.Lng != 116.4 || out.Reason != "在校门口" {
		t.Fatalf("out = %+v", out)
	}
	if svc.lastIn.Mode != "fixed" || svc.lastIn.Lng == nil || *svc.lastIn.Lng != 116.4 {
		t.Fatalf("lastIn = %+v", svc.lastIn)
	}
	if svc.lastIn.Reason != "在校门口" {
		t.Fatalf("reason 透传丢失: %+v", svc.lastIn)
	}
}

// auto 设定：响应的 lng/lat 为 null。
func TestSetLocationAutoNullPoint(t *testing.T) {
	svc := &fakeService{out: contract.Location{Mode: contract.LocationAuto}}
	engine, issued := newRouter(t, &fakeAuthorizer{scope: corescope.User("u1")}, svc)
	rec := doJSON(t, engine, issued, http.MethodPut, "/api/accounts/a1/location", map[string]any{
		"mode": "auto",
	})
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d: %s", rec.Code, rec.Body.String())
	}
	var out oapi.AccountLocation
	if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if string(out.Mode) != "auto" || out.Lng != nil || out.Lat != nil {
		t.Fatalf("out = %+v", out)
	}
}

// 用例校验错误映射 400。
func TestSetLocationValidationMapped(t *testing.T) {
	svc := &fakeService{err: errs.New(errs.CodeValidation, "fixed mode requires lng and lat")}
	engine, issued := newRouter(t, &fakeAuthorizer{scope: corescope.User("u1")}, svc)
	rec := doJSON(t, engine, issued, http.MethodPut, "/api/accounts/a1/location", map[string]any{
		"mode": "fixed",
	})
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("status = %d", rec.Code)
	}
}

// 跨面（user 写他人账号、admin 写未绑定校）映射 404。
func TestSetLocationCrossScopeMapped(t *testing.T) {
	svc := &fakeService{err: errs.ErrNotFound}
	engine, issued := newRouter(t, &fakeAuthorizer{scope: corescope.User("user-2")}, svc)
	rec := doJSON(t, engine, issued, http.MethodPut, "/api/accounts/a1/location", map[string]any{
		"mode": "auto",
	})
	if rec.Code != http.StatusNotFound {
		t.Fatalf("status = %d", rec.Code)
	}
}

// 作用域解析失败映射 403。
func TestSetLocationForbiddenMapped(t *testing.T) {
	svc := &fakeService{}
	denied := &fakeAuthorizer{err: errs.New(errs.CodeForbidden, "role cannot change location")}
	engine, issued := newRouter(t, denied, svc)
	rec := doJSON(t, engine, issued, http.MethodPut, "/api/accounts/a1/location", map[string]any{
		"mode": "auto",
	})
	if rec.Code != http.StatusForbidden {
		t.Fatalf("status = %d", rec.Code)
	}
	if svc.calls != 0 {
		t.Fatal("作用域解析失败不应调用用例")
	}
}

// 非法 JSON 请求体映射 400，不触达用例。
func TestSetLocationBadBody(t *testing.T) {
	svc := &fakeService{}
	engine, issued := newRouter(t, &fakeAuthorizer{scope: corescope.User("u1")}, svc)
	req := httptest.NewRequest(http.MethodPut, "/api/accounts/a1/location", bytes.NewReader([]byte("{broken")))
	req.Header.Set("Content-Type", "application/json")
	req.AddCookie(&http.Cookie{Name: coreauth.InsecureCookieName, Value: issued.Token})
	req.Header.Set("X-CSRF-Token", issued.CSRFToken)
	rec := httptest.NewRecorder()
	engine.ServeHTTP(rec, req)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("status = %d", rec.Code)
	}
	if svc.calls != 0 {
		t.Fatal("坏请求体不应调用用例")
	}
}
