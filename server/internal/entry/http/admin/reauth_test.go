package admin_test

// 学校管理员绑定/解绑的二次口令入口层判据。
//
// # 功能
// 钉住两个端点的门形：绑定与解绑总是要求 current_password。
// 缺失 400、错误 403、正确进用例、未接线 500。
// 本文件**不**连数据库，**不**做真实口令校验。

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/gin-gonic/gin"

	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	appadmin "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/schooladmin"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
	adminhttp "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/admin"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/oapi"
)

// fakeAdminService 记录绑定与解绑调用并返回固定形状。
type fakeAdminService struct {
	grants  int
	revokes int
}

func (f *fakeAdminService) List(context.Context, corescope.Scope, string, string, int) (appadmin.Page, error) {
	return appadmin.Page{Items: []appadmin.Binding{}}, nil
}

func (f *fakeAdminService) Grant(context.Context, corescope.Scope, string, string, appaudit.Actor) (appadmin.Binding, error) {
	f.grants++
	return appadmin.Binding{UserID: "u2", Email: "u2@example.com", Role: coreauth.RoleAdmin}, nil
}

func (f *fakeAdminService) Revoke(context.Context, corescope.Scope, string, string, appaudit.Actor) error {
	f.revokes++
	return nil
}

// fakeAdminAuthorizer 返回 master 作用域与会话主体。
type fakeAdminAuthorizer struct{}

func (fakeAdminAuthorizer) Authorize(context.Context, coreauth.User) (corescope.Scope, appaudit.Actor, error) {
	return corescope.Master(), appaudit.Actor{UserID: "u1", Role: coreauth.RoleMaster}, nil
}

// recordingAdminReauth 记录收到的口令，按预设复述错误。
type recordingAdminReauth struct {
	calls int
	pw    string
	err   error
}

func (r *recordingAdminReauth) Reauthenticate(_ context.Context, _ string, password string) error {
	r.calls++
	r.pw = password
	return r.err
}

// callAdminHandler 在测试上下文里调 handler 并返回状态码。
func callAdminHandler(t *testing.T, h *adminhttp.Handler, method, path, body string,
	invoke func(*adminhttp.Handler, *gin.Context)) int {
	t.Helper()
	gin.SetMode(gin.TestMode)
	rec := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(rec)
	req := httptest.NewRequest(method, path, strings.NewReader(body))
	req = req.WithContext(coreauth.WithSession(req.Context(), coreauth.Session{
		User: coreauth.User{ID: "u1", Role: coreauth.RoleMaster, Status: coreauth.StatusActive},
	}))
	c.Request = req
	invoke(h, c)
	return c.Writer.Status()
}

// 绑定：缺口令 400、错口令 403、正确 201；失败零调用。
func TestSchoolAdminAddReauthGate(t *testing.T) {
	svc := &fakeAdminService{}
	r := &recordingAdminReauth{}
	h := adminhttp.NewHandler(svc, fakeAdminAuthorizer{}).WithReauth(r)

	code := callAdminHandler(t, h, http.MethodPost, "/api/schools/s1/admins", `{"user_id":"u2"}`,
		func(h *adminhttp.Handler, c *gin.Context) { h.Add(c, "s1", oapi.SchoolAdminAddParams{}) })
	if code != http.StatusBadRequest {
		t.Fatalf("missing password status = %d, want 400", code)
	}
	if svc.grants != 0 || r.calls != 0 {
		t.Fatalf("grants=%d reauth=%d, want 0/0", svc.grants, r.calls)
	}

	r.err = errs.New(errs.CodeForbidden, "reauthentication failed")
	code = callAdminHandler(t, h, http.MethodPost, "/api/schools/s1/admins",
		`{"user_id":"u2","current_password":"wrong"}`,
		func(h *adminhttp.Handler, c *gin.Context) { h.Add(c, "s1", oapi.SchoolAdminAddParams{}) })
	if code != http.StatusForbidden {
		t.Fatalf("wrong password status = %d, want 403", code)
	}
	if svc.grants != 0 {
		t.Fatal("a rejected reauth must not reach the use case")
	}

	r.err = nil
	code = callAdminHandler(t, h, http.MethodPost, "/api/schools/s1/admins",
		`{"user_id":"u2","current_password":"pw"}`,
		func(h *adminhttp.Handler, c *gin.Context) { h.Add(c, "s1", oapi.SchoolAdminAddParams{}) })
	if code != http.StatusCreated {
		t.Fatalf("right password status = %d, want 201", code)
	}
	if svc.grants != 1 || r.calls != 2 || r.pw != "pw" {
		t.Fatalf("grants=%d reauth=%d pw=%q, want 1/2/pw", svc.grants, r.calls, r.pw)
	}
}

// 解绑：缺口令 400，用例不被调用；带口令 204。
func TestSchoolAdminRemoveReauthGate(t *testing.T) {
	svc := &fakeAdminService{}
	h := adminhttp.NewHandler(svc, fakeAdminAuthorizer{}).WithReauth(&recordingAdminReauth{})

	code := callAdminHandler(t, h, http.MethodDelete, "/api/schools/s1/admins/u2", `{}`,
		func(h *adminhttp.Handler, c *gin.Context) {
			h.Remove(c, "s1", "u2", oapi.SchoolAdminRemoveParams{})
		})
	if code != http.StatusBadRequest {
		t.Fatalf("missing password status = %d, want 400", code)
	}
	if svc.revokes != 0 {
		t.Fatal("a missing password must not reach the use case")
	}

	code = callAdminHandler(t, h, http.MethodDelete, "/api/schools/s1/admins/u2",
		`{"current_password":"pw"}`,
		func(h *adminhttp.Handler, c *gin.Context) {
			h.Remove(c, "s1", "u2", oapi.SchoolAdminRemoveParams{})
		})
	if code != http.StatusNoContent {
		t.Fatalf("right password status = %d, want 204", code)
	}
	if svc.revokes != 1 {
		t.Fatalf("revokes=%d, want 1", svc.revokes)
	}
}

// 未接线：500 失败关闭，用例不被调用。
func TestSchoolAdminAddWithoutWiredReauthFailsClosed(t *testing.T) {
	svc := &fakeAdminService{}
	h := adminhttp.NewHandler(svc, fakeAdminAuthorizer{})
	code := callAdminHandler(t, h, http.MethodPost, "/api/schools/s1/admins",
		`{"user_id":"u2","current_password":"pw"}`,
		func(h *adminhttp.Handler, c *gin.Context) { h.Add(c, "s1", oapi.SchoolAdminAddParams{}) })
	if code != http.StatusInternalServerError {
		t.Fatalf("status = %d, want 500", code)
	}
	if svc.grants != 0 {
		t.Fatal("unwired reauth must not reach the use case")
	}
}
