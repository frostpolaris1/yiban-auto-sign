package user_test

// 危险操作二次口令的入口层判据（用户域）。
//
// # 功能
// 钉住五个端点的门形：改角色/状态、删用户、重置口令、恢复与新建
// 总是要求 current_password。缺失 400、错误 403、正确进用例、
// 未接线 500。本文件**不**连数据库，**不**做真实口令校验。

import (
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/gin-gonic/gin"

	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	appauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/auth"
	appuser "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/user"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/oapi"
	userhttp "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/user"
)

// fakeUserService 记录调用并返回固定形状。
type fakeUserService struct {
	creates  int
	updates  int
	deletes  int
	resets   int
	restores int
	purges   int
	updateIn appuser.UpdateInput
}

func (f *fakeUserService) Create(context.Context, corescope.Scope, appuser.CreateInput, appaudit.Actor) (appuser.Record, error) {
	f.creates++
	return appuser.Record{ID: "t2", Email: "t2@example.com", Role: coreauth.RoleUser, Status: coreauth.StatusActive}, nil
}

func (f *fakeUserService) List(context.Context, corescope.Scope, appuser.ListFilter, string, int) (appuser.Page, error) {
	return appuser.Page{Items: []appuser.Record{}}, nil
}

func (f *fakeUserService) Update(_ context.Context, _ corescope.Scope, _ string, in appuser.UpdateInput, _ appaudit.Actor) (appuser.Record, error) {
	f.updates++
	f.updateIn = in
	return appuser.Record{ID: "t1", Email: "t1@example.com", Role: coreauth.RoleUser, Status: coreauth.StatusActive}, nil
}

func (f *fakeUserService) PasswordReset(context.Context, corescope.Scope, string, appaudit.Actor) (string, error) {
	f.resets++
	return "temp-password-42", nil
}

func (f *fakeUserService) Delete(context.Context, corescope.Scope, string, appaudit.Actor) error {
	f.deletes++
	return nil
}

func (f *fakeUserService) Purge(context.Context, corescope.Scope, string, appaudit.Actor) error {
	f.purges++
	return nil
}

func (f *fakeUserService) Restore(context.Context, corescope.Scope, string, appaudit.Actor) (appuser.Record, error) {
	f.restores++
	return appuser.Record{ID: "t1", Email: "t1@example.com", Role: coreauth.RoleUser, Status: coreauth.StatusActive}, nil
}

func (f *fakeUserService) Lookup(context.Context, corescope.Scope, string, int, bool) ([]appuser.LookupRef, error) {
	return []appuser.LookupRef{}, nil
}

func (f *fakeUserService) MailNotify(context.Context, string) (bool, error) { return true, nil }

func (f *fakeUserService) SetMailNotify(context.Context, string, bool, appaudit.Actor) (bool, error) {
	return true, nil
}

// fakeUserAuthorizer 返回 master 作用域与会话主体。
type fakeUserAuthorizer struct{}

func (fakeUserAuthorizer) Authorize(context.Context, coreauth.User) (corescope.Scope, appaudit.Actor, error) {
	return corescope.Master(), appaudit.Actor{UserID: "u1", Role: coreauth.RoleMaster}, nil
}

// recordingReauth 记录收到的 user 与口令，按预设复述错误。
type recordingReauth struct {
	calls int
	err   error
}

func (r *recordingReauth) Reauthenticate(context.Context, string, string) error {
	r.calls++
	return r.err
}

// callUserHandler 在测试上下文里调 handler，返回 gin 状态码、响应体与响应头。
// gin 的包装 writer 才知道真实状态码（recorder 默认 200）。
func callUserHandler(t *testing.T, h *userhttp.Handler, method, path, body string,
	invoke func(*userhttp.Handler, *gin.Context)) (int, string, http.Header) {
	t.Helper()
	gin.SetMode(gin.TestMode)
	rec := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(rec)
	var reader io.Reader
	if body != "" {
		reader = strings.NewReader(body)
	} else {
		reader = strings.NewReader("")
	}
	req := httptest.NewRequest(method, path, reader)
	req = req.WithContext(coreauth.WithSession(req.Context(), coreauth.Session{
		User: coreauth.User{ID: "u1", Role: coreauth.RoleMaster, Status: coreauth.StatusActive},
	}))
	c.Request = req
	invoke(h, c)
	return c.Writer.Status(), rec.Body.String(), rec.Header()
}

