package router

// 认证端点的处理器。
//
// # 功能
// 登录、登出、会话主体、忘记口令与重置口令的 HTTP 编码。
// 本文件**不**校验口令，**不**写 SQL；用例在 app/auth。
//
// # 归属
// entry/http。中间件已解析会话并执行 CSRF 门禁；
// 处理器只做绑定、调用用例、写 cookie 与响应。
//
// # 口径
// 登录与重置的请求体绑定失败回 400 validation_failed。
// 登录失败回 401，文案与错误码由用例统一给出。
// 登出与忘记口令回 204，响应体为空。

import (
	"context"
	"net/http"

	"github.com/gin-gonic/gin"

	appauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/auth"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/oapi"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/respond"
)

// AuthService 是入口需要的认证用例集合。
// 实现在 app/auth.Service；测试可注入替身。
type AuthService interface {
	Login(ctx context.Context, email, password string) (coreauth.Issued, error)
	Logout(ctx context.Context, sess coreauth.Session, token string) error
	ForgotPassword(ctx context.Context, email string) error
	ResetPassword(ctx context.Context, token, newPassword string) error
}

// AuthLogin 校验口令并写入会话 cookie。
func (s *server) AuthLogin(c *gin.Context) {
	if s.auth == nil {
		notImplemented(c)
		return
	}
	var req oapi.AuthLoginJSONRequestBody
	if err := c.ShouldBindJSON(&req); err != nil {
		bindError(c, err)
		return
	}
	issued, err := s.auth.Login(c.Request.Context(), string(req.Email), req.Password)
	if err != nil {
		respond.Error(c.Writer, c.Request, err)
		return
	}
	s.cookies.set(c.Writer, issued.Token)
	respond.JSON(c.Writer, http.StatusOK, sessionUserOf(issued.Session))
}

// AuthLogout 撤销当前会话并清除 cookie。
// 无会话时仍回 204：契约把登出定义为幂等。
// 带会话但不带 CSRF 的请求已在中间件被拒。
func (s *server) AuthLogout(c *gin.Context, _ oapi.AuthLogoutParams) {
	sess, ok := coreauth.FromContext(c.Request.Context())
	if !ok {
		s.cookies.clear(c.Writer)
		c.Status(http.StatusNoContent)
		return
	}
	if s.auth == nil {
		notImplemented(c)
		return
	}
	token, err := c.Cookie(s.cookies.name)
	if err != nil {
		// 已有会话却读不到令牌，说明中间件与处理器口径不一致。
		// 不在这里「当作已登出」：那会留下未撤销的会话。
		respond.Error(c.Writer, c.Request, errs.New(errs.CodeInternal, "session cookie is missing"))
		return
	}
	if err := s.auth.Logout(c.Request.Context(), sess, token); err != nil {
		respond.Error(c.Writer, c.Request, err)
		return
	}
	s.cookies.clear(c.Writer)
	c.Status(http.StatusNoContent)
}

// AuthMe 返回当前用户与 CSRF 令牌。
func (s *server) AuthMe(c *gin.Context) {
	sess, err := appauth.Me(c.Request.Context())
	if err != nil {
		respond.Error(c.Writer, c.Request, err)
		return
	}
	respond.JSON(c.Writer, http.StatusOK, sessionUserOf(sess))
}

// AuthForgotPassword 创建一次性重置令牌。
// **始终回 204**，不区分邮箱是否存在。
func (s *server) AuthForgotPassword(c *gin.Context) {
	if s.auth == nil {
		notImplemented(c)
		return
	}
	var req oapi.AuthForgotPasswordJSONRequestBody
	if err := c.ShouldBindJSON(&req); err != nil {
		bindError(c, err)
		return
	}
	if err := s.auth.ForgotPassword(c.Request.Context(), string(req.Email)); err != nil {
		respond.Error(c.Writer, c.Request, err)
		return
	}
	c.Status(http.StatusNoContent)
}

// AuthResetPassword 用一次性令牌替换口令。
func (s *server) AuthResetPassword(c *gin.Context) {
	if s.auth == nil {
		notImplemented(c)
		return
	}
	var req oapi.AuthResetPasswordJSONRequestBody
	if err := c.ShouldBindJSON(&req); err != nil {
		bindError(c, err)
		return
	}
	if err := s.auth.ResetPassword(c.Request.Context(), req.Token, req.NewPassword); err != nil {
		respond.Error(c.Writer, c.Request, err)
		return
	}
	c.Status(http.StatusNoContent)
}

// sessionUserOf 把会话主体转成契约类型。
// **不输出会话令牌**，只输出 CSRF 令牌。
func sessionUserOf(sess coreauth.Session) oapi.SessionUser {
	return oapi.SessionUser{
		Id:                 sess.User.ID,
		Email:              openapiEmail(sess.User.Email),
		Role:               oapi.UserRole(sess.User.Role),
		Status:             oapi.UserStatus(sess.User.Status),
		MustChangePassword: sess.User.MustChangePassword,
		CsrfToken:          sess.CSRFToken,
	}
}
