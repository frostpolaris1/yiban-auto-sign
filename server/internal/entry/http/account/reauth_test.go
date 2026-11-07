package account_test

// 危险操作二次口令的入口层判据。
//
// # 功能
// 钉住六个端点的门形：删号、自助删号、设备刷新、管理员恢复与
// 自助恢复总是要求 current_password；改号只在携带
// phone/password/device_code 时要求，只改 name 不要求。
// 缺失 400、错误 403、用例不被调用。
// 本文件**不**连数据库，**不**做真实口令校验。
//
// # 口径
// 真库与真 HTTP 行为由 cmd/server 的 e2e 负责。

import (
	"context"
	"errors"
	"net/http"
	"testing"
	"time"

	appauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/auth"

	"github.com/gin-gonic/gin"

	appaccount "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/account"
	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
	accounthttp "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/account"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/oapi"
)

// recordingReauth 记录收到的 user 与口令，按预设复述错误。
type recordingReauth struct {
	calls  int
	userID string
	pw     string
	err    error
}

func (r *recordingReauth) Reauthenticate(_ context.Context, userID, password string) error {
	r.calls++
	r.userID = userID
	r.pw = password
	return r.err
}

// deleteFake 与 fakeService 相同但单独数删除与设备刷新。
type deleteFake struct {
	fakeService
	deletes int
}

func (f *deleteFake) Delete(context.Context, corescope.Scope, string, appaudit.Actor) error {
	f.deletes++
	return nil
}

func (f *deleteFake) DeviceRefresh(context.Context, corescope.Scope, string, appaudit.Actor) (appaccount.DeviceRefreshResult, error) {
	return appaccount.DeviceRefreshResult{DeviceCode: "dev-1234"}, nil
}

// callHandler 在测试上下文里调 handler 并返回状态码。
// gin 的包装 writer 才知道真实状态码（recorder 默认 200）。
func callHandler(t *testing.T, h *accounthttp.Handler, method, path, body string,
	invoke func(*accounthttp.Handler, *gin.Context)) int {
	t.Helper()
	gin.SetMode(gin.TestMode)
	c, _ := newContext(t, method, path, body)
	invoke(h, c)
	return c.Writer.Status()
}

// 缺 current_password：400，用例与 reauth 都不被调用。
func TestDeleteRequiresCurrentPassword(t *testing.T) {
	svc := &deleteFake{}
	r := &recordingReauth{}
	h := accounthttp.NewHandler(svc, fakeAuthorizer{}).WithReauth(r)
	code := callHandler(t, h, http.MethodDelete, "/api/accounts/a1", `{}`,
		func(h *accounthttp.Handler, c *gin.Context) { h.Delete(c, "a1", oapi.AccountDeleteParams{}) })
	if code != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400", code)
	}
	if svc.deletes != 0 || r.calls != 0 {
		t.Fatalf("deletes=%d reauth calls=%d, want 0/0", svc.deletes, r.calls)
	}
}

// 错误口令：403，用例不被调用。
func TestDeleteRejectsWrongPassword(t *testing.T) {
	svc := &deleteFake{}
	r := &recordingReauth{err: errs.New(errs.CodeForbidden, "reauthentication failed")}
	h := accounthttp.NewHandler(svc, fakeAuthorizer{}).WithReauth(r)
	code := callHandler(t, h, http.MethodDelete, "/api/accounts/a1", `{"current_password":"pw"}`,
		func(h *accounthttp.Handler, c *gin.Context) { h.Delete(c, "a1", oapi.AccountDeleteParams{}) })
	if code != http.StatusForbidden {
		t.Fatalf("status = %d, want 403", code)
	}
	if svc.deletes != 0 {
		t.Fatal("a rejected reauth must not reach the use case")
	}
}

// 正确口令：204，reauth 收到的 user 是会话主体。
func TestDeletePassesWithRightPassword(t *testing.T) {
	svc := &deleteFake{}
	r := &recordingReauth{}
	h := accounthttp.NewHandler(svc, fakeAuthorizer{}).WithReauth(r)
	code := callHandler(t, h, http.MethodDelete, "/api/accounts/a1", `{"current_password":"pw"}`,
		func(h *accounthttp.Handler, c *gin.Context) { h.Delete(c, "a1", oapi.AccountDeleteParams{}) })
	if code != http.StatusNoContent {
		t.Fatalf("status = %d, want 204", code)
	}
	if svc.deletes != 1 || r.calls != 1 || r.userID != "u1" || r.pw != "pw" {
		t.Fatalf("deletes=%d reauth=%d user=%q pw=%q", svc.deletes, r.calls, r.userID, r.pw)
	}
}

