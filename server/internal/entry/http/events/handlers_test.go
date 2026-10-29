package events_test

// 诊断与轮询端点的入口层判据（P6）：
//   - x-roles 运行时门：diagnostics [user, admin]，sign-status [user]；
//   - 响应形状与用例值一一对应；
//   - 未装配会话 401 由路由中间件承担，本文件注入固定会话。

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	appdiagnostic "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/diagnostic"
	appevents "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/events"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	corediag "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/diagnostic"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
	corestatus "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/status"
	eventshttp "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/events"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/router"
)

type fakeDiag struct {
	mine   appdiagnostic.Report
	detail appdiagnostic.Detail
	status appdiagnostic.StatusReport
	counts map[string]int
	err    error

	calls  []string
	scopes []corescope.Scope
}

func (f *fakeDiag) Mine(_ context.Context, _ string) (appdiagnostic.Report, error) {
	f.calls = append(f.calls, "mine")
	return f.mine, f.err
}

func (f *fakeDiag) Account(_ context.Context, scope corescope.Scope, _ string) (appdiagnostic.Detail, error) {
	f.calls = append(f.calls, "account")
	f.scopes = append(f.scopes, scope)
	return f.detail, f.err
}

func (f *fakeDiag) SignStatus(_ context.Context, _ string) (appdiagnostic.StatusReport, error) {
	f.calls = append(f.calls, "sign-status")
	return f.status, f.err
}

func (f *fakeDiag) Snapshot(_ context.Context, _ corescope.Scope) (corediag.View, error) {
	f.calls = append(f.calls, "snapshot")
	return corediag.View{Day: "2026-10-29", States: f.counts}, f.err
}

type fakeAuthorizer struct {
	scope corescope.Scope
	err   error
}

func (f *fakeAuthorizer) Authorize(context.Context, coreauth.User) (corescope.Scope, appaudit.Actor, error) {
	return f.scope, appaudit.Actor{}, f.err
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
		return coreauth.Session{}, errs.New(errs.CodeUnauthorized, "unknown session")
	}
	return f.sess, nil
}

func (f *fakeIssuer) Revoke(context.Context, string) error           { return nil }
func (f *fakeIssuer) RevokeAllForUser(context.Context, string) error { return nil }

// newEngine 构造只装配 events 域的路由，返回引擎与会话。
func newEngine(t *testing.T, authz *fakeAuthorizer, diag *fakeDiag, broker *appevents.Broker) (http.Handler, coreauth.Issued) {
	t.Helper()
	issuer := &fakeIssuer{secret: []byte("0123456789abcdef0123456789abcdef")}
	issued, err := issuer.Issue(context.Background(), coreauth.User{
		ID: "u1", Email: "user@example.com", Role: coreauth.RoleUser, Status: coreauth.StatusActive,
	})
	if err != nil {
		t.Fatalf("issue session: %v", err)
	}
	if authz == nil {
		authz = &fakeAuthorizer{scope: corescope.User("u1")}
	}
	timer := newFakeTimer(newFakeClock(diagNowTime))
	handler := eventshttp.NewHandler(broker, diag, authz, timer.clock, timer)
	engine, err := router.New(router.Options{
		Sessions: issuer, InsecureCookies: true, Events: handler,
	})
	if err != nil {
		t.Fatalf("router: %v", err)
	}
	return engine, issued
}

func doGet(engine http.Handler, issued coreauth.Issued, path string, headers map[string]string) *httptest.ResponseRecorder {
	req := httptest.NewRequest(http.MethodGet, path, nil)
	req.AddCookie(&http.Cookie{Name: coreauth.InsecureCookieName, Value: issued.Token})
	for k, v := range headers {
		req.Header.Set(k, v)
	}
	rec := httptest.NewRecorder()
	engine.ServeHTTP(rec, req)
	return rec
}

func decodeBody(t *testing.T, rec *httptest.ResponseRecorder) map[string]any {
	t.Helper()
	var out map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
		t.Fatalf("decode %q: %v", rec.Body.String(), err)
	}
	return out
}

var diagNowTime = time.Date(2026, 10, 29, 7, 0, 0, 0, time.FixedZone("CST", 8*3600))

func sampleReport() appdiagnostic.Report {
	state := string(corestatus.TaskPending)
	runAt := diagNowTime.Add(time.Minute)
	next := runAt
	return appdiagnostic.Report{
		UpdatedAt: diagNowTime,
		Items: []appdiagnostic.Item{{
			AccountID: "a1", State: &state, RunAt: &runAt,
			Attempts: 2, Result: "wait_window_open",
			NextRetryAt: &next, Suggestion: corediag.SuggestWait,
		}},
	}
}

