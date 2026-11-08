package signing_test

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"
	"time"

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
	actor appaudit.Actor
	err   error
}

func (f *fakeAuthorizer) Authorize(context.Context, coreauth.User) (corescope.Scope, appaudit.Actor, error) {
	return f.scope, f.actor, f.err
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
	verifier := &fakeVerifier{profile: contract.Profile{
		DisplayName: "张三", SchoolName: "示例大学", UniversityID: "0000000001",
	}}
	engine, issued := newTestRouter(t, &fakeAuthorizer{scope: corescope.Master()}, svc, verifier)

	rec := callVerify(t, engine, issued, true)
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, body=%s", rec.Code, rec.Body.String())
	}
	result := decodeResult(t, rec)
	if !result.Ok || result.Code != string(contract.OutcomeSuccess) {
		t.Fatalf("result = %+v", result)
	}
	// 提供方带出的院校两字段必须原样透出：建号定校链路拿它对学校表。
	if result.Identity == nil {
		t.Fatalf("identity is missing from a verified result: %+v", result)
	}
	if result.Identity.UniversityName != "示例大学" ||
		result.Identity.UniversityId != "0000000001" {
		t.Fatalf("identity = %+v", result.Identity)
	}
	// 姓名属个人隐私，不在 verify 顺手透出：响应体不得出现 person_name。
	if strings.Contains(rec.Body.String(), "person_name") {
		t.Fatalf("verify response leaked person_name: %q", rec.Body.String())
	}
	if verifier.last.Credential.Phone != "13800000000" ||
		verifier.last.Credential.Password != "secret" ||
		verifier.last.Credential.DeviceCode != "device-code" {
		t.Fatalf("verifier credential = %+v", verifier.last.Credential)
	}
}

// 平台可能只回签发回执不带身份数据（data={}）：identity 必须缺席，
// 不得回一个空字段的对象假装有数据；只带姓名的回执同样不出院校键。
func TestVerifyIdentityAbsentWithoutProfileData(t *testing.T) {
	svc := &fakeService{cred: appaccount.SigningCredential{SchoolID: "school-1", Phone: "13800000000"}}
	for name, profile := range map[string]contract.Profile{
		"空回执":  {},
		"只有姓名": {DisplayName: "张三"},
	} {
		engine, issued := newTestRouter(t, &fakeAuthorizer{scope: corescope.Master()},
			svc, &fakeVerifier{profile: profile})
		result := decodeResult(t, callVerify(t, engine, issued, true))
		if !result.Ok {
			t.Fatalf("%s: result = %+v", name, result)
		}
		if result.Identity != nil {
			t.Fatalf("%s: identity = %+v, want absent", name, result.Identity)
		}
	}
}