// 未注入 reauth：500 失败关闭，用例不被调用。
func TestDeleteWithoutWiredReauthFailsClosed(t *testing.T) {
	svc := &deleteFake{}
	h := accounthttp.NewHandler(svc, fakeAuthorizer{})
	code := callHandler(t, h, http.MethodDelete, "/api/accounts/a1", `{"current_password":"pw"}`,
		func(h *accounthttp.Handler, c *gin.Context) { h.Delete(c, "a1", oapi.AccountDeleteParams{}) })
	if code != http.StatusInternalServerError {
		t.Fatalf("status = %d, want 500", code)
	}
	if svc.deletes != 0 {
		t.Fatal("unwired reauth must not reach the use case")
	}
}

// 自助删号同样要求口令：缺失 400。
func TestMyAccountDeleteRequiresCurrentPassword(t *testing.T) {
	svc := &fakeService{}
	h := accounthttp.NewHandler(svc, fakeAuthorizer{}).WithReauth(&recordingReauth{})
	code := callHandler(t, h, http.MethodPost, "/api/me/accounts/a1/delete", `{}`,
		func(h *accounthttp.Handler, c *gin.Context) { h.MyAccountDelete(c, "a1", oapi.MyAccountDeleteParams{}) })
	if code != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400", code)
	}
	if svc.selfDeletes != 0 {
		t.Fatal("a missing password must not reach the use case")
	}
}

// 设备刷新要求口令：缺失 400、正确 200。
func TestDeviceRefreshReauthGate(t *testing.T) {
	svc := &deleteFake{}
	h := accounthttp.NewHandler(svc, fakeAuthorizer{}).WithReauth(&recordingReauth{})
	code := callHandler(t, h, http.MethodPost, "/api/accounts/a1/device-refresh", `{}`,
		func(h *accounthttp.Handler, c *gin.Context) {
			h.DeviceRefresh(c, "a1", oapi.AccountDeviceRefreshParams{})
		})
	if code != http.StatusBadRequest {
		t.Fatalf("missing password status = %d, want 400", code)
	}
	code = callHandler(t, h, http.MethodPost, "/api/accounts/a1/device-refresh", `{"current_password":"pw"}`,
		func(h *accounthttp.Handler, c *gin.Context) {
			h.DeviceRefresh(c, "a1", oapi.AccountDeviceRefreshParams{})
		})
	if code != http.StatusOK {
		t.Fatalf("right password status = %d, want 200", code)
	}
}

// 改号：带 phone 必须口令；只改 name 不要口令。
func TestUpdateReauthOnlyForCredentialFields(t *testing.T) {
	svc := &fakeService{}
	r := &recordingReauth{}
	h := accounthttp.NewHandler(svc, fakeAuthorizer{}).WithReauth(r)

	// 只改 name：不要口令，reauth 不被调用，用例被调用。
	code := callHandler(t, h, http.MethodPatch, "/api/accounts/a1", `{"name":"shop"}`,
		func(h *accounthttp.Handler, c *gin.Context) { h.Update(c, "a1", oapi.AccountUpdateParams{}) })
	if code != http.StatusOK {
		t.Fatalf("name-only status = %d, want 200", code)
	}
	if r.calls != 0 || svc.updates != 1 {
		t.Fatalf("name-only reauth=%d updates=%d, want 0/1", r.calls, svc.updates)
	}

	// 带 phone：缺口令 400。
	code = callHandler(t, h, http.MethodPatch, "/api/accounts/a1", `{"phone":"13800000003"}`,
		func(h *accounthttp.Handler, c *gin.Context) { h.Update(c, "a1", oapi.AccountUpdateParams{}) })
	if code != http.StatusBadRequest {
		t.Fatalf("phone without password status = %d, want 400", code)
	}

	// 带 phone 与空串 device_code：都算凭据面，缺口令 400。
	code = callHandler(t, h, http.MethodPatch, "/api/accounts/a1", `{"device_code":""}`,
		func(h *accounthttp.Handler, c *gin.Context) { h.Update(c, "a1", oapi.AccountUpdateParams{}) })
	if code != http.StatusBadRequest {
		t.Fatalf("empty device_code status = %d, want 400", code)
	}

	// 带 phone 且口令错误：403，用例不被调用。
	r.err = errs.New(errs.CodeForbidden, "reauthentication failed")
	code = callHandler(t, h, http.MethodPatch, "/api/accounts/a1",
		`{"phone":"13800000003","current_password":"pw"}`,
		func(h *accounthttp.Handler, c *gin.Context) { h.Update(c, "a1", oapi.AccountUpdateParams{}) })
	if code != http.StatusForbidden {
		t.Fatalf("phone with wrong password status = %d, want 403", code)
	}
	if svc.updates != 1 {
		t.Fatalf("updates after rejections = %d, want 1", svc.updates)
	}

	// 带 phone 且口令正确：200。
	r.err = nil
	code = callHandler(t, h, http.MethodPatch, "/api/accounts/a1",
		`{"phone":"13800000003","current_password":"pw"}`,
		func(h *accounthttp.Handler, c *gin.Context) { h.Update(c, "a1", oapi.AccountUpdateParams{}) })
	if code != http.StatusOK {
		t.Fatalf("phone with right password status = %d, want 200", code)
	}
	if r.calls != 2 || r.pw != "pw" {
		t.Fatalf("reauth calls=%d pw=%q, want 2/pw", r.calls, r.pw)
	}
}