// 缺口令：400，用例与 reauth 都不被调用。
func TestUserUpdateRequiresCurrentPassword(t *testing.T) {
	svc := &fakeUserService{}
	r := &recordingReauth{}
	h := userhttp.NewHandler(svc, fakeUserAuthorizer{}).WithReauth(r)
	code, _, _ := callUserHandler(t, h, http.MethodPatch, "/api/users/t1", `{"role":"admin"}`,
		func(h *userhttp.Handler, c *gin.Context) { h.Update(c, "t1", oapi.UserUpdateParams{}) })
	if code != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400", code)
	}
	if svc.updates != 0 || r.calls != 0 {
		t.Fatalf("updates=%d reauth=%d, want 0/0", svc.updates, r.calls)
	}
}

// 错误口令：403，用例不被调用。
func TestUserUpdateRejectsWrongPassword(t *testing.T) {
	svc := &fakeUserService{}
	r := &recordingReauth{err: errs.New(errs.CodeForbidden, "reauthentication failed")}
	h := userhttp.NewHandler(svc, fakeUserAuthorizer{}).WithReauth(r)
	code, _, _ := callUserHandler(t, h, http.MethodPatch, "/api/users/t1",
		`{"role":"admin","current_password":"pw"}`,
		func(h *userhttp.Handler, c *gin.Context) { h.Update(c, "t1", oapi.UserUpdateParams{}) })
	if code != http.StatusForbidden {
		t.Fatalf("status = %d, want 403", code)
	}
	if svc.updates != 0 {
		t.Fatal("a rejected reauth must not reach the use case")
	}
}

// 正确口令：200，角色变更原样进用例。
func TestUserUpdatePassesWithRightPassword(t *testing.T) {
	svc := &fakeUserService{}
	h := userhttp.NewHandler(svc, fakeUserAuthorizer{}).WithReauth(&recordingReauth{})
	code, _, _ := callUserHandler(t, h, http.MethodPatch, "/api/users/t1",
		`{"role":"admin","current_password":"pw"}`,
		func(h *userhttp.Handler, c *gin.Context) { h.Update(c, "t1", oapi.UserUpdateParams{}) })
	if code != http.StatusOK {
		t.Fatalf("status = %d, want 200", code)
	}
	if svc.updates != 1 || svc.updateIn.Role == nil || *svc.updateIn.Role != coreauth.RoleAdmin {
		t.Fatalf("updates=%d in=%+v", svc.updates, svc.updateIn)
	}
}

// 删用户：请求体缺字段 400；带口令 204。
func TestUserDeleteReauthGate(t *testing.T) {
	svc := &fakeUserService{}
	r := &recordingReauth{}
	h := userhttp.NewHandler(svc, fakeUserAuthorizer{}).WithReauth(r)
	code, _, _ := callUserHandler(t, h, http.MethodDelete, "/api/users/t1", `{}`,
		func(h *userhttp.Handler, c *gin.Context) { h.Delete(c, "t1", oapi.UserDeleteParams{}) })
	if code != http.StatusBadRequest {
		t.Fatalf("missing password status = %d, want 400", code)
	}
	if svc.deletes != 0 {
		t.Fatal("a missing password must not reach the use case")
	}
	code, _, _ = callUserHandler(t, h, http.MethodDelete, "/api/users/t1", `{"current_password":"pw"}`,
		func(h *userhttp.Handler, c *gin.Context) { h.Delete(c, "t1", oapi.UserDeleteParams{}) })
	if code != http.StatusNoContent {
		t.Fatalf("right password status = %d, want 204", code)
	}
	if svc.deletes != 1 || r.calls != 1 {
		t.Fatalf("deletes=%d reauth=%d, want 1/1", svc.deletes, r.calls)
	}
}

