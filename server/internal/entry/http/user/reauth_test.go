package user_test

// 危险操作二次口令的入口层判据（用户域）。
//
// # 功能
// 钉住三个端点的门形：改角色/状态、删用户与重置口令总是要求
// current_password。缺失 400、错误 403、正确进用例、未接线 500。
// 本文件**不**连数据库，**不**做真实口令校验。

import (
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/gin-gonic/gin"

	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	appuser "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/user"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/oapi"
	userhttp "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/user"
)

// fakeUserService 记录调用并返回固定形状。
type fakeUserService struct {
	updates  int
	deletes  int
	resets   int
	updateIn appuser.UpdateInput
}

func (f *fakeUserService) Create(context.Context, corescope.Scope, appuser.CreateInput, appaudit.Actor) (appuser.Record, error) {
	return appuser.Record{}, nil
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

func (f *fakeUserService) Restore(context.Context, corescope.Scope, string, appaudit.Actor) (appuser.Record, error) {
	return appuser.Record{}, nil
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

// callUserHandler 在测试上下文里调 handler 并返回 gin 状态码。
func callUserHandler(t *testing.T, h *userhttp.Handler, method, path, body string,
	invoke func(*userhttp.Handler, *gin.Context)) int {
	t.Helper()
	gin.SetMode(gin.TestMode)
	c, _ := gin.CreateTestContext(httptest.NewRecorder())
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
	return c.Writer.Status()
}

// 缺口令：400，用例与 reauth 都不被调用。
func TestUserUpdateRequiresCurrentPassword(t *testing.T) {
	svc := &fakeUserService{}
	r := &recordingReauth{}
	h := userhttp.NewHandler(svc, fakeUserAuthorizer{}).WithReauth(r)
	code := callUserHandler(t, h, http.MethodPatch, "/api/users/t1", `{"role":"admin"}`,
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
	code := callUserHandler(t, h, http.MethodPatch, "/api/users/t1",
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
	code := callUserHandler(t, h, http.MethodPatch, "/api/users/t1",
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
	code := callUserHandler(t, h, http.MethodDelete, "/api/users/t1", `{}`,
		func(h *userhttp.Handler, c *gin.Context) { h.Delete(c, "t1", oapi.UserDeleteParams{}) })
	if code != http.StatusBadRequest {
		t.Fatalf("missing password status = %d, want 400", code)
	}
	if svc.deletes != 0 {
		t.Fatal("a missing password must not reach the use case")
	}
	code = callUserHandler(t, h, http.MethodDelete, "/api/users/t1", `{"current_password":"pw"}`,
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
	code := callUserHandler(t, h, http.MethodPost, "/api/users/t1/password-reset", `{}`,
		func(h *userhttp.Handler, c *gin.Context) { h.PasswordReset(c, "t1", oapi.UserPasswordResetParams{}) })
	if code != http.StatusBadRequest {
		t.Fatalf("missing password status = %d, want 400", code)
	}
	if svc.resets != 0 {
		t.Fatal("a missing password must not reach the use case")
	}
	var body string
	code = callUserHandler(t, h, http.MethodPost, "/api/users/t1/password-reset",
		`{"current_password":"pw"}`,
		func(h *userhttp.Handler, c *gin.Context) {
			h.PasswordReset(c, "t1", oapi.UserPasswordResetParams{})
		})
	if code != http.StatusOK {
		t.Fatalf("right password status = %d, want 200", code)
	}
	if svc.resets != 1 || body != "" {
		t.Fatalf("resets=%d", svc.resets)
	}
}

// 未接线：500 失败关闭。
func TestUserDeleteWithoutWiredReauthFailsClosed(t *testing.T) {
	svc := &fakeUserService{}
	h := userhttp.NewHandler(svc, fakeUserAuthorizer{})
	code := callUserHandler(t, h, http.MethodDelete, "/api/users/t1", `{"current_password":"pw"}`,
		func(h *userhttp.Handler, c *gin.Context) { h.Delete(c, "t1", oapi.UserDeleteParams{}) })
	if code != http.StatusInternalServerError {
		t.Fatalf("status = %d, want 500", code)
	}
	if svc.deletes != 0 {
		t.Fatal("unwired reauth must not reach the use case")
	}
}