// 节流命中：429 + Retry-After 头（秒，向上取整）。
func TestDeleteThrottledWritesRetryAfter(t *testing.T) {
	svc := &deleteFake{}
	r := &recordingReauth{err: appauth.NewReauthThrottled(90 * time.Second)}
	h := accounthttp.NewHandler(svc, fakeAuthorizer{}).WithReauth(r)
	gin.SetMode(gin.TestMode)
	c, rec := newContext(t, http.MethodDelete, "/api/accounts/a1", `{"current_password":"pw"}`)
	h.Delete(c, "a1", oapi.AccountDeleteParams{})
	if got := c.Writer.Status(); got != http.StatusTooManyRequests {
		t.Fatalf("status = %d, want 429", got)
	}
	if got := rec.Header().Get("Retry-After"); got != "90" {
		t.Fatalf("Retry-After = %q, want 90", got)
	}
	if svc.deletes != 0 {
		t.Fatal("a throttled request must not reach the use case")
	}
}

// reauth 返回非门形错误时原样透传（这里用内部错误）。
func TestReauthInternalErrorPropagates(t *testing.T) {
	svc := &deleteFake{}
	r := &recordingReauth{err: errors.New("db down")}
	h := accounthttp.NewHandler(svc, fakeAuthorizer{}).WithReauth(r)
	code := callHandler(t, h, http.MethodDelete, "/api/accounts/a1", `{"current_password":"pw"}`,
		func(h *accounthttp.Handler, c *gin.Context) { h.Delete(c, "a1", oapi.AccountDeleteParams{}) })
	if code != http.StatusInternalServerError {
		t.Fatalf("status = %d, want 500", code)
	}
	if svc.deletes != 0 {
		t.Fatal("use case must not run after a reauth failure")
	}
}

// 管理员恢复：缺口令 400、错口令 403、正确口令 200；失败零调用。
func TestAccountRestoreReauthGate(t *testing.T) {
	svc := &fakeService{}
	r := &recordingReauth{}
	h := accounthttp.NewHandler(svc, fakeAuthorizer{}).WithReauth(r)

	code := callHandler(t, h, http.MethodPost, "/api/accounts/a1/restore", `{}`,
		func(h *accounthttp.Handler, c *gin.Context) { h.Restore(c, "a1", oapi.AccountRestoreParams{}) })
	if code != http.StatusBadRequest {
		t.Fatalf("missing password status = %d, want 400", code)
	}
	if svc.restores != 0 || r.calls != 0 {
		t.Fatalf("restores=%d reauth=%d, want 0/0", svc.restores, r.calls)
	}

	r.err = errs.New(errs.CodeForbidden, "reauthentication failed")
	code = callHandler(t, h, http.MethodPost, "/api/accounts/a1/restore",
		`{"current_password":"wrong"}`,
		func(h *accounthttp.Handler, c *gin.Context) { h.Restore(c, "a1", oapi.AccountRestoreParams{}) })
	if code != http.StatusForbidden {
		t.Fatalf("wrong password status = %d, want 403", code)
	}
	if svc.restores != 0 {
		t.Fatal("a rejected reauth must not reach the use case")
	}

	r.err = nil
	code = callHandler(t, h, http.MethodPost, "/api/accounts/a1/restore",
		`{"current_password":"pw"}`,
		func(h *accounthttp.Handler, c *gin.Context) { h.Restore(c, "a1", oapi.AccountRestoreParams{}) })
	if code != http.StatusOK {
		t.Fatalf("right password status = %d, want 200", code)
	}
	if svc.restores != 1 || r.calls != 2 {
		t.Fatalf("restores=%d reauth=%d, want 1/2", svc.restores, r.calls)
	}
}

