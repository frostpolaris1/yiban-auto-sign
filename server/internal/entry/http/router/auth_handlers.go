package router

// 认证端点的处理器。
//
// # 功能
// 登录、登出、会话主体、忘记口令、重置口令与登录态改密的 HTTP 编码。
// 本文件**不**校验口令，**不**写 SQL；用例在 app/auth。
//
// # 归属
// entry/http。中间件已解析会话并执行 CSRF 门禁；
// 处理器只做绑定、调用用例、写 cookie 与响应。
//
// # 口径
// 登录与重置的请求体绑定失败回 400 validation_failed。
// 登录失败回 401，文案与错误码由用例统一给出。
// 登出、忘记口令、重置口令与改密回 204，响应体为空。
// 改密**不**改写当前会话的 cookie：只撤销其余会话。

import (
	"context"
	"encoding/hex"
	"net/http"
	"net/url"
	"strings"

	"github.com/gin-gonic/gin"

	appauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/auth"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	coreuser "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/user"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/oapi"
	ratelimit "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/ratelimit"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/respond"
)

// AuthService 是入口需要的认证用例集合。
// 实现在 app/auth.Service；测试可注入替身。
type AuthService interface {
	Login(ctx context.Context, email, password string) (coreauth.Issued, error)
	Logout(ctx context.Context, sess coreauth.Session, token string) error
	ForgotPassword(ctx context.Context, email string) error
	ResetPassword(ctx context.Context, token, newPassword string) error
	ChangePassword(ctx context.Context, sess coreauth.Session, keepToken, currentPassword, newPassword string) error
}

// AuthLogin 校验口令并写入会话 cookie。
func (s *server) AuthLogin(c *gin.Context) {
	if s.auth == nil {
		notImplemented(c)
		return
	}
	// 登录免 CSRF；跨站表单顶号必须在这里挡住。
	// 浏览器给的 Origin 与 Sec-Fetch-Site 是唯一可信的跨站信号。
	if !s.loginOriginAllowed(c) {
		respond.Error(c.Writer, c.Request, errs.New(errs.CodeForbidden, "cross-site login is not allowed"))
		return
	}
	var req oapi.AuthLoginJSONRequestBody
	if err := c.ShouldBindJSON(&req); err != nil {
		bindError(c, err)
		return
	}
	// 账号面在付出 KDF 之前判：归一邮箱与用例同口径，大小写变体共用一个桶。
	if !s.accountLimit(c, coreuser.NormalizeEmail(string(req.Email))) {
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
	// 账号键取令牌摘要：原始令牌不进内存索引。
	if !s.accountLimit(c, hex.EncodeToString(coreauth.HashToken(req.Token).Bytes())) {
		return
	}
	if err := s.auth.ResetPassword(c.Request.Context(), req.Token, req.NewPassword); err != nil {
		respond.Error(c.Writer, c.Request, err)
		return
	}
	c.Status(http.StatusNoContent)
}

// AuthChangePassword 校验旧口令并替换当前用户的口令。
// 会话与 CSRF 已由中间件校验。
// 成功回 204，**保留**当前会话的 cookie。
func (s *server) AuthChangePassword(c *gin.Context, _ oapi.AuthChangePasswordParams) {
	if s.auth == nil {
		notImplemented(c)
		return
	}
	var req oapi.AuthChangePasswordJSONRequestBody
	if err := c.ShouldBindJSON(&req); err != nil {
		bindError(c, err)
		return
	}
	sess, ok := coreauth.FromContext(c.Request.Context())
	if !ok {
		// 中间件已拒绝无会话请求；走到这里是接线错误。
		respond.Error(c.Writer, c.Request, errs.New(errs.CodeUnauthorized, "authentication required"))
		return
	}
	// 账号面按会话 userID 判：改密要付两次 KDF，先挡在门外。
	if !s.accountLimit(c, sess.User.ID) {
		return
	}
	token, err := c.Cookie(s.cookies.name)
	if err != nil {
		// 已有会话却读不到令牌，说明中间件与处理器口径不一致。
		respond.Error(c.Writer, c.Request, errs.New(errs.CodeInternal, "session cookie is missing"))
		return
	}
	if err := s.auth.ChangePassword(c.Request.Context(), sess, token,
		req.CurrentPassword, req.NewPassword); err != nil {
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

// loginOriginAllowed 判定登录请求的页面来源。
//
// 规则：
//   - Sec-Fetch-Site 是 cross-site/same-site：拒绝；
//   - 是 same-origin/none：放行（浏览器自证；反向代理可改写 Host）；
//   - 头缺失且无 Origin：放行（非浏览器客户端）；
//   - 头缺失且有 Origin：必须等于允许基址或请求自身的 scheme://Host；
//   - Origin 为字面量 null 或解析失败：拒绝。
//
// 允许基址来自 YIBAN_PUBLIC_BASE_URL；为空时只做同源判定。
// scheme 在可信代理后取 X-Forwarded-Proto，TLS 在容器外终结时仍判对。
func (s *server) loginOriginAllowed(c *gin.Context) bool {
	switch c.GetHeader("Sec-Fetch-Site") {
	case "cross-site", "same-site":
		return false
	case "same-origin", "none":
		return true
	}
	origin := strings.TrimSpace(c.GetHeader("Origin"))
	if origin == "" {
		return true
	}
	if origin == "null" {
		return false
	}
	if s.loginOrigin != "" && sameOrigin(origin, s.loginOrigin) {
		return true
	}
	return sameOrigin(origin, s.requestScheme(c)+"://"+c.Request.Host)
}

// requestScheme 返回浏览器感知的请求 scheme。
// 只有可信对端的 X-Forwarded-Proto 才采信；否则按连接是否 TLS。
func (s *server) requestScheme(c *gin.Context) string {
	if c.Request.TLS != nil {
		return "https"
	}
	if s.rateLimit != nil &&
		ratelimit.IsTrustedPeer(c.Request.RemoteAddr, s.rateLimit.Trusted) {
		raw := c.GetHeader("X-Forwarded-Proto")
		if i := strings.IndexByte(raw, ','); i >= 0 {
			raw = raw[:i]
		}
		switch scheme := strings.ToLower(strings.TrimSpace(raw)); scheme {
		case "http", "https":
			return scheme
		}
	}
	return "http"
}

// sameOrigin 比较两个 URL 的 scheme 与 host（host 含端口）。
// 解析失败或任一侧无 host 都判不同源。
func sameOrigin(a, b string) bool {
	ua, err := url.Parse(a)
	if err != nil {
		return false
	}
	ub, err := url.Parse(b)
	if err != nil {
		return false
	}
	if ua.Host == "" || ub.Host == "" {
		return false
	}
	return strings.EqualFold(ua.Scheme, ub.Scheme) && strings.EqualFold(ua.Host, ub.Host)
}