// 重置口令：缺口令 400；带口令 200 且 temp_password 只在响应出现。
func TestUserPasswordResetReauthGate(t *testing.T) {
	svc := &fakeUserService{}
	h := userhttp.NewHandler(svc, fakeUserAuthorizer{}).WithReauth(&recordingReauth{})
	code, _, _ := callUserHandler(t, h, http.MethodPost, "/api/users/t1/password-reset", `{}`,
		func(h *userhttp.Handler, c *gin.Context) { h.PasswordReset(c, "t1", oapi.UserPasswordResetParams{}) })
	if code != http.StatusBadRequest {
		t.Fatalf("missing password status = %d, want 400", code)
	}
	if svc.resets != 0 {
		t.Fatal("a missing password must not reach the use case")
	}
	code, body, _ := callUserHandler(t, h, http.MethodPost, "/api/users/t1/password-reset",
		`{"current_password":"pw"}`,
		func(h *userhttp.Handler, c *gin.Context) {
			h.PasswordReset(c, "t1", oapi.UserPasswordResetParams{})
		})
	if code != http.StatusOK {
		t.Fatalf("right password status = %d, want 200", code)
	}
	if svc.resets != 1 {
		t.Fatalf("resets=%d", svc.resets)
	}
	// temp_password 在响应体出现恰好一次（fake 用例固定返回 temp-password-42）。
	if got := strings.Count(body, "temp-password-42"); got != 1 {
		t.Fatalf("temp_password occurrences = %d, want 1; body=%q", got, body)
	}
}

// 新建用户：可用该端点建 master/admin，故缺口令 400、错口令 403、正确 201。
func TestUserCreateReauthGate(t *testing.T) {
	svc := &fakeUserService{}
	r := &recordingReauth{}
	h := userhttp.NewHandler(svc, fakeUserAuthorizer{}).WithReauth(r)
	body := `{"email":"new@example.com","password":"secret","role":"admin"}`

	code, _, _ := callUserHandler(t, h, http.MethodPost, "/api/users", body,
		func(h *userhttp.Handler, c *gin.Context) { h.Create(c, oapi.UserCreateParams{}) })
	if code != http.StatusBadRequest {
		t.Fatalf("missing password status = %d, want 400", code)
	}
	if svc.creates != 0 || r.calls != 0 {
		t.Fatalf("creates=%d reauth=%d, want 0/0", svc.creates, r.calls)
	}

	r.err = errs.New(errs.CodeForbidden, "reauthentication failed")
	code, _, _ = callUserHandler(t, h, http.MethodPost, "/api/users",
		`{"email":"new@example.com","password":"secret","role":"admin","current_password":"wrong"}`,
		func(h *userhttp.Handler, c *gin.Context) { h.Create(c, oapi.UserCreateParams{}) })
	if code != http.StatusForbidden {
		t.Fatalf("wrong password status = %d, want 403", code)
	}
	if svc.creates != 0 {
		t.Fatal("a rejected reauth must not reach the use case")
	}

	r.err = nil
	code, _, _ = callUserHandler(t, h, http.MethodPost, "/api/users",
		`{"email":"new@example.com","password":"secret","role":"admin","current_password":"pw"}`,
		func(h *userhttp.Handler, c *gin.Context) { h.Create(c, oapi.UserCreateParams{}) })
	if code != http.StatusCreated {
		t.Fatalf("right password status = %d, want 201", code)
	}
	if svc.creates != 1 || r.calls != 2 {
		t.Fatalf("creates=%d reauth=%d, want 1/2", svc.creates, r.calls)
	}
}

// 恢复用户：缺口令 400、正确口令 200；失败零调用。
func TestUserRestoreReauthGate(t *testing.T) {
	svc := &fakeUserService{}
	h := userhttp.NewHandler(svc, fakeUserAuthorizer{}).WithReauth(&recordingReauth{})

	code, _, _ := callUserHandler(t, h, http.MethodPost, "/api/users/t1/restore", `{}`,
		func(h *userhttp.Handler, c *gin.Context) { h.Restore(c, "t1", oapi.UserRestoreParams{}) })
	if code != http.StatusBadRequest {
		t.Fatalf("missing password status = %d, want 400", code)
	}
	if svc.restores != 0 {
		t.Fatal("a missing password must not reach the use case")
	}

	code, _, _ = callUserHandler(t, h, http.MethodPost, "/api/users/t1/restore",
		`{"current_password":"pw"}`,
		func(h *userhttp.Handler, c *gin.Context) { h.Restore(c, "t1", oapi.UserRestoreParams{}) })
	if code != http.StatusOK {
		t.Fatalf("right password status = %d, want 200", code)
	}
	if svc.restores != 1 {
		t.Fatalf("restores=%d, want 1", svc.restores)
	}
}

