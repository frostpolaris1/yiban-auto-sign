package account_test

// 账号变更限流的入口层判据：每属主密钥、429 带 Retry-After、用例不被调用。

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/gin-gonic/gin"

	appaccount "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/account"
	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
	accounthttp "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/account"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/oapi"
)

type fakeService struct {
	creates int
	updates int
	// 自助面记录：作用域必须由会话构造，且对任何角色都是 user 形状。
	selfDeletes  int
	selfRestores int
	selfScope    corescope.Scope
	restores     int
	purges       int
	batches      int
}

func (f *fakeService) List(context.Context, corescope.Scope, appaccount.ListFilter, string, int) (appaccount.Page, error) {
	return appaccount.Page{}, nil
}

func (f *fakeService) Create(context.Context, corescope.Scope, appaccount.CreateInput, appaudit.Actor) (appaccount.Record, error) {
	f.creates++
	return appaccount.Record{}, nil
}

func (f *fakeService) Get(context.Context, corescope.Scope, string) (appaccount.Record, error) {
	return appaccount.Record{}, nil
}

func (f *fakeService) Summary(context.Context, corescope.Scope) (appaccount.StatusCounts, error) {
	return appaccount.StatusCounts{}, nil
}

func (f *fakeService) Update(context.Context, corescope.Scope, string, appaccount.UpdateInput, appaudit.Actor) (appaccount.Record, error) {
	f.updates++
	return appaccount.Record{}, nil
}

func (f *fakeService) Review(context.Context, corescope.Scope, string, bool, string, appaudit.Actor) (appaccount.Record, error) {
	return appaccount.Record{}, nil
}

func (f *fakeService) Pause(context.Context, corescope.Scope, string, bool, appaudit.Actor) (appaccount.Record, error) {
	return appaccount.Record{}, nil
}

func (f *fakeService) Restore(context.Context, corescope.Scope, string, appaudit.Actor) (appaccount.Record, error) {
	f.restores++
	return appaccount.Record{}, nil
}

func (f *fakeService) DeviceRefresh(context.Context, corescope.Scope, string, appaudit.Actor) (appaccount.DeviceRefreshResult, error) {
	return appaccount.DeviceRefreshResult{}, nil
}

func (f *fakeService) Detect(context.Context, corescope.Scope, appaccount.DetectInput) (appaccount.DetectResult, error) {
	return appaccount.DetectResult{}, nil
}

func (f *fakeService) Delete(context.Context, corescope.Scope, string, appaudit.Actor) error {
	return nil
}

func (f *fakeService) Purge(context.Context, corescope.Scope, string, appaudit.Actor) error {
	f.purges++
	return nil
}

func (f *fakeService) Batch(context.Context, corescope.Scope, string, []string, string, appaudit.Actor) ([]appaccount.BatchItemResult, error) {
	f.batches++
	return nil, nil
}

func (f *fakeService) SelfDelete(_ context.Context, scope corescope.Scope, _ string, _ appaudit.Actor) error {
	f.selfDeletes++
	f.selfScope = scope
	return nil
}

func (f *fakeService) SelfRestore(_ context.Context, scope corescope.Scope, _ string, _ appaudit.Actor) (appaccount.Record, error) {
	f.selfRestores++
	f.selfScope = scope
	return appaccount.Record{}, nil
}

type fakeAuthorizer struct{}

func (fakeAuthorizer) Authorize(context.Context, coreauth.User) (corescope.Scope, appaudit.Actor, error) {
	return corescope.User("u1"), appaudit.Actor{UserID: "u1", Role: "user"}, nil
}

type fakeLimiter struct {
	allow bool
	wait  time.Duration
	keys  []string
}

func (f *fakeLimiter) AllowAccount(key string) (bool, time.Duration) {
	f.keys = append(f.keys, key)
	return f.allow, f.wait
}

func newContext(t *testing.T, method, path, body string) (*gin.Context, *httptest.ResponseRecorder) {
	t.Helper()
	gin.SetMode(gin.TestMode)
	rec := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(rec)
	req := httptest.NewRequest(method, path, strings.NewReader(body))
	req = req.WithContext(coreauth.WithSession(req.Context(), coreauth.Session{
		User: coreauth.User{ID: "u1", Role: coreauth.RoleUser, Status: coreauth.StatusActive},
	}))
	c.Request = req
	return c, rec
}

