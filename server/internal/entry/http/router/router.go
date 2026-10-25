// Package router 装配 Gin 引擎、中间件链与生成的契约处理器。
//
// # 功能
// 建路由、串中间件、读会话 cookie。
// 认证端点由 Options.Auth 注入；未注入时保持 501。
// 学校、管理员、账号三组端点委托到域处理器；/api/users 仍是 501 占位。
// 本包**不**连数据库、**不**写业务逻辑。
//
// # 归属
// entry/http 入口层。存储与用例经 Options 注入，路由不自行构造依赖。
//
// # 复用
// New、Options、AuthService；server 实现生成的契约接口。
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
	accounthttp "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/account"
	adminhttp "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/admin"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/oapi"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/respond"
	schoolhttp "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/school"
)

// Options 装配路由。Sessions 必填；其余有默认值。
//
// Schools、Admins、Accounts 是三个业务域的处理器。
// 为 nil 时对应端点回 501：本批只交付委托与 e2e，
// 进程装配（cmd/server）由集成批接线。
type Options struct {
	Sessions auth.SessionIssuer
	// Auth 是认证用例。为 nil 时登录、登出与口令重置回 501。
	// 生产必须有值；cmd/server 在配置了数据库时注入。
	Auth  AuthService
	Clock clock.Clock
	// InsecureCookies 选择无前缀的 cookie 名。
	// 浏览器拒绝不带 Secure 的 __Host- cookie，故仅限本地 HTTP。
	InsecureCookies bool
	Logger          *slog.Logger
	// Schools、Admins、Accounts 是三组业务端点的域处理器。
	// 为 nil 时对应端点回 501，装配代码必须显式决定。
	Schools  *schoolhttp.Handler
	Admins   *adminhttp.Handler
	Accounts *accounthttp.Handler
}

// server 实现生成的契约接口。
// /api/users 仍为占位；其余业务端点委托给域处理器。
type server struct {
	sessions auth.SessionIssuer
	auth     AuthService
	cookies  sessionCookie
	schools  *schoolhttp.Handler
	admins   *adminhttp.Handler
	accounts *accounthttp.Handler
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
	srv := &server{
		sessions: opts.Sessions,
		auth:     opts.Auth,
		cookies:  newSessionCookie(opts.InsecureCookies),
	}
	if srv.auth == nil {
		// 显性可见：没有用例就没有可用登录。
		// 健康检查与 me 仍可服务，其余认证端点回 501。
		logger.Warn("auth endpoints are not wired: login, logout and password reset return 501")
	}
	srv.schools = opts.Schools
	srv.admins = opts.Admins
	srv.accounts = opts.Accounts

	engine := gin.New()
	engine.Use(
		requestID(),
		bodyLimit(maxRequestBodyBytes),
		accessLog(logger, clk),
		recoverPanic(logger),
		securityHeaders(),
		srv.authenticate(),
		srv.requireCSRF(),
	)
	oapi.RegisterHandlersWithOptions(engine, srv, oapi.GinServerOptions{
		ErrorHandler: func(c *gin.Context, err error, _ int) {
			bindError(c, err)
		},
	})
	engine.NoRoute(func(c *gin.Context) {
		respond.Error(c.Writer, c.Request, errs.New(errs.CodeNotFound, "route not found"))
	})
	return engine, nil
}

// HealthCheck 报告进程正在服务请求。
func (s *server) HealthCheck(c *gin.Context) {
	respond.JSON(c.Writer, http.StatusOK, oapi.HealthStatus{Status: oapi.HealthStatusStatus("ok")})
}