// 平台结论失败路径不携带身份：identity 只属于成功回执。
func TestVerifyFailureCarriesNoIdentity(t *testing.T) {
	svc := &fakeService{cred: appaccount.SigningCredential{SchoolID: "school-1"}}
	verifier := &fakeVerifier{err: &contract.OutcomeError{
		Outcome: contract.OutcomeCredentialBad, Message: "reject 13800138000",
	}}
	engine, issued := newTestRouter(t, &fakeAuthorizer{scope: corescope.Master()}, svc, verifier)

	result := decodeResult(t, callVerify(t, engine, issued, true))
	if result.Ok || result.Code != "credential_invalid" {
		t.Fatalf("result = %+v", result)
	}
	if result.Identity != nil {
		t.Fatalf("identity = %+v, want absent on a failure conclusion", result.Identity)
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
	// actor 记录入口层传下来的审计主体：重开审计靠它归属到人。
	actor appaudit.Actor
}

func (f *fakeManual) Request(_ context.Context, _ string, actor appaudit.Actor) (string, error) {
	f.calls++
	f.actor = actor
	return f.taskID, f.err
}

type fakeAccess struct {
	err    error
	status appaccount.Status
	calls  int
}

func (f *fakeAccess) Get(context.Context, corescope.Scope, string) (appaccount.Record, error) {
	f.calls++
	status := f.status
	if status == "" {
		status = appaccount.StatusActive
	}
	return appaccount.Record{Status: status}, f.err
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

// 手动签到成功：202 且回 task_id（入队而非完成）；
// 会话主体原样传到用例层，重开审计才有归属。
func TestSignAccepted(t *testing.T) {
	manual := &fakeManual{taskID: "task-1"}
	actor := appaudit.Actor{UserID: "u1", Label: "u***@example.test"}
	engine, issued := newSignRouter(t, &fakeAuthorizer{scope: corescope.Master(), actor: actor}, manual)
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
	if manual.actor != actor {
		t.Fatalf("审计主体 = %+v, want %+v", manual.actor, actor)
	}
}

// 非 active 账号不进入手动签到：旧线按钮只对 active 出现；
// 入口层把“找不到可用任务”的 404 收敛成 409 conflict，
// worker 侧仍以 active 谓词为根守卫。
func TestSignRejectsInactiveAccount(t *testing.T) {
	for _, status := range []appaccount.Status{
		appaccount.StatusPending,
		appaccount.StatusRejected,
		appaccount.StatusPausedAdmin,
		appaccount.StatusPausedUser,
		appaccount.StatusCredentialError,
	} {
		manual := &fakeManual{taskID: "task-1"}
		engine, issued := newSignRouterWithAccess(
			t, &fakeAuthorizer{scope: corescope.Master()}, manual, &fakeAccess{status: status})
		rec := callSign(t, engine, issued)
		if rec.Code != http.StatusConflict {
			t.Fatalf("%s: status = %d, want 409: %s", status, rec.Code, rec.Body.String())
		}
		if !strings.Contains(rec.Body.String(), "conflict") {
			t.Fatalf("%s: body = %s, want conflict code", status, rec.Body.String())
		}
		if manual.calls != 0 {
			t.Fatalf("%s: manual.Request called %d times, want 0", status, manual.calls)
		}
	}
}

// 四类冲突都映射 409 与稳定错误码。
func TestSignConflicts(t *testing.T) {
	cases := []struct {
		err  error
		code string
	}{
		{appworker.ErrAlreadySigned, "already_signed"},
		{appworker.ErrSignRunning, "sign_running"},
		{appworker.ErrWindowClosed, "window_closed"},
		// task_ambiguous 保留为历史错误码：多任务时随机选一个，不再返回。
		{errs.New(errs.CodeTaskAmbiguous, "legacy"), "task_ambiguous"},
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

// --- 批量签到 --------------------------------------------------------------

// batchAccess 按 id 给出可见面结论：errs 命中报错，status 命中用该状态，
// 其余视为 active。calls 记录访问顺序，供「失败继续」断言使用。
type batchAccess struct {
	errs   map[string]error
	status map[string]appaccount.Status
	calls  []string
}

func (f *batchAccess) Get(_ context.Context, _ corescope.Scope, id string) (appaccount.Record, error) {
	f.calls = append(f.calls, id)
	if err, ok := f.errs[id]; ok {
		return appaccount.Record{}, err
	}
	status := f.status[id]
	if status == "" {
		status = appaccount.StatusActive
	}
	return appaccount.Record{Status: status}, nil
}

// batchManual 按 id 给出入队结论：errs 命中报错，其余回 task-<id>。
type batchManual struct {
	errs  map[string]error
	calls []string
}

func (f *batchManual) Request(_ context.Context, accountID string, _ appaudit.Actor) (string, error) {
	f.calls = append(f.calls, accountID)
	if err, ok := f.errs[accountID]; ok {
		return "", err
	}
	return "task-" + accountID, nil
}

// batchLimiter 按 id 拒绝限流：denied 命中即拒，其余放行。
// calls 记录判定顺序，供「逐项判定、每 id 判一次」断言使用。
type batchLimiter struct {
	denied map[string]bool
	calls  []string
}

func (f *batchLimiter) AllowAccount(id string) (bool, time.Duration) {
	f.calls = append(f.calls, id)
	return !f.denied[id], 0
}

// newBatchSignRouter 装配带全部接缝的路由（限流全放行），
// 供批量用例按 id 定制结论。
func newBatchSignRouter(t *testing.T, authz *fakeAuthorizer, manual *batchManual, access *batchAccess) (http.Handler, coreauth.Issued) {
	return newBatchSignRouterLimiter(t, authz, manual, access, &batchLimiter{})
}

// newBatchSignRouterLimiter 装配指定限流器的路由；limiter 为 nil 时
// 不注入，供「未接线 500」用例使用。
func newBatchSignRouterLimiter(t *testing.T, authz *fakeAuthorizer, manual *batchManual, access *batchAccess, limiter signinghttp.AccountLimiter) (http.Handler, coreauth.Issued) {
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
	if limiter != nil {
		handler = handler.WithAccountLimiter(limiter)
	}
	engine, err := router.New(router.Options{Sessions: issuer, InsecureCookies: true, Signing: handler})
	if err != nil {
		t.Fatalf("router: %v", err)
	}
	return engine, issued
}

func callBatchSign(t *testing.T, engine http.Handler, issued coreauth.Issued, body any) *httptest.ResponseRecorder {
	t.Helper()
	raw, err := json.Marshal(body)
	if err != nil {
		t.Fatalf("marshal body: %v", err)
	}
	req := httptest.NewRequest(http.MethodPost, "/api/accounts/batch/sign", bytes.NewReader(raw))
	req.Header.Set("Content-Type", "application/json")
	req.AddCookie(&http.Cookie{Name: coreauth.InsecureCookieName, Value: issued.Token})
	req.Header.Set("X-CSRF-Token", issued.CSRFToken)
	rec := httptest.NewRecorder()
	engine.ServeHTTP(rec, req)
	return rec
}

func decodeBatchResult(t *testing.T, rec *httptest.ResponseRecorder) oapi.AccountBatchSignResult {
	t.Helper()
	var result oapi.AccountBatchSignResult
	if err := json.Unmarshal(rec.Body.Bytes(), &result); err != nil {
		t.Fatalf("decode %q: %v", rec.Body.String(), err)
	}
	return result
}

// user 整批 403：角色门先于任何逐条动作，一条都不入队。
func TestBatchSignUserForbidden(t *testing.T) {
	manual := &batchManual{}
	engine, issued := newBatchSignRouter(t,
		&fakeAuthorizer{scope: corescope.User("u1")}, manual, &batchAccess{})
	rec := callBatchSign(t, engine, issued, oapi.AccountBatchSignRequest{Ids: []string{"acc-1"}})
	if rec.Code != http.StatusForbidden {
		t.Fatalf("status = %d, want 403: %s", rec.Code, rec.Body.String())
	}
	if len(manual.calls) != 0 {
		t.Fatalf("user 不得入队任何一条, manual=%v", manual.calls)
	}
}

// ids 空、超过 10 个、重复一律整批 400，一条都不跑。
func TestBatchSignValidationRejects(t *testing.T) {
	manual := &batchManual{}
	engine, issued := newBatchSignRouter(t,
		&fakeAuthorizer{scope: corescope.Master()}, manual, &batchAccess{})
	cases := map[string][]string{
		"空":   {},
		"11个": {"a1", "a2", "a3", "a4", "a5", "a6", "a7", "a8", "a9", "a10", "a11"},
		"重复":  {"acc-1", "acc-2", "acc-1"},
	}
	for name, ids := range cases {
		rec := callBatchSign(t, engine, issued, oapi.AccountBatchSignRequest{Ids: ids})
		if rec.Code != http.StatusBadRequest {
			t.Fatalf("%s: status = %d, want 400: %s", name, rec.Code, rec.Body.String())
		}
		if !strings.Contains(rec.Body.String(), "validation_failed") {
			t.Fatalf("%s: body = %s, want validation_failed", name, rec.Body.String())
		}
	}
	if len(manual.calls) != 0 {
		t.Fatalf("校验失败不得入队任何一条, manual=%v", manual.calls)
	}
}

// 混合批次：顺序同请求；成功项带 task_id 不带 code/message；
// 错误项带稳定 code 不带 task_id；一条失败不中断后续。
func TestBatchSignItemsKeepOrderAndShape(t *testing.T) {
	access := &batchAccess{
		errs:   map[string]error{"acc-b": errs.New(errs.CodeNotFound, "account not found")},
		status: map[string]appaccount.Status{"acc-c": appaccount.StatusPausedAdmin},
	}
	manual := &batchManual{errs: map[string]error{"acc-d": appworker.ErrWindowClosed}}
	engine, issued := newBatchSignRouter(t, &fakeAuthorizer{scope: corescope.Master()}, manual, access)
	ids := []string{"acc-a", "acc-b", "acc-c", "acc-d", "acc-e"}
	rec := callBatchSign(t, engine, issued, oapi.AccountBatchSignRequest{Ids: ids})
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200: %s", rec.Code, rec.Body.String())
	}
	result := decodeBatchResult(t, rec)
	if len(result.Items) != len(ids) {
		t.Fatalf("items = %d, want %d", len(result.Items), len(ids))
	}
	for i, id := range ids {
		if result.Items[i].Id != id {
			t.Fatalf("items[%d].id = %s, want %s（顺序必须同请求）", i, result.Items[i].Id, id)
		}
	}
	ok := result.Items[0]
	if ok.Status != oapi.AccountBatchSignItemStatusOk || ok.TaskId == nil || *ok.TaskId != "task-acc-a" {
		t.Fatalf("items[0] = %+v, want ok with task_id", ok)
	}
	if ok.Code != nil || ok.Message != nil {
		t.Fatalf("成功项不得带 code/message: %+v", ok)
	}
	missing := result.Items[1]
	if missing.Status != oapi.AccountBatchSignItemStatusError || missing.Code == nil || *missing.Code != oapi.NotFound {
		t.Fatalf("items[1] = %+v, want error not_found", missing)
	}
	if missing.TaskId != nil {
		t.Fatalf("错误项不得带 task_id: %+v", missing)
	}
	inactive := result.Items[2]
	if inactive.Code == nil || *inactive.Code != oapi.Conflict {
		t.Fatalf("items[2] = %+v, want error conflict", inactive)
	}
	closed := result.Items[3]
	if closed.Code == nil || *closed.Code != oapi.WindowClosed {
		t.Fatalf("items[3] = %+v, want error window_closed", closed)
	}
	last := result.Items[4]
	if last.Status != oapi.AccountBatchSignItemStatusOk || last.TaskId == nil {
		t.Fatalf("items[4] = %+v, want ok（错误不中断后续）", last)
	}
	// acc-b 卡在可见面、acc-c 卡在非 active 门，都不得到入队口；
	// 其余三条照常入队。
	want := []string{"acc-a", "acc-d", "acc-e"}
	if strings.Join(manual.calls, ",") != strings.Join(want, ",") {
		t.Fatalf("manual = %v, want %v", manual.calls, want)
	}
	if len(access.calls) != len(ids) {
		t.Fatalf("access = %v, want 全部 %d 个 id", access.calls, len(ids))
	}
}

// 未装配 signing 处理器：501，与单条端点同门。
func TestBatchSignUnwired(t *testing.T) {
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
	rec := callBatchSign(t, engine, issued, oapi.AccountBatchSignRequest{Ids: []string{"acc-1"}})
	if rec.Code != http.StatusNotImplemented {
		t.Fatalf("status = %d, want 501: %s", rec.Code, rec.Body.String())
	}
}

// 账号键限流命中：per-item rate_limited，后续 id 照常判定入队；
// 每个 id 判一次限流，先于可见面（与单条端点同序）。
func TestBatchSignAccountLimitHit(t *testing.T) {
	limiter := &batchLimiter{denied: map[string]bool{"acc-b": true}}
	manual := &batchManual{}
	engine, issued := newBatchSignRouterLimiter(t,
		&fakeAuthorizer{scope: corescope.Master()}, manual, &batchAccess{}, limiter)
	rec := callBatchSign(t, engine, issued,
		oapi.AccountBatchSignRequest{Ids: []string{"acc-a", "acc-b", "acc-c"}})
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200: %s", rec.Code, rec.Body.String())
	}
	result := decodeBatchResult(t, rec)
	if len(result.Items) != 3 {
		t.Fatalf("items = %d, want 3", len(result.Items))
	}
	limited := result.Items[1]
	if limited.Status != oapi.AccountBatchSignItemStatusError ||
		limited.Code == nil || *limited.Code != oapi.RateLimited || limited.TaskId != nil {
		t.Fatalf("items[1] = %+v, want error rate_limited without task_id", limited)
	}
	if result.Items[0].TaskId == nil || *result.Items[0].TaskId != "task-acc-a" {
		t.Fatalf("items[0] = %+v, want ok（前项不受影响）", result.Items[0])
	}
	if result.Items[2].TaskId == nil || *result.Items[2].TaskId != "task-acc-c" {
		t.Fatalf("items[2] = %+v, want ok（命中项不拖垮整批）", result.Items[2])
	}
	if strings.Join(manual.calls, ",") != "acc-a,acc-c" {
		t.Fatalf("manual = %v, want acc-a,acc-c", manual.calls)
	}
	if strings.Join(limiter.calls, ",") != "acc-a,acc-b,acc-c" {
		t.Fatalf("limiter = %v, want 逐项判定三个 id", limiter.calls)
	}
}

// 限流器未接线＝装配错误：500 响亮失败，契约 429 不得静默不可达。
func TestBatchSignWithoutLimiterFailsLoud(t *testing.T) {
	engine, issued := newBatchSignRouterLimiter(t,
		&fakeAuthorizer{scope: corescope.Master()}, &batchManual{}, &batchAccess{}, nil)
	rec := callBatchSign(t, engine, issued, oapi.AccountBatchSignRequest{Ids: []string{"acc-1"}})
	if rec.Code != http.StatusInternalServerError {
		t.Fatalf("未接线 status = %d, want 500: %s", rec.Code, rec.Body.String())
	}
	if !strings.Contains(rec.Body.String(), "internal_error") {
		t.Fatalf("body = %s, want internal_error", rec.Body.String())
	}
}

// 限流键归一与单条端点同口径：id 先小写去首尾空白再判桶，
// 大小写变体不得拆桶；Get 与入队仍收原样 id。
func TestBatchSignLimitKeyNormalized(t *testing.T) {
	limiter := &batchLimiter{denied: map[string]bool{"acc-b": true}}
	access := &batchAccess{}
	manual := &batchManual{}
	engine, issued := newBatchSignRouterLimiter(t,
		&fakeAuthorizer{scope: corescope.Master()}, manual, access, limiter)
	rec := callBatchSign(t, engine, issued,
		oapi.AccountBatchSignRequest{Ids: []string{"ACC-B", "ACC-A"}})
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200: %s", rec.Code, rec.Body.String())
	}
	result := decodeBatchResult(t, rec)
	if result.Items[0].Code == nil || *result.Items[0].Code != oapi.RateLimited {
		t.Fatalf("items[0] = %+v, want rate_limited（大写 id 须命中归一键）", result.Items[0])
	}
	if result.Items[1].TaskId == nil || *result.Items[1].TaskId != "task-ACC-A" {
		t.Fatalf("items[1] = %+v, want ok（限流键归一不改变入队 id）", result.Items[1])
	}
	if strings.Join(limiter.calls, ",") != "acc-b,acc-a" {
		t.Fatalf("limiter keys = %v, want 归一后小写键", limiter.calls)
	}
	// 限流只归一键：Get 与入队仍收原样 id。
	if strings.Join(access.calls, ",") != "ACC-A" {
		t.Fatalf("access = %v, want ACC-A（原样 id）", access.calls)
	}
	if strings.Join(manual.calls, ",") != "ACC-A" {
		t.Fatalf("manual = %v, want ACC-A（原样 id）", manual.calls)
	}
}

// --- 平台姓名懒加载 ----------------------------------------------------------

// fakeRevealAuditor 记录揭示审计的调用与入参。
type fakeRevealAuditor struct {
	scope    corescope.Scope
	id       string
	schoolID string
	actor    appaudit.Actor
	calls    int
	err      error
}

func (f *fakeRevealAuditor) RecordPersonNameReveal(
	_ context.Context, scope corescope.Scope, id, schoolID string, actor appaudit.Actor) error {
	f.calls++
	f.scope, f.id, f.schoolID, f.actor = scope, id, schoolID, actor
	return f.err
}

// newPersonNameRouter 装配带揭示审计接缝的路由；auditor 传 nil 供未接线用例。
func newPersonNameRouter(t *testing.T, authz *fakeAuthorizer, svc *fakeService,
	verifier *fakeVerifier, auditor signinghttp.RevealAuditor) (http.Handler, coreauth.Issued) {
	t.Helper()
	issuer := &fakeIssuer{secret: []byte("0123456789abcdef0123456789abcdef")}
	issued, err := issuer.Issue(context.Background(), coreauth.User{
		ID: "u1", Email: "boss@example.com", Role: coreauth.RoleMaster, Status: coreauth.StatusActive,
	})
	if err != nil {
		t.Fatalf("issue session: %v", err)
	}
	handler := signinghttp.NewHandler(svc, authz, verifier).WithRevealAudit(auditor)
	engine, err := router.New(router.Options{Sessions: issuer, InsecureCookies: true, Signing: handler})
	if err != nil {
		t.Fatalf("router: %v", err)
	}
	return engine, issued
}

func callPersonName(t *testing.T, engine http.Handler, issued coreauth.Issued) *httptest.ResponseRecorder {
	t.Helper()
	req := httptest.NewRequest(http.MethodPost, "/api/accounts/acc-1/person-name", nil)
	req.AddCookie(&http.Cookie{Name: coreauth.InsecureCookieName, Value: issued.Token})
	req.Header.Set("X-CSRF-Token", issued.CSRFToken)
	rec := httptest.NewRecorder()
	engine.ServeHTTP(rec, req)
	return rec
}

// 成功：只回姓名单字段；审计接缝收到作用域、账号与院校。
func TestPersonNameReturnsNameAndAudits(t *testing.T) {
	svc := &fakeService{cred: appaccount.SigningCredential{SchoolID: "school-1", Phone: "13800000000"}}
	verifier := &fakeVerifier{profile: contract.Profile{
		DisplayName: "张三", SchoolName: "示例大学", UniversityID: "0000000001",
	}}
	auditor := &fakeRevealAuditor{}
	engine, issued := newPersonNameRouter(t, &fakeAuthorizer{scope: corescope.Master()}, svc, verifier, auditor)

	rec := callPersonName(t, engine, issued)
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, body=%s", rec.Code, rec.Body.String())
	}
	var result oapi.AccountPersonNameResult
	if err := json.Unmarshal(rec.Body.Bytes(), &result); err != nil {
		t.Fatalf("decode %q: %v", rec.Body.String(), err)
	}
	if result.PersonName != "张三" {
		t.Fatalf("person_name = %q", result.PersonName)
	}
	// 响应只有 person_name 一个键：院校字段不搭车。
	if strings.Contains(rec.Body.String(), "university") {
		t.Fatalf("person-name response carried university fields: %q", rec.Body.String())
	}
	if auditor.calls != 1 || auditor.id != "acc-1" || auditor.schoolID != "school-1" {
		t.Fatalf("auditor = %+v", auditor)
	}
}

