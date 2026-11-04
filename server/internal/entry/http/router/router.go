// Package router 装配 Gin 引擎、中间件链与生成的契约处理器。
//
// # 功能
// 建路由、串中间件、读会话 cookie。
// 认证端点由 Options.Auth 注入；未注入时保持 501。
// 学校、管理员、账号、用户四组端点委托到域处理器。
// 本包**不**连数据库、**不**写业务逻辑。
//
// # 归属
// entry/http 入口层。存储与用例经 Options 注入，路由不自行构造依赖。
//
// # 复用
// New、Options、AuthService；server 实现生成的契约接口。
//
// # 口径
// 中间件顺序固定：requestID → mergeListHeaders → bodyLimit → accessLog →
// recoverPanic → securityHeaders → authenticate → requireCSRF。
// 不安全 cookie 名只用于本地 HTTP。
package router

import (
	"context"
	"errors"
	"log/slog"
	"net/http"

	"github.com/gin-gonic/gin"

	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
	accounthttp "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/account"
	adminhttp "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/admin"
	audithttp "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/audit"
	eventshttp "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/events"
	locationhttp "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/location"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/oapi"
	photoshttp "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/photos"
	ratelimit "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/ratelimit"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/respond"
	schedulehttp "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/schedule"
	schoolhttp "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/school"
	settingshttp "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/settings"
	signinghttp "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/signing"
	userhttp "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/user"
)

// Options 装配路由。Sessions 必填；其余有默认值。
//
// Schools、Admins、Accounts、Users 是四组业务端点的域处理器。
// 生产由 cmd/server 装配；为 nil 时对应端点回 501，
// 该状态只出现在测试或降级进程里。
type Options struct {
	Sessions auth.SessionIssuer
	// Auth 是认证用例。为 nil 时登录、登出与口令重置回 501。
	// 生产必须有值；cmd/server 在配置了数据库时注入。
	Auth AuthService
	// Authorizer 把会话用户解析为作用域与审计主体。
	// 目前只服务 master 专属的注册开关；为 nil 时该端点回 501。
	Authorizer Authorizer
	Clock      clock.Clock
	// InsecureCookies 选择无前缀的 cookie 名。
	// 浏览器拒绝不带 Secure 的 __Host- cookie，故仅限本地 HTTP。
	InsecureCookies bool
	Logger          *slog.Logger
	// Schools、Admins、Accounts、Users 是四组业务端点的域处理器。
	// 为 nil 时对应端点回 501，装配代码必须显式决定。
	Schools  *schoolhttp.Handler
	Admins   *adminhttp.Handler
	Accounts *accounthttp.Handler
	Users    *userhttp.Handler
	// Signing 校验账号凭据。为 nil 时校验端点回 501。
	Signing *signinghttp.Handler
	// Schedule 提供计划、容量与自选时间片端点。
	// 为 nil 时这些端点回 501。
	Schedule *schedulehttp.Handler
	// Tasks 提供运行页任务检索与 CSV 导出端点。
	// 为 nil 时这些端点回 501。
	Tasks *schedulehttp.TasksHandler
	// Engine 提供执行监控快照端点。为 nil 时该端点回 501。
	Engine *schedulehttp.EngineHandler
	// Photos 提供图片库端点。为 nil 时这些端点回 501。
	Photos *photoshttp.Handler
	// Locations 提供账号定位策略端点。为 nil 时这些端点回 501。
	Locations *locationhttp.Handler
	// Events 提供管理员事件流与诊断轮询端点。为 nil 时这些端点回 501。
	Events *eventshttp.Handler
	// Settings 提供通知订阅、设置名册与公告端点。为 nil 时这些端点回 501。
	Settings *settingshttp.Handler
	// Audit 提供审计只读列表端点。为 nil 时该端点回 501。
	Audit *audithttp.Handler
	// RateLimit 限制入站速率。nil 表示不限流：只用于测试与降级进程。
	// 生产装配必须给值，且必须带可信代理表。
	RateLimit *ratelimit.Guard
	// LoginOrigin 是登录端点接受的页面来源（scheme://host[:port]）。
	// 空串表示只做同源判定；跨站 Origin 一律 403。
	LoginOrigin string
	// TrustedProxies 是可信反向代理网段。
	// 只有它们的 X-Forwarded-Proto 才用于判定登录 scheme。
	// 与限流开关解耦：关限流时登录来源仍能判对。
	TrustedProxies []ratelimit.Prefix
	// UIDir 是前端构建产物目录（含 index.html）。
	// 空表示只提供 API；非空时未命中的路径交给页面路由。
	// 目录无效或缺 index.html 时 New 失败：启动期暴露，不带病上线。
	UIDir string
}