// 配额命中即 429 + Retry-After，且用例不被调用。
func TestCreateRateLimitedPerOwner(t *testing.T) {
	svc := &fakeService{}
	lim := &fakeLimiter{allow: false, wait: 2 * time.Second}
	h := accounthttp.NewHandler(svc, fakeAuthorizer{}, lim)
	c, rec := newContext(t, http.MethodPost, "/api/accounts",
		`{"school_id":"s1","phone":"13800000001","password":"x"}`)
	h.Create(c, oapi.AccountCreateParams{})
	if rec.Code != http.StatusTooManyRequests {
		t.Fatalf("status = %d, want 429: %s", rec.Code, rec.Body.String())
	}
	if rec.Header().Get("Retry-After") != "2" {
		t.Fatalf("Retry-After = %q, want 2", rec.Header().Get("Retry-After"))
	}
	if svc.creates != 0 {
		t.Fatal("限流命中后不得调用用例")
	}
	if len(lim.keys) != 1 || lim.keys[0] != "account-actions:u1" {
		t.Fatalf("限流键 = %v, want account-actions:u1", lim.keys)
	}
}

// 改号走同一个每属主桶。
func TestUpdateRateLimitedPerOwner(t *testing.T) {
	svc := &fakeService{}
	lim := &fakeLimiter{allow: false, wait: time.Second}
	h := accounthttp.NewHandler(svc, fakeAuthorizer{}, lim)
	c, rec := newContext(t, http.MethodPatch, "/api/accounts/a1", `{"phone":"13800000002"}`)
	h.Update(c, "a1", oapi.AccountUpdateParams{})
	if rec.Code != http.StatusTooManyRequests {
		t.Fatalf("status = %d, want 429", rec.Code)
	}
	if svc.updates != 0 {
		t.Fatal("限流命中后不得调用用例")
	}
}

// masterAuthorizer 返回全量作用域，代表管理角色登录后的常规授权形状。
type masterAuthorizer struct{}

func (masterAuthorizer) Authorize(context.Context, coreauth.User) (corescope.Scope, appaudit.Actor, error) {
	return corescope.Master(), appaudit.Actor{UserID: "u1", Role: coreauth.RoleMaster}, nil
}

// newMasterContext 构造 master 会话的请求上下文。
func newMasterContext(t *testing.T, body string) (*gin.Context, *httptest.ResponseRecorder) {
	t.Helper()
	gin.SetMode(gin.TestMode)
	rec := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(rec)
	req := httptest.NewRequest(http.MethodPost, "/api/me/accounts/a1/delete", strings.NewReader(body))
	req = req.WithContext(coreauth.WithSession(req.Context(), coreauth.Session{
		User: coreauth.User{ID: "u1", Role: coreauth.RoleMaster, Status: coreauth.StatusActive},
	}))
	c.Request = req
	return c, rec
}

// acceptReauth 是放行一切口令的二次口令替身。
type acceptReauth struct{}

func (acceptReauth) Reauthenticate(context.Context, string, string) error { return nil }

// 自助端点对 master 也只用会话 user id 构造作用域：
// 授权解析出的是全量作用域，但传给用例的必须是 user 形状。
func TestMyAccountDeleteNarrowsMasterScope(t *testing.T) {
	svc := &fakeService{}
	h := accounthttp.NewHandler(svc, masterAuthorizer{}).WithReauth(acceptReauth{})
	c, _ := newMasterContext(t, `{"current_password":"pw"}`)
	h.MyAccountDelete(c, "a1", oapi.MyAccountDeleteParams{})
	// 直接调用处理器时 gin 尚不落盘状态码（无响应体），读 Writer.Status。
	if got := c.Writer.Status(); got != http.StatusNoContent {
		t.Fatalf("status = %d, want 204", got)
	}
	if svc.selfDeletes != 1 {
		t.Fatalf("self deletes = %d, want 1", svc.selfDeletes)
	}
	if svc.selfScope.Kind() != corescope.KindUser || svc.selfScope.UserID() != "u1" {
		t.Fatalf("scope = kind %d user %q, want user u1", svc.selfScope.Kind(), svc.selfScope.UserID())
	}
}

// 无会话：401，且用例不被调用。
func TestMyAccountDeleteRequiresSession(t *testing.T) {
	svc := &fakeService{}
	h := accounthttp.NewHandler(svc, fakeAuthorizer{})
	gin.SetMode(gin.TestMode)
	rec := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(rec)
	c.Request = httptest.NewRequest(http.MethodPost, "/api/me/accounts/a1/delete", nil)
	h.MyAccountDelete(c, "a1", oapi.MyAccountDeleteParams{})
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("status = %d, want 401: %s", rec.Code, rec.Body.String())
	}
	if svc.selfDeletes != 0 {
		t.Fatal("无会话不得调用用例")
	}
}