// user 只能取自己账号：作用域内放行。
func TestPersonNameUserOwnAllowed(t *testing.T) {
	svc := &fakeService{cred: appaccount.SigningCredential{SchoolID: "school-1", Phone: "13800000000"}}
	verifier := &fakeVerifier{profile: contract.Profile{DisplayName: "张三"}}
	auditor := &fakeRevealAuditor{}
	engine, issued := newPersonNameRouter(t,
		&fakeAuthorizer{scope: corescope.User("u1")}, svc, verifier, auditor)

	rec := callPersonName(t, engine, issued)
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, body=%s", rec.Code, rec.Body.String())
	}
	var result oapi.AccountPersonNameResult
	if err := json.Unmarshal(rec.Body.Bytes(), &result); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if result.PersonName != "张三" || auditor.calls != 1 {
		t.Fatalf("result = %+v auditor = %+v", result, auditor)
	}
}

// user 越权他人账号：作用域读不到行，404 与签到同口径。
func TestPersonNameUserCrossScopeNotFound(t *testing.T) {
	svc := &fakeService{err: errs.New(errs.CodeNotFound, "account not found")}
	engine, issued := newPersonNameRouter(t,
		&fakeAuthorizer{scope: corescope.User("u1")}, svc, &fakeVerifier{}, &fakeRevealAuditor{})

	rec := callPersonName(t, engine, issued)
	if rec.Code != http.StatusNotFound {
		t.Fatalf("status = %d, body=%s", rec.Code, rec.Body.String())
	}
}

