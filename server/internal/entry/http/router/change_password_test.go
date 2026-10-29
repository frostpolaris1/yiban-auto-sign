package router_test

// 改密处理器的单元测试。
//
// # 功能
// 钉住三条处理器契约：请求参数原样交给用例、204 无响应体、
// 用例错误按稳定码映射。会话与 CSRF 门禁由 e2e 覆盖。
// 本文件**不**连数据库，**不**校验口令。

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/gin-gonic/gin"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
)

// changePasswordStub 记录处理器转交给用例的参数并复述预设错误。
type changePasswordStub struct {
	stubAuth
	calls   int
	userID  string
	token   string
	current string
	next    string
	err     error
}

func (s *changePasswordStub) ChangePassword(_ context.Context, sess auth.Session, token, current, next string) error {
	s.calls++
	s.userID = sess.User.ID
	s.token = token
	s.current = current
	s.next = next
	return s.err
}

// newChangePasswordEngine 构造注入了改密替身的引擎与一个有效会话。
func newChangePasswordEngine(t *testing.T, svc *changePasswordStub) (*gin.Engine, auth.Issued) {
	t.Helper()
	issuer := newStubIssuer()
	engine := newAuthEngine(t, issuer, svc)
	issued, err := issuer.Issue(context.Background(), auth.User{
		ID: "u1", Email: "u1@example.com", Role: auth.RoleUser, Status: auth.StatusActive,
	})
	if err != nil {
		t.Fatalf("issue session: %v", err)
	}
	return engine, issued
}

// changePasswordRequest 构造一个带会话与 CSRF 的改密请求。
func changePasswordRequest(t *testing.T, issued auth.Issued, body string) *http.Request {
	t.Helper()
	req := httptest.NewRequest(http.MethodPost, "/api/auth/change-password", strings.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-CSRF-Token", issued.CSRFToken)
	req.AddCookie(&http.Cookie{Name: auth.CookieName, Value: issued.Token})
	return req
}

// 成功路径：204、无响应体、参数原样到达用例。
func TestChangePasswordHandlerForwardsRequestAndAnswers204(t *testing.T) {
	svc := &changePasswordStub{}
	engine, issued := newChangePasswordEngine(t, svc)

	rec := serve(engine, changePasswordRequest(t, issued, `{"current_password":"old-pass","new_password":"new-pass"}`))
	if rec.Code != http.StatusNoContent {
		t.Fatalf("status = %d, want 204; body=%q", rec.Code, rec.Body.String())
	}
	if rec.Body.Len() != 0 {
		t.Fatalf("204 carried a body: %q", rec.Body.String())
	}
	if svc.calls != 1 {
		t.Fatalf("use case calls = %d, want 1", svc.calls)
	}
	if svc.userID != "u1" || svc.token != issued.Token ||
		svc.current != "old-pass" || svc.next != "new-pass" {
		t.Fatalf("use case got user=%q token=%q current=%q new=%q",
			svc.userID, svc.token, svc.current, svc.next)
	}
}

// 用例返回 409：处理器原样映射为 conflict 信封。
func TestChangePasswordHandlerMapsUseCaseError(t *testing.T) {
	svc := &changePasswordStub{err: errs.New(errs.CodeConflict, "new password must differ from the current password")}
	engine, issued := newChangePasswordEngine(t, svc)

	rec := serve(engine, changePasswordRequest(t, issued, `{"current_password":"old-pass","new_password":"old-pass"}`))
	if rec.Code != http.StatusConflict {
		t.Fatalf("status = %d, want 409; body=%q", rec.Code, rec.Body.String())
	}
	if env := decodeEnvelope(t, rec); env.Error.Code != "conflict" {
		t.Fatalf("code = %q, want conflict", env.Error.Code)
	}
}

// 畸形 JSON：400，用例不被调用。
func TestChangePasswordHandlerRejectsMalformedBody(t *testing.T) {
	svc := &changePasswordStub{}
	engine, issued := newChangePasswordEngine(t, svc)

	rec := serve(engine, changePasswordRequest(t, issued, `{"current_password":`))
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400; body=%q", rec.Code, rec.Body.String())
	}
	if env := decodeEnvelope(t, rec); env.Error.Code != "validation_failed" {
		t.Fatalf("code = %q, want validation_failed", env.Error.Code)
	}
	if svc.calls != 0 {
		t.Fatalf("use case calls = %d after a binding failure, want 0", svc.calls)
	}
}