// 自助恢复：缺口令 400，用例不被调用；带口令 200 且作用域仍是会话本人。
func TestMyAccountRestoreReauthGate(t *testing.T) {
	svc := &fakeService{}
	h := accounthttp.NewHandler(svc, fakeAuthorizer{}).WithReauth(&recordingReauth{})

	code := callHandler(t, h, http.MethodPost, "/api/me/accounts/a1/restore", `{}`,
		func(h *accounthttp.Handler, c *gin.Context) {
			h.MyAccountRestore(c, "a1", oapi.MyAccountRestoreParams{})
		})
	if code != http.StatusBadRequest {
		t.Fatalf("missing password status = %d, want 400", code)
	}
	if svc.selfRestores != 0 {
		t.Fatal("a missing password must not reach the use case")
	}

	code = callHandler(t, h, http.MethodPost, "/api/me/accounts/a1/restore",
		`{"current_password":"pw"}`,
		func(h *accounthttp.Handler, c *gin.Context) {
			h.MyAccountRestore(c, "a1", oapi.MyAccountRestoreParams{})
		})
	if code != http.StatusOK {
		t.Fatalf("right password status = %d, want 200", code)
	}
	if svc.selfRestores != 1 || svc.selfScope.Kind() != corescope.KindUser {
		t.Fatalf("selfRestores=%d scope kind=%v", svc.selfRestores, svc.selfScope.Kind())
	}
}

// 物理清除：缺口令 400、错口令 403、正确口令 204；失败零调用。
func TestAccountPurgeReauthGate(t *testing.T) {
	svc := &fakeService{}
	r := &recordingReauth{}
	h := accounthttp.NewHandler(svc, fakeAuthorizer{}).WithReauth(r)

	code := callHandler(t, h, http.MethodPost, "/api/accounts/a1/purge", `{}`,
		func(h *accounthttp.Handler, c *gin.Context) { h.Purge(c, "a1", oapi.AccountPurgeParams{}) })
	if code != http.StatusBadRequest {
		t.Fatalf("missing password status = %d, want 400", code)
	}
	if svc.purges != 0 || r.calls != 0 {
		t.Fatalf("purges=%d reauth=%d, want 0/0", svc.purges, r.calls)
	}

	r.err = errs.New(errs.CodeForbidden, "reauthentication failed")
	code = callHandler(t, h, http.MethodPost, "/api/accounts/a1/purge",
		`{"current_password":"wrong"}`,
		func(h *accounthttp.Handler, c *gin.Context) { h.Purge(c, "a1", oapi.AccountPurgeParams{}) })
	if code != http.StatusForbidden {
		t.Fatalf("wrong password status = %d, want 403", code)
	}
	if svc.purges != 0 {
		t.Fatal("a rejected reauth must not reach the use case")
	}

	r.err = nil
	code = callHandler(t, h, http.MethodPost, "/api/accounts/a1/purge",
		`{"current_password":"pw"}`,
		func(h *accounthttp.Handler, c *gin.Context) { h.Purge(c, "a1", oapi.AccountPurgeParams{}) })
	if code != http.StatusNoContent {
		t.Fatalf("right password status = %d, want 204", code)
	}
	if svc.purges != 1 || r.calls != 2 {
		t.Fatalf("purges=%d reauth=%d, want 1/2", svc.purges, r.calls)
	}
}

// 物理清除未接线：500 失败关闭，用例不被调用。
func TestAccountPurgeWithoutWiredReauthFailsClosed(t *testing.T) {
	svc := &fakeService{}
	h := accounthttp.NewHandler(svc, fakeAuthorizer{})
	code := callHandler(t, h, http.MethodPost, "/api/accounts/a1/purge", `{"current_password":"pw"}`,
		func(h *accounthttp.Handler, c *gin.Context) { h.Purge(c, "a1", oapi.AccountPurgeParams{}) })
	if code != http.StatusInternalServerError {
		t.Fatalf("status = %d, want 500", code)
	}
	if svc.purges != 0 {
		t.Fatal("unwired reauth must not reach the use case")
	}
}