// 平台失败统一 502 中性信封：结论文案不出本层，姓名响应没有结论形状。
func TestPersonNamePlatformFailureIs502(t *testing.T) {
	svc := &fakeService{cred: appaccount.SigningCredential{SchoolID: "school-1"}}
	for name, err := range map[string]error{
		"凭据结论": &contract.OutcomeError{Outcome: contract.OutcomeCredentialBad, Message: "reject 13800138000"},
		"传输失败": &url.Error{Op: "Get", URL: "https://api.uyiban.com", Err: errors.New("timeout")},
	} {
		engine, issued := newPersonNameRouter(t, &fakeAuthorizer{scope: corescope.Master()},
			svc, &fakeVerifier{err: err}, &fakeRevealAuditor{})
		rec := callPersonName(t, engine, issued)
		if rec.Code != http.StatusBadGateway {
			t.Fatalf("%s: status = %d, want 502; body=%s", name, rec.Code, rec.Body.String())
		}
		if strings.Contains(rec.Body.String(), "reject 138") {
			t.Fatalf("%s: response leaked platform text: %q", name, rec.Body.String())
		}
	}
}

// 审计接缝未接线＝装配错误：500 响亮失败，揭示不得无痕发生。
func TestPersonNameUnwiredAuditorFailsLoud(t *testing.T) {
	svc := &fakeService{cred: appaccount.SigningCredential{SchoolID: "school-1"}}
	engine, issued := newPersonNameRouter(t, &fakeAuthorizer{scope: corescope.Master()},
		svc, &fakeVerifier{profile: contract.Profile{DisplayName: "张三"}}, nil)
	rec := callPersonName(t, engine, issued)
	if rec.Code != http.StatusInternalServerError {
		t.Fatalf("status = %d, want 500: %s", rec.Code, rec.Body.String())
	}
}

// 审计写失败：响应 5xx，姓名不得在审计缺失的情况下外泄。
func TestPersonNameAuditFailureBlocksResponse(t *testing.T) {
	svc := &fakeService{cred: appaccount.SigningCredential{SchoolID: "school-1"}}
	verifier := &fakeVerifier{profile: contract.Profile{DisplayName: "张三"}}
	auditor := &fakeRevealAuditor{err: errors.New("audit insert failed")}
	engine, issued := newPersonNameRouter(t, &fakeAuthorizer{scope: corescope.Master()}, svc, verifier, auditor)
	rec := callPersonName(t, engine, issued)
	if rec.Code < 500 {
		t.Fatalf("status = %d, want 5xx; body=%s", rec.Code, rec.Body.String())
	}
	if strings.Contains(rec.Body.String(), "张三") {
		t.Fatalf("audit failure still leaked the name: %q", rec.Body.String())
	}
}