// 节流命中：429 + Retry-After 头（秒，向上取整）。
func TestUserUpdateThrottledWritesRetryAfter(t *testing.T) {
	svc := &fakeUserService{}
	r := &recordingReauth{err: appauth.NewReauthThrottled(90 * time.Second)}
	h := userhttp.NewHandler(svc, fakeUserAuthorizer{}).WithReauth(r)
	code, _, header := callUserHandler(t, h, http.MethodPatch, "/api/users/t1",
		`{"role":"admin","current_password":"pw"}`,
		func(h *userhttp.Handler, c *gin.Context) { h.Update(c, "t1", oapi.UserUpdateParams{}) })
	if code != http.StatusTooManyRequests {
		t.Fatalf("status = %d, want 429", code)
	}
	if got := header.Get("Retry-After"); got != "90" {
		t.Fatalf("Retry-After = %q, want 90", got)
	}
	if svc.updates != 0 {
		t.Fatal("a throttled request must not reach the use case")
	}
}

// 未接线：500 失败关闭。
func TestUserDeleteWithoutWiredReauthFailsClosed(t *testing.T) {
	svc := &fakeUserService{}
	h := userhttp.NewHandler(svc, fakeUserAuthorizer{})
	code, _, _ := callUserHandler(t, h, http.MethodDelete, "/api/users/t1", `{"current_password":"pw"}`,
		func(h *userhttp.Handler, c *gin.Context) { h.Delete(c, "t1", oapi.UserDeleteParams{}) })
	if code != http.StatusInternalServerError {
		t.Fatalf("status = %d, want 500", code)
	}
	if svc.deletes != 0 {
		t.Fatal("unwired reauth must not reach the use case")
	}
}

// 物理清除用户：缺口令 400、错口令 403、正确口令 204；失败零调用。
func TestUserPurgeReauthGate(t *testing.T) {
	svc := &fakeUserService{}
	r := &recordingReauth{}
	h := userhttp.NewHandler(svc, fakeUserAuthorizer{}).WithReauth(r)

	code, _, _ := callUserHandler(t, h, http.MethodPost, "/api/users/t1/purge", `{}`,
		func(h *userhttp.Handler, c *gin.Context) { h.Purge(c, "t1", oapi.UserPurgeParams{}) })
	if code != http.StatusBadRequest {
		t.Fatalf("missing password status = %d, want 400", code)
	}
	if svc.purges != 0 || r.calls != 0 {
		t.Fatalf("purges=%d reauth=%d, want 0/0", svc.purges, r.calls)
	}

	r.err = errs.New(errs.CodeForbidden, "reauthentication failed")
	code, _, _ = callUserHandler(t, h, http.MethodPost, "/api/users/t1/purge",
		`{"current_password":"wrong"}`,
		func(h *userhttp.Handler, c *gin.Context) { h.Purge(c, "t1", oapi.UserPurgeParams{}) })
	if code != http.StatusForbidden {
		t.Fatalf("wrong password status = %d, want 403", code)
	}
	if svc.purges != 0 {
		t.Fatal("a rejected reauth must not reach the use case")
	}

	r.err = nil
	code, _, _ = callUserHandler(t, h, http.MethodPost, "/api/users/t1/purge",
		`{"current_password":"pw"}`,
		func(h *userhttp.Handler, c *gin.Context) { h.Purge(c, "t1", oapi.UserPurgeParams{}) })
	if code != http.StatusNoContent {
		t.Fatalf("right password status = %d, want 204", code)
	}
	if svc.purges != 1 || r.calls != 2 {
		t.Fatalf("purges=%d reauth=%d, want 1/2", svc.purges, r.calls)
	}
}

// 物理清除用户未接线：500 失败关闭，用例不被调用。
func TestUserPurgeWithoutWiredReauthFailsClosed(t *testing.T) {
	svc := &fakeUserService{}
	h := userhttp.NewHandler(svc, fakeUserAuthorizer{})
	code, _, _ := callUserHandler(t, h, http.MethodPost, "/api/users/t1/purge", `{"current_password":"pw"}`,
		func(h *userhttp.Handler, c *gin.Context) { h.Purge(c, "t1", oapi.UserPurgeParams{}) })
	if code != http.StatusInternalServerError {
		t.Fatalf("status = %d, want 500", code)
	}
	if svc.purges != 0 {
		t.Fatal("unwired reauth must not reach the use case")
	}
}
