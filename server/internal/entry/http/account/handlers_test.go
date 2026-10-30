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

func (f *fakeService) Delete(context.Context, corescope.Scope, string, appaudit.Actor) error {
	return nil
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