// Authorizer 把会话用户解析为作用域与审计主体。
// 实现是 app/admin.Service；入口层只依赖这个窄接口。
type Authorizer interface {
	Authorize(ctx context.Context, user auth.User) (corescope.Scope, appaudit.Actor, error)
}

// server 实现生成的契约接口。
// 全部业务端点委托给域处理器。
type server struct {
	sessions       auth.SessionIssuer
	auth           AuthService
	authorizer     Authorizer
	cookies        sessionCookie
	schools        *schoolhttp.Handler
	admins         *adminhttp.Handler
	accounts       *accounthttp.Handler
	users          *userhttp.Handler
	signing        *signinghttp.Handler
	schedule       *schedulehttp.Handler
	tasks          *schedulehttp.TasksHandler
	engine         *schedulehttp.EngineHandler
	photos         *photoshttp.Handler
	locations      *locationhttp.Handler
	events         *eventshttp.Handler
	settings       *settingshttp.Handler
	audit          *audithttp.Handler
	rateLimit      *ratelimit.Guard
	loginOrigin    string
	trustedProxies []ratelimit.Prefix
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
		sessions:   opts.Sessions,
		auth:       opts.Auth,
		authorizer: opts.Authorizer,
		cookies:    newSessionCookie(opts.InsecureCookies),
	}
	if srv.auth == nil {
		// 显性可见：没有用例就没有可用登录。
		// 健康检查与 me 仍可服务，其余认证端点回 501。
		logger.Warn("auth endpoints are not wired: login, logout, password reset and password change return 501")
	}
	srv.schools = opts.Schools
	srv.admins = opts.Admins
	srv.accounts = opts.Accounts
	srv.users = opts.Users
	srv.signing = opts.Signing
	srv.schedule = opts.Schedule
	srv.tasks = opts.Tasks
	srv.engine = opts.Engine
	srv.photos = opts.Photos
	srv.locations = opts.Locations
	srv.events = opts.Events
	srv.settings = opts.Settings
	srv.audit = opts.Audit
	srv.rateLimit = opts.RateLimit
	srv.loginOrigin = opts.LoginOrigin
	srv.trustedProxies = opts.TrustedProxies

	engine := gin.New()
	engine.Use(
		requestID(),
		mergeListHeaders(),
		bodyLimit(requestBodyLimit),
		accessLog(logger, clk),
		recoverPanic(logger),
		securityHeaders(),
		// 限流在解析会话之前：未认证洪水不得先付一次会话查询。
		srv.rateLimitInbound(),
		srv.authenticate(),
		srv.requireCSRF(),
	)
	oapi.RegisterHandlersWithOptions(engine, srv, oapi.GinServerOptions{
		ErrorHandler: func(c *gin.Context, err error, _ int) {
			bindError(c, err)
		},
	})
	if opts.UIDir != "" {
		ui, err := newUIHandler(opts.UIDir)
		if err != nil {
			return nil, err
		}
		engine.NoRoute(ui.serve)
	} else {
		engine.NoRoute(routeNotFound)
	}
	return engine, nil
}

// HealthCheck 报告进程正在服务请求。
func (s *server) HealthCheck(c *gin.Context) {
	respond.JSON(c.Writer, http.StatusOK, oapi.HealthStatus{Status: oapi.HealthStatusStatus("ok")})
}
