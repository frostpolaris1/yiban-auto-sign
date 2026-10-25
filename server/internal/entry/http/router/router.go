// Package router 装配 Gin 引擎、中间件链与生成的契约处理器。
//
// # 功能
// 建路由、串中间件、读会话 cookie、回 501 占位响应。
// 本包**不**连数据库、**不**写业务逻辑。
// 本阶段业务端点全部 501；只有 /healthz 与 /api/auth/me 是真的。
//
// # 归属
// entry/http 入口层。存储经 Options 注入，路由不自行构造依赖。
//
// # 复用
// New、Options；server 实现生成的契约接口。
//
// # 口径
// 中间件顺序固定：requestID → accessLog → recoverPanic →
// securityHeaders → authenticate → requireCSRF。
// 不安全 cookie 名只用于本地 HTTP。
package router

import (
	"errors"
	"log/slog"
	"net/http"

	"github.com/gin-gonic/gin"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/oapi"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/respond"
)

// Options 装配路由。Sessions 必填；其余有默认值。
type Options struct {
	Sessions auth.SessionIssuer
	Clock    clock.Clock
	Logger   *slog.Logger
	// InsecureCookies 选择无前缀的 cookie 名。
	// 浏览器拒绝不带 Secure 的 __Host- cookie，故仅限本地 HTTP。
	InsecureCookies bool
}

// server 实现生成的契约接口。
// 本阶段业务端点全部回 501；只有 /healthz 与 /api/auth/me 是真的。
type server struct {
	sessions   auth.SessionIssuer
	cookieName string
}

// New 构造 HTTP 引擎。缺 SessionIssuer 时**失败关闭**。
func New(opts Options) (*gin.Engine, error) {
	if opts.Sessions == nil {
		return nil, errors.New("router: nil SessionIssuer")
	}
	clk := opts.Clock
	if clk == nil {
		clk = clock.System{}
	}
	logger := opts.Logger
	if logger == nil {
		logger = slog.Default()
	}
	cookieName := auth.CookieName
	if opts.InsecureCookies {
		cookieName = auth.InsecureCookieName
	}
	srv := &server{sessions: opts.Sessions, cookieName: cookieName}

	engine := gin.New()
	engine.Use(
		requestID(),
		accessLog(logger, clk),
		recoverPanic(logger),
		securityHeaders(),
		srv.authenticate(),
		srv.requireCSRF(),
	)
	oapi.RegisterHandlersWithOptions(engine, srv, oapi.GinServerOptions{
		ErrorHandler: func(c *gin.Context, _ error, _ int) {
			respond.Error(c.Writer, c.Request, errs.New(errs.CodeValidation, "invalid request"))
		},
	})
	engine.NoRoute(func(c *gin.Context) {
		respond.Error(c.Writer, c.Request, errs.New(errs.CodeNotFound, "route not found"))
	})
	return engine, nil
}

// AuthMe 返回会话主体。
// **响应不回传会话令牌**，只回 CSRF 令牌。
func (s *server) AuthMe(c *gin.Context) {
	sess, ok := auth.FromContext(c.Request.Context())
	if !ok {
		respond.Error(c.Writer, c.Request, errs.New(errs.CodeUnauthorized, "authentication required"))
		return
	}
	respond.JSON(c.Writer, http.StatusOK, oapi.SessionUser{
		Id:                 sess.User.ID,
		Email:              openapiEmail(sess.User.Email),
		Role:               oapi.UserRole(sess.User.Role),
		Status:             oapi.UserStatus(sess.User.Status),
		MustChangePassword: sess.User.MustChangePassword,
		CsrfToken:          sess.CSRFToken,
	})
}

// HealthCheck 报告进程正在服务请求。
func (s *server) HealthCheck(c *gin.Context) {
	respond.JSON(c.Writer, http.StatusOK, oapi.HealthStatus{Status: oapi.HealthStatusStatus("ok")})
}