// 200 形状：items 逐键对齐。
func TestMeDiagnosticsOK(t *testing.T) {
	diag := &fakeDiag{mine: sampleReport()}
	engine, issued := newEngine(t, nil, diag, appevents.NewBroker())
	rec := doGet(engine, issued, "/api/me/diagnostics", nil)
	if rec.Code != 200 {
		t.Fatalf("status = %d: %s", rec.Code, rec.Body.String())
	}
	body := decodeBody(t, rec)
	items, ok := body["items"].([]any)
	if !ok || len(items) != 1 {
		t.Fatalf("items = %v", body["items"])
	}
	item := items[0].(map[string]any)
	if item["account_id"] != "a1" || item["state"] != string(corestatus.TaskPending) ||
		item["attempts"] != float64(2) || item["result"] != "wait_window_open" ||
		item["suggestion"] != "wait" {
		t.Fatalf("item = %v", item)
	}
	if item["next_retry_at"] == nil || item["run_at"] == nil {
		t.Fatalf("run_at/next_retry_at 缺失: %v", item)
	}
	if _, ok := body["updated_at"]; !ok {
		t.Fatal("updated_at 缺失")
	}
}

// 轮询 200：items 带 stale；无行账号 state 为 null。
func TestMeSignStatusOK(t *testing.T) {
	state := string(corestatus.TaskSuccess)
	runAt := diagNowTime.Add(-time.Minute)
	diag := &fakeDiag{status: appdiagnostic.StatusReport{
		UpdatedAt: diagNowTime,
		Items: []appdiagnostic.StatusItem{
			{AccountID: "a1", State: &state, RunAt: &runAt, Stale: true},
			{AccountID: "a2", Stale: false},
		},
	}}
	engine, issued := newEngine(t, nil, diag, appevents.NewBroker())
	rec := doGet(engine, issued, "/api/me/sign-status", nil)
	if rec.Code != 200 {
		t.Fatalf("status = %d: %s", rec.Code, rec.Body.String())
	}
	body := decodeBody(t, rec)
	items := body["items"].([]any)
	first := items[0].(map[string]any)
	if first["stale"] != true || first["account_id"] != "a1" {
		t.Fatalf("item0 = %v", first)
	}
	second := items[1].(map[string]any)
	if second["state"] != nil || second["stale"] != false {
		t.Fatalf("无行账号 = %v", second)
	}
}

// 单账号详版：窗口与出口标签透传为对象；作用域原样下发。
func TestAccountDiagnosticsOK(t *testing.T) {
	state := string(corestatus.TaskFailed)
	start := diagNowTime.Add(-time.Hour)
	end := diagNowTime.Add(time.Hour)
	diag := &fakeDiag{detail: appdiagnostic.Detail{
		Item: appdiagnostic.Item{
			AccountID: "a1", State: &state, Attempts: 3,
			Result: "rejected", Suggestion: corediag.SuggestRetryNow,
		},
		Window: &corediag.WindowInfo{Start: start, End: end, Source: "platform", Version: 4},
		Egress: &corediag.EgressInfo{Identity: "https://eg-1"},
	}}
	engine, issued := newEngine(t, nil, diag, appevents.NewBroker())
	rec := doGet(engine, issued, "/api/accounts/a1/diagnostics", nil)
	if rec.Code != 200 {
		t.Fatalf("status = %d: %s", rec.Code, rec.Body.String())
	}
	body := decodeBody(t, rec)
	if body["account_id"] != "a1" || body["suggestion"] != "retry_now" {
		t.Fatalf("body = %v", body)
	}
	win, ok := body["window"].(map[string]any)
	if !ok || win["source"] != "platform" || win["version"] != float64(4) {
		t.Fatalf("window = %v", body["window"])
	}
	eg, ok := body["egress"].(map[string]any)
	if !ok || eg["identity"] != "https://eg-1" {
		t.Fatalf("egress = %v", body["egress"])
	}
	if diag.scopes[0].Kind() != corescope.KindUser {
		t.Fatalf("透传作用域 = %v", diag.scopes[0].Kind())
	}
}

// x-roles 运行时门：master 被拒于三个 REST 端点；admin 可诊断、
// 不可轮询；user 全部可访问。
func TestDiagnosticsRoleGates(t *testing.T) {
	diag := &fakeDiag{
		mine:   sampleReport(),
		status: appdiagnostic.StatusReport{UpdatedAt: diagNowTime},
		detail: appdiagnostic.Detail{
			Item: appdiagnostic.Item{AccountID: "a1", Suggestion: corediag.SuggestWait},
		},
	}
	cases := []struct {
		scope                        corescope.Scope
		wantDiag, wantDetail, wantSS int
	}{
		{corescope.Master(), 403, 403, 403},
		{corescope.Admin([]string{"s1"}), 200, 200, 403},
		{corescope.User("u1"), 200, 200, 200},
	}
	for _, c := range cases {
		engine, issued := newEngine(t, &fakeAuthorizer{scope: c.scope}, diag, appevents.NewBroker())
		if got := doGet(engine, issued, "/api/me/diagnostics", nil).Code; got != c.wantDiag {
			t.Fatalf("diag scope=%v status=%d, want %d", c.scope.Kind(), got, c.wantDiag)
		}
		if got := doGet(engine, issued, "/api/accounts/a1/diagnostics", nil).Code; got != c.wantDetail {
			t.Fatalf("detail scope=%v status=%d, want %d", c.scope.Kind(), got, c.wantDetail)
		}
		if got := doGet(engine, issued, "/api/me/sign-status", nil).Code; got != c.wantSS {
			t.Fatalf("sign-status scope=%v status=%d, want %d", c.scope.Kind(), got, c.wantSS)
		}
	}
}
